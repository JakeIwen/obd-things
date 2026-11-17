"""The installer's measure and budget-check steps, end to end through the worker and the fake Claude."""

import contextlib
import io
import json
import unittest

from projects.vehicle_data.research_cli_install import Abort, Installer
from tests.research_support import WorkerRig
from tests.test_research_cli_install import Client


class MaintenanceStepTests(unittest.TestCase):
    def setUp(self):
        self.rig = WorkerRig()
        self.installer = Installer(cli_root=self.rig.cli_root, client=Client(self.rig.worker), poll=.05)

    def tearDown(self):
        self.rig.close()

    def run_check(self, purpose, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.installer.run_check(purpose, **kwargs)
        return code, out.getvalue()

    def test_measure_runs_a_report_and_a_resumed_follow_up_and_saves_the_figures(self):
        code, output = self.run_check("measure")
        self.assertEqual(code, 0)
        saved = json.loads((self.rig.cli_root / "measurement.json").read_text())
        self.assertEqual(saved["result"], "measured")
        self.assertEqual(saved["cli_digest"], self.rig.digest)
        self.assertEqual(set(saved["figures"]), {"report", "follow_up"})
        self.assertTrue(all(part["status"] == "complete" for part in saved["figures"].values()))
        self.assertIn("memory block for research_cli_policy.json", output)
        turns = [call["argv"] for call in self.rig.calls() if "--print" in call["argv"]]
        self.assertEqual(len(turns), 2)
        self.assertIn("--resume", turns[1])
        self.assertIsNone(self.rig.worker.table.current)  # the reservation ended

    def test_an_incomplete_measurement_records_nothing(self):
        self.rig.scenario({"mode": "exit_error"})
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Abort):
            self.installer.run_check("measure")
        self.assertFalse((self.rig.cli_root / "measurement.json").exists())
        self.assertIsNone(self.rig.worker.table.current)

    def test_budget_check_verdicts(self):
        cases = (({"mode": "budget"}, "enforced"), ({"mode": "ok", "cost": 0.5}, "not_enforced"),
                 ({"mode": "ok", "cost": 0.001}, "inconclusive"))
        for scenario, verdict in cases:
            with self.subTest(verdict):
                self.rig.scenario(scenario)
                code, output = self.run_check("budget_check")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output[output.index("{"):])["result"], verdict)

    def test_dry_run_starts_no_turn(self):
        dry = Installer(cli_root=self.rig.cli_root, client=Client(self.rig.worker), dry_run=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dry.run_check("measure"), 0)
        self.assertEqual(self.rig.calls(), [])


if __name__ == "__main__":
    unittest.main()
