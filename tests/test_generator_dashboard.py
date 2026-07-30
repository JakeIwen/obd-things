from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "projects" / "vehicle_data" / "static"
APP = STATIC / "app.js"
INDEX = STATIC / "index.html"
PROFILES = STATIC / "profiles.js"
STYLE = STATIC / "style.css"


class GeneratorDashboardAssetTests(unittest.TestCase):
    def test_charging_card_is_explicit_and_has_no_danger_style(self):
        index = INDEX.read_text()
        style = STYLE.read_text()

        self.assertIn("Generator field duty", index)
        self.assertIn("high commanded charging effort", index)
        self.assertIn("not alternator current or alternator", index)
        self.assertIn("temperature", index)
        self.assertIn("does not infer a thermal danger threshold", index)
        self.assertIn('data-widget="charging"', index)
        self.assertNotIn("var(--danger)", style)

    @unittest.skipUnless(shutil.which("node"), "node is required for profile test")
    def test_charging_widget_is_in_requested_profile_defaults(self):
        script = r"""
global.window = {
  localStorage: {
    getItem: () => null,
    setItem: () => undefined,
  },
};
require(process.argv[1]);
const manager = window.VanDashboardProfiles;
process.stdout.write(JSON.stringify({
  overview: manager.profiles.overview.widgets,
  parked: manager.profiles.parked.widgets,
  driving: manager.profiles.driving.widgets,
  diagnostics: manager.profiles.diagnostics.widgets,
  custom: manager.defaultSettings().customWidgets,
}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(PROFILES)],
            check=True,
            capture_output=True,
            text=True,
        )
        profiles = json.loads(completed.stdout)

        for name in ("overview", "driving", "diagnostics", "custom"):
            with self.subTest(profile=name):
                self.assertIn("charging", profiles[name])
        self.assertNotIn("charging", profiles["parked"])

    @unittest.skipUnless(shutil.which("node"), "node is required for browser JS test")
    def test_generator_rendering_preserves_value_and_exposes_runtime_state(self):
        script = r"""
const fs = require("fs");
const vm = require("vm");
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      dataset: {},
      hidden: false,
      textContent: "",
    });
  }
  return elements.get(id);
}
global.document = {
  visibilityState: "visible",
  getElementById: element,
};
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
  globalThis.generatorDashboard = {
    featuredMetricNames,
    renderCharging,
  };
`);
const dashboard = global.generatorDashboard;
const definition = {
  name: "generator.field_duty",
  unit: "%",
  stale_after_seconds: 5,
  sources: [{
    name: "pcm.did.01a1",
    bus: "c-can",
    quality: "observed_alfa_scale",
    acquisition_class: "physical_read_data_by_identifier",
  }],
};
const liveMetric = {
  available: true,
  stale: false,
  value: 100.008,
  unit: "%",
  quality: "observed_alfa_scale",
  source: "pcm.did.01a1",
  acquisition: "physical_read_data_by_identifier",
  interface_mode: "armed_diagnostic",
  age_ms: 125,
};
dashboard.renderCharging(
  {interface: {adapter_present: true, up: true, listen_only: false}},
  [definition],
  {"generator.field_duty": liveMetric},
);
function capture() {
  return {
    value: element("charging-generator-field-duty").textContent,
    unit: element("charging-generator-field-duty-unit").textContent,
    status: element("charging-generator-field-duty-status").textContent,
    state: element("charging-generator-field-duty-inactive-reason").textContent,
    quality: element("charging-generator-field-duty-quality").textContent,
    source: element("charging-generator-field-duty-source").textContent,
    acquisition: element("charging-generator-field-duty-acquisition").textContent,
    interfaceMode: element("charging-generator-field-duty-interface-mode").textContent,
    detail: element("charging-generator-field-duty-detail").textContent,
    cardState: element("charging-generator-field-duty-card").dataset.state,
    summary: element("charging-state").textContent,
  };
}
const live = capture();

dashboard.renderCharging(
  {interface: {adapter_present: true, up: true, listen_only: true}},
  [definition],
  {
    "generator.field_duty": {
      available: false,
      reason: "session_required",
      detail: "PCM 01A1 did not answer without a session change.",
      interface_mode: "listen_only",
    },
  },
);
const inactive = capture();

dashboard.renderCharging(
  {interface: {adapter_present: true, up: true, listen_only: true}},
  [definition],
  {
    "generator.field_duty": {
      available: false,
      reason: "restoration_failed",
      detail: "Final listen-only restoration could not be verified.",
      interface_mode: "unknown",
    },
  },
);
const restorationFailed = capture();

dashboard.renderCharging(
  {interface: {adapter_present: true, up: true, listen_only: true}},
  [definition],
  {
    "generator.field_duty": {
      ...liveMetric,
      stale: true,
      age_ms: 5001,
    },
  },
);
const stale = capture();

dashboard.renderCharging(
  {interface: {adapter_present: true, up: true, listen_only: true}},
  [definition],
  {
    "generator.field_duty": {
      ...liveMetric,
      last_acquisition_error: {
        reason: "can_busy",
        detail: "another participating CAN operation owns can0",
      },
    },
  },
);
const cachedError = capture();

dashboard.renderCharging({}, [], {});
const pending = capture();
process.stdout.write(JSON.stringify({
  live,
  inactive,
  restorationFailed,
  stale,
  cachedError,
  pending,
  featured: [...dashboard.featuredMetricNames([definition])],
}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(APP)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rendered = json.loads(completed.stdout)

        live = rendered["live"]
        self.assertEqual(live["value"], "100.008")
        self.assertEqual(live["unit"], "%")
        self.assertIn("OBSERVED ALFA SCALE", live["status"])
        self.assertEqual(live["state"], "Live")
        self.assertEqual(live["quality"], "OBSERVED ALFA SCALE")
        self.assertEqual(live["source"], "pcm.did.01a1")
        self.assertEqual(
            live["acquisition"], "physical read data by identifier"
        )
        self.assertEqual(
            live["interfaceMode"], "Armed diagnostic · observation"
        )
        self.assertEqual(live["cardState"], "observed_alfa_scale")

        inactive = rendered["inactive"]
        self.assertEqual(inactive["value"], "—")
        self.assertEqual(inactive["status"], "session required")
        self.assertEqual(inactive["state"], "session required")
        self.assertEqual(
            inactive["interfaceMode"], "Listen-only · acquisition result"
        )
        self.assertEqual(inactive["source"], "pcm.did.01a1 · registered")
        self.assertIn("did not answer", inactive["detail"])

        restoration_failed = rendered["restorationFailed"]
        self.assertEqual(restoration_failed["status"], "restoration failed")
        self.assertEqual(restoration_failed["state"], "restoration failed")
        self.assertEqual(
            restoration_failed["interfaceMode"],
            "Unknown · acquisition result",
        )
        self.assertIn("could not be verified", restoration_failed["detail"])

        stale = rendered["stale"]
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["state"], "stale")
        self.assertEqual(stale["cardState"], "stale")
        self.assertIn("freshness window", stale["detail"])

        cached_error = rendered["cachedError"]
        self.assertEqual(cached_error["value"], "—")
        self.assertEqual(cached_error["state"], "can busy")
        self.assertEqual(cached_error["cardState"], "unavailable")
        self.assertIn("owns can0", cached_error["detail"])

        self.assertEqual(rendered["pending"]["status"], "Mapping pending")
        self.assertEqual(rendered["pending"]["state"], "mapping pending")
        self.assertIn("generator.field_duty", rendered["featured"])


if __name__ == "__main__":
    unittest.main()
