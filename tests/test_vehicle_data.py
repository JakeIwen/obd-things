import contextlib
import http.client
import json
import pathlib
import socket
import struct
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
    TelemetryApiHandler,
    TelemetryClient,
    UnixHTTPServer,
)
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.metrics import METRICS
from projects.vehicle_data.models import failure, success
from projects.vehicle_data.sources import DecodedVoltage, VoltageAcquirer
from projects.vehicle_data.web import TelemetryWebHandler, TelemetryWebServer


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
                else "ccan.broadcast.0x2ef"
            ),
            quality="verified" if bus == "b-can" else "approximate",
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
        self.assertEqual(set(METRICS), {"battery.voltage"})
        sources = METRICS["battery.voltage"].sources
        self.assertEqual(
            {source.name for source in sources},
            {
                "bcan.broadcast.0x46c",
                "ccan.broadcast.0x2ef",
                "ccan.broadcast.0x41a",
            },
        )
        self.assertTrue(all(source.provenance for source in sources))

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

    def test_ccan_0x2ef_replay_decodes_qualified_fine_source(self):
        data = bytes((0x88, 0x13, 0, 0, 0, 0, 0, 0))
        frame = struct.pack("=IB3x8s", 0x2EF, 8, data)
        replay = ReplaySocket([frame] * 7)
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

        self.assertEqual(volts, 12.5)
        self.assertIn("0x2EF", detail)
        self.assertEqual(replay.bound, ("can9",))
        self.assertTrue(replay.closed)


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeAcquirer:
    channel = "can0"

    def __init__(self, *, block=None):
        self.calls = []
        self.block = block

    def acquire(self, mode):
        self.calls.append(mode)
        if self.block:
            self.block.wait()
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


class BrokerTests(unittest.TestCase):
    def test_cache_get_never_calls_acquirer(self):
        acquirer = FakeAcquirer()
        broker = TelemetryBroker(acquirer=acquirer, monotonic=FakeClock())

        self.assertEqual(
            broker.metric_response("battery.voltage")["reason"], "stale"
        )
        broker.status_response()
        broker.list_metrics()
        self.assertEqual(acquirer.calls, [])

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

    def test_unknown_metric_never_reaches_source(self):
        acquirer = FakeAcquirer()
        broker = TelemetryBroker(acquirer=acquirer)
        result = broker.acquire("raw.did", "passive")
        self.assertEqual(result.reason, "unknown_metric")
        self.assertEqual(acquirer.calls, [])

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
        connection.close()

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
