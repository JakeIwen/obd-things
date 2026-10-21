import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.vehicle_data import fuel_economy
from projects.vehicle_data.historian import HistorianConfig, TelemetryHistorian
from projects.vehicle_data.metrics import METRICS
from tests.test_vehicle_historian import available, definition, snapshot, unavailable

UTC = timezone.utc


class FuelEconomyHistoryTests(unittest.TestCase):
    """Economy from a real historian: running samples only, rollups plus raw tail."""

    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.path = Path(tempdir.name) / "history.sqlite3"
        self.rpm = definition("engine.rpm", "rpm")
        self.speed = definition("vehicle.speed", "mph")
        self.fuel = definition("engine.fuel_rate", "gal/h")
        self.definitions = [self.rpm, self.speed, self.fuel]
        self.start = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        self.config = HistorianConfig(trip_idle_timeout_seconds=300, rollup_seconds=60)

    def ingest(self, historian, at, rpm, mph, gph):
        values = {
            "engine.rpm": available(self.rpm, rpm, at),
            "vehicle.speed": available(self.speed, mph, at),
            "engine.fuel_rate": (
                unavailable(self.fuel) if gph is None else available(self.fuel, gph, at)
            ),
        }
        historian.ingest_snapshot(snapshot(at, self.definitions, values), captured_at=at)

    def drive(self, historian):
        """Five minutes running (idle, then 40 mph), then engine off with speed still sent."""
        speeds, fuels = [], []
        for index in range(60):
            at = self.start + timedelta(seconds=5 * index)
            mph, gph = (0.0, 0.4) if index < 12 else (40.0 + index % 5, 2.0 + (index % 3) / 4)
            self.ingest(historian, at, 800.0 + 20 * index, mph, gph)
            speeds.append(mph)
            fuels.append(gph)
        for index in range(60, 70):
            self.ingest(historian, self.start + timedelta(seconds=5 * index), 0.0, 0.0, None)
        return sum(speeds) / sum(fuels)

    def test_trip_economy_uses_running_rollups_and_raw_tail(self):
        with TelemetryHistorian(self.path, config=self.config) as historian:
            expected = self.drive(historian)
            # Roll up the first three minutes; the rest stays in the raw tail.
            historian.refresh_rollups(through=self.start + timedelta(seconds=180))
            trip = historian.list_trips()[0]
            totals = historian.running_totals_for_trip(trip["id"], fuel_economy.METRIC_UNITS)
            self.assertEqual(totals["vehicle.speed"][1], 60)
            self.assertEqual(totals["engine.fuel_rate"][1], 60)
            result = fuel_economy.economy(totals)
            self.assertTrue(result["available"])
            self.assertAlmostEqual(result["mpg"], expected)

    def test_window_economy_counts_complete_running_buckets(self):
        with TelemetryHistorian(self.path, config=self.config) as historian:
            self.drive(historian)
            end = self.start + timedelta(seconds=360)
            historian.refresh_rollups(through=end)
            totals = historian.running_totals_for_window(
                fuel_economy.METRIC_UNITS, start=self.start, end=end
            )
            self.assertEqual(totals["vehicle.speed"][1], 60)
            self.assertGreater(fuel_economy.economy(totals)["mpg"], 15)

    def test_attach_adds_trip_and_window_figures_to_history(self):
        with TelemetryHistorian(self.path, config=self.config) as historian:
            expected = self.drive(historian)
            now = self.start + timedelta(seconds=360)
            history = historian.dashboard_summary(now=now)
            history["available"] = True
            fuel_economy.attach_fuel_economy(history, historian, now=now)
            self.assertAlmostEqual(history["current_trip"]["fuel_economy"]["mpg"], expected)
            self.assertTrue(history["fuel_economy"]["available"])
            self.assertEqual(history["fuel_economy"]["quality"], "candidate")
            self.assertIn("mpg", history["fuel_economy"]["days_7"])

    def test_trip_from_before_fuel_was_recorded_has_no_economy(self):
        with TelemetryHistorian(self.path, config=self.config) as historian:
            for index in range(40):
                at = self.start + timedelta(seconds=5 * index)
                gph = None if index < 20 else 2.0
                self.ingest(historian, at, 1500.0, 40.0, gph)
            trip = historian.list_trips()[0]
            result = fuel_economy.economy(
                historian.running_totals_for_trip(trip["id"], fuel_economy.METRIC_UNITS)
            )
            self.assertEqual(result, {"available": False, "samples": 20,
                                      "reason": "partial_fuel_coverage"})

    def test_windows_start_when_fuel_was_first_recorded(self):
        with TelemetryHistorian(self.path, config=self.config) as historian:
            # An earlier drive with speed but no fuel rate, as before the metric existed.
            for index in range(60):
                at = self.start - timedelta(hours=2) + timedelta(seconds=5 * index)
                self.ingest(historian, at, 1500.0, 40.0, None)
            expected = self.drive(historian)
            now = self.start + timedelta(seconds=360)
            historian.refresh_rollups(through=now)
            history = {"available": True, "recent_trips": []}
            fuel_economy.attach_fuel_economy(history, historian, now=now)
            for key in ("days_7", "days_30"):
                window = history["fuel_economy"][key]
                self.assertAlmostEqual(window["mpg"], expected)
                self.assertEqual(datetime.fromisoformat(window["since"]), self.start)

    def test_query_failure_leaves_history_intact(self):
        class Broken:
            def running_totals_for_trip(self, *_args):
                raise RuntimeError("database is locked")

        history = {"available": True, "current_trip": {"id": 7}, "recent_trips": []}
        fuel_economy.attach_fuel_economy(history, Broken())
        self.assertNotIn("fuel_economy", history["current_trip"])
        self.assertFalse(history["fuel_economy"]["available"])
        self.assertIn("database is locked", history["fuel_economy"]["detail"])


class EconomyRuleTests(unittest.TestCase):
    def totals(self, speed, fuel):
        return {"vehicle.speed": speed, "engine.fuel_rate": fuel}

    def test_ratio_of_means(self):
        result = fuel_economy.economy(self.totals((450.0, 15), (30.0, 15)))
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["mpg"], 15.0)
        self.assertAlmostEqual(result["mean_speed_mph"], 30.0)
        self.assertAlmostEqual(result["mean_fuel_rate_gph"], 2.0)

    def test_needs_a_minute_of_running_samples(self):
        result = fuel_economy.economy(self.totals((100.0, 11), (5.0, 11)))
        self.assertEqual(result["reason"], "not_enough_running_samples")

    def test_zero_fuel_is_not_infinite_economy(self):
        result = fuel_economy.economy(self.totals((300.0, 20), (0.0, 20)))
        self.assertEqual(result["reason"], "no_fuel_recorded")

    def test_idle_only_trip_is_zero_mpg(self):
        self.assertEqual(fuel_economy.economy(self.totals((0.0, 20), (8.0, 20)))["mpg"], 0.0)


class FuelRateMetricTests(unittest.TestCase):
    def test_registry_entry_matches_the_decode(self):
        definition_ = METRICS["engine.fuel_rate"]
        self.assertIs(definition_, fuel_economy.DEFINITION)
        self.assertEqual(definition_.unit, "gal/h")
        (source,) = definition_.sources
        self.assertEqual(source.name, "ccan.broadcast.0x2ed")
        self.assertEqual(source.quality, "candidate")
        self.assertTrue(source.publisher_allowed)
        self.assertGreater(definition_.maximum, 65535 * 1.131 / 512 / 3.785411784)


if __name__ == "__main__":
    unittest.main()
