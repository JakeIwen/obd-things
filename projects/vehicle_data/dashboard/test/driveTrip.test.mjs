import { test } from "node:test";
import assert from "node:assert/strict";
import { tripRowsModel } from "../src/views/driveTrip.helpers.js";
import { mpgFigure } from "../src/fuelEconomy.helpers.js";
import { buildDemoScene } from "../tools/demo_data.mjs";

const NOW = Date.parse("2026-10-30T18:00:00Z");
const scene = buildDemoScene(NOW);
const context = { running: true, miles: scene.trip.distanceMiles, nowMs: NOW };

test("This drive groups time, distance and estimated MPG before the existing engine summaries", () => {
  assert.deepEqual(tripRowsModel(scene.history, context), [
    ["Time", "42 min"],
    ["Distance", "≈ 39.8 mi"],
    ["MPG", "≈ 16.4 mpg"],
    ["Coolant max", "209 °F"],
    ["Avg power", "48 hp"],
  ]);
});

test("drive MPG uses only the current drive, not a previous drive or a window", () => {
  const history = structuredClone(scene.history);
  delete history.current_trip.fuel_economy;
  assert.deepEqual(tripRowsModel(history, context)[2], ["MPG", "—"]);
  for (const economy of [{ available: false, mpg: 20 }, { available: true, mpg: NaN }, { available: true, mpg: -1 }]) {
    history.current_trip.fuel_economy = economy;
    assert.deepEqual(tripRowsModel(history, context)[2], ["MPG", "—"]);
  }
  history.current_trip = null;
  assert.deepEqual(tripRowsModel(history, context).slice(0, 3), [
    ["Time", "—"], ["Distance", "≈ 39.8 mi"], ["MPG", "—"],
  ]);
});

test("drive rows retain the existing running gate and show unavailable summaries as dashes", () => {
  assert.deepEqual(tripRowsModel(null, { ...context, miles: null }), [
    ["Time", "—"], ["Distance", "—"], ["MPG", "—"], ["Coolant max", "—"], ["Avg power", "—"],
  ]);
  const rows = tripRowsModel(scene.history, { ...context, running: false });
  assert.deepEqual(rows.slice(0, 2), [["Time", "—"], ["Distance", "—"]]);
  assert.deepEqual(rows.slice(2), tripRowsModel(scene.history, context).slice(2));
});

test("drive MPG follows the shared history formatter, including zero and one decimal", () => {
  for (const value of [0, 16.46, 100]) {
    const history = structuredClone(scene.history);
    const economy = { available: true, mpg: value };
    history.current_trip.fuel_economy = economy;
    const figure = mpgFigure(economy);
    assert.equal(tripRowsModel(history, context)[2][1], figure.value + " " + figure.unit);
  }
});
