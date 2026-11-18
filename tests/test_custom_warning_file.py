"""The custom early-warning rules file: tolerant read, one durable write, removed ids."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data import custom_warning_contract as contract
from projects.vehicle_data import custom_warning_file as rules_file
from projects.vehicle_data import custom_warnings
from projects.vehicle_data.custom_warnings import load_rules
from tests.custom_rule_history import CustomRuleHistory, battery, battery_rule

GOOD = battery_rule(window_seconds=10)
BAD = dict(GOOD, metric="engine.oil_life_remaining")


def row(index, rule=GOOD, **extra):
    return {"id": format(index, "064x"), "rule": dict(rule), "approved_at": "2026-11-17T00:00:00+00:00",
            "chat_id": None, "owner": "o" * 64, **extra}


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "early-warnings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, payload):
        self.path.write_text(json.dumps(payload))

    def test_tolerant_read_keeps_rejected_rows_whole_and_other_keys(self):
        bad = row(2, BAD, note="kept")
        broken = {"id": "not-hex", "rule": GOOD}
        self.put({"version": 1, "rules": [row(1), bad, broken], "removed": [format(9, "064x"), "junk"],
                  "future": {"x": 1}})
        loaded = rules_file.read(self.path)
        self.assertEqual([item["id"] for item in loaded.rows], [format(1, "064x")])
        self.assertEqual(loaded.rejected, [bad, broken])
        self.assertEqual(loaded.removed, [format(9, "064x")])
        self.assertEqual(loaded.other, {"future": {"x": 1}})
        rules_file.write(self.path, loaded)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["future"], {"x": 1})
        self.assertIn(bad, saved["rules"])
        self.assertIn(broken, saved["rules"])
        self.assertRaises(ValueError, load_rules, self.path)

    def test_file_level_problems_are_errors(self):
        for payload in ({"version": 2, "rules": []}, {"version": 1, "rules": {}}, [],
                        {"version": 1, "rules": [row(1), row(1)]},
                        {"version": 1, "rules": [row(index) for index in range(1, contract.MAX_RULES + 2)]}):
            with self.subTest(payload=str(payload)[:60]):
                self.put(payload)
                self.assertRaises(ValueError, rules_file.read, self.path)
        self.path.write_text(" " * (contract.MAX_FILE_BYTES + 1))
        self.assertRaises(ValueError, rules_file.read, self.path)
        self.assertEqual(rules_file.read(Path(self.tmp.name) / "absent.json").rows, [])

    def test_removed_ids_are_ignored_by_the_broker_and_the_current_loader(self):
        self.put({"version": 1, "rules": [row(1)], "removed": [format(2, "064x")]})
        self.assertEqual([item["id"] for item in load_rules(self.path)], [format(1, "064x")])
        history = CustomRuleHistory()
        try:
            history.save({"version": 1, "rules": [row(1)], "removed": [format(2, "064x")]})
            status = history.evaluator.evaluate()["custom_rules"]
            self.assertEqual((status["count"], status["error"], status["rejected_ids"]), (1, None, []))
        finally:
            history.close()

    def test_write_keeps_the_newest_256_removed_ids_and_drops_ids_present_as_rows(self):
        removed = [format(index, "064x") for index in range(10, 10 + contract.MAX_REMOVED_IDS + 5)]
        loaded = rules_file.RulesFile(rows=[row(1)], removed=[format(1, "064x"), *removed])
        rules_file.write(self.path, loaded)
        saved = json.loads(self.path.read_text())["removed"]
        self.assertEqual(saved, removed[-contract.MAX_REMOVED_IDS:])
        self.assertNotIn(format(1, "064x"), saved)

    def test_write_over_128_kib_is_refused_without_a_write(self):
        self.put({"version": 1, "rules": []})
        before = self.path.read_bytes()
        big = rules_file.RulesFile(rows=[row(1, padding="x" * contract.MAX_FILE_BYTES)])
        with mock.patch.object(rules_file, "_atomic_json") as atomic:
            self.assertRaises(ValueError, rules_file.write, self.path, big)
        atomic.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_crash_before_replace_leaves_the_old_file(self):
        self.put({"version": 1, "rules": [row(1)]})
        before = self.path.read_bytes()
        with mock.patch("os.replace", side_effect=OSError("killed")):
            self.assertRaises(OSError, rules_file.add, self.path, row(2))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["early-warnings.json"])
        self.assertTrue(rules_file.add(self.path, row(2)))
        self.assertFalse(rules_file.add(self.path, row(2)))  # a retried install installs once
        self.assertEqual(len(rules_file.read(self.path).rows), 2)

    def test_write_fsyncs_the_directory_after_replace(self):
        events = []
        real_replace, real_fsync = os.replace, os.fsync

        def replace(*args):
            events.append("replace")
            return real_replace(*args)

        def fsync(descriptor):
            events.append("dir-fsync" if os.path.isdir(f"/proc/self/fd/{descriptor}") else "file-fsync")
            return real_fsync(descriptor)

        with mock.patch("os.replace", side_effect=replace), mock.patch("os.fsync", side_effect=fsync):
            rules_file.write(self.path, rules_file.RulesFile(rows=[row(1)]))
        self.assertEqual(events, ["file-fsync", "replace", "dir-fsync"])

    def test_add_and_remove_record_removed_ids_and_keep_rejected_rows(self):
        bad = row(3, BAD)
        self.put({"version": 1, "rules": [row(1), bad]})
        self.assertTrue(rules_file.remove(self.path, format(1, "064x")))
        self.assertRaises(rules_file.RemovedRule, rules_file.add, self.path, row(1))
        self.assertFalse(rules_file.remove(self.path, format(7, "064x")))  # still recorded
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["rules"], [bad])
        self.assertEqual(saved["removed"], [format(1, "064x"), format(7, "064x")])
        self.assertFalse(rules_file.remove(self.path, format(3, "064x"), owner="someone else"))
        self.assertTrue(rules_file.remove(self.path, format(3, "064x")))
        self.assertEqual(json.loads(self.path.read_text())["rules"], [])

    def test_cap_counts_rejected_rows(self):
        self.put({"version": 1, "rules": [row(index) for index in range(1, contract.MAX_RULES)] + [row(99, BAD)]})
        self.assertRaises(rules_file.RulesFull, rules_file.add, self.path, row(100))


class BrokerToleranceTests(unittest.TestCase):
    def setUp(self):
        self.h = CustomRuleHistory()

    def tearDown(self):
        self.h.close()

    def test_one_rejected_row_leaves_the_other_custom_rules_evaluating(self):
        self.h.save({"version": 1, "rules": [row(1), row(2, BAD)]})
        self.h.rows = [row(1)]
        results = [self.h.record(t, [battery()]) for t in (0, 5, 10)]
        self.assertEqual(results[-1]["state"], "warning")
        status = self.h.evaluator.last["custom_rules"]
        self.assertEqual(status["rule_ids"], [format(1, "064x")])
        self.assertEqual(status["rejected_ids"], [format(2, "064x")])
        self.assertIsNone(status["error"])

    def test_one_rule_failing_its_assessment_stops_alone(self):
        self.h.save({"version": 1, "rules": [row(1), row(2)]})
        self.h.rows = [row(1)]
        real = custom_warnings.evaluate_rule

        def flaky(historian, item, at, **kwargs):
            if item["id"] == format(2, "064x"):
                raise KeyError("fixture assessment failure")
            return real(historian, item, at, **kwargs)

        with mock.patch.object(custom_warnings, "evaluate_rule", flaky):
            results = [self.h.record(t, [battery()]) for t in (0, 5, 10)]
        self.assertEqual(results[-1]["state"], "warning")
        status = self.h.evaluator.last["custom_rules"]
        self.assertEqual((status["count"], status["rule_ids"]), (1, [format(1, "064x")]))
        self.assertEqual(status["rejected_ids"], [format(2, "064x")])
        self.assertIsNone(status["error"])

    def test_rejected_rule_retires_its_open_episode(self):
        self.h.save({"version": 1, "rules": [row(1)]})
        self.h.rows = [row(1)]
        for t in (0, 5, 10):
            self.h.record(t, [battery()])
        key = "custom_" + format(1, "064x")
        active = [e["rule"] for e in self.h.historian.list_advisory_episodes(active_only=True, limit=200)]
        self.assertIn(key, active)
        self.h.save({"version": 1, "rules": [row(1, BAD)]})
        self.h.record(15, [battery()])
        active = [e["rule"] for e in self.h.historian.list_advisory_episodes(active_only=True, limit=200)]
        self.assertNotIn(key, active)
        self.assertEqual(self.h.evaluator.last["custom_rules"]["rejected_ids"], [format(1, "064x")])

    def test_file_level_corruption_still_reports_an_error_and_built_ins_continue(self):
        history = CustomRuleHistory(evaluator_rules=None)
        try:
            history.rules_path.write_text("{not json")
            result = history.evaluator.evaluate()
            self.assertEqual(result["custom_rules"]["error"], "Custom warning configuration could not be evaluated")
            self.assertGreater(len(result["assessments"]), 5)
        finally:
            history.close()


if __name__ == "__main__":
    unittest.main()
