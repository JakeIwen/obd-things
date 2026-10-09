"""Structural compatibility of the extracted evaluator and its historical API."""
from dataclasses import asdict, fields
import importlib
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from projects.vehicle_data import early_warning as ew
from projects.vehicle_data import warning_engine
from tools import warning_replay as wr


class WarningPackageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        if wr._git("rev-parse", "ee3ad14") is None:
            self.skipTest("historical evaluator ref unavailable")
        self.old, _ = wr.load_current_evaluator("ee3ad14", out_dir=self.directory.name)

    def test_every_historical_global_still_resolves(self):
        missing = [name for name in vars(self.old) if not name.startswith("__") and not hasattr(ew, name)]
        self.assertEqual(missing, [])
        for name, value in vars(ew).items():
            module_name = getattr(value, "__module__", "")
            if module_name.startswith("projects.vehicle_data.warning_engine."):
                with self.subTest(name=name):
                    self.assertIs(value, getattr(importlib.import_module(module_name), name))
        self.assertIs(ew.EarlyWarningEvaluator, warning_engine.EarlyWarningEvaluator)
        self.assertIs(ew.InfrastructureHealthEvaluator, warning_engine.InfrastructureHealthEvaluator)

    def test_rule_values_field_order_and_revision_digests_are_identical(self):
        for name in ("WarningRule", "AbsoluteRule", "AbsoluteOilPressureRule", "TireGroupRule", "CorroborationRule"):
            with self.subTest(dataclass=name):
                self.assertEqual([f.name for f in fields(getattr(ew, name))],
                                 [f.name for f in fields(getattr(self.old, name))])
        for table in ("DEFAULT_WARNING_RULES", "DEFAULT_ABSOLUTE_WARNING_RULES", "DEFAULT_EVALUATION_RULES"):
            current, previous = getattr(ew, table), getattr(self.old, table)
            self.assertEqual(len(current), len(previous))
            for left, right in zip(current, previous):
                with self.subTest(table=table, rule=left.key):
                    self.assertEqual(asdict(left), asdict(right))
                    self.assertEqual(ew._rule_snapshot(left), self.old._rule_snapshot(right))
        self.assertEqual(ew.WARNING_SCHEMA_VERSION, self.old.WARNING_SCHEMA_VERSION)
        self.assertEqual(ew.default_rule_catalog(), self.old.default_rule_catalog())

    def test_mixins_have_no_overlapping_methods_or_initializers(self):
        seen = set()
        for base in ew.EarlyWarningEvaluator.__mro__[:-1]:
            methods = {name for name, value in vars(base).items()
                       if inspect.isfunction(value) or isinstance(value, (staticmethod, classmethod))}
            self.assertFalse(seen & methods, (base, seen & methods))
            if base is not ew.EarlyWarningEvaluator:
                self.assertNotIn("__init__", methods)
            seen.update(methods)

    def test_new_layout_loaded_from_ref_is_not_the_worktree_package(self):
        paths = [wr.REPO / wr.EVALUATOR_PATH, *sorted((wr.REPO / wr.EVALUATOR_PACKAGE).rglob("*.py"))]
        sources = {path.relative_to(wr.REPO).as_posix(): path.read_text() for path in paths}
        with patch.object(wr, "_ref_evaluator_sources", return_value=sources):
            saved, _ = wr.load_current_evaluator("ee3ad14", out_dir=self.directory.name)
        self.assertIsNot(saved.EarlyWarningEvaluator, ew.EarlyWarningEvaluator)
        self.assertIsNot(saved.DEFAULT_EVALUATION_RULES, ew.DEFAULT_EVALUATION_RULES)
        self.assertEqual(saved.default_rule_catalog(), ew.default_rule_catalog())
        for name in ("_relative_core", "_evaluate_absolute_oil_rule", "_pair_axle", "_running_gate"):
            self.assertTrue(getattr(saved.EarlyWarningEvaluator, name).__module__.startswith("early_warning_replay_current."))

    def test_worktree_identity_tracks_a_package_only_change(self):
        _, before = wr.load_worktree_evaluator()
        read_text = Path.read_text

        def changed(path, *args, **kwargs):
            value = read_text(path, *args, **kwargs)
            return value + "\n# package-only edit\n" if path.name == "rules.py" else value

        with patch.object(Path, "read_text", changed):
            _, after = wr.load_worktree_evaluator()
        self.assertEqual(before["blob"], after["blob"])
        self.assertNotEqual(before["evaluator_digest"], after["evaluator_digest"])
        self.assertFalse(after["matches_head"])
