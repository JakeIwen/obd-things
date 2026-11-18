import { test } from "node:test";
import assert from "node:assert/strict";
import { HEAT_TOKENS, TILE_HEAT, heatFraction, heatRangeFor, mixHeat } from "../src/tileHeat.helpers.js";

// Why: owner request 2026-11-17: the alternator field numeral shades yellow → orange → red from 60 to 100 %.

const STOPS = ["#ffd84a", "#ff9a3c", "#ff5d4d"];

test("only the alternator field has a heat range, 60 to 100", () => {
  assert.deepEqual(Object.keys(TILE_HEAT), ["generator.field_duty"]);
  assert.deepEqual(heatRangeFor("generator.field_duty"), { from: 60, to: 100 });
  assert.equal(heatRangeFor("engine.rpm"), null);
  assert.equal(heatRangeFor("toString"), null, "inherited keys are not metrics");
  assert.equal(HEAT_TOKENS.length, 3);
});

test("the numeral keeps its colour below 60 and shades from 60, saturating at 100", () => {
  const f = (v) => heatFraction("generator.field_duty", v);
  assert.equal(f(59.9), null);
  assert.equal(f(60), 0);
  assert.equal(f(80), 0.5);
  assert.equal(f(100), 1);
  assert.equal(f(120), 1);
  assert.equal(f(null), null);
  assert.equal(f(NaN), null);
  assert.equal(heatFraction("engine.rpm", 5000), null);
});

test("yellow at the start, orange midway, red at the top, blended between", () => {
  assert.equal(mixHeat(STOPS, 0), "rgb(255, 216, 74)");
  assert.equal(mixHeat(STOPS, 0.5), "rgb(255, 154, 60)");
  assert.equal(mixHeat(STOPS, 1), "rgb(255, 93, 77)");
  assert.equal(mixHeat(STOPS, 0.25), "rgb(255, 185, 67)");
  assert.equal(mixHeat(["#fd4", "#f93", "#f54"], 0), "rgb(255, 221, 68)", "short hex");
});

test("an unreadable token falls back to the normal colour", () => {
  assert.equal(mixHeat(["", STOPS[1], STOPS[2]], 0.5), "");
  assert.equal(mixHeat(["rgb(1,2,3)", STOPS[1], STOPS[2]], 0.5), "");
  assert.equal(mixHeat(STOPS.slice(0, 2), 0.5), "");
  assert.equal(mixHeat(STOPS, NaN), "");
});
