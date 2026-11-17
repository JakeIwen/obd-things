import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { FUEL_LEVEL, FUEL_RANGE, tankValueText, tankRangeText, tankIsLive } from "../src/fuelLevel.js";
import { fmtValue } from "../src/format.js";
import * as store from "../src/store.js";

test("a fresh or retained record reads as an integer percent with the candidate tilde", () => {
  assert.equal(tankValueText({ available: true, stale: false, value: 49 }, null), "~49");
  assert.equal(tankValueText({ available: false }, { kind: "held", value: 48 }), "~48");
  assert.equal(tankValueText({ available: true, stale: true, value: 49 }, { kind: "held", value: 49 }), "~49");
  assert.equal(fmtValue(FUEL_LEVEL.metric, 49), "49");
});

test("values outside the 1-100 contract and missing readings render nothing", () => {
  for (const value of [0, 101, 49.5, "49", null, undefined, NaN]) {
    assert.equal(tankValueText({ available: true, stale: false, value }, null), "");
  }
  assert.equal(tankValueText(null, null), "");
  assert.equal(tankValueText({ available: false }, { kind: "off", value: null }), "");
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

test("range line shows miles to empty and marks a retained tank or range reading once", () => {
  const live = (value) => ({ available: true, stale: false, value });
  const held = (value) => [{ available: false }, { kind: "held", value }];
  assert.equal(tankRangeText(live(49), null, live(126), null), "126 mi to empty");
  assert.equal(tankRangeText(null, null, live(126), null), "126 mi to empty");
  assert.equal(tankRangeText(...held(49), ...held(126)), "126 mi to empty (last)");
  assert.equal(tankRangeText(live(49), null, ...held(126)), "126 mi to empty (last)");
  assert.equal(tankRangeText(...held(49), live(126), null), "126 mi to empty (last)");
  assert.equal(tankRangeText(live(49), null, live(0), null), "0 mi to empty");
  assert.equal(tankRangeText(live(49), null, live(2047), null), "");
  assert.equal(tankRangeText(live(49), null, null, null), "");
});

test("tank liveness follows the level record only", () => {
  assert.equal(tankIsLive({ available: true, stale: false, value: 49 }, null), true);
  assert.equal(tankIsLive({ available: false }, { kind: "held", value: 49 }), false);
  assert.equal(tankIsLive(null, null), false);
});

test("public store retains the candidate distance to empty across expiry", () => {
  const body = JSON.parse(readFileSync(new URL("./fixtures/snapshot.json", import.meta.url)));
  const observed = "2026-11-15T20:00:00Z";
  body.catalog.push({ name: FUEL_RANGE.metric, unit: "mi", value_type: "integer", minimum: 0, maximum: 2046,
    stale_after_seconds: FUEL_RANGE.stale_after_seconds,
    sources: [{ name: FUEL_RANGE.source, quality: "candidate", bus: "b-can" }] });
  body.metrics[FUEL_RANGE.metric] = { metric: FUEL_RANGE.metric, available: true, unit: "mi", value: 126,
    quality: "candidate", source: FUEL_RANGE.source, bus: "b-can", observed_at: observed, age_ms: 0, stale: false };
  store.applyBaseline(body, 0, 1000);
  store.tick(361000);
  const held = store.observationState(FUEL_RANGE.metric, 361000, Date.parse(observed) + 360000);
  assert.equal(held.kind, "held");
  assert.equal(held.value, 126);
  assert.equal(fmtValue(FUEL_RANGE.metric, 126), "126");
});
