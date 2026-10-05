"""Deterministic concurrency and lifecycle regressions for the Unix telemetry API.

These tests intentionally use only fake telemetry backends.  The subprocess cases
exercise the production ``interrupt_on_termination`` + ``serve_unix`` try/finally
path, rather than invoking vehicle runtime factories or touching CAN.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import queue
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from projects.vehicle_data import api_server
from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.event_history import EventReader
from projects.vehicle_data.historian import TelemetryHistorian
from projects.vehicle_data.metrics import METRICS
from projects.vehicle_data.models import success


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP_BASE = os.environ.get("TMPDIR", "/private/tmp")


class MutableClock:
    def __init__(self, value: float = 100.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class BlockingAcquirer:
    """A fake approved source with per-call barriers and overlap accounting."""

    channel = "can7"

    def __init__(self, *, block_indices=()):
        self.block_indices = set(block_indices)
        self.calls: list[str] = []
        self.thread_ids: list[int] = []
        self.started: dict[int, threading.Event] = {}
        self.releases: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def acquire(self, mode):
        with self._lock:
            index = len(self.calls)
            self.calls.append(mode)
            self.thread_ids.append(threading.get_ident())
            started = self.started.setdefault(index, threading.Event())
            release = self.releases.setdefault(index, threading.Event())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            started.set()
        try:
            if index in self.block_indices:
                release.wait()
            return success(
                metric="battery.voltage",
                unit="V",
                value=12.5 + index / 10.0,
                source="bcan.broadcast.0x46c",
                bus="b-can",
                acquisition=mode,
                quality="verified",
                observed_monotonic=100.0 + index,
                observed_at=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
            )
        finally:
            with self._lock:
                self.active -= 1

    def status_snapshot(self):
        return {
            "channel": self.channel,
            "adapter_present": True,
            "up": True,
            "bitrate": 125000,
            "fd_enabled": False,
            "one_shot": False,
            "listen_only": True,
            "controller_state": "ERROR-ACTIVE",
            "topology": {"bus": "b-can", "usable": True, "reason": ""},
            "active_inhibits": [],
        }

    def wait_started(self, index: int, timeout: float = 1.0) -> bool:
        with self._lock:
            event = self.started.setdefault(index, threading.Event())
        return event.wait(timeout)

    def release(self, index: int) -> None:
        with self._lock:
            event = self.releases.setdefault(index, threading.Event())
        event.set()

    def release_all(self):
        with self._lock:
            # Cleanup must also release a queued call that has not entered yet.
            self.block_indices.clear()
            for release in self.releases.values():
                release.set()


class CountingServer(api_server.UnixHTTPServer):
    """Record actual accept completions, not merely successful client connect()."""

    def __init__(self, path, broker):
        self.accepted = 0
        self.accepted_event = threading.Event()
        self.accept_lock = threading.Lock()
        super().__init__(path, broker)

    def get_request(self):
        request = super().get_request()
        with self.accept_lock:
            self.accepted += 1
            self.accepted_event.set()
        return request


class SingleWorkerServer(api_server.UnixHTTPServer):
    request_worker_count = 1

    def __init__(self, path, broker):
        self.accepted = 0
        self.accepted_event = threading.Event()
        super().__init__(path, broker)

    def get_request(self):
        request, client_address = super().get_request()
        request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        self.accepted += 1
        self.accepted_event.set()
        return request, client_address


class FailingAcceptServer(CountingServer):
    def __init__(self, path, broker):
        self.runtime_failure = False
        self.transient_failures = 0
        self.failure_event = threading.Event()
        super().__init__(path, broker)

    def get_request(self):
        if self.runtime_failure:
            self.failure_event.set()
            raise RuntimeError("unexpected acceptor failure")
        if self.transient_failures:
            self.transient_failures -= 1
            self.failure_event.set()
            raise OSError("transient accept failure")
        return super().get_request()


class PostReadyServer(api_server.UnixHTTPServer):
    def __init__(self, path, broker):
        self.posted = queue.Queue(maxsize=8)
        super().__init__(path, broker)

    def _submit_post(self, handler):
        job = super()._submit_post(handler)
        self.posted.put_nowait((handler.path, job))
        return job


class BodyPhaseServer(api_server.UnixHTTPServer):
    def __init__(self, path, broker):
        self.body_started = threading.Event()
        self.post_finished = threading.Event()
        super().__init__(path, broker)

    def _begin_body_phase(self, handler):
        super()._begin_body_phase(handler)
        self.body_started.set()

    def _execute_post(self, job):
        try:
            return super()._execute_post(job)
        finally:
            self.post_finished.set()


class BodyFailingAcceptServer(BodyPhaseServer, FailingAcceptServer):
    pass


class ServerHarness:
    def __init__(self, *, acquirer=None, server_class=api_server.UnixHTTPServer, start=True):
        self.tmp = tempfile.TemporaryDirectory(prefix="o2-api-", dir=TMP_BASE)
        self.socket_path = str(pathlib.Path(self.tmp.name) / "api.sock")
        self.acquirer = acquirer or BlockingAcquirer()
        self._clients = []
        self._clients_lock = threading.Lock()
        self._closed = False
        definitions = dict(METRICS)
        definitions["battery.voltage"] = replace(
            METRICS["battery.voltage"],
            passive_min_interval_seconds=0.0,
            wake_min_interval_seconds=0.0,
        )
        self.broker = TelemetryBroker(
            acquirer=self.acquirer,
            definitions=definitions,
            monotonic=MutableClock(),
            collector_interval_seconds=0.0,
        )
        self.server = server_class(self.socket_path, self.broker)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="test-van-telemetry-api-serve",
        )
        if start:
            self.thread.start()
            with self.server._condition:
                ready = self.server._condition.wait_for(
                    lambda: self.server._serve_state in {"serving", "stopped", "closed"},
                    timeout=1.0,
                )
                assert ready and self.server._serve_state == "serving", "API startup failed"

    def connect(self, timeout=1.0):
        sock = unix_socket(self.socket_path, timeout)
        with self._clients_lock:
            if self._closed:
                sock.close()
                raise RuntimeError("test server already closed")
            self._clients.append(sock)
        return sock

    def close(self):
        with self._clients_lock:
            if self._closed:
                return
            self._closed = True
            clients, self._clients = self._clients, []
        # A failed assertion must not strand a partial body or a fake source.
        for sock in clients:
            sock.close()
        self.acquirer.release_all()
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        if self.thread.ident is not None:
            self.thread.join(1.0)
            assert not self.thread.is_alive(), "test serving thread did not stop"
        self.broker.close()
        self.tmp.cleanup()


@contextlib.contextmanager
def running_server(*, acquirer=None, server_class=api_server.UnixHTTPServer):
    harness = ServerHarness(acquirer=acquirer, server_class=server_class)
    try:
        yield harness
    finally:
        harness.close()


def unix_socket(path: str, timeout: float = 1.0) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(path)
    return sock


def request_bytes(
    sock: socket.socket,
    method: str,
    path: str,
    payload=None,
    *,
    headers=None,
):
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request_headers = {"Host": "local", **(headers or {})}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers["Content-Length"] = str(len(body))
    head = "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
    sock.sendall(f"{method} {path} HTTP/1.1\r\n{head}\r\n".encode() + body)


def read_http(sock: socket.socket, timeout: float = 1.0) -> tuple[int, dict]:
    sock.settimeout(timeout)
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("HTTP peer closed before headers")
        raw.extend(chunk)
    head, body = bytes(raw).split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get(b"content-length", b"0"))
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("HTTP peer closed before body")
        body += chunk
    return status, json.loads(body[:length]) if length else {}


def read_available(sock: socket.socket, timeout: float = 0.5) -> bytes:
    sock.settimeout(timeout)
    chunks = []
    while True:
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    return bool(predicate())


def prime_event_reader(reader: EventReader) -> None:
    for _ in range(40):
        status, payload = reader.request("GET", "/v1/events")
        if status == 200:
            return
        assert status == 202, payload
        threading.Event().wait(0.02)
    raise AssertionError("real EventReader did not produce its cached 200")


def valid_observation():
    return {
        "value": 12.3,
        "unit": "V",
        "source": "cluster.did.1004",
        "bus": "c-can",
        "quality": "observed_alfa_scale",
    }


def valid_oil_change():
    return {
        "date": dt.date.today().isoformat(),
        "mileage_mi": None,
        "mileage_source": "unknown",
        "notes": "concurrency test",
        "request_id": "o2-api-oil-0001",
    }


class ApiConcurrencyTests(unittest.TestCase):
    def test_cache_gets_and_mutations_are_isolated_from_a_blocked_source(self):
        acquirer = BlockingAcquirer(block_indices={0})
        with running_server(acquirer=acquirer, server_class=PostReadyServer) as harness:
            history_tmp = tempfile.TemporaryDirectory(prefix="o2-history-", dir=TMP_BASE)
            self.addCleanup(history_tmp.cleanup)
            history_path = pathlib.Path(history_tmp.name) / "history.sqlite3"
            historian = TelemetryHistorian(history_path)
            self.addCleanup(historian.close)
            reader = EventReader(str(history_path))
            self.addCleanup(reader.close)
            harness.broker.event_reader = reader
            prime_event_reader(reader)

            callback_log = []
            originals = {}

            def observe_callback(name, *, post_only=False):
                original = getattr(harness.broker, name)
                originals[name] = original

                def wrapped(*args, **kwargs):
                    if not post_only or args and args[0] == "POST":
                        callback_log.append(
                            (name, threading.get_ident(), acquirer.active)
                        )
                    return original(*args, **kwargs)

                setattr(harness.broker, name, wrapped)

            observe_callback("publish_observation")
            observe_callback("record_oil_change")
            observe_callback("event_request", post_only=True)

            acquisition = harness.connect()
            request_bytes(
                acquisition,
                "POST",
                "/v1/acquisitions/battery.voltage",
                {"mode": "passive"},
            )
            self.assertTrue(acquirer.wait_started(0))
            self.assertEqual(harness.server.posted.get(timeout=1.0)[0],
                             "/v1/acquisitions/battery.voltage")

            get_paths = (
                "/v1/status",
                "/v1/snapshot",
                "/v1/history",
                "/v1/health",
                "/v1/diagnostics/dtcs",
                "/v1/maintenance",
                "/v1/metrics",
                "/v1/metrics/battery.voltage",
                "/v1/events",
            )

            def get(path):
                client = TelemetryClient(harness.socket_path, timeout=0.8)
                return path, client.request("GET", path)

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(get, get_paths))
            for path, (status, payload) in results:
                with self.subTest(path=path):
                    self.assertEqual(status, 200)
                    self.assertIsInstance(payload, dict)
            self.assertEqual(acquirer.calls, ["passive"])

            queued_posts = []
            post_specs = (
                (
                    "/v1/observations/battery.voltage",
                    valid_observation(),
                    {api_server.OBSERVATION_DEADLINE_HEADER: f"{time.monotonic() + 1.0:.9f}"},
                ),
                ("/v1/maintenance/oil-changes", valid_oil_change(), {}),
                (
                    "/v1/events/1/annotations",
                    {
                        "kind": "dismissed",
                        "note": "queued",
                        "links": [],
                        "request_id": "o2-event-0001",
                    },
                    {},
                ),
                ("/v1/events/1/replay", {}, {}),
            )
            responses = []
            for path, payload, headers in post_specs:
                sock = harness.connect()
                request_bytes(sock, "POST", path, payload, headers=headers)
                self.assertEqual(harness.server.posted.get(timeout=1.0)[0], path)
                queued_posts.append(sock)
                responses.append([])

            self.assertFalse(
                any(name != "acquire" for name, _thread, _active in callback_log),
                "a mutation callback ran while the source callback was blocked",
            )
            acquirer.release(0)
            self.assertEqual(read_http(acquisition)[0], 200)
            acquisition.close()
            for sock, response in zip(queued_posts, responses):
                response.append(read_http(sock))
                sock.close()
            self.assertEqual([code for code, _payload in (r[0] for r in responses)], [200, 201, 202, 202])
            self.assertEqual(acquirer.max_active, 1)
            self.assertEqual(acquirer.thread_ids, [harness.thread.ident])
            self.assertTrue(callback_log)
            self.assertTrue(all(thread_id == harness.thread.ident for _, thread_id, _ in callback_log))
            self.assertTrue(all(active == 0 for _, _, active in callback_log))

    def test_ready_post_is_not_barred_by_an_earlier_header_incomplete_connection(self):
        acquirer = BlockingAcquirer(block_indices={0})
        with running_server(acquirer=acquirer, server_class=CountingServer) as harness:
            harness.server.request_io_timeout = 1.0
            silent = harness.connect()
            silent.sendall(
                b"POST /v1/acquisitions/battery.voltage HTTP/1.1\r\n"
                b"Host: local\r\nContent-Length: 18\r\n"
            )
            self.assertTrue(harness.server.accepted_event.wait(1.0))
            ready = harness.connect()
            request_bytes(
                ready,
                "POST",
                "/v1/acquisitions/battery.voltage",
                {"mode": "passive"},
            )
            self.assertTrue(acquirer.wait_started(0), "ready POST was blocked by silent connection")
            self.assertGreaterEqual(harness.server.accepted, 2)
            acquirer.release(0)
            self.assertEqual(read_http(ready)[0], 200)
            ready.close()
            silent.close()

    def test_observation_deadline_expires_after_queued_acquisition_without_cache_mutation(self):
        acquirer = BlockingAcquirer(block_indices={0})
        clock = MutableClock()
        with mock.patch.object(api_server, "time", SimpleNamespace(monotonic=clock)):
            with running_server(acquirer=acquirer, server_class=PostReadyServer) as harness:
                acquisition = harness.connect()
                request_bytes(acquisition, "POST", "/v1/acquisitions/battery.voltage",
                              {"mode": "passive"})
                self.assertTrue(acquirer.wait_started(0))
                harness.server.posted.get(timeout=1.0)
                observation = harness.connect()
                request_bytes(
                    observation, "POST", "/v1/observations/battery.voltage",
                    valid_observation(),
                    headers={api_server.OBSERVATION_DEADLINE_HEADER: "100.5"},
                )
                self.assertEqual(harness.server.posted.get(timeout=1.0)[0],
                                 "/v1/observations/battery.voltage")
                clock.value = 101.0
                acquirer.release(0)
                self.assertEqual(read_http(acquisition)[0], 200)
                status, payload = read_http(observation)
                self.assertEqual(status, 408)
                self.assertEqual(payload, {
                    "metric": "battery.voltage",
                    "available": False,
                    "reason": "observation_expired",
                    "detail": "the local publication waited too long before the serialized broker could receive it",
                })
                cached = harness.broker.metric_response("battery.voltage")
                self.assertEqual(cached["value"], 12.5)
                self.assertEqual(cached["source"], "bcan.broadcast.0x46c")

    def test_bounded_status_lock_contention_outlives_transport_but_not_lock_release(self):
        with running_server() as harness:
            radar_lock = harness.broker._radar_history.lock
            summary_entered = threading.Event()
            network_closed = threading.Event()
            broker_closed = threading.Event()
            broker_closing = threading.Event()
            ownership = []
            original = harness.broker._radar_history.summary

            def summary(now):
                ownership.append(harness.broker._lock._is_owned())
                summary_entered.set()
                return original(now)

            def close_transport():
                harness.server.shutdown()
                harness.server.server_close()
                network_closed.set()

            def close_broker():
                broker_closing.set()
                harness.broker.close()
                broker_closed.set()

            harness.broker._radar_history.summary = summary
            network_thread = threading.Thread(target=close_transport)
            broker_thread = threading.Thread(target=close_broker)
            radar_lock.acquire()
            held = True
            try:
                sock = harness.connect()
                request_bytes(sock, "GET", "/v1/status")
                self.assertTrue(summary_entered.wait(1.0))
                self.assertEqual(ownership, [True])  # Existing B -> radar lock edge.
                network_thread.start()
                self.assertTrue(network_closed.wait(1.0), "HTTP cleanup joined the reader")
                broker_thread.start()
                self.assertTrue(broker_closing.wait(1.0))
                self.assertFalse(broker_closed.wait(0.05))
                radar_lock.release()
                held = False
                self.assertTrue(broker_closed.wait(1.0))
            finally:
                if held:
                    radar_lock.release()
                for thread in (network_thread, broker_thread):
                    if thread.ident is not None:
                        thread.join(1.0)
                        self.assertFalse(thread.is_alive())

    def test_observation_deadline_validation_retains_missing_nonfinite_and_future_semantics(self):
        with running_server() as harness:
            payload = valid_observation()
            cases = (
                ({}, "missing"),
                ({api_server.OBSERVATION_DEADLINE_HEADER: "nan"}, "nonfinite"),
                ({api_server.OBSERVATION_DEADLINE_HEADER: "inf"}, "nonfinite"),
                ({api_server.OBSERVATION_DEADLINE_HEADER: "-inf"}, "nonfinite"),
                ({api_server.OBSERVATION_DEADLINE_HEADER: str(time.monotonic() + 60)}, "future"),
            )
            for headers, label in cases:
                with self.subTest(label=label):
                    sock = harness.connect()
                    request_bytes(sock, "POST", "/v1/observations/battery.voltage", payload, headers=headers)
                    status, result = read_http(sock)
                    sock.close()
                    self.assertEqual(status, 400)
                    self.assertEqual(result["reason"], "invalid_request")
            self.assertEqual(harness.acquirer.calls, [])

    def test_posted_acquisitions_are_fifo_and_never_overlap_with_zero_test_rate_limit(self):
        acquirer = BlockingAcquirer(block_indices={0, 1})
        with running_server(acquirer=acquirer, server_class=PostReadyServer) as harness:
            sockets = []
            modes = ["passive", "wake_if_asleep", "passive"]
            for mode in modes:
                sock = harness.connect()
                sockets.append(sock)
                request_bytes(sock, "POST", "/v1/acquisitions/battery.voltage", {"mode": mode})
                harness.server.posted.get(timeout=1.0)  # Establish actual enqueue order.
            self.assertTrue(acquirer.wait_started(0))
            self.assertEqual(acquirer.calls, ["passive"])
            acquirer.release(0)
            self.assertTrue(acquirer.wait_started(1))
            self.assertEqual(acquirer.calls, modes[:2])
            self.assertEqual(acquirer.max_active, 1)
            acquirer.release(1)
            for sock in sockets:
                self.assertEqual(read_http(sock)[0], 200)
            self.assertEqual(acquirer.calls, modes)
            self.assertEqual(acquirer.thread_ids, [harness.thread.ident] * 3)
            self.assertEqual(acquirer.max_active, 1)

    def test_eight_blocked_read_handlers_bound_acceptance_and_recover_after_release(self):
        with running_server(server_class=CountingServer) as harness:
            self.assertFalse(harness.server.socket.getblocking())
            harness.server.request_io_timeout = 2.0
            blocked = []
            for _ in range(harness.server.request_worker_count):
                sock = harness.connect()
                sock.sendall(b"GET /v1/status HTTP/1.1\r\nHost: local\r\n")
                blocked.append(sock)
            deadline = time.monotonic() + 1.0
            while harness.server.accepted < harness.server.request_worker_count and time.monotonic() < deadline:
                harness.server.accepted_event.wait(0.05)
            self.assertEqual(harness.server.accepted, harness.server.request_worker_count)
            self.assertEqual(harness.server.request_queue_size, 64)
            self.assertEqual(harness.server._transport_queue.maxsize, harness.server.request_worker_count)
            self.assertEqual(harness.server._post_jobs.maxsize, harness.server.request_worker_count)
            api_threads = [
                thread for thread in harness.server._helper_threads
                if thread.name.startswith("van-telemetry-api")
            ]
            self.assertEqual(len(api_threads), harness.server.request_worker_count + 1)
            ninth = harness.connect()
            request_bytes(ninth, "GET", "/v1/status")
            threading.Event().wait(0.1)
            self.assertEqual(harness.server.accepted, harness.server.request_worker_count)
            for sock in blocked:
                sock.sendall(b"\r\n")
            self.assertTrue(harness.server.accepted_event.wait(1.0))
            deadline = time.monotonic() + 1.0
            while harness.server.accepted < 9 and time.monotonic() < deadline:
                threading.Event().wait(0.02)
            self.assertEqual(harness.server.accepted, 9)
            self.assertEqual(read_http(ninth)[0], 200)
            ninth.close()
            for sock in blocked:
                sock.close()

    def test_expired_valid_prefix_never_acquires_and_recovers(self):
        prefix = b'{"mode":"wake_if_asleep"}'
        acquirer = BlockingAcquirer()
        with running_server(acquirer=acquirer, server_class=BodyPhaseServer) as harness:
            harness.server.request_io_timeout = 0.1
            sock = harness.connect()
            sock.sendall(
                b"POST /v1/acquisitions/battery.voltage HTTP/1.1\r\n"
                b"Host: local\r\nContent-Type: application/json\r\n"
                b"Content-Length: 64\r\n\r\n" + prefix
            )
            self.assertTrue(harness.server.body_started.wait(1.0))
            raw = read_available(sock, timeout=0.5)
            self.assertTrue(harness.server.post_finished.wait(1.0))
            self.assertNotIn(b'"reason":"transport_timeout"', raw)
            self.assertEqual(acquirer.calls, [])
            self.assertEqual(
                TelemetryClient(harness.socket_path, timeout=0.8).request("GET", "/v1/status")[0],
                200,
            )

    def test_buffered_valid_prefix_expiry_is_checked_without_watchdog(self):
        harness = ServerHarness(start=False)
        request, peer = socket.socketpair()
        clock = MutableClock()
        record = api_server._ConnectionRecord(request, "", None, None)
        harness.server._connections[id(record)] = record
        handler = object.__new__(api_server.TelemetryApiHandler)
        handler.server = harness.server
        handler._transport_record = record
        handler.path = "/v1/acquisitions/battery.voltage"
        handler.headers = {"Content-Length": "64"}
        handler._json = mock.Mock()

        class DelayedBuffer(io.BytesIO):
            def read(self, size):
                body = super().read(size)
                clock.value += 6.0
                return body

        handler.rfile = DelayedBuffer(b'{"mode":"wake_if_asleep"}')
        try:
            with mock.patch.object(api_server, "time", SimpleNamespace(monotonic=clock)):
                with self.assertRaises(TimeoutError):
                    handler._do_POST_serialized()
            self.assertTrue(record.expired)
            self.assertEqual(harness.acquirer.calls, [])
            handler._json.assert_not_called()
            peer.settimeout(0.5)
            self.assertEqual(peer.recv(1), b"")
        finally:
            handler.rfile.close()
            peer.close()
            harness.close()

    def test_phase_transition_cannot_revive_expiry_or_use_stale_deadline(self):
        harness = ServerHarness(start=False)
        request, peer = socket.socketpair()
        clock = MutableClock()
        record = api_server._ConnectionRecord(request, "", "headers", 101.0)
        harness.server._connections[id(record)] = record
        handler = object.__new__(api_server.TelemetryApiHandler)
        handler.server = harness.server
        handler._transport_record = record
        try:
            with mock.patch.object(api_server, "time", SimpleNamespace(monotonic=clock)):
                harness.server._complete_header_phase(handler)
                clock.value = 102.0
                self.assertEqual(harness.server._expire_due_connections(), [])
                harness.server._begin_body_phase(handler)
                clock.value = 108.0
                self.assertEqual(harness.server._expire_due_connections(), [request])
                # Interleave here, before the watchdog's raw-socket shutdown.
                self.assertTrue(record.expired)
                with self.assertRaises(TimeoutError):
                    harness.server._complete_body_phase(handler)
                with self.assertRaises(TimeoutError):
                    harness.server._begin_response_phase(handler)
                self.assertIsNone(record.deadline)
        finally:
            peer.close()
            harness.close()

    def test_nonexpired_short_valid_json_at_eof_keeps_baseline_behavior(self):
        acquirer = BlockingAcquirer()
        with running_server(acquirer=acquirer) as harness:
            harness.server.request_io_timeout = 1.0
            body = b'{"mode":"wake_if_asleep"}'
            sock = harness.connect()
            sock.sendall(
                b"POST /v1/acquisitions/battery.voltage HTTP/1.1\r\n"
                b"Host: local\r\nContent-Type: application/json\r\n"
                b"Content-Length: 64\r\n\r\n" + body
            )
            sock.shutdown(socket.SHUT_WR)
            status, payload = read_http(sock)
            sock.close()
            self.assertEqual(status, 200)
            self.assertTrue(payload["available"])
            self.assertEqual(acquirer.calls, ["wake_if_asleep"])

    def test_header_and_body_trickle_deadlines_close_transport_without_error_json(self):
        for kind in ("headers", "body"):
            with self.subTest(kind=kind):
                acquirer = BlockingAcquirer()
                with running_server(acquirer=acquirer, server_class=BodyPhaseServer) as harness:
                    harness.server.request_io_timeout = 0.12
                    sock = harness.connect()
                    if kind == "headers":
                        sock.sendall(
                            b"POST /v1/acquisitions/battery.voltage HTTP/1.1\r\nX-Trickle: "
                        )
                    else:
                        sock.sendall(
                            b"POST /v1/acquisitions/battery.voltage HTTP/1.1\r\n"
                            b"Host: local\r\nContent-Type: application/json\r\n"
                            b"Content-Length: 4096\r\n\r\n"
                            b'{"mode":"wake_if_asleep"}'
                        )
                        self.assertTrue(harness.server.body_started.wait(1.0))
                    stopped = threading.Event()

                    def trickle():
                        while not stopped.is_set():
                            try:
                                sock.sendall(b" ")
                            except OSError:
                                return
                            stopped.wait(0.01)  # Always shorter than the socket timeout.

                    sender = threading.Thread(target=trickle)
                    sender.start()
                    try:
                        sock.settimeout(0.7)
                        # The sender remains active until the server closes. A mere
                        # idle socket timeout cannot satisfy this assertion.
                        try:
                            closed = sock.recv(1) == b""
                        except ConnectionResetError:
                            closed = True
                        self.assertTrue(closed)
                        if kind == "body":
                            self.assertTrue(harness.server.post_finished.wait(1.0))
                        self.assertEqual(acquirer.calls, [])
                    finally:
                        stopped.set()
                        sender.join(1.0)
                        self.assertFalse(sender.is_alive())
                    self.assertEqual(
                        TelemetryClient(harness.socket_path, timeout=0.8).request("GET", "/v1/status")[0],
                        200,
                    )

    def test_nonexpired_eof_with_exact_content_length_remains_accepted(self):
        acquirer = BlockingAcquirer()
        with running_server(acquirer=acquirer) as harness:
            body = b'{"mode":"passive"}'
            sock = harness.connect()
            sock.sendall(
                b"POST /v1/acquisitions/battery.voltage HTTP/1.1\r\n"
                b"Host: local\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            sock.shutdown(socket.SHUT_WR)
            status, payload = read_http(sock)
            sock.close()
            self.assertEqual(status, 200)
            self.assertTrue(payload["available"])
            self.assertEqual(acquirer.calls, ["passive"])

    def test_stuck_output_deadline_releases_single_worker_while_peer_remains_open(self):
        with running_server(server_class=SingleWorkerServer) as harness:
            harness.server.request_io_timeout = 0.05
            calls = 0
            called = threading.Event()

            def status_response():
                nonlocal calls
                calls += 1
                called.set()
                if calls == 1:
                    return {"blob": "x" * 8_000_000}
                return {"available": True, "recovered": True}

            harness.broker.status_response = status_response
            stuck = harness.connect()
            request_bytes(stuck, "GET", "/v1/status")
            self.assertTrue(called.wait(1.0))
            def response_phase_active():
                with harness.server._condition:
                    return any(
                        record.phase == "response"
                        for record in harness.server._connections.values()
                    )
            self.assertTrue(wait_until(response_phase_active, timeout=0.5))
            self.assertTrue(wait_until(lambda: harness.server.accepted == 1, timeout=0.5))

            fresh = harness.connect()
            request_bytes(fresh, "GET", "/v1/status")
            self.assertTrue(
                wait_until(lambda: harness.server.accepted == 2, timeout=0.8),
                "the single worker did not recover while the stuck peer remained open",
            )
            status, payload = read_http(fresh, timeout=0.8)
            self.assertEqual((status, payload), (200, {"available": True, "recovered": True}))
            fresh.close()
            stuck.close()

    def test_stdlib_error_output_and_normal_protocol_error_allow_recovery(self):
        with running_server(server_class=SingleWorkerServer) as harness:
            harness.server.request_io_timeout = 0.05
            malformed = harness.connect()
            malformed.sendall(
                b"U" * 60000
                + b" /v1/status HTTP/1.1\r\nHost: local\r\n\r\n"
            )
            fresh = harness.connect()
            request_bytes(fresh, "GET", "/v1/status")
            self.assertTrue(wait_until(lambda: harness.server.accepted == 2, timeout=0.8))
            status, payload = read_http(fresh, timeout=0.8)
            self.assertEqual(status, 200)
            self.assertIsInstance(payload, dict)
            fresh.close()
            malformed.close()

    def test_unexpected_acceptor_runtime_error_propagates_and_blocks_later_mutations(self):
        harness = ServerHarness(server_class=FailingAcceptServer, start=False)
        caught = []
        caught_event = threading.Event()
        harness.thread = threading.Thread(
            target=lambda: self._serve_catching(harness.server, caught, caught_event),
            name="test-accept-runtime",
        )
        harness.thread.start()
        try:
            harness.server.runtime_failure = True
            trigger = harness.connect()
            self.assertTrue(caught_event.wait(1.0))
            trigger.close()
            self.assertEqual(len(caught), 1)
            self.assertIsInstance(caught[0], RuntimeError)
        finally:
            harness.close()

    @staticmethod
    def _serve_catching(server, caught, caught_event):
        try:
            server.serve_forever()
        except BaseException as exc:
            caught.append(exc)
            caught_event.set()

    def test_acceptor_runtime_error_during_active_acquisition_waits_for_callback_then_stops_work(self):
        acquirer = BlockingAcquirer(block_indices={0})
        harness = ServerHarness(
            acquirer=acquirer, server_class=FailingAcceptServer, start=False
        )
        caught = []
        caught_event = threading.Event()
        harness.thread = threading.Thread(
            target=lambda: self._serve_catching(harness.server, caught, caught_event),
            name="test-active-accept-runtime",
        )
        active = None
        trigger = None
        harness.thread.start()
        try:
            active = harness.connect()
            request_bytes(active, "POST", "/v1/acquisitions/battery.voltage", {"mode": "passive"})
            self.assertTrue(acquirer.wait_started(0))
            harness.server.runtime_failure = True
            trigger = harness.connect()
            self.assertTrue(harness.server.failure_event.wait(1.0))
            self.assertEqual(acquirer.calls, ["passive"])
            self.assertEqual(acquirer.active, 1)
            acquirer.release(0)
            try:
                read_http(active, timeout=0.5)
            except (EOFError, OSError, socket.timeout, TimeoutError):
                pass
            self.assertTrue(caught_event.wait(1.0))
            self.assertEqual(acquirer.calls, ["passive"])
        finally:
            acquirer.release_all()
            for sock in (active, trigger):
                if sock is not None:
                    sock.close()
            harness.close()

    def test_transient_acceptor_oserror_is_nonfatal(self):
        harness = ServerHarness(server_class=FailingAcceptServer)
        try:
            harness.server.transient_failures = 1
            first = harness.connect()
            request_bytes(first, "GET", "/v1/status")
            second = harness.connect()
            request_bytes(second, "GET", "/v1/status")
            self.assertEqual(read_http(first)[0], 200)
            first.close()
            second.close()
            self.assertTrue(harness.thread.is_alive())
        finally:
            harness.close()

    def test_acceptor_fatal_closes_partial_valid_body_without_mutation(self):
        acquirer = BlockingAcquirer()
        harness = ServerHarness(
            acquirer=acquirer, server_class=BodyFailingAcceptServer, start=False
        )
        caught = []
        caught_event = threading.Event()
        harness.thread = threading.Thread(
            target=lambda: self._serve_catching(harness.server, caught, caught_event),
            name="test-partial-fatal",
        )
        partial = None
        trigger = None
        harness.thread.start()
        try:
            harness.server.request_io_timeout = 1.0
            partial = harness.connect()
            partial.sendall(
                b"POST /v1/acquisitions/battery.voltage HTTP/1.1\r\n"
                b"Host: local\r\nContent-Type: application/json\r\n"
                b"Content-Length: 64\r\n\r\n"
                b'{"mode":"wake_if_asleep"}'
            )
            self.assertTrue(harness.server.body_started.wait(1.0))
            harness.server.runtime_failure = True
            started = time.monotonic()
            trigger = harness.connect()
            self.assertTrue(harness.server.failure_event.wait(0.5))
            self.assertTrue(caught_event.wait(0.5), "fatal path waited for socket timeout")
            self.assertTrue(harness.server.post_finished.wait(0.5))
            self.assertLess(time.monotonic() - started, 0.8)
            raw = read_available(partial, timeout=0.2)
            self.assertNotIn(b'"reason":"transport_timeout"', raw)
            self.assertEqual(acquirer.calls, [])
            self.assertIsInstance(caught[0], RuntimeError)
        finally:
            acquirer.release_all()
            for sock in (partial, trigger):
                if sock is not None:
                    sock.close()
            harness.close()

    def test_interrupt_after_dequeue_releases_waiter_without_running_callback(self):
        jobs = []

        class InterruptBeforeCallback(api_server.UnixHTTPServer):
            def _execute_post(self, job):
                jobs.append(job)
                raise KeyboardInterrupt

        harness = ServerHarness(server_class=InterruptBeforeCallback, start=False)
        caught, caught_event = [], threading.Event()
        harness.thread = threading.Thread(
            target=lambda: self._serve_catching(harness.server, caught, caught_event)
        )
        harness.thread.start()
        try:
            sock = harness.connect()
            request_bytes(sock, "POST", "/v1/acquisitions/battery.voltage", {"mode": "passive"})
            self.assertTrue(caught_event.wait(1.0))
            self.assertIsInstance(caught[0], KeyboardInterrupt)
            self.assertTrue(jobs[0].done.wait(1.0))
            self.assertTrue(jobs[0].cancelled)
            self.assertEqual(harness.server._post_jobs.unfinished_tasks, 0)
            self.assertEqual(harness.acquirer.calls, [])
        finally:
            harness.close()

    def test_unexpected_worker_loss_reaches_serving_thread(self):
        for failure in ("exception", "return"):
            with self.subTest(failure=failure):
                entered = threading.Event()
                lose_worker = threading.Event()

                class LostWorkerServer(api_server.UnixHTTPServer):
                    request_worker_count = 1

                    def _worker_loop(self):
                        entered.set()
                        lose_worker.wait()
                        if failure == "exception":
                            raise RuntimeError("injected worker failure")

                harness = ServerHarness(server_class=LostWorkerServer, start=False)
                caught, caught_event = [], threading.Event()
                harness.thread = threading.Thread(
                    target=lambda: self._serve_catching(harness.server, caught, caught_event)
                )
                harness.thread.start()
                try:
                    self.assertTrue(entered.wait(1.0))
                    lose_worker.set()
                    self.assertTrue(caught_event.wait(1.0))
                    self.assertIsInstance(caught[0], RuntimeError)
                finally:
                    lose_worker.set()
                    harness.close()

    def test_server_lifetime_is_one_shot_without_spawning_another_pool(self):
        with running_server() as harness:
            helpers = tuple(harness.server._helper_threads)
            with self.assertRaisesRegex(RuntimeError, "one-shot"):
                harness.server.serve_forever()
            self.assertEqual(TelemetryClient(harness.socket_path).request("GET", "/v1/status")[0], 200)
            harness.server.shutdown()
            harness.thread.join(1.0)
            with self.assertRaisesRegex(RuntimeError, "one-shot"):
                harness.server.serve_forever()
            self.assertEqual(tuple(harness.server._helper_threads), helpers)

    def test_direct_close_without_start_and_external_shutdown_are_safe(self):
        tmp = tempfile.TemporaryDirectory(prefix="o2-direct-", dir=TMP_BASE)
        path = str(pathlib.Path(tmp.name) / "never-started.sock")
        broker = TelemetryBroker(acquirer=BlockingAcquirer())
        server = api_server.UnixHTTPServer(path, broker)
        server.server_close()
        # Direct server_close is deliberately separate from serve_unix's
        # ownership cleanup; it must be safe even when serve_forever never ran.
        pathlib.Path(path).unlink(missing_ok=True)
        broker.close()
        tmp.cleanup()

        with running_server() as harness:
            harness.server.shutdown()
            harness.thread.join(1.0)
            self.assertFalse(harness.thread.is_alive())

    def test_external_shutdown_waits_for_active_stream_and_cancels_queued_mutation(self):
        acquirer = BlockingAcquirer(block_indices={0, 1})
        harness = ServerHarness(acquirer=acquirer)
        first = None
        second = None
        first_reader = None
        second_reader = None
        shutdown = None
        first_done = threading.Event()
        first_result = []
        second_result = []

        def read_first():
            try:
                first_result.append(read_http(first))
            except (EOFError, OSError, socket.timeout, TimeoutError):
                first_result.append(None)
            finally:
                first_done.set()

        def read_second():
            try:
                second_result.append(read_http(second, timeout=0.8))
            except (EOFError, OSError, socket.timeout, TimeoutError):
                second_result.append(None)

        try:
            first = harness.connect()
            request_bytes(first, "POST", "/v1/acquisitions/battery.voltage", {"mode": "passive"})
            self.assertTrue(acquirer.wait_started(0))
            second = harness.connect()
            request_bytes(second, "POST", "/v1/acquisitions/battery.voltage", {"mode": "passive"})
            first_reader = threading.Thread(target=read_first)
            second_reader = threading.Thread(target=read_second)
            first_reader.start()
            second_reader.start()
            threading.Event().wait(0.05)
            shutdown = threading.Thread(target=harness.server.shutdown)
            shutdown.start()
            self.assertFalse(first_done.wait(0.1), "shutdown finished the active stream early")
            self.assertEqual(acquirer.calls, ["passive"])
            acquirer.release(0)
            first_reader.join(1.0)
            shutdown.join(1.0)
            self.assertTrue(first_done.is_set())
            self.assertEqual(first_result[0][0], 200)
            self.assertEqual(acquirer.calls, ["passive"])
        finally:
            acquirer.release_all()
            for reader in (first_reader, second_reader):
                if reader is not None:
                    reader.join(1.0)
            for sock in (first, second):
                if sock is not None:
                    sock.close()
            if shutdown is not None:
                shutdown.join(1.0)
            harness.close()

    def test_disconnect_and_normal_protocol_error_allow_later_request_recovery(self):
        acquirer = BlockingAcquirer()
        with running_server(acquirer=acquirer) as harness:
            disconnected = harness.connect()
            request_bytes(
                disconnected,
                "POST",
                "/v1/acquisitions/battery.voltage",
                {"mode": "passive"},
            )
            disconnected.close()
            self.assertTrue(acquirer.wait_started(0))
            malformed = harness.connect()
            malformed.sendall(b"NOT-HTTP\r\n\r\n")
            malformed.close()
            client = TelemetryClient(harness.socket_path, timeout=0.8)
            status, payload = client.request("GET", "/v1/status")
            self.assertEqual(status, 200)
            self.assertIsInstance(payload, dict)
            self.assertEqual(acquirer.calls, ["passive"])


SUBPROCESS_SCRIPT = r'''
import os
import pathlib
import sys
import threading
from dataclasses import replace

from lib import can_wake
from lib.diagnostic_safety import interrupt_on_termination
from projects.vehicle_data import api_server
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.metrics import METRICS

HANDSHAKE = int(os.environ["O2_HANDSHAKE_FD"])
SOCKET = pathlib.Path(os.environ["O2_SOCKET"])
MODE = os.environ["O2_MODE"]

def report(message):
    os.write(HANDSHAKE, (message + "\n").encode())

def control(expected):
    received = sys.stdin.readline().strip()
    if received != expected:
        raise AssertionError((expected, received))

class FakeWakeTurn:
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return None

class FakeWakeSession:
    def __init__(self, state):
        self.state = state
        self.closed = False
    def trigger(self):
        self.state["started"].set()
        report("ACTIVE main=" + str(threading.current_thread() is threading.main_thread()))
        self.state["block"].wait()
        raise AssertionError("fake wake was unexpectedly released")
    def close(self):
        if self.closed:
            return
        self.closed = True
        report("RESTORE_WAIT")
        self.state["restoring"].set()
        control("restore")
        self.state["restored"] = True
        self.state["owner"].restored = True
        report("RESTORED")

class FakeAcquirer:
    channel = "can7"
    def __init__(self):
        self.started = threading.Event()
        self.block = threading.Event()
        self.restoring = threading.Event()
        self.restored = MODE in ("idle", "read")
        self.calls = []
        if MODE == "wake":
            can_wake.can_handoff.active_turn = lambda _role: FakeWakeTurn()
            state = {
                "started": self.started,
                "block": self.block,
                "restoring": self.restoring,
                "restored": False,
                "owner": self,
            }
            can_wake._open_wake_session = lambda *_args, **_kwargs: FakeWakeSession(state)
            self._wake_state = state
    def acquire(self, mode):
        self.calls.append(mode)
        self.started.set()
        try:
            if MODE == "wake":
                return can_wake.wake_once("b-can", prearm_check=lambda: ())
            if MODE == "interrupt":
                report("ACTIVE main=" + str(threading.current_thread() is threading.main_thread()))
                control("interrupt")
                raise KeyboardInterrupt
            raise AssertionError("unexpected acquisition in " + MODE)
        finally:
            if MODE == "interrupt":
                self.restored = True
                report("RESTORED")
    def status_snapshot(self):
        return {"channel": self.channel, "adapter_present": True, "up": True,
                "bitrate": 125000, "fd_enabled": False, "one_shot": False,
                "listen_only": True, "controller_state": "ERROR-ACTIVE",
                "topology": {"bus": "b-can", "usable": True, "reason": ""},
                "active_inhibits": []}

state = FakeAcquirer()
MAIN_THREAD_ID = None
class HandshakeServer(api_server.UnixHTTPServer):
    def serve_forever(self, *args, **kwargs):
        global MAIN_THREAD_ID
        MAIN_THREAD_ID = threading.get_ident()
        report("READY")
        return super().serve_forever(*args, **kwargs)
    def _submit_post(self, handler):
        job = super()._submit_post(handler)
        if handler.headers.get("X-O2-Queued") == "yes":
            report("QUEUED")
        return job
    def server_close(self):
        report("SERVER_CLOSE restored=" + str(state.restored) + " socket=" + str(SOCKET.exists()) + " calls=" + str(len(state.calls)))
        return super().server_close()

api_server.UnixHTTPServer = HandshakeServer
definitions = dict(METRICS)
definitions["battery.voltage"] = replace(
    METRICS["battery.voltage"], passive_min_interval_seconds=0.0,
    wake_min_interval_seconds=0.0,
)
broker = TelemetryBroker(acquirer=state, definitions=definitions, collector_interval_seconds=0.0)
if MODE == "read":
    def blocked_status():
        report("READ_ACTIVE worker=" + str(threading.get_ident()) + " main=" + str(MAIN_THREAD_ID)
               + " owns_lock=" + str(broker._lock._is_owned()))
        threading.Event().wait()
        raise AssertionError("blocked reader was unexpectedly released")
    broker.status_response = blocked_status
original_close = broker.close
def close():
    report("BEFORE_CLOSE restored=" + str(state.restored) + " socket=" + str(SOCKET.exists()))
    return original_close()
broker.close = close
try:
    with interrupt_on_termination() as termination:
        try:
            api_server.serve_unix(broker, str(SOCKET))
        finally:
            termination.begin_cleanup()
            report("SERVE_FINALLY restored=" + str(state.restored) + " socket=" + str(SOCKET.exists()))
            broker.close()
except KeyboardInterrupt:
    if MODE != "interrupt":
        raise  # Real SIGTERM cases must retain the production -SIGINT exit.
    report("INTERRUPT_PROPAGATED")  # Unit case catches its explicitly raised exception.
'''


class HandshakeReader:
    def __init__(self, fd: int):
        self.fd = fd
        self.buffer = bytearray()

    def line(self, timeout: float = 1.0) -> str:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("subprocess handshake timed out")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                raise AssertionError("subprocess handshake timed out")
            chunk = os.read(self.fd, 4096)
            if not chunk:
                raise AssertionError("subprocess closed handshake pipe")
            self.buffer.extend(chunk)
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        return line.decode()

    def rest(self, timeout: float = 1.0) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("subprocess handshake EOF timed out")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                raise AssertionError("subprocess handshake EOF timed out")
            chunk = os.read(self.fd, 4096)
            if not chunk:
                return bytes(self.buffer).decode()
            self.buffer.extend(chunk)


def start_signal_child(tmp_name: str, mode: str, repo_root=REPO_ROOT):
    read_fd, write_fd = os.pipe()
    socket_path = str(pathlib.Path(tmp_name) / "child.sock")
    env = {
        **os.environ,
        "O2_HANDSHAKE_FD": str(write_fd),
        "O2_SOCKET": socket_path,
        "O2_MODE": mode,
        "PYTHONPATH": str(repo_root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", SUBPROCESS_SCRIPT],
        cwd=repo_root,
        env=env,
        pass_fds=(write_fd,),
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(write_fd)
    return process, read_fd, socket_path


class SubprocessTerminationTests(unittest.TestCase):
    def _signal_case(self, mode: str, *, repo_root=REPO_ROOT, concurrent_reads=True):
        tmp = tempfile.TemporaryDirectory(prefix="o2-signal-", dir=TMP_BASE)
        process, read_fd, socket_path = start_signal_child(tmp.name, mode, repo_root)
        reader = HandshakeReader(read_fd)
        lines = []
        clients = []
        try:
            lines.append(reader.line())
            self.assertEqual(lines[-1], "READY")
            if mode in ("wake", "interrupt"):
                sock = unix_socket(socket_path)
                clients.append(sock)
                request_bytes(sock, "POST", "/v1/acquisitions/battery.voltage",
                              {"mode": "wake_if_asleep" if mode == "wake" else "passive"})
                lines.append(reader.line())
                self.assertEqual(lines[-1], "ACTIVE main=True", lines)
            elif mode == "read":
                sock = unix_socket(socket_path)
                clients.append(sock)
                request_bytes(sock, "GET", "/v1/status")
                lines.append(reader.line())
                self.assertTrue(lines[-1].startswith("READ_ACTIVE "), lines)
                fields = dict(item.split("=", 1) for item in lines[-1].split()[1:])
                self.assertEqual(fields["owns_lock"], "False")
                self.assertEqual(fields["worker"] != fields["main"], concurrent_reads)
            else:
                # READY is inside serve_unix's try; this also proves loop startup.
                sock = unix_socket(socket_path)
                clients.append(sock)
                request_bytes(sock, "GET", "/v1/metrics")
                self.assertEqual(read_http(sock)[0], 200)

            if mode == "interrupt":
                queued = unix_socket(socket_path)
                clients.append(queued)
                request_bytes(queued, "POST", "/v1/acquisitions/battery.voltage",
                              {"mode": "passive"}, headers={"X-O2-Queued": "yes"})
                lines.append(reader.line())
                self.assertEqual(lines[-1], "QUEUED")
            started_signal = time.monotonic()
            if mode == "interrupt":
                process.stdin.write(b"interrupt\n")
                process.stdin.flush()
            else:
                process.terminate()
            if mode == "wake":
                lines.append(reader.line())
                self.assertEqual(lines[-1], "RESTORE_WAIT")
                os.kill(process.pid, signal.SIGTERM)
                os.kill(process.pid, signal.SIGTERM)
                process.stdin.write(b"restore\n")  # Restore cannot finish before these signals.
                process.stdin.flush()
            return_code = process.wait(timeout=1.5)
            elapsed = time.monotonic() - started_signal
            lines.extend(reader.rest().splitlines())
            stderr = process.stderr.read().decode()
            # Only the explicit unit exception is caught by its test harness.
            self.assertEqual(return_code, 0 if mode == "interrupt" else -signal.SIGINT,
                             (lines, stderr))
            self.assertLess(elapsed, 2.0)
            server_close = next(line for line in lines if line.startswith("SERVER_CLOSE"))
            self.assertIn("restored=True", server_close)
            self.assertIn("socket=True", server_close)
            self.assertIn("calls=" + ("1" if mode in ("wake", "interrupt") else "0"), server_close)
            before = next(line for line in lines if line.startswith("BEFORE_CLOSE"))
            self.assertIn("restored=True", before)
            self.assertIn("socket=False", before)
            self.assertLess(lines.index(server_close), lines.index(before))
            self.assertFalse(pathlib.Path(socket_path).exists())
            if mode in ("wake", "interrupt"):
                self.assertLess(lines.index("RESTORED"), lines.index(server_close))
            if mode == "interrupt":
                self.assertIn("INTERRUPT_PROPAGATED", lines)
            return elapsed
        finally:
            for sock in clients:
                sock.close()
            if process.poll() is None:
                process.kill()
                process.wait(1.0)
            process.stdin.close()
            process.stderr.close()
            os.close(read_fd)
            tmp.cleanup()

    def test_keyboard_interrupt_on_main_unwinds_serve_unix_and_cancels_queued_post(self):
        # The child is a real main thread; a bounded parent prevents a regression
        # from hanging pytest itself. Unlike the SIGTERM cases, this unit catches
        # its explicitly raised KeyboardInterrupt only after serve_unix unwinds.
        self._signal_case("interrupt")

    def test_idle_main_thread_termination_unlinks_before_broker_close(self):
        self._signal_case("idle")

    def test_real_wake_once_restores_under_repeated_term_before_server_close(self):
        # The child patches only can_wake's ownership/session seams, so the real
        # wake_once termination guard and exact serve_unix finally are exercised
        # without opening a CAN socket. Parent acknowledgement keeps restoration
        # inside the shield until both repeated TERM signals have been sent.
        self._signal_case("wake")

    def test_blocked_read_without_broker_lock_does_not_delay_signal_exit(self):
        self._signal_case("read")


if __name__ == "__main__":
    unittest.main()
