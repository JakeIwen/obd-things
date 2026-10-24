import { test } from "node:test";
import assert from "node:assert/strict";
import { fuelRateText, instantFigure, instantText, mpgFigure, mpgText, windowsText } from "../src/fuelEconomy.helpers.js";
import { tripsModel } from "../src/views/history.helpers.js";

const NOW = Date.parse("2026-09-22T19:00:00Z");
const live = (value) => ({ available: true, stale: false, value });

test("economy figures are marked estimated and only shown when the broker has one", () => {
  assert.equal(mpgText({ available: true, mpg: 14.06 }), "≈ 14.1 mpg");
  assert.equal(mpgText({ available: true, mpg: 0 }), "≈ 0.0 mpg");
  assert.equal(mpgText({ available: false, reason: "partial_fuel_coverage", samples: 20 }), null);
  assert.equal(mpgText({ available: true, mpg: Infinity }), null);
  assert.equal(mpgText(undefined), null);
});

test("live fuel rate needs a current record", () => {
  assert.equal(fuelRateText(live(1.234)), "≈ 1.2");
  assert.equal(fuelRateText({ ...live(1.2), stale: true }), "—");
  assert.equal(fuelRateText({ available: false }), "—");
  assert.equal(fuelRateText(null), "—");
});

test("instant economy while moving, fuel cut when coasting, nothing when stopped", () => {
  assert.equal(instantText(60, live(3)), "now ≈ 20 mpg");
  assert.equal(instantText(60, live(0.5)), "now ≈ 99+ mpg");
  assert.equal(instantText(45, live(0)), "coasting, no fuel");
  assert.equal(instantText(2, live(0.4)), "");
  assert.equal(instantText(null, live(2)), "");
  assert.equal(instantText(60, { ...live(2), stale: true }), "");
});

test("Speed-tile figures carry the unit only with a value and read 99+ while coasting", () => {
  assert.deepEqual(instantFigure(60, live(3)), { value: "≈ 20", unit: "mpg" });
  assert.deepEqual(instantFigure(60, live(0.5)), { value: "≈ 99+", unit: "mpg" });
  assert.deepEqual(instantFigure(45, live(0)), { value: "≈ 99+", unit: "mpg" });
  assert.deepEqual(instantFigure(2, live(0.4)), { value: "—", unit: "" });
  assert.deepEqual(instantFigure(60, { ...live(2), stale: true }), { value: "—", unit: "" });
  assert.deepEqual(mpgFigure({ available: true, mpg: 16.44 }), { value: "≈ 16.4", unit: "mpg" });
  assert.deepEqual(mpgFigure({ available: false, samples: 3 }), { value: "—", unit: "" });
  assert.deepEqual(mpgFigure(null), { value: "—", unit: "" });
});

test("7- and 30-day line skips a window without a figure", () => {
  const summary = { available: true, days_7: { available: true, mpg: 15.24 }, days_30: { available: true, mpg: 15.0 } };
  assert.equal(windowsText(summary), "Fuel economy ≈ 15.2 mpg over 7 days · ≈ 15.0 mpg over 30 days");
  assert.equal(windowsText({ ...summary, days_7: { available: false } }), "Fuel economy ≈ 15.0 mpg over 30 days");
  assert.equal(windowsText({ available: false, reason: "fuel_economy_unavailable" }), null);
  assert.equal(windowsText(undefined), null);
});

test("a window that starts at the first fuel reading says since when, shown once", () => {
  const since = "2026-09-20T18:00:00+00:00";
  const late = { available: true, mpg: 9.81, since };
  assert.equal(windowsText({ available: true, days_7: late, days_30: late }, NOW), "Fuel economy ≈ 9.8 mpg since Sep 20");
  const full7 = { available: true, days_7: { available: true, mpg: 15.24 }, days_30: late };
  assert.equal(windowsText(full7, NOW), "Fuel economy ≈ 15.2 mpg over 7 days · ≈ 9.8 mpg since Sep 20");
});

test("trips card carries each trip's economy and the window line", () => {
  const history = {
    current_trip: { id: 9, started_at: "2026-09-22T18:30:00+00:00", duration_seconds: 600, snapshot_count: 120,
      fuel_economy: { available: true, mpg: 13.92 } },
    recent_trips: [
      { id: 8, started_at: "2026-09-22T16:00:00+00:00", ended_at: "2026-09-22T16:30:00+00:00",
        duration_seconds: 1800, snapshot_count: 360, fuel_economy: { available: true, mpg: 16.1 } },
      { id: 7, started_at: "2026-09-22T15:00:00+00:00", ended_at: "2026-09-22T15:10:00+00:00",
        duration_seconds: 600, snapshot_count: 120, fuel_economy: { available: false, samples: 0 } },
    ],
    fuel_economy: { available: true, days_7: { available: true, mpg: 15.5 } },
  };
  const t = tripsModel(history, { nowMs: NOW });
  assert.match(t.current.meta, /120 samples · ≈ 13\.9 mpg$/);
  assert.match(t.rows[0].meta, /360 samples · ≈ 16\.1 mpg$/);
  assert.doesNotMatch(t.rows[1].meta, /mpg/);
  assert.equal(t.fuel, "Fuel economy ≈ 15.5 mpg over 7 days");
});
