"""Direct broker contracts, not comparisons against a frozen implementation.

Status JSON hashes retain the established /v1 contract. To regenerate them for
an intentionally reviewed contract change (run from the checkout root)::

    python tests/broker_pure_oracle.py --status-hashes tmp/broker-status-hashes.json
    cp tmp/broker-status-hashes.json tests/broker_status_hashes.json

Review the changed cases before replacing that fixture. The standalone oracle
also captures old-tree/new-tree event and vehicle-state outcomes without keeping
an old implementation in the permanent test suite.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from projects.vehicle_data import broker as broker_module
from projects.vehicle_data.broker import ACTIVE_DRIVE_FAILURE_REASONS
from projects.vehicle_data.helper_events import HelperStatus, validate_helper_status
from projects.vehicle_data.models import AcquisitionResult
from projects.vehicle_data.vehicle_state import (
    accepts_state_observation, needs_current_authority,
    preservation_authority, qualified_engine_state,
)
from tests.broker_pure_oracle import (
    delivery_rows, encode, fixed_time, make_broker, status_cases, status_hashes,
)


class BrokerPureTests(unittest.TestCase):
    def test_status_keeps_copies_serialization_and_clocks_in_original_lock_scope(self):
        with fixed_time():
            broker = make_broker()
            broker._vehicle_state.update(basis="qualified_ccan_0x0fc_engine_speed")
            broker._vehicle_state_observed_monotonic = 99
            broker._cache["battery.voltage"] = broker.acquirer.acquire("passive")
            clock_locks = []
            copy_locks = []
            component_locks = []
            dumps = json.dumps

            def clock():
                clock_locks.append(broker._lock._is_owned())
                return 100.0

            def copy_json(*args, **kwargs):
                copy_locks.append(broker._lock._is_owned())
                return dumps(*args, **kwargs)

            def component_status():
                component_locks.append(broker._lock._is_owned())
                return {"state": "ready"}

            broker.monotonic = clock
            broker.usb_can_monitor = SimpleNamespace(status_snapshot=component_status)
            broker.engine_off_voltage_capture = SimpleNamespace(status_snapshot=component_status)
            broker.display_receiver = SimpleNamespace(status=component_status)
            with mock.patch.object(broker_module.json, "dumps", side_effect=copy_json):
                broker.status_response()
            # Nine broker copies plus AlignmentHistory.summary's JSON copy.
            self.assertEqual(copy_locks, [True] * 10)
            self.assertEqual(clock_locks, [True, True, True, False, False])
            self.assertEqual(component_locks, [False, False, False])

    def test_status_builders_do_not_mutate_snapshot(self):
        from dataclasses import asdict
        from projects.vehicle_data.status_view import build_interface_view, build_vehicle_state

        with fixed_time():
            for label, configure in status_cases():
                with self.subTest(case=label):
                    broker = make_broker()
                    configure(broker)
                    snapshot = broker._status_snapshot()
                    before = encode(asdict(snapshot))
                    build_interface_view(
                        snapshot, auxiliary_drive_enabled=broker.auxiliary_drive_enabled,
                        auxiliary_restoration_latched=broker._auxiliary_drive_restoration_latched,
                    )
                    build_vehicle_state(snapshot, age_ms=1000, ignition_stale=True, rpm_stale=False)
                    self.assertEqual(before, encode(asdict(snapshot)))

    def test_real_status_web_delivery_bytes_match_baseline(self):
        # Captured independently by running delivery_rows against bc94da0.
        data = (encode(delivery_rows()) + "\n").encode()
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            "1b340b2f73666b7b63c799a6531b0c1f447f534eed9e021850deb69e38e216f9",
        )

    def test_status_byte_baseline(self):
        expected = json.loads(Path(__file__).with_name("broker_status_hashes.json").read_text())
        self.assertEqual(
            expected, status_hashes(),
            "Status bytes changed; see this module's docstring for the reviewed regeneration command.",
        )


class HelperStatusContractTests(unittest.TestCase):
    def validate(self, bus, event):
        return validate_helper_status(event, bus=bus, failure_reasons=ACTIVE_DRIVE_FAILURE_REASONS)

    def reject(self, bus, event, message):
        with self.assertRaises(ValueError) as caught:
            self.validate(bus, event)
        self.assertEqual(str(caught.exception), message)

    def test_missing_state_is_inferred_only_for_ccan(self):
        for event_type, reason, mode, restored, state in (
            ("failure", "helper_failed", "listen_only", None, "failure"),
            ("final", "engine_not_running", "listen_only", True, "idle"),
            ("final", "restoration_failed", "armed_diagnostic", False, "restoration_failed"),
        ):
            with self.subTest(event_type=event_type, reason=reason):
                event = dict(type=event_type, reason=reason, detail="test", interface_mode=mode, restored=restored)
                self.assertEqual(self.validate("c-can", event), HelperStatus(state, reason, "test", mode))
                self.reject("b-can", event, "B-CAN auxiliary state must be a string")

    def test_restoration_failure_requires_false_only_for_ccan(self):
        event = dict(type="final", state="idle", reason="restoration_failed", detail="test", restored=True)
        self.reject("c-can", event, "restoration failure must carry restored=false")
        self.assertEqual(
            self.validate("b-can", event),
            HelperStatus("idle", "restoration_failed", "test", "listen_only"),
        )

    def test_unverified_armed_final_is_rejected_only_by_ccan(self):
        event = dict(type="final", state="idle", reason="helper_failed", detail="test", interface_mode="armed_diagnostic")
        self.reject("c-can", event, "unverified final cannot claim armed ownership")
        self.assertEqual(
            self.validate("b-can", event),
            HelperStatus("idle", "helper_failed", "test", "armed_diagnostic"),
        )

    def test_final_error_order_is_bus_specific(self):
        event = dict(type="final", state="idle", reason="invalid", detail="test", restored="invalid")
        self.reject("c-can", event, "active-drive final restored must be boolean or null")
        self.reject("b-can", event, "B-CAN auxiliary failure reason is not allowlisted")

    def test_both_buses_accept_verified_restoration_outcomes(self):
        for bus in ("c-can", "b-can"):
            for restored, state, reason, mode in (
                (True, "idle", "engine_not_running", "listen_only"),
                (False, "restoration_failed", "restoration_failed", "armed_diagnostic"),
            ):
                with self.subTest(bus=bus, restored=restored):
                    event = dict(type="final", restored=restored, state=state, reason=reason, detail="test", interface_mode=mode)
                    self.assertEqual(self.validate(bus, event), HelperStatus(state, reason, "test", mode))

    def test_restored_final_mode_errors_remain_bus_specific(self):
        event = dict(type="final", state="idle", reason="engine_not_running", detail="test", restored=True, interface_mode="armed_diagnostic")
        self.reject("c-can", event, "restored final must report listen_only mode")
        self.reject("b-can", event, "restored B-CAN final must report listen_only")


class VehicleStateContractTests(unittest.TestCase):
    def test_qualified_rpm_threshold_and_source(self):
        for value, state, running in ((0, "ignition_on", False), (399, "ignition_on", False), (400, "running", True)):
            with self.subTest(value=value):
                result = AcquisitionResult(metric="engine.rpm", available=True, unit="rpm", value=value, source="ccan.broadcast.0x0fc")
                self.assertEqual(qualified_engine_state(result), {
                    "state": state, "running": running, "confidence": "verified",
                    "basis": "qualified_ccan_0x0fc_engine_speed",
                    "detail": f"qualified passive 0x0FC engine speed is {value} rpm",
                })
        for source, value in (("other", 750), ("ccan.broadcast.0x0fc", True), ("ccan.broadcast.0x0fc", "750")):
            with self.subTest(source=source, value=value):
                result = AcquisitionResult(metric="engine.rpm", available=True, unit="rpm", value=value, source=source)
                self.assertIsNone(qualified_engine_state(result))

    def test_fresh_authority_precedence_and_solicited_voltage_exclusion(self):
        voltage = AcquisitionResult(metric="battery.voltage", available=True, unit="V", value=14)
        ignition = AcquisitionResult(metric="vehicle.ignition_on", available=True, unit="bool", value=True)
        for result, basis, expected in (
            (voltage, "qualified_ccan_0x0fc_engine_speed", ("engine.rpm", True)),
            (voltage, "ccan_0x2ef_ignition_gate", ("vehicle.ignition_on", True)),
            (voltage, "passive_bus_activity", ("", False)),
            (ignition, "qualified_ccan_0x0fc_engine_speed", ("engine.rpm", True)),
            (ignition, "passive_bus_activity", ("engine.rpm", False)),
        ):
            with self.subTest(metric=result.metric, basis=basis):
                self.assertTrue(accepts_state_observation(result))
                self.assertTrue(needs_current_authority(result))
                self.assertEqual(preservation_authority(result, basis), expected)
        self.assertFalse(accepts_state_observation(AcquisitionResult(
            metric="battery.voltage", available=True, unit="V", value=14,
            acquisition="physical_read_data_by_identifier",
        )))
