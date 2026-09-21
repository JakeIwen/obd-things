from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data.last_readings import LastReadings
from projects.vehicle_data.metrics import METRICS
from projects.vehicle_data.models import success, failure


class LastReadingsTests(unittest.TestCase):
    def sample(self, name="battery.voltage", value=12.3):
        definition = METRICS[name]
        source = definition.sources[0]
        return {"value": value, "unit": definition.unit, "source": source.name,
                "quality": source.quality,
                "observed_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}

    def test_disk_round_trip_keeps_original_timestamp_and_no_live_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last-readings.json"
            store = LastReadings(METRICS, path)
            sample = self.sample()
            self.assertTrue(store.remember("battery.voltage", sample))
            store.flush()
            restored = LastReadings(METRICS, path).get("battery.voltage")
            self.assertEqual(restored["observed_at"], sample["observed_at"])
            self.assertEqual(restored["value"], 12.3)
            self.assertNotIn("available", restored)
            self.assertNotIn("age_ms", restored)
            restored["value"] = 0
            self.assertEqual(store.get("battery.voltage")["value"], 12.3)

    def test_invalid_sources_ranges_dates_and_older_readings_do_not_replace_good(self):
        store = LastReadings(METRICS)
        sample = self.sample()
        self.assertTrue(store.remember("battery.voltage", sample))
        for change in ({"source": "made_up"}, {"quality": "candidate"}, {"unit": "psi"},
                       {"value": float("nan")}, {"value": True}, {"value": 900},
                       {"observed_at": "broken"}, {"observed_at": "2020-01-01"},
                       {"observed_at": "2100-01-01T00:00:00Z"}):
            with self.subTest(change=change):
                self.assertFalse(store.remember("battery.voltage", {**sample, **change}))
        self.assertFalse(store.remember("battery.voltage", {
            **sample, "value": 12.7, "observed_at": "2020-01-01T00:00:00Z"}))
        self.assertEqual(store.get("battery.voltage")["value"], 12.3)
        self.assertFalse(store.remember("engine.oil_life_remaining", self.sample()))

    def test_failure_does_not_replace_success(self):
        store = LastReadings(METRICS)
        sample = self.sample("engine.vvt_oil_temperature", 180)
        source = METRICS["engine.vvt_oil_temperature"].sources[0]
        store.observe(success(metric="engine.vvt_oil_temperature", unit=sample["unit"],
            value=180, source=source.name, bus=source.bus, quality=source.quality,
            acquisition=source.acquisition_class, observed_monotonic=1,
            observed_at=datetime.fromisoformat(sample["observed_at"])))
        store.observe(failure(metric="engine.vvt_oil_temperature", unit=sample["unit"],
            reason="engine_not_running", detail="stopped"))
        self.assertEqual(store.get("engine.vvt_oil_temperature")["value"], 180)

    def test_indexed_startup_recovery_and_failures_are_nonfatal(self):
        store = LastReadings(METRICS)
        historian = mock.Mock()
        historian.latest_sample.side_effect = lambda name, **kwargs: self.sample() if name == "battery.voltage" else None
        store.restore_from_historian(historian)
        self.assertEqual(historian.latest_sample.call_count, len(METRICS))
        self.assertEqual(store.get("battery.voltage")["value"], 12.3)
        historian.latest_sample.assert_any_call("battery.voltage", fresh_only=True)
        historian.latest_sample.side_effect = OSError("unavailable")
        store.restore_from_historian(historian)
        self.assertIn("recovery failed", store.storage_error)

    def test_flush_failure_preserves_memory_and_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LastReadings(METRICS, Path(tmp) / "last.json")
            store.remember("battery.voltage", self.sample())
            with mock.patch("projects.vehicle_data.last_readings._atomic_json", side_effect=OSError("full")):
                store.flush()
            self.assertTrue(store.dirty)
            self.assertIn("full", store.storage_error)
            self.assertEqual(store.get("battery.voltage")["value"], 12.3)
            store.flush()
            self.assertFalse(store.dirty)
            self.assertIsNone(store.storage_error)
