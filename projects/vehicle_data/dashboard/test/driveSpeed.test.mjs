import { test } from "node:test";
import assert from "node:assert/strict";
import { speedFooter, speedLimitText } from "../src/views/driveSpeed.helpers.js";

test("speedLimitText: whole mph, or — when the cluster shows none", () => {
  assert.equal(speedLimitText(55), "55");
  assert.equal(speedLimitText(30), "30");
  assert.equal(speedLimitText(0), "—");
  assert.equal(speedLimitText(null), "—");
  assert.equal(speedLimitText(NaN), "—");
});

test("Speed footer shows set speed and vehicle ahead; the limit is the sign, not a line", () => {
  assert.equal(speedFooter("engaged", 66, {mode: "adaptive", lead: true}),
    "ACC 66 mph · engaged · vehicle ahead");
  assert.equal(speedFooter("override", 66, {mode: "adaptive", lead: true}),
    "ACC 66 mph · override · vehicle ahead");
});

test("Speed footer gates retained fields on the current state and mode", () => {
  const shown = {mode: "adaptive", lead: true};
  assert.equal(speedFooter(null, 66, shown), "");
  assert.equal(speedFooter("mystery", 66, shown), "");
  assert.equal(speedFooter("off", 66, shown), "ACC off");
  assert.equal(speedFooter("ready", 66, shown), "ACC · ready");
  assert.equal(speedFooter("standby", 66, shown), "ACC 66 mph · standby · saved");
  assert.equal(speedFooter("engaged", 65, {mode: "fixed", lead: true}), "Cruise 65 mph · engaged");
  assert.equal(speedFooter("engaged", null, shown), "ACC · engaged · vehicle ahead");
});
