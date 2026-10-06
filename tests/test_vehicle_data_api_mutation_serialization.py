"""Mutation serialization tests for the Unix telemetry API."""

from __future__ import annotations

import pathlib
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

from projects.vehicle_data import api_server
from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.event_history import EventReader
from projects.vehicle_data.historian import TelemetryHistorian
from tests.vehicle_data_api_concurrency_support import (
    BlockingAcquirer,
    MutableClock,
    PostReadyServer,
    TMP_BASE,
    prime_event_reader,
    read_http,
    request_bytes,
    running_server,
    valid_observation,
    valid_oil_change,
)


class ApiConcurrencyTests(unittest.TestCase):
    def _install_observation_callbacks(self, harness, acquirer, callback_log):
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

    def _start_blocked_acquisition(self, harness, acquirer):
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
        return acquisition

    def _assert_cache_gets_remain_available(self, harness, acquirer):
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

    def _queue_mutations_while_source_is_blocked(self, harness, callback_log):
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
        return queued_posts, responses

    def _finish_mutation_serialization(
        self, harness, acquirer, acquisition, queued_posts, responses, callback_log
    ):
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
            self._install_observation_callbacks(harness, acquirer, callback_log)
            acquisition = self._start_blocked_acquisition(harness, acquirer)
            self._assert_cache_gets_remain_available(harness, acquirer)
            queued_posts, responses = self._queue_mutations_while_source_is_blocked(
                harness, callback_log
            )
            self._finish_mutation_serialization(
                harness, acquirer, acquisition, queued_posts, responses, callback_log
            )

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


if __name__ == "__main__":
    unittest.main()
