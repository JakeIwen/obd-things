import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from projects.vehicle_data.custom_warnings import validate_rule, load_rules, evaluate_rule
from projects.vehicle_data.early_warning import EarlyWarningEvaluator


RULE = {"title": "Low supply voltage", "metric": "battery.voltage", "operator": "below",
        "threshold": 11.8, "persistence_observations": 3, "window_seconds": 60,
        "max_age_seconds": 10, "engine_running": False}
STAMP = datetime(2026, 9, 20, 20, tzinfo=timezone.utc)


def sample(value=11.5, seconds=0, **extra):
    return {"value": value, "unit": "V", "freshness": "fresh", "source": "ccan.broadcast.0x41a",
            "quality": "verified", "provenance": "fixture", "observed_at": (STAMP - timedelta(seconds=seconds)).isoformat(),
            "regime": "running:stationary:idle:warm", "trip_id": 1, **extra}


class History:
    def __init__(self):
        self.latest = sample()
        self.points = [sample(seconds=n) for n in (0, 3, 6)]
        self.rpm = None

    def latest_sample(self, metric, **kwargs):
        assert kwargs["fresh_only"] is False
        return self.rpm if metric == "engine.rpm" else self.latest

    def recent_numeric_samples(self, *_args, **_kwargs):
        return self.points

    def refresh_rollups(self, **_kwargs):
        pass


class CustomWarningTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.row = {"id": "a" * 64, "rule": dict(RULE)}

    def evaluate(self):
        return evaluate_rule(self.history, self.row, STAMP)

    def test_persistence_and_owner_reference(self):
        result = self.evaluate()
        self.assertEqual(result["state"], "warning")
        self.assertTrue(result["notification_eligible"])
        self.assertIn("not an OEM limit", result["interpretation"])
        self.history.points = self.history.points[:1]
        self.assertEqual(self.evaluate()["state"], "watch")

    def test_no_stale_future_unqualified_or_invalid_latest_alerts(self):
        for point in (sample(seconds=11), sample(seconds=-1), sample(freshness="stale"),
                      sample(quality="candidate"), sample(unit="psi"), sample(value=True), sample(value=float("nan"))):
            with self.subTest(point=point):
                self.history.latest = point
                self.assertEqual(self.evaluate()["state"], "unavailable")

    def test_distinct_observations_gaps_and_intervening_normal_reset(self):
        for points in ([sample()] * 3, [sample(), sample(seconds=20), sample(seconds=23)],
                       [sample(), sample(value=12.5, seconds=3), sample(seconds=6)]):
            self.history.points = points
            self.assertFalse(self.evaluate()["notification_eligible"])

    def test_running_evidence_required(self):
        self.row["rule"]["engine_running"] = True
        self.assertEqual(self.evaluate()["state"], "unavailable")
        self.history.rpm = sample(value=0, unit="rpm", source="ccan.broadcast.0x0fc", quality="observed_alfa_scale")
        self.assertEqual(self.evaluate()["state"], "unavailable")
        original = self.history.latest_sample
        self.history.latest_sample = lambda metric, **kwargs: (
            sample(value=800, unit="rpm", source="ccan.broadcast.0x0fc", quality="observed_alfa_scale",
                   observed_at=kwargs["at"].isoformat()) if metric == "engine.rpm" else original(metric, **kwargs))
        self.assertEqual(self.evaluate()["state"], "warning")

    def test_absolute_magnitude_and_normal(self):
        self.row["rule"].update(operator="absolute_above", threshold=12)
        self.assertEqual(self.evaluate()["state"], "normal")
        self.history.latest = sample(value=-13)
        self.history.points = [sample(value=-13, seconds=n) for n in (0, 3, 6)]
        self.assertEqual(self.evaluate()["state"], "warning")

    def test_schema_and_limits_reject_unsafe_or_ambiguous_rules(self):
        for change in ({"command": "anything"}, {"metric": "generator.field_duty"},
                       {"metric": "engine.oil_life_remaining"}, {"threshold": float("nan")},
                       {"persistence_observations": 1}, {"persistence_observations": True},
                       {"max_age_seconds": 3600}, {"window_seconds": 601}, {"engine_running": "yes"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_rule({**RULE, **change})

    def test_configuration_reload_and_failure_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warnings.json"
            evaluator = EarlyWarningEvaluator(self.history, rules=(), custom_rules_path=path)
            self.assertEqual(evaluator.evaluate(at=STAMP)["custom_rules"]["count"], 0)
            path.write_text(json.dumps({"version": 1, "rules": [self.row]}))
            self.assertEqual(len(evaluator.evaluate(at=STAMP)["active"]), 1)
            path.write_text("broken config")
            self.assertIsNotNone(evaluator.evaluate(at=STAMP)["custom_rules"]["error"])
            path.write_text(json.dumps({"version": 1, "rules": []}))
            self.assertEqual(evaluator.evaluate(at=STAMP)["active"], [])

    def test_no_symlink_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warnings.json"
            real = Path(tmp) / "real.json"
            real.write_text(json.dumps({"version": 1, "rules": []}))
            path.symlink_to(real)
            with self.assertRaises(ValueError):
                load_rules(path)
