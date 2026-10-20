import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { COMPASS, compassDisplay, compassLetters } from "../src/compass.js";
import { fmtValue, fmtUnit } from "../src/format.js";
import * as store from "../src/store.js";

test("all eight directions render letters only, including north at zero", () => {
  assert.deepEqual(Array.from({ length: 8 }, (_, n) => compassLetters(n)),
    ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]);
  assert.equal(fmtValue(COMPASS.metric, 7), "NW");
  assert.equal(fmtUnit(COMPASS.metric, "direction"), "");
  for (const value of [null, undefined, false, "N", "0", -1, 8, .5, NaN]) {
    assert.equal(compassLetters(value), "");
  }
});

test("retained and disconnected directions stay visible but dim", () => {
  assert.deepEqual(compassDisplay({kind: "live", value: 0}, "live"), {letters: "N", stale: false});
  assert.deepEqual(compassDisplay({kind: "held", value: 6}, "live"), {letters: "W", stale: true});
  assert.deepEqual(compassDisplay({kind: "live", value: 7}, "unavailable"), {letters: "NW", stale: true});
  assert.deepEqual(compassDisplay(null, "live"), {letters: "", stale: true});
});

test("public store retains compass across expiry and a restored last-recorded baseline", () => {
  const body = JSON.parse(readFileSync(new URL("./fixtures/snapshot.json", import.meta.url)));
  const observed = "2026-10-14T20:00:00Z";
  const source = {name: COMPASS.source, quality: "verified", bus: "b-can"};
  body.catalog.push({name: COMPASS.metric, unit: "direction", value_type: "integer",
    minimum: 0, maximum: 7, stale_after_seconds: COMPASS.stale_after_seconds, sources: [source]});
  body.metrics[COMPASS.metric] = {metric: COMPASS.metric, available: true, unit: "direction",
    value: 2, quality: "verified", source: COMPASS.source, bus: "b-can",
    observed_at: observed, age_ms: 0, stale: false};
  store.applyBaseline(body, 0, 1000);
  const wall = Date.parse(observed) + 360000;
  assert.equal(store.observationState(COMPASS.metric, 1000, wall).kind, "live");
  store.tick(361000);
  const held = store.observationState(COMPASS.metric, 361000, wall);
  assert.equal(held.kind, "held");
  assert.equal(held.value, 2);
  assert.equal(held.retained.observed_at, observed);
  body.metrics[COMPASS.metric] = {metric: COMPASS.metric, unit: "direction", available: false,
    last_recorded: {value: 2, unit: "direction", source: COMPASS.source,
      quality: "verified", observed_at: observed}};
  store.applyBaseline(body, 0, 362000);
  assert.equal(store.observationState(COMPASS.metric, 362000, wall).kind, "held");
});
