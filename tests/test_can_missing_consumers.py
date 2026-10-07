"""Fail-closed consumer regressions for unavailable CAN hardware state."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lib import dtc_batch
from projects.vehicle_data import cop_can_wake, display_receiver, drive_recorder
from projects.vehicle_data.can_availability import (
    CAN_ADAPTER_INITIALIZING,
    CAN_ADAPTER_MISSING,
    CAN_ADAPTER_RECOVERING,
    CAN_UNAVAILABLE_ENGINE,
)
from projects.vehicle_data.historian import HistorianConfig, TelemetryHistorian
from projects.vehicle_data.warning_chat import event_context
from tests.test_cop_can_wake import safe_status
from tests.test_dtc_batch import broker_status
from tests.test_vehicle_drive_recorder import ready_status
from tests.test_vehicle_historian import available, definition, snapshot, unavailable


LIFECYCLE_BASES = (
    CAN_ADAPTER_MISSING,
    CAN_ADAPTER_RECOVERING,
    CAN_ADAPTER_INITIALIZING,
)
UTC = timezone.utc


def unavailable_vehicle(basis: str) -> dict[str, object]:
    return {
        "state": "unknown",
        "running": None,
        "confidence": "unavailable",
        "basis": basis,
        "detail": "CAN adapter unavailable in fixture",
        "observed_at": "2026-10-07T12:00:00+00:00",
        "age_ms": 0,
    }


def with_unavailable_vehicle(payload, basis: str):
    payload["vehicle_state"] = unavailable_vehicle(basis)
    return payload


class SafetyConsumerTests(unittest.TestCase):
    def test_recorder_rejects_every_unavailable_lifecycle(self):
        for basis in LIFECYCLE_BASES:
            with self.subTest(basis=basis):
                status = with_unavailable_vehicle(ready_status(), basis)
                self.assertFalse(drive_recorder.broker_armed_ready(status))
                with self.assertRaises(drive_recorder.BrokerOwnershipLost):
                    drive_recorder.broker_c_can_route(status)

    def test_display_armed_route_inherits_recorder_rejection(self):
        for basis in LIFECYCLE_BASES:
            with self.subTest(basis=basis):
                status = with_unavailable_vehicle(ready_status(), basis)
                worker = display_receiver.DisplayReceiver(
                    mock.Mock(), status_reader=lambda: status, publish=mock.Mock()
                )
                self.assertTrue(worker.pause_passive())
                worker.start()
                try:
                    deadline = time.monotonic() + 1
                    receiver_status = worker.status()
                    while (
                        receiver_status["state"] != "waiting"
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                        receiver_status = worker.status()
                    self.assertEqual(receiver_status["state"], "waiting")
                    self.assertIn(
                        "broker does not prove an armed C-CAN route",
                        receiver_status["reason"],
                    )
                finally:
                    worker.stop()

    def test_cop_wake_rejects_every_unavailable_lifecycle(self):
        for basis in LIFECYCLE_BASES:
            with self.subTest(basis=basis):
                status = with_unavailable_vehicle(safe_status(), basis)
                self.assertTrue(cop_can_wake.broker_safety_conflicts(status))

    def test_dtc_initial_gate_rejects_every_unavailable_lifecycle(self):
        for basis in LIFECYCLE_BASES:
            with self.subTest(basis=basis):
                status = with_unavailable_vehicle(broker_status(), basis)
                with self.assertRaises(dtc_batch.VehicleGateError):
                    dtc_batch.validate_initial_broker_vehicle_state(status)

    def test_advisor_forwards_uncertainty_using_gets_only(self):
        calls = []
        vehicle = unavailable_vehicle(CAN_ADAPTER_MISSING)

        def request(method, path):
            calls.append((method, path))
            if path == "/v1/health":
                return 200, {
                    "available": True,
                    "active": [
                        {
                            "rule": "battery_watch",
                            "state": "watch",
                            "metric": "battery.voltage",
                        }
                    ],
                    "assessments": [],
                }
            if path == "/v1/snapshot":
                return 200, {
                    "status": {"vehicle_state": vehicle},
                    "metrics": {},
                    "catalog": [],
                }
            if path == "/v1/history":
                return 200, {"metric_trends": {}}
            raise AssertionError(path)

        client = mock.Mock()
        client.request.side_effect = request
        context = event_context(
            client, {"kind": "assessment", "id": "battery_watch"}
        )

        self.assertEqual(context["vehicle_state"], vehicle)
        self.assertTrue(calls)
        self.assertTrue(all(method == "GET" for method, _path in calls))


class HistorianUnavailableHardwareTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "history.sqlite3"
        self.start = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        self.rpm = definition("engine.rpm", "rpm")
        self.speed = definition("vehicle.speed", "mph")
        self.coolant = definition("engine.coolant_temperature", "°F")
        self.definitions = [self.rpm, self.speed, self.coolant]
        self.config = HistorianConfig(
            trip_idle_timeout_seconds=300, rollup_seconds=10
        )

    def active_values(self, at):
        return {
            "engine.rpm": available(self.rpm, 800.0, at),
            "vehicle.speed": available(self.speed, 12.0, at),
            "engine.coolant_temperature": available(self.coolant, 190.0, at),
        }

    def inactive_values(self):
        return {
            item["name"]: unavailable(item) for item in self.definitions
        }

    def hardware_snapshot(self, at, basis, *, active_metrics=True):
        values = self.active_values(at) if active_metrics else self.inactive_values()
        payload = snapshot(
            at, self.definitions, values, running=False
        )
        payload["status"]["vehicle_state"] = unavailable_vehicle(basis)
        return payload

    def test_recent_cached_motion_cannot_create_a_trip_or_running_regime(self):
        with TelemetryHistorian(self.path, config=self.config) as historian:
            result = historian.ingest_snapshot(
                self.hardware_snapshot(self.start, CAN_ADAPTER_MISSING),
                captured_at=self.start,
            )

            self.assertIsNone(result.trip_id)
            self.assertEqual(historian.list_trips(), [])
            self.assertEqual(
                result.regime,
                CAN_UNAVAILABLE_ENGINE + ":speed_unknown:rpm_unknown:warm",
            )

    def test_first_active_recovery_resumes_trip_beyond_idle_timeout(self):
        with TelemetryHistorian(self.path, config=self.config) as historian:
            first = historian.ingest_snapshot(
                snapshot(
                    self.start,
                    self.definitions,
                    self.active_values(self.start),
                ),
                captured_at=self.start,
            )
            missing_at = self.start + timedelta(seconds=301)
            missing = historian.ingest_snapshot(
                self.hardware_snapshot(missing_at, CAN_ADAPTER_MISSING),
                captured_at=missing_at,
            )
            before_recovery = historian.list_trips()[0]

            recovered_at = self.start + timedelta(seconds=602)
            recovered = historian.ingest_snapshot(
                snapshot(
                    recovered_at,
                    self.definitions,
                    self.active_values(recovered_at),
                ),
                captured_at=recovered_at,
            )

            self.assertEqual(missing.trip_id, first.trip_id)
            self.assertEqual(before_recovery["last_active_at"], self.start.isoformat())
            self.assertEqual(recovered.trip_id, first.trip_id)
            trips = historian.list_trips()
            self.assertEqual(len(trips), 1)
            self.assertEqual(trips[0]["state"], "open")
            self.assertEqual(trips[0]["last_active_at"], recovered_at.isoformat())

    def test_ambiguous_return_preserves_gap_until_later_activity(self):
        with TelemetryHistorian(self.path, config=self.config) as historian:
            first = historian.ingest_snapshot(
                snapshot(self.start, self.definitions, self.active_values(self.start)),
                captured_at=self.start,
            )
            lost_at = self.start + timedelta(seconds=301)
            historian.ingest_snapshot(
                self.hardware_snapshot(lost_at, CAN_ADAPTER_MISSING), captured_at=lost_at,
            )
            for second, state in ((400, "awake"), (500, "unknown"), (600, "awake")):
                at = self.start + timedelta(seconds=second)
                payload = snapshot(at, self.definitions, self.inactive_values(), running=False)
                payload["status"]["vehicle_state"]["state"] = state
                result = historian.ingest_snapshot(payload, captured_at=at)
                self.assertEqual(result.trip_id, first.trip_id)
                self.assertEqual(historian.list_trips()[0]["last_active_at"], self.start.isoformat())
            at = self.start + timedelta(seconds=700)
            resumed = historian.ingest_snapshot(
                snapshot(at, self.definitions, self.active_values(at)), captured_at=at,
            )
            self.assertEqual(resumed.trip_id, first.trip_id)
            self.assertEqual(len(historian.list_trips()), 1)

    def test_unresolved_gap_then_sleep_closes_without_bogus_trip(self):
        with TelemetryHistorian(self.path, config=self.config) as historian:
            first = historian.ingest_snapshot(
                snapshot(
                    self.start,
                    self.definitions,
                    self.active_values(self.start),
                ),
                captured_at=self.start,
            )
            for index, basis in enumerate(LIFECYCLE_BASES, start=1):
                at = self.start + timedelta(seconds=301 * index)
                held = historian.ingest_snapshot(
                    self.hardware_snapshot(at, basis), captured_at=at
                )
                self.assertEqual(held.trip_id, first.trip_id)
                self.assertEqual(
                    historian.list_trips()[0]["last_active_at"],
                    self.start.isoformat(),
                )

            asleep_at = self.start + timedelta(seconds=1_204)
            asleep_payload = snapshot(
                asleep_at, self.definitions, self.inactive_values(), running=False,
            )
            asleep_payload["status"]["vehicle_state"].update(
                state="asleep", running=False, confidence="inferred",
                basis="passive_bus_silence", age_ms=0,
            )
            asleep = historian.ingest_snapshot(asleep_payload, captured_at=asleep_at)
            self.assertIsNone(asleep.trip_id)
            completed = historian.list_trips()[0]
            self.assertEqual(completed["state"], "complete")
            self.assertEqual(completed["ended_at"], self.start.isoformat())

            running_at = asleep_at + timedelta(seconds=1)
            running = historian.ingest_snapshot(
                snapshot(
                    running_at,
                    self.definitions,
                    self.active_values(running_at),
                ),
                captured_at=running_at,
            )
            self.assertIsNotNone(running.trip_id)
            self.assertNotEqual(running.trip_id, first.trip_id)
            self.assertEqual(len(historian.list_trips()), 2)


if __name__ == "__main__":
    unittest.main()
