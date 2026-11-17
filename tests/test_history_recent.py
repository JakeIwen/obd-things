"""GET /v1/history/recent: the fixed 15-minute sparkline seed (owner request 2026-11-17)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest
import tempfile
from pathlib import Path
from unittest import mock

from projects.vehicle_data import history_recent
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.historian import TelemetryHistorian
from tests.test_vehicle_data import FakeAcquirer
from tests.test_vehicle_historian import available, definition, snapshot

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


class FakeHistorian:
    """Answers metric_series like the real one: points labelled by bucket start."""

    def __init__(self, points=None):
        self.calls = []
        self.points = points if points is not None else {}

    def metric_series(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return {"points": [dict(p, minimum=0, maximum=1, sample_count=5) for p in self.points.get(name, [])]}


class QueryTests(unittest.TestCase):
    def test_contract_is_fixed_and_bounded(self):
        self.assertEqual(history_recent.API_PATH, "/v1/history/recent")
        self.assertEqual(history_recent.WINDOW_SECONDS, 900)
        self.assertEqual(history_recent.BUCKET_SECONDS, 60)
        self.assertEqual(len(history_recent.METRICS), len(set(history_recent.METRICS)))
        self.assertIn("generator.field_duty", history_recent.METRICS)

    def test_one_bounded_query_per_listed_metric_and_only_at_value_kept(self):
        historian = FakeHistorian({"vehicle.speed": [{"at": at(120), "value": 55.5}, {"at": at(60), "value": None}]})
        product = history_recent.query(historian, NOW)
        self.assertEqual([c[0] for c in historian.calls], list(history_recent.METRICS))
        for _, kwargs in historian.calls:
            self.assertEqual(kwargs["window_seconds"], 900)
            self.assertEqual(kwargs["bucket_seconds"], 60)
            self.assertEqual(kwargs["max_points"], 15)
            self.assertEqual(kwargs["end"], NOW)
        self.assertEqual(product["metrics"]["vehicle.speed"], [{"at": at(120), "value": 55.5}])
        self.assertEqual(product["metrics"]["engine.rpm"], [])


class RealHistorianLabelTests(unittest.TestCase):
    """The reply's ``at`` must be the true wall-clock minute start, whatever second the query runs."""

    def test_labels_are_the_minute_each_value_was_recorded_in(self):
        speed = definition("vehicle.speed", "mph")
        start = datetime(2026, 11, 17, 11, 40, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            with TelemetryHistorian(Path(tmp) / "h.sqlite3") as historian:
                # One sample every 10 s; the value is the minute-of-hour, so a point's value names
                # the minute it covers.
                for index in range(0, 25 * 6):
                    when = start + timedelta(seconds=10 * index)
                    historian.ingest_snapshot(
                        snapshot(when, [speed], {"vehicle.speed": available(speed, float(when.minute), when)}),
                        captured_at=when,
                    )
                for now in (
                    datetime(2026, 11, 17, 11, 56, 1, tzinfo=timezone.utc),
                    datetime(2026, 11, 17, 11, 57, 30, 500000, tzinfo=timezone.utc),
                    datetime(2026, 11, 17, 11, 59, 59, tzinfo=timezone.utc),
                    datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 11, 17, 12, 1, 1, tzinfo=timezone.utc),
                    datetime(2026, 11, 17, 12, 4, 12, tzinfo=timezone.utc),
                ):
                    with self.subTest(now=now.isoformat()):
                        points = history_recent.query(historian, now)["metrics"]["vehicle.speed"]
                        self.assertTrue(points)
                        for point in points:
                            label = datetime.fromisoformat(point["at"])
                            self.assertEqual((label.second, label.microsecond), (0, 0))
                            self.assertEqual(float(label.minute), point["value"])
                            self.assertLessEqual(label + timedelta(minutes=1), now)


class FreshResponseTests(unittest.TestCase):
    def cached(self, **points):
        return {"queried_at": NOW.isoformat(), "metrics": points}

    def test_reply_shape(self):
        reply = history_recent.fresh_response(self.cached(**{"engine.rpm": [{"at": at(300), "value": 2000.0}]}), NOW)
        self.assertEqual(
            set(reply),
            {"available", "generated_at", "window_seconds", "bucket_seconds", "at_basis", "metrics"},
        )
        self.assertIs(reply["available"], True)
        self.assertEqual(reply["generated_at"], NOW.isoformat())
        self.assertEqual(reply["at_basis"], "bucket_start")
        self.assertEqual(list(reply["metrics"]), list(history_recent.METRICS))
        self.assertEqual(reply["metrics"]["engine.rpm"], [{"at": at(300), "value": 2000.0}])

    def test_a_van_parked_for_an_hour_answers_empty_lists(self):
        old = self.cached(**{"engine.rpm": [{"at": at(3600), "value": 2000.0}, {"at": at(3540), "value": 1900.0}]})
        reply = history_recent.fresh_response(old, NOW)
        self.assertIs(reply["available"], True)
        self.assertTrue(all(points == [] for points in reply["metrics"].values()))

    def test_a_bucket_still_overlapping_the_window_is_kept_and_an_ended_one_dropped(self):
        cached = self.cached(**{"engine.rpm": [{"at": at(930), "value": 1.0}, {"at": at(961), "value": 2.0}]})
        points = history_recent.fresh_response(cached, NOW)["metrics"]["engine.rpm"]
        self.assertEqual([p["value"] for p in points], [1.0])

    def test_unknown_metrics_in_the_cache_are_not_served(self):
        reply = history_recent.fresh_response(self.cached(**{"tire.pressure.fl": [{"at": at(60), "value": 1.0}]}), NOW)
        self.assertNotIn("tire.pressure.fl", reply["metrics"])

    def test_before_the_first_refresh_is_explicit(self):
        reply = history_recent.fresh_response(None, NOW)
        self.assertIs(reply["available"], False)
        self.assertEqual(reply["reason"], "history_recent_cache_starting")


class BrokerCacheTests(unittest.TestCase):
    def broker(self, historian):
        insights = mock.Mock()
        insights.historian = historian
        for name in ("history_response", "health_response", "dtc_response"):
            getattr(insights, name).return_value = {"available": True}
        return TelemetryBroker(acquirer=FakeAcquirer(), insights=insights), insights

    def test_served_from_memory_and_refreshed_with_the_supplemental_cache(self):
        recent = datetime.now(timezone.utc) - timedelta(seconds=120)
        historian = FakeHistorian({"engine.rpm": [{"at": recent.isoformat(), "value": 1800.0}]})
        broker, _ = self.broker(historian)
        self.assertEqual(broker.cached_history_recent_response()["reason"], "history_recent_cache_starting")
        self.assertEqual(historian.calls, [])
        broker._refresh_supplemental_cache()
        queries = len(historian.calls)
        self.assertEqual(queries, len(history_recent.METRICS))
        first = broker.cached_history_recent_response()
        second = broker.cached_history_recent_response()
        self.assertEqual(len(historian.calls), queries, "requests never reach the historian")
        self.assertEqual(first["metrics"]["engine.rpm"][0]["value"], 1800.0)
        self.assertEqual(second["metrics"]["engine.rpm"], first["metrics"]["engine.rpm"])
        json.dumps(first)

    def test_a_failing_query_neither_degrades_the_cache_status_nor_serves_old_points_forever(self):
        historian = mock.Mock()
        historian.metric_series.side_effect = RuntimeError("db busy")
        broker, _ = self.broker(historian)
        broker._refresh_supplemental_cache()
        self.assertEqual(broker.status_response()["supplemental_cache"]["state"], "ready")
        self.assertIs(broker.cached_history_recent_response()["available"], False)

    def test_history_disabled_is_explicit(self):
        broker = TelemetryBroker(acquirer=FakeAcquirer())
        self.assertEqual(broker.cached_history_recent_response()["reason"], "history_disabled")


if __name__ == "__main__":
    unittest.main()
