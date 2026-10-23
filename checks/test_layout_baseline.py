"""The ratchet against the `layout` key of .codeshape/baseline.json, rebaseline, exceptions,
config errors and coverage counts."""
import json

from layout_fixture import LayoutCase, python_module

GROUP = {"tools/radar_a.py": "", "tools/radar_b.py": "", "tools/radar_c.py": ""}


class Ratchet(LayoutCase):
    def baselined_tree(self):
        self.write(GROUP)
        self.baseline_file()
        self.commit()
        result = self.run_checker("rebaseline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.commit("baseline")

    def test_without_a_layout_key_everything_is_advisory(self):
        self.write(GROUP)
        self.baseline_file()
        data = self.check(expect=0)
        self.assertEqual({f["status"] for f in data["findings"]}, {"advisory"})
        self.assertIn("advisory only", data["verdict"])

    def test_baselined_findings_pass_and_a_new_one_fails(self):
        self.baselined_tree()
        data = self.check(expect=0)
        self.assertEqual([f["status"] for f in self.found(data, "prefix-group")], ["baselined"] * 3)
        self.write({"tools/radar_d.py": python_module(1)})
        data = self.check(expect=1)
        statuses = {f["path"]: f["status"] for f in self.found(data, "prefix-group")}
        self.assertEqual(statuses["tools/radar_d.py"], "new")
        self.assertEqual(data["counts"]["findings"]["new"], 1)

    def test_renamed_file_keeps_its_baseline_entry(self):
        self.baselined_tree()
        self.git("mv", "tools/radar_c.py", "tools/radar_cal.py")
        data = self.check(expect=0)
        self.assertEqual(data["counts"]["findings"]["new"], 0)

    def test_rebaseline_replaces_only_the_layout_key(self):
        self.write(GROUP)
        original = self.baseline_file().read_text()
        self.commit()
        self.assertEqual(self.run_checker("rebaseline").returncode, 0)
        after = (self.root / ".codeshape/baseline.json").read_text()
        layout_start = after.index(' "layout"')
        layout_end = after.index('\n "limits"')
        self.assertEqual(after[:layout_start] + after[layout_end + 1:], original)
        data = json.loads(after)
        self.assertEqual(data["layout"]["findings"]["prefix-group"], sorted(GROUP))
        self.commit("baseline")
        self.assertEqual(self.run_checker("rebaseline").returncode, 0)
        again = (self.root / ".codeshape/baseline.json").read_text()
        changed = [line for line in again.splitlines() if line not in after.splitlines()]
        self.assertEqual(len(changed), 1)
        self.assertIn('"head"', changed[0])

    def test_rebaseline_refuses_without_a_baseline_file_or_on_a_dirty_tree(self):
        self.write(GROUP)
        self.commit()
        result = self.run_checker("rebaseline")
        self.assertEqual(result.returncode, 2)
        self.assertIn("drift_check.py rebaseline", result.stderr)
        self.baseline_file()
        self.write({"tools/new.py": ""})
        result = self.run_checker("rebaseline")
        self.assertEqual(result.returncode, 2)
        self.assertIn("uncommitted", result.stderr)

    def test_exception_exempts_and_unused_exception_is_reported(self):
        self.write(GROUP)
        self.config({"exceptions": [{"rule": "prefix-group", "path": "tools/radar_*", "reason": "one CLI each"},
                                    {"rule": "cycle", "path": "nowhere", "reason": "stale"}]})
        data = self.check()
        self.assertEqual({f["status"] for f in self.found(data, "prefix-group")}, {"exempt"})
        self.assertEqual([e["covers"] for e in data["exceptions"]], [3, 0])
        self.assertTrue(any("matched nothing" in w for w in data["warnings"]))


class ConfigAndCoverage(LayoutCase):
    def assert_config_error(self, text: str, fragment: str):
        self.write({"app.py": "", ".codeshape/config.json": text})
        result = self.run_checker()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn(fragment, result.stderr)

    def test_malformed_json(self):
        self.assert_config_error("{not json", "config error")

    def test_unknown_keys_fail(self):
        self.assert_config_error(json.dumps({"layout": {"sourceRoot": ["."]}}), "unknown layout setting(s): sourceRoot")
        self.assert_config_error(json.dumps({"layout": {"thresholds": {"entrylines": 5}}}), "entrylines")

    def test_bad_exception_and_missing_source_root(self):
        self.assert_config_error(json.dumps({"layout": {"exceptions": [{"rule": "size", "path": "x", "reason": "r"}]}}),
                                 "rule must be one of")
        self.assert_config_error(json.dumps({"layout": {"exceptions": [{"rule": "cycle", "path": "x"}]}}),
                                 "needs a `reason`")
        self.assert_config_error(json.dumps({"layout": {"sourceRoots": ["missing"]}}), "source root not found")

    def test_other_tools_keys_coexist(self):
        self.write({"app.py": ""})
        self.config({}, file_lines=900, hook_mode="block", css={"sourceRoots": ["src"]}, structure={})
        self.assertEqual(self.check()["exitCode"], 0)

    def test_zero_files_scanned_is_an_error(self):
        self.write({"README.md": "# nothing to scan\n"})
        data = self.check(expect=1)
        self.assertEqual(data["counts"]["filesScanned"], 0)
        self.assertIn("zero files scanned", data["verdict"])

    def test_counts_cover_unresolved_and_unsupported_imports(self):
        self.write({"pkg/__init__.py": "", "pkg/a.py": "from .missing import x\nimport os\n",
                    "run.sh": "#!/bin/sh\n. ./lib/common.sh\n", "web/a.mjs": "import x from './nope.mjs'\n"})
        counts = self.check()["counts"]
        self.assertEqual((counts["importsUnresolved"], counts["importsUnsupported"]), (2, 1))

    def test_text_output_shows_every_count(self):
        self.write({"app.py": ""})
        out = self.run_checker().stdout
        for label in ("files scanned: 1", "imports:", "errors: 0 parse", "baseline:", "findings:",
                      "exceptions applied:", "verdict: OK"):
            self.assertIn(label, out)
