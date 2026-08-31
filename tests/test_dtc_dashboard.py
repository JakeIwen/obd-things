import json
from pathlib import Path
import shutil
import subprocess
import unittest


REPO = Path(__file__).resolve().parents[1]
APP = REPO / "projects" / "vehicle_data" / "static" / "app.js"
INDEX = REPO / "projects" / "vehicle_data" / "static" / "index.html"


class DtcDashboardContractTests(unittest.TestCase):
    def test_static_panel_calls_module_evidence_what_it_is(self):
        html = INDEX.read_text()

        self.assertIn("Newest module evidence", html)
        self.assertNotIn("Last completed scan", html)
        self.assertIn("not live", html)
        self.assertIn("DIAGNOSTICS · CACHED ONLY", html)
        self.assertIn("locally armed fixed scan", html)

    @unittest.skipUnless(shutil.which("node"), "node is required for DTC UI test")
    def test_compact_dtc_states_render_without_claiming_live_or_clear(self):
        script = r"""
const fs = require("fs");
const vm = require("vm");

function makeElement(tag = "div", id = "") {
  return {
    tagName: tag.toUpperCase(),
    id,
    dataset: {},
    className: "",
    hidden: false,
    textContent: "",
    children: [],
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = [...nodes]; },
  };
}

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, makeElement("div", id));
  return elements.get(id);
}
global.document = {
  visibilityState: "visible",
  getElementById: element,
  createElement: (tag) => makeElement(tag),
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
  globalThis.dtcDashboardUnderTest = {renderDtcs};
`);

function emptyGroups() {
  return {
    current: [],
    pending: [],
    confirmed_history: [],
    incomplete_only: [],
    other: [],
  };
}

function emptyCounts() {
  return {
    current: 0,
    pending: 0,
    confirmed_history: 0,
    incomplete_only: 0,
    other: 0,
  };
}

function record(overrides) {
  return {
    fca_display: "U0001-00",
    module_name: "Example module",
    module_key: "example",
    logical_bus: "c-can",
    status_flags: ["test_failed"],
    current: true,
    observation_state: "observed_in_latest_success",
    module_availability: "available",
    last_seen_at: "2025-01-02T00:00:00Z",
    description: "Example reviewed DTC meaning",
    description_reviewed: true,
    ...overrides,
  };
}

function module(overrides) {
  return {
    module_key: "example",
    module_name: "Example module",
    logical_bus: "c-can",
    availability: "available",
    result_state: "dtcs_present",
    unavailable_reason: null,
    last_attempt_at: "2025-01-02T00:00:00Z",
    last_success_at: "2025-01-02T00:00:00Z",
    last_success_dtc_count: 1,
    absence_authoritative: true,
    ...overrides,
  };
}

function renderDtcPayload(payload) {
  globalThis.dtcDashboardUnderTest.renderDtcs(payload);
  const root = element("dtc-groups");
  const nodes = [];
  function visit(node) {
    nodes.push(node);
    (node.children || []).forEach(visit);
  }
  visit(root);
  return {
    state: element("dtc-state").textContent,
    newest: element("dtc-last-scan").textContent,
    coverage: element("dtc-module-coverage").textContent,
    note: element("dtc-note").textContent,
    tree: nodes.map((node) => node.textContent).filter(Boolean).join(" | "),
    headings: nodes
      .filter((node) => ["H3", "SUMMARY"].includes(node.tagName))
      .map((node) => node.textContent),
    details: nodes
      .filter((node) => node.className === "dtc-detail")
      .map((node) => node.textContent),
  };
}

const mixedGroups = emptyGroups();
mixedGroups.current = [
  record({
    module_key: "pcm",
    module_name: "Powertrain Control Module",
    module_availability: "unavailable",
    observation_state: "stale_after_unavailable_attempt",
  }),
  record({
    module_key: "bcm",
    module_name: "Body Control Module",
    observation_state: "retained_incompatible_status_mask",
  }),
];
mixedGroups.confirmed_history = [record({
  module_key: "bcm",
  module_name: "Body Control Module",
  current: false,
  status_flags: ["confirmed"],
})];
const mixed = {
  schema_version: 2,
  available: true,
  compact: true,
  groups: mixedGroups,
  group_counts: {
    current: 5,
    pending: 0,
    confirmed_history: 1,
    incomplete_only: 0,
    other: 0,
  },
  group_returned_counts: {
    current: 2,
    pending: 0,
    confirmed_history: 1,
    incomplete_only: 0,
    other: 0,
  },
  groups_truncated: true,
  description_catalog: {
    reviewed_records: 3,
    returned_records: 3,
    detail: "A DTC title is not a diagnosis.",
  },
  coverage: {
    total_modules: 4,
    available_modules: 2,
    unavailable_modules: 1,
    never_scanned_modules: 1,
    modules_status_coverage_incomplete: 1,
    last_attempt_at: "2026-01-02T00:00:00Z",
    last_success_at: "2025-01-02T00:00:00Z",
  },
  modules: [
    module({
      module_key: "pcm",
      module_name: "Powertrain Control Module",
      availability: "unavailable",
      result_state: "unavailable",
      unavailable_reason: "timeout",
      last_attempt_at: "2026-01-02T00:00:00Z",
      absence_authoritative: false,
    }),
    module({
      module_key: "abs",
      module_name: "Antilock Brake System",
      logical_bus: "can-ch",
      availability: "never_scanned",
      result_state: "never_scanned",
      last_attempt_at: null,
      last_success_at: null,
      last_success_dtc_count: null,
      absence_authoritative: false,
    }),
    module({
      module_key: "cluster",
      module_name: "Instrument Cluster",
      result_state: "status_coverage_incomplete",
      last_success_dtc_count: 0,
      absence_authoritative: false,
    }),
    module({module_key: "bcm", module_name: "Body Control Module"}),
  ],
  detail: "Saved results only; this endpoint cannot scan or clear.",
};

const authoritativeGroups = emptyGroups();
const authoritativeZero = {
  available: true,
  groups: authoritativeGroups,
  group_counts: emptyCounts(),
  group_returned_counts: emptyCounts(),
  groups_truncated: false,
  coverage: {
    total_modules: 1,
    available_modules: 1,
    unavailable_modules: 0,
    never_scanned_modules: 0,
    modules_status_coverage_incomplete: 0,
    last_attempt_at: "2026-01-02T00:00:00Z",
    last_success_at: "2026-01-02T00:00:00Z",
  },
  modules: [module({
    result_state: "no_dtcs",
    last_success_dtc_count: 0,
    absence_authoritative: true,
  })],
};

const nonAuthoritativeZero = {
  ...authoritativeZero,
  coverage: {
    ...authoritativeZero.coverage,
    modules_status_coverage_incomplete: 1,
  },
  modules: [module({
    result_state: "status_coverage_incomplete",
    last_success_dtc_count: 0,
    absence_authoritative: false,
  })],
};

const output = {
  sourceUsesReturnedCounts: source.includes("group_returned_counts"),
  sourceUsesTruncated: source.includes("groups_truncated"),
  expectedNewest: new Date(mixed.coverage.last_attempt_at).toLocaleString(),
  oldSuccess: new Date(mixed.coverage.last_success_at).toLocaleString(),
  mixed: renderDtcPayload(mixed),
  authoritative: renderDtcPayload(authoritativeZero),
  nonAuthoritative: renderDtcPayload(nonAuthoritativeZero),
};
process.stdout.write(JSON.stringify(output));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(APP)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)

        self.assertTrue(result["sourceUsesReturnedCounts"])
        self.assertTrue(result["sourceUsesTruncated"])
        self.assertEqual(result["mixed"]["newest"], result["expectedNewest"])
        self.assertNotEqual(result["mixed"]["newest"], result["oldSuccess"])
        self.assertIn("COVERAGE GAPS", result["mixed"]["state"])
        self.assertIn("1 unavailable", result["mixed"]["coverage"])
        self.assertIn("1 never scanned", result["mixed"]["coverage"])
        self.assertIn("1 status coverage incomplete", result["mixed"]["coverage"])
        self.assertTrue(
            any(
        "Current / test-failed when last observed" in heading
                and "showing 2 of 5" in heading
                for heading in result["mixed"]["headings"]
            )
        )
        self.assertTrue(
            any(
                "stale saved state: latest module attempt unavailable" in detail
        and "current / test-failed when this DTC was last observed" in detail
                for detail in result["mixed"]["details"]
            )
        )
        self.assertTrue(
            any(
                "retained saved state: latest successful status-mask coverage was incompatible"
                in detail
                for detail in result["mixed"]["details"]
            )
        )
        self.assertIn(
            "Compact cache: showing 3 of 6 saved records.", result["mixed"]["note"]
        )
        self.assertIn("Example reviewed DTC meaning", result["mixed"]["tree"])
        self.assertIn("A DTC title is not a diagnosis.", result["mixed"]["note"])

        self.assertIn("NO DTCs IN AUTHORITATIVE RESULTS", result["authoritative"]["state"])
        self.assertIn("No DTCs", result["authoritative"]["tree"])
        self.assertIn("authoritative", result["authoritative"]["tree"])

        self.assertIn("COVERAGE INCOMPLETE", result["nonAuthoritative"]["state"])
        non_authoritative_text = result["nonAuthoritative"]["tree"]
        self.assertIn("DTC absence is not established", non_authoritative_text)
        self.assertIn("Status coverage incomplete", non_authoritative_text)
        self.assertNotIn("NO DTC", non_authoritative_text.upper())


if __name__ == "__main__":
    unittest.main()
