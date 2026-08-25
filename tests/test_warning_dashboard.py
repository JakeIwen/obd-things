from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


REPO = Path(__file__).resolve().parents[1]
APP = REPO / "projects" / "vehicle_data" / "static" / "app.js"


class WarningDashboardTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required for warning UI test")
    def test_only_real_watches_or_open_episodes_become_cards(self):
        script = r"""
const fs = require("fs");
const vm = require("vm");

global.document = {
  visibilityState: "visible",
  getElementById: () => ({dataset: {}, textContent: ""}),
};
global.window = {
  VanDashboardProfiles: {
    loadSettings: () => ({selected: "overview", customWidgets: []}),
  },
};
global.performance = {now: () => 1000};

const source = fs.readFileSync(process.argv[1], "utf8");
const definitionsOnly = source.slice(
  0,
  source.indexOf('\nbyId("refresh").addEventListener'),
);
vm.runInThisContext(definitionsOnly + `
  globalThis.warningDashboardUnderTest = {selectWarningCards};
`);

const assessments = [
  {rule: "normal", state: "normal"},
  {
    rule: "no-data",
    state: "unavailable",
    persistence: {observed: 0, required: 10},
  },
  {
    rule: "training",
    state: "insufficient_history",
    persistence: {observed: 0, required: 10},
  },
  {
    rule: "watch",
    state: "watch",
    persistence: {observed: 1, required: 10},
  },
  {
    rule: "warning",
    state: "warning",
    persistence: {observed: 10, required: 10},
  },
];
const openEpisode = [{
  rule: "open-but-inconclusive",
  state: "unavailable",
  episode_id: 7,
  persistence: {observed: 0, required: 10},
}];

process.stdout.write(JSON.stringify({
  fallback: warningDashboardUnderTest
    .selectWarningCards([], assessments)
    .map((item) => item.rule),
  persisted: warningDashboardUnderTest
    .selectWarningCards(openEpisode, assessments)
    .map((item) => item.rule),
}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(APP)],
            check=True,
            capture_output=True,
            text=True,
        )
        rendered = json.loads(completed.stdout)

        self.assertEqual(rendered["fallback"], ["watch", "warning"])
        self.assertEqual(rendered["persisted"], ["open-but-inconclusive"])


if __name__ == "__main__":
    unittest.main()
