"""Enforced boundaries independent of the advisory layout baseline.

These checks use source inventories and isolated imports, never live entry
points. The manifest checks also run on a compute worker's gitless payload.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

from checks.backend_layout import config, contracts_table, files, targets
from checks.backend_layout.references import RepoIndex


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REFERENCE = re.compile(r"\{source:([^}]+)\}")
PURE_MODULES = (
    "lib.signal_search_contract",
    "tools.signal_benchmark_policy",
    "projects.vehicle_data.metric_types",
    "projects.vehicle_data.helper_events",
    "projects.vehicle_data.armed_at_startup",
    "projects.vehicle_data.helper_hold",
    "projects.vehicle_data.wake_state",
    "projects.vehicle_data.usb_recovery_contract",
    "projects.vehicle_data.usb_recovery_model",
)


def packaged(path: str, sources: list[str]) -> bool:
    """Manifest entries may be files, directory cohorts or glob patterns."""
    return any(
        path == source
        or path.startswith(source.rstrip("/") + "/")
        or fnmatch.fnmatchcase(path, source)
        for source in sources
    )


class BackendContractsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = config.load(str(ROOT), None)
        cls.paths = files.listed_paths(str(ROOT))
        cls.index = RepoIndex.build(str(ROOT), cls.paths)
        cls.table = contracts_table.read(
            str(ROOT), cls.settings["contractsTable"], cls.index
        )

    def test_launch_targets_have_existing_owned_contracts(self):
        self.assertTrue(self.table.found, self.table.note)
        self.assertTrue(self.table.has_caller and self.table.has_owner)
        self.assertTrue(self.table.rows)
        found = targets.collect(self.settings, self.paths, self.index)
        self.assertTrue(found.files, "no service/package declarations scanned")
        self.assertEqual(found.errors, [])
        self.assertEqual(
            [finding.message for finding in targets.findings(self.table, found)], []
        )
        # Dynamic commands require a deliberate review instead of quietly
        # disappearing from the declared-target inventory.
        self.assertEqual(found.dynamic, [])
        for row in self.table.rows:
            with self.subTest(row=row.cell):
                self.assertTrue(row.caller and row.owner)
                self.assertTrue(row.references, "unresolved contract row")
                self.assertTrue(
                    all(reference.kind == "repo" for reference in row.references),
                    row.references,
                )

    def test_compute_entry_targets_are_packaged_and_documented(self):
        manifest = json.loads((ROOT / ".van-compute.json").read_text())
        tasks = manifest["tasks"]
        self.assertTrue(tasks)
        entry_count = 0
        for task in tasks:
            for argument in task["argv"]:
                for path in SOURCE_REFERENCE.findall(argument):
                    entry_count += 1
                    with self.subTest(task=task["name"], path=path):
                        self.assertTrue((ROOT / path).is_file())
                        self.assertTrue(packaged(path, task["source_paths"]))
                        self.assertTrue(self.table.lists(path))
            if any("tools/can_timeseries_correlate.py" in arg for arg in task["argv"]):
                with self.subTest(task=task["name"], dependency="search contract"):
                    self.assertTrue(
                        packaged("lib/signal_search_contract.py", task["source_paths"])
                    )
        self.assertGreater(entry_count, 0)

    def test_tasks_that_carry_the_dashboard_source_carry_every_json_it_imports(self):
        # The dashboard imports its shared vocabularies from files beside the Python modules; a task
        # that packages the dashboard but not one of them fails its isolated build.
        dashboard = ROOT / "projects/vehicle_data/dashboard"
        import_re = re.compile(r"""from\s+["']([^"']+\.json)["']""")
        needed = set()
        for source in sorted((dashboard / "src").rglob("*")):
            if source.suffix in {".js", ".jsx", ".mjs"}:
                for target in import_re.findall(source.read_text()):
                    resolved = (source.parent / target).resolve()
                    if resolved.is_relative_to(dashboard.resolve().parent):
                        needed.add(str(resolved.relative_to(ROOT)))
        self.assertIn("projects/vehicle_data/api_credits.json", needed)
        tasks = json.loads((ROOT / ".van-compute.json").read_text())["tasks"]
        carriers = [task for task in tasks if packaged("projects/vehicle_data/dashboard/src/main.jsx", task["source_paths"])]
        self.assertTrue(carriers)
        for task in carriers:
            for path in sorted(needed):
                with self.subTest(task=task["name"], path=path):
                    self.assertTrue(packaged(path, task["source_paths"]))

    def test_shared_library_has_no_application_dependencies(self):
        libraries = sorted((ROOT / "lib").rglob("*.py"))
        self.assertTrue(libraries)
        for path in libraries:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                if not names:
                    continue
                with self.subTest(path=path.relative_to(ROOT), imports=names):
                    self.assertFalse(
                        {name.split(".")[0] for name in names}
                        & {"tools", "projects", "live_data"}
                    )

    def test_pure_owners_import_without_runtime_or_transport(self):
        program = """
import importlib
import sys

blocked = (
    'socket', 'subprocess', 'sqlite3', 'isotp', 'can', 'numpy',
    'lib.uds', 'lib.canbus', 'lib.can_runtime_route', 'lib.can_wake',
    'tools.can_timeseries_correlate', 'tools.can_signal_benchmark',
    'projects.vehicle_data.broker', 'projects.vehicle_data.historian',
    'projects.vehicle_data.can_runtime', 'projects.vehicle_data.api_server',
)

class Boundary:
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('pure owner imported effectful dependency: ' + fullname)

sys.meta_path.insert(0, Boundary())
importlib.import_module(sys.argv[1])
print('imported ' + sys.argv[1])
"""
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for module in PURE_MODULES:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-c", program, module], cwd=ROOT, env=env,
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"imported {module}\n")
                self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
