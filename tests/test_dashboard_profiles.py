from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


REPO = Path(__file__).resolve().parents[1]
PROFILES = REPO / "projects" / "vehicle_data" / "static" / "profiles.js"


@unittest.skipUnless(shutil.which("node"), "node is required for browser JS test")
class DashboardProfileTests(unittest.TestCase):
    def test_automatic_driving_profile_requires_fresh_verified_state(self):
        script = """
global.window = {
  localStorage: {
    getItem: () => null,
    setItem: () => undefined,
  },
};
require(process.argv[1]);
const resolve = window.VanDashboardProfiles.resolve;
const settings = {selected: "auto"};
const inputs = [
  {state: "running", confidence: "observed", age_ms: 10},
  {state: "moving", confidence: "inferred", age_ms: 10},
  {state: "ignition_on", confidence: "stale", age_ms: 10},
  {state: "ignition_on", confidence: "verified"},
  {state: "ignition_on", confidence: "verified", age_ms: null},
  {state: "ignition_on", confidence: "verified", age_ms: -1},
  {state: "ignition_on", confidence: "verified", age_ms: 3001},
  {state: "ignition_on", confidence: "verified", age_ms: false},
  {state: "ignition_on", confidence: "verified", age_ms: "0"},
  {state: "ignition_on", confidence: "verified", age_ms: 0},
  {state: "running", confidence: "verified", age_ms: 3000},
  {state: "asleep", confidence: "inferred"},
  {state: "asleep", confidence: "inferred", age_ms: 10},
  {state: "parked", confidence: "inferred", age_ms: false},
];
process.stdout.write(JSON.stringify(inputs.map((state) => resolve(settings, state))));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(PROFILES)],
            check=True,
            capture_output=True,
            text=True,
        )
        resolved = json.loads(completed.stdout)

        self.assertEqual(
            [profile["id"] for profile in resolved],
            [
                "overview",
                "overview",
                "overview",
                "overview",
                "overview",
                "overview",
                "overview",
                "overview",
                "overview",
                "driving",
                "driving",
                "overview",
                "parked",
                "overview",
            ],
        )
        self.assertIn("observed", resolved[0]["reason"])
        self.assertIn("verified but stale", resolved[3]["reason"])


if __name__ == "__main__":
    unittest.main()
