"""Read-concurrency tests for the Unix telemetry API."""

from __future__ import annotations

import io
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from projects.vehicle_data import api_server
from projects.vehicle_data.api_client import TelemetryClient
from tests.vehicle_data_api_concurrency_support import (
    BlockingAcquirer,
    BodyPhaseServer,
    CountingServer,
    MutableClock,
    ServerHarness,
    SingleWorkerServer,
    read_available,
    read_http,
    request_bytes,
    running_server,
    wait_until,
)


class ApiConcurrencyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
