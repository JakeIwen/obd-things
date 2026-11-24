// Labels are local clock times; pin the zone so the expected text does not depend on the host.
process.env.TZ = "UTC";
import { test } from "node:test";
import assert from "node:assert/strict";
import vocabulary from "../../history_series.json" with { type: "json" };
import {
  DEFAULT_SPAN, SERIES_PATH, SERIES_REFRESH_MS, SPANS, buildChartModel, chooseTimeStep, formatTick,
  historySeries, liveSeries, niceTicks, seriesUrl, splitRuns, stepDecimals, timeTicks, unavailableText,
} from "../src/chart.helpers.js";

// Why: owner request 2026-11-23: every graph tile opens a larger chart with axis labels and spans.

const UTC = () => 0;
const MIN = 60000;
const HOUR = 3600000;
const END = Date.UTC(2026, 10, 23, 15, 32);

test("the span list, path and refresh come from the shared contract", () => {
  assert.equal(SERIES_PATH, vocabulary.path);
  assert.equal(SERIES_REFRESH_MS, vocabulary.refresh_seconds * 1000);
  assert.deepEqual(SPANS.map((s) => s.key), vocabulary.spans.map((s) => s.key));
  assert.equal(DEFAULT_SPAN, "15m");
  assert.equal(seriesUrl("engine.coolant_temperature", "24h"), SERIES_PATH + "?metric=engine.coolant_temperature&span=24h");
});

test("niceTicks gives round, evenly spaced ticks that contain the data", () => {
  for (const [lo, hi] of [[198, 203], [0, 100], [13.8, 14.4], [1500, 4200], [-5, 7], [41.2, 43.9]]) {
    const t = niceTicks(lo, hi, 6);
    assert.ok(t.ticks.length >= 2 && t.ticks.length <= 6, `${lo}-${hi}: ${t.ticks}`);
    assert.ok(t.lo <= lo && t.hi >= hi, `${lo}-${hi} contained by ${t.lo}-${t.hi}`);
    assert.ok(t.ticks.every((v, i) => i === 0 || Math.abs(v - t.ticks[i - 1] - t.step) < 1e-9), "even steps");
  }
  assert.deepEqual(niceTicks(0, 100, 6).ticks, [0, 20, 40, 60, 80, 100]);
  assert.deepEqual(niceTicks(13.8, 14.4, 6).ticks, [13.8, 14, 14.2, 14.4]);
  assert.ok(niceTicks(5, 5).ticks.length > 1, "a flat range still spans");
  assert.deepEqual(niceTicks(NaN, 3).ticks, []);
});

test("tick text uses the metric's decimals, widened when the step needs more", () => {
  assert.equal(stepDecimals(0.25), 2);
  assert.equal(stepDecimals(20), 0);
  assert.equal(formatTick("engine.rpm", 2500, 500), "2,500");
  assert.equal(formatTick("engine.coolant_temperature", 200, 5), "200");
  assert.equal(formatTick("battery.voltage", 14, 0.2, 1), "14.0");
  assert.equal(formatTick("engine.coolant_temperature", 200.5, 0.5), "200.5");
});

test("time ticks follow the span: 5 min, 10 min, hourly, 4 hourly, daily", () => {
  const ticks = (key, windowMs, max = 8) => timeTicks(key, END - windowMs, END, max, UTC);
  assert.deepEqual(ticks("15m", 15 * MIN).map((t) => t.label), ["3:20 pm", "3:25 pm", "3:30 pm"]);
  assert.deepEqual(ticks("1h", HOUR).map((t) => t.label), ["2:40 pm", "2:50 pm", "3:00 pm", "3:10 pm", "3:20 pm", "3:30 pm"]);
  assert.deepEqual(ticks("6h", 6 * HOUR).map((t) => t.label), ["10 am", "11 am", "12 pm", "1 pm", "2 pm", "3 pm"]);
  const day = ticks("7d", 7 * 24 * HOUR);
  assert.equal(day.length, 7);
  assert.ok(day.every((t) => /^[A-Z][a-z]{2} \d+$/.test(t.label)));
  assert.ok(day.every((t, i) => i === 0 || t.t - day[i - 1].t === 24 * HOUR));
});

test("a 24 h axis labels hours and shows the date at local midnight", () => {
  const labels = timeTicks("24h", END - 24 * HOUR, END, 8, UTC).map((t) => t.label);
  assert.deepEqual(labels, ["4 pm", "8 pm", "Nov 23", "4 am", "8 am", "12 pm"].map((l) => l));
});

test("ticks respect the local offset and the window, and thin out when space is short", () => {
  process.env.TZ = "America/Chicago";
  const t = timeTicks("1h", END - HOUR, END, 8);
  process.env.TZ = "UTC";
  assert.deepEqual(t.map((x) => x.label), ["8:40 am", "8:50 am", "9:00 am", "9:10 am", "9:20 am", "9:30 am"]);
  assert.ok(t.every((x) => x.t >= END - HOUR && x.t <= END));
  assert.equal(chooseTimeStep("15m", 15 * MIN, 8), 5 * MIN);
  assert.equal(chooseTimeStep("15m", 15 * MIN, 2), 10 * MIN);
  assert.ok(chooseTimeStep("6h", 6 * HOUR, 3) >= 2 * HOUR);
  assert.deepEqual(timeTicks("15m", 5, 5, 8), []);
});

test("a history reply becomes points at bucket middles, clipped to the window, with min and max", () => {
  const reply = {
    available: true, metric: "engine.coolant_temperature", span: "1h", window_seconds: 3600, bucket_seconds: 60,
    start_at: "2026-11-23T14:00:00Z", end_at: "2026-11-23T15:00:00Z",
    points: [
      { at: "2026-11-23T14:58:00Z", value: 201, min: 199, max: 203 },
      { at: "2026-11-23T14:00:00Z", value: 190, min: 188, max: 192 },
      { at: "2026-11-23T14:59:50Z", value: 202 },
      { at: "2026-11-23T14:30:00Z", value: "bad" },
    ],
  };
  const s = historySeries(reply);
  assert.equal(s.kind, "history");
  assert.equal(s.endMs - s.startMs, 3600000);
  assert.equal(s.points.length, 3);
  assert.equal(s.points[0].t, s.startMs + 30000);
  assert.equal(s.points[2].t, s.endMs, "the newest bucket is clamped to the window's end");
  assert.deepEqual([s.points[1].min, s.points[1].max], [199, 203]);
  assert.deepEqual([s.points[2].min, s.points[2].max], [202, 202], "no min/max falls back to the mean");
});

test("refusals and malformed replies give no series", () => {
  assert.equal(historySeries({ available: false, reason: "history_disabled" }), null);
  assert.equal(historySeries(null), null);
  assert.equal(historySeries({ available: true, points: [], start_at: "x", end_at: "y", bucket_seconds: 60 }), null);
  assert.equal(historySeries({ available: true, points: [], start_at: "2026-11-23T15:00:00Z", end_at: "2026-11-23T14:00:00Z", bucket_seconds: 60 }), null);
  assert.match(unavailableText("history_disabled"), /off/);
  assert.match(unavailableText("not_found"), /does not serve/);
});

test("a missing bucket lifts the pen; live columns map onto the wall-clock window", () => {
  const pts = [0, 60, 120, 300, 360].map((s) => ({ t: s * 1000, mean: s, min: s, max: s }));
  assert.deepEqual(splitRuns(pts, 96000).map((r) => r.length), [3, 2]);
  const cols = [{ min: 1, max: 3, mean: 2 }, null, { min: 2, max: 2, mean: 2 }, { min: 4, max: 6, mean: 5 }];
  const live = liveSeries(cols, 1000000, 400);
  assert.equal(live.startMs, 999600);
  assert.deepEqual(live.points.map((p) => p.t), [999650, 999850, 999950]);
  assert.equal(live.kind, "live");
});

test("the chart model scales to the data, clips the band, and marks empties", () => {
  const series = historySeries({
    available: true, bucket_seconds: 360, start_at: "2026-11-23T00:00:00Z", end_at: "2026-11-24T00:00:00Z",
    points: [
      { at: "2026-11-23T10:00:00Z", value: 200, min: 196, max: 204 },
      { at: "2026-11-23T10:06:00Z", value: 201, min: 197, max: 205 },
      { at: "2026-11-23T18:00:00Z", value: 190, min: 185, max: 195 },
    ],
  });
  const m = buildChartModel({ series, name: "engine.coolant_temperature", spanKey: "24h", band: { lo: 170, hi: 210 }, minSpan: 20, maxTimeTicks: 8, offsetAt: UTC });
  assert.equal(m.empty, false);
  assert.equal(m.runs.length, 2, "eight hours of no buckets is a gap");
  assert.equal(m.envelope, true);
  assert.ok(m.y.lo <= 185 && m.y.hi >= 205);
  assert.ok(m.y.ticks.length >= 3 && m.y.ticks.length <= 6);
  assert.ok(m.band.lo >= m.y.lo && m.band.hi <= m.y.hi);
  assert.equal(m.xTicks.length, 7, "midnight to midnight, every 4 h, both ends");
  const steady = buildChartModel({
    series: { ...series, points: [{ t: series.startMs + 1000, mean: 14.1, min: 14.1, max: 14.1 }] },
    name: "battery.voltage", spanKey: "24h", band: null, minSpan: 0.5, maxTimeTicks: 8, decimals: 1, offsetAt: UTC,
  });
  assert.ok(steady.y.hi - steady.y.lo >= 0.5, "the minimum span keeps a flat line from filling the chart");
  assert.equal(steady.band, null);
  const empty = buildChartModel({ series: { ...series, points: [] }, name: "x", spanKey: "24h", band: null, minSpan: 1, maxTimeTicks: 8, offsetAt: UTC });
  assert.equal(empty.empty, true);
  assert.equal(empty.xTicks.length, 7, "the time axis still shows for an empty span");
});
