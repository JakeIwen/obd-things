// Illustrative answers for GET /v1/history/series (the full chart dialog's longer spans), for the
// demo captures. Every number is synthetic. The series follow the demo scene: they exist only
// inside the same trips the History page lists (the broker records while the engine runs), warm up
// from a cold start toward the scene's highway values, and in the current drive's last 15 minutes
// they follow the scene's sparkline knots, so the chart's 1 h span agrees with the Drive tile.
import historySeries from "../../history_series.json" with {type: "json"};
import {HOUR_MS, MINUTE_MS, iso} from "./demo_data.mjs";
import {EARLIER_TRIPS} from "./demo_views.mjs";

/** Where each metric starts a cold drive and how fast it warms (minutes), by name. */
const WARM_UP = Object.freeze({
  "engine.coolant_temperature": {cold: 62, minutes: 9},
  "engine.vvt_oil_temperature": {cold: 58, minutes: 14},
  "transmission.oil_temperature": {cold: 60, minutes: 18},
  "engine.oil_pressure": {cold: 63, minutes: 6},
  // Tyres gain a few psi as they warm (History's tyre trends have no sparkline, so no knots).
  "tire.pressure.fl": {cold: 61, minutes: 25}, "tire.pressure.fr": {cold: 62, minutes: 25},
  "tire.pressure.rl": {cold: 76, minutes: 25}, "tire.pressure.rr": {cold: 75, minutes: 25},
});
/** Wobble amplitude (display units) around a steady cruise, so buckets have a min-max envelope. */
const WOBBLE = Object.freeze({
  "engine.coolant_temperature": 1.5, "engine.vvt_oil_temperature": 1.5, "transmission.oil_temperature": 1.5,
  "engine.oil_pressure": 2, "battery.voltage": 0.12, "generator.field_duty": 6, "engine.crankshaft_power": 14,
  "engine.rpm": 120, "vehicle.speed": 4,
  "tire.pressure.fl": 0.4, "tire.pressure.fr": 0.4, "tire.pressure.rl": 0.4, "tire.pressure.rr": 0.4,
});
const SAMPLES_PER_BUCKET = 6;

/** A stable pseudo-random number in [-1, 1) from two integers (no Math.random: captures repeat). */
function noise(a, b) {
  let h = Math.imul(a ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul(b + 0x7f4a7c15, 0xc2b2ae35);
  h ^= h >>> 15;
  h = Math.imul(h, 0x2c1b3c6d);
  h ^= h >>> 12;
  return ((h >>> 0) / 0x100000000) * 2 - 1;
}

const hash = (text) => [...text].reduce((h, c) => (Math.imul(h, 31) + c.charCodeAt(0)) | 0, 7);

/** The drives [startMs, stopMs] the scene's History lists, newest first; the current one ends now. */
export function demoDriveWindows(scene) {
  const current = scene.nowMs - scene.trip.elapsedMinutes * MINUTE_MS;
  return [[current, scene.nowMs],
    ...EARLIER_TRIPS.map(([hoursBefore, minutes]) => [current - hoursBefore * HOUR_MS, current - hoursBefore * HOUR_MS + minutes * MINUTE_MS])];
}

/** The metric's value `minutesIn` minutes into a drive, before wobble. */
function profile(name, scene, minutesIn, minutesFromNow, current) {
  const knots = scene.sparklines[name] || [scene.metrics[name].value];
  const warm = WARM_UP[name];
  // A drive's steady value before the scene's own trace begins: the trace's first knot.
  const cruise = knots[0];
  // The scene's trace covers the current drive's last 14 minutes, interpolated as the Drive tile seeds it.
  if (current && minutesFromNow <= 14) {
    const position = (1 - minutesFromNow / 14) * (knots.length - 1);
    const left = Math.floor(position);
    const right = Math.min(left + 1, knots.length - 1);
    return knots[left] + (knots[right] - knots[left]) * (position - left);
  }
  if (!warm) return cruise;
  return warm.cold + (cruise - warm.cold) * (1 - Math.exp(-minutesIn / warm.minutes));
}

function bucketPoint(name, scene, windows, bucketStart, bucketMs) {
  const samples = [];
  for (let k = 0; k < SAMPLES_PER_BUCKET; k += 1) {
    const t = bucketStart + ((k + 0.5) / SAMPLES_PER_BUCKET) * bucketMs;
    const driveIndex = windows.findIndex(([start, stop]) => t >= start && t < stop);
    if (driveIndex < 0) continue;
    const [start] = windows[driveIndex];
    const minutesIn = (t - start) / MINUTE_MS;
    const fromNow = (scene.nowMs - t) / MINUTE_MS;
    const wobble = (WOBBLE[name] || 0) * noise(hash(name) + k, Math.floor(t / 1000));
    // Inside the scene's own trace the knots already carry the shape; keep the wobble small there.
    const damp = driveIndex === 0 && fromNow <= 14 ? 0.3 : 1;
    samples.push(profile(name, scene, minutesIn, fromNow, driveIndex === 0) + wobble * damp);
  }
  if (!samples.length) return null;
  const mean = samples.reduce((sum, v) => sum + v, 0) / samples.length;
  return {at: iso(bucketStart), value: mean, min: Math.min(...samples), max: Math.max(...samples)};
}

/** One reply for the metric and span, in the route's shape. */
export function demoSeriesReply(scene, name, span) {
  const windowMs = span.seconds * 1000;
  const bucketMs = span.bucket_seconds * 1000;
  const windows = demoDriveWindows(scene);
  // Buckets end at the newest whole bucket, as the broker aligns them; the window ends at "now".
  const startMs = scene.nowMs - windowMs;
  const points = [];
  for (let at = startMs; at < scene.nowMs; at += bucketMs) {
    const point = bucketPoint(name, scene, windows, at, bucketMs);
    if (point) points.push(point);
  }
  return {available: true, metric: name, span: span.key, window_seconds: span.seconds, bucket_seconds: span.bucket_seconds,
    start_at: iso(startMs), end_at: iso(scene.nowMs), queried_at: iso(scene.nowMs), points};
}

/**
 * The metrics a chart can open on in the demo: every sparkline tile, plus the tyre pressures that
 * History's trend cards open (no sparkline, so the chart offers history spans only).
 */
export function demoSeriesMetrics(scene) {
  const tyres = Object.keys(scene.metrics).filter((name) => name.startsWith("tire.pressure."));
  return [...Object.keys(scene.sparklines), ...tyres];
}

/** Route-table entries (the shape demo_capture's answerDemoRequest matches, with `query`) for every history span of every demo chart metric. */
export function demoSeriesRoutes(scene) {
  const spans = historySeries.spans.filter((span) => span.source === "history");
  return demoSeriesMetrics(scene).flatMap((name) => spans.map((span) => ({
    method: "GET", pathname: historySeries.path, prefix: false, status: 200,
    query: `${historySeries.query_keys[0]}=${encodeURIComponent(name)}&${historySeries.query_keys[1]}=${span.key}`,
    json: demoSeriesReply(scene, name, span),
  })));
}
