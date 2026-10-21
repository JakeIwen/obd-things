"""The advisory rules on fixture trees: fan-in-size, shim-stale, effects-in-rules, shell-size.
They are reported and never fail a run, even with a layout baseline."""
from layout_fixture import LayoutCase, python_module


class FanInSize(LayoutCase):
    def test_big_module_with_three_importers(self):
        self.write({"core.py": python_module(30), "small.py": python_module(5),
                    "a.py": "import core, small\n", "b.py": "import core, small\n", "c.py": "import core, small\n"})
        self.config({}, file_lines=20)
        self.assertEqual(self.paths(self.check(), "fan-in-size"), ["core.py"])

    def test_test_importers_do_not_count(self):
        self.write({"core.py": python_module(30), "a.py": "import core\n", "b.py": "import core\n",
                    "tests/test_core.py": "import core\n"})
        self.config({}, file_lines=20)
        self.assertEqual(self.found(self.check(), "fan-in-size"), [])


class ShimStale(LayoutCase):
    def test_reexport_with_no_importers_and_no_contract(self):
        self.write({"history/__init__.py": "", "history/service.py": "def main():\n    return 0\n",
                    "historian.py": '"""Old path."""\nfrom history.service import main\n__all__ = ["main"]\n',
                    "web/old.mjs": "export { serve } from './serve.mjs'\n", "web/serve.mjs": "export const serve = 1\n"})
        self.assertEqual(self.paths(self.check(), "shim-stale"), ["historian.py", "web/old.mjs"])

    def test_imported_or_listed_shims_are_supported_interfaces(self):
        self.write({"history/__init__.py": "", "history/service.py": "def main():\n    return 0\n",
                    "historian.py": "from history.service import main\n", "early.py": "from history.service import main\n",
                    "uses.py": "import historian\n",
                    "AGENTS.md": "# Repo\n\n## Contracts\n\n| Path | Caller | Owner |\n|---|---|---|\n"
                                 "| `early.py` | cron on the Pi | history/service.py |\n"})
        self.assertEqual(self.found(self.check(), "shim-stale"), [])


    def test_shims_named_by_other_files_are_not_stale(self):
        self.write({"lib/__init__.py": "", "lib/report.py": "def main():\n    return 0\n",
                    "tools/report_cli.py": "from lib.report import main\n",
                    "lib/run.py": "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'tools.report_cli'])\n",
                    "apps/catalog/asset_catalog.py": "from apps.catalog.core import *\n",
                    "apps/catalog/core.py": "", "checks/test_assets.py": "from asset_catalog import Asset\n"})
        self.assertEqual(self.found(self.check(), "shim-stale"), [])


class EffectsInRules(LayoutCase):
    def test_model_and_rules_modules_that_touch_the_world(self):
        self.write({"van/models.py": "import subprocess\n", "van/wake_state.py": "from datetime import datetime\n"
                    "def now():\n    return datetime.now()\n", "van/pure_view.py": "def view(x):\n    return x\n",
                    "web/status.helpers.js": "export const get = () => fetch('/x')\n",
                    "van/store.py": "import sqlite3\n"})
        data = self.check()
        self.assertEqual(self.paths(data, "effects-in-rules"),
                         ["van/models.py", "van/wake_state.py", "web/status.helpers.js"])
        self.assertTrue(all(f["treatment"] == "advisory" for f in self.found(data, "effects-in-rules")))

    def test_writes_count_but_reads_do_not(self):
        self.write({"a_rules.py": "def load(p):\n    return open(p).read()\n",
                    "b_rules.py": "def save(p):\n    open(p, 'w').write('x')\n"})
        self.assertEqual(self.paths(self.check(), "effects-in-rules"), ["b_rules.py"])


class ShellSize(LayoutCase):
    def test_long_shell_script_is_a_review_trigger(self):
        self.write({"scripts/umount_disks.sh": "#!/bin/bash\n" + "echo x\n" * 210,
                    "scripts/short.sh": "#!/bin/bash\necho y\n"})
        found = self.found(self.check(), "shell-size")
        self.assertEqual([f["path"] for f in found], ["scripts/umount_disks.sh"])
        self.assertIn("211 lines", found[0]["message"])

    def test_threshold_is_configurable_and_never_fails_with_a_baseline(self):
        self.write({"scripts/a.sh": "#!/bin/bash\n" + "echo x\n" * 20})
        self.config({"thresholds": {"shellLines": 10}})
        self.baseline_file()
        self.commit()
        self.assertEqual(self.run_checker("rebaseline").returncode, 0)
        data = self.check(expect=0)
        self.assertEqual([f["status"] for f in self.found(data, "shell-size")], ["advisory"])
