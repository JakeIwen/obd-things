import test from "node:test";
import assert from "node:assert/strict";
import { applyHeadlightNudge, headlightReading, headlightsOffWhileOn, themeSchedule, vanIsOn } from "../src/theme.helpers.js";

const live = (value) => ({ kind: "live", value });
const night = Date.parse("2026-06-21T23:00:00-06:00");
const day = Date.parse("2026-06-21T12:00:00-06:00");
const run = (now, mode, settings, nudge) =>
  applyHeadlightNudge(themeSchedule(now, mode, settings), mode, settings, now, nudge);

test("headlight reading: on, off, unknown", () => {
  assert.equal(headlightReading([live(false), live(true)], "live"), "on");
  assert.equal(headlightReading([live(true), null], "live"), "on");
  assert.equal(headlightReading([live(false), live(false)], "live"), "off");
  for (const unknown of [[live(false), null], [live(false), { kind: "held", value: false }],
    [live(false), { kind: "live", value: null }], []]) {
    assert.equal(headlightReading(unknown, "live"), "unknown");
  }
  assert.equal(headlightReading([live(false), live(false)], "connecting"), "unknown");
});

test("van on needs verified, fresh, driving-class state", () => {
  assert.equal(vanIsOn({ state: "running", confidence: "verified", ageMs: 1000 }), true);
  assert.equal(vanIsOn({ state: "ignition_on", confidence: "verified", ageMs: 0 }), true);
  assert.equal(vanIsOn({ state: "running", confidence: "verified", ageMs: 9000 }), false);
  assert.equal(vanIsOn({ state: "running", confidence: "inferred", ageMs: 1000 }), false);
  assert.equal(vanIsOn({ state: "asleep", confidence: "verified", ageMs: 1000 }), false);
  assert.equal(vanIsOn({ state: "running", confidence: "verified", ageMs: null }), false);
  assert.equal(vanIsOn(), false);
});

test("off nudge condition needs known-off lights and the van on", () => {
  assert.equal(headlightsOffWhileOn("off", true), true);
  for (const [r, v] of [["off", false], ["on", true], ["unknown", true]]) {
    assert.equal(headlightsOffWhileOn(r, v), false);
  }
});

test("auto at night: nudge gives bright palette, no nudge keeps time of day", () => {
  const plain = run(night, "auto", {}, false);
  assert.deepEqual([plain.theme, plain.dim], ["dark", true]);
  const nudged = run(night, "auto", {}, true);
  assert.deepEqual([nudged.theme, nudged.dim], ["light", false]);
});

test("auto by day is already bright, nudge changes nothing visible", () => {
  const nudged = run(day, "auto", {}, true);
  assert.deepEqual([nudged.theme, nudged.dim], ["light", false]);
});

test("manual themes are never overridden", () => {
  for (const mode of ["light", "dark"]) {
    const r = run(night, mode, { dim: true }, true);
    assert.deepEqual([r.theme, r.dim], [mode, true]);
  }
});

test("an active owner DIM override wins; an expired one does not", () => {
  const active = run(day, "auto", { dim: true, dimOverrideUntil: day + 60_000 }, true);
  assert.deepEqual([active.theme, active.dim], ["light", true]);
  const duskActive = run(night, "auto", { dim: true, dimOverrideUntil: night + 60_000 }, true);
  assert.deepEqual([duskActive.theme, duskActive.dim], ["dark", true]);
  const expired = run(night, "auto", { dim: true, dimOverrideUntil: night - 1 }, true);
  assert.deepEqual([expired.theme, expired.dim], ["light", false]);
});
