"""Owner custom early warnings: time-defined holds on the broker's recording clock.

Every test stores snapshots in a real historian through ``TelemetryInsights.ingest_snapshot`` and
reads the assessment its evaluation produced (``tests/custom_rule_history.py``). Contract:
``projects/vehicle_data/docs/custom-warnings.md``.
"""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data import custom_warning_contract as contract
from projects.vehicle_data.custom_warnings import advice, catalog, catalog_from, load_rules, validate_rule
from projects.vehicle_data.early_warning import EarlyWarningEvaluator
from projects.vehicle_data.metrics import METRICS
from tests.custom_rule_history import (BATTERY, BCAN, CCAN, COOLANT, FIELD_DUTY, RULE, START, CustomRuleHistory,
                                       battery, battery_rule, coolant, reading, rpm, unavailable)

class HistoryCase(unittest.TestCase):
    rules = (BATTERY,)

    def setUp(self):
        self.h = CustomRuleHistory([dict(rule) for rule in self.rules])

    def tearDown(self):
        self.h.close()

    def series(self, times, entries=lambda t: [battery()], clocks=None):
        clocks = clocks or times
        return [self.h.record(t, entries(t), clock=c) for t, c in zip(times, clocks)]


class CurrentReadingTests(HistoryCase):
    rules = (battery_rule(window_seconds=10),)

    def test_unavailable_snapshot_after_fresh_readings_is_unavailable_not_warning(self):
        results = self.series([0, 5, 10])
        self.assertEqual(results[-1]["state"], "warning")
        gap = self.h.record(15, [unavailable("battery.voltage")])
        self.assertEqual((gap["state"], gap["reason"]), ("unavailable", "No current reading"))
        after = self.h.record(20, [battery()])
        # The hold starts again; only the episode opened at 10 s keeps the card (``_open_warning``).
        self.assertEqual((after["persistence"]["held_seconds"], after["reason"]),
                         (0.0, "Still outside the owner-approved reference"))

    def test_latest_reading_older_than_its_stale_limit_is_unavailable(self):
        self.h.add(dict(COOLANT, engine_running=False))
        rule_id = self.h.rows[-1]["id"]
        old = self.h.record(0, [coolant(lag=6)], rule_id=rule_id)
        self.assertEqual(old["state"], "unavailable")
        fresh = self.h.record(5, [coolant(lag=1)], rule_id=rule_id)
        self.assertEqual(fresh["state"], "normal")
        later, _ = self.h.health(10, rule_id=rule_id)  # 1 s lag + 5 s since capture > 5 s stale limit
        self.assertEqual(later["state"], "unavailable")

    def test_stale_snapshot_between_readings_breaks_the_run(self):
        self.series([0, 5])
        stale = self.h.record(10, [battery(stale=True)])
        self.assertEqual(stale["state"], "unavailable")
        self.assertEqual(self.h.record(15, [battery()])["persistence"]["held_seconds"], 0.0)


class EngineRunningTests(HistoryCase):
    rules = (COOLANT,)

    def test_engine_running_rule_confirms_with_capture_and_observation_times_apart(self):
        times = [round(5.2 * index, 1) for index in range(5)]
        lags = [0.1, 2.0, 0.1, 2.0, 0.1]
        results = [self.h.record(t, [coolant(lag=lag), rpm(lag=0.05)]) for t, lag in zip(times, lags)]
        self.assertEqual([r["state"] for r in results], ["normal"] * 3 + ["warning"] * 2)
        self.assertEqual(results[3]["persistence"]["held_seconds"], 15.6)

    def test_engine_running_uses_only_rpm_from_the_same_snapshot(self):
        self.assertEqual(self.h.record(0, [coolant(), rpm()])["state"], "normal")
        for entries in ([coolant()], [coolant(), unavailable("engine.rpm")], [coolant(), rpm(300)]):
            with self.subTest(entries=entries):
                result = self.h.record(self.h.sequence * 2, entries)
                self.assertEqual((result["state"], result["reason"]),
                                 ("unavailable", "Fresh engine-running evidence is absent"))

    def test_no_evidence_captured_after_at_is_used(self):
        self.h.record(0, [coolant(240), rpm()])
        self.h.record(5, [coolant(200), unavailable("engine.rpm")])
        between, _ = self.h.health(3)
        self.assertEqual(between["current"]["value"], 240)
        self.assertEqual(between["current"]["captured_at"], START.isoformat())
        self.assertEqual(between["reason"], "Outside the owner-approved reference; waiting to confirm")


class RegimeTests(HistoryCase):
    rules = (dict(COOLANT, threshold=200, window_seconds=10, max_age_seconds=10, engine_running=False),)

    def test_normal_reading_in_another_regime_breaks_the_run(self):
        self.h.record(0, [coolant(240), rpm(800)])
        inside = self.h.record(5, [coolant(190), rpm(2500)])
        self.assertNotIn("candidate", inside)
        self.assertEqual(self.h.record(10, [coolant(240), rpm(800)])["persistence"]["held_seconds"], 0.0)

    def test_regime_change_without_a_normal_reading_does_not_break_the_run(self):
        results = [self.h.record(t, [coolant(240), rpm(value)]) for t, value in ((0, 800), (5, 2500), (10, 800))]
        self.assertEqual(results[-1]["state"], "warning")


class TimeContractTests(HistoryCase):
    def test_fast_recordings_never_confirm_before_the_hold_time(self):
        self.h.add(dict(FIELD_DUTY, persistence_observations=7, max_age_seconds=4))
        rule_id = self.h.rows[-1]["id"]
        results = [self.h.record(t, [reading("generator.field_duty", 99, lag=0.1)], rule_id=rule_id) for t in range(7)]
        self.assertEqual(results[-1]["state"], "normal")
        self.assertEqual(results[-1]["persistence"]["held_seconds"], 6.0)
        self.assertEqual(results[-1]["persistence"]["observed"], 7)

    def test_hold_is_measured_in_time_at_any_recording_interval(self):
        holds = contract.FORM_DEFAULTS["hold_seconds_choices"]
        patterns = {"1 s": [1.0] * 610, "5.2 s": [5.2] * 120, "8 s": [8.0] * 78,
                    "5 s then 1 s": [5.0] * 60 + [1.0] * 310}
        for name, steps in patterns.items():
            with self.subTest(pattern=name):
                history = CustomRuleHistory([battery_rule(title=f"Hold {hold}", window_seconds=hold,
                                                  max_age_seconds=min(15, hold)) for hold in holds], episodes=False)
                try:
                    confirmed = {}
                    elapsed = 0.0
                    for step in [0.0, *steps]:
                        elapsed = round(elapsed + step, 1)
                        history.record(elapsed, [battery()])
                        for row in history.rows:
                            result = history.assessment(history.evaluator.last, row["id"])
                            hold = row["rule"]["window_seconds"]
                            if result["state"] == "warning":
                                confirmed.setdefault(hold, elapsed)
                            else:
                                self.assertLess(result["persistence"]["held_seconds"], hold)
                    for hold in holds:
                        self.assertGreaterEqual(confirmed[hold], hold)
                        self.assertLess(confirmed[hold] - hold, max(steps))
                finally:
                    history.close()

    def test_gap_within_the_rule_gap_continues_and_over_it_breaks(self):
        results = self.series([0, 12, 24, 36])
        self.assertEqual([r["state"] for r in results], ["normal"] * 3 + ["warning"])
        self.assertEqual(self.h.record(56, [battery()])["persistence"]["held_seconds"], 0.0)

    def test_broker_restart_starts_the_hold_again_and_an_open_warning_holds(self):
        self.assertEqual(self.series([0, 15, 30])[-1]["state"], "warning")
        restarted = CustomRuleHistory.__new__(CustomRuleHistory)
        restarted.__dict__.update(self.h.__dict__)
        from tests.custom_rule_history import RecordingEvaluator
        fresh = RecordingEvaluator(self.h.historian, rules=(), custom_rules_path=self.h.rules_path)
        self.h.evaluator = self.h.insights.warning_evaluator = fresh
        held = self.h.record(35, [battery()])
        self.assertEqual(held["persistence"]["held_seconds"], 0.0)
        self.assertEqual((held["state"], held["reason"]), ("warning", "Still outside the owner-approved reference"))

    def test_broker_restart_without_an_open_warning_waits_again(self):
        self.series([0, 15])
        self.h.evaluator = self.h.insights.warning_evaluator = type(self.h.evaluator)(
            self.h.historian, rules=(), custom_rules_path=self.h.rules_path)
        self.assertEqual(self.h.record(30, [battery()])["state"], "normal")

    def test_trip_change_breaks_the_run(self):
        self.series([0, 5], clocks=[0, 5])
        # A +300 s wall step (the historian's trip idle timeout) inside the rule gap opens a new trip.
        result = self.h.record(310, [battery(), rpm()], clock=10)
        self.assertEqual(result["persistence"]["held_seconds"], 0.0)

    def test_variable_source_lag_does_not_change_the_outcome(self):
        lags = [0.1, 3.0, 0.5, 2.9, 0.2, 2.5, 0.1, 1.0]
        steady = [r["state"] for r in self.series([5 * i for i in range(8)])]
        other = CustomRuleHistory([battery_rule()])
        try:
            varied = [other.record(5 * i, [battery(lag=lag)])["state"] for i, lag in enumerate(lags)]
        finally:
            other.close()
        self.assertEqual(varied, steady)
        self.assertEqual(steady.index("warning"), 6)

    def test_repeated_cached_observation_counts_once_and_does_not_break_the_run(self):
        self.h.save({"version": 1, "rules": [{"id": self.h.rows[0]["id"], "rule": battery_rule(window_seconds=10)}]})
        results = [self.h.record(t, [battery(source=BCAN, lag=t + 0.1)]) for t in (0, 5, 10)]
        self.assertEqual([r["persistence"]["observed"] for r in results], [1, 1, 1])
        self.assertEqual((results[-1]["state"], results[-1]["persistence"]["held_seconds"]), ("normal", 10.0))
        self.assertEqual(self.h.record(15, [battery(source=BCAN, lag=0.1)])["state"], "warning")

    def test_distinct_reading_floor_above_two_is_enforced(self):
        self.h.save({"version": 1, "rules": [{"id": self.h.rows[0]["id"],
                                               "rule": battery_rule(window_seconds=10, persistence_observations=5)}]})
        results = self.series([0, 5, 10, 15, 20])
        self.assertEqual([r["state"] for r in results], ["normal"] * 4 + ["warning"])
        self.assertEqual(results[2]["persistence"]["observed"], 3)

    def test_one_cached_reading_across_the_hold_waits_for_a_second_reading(self):
        self.h.save({"version": 1, "rules": [{"id": self.h.rows[0]["id"], "rule": battery_rule(window_seconds=25)}]})
        cached = [self.h.record(t, [battery(source=BCAN, lag=t + 0.1)]) for t in range(0, 30, 5)]
        self.assertEqual((cached[-1]["state"], cached[-1]["persistence"]["held_seconds"]), ("normal", 25.0))
        self.assertEqual(cached[-1]["persistence"]["observed"], 1)
        self.assertEqual(self.h.record(30, [battery(source=BCAN, lag=0.1)])["state"], "warning")

    def test_source_change_with_qualified_sources_continues(self):
        results = self.series([0, 5, 10, 15, 20, 25, 30], lambda t: [battery(source=CCAN if t % 10 else BCAN)])
        self.assertEqual(results[-1]["state"], "warning")


class ClockTests(HistoryCase):
    def test_source_switch_to_an_older_cached_observation_does_not_extend_the_hold(self):
        self.h.record(0, [battery(13.0, source=CCAN)])
        self.h.record(5, [battery(11.0, source=BCAN, lag=29)])
        result = self.h.record(10, [battery(11.0, source=CCAN)])
        self.assertEqual((result["state"], result["persistence"]["held_seconds"]), ("normal", 5.0))

    def test_forward_wall_step_within_the_gap_does_not_shorten_the_hold(self):
        clocks = [0, 5, 10, 15, 20, 25, 30]
        captures = [0, 5, 20, 25, 30, 35, 40]
        results = self.series(captures, clocks=clocks)
        self.assertEqual([r["state"] for r in results], ["normal"] * 6 + ["warning"])

    def test_forward_wall_step_never_lengthens_the_hold(self):
        clocks = [0, 5, 10, 15, 20, 25, 30]
        captures = [0, 5, 130, 135, 140, 145, 150]
        for clock, result in zip(clocks, self.series(captures, clocks=clocks)):
            self.assertLessEqual(result["persistence"]["held_seconds"], clock)
            self.assertEqual(result["state"] == "warning", clock >= 30)

    def test_backward_wall_step_refused_recordings_restart_the_hold(self):
        self.h.save({"version": 1, "rules": [{"id": self.h.rows[0]["id"], "rule": battery_rule(max_age_seconds=10)}]})
        results = self.series([0, 5, -2, 3, 8], clocks=[0, 5, 10, 15, 20])
        self.assertEqual(results[2:4], [None, None])  # the historian refused both captures
        self.assertEqual(results[4]["persistence"]["held_seconds"], 0.0)

    def test_backward_wall_step_within_the_gap_continues(self):
        results = self.series([0, 5, 4, 12], clocks=[0, 5, 7, 15])
        self.assertIsNone(results[2])
        self.assertEqual(results[3]["persistence"]["held_seconds"], 15.0)

    def test_failed_evaluation_of_a_stored_recording_restarts_the_hold(self):
        self.series([0, 5])
        with mock.patch.object(self.h.historian, "recording_readings", side_effect=sqlite3.OperationalError("busy")):
            self.assertIsNone(self.h.record(10, [battery()]))
        self.assertEqual(self.h.record(15, [battery()])["persistence"]["held_seconds"], 0.0)

    def test_readings_database_error_is_a_file_level_failure_and_keeps_built_in_assessments(self):
        history = CustomRuleHistory([battery_rule()], evaluator_rules=None, episodes=False)
        try:
            history.record(0, [battery()])
            with mock.patch.object(history.historian, "recording_readings",
                                   side_effect=sqlite3.OperationalError("busy")):
                self.assertIsNone(history.record(5, [battery()]))
            result = history.evaluator.last
            names = [item["rule"] for item in result["assessments"]]
            self.assertTrue(any(not name.startswith("custom_") for name in names))
            self.assertFalse(any(name.startswith("custom_") for name in names))
            self.assertEqual(result["custom_rules"]["error"], "Custom warning configuration could not be evaluated")
        finally:
            history.close()

    def test_health_evaluation_reports_the_hold_and_changes_nothing(self):
        recorded = self.series([0, 5, 10])[-1]
        health, result = self.h.health(11)
        self.assertEqual(health["persistence"]["held_seconds"], 10.0)
        self.assertEqual(health["state"], recorded["state"])
        self.assertEqual(result["custom_rules"]["count"], 1)
        self.h.health(12)
        self.assertEqual(self.h.record(15, [battery()])["persistence"]["held_seconds"], 15.0)

    def test_removed_or_revised_rule_state_is_dropped(self):
        rule_id = self.h.rows[0]["id"]
        self.series([0, 5])
        self.h.save({"version": 1, "rules": [{"id": rule_id, "rule": battery_rule(threshold=12.5)}]})
        self.assertEqual(self.h.record(10, [battery()])["persistence"]["held_seconds"], 0.0)
        self.h.record(15, [battery()])
        self.h.save({"version": 1, "rules": []})
        self.assertIsNone(self.h.record(20, [battery()]))
        self.h.save({"version": 1, "rules": [{"id": rule_id, "rule": battery_rule(threshold=12.5)}]})
        self.assertEqual(self.h.record(25, [battery()])["persistence"]["held_seconds"], 0.0)


class QueryCostTests(unittest.TestCase):
    def test_one_readings_query_per_recording_for_all_custom_rules(self):
        metrics = sorted(catalog())
        rules = [battery_rule(title=f"Rule {index}", metric=metrics[index % len(metrics)], operator="above",
                      threshold=-1000, engine_running=index % 2 == 0) for index in range(contract.MAX_RULES)]
        history = CustomRuleHistory(rules)
        try:
            calls = []
            real = history.historian.recording_readings

            def counted(*args, **kwargs):
                calls.append(args)
                return real(*args, **kwargs)

            with mock.patch.object(history.historian, "recording_readings", side_effect=counted):
                history.record(0, [battery(), rpm()])
            self.assertEqual(len(calls), 1)
            self.assertEqual(len([a for a in history.evaluator.last["assessments"] if a["rule"].startswith("custom_")]), 32)
        finally:
            history.close()

    def test_one_episode_read_per_evaluation_for_all_waiting_custom_rules(self):
        # Every rule is beyond its limit and waiting to confirm, so each one asks whether it
        # already has an open warning; the evaluation answers them all from one episode read.
        rules = [battery_rule(title=f"Rule {index}", operator="above", threshold=-1000 + index,
                              window_seconds=600, engine_running=index % 2 == 0)
                 for index in range(contract.MAX_RULES)]
        history = CustomRuleHistory(rules, episodes=False)
        try:
            calls = []
            real = history.historian.list_advisory_episodes

            def counted(*args, **kwargs):
                calls.append(kwargs)
                return real(*args, **kwargs)

            with mock.patch.object(history.historian, "list_advisory_episodes", side_effect=counted):
                for t in (0, 5):
                    history.record(t, [battery(), rpm()])
            custom = [a for a in history.evaluator.last["assessments"] if a["rule"].startswith("custom_")]
            self.assertEqual([a["reason"] for a in custom],
                             ["Outside the owner-approved reference; waiting to confirm"] * contract.MAX_RULES)
            self.assertEqual(len(calls), 2)
            from projects.vehicle_data.event_history import digest
            self.assertEqual([a["rule_revision"] for a in custom], [digest(a["rule_snapshot"]) for a in custom])
        finally:
            history.close()

    def test_built_in_rules_ignore_the_recording_argument(self):
        history = CustomRuleHistory()
        try:
            for t in range(0, 30, 5):
                history.record(t, [battery(), coolant(), rpm()])
            outputs = []
            for recording in (None, (6, 1234.5)):
                evaluator = EarlyWarningEvaluator(history.historian)
                result = evaluator.evaluate(at=START.replace(second=25), recording=recording)
                outputs.append(json.dumps(result["assessments"], sort_keys=True, default=str))
            self.assertEqual(outputs[0], outputs[1])
            self.assertGreater(len(json.loads(outputs[0])), 5)
        finally:
            history.close()


class ValidationTests(unittest.TestCase):
    def test_gap_may_exceed_the_stale_limit_but_not_the_hold(self):
        self.assertEqual(validate_rule(dict(FIELD_DUTY, max_age_seconds=20))["max_age_seconds"], 20)
        for change in ({"max_age_seconds": 31}, {"window_seconds": 600, "max_age_seconds": 61}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_rule(dict(FIELD_DUTY, **change))

    def test_advice_uses_the_reported_interval_and_is_silent_without_one(self):
        rule = battery_rule(max_age_seconds=8)
        self.assertIn("about every 5 s", advice(rule, 5))
        self.assertIn("under 10 s", advice(rule, 5))
        self.assertIsNone(advice(dict(rule, max_age_seconds=10), 5))
        for interval in (None, 0, -1, float("nan"), True, "5"):
            self.assertIsNone(advice(rule, interval))

    def test_contract_limits_are_the_validator_limits(self):
        base = battery_rule(window_seconds=600, max_age_seconds=1)
        for key in ("threshold", "persistence_observations", "window_seconds", "max_age_seconds"):
            low, high = contract.LIMITS[key]
            for value, valid in ((low, True), (high, True), (low - 1, False), (high + 1, False)):
                rule = dict(base, **{key: value})
                if key == "window_seconds":
                    rule["max_age_seconds"] = 1
                with self.subTest(key=key, value=value):
                    if valid:
                        self.assertEqual(validate_rule(rule)[key], value)
                    else:
                        self.assertRaises(ValueError, validate_rule, rule)
        low, high = contract.LIMITS["title_chars"]
        self.assertEqual(len(validate_rule(dict(base, title="x" * high))["title"]), high)
        self.assertRaises(ValueError, validate_rule, dict(base, title="x" * (high + 1)))
        self.assertRaises(ValueError, validate_rule, dict(base, title=" " * low))
        self.assertEqual(set(contract.FIELDS), set(BATTERY))

    def test_catalog_from_served_definitions_equals_catalog(self):
        served = json.loads(json.dumps([definition.public_dict() for definition in METRICS.values()]))
        self.assertEqual(catalog_from({entry["name"]: entry for entry in served}), catalog())

    def test_alternator_field_duty_is_an_owner_rule_metric(self):
        entries = catalog()
        self.assertIn("generator.field_duty", entries)
        self.assertEqual(validate_rule(FIELD_DUTY)["metric"], "generator.field_duty")
        for name, definition in METRICS.items():
            established = any(s.quality in ("verified", "observed_alfa_scale") for s in definition.sources)
            self.assertEqual(name in entries, definition.value_type == "number" and established, name)

    def test_schema_and_limits_reject_unsafe_or_ambiguous_rules(self):
        for change in ({"command": "anything"}, {"metric": "engine.oil_life_remaining"}, {"threshold": float("nan")},
                       {"persistence_observations": 1}, {"persistence_observations": True},
                       {"max_age_seconds": 3600}, {"window_seconds": 601}, {"engine_running": "yes"},
                       {"operator": "absolute_above", "threshold": -1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_rule({**RULE, **change})


class OwnerReferenceTests(HistoryCase):
    rules = (battery_rule(window_seconds=10),)

    def test_persistence_and_owner_reference(self):
        results = self.series([0, 5, 10])
        pending, result = results[1], results[2]
        self.assertEqual(result["state"], "warning")
        self.assertTrue(result["notification_eligible"])
        self.assertIn("not an OEM limit", result["interpretation"])
        self.assertEqual((result["tier"], result["group"], result["confidence"]), (1, "custom", "medium"))
        self.assertEqual(result["action"], "Check it at the next stop.")
        self.assertEqual(result["evaluator_revision"], "owner-threshold-v2")
        self.assertNotIn("candidate", result)
        self.assertEqual(pending["state"], "normal")
        self.assertFalse(pending["notification_eligible"])
        self.assertEqual(pending["candidate"]["held_seconds"], 5.0)
        self.assertEqual(pending["candidate"]["hold_seconds"], 10.0)
        self.assertEqual((pending["candidate"]["observed"], pending["candidate"]["required"]), (2, 2))
        self.assertEqual(pending["candidate"]["first_seen_at"], START.isoformat())

    def test_unqualified_latest_readings_never_alert(self):
        for entry in (battery(lag=31), battery(stale=True)):
            with self.subTest(entry=entry):
                self.assertEqual(self.h.record(self.h.sequence * 5, [entry])["state"], "unavailable")

    def test_absolute_magnitude_and_normal(self):
        self.h.save({"version": 1, "rules": [{"id": self.h.rows[0]["id"],
                                               "rule": battery_rule(operator="absolute_above", threshold=12,
                                                            window_seconds=10)}]})
        self.assertEqual(self.h.record(0, [battery(11.5)])["state"], "normal")
        self.assertNotIn("candidate", self.h.record(5, [battery(11.5)]))
        self.assertEqual([self.h.record(t, [battery(-13)])["state"] for t in (10, 15, 20)],
                         ["normal", "normal", "warning"])


class ConfigurationTests(unittest.TestCase):
    def test_configuration_reload_and_failure_isolation(self):
        history = CustomRuleHistory()
        try:
            self.assertEqual(history.evaluator.evaluate(at=START)["custom_rules"]["count"], 0)
            history.add(battery_rule(window_seconds=5, max_age_seconds=5))
            history.record(0, [battery()])
            self.assertEqual(len(history.record(5, [battery()]) and history.evaluator.last["active"]), 1)
            history.rules_path.write_text("broken config")
            self.assertIsNotNone(history.evaluator.evaluate(at=START)["custom_rules"]["error"])
            history.save({"version": 1, "rules": []})
            self.assertEqual(history.evaluator.evaluate(at=START)["active"], [])
        finally:
            history.close()

    def test_no_symlink_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warnings.json"
            real = Path(tmp) / "real.json"
            real.write_text(json.dumps({"version": 1, "rules": []}))
            path.symlink_to(real)
            with self.assertRaises(ValueError):
                load_rules(path)


if __name__ == "__main__":
    unittest.main()
