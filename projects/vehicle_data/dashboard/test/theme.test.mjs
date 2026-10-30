import test from "node:test";
import assert from "node:assert/strict";
import { mountainDay, sunTimes } from "../src/sunTimes.js";
import { themeSchedule, sunBoundaries, dimToggleSettings, nextThemeMode, themeButtonLabel } from "../src/theme.helpers.js";
import { defaultSettings, normalizeSettings, loadSettings, saveSettings, STORAGE_KEY, LEGACY_KEY } from "../src/settings.js";
import { DAY_MS, MINUTE_MS } from "../src/theme.config.js";

// Independent NOAA main.js calcSunriseSet results, retrieved 2026-10-30:
// https://gml.noaa.gov/grad/solcalc/main.js (39.7392, -104.9903, zenith 90.833).
const NOAA = [
  ["2026-06-21", "05:32:11-06:00", "20:31:30-06:00"],
  ["2026-12-21", "07:17:33-07:00", "16:38:49-07:00"],
  ["2026-03-08", "07:22:11-06:00", "18:59:43-06:00"],
  ["2026-11-01", "06:28:44-07:00", "16:57:45-07:00"],
];

for (const [date, rise, set] of NOAA) {
  test(`Denver sun agrees with NOAA within two minutes on ${date}`, () => {
    const times = sunTimes(Date.parse(date));
    assert.ok(Math.abs(times.sunrise - Date.parse(`${date}T${rise}`)) < 2 * MINUTE_MS);
    assert.ok(Math.abs(times.sunset - Date.parse(`${date}T${set}`)) < 2 * MINUTE_MS);
  });

  test(`all four schedule boundaries on ${date}, inclusive new state`, () => {
    const b = sunBoundaries(Date.parse(date));
    for (const [key, before, after] of [
      ["undim", ["dark", true], ["dark", false]],
      ["light", ["dark", false], ["light", false]],
      ["dark", ["light", false], ["dark", false]],
      ["dim", ["dark", false], ["dark", true]],
    ]) {
      for (const delta of [-MINUTE_MS, 0, MINUTE_MS]) {
        const result = themeSchedule(b[key] + delta, "auto");
        assert.deepEqual([result.theme, result.dim], delta < 0 ? before : after, `${key} ${delta}`);
        assert.ok(result.nextBoundary > b[key] + delta);
        if (delta < 0) assert.equal(result.nextBoundary, b[key]);
      }
    }
  });
}

test("Mountain calendar days do not depend on browser time zone or UTC date", () => {
  for (const [instant, day] of [
    ["2026-06-22T05:59:59Z", "2026-06-21"], ["2026-06-22T06:00:00Z", "2026-06-22"],
    ["2026-12-22T06:59:59Z", "2026-12-21"], ["2026-12-22T07:00:00Z", "2026-12-22"],
    ["2026-03-08T08:59:59Z", "2026-03-08"], ["2026-03-08T09:00:00Z", "2026-03-08"],
    ["2026-11-01T07:59:59Z", "2026-11-01"], ["2026-11-01T08:00:00Z", "2026-11-01"],
  ]) assert.equal(mountainDay(Date.parse(instant)), Date.parse(day));
});

test("auto DIM override expires at next dim boundary, not a theme boundary, and survives reload", () => {
  const day = Date.parse("2026-06-21");
  const b = sunBoundaries(day);
  const memory = new Map();
  const storage = { getItem: (key) => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value) };
  for (const [tapAt, expires] of [
    [b.undim - MINUTE_MS, b.undim], [b.undim, b.dim], [b.light - MINUTE_MS, b.dim],
    [b.dark - MINUTE_MS, b.dim], [b.dim, sunBoundaries(day + DAY_MS).undim],
  ]) {
    const tapped = dimToggleSettings(defaultSettings(), tapAt);
    assert.equal(tapped.dimOverrideUntil, expires);
    saveSettings(tapped, storage);
    const loaded = loadSettings(storage);
    assert.equal(themeSchedule(tapAt, "auto", loaded).dim, !themeSchedule(tapAt, "auto").dim);
    assert.equal(themeSchedule(expires - 1, "auto", loaded).dim, tapped.dim);
    assert.equal(themeSchedule(expires, "auto", loaded).dim, themeSchedule(expires, "auto").dim);
    assert.equal(themeSchedule(expires + MINUTE_MS, "auto", loaded).dim, themeSchedule(expires + MINUTE_MS, "auto").dim);
  }
  saveSettings(defaultSettings(), null);
});

test("manual themes and DIM never follow the schedule; repeat auto taps replace the override", () => {
  const now = Date.parse("2026-12-21T12:00:00-07:00");
  for (const themeMode of ["light", "dark"]) {
    for (const dim of [false, true]) {
      const s = { ...defaultSettings(), themeMode, dim };
      for (const t of [now, now + 12 * 60 * MINUTE_MS]) {
        assert.deepEqual(themeSchedule(t, themeMode, s), { theme: themeMode, dim, nextBoundary: null, nextDimBoundary: null });
      }
      const next = dimToggleSettings(s, now);
      assert.equal(next.dim, !dim);
      assert.equal(next.dimOverrideUntil, null);
    }
  }
  const one = dimToggleSettings(defaultSettings(), now);
  const two = dimToggleSettings(one, now + MINUTE_MS);
  assert.equal(two.dim, false);
  assert.equal(two.dimOverrideUntil, one.dimOverrideUntil);
});

test("old settings gain auto theme without losing view, manual dim or customisation", () => {
  const old = { view: "history", auto: false, dim: true, drivePage: 2, hidden: { drive: ["trip"] } };
  const stored = loadSettings({ getItem: (key) => key === STORAGE_KEY ? JSON.stringify(old) : null });
  assert.deepEqual(stored, { ...defaultSettings(), ...old });
  const legacy = loadSettings({ getItem: (key) => key === LEGACY_KEY ? '{"selected":"diagnostics"}' : null, setItem() {} });
  assert.equal(legacy.themeMode, "auto");
  assert.equal(legacy.view, "system");
  for (const invalid of [null, "nope", true, 3]) assert.equal(normalizeSettings({ themeMode: invalid }).themeMode, "auto");
  for (const invalid of [null, "100", Infinity, NaN, -1, 2.5]) {
    assert.equal(normalizeSettings({ dim: true, dimOverrideUntil: invalid }).dimOverrideUntil, null);
  }
  assert.equal(normalizeSettings({ dimOverrideUntil: 100 }).dimOverrideUntil, null);
  assert.equal(normalizeSettings({ themeMode: "light", dim: true, dimOverrideUntil: 100 }).dimOverrideUntil, null);
});

test("theme cycle and accessible labels are independent of view AUTO", () => {
  assert.deepEqual([nextThemeMode("auto"), nextThemeMode("light"), nextThemeMode("dark")], ["light", "dark", "auto"]);
  assert.match(themeButtonLabel("auto"), /Theme: Auto.*Denver.*Tap for light/);
  assert.match(themeButtonLabel("light"), /Theme: light.*Tap for dark/);
  assert.match(themeButtonLabel("dark"), /Theme: dark.*Tap for auto/);
});
