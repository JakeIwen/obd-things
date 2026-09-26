"""Acceptance tests for the tiered warning rules (tiers 0, 1 and 3).

Offline only: temporary historians, synthetic rollups and stub contexts.  No
CAN access, no services, no live database.
"""

import dataclasses
import json
import math
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from projects.vehicle_data import warning_context
from projects.vehicle_data.early_warning import (
    DEFAULT_EVALUATION_RULES,
    DEFAULT_WARNING_RULES,
    GROUPS,
    SEVERITIES,
    AbsoluteOilPressureRule,
    AbsoluteRule,
    CorroborationRule,
    EarlyWarningEvaluator,
    InfrastructureHealthEvaluator,
    TireGroupRule,
    WarningRule,
    _threshold,
    default_rule_catalog,
)
from projects.vehicle_data.historian import (
    BaselineStats,
    HistorianConfig,
    TelemetryHistorian,
)
from tests.test_vehicle_historian import (
    available,
    definition,
    role_aware_interface,
    role_status,
    snapshot,
    unavailable,
)


UTC = timezone.utc
US = 1_000_000
FORBIDDEN = re.compile(
    r"\b(regime|mad|deviation|persisten\w*|episode|advisory|unavailable)\b",
    re.IGNORECASE,
)
IDLE = "engine_running:stationary:rpm_idle:warm"
ROAD = "engine_running:road:rpm_low:warm"
WHEELS = ("fl", "fr", "rl", "rr")


def us(value: datetime) -> int:
    return int(round(value.timestamp() * US))


def rule_by_key(key):
    return next(rule for rule in DEFAULT_EVALUATION_RULES if rule.key == key)


class Harness(unittest.TestCase):
    start = datetime(2026, 9, 1, 12, tzinfo=UTC)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.defs = {
            "engine.rpm": definition("engine.rpm", "rpm"),
            "vehicle.speed": definition("vehicle.speed", "mph"),
            "engine.coolant_temperature": definition("engine.coolant_temperature", "°F"),
            "engine.oil_pressure": definition("engine.oil_pressure", "psi"),
            "transmission.oil_temperature": definition("transmission.oil_temperature", "°F"),
            "battery.voltage": definition("battery.voltage", "V", stale=30),
            "generator.field_duty": definition("generator.field_duty", "%"),
            **{
                f"tire.pressure.{wheel}": definition(f"tire.pressure.{wheel}", "psi", stale=35)
                for wheel in WHEELS
            },
        }
        self._count = 0

    def open(self, *, idle=15.0):
        self._count += 1
        historian = TelemetryHistorian(
            Path(self.tmp.name) / f"history-{self._count}.sqlite3",
            config=HistorianConfig(trip_idle_timeout_seconds=idle, rollup_seconds=5),
        )
        self.addCleanup(historian.close)
        return historian

    def ingest(self, historian, at, values, *, running=None):
        metrics = {
            name: (
                available(defn, values[name], at)
                if values.get(name) is not None
                else unavailable(defn)
            )
            for name, defn in self.defs.items()
        }
        if running is None:
            running = (values.get("engine.rpm") or 0) >= 400
        historian.ingest_snapshot(
            snapshot(at, list(self.defs.values()), metrics, running=running),
            captured_at=at,
        )

    def add_trip(self, historian, trip_id, started, ended):
        with historian._lock, historian._conn:
            historian._conn.execute(
                "INSERT INTO trips(id,started_us,started_at,last_active_us,last_active_at,"
                "ended_us,ended_at,start_basis,end_reason,snapshot_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    trip_id, us(started), started.isoformat(), us(ended), ended.isoformat(),
                    us(ended), ended.isoformat(), "test", "test", 0,
                ),
            )

    def add_rollups(self, historian, metric, rows):
        """rows: (bucket datetime, trip id, stored regime, median value)."""

        defn = self.defs[metric]
        source = defn["sources"][0]
        with historian._lock, historian._conn:
            historian._conn.executemany(
                "INSERT INTO metric_rollups(bucket_us,bucket_seconds,trip_key,metric,regime,"
                "unit,source,quality,provenance,sample_count,minimum,maximum,mean,median,mad,"
                "first_us,last_us) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        us(bucket), 60, trip, metric, regime, defn["unit"], source["name"],
                        source["quality"], source["provenance"], 1, value, value, value,
                        value, 0.0, us(bucket), us(bucket),
                    )
                    for bucket, trip, regime, value in rows
                ],
            )

    def prior_trips(self, historian, count=3, *, minutes=40, gap_days=1):
        """Completed synthetic trips ending at least a day before ``start``."""

        trips = []
        for index in range(count):
            started = self.start - timedelta(days=gap_days * (count - index) + 1)
            ended = started + timedelta(minutes=minutes)
            self.add_trip(historian, index + 1, started, ended)
            trips.append((index + 1, started, ended))
        return trips

    @staticmethod
    def rule_for(evaluator_report, key):
        return next(item for item in evaluator_report["assessments"] if item["rule"] == key)


class RelativeTierOneTests(Harness):
    def train_oil(self, historian):
        """Three prior trips at 29/30/31 psi (MAD 1 psi) in the idle regime."""

        rpm, speed, coolant, oil = (self.defs[name] for name in (
            "engine.rpm", "vehicle.speed", "engine.coolant_temperature", "engine.oil_pressure"))
        definitions = [rpm, speed, coolant, oil]
        for trip in range(3):
            trip_start = self.start + timedelta(seconds=trip * 60)
            for point, value in enumerate((29.0, 30.0, 31.0)):
                at = trip_start + timedelta(seconds=point * 10)
                historian.ingest_snapshot(snapshot(at, definitions, {
                    "engine.rpm": available(rpm, 800.0, at),
                    "vehicle.speed": available(speed, 0.0, at),
                    "engine.coolant_temperature": available(coolant, 190.0, at),
                    "engine.oil_pressure": available(oil, value, at),
                }), captured_at=at)
            idle = trip_start + timedelta(seconds=40)
            historian.ingest_snapshot(snapshot(
                idle, definitions, {d["name"]: unavailable(d) for d in definitions},
                running=False), captured_at=idle)
        return definitions

    def oil_point(self, historian, definitions, at, value):
        rpm, speed, coolant, oil = definitions
        historian.ingest_snapshot(snapshot(at, definitions, {
            "engine.rpm": available(rpm, 800.0, at),
            "vehicle.speed": available(speed, 0.0, at),
            "engine.coolant_temperature": available(coolant, 190.0, at),
            "engine.oil_pressure": available(oil, value, at),
        }), captured_at=at)

    def oil_rule(self, key="oil_test", **changes):
        values = dict(
            key=key,
            title="Oil test",
            metric="engine.oil_pressure",
            direction="low",
            minimum_effect=1.0,
            sigma_floor=2.0,
            minimum_baseline_buckets=6,
            minimum_baseline_trips=3,
            persistence_observations=3,
            persistence_window_seconds=20,
        )
        values.update(changes)
        return WarningRule(**values)

    def test_threshold_uses_sigma_floor(self):
        baseline = BaselineStats(
            metric="m", regime="r", unit="u", quality="q", source="s", provenance="p",
            bucket_count=40, trip_count=4, sample_count=40, median=14.0, mad=0.0,
            minimum=14.0, maximum=14.0, first_at="", last_at="",
        )
        self.assertEqual(
            _threshold(baseline, mad_multiplier=4.5, minimum_effect=1.0, sigma_floor=1.0), 4.5
        )
        self.assertEqual(
            _threshold(baseline, mad_multiplier=4.5, minimum_effect=5.0, sigma_floor=1.0), 5.0
        )
        # The keyword default keeps the corroborator's legacy call valid.
        self.assertEqual(_threshold(baseline, mad_multiplier=4.5, minimum_effect=0.2), 0.2)
        historian = self.open()
        definitions = self.train_oil(historian)
        at = self.start + timedelta(seconds=300)
        self.oil_point(historian, definitions, at, 20.0)
        assessment = EarlyWarningEvaluator(historian, (self.oil_rule(),)).evaluate(at=at)[
            "assessments"][0]
        # MAD 1 psi -> robust sigma 1.48 < 2.0 floor -> 4.5 x 2.0.
        self.assertAlmostEqual(assessment["deviation"]["threshold"], 9.0)
        self.assertEqual(assessment["deviation"]["sigma_floor"], 2.0)

    def test_single_sample_is_candidate_then_warning_after_persistence(self):
        historian = self.open()
        definitions = self.train_oil(historian)
        rule = self.oil_rule()
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        first_at = self.start + timedelta(seconds=300)
        reports = []
        for index in range(3):
            at = first_at + timedelta(seconds=5 * index)
            self.oil_point(historian, definitions, at, 20.0)
            reports.append(evaluator.evaluate(at=at))
        first = reports[0]["assessments"][0]
        self.assertEqual(first["state"], "normal")
        self.assertFalse(first["notification_eligible"])
        self.assertEqual(first["candidate"]["observed"], 1)
        self.assertEqual(first["candidate"]["required"], 3)
        self.assertEqual(first["candidate"]["confidence"], "low")
        self.assertEqual(first["candidate"]["window_seconds"], 20.0)
        self.assertEqual(first["confidence"], "low")
        self.assertEqual(reports[0]["active"], [])
        self.assertEqual([item["rule"] for item in reports[0]["candidates"]], ["oil_test"])
        second = reports[1]["assessments"][0]
        self.assertEqual(second["candidate"]["observed"], 2)
        # Candidate memory keeps the first sighting.
        self.assertEqual(second["candidate"]["first_seen_at"], first["candidate"]["first_seen_at"])
        self.assertEqual(first["candidate"]["first_seen_at"], first_at.isoformat())
        warning = reports[2]["assessments"][0]
        self.assertEqual(warning["state"], "warning")
        self.assertTrue(warning["notification_eligible"])
        self.assertEqual(warning["confidence"], "medium")
        self.assertNotIn("candidate", warning)
        self.assertEqual(warning["trigger"], "deviation")
        self.assertEqual([item["rule"] for item in reports[2]["active"]], ["oil_test"])
        self.assertEqual(reports[2]["candidates"], [])
        self.assertNotIn("oil_test", evaluator._candidates)
        self.assertEqual((warning["tier"], warning["group"]), (1, "oil"))
        self.assertEqual(warning["action"], "Check it at the next stop.")
        self.assertEqual(warning["evaluator_revision"], "relative-v3")
        snapshot_fields = warning["rule_snapshot"]
        for key in ("max_age_seconds", "recovery_seconds", "deescalate_fraction",
                    "recovery_observations"):
            self.assertIn(key, snapshot_fields)

    def test_hysteresis_holds_warning_until_samples_clear_the_lower_bar(self):
        historian = self.open()
        definitions = self.train_oil(historian)
        rule = self.oil_rule()
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        at = self.start + timedelta(seconds=300)
        for index in range(3):
            at = self.start + timedelta(seconds=300 + 5 * index)
            self.oil_point(historian, definitions, at, 20.0)
        warning = evaluator.evaluate(at=at)["assessments"][0]
        self.assertEqual(warning["state"], "warning")
        historian.record_advisory_assessments([warning], evaluated_at=at)
        self.assertEqual(
            historian.list_advisory_episodes(active_only=True)[0]["state"], "warning"
        )
        # Effect 7.5 psi: below the 9 psi threshold but above 0.7 x 9 = 6.3.
        states = []
        for index in range(4):
            at = self.start + timedelta(seconds=315 + 5 * index)
            self.oil_point(historian, definitions, at, 22.5)
            states.append(evaluator.evaluate(at=at)["assessments"][0])
        self.assertTrue(all(item["state"] == "warning" for item in states))
        self.assertEqual(states[-1]["trigger"], "hysteresis")
        self.assertAlmostEqual(states[-1]["hysteresis"]["deescalate_threshold"], 6.3)
        self.assertEqual(states[-1]["hysteresis"]["clear_observed"], 0)
        # The same value without an open warning is simply normal.
        fresh = EarlyWarningEvaluator(historian, (self.oil_rule("oil_fresh"),)).evaluate(at=at)
        self.assertEqual(fresh["assessments"][0]["state"], "normal")
        self.assertNotIn("candidate", fresh["assessments"][0])
        # Effect 5 psi (<= 6.3) must hold for N samples before normal.
        cleared = []
        for index in range(3):
            at = self.start + timedelta(seconds=340 + 5 * index)
            self.oil_point(historian, definitions, at, 25.0)
            cleared.append(evaluator.evaluate(at=at)["assessments"][0])
        self.assertEqual([item["state"] for item in cleared], ["warning", "warning", "normal"])
        self.assertEqual(cleared[-1]["hysteresis"]["clear_observed"], 3)

    def test_candidate_memory_expires_after_two_windows(self):
        historian = self.open()
        definitions = self.train_oil(historian)
        evaluator = EarlyWarningEvaluator(historian, (self.oil_rule(),))
        first = self.start + timedelta(seconds=300)
        self.oil_point(historian, definitions, first, 20.0)
        candidate = evaluator.evaluate(at=first)["assessments"][0]["candidate"]
        self.assertEqual(candidate["first_seen_at"], first.isoformat())
        self.assertIn("oil_test", evaluator._candidates)
        # Within two windows (40 s) the memory survives an in-band reading.
        inside = first + timedelta(seconds=30)
        self.oil_point(historian, definitions, inside, 30.0)
        self.assertEqual(evaluator.evaluate(at=inside)["assessments"][0]["state"], "normal")
        self.assertIn("oil_test", evaluator._candidates)
        # Two windows after the last qualifying reading the memory is gone.
        later = first + timedelta(seconds=41)
        self.oil_point(historian, definitions, later, 30.0)
        evaluator.evaluate(at=later)
        self.assertNotIn("oil_test", evaluator._candidates)
        again = first + timedelta(seconds=46)
        self.oil_point(historian, definitions, again, 20.0)
        renewed = evaluator.evaluate(at=again)["assessments"][0]["candidate"]
        self.assertEqual(renewed["first_seen_at"], again.isoformat())
        self.assertEqual(renewed["observed"], 1)

    def test_warning_rule_derives_and_validates_grading_fields(self):
        oil = WarningRule(key="k", title="Oil", metric="engine.oil_pressure", direction="low")
        self.assertEqual(oil.group, "oil")
        self.assertEqual(oil.action, "Check it at the next stop.")
        self.assertEqual(oil.tier, 1)
        self.assertEqual(oil.recovery_observations, 3)
        self.assertEqual(oil.recovery_seconds, 600)
        for metric, group in (
            ("engine.coolant_temperature", "cooling"),
            ("transmission.oil_temperature", "transmission"),
            ("battery.voltage", "charging"),
            ("tire.pressure.rr", "tires"),
            ("vehicle.speed", "custom"),
        ):
            self.assertEqual(
                WarningRule(key="k", title="t", metric=metric, direction="high").group, group
            )
        bad = (
            {"group": "engine"},
            {"action": "Check the oil"},
            {"action": "Watch the deviation."},
            {"action": "Check it" + " now" * 40 + "."},
            {"deescalate_fraction": 1.2},
            {"deescalate_fraction": 0},
            {"phase_dimension": "motion"},
            {"companion_band": ("generator.field_duty", 90, 80)},
            {"rate_of_rise": (215.0, -0.1, 180.0)},
        )
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                WarningRule(key="k", title="t", metric="engine.oil_pressure",
                            direction="low", **change)
        with self.assertRaises(ValueError):
            AbsoluteRule(key="k", title="t", metric="battery.voltage", group="engine",
                         action="Check it.", unit="V", direction="low", gate="parked",
                         warning_threshold=11.8, reference_provenance="p")
        with self.assertRaises(ValueError):
            TireGroupRule(wheels=("fl", "xx"))
        catalog = default_rule_catalog()
        json.dumps(catalog, allow_nan=False)
        self.assertEqual(len(catalog), len(DEFAULT_EVALUATION_RULES))

    def test_baseline_memo_caches_every_baseline_within_a_trip(self):
        historian = self.open()
        definitions = self.train_oil(historian)
        rule = self.oil_rule()
        at1 = self.start + timedelta(seconds=300)
        at2 = at1 + timedelta(seconds=5)
        self.oil_point(historian, definitions, at1, 20.0)
        self.oil_point(historian, definitions, at2, 20.0)
        with mock.patch.object(historian, "robust_baseline", wraps=historian.robust_baseline) as spy:
            memo_evaluator = EarlyWarningEvaluator(historian, (rule,))
            memo_evaluator.baseline_memo = {}
            memo_evaluator.evaluate(at=at1)
            memo_evaluator.evaluate(at=at2)
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(len(memo_evaluator.baseline_memo), 1)
            spy.reset_mock()
            plain = EarlyWarningEvaluator(historian, (rule,))
            self.assertIsNone(plain.baseline_memo)
            plain.evaluate(at=at1)
            plain.evaluate(at=at2)
            self.assertEqual(spy.call_count, 2)
        key = next(iter(memo_evaluator.baseline_memo))
        self.assertEqual(key[0], "engine.oil_pressure")
        self.assertIsInstance(key[-1], int)  # exclude_trip_id inside a trip
        # rollup_baseline (module function) goes through the same memo.
        trans = next(r for r in DEFAULT_WARNING_RULES if r.key.startswith("transmission"))
        trans_def = self.defs["transmission.oil_temperature"]
        for index, at in enumerate((at1 + timedelta(seconds=10), at1 + timedelta(seconds=15))):
            values = {
                "engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 190,
                "transmission.oil_temperature": 150.0 + index,
            }
            self.ingest(historian, at, values)
        del trans_def
        with mock.patch.object(
            warning_context, "rollup_baseline", wraps=warning_context.rollup_baseline
        ) as spy:
            evaluator = EarlyWarningEvaluator(historian, (trans,))
            evaluator.baseline_memo = {}
            evaluator.evaluate(at=at1 + timedelta(seconds=10))
            evaluator.evaluate(at=at1 + timedelta(seconds=15))
            self.assertEqual(spy.call_count, 1)
            spy.reset_mock()
            evaluator = EarlyWarningEvaluator(historian, (trans,))
            evaluator.evaluate(at=at1 + timedelta(seconds=10))
            evaluator.evaluate(at=at1 + timedelta(seconds=15))
            self.assertEqual(spy.call_count, 2)


class ConditionedBaselineTests(Harness):
    def test_transmission_band_comes_from_its_own_value(self):
        historian = self.open()
        trips = self.prior_trips(historian)
        rows = []
        for trip_id, started, _ended in trips:
            for index in range(12):
                rows.append((started + timedelta(minutes=5 + index), trip_id, ROAD, 150.0))
                rows.append((started + timedelta(minutes=20, seconds=30 * index), trip_id, ROAD, 185.0))
        self.add_rollups(historian, "transmission.oil_temperature", rows)
        rule = rule_by_key("transmission_oil_temperature_relative_high")
        for index in range(2):
            at = self.start + timedelta(seconds=5 * index)
            self.ingest(historian, at, {
                "engine.rpm": 1500, "vehicle.speed": 50, "engine.coolant_temperature": 190,
                "transmission.oil_temperature": 152.0,
            })
        warm = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=at)["assessments"][0]
        self.assertEqual(warm["conditioning"]["thermal_band"], "warm")
        self.assertEqual(warm["baseline"]["bucket_count"], 36)
        self.assertEqual(warm["baseline"]["median"], 150.0)
        self.assertIn("thermal_band=warm", warm["baseline_regime"])
        self.assertIn("thermal_band", warm["regime_dimensions"])
        at = at + timedelta(seconds=5)
        self.ingest(historian, at, {
            "engine.rpm": 1500, "vehicle.speed": 50, "engine.coolant_temperature": 190,
            "transmission.oil_temperature": 181.0,
        })
        hot = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=at)["assessments"][0]
        self.assertEqual(hot["conditioning"]["thermal_band"], "hot")
        self.assertEqual(hot["baseline"]["median"], 185.0)
        self.assertEqual(hot["baseline"]["bucket_count"], 36)

    def test_battery_baseline_uses_high_duty_buckets_and_requires_generator(self):
        historian = self.open()
        trips = self.prior_trips(historian)
        voltage, duty = [], []
        for trip_id, started, _ended in trips:
            for index in range(12):
                high = started + timedelta(minutes=5 + index)
                low = started + timedelta(minutes=20 + index)
                voltage += [(high, trip_id, IDLE, 14.2), (low, trip_id, IDLE, 13.0)]
                duty += [(high, trip_id, IDLE, 92.0), (low, trip_id, IDLE, 30.0)]
        self.add_rollups(historian, "battery.voltage", voltage)
        self.add_rollups(historian, "generator.field_duty", duty)
        rule = dataclasses.replace(
            rule_by_key("battery_voltage_relative_low"),
            persistence_observations=3,
            persistence_window_seconds=15.0,
        )
        self.assertEqual(rule.required_corroborators, 1)
        for index in range(3):
            at = self.start + timedelta(seconds=5 * index)
            self.ingest(historian, at, {
                "engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 190,
                "battery.voltage": 13.2, "generator.field_duty": 95.0,
            })
        warning = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=at)["assessments"][0]
        self.assertEqual(warning["baseline"]["bucket_count"], 36)
        self.assertAlmostEqual(warning["baseline"]["median"], 14.2)
        self.assertIn("generator.field_duty=85..100", warning["baseline_regime"])
        self.assertEqual(warning["state"], "warning")
        self.assertEqual(warning["confidence"], "high")
        self.assertEqual(warning["corroborators"][0]["state"], "corroborating")
        self.assertEqual(warning["corroborators"][0]["band"], "high")
        self.assertEqual(warning["group"], "charging")
        # The inputs are registered for event-history archival.
        self.assertIn(warning["baseline"]["input_digest"], historian._baseline_inputs)
        # Low generator duty is a PCM charge reduction, not a warning.
        for index in range(3, 6):
            at = self.start + timedelta(seconds=5 * index)
            self.ingest(historian, at, {
                "engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 190,
                "battery.voltage": 13.2, "generator.field_duty": 30.0,
            })
        quiet = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=at)["assessments"][0]
        self.assertEqual(quiet["state"], "normal")
        self.assertEqual(quiet["corroborators"][0]["state"], "not_corroborating")
        self.assertEqual(quiet["corroborators"][0]["band"], "low")
        self.assertGreaterEqual(quiet["candidate"]["observed"], 3)

    def test_rollup_baseline_matches_robust_baseline_statistics(self):
        historian = self.open()
        trips = self.prior_trips(historian)
        rows = [
            (started + timedelta(minutes=5 + index), trip_id, IDLE, 30.0 + (index % 5))
            for trip_id, started, _ended in trips
            for index in range(12)
        ]
        self.add_rollups(historian, "engine.oil_pressure", rows)
        source = self.defs["engine.oil_pressure"]["sources"][0]
        common = dict(
            before=self.start, lookback_days=30, exclude_trip_id=None, unit="psi",
            quality=source["quality"], source=source["name"], provenance=source["provenance"],
        )
        for dims in (("engine", "motion", "rpm"), ("engine",), ("motion",),
                     ("engine", "motion", "rpm", "thermal")):
            expected = historian.robust_baseline(
                "engine.oil_pressure", IDLE, regime_dimensions=dims, **common)
            actual = warning_context.rollup_baseline(
                historian, "engine.oil_pressure", regime=IDLE, regime_dimensions=dims, **common)
            self.assertEqual(actual, expected)
        self.assertIn(actual.input_digest, historian._baseline_inputs)

    def test_duty_and_transmission_bands(self):
        self.assertEqual(
            [warning_context.duty_band(v) for v in (10, 39.9, 40, 84.9, 85, 100, None)],
            ["low", "low", "mid", "mid", "high", "high", None],
        )
        self.assertEqual(warning_context.transmission_band(119.9)[0], "cold")
        self.assertEqual(warning_context.transmission_band(120)[0], "warm")
        self.assertEqual(warning_context.transmission_band(175)[0], "hot")


class TireRuleTests(Harness):
    def tire_history(self, historian):
        """Three cold starts: cold 74/75, warming 77/78, warm 82/83 psi (RR/RL)."""

        trips = self.prior_trips(historian)
        speed, rr, rl = [], [], []
        for trip_id, started, _ended in trips:
            speed.append((started, trip_id, IDLE, 0.0))
            speed.append((started + timedelta(seconds=60), trip_id, ROAD, 40.0))
            for step in range(120):
                offset = 20 * step
                bucket = started + timedelta(seconds=offset)
                if offset < 240:
                    values = (74.0, 75.0)
                elif offset < 1260:
                    values = (77.0, 78.0)
                else:
                    values = (82.0, 83.0)
                rr.append((bucket, trip_id, ROAD, values[0]))
                rl.append((bucket, trip_id, ROAD, values[1]))
        self.add_rollups(historian, "vehicle.speed", speed)
        self.add_rollups(historian, "tire.pressure.rr", rr)
        self.add_rollups(historian, "tire.pressure.rl", rl)
        return trips

    def small_tire_rule(self):
        return dataclasses.replace(
            TireGroupRule(), persistence_observations=3, persistence_window_seconds=15.0
        )

    def test_phase_index_and_phase_baselines(self):
        historian = self.open()
        trips = self.tire_history(historian)
        index = warning_context.trip_phase_index(historian, at=self.start)
        trip_id, started, _ended = trips[0]
        info = index[trip_id]
        self.assertTrue(info["cold_eligible"])
        self.assertEqual(info["first_moving_us"], us(started + timedelta(seconds=60)))
        self.assertEqual(warning_context.phase_at(info, us(started + timedelta(seconds=200))), "cold")
        self.assertEqual(warning_context.phase_at(info, us(started + timedelta(seconds=300))), "warming")
        self.assertEqual(warning_context.phase_at(info, us(started + timedelta(minutes=30))), "warm")
        self.assertIsNone(warning_context.phase_of({"trip_id": None, "captured_at": self.start.isoformat()}, index))
        source = self.defs["tire.pressure.rr"]["sources"][0]
        medians = {}
        for phase in ("cold", "warming", "warm"):
            baseline = warning_context.rollup_baseline(
                historian, "tire.pressure.rr", regime=ROAD, regime_dimensions=("engine",),
                before=self.start, exclude_trip_id=None, unit="psi", quality=source["quality"],
                source=source["name"], provenance=source["provenance"], phase=(phase, index),
            )
            medians[phase] = (baseline.median, baseline.bucket_count)
            self.assertTrue(baseline.regime.endswith(f"tire_phase={phase}"))
        self.assertEqual(medians, {"cold": (74.0, 36), "warming": (77.0, 153), "warm": (82.0, 171)})
        # A trip that starts less than four hours after the last has no cold phase.
        self.add_trip(historian, 9, self.start - timedelta(hours=3), self.start - timedelta(hours=2))
        self.add_trip(historian, 10, self.start - timedelta(hours=1), self.start - timedelta(minutes=30))
        near = warning_context.trip_phase_index(historian, at=self.start)[10]
        self.assertFalse(near["cold_eligible"])
        self.assertAlmostEqual(near["prior_gap_seconds"], 3600.0)
        self.assertIsNone(warning_context.phase_at(near, us(self.start - timedelta(hours=1))))

    def test_combined_rule_reports_worst_wheel_and_lists_all_in_cold_phase(self):
        historian = self.open()
        self.tire_history(historian)
        rule = self.small_tire_rule()
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        reports = []
        for index in range(3):
            at = self.start + timedelta(seconds=5 * index)
            self.ingest(historian, at, {
                "engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 150,
                "tire.pressure.rr": 70.0, "tire.pressure.rl": 70.5,
            })
            reports.append(evaluator.evaluate(at=at)["assessments"][0])
        first = reports[0]
        self.assertEqual(first["state"], "normal")
        self.assertEqual(first["candidate"]["observed"], 1)
        warning = reports[-1]
        self.assertEqual(warning["rule"], "tire_pressure_relative_low")
        self.assertEqual(warning["state"], "warning")
        self.assertEqual(warning["phase"], "cold")
        self.assertEqual(warning["metric"], "tire.pressure.rl")
        self.assertEqual(warning["wheels_past_threshold"], ["rl", "rr"])
        self.assertEqual(warning["action"], "Check the RL and RR tire pressure at the next stop.")
        self.assertEqual(warning["title"], "RL and RR tires low")
        self.assertEqual(warning["confidence"], "medium")
        self.assertEqual(warning["baseline"]["median"], 75.0)
        self.assertEqual(warning["wheel_assessments"]["rr"]["baseline"]["median"], 74.0)
        self.assertEqual(warning["wheel_assessments"]["rr"]["state"], "warning")
        self.assertEqual(warning["wheel_assessments"]["fl"]["state"], "unavailable")
        for wheel in warning["wheel_assessments"].values():
            self.assertNotIn("observations", wheel.get("persistence") or {})
            self.assertNotIn("input_buckets", wheel.get("baseline") or {})
        self.assertEqual(warning["evaluator_revision"], "tires-v1")
        self.assertIn("deescalate_fraction", warning["rule_snapshot"])

    def test_warming_phase_compares_with_warming_history_only(self):
        historian = self.open(idle=600)
        self.tire_history(historian)
        rule = self.small_tire_rule()
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        values = {"engine.rpm": 1500, "vehicle.speed": 30, "engine.coolant_temperature": 190,
                  "tire.pressure.rr": 73.0, "tire.pressure.rl": 78.0}
        self.ingest(historian, self.start, values)
        for offset in (300, 305, 310):
            at = self.start + timedelta(seconds=offset)
            self.ingest(historian, at, values)
        warning = evaluator.evaluate(at=at)["assessments"][0]
        self.assertEqual(warning["phase"], "warming")
        self.assertEqual(warning["state"], "warning")
        self.assertEqual(warning["metric"], "tire.pressure.rr")
        self.assertEqual(warning["wheels_past_threshold"], ["rr"])
        self.assertEqual(warning["baseline"]["median"], 77.0)
        self.assertEqual(warning["wheel_assessments"]["rl"]["state"], "normal")
        self.assertEqual(warning["persistence"]["observed"], 3)

    def test_unknown_phase_is_not_applicable(self):
        historian = self.open()
        self.tire_history(historian)
        self.add_trip(historian, 9, self.start - timedelta(hours=2), self.start - timedelta(hours=1))
        at = self.start
        self.ingest(historian, at, {
            "engine.rpm": 800, "vehicle.speed": 0, "tire.pressure.rr": 60.0,
            "tire.pressure.rl": 60.0,
        })
        report = EarlyWarningEvaluator(historian, (self.small_tire_rule(),)).evaluate(at=at)
        for assessment in report["assessments"]:
            self.assertEqual(assessment["state"], "not_applicable", assessment["rule"])

    def test_absolute_tire_limit_applies_without_a_cold_phase(self):
        """A trip < 4 h after the last has no cold phase; 60 psi rears still warn."""

        historian = self.open(idle=600)
        self.tire_history(historian)
        self.add_trip(historian, 9, self.start - timedelta(hours=2), self.start - timedelta(hours=1))
        evaluator = EarlyWarningEvaluator(historian, (rule_by_key("tire_pressure_low_absolute"),))
        states = []
        for index in range(3):
            at = self.start + timedelta(seconds=6 * index)
            self.ingest(historian, at, {
                "engine.rpm": 800, "vehicle.speed": 0, "tire.pressure.rr": 60.0,
                "tire.pressure.rl": 60.0,
            })
            states.append(evaluator.evaluate(at=at)["assessments"][0])
        self.assertIsNone(states[-1]["phase"])
        self.assertEqual([item["state"] for item in states], ["normal", "normal", "warning"])
        self.assertEqual(states[-1]["wheels_past_threshold"], ["rl", "rr"])

    def test_absolute_tire_limit_catches_a_mid_trip_puncture(self):
        historian = self.open(idle=600)
        self.tire_history(historian)
        evaluator = EarlyWarningEvaluator(historian, (rule_by_key("tire_pressure_low_absolute"),))
        values = {"engine.rpm": 2000, "vehicle.speed": 65, "engine.coolant_temperature": 195,
                  "tire.pressure.rr": 82.0, "tire.pressure.rl": 83.0}
        for offset in range(0, 1800, 60):
            self.ingest(historian, self.start + timedelta(seconds=offset), values)
        for offset in range(1800, 1800 + 3 * 6, 6):  # 30 min into the drive: warm phase
            at = self.start + timedelta(seconds=offset)
            self.ingest(historian, at, {**values, "tire.pressure.rr": 55.0})
        warning = evaluator.evaluate(at=at)["assessments"][0]
        self.assertEqual(warning["phase"], "warm")
        self.assertEqual((warning["state"], warning["severity"]), ("warning", "critical"))
        self.assertEqual(warning["wheels_past_threshold"], ["rr"])

    def test_absolute_cold_tire_limits(self):
        historian = self.open()
        rule = rule_by_key("tire_pressure_low_absolute")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        states = []
        for index in range(3):
            at = self.start + timedelta(seconds=5 * index)
            self.ingest(historian, at, {
                "engine.rpm": 800, "vehicle.speed": 0, "tire.pressure.fl": 55.0,
                "tire.pressure.fr": 55.0, "tire.pressure.rl": 67.0, "tire.pressure.rr": 66.0,
            })
            states.append(evaluator.evaluate(at=at)["assessments"][0])
        self.assertEqual([item["state"] for item in states], ["normal", "normal", "warning"])
        self.assertEqual(states[0]["candidate"]["observed"], 1)
        warning = states[-1]
        self.assertEqual(warning["severity"], "warning")
        self.assertEqual(warning["metric"], "tire.pressure.rr")
        self.assertEqual(warning["wheels_past_threshold"], ["rr", "rl"])
        self.assertEqual(warning["action"], "Inflate the RR and RL tires before driving far.")
        self.assertEqual(warning["absolute_threshold"], {"operator": "below", "value": 68.0, "unit": "psi"})
        self.assertEqual(warning["tier"], 0)
        historian.record_advisory_assessments([warning], evaluated_at=at)
        # 69 psi is above 68 but inside the 2 psi clear margin: still held.
        for index in range(3, 6):
            at = self.start + timedelta(seconds=5 * index)
            self.ingest(historian, at, {
                "engine.rpm": 800, "vehicle.speed": 0, "tire.pressure.rl": 71.0,
                "tire.pressure.rr": 69.0,
            })
        held = evaluator.evaluate(at=at)["assessments"][0]
        self.assertEqual(held["state"], "warning")
        self.assertEqual(held["action_wheels"], ["rr"])
        for index in range(6, 9):
            at = self.start + timedelta(seconds=5 * index)
            self.ingest(historian, at, {
                "engine.rpm": 800, "vehicle.speed": 0, "tire.pressure.rl": 71.0,
                "tire.pressure.rr": 70.5,
            })
        self.assertEqual(evaluator.evaluate(at=at)["assessments"][0]["state"], "normal")
        critical_db = self.open()
        evaluator = EarlyWarningEvaluator(critical_db, (rule,))
        for index in range(3):
            at = self.start + timedelta(seconds=5 * index)
            self.ingest(critical_db, at, {"engine.rpm": 800, "vehicle.speed": 0,
                                          "tire.pressure.rr": 58.0})
        critical = evaluator.evaluate(at=at)["assessments"][0]
        self.assertEqual((critical["state"], critical["severity"]), ("warning", "critical"))

    def test_pair_asymmetry_removes_the_usual_offset(self):
        historian = self.open()
        trips = self.prior_trips(historian)
        rl, rr = [], []
        for trip_id, started, _ended in trips:
            for index in range(12):
                bucket = started + timedelta(minutes=5 + index)
                rl.append((bucket, trip_id, ROAD, 77.0))
                rr.append((bucket, trip_id, ROAD, 76.2))
        self.add_rollups(historian, "tire.pressure.rl", rl)
        self.add_rollups(historian, "tire.pressure.rr", rr)
        default = rule_by_key("tire_pressure_pair_asymmetry")
        self.assertEqual((default.persistence_observations, default.persistence_window_seconds), (60, 480.0))
        rule = dataclasses.replace(default, persistence_observations=3, persistence_window_seconds=15.0)
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        base = {"engine.rpm": 1500, "vehicle.speed": 50, "engine.coolant_temperature": 190}
        # Moving for longer than PAIR_ROLLING_TRUST_SECONDS: both sensors count.
        for offset in range(0, 160, 8):
            self.ingest(historian, self.start + timedelta(seconds=offset),
                        {**base, "tire.pressure.rl": 77.0, "tire.pressure.rr": 76.2})
        # Raw difference 3.4 psi, but only 2.6 psi beyond the usual 0.8 psi.
        for index in range(3):
            at = self.start + timedelta(seconds=160 + 5 * index)
            self.ingest(historian, at, {**base, "tire.pressure.rl": 77.0, "tire.pressure.rr": 73.6})
        normal = evaluator.evaluate(at=at)["assessments"][0]
        self.assertEqual(normal["state"], "normal")
        self.assertNotIn("candidate", normal)
        self.assertAlmostEqual(normal["pair"]["usual_offset"], 0.8)
        self.assertAlmostEqual(normal["pair"]["asymmetry"], 2.6)
        self.assertEqual(normal["axle_assessments"]["flfr"]["state"], "unavailable")
        for index in range(3, 6):
            at = self.start + timedelta(seconds=160 + 5 * index)
            self.ingest(historian, at, {**base, "tire.pressure.rl": 77.0, "tire.pressure.rr": 72.8})
        warning = evaluator.evaluate(at=at)["assessments"][0]
        self.assertEqual(warning["state"], "warning")
        self.assertEqual(warning["confidence"], "high")
        self.assertEqual(warning["metric"], "tire.pressure.rr")
        self.assertEqual(warning["action"], "Check both rear tires at the next stop.")
        self.assertEqual(warning["title"], "Rear tires uneven")
        self.assertEqual([c["state"] for c in warning["corroborators"]], ["paired", "paired"])


class AbsoluteTierZeroTests(Harness):
    def run_sequence(self, rule, seconds, values, *, historian=None, evaluator=None):
        historian = historian or self.open()
        evaluator = evaluator or EarlyWarningEvaluator(historian, (rule,))
        results = []
        for second in seconds:
            at = self.start + timedelta(seconds=second)
            self.ingest(historian, at, values(second) if callable(values) else values)
            results.append(evaluator.evaluate(at=at)["assessments"][0])
        return historian, evaluator, results

    def test_tier0_limits_confirm_at_the_live_observation_cadence(self):
        """The historian stores ~6 s observations (p90 8 s), not 5 s."""

        seconds, second = [], 0
        while second <= 300:
            seconds.append(second)
            second += 8 if len(seconds) % 3 == 0 else 6
        cases = {
            "transmission_oil_temperature_hot": {
                "engine.rpm": 1500, "vehicle.speed": 40, "engine.coolant_temperature": 200,
                "transmission.oil_temperature": 240.0},
            "engine_coolant_temperature_hot": {
                "engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 235.0},
            "battery_voltage_charging_failure": {
                "engine.rpm": 1500, "vehicle.speed": 40, "engine.coolant_temperature": 200,
                "battery.voltage": 11.5, "generator.field_duty": 100.0},
            "battery_voltage_charging_failure@idle": {
                "engine.rpm": 750, "vehicle.speed": 0, "engine.coolant_temperature": 200,
                "battery.voltage": 11.5, "generator.field_duty": 100.0},
            "engine_oil_pressure_below_band": {
                "engine.rpm": 700, "vehicle.speed": 0, "engine.coolant_temperature": 195,
                "engine.oil_pressure": 13.0},
            "tire_pressure_low_absolute": {
                "engine.rpm": 1500, "vehicle.speed": 45, "tire.pressure.rr": 55.0,
                "tire.pressure.rl": 76.0, "tire.pressure.fl": 57.0, "tire.pressure.fr": 56.0},
        }
        # Iteration-2 acceptance bounds (seconds from the first faulty reading).
        bounds = {
            "transmission_oil_temperature_hot": (100, "warning"),
            "engine_coolant_temperature_hot": (55, "warning"),
            "battery_voltage_charging_failure": (100, "warning"),
            "battery_voltage_charging_failure@idle": (100, "warning"),
            "engine_oil_pressure_below_band": (35, "warning"),
            "tire_pressure_low_absolute": (35, "critical"),
        }
        for key, values in cases.items():
            with self.subTest(rule=key):
                _historian, _evaluator, results = self.run_sequence(
                    rule_by_key(key.split("@")[0]), seconds, values, historian=self.open(idle=600)
                )
                first = next(
                    (i for i, item in enumerate(results) if item["state"] == "warning"),
                    None,
                )
                self.assertIsNotNone(first)
                limit, severity = bounds[key]
                self.assertLessEqual(seconds[first], limit)
                self.assertEqual(results[first]["severity"], severity)

    def test_coolant_hot_gate_persistence_critical_and_clear_margin(self):
        rule = rule_by_key("engine_coolant_temperature_hot")
        running = {"engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 235.0}
        historian, evaluator, results = self.run_sequence(rule, range(0, 40, 5), running)
        self.assertEqual(results[0]["state"], "not_applicable")  # startup grace
        self.assertEqual(results[6]["state"], "normal")          # 5 post-grace samples
        self.assertEqual(results[6]["candidate"]["observed"], 5)
        warning = results[7]
        self.assertEqual((warning["state"], warning["severity"]), ("warning", "warning"))
        self.assertEqual(warning["action"], "Reduce load and watch the gauge.")
        self.assertEqual(warning["confidence"], "medium")
        self.assertEqual(warning["absolute_threshold"]["value"], 230.0)
        self.assertEqual(warning["evaluator_revision"], "absolute-v1")
        self.assertIn("clear_margin", warning["rule_snapshot"])
        self.assertEqual(warning["running_evidence"]["qualified"], True)
        historian.record_advisory_assessments([warning], evaluated_at=self.start + timedelta(seconds=35))
        # 227 °F is below 230 but inside the 5 °F clear margin.
        _h, _e, held = self.run_sequence(
            rule, range(40, 70, 5), {**running, "engine.coolant_temperature": 227.0},
            historian=historian, evaluator=evaluator)
        self.assertTrue(all(item["state"] == "warning" for item in held))
        _h, _e, cleared = self.run_sequence(
            rule, range(70, 100, 5), {**running, "engine.coolant_temperature": 224.0},
            historian=historian, evaluator=evaluator)
        self.assertEqual([item["state"] for item in cleared][-2:], ["warning", "normal"])
        # Engine off: the gate blocks (inconclusive, keeps an open episode).
        _h, _e, off = self.run_sequence(
            rule, (100,), {"engine.rpm": 0, "engine.coolant_temperature": 235.0},
            historian=historian, evaluator=evaluator)
        self.assertEqual(off[0]["state"], "not_applicable")
        _h, _e, critical = self.run_sequence(
            rule, range(0, 40, 5), {**running, "engine.coolant_temperature": 245.0})
        self.assertEqual((critical[-1]["state"], critical[-1]["severity"]), ("warning", "critical"))
        self.assertEqual(critical[-1]["action"], "Pull over and let it idle to cool.")

    def test_transmission_hot_needs_twelve_samples(self):
        rule = rule_by_key("transmission_oil_temperature_hot")
        _h, _e, results = self.run_sequence(
            rule, range(0, 70, 5),
            {"engine.rpm": 1500, "vehicle.speed": 40, "transmission.oil_temperature": 232.0})
        self.assertEqual(results[-2]["state"], "normal")
        self.assertEqual(results[-1]["state"], "warning")
        self.assertEqual(results[-1]["group"], "transmission")

    def test_oil_band_floor_follows_rpm_and_needs_warm_engine(self):
        rule = rule_by_key("engine_oil_pressure_below_band")
        idle = {"engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 190,
                "engine.oil_pressure": 13.0}
        _h, _e, results = self.run_sequence(rule, range(0, 25, 5), idle)
        self.assertEqual(results[3]["state"], "normal")
        self.assertEqual(results[4]["state"], "warning")
        self.assertEqual(results[4]["absolute_threshold"]["value"], 15.0)
        self.assertEqual(results[4]["action"], "Check the oil level at the next stop.")
        _h, _e, results = self.run_sequence(rule, range(0, 25, 5), {**idle, "engine.oil_pressure": 16.0})
        self.assertEqual(results[-1]["state"], "normal")
        self.assertNotIn("candidate", results[-1])
        cruise = {**idle, "engine.rpm": 2000, "vehicle.speed": 50, "engine.oil_pressure": 20.0}
        _h, _e, results = self.run_sequence(rule, range(0, 25, 5), cruise)
        self.assertEqual(results[-1]["state"], "warning")
        self.assertEqual(results[-1]["absolute_threshold"]["value"], 22.0)
        _h, _e, results = self.run_sequence(rule, range(0, 25, 5), {**idle, "engine.rpm": 920})
        self.assertEqual(results[-1]["state"], "not_applicable")
        _h, _e, results = self.run_sequence(
            rule, range(0, 25, 5), {**idle, "engine.coolant_temperature": 150})
        self.assertEqual(results[-1]["state"], "not_applicable")

    def test_charging_failure_needs_saturated_generator_on_every_sample(self):
        rule = rule_by_key("battery_voltage_charging_failure")
        failing = {"engine.rpm": 1500, "vehicle.speed": 40, "battery.voltage": 11.9,
                   "generator.field_duty": 95.0}
        _h, _e, results = self.run_sequence(rule, range(0, 70, 5), failing)
        self.assertEqual(results[-2]["state"], "normal")
        warning = results[-1]
        self.assertEqual(warning["state"], "warning")
        self.assertEqual(warning["confidence"], "high")
        self.assertEqual(warning["persistence"]["companion_gaps"], 0)
        self.assertEqual(warning["title"], "Charging problem")
        _h, _e, results = self.run_sequence(
            rule, range(0, 70, 5), {**failing, "generator.field_duty": 50.0})
        self.assertEqual(results[-1]["state"], "normal")
        self.assertNotIn("candidate", results[-1])
        # Duty missing for 15 s: the 10 s companion age cannot bridge it.
        gap = {30, 35, 40}
        _h, _e, results = self.run_sequence(
            rule, range(0, 70, 5),
            lambda second: {**failing, "generator.field_duty": None if second in gap else 95.0})
        self.assertEqual(results[-1]["state"], "normal")
        self.assertEqual(results[-1]["persistence"]["companion_gaps"], 1)
        self.assertEqual(results[-1]["persistence"]["observed"], 5)  # 45..65 s
        # Idle counts too (stop-and-go, review S2): 800 rpm is evaluated, not gated out.
        _h, _e, results = self.run_sequence(rule, range(0, 20, 5), {**failing, "engine.rpm": 800})
        self.assertNotEqual(results[-1]["state"], "not_applicable")
        # Below the 400 rpm running threshold the rule does not apply.
        _h, _e, results = self.run_sequence(rule, range(0, 20, 5), {**failing, "engine.rpm": 300})
        self.assertEqual(results[-1]["state"], "not_applicable")

    def test_parked_battery_waits_thirty_seconds_after_the_last_drive(self):
        rule = rule_by_key("battery_voltage_low_parked")

        def values(second):
            if second <= 20:
                return {"engine.rpm": 800, "vehicle.speed": 0, "battery.voltage": 14.0}
            return {"engine.rpm": 0, "vehicle.speed": 0, "battery.voltage": 11.6}

        historian, evaluator, results = self.run_sequence(rule, range(0, 65, 5), values)
        by_second = dict(zip(range(0, 65, 5), results))
        self.assertEqual(by_second[40]["state"], "not_applicable")  # 20 s after last activity
        self.assertEqual(by_second[50]["state"], "normal")          # 30 s: first counted
        self.assertEqual(by_second[50]["candidate"]["observed"], 1)
        warning = by_second[60]                                     # 40 s: third counted
        self.assertEqual(warning["state"], "warning")
        self.assertEqual(warning["severity"], "warning")
        self.assertTrue(warning["regime"].startswith("engine_off"))
        self.assertEqual(warning["action"], "Start the engine or charge the battery soon.")

        def unknown(second):
            if second <= 20:
                return {"engine.rpm": 800, "vehicle.speed": 0, "battery.voltage": 14.0}
            return {"battery.voltage": 11.4}

        _h, _e, results = self.run_sequence(rule, range(0, 65, 5), unknown)
        self.assertTrue(results[-1]["regime"].startswith("engine_unknown"))
        self.assertEqual((results[-1]["state"], results[-1]["severity"]), ("warning", "critical"))
        # Clear margin: 11.9 V is above 11.8 but not by 0.2 V.
        historian.record_advisory_assessments([warning], evaluated_at=self.start + timedelta(seconds=60))
        _h, _e, held = self.run_sequence(
            rule, range(65, 80, 5), {"engine.rpm": 0, "battery.voltage": 11.9},
            historian=historian, evaluator=evaluator)
        self.assertEqual(held[-1]["state"], "warning")
        _h, _e, cleared = self.run_sequence(
            rule, range(80, 95, 5), {"engine.rpm": 0, "battery.voltage": 12.1},
            historian=historian, evaluator=evaluator)
        self.assertEqual(cleared[-1]["state"], "normal")

    def test_single_crank_or_key_on_dip_never_warns(self):
        """Owner rule: one low reading while cranking must never alert.

        Replays the archived shapes at the live 6/6/8 s cadence: a settled
        parked burst, one 11.2 V key-on/crank sample 6 s before the first
        running sample (episodes 534/599/639), recovery through 11.5 V to
        14.2 V; and one 11.8 V sample inside a 12.0-12.1 V burst (594).
        """

        rules = (
            rule_by_key("battery_voltage_low_parked"),
            rule_by_key("battery_voltage_charging_failure"),
        )
        for dip in (11.2, 11.8):
            with self.subTest(dip=dip):
                historian = self.open(idle=600)
                evaluator = EarlyWarningEvaluator(historian, rules)
                steps, second = [], 0
                while second <= 900:
                    steps.append(second)
                    second += 8 if len(steps) % 3 == 0 else 6
                dip_at = next(s for s in steps if s >= 672)
                seen = []
                for second in steps:
                    if second < 60:
                        values = {"engine.rpm": 800, "vehicle.speed": 0, "battery.voltage": 14.0,
                                  "generator.field_duty": 40.0}
                    elif second < dip_at:
                        values = {"engine.rpm": 0, "vehicle.speed": 0,
                                  "battery.voltage": 12.4 if dip == 11.2 else 12.05}
                    elif second == dip_at:
                        values = {"engine.rpm": 0, "vehicle.speed": 0, "battery.voltage": dip}
                    elif second == steps[steps.index(dip_at) + 1]:
                        values = {"engine.rpm": 800, "vehicle.speed": 0, "battery.voltage": 11.5,
                                  "generator.field_duty": 100.0}
                    else:
                        values = {"engine.rpm": 1500, "vehicle.speed": 30, "battery.voltage": 14.2,
                                  "generator.field_duty": 40.0}
                    at = self.start + timedelta(seconds=second)
                    self.ingest(historian, at, values)
                    report = evaluator.evaluate(at=at)
                    seen.extend(report["assessments"])
                    historian.record_advisory_assessments(report["assessments"], evaluated_at=at)
                self.assertEqual([a for a in seen if a["state"] == "warning"], [])
                parked = [a for a in seen if a["rule"] == "battery_voltage_low_parked"]
                self.assertTrue(any(a["state"] == "normal" for a in parked))
                self.assertLessEqual(
                    max((a.get("candidate") or {}).get("observed", 0) for a in parked), 1)
                with historian._lock:
                    opened = historian._conn.execute(
                        "SELECT COUNT(*) FROM advisory_episodes").fetchone()[0]
                    outbox = historian._conn.execute(
                        "SELECT COUNT(*) FROM advisory_notification_outbox").fetchone()[0]
                self.assertEqual((opened, outbox), (0, 0))

    def test_existing_absolute_oil_rule_uses_candidate_not_watch(self):
        rule = AbsoluteOilPressureRule()
        self.assertEqual((rule.tier, rule.group, rule.recovery_seconds), (0, "oil", 600))
        self.defs["engine.rpm"]["sources"][0].update(
            name="ccan.broadcast.0x0fc", quality="observed_alfa_scale")
        self.defs["engine.oil_pressure"]["sources"][0].update(
            name="ccan.broadcast.0x41d", quality="observed_alfa_scale")
        _h, _e, results = self.run_sequence(
            rule, range(0, 20, 5), {"engine.rpm": 800, "vehicle.speed": 0,
                                     "engine.oil_pressure": 10.0})
        self.assertEqual([item["state"] for item in results],
                         ["not_applicable", "not_applicable", "normal", "warning"])
        self.assertEqual(results[2]["candidate"]["observed"], 1)
        self.assertEqual(results[3]["evaluator_revision"], "absolute-oil-v2")
        self.assertEqual(results[3]["action"], "Stop the engine when safe.")
        self.assertEqual(results[3]["notification_rate_limit_seconds"], 300)


class CoolantRateOfRiseTests(Harness):
    def history(self, historian):
        trips = self.prior_trips(historian)
        rows = [
            (started + timedelta(minutes=6, seconds=20 * index), trip_id, IDLE, 214.0)
            for trip_id, started, _ended in trips
            for index in range(12)
        ]
        self.add_rollups(historian, "engine.coolant_temperature", rows)

    def drive(self, historian, speed, *, start=191.0, slope=0.15):
        rule = rule_by_key("engine_coolant_temperature_relative_high")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        for step in range(73):
            second = 5 * step
            rising = max(0, second - 180)
            coolant = start + slope * rising
            at = self.start + timedelta(seconds=second)
            self.ingest(historian, at, {
                "engine.rpm": 800 if speed == 0 else 2400,
                "vehicle.speed": speed,
                "engine.coolant_temperature": coolant,
            })
        return evaluator.evaluate(at=at)["assessments"][0]

    def test_normal_warm_up_through_the_floor_does_not_trigger(self):
        """191 -> 218 F in 3 min at idle: a warm-up/fan cycle, not overheating.

        Iteration 1 fired here (newest reading >= 215 F was enough); the
        strict rule needs every reading in the window at or above 215 F.
        """

        historian = self.open()
        self.history(historian)
        assessment = self.drive(historian, 0)
        self.assertEqual(assessment["current"]["value"], 218.0)
        self.assertNotEqual(assessment["state"], "warning")
        rate = assessment["rate_of_rise"]
        self.assertFalse(rate["triggered"])
        self.assertAlmostEqual(rate["slope_f_per_s"], 0.15, places=3)
        self.assertLess(rate["window_min"], 215.0)
        self.assertEqual(rate["samples"], 37)

    def test_fast_rise_entirely_above_the_floor_warns_at_idle(self):
        historian = self.open()
        self.history(historian)
        assessment = self.drive(historian, 0, start=216.0, slope=0.105)
        self.assertAlmostEqual(assessment["current"]["value"], 234.9)
        self.assertEqual(assessment["state"], "warning")
        self.assertEqual(assessment["trigger"], "rate_of_rise")
        self.assertEqual(assessment["action"], "Pull over and let it idle to cool; check the fan.")
        self.assertIn("no fan cycle", assessment["reason"])
        rate = assessment["rate_of_rise"]
        self.assertTrue(rate["triggered"])
        self.assertGreaterEqual(rate["window_min"], 215.0)
        self.assertGreaterEqual(rate["span_seconds"], 170)

    def test_fast_rise_above_the_floor_on_the_highway_does_not_trigger(self):
        historian = self.open()
        self.history(historian)
        assessment = self.drive(historian, 70, start=216.0, slope=0.105)
        self.assertFalse(assessment["rate_of_rise"]["triggered"])
        self.assertEqual(assessment["rate_of_rise"]["motions"], ["highway"])
        self.assertNotEqual(assessment.get("trigger"), "rate_of_rise")

    def test_same_rise_on_the_highway_does_not_trigger(self):
        historian = self.open()
        self.history(historian)
        assessment = self.drive(historian, 70)
        self.assertNotEqual(assessment["state"], "warning")
        self.assertFalse(assessment["rate_of_rise"]["triggered"])
        self.assertEqual(assessment["rate_of_rise"]["motions"], ["highway"])


class StubHistorian:
    def __init__(self, context, usb):
        self.context = context
        self.usb = usb

    def system_health_context(self, _snapshot_id):
        return self.context

    def usb_can_health_context(self, _snapshot_id):
        return self.usb


class SystemTierThreeTests(unittest.TestCase):
    at = datetime(2026, 9, 1, 12, tzinfo=UTC)

    @staticmethod
    def healthy():
        return {role: {"health": "healthy", "resolution": "resolved", "reason": "ready",
                       "active_gap": {"observation_count": 0}}
                for role in ("c-can", "b-can", "can-ch")}

    def evaluate(self, *, roles=None, topology_changed=False, usb=None, probe=None, inhibit=False):
        context = {
            "roles": roles or self.healthy(),
            "active_interface_gaps": {},
            "topology_changed": topology_changed,
            "topology_generation": "b",
            "previous_topology_generation": "a",
            "issues": [],
            "active_inhibits": [],
            "restoration_failed": inhibit,
        }
        if probe is not None:
            context["interface_probe"] = probe
        usb = usb if usb is not None else {"available": True, "new_removal_events": [],
                                           "active_incidents": [], "removal_event_count_24h": 0}
        report = InfrastructureHealthEvaluator(StubHistorian(context, usb)).evaluate(1, at=self.at)
        return {item["rule"]: item for item in report["assessments"]}

    def incident(self, seconds):
        return {"incident_id": "i", "state": "active", "kind": "usb_parent_hub_removed",
                "scope": "hub", "affected_serials": ["x"], "opened_at": (self.at - timedelta(seconds=seconds)).isoformat()}

    def test_topology_change_alone_is_information(self):
        items = self.evaluate(topology_changed=True)
        topology = items["usb_can_topology_generation_changed"]
        self.assertEqual((topology["state"], topology["severity"]), ("normal", "info"))
        self.assertFalse(topology["notification_eligible"])
        self.assertTrue(topology["current"]["topology_changed"])
        for item in items.values():
            self.assertEqual((item["tier"], item["group"], item["confidence"]), (3, "system", None))
            self.assertTrue(item["action"].endswith("."))

    def test_transient_disconnect_warns_only_when_long_or_repeated(self):
        short = self.evaluate(usb={"available": True, "new_removal_events": [{"event_id": "e", "affected_serials": ["x"]}],
                                   "active_incidents": [self.incident(10)], "removal_event_count_24h": 1})
        item = short["usb_can_transient_disconnect"]
        self.assertEqual((item["state"], item["severity"]), ("normal", "info"))
        self.assertEqual(item["current"]["event_ids"], ["e"])
        for usb in (
            {"available": True, "new_removal_events": [], "active_incidents": [self.incident(360)],
             "removal_event_count_24h": 1},
            {"available": True, "new_removal_events": [], "active_incidents": [],
             "removal_event_count_24h": 3},
        ):
            item = self.evaluate(usb=usb)["usb_can_transient_disconnect"]
            self.assertEqual((item["state"], item["severity"]), ("warning", "warning"))
            self.assertFalse(item["notification_eligible"])

    def test_missing_adapter_ranks_hard_after_sixty_samples_but_never_notifies(self):
        # Owner decision 2026-09-24: adapter/bus items are System notes only.
        for count, hard in ((59, False), (60, True)):
            roles = self.healthy()
            roles["b-can"] = {"health": "unhealthy", "resolution": "missing", "reason": "role_missing",
                              "active_gap": {"observation_count": count}}
            items = self.evaluate(roles=roles, topology_changed=True)
            role = items["can_interface_role_b_can"]
            self.assertEqual(role["state"], "warning")
            self.assertFalse(role["notification_eligible"])
            self.assertEqual(role["severity"], "warning" if hard else "info")
            topology = items["usb_can_topology_generation_changed"]
            self.assertEqual(topology["state"], "warning" if hard else "normal")
            self.assertFalse(topology["notification_eligible"])
        roles = self.healthy()
        roles["c-can"] = {"health": "unhealthy", "resolution": "resolved",
                          "reason": "controller_unhealthy", "active_gap": {"observation_count": 0}}
        role = self.evaluate(roles=roles)["can_interface_role_c_can"]
        self.assertEqual((role["state"], role["severity"]), ("warning", "warning"))
        self.assertFalse(role["notification_eligible"])
        self.assertEqual(role["action"], "Check the CAN adapter cable.")

    def test_probe_and_inhibit(self):
        delayed = self.evaluate(probe={"state": "awaiting_first_probe", "elapsed_seconds": 40})
        probe = delayed["can_interface_status_probe"]
        self.assertEqual((probe["state"], probe["severity"]), ("warning", "info"))
        self.assertFalse(probe["notification_eligible"])
        failed = self.evaluate(probe={"state": "failed", "elapsed_seconds": 5})
        self.assertFalse(failed["can_interface_status_probe"]["notification_eligible"])
        self.assertEqual(failed["can_interface_status_probe"]["severity"], "warning")
        inhibit = self.evaluate(inhibit=True)["can_restoration_inhibit"]
        self.assertEqual((inhibit["state"], inhibit["severity"]), ("warning", "critical"))
        self.assertFalse(inhibit["notification_eligible"])
        self.assertEqual(inhibit["action"], "Restart the telemetry service when parked.")


class CustomRuleTests(unittest.TestCase):
    def test_custom_rule_candidate_then_warning(self):
        from projects.vehicle_data.custom_warnings import evaluate_rule
        from tests.test_custom_warnings import History, RULE, STAMP
        history = History()
        row = {"id": "b" * 64, "rule": dict(RULE)}
        history.points = history.points[:1]
        first = evaluate_rule(history, row, STAMP)
        self.assertEqual(first["state"], "normal")
        self.assertEqual(first["candidate"]["observed"], 1)
        self.assertEqual(first["candidate"]["first_seen_at"], history.points[0]["observed_at"])
        self.assertEqual((first["tier"], first["group"], first["confidence"]), (1, "custom", "low"))
        history.points = History().points
        warning = evaluate_rule(history, row, STAMP)
        self.assertEqual(warning["state"], "warning")
        self.assertNotIn("candidate", warning)
        self.assertEqual(warning["confidence"], "medium")
        self.assertEqual(warning["action"], "Check it at the next stop.")


class ContractTests(Harness):
    def test_every_assessment_carries_valid_grading_fields(self):
        historian = self.open()
        custom = Path(self.tmp.name) / "custom.json"
        custom.write_text(json.dumps({"version": 1, "rules": []}))
        evaluator = EarlyWarningEvaluator(historian, custom_rules_path=custom)
        reports = []
        for index in range(8):
            at = self.start + timedelta(seconds=5 * index)
            self.ingest(historian, at, {
                "engine.rpm": 1500, "vehicle.speed": 40, "engine.coolant_temperature": 235,
                "engine.oil_pressure": 11.0, "transmission.oil_temperature": 150,
                "battery.voltage": 11.9, "generator.field_duty": 95,
                **{f"tire.pressure.{wheel}": 60.0 for wheel in WHEELS},
            })
            reports.append(evaluator.evaluate(at=at))
        at = self.start + timedelta(seconds=60)
        stopped = {"engine.rpm": 0, "battery.voltage": 11.4}
        self.ingest(historian, at, stopped)
        reports.append(evaluator.evaluate(at=at))
        payload = snapshot(at + timedelta(seconds=5), [], {}, running=False)
        payload["status"]["interface"] = role_aware_interface(roles={
            "c-can": role_status("c-can", "can0", 500000),
            "b-can": role_status("b-can", "can1", 125000),
            "can-ch": role_status("can-ch", "can2", 500000),
        })
        saved = historian.ingest_snapshot(payload, captured_at=at + timedelta(seconds=5))
        infra = InfrastructureHealthEvaluator(historian).evaluate(saved.snapshot_id, at=at + timedelta(seconds=5))
        assessments = [item for report in reports for item in report["assessments"]]
        assessments += infra["assessments"]
        self.assertTrue(any(item["state"] == "warning" and item["tier"] == 0 for item in assessments))
        rules_seen = set()
        for item in assessments:
            rules_seen.add(item["rule"])
            with self.subTest(rule=item["rule"], state=item["state"]):
                self.assertIn(item["tier"], (0, 1, 3))
                self.assertIn(item["group"], GROUPS)
                self.assertIn(item["severity"], SEVERITIES)
                self.assertIn(item["confidence"], ("high", "medium", "low", None))
                action = item["action"]
                self.assertTrue(action.endswith("."))
                self.assertLessEqual(len(action), 120)
                self.assertIsNone(FORBIDDEN.search(action))
                self.assertIsNone(FORBIDDEN.search(item["title"]))
                self.assertIsNone(re.search(r"\d", item["title"]))
                self.assertTrue(60 <= item["notification_rate_limit_seconds"] <= 86400)
                self.assertEqual(item["notification_eligible"] and item["state"] != "warning", False)
                if item["tier"] in (0, 1):
                    self.assertNotEqual(item["state"], "watch")
                    snapshot_fields = item["rule_snapshot"]
                    self.assertIn("max_age_seconds", snapshot_fields)
                    self.assertIn("recovery_seconds", snapshot_fields)
                    self.assertIn(
                        "deescalate_fraction" if item["tier"] == 1 else "clear_margin",
                        snapshot_fields,
                    )
                if "candidate" in item:
                    self.assertEqual(item["state"], "normal")
                    self.assertEqual(set(item["candidate"]), {
                        "observed", "required", "window_seconds", "first_seen_at", "confidence"})
                json.dumps(item, allow_nan=False)
        expected = {rule.key for rule in DEFAULT_EVALUATION_RULES}
        self.assertTrue(expected <= rules_seen)
        for report in reports:
            for key in ("schema_version", "generated_at", "custom_rules", "method", "active",
                        "assessments", "candidates"):
                self.assertIn(key, report)
            self.assertIn("tiers", report["method"])
            self.assertTrue(all(item["state"] == "normal" for item in report["candidates"]))
        # The historian accepts the whole tiered assessment set.
        last = reports[-2]
        historian.record_advisory_assessments(
            last["assessments"],
            evaluated_at=self.start + timedelta(seconds=35, milliseconds=1),
            authoritative_rule_keys=[item["rule"] for item in last["assessments"]],
        )


def cadence_steps(start, seconds):
    """Offsets from ``start`` to ``start + seconds`` at the live 6/6/8 s cadence."""

    offset, index = 0, 0
    while offset <= seconds:
        yield start + offset
        offset += (6, 6, 8)[index % 3]
        index += 1


class IterationTwoHarness(Harness):
    """Shared helpers for the iteration-2 regression tests (plan section 4.10)."""

    def rollups(self, historian, metric, regime, value, *, trips=None, count=12):
        trips = trips or self.prior_trips(historian)
        self.add_rollups(historian, metric, [
            (started + timedelta(minutes=5 + index), trip_id, regime, value)
            for trip_id, started, _ended in trips
            for index in range(count)
        ])
        return trips

    def step(self, historian, evaluator, offsets, values, *, record=False, stop=None):
        results = []
        for offset in offsets:
            at = self.start + timedelta(seconds=offset)
            self.ingest(historian, at, values(offset) if callable(values) else values)
            item = evaluator.evaluate(at=at)["assessments"][0]
            if record:
                historian.record_advisory_assessments([item], evaluated_at=at)
            results.append((offset, item))
            if stop is not None and stop(item):
                break
        return results

    def episode(self, historian, key):
        return next(
            (item for item in historian.list_advisory_episodes(limit=50) if item["rule"] == key),
            None,
        )


class TierOneCadenceTests(IterationTwoHarness):
    """R1: tier-1 runs of N readings fit N x 8 s windows at 6/6/8 s spacing."""

    def test_default_windows_allow_eight_seconds_per_reading(self):
        from projects.vehicle_data.early_warning import OBSERVATION_SPACING_ALLOWANCE_SECONDS as ALLOW
        for rule in DEFAULT_WARNING_RULES:
            with self.subTest(rule=rule.key):
                self.assertEqual(
                    rule.persistence_window_seconds, rule.persistence_observations * ALLOW)
        self.assertEqual(TireGroupRule().persistence_window_seconds, 480)
        self.assertEqual(rule_by_key("tire_pressure_pair_asymmetry").persistence_window_seconds, 480)

    def first_warning(self, rule, historian, normal, deviating, *, lead=30, hold=300):
        """Seconds from the first deviating reading to the first warning."""

        evaluator = EarlyWarningEvaluator(historian, (rule,))
        for offset in cadence_steps(0, lead + hold):
            at = self.start + timedelta(seconds=offset)
            self.ingest(historian, at, normal if offset < lead else deviating)
            if offset < lead:
                continue
            item = evaluator.evaluate(at=at)["assessments"][0]
            if item["state"] == "warning":
                return offset - lead, item
        return None, item

    def test_each_tier_one_rule_confirms_at_the_live_cadence(self):
        """Iteration 1 used N x 5 s windows (12/60, 24/120, 60/300): never reachable."""

        cases = {}
        historian = self.open(idle=600)
        self.rollups(historian, "engine.oil_pressure", IDLE, 30.0)
        idle = {"engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 190}
        cases["engine_oil_pressure_relative_low"] = (
            historian, {**idle, "engine.oil_pressure": 30.0},
            {**idle, "engine.oil_pressure": 20.0}, 30)
        historian = self.open(idle=600)
        self.rollups(historian, "engine.coolant_temperature", ROAD, 195.0)
        road = {"engine.rpm": 1500, "vehicle.speed": 50}
        cases["engine_coolant_temperature_relative_high"] = (
            historian, {**road, "engine.coolant_temperature": 195.0},
            {**road, "engine.coolant_temperature": 215.0}, 300)
        historian = self.open(idle=600)
        self.rollups(historian, "transmission.oil_temperature", ROAD, 150.0)
        cases["transmission_oil_temperature_relative_high"] = (
            historian, {**road, "engine.coolant_temperature": 190, "transmission.oil_temperature": 150.0},
            {**road, "engine.coolant_temperature": 190, "transmission.oil_temperature": 170.0}, 30)
        historian = self.open(idle=600)
        trips = self.prior_trips(historian)
        self.rollups(historian, "battery.voltage", IDLE, 14.2, trips=trips)
        self.rollups(historian, "generator.field_duty", IDLE, 92.0, trips=trips)
        cases["battery_voltage_relative_low"] = (
            historian, {**idle, "battery.voltage": 14.2, "generator.field_duty": 95.0},
            {**idle, "battery.voltage": 13.2, "generator.field_duty": 95.0}, 30)
        for key, (historian, normal, deviating, lead) in cases.items():
            with self.subTest(rule=key):
                rule = rule_by_key(key)
                elapsed, item = self.first_warning(rule, historian, normal, deviating, lead=lead)
                self.assertIsNotNone(elapsed, item["reason"])
                self.assertLessEqual(elapsed, rule.persistence_observations * 8 + 10)
                self.assertEqual(item["persistence"]["observed"], rule.persistence_observations)

    def test_combined_tire_rule_confirms_at_the_live_cadence(self):
        historian = self.open(idle=900)
        TireRuleTests.tire_history(self, historian)
        base = {"engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 150}
        elapsed, item = self.first_warning(
            TireGroupRule(), historian,
            {**base, "tire.pressure.rr": 74.0, "tire.pressure.rl": 75.0},
            {**base, "tire.pressure.rr": 70.0, "tire.pressure.rl": 75.0},
            lead=30, hold=520,
        )
        self.assertIsNotNone(elapsed, item["reason"])
        self.assertLessEqual(elapsed, 60 * 8 + 10)
        self.assertEqual((item["phase"], item["held_keys"]), ("cold", ["rr"]))


class RunningGapTests(IterationTwoHarness):
    """R2: the running gate tolerates the live 10-14 s RPM gaps, not a stop."""

    hot = {"engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 235.0}

    def confirmed(self, value=235.0, *, record=False):
        historian = self.open(idle=600)
        rule = rule_by_key("engine_coolant_temperature_hot")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        results = self.step(historian, evaluator, range(0, 40, 5),
                           {**self.hot, "engine.coolant_temperature": value}, record=record)
        self.assertEqual(results[-1][1]["state"], "warning")
        return historian, evaluator, results[-1][1]

    def test_a_fourteen_second_rpm_gap_keeps_the_gate(self):
        historian, evaluator, _warning = self.confirmed()
        # One missing observation: 35 -> 49 s (iteration 1: not_applicable).
        (_offset, item), = self.step(historian, evaluator, (49,), self.hot)
        self.assertEqual(item["state"], "warning")
        self.assertTrue(item["running_evidence"]["qualified"])

    def test_a_twenty_five_second_gap_restarts_the_grace(self):
        historian, evaluator, _warning = self.confirmed()
        (_offset, item), = self.step(historian, evaluator, (60,), self.hot)
        self.assertEqual(item["state"], "not_applicable")

    def test_oil_rule_honours_its_own_gap_field(self):
        self.defs["engine.rpm"]["sources"][0].update(
            name="ccan.broadcast.0x0fc", quality="observed_alfa_scale")
        self.defs["engine.oil_pressure"]["sources"][0].update(
            name="ccan.broadcast.0x41d", quality="observed_alfa_scale")
        values = {"engine.rpm": 800, "vehicle.speed": 0, "engine.oil_pressure": 10.0}
        self.assertEqual(AbsoluteOilPressureRule().running_max_gap_seconds, 20.0)
        states = {}
        for gap in (20.0, 30.0):
            historian = self.open(idle=600)
            evaluator = EarlyWarningEvaluator(
                historian, (AbsoluteOilPressureRule(running_max_gap_seconds=gap),))
            results = self.step(historian, evaluator, (0, 5, 10, 15, 40), values)
            self.assertEqual(results[3][1]["state"], "warning")
            states[gap] = results[-1][1]
        self.assertEqual(states[20.0]["state"], "not_applicable")
        self.assertNotEqual(states[30.0]["state"], "not_applicable")
        self.assertTrue(states[30.0]["running_evidence"]["qualified"])


class StickyCriticalTests(IterationTwoHarness):
    """R3: an open critical stays critical across inconclusive ticks."""

    def test_coolant_critical_survives_a_running_gap(self):
        historian = self.open(idle=600)
        rule = rule_by_key("engine_coolant_temperature_hot")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        hot = {"engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 241.0}
        results = self.step(historian, evaluator, range(0, 40, 5), hot, record=True)
        self.assertEqual((results[-1][1]["state"], results[-1][1]["severity"]), ("warning", "critical"))
        # A 25 s RPM gap restarts the grace: inconclusive, but still critical
        # (iteration 1 reported "warning" and recorded the downgrade).
        gap = self.step(historian, evaluator, (60, 65), hot, record=True)
        for _offset, item in gap:
            self.assertEqual((item["state"], item["severity"]), ("not_applicable", "critical"))
            self.assertEqual(item["action"], "Pull over and let it idle to cool.")
        (_offset, item), = self.step(historian, evaluator, (70,), hot, record=True)
        self.assertEqual((item["state"], item["severity"]), ("warning", "critical"))
        self.assertEqual(self.episode(historian, rule.key)["latest_assessment"]["severity"], "critical")

    def test_tire_critical_survives_an_unavailable_tick(self):
        historian = self.open(idle=600)
        rule = rule_by_key("tire_pressure_low_absolute")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        base = {"engine.rpm": 800, "vehicle.speed": 0}
        results = self.step(historian, evaluator, (0, 5, 10),
                           {**base, "tire.pressure.rr": 58.0}, record=True)
        self.assertEqual((results[-1][1]["state"], results[-1][1]["severity"]), ("warning", "critical"))
        gap = self.step(historian, evaluator, range(15, 60, 5), base, record=True)
        item = gap[-1][1]
        self.assertEqual((item["state"], item["severity"]), ("unavailable", "critical"))
        self.assertEqual(item["held_keys"], ["rr"])
        self.assertEqual(item["metric"], "tire.pressure.rr")


class CoolantFloorTests(IterationTwoHarness):
    """D5: slow-motion coolant readings count only from 222 F."""

    def hold(self, regime, baseline, motion, value, *, lead_value):
        historian = self.open(idle=900)
        self.rollups(historian, "engine.coolant_temperature", regime, baseline)
        evaluator = EarlyWarningEvaluator(
            historian, (rule_by_key("engine_coolant_temperature_relative_high"),))
        offsets = list(cadence_steps(0, 300 + 24 * 8))
        for offset in offsets:
            at = self.start + timedelta(seconds=offset)
            self.ingest(historian, at, {**motion, "engine.coolant_temperature":
                                        lead_value if offset < 300 else value})
        return evaluator.evaluate(at=at)["assessments"][0]

    def test_idle_fan_cycle_band_does_not_count(self):
        self.assertEqual(
            rule_by_key("engine_coolant_temperature_relative_high").slow_motion_floor, 222.0)
        idle = {"engine.rpm": 800, "vehicle.speed": 0}
        item = self.hold(IDLE, 190.0, idle, 212.0, lead_value=190.0)
        # 22 F over a 190 F median (threshold 13.5) but inside the fan cycle.
        self.assertGreater(item["deviation"]["effect_in_rule_direction"], item["deviation"]["threshold"])
        self.assertEqual(item["state"], "normal")
        self.assertNotIn("candidate", item)
        self.assertEqual(item["persistence"]["observed"], 0)
        self.assertEqual(item["floor"], {"value": 222.0, "applied": True, "motion": "stationary"})
        hot = self.hold(IDLE, 190.0, idle, 224.0, lead_value=190.0)
        self.assertEqual(hot["state"], "warning")
        self.assertEqual(hot["trigger"], "deviation")

    def test_road_keeps_the_plain_comparison(self):
        item = self.hold(ROAD, 195.0, {"engine.rpm": 1500, "vehicle.speed": 50}, 212.0,
                         lead_value=195.0)
        self.assertEqual(item["state"], "warning")
        self.assertFalse(item["floor"]["applied"])


class HeldWheelRecoveryTests(IterationTwoHarness):
    """P6: open tire events keep describing the opening wheel until recovered."""

    def recover(self, historian, evaluator, key, values, start):
        """Normal ticks at 5 s until the episode resolves (at most 700 s)."""

        results = self.step(
            historian, evaluator, range(start, start + 700, 5), values, record=True,
            stop=lambda _item: self.episode(historian, key)["status"] != "open")
        return results

    def check(self, historian, evaluator, rule, low, back, *, wheel, others, back_at=90):
        key = rule.key
        opened = self.step(historian, evaluator, (0, 5, 10), {**others, **low}, record=True)
        warning = opened[-1][1]
        self.assertEqual(warning["state"], "warning")
        self.assertEqual(warning["metric"], f"tire.pressure.{wheel}")
        held = warning["held_keys"]
        # The sensor drops out, then reports its usual value again once the
        # low readings have left the rule's lookback.
        gap = self.step(historian, evaluator, range(15, back_at, 5), others, record=True)
        self.assertIn(gap[-1][1]["state"], ("unavailable", "not_applicable"))
        self.assertEqual(gap[-1][1]["held_keys"], held)
        back_results = self.step(historian, evaluator, (back_at, back_at + 5, back_at + 10),
                                 {**others, **back}, record=True)
        states = [item["state"] for _offset, item in back_results]
        self.assertEqual(states, ["recovering", "recovering", "normal"])
        for _offset, item in back_results:
            self.assertEqual(item["metric"], f"tire.pressure.{wheel}")
            self.assertEqual(item["held_keys"], held)
            self.assertEqual(item["current"]["source"], warning["current"]["source"])
        results = self.recover(historian, evaluator, key, {**others, **back}, back_at + 15)
        episode = self.episode(historian, key)
        self.assertNotEqual(episode["status"], "open", results[-1][1])
        self.assertEqual(episode["latest_assessment"]["metric"], f"tire.pressure.{wheel}")

    def test_combined_tire_rule(self):
        historian = self.open(idle=900)
        TireRuleTests.tire_history(self, historian)
        rule = dataclasses.replace(
            TireGroupRule(), persistence_observations=3, persistence_window_seconds=24.0)
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        base = {"engine.rpm": 800, "vehicle.speed": 0, "engine.coolant_temperature": 150,
                "tire.pressure.rl": 75.0}
        self.check(historian, evaluator, rule, {"tire.pressure.rr": 70.0},
                   {"tire.pressure.rr": 74.0}, wheel="rr", others=base)

    def test_absolute_tire_rule(self):
        historian = self.open(idle=900)
        rule = rule_by_key("tire_pressure_low_absolute")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        # RL (70.5) sits closer to its limit than the recovered RR (71): the
        # iteration-1 aggregation described RL once the event cleared.
        base = {"engine.rpm": 800, "vehicle.speed": 0, "tire.pressure.rl": 70.5}
        self.check(historian, evaluator, rule, {"tire.pressure.rr": 62.0},
                   {"tire.pressure.rr": 71.0}, wheel="rr", others=base, back_at=130)

    def test_pair_rule(self):
        historian = self.open(idle=900)
        trips = self.prior_trips(historian)
        self.rollups(historian, "tire.pressure.fl", ROAD, 57.7, trips=trips)
        self.rollups(historian, "tire.pressure.fr", ROAD, 56.0, trips=trips)
        rule = dataclasses.replace(
            rule_by_key("tire_pressure_pair_asymmetry"),
            persistence_observations=3, persistence_window_seconds=24.0)
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        # FL wobbles by 0.1 psi so it is never mistaken for a cached reading.
        flip = {"n": 0}

        def base(_offset=None):
            flip["n"] += 1
            return {"engine.rpm": 1500, "vehicle.speed": 50,
                    "tire.pressure.fl": 57.0 if flip["n"] % 2 else 57.1}

        def merged(extra):
            return lambda offset: {**base(offset), **extra}

        key = rule.key
        # Moving past PAIR_ROLLING_TRUST_SECONDS first, so FR counts.
        self.step(historian, evaluator, range(0, 160, 8), merged({"tire.pressure.fr": 56.0}))
        opened = self.step(historian, evaluator, (160, 165, 170),
                          merged({"tire.pressure.fr": 52.0}), record=True)
        warning = opened[-1][1]
        self.assertEqual((warning["state"], warning["metric"]), ("warning", "tire.pressure.fr"))
        gap = self.step(historian, evaluator, range(175, 250, 5), merged({}), record=True)
        self.assertEqual(gap[-1][1]["state"], "unavailable")
        self.assertEqual(gap[-1][1]["held_keys"], ["flfr"])
        # FR overshoots: FL is now the lower wheel, but the event keeps
        # describing FR, which opened it (iteration 1 switched to FL).  Eight
        # clear readings (PAIR_CLEAR_OBSERVATIONS), not the opening run, clear it.
        back = self.step(historian, evaluator, range(250, 290, 5),
                        merged({"tire.pressure.fr": 56.6}), record=True)
        self.assertEqual([item["state"] for _o, item in back], ["recovering"] * 7 + ["normal"])
        for _offset, item in back:
            self.assertEqual(item["pair"]["lower_wheel"], "fl")
            self.assertEqual(item["metric"], "tire.pressure.fr")
            self.assertEqual(item["current"]["source"], warning["current"]["source"])
            self.assertEqual(item["held_keys"], ["flfr"])
        self.recover(historian, evaluator, key, merged({"tire.pressure.fr": 56.6}), 290)
        self.assertNotEqual(self.episode(historian, key)["status"], "open")


class CachedTireReadingTests(IterationTwoHarness):
    """D9: pair asymmetry waits until both wheels report since the drive began."""

    def setup_pair(self, *, pre_trip):
        historian = self.open(idle=900)
        trips = self.prior_trips(historian)
        self.rollups(historian, "tire.pressure.fl", ROAD, 57.7, trips=trips)
        self.rollups(historian, "tire.pressure.fr", ROAD, 56.0, trips=trips)
        if pre_trip:
            # The last readings before the van was parked 5 h ago.
            parked = self.start - timedelta(hours=5)
            self.ingest(historian, parked, {"tire.pressure.fl": 57.7, "tire.pressure.fr": 54.9},
                        running=False)
        return historian

    def drive(self, historian, *, fr_after=180, minutes=10, fl=57.7):
        for offset in cadence_steps(0, minutes * 60):
            at = self.start + timedelta(seconds=offset)
            self.ingest(historian, at, {
                "engine.rpm": 1500, "vehicle.speed": 50, "tire.pressure.fl": fl,
                "tire.pressure.fr": 54.9 if offset < fr_after else 52.5,
            })
        return at

    def test_trip_34_pattern_is_not_applicable(self):
        historian = self.setup_pair(pre_trip=True)
        at = self.drive(historian)
        rule = rule_by_key("tire_pressure_pair_asymmetry")
        item = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=at)["assessments"][0]
        axle = item["axle_assessments"]["flfr"]
        self.assertEqual(axle["state"], "not_applicable")
        self.assertIn("FL tire reading has not updated", axle["reason"])
        self.assertTrue(axle["stale"]["fl"]["stale"])
        self.assertEqual(axle["stale"]["fl"]["pre_trip_value"], 57.7)
        self.assertFalse(axle["stale"]["fr"]["stale"])
        self.assertNotEqual(item["state"], "warning")
        # The same readings without the cached-reading gate would warn: the
        # FR drop is 57.7 - 52.5 - 1.7 = 3.5 psi past the usual offset.
        with mock.patch.object(EarlyWarningEvaluator, "_wheel_stale",
                               return_value={"stale": False}):
            ungated = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=at)["assessments"][0]
        self.assertEqual(ungated["state"], "warning")
        # Once FL reports a new value, the axle is compared again.
        later = at + timedelta(seconds=6)
        self.ingest(historian, later, {"engine.rpm": 1500, "vehicle.speed": 50,
                                       "tire.pressure.fl": 56.0, "tire.pressure.fr": 52.5})
        item = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=later)["assessments"][0]
        axle = item["axle_assessments"]["flfr"]
        self.assertEqual(axle["state"], "normal")
        self.assertFalse(axle["stale"]["fl"]["stale"])

    def test_retention_fallback_needs_five_minutes_of_one_value(self):
        historian = self.setup_pair(pre_trip=False)
        rule = rule_by_key("tire_pressure_pair_asymmetry")
        at = self.drive(historian, minutes=4)
        axle = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=at)[
            "assessments"][0]["axle_assessments"]["flfr"]
        self.assertIsNone(axle["stale"]["fl"]["pre_trip_value"])
        self.assertFalse(axle["stale"]["fl"]["stale"])  # under 300 s
        historian = self.setup_pair(pre_trip=False)
        at = self.drive(historian, minutes=6)
        axle = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=at)[
            "assessments"][0]["axle_assessments"]["flfr"]
        self.assertEqual(axle["state"], "not_applicable")
        self.assertTrue(axle["stale"]["fl"]["stale"])
        self.assertGreaterEqual(axle["stale"]["fl"]["span_seconds"], 300)


URBAN = "engine_running:urban:rpm_low:warm"
# Trip 59 (2026-09-24), seconds from 23:44:17Z when the van pulled away after
# a ~23 min idle: (offset, RL psi, RR psi, mph).  Values from the historian
# (RF hub DIDs 31D3/31D2); a row holds until the next.  The rows before -608
# are condensed: urban driving, then the idle from 23:15Z; RL reported its
# cooling tire at 23:28:35Z (76.4) and 23:32:09Z (76.0) while RR repeated 79.6.
TRIP_59 = (
    (-2000, 79.6, 79.6, 25), (-1755, 79.6, 79.6, 0), (-942, 76.4, 79.6, 0),
    (-728, 76.0, 79.6, 0), (1, 76.0, 79.6, 12), (8, 76.0, 79.6, 25), (14, 76.0, 74.8, 35),
    (22, 76.0, 74.8, 44), (40, 76.0, 74.8, 53), (59, 76.0, 74.8, 59), (65, 76.0, 75.6, 59),
    (79, 75.2, 75.6, 59), (110, 75.2, 76.0, 59), (129, 76.0, 76.0, 59), (135, 76.0, 76.4, 59),
    (166, 76.4, 76.4, 60), (179, 76.4, 76.8, 61), (213, 76.8, 76.8, 61), (219, 76.8, 77.2, 61),
    (253, 77.2, 77.2, 61), (294, 77.2, 77.6, 61), (300, 77.6, 77.6, 62), (313, 77.6, 78.0, 62),
    (348, 78.0, 78.0, 62), (360, 78.0, 78.4, 62), (422, 78.4, 78.4, 62), (467, 78.4, 78.8, 62),
    (537, 78.4, 79.2, 62), (556, 78.8, 79.2, 62), (574, 78.8, 79.2, 57), (600, 78.8, 79.2, 52),
    (606, 78.8, 79.2, 43), (612, 78.8, 79.2, 35), (618, 78.8, 79.2, 29), (631, 79.2, 79.2, 20),
    (667, 79.2, 79.2, 15), (698, 78.8, 79.2, 26), (711, 78.8, 79.6, 27), (717, 78.8, 79.6, 35),
    (728, 78.8, 79.6, 47), (734, 78.8, 79.6, 53), (740, 78.8, 79.6, 60), (778, 78.8, 79.2, 62),
    (901, 79.2, 79.2, 62), (1043, 79.2, 79.6, 62), (1096, 79.6, 79.6, 62),
)


class PairAsymmetryIncidentTests(IterationTwoHarness):
    """Episode 642: a stationary, stale-paired asymmetry must not warn."""

    # The live readings arrive slightly apart: speed, then RL, then RR.
    LAG = {"vehicle.speed": 3.0, "tire.pressure.rl": 2.0, "tire.pressure.rr": 1.0}

    def history(self, *, pre_rl=80.4, pre_rr=79.6):
        historian = self.open(idle=900)
        trips = self.prior_trips(historian)
        # Usual RL - RR offset +0.78 psi in every motion band.
        for regime in (IDLE, URBAN, ROAD):
            self.rollups(historian, "tire.pressure.rl", regime, 79.18, trips=trips)
            self.rollups(historian, "tire.pressure.rr", regime, 78.4, trips=trips)
        # The last readings before the trip (real values, 2026-09-24 21:54Z).
        self.ingest(historian, self.start - timedelta(hours=5),
                    {"tire.pressure.rl": pre_rl, "tire.pressure.rr": pre_rr}, running=False)
        return historian

    def put(self, historian, offset, rl, rr, mph):
        at = self.start + timedelta(seconds=offset)
        values = {
            "engine.rpm": 800 if mph < 5 else 1500, "vehicle.speed": mph,
            "engine.coolant_temperature": 195, "tire.pressure.rl": rl, "tire.pressure.rr": rr,
        }
        metrics = {
            name: (
                available(defn, values[name], at - timedelta(seconds=self.LAG.get(name, 0.0)))
                if values.get(name) is not None else unavailable(defn)
            )
            for name, defn in self.defs.items()
        }
        historian.ingest_snapshot(
            snapshot(at, list(self.defs.values()), metrics, running=True), captured_at=at)
        return at

    def replay(self, historian, evaluator, rows, *, until, evaluate_from, step=8):
        """Ingest the rows at an 8 s cadence; evaluate and record from ``evaluate_from``."""

        results = []
        offset = rows[0][0]
        while offset <= until:
            row = [item for item in rows if item[0] <= offset][-1]
            at = self.put(historian, offset, *row[1:])
            if offset >= evaluate_from:
                item = evaluator.evaluate(at=at)["assessments"][0]
                historian.record_advisory_assessments([item], evaluated_at=at)
                results.append((offset, item))
            offset += step
        return results

    def test_trip_59_idle_and_pull_away_never_warns(self):
        historian = self.history()
        rule = rule_by_key("tire_pressure_pair_asymmetry")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        results = self.replay(historian, evaluator, TRIP_59, until=1100, evaluate_from=-900)
        self.assertEqual([o for o, item in results if item["state"] == "warning"], [])
        self.assertIsNone(self.episode(historian, rule.key))
        by_offset = dict(results)
        # During the idle RR still repeats its pre-trip 79.6, so the older
        # cached-reading gate already answers (the stop gate is checked in
        # the ungated test below).
        stopped = by_offset[-600]["axle_assessments"]["rlrr"]
        self.assertEqual(stopped["state"], "not_applicable")
        # 23:44:31Z (+14 s, the incident's opening tick): RR has re-sent,
        # RL still shows the value it had when the van pulled away.
        opening = by_offset[16]["axle_assessments"]["rlrr"]
        self.assertEqual(opening["state"], "not_applicable")
        self.assertIn("RL tire has not reported", opening["reason"])
        self.assertEqual(opening["motion"]["fresh"], {"rl": False, "rr": True})
        # Once RL re-sends (+79 s) the axle is compared again, and is normal.
        later = by_offset[96]
        self.assertEqual(later["axle_assessments"]["rlrr"]["state"], "normal")
        self.assertLess(abs(later["pair"]["asymmetry"]), 2.0)  # 75.2 - 75.6 - 0.78

    def test_without_the_motion_gates_the_idle_opens_a_warning(self):
        """The same idle, ungated, reproduces the false warning."""

        # A pre-trip RR different from 79.6, so the trip-start cached-reading
        # gate does not hide the repeated RR value either.
        historian = self.history(pre_rr=78.0)
        rule = rule_by_key("tire_pressure_pair_asymmetry")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        always = lambda _self, _tick, _anchor: (lambda _us: {"speed": 60.0, "segment_start_us": 0})
        with mock.patch.object(EarlyWarningEvaluator, "_pair_motion", always):
            results = self.replay(historian, evaluator, TRIP_59[:4], until=-120, evaluate_from=-200)
        self.assertEqual(results[-1][1]["state"], "warning")
        self.assertAlmostEqual(results[-1][1]["pair"]["asymmetry"], -4.38, places=2)
        # Gated, the same readings stay quiet.
        historian = self.history(pre_rr=78.0)
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        results = self.replay(historian, evaluator, TRIP_59[:4], until=-120, evaluate_from=-200)
        self.assertEqual({item["state"] for _o, item in results}, {"not_applicable"})
        axle = results[-1][1]["axle_assessments"]["rlrr"]
        self.assertIn("stopped", axle["reason"])
        self.assertFalse(axle["motion"]["moving"])

    def test_opening_needs_the_latest_pair_past_the_limit(self):
        """Iteration 2 opened when the run's newest reading paired RL with RR's
        previous value while the two latest readings were 0.4 psi apart."""

        historian = self.history()
        rule = dataclasses.replace(rule_by_key("tire_pressure_pair_asymmetry"),
                                   persistence_observations=3, persistence_window_seconds=24.0)
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        for offset in range(0, 160, 8):
            self.put(historian, offset, 79.2, 78.4, 60)
        self.put(historian, 160, 79.2, 74.8, 60)
        self.put(historian, 168, 79.2, 74.8, 60)
        at = self.put(historian, 176, 79.2, 78.0, 60)  # RR back, sent after RL
        item = evaluator.evaluate(at=at)["assessments"][0]
        self.assertEqual(item["state"], "normal")
        self.assertAlmostEqual(item["pair"]["asymmetry"], 0.42, places=2)
        # The run itself was past the limit on all three readings.
        self.assertEqual(item["axle_assessments"]["rlrr"]["observed"], 0)

    def test_stop_holds_an_open_warning_and_fresh_readings_clear_it_quickly(self):
        historian = self.history()
        rule = rule_by_key("tire_pressure_pair_asymmetry")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        # Moving; RR slowly loses pressure until it is 4 psi below its usual
        # place (a real asymmetric leak), then holds there.
        rows = [(0, 79.2, 78.4, 60)] + [
            (200 + 64 * step, 79.2, round(78.4 - 0.4 * (step + 1), 1), 60) for step in range(10)
        ]
        results = self.replay(historian, evaluator, rows, until=1500, evaluate_from=1200)
        self.assertEqual(results[-1][1]["state"], "warning")
        self.assertEqual(results[-1][1]["title"], "Rear tires uneven")
        episode = self.episode(historian, rule.key)
        self.assertEqual(episode["status"], "open")
        # A 3 min stop: not judged, and the event stays open.
        stop = self.replay(historian, evaluator, [(1508, 79.2, 74.4, 0)], until=1690, evaluate_from=1508)
        self.assertEqual({item["state"] for _o, item in stop}, {"not_applicable"})
        self.assertEqual(self.episode(historian, rule.key)["id"], episode["id"])
        self.assertEqual(self.episode(historian, rule.key)["status"], "open")
        # Pulling away, then the tire is refilled on the road (the RR sensor
        # re-sends 78.4 at +30 s): eight fresh readings clear it, and the
        # 120 s recovery hold closes the event about three minutes later.
        drive = [(1698, 79.2, 74.4, 60), (1728, 79.2, 78.4, 60), (1760, 79.0, 78.4, 60)]
        closed = self.replay(historian, evaluator, drive, until=2100, evaluate_from=1698,
                             step=8)
        resolved_at = next(
            (offset for offset, _item in closed
             if self.episode(historian, rule.key)["status"] != "open"
             and self._resolved_before(historian, rule.key, offset)),
            None,
        )
        episode = self.episode(historian, rule.key)
        self.assertEqual(episode["status"], "resolved")
        resolved = datetime.fromisoformat(episode["resolved_at"])
        self.assertLessEqual((resolved - (self.start + timedelta(seconds=1760))).total_seconds(), 200)
        self.assertIsNotNone(resolved_at)

    def _resolved_before(self, historian, key, offset):
        episode = self.episode(historian, key)
        resolved = episode.get("resolved_at")
        return bool(resolved) and datetime.fromisoformat(resolved) <= self.start + timedelta(seconds=offset)

    def test_slow_asymmetric_leak_on_a_moving_van_still_warns(self):
        historian = self.history()
        rule = rule_by_key("tire_pressure_pair_asymmetry")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        # RR loses 0.4 psi per sensor transmission (every 64 s) at 62 mph.
        rows = [(0, 79.2, 78.4, 62)] + [
            (64 * (step + 1), 79.2, round(78.4 - 0.4 * (step + 1), 1), 62) for step in range(20)
        ]
        crossed = next(offset for offset, rl, rr, _mph in rows if rl - rr - 0.78 > 3.0)
        results = self.replay(historian, evaluator, rows, until=crossed + 60 * 8 + 80,
                              evaluate_from=crossed + 400)
        first = next((offset for offset, item in results if item["state"] == "warning"), None)
        self.assertIsNotNone(first, results[-1][1]["reason"])
        self.assertLessEqual(first - crossed, 60 * 8 + 16)
        self.assertEqual(results[-1][1]["pair"]["lower_wheel"], "rr")


class DeterminismTests(Harness):
    def test_phase_cache_and_candidates_follow_the_evaluation_time(self):
        historian = self.open()
        evaluator = EarlyWarningEvaluator(historian, (rule_by_key("tire_pressure_low_absolute"),))
        at = self.start
        self.ingest(historian, at, {"engine.rpm": 800, "vehicle.speed": 0, "tire.pressure.rr": 66.0})
        with mock.patch("time.monotonic", side_effect=AssertionError("wall clock")), \
                mock.patch("time.time", side_effect=AssertionError("wall clock")):
            evaluator.evaluate(at=at)
        self.assertEqual(evaluator._phase_cache[0], us(at))
        self.assertTrue(math.isfinite(evaluator._candidates["tire_pressure_low_absolute"]["last_seen_us"]))


if __name__ == "__main__":
    unittest.main()


def test_pair_summary_leads_with_the_lower_wheel_against_its_mate():
    """Card sentence for pair rules (replaced "RR tire 75 psi. Check both rear tires...")."""
    from projects.vehicle_data.early_warning import _pair_summary

    base = {"left": "rl", "right": "rr", "left_value": 76.0, "right_value": 79.6, "offset": 0.78, "lower": "rl"}
    assert _pair_summary(base) == "RL 76.0 psi is 3.6 under RR 79.6 (usually 0.8 over)"
    rr_low = {**base, "left_value": 79.6, "right_value": 75.2, "lower": "rr"}
    assert _pair_summary(rr_low) == "RR 75.2 psi is 4.4 under RL 79.6 (usually 0.8 under)"
    assert _pair_summary({**base, "offset": 0.01}).endswith("(usually even)")
    assert _pair_summary({**base, "offset": None}) == "RL 76.0 psi is 3.6 under RR 79.6"
    assert _pair_summary({**base, "left_value": None}) is None
