import { test } from "node:test";
import assert from "node:assert/strict";
import { speedAccLines, speedLimitText } from "../src/views/driveSpeed.helpers.js";

test("speedLimitText: whole mph, or — when the cluster shows none", () => {
  assert.equal(speedLimitText(55), "55");
  assert.equal(speedLimitText(30), "30");
  assert.equal(speedLimitText(0), "—");
  assert.equal(speedLimitText(null), "—");
  assert.equal(speedLimitText(NaN), "—");
});

test("ACC column stacks the mode and set speed over a short state word", () => {
  const adaptive = {mode: "adaptive", lead: true};
  assert.deepEqual(speedAccLines("engaged", 66, adaptive), {title: "ACC 66", status: "engaged", saved: false});
  assert.deepEqual(speedAccLines("override", 66, adaptive), {title: "ACC 66", status: "override", saved: false});
  assert.deepEqual(speedAccLines("engaged", 65, {mode: "fixed", lead: true}),
    {title: "Cruise 65", status: "engaged", saved: false});
});

test("ACC column gates retained fields on the current state and mode", () => {
  const shown = {mode: "adaptive", lead: true};
  assert.equal(speedAccLines(null, 66, shown), null);
  assert.equal(speedAccLines("mystery", 66, shown), null);
  assert.deepEqual(speedAccLines("off", 66, shown), {title: "ACC off", status: "", saved: false});
  assert.deepEqual(speedAccLines("ready", 66, shown), {title: "ACC", status: "ready", saved: false});
  assert.deepEqual(speedAccLines("standby", 66, shown), {title: "ACC 66", status: "standby", saved: true});
  assert.deepEqual(speedAccLines("engaged", null, shown), {title: "ACC", status: "engaged", saved: false});
});
