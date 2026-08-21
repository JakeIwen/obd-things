from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.modules import MODULES
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data import insights as insights_module
from projects.vehicle_data.insights import TelemetryInsights


DTC_GROUPS = (
    "current",
    "pending",
    "confirmed_history",
    "incomplete_only",
    "other",
)


def module_row(module_key, **updates):
    module = MODULES[module_key]
    row = {
        "module_key": module.key,
        "module_name": module.name,
        "logical_bus": module.bus,
        "resolved_channel": None,
        "bitrate": module.bitrate,
        "availability": "never_scanned",
        "result_state": "never_scanned",
        "unavailable_reason": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "successful_scans": 0,
        "unavailable_scans": 0,
        "consecutive_unavailable": 0,
        "last_success_dtc_count": None,
        "status_availability_mask": None,
        "absence_authoritative": False,
    }
    row.update(updates)
    return row


def compact_dtc_cache(*, rows=None, groups=None, group_counts=None):
    rows = rows or [module_row(key) for key in MODULES]
    groups = groups or {name: [] for name in DTC_GROUPS}
    returned = {name: len(groups[name]) for name in DTC_GROUPS}
    counts = group_counts or dict(returned)
    attempt_times = [row["last_attempt_at"] for row in rows if row["last_attempt_at"]]
    success_times = [row["last_success_at"] for row in rows if row["last_success_at"]]
    return {
        "schema_version": 2,
        "generated_at": "2026-08-20T12:00:00Z",
        "acquisition": "cache_only",
        "compact": True,
        "per_group_limit": 25,
        "group_counts": counts,
        "group_returned_counts": returned,
        "groups_truncated": any(returned[name] < counts[name] for name in DTC_GROUPS),
        "coverage": {
            "total_modules": len(rows),
            "available_modules": sum(row["availability"] == "available" for row in rows),
            "unavailable_modules": sum(
                row["availability"] == "unavailable" for row in rows
            ),
            "never_scanned_modules": sum(
                row["availability"] == "never_scanned" for row in rows
            ),
            "modules_with_dtcs": sum(
                row["result_state"] == "dtcs_present" for row in rows
            ),
            "modules_no_dtcs": sum(row["result_state"] == "no_dtcs" for row in rows),
            "modules_status_coverage_incomplete": sum(
                row["result_state"] == "status_coverage_incomplete" for row in rows
            ),
            "modules_with_last_known_dtcs": len(
                {
                    record["module_key"]
                    for name in DTC_GROUPS
                    for record in groups[name]
                }
            ),
            "last_attempt_at": max(attempt_times) if attempt_times else None,
            "last_success_at": max(success_times) if success_times else None,
        },
        "groups": groups,
        "modules": rows,
    }


class FakeHistorian:
    def __init__(self):
        self.closed = False
        self.requested_metrics = None
        self.ingest_calls = []
        self.maintenance_calls = []
        self.maintenance_error = None

    def close(self):
        self.closed = True

    def ingest_snapshot(self, snapshot, *, captured_at, ingest_key):
        result = mock.Mock(duplicate=False, captured_at=captured_at.isoformat())
        self.ingest_calls.append((snapshot, captured_at, ingest_key))
        return result

    def maybe_run_maintenance(self, *, now):
        self.maintenance_calls.append(now)
        if self.maintenance_error is not None:
            raise self.maintenance_error
        return mock.Mock(
            as_dict=lambda: {
                "status": "not_due",
                "attempted_at": now.isoformat(),
            }
        )

    def dashboard_summary(self, *, metrics):
        self.requested_metrics = tuple(metrics)
        return {
            "schema_version": 1,
            "coverage": {"status": "current", "last_snapshot_at": "2026-08-20T00:00:00+00:00"},
            "current_trip": {"id": 4, "state": "open"},
            "recent_trips": [],
            "trip_comparison": {
                "metrics": {
                    metric: {
                        "current_trip": {"mean": 12.5, "units": ["V"]},
                        "prior_trips": {"trip_count": 3},
                        "current_minus_prior_median": -0.1,
                    }
                    for metric in metrics
                }
            },
            "windows": {
                "7d": {"metrics": {metric: {"mean": 12.6, "units": ["V"]} for metric in metrics}},
                "30d": {"metrics": {metric: {"mean": 12.7, "units": ["V"]} for metric in metrics}},
            },
        }

    def metric_series(self, metric, **kwargs):
        self.series_call = (metric, kwargs)
        return {
            "units": ["V"],
            "points": [{"at": "2026-08-20T00:00:00+00:00", "value": 12.5}],
            "start_at": "2026-08-19T00:00:00+00:00",
            "end_at": "2026-08-20T00:00:00+00:00",
            "bucket_seconds": 900,
            "point_limit": 96,
            "mixed_provenance": False,
        }


class VehicleInsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vehicle-insights-", dir="/tmp")
        self.cache = Path(self.tmp.name) / "dtcs.json"
        self.historian = FakeHistorian()
        self.evaluator = mock.Mock()
        self.evaluator.evaluate.return_value = {
            "schema_version": 1,
            "active": [],
            "assessments": [],
        }
        self.insights = TelemetryInsights(
            self.historian,
            warning_evaluator=self.evaluator,
            dtc_cache_path=self.cache,
            history_metrics=("battery.voltage",),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_history_response_is_bounded_and_uses_dedicated_summary(self):
        response = self.insights.history_response()

        self.assertTrue(response["available"])
        self.assertEqual(self.historian.requested_metrics, ("battery.voltage",))
        trend = response["metric_trends"]["battery.voltage"]
        self.assertEqual(trend["current_trip"]["mean"], 12.5)
        self.assertEqual(trend["days_7"]["mean"], 12.6)
        self.assertEqual(len(trend["sparkline"]), 1)
        self.assertEqual(self.historian.series_call[1]["max_points"], 96)

    def test_ingest_checks_maintenance_at_most_hourly(self):
        start = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        with mock.patch.object(
            insights_module.time,
            "monotonic",
            side_effect=(100.0, 200.0, 3701.0),
        ):
            first = self.insights.ingest_snapshot(
                {}, captured_at=start, ingest_key="first"
            )
            second = self.insights.ingest_snapshot(
                {}, captured_at=start + timedelta(minutes=5), ingest_key="second"
            )
            third = self.insights.ingest_snapshot(
                {}, captured_at=start + timedelta(hours=1), ingest_key="third"
            )

        self.assertFalse(first.duplicate)
        self.assertFalse(second.duplicate)
        self.assertFalse(third.duplicate)
        self.assertEqual(len(self.historian.ingest_calls), 3)
        self.assertEqual(
            self.historian.maintenance_calls,
            [start, start + timedelta(hours=1)],
        )
        hook = self.insights.history_response()["maintenance_hook"]
        self.assertEqual(hook["last_result"]["status"], "not_due")
        self.assertIsNone(hook["last_error"])

    def test_maintenance_failure_does_not_fail_committed_ingest(self):
        captured_at = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        self.historian.maintenance_error = RuntimeError("maintenance failed")

        result = self.insights.ingest_snapshot(
            {}, captured_at=captured_at, ingest_key="committed"
        )

        self.assertFalse(result.duplicate)
        self.assertEqual(len(self.historian.ingest_calls), 1)
        hook = self.insights.history_response()["maintenance_hook"]
        self.assertIsNone(hook["last_result"])
        self.assertEqual(hook["last_error"], "RuntimeError: maintenance failed")

    def test_dtc_response_reads_saved_cache_only_and_preserves_unavailable(self):
        rows = [module_row(key) for key in MODULES]
        pcm_index = list(MODULES).index("pcm")
        rows[pcm_index] = module_row(
            "pcm",
            resolved_channel="can0",
            availability="unavailable",
            result_state="unavailable",
            unavailable_reason="timeout",
            last_attempt_at="2026-08-20T12:00:00Z",
            unavailable_scans=1,
            consecutive_unavailable=1,
        )
        self.cache.write_text(json.dumps(compact_dtc_cache(rows=rows)))

        response = self.insights.dtc_response()

        self.assertTrue(response["available"])
        self.assertEqual(response["acquisition"], "cache_only")
        self.assertEqual(response["coverage"]["unavailable_modules"], 1)
        pcm = next(row for row in response["modules"] if row["module_key"] == "pcm")
        self.assertEqual(pcm["availability"], "unavailable")
        self.assertFalse(pcm["absence_authoritative"])

    def test_dtc_response_rejects_partial_or_noncompact_schema(self):
        invalid_payloads = (
            {},
            {"schema_version": 2},
            {**compact_dtc_cache(), "compact": False},
            {
                **compact_dtc_cache(),
                "modules": compact_dtc_cache()["modules"][:-1],
            },
        )
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(index=index):
                self.cache.write_text(json.dumps(payload))
                response = self.insights.dtc_response()
                self.assertFalse(response["available"])
                self.assertEqual(response["reason"], "dtc_cache_unavailable")
                self.assertIn("schema", response["detail"])

    def test_dtc_response_requires_authoritative_module_fields(self):
        payload = compact_dtc_cache()
        del payload["modules"][0]["absence_authoritative"]
        self.cache.write_text(json.dumps(payload))

        response = self.insights.dtc_response()

        self.assertFalse(response["available"])
        self.assertIn("absence_authoritative", response["detail"])

    def test_dtc_response_validates_record_observation_state(self):
        observed_at = "2026-08-20T12:00:00Z"
        rows = [module_row(key) for key in MODULES]
        rf_index = list(MODULES).index("rf_hub")
        rows[rf_index] = module_row(
            "rf_hub",
            resolved_channel="can0",
            availability="available",
            result_state="dtcs_present",
            last_attempt_at=observed_at,
            last_success_at=observed_at,
            successful_scans=1,
            last_success_dtc_count=1,
            status_availability_mask="CF",
            absence_authoritative=True,
        )
        rf_hub = rows[rf_index]
        record = {
            "module_key": "rf_hub",
            "module_name": rf_hub["module_name"],
            "logical_bus": rf_hub["logical_bus"],
            "resolved_channel": "can0",
            "module_availability": "available",
            "module_result_state": "dtcs_present",
            "latest_attempt_successful": True,
            "last_attempt_at": observed_at,
            "last_success_at": observed_at,
            "raw_dtc": "D42787",
            "fca_display": "U1427-87",
            "status": "01",
            "status_flags": ["test_failed"],
            "display_group": "current",
            "current": True,
            "pending": False,
            "confirmed": False,
            "warning_indicator_requested": False,
            "incomplete_only": False,
            "present": True,
            "observation_state": "observed_in_latest_success",
            "status_availability_mask": "CF",
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
            "observation_count": 1,
            "episode_count": 1,
            "recurrence_count": 0,
            "status_changed_at": observed_at,
            "resolved_at": None,
        }
        groups = {name: [] for name in DTC_GROUPS}
        groups["current"] = [record]
        payload = compact_dtc_cache(rows=rows, groups=groups)
        self.cache.write_text(json.dumps(payload))
        self.assertTrue(self.insights.dtc_response()["available"])

        payload["groups"]["current"][0]["observation_state"] = "unknown"
        self.cache.write_text(json.dumps(payload))
        response = self.insights.dtc_response()
        self.assertFalse(response["available"])
        self.assertIn("observation state", response["detail"])

    def test_dtc_response_enforces_raw_and_encoded_bounds(self):
        payload = compact_dtc_cache()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.cache.write_bytes(raw)

        with (
            mock.patch.object(insights_module, "MAX_DTC_CACHE_BYTES", len(raw) - 1),
            mock.patch.object(insights_module, "MAX_RESPONSE_BYTES", len(raw) + 4096),
        ):
            response = self.insights.dtc_response()
        self.assertFalse(response["available"])
        self.assertIn("one-megabyte limit", response["detail"])

        with (
            mock.patch.object(insights_module, "MAX_DTC_CACHE_BYTES", len(raw)),
            mock.patch.object(insights_module, "MAX_RESPONSE_BYTES", len(raw) + 1),
        ):
            response = self.insights.dtc_response()
        self.assertFalse(response["available"])
        self.assertIn("transport limit", response["detail"])

    def test_missing_dtc_cache_is_explicitly_unavailable(self):
        response = self.insights.dtc_response()

        self.assertFalse(response["available"])
        self.assertEqual(response["reason"], "dtc_cache_unavailable")

    def test_broker_keeps_large_supplemental_products_out_of_snapshot(self):
        broker = TelemetryBroker(
            acquirer=mock.Mock(channel="c-can-unresolved"),
            insights=self.insights,
        )

        snapshot = broker.snapshot_response()

        self.assertNotIn("history", snapshot)
        self.assertNotIn("health", snapshot)
        self.assertNotIn("dtcs", snapshot)
        self.assertTrue(broker.history_response()["available"])
        self.assertTrue(broker.health_response()["available"])


if __name__ == "__main__":
    unittest.main()
