import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { buildWarningContext } from "../src/warningContext.js";
import { buildWarningCards } from "../src/warnings.js";

const structural = (card) => Object.fromEntries([
  "id", "tier", "metric", "title", "state", "episodeId", "rule", "category",
  "severity", "group", "value", "unit", "since",
].map((key) => [key, card[key]]));
const now = Date.parse("2026-10-07T12:00:00Z");

function compare(health) {
  const core = buildWarningContext(health);
  const full = buildWarningCards(health, now);
  assert.deepEqual(core.warnings.map(structural), full.warnings.map(structural));
  for (const key of ["critical", "warning", "notice", "open", "unconfirmed"]) {
    assert.equal(core.counts[key], full.counts[key], key);
  }
}

test("lazy warning prose and core indicators share the same structural decisions", () => {
  compare(JSON.parse(readFileSync(new URL("./fixtures/health-lite.json", import.meta.url), "utf8")));
  compare(null);
  compare({ available: false });
  const rules = [
    "engine_oil_pressure_low", "engine_coolant_hot", "transmission_oil_hot",
    "battery_voltage_low", "tire_pressure_fl_low", "tire_pressure_pair_asymmetry",
    "custom_threshold", "unrecognized_rule",
  ];
  for (const rule of rules) {
    for (const state of ["watch", "warning", "normal", "recovering", "unavailable"]) {
      for (const tier of [0, 1, 2]) {
        const assessment = {
          rule, state, tier, category: "vehicle_health", severity: "critical",
          metric: "battery.voltage", direction: "low", title: "Supplied title",
          reason: "Reading below its usual value", current: { value: 11.5, unit: "V" },
          evaluated_at: "2026-10-07T12:00:00Z", regime: "engine_off:stationary:rpm_off:cold",
        };
        compare({ available: true, assessments: [assessment], episodes: { active: [{
          id: 7, rule, status: "open", opened_at: "2026-10-07T11:00:00Z",
          latest_assessment: assessment,
        }] } });
      }
    }
  }
});
