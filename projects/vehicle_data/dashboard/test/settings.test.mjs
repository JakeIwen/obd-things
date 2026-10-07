import test from "node:test";
import assert from "node:assert/strict";

import {
  STORAGE_KEY,
  LEGACY_KEY,
  VIEWS,
  MAX_AUTOMATIC_STATE_AGE_MS,
  defaultSettings,
  normalizeSettings,
  migrateLegacy,
  loadSettings,
  saveSettings,
  settings,
  autoView,
} from "../src/settings.js";

/** In-memory Storage stand-in. */
function memoryStorage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => {
      map.set(key, String(value));
    },
    removeItem: (key) => {
      map.delete(key);
    },
    map,
  };
}

/** Storage that throws on every access (private mode / denied). */
const deniedStorage = {
  getItem() {
    throw new Error("SecurityError: access denied");
  },
  setItem() {
    throw new Error("QuotaExceededError");
  },
};

test("defaults and constants", () => {
  assert.equal(STORAGE_KEY, "van-telemetry.v2.settings");
  assert.equal(LEGACY_KEY, "van-telemetry.dashboard.v3");
  assert.deepEqual(VIEWS, ["drive", "parked", "health", "history", "system"]);
  assert.equal(MAX_AUTOMATIC_STATE_AGE_MS, 5000);
  assert.deepEqual(defaultSettings(), {
    version: 1,
    view: "drive",
    auto: true,
    hidden: {},
    order: {},
    timeFormat: "12h",
    dim: false,
    drivePage: 1,
  });
  assert.notEqual(defaultSettings(), defaultSettings(), "a fresh object every call");
});

test("the module-level signal is initialized without localStorage (Node)", () => {
  assert.equal(typeof globalThis.localStorage, "undefined");
  assert.deepEqual(settings.value, defaultSettings());
});

test("normalizeSettings drops unknown views, non-string ids, duplicates and bad formats", () => {
  const out = normalizeSettings({
    version: 9,
    view: "history",
    auto: false,
    hidden: { drive: ["trip", 3, "trip", "", "tires"], bogus: ["x"], parked: "no" },
    order: { system: ["buses", "broker"], health: [] },
    timeFormat: "24h",
    extra: true,
  });
  assert.deepEqual(out, {
    version: 1,
    view: "history",
    auto: false,
    hidden: { drive: ["trip", "tires"] },
    order: { system: ["buses", "broker"] },
    timeFormat: "24h",
    dim: false,
    drivePage: 1,
  });
  assert.deepEqual(normalizeSettings({ view: "overview", auto: "yes", timeFormat: "bad" }), defaultSettings());
  assert.deepEqual(normalizeSettings(null), defaultSettings());
  assert.deepEqual(normalizeSettings([1, 2]), defaultSettings());
  assert.deepEqual(normalizeSettings("drive"), defaultSettings());
});

test("normalizeSettings keeps Drive page 1 or 2 and drops anything else", () => {
  assert.equal(normalizeSettings({ drivePage: 2 }).drivePage, 2);
  assert.equal(normalizeSettings({ drivePage: 1 }).drivePage, 1);
  for (const bad of [0, 3, "2", null, 1.5, true]) {
    assert.equal(normalizeSettings({ drivePage: bad }).drivePage, 1, String(bad));
  }
  // Settings saved before the second page existed load on page 1.
  assert.equal(normalizeSettings({ view: "drive", dim: true }).drivePage, 1);
});

test("loadSettings reads the v2 key when present", () => {
  const storage = memoryStorage({
    [STORAGE_KEY]: JSON.stringify({ version: 1, view: "system", auto: false, hidden: { parked: ["codes"] }, order: {}, timeFormat: "12h", dim: false }),
    [LEGACY_KEY]: JSON.stringify({ version: 3, selected: "driving" }),
  });
  const loaded = loadSettings(storage);
  assert.equal(loaded.view, "system");
  assert.equal(loaded.auto, false);
  assert.deepEqual(loaded.hidden, { parked: ["codes"] });
});

test("migration from the old van-telemetry.dashboard.v3 key when the v2 key is absent", () => {
  const cases = [
    ["driving", { view: "drive", auto: false }],
    ["parked", { view: "parked", auto: false }],
    ["overview", { view: "health", auto: false }],
    ["diagnostics", { view: "system", auto: false }],
    ["custom", { view: "health", auto: false }],
    ["auto", { view: "drive", auto: true }],
    ["garbage", { view: "drive", auto: true }],
  ];
  for (const [selected, expected] of cases) {
    const storage = memoryStorage({ [LEGACY_KEY]: JSON.stringify({ version: 3, selected, customLayout: [] }) });
    const loaded = loadSettings(storage);
    assert.equal(loaded.view, expected.view, `selected=${selected}`);
    assert.equal(loaded.auto, expected.auto, `selected=${selected}`);
    assert.equal(loaded.version, 1);
    // The migration is written under the v2 key so it runs once.
    assert.deepEqual(JSON.parse(storage.getItem(STORAGE_KEY)), loaded);
    assert.ok(storage.map.has(LEGACY_KEY), "the old key is left untouched");
  }
  assert.deepEqual(migrateLegacy(null), defaultSettings());
  assert.deepEqual(migrateLegacy({ selected: "parked" }), Object.assign(defaultSettings(), { view: "parked", auto: false }));
});

test("corrupt storage falls back to defaults (or to the legacy key)", () => {
  assert.deepEqual(loadSettings(memoryStorage({ [STORAGE_KEY]: "{not json" })), defaultSettings());
  assert.deepEqual(loadSettings(memoryStorage({ [STORAGE_KEY]: "[1,2,3]" })), defaultSettings());
  assert.deepEqual(loadSettings(memoryStorage({ [STORAGE_KEY]: "null" })), defaultSettings());
  assert.deepEqual(loadSettings(memoryStorage({ [LEGACY_KEY]: "<<<" })), defaultSettings());
  const mixed = memoryStorage({ [STORAGE_KEY]: "{oops", [LEGACY_KEY]: JSON.stringify({ selected: "diagnostics" }) });
  assert.equal(loadSettings(mixed).view, "system");
  assert.deepEqual(loadSettings(memoryStorage()), defaultSettings());
});

test("denied storage (throwing localStorage) yields defaults and does not break saves", () => {
  assert.deepEqual(loadSettings(deniedStorage), defaultSettings());
  assert.deepEqual(loadSettings(null), defaultSettings());
  const saved = saveSettings({ view: "parked", auto: false }, deniedStorage);
  assert.equal(saved.view, "parked");
  assert.equal(saved.auto, false);
  assert.equal(settings.value, saved, "the in-memory signal still carries the new settings");
});

test("saveSettings normalizes, publishes to the signal and persists", () => {
  const storage = memoryStorage();
  const saved = saveSettings({ view: "health", auto: false, hidden: { health: ["service"] }, timeFormat: "24h", dim: false }, storage);
  assert.deepEqual(saved, {
    version: 1,
    view: "health",
    auto: false,
    hidden: { health: ["service"] },
    order: {},
    timeFormat: "24h",
    dim: false,
    drivePage: 1,
  });
  assert.equal(settings.value, saved);
  assert.deepEqual(JSON.parse(storage.getItem(STORAGE_KEY)), saved);
  assert.deepEqual(loadSettings(storage), saved);
  // Restore the shared signal for later tests.
  saveSettings(defaultSettings(), storage);
});

test("autoView follows design 5.2 / rule 16", () => {
  const verified = (state, ageMs) => ({ state, confidence: "verified", ageMs });
  const inferred = (state, ageMs) => ({ state, confidence: "inferred", ageMs });

  assert.equal(autoView({ rpmLive: true, vehicle: inferred("asleep", 100), current: "history" }), "drive", "live rpm wins");
  assert.equal(
    autoView({
      rpmLive: true,
      vehicle: { state: "unknown", confidence: "unavailable", basis: "can_adapter_missing", ageMs: 100 },
      current: "health",
    }),
    "health",
    "CAN loss keeps the current view even with surviving-role RPM",
  );
  assert.equal(autoView({ rpmLive: false, vehicle: verified("running", 4999), current: "history" }), "drive");
  assert.equal(autoView({ rpmLive: false, vehicle: verified("ignition_on", 0), current: "history" }), "drive");
  assert.equal(autoView({ rpmLive: false, vehicle: verified("moving", 10), current: "history" }), "drive");
  assert.equal(autoView({ rpmLive: false, vehicle: verified("running", 5001), current: "history" }), "history", "verified but not fresh");
  assert.equal(autoView({ rpmLive: false, vehicle: { state: "running", confidence: "observed", ageMs: 10 }, current: "history" }), "history", "running needs verified");
  assert.equal(autoView({ rpmLive: false, vehicle: inferred("asleep", 3000), current: "history" }), "parked", "inferred asleep qualifies");
  assert.equal(autoView({ rpmLive: false, vehicle: verified("parked", 100), current: "drive" }), "parked");
  assert.equal(autoView({ rpmLive: false, vehicle: inferred("asleep", 6000), current: "health" }), "health", "stale asleep keeps current");
  assert.equal(autoView({ rpmLive: false, vehicle: inferred("asleep", null), current: "health" }), "health", "invalid age keeps current");
  assert.equal(autoView({ rpmLive: false, vehicle: { state: "unknown", confidence: "stale", ageMs: 10 }, current: "system" }), "system");
  assert.equal(autoView({ rpmLive: false, vehicle: { state: "awake", confidence: "observed", ageMs: 10 }, current: "parked" }), "parked", "awake keeps current");
  assert.equal(autoView({ rpmLive: false, vehicle: verified("running", -1), current: "health" }), "health", "negative age is not fresh");
  assert.equal(autoView({ rpmLive: false, vehicle: null, current: "bogus" }), "drive", "unknown current falls back to drive");
  assert.equal(autoView(undefined), "drive");
});

test("with no storage argument the bare global localStorage is used (no globalThis dependency)", () => {
  globalThis.localStorage = memoryStorage({ [STORAGE_KEY]: JSON.stringify({ view: "history", auto: false }) });
  try {
    const loaded = loadSettings();
    assert.equal(loaded.view, "history");
    assert.equal(loaded.auto, false);
  } finally {
    delete globalThis.localStorage;
  }
});
