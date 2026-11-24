// Why: the cross-family review of the full chart dialog (2026-11-23) held it for held readings shown
// as current, graphs that could not open, a normal band painted over a week, a lone bucket losing its
// range, a stale chart staying up and DST dropping axis labels. DST needs a real zone, so pin one.
process.env.TZ = "America/Denver";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_SPAN, FETCH_DEADLINE_MS, nowLabel, settleFetch, PENDING_RETRY_MS, RINGLESS_DEFAULT_SPAN, SERIES_REFRESH_MS, buildChartModel, hasLiveRing,
  historySeries, isolatedRanges, liveSeries, nextHistoryView, nowReading, spansFor, startSpanFor, timeTicks,
} from "../src/chart.helpers.js";
import { RECENT_METRICS } from "../src/historySeed.helpers.js";

const MIN = 60000;
const HOUR = 3600000;

test("a held reading is shown dimmed with its time stamp, an absent one as the dash, as the tile does", () => {
  const base = { valueText: "198", unitText: "°F", heldText: "3:41 pm" };
  assert.deepEqual(nowReading({ ...base, kind: "live" }), { state: "live", valueText: "198", unitText: "°F", stamp: "" });
  assert.deepEqual(nowReading({ ...base, kind: "held" }), { state: "held", valueText: "198", unitText: "°F", stamp: "as of 3:41 pm" });
  assert.equal(nowReading({ ...base, kind: "held", heldText: "" }).stamp, "held", "a held value always says so");
  const off = nowReading({ ...base, kind: "off", valueText: "–" });
  assert.equal(off.state, "off");
  assert.equal(off.stamp, "");
});

test("a metric without a browser ring offers only history spans and opens on 24 hours", () => {
  assert.ok(hasLiveRing("engine.coolant_temperature"));
  assert.equal(hasLiveRing("tire.pressure.fl"), false);
  assert.ok(RECENT_METRICS.every(hasLiveRing));
  assert.ok(spansFor("engine.coolant_temperature").some((s) => s.source === "live"));
  assert.ok(spansFor("tire.pressure.fl").every((s) => s.source === "history"));
  assert.equal(startSpanFor("engine.coolant_temperature", "15m"), "15m");
  assert.equal(startSpanFor("engine.coolant_temperature", "7d"), "7d");
  assert.equal(startSpanFor("tire.pressure.fl", "15m"), RINGLESS_DEFAULT_SPAN, "the live span the last chart used is not on offer");
  assert.equal(startSpanFor("tire.pressure.fl", "7d"), "7d");
  assert.equal(RINGLESS_DEFAULT_SPAN, "24h");
  assert.equal(DEFAULT_SPAN, "15m");
});

const history = (points, extra = {}) => historySeries({
  available: true, bucket_seconds: 2520, start_at: "2026-11-16T00:00:00Z", end_at: "2026-11-23T00:00:00Z", points, ...extra,
});

test("the normal band is painted on the live span only", () => {
  const band = { lo: 13.5, hi: 14.8 };
  const live = liveSeries([{ min: 13.9, max: 14.1, mean: 14 }, { min: 14, max: 14.2, mean: 14.1 }], Date.UTC(2026, 10, 23, 12), 900000);
  const week = history([{ at: "2026-11-20T10:00:00Z", value: 14, min: 13.9, max: 14.2 }]);
  const input = { name: "battery.voltage", band, minSpan: 0.5, maxTimeTicks: 6, decimals: 1, offsetAt: () => 0 };
  assert.ok(buildChartModel({ ...input, series: live, spanKey: "15m" }).band, "live span shows it");
  assert.equal(buildChartModel({ ...input, series: week, spanKey: "7d" }).band, null);
});

test("an isolated bucket keeps its min-max range as a whisker; buckets in a run, equal ranges and live columns do not", () => {
  const lone = { t: 1, mean: 50, min: 40, max: 70 };
  const flat = { t: 9, mean: 50, min: 50, max: 50 };
  assert.deepEqual(isolatedRanges([[lone], [flat], [{ ...lone, t: 20 }, { ...lone, t: 21 }]]), [lone]);
  // A missing or null min/max (a bucket the route only has a mean for) is a zero-width range, not a crash.
  const series = history([
    { at: "2026-11-18T10:00:00Z", value: 50, min: null, max: null },
    { at: "2026-11-20T10:00:00Z", value: 60, min: 41, max: 88 },
  ]);
  const model = buildChartModel({ series, name: "engine.coolant_temperature", spanKey: "7d", band: null, minSpan: 20, maxTimeTicks: 6, offsetAt: () => 0 });
  assert.equal(model.runs.length, 2);
  assert.deepEqual(model.whiskers.map((w) => [w.min, w.max]), [[41, 88]]);
  assert.ok(model.y.lo <= 41 && model.y.hi >= 88, "the axis covers the whisker");
  const live = liveSeries([{ min: 1, max: 9, mean: 5 }], Date.UTC(2026, 10, 23), 900000);
  assert.deepEqual(buildChartModel({ series: live, name: "x", spanKey: "15m", band: null, minSpan: 1, maxTimeTicks: 6, offsetAt: () => 0 }).whiskers, []);
});

test("after a success a failed refresh keeps the series, marked stale as of its end; with none it says why", () => {
  const series = history([{ at: "2026-11-20T10:00:00Z", value: 14 }]);
  const fresh = nextHistoryView(null, { series }, 0);
  assert.deepEqual([fresh.series === series, fresh.note, fresh.staleAs, fresh.retryMs], [true, "", null, null]);
  const failed = nextHistoryView(series, { note: "History is not available right now." }, 61000);
  assert.deepEqual([failed.series === series, failed.note, failed.staleAs, failed.retryMs], [true, "", series.endMs, null]);
  const first = nextHistoryView(null, { note: "History is not available right now." }, 0);
  assert.deepEqual([first.series, first.note, first.staleAs], [null, "History is not available right now.", null]);
});

test("pending is loading: retried quickly, and only a clearly old series is marked stale", () => {
  const series = history([{ at: "2026-11-20T10:00:00Z", value: 14 }]);
  const none = nextHistoryView(null, { pending: true }, 0);
  assert.equal(none.retryMs, PENDING_RETRY_MS);
  assert.match(none.note, /Preparing/);
  assert.equal(nextHistoryView(series, { pending: true }, SERIES_REFRESH_MS).staleAs, null);
  assert.equal(nextHistoryView(series, { pending: true }, 3 * SERIES_REFRESH_MS).staleAs, series.endMs);
  assert.ok(PENDING_RETRY_MS < SERIES_REFRESH_MS);
});

test("axis ticks survive the November fall-back: a 15 min window across 2 a.m. keeps all four labels", () => {
  // 2026-11-01: 2:00 a.m. MDT becomes 1:00 a.m. MST. The window starts at 1:55 a.m. MDT.
  const start = Date.UTC(2026, 10, 1, 7, 55);
  const ticks = timeTicks("15m", start, start + 15 * MIN, 8);
  assert.deepEqual(ticks.map((t) => (t.t - start) / MIN), [0, 5, 10, 15]);
  assert.deepEqual(ticks.map((t) => t.label), ["1:55 am", "1:00 am", "1:05 am", "1:10 am"]);
});

test("axis ticks across the spring-forward gap are real local times, in order", () => {
  // 2026-03-08: 2:00 a.m. MST becomes 3:00 a.m. MDT. Hourly ticks from 12:30 a.m. MST to 5:30 a.m. MDT.
  const start = Date.UTC(2026, 2, 8, 7, 30);
  const ticks = timeTicks("6h", start, start + 4 * HOUR, 8);
  assert.deepEqual(ticks.map((t) => t.label), ["1 am", "3 am", "4 am", "5 am"]);
  assert.ok(ticks.every((t, i) => i === 0 || t.t > ticks[i - 1].t));
});

test("a 24 h window over the fall-back keeps four-hourly local ticks and every tick inside the window", () => {
  const start = Date.UTC(2026, 10, 1, 0);
  const ticks = timeTicks("24h", start, start + 24 * HOUR, 8);
  assert.ok(ticks.length >= 5, ticks.map((t) => t.label).join());
  assert.ok(ticks.every((t) => t.t >= start && t.t <= start + 24 * HOUR));
  assert.ok(ticks.every((t) => new Date(t.t).getHours() % 4 === 0 && new Date(t.t).getMinutes() === 0), "ticks stay on the local 4 h grid");
});

test("day ticks stay on local midnights across a DST change", () => {
  const start = Date.UTC(2026, 9, 29, 12);
  const ticks = timeTicks("7d", start, start + 7 * 24 * HOUR, 8);
  assert.ok(ticks.length >= 6);
  assert.ok(ticks.every((t) => new Date(t.t).getHours() === 0 && new Date(t.t).getMinutes() === 0));
});

test("a fetch deadline is a failure that retries; a deliberate abort is dropped", () => {
  assert.ok(FETCH_DEADLINE_MS > 0 && FETCH_DEADLINE_MS < SERIES_REFRESH_MS);
  const timeout = settleFetch(null, { cancelled: false, timedOut: true });
  assert.match(timeout.note, /too long/);
  const stale = { endMs: 5, points: [] };
  const view = nextHistoryView(stale, timeout, 0);
  assert.equal(view.series, stale);
  assert.equal(view.staleAs, 5);
  assert.equal(nextHistoryView(null, timeout, 0).note, timeout.note);
  assert.equal(settleFetch(null, { cancelled: true, timedOut: false }), null);
  assert.equal(settleFetch(null, { cancelled: true, timedOut: true }), null, "closed wins over a late deadline");
  const ok = { series: stale };
  assert.equal(settleFetch(ok, { cancelled: false, timedOut: false }), ok);
});

test("the header's accessible name says when the reading is held", () => {
  assert.equal(nowLabel({ state: "live", stamp: "" }), "Current value");
  assert.equal(nowLabel({ state: "held", stamp: "as of 3:04 pm" }), "Last reading as of 3:04 pm");
  assert.match(nowLabel({ state: "off", stamp: "" }), /No current/);
});
