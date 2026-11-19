import { test } from "node:test";
import assert from "node:assert/strict";
import { CANDIDATE_READING_METRICS, candidateReadingModel } from "../src/candidateReading.helpers.js";

const IAT = "engine.intake_air_temperature";
const AC = "hvac.ac_high_side_pressure";
const rec = (value, extra = {}) => ({ available: true, stale: false, value, unit: undefined, ...extra });

test("a fresh candidate reading shows its value and unit", () => {
  assert.deepEqual(candidateReadingModel(IAT, rec(96.8, { unit: "°F" })), { kind: "live", text: "97", unit: "°F" });
  assert.deepEqual(candidateReadingModel(AC, rec(215.4)), { kind: "live", text: "215", unit: "psi" });
});

test("stale, unavailable, missing and non-numeric readings are dashes, never retained", () => {
  const off = { kind: "off", text: "—", unit: "" };
  for (const name of CANDIDATE_READING_METRICS) {
    assert.deepEqual(candidateReadingModel(name, rec(90, { stale: true })), off);
    assert.deepEqual(candidateReadingModel(name, rec(90, { available: false })), off);
    assert.deepEqual(candidateReadingModel(name, null), off);
    assert.deepEqual(candidateReadingModel(name, rec(NaN)), off);
    assert.deepEqual(candidateReadingModel(name, rec("90")), off);
  }
});
