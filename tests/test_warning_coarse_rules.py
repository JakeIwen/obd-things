"""The coarse approximation takes its limits from the evaluator's rule table."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools import warning_replay as wr


class CoarseRuleSourceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "coarse.sqlite3"
        self.start = 100 * wr.DAY_US
        self.now = self.start + wr.HOUR_US
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE trips(id,started_us,last_active_us,ended_us);
                CREATE TABLE metric_samples(trip_id);
                CREATE TABLE metric_rollups(metric,bucket_seconds,bucket_us,trip_key,regime,
                                            minimum,maximum,median,sample_count);
            """)
            conn.execute("INSERT INTO trips VALUES(1,?,?,?)", (self.start, self.now, self.now))

            def row(metric, value, *, index=0, trip=1, regime="engine_running:stationary:rpm_low:warm"):
                conn.execute("INSERT INTO metric_rollups VALUES(?,60,?,?,?,?,?,?,1)",
                             (metric, self.start + index * wr.BUCKET_US, trip, regime, value, value, value))

            row("engine.coolant_temperature", 245.0)
            row("transmission.oil_temperature", 235.0)
            row("engine.oil_pressure", 10.0)
            row("battery.voltage", 11.0)
            row("generator.field_duty", 95.0)
            row("battery.voltage", 11.0, trip=0, regime="engine_off:stationary:rpm_off:cold")
            row("tire.pressure.fl", 44.0)
            row("tire.pressure.fr", 44.0)
            for index in range(35):
                row("tire.pressure.rl", 59.0 if index < 30 else 64.0, index=index)
                row("tire.pressure.rr", 59.0, index=index)
        self.rules = {rule.key: rule for rule in wr.DEFAULT_EVALUATION_RULES}

    def evaluate(self, key=None, **changes):
        rules = tuple(replace(rule, **changes) if rule.key == key else rule
                      for rule in wr.DEFAULT_EVALUATION_RULES)
        with patch.object(wr, "DEFAULT_EVALUATION_RULES", rules):
            result = wr.coarse(self.path, now_us=self.now, write=False)
        return {entry["trip"]: entry["hits"] for entry in result["trips"]}

    def test_warning_limits_and_companion_are_not_copied_constants(self):
        cases = (
            ("engine_coolant_temperature_hot", dict(warning_threshold=250.0, critical_threshold=260.0), 1),
            ("transmission_oil_temperature_hot", dict(warning_threshold=250.0), 1),
            ("engine_oil_pressure_absolute_critical", dict(minimum_pressure_psi=5.0), 1),
            ("engine_oil_pressure_below_band", dict(bands=((550.0, 850.0, 5.0), (1000.0, 3000.0, 5.0), (3500.0, None, 5.0))), 1),
            ("battery_voltage_charging_failure", dict(warning_threshold=10.0), 1),
            ("battery_voltage_charging_failure", dict(companion=("generator.field_duty", 99.0, 10.0)), 1),
            ("battery_voltage_low_parked", dict(warning_threshold=10.0, critical_threshold=9.0), "parked"),
            ("tire_pressure_low_absolute", dict(wheel_thresholds=tuple((w, 30.0, 20.0) for w in ("fl", "fr", "rl", "rr"))), 1),
            ("tire_pressure_pair_asymmetry", dict(warning_threshold=6.0), 1),
        )
        baseline = self.evaluate()
        for key, changes, trip in cases:
            hit_key = key + ":rear" if key == "tire_pressure_pair_asymmetry" else key
            with self.subTest(rule=key, fields=tuple(changes)):
                self.assertGreater(baseline[trip][hit_key]["hits"], 0)
                self.assertNotIn(hit_key, self.evaluate(key, **changes)[trip])

    def test_critical_limits_are_not_copied_constants(self):
        cases = (
            ("engine_coolant_temperature_hot", dict(critical_threshold=250.0), 1),
            ("battery_voltage_low_parked", dict(critical_threshold=10.0), "parked"),
            ("tire_pressure_low_absolute", dict(wheel_thresholds=tuple((w, warn, 30.0) for w, warn, _ in self.rules["tire_pressure_low_absolute"].wheel_thresholds)), 1),
        )
        baseline = self.evaluate()
        for key, changes, trip in cases:
            with self.subTest(rule=key):
                self.assertGreater(baseline[trip][key]["critical_buckets"], 0)
                changed = self.evaluate(key, **changes)[trip][key]
                self.assertEqual(changed["hits"], baseline[trip][key]["hits"])
                self.assertEqual(changed["critical_buckets"], 0)
