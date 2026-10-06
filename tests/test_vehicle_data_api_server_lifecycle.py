"""Server-lifecycle tests for the Unix telemetry API."""

from __future__ import annotations

import pathlib
import socket
import tempfile
import threading
import time
import unittest

from projects.vehicle_data import api_server
from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.broker import TelemetryBroker
from tests.vehicle_data_api_concurrency_support import (
    BodyFailingAcceptServer,
    BlockingAcquirer,
    FailingAcceptServer,
    ServerHarness,
    TMP_BASE,
    read_available,
    read_http,
    request_bytes,
    running_server,
    serve_catching,
)


class ApiConcurrencyTests(unittest.TestCase):
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

    def test_unexpected_acceptor_runtime_error_propagates_and_blocks_later_mutations(self):
        harness = ServerHarness(server_class=FailingAcceptServer, start=False)
        caught = []
        caught_event = threading.Event()
        harness.thread = threading.Thread(
            target=lambda: serve_catching(harness.server, caught, caught_event),
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

    def test_acceptor_runtime_error_during_active_acquisition_waits_for_callback_then_stops_work(self):
        acquirer = BlockingAcquirer(block_indices={0})
        harness = ServerHarness(
            acquirer=acquirer, server_class=FailingAcceptServer, start=False
        )
        caught = []
        caught_event = threading.Event()
        harness.thread = threading.Thread(
            target=lambda: serve_catching(harness.server, caught, caught_event),
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
            target=lambda: serve_catching(harness.server, caught, caught_event),
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
            target=lambda: serve_catching(harness.server, caught, caught_event)
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
                    target=lambda: serve_catching(harness.server, caught, caught_event)
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


if __name__ == "__main__":
    unittest.main()
