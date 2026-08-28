import json
from pathlib import Path
import shutil
import subprocess
import unittest


REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "projects" / "vehicle_data" / "static"
APP = STATIC / "app.js"
INDEX = STATIC / "index.html"


class OdometerDashboardTests(unittest.TestCase):
    def test_card_is_visibly_starred_and_discloses_counterexample(self):
        index = INDEX.read_text()
        self.assertIn("ODOMETER*", index)
        self.assertIn("11.14 mi below", index)
        self.assertIn("further validation required", index)

    @unittest.skipUnless(shutil.which("node"), "node is required for browser JS test")
    def test_candidate_value_is_visible_but_never_driver_qualified(self):
        script = r"""
const fs = require("fs");
const vm = require("vm");
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {dataset: {}, hidden: false, textContent: ""});
  }
  return elements.get(id);
}
global.document = {visibilityState: "visible", getElementById: element};
global.window = {
  VanDashboardProfiles: {
    loadSettings: () => ({selected: "overview", customWidgets: []}),
  },
};
const source = fs.readFileSync(process.argv[1], "utf8");
const definitionsOnly = source.slice(
  0,
  source.indexOf('\nbyId("refresh").addEventListener'),
);
vm.runInThisContext(definitionsOnly + `
  globalThis.odometerDashboard = {renderDrive};
`);
const definition = {
  name: "vehicle.odometer",
  unit: "mi",
  stale_after_seconds: 15,
  sources: [{name: "ics.did.2001", quality: "candidate"}],
};
odometerDashboard.renderDrive(
  {},
  [definition],
  {"vehicle.odometer": {
    available: true,
    stale: false,
    value: 53191.86,
    unit: "mi",
    quality: "candidate",
    source: "ics.did.2001",
    age_ms: 125,
  }},
);
process.stdout.write(JSON.stringify({
  value: element("drive-odometer").textContent,
  unit: element("drive-odometer-unit").textContent,
  status: element("drive-odometer-status").textContent,
  state: element("drive-odometer-card").dataset.state,
}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(APP)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rendered = json.loads(completed.stdout)
        self.assertEqual(rendered["value"], "53191.9")
        self.assertEqual(rendered["unit"], "mi")
        self.assertEqual(rendered["state"], "candidate")
        self.assertIn("CANDIDATE", rendered["status"])
        self.assertIn("validation required", rendered["status"])


if __name__ == "__main__":
    unittest.main()
