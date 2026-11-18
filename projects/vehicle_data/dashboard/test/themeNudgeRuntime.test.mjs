import test from "node:test";
import assert from "node:assert/strict";

// Runtime check of the headlights-off nudge through startTheme/toggleDim/displayTheme, with
// injected wall, monotonic and timer seams and metrics fed through the real store.
const clk = { mono: 100000, wall: Date.parse("2026-06-21T23:00:00-06:00") };
performance.now = () => clk.mono;
Date.now = () => clk.wall;

const store = await import("../src/store.js");
const { startTheme, toggleDim, displayTheme } = await import("../src/theme.js");
const { defaultSettings, saveSettings, settings } = await import("../src/settings.js");
const { HEADLIGHT_METRICS, HEADLIGHT_OFF_HOLD_MS } = await import("../src/theme.config.js");

const def = (name) => ({ name, value_type: "boolean", unit: "bool", stale_after_seconds: 2,
  sources: [{ name: "ccan.fixture", quality: "verified" }] });
const CATALOG = HEADLIGHT_METRICS.map(def);

function deliver(lightsOn) {
  const metrics = {};
  for (const name of HEADLIGHT_METRICS) {
    metrics[name] = { available: true, stale: false, value: lightsOn, unit: "bool", quality: "verified",
      source: "ccan.fixture", age_ms: 100, observed_at: new Date(clk.wall - 100).toISOString() };
  }
  store.applyBaseline({ catalog: CATALOG, metrics, web: {},
    status: { vehicle_state: { state: "running", confidence: "verified", basis: "x", detail: "x",
      running: true, observed_at: new Date(clk.wall).toISOString(), age_ms: 0 } } }, 0, clk.mono);
  store.connection.value = { ...store.connection.value, state: "live" };
}

function harness() {
  const timers = [];
  const tick = { fn: null };
  const classes = new Set();
  const document = {
    documentElement: { classList: { toggle: (n, on) => (on ? classes.add(n) : classes.delete(n)) } },
    querySelector: () => null, addEventListener() {}, removeEventListener() {},
  };
  saveSettings(defaultSettings(), null);
  const stop = startTheme({ document, now: () => clk.wall, monoNow: () => clk.mono,
    setInterval: (fn) => { tick.fn = fn; return 1; }, clearInterval() {},
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {} });
  return { timers, stop, tick };
}
const shown = () => [displayTheme.peek().theme, displayTheme.peek().dim];

test("a DIM tap during the nighttime nudge dims on the first tap, then the nudge returns", () => {
  const { timers, stop, tick } = harness();
  deliver(false);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, HEADLIGHT_OFF_HOLD_MS);
  clk.mono += 1500; clk.wall += 1500;
  deliver(false);
  clk.mono += 3500; clk.wall += 3500;
  deliver(false); // fresh again just before the hold fires
  timers[0].fn();
  timers[0].fn();
  assert.deepEqual(shown(), ["light", false]);
  toggleDim();
  assert.deepEqual(shown(), ["dark", true]);
  assert.ok(settings.peek().dimOverrideUntil > clk.wall);
  // Once the override has expired the nudge shows again while the lights stay off.
  clk.wall = settings.peek().dimOverrideUntil + 1;
  tick.fn(); // the minute refresh
  assert.deepEqual(shown(), ["light", false]);
  stop();
  saveSettings(defaultSettings(), null);
});

test("the hold does not raise the nudge on readings that expired before it fired", () => {
  clk.wall = Date.parse("2026-06-21T23:00:00-06:00");
  const { timers, stop } = harness();
  deliver(true);
  deliver(false);
  const hold = timers.at(-1);
  assert.equal(hold.ms, HEADLIGHT_OFF_HOLD_MS);
  // No tick or delivery since: the 2 s lights freshness lapsed before the 5 s hold.
  clk.mono += HEADLIGHT_OFF_HOLD_MS; clk.wall += HEADLIGHT_OFF_HOLD_MS;
  hold.fn();
  hold.fn();
  assert.deepEqual(shown(), ["dark", true]);
  stop();
  saveSettings(defaultSettings(), null);
});
