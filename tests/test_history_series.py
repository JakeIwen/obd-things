"""GET /v1/history/series: validation, the bounded query, the memo and the broker route."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from projects.vehicle_data import history_recent, history_series
from projects.vehicle_data.api import TelemetryClient, UnixHTTPServer
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.historian import TelemetryHistorian
from tests.test_vehicle_data import FakeAcquirer, FakeClock
from tests.test_vehicle_historian import available, definition, snapshot

NOW = datetime(2026, 11, 23, 12, 7, 31, 250000, tzinfo=timezone.utc)
END = datetime(2026, 11, 23, 12, 7, 0, tzinfo=timezone.utc)
METRIC = "vehicle.speed"


class FakeHistorian:
    def __init__(self, points=()):
        self.calls = []
        self.points = list(points)

    def metric_series(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return {"points": [dict(p) for p in self.points]}


def point(seconds_before_end, value, minimum=0.0, maximum=9.0):
    return {"at": (END - timedelta(seconds=seconds_before_end)).isoformat(),
            "value": value, "minimum": minimum, "maximum": maximum}


class ParseTests(unittest.TestCase):
    def test_a_known_metric_and_served_span_parse(self):
        for span in ("1h", "6h", "24h", "7d"):
            self.assertEqual(history_series.parse_query(f"metric={METRIC}&span={span}"), (METRIC, span))
        self.assertEqual(history_series.parse_query("span=1h&metric=engine.rpm"), ("engine.rpm", "1h"))

    def test_every_recent_metric_is_accepted_and_nothing_else(self):
        for name in history_recent.METRICS:
            history_series.parse_query(f"metric={name}&span=1h")
        for query in (
            "metric=tire.pressure.fl&span=1h",
            "metric=&span=1h",
            f"metric={METRIC}&span=2h",
            f"metric={METRIC}&span=15m",  # the browser's ring serves it
            f"metric={METRIC}",
            "span=1h",
            "",
            f"metric={METRIC}&span=1h&extra=1",
            f"metric={METRIC}&metric=engine.rpm&span=1h",
            f"metric={METRIC}&span=1h&span=6h",
            "metric",
        ):
            with self.subTest(query=query):
                with self.assertRaises(history_series.InvalidRequest):
                    history_series.parse_query(query)

    def test_spans_are_bounded_and_whole_minutes(self):
        self.assertEqual(sorted(history_series.SPANS), ["1h", "24h", "6h", "7d"])
        for span in history_series.SPANS.values():
            self.assertLessEqual(span["seconds"] // span["bucket_seconds"], 240)
            self.assertEqual(span["bucket_seconds"] % history_series.ALIGN_SECONDS, 0)


class QueryTests(unittest.TestCase):
    def test_end_is_the_last_whole_minute_and_never_later_than_now(self):
        historian = FakeHistorian()
        product = history_series.query(historian, METRIC, "6h", NOW)
        (name, kwargs), = historian.calls
        self.assertEqual(name, METRIC)
        self.assertEqual(kwargs, {"end": END, "window_seconds": 21600, "bucket_seconds": 120, "max_points": 180})
        self.assertLessEqual(END, NOW)
        self.assertEqual(product["end_at"], END.isoformat())
        self.assertEqual(product["start_at"], (END - timedelta(hours=6)).isoformat())
        self.assertEqual(product["queried_at"], NOW.isoformat())

    def test_points_keep_numeric_means_and_drop_the_partial_bucket_at_the_end(self):
        historian = FakeHistorian([
            point(240, 10.0, 8.0, 12.0),
            point(120, None),
            point(60, 11, 9, 13),
            point(0, 99.0),  # raw samples exactly on the boundary
        ])
        product = history_series.query(historian, METRIC, "1h", NOW)
        self.assertEqual(product["points"], [
            {"at": point(240, 0)["at"], "value": 10.0, "min": 8.0, "max": 12.0},
            {"at": point(60, 0)["at"], "value": 11, "min": 9, "max": 13},
        ])
        self.assertEqual((product["window_seconds"], product["bucket_seconds"]), (3600, 60))


class MemoTests(unittest.TestCase):
    def service(self):
        self.now = NOW
        return history_series.SeriesService(clock=lambda: self.now)

    def test_a_second_call_within_the_ttl_reuses_the_product_and_one_after_it_queries_again(self):
        service, historian = self.service(), FakeHistorian([point(120, 5.0)])
        first = service.respond(historian, f"metric={METRIC}&span=1h")
        self.now = NOW + timedelta(seconds=history_series.CACHE_SECONDS - 1)
        second = service.respond(historian, f"span=1h&metric={METRIC}")
        self.assertEqual(len(historian.calls), 1)
        self.assertEqual(first, second)
        service.respond(historian, f"metric={METRIC}&span=6h")
        service.respond(historian, f"metric=engine.rpm&span=1h")
        self.assertEqual(len(historian.calls), 3, "each (metric, span) has its own memo entry")
        self.now = NOW + timedelta(seconds=history_series.CACHE_SECONDS)
        status, third = service.respond(historian, f"metric={METRIC}&span=1h")
        self.assertEqual((status, len(historian.calls)), (200, 4))
        self.assertEqual(third["queried_at"], self.now.isoformat())

    def test_concurrent_requests_for_one_key_share_one_query(self):
        service = self.service()
        release = threading.Event()
        historian = FakeHistorian([point(120, 5.0)])
        original = historian.metric_series

        def slow(name, **kwargs):
            release.wait(2)
            return original(name, **kwargs)

        historian.metric_series = slow
        results = []
        threads = [threading.Thread(target=lambda: results.append(service.respond(historian, f"metric={METRIC}&span=7d")))
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        release.set()
        for thread in threads:
            thread.join()
        self.assertEqual(len(historian.calls), 1)
        self.assertEqual({status for status, _ in results}, {200})

    def test_refusals_never_query_and_a_failed_query_is_not_memoised(self):
        service, historian = self.service(), FakeHistorian()
        status, payload = service.respond(historian, f"metric={METRIC}&span=15m")
        self.assertEqual((status, payload["reason"], payload["available"]), (400, "invalid_request", False))
        self.assertEqual(historian.calls, [])
        broken = mock.Mock()
        broken.metric_series.side_effect = RuntimeError("db busy")
        self.assertEqual(service.respond(broken, f"metric={METRIC}&span=1h")[0], 503)
        self.assertEqual(service.respond(broken, f"metric={METRIC}&span=1h")[0], 503)
        self.assertEqual(broken.metric_series.call_count, 2)

    def test_no_historian_is_history_disabled_after_validation(self):
        service = self.service()
        status, payload = service.respond(None, f"metric={METRIC}&span=1h")
        self.assertEqual((status, payload["reason"]), (200, "history_disabled"))
        self.assertEqual(service.respond(None, "metric=x&span=1h")[0], 400)


class RealHistorianTests(unittest.TestCase):
    def test_long_spans_return_bucket_start_labels_with_min_and_max(self):
        speed = definition(METRIC, "mph")
        start = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            with TelemetryHistorian(pathlib.Path(tmp) / "h.sqlite3") as historian:
                for index in range(0, 2 * 24 * 60, 5):  # a sample every 5 minutes for two days
                    when = start + timedelta(minutes=index)
                    historian.ingest_snapshot(
                        snapshot(when, [speed], {METRIC: available(speed, float(index % 60), when)}),
                        captured_at=when,
                    )
                now = datetime(2026, 11, 23, 11, 59, 40, tzinfo=timezone.utc)
                for span in ("1h", "6h", "24h", "7d"):
                    with self.subTest(span=span):
                        product = history_series.query(historian, METRIC, span, now)
                        self.assertLessEqual(len(product["points"]), history_series.SPANS[span]["seconds"]
                                             // history_series.SPANS[span]["bucket_seconds"])
                        if span in ("24h", "7d"):
                            self.assertTrue(product["points"])
                        for entry in product["points"]:
                            at = datetime.fromisoformat(entry["at"])
                            self.assertLess(at, datetime.fromisoformat(product["end_at"]))
                            self.assertLessEqual(entry["min"], entry["value"])
                            self.assertLessEqual(entry["value"], entry["max"])
                json.dumps(product)


class BrokerRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vt-series-", dir="/tmp")
        socket_path = str(pathlib.Path(self.tmp.name) / "api.sock")
        self.historian = FakeHistorian([point(120, 5.0)])
        insights = mock.Mock()
        insights.historian = self.historian
        self.broker = TelemetryBroker(acquirer=FakeAcquirer(), monotonic=FakeClock(), insights=insights)
        self.server = UnixHTTPServer(socket_path, self.broker)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = TelemetryClient(socket_path)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_series_is_served_with_its_query_string_and_refusals_are_400(self):
        status, payload = self.client.request("GET", f"/v1/history/series?metric={METRIC}&span=24h")
        self.assertEqual(status, 200)
        self.assertEqual((payload["metric"], payload["span"], payload["bucket_seconds"]), (METRIC, "24h", 360))
        self.assertEqual(len(payload["points"]), 1)
        self.assertEqual(self.historian.calls[0][1]["max_points"], 240)
        for query in ("", f"?metric={METRIC}&span=15m", f"?metric={METRIC}&span=1h&x=1", "?metric=nope&span=1h"):
            with self.subTest(query=query):
                status, payload = self.client.request("GET", "/v1/history/series" + query)
                self.assertEqual((status, payload["reason"], payload["available"]), (400, "invalid_request", False))
        self.assertEqual(len(self.historian.calls), 1)

    def test_history_disabled(self):
        self.broker.insights = None
        status, payload = self.client.request("GET", f"/v1/history/series?metric={METRIC}&span=1h")
        self.assertEqual((status, payload["reason"]), (200, "history_disabled"))


if __name__ == "__main__":
    unittest.main()
