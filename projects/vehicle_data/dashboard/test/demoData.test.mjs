import test from "node:test";
import assert from "node:assert/strict";

import { buildDemoScene, validateDemoScene } from "../tools/demo_data.mjs";

const NOW = Date.UTC(2026, 9, 21, 19, 30);
const withMetric = (scene, name, value) =>
  ({ ...scene, metrics: { ...scene.metrics, [name]: { ...scene.metrics[name], value } } });

test("demo scene fills both Drive pages and dates its trip from the capture time", () => {
  const scene = buildDemoScene(NOW);
  assert.equal(validateDemoScene(scene), true);
  const started = Date.parse(scene.history.current_trip.started_at);
  assert.equal(NOW - started, scene.history.current_trip.duration_seconds * 1000);
});

test("demo validation rejects a missing tile or values that contradict each other", () => {
  const scene = buildDemoScene(NOW);
  const { "engine.fuel_rate": _dropped, ...rest } = scene.metrics;
  assert.throws(() => validateDemoScene({ ...scene, metrics: rest }), /missing Drive metrics: engine\.fuel_rate/);
  assert.throws(() => validateDemoScene(withMetric(scene, "engine.fuel_rate", 12)), /instant economy/);
  assert.throws(() => validateDemoScene(withMetric(scene, "engine.crankshaft_power", 120)), /power must agree/);
  assert.throws(() => validateDemoScene(withMetric(scene, "acc.set_speed", 55)), /ACC set speed/);
});
