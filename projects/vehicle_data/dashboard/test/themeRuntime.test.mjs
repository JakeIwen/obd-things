import test from "node:test";
import assert from "node:assert/strict";
import { startTheme, displayTheme } from "../src/theme.js";
import { defaultSettings, saveSettings } from "../src/settings.js";
import { sunBoundaries } from "../src/theme.helpers.js";
import { THEME_REFRESH_MS } from "../src/theme.config.js";

// Exercise the lifecycle through its public start/stop API. The document/timer seam
// models browser sleep without waiting for real time or importing telemetry state.
test("minute tick and visibility wake update the root, and stop releases listeners/timer", () => {
  const b = sunBoundaries(Date.parse("2026-06-21"));
  let now = b.light - 1;
  let interval;
  let cleared;
  const events = new Map();
  const classes = new Set();
  const document = {
    documentElement: {classList: {toggle: (name, on) => on ? classes.add(name) : classes.delete(name)}},
    querySelector: () => null,
    addEventListener: (type, fn) => events.set(type, fn),
    removeEventListener: (type, fn) => { assert.equal(events.get(type), fn); events.delete(type); },
  };
  saveSettings(defaultSettings(), null);
  const stop = startTheme({document, now: () => now,
    setInterval: (fn, ms) => { assert.equal(ms, THEME_REFRESH_MS); interval = fn; return 7; },
    clearInterval: (id) => { cleared = id; },
  });
  assert.deepEqual([...classes], []);
  now = b.light;
  interval();
  assert.ok(classes.has("light"));
  // Sleep across sunset: the visibility event takes effect immediately, not next interval.
  now = b.dim;
  events.get("visibilitychange")();
  assert.ok(!classes.has("light"));
  assert.ok(classes.has("dim"));
  assert.equal(displayTheme.peek().dim, true);
  saveSettings({...defaultSettings(), themeMode: "light", dim: true}, null);
  assert.ok(classes.has("light") && classes.has("dim"), "dim palette wins over forced light in CSS");
  stop();
  assert.equal(cleared, 7);
  assert.equal(events.size, 0);
  saveSettings(defaultSettings(), null);
});
