import contextlib
import http.client
import json
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from lib import canbus
from projects.battery import bcan_voltage, ccan_voltage
from projects.vehicle_data.api import (
    OBSERVATION_DEADLINE_HEADER,
    TelemetryApiHandler,
    TelemetryClient,
    UnixHTTPServer,
)
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.metrics import METRICS
from projects.vehicle_data.models import failure, success
from projects.vehicle_data.sources import DecodedVoltage, VoltageAcquirer
from projects.vehicle_data.web import (
    TelemetryWebHandler,
    TelemetryWebServer,
    validate_bind,
)


def interface(
    *,
    bitrate=500000,
    listen_only=True,
    state="ERROR-ACTIVE",
    up=True,
):
    return canbus.InterfaceState(
        channel="can0",
        present=True,
        up=up,
        bitrate=bitrate,
        listen_only=listen_only,
        controller_state=state,
        restart_ms=100,
    )


class FakeLocks:
    def __init__(self):
        self.observer_count = 0
        self.exclusive_count = 0
        self.handle = object()

    @contextlib.contextmanager
    def observer(self, _channel):
        self.observer_count += 1
        yield object()

    @contextlib.contextmanager
    def exclusive(self, _channel):
        self.exclusive_count += 1
        yield self.handle


class FakeBackend:
    def __init__(self, buses=("c-can",), states=None):
        self.buses = list(buses)
        self.states = list(states or (interface(),))
        self.read_count = 0
        self.wake_count = 0
        self.recorded = []
        self.topology = SimpleNamespace(
            bus="c-can",
            pair="6/14",
            source="test",
            usable=True,
            reason="",
        )
        self.inhibits = ()

    def interface_state(self, _channel):
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    def identify_bus(self, _channel, probe):
        del probe
        if len(self.buses) > 1:
            return self.buses.pop(0)
        return self.buses[0]

    def read_voltage(self, bus, _channel, timeout):
        del timeout
        self.read_count += 1
        return DecodedVoltage(
            value=12.62,
            source=(
                "bcan.broadcast.0x46c"
                if bus == "b-can"
                else "ccan.broadcast.0x41a"
            ),
            quality="verified",
            detail="fake approved broadcast",
        )

    def load_topology(self, _channel):
        return self.topology

    def active_inhibits(self, _channel):
        return self.inhibits

    def record_topology(self, _channel, bus):
        self.recorded.append(bus)

    def wake(self, bus, _channel, lock_handle, restore_state):
        self.wake_count += 1
        self.last_wake = (bus, lock_handle, restore_state)
        return True


class SourceTests(unittest.TestCase):
    def test_registry_is_metric_allowlist_with_provenance(self):
        self.assertEqual(
            set(METRICS),
            {
                "battery.voltage",
                "vehicle.ignition_on",
                "diagnostics.cluster.did.0107.raw",
                "diagnostics.cluster.did.1000.raw",
                "diagnostics.cluster.did.1002.raw",
                "diagnostics.cluster.did.1005.raw",
            },
        )
        sources = METRICS["battery.voltage"].sources
        self.assertEqual(
            {source.name for source in sources},
            {
                "bcan.broadcast.0x46c",
                "ccan.broadcast.0x41a",
                "cluster.did.1004",
            },
        )
        self.assertTrue(
            all(
                source.provenance
                for definition in METRICS.values()
                for source in definition.sources
            )
        )
        self.assertEqual(
            METRICS["diagnostics.cluster.did.1000.raw"].unit,
            "raw_u16_be",
        )
        self.assertEqual(
            METRICS["diagnostics.cluster.did.1002.raw"].unit,
            "raw_u8",
        )

    def test_passive_awake_read_takes_only_observer_lock(self):
        backend = FakeBackend()
        locks = FakeLocks()
        result = VoltageAcquirer(
            backend=backend,
            locks=locks,
            monotonic=lambda: 10.0,
        ).acquire("passive")

        self.assertTrue(result.available)
        self.assertEqual(result.acquisition, "passive")
        self.assertEqual(locks.observer_count, 1)
        self.assertEqual(locks.exclusive_count, 0)
        self.assertEqual(backend.wake_count, 0)

    def test_armed_interface_fails_before_probe_read_or_wake(self):
        backend = FakeBackend(states=(interface(listen_only=False),))
        result = VoltageAcquirer(
            backend=backend, locks=FakeLocks()
        ).acquire("wake_if_asleep")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, "can_busy")
        self.assertEqual(backend.read_count, 0)
        self.assertEqual(backend.wake_count, 0)

    def test_canch_never_wakes(self):
        backend = FakeBackend(buses=("can-ch",))
        result = VoltageAcquirer(
            backend=backend, locks=FakeLocks()
        ).acquire("wake_if_asleep")

        self.assertFalse(result.available)
        self.assertEqual(result.bus, "can-ch")
        self.assertEqual(backend.wake_count, 0)

    def test_silent_bus_requires_same_boot_topology(self):
        backend = FakeBackend(buses=("silent", "silent"))
        backend.topology = SimpleNamespace(
            bus="unknown",
            pair=None,
            source=None,
            usable=False,
            reason="boot id mismatch",
        )
        result = VoltageAcquirer(
            backend=backend, locks=FakeLocks()
        ).acquire("wake_if_asleep")

        self.assertEqual(result.reason, "unrecognized_bus")
        self.assertEqual(backend.wake_count, 0)

    def test_inhibit_blocks_silent_wake(self):
        backend = FakeBackend(buses=("silent", "silent"))
        backend.inhibits = ({"name": "alfaobd"},)
        result = VoltageAcquirer(
            backend=backend, locks=FakeLocks()
        ).acquire("wake_if_asleep")

        self.assertEqual(result.reason, "can_busy")
        self.assertIn("alfaobd", result.detail)
        self.assertEqual(backend.wake_count, 0)

    def test_wake_is_rechecked_and_exact_interface_is_restored(self):
        backend = FakeBackend(
            buses=("silent", "silent", "silent", "c-can"),
            states=(interface(), interface(), interface()),
        )
        locks = FakeLocks()
        result = VoltageAcquirer(
            backend=backend, locks=locks, monotonic=lambda: 10.0
        ).acquire("wake_if_asleep")

        self.assertTrue(result.available)
        self.assertEqual(result.acquisition, "wake_assisted")
        self.assertEqual(backend.wake_count, 1)
        self.assertEqual(
            backend.last_wake, ("c-can", locks.handle, interface())
        )

    def test_post_wake_state_change_overrides_success(self):
        backend = FakeBackend(
            buses=("silent", "silent", "silent", "c-can"),
            states=(
                interface(),
                interface(),
                interface(),
                interface(listen_only=False),
            ),
        )
        result = VoltageAcquirer(
            backend=backend, locks=FakeLocks()
        ).acquire("wake_if_asleep")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, "restoration_failed")


class ReplaySocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.bound = None
        self.closed = False

    def setsockopt(self, *_args):
        return None

    def bind(self, address):
        self.bound = address

    def settimeout(self, _timeout):
        return None

    def recv(self, _size):
        return self.frames.pop(0)

    def close(self):
        self.closed = True


class BroadcastReplayTests(unittest.TestCase):
    def test_bcan_0x46c_replay_masks_flags_and_decodes_verified_scale(self):
        data = bytes((0, 0, 0, 0, 0x53, 0x88, 0, 0))
        frame = struct.pack("=IB3x8s", 0x46C, 8, data)
        replay = ReplaySocket([frame] * 7)
        with (
            mock.patch.multiple(
                bcan_voltage.socket,
                AF_CAN=29,
                SOCK_RAW=3,
                CAN_RAW=1,
                SOL_CAN_RAW=101,
                CAN_RAW_FILTER=1,
                create=True,
            ),
            mock.patch.object(
                bcan_voltage.socket, "socket", return_value=replay
            ),
        ):
            volts, detail = bcan_voltage.read_voltage(
                "can9", timeout=0.1
            )

        self.assertEqual(volts, 12.5)
        self.assertIn("7 frames", detail)
        self.assertEqual(replay.bound, ("can9",))
        self.assertTrue(replay.closed)

    def test_ccan_0x41a_replay_decodes_verified_affine_scale(self):
        for raw, expected in ((0xBE, 13.5), (0xB0, 12.8), (0xAE, 12.7)):
            with self.subTest(raw=raw):
                data = bytes((raw, 0, 0, 0, 0, 0, 0, 0))
                frame = struct.pack("=IB3x8s", 0x41A, 8, data)
                replay = ReplaySocket([frame] * 15)
                with (
                    mock.patch.multiple(
                        ccan_voltage.socket,
                        AF_CAN=29,
                        SOCK_RAW=3,
                        CAN_RAW=1,
                        SOL_CAN_RAW=101,
                        CAN_RAW_FILTER=1,
                        create=True,
                    ),
                    mock.patch.object(
                        ccan_voltage.socket, "socket", return_value=replay
                    ),
                ):
                    volts, detail = ccan_voltage.read_voltage(
                        "can9", timeout=0.1
                    )

                self.assertEqual(volts, expected)
                self.assertIn("0x41A", detail)
                self.assertIn("verified affine", detail)
                self.assertEqual(replay.bound, ("can9",))
                self.assertTrue(replay.closed)

    def test_ccan_0x2ef_payload_is_not_a_voltage_source(self):
        self.assertIsNone(
            ccan_voltage._decode(
                0x2EF, bytes((0x88, 0x13, 0, 0, 0, 0, 0, 0))
            )
        )


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeAcquirer:
    channel = "can0"

    def __init__(self, *, block=None, result=None):
        self.calls = []
        self.block = block
        self.result = result

    def acquire(self, mode):
        self.calls.append(mode)
        if self.block:
            self.block.wait()
        if self.result is not None:
            return self.result
        return success(
            metric="battery.voltage",
            unit="V",
            value=12.5,
            source="bcan.broadcast.0x46c",
            bus="b-can",
            acquisition=mode,
            quality="verified",
            observed_monotonic=100.0,
            observed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )

    def status_snapshot(self):
        return {
            "channel": "can0",
            "adapter_present": True,
            "up": True,
            "bitrate": 125000,
            "listen_only": True,
            "controller_state": "ERROR-ACTIVE",
            "topology": {
                "bus": "b-can",
                "usable": True,
                "reason": "",
            },
            "active_inhibits": [],
        }


class FakeAutoRetuner:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "state": "switched",
            "reason": "bus_identified",
            "detail": "passively switched to b-can at 125000 bit/s",
            "from_bitrate": 500000,
            "to_bitrate": 125000,
            "bus": "b-can",
            "completed_at": "2026-07-26T00:00:00+00:00",
        }

    def attempt(self, expected_bitrate):
        self.calls.append(expected_bitrate)
        return dict(self.result)


class BrokerTests(unittest.TestCase):
    def test_cache_get_never_calls_acquirer(self):
        acquirer = FakeAcquirer()
        broker = TelemetryBroker(acquirer=acquirer, monotonic=FakeClock())

        self.assertEqual(
            broker.metric_response("battery.voltage")["reason"], "stale"
        )
        broker.status_response()
        broker.list_metrics()
        snapshot = broker.snapshot_response()
        self.assertEqual(acquirer.calls, [])
        self.assertEqual(
            [item["name"] for item in snapshot["catalog"]],
            sorted(METRICS),
        )
        self.assertEqual(
            snapshot["metrics"]["battery.voltage"]["reason"], "stale"
        )

    def test_acquisition_caches_source_metadata_and_then_rate_limits(self):
        clock = FakeClock()
        acquirer = FakeAcquirer()
        broker = TelemetryBroker(acquirer=acquirer, monotonic=clock)

        first = broker.acquire("battery.voltage", "passive")
        second = broker.acquire("battery.voltage", "passive")
        payload = broker.metric_response("battery.voltage")

        self.assertTrue(first.available)
        self.assertEqual(second.reason, "rate_limited")
        self.assertEqual(payload["source"], "bcan.broadcast.0x46c")
        self.assertEqual(payload["quality"], "verified")
        self.assertEqual(acquirer.calls, ["passive"])

    def test_passive_activity_reports_awake_without_guessing_running(self):
        clock = FakeClock()
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(), monotonic=clock
        )

        broker.acquire("battery.voltage", "passive")
        state = broker.status_response()["vehicle_state"]

        self.assertEqual(state["state"], "awake")
        self.assertIsNone(state["running"])
        self.assertEqual(state["basis"], "passive_bus_activity")
        self.assertEqual(state["age_ms"], 0)
        self.assertIn("not yet distinguished", state["detail"])

    def test_passive_silence_reports_inferred_asleep_with_caveat(self):
        asleep = failure(
            metric="battery.voltage",
            unit="V",
            reason="bus_asleep",
            detail="passive bus identification returned silent",
            bus="silent",
            acquisition="passive",
        )
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(result=asleep),
            monotonic=FakeClock(),
        )

        broker.acquire("battery.voltage", "passive")
        state = broker.status_response()["vehicle_state"]

        self.assertEqual(state["state"], "asleep")
        self.assertFalse(state["running"])
        self.assertEqual(state["confidence"], "inferred")
        self.assertIn("unplugged", state["detail"])

    def test_wake_assisted_read_is_not_reported_as_running_evidence(self):
        wake_result = success(
            metric="battery.voltage",
            unit="V",
            value=12.5,
            source="ccan.broadcast.0x41a",
            bus="c-can",
            acquisition="wake_assisted",
            quality="verified",
            observed_monotonic=100.0,
        )
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(result=wake_result),
            monotonic=FakeClock(),
        )

        broker.acquire("battery.voltage", "wake_if_asleep")
        state = broker.status_response()["vehicle_state"]

        self.assertEqual(state["state"], "awake")
        self.assertIsNone(state["running"])
        self.assertEqual(state["basis"], "broker_wake_activity")
        self.assertIn("not evidence", state["detail"])

    def test_wrong_rate_activity_never_claims_running_state(self):
        wrong_rate = failure(
            metric="battery.voltage",
            unit="V",
            reason="wrong_bus",
            detail="passive bus identification returned wrong-rate",
            bus="wrong-rate",
            acquisition="passive",
        )
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(result=wrong_rate),
            monotonic=FakeClock(),
        )

        broker.acquire("battery.voltage", "passive")
        state = broker.status_response()["vehicle_state"]

        self.assertEqual(state["state"], "awake")
        self.assertIsNone(state["running"])
        self.assertEqual(state["basis"], "wrong_rate_rx_activity")

    def test_unknown_metric_never_reaches_source(self):
        acquirer = FakeAcquirer()
        broker = TelemetryBroker(acquirer=acquirer)
        result = broker.acquire("raw.did", "passive")
        self.assertEqual(result.reason, "unknown_metric")
        self.assertEqual(acquirer.calls, [])

    def test_publisher_stamps_receipt_and_caches_exact_allowlisted_source(self):
        clock = FakeClock()
        broker = TelemetryBroker(acquirer=FakeAcquirer(), monotonic=clock)

        result = broker.publish_observation(
            "battery.voltage",
            value=12.4,
            unit="V",
            source="cluster.did.1004",
            bus="c-can",
            quality="observed_alfa_scale",
        )
        clock.value = 102.5
        payload = broker.metric_response("battery.voltage")

        self.assertTrue(result.available)
        self.assertEqual(result.observed_monotonic, 100.0)
        self.assertEqual(payload["age_ms"], 2500)
        self.assertEqual(payload["source"], "cluster.did.1004")
        self.assertEqual(
            payload["acquisition"], "physical_read_data_by_identifier"
        )
        self.assertEqual(
            broker.status_response()["vehicle_state"]["state"], "unknown"
        )

    def test_publisher_success_does_not_erase_acquisition_failure(self):
        acquisition_failure = failure(
            metric="battery.voltage",
            unit="V",
            reason="restoration_failed",
            detail="listen-only restoration could not be proven",
        )
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(result=acquisition_failure),
            monotonic=FakeClock(),
        )

        failed = broker.acquire("battery.voltage", "passive")
        published = broker.publish_observation(
            "battery.voltage",
            value=12.4,
            unit="V",
            source="cluster.did.1004",
            bus="c-can",
            quality="observed_alfa_scale",
        )

        self.assertEqual(failed.reason, "restoration_failed")
        self.assertTrue(published.available)
        cached = broker.metric_response("battery.voltage")
        self.assertEqual(
            cached["last_acquisition_error"]["reason"],
            "restoration_failed",
        )
        self.assertEqual(
            broker.status_response()["last_acquisition_errors"][
                "battery.voltage"
            ]["reason"],
            "restoration_failed",
        )

    def test_publisher_rejects_source_metadata_type_and_range_mismatches(self):
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(), monotonic=FakeClock()
        )
        valid = {
            "value": 12.4,
            "unit": "V",
            "source": "cluster.did.1004",
            "bus": "c-can",
            "quality": "observed_alfa_scale",
        }
        changes = (
            ({"unit": "mV"}, "unit"),
            ({"source": "cluster.did.ffff"}, "source"),
            ({"bus": "b-can"}, "bus"),
            ({"quality": "verified"}, "quality"),
            ({"value": True}, "registry type number"),
            ({"value": 99.0}, "above maximum"),
        )

        for change, expected in changes:
            with self.subTest(change=change):
                result = broker.publish_observation(
                    "battery.voltage", **{**valid, **change}
                )
                self.assertFalse(result.available)
                self.assertIn(expected, result.detail)
        self.assertEqual(
            broker.metric_response("battery.voltage")["reason"], "stale"
        )

    def test_huge_integer_fails_range_validation_without_sticking_inflight(self):
        huge = success(
            metric="battery.voltage",
            unit="V",
            value=10**309,
            source="bcan.broadcast.0x46c",
            bus="b-can",
            acquisition="passive_broadcast",
            quality="verified",
            observed_monotonic=100.0,
        )
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(result=huge),
            monotonic=FakeClock(),
        )

        result = broker.acquire("battery.voltage", "passive")

        self.assertEqual(result.reason, "invalid_source_result")
        self.assertIn("value", result.detail)
        self.assertEqual(broker.status_response()["inflight"], [])

    def test_in_process_broadcast_source_cannot_be_spoofed_by_publisher(self):
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(), monotonic=FakeClock()
        )

        result = broker.publish_observation(
            "battery.voltage",
            value=12.4,
            unit="V",
            source="ccan.broadcast.0x41a",
            bus="c-can",
            quality="verified",
        )

        self.assertEqual(result.reason, "source_not_publishable")

    def test_publisher_accepts_typed_ignition_and_raw_diagnostic_values(self):
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(), monotonic=FakeClock()
        )

        ignition = broker.publish_observation(
            "vehicle.ignition_on",
            value=True,
            unit="boolean",
            source="ccan.broadcast.0x2ef",
            bus="c-can",
            quality="verified",
        )
        raw = broker.publish_observation(
            "diagnostics.cluster.did.1000.raw",
            value=2048,
            unit="raw_u16_be",
            source="cluster.did.1000",
            bus="c-can",
            quality="candidate",
        )

        self.assertTrue(ignition.available)
        self.assertIs(ignition.value, True)
        self.assertTrue(raw.available)
        self.assertEqual(raw.value, 2048)
        state = broker.status_response()["vehicle_state"]
        self.assertEqual(state["state"], "ignition_on")
        self.assertEqual(state["basis"], "ccan_0x2ef_ignition_gate")

        broker.monotonic.value = 104.0
        stale_state = broker.status_response()["vehicle_state"]
        self.assertEqual(stale_state["state"], "unknown")
        self.assertEqual(stale_state["confidence"], "stale")
        self.assertEqual(
            stale_state["basis"], "stale_ccan_0x2ef_ignition_gate"
        )

    def test_ignition_presence_publisher_rejects_false_without_changing_state(self):
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(), monotonic=FakeClock()
        )
        valid = {
            "unit": "boolean",
            "source": "ccan.broadcast.0x2ef",
            "bus": "c-can",
            "quality": "verified",
        }

        present = broker.publish_observation(
            "vehicle.ignition_on", value=True, **valid
        )
        absent = broker.publish_observation(
            "vehicle.ignition_on", value=False, **valid
        )

        self.assertTrue(present.available)
        self.assertFalse(absent.available)
        self.assertEqual(absent.reason, "invalid_observation")
        self.assertIn("not permitted", absent.detail)
        cached = broker.metric_response("vehicle.ignition_on")
        self.assertTrue(cached["available"])
        self.assertIs(cached["value"], True)
        state = broker.status_response()["vehicle_state"]
        self.assertEqual(state["state"], "ignition_on")
        self.assertNotEqual(state["state"], "parked")

    def test_publisher_only_metric_never_reaches_battery_acquirer(self):
        acquirer = FakeAcquirer()
        broker = TelemetryBroker(acquirer=acquirer)

        result = broker.acquire("vehicle.ignition_on", "passive")

        self.assertEqual(result.reason, "unsupported_mode")
        self.assertEqual(acquirer.calls, [])

    def test_misrouted_acquirer_result_cannot_cross_cache(self):
        misrouted = success(
            metric="vehicle.ignition_on",
            unit="V",
            value=12.5,
            source="bcan.broadcast.0x46c",
            bus="b-can",
            acquisition="passive",
            quality="verified",
            observed_monotonic=100.0,
        )
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(result=misrouted),
            monotonic=FakeClock(),
        )

        result = broker.acquire("battery.voltage", "passive")

        self.assertEqual(result.reason, "invalid_source_result")
        self.assertEqual(
            broker.metric_response("battery.voltage")["reason"],
            "invalid_source_result",
        )
        self.assertEqual(
            broker.metric_response("vehicle.ignition_on")["reason"], "stale"
        )

    def test_busy_failure_is_visible_as_owner_and_blocks_permission(self):
        acquirer = FakeAcquirer()
        broker = TelemetryBroker(acquirer=acquirer, monotonic=FakeClock())
        broker._interface_status = acquirer.status_snapshot()
        broker._last_error["battery.voltage"] = failure(
            metric="battery.voltage",
            unit="V",
            reason="can_busy",
            detail="another participating CAN operation owns can0",
        )

        status = broker.status_response()
        self.assertFalse(status["active_acquisition_permitted"])
        self.assertEqual(
            status["current_owner"]["kind"],
            "participating_or_external_can_user",
        )

    def test_auto_retune_requires_repeated_wrong_rate_then_reports_switch(self):
        acquirer = FakeAcquirer()
        retuner = FakeAutoRetuner()
        broker = TelemetryBroker(
            acquirer=acquirer,
            monotonic=FakeClock(),
            auto_retuner=retuner,
            auto_retune_trigger=3,
        )
        broker._interface_status = {
            **acquirer.status_snapshot(),
            "bitrate": 500000,
            "topology": {
                "bus": "c-can",
                "usable": True,
                "reason": "",
            },
        }
        wrong_rate = failure(
            metric="battery.voltage",
            unit="V",
            reason="wrong_bus",
            detail="passive bus identification returned wrong-rate",
            bus="wrong-rate",
            acquisition="passive",
        )

        broker._consider_auto_retune(wrong_rate)
        broker._consider_auto_retune(wrong_rate)
        self.assertEqual(retuner.calls, [])
        broker._consider_auto_retune(wrong_rate)

        status = broker.status_response()["auto_retune"]
        self.assertEqual(retuner.calls, [500000])
        self.assertEqual(status["state"], "switched")
        self.assertEqual(status["wrong_rate_streak"], 0)
        self.assertEqual(status["last_attempt"]["bus"], "b-can")

    def test_auto_retune_reports_external_inhibit_without_calling_helper(self):
        acquirer = FakeAcquirer()
        retuner = FakeAutoRetuner()
        broker = TelemetryBroker(
            acquirer=acquirer,
            monotonic=FakeClock(),
            auto_retuner=retuner,
            auto_retune_trigger=1,
        )
        broker._interface_status = {
            **acquirer.status_snapshot(),
            "bitrate": 500000,
            "active_inhibits": ["alfaobd"],
        }
        wrong_rate = failure(
            metric="battery.voltage",
            unit="V",
            reason="wrong_bus",
            detail="passive bus identification returned wrong-rate",
            bus="wrong-rate",
        )

        broker._consider_auto_retune(wrong_rate)

        status = broker.status_response()["auto_retune"]
        self.assertEqual(retuner.calls, [])
        self.assertEqual(status["state"], "blocked")
        self.assertIn("alfaobd", status["detail"])

    def test_auto_retune_continues_streak_if_wrong_rate_degrades_controller(self):
        acquirer = FakeAcquirer()
        retuner = FakeAutoRetuner()
        broker = TelemetryBroker(
            acquirer=acquirer,
            monotonic=FakeClock(),
            auto_retuner=retuner,
            auto_retune_trigger=3,
        )
        broker._interface_status = {
            **acquirer.status_snapshot(),
            "bitrate": 500000,
            "controller_state": "ERROR-PASSIVE",
        }
        wrong_rate = failure(
            metric="battery.voltage",
            unit="V",
            reason="wrong_bus",
            detail="passive bus identification returned wrong-rate",
            bus="wrong-rate",
        )
        degraded = failure(
            metric="battery.voltage",
            unit="V",
            reason="source_unavailable",
            detail="can0 controller is ERROR-PASSIVE; left unchanged",
        )

        broker._consider_auto_retune(wrong_rate)
        broker._consider_auto_retune(degraded)
        broker._consider_auto_retune(degraded)

        self.assertEqual(retuner.calls, [500000])
        self.assertEqual(
            broker.status_response()["auto_retune"]["state"], "switched"
        )

    def test_auto_retune_cooldown_reports_why_retry_is_deferred(self):
        clock = FakeClock()
        acquirer = FakeAcquirer()
        retuner = FakeAutoRetuner(
            {
                "state": "failed",
                "reason": "alternate_not_identified",
                "detail": "alternate passive probe returned unknown",
                "from_bitrate": 500000,
                "to_bitrate": 125000,
                "bus": "unknown",
                "completed_at": "2026-07-26T00:00:00+00:00",
            }
        )
        broker = TelemetryBroker(
            acquirer=acquirer,
            monotonic=clock,
            auto_retuner=retuner,
            auto_retune_trigger=1,
            auto_retune_cooldown_seconds=30,
        )
        broker._interface_status = {
            **acquirer.status_snapshot(),
            "bitrate": 500000,
        }
        wrong_rate = failure(
            metric="battery.voltage",
            unit="V",
            reason="wrong_bus",
            detail="passive bus identification returned wrong-rate",
            bus="wrong-rate",
        )

        broker._consider_auto_retune(wrong_rate)
        broker._consider_auto_retune(wrong_rate)

        status = broker.status_response()["auto_retune"]
        self.assertEqual(retuner.calls, [500000])
        self.assertEqual(status["state"], "cooldown")
        self.assertIn("retry in 30.0 seconds", status["detail"])
        self.assertEqual(status["cooldown_remaining_seconds"], 30.0)

    def test_auto_retune_reports_armed_interface_as_blocked(self):
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(),
            monotonic=FakeClock(),
            auto_retuner=FakeAutoRetuner(),
        )
        broker._consider_auto_retune(
            failure(
                metric="battery.voltage",
                unit="V",
                reason="can_busy",
                detail="can0 is armed; refusing to touch another CAN operation",
            )
        )

        status = broker.status_response()["auto_retune"]
        self.assertEqual(status["state"], "blocked")
        self.assertIn("armed", status["detail"])

    def test_concurrent_identical_requests_are_coalesced(self):
        release = threading.Event()
        acquirer = FakeAcquirer(block=release)
        broker = TelemetryBroker(
            acquirer=acquirer,
            monotonic=FakeClock(),
            acquisition_wait_seconds=2.0,
        )
        results = []
        first = threading.Thread(
            target=lambda: results.append(
                broker.acquire("battery.voltage", "wake_if_asleep")
            )
        )
        second = threading.Thread(
            target=lambda: results.append(
                broker.acquire("battery.voltage", "wake_if_asleep")
            )
        )
        first.start()
        while not acquirer.calls:
            time.sleep(0.005)
        second.start()
        release.set()
        first.join()
        second.join()

        self.assertEqual(acquirer.calls, ["wake_if_asleep"])
        self.assertEqual(sum(result.coalesced for result in results), 1)

    def test_passive_collector_and_active_request_share_one_source_lane(self):
        release = threading.Event()
        acquirer = FakeAcquirer(block=release)
        broker = TelemetryBroker(
            acquirer=acquirer,
            monotonic=FakeClock(),
            acquisition_wait_seconds=2.0,
        )
        results = []
        active = threading.Thread(
            target=lambda: results.append(
                broker.acquire("battery.voltage", "wake_if_asleep")
            )
        )
        passive = threading.Thread(
            target=lambda: results.append(
                broker.acquire("battery.voltage", "passive")
            )
        )
        active.start()
        while not acquirer.calls:
            time.sleep(0.005)
        passive.start()
        time.sleep(0.05)
        self.assertEqual(acquirer.calls, ["wake_if_asleep"])
        release.set()
        active.join()
        passive.join()

        self.assertEqual(
            acquirer.calls, ["wake_if_asleep", "passive"]
        )
        self.assertTrue(all(result.available for result in results))


class ApiTests(unittest.TestCase):
    def setUp(self):
        # AF_UNIX path limits are small on macOS; the compute checkout itself
        # can be deeply nested, so deliberately place sockets under /tmp.
        self.tmp = tempfile.TemporaryDirectory(prefix="vt-api-", dir="/tmp")
        self.socket_path = str(pathlib.Path(self.tmp.name) / "api.sock")
        self.acquirer = FakeAcquirer()
        self.broker = TelemetryBroker(
            acquirer=self.acquirer, monotonic=FakeClock()
        )
        self.server = UnixHTTPServer(self.socket_path, self.broker)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = TelemetryClient(self.socket_path)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_get_is_cache_only_and_post_is_allowlisted(self):
        status, snapshot = self.client.request("GET", "/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(
            snapshot["metrics"]["battery.voltage"]["reason"], "stale"
        )
        self.assertEqual(
            snapshot["status"]["vehicle_state"]["state"], "unknown"
        )
        self.assertEqual(self.acquirer.calls, [])

        status, payload = self.client.request(
            "GET", "/v1/metrics/battery.voltage"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["reason"], "stale")
        self.assertEqual(self.acquirer.calls, [])

        status, payload = self.client.request(
            "POST",
            "/v1/acquisitions/battery.voltage",
            {"mode": "passive"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(self.acquirer.calls, ["passive"])

        status, payload = self.client.request(
            "POST", "/v1/acquisitions/raw.did", {"mode": "passive"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["reason"], "unknown_metric")

    def test_unix_only_observation_post_uses_strict_contract(self):
        status, payload = self.client.publish(
            "battery.voltage",
            value=12.3,
            unit="V",
            source="cluster.did.1004",
            bus="c-can",
            quality="observed_alfa_scale",
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["value"], 12.3)
        self.assertEqual(self.acquirer.calls, [])

        status, payload = self.client.request(
            "POST",
            "/v1/observations/battery.voltage",
            {
                "value": 12.3,
                "unit": "V",
                "source": "cluster.did.1004",
                "bus": "c-can",
                "quality": "observed_alfa_scale",
                "observed_monotonic": -1,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason"], "invalid_request")

        status, payload = self.client.request(
            "POST",
            "/v1/observations/vehicle.ignition_on",
            {
                "value": True,
                "unit": "boolean",
                "source": "ccan.broadcast.0x2ef",
                "bus": "c-can",
                "quality": "verified",
            },
            headers={
                OBSERVATION_DEADLINE_HEADER: (
                    f"{time.monotonic() - 1.0:.9f}"
                )
            },
        )
        self.assertEqual(status, 408)
        self.assertEqual(payload["reason"], "observation_expired")
        self.assertEqual(
            self.broker.metric_response("vehicle.ignition_on")["reason"],
            "stale",
        )

        status, payload = self.client.publish(
            "vehicle.ignition_on",
            value=True,
            unit="boolean",
            source="ccan.broadcast.0x2ef",
            bus="c-can",
            quality="verified",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])

        status, payload = self.client.publish(
            "vehicle.ignition_on",
            value=False,
            unit="boolean",
            source="ccan.broadcast.0x2ef",
            bus="c-can",
            quality="verified",
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "invalid_observation")
        self.assertIn("not permitted", payload["detail"])
        self.assertEqual(
            self.broker.status_response()["vehicle_state"]["state"],
            "ignition_on",
        )

    def test_disconnected_client_does_not_escape_json_writer(self):
        handler = object.__new__(TelemetryApiHandler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = SimpleNamespace(
            write=mock.Mock(side_effect=BrokenPipeError)
        )

        handler._json(200, {"available": False})
        handler.wfile.write.assert_called_once()

        handler.end_headers.side_effect = ConnectionResetError
        handler.wfile.write.reset_mock()
        handler._json(200, {"available": False})
        handler.wfile.write.assert_not_called()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vt-web-", dir="/tmp")
        socket_path = str(pathlib.Path(self.tmp.name) / "api.sock")
        self.acquirer = FakeAcquirer()
        broker = TelemetryBroker(
            acquirer=self.acquirer, monotonic=FakeClock()
        )
        self.api = UnixHTTPServer(socket_path, broker)
        self.api_thread = threading.Thread(target=self.api.serve_forever)
        self.api_thread.start()
        self.web = TelemetryWebServer(
            ("127.0.0.1", 0),
            socket_path=socket_path,
            allow_acquisitions=False,
            stream_interval_seconds=0.05,
            stream_max_seconds=0.1,
        )
        self.web_thread = threading.Thread(target=self.web.serve_forever)
        self.web_thread.start()

    def tearDown(self):
        self.web.shutdown()
        self.web.server_close()
        self.web_thread.join()
        self.api.shutdown()
        self.api.server_close()
        self.api_thread.join()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.web.server_port, timeout=2
        )
        headers = {}
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, raw

    def test_web_gets_are_cache_only_and_posts_default_closed(self):
        status, raw = self.request("GET", "/v1/snapshot")
        self.assertEqual(status, 200)
        snapshot = json.loads(raw)
        self.assertEqual(
            snapshot["metrics"]["battery.voltage"]["reason"], "stale"
        )
        self.assertFalse(
            snapshot["web"]["active_acquisition_enabled"]
        )

        status, raw = self.request(
            "GET", "/v1/metrics/battery.voltage"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["reason"], "stale")

        status, raw = self.request(
            "POST",
            "/v1/acquisitions/battery.voltage",
            {"mode": "wake_if_asleep"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(
            json.loads(raw)["reason"], "web_acquisition_disabled"
        )
        self.assertEqual(self.acquirer.calls, [])

        status, raw = self.request(
            "POST",
            "/v1/observations/vehicle.ignition_on",
            {
                "value": True,
                "unit": "boolean",
                "source": "ccan.broadcast.0x2ef",
                "bus": "c-can",
                "quality": "verified",
            },
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw)["reason"], "not_found")

    def test_snapshot_delivery_metadata_orders_http_and_stream_payloads(self):
        status, raw = self.request("GET", "/v1/snapshot")
        self.assertEqual(status, 200)
        first = json.loads(raw)["web_delivery"]
        self.assertEqual(
            set(first),
            {
                "instance_id",
                "sequence",
                "generated_at_ms",
                "generated_monotonic_ms",
            },
        )
        self.assertTrue(first["instance_id"])
        self.assertGreater(first["generated_at_ms"], 0)
        self.assertGreaterEqual(first["generated_monotonic_ms"], 0)

        status, raw = self.request("GET", "/v1/snapshot?fresh=test")
        self.assertEqual(status, 200)
        second = json.loads(raw)["web_delivery"]
        self.assertEqual(second["instance_id"], first["instance_id"])
        self.assertGreater(second["sequence"], first["sequence"])
        self.assertGreaterEqual(
            second["generated_monotonic_ms"],
            first["generated_monotonic_ms"],
        )

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.web.server_port, timeout=2
        )
        connection.request("GET", "/v1/stream")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        lines = []
        while True:
            line = response.fp.readline()
            if not line or line in (b"\n", b"\r\n"):
                break
            lines.append(line.decode().rstrip())
        connection.close()

        event_id = next(
            line.removeprefix("id: ")
            for line in lines
            if line.startswith("id: ")
        )
        data = json.loads(
            next(
                line.removeprefix("data: ")
                for line in lines
                if line.startswith("data: ")
            )
        )
        streamed = data["web_delivery"]
        self.assertEqual(streamed["instance_id"], first["instance_id"])
        self.assertGreater(streamed["sequence"], second["sequence"])
        self.assertEqual(
            event_id,
            f"{streamed['instance_id']}:{streamed['sequence']}",
        )
        self.assertEqual(self.acquirer.calls, [])

    def test_dashboard_assets_are_served_with_csp(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.web.server_port, timeout=2
        )
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200)
        self.assertIn("default-src", response.getheader("Content-Security-Policy"))
        self.assertIn(b"Van telemetry", body)
        self.assertIn(b"Drive essentials", body)
        self.assertIn(b"Engine health", body)
        self.assertIn(b"OIL PRESSURE", body)
        self.assertIn(b"COOLANT", body)
        self.assertIn(b"CRANK TORQUE", body)
        self.assertIn(b"Tire pressure", body)
        self.assertIn(b"Only fresh, driver-qualified values", body)
        self.assertIn(b"Automatic bus switch", body)
        self.assertIn(b"Customize this device", body)
        self.assertIn(b"Loading metric catalog", body)
        self.assertNotIn(b"Not yet allowlisted", body)
        self.assertNotIn(b"ALLOWLISTED TELEMETRY", body)
        connection.close()

        status, profiles = self.request("GET", "/profiles.js")
        self.assertEqual(status, 200)
        self.assertIn(b"localStorage", profiles)
        self.assertIn(b"automaticProfile", profiles)
        self.assertIn(b'"drive"', profiles)
        self.assertIn(b'"engine"', profiles)
        self.assertIn(b'"tires"', profiles)

        status, app = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"DRIVER_QUALITIES", app)
        self.assertIn(b"MAX_STATE_FALLBACK_AGE_MS", app)
        self.assertIn(b"diagnostics only", app)
        self.assertIn(b"vehicle.ignition_on", app)
        self.assertIn(b"MAX_STREAM_DELIVERY_AGE_MS", app)
        self.assertIn(b"queued_stream_event", app)
        self.assertIn(b'cache: "no-store"', app)
        self.assertIn(b'eventStream.close()', app)
        self.assertIn(b'"visibilitychange"', app)
        self.assertIn(b'"pageshow"', app)
        self.assertIn(b"ALFA SCALE means", app)
        self.assertIn(b"ENGINE_HEALTH_METRICS", app)
        self.assertIn(b"Mapping pending", app)
        self.assertIn(b"/4 MAPPED", app)
        self.assertNotIn(b"card.hidden = !definition", app)
        self.assertNotIn(b'byId("tire-grid").hidden = registered === 0', app)
        self.assertNotIn(b"Not yet allowlisted", app)
        self.assertNotIn(b"allowlisted", app.lower())
        self.assertIn(b"registered", app)

    @unittest.skipUnless(
        shutil.which("node"), "node is required for browser JS test"
    )
    def test_dashboard_rejects_queued_stream_and_labels_tire_quality(self):
        app = (
            pathlib.Path(__file__).resolve().parents[1]
            / "projects"
            / "vehicle_data"
            / "static"
            / "app.js"
        )
        script = r"""
const fs = require("fs");
const vm = require("vm");
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      dataset: {},
      textContent: "",
    });
  }
  return elements.get(id);
}
global.document = {
  visibilityState: "visible",
  getElementById: element,
};
let fakeMonotonicMs = 1000;
global.performance = {now: () => fakeMonotonicMs};
global.window = {
  VanDashboardProfiles: {
    loadSettings: () => ({selected: "auto", customWidgets: []}),
  },
};
const source = fs.readFileSync(process.argv[1], "utf8");
const definitionsOnly = source.slice(
  0,
  source.indexOf('\nbyId("refresh").addEventListener'),
);
vm.runInThisContext(definitionsOnly + `
  const renderedMarkers = [];
  let timeSensitiveRenders = 0;
  render = (snapshot) => {
    renderedMarkers.push(snapshot.marker);
    lastSnapshot = {
      status: snapshot.status || {},
      web: snapshot.web || {},
      metrics: snapshot.metrics || {},
      catalog: Array.isArray(snapshot.catalog) ? snapshot.catalog : [],
    };
  };
  renderTimeSensitiveSnapshot = () => {
    timeSensitiveRenders += 1;
  };
  globalThis.dashboardUnderTest = {
    acceptSnapshot,
    advanceDisplayedAges,
    featuredMetricNames,
    ignitionFromVehicleState,
    invalidateDisplayedFreshness,
    observationState,
    renderEngineHealth,
    renderHeroMetric,
    renderTires,
    renderedMarkers,
    renderedSnapshot: () => lastSnapshot,
    timeSensitiveRenders: () => timeSensitiveRenders,
    resetDelivery: () => {
      acceptedDelivery = null;
      serverMonotonicOffsetMs = null;
      serverMonotonicUncertaintyMs = null;
      lastAcceptedMonotonicMs = null;
      ageCursorMonotonicMs = null;
      streamAccepting = true;
      retiredInstances.clear();
    },
  };
`);
const dashboard = global.dashboardUnderTest;
dashboard.resetDelivery();
function snapshot(instanceId, sequence, generatedAtMs, monotonicMs, marker) {
  return {
    marker,
    catalog: [{
      name: "vehicle.ignition_on",
      stale_after_seconds: 3,
      sources: [{quality: "verified"}],
    }],
    metrics: {
      "vehicle.ignition_on": {
        available: true,
        stale: false,
        value: true,
        quality: "verified",
        age_ms: 100,
      },
    },
    status: {
      vehicle_state: {
        state: "ignition_on",
        running: null,
        confidence: "verified",
        age_ms: 100,
      },
    },
    web_delivery: {
      instance_id: instanceId,
      sequence,
      generated_at_ms: generatedAtMs,
      generated_monotonic_ms: monotonicMs,
    },
  };
}
const now = 1700000000000;
const immediateHttpTiming = {
  roundTripMs: 0,
  clientMidpointMonotonicMs: fakeMonotonicMs,
  clientReceiptMonotonicMs: fakeMonotonicMs,
};
const acceptedHttp = dashboard.acceptSnapshot(
  snapshot("one", 1, now, 100000, "http"), "http", immediateHttpTiming
);
fakeMonotonicMs = 1001;
const acceptedStream = dashboard.acceptSnapshot(
  snapshot("one", 2, now + 1, 100001, "stream"), "stream"
);
const outOfOrder = dashboard.acceptSnapshot(
  snapshot("one", 1, now + 2, 100002, "old"), "stream"
);
fakeMonotonicMs = 6100;
const shortExpiredQueue = dashboard.acceptSnapshot(
  snapshot("one", 3, now - 4900, 100100, "short-expired"), "stream"
);
fakeMonotonicMs = 6200;
const shortSnapshot = snapshot("one", 4, now - 300, 104700, "short");
const shortAccepted = dashboard.acceptSnapshot(
  shortSnapshot, "stream"
);
const shortAges = {
  metric: shortSnapshot.metrics["vehicle.ignition_on"].age_ms,
  vehicle: shortSnapshot.status.vehicle_state.age_ms,
  stale: shortSnapshot.metrics["vehicle.ignition_on"].stale,
};
fakeMonotonicMs = 26200;
const queued = dashboard.acceptSnapshot(
  snapshot("one", 5, now + 60000, 105200, "queued"), "stream"
);
const changedInstance = dashboard.acceptSnapshot(
  snapshot("two", 1, now + 3, 1, "restart"), "stream"
);

dashboard.resetDelivery();
const delayedHttp = dashboard.acceptSnapshot(
  snapshot("delayed", 1, now, 1, "delayed"),
  "http",
  {
    roundTripMs: 60000,
    clientMidpointMonotonicMs: fakeMonotonicMs + 30000,
    clientReceiptMonotonicMs: fakeMonotonicMs + 60000,
  },
);

dashboard.resetDelivery();
fakeMonotonicMs = 27000;
const boundedHttpSnapshot = snapshot("bounded", 1, now, 1, "bounded");
const boundedHttp = dashboard.acceptSnapshot(
  boundedHttpSnapshot,
  "http",
  {
    roundTripMs: 1000,
    clientMidpointMonotonicMs: fakeMonotonicMs - 500,
    clientReceiptMonotonicMs: fakeMonotonicMs,
  },
);
const boundedHttpAges = {
  metric: boundedHttpSnapshot.metrics["vehicle.ignition_on"].age_ms,
  vehicle: boundedHttpSnapshot.status.vehicle_state.age_ms,
};

dashboard.resetDelivery();
fakeMonotonicMs = 28000;
const missingAgeSnapshot = snapshot("missing", 1, now, 1, "missing");
missingAgeSnapshot.metrics["vehicle.ignition_on"].age_ms = null;
missingAgeSnapshot.status.vehicle_state.age_ms = null;
const missingAgeHttp = dashboard.acceptSnapshot(
  missingAgeSnapshot,
  "http",
  {
    ...immediateHttpTiming,
    clientMidpointMonotonicMs: fakeMonotonicMs,
    clientReceiptMonotonicMs: fakeMonotonicMs,
  },
);
const missingAgeState = {
  metric: dashboard.observationState(
    missingAgeSnapshot.catalog[0],
    missingAgeSnapshot.metrics["vehicle.ignition_on"],
  ),
  fallback: dashboard.ignitionFromVehicleState(missingAgeSnapshot.status),
  vehicle: missingAgeSnapshot.status.vehicle_state,
};
const invalidAgeInputs = [false, "", "0"];
const invalidAgeStates = invalidAgeInputs.map((age) => ({
  metric: dashboard.observationState(
    missingAgeSnapshot.catalog[0],
    {
      ...missingAgeSnapshot.metrics["vehicle.ignition_on"],
      stale: false,
      age_ms: age,
    },
  ),
  fallback: dashboard.ignitionFromVehicleState({
    vehicle_state: {
      state: "ignition_on",
      confidence: "verified",
      age_ms: age,
    },
  }),
}));

const missingDrive = dashboard.renderHeroMetric("rpm", [], {});
const missingDriveRender = {
  registered: Boolean(missingDrive.definition),
  hidden: element("drive-rpm-card").hidden,
  status: element("drive-rpm-status").textContent,
};
const candidateDriveCatalog = [{
  name: "engine.rpm",
  unit: "rpm",
  stale_after_seconds: 3,
  sources: [{quality: "candidate"}],
}];
const candidateDriveMetrics = {
  "engine.rpm": {
    available: true,
    stale: false,
    value: 1234,
    unit: "rpm",
    quality: "candidate",
    age_ms: 100,
  },
};
const candidateDrive = dashboard.renderHeroMetric(
  "rpm",
  candidateDriveCatalog,
  candidateDriveMetrics,
);
const candidateDriveRender = {
  hidden: element("drive-rpm-card").hidden,
  heroReady: candidateDrive.state.heroReady,
  value: element("drive-rpm").textContent,
  status: element("drive-rpm-status").textContent,
};
const verifiedDriveCatalog = [{
  ...candidateDriveCatalog[0],
  sources: [{quality: "verified"}],
}];
const verifiedDriveMetrics = {
  "engine.rpm": {
    ...candidateDriveMetrics["engine.rpm"],
    quality: "verified",
  },
};
const verifiedDrive = dashboard.renderHeroMetric(
  "rpm",
  verifiedDriveCatalog,
  verifiedDriveMetrics,
);
const verifiedDriveRender = {
  hidden: element("drive-rpm-card").hidden,
  heroReady: verifiedDrive.state.heroReady,
  value: element("drive-rpm").textContent,
  status: element("drive-rpm-status").textContent,
};
const featuredCandidateDrive = [
  ...dashboard.featuredMetricNames(candidateDriveCatalog),
];
const featuredVerifiedDrive = [
  ...dashboard.featuredMetricNames(verifiedDriveCatalog),
];

dashboard.resetDelivery();
fakeMonotonicMs = 29000;
const freshForAging = snapshot("aging", 1, now, 1, "fresh");
dashboard.acceptSnapshot(
  freshForAging,
  "http",
  {
    ...immediateHttpTiming,
    clientMidpointMonotonicMs: fakeMonotonicMs,
    clientReceiptMonotonicMs: fakeMonotonicMs,
  },
);
fakeMonotonicMs = 33001;
dashboard.advanceDisplayedAges();
const locallyAged = {
  metric: dashboard.renderedSnapshot().metrics["vehicle.ignition_on"],
  vehicle: dashboard.renderedSnapshot().status.vehicle_state,
  renders: dashboard.timeSensitiveRenders(),
};

dashboard.resetDelivery();
fakeMonotonicMs = 34000;
const lifecycleSnapshot = snapshot("lifecycle", 1, now, 1, "lifecycle");
dashboard.acceptSnapshot(
  lifecycleSnapshot,
  "http",
  {
    ...immediateHttpTiming,
    clientMidpointMonotonicMs: fakeMonotonicMs,
    clientReceiptMonotonicMs: fakeMonotonicMs,
  },
);
dashboard.invalidateDisplayedFreshness("test_page_hidden");
const lifecycleInvalidated = {
  metric: dashboard.renderedSnapshot().metrics["vehicle.ignition_on"],
  vehicle: dashboard.renderedSnapshot().status.vehicle_state,
};

const positions = ["fl", "fr", "rl", "rr"];
const catalog = positions.map((position) => ({
  name: `tire.pressure.${position}`,
  unit: "psi",
  stale_after_seconds: 30,
  sources: [{quality: "observed_alfa_scale"}],
}));
const metrics = Object.fromEntries(catalog.map((definition) => [
  definition.name,
  {
    available: true,
    stale: false,
    value: 55,
    unit: "psi",
    quality: "observed_alfa_scale",
    age_ms: 100,
  },
]));
dashboard.renderTires(catalog, metrics);
const alfaTires = {
  state: element("tires-state").textContent,
  stateQuality: element("tires-state").dataset.state,
  note: element("tires-note").textContent,
};
dashboard.renderTires(
  catalog,
  Object.fromEntries(catalog.map((definition) => [
    definition.name,
    {available: false, reason: "stale"},
  ])),
);
const registeredStale = element("tires-state").textContent;
dashboard.renderTires([], {});
const emptyTires = {
  state: element("tires-state").textContent,
  note: element("tires-note").textContent,
  gridHidden: element("tire-grid").hidden,
  cardsHidden: positions.every(
    (position) => element(`tire-${position}-card`).hidden,
  ),
};
const emptyEngineStates = dashboard.renderEngineHealth([], {});
const emptyEngine = {
  mapped: emptyEngineStates.filter((state) => state.definition).length,
  state: element("engine-health-state").textContent,
  note: element("engine-health-note").textContent,
  cardsVisible: [
    "oil-pressure",
    "coolant-temperature",
    "oil-temperature",
    "torque",
    "power",
  ].every((name) => element(`engine-${name}-card`).hidden === false),
  statusesPending: [
    "oil-pressure",
    "coolant-temperature",
    "oil-temperature",
    "torque",
    "power",
  ].every(
    (name) => element(`engine-${name}-status`).textContent === "Mapping pending",
  ),
};
const coolantDefinition = {
  name: "engine.coolant_temperature",
  unit: "\u00b0F",
  stale_after_seconds: 3,
  sources: [{quality: "verified"}],
};
const coolantStates = dashboard.renderEngineHealth(
  [coolantDefinition],
  {
    "engine.coolant_temperature": {
      available: true,
      stale: false,
      value: 194,
      unit: "\u00b0F",
      quality: "verified",
      age_ms: 100,
    },
  },
);
const liveCoolant = {
  ready: coolantStates.filter((state) => state.state.heroReady).length,
  state: element("engine-health-state").textContent,
  value: element("engine-coolant-temperature").textContent,
  unit: element("engine-coolant-temperature-unit").textContent,
  status: element("engine-coolant-temperature-status").textContent,
};
process.stdout.write(JSON.stringify({
  acceptedHttp,
  acceptedStream,
  outOfOrder,
  shortExpiredQueue,
  shortAccepted,
  shortAges,
  queued,
  changedInstance,
  delayedHttp,
  boundedHttp,
  boundedHttpAges,
  missingAgeHttp,
  missingAgeState,
  invalidAgeStates,
  missingDriveRender,
  candidateDriveRender,
  verifiedDriveRender,
  featuredCandidateDrive,
  featuredVerifiedDrive,
  locallyAged,
  lifecycleInvalidated,
  renderedMarkers: dashboard.renderedMarkers,
  alfaTires,
  registeredStale,
  emptyTires,
  emptyEngine,
  liveCoolant,
}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(app)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)

        self.assertTrue(result["acceptedHttp"]["accepted"])
        self.assertTrue(result["acceptedStream"]["accepted"])
        self.assertTrue(result["shortAccepted"]["accepted"])
        self.assertEqual(
            result["renderedMarkers"],
            [
                "http",
                "stream",
                "short",
                "bounded",
                "missing",
                "fresh",
                "lifecycle",
            ],
        )
        self.assertEqual(result["outOfOrder"]["reason"], "out_of_order")
        self.assertEqual(
            result["shortExpiredQueue"]["reason"],
            "queued_stream_event",
        )
        self.assertGreaterEqual(result["shortAges"]["metric"], 600)
        self.assertGreaterEqual(result["shortAges"]["vehicle"], 600)
        self.assertFalse(result["shortAges"]["stale"])
        self.assertEqual(result["queued"]["reason"], "queued_stream_event")
        self.assertEqual(result["changedInstance"]["reason"], "instance_changed")
        self.assertEqual(result["delayedHttp"]["reason"], "http_response_delayed")
        self.assertTrue(result["boundedHttp"]["accepted"])
        self.assertEqual(result["boundedHttpAges"]["metric"], 1100)
        self.assertEqual(result["boundedHttpAges"]["vehicle"], 1100)
        self.assertTrue(result["missingAgeHttp"]["accepted"])
        self.assertTrue(result["missingAgeState"]["metric"]["stale"])
        self.assertFalse(result["missingAgeState"]["metric"]["heroReady"])
        self.assertIsNone(result["missingAgeState"]["fallback"])
        self.assertEqual(result["missingAgeState"]["vehicle"]["state"], "unknown")
        self.assertEqual(result["missingAgeState"]["vehicle"]["confidence"], "stale")
        self.assertTrue(
            all(
                not state["metric"]["heroReady"] and state["fallback"] is None
                for state in result["invalidAgeStates"]
            )
        )
        self.assertFalse(result["missingDriveRender"]["registered"])
        self.assertFalse(result["missingDriveRender"]["hidden"])
        self.assertEqual(result["missingDriveRender"]["status"], "Mapping pending")
        self.assertFalse(result["candidateDriveRender"]["hidden"])
        self.assertFalse(result["candidateDriveRender"]["heroReady"])
        self.assertEqual(result["candidateDriveRender"]["value"], "\u2014")
        self.assertIn(
            "diagnostics only", result["candidateDriveRender"]["status"]
        )
        self.assertFalse(result["verifiedDriveRender"]["hidden"])
        self.assertTrue(result["verifiedDriveRender"]["heroReady"])
        self.assertEqual(result["verifiedDriveRender"]["value"], "1,234")
        self.assertIn("VERIFIED", result["verifiedDriveRender"]["status"])
        self.assertNotIn("engine.rpm", result["featuredCandidateDrive"])
        self.assertIn("engine.rpm", result["featuredVerifiedDrive"])
        self.assertTrue(result["locallyAged"]["metric"]["stale"])
        self.assertEqual(result["locallyAged"]["vehicle"]["state"], "unknown")
        self.assertEqual(result["locallyAged"]["vehicle"]["confidence"], "stale")
        self.assertEqual(result["locallyAged"]["renders"], 1)
        self.assertTrue(result["lifecycleInvalidated"]["metric"]["stale"])
        self.assertEqual(
            result["lifecycleInvalidated"]["vehicle"]["state"],
            "unknown",
        )
        self.assertEqual(
            result["lifecycleInvalidated"]["vehicle"]["basis"],
            "test_page_hidden",
        )
        self.assertEqual(result["alfaTires"]["state"], "4/4 LIVE · ALFA SCALE")
        self.assertEqual(result["alfaTires"]["stateQuality"], "partial")
        self.assertIn(
            "not independent verification",
            result["alfaTires"]["note"],
        )
        self.assertEqual(result["registeredStale"], "0/4 LIVE · 4/4 MAPPED")
        self.assertEqual(result["emptyTires"]["state"], "0/4 MAPPED")
        self.assertIn(
            "mapping is pending",
            result["emptyTires"]["note"],
        )
        self.assertFalse(result["emptyTires"]["gridHidden"])
        self.assertFalse(result["emptyTires"]["cardsHidden"])
        self.assertEqual(result["emptyEngine"]["mapped"], 0)
        self.assertEqual(result["emptyEngine"]["state"], "0/5 MAPPED")
        self.assertIn("remain visible", result["emptyEngine"]["note"])
        self.assertTrue(result["emptyEngine"]["cardsVisible"])
        self.assertTrue(result["emptyEngine"]["statusesPending"])
        self.assertEqual(result["liveCoolant"]["ready"], 1)
        self.assertEqual(result["liveCoolant"]["state"], "1/5 LIVE · 1/5 MAPPED")
        self.assertEqual(result["liveCoolant"]["value"], "194")
        self.assertEqual(result["liveCoolant"]["unit"], "\u00b0F")
        self.assertIn("VERIFIED", result["liveCoolant"]["status"])

    @unittest.skipUnless(
        shutil.which("node"), "node is required for browser JS test"
    )
    def test_dashboard_discards_obsolete_http_and_resyncs_stream_failures(self):
        app = (
            pathlib.Path(__file__).resolve().parents[1]
            / "projects"
            / "vehicle_data"
            / "static"
            / "app.js"
        )
        script = r"""
const fs = require("fs");
const vm = require("vm");
let fakeMonotonicMs = 1000;
let fakeEpochMs = 1700000000000;
let resolveFetch;
let latestEventSource = null;
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {dataset: {}, textContent: ""});
  }
  return elements.get(id);
}
class FakeEventSource {
  constructor() {
    this.listeners = {};
    latestEventSource = this;
  }
  addEventListener(name, callback) {
    this.listeners[name] = callback;
  }
  close() {}
}
global.performance = {now: () => fakeMonotonicMs};
Date.now = () => fakeEpochMs;
global.document = {
  visibilityState: "visible",
  getElementById: element,
};
global.window = {
  VanDashboardProfiles: {
    loadSettings: () => ({selected: "auto", customWidgets: []}),
  },
};
global.EventSource = FakeEventSource;
global.fetch = () => new Promise((resolve) => {
  resolveFetch = resolve;
});
const source = fs.readFileSync(process.argv[1], "utf8");
const definitionsOnly = source.slice(
  0,
  source.indexOf('\nbyId("refresh").addEventListener'),
);
vm.runInThisContext(definitionsOnly + `
  const renderedMarkers = [];
  const resyncReasons = [];
  render = (snapshot) => renderedMarkers.push(snapshot.marker);
  globalThis.dashboardUnderTest = {
    acceptSnapshot,
    fetchSnapshot,
    startEventStream,
    freshnessTick,
    renderedMarkers,
    resyncReasons,
    acceptedDelivery: () => acceptedDelivery,
    setResyncGeneration: (value) => {
      resyncGeneration = value;
    },
    stubResync: () => {
      resyncSnapshot = (reason) => {
        resyncReasons.push(reason);
      };
      advanceDisplayedAges = () => {};
    },
    armStall: (acceptedAt) => {
      lastAcceptedMonotonicMs = acceptedAt;
      streamAccepting = true;
    },
  };
`);
const dashboard = global.dashboardUnderTest;
(async () => {
  function snapshot(
    instanceId,
    sequence,
    generatedAtMs,
    generatedMonotonicMs,
    marker,
  ) {
    return {
      marker,
      catalog: [],
      metrics: {},
      status: {},
      web_delivery: {
        instance_id: instanceId,
        sequence,
        generated_at_ms: generatedAtMs,
        generated_monotonic_ms: generatedMonotonicMs,
      },
    };
  }

  dashboard.setResyncGeneration(1);
  const baselinePending = dashboard.fetchSnapshot(1);
  resolveFetch({
    ok: true,
    status: 200,
    json: async () => snapshot(
      "baseline-instance",
      1,
      fakeEpochMs,
      100000,
      "baseline",
    ),
  });
  const baselineAccepted = await baselinePending;

  fakeMonotonicMs = 1100;
  fakeEpochMs += 100;
  const delayedPending = dashboard.fetchSnapshot(1);
  fakeMonotonicMs = 3201;
  fakeEpochMs += 2101;
  resolveFetch({
    ok: true,
    status: 200,
    json: async () => snapshot(
      "delayed-instance",
      1,
      fakeEpochMs,
      100100,
      "delayed",
    ),
  });
  let delayedError = null;
  try {
    await delayedPending;
  } catch (error) {
    delayedError = String(error.message || error);
  }

  streamAccepting = true;
  fakeMonotonicMs = 3202;
  const postDelayStream = dashboard.acceptSnapshot(
    snapshot(
      "baseline-instance",
      2,
      1700000000000 + 2202,
      102202,
      "post-delay-stream",
    ),
    "stream",
  );
  fakeMonotonicMs = 23203;
  const wallStepQueued = dashboard.acceptSnapshot(
    snapshot(
      "baseline-instance",
      3,
      fakeEpochMs + 60000,
      102203,
      "wall-step-queued",
    ),
    "stream",
  );

  fakeMonotonicMs = 23400;
  fakeEpochMs = 1700000002300;
  dashboard.setResyncGeneration(1);
  const obsoletePending = dashboard.fetchSnapshot(1);
  dashboard.setResyncGeneration(2);
  resolveFetch({
    ok: true,
    status: 200,
    json: async () => snapshot(
      "obsolete-instance",
      1,
      fakeEpochMs,
      102300,
      "obsolete",
    ),
  });
  const obsoleteAccepted = await obsoletePending;

  dashboard.stubResync();
  dashboard.startEventStream();
  latestEventSource.listeners.error();
  dashboard.armStall(0);
  fakeMonotonicMs = 50000;
  dashboard.freshnessTick();

  process.stdout.write(JSON.stringify({
    baselineAccepted,
    delayedError,
    postDelayStream,
    wallStepQueued,
    obsoleteAccepted,
    renderedMarkers: dashboard.renderedMarkers,
    acceptedDelivery: dashboard.acceptedDelivery(),
    resyncReasons: dashboard.resyncReasons,
  }));
})().catch((error) => {
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node", "-e", script, str(app)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)

        self.assertTrue(result["baselineAccepted"])
        self.assertIn("freshness bound", result["delayedError"])
        self.assertTrue(result["postDelayStream"]["accepted"])
        self.assertEqual(
            result["wallStepQueued"]["reason"],
            "queued_stream_event",
        )
        self.assertFalse(result["obsoleteAccepted"])
        self.assertEqual(
            result["renderedMarkers"],
            ["baseline", "post-delay-stream"],
        )
        self.assertEqual(
            result["acceptedDelivery"]["instanceId"],
            "baseline-instance",
        )
        self.assertEqual(
            result["resyncReasons"],
            ["stream_error", "stream_stall"],
        )

    def test_disconnected_client_does_not_escape_web_json_writer(self):
        handler = object.__new__(TelemetryWebHandler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = SimpleNamespace(
            write=mock.Mock(side_effect=ConnectionResetError)
        )

        handler._json(200, {"available": False})
        handler.wfile.write.assert_called_once()

        handler.end_headers.side_effect = BrokenPipeError
        handler.wfile.write.reset_mock()
        handler._json(200, {"available": False})
        handler.wfile.write.assert_not_called()

    def test_non_loopback_bind_requires_explicit_opt_in(self):
        with self.assertRaises(SystemExit):
            validate_bind("192.0.2.10", allow_remote_bind=False)

        validate_bind("192.0.2.10", allow_remote_bind=True)

    def test_loopback_bind_remains_allowed_by_default(self):
        validate_bind("127.0.0.1", allow_remote_bind=False)


class InterfaceStateTests(unittest.TestCase):
    def test_atomic_snapshot_parser(self):
        output = """2: can0: <NOARP,UP,LOWER_UP> mtu 16 qdisc pfifo_fast state UNKNOWN
    link/can  promiscuity 0
    can <LISTEN-ONLY> state ERROR-ACTIVE restart-ms 100
      bitrate 500000 sample-point 0.875
"""
        completed = SimpleNamespace(returncode=0, stdout=output)
        with mock.patch("lib.canbus.subprocess.run", return_value=completed):
            state = canbus.interface_state("can0")
        self.assertTrue(state.present)
        self.assertTrue(state.up)
        self.assertTrue(state.listen_only)
        self.assertEqual(state.bitrate, 500000)
        self.assertEqual(state.controller_state, "ERROR-ACTIVE")
        self.assertEqual(state.restart_ms, 100)

    def test_exact_restore_sets_restart_timing_and_verifies_snapshot(self):
        original = interface()
        with (
            mock.patch("lib.canbus.ip_up", return_value=True) as ip_up,
            mock.patch(
                "lib.canbus.interface_state", return_value=original
            ) as snapshot,
        ):
            self.assertTrue(canbus.restore_interface_state(original))
        ip_up.assert_called_once_with(
            "can0",
            500000,
            listen_only=True,
            restart_ms=100,
        )
        snapshot.assert_called_once_with("can0")


if __name__ == "__main__":
    unittest.main()
