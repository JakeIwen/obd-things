"""Ref-layout isolation and shared rule contracts for the offline replay."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from projects.vehicle_data.early_warning import EarlyWarningEvaluator
from tools import warning_replay as wr


class EvaluatorLoadingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = Path(self.directory.name)
        prefix = "early_warning_replay_current"
        saved = {k: v for k, v in sys.modules.items() if k == prefix or k.startswith(prefix + ".")}

        def restore():
            for key in list(sys.modules):
                if key == prefix or key.startswith(prefix + "."):
                    del sys.modules[key]
            sys.modules.update(saved)

        self.addCleanup(restore)

    def load(self, sources):
        def git(*args, strip=True):
            if args == ("rev-parse", "test"):
                return "commit"
            if args == ("rev-parse", f"test:{wr.EVALUATOR_PATH}"):
                return "shim-blob"
            if args == ("ls-tree", "-r", "--name-only", "commit", "--", wr.EVALUATOR_PACKAGE):
                return "\n".join(p for p in sources if p.startswith(wr.EVALUATOR_PACKAGE + "/"))
            if args[0] == "show":
                return sources.get(args[1].removeprefix("commit:"))
            raise AssertionError(args)

        with patch.object(wr, "_git", side_effect=git):
            return wr.load_current_evaluator("test", out_dir=self.out)

    @staticmethod
    def package(value):
        return {
            wr.EVALUATOR_PATH: "from .warning_engine import EarlyWarningEvaluator\n",
            wr.EVALUATOR_PACKAGE + "/__init__.py": "from .evaluator import EarlyWarningEvaluator\n",
            wr.EVALUATOR_PACKAGE + "/evaluator.py": (
                "from dataclasses import dataclass\n"
                "from .rules import VALUE\n"
                "@dataclass\nclass EarlyWarningEvaluator:\n    value: str = VALUE\n"
            ),
            wr.EVALUATOR_PACKAGE + "/rules.py": f"VALUE = {value!r}\n",
        }

    def test_monolith_imports_saved_source_not_worktree(self):
        source = "from dataclasses import dataclass\n@dataclass\nclass EarlyWarningEvaluator:\n    value: str = 'saved monolith'\n"
        module, info = self.load({wr.EVALUATOR_PATH: source})
        self.assertEqual(module.EarlyWarningEvaluator().value, "saved monolith")
        self.assertIsNot(module.EarlyWarningEvaluator, EarlyWarningEvaluator)
        self.assertEqual(Path(info["module_path"]).read_text(), source)

    def test_package_imports_every_saved_module_and_reloads_between_refs(self):
        first, first_info = self.load(self.package("first ref"))
        second, second_info = self.load(self.package("second ref"))
        self.assertEqual(first.EarlyWarningEvaluator().value, "first ref")
        self.assertEqual(second.EarlyWarningEvaluator().value, "second ref")
        self.assertIsNot(first.EarlyWarningEvaluator, second.EarlyWarningEvaluator)
        self.assertIsNot(second.EarlyWarningEvaluator, EarlyWarningEvaluator)
        self.assertEqual(first_info["blob"], second_info["blob"])
        self.assertNotEqual(first_info["evaluator_digest"], second_info["evaluator_digest"])
        self.assertNotEqual(first_info["module_path"], second_info["module_path"])
        for path, source in self.package("second ref").items():
            materialized = Path(second_info["module_path"]).parent / Path(path).relative_to("projects/vehicle_data")
            self.assertEqual(materialized.read_text(), source)

    def test_incomplete_package_does_not_fall_back_to_worktree(self):
        first, _ = self.load(self.package("first ref"))
        sources = self.package("broken ref")
        del sources[wr.EVALUATOR_PACKAGE + "/rules.py"]
        with self.assertRaises(ModuleNotFoundError):
            self.load(sources)
        self.assertIs(sys.modules["early_warning_replay_current"], first)
        self.assertIs(sys.modules["early_warning_replay_current.warning_engine.evaluator"].EarlyWarningEvaluator,
                      first.EarlyWarningEvaluator)

    def test_package_to_monolith_drops_old_submodules(self):
        self.load(self.package("old package"))
        module, _ = self.load({wr.EVALUATOR_PATH: "class EarlyWarningEvaluator: pass\n"})
        self.assertIs(sys.modules["early_warning_replay_current"], module)
        self.assertFalse(any(k.startswith("early_warning_replay_current.") for k in sys.modules))

    def test_digest_includes_package_contents_and_paths(self):
        source = self.package("one")
        changed = self.package("two")
        renamed = dict(source)
        renamed[wr.EVALUATOR_PACKAGE + "/other.py"] = renamed.pop(wr.EVALUATOR_PACKAGE + "/rules.py")
        self.assertNotEqual(wr._evaluator_digest(source), wr._evaluator_digest(changed))
        self.assertNotEqual(wr._evaluator_digest(source), wr._evaluator_digest(renamed))
        self.assertEqual(wr._evaluator_digest(source), wr._evaluator_digest(dict(reversed(list(source.items())))))

    def test_memo_accessor_keeps_tuple_layout_and_edge_memo_uses_it(self):
        at = datetime(2026, 10, 3, tzinfo=timezone.utc)
        sample = dict(regime="engine_running:highway:rpm_low:warm", unit="V", quality="q",
                      source="s", provenance="p", trip_id=7)
        key = EarlyWarningEvaluator._memo_key("m", sample, at, dimensions=("engine",), lookback_days=23)
        self.assertEqual(key, ("m", "engine=engine_running", "V", "q", "s", "p", ("engine",),
                               23, 0.0, None, None, None, 7))
        self.assertEqual(EarlyWarningEvaluator.memo_key_lookback_days(key), 23)
        memo = wr.EdgeAwareMemo()
        memo.now_us = 25 * wr.DAY_US
        memo[key] = None
        memo._stored[key] = (wr.DAY_US, 0)
        with patch.object(EarlyWarningEvaluator, "memo_key_lookback_days", return_value=30) as read:
            self.assertIn(key, memo)
            read.assert_called_once_with(key)
        self.assertNotIn(key, memo)
