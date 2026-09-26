"""Lifecycle regressions in the tiered warning path (offline only).

Each test pins a path where an episode could stay open indefinitely, show a
confirmed warning at a normal value, close while the condition still held, or
lose an undelivered critical push.  Temporary historians only; no CAN access.
"""

from __future__ import annotations

from datetime import timedelta
import unittest

from projects.vehicle_data.custom_warnings import evaluate_rule as custom_evaluate
from projects.vehicle_data.early_warning import AbsoluteOilPressureRule, EarlyWarningEvaluator
from tests.test_custom_warnings import RULE, STAMP, History, sample
from tests import test_warning_delivery as delivery
from tests import test_warning_rules_v3 as rules_v3

DAY = delivery.DAY
tiered = delivery.tiered
rule_by_key = rules_v3.rule_by_key


class TimedRecoverySurvivesInconclusiveTicks(delivery.TieredDeliveryBase):
    def normal(self, at):
        return tiered("normal", at=at,
                      deviation={"effect_in_rule_direction": 1.0, "threshold": 5.0})

    def test_one_stale_evaluation_does_not_restart_the_clock(self):
        self.record(tiered("warning", at=DAY), at=DAY)
        start = DAY + timedelta(seconds=10)
        self.hold_normal(self.normal, start, seconds=300)
        blip = start + timedelta(seconds=305)
        stale = tiered("unavailable", at=blip)
        stale["current"]["observed_at"] = (start + timedelta(seconds=300)).isoformat()
        self.record(stale, at=blip)
        episode = self.episode("tire_pressure_relative_low")
        self.assertEqual(episode["status"], "open")
        self.hold_normal(self.normal, start + timedelta(seconds=310), seconds=280)
        self.assertEqual(self.episode("tire_pressure_relative_low")["status"], "open")
        final = start + timedelta(seconds=600)
        self.record(self.normal(final), at=final)
        self.assertEqual(self.episode("tire_pressure_relative_low")["status"], "resolved")

    def test_a_warning_between_normal_readings_still_restarts_it(self):
        self.record(tiered("warning", at=DAY), at=DAY)
        start = DAY + timedelta(seconds=10)
        self.hold_normal(self.normal, start, seconds=300)
        again = start + timedelta(seconds=305)
        self.record(tiered("warning", at=again), at=again)
        self.hold_normal(self.normal, start + timedelta(seconds=310), seconds=300)
        self.assertEqual(self.episode("tire_pressure_relative_low")["status"], "open")


class CustomWarningHoldsWhileOutside(delivery.TieredDeliveryBase):
    def test_short_run_after_a_regime_change_keeps_the_event_open(self):
        history = History()
        history.list_advisory_episodes = self.historian.list_advisory_episodes
        row = {"id": "a" * 64, "rule": dict(RULE)}
        warning = custom_evaluate(history, row, STAMP)
        self.assertEqual(warning["state"], "warning")
        self.record(warning, at=STAMP)
        history.latest = sample(seconds=-5, regime="running:urban:low:warm")
        history.points = [history.latest]
        later = STAMP + timedelta(seconds=5)
        held = custom_evaluate(history, row, later)
        self.assertEqual(held["state"], "warning")
        self.record(held, at=later)
        self.assertEqual(self.episode(warning["rule"])["status"], "open")
        self.assertEqual(len(self.rows()), 1)  # no second push for the same event
        inside = STAMP + timedelta(seconds=10)
        history.latest = sample(value=12.4, seconds=-10)
        self.record(custom_evaluate(history, row, inside), at=inside)
        self.assertEqual(self.episode(warning["rule"])["status"], "resolved")

    def test_without_an_open_event_a_short_run_is_only_a_candidate(self):
        history = History()
        history.list_advisory_episodes = self.historian.list_advisory_episodes
        history.points = history.points[:1]
        result = custom_evaluate(history, {"id": "b" * 64, "rule": dict(RULE)}, STAMP)
        self.assertEqual(result["state"], "normal")
        self.assertIn("candidate", result)


class ParkedBatteryClosesDespiteDailyWakes(rules_v3.Harness):
    def step(self, historian, evaluator, second, values=None):
        at = self.start + timedelta(seconds=second)
        if values is not None:
            self.ingest(historian, at, values)
        item = evaluator.evaluate(at=at)["assessments"][0]
        historian.record_advisory_assessments([item], evaluated_at=at)
        return item

    def test_wakes_show_recovering_and_the_event_closes_after_24_hours(self):
        historian = self.open()
        evaluator = EarlyWarningEvaluator(historian, (rule_by_key("battery_voltage_low_parked"),))
        for second in range(0, 25, 5):
            self.step(historian, evaluator, second,
                      {"engine.rpm": 800, "vehicle.speed": 0, "battery.voltage": 14.0})
        for second in range(25, 65, 5):
            last = self.step(historian, evaluator, second,
                             {"engine.rpm": 0, "vehicle.speed": 0, "battery.voltage": 11.6})
        self.assertEqual(last["state"], "warning")
        hour = 3600
        for h in range(1, 20):
            self.step(historian, evaluator, 60 + h * hour)
        wake = 60 + 20 * hour
        burst = [self.step(historian, evaluator, wake + s,
                           {"engine.rpm": 0, "vehicle.speed": 0, "battery.voltage": 12.6})
                 for s in range(0, 95, 5)]
        self.assertEqual([item["state"] for item in burst[:2]], ["recovering", "recovering"])
        self.assertFalse(any(item["state"] == "warning" for item in burst))
        for h in range(21, 26):
            self.step(historian, evaluator, 60 + h * hour)
        episode = historian._conn.execute(
            "SELECT status,resolution_reason FROM advisory_episodes").fetchone()
        self.assertEqual((episode["status"], episode["resolution_reason"]),
                         ("resolved", "not_re_observed"))


class OilCriticalSurvivesEngineStop(rules_v3.Harness):
    def step(self, historian, evaluator, second, values):
        at = self.start + timedelta(seconds=second)
        self.ingest(historian, at, values)
        item = evaluator.evaluate(at=at)["assessments"][0]
        result = historian.record_advisory_assessments([item], evaluated_at=at)
        return item, result

    def test_stop_keeps_the_event_and_restart_does_not_repeat_the_push(self):
        self.defs["engine.rpm"]["sources"][0].update(
            name="ccan.broadcast.0x0fc", quality="observed_alfa_scale")
        self.defs["engine.oil_pressure"]["sources"][0].update(
            name="ccan.broadcast.0x41d", quality="observed_alfa_scale")
        historian = self.open()
        evaluator = EarlyWarningEvaluator(historian, (AbsoluteOilPressureRule(),))
        for second in range(0, 20, 5):
            warning, _ = self.step(historian, evaluator, second,
                                   {"engine.rpm": 800, "vehicle.speed": 0,
                                    "engine.oil_pressure": 10.0})
        self.assertEqual((warning["state"], warning["severity"]), ("warning", "critical"))
        rows = historian._conn.execute(
            "SELECT id,status FROM advisory_notification_outbox").fetchall()
        self.assertEqual([row["status"] for row in rows], ["pending"])
        stopped, _ = self.step(historian, evaluator, 20,
                               {"engine.rpm": 0, "vehicle.speed": 0})
        self.assertEqual(stopped["state"], "not_applicable")
        self.assertEqual(len(historian.list_advisory_episodes(active_only=True)), 1)
        self.assertEqual(historian._conn.execute(
            "SELECT status FROM advisory_notification_outbox").fetchone()["status"], "pending")
        historian.mark_advisory_notification_delivered(
            int(rows[0]["id"]), delivered_at=self.start + timedelta(seconds=25))
        restart = 3600
        states = []
        for offset in range(0, 45, 5):
            item, result = self.step(historian, evaluator, restart + offset,
                                     {"engine.rpm": 800, "vehicle.speed": 0,
                                      "engine.oil_pressure": 30.0})
            states.append(item["state"])
            self.assertEqual(result.notifications_enqueued, 0, states)
        self.assertNotIn("warning", states)
        self.assertIn("recovering", states)


class RelativeStickyShortSeries(rules_v3.Harness):
    # Helpers only; subclassing RelativeTierOneTests would rerun its tests.
    train_oil = rules_v3.RelativeTierOneTests.train_oil
    oil_point = rules_v3.RelativeTierOneTests.oil_point
    oil_rule = rules_v3.RelativeTierOneTests.oil_rule

    def test_new_trip_inside_the_band_is_recovering_not_warning(self):
        historian = self.open()
        definitions = self.train_oil(historian)
        evaluator = EarlyWarningEvaluator(historian, (self.oil_rule(),))
        for index in range(3):
            at = self.start + timedelta(seconds=300 + 5 * index)
            self.oil_point(historian, definitions, at, 20.0)
        warning = evaluator.evaluate(at=at)["assessments"][0]
        self.assertEqual(warning["state"], "warning")
        historian.record_advisory_assessments([warning], evaluated_at=at)
        # A later trip: the only same-conditions readings are normal.
        later = self.start + timedelta(hours=2)
        self.oil_point(historian, definitions, later, 30.0)
        first = evaluator.evaluate(at=later)["assessments"][0]
        self.assertEqual(first["state"], "recovering")
        self.assertFalse(first["notification_eligible"])
        for index in range(1, 3):
            at = later + timedelta(seconds=5 * index)
            self.oil_point(historian, definitions, at, 30.0)
        self.assertEqual(evaluator.evaluate(at=at)["assessments"][0]["state"], "normal")


class WarmTireIsNotRecovery(rules_v3.IterationTwoHarness):
    """A tire that warned cold must not "recover" because it warmed up.

    Trips 43-57 gain 3.2-4.4 psi during a drive.  Before the fix a front at
    49 psi cold read 53.5 warm, cleared the 50 + 2 psi margin, and after 10
    minutes sent a "normal" push; that push ended the group cooldown, so the
    next cold morning pushed the same low tire again (two pushes a day).
    """

    OTHERS = {"tire.pressure.fr": 55.0, "tire.pressure.rl": 75.0, "tire.pressure.rr": 75.0}

    def rows(self, historian):
        return historian._conn.execute(
            "SELECT id,status,json_extract(payload_json,'$.notification_kind') AS kind "
            "FROM advisory_notification_outbox ORDER BY id").fetchall()

    def test_warm_readings_do_not_clear_a_cold_low_tire(self):
        historian = self.open(idle=900)
        # Parked 5 h, so the new drive has a cold phase.
        self.add_trip(historian, 1, self.start - timedelta(hours=6),
                      self.start - timedelta(hours=5))
        rule = rule_by_key("tire_pressure_low_absolute")
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        cold = {"engine.rpm": 800, "vehicle.speed": 0, "tire.pressure.fl": 49.0, **self.OTHERS}
        opened = self.step(historian, evaluator, (0, 6, 12), cold, record=True)
        self.assertEqual((opened[-1][1]["state"], opened[-1][1]["phase"]), ("warning", "cold"))
        first, = self.rows(historian)
        historian.mark_advisory_notification_delivered(
            int(first["id"]), delivered_at=self.start + timedelta(seconds=13))

        def drive(offset):
            # 3 minutes still cold after moving off, then warm (+4.5 psi).
            value = 49.4 if offset < 200 else 53.5
            return {"engine.rpm": 1500, "vehicle.speed": 40, "tire.pressure.fl": value,
                    **self.OTHERS}

        results = self.step(historian, evaluator, range(18, 1800, 6), drive, record=True)
        warm = [item for _offset, item in results if item["phase"] in ("warming", "warm")]
        self.assertTrue(warm)
        self.assertTrue(all(item["state"] == "warning" for item in warm),
                        {item["state"] for item in warm})
        episode = self.episode(historian, rule.key)
        self.assertEqual(episode["status"], "open")
        self.assertEqual([row["kind"] for row in self.rows(historian)], ["warning"])
        # A warm reading past the allowance (52 x 1.08 = 56.2 psi) still clears.
        self.assertEqual(warm[-1]["hysteresis"]["warm_clear_fraction"], 0.08)


if __name__ == "__main__":
    unittest.main()
