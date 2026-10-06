"""Deterministic concurrency and lifecycle regressions for the Unix telemetry API.

These tests intentionally use only fake telemetry backends.  The subprocess cases
exercise the production ``interrupt_on_termination`` + ``serve_unix`` try/finally
path, rather than invoking vehicle runtime factories or touching CAN.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import pathlib
import queue
import socket
import tempfile
import threading
import time
from dataclasses import replace

from projects.vehicle_data import api_server
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.event_history import EventReader
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


def serve_catching(server, caught, caught_event):
    try:
        server.serve_forever()
    except BaseException as exc:
        caught.append(exc)
        caught_event.set()
