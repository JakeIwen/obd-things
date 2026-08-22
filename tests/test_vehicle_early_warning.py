import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.vehicle_data.early_warning import (
    DEFAULT_WARNING_RULES,
    CorroborationRule,
    EarlyWarningEvaluator,
    WarningRule,
    default_rule_catalog,
)
from projects.vehicle_data.historian import HistorianConfig, TelemetryHistorian
from tests.test_vehicle_historian import available, definition, snapshot, unavailable


UTC = timezone.utc


class EarlyWarningTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "history.sqlite3"
        self.rpm = definition("engine.rpm", "rpm")
        self.speed = definition("vehicle.speed", "mph")
        self.coolant = definition("engine.coolant_temperature", "°F")
        self.oil = definition("engine.oil_pressure", "psi")
        self.battery = definition("battery.voltage", "V", stale=30)
        self.generator = definition("generator.field_duty", "%")
        self.definitions = [
            self.rpm,
            self.speed,
            self.coolant,
            self.oil,
            self.battery,
            self.generator,
        ]
        self.start = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)

    def active_values(self, at, *, oil=30.0, battery=14.0, generator=30.0):
        return {
            "engine.rpm": available(self.rpm, 800.0, at),
            "vehicle.speed": available(self.speed, 0.0, at),
            "engine.coolant_temperature": available(self.coolant, 190.0, at),
            "engine.oil_pressure": available(self.oil, oil, at),
            "battery.voltage": available(self.battery, battery, at),
            "generator.field_duty": available(self.generator, generator, at),
        }

    def inactive_values(self):
        return {definition["name"]: unavailable(definition) for definition in self.definitions}

    def train_three_trips(self, historian):
        for trip_index in range(3):
            trip_start = self.start + timedelta(seconds=trip_index * 60)
            for point_index, value in enumerate((29.0, 30.0, 31.0)):
                at = trip_start + timedelta(seconds=point_index * 10)
                historian.ingest_snapshot(
                    snapshot(
                        at,
                        self.definitions,
                        self.active_values(at, oil=value),
                    ),
                    captured_at=at,
                )
            idle_at = trip_start + timedelta(seconds=40)
            historian.ingest_snapshot(
                snapshot(
                    idle_at,
                    self.definitions,
                    self.inactive_values(),
                    running=False,
                ),
                captured_at=idle_at,
            )

    def test_persistent_regime_conditioned_deviation_becomes_warning(self):
        config = HistorianConfig(trip_idle_timeout_seconds=15, rollup_seconds=5)
        with TelemetryHistorian(self.path, config=config) as historian:
            self.train_three_trips(historian)
            current_start = self.start + timedelta(seconds=200)
            for index in range(3):
                at = current_start + timedelta(seconds=index * 5)
                historian.ingest_snapshot(
                    snapshot(
                        at,
                        self.definitions,
                        self.active_values(at, oil=20.0),
                    ),
                    captured_at=at,
                )
            rule = WarningRule(
                key="test_oil_low",
                title="Test oil relative low",
                metric="engine.oil_pressure",
                direction="low",
                mad_multiplier=3.0,
                minimum_effect=1.0,
                minimum_baseline_buckets=6,
                minimum_baseline_trips=3,
                persistence_observations=3,
                persistence_window_seconds=20,
            )
            report = EarlyWarningEvaluator(historian, (rule,)).evaluate(
                at=current_start + timedelta(seconds=10)
            )
            assessment = report["assessments"][0]
            self.assertEqual(assessment["state"], "warning")
            self.assertEqual(assessment["persistence"]["observed"], 3)
            self.assertEqual(assessment["baseline"]["trip_count"], 3)
            self.assertIn("rpm_idle", assessment["regime"])
            self.assertIn("warm", assessment["regime"])
            self.assertFalse(report["method"]["opaque_health_score"])
            self.assertEqual(report["active"][0]["rule"], "test_oil_low")

    def test_nonpersistent_deviation_is_watch_and_provenance_change_retrains(self):
        config = HistorianConfig(trip_idle_timeout_seconds=15, rollup_seconds=5)
        with TelemetryHistorian(self.path, config=config) as historian:
            self.train_three_trips(historian)
            at = self.start + timedelta(seconds=200)
            historian.ingest_snapshot(
                snapshot(at, self.definitions, self.active_values(at, oil=20.0)),
                captured_at=at,
            )
            watch_rule = WarningRule(
                key="watch",
                title="Watch",
                metric="engine.oil_pressure",
                direction="low",
                minimum_effect=1.0,
                minimum_baseline_buckets=6,
                minimum_baseline_trips=3,
                persistence_observations=3,
            )
            assessment = EarlyWarningEvaluator(historian, (watch_rule,)).evaluate(
                at=at
            )["assessments"][0]
            self.assertEqual(assessment["state"], "watch")
            self.assertEqual(assessment["persistence"]["observed"], 1)

        changed_path = Path(self.tempdir.name) / "changed.sqlite3"
        changed_oil = definition(
            "engine.oil_pressure", "psi", provenance="replacement decode evidence"
        )
        changed_definitions = [
            self.rpm,
            self.speed,
            self.coolant,
            changed_oil,
            self.battery,
            self.generator,
        ]
        with TelemetryHistorian(changed_path, config=config) as historian:
            self.train_three_trips(historian)
            at = self.start + timedelta(seconds=200)
            values = self.active_values(at, oil=20.0)
            values["engine.oil_pressure"] = available(changed_oil, 20.0, at)
            historian.ingest_snapshot(
                snapshot(at, changed_definitions, values), captured_at=at
            )
            assessment = EarlyWarningEvaluator(historian, (watch_rule,)).evaluate(
                at=at
            )["assessments"][0]
            self.assertEqual(assessment["state"], "insufficient_history")
            self.assertIn("no completed comparable", assessment["reason"])

    def test_cached_repeat_is_one_independent_persistence_observation(self):
        config = HistorianConfig(trip_idle_timeout_seconds=15, rollup_seconds=5)
        with TelemetryHistorian(self.path, config=config) as historian:
            self.train_three_trips(historian)
            current_start = self.start + timedelta(seconds=200)
            for index in range(3):
                at = current_start + timedelta(seconds=index)
                values = self.active_values(at, oil=20.0)
                values["engine.oil_pressure"] = available(
                    self.oil,
                    20.0,
                    current_start,
                    age_ms=index * 1_000,
                )
                historian.ingest_snapshot(
                    snapshot(at, self.definitions, values),
                    captured_at=at,
                )
            rule = WarningRule(
                key="cached_repeat",
                title="Cached repeat",
                metric="engine.oil_pressure",
                direction="low",
                minimum_effect=1.0,
                minimum_baseline_buckets=6,
                minimum_baseline_trips=3,
                persistence_observations=3,
                persistence_window_seconds=20,
            )
            assessment = EarlyWarningEvaluator(historian, (rule,)).evaluate(
                at=current_start + timedelta(seconds=2)
            )["assessments"][0]
            self.assertEqual(assessment["state"], "watch")
            self.assertEqual(assessment["persistence"]["observed"], 1)

    def test_required_companion_metric_can_corroborate(self):
        config = HistorianConfig(trip_idle_timeout_seconds=15, rollup_seconds=5)
        with TelemetryHistorian(self.path, config=config) as historian:
            self.train_three_trips(historian)
            current_start = self.start + timedelta(seconds=200)
            for index in range(3):
                at = current_start + timedelta(seconds=index * 5)
                historian.ingest_snapshot(
                    snapshot(
                        at,
                        self.definitions,
                        self.active_values(at, battery=13.0, generator=60.0),
                    ),
                    captured_at=at,
                )
            rule = WarningRule(
                key="charging",
                title="Charging relative",
                metric="battery.voltage",
                direction="low",
                minimum_effect=0.1,
                minimum_baseline_buckets=6,
                minimum_baseline_trips=3,
                persistence_observations=3,
                persistence_window_seconds=20,
                corroborators=(
                    CorroborationRule(
                        metric="generator.field_duty",
                        direction="high",
                        minimum_effect=1.0,
                    ),
                ),
                required_corroborators=1,
            )
            assessment = EarlyWarningEvaluator(historian, (rule,)).evaluate(
                at=current_start + timedelta(seconds=10)
            )["assessments"][0]
            self.assertEqual(assessment["state"], "warning")
            self.assertEqual(assessment["corroboration"]["observed"], 1)
            self.assertEqual(
                assessment["corroborators"][0]["state"], "corroborating"
            )

    def test_default_rules_are_allowlisted_and_start_insufficient_or_unavailable(self):
        expected = {
            "engine.oil_pressure",
            "engine.coolant_temperature",
            "transmission.oil_temperature",
            "battery.voltage",
            "tire.pressure.fl",
            "tire.pressure.fr",
            "tire.pressure.rl",
            "tire.pressure.rr",
        }
        self.assertEqual({rule.metric for rule in DEFAULT_WARNING_RULES}, expected)
        self.assertEqual(len(default_rule_catalog()), len(DEFAULT_WARNING_RULES))
        by_metric = {rule.metric: rule for rule in DEFAULT_WARNING_RULES}
        self.assertEqual(
            by_metric["engine.oil_pressure"].regime_dimensions,
            ("engine", "motion", "rpm", "thermal"),
        )
        self.assertNotIn(
            "thermal",
            by_metric["engine.coolant_temperature"].regime_dimensions,
        )
        self.assertEqual(
            by_metric["tire.pressure.fl"].regime_dimensions,
            ("motion",),
        )
        self.assertNotIn("generator.field_duty", by_metric)
        self.assertEqual(
            by_metric["battery.voltage"].required_corroborators,
            0,
        )
        config = HistorianConfig(rollup_seconds=5)
        with TelemetryHistorian(self.path, config=config) as historian:
            at = self.start
            historian.ingest_snapshot(
                snapshot(at, self.definitions, self.active_values(at)),
                captured_at=at,
            )
            report = EarlyWarningEvaluator(historian).evaluate(at=at)
            oil = next(
                item
                for item in report["assessments"]
                if item["metric"] == "engine.oil_pressure"
            )
            self.assertEqual(oil["state"], "insufficient_history")
            tire = next(
                item
                for item in report["assessments"]
                if item["metric"] == "tire.pressure.fl"
            )
            self.assertEqual(tire["state"], "unavailable")

    def test_evaluate_refreshes_rollups_once_and_standalone_rule_still_refreshes(self):
        config = HistorianConfig(rollup_seconds=5)
        with TelemetryHistorian(self.path, config=config) as historian:
            at = self.start
            historian.ingest_snapshot(
                snapshot(at, self.definitions, self.active_values(at)),
                captured_at=at,
            )
            evaluator = EarlyWarningEvaluator(historian)
            with mock.patch.object(
                historian,
                "refresh_rollups",
                wraps=historian.refresh_rollups,
            ) as refresh:
                evaluator.evaluate(at=at)
                self.assertEqual(refresh.call_count, 1)
                refresh.reset_mock()
                evaluator.evaluate_rule(DEFAULT_WARNING_RULES[0], at=at)
                self.assertEqual(refresh.call_count, 1)


if __name__ == "__main__":
    unittest.main()
