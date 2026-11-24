"""GET /v1/history/series: validation, the bounded query, the cache-only refresh and the route."""

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
from projects.vehicle_data.insights import DEFAULT_HISTORY_METRICS
from tests.test_vehicle_data import FakeAcquirer, FakeClock
from tests.test_vehicle_historian import available, definition, snapshot

NOW = datetime(2026, 11, 23, 12, 7, 31, 250000, tzinfo=timezone.utc)
END = datetime(2026, 11, 23, 12, 7, 0, tzinfo=timezone.utc)
METRIC = "vehicle.speed"
TRENDS = ("battery.voltage", "tire.pressure.fl")


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

    def test_every_recent_and_trend_metric_is_accepted_and_nothing_else(self):
        allowed = history_series.accepted_metrics(DEFAULT_HISTORY_METRICS)
        self.assertTrue(set(history_recent.METRICS) | set(DEFAULT_HISTORY_METRICS) <= set(allowed))
        self.assertEqual(len(allowed), len(set(allowed)))
        for name in allowed:
            history_series.parse_query(f"metric={name}&span=1h", allowed)
        history_series.parse_query("metric=battery.voltage&span=7d", allowed)
        with self.assertRaises(history_series.InvalidRequest):
            history_series.parse_query("metric=tire.pressure.fl&span=1h")  # default is the sparklines only
        for query in (
            "metric=tire.pressure.xx&span=1h",
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
                    history_series.parse_query(query, allowed)

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


def drain(service, historian):
    """Refresh until a pass finds nothing due (a cold start takes more than one capped pass)."""

    passes = 0
    while True:
        before = len(historian.calls)
        service.refresh(historian)
        if len(historian.calls) == before:
            return passes
        passes += 1


class RefreshTests(unittest.TestCase):
    def service(self, metrics=lambda: history_series.accepted_metrics(TRENDS)):
        self.now = NOW
        return history_series.SeriesService(clock=lambda: self.now, metrics=metrics)

    def test_a_request_before_the_first_refresh_is_pending_and_never_queries(self):
        service, historian = self.service(), FakeHistorian([point(120, 5.0)])
        status, payload = service.respond(True, f"metric={METRIC}&span=1h")
        self.assertEqual((status, payload["available"], payload["reason"]), (200, False, "pending"))
        self.assertEqual(historian.calls, [])

    def test_refresh_fills_every_key_and_respond_reads_memory_only(self):
        service, historian = self.service(), FakeHistorian([point(120, 5.0)])
        drain(service, historian)
        keys = len(history_series.accepted_metrics(TRENDS)) * len(history_series.SPANS)
        self.assertEqual(len(historian.calls), keys)
        for name in ("battery.voltage", "tire.pressure.fl", METRIC):
            status, payload = service.respond(True, f"metric={name}&span=7d")
            self.assertEqual((status, payload["available"], payload["metric"]), (200, True, name))
        self.assertEqual(len(historian.calls), keys)

    def test_each_span_is_recomputed_only_when_its_bucket_has_advanced(self):
        service, historian = self.service(), FakeHistorian([point(120, 5.0)])
        drain(service, historian)
        metrics = len(historian.calls) // len(history_series.SPANS)

        def buckets_after(seconds):
            self.now = NOW + timedelta(seconds=seconds)
            historian.calls.clear()
            drain(service, historian)
            return sorted({kwargs["bucket_seconds"] for _, kwargs in historian.calls})

        self.assertEqual(buckets_after(60), [60, 120], "12:08:31 crossed a minute and a 2-minute edge only")
        self.assertEqual(len(historian.calls), 2 * metrics)
        self.assertEqual(buckets_after(90), [60], "12:09:01 crossed a minute edge only")
        self.assertEqual(buckets_after(7 * 60), [60, 120, 360])  # 12:14:31 crosses the 12:12 edge too
        self.assertEqual(buckets_after(7 * 60 + 5), [], "nothing is due inside the same minute")
        self.assertEqual(buckets_after(2520 * 2), [60, 120, 360, 2520])

    def test_a_failed_refresh_serves_the_old_product_until_its_age_bound(self):
        service, historian = self.service(), FakeHistorian([point(120, 5.0)])
        drain(service, historian)
        broken = mock.Mock()
        broken.metric_series.side_effect = RuntimeError("db busy")
        self.now = NOW + timedelta(seconds=90)
        service.refresh(broken)
        query = f"metric={METRIC}&span=1h"
        status, payload = service.respond(True, query)
        self.assertEqual((status, payload["queried_at"]), (200, NOW.isoformat()))
        bound = max(2 * history_series.SPANS["1h"]["bucket_seconds"], 3 * history_series.REFRESH_SECONDS)
        self.now = NOW + timedelta(seconds=bound)
        status, payload = service.respond(True, query)
        self.assertEqual((status, payload["reason"]), (503, "history_unavailable"))
        self.assertEqual(service.respond(True, f"metric={METRIC}&span=7d")[0], 200)
        self.now = NOW + timedelta(seconds=bound)
        service.refresh(historian)  # recovery
        self.assertEqual(service.respond(True, query)[0], 200)

    def test_a_pass_stops_at_its_first_failed_query(self):
        service = self.service()
        broken = mock.Mock()
        broken.metric_series.side_effect = RuntimeError("db busy")
        service.refresh(broken)
        self.assertEqual(broken.metric_series.call_count, 1, "one busy timeout, not one per key")
        healthy = FakeHistorian([point(120, 5.0)])
        drain(service, healthy)  # the untried keys were left due
        keys = len(history_series.accepted_metrics(TRENDS)) * len(history_series.SPANS)
        self.assertEqual(len(healthy.calls), keys)

    def test_a_pass_is_capped_finest_span_first_and_the_rest_follows(self):
        service, historian = self.service(), FakeHistorian([point(120, 5.0)])
        service.refresh(historian)
        cap = history_series.MAX_KEYS_PER_PASS
        self.assertEqual(len(historian.calls), cap)
        first = [kwargs["bucket_seconds"] for _, kwargs in historian.calls]
        self.assertEqual(first, sorted(first), "the finest spans are served first")
        self.assertEqual(drain(service, historian), 1, "the all-due pass finishes in a second pass")
        self.assertEqual(len(historian.calls), len(history_series.accepted_metrics(TRENDS)) * len(history_series.SPANS))

    def test_a_product_is_unavailable_once_old_even_with_no_refresh_failure(self):
        service, historian = self.service(), FakeHistorian([point(120, 5.0)])
        drain(service, historian)
        query = f"metric={METRIC}&span=1h"
        bound = max(2 * history_series.SPANS["1h"]["bucket_seconds"], 3 * history_series.REFRESH_SECONDS)
        self.now = NOW + timedelta(seconds=bound - 1)
        self.assertEqual(service.respond(True, query)[0], 200)
        self.now = NOW + timedelta(seconds=bound)  # the producer stalled: refresh() never runs
        status, payload = service.respond(True, query)
        self.assertEqual((status, payload["reason"]), (503, "history_unavailable"))

    def test_a_product_dated_in_the_future_is_not_served_and_is_rebuilt(self):
        service, historian = self.service(), FakeHistorian([point(120, 5.0)])
        drain(service, historian)
        query = f"metric={METRIC}&span=1h"
        self.now = NOW - timedelta(hours=1)  # the wall clock stepped back
        self.assertEqual(service.respond(True, query)[0], 503)
        historian.calls.clear()
        drain(service, historian)
        self.assertEqual(len(historian.calls), len(history_series.accepted_metrics(TRENDS)) * len(history_series.SPANS))
        status, payload = service.respond(True, query)
        self.assertEqual((status, payload["queried_at"]), (200, self.now.isoformat()))

    def test_failure_with_no_earlier_product_is_unavailable_not_pending(self):
        service = self.service()
        broken = mock.Mock()
        broken.metric_series.side_effect = RuntimeError("db busy")
        service.refresh(broken)
        query = f"metric={METRIC}&span=7d"
        self.assertEqual(service.respond(True, query)[1]["reason"], "history_unavailable")
        service.refresh(FakeHistorian())
        self.assertEqual(service.respond(True, query)[1]["reason"], "pending")

    def test_products_are_bounded_and_a_metric_that_leaves_the_allowlist_is_dropped(self):
        metrics = ["battery.voltage", "engine.rpm"]
        service = self.service(metrics=lambda: tuple(metrics))
        historian = FakeHistorian([point(120, 5.0)])
        drain(service, historian)
        self.assertEqual(len(service._products), 2 * len(history_series.SPANS))
        metrics.remove("engine.rpm")
        service.refresh(historian)
        self.assertEqual(len(service._products), len(history_series.SPANS))
        self.assertEqual(service.respond(True, "metric=engine.rpm&span=1h")[0], 400)

    def test_refusals_and_no_historian(self):
        service = self.service()
        status, payload = service.respond(True, f"metric={METRIC}&span=15m")
        self.assertEqual((status, payload["reason"], payload["available"]), (400, "invalid_request", False))
        status, payload = service.respond(False, f"metric={METRIC}&span=1h")
        self.assertEqual((status, payload["reason"]), (200, "history_disabled"))
        self.assertEqual(service.respond(False, "metric=x&span=1h")[0], 400)

    def test_a_slow_refresh_does_not_block_requests(self):
        service = self.service()
        started, release = threading.Event(), threading.Event()
        historian = FakeHistorian([point(120, 5.0)])
        original = historian.metric_series

        def slow(name, **kwargs):
            started.set()
            release.wait(5)
            return original(name, **kwargs)

        historian.metric_series = slow
        worker = threading.Thread(target=service.refresh, args=(historian,))
        worker.start()
        self.assertTrue(started.wait(2))
        self.assertEqual(service.respond(True, f"metric={METRIC}&span=1h")[1]["reason"], "pending")
        release.set()
        worker.join()


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
        insights.history_metrics = DEFAULT_HISTORY_METRICS
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

    def test_get_is_pending_until_the_supplemental_refresh_and_never_queries(self):
        path = "/v1/history/series?metric=battery.voltage&span=24h"
        status, payload = self.client.request("GET", path)
        self.assertEqual((status, payload["available"], payload["reason"]), (200, False, "pending"))
        self.assertEqual(self.historian.calls, [])
        self.broker._refresh_supplemental_cache()
        calls = len(self.historian.calls)
        self.assertGreater(calls, 0)
        status, payload = self.client.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual((payload["metric"], payload["span"], payload["bucket_seconds"]),
                         ("battery.voltage", "24h", 360))
        self.assertEqual(len(payload["points"]), 1)
        self.assertEqual(len(self.historian.calls), calls, "a GET reads memory")

    def test_the_series_refresh_runs_after_the_recorders_insert_on_the_same_thread(self):
        """Why the rollup needs no grace: the minute a snapshot belongs to is sealed only by a query
        that starts after its insert, because the recorder thread inserts, then refreshes."""

        events = []
        self.broker.insights.ingest_snapshot.side_effect = lambda *a, **k: events.append(("insert", threading.get_ident()))
        original = self.historian.metric_series

        def recording(name, **kwargs):
            events.append(("query", threading.get_ident()))
            self.broker._history_stop.set()  # one loop iteration is enough
            return original(name, **kwargs)

        self.historian.metric_series = recording
        self.broker._history_loop()
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[0], "insert")
        self.assertIn("query", kinds)
        self.assertLess(kinds.index("insert"), kinds.index("query"))
        self.assertEqual({thread for _, thread in events}, {threading.get_ident()})

    def test_trend_metrics_are_served_and_refusals_are_400(self):
        self.broker._refresh_supplemental_cache()
        for name in DEFAULT_HISTORY_METRICS:
            status, payload = self.client.request("GET", f"/v1/history/series?metric={name}&span=1h")
            self.assertEqual((status, payload["available"]), (200, True), name)
        for query in ("", f"?metric={METRIC}&span=15m", f"?metric={METRIC}&span=1h&x=1", "?metric=nope&span=1h"):
            with self.subTest(query=query):
                status, payload = self.client.request("GET", "/v1/history/series" + query)
                self.assertEqual((status, payload["reason"], payload["available"]), (400, "invalid_request", False))

    def test_history_disabled(self):
        self.broker.insights = None
        status, payload = self.client.request("GET", f"/v1/history/series?metric={METRIC}&span=1h")
        self.assertEqual((status, payload["reason"]), (200, "history_disabled"))


if __name__ == "__main__":
    unittest.main()
