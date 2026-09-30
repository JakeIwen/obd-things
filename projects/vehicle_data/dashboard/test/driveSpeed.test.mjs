import { test } from "node:test";
import assert from "node:assert/strict";
import { speedFooter } from "../src/views/driveSpeed.helpers.js";

test("Speed footer keeps limit, set speed and vehicle ahead within two lines", () => {
  assert.equal(speedFooter(55, "engaged", 66, {mode: "adaptive", lead: true}),
    "limit 55 mph\nACC 66 mph · engaged · vehicle ahead");
  assert.equal(speedFooter(55, "override", 66, {mode: "adaptive", lead: true}),
    "limit 55 mph\nACC 66 mph · override · vehicle ahead");
});

test("Speed footer gates retained fields on the current state and mode", () => {
  const shown = {mode: "adaptive", lead: true};
  assert.equal(speedFooter(55, null, 66, shown), "limit 55 mph");
  assert.equal(speedFooter(null, "mystery", 66, shown), "");
  assert.equal(speedFooter(null, "off", 66, shown), "ACC off");
  assert.equal(speedFooter(null, "ready", 66, shown), "ACC · ready");
  assert.equal(speedFooter(null, "standby", 66, shown), "ACC 66 mph · standby");
  assert.equal(speedFooter(null, "engaged", 65, {mode: "fixed", lead: true}), "Cruise 65 mph · engaged");
  assert.equal(speedFooter(null, "engaged", null, shown), "ACC · engaged · vehicle ahead");
});
