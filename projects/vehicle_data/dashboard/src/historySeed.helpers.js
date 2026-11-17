/**
 * Pure rules that turn `GET /v1/history/recent` into ring seed points (owner request 2026-11-17: the
 * Drive sparklines started empty after a page load). The contract (path, 15-minute window,
 * 60-second buckets, the metric list) is `projects/vehicle_data/history_recent.json`, read by the
 * route and by this file, so the two cannot drift.
 *
 * Clock rule: the tablet's wall clock is never trusted. The reply's `generated_at` is the Pi's clock
 * when it was served, so a point's age is `generated_at - at`; the plan maps that onto the
 * monotonic axis from one `{nowMono, nowEpochMs}` pair whose epoch side is the Pi's time (the reply's
 * `generated_at` plus half the measured round trip), which makes any tablet/Pi skew irrelevant.
 */

import vocabulary from "../../history_recent.json" with { type: "json" };
import { parseEpochMs } from "./ring.js";

export const RECENT_PATH = vocabulary.path;
export const RECENT_METRICS = Object.freeze([...vocabulary.metrics]);
export const WINDOW_MS = vocabulary.window_seconds * 1000;
export const BUCKET_MS = vocabulary.bucket_seconds * 1000;
/** Spacing of the points joining two adjacent buckets: finer than a sparkline column (about 16 s). */
export const JOIN_STEP_MS = 5000;
/** Adjacent buckets are one bucket apart; allow some slack before calling it a gap. */
const ADJACENT_MS = BUCKET_MS * 1.5;

/**
 * Join one metric's bucket means into a line: each bucket becomes a point at its middle, and two
 * buckets one minute apart are joined by linearly interpolated points. A missing minute is not
 * bridged, so an engine-off pause between two drives stays a gap (the sparkline lifts its pen).
 * Points after `nowEpochMs` are clamped to it and points before the window are dropped.
 * @param {{at: string|number, value: number}[]} buckets `at` is the bucket start
 * @param {number} nowEpochMs the Pi's time at the reply's receipt
 * @returns {{t: number, value: number}[]} epoch-ms points, oldest first
 */
export function joinBuckets(buckets, nowEpochMs) {
  const middles = [];
  for (const bucket of Array.isArray(buckets) ? buckets : []) {
    if (!bucket || typeof bucket.value !== "number" || !Number.isFinite(bucket.value)) continue;
    const start = parseEpochMs(bucket.at);
    if (!Number.isFinite(start)) continue;
    const t = Math.min(start + BUCKET_MS / 2, nowEpochMs);
    if (t < nowEpochMs - WINDOW_MS) continue;
    middles.push({ t, value: bucket.value });
  }
  middles.sort((a, b) => a.t - b.t);
  const points = [];
  for (let i = 0; i < middles.length; i += 1) {
    const point = middles[i];
    const last = points[points.length - 1];
    if (last && point.t === last.t) continue; // two buckets clamped onto "now"
    if (last && point.t - last.t <= ADJACENT_MS) {
      for (let t = last.t + JOIN_STEP_MS; t < point.t; t += JOIN_STEP_MS) {
        points.push({ t, value: last.value + ((point.value - last.value) * (t - last.t)) / (point.t - last.t) });
      }
    }
    points.push(point);
  }
  return points;
}

/**
 * What to seed from one reply, or null when the reply is not a usable current-contract answer
 * (any other shape, `available` false, a different window, bucket or `at_basis`): the caller then seeds nothing.
 * @param {unknown} reply parsed JSON
 * @param {{sentMono: number, receivedMono: number}} receipt `performance.now()` around the fetch
 * @returns {{mapping: {nowMono: number, nowEpochMs: number}, metrics: Record<string, {t: number, value: number}[]>}|null}
 */
export function seedPlan(reply, receipt) {
  if (!reply || typeof reply !== "object" || reply.available !== true) return null;
  if (reply.window_seconds !== vocabulary.window_seconds || reply.bucket_seconds !== vocabulary.bucket_seconds) return null;
  // `joinBuckets` places a point from its bucket start; any other basis would be plotted wrongly.
  if (reply.at_basis !== vocabulary.at_basis) return null;
  if (!reply.metrics || typeof reply.metrics !== "object") return null;
  const generated = parseEpochMs(reply.generated_at);
  const roundTrip = receipt.receivedMono - receipt.sentMono;
  if (!Number.isFinite(generated) || !Number.isFinite(roundTrip) || roundTrip < 0) return null;
  const nowEpochMs = generated + roundTrip / 2;
  const metrics = {};
  for (const name of RECENT_METRICS) {
    const points = joinBuckets(reply.metrics[name], nowEpochMs);
    if (points.length) metrics[name] = points;
  }
  return { mapping: { nowMono: receipt.receivedMono, nowEpochMs }, metrics };
}
