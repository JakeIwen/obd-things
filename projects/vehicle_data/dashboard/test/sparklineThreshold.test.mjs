import { test } from "node:test";
import assert from "node:assert/strict";
import { SPARK_RED_ABOVE, isAboveRed, redAboveFor, splitByThreshold } from "../src/sparkline.helpers.js";
import { SPARK_METRICS } from "../src/app/rings.js";

const pts = (...vs) => vs.map((v, x) => ({ x, v }));
const shape = (runs) => runs.map((r) => (r.red ? "red:" : "ok:") + r.points.map((p) => p.x + "/" + p.v).join(","));

test("the alternator field is red above 80 and nothing else has a threshold", () => {
  assert.equal(redAboveFor("generator.field_duty"), 80);
  assert.equal(redAboveFor("engine.rpm"), null);
  assert.equal(redAboveFor("toString"), null, "inherited keys are not metrics");
  assert.deepEqual(Object.keys(SPARK_RED_ABOVE), ["generator.field_duty"]);
});

test("the numeral rule is strictly above the threshold and only for numbers", () => {
  assert.equal(isAboveRed("generator.field_duty", 80), false);
  assert.equal(isAboveRed("generator.field_duty", 80.1), true);
  assert.equal(isAboveRed("generator.field_duty", 100), true);
  assert.equal(isAboveRed("generator.field_duty", null), false);
  assert.equal(isAboveRed("generator.field_duty", NaN), false);
  assert.equal(isAboveRed("engine.rpm", 9000), false);
});

test("without a threshold the line is one normal run", () => {
  assert.deepEqual(shape(splitByThreshold(pts(10, 90, 20), null)), ["ok:0/10,1/90,2/20"]);
  assert.deepEqual(splitByThreshold([], 80), []);
});

test("the line turns red exactly where it crosses the threshold", () => {
  const runs = splitByThreshold(pts(60, 100, 100, 60), 80);
  assert.deepEqual(shape(runs), ["ok:0/60,0.5/80", "red:0.5/80,1/100,2/100,2.5/80", "ok:2.5/80,3/60"]);
});

test("a run that stays on one side keeps one colour, touching the threshold is not red", () => {
  assert.deepEqual(shape(splitByThreshold(pts(10, 40, 80), 80)), ["ok:0/10,1/40,2/80"]);
  assert.deepEqual(shape(splitByThreshold(pts(90, 95, 85), 80)), ["red:0/90,1/95,2/85"]);
  assert.deepEqual(shape(splitByThreshold(pts(80, 90), 80)), ["red:0/80,1/90"]);
  assert.deepEqual(shape(splitByThreshold(pts(95), 80)), ["red:0/95"]);
});

test("the alternator field keeps a sparkline ring, so its tile can draw the red-above trace", () => {
  assert.ok(SPARK_METRICS.includes("generator.field_duty"));
});
