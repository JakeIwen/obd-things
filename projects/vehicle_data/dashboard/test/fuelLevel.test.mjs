import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { FUEL_LEVEL, fuelLevelText } from "../src/fuelLevel.js";
import { fmtValue } from "../src/format.js";
import * as store from "../src/store.js";

test("a fresh record reads as an integer percent and a retained one is marked last", () => {
  assert.equal(fuelLevelText({ available: true, stale: false, value: 49 }, null), "tank ~49%");
  assert.equal(fuelLevelText({ available: false }, { kind: "held", value: 48 }), "tank ~48% (last)");
  assert.equal(fuelLevelText({ available: true, stale: true, value: 49 }, { kind: "held", value: 49 }), "tank ~49% (last)");
  assert.equal(fmtValue(FUEL_LEVEL.metric, 49), "49");
});

test("values outside the 1-100 contract and missing readings render nothing", () => {
  for (const value of [0, 101, 49.5, "49", null, undefined, NaN]) {
    assert.equal(fuelLevelText({ available: true, stale: false, value }, null), "");
  }
  assert.equal(fuelLevelText(null, null), "");
  assert.equal(fuelLevelText({ available: false }, { kind: "off", value: null }), "");
});

test("public store retains the candidate tank level across expiry", () => {
  const body = JSON.parse(readFileSync(new URL("./fixtures/snapshot.json", import.meta.url)));
  const observed = "2026-11-15T20:00:00Z";
  const source = { name: FUEL_LEVEL.source, quality: "candidate", bus: "b-can" };
  body.catalog.push({ name: FUEL_LEVEL.metric, unit: "%", value_type: "integer", minimum: 1, maximum: 100,
    stale_after_seconds: FUEL_LEVEL.stale_after_seconds, sources: [source] });
  body.metrics[FUEL_LEVEL.metric] = { metric: FUEL_LEVEL.metric, available: true, unit: "%", value: 48,
    quality: "candidate", source: FUEL_LEVEL.source, bus: "b-can", observed_at: observed, age_ms: 0, stale: false };
  store.applyBaseline(body, 0, 1000);
  const wall = Date.parse(observed) + 360000;
  store.tick(361000);
  const held = store.observationState(FUEL_LEVEL.metric, 361000, wall);
  assert.equal(held.kind, "held");
  assert.equal(held.value, 48);
  body.metrics[FUEL_LEVEL.metric] = { metric: FUEL_LEVEL.metric, unit: "%", available: false,
    last_recorded: { value: 48, unit: "%", source: FUEL_LEVEL.source, quality: "candidate", observed_at: observed } };
  store.applyBaseline(body, 0, 362000);
  assert.equal(store.observationState(FUEL_LEVEL.metric, 362000, wall).kind, "held");
});
