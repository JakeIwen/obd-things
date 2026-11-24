/**
 * Pure rules for the full chart dialog (`dialogs/Chart.jsx`): the span vocabulary, the history
 * route's request and reply, axis ticks and the geometry-free chart model the canvas draws.
 *
 * The span list, path and refresh interval are `projects/vehicle_data/history_series.json`, the
 * contract the route and this file share, so they cannot drift. Times are epoch milliseconds;
 * the axis shows the browser's local clock times. Display only: nothing here raises a warning.
 */

import vocabulary from "../../history_series.json" with { type: "json" };
import { THOUSANDS, decimalsFor, fmtClock, fmtDate, fmtFixed } from "./format.js";
import { RECENT_METRICS } from "./historySeed.helpers.js";
import { parseEpochMs } from "./ring.js";

export const SERIES_PATH = vocabulary.path;
export const SERIES_REFRESH_MS = vocabulary.refresh_seconds * 1000;
export const DEFAULT_SPAN = vocabulary.default_span;
/** The spans in the contract's order: `{key, label, seconds, source: "live"|"history"}`. */
export const SPANS = Object.freeze(vocabulary.spans.map((span) => Object.freeze({ ...span })));

/** The live (browser ring) span redraws at most this often while the dialog is open. */
export const LIVE_REDRAW_MS = 5000;
/** A live gap: no sample for this long lifts the pen (the ring holds samples every 1 to 4 s). */
const LIVE_GAP_MS = 30000;
/** Two history buckets further apart than this many bucket widths leave a gap. */
const HISTORY_GAP_BUCKETS = 1.6;

const MINUTE = 60000;
const HOUR = 3600000;
const DAY = 86400000;
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** The span a metric without a browser ring opens on (a drive's warm-up fits in it). */
export const RINGLESS_DEFAULT_SPAN = "24h";
/** First retry after a "pending" answer (the broker is still preparing its products), well under the normal refresh. */
export const PENDING_RETRY_MS = 5000;
/** One history fetch (request and body) gets this long before it counts as a failure and the retry path runs. */
export const FETCH_DEADLINE_MS = 15000;
/** A retained series this many refresh intervals old is dimmed even when the route only said "pending". */
const STALE_AFTER_REFRESHES = 2;

/** @param {string} key a span key @returns {object|undefined} */
export function spanByKey(key) {
  return SPANS.find((span) => span.key === key);
}

/** Does the browser keep a 15 min ring for this metric (history_recent.json)? Only then is the live span offered. */
export function hasLiveRing(name) {
  return RECENT_METRICS.includes(name);
}

/** The spans a metric's chart offers: all of them with a ring, the history ones without. */
export function spansFor(name) {
  return hasLiveRing(name) ? SPANS : SPANS.filter((span) => span.source !== "live");
}

/** The span to open on: the one last chosen when this metric offers it, else the default for its kind. */
export function startSpanFor(name, last) {
  if (spansFor(name).some((span) => span.key === last)) return last;
  return hasLiveRing(name) ? DEFAULT_SPAN : RINGLESS_DEFAULT_SPAN;
}

/** The accessible name of the header's reading: a held value is not the current one. */
export function nowLabel(reading) {
  return reading.state === "held" ? "Last reading " + reading.stamp : reading.state === "off" ? "No current reading" : "Current value";
}

/**
 * The header's current reading under the tile's freshness rules (`Tile.jsx`): a live value plainly,
 * a held one dimmed with the time it was last seen, an absent one as the dash.
 * @param {{kind: string, valueText: string, unitText: string, heldText: string}} input the tile model's values
 * @returns {{state: "live"|"held"|"off", valueText: string, unitText: string, stamp: string}}
 */
export function nowReading({ kind, valueText, unitText, heldText }) {
  if (kind === "live") return { state: "live", valueText, unitText, stamp: "" };
  if (kind === "held") return { state: "held", valueText, unitText, stamp: heldText ? "as of " + heldText : "held" };
  return { state: "off", valueText, unitText, stamp: "" };
}

/**
 * What the dialog shows after one history fetch, given what it showed before.
 * A usable answer replaces the series. A failure after a success keeps the old series but marks it
 * stale (`staleAs` is its "as of" time, for a dimmed plot and a visible notice); a failure with
 * nothing to keep says why. "Pending" (the broker is preparing its products) is loading, retried
 * quickly; the retained series is only marked stale once it is clearly older than the refresh cadence.
 * @param {object|null} series the series on screen, if any
 * @param {{series?: object, note?: string, pending?: boolean}} result `loadHistory`'s answer
 * @param {number} ageMs how long ago the series on screen arrived (monotonic)
 * @returns {{series: object|null, note: string, staleAs: number|null, retryMs: number|null}}
 */
export function nextHistoryView(series, result, ageMs) {
  if (result.series) return { series: result.series, note: "", staleAs: null, retryMs: null };
  if (result.pending) {
    const old = series && ageMs > STALE_AFTER_REFRESHES * SERIES_REFRESH_MS;
    return { series, note: series ? "" : "Preparing history…", staleAs: old ? series.endMs : null, retryMs: PENDING_RETRY_MS };
  }
  return { series, note: series ? "" : result.note || unavailableText(""), staleAs: series ? series.endMs : null, retryMs: null };
}

/**
 * What an aborted or finished fetch means. A deliberate cancel (dialog closed, span changed) is
 * dropped; the deadline's own abort is an ordinary failure, so the stale/retry path still runs
 * instead of the dialog waiting on a request that never settles.
 * @param {object|null} result `loadHistory`'s answer (null when its signal aborted)
 * @param {{cancelled: boolean, timedOut: boolean}} how why the signal aborted, if it did
 * @returns {object|null} the result to show, or null for nothing to do
 */
export function settleFetch(result, { cancelled, timedOut }) {
  if (cancelled) return null;
  if (result) return result;
  return timedOut ? { note: unavailableText("timeout") } : null;
}

/** The request for one metric and span, from the contract's path and query keys. */
export function seriesUrl(metric, spanKey) {
  const query = new URLSearchParams();
  query.set(vocabulary.query_keys[0], metric);
  query.set(vocabulary.query_keys[1], spanKey);
  return SERIES_PATH + "?" + query.toString();
}

// ---------------------------------------------------------------------------
// value axis

/** Decimals needed to write a tick step exactly (0.5 gives 1, 0.25 gives 2, 20 gives 0). */
export function stepDecimals(step) {
  for (let d = 0; d <= 6; d += 1) {
    const scaled = step * 10 ** d;
    if (Math.abs(scaled - Math.round(scaled)) < 1e-9 * Math.max(1, Math.abs(scaled))) return d;
  }
  return 6;
}

/**
 * Round tick values covering `[lo, hi]`, at most `maxCount` of them (the finest 1/2/5 step that
 * allows). The first and last ticks lie on or outside the data, so the domain they span always
 * contains it.
 * @returns {{ticks: number[], step: number, lo: number, hi: number}}
 */
export function niceTicks(lo, hi, maxCount = 6) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi < lo) return { ticks: [], step: 0, lo: 0, hi: 0 };
  if (hi === lo) { lo -= 0.5; hi += 0.5; }
  const raw = (hi - lo) / Math.max(1, maxCount - 1);
  const base = 10 ** Math.floor(Math.log10(raw));
  let step = base;
  for (const factor of [1, 2, 5, 10, 20]) {
    step = factor * base;
    const count = Math.ceil(hi / step - 1e-9) - Math.floor(lo / step + 1e-9) + 1;
    if (count <= maxCount) break;
  }
  const first = Math.floor(lo / step + 1e-9);
  const last = Math.ceil(hi / step - 1e-9);
  const decimals = stepDecimals(step);
  const ticks = [];
  for (let i = first; i <= last; i += 1) ticks.push(Number((i * step).toFixed(decimals)));
  return { ticks, step, lo: ticks[0], hi: ticks[ticks.length - 1] };
}

/** A tick's text: the metric's tile decimals, widened when the step needs more. */
export function formatTick(name, value, step, decimals) {
  const base = typeof decimals === "number" ? decimals : decimalsFor(name);
  return fmtFixed(value, Math.max(base, stepDecimals(step)), THOUSANDS.has(name));
}

// ---------------------------------------------------------------------------
// time axis

/** Candidate tick steps, finest first; a span starts at its preferred one and coarsens to fit. */
const STEP_LADDER = Object.freeze([
  MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE,
  HOUR, 2 * HOUR, 3 * HOUR, 4 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY,
]);
/** The tick step each span prefers when there is room (5 min for 15 min, hourly for 6 h, daily for 7 d). */
const PREFERRED_STEP = Object.freeze({ "15m": 5 * MINUTE, "1h": 10 * MINUTE, "6h": HOUR, "24h": 4 * HOUR, "7d": DAY });

/** Local UTC offset in ms at an instant (positive east of Greenwich). */
export const localOffsetMs = (ms) => -new Date(ms).getTimezoneOffset() * MINUTE;

/** The step to use for a window: the span's preferred one, coarsened until at most `maxTicks` fit. */
export function chooseTimeStep(spanKey, windowMs, maxTicks) {
  const wanted = PREFERRED_STEP[spanKey] || windowMs / 5;
  let i = STEP_LADDER.findIndex((step) => step >= wanted);
  if (i < 0) i = STEP_LADDER.length - 1;
  while (i < STEP_LADDER.length - 1 && windowMs / STEP_LADDER[i] > maxTicks) i += 1;
  return STEP_LADDER[i];
}

function hourLabel(d) {
  const h = d.getHours();
  return (h % 12 === 0 ? 12 : h % 12) + (h < 12 ? " am" : " pm");
}

/** Label for a tick at `ms`: clock time under an hour's step, hour (date at midnight) under a day's, weekday and day above. */
export function formatTimeTick(ms, step) {
  const d = new Date(ms);
  if (step >= DAY) return WEEKDAYS[d.getDay()] + " " + d.getDate();
  if (step >= HOUR) return d.getHours() === 0 && d.getMinutes() === 0 ? fmtDate(d, d.getFullYear()) : hourLabel(d);
  return fmtClock(d);
}

/**
 * Tick times (local-clock aligned) inside `[startMs, endMs]`, with their labels.
 * @param {string} spanKey
 * @param {number} startMs
 * @param {number} endMs
 * @param {number} maxTicks the most labels the plot width has room for
 * @param {(ms: number) => number} [offsetAt] local UTC offset, injectable for tests
 * @returns {{t: number, label: string}[]}
 */
export function timeTicks(spanKey, startMs, endMs, maxTicks, offsetAt = localOffsetMs) {
  if (!(endMs > startMs)) return [];
  const step = chooseTimeStep(spanKey, endMs - startMs, Math.max(2, maxTicks));
  const ticks = [];
  if (step >= DAY) {
    const days = step / DAY;
    const from = new Date(startMs);
    for (let k = 0; ; k += 1) {
      const t = new Date(from.getFullYear(), from.getMonth(), from.getDate() + k * days).getTime();
      if (t > endMs) break;
      if (t >= startMs) ticks.push({ t, label: formatTimeTick(t, step) });
    }
    return ticks;
  }
  // Elapsed time, not a local-clock loop: across a DST change the wall clock repeats or skips an
  // hour, and stepping it loses ticks. The first tick is aligned on the local clock; later ones keep
  // the step and, from an hour up, snap back onto the local clock after the offset changes.
  const first = startMs + offsetAt(startMs);
  let t = Math.ceil(first / step) * step - offsetAt(startMs);
  const settled = Math.ceil((startMs + offsetAt(t)) / step) * step - offsetAt(t);
  if (settled >= startMs) t = settled;
  while (t <= endMs) {
    if (t >= startMs) ticks.push({ t, label: formatTimeTick(t, step) });
    let next = t + step;
    if (step >= HOUR && offsetAt(next) !== offsetAt(t)) {
      const off = offsetAt(next);
      const snapped = Math.round((next + off) / step) * step - off;
      if (snapped > t) next = snapped;
    }
    t = next;
  }
  return ticks;
}

// ---------------------------------------------------------------------------
// series

/**
 * A usable `/v1/history/series` answer as chart data, or null (a refusal, an other-shape body, or a
 * window that is not a positive length). Each bucket is placed at its middle, never past the window's end.
 * @param {unknown} reply parsed JSON
 * @returns {{kind: "history", startMs: number, endMs: number, gapMs: number,
 *   points: {t: number, mean: number, min: number, max: number}[]}|null}
 */
export function historySeries(reply) {
  if (!reply || typeof reply !== "object" || reply.available !== true || !Array.isArray(reply.points)) return null;
  const startMs = parseEpochMs(reply.start_at);
  const endMs = parseEpochMs(reply.end_at);
  const bucketMs = Number(reply.bucket_seconds) * 1000;
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs || !(bucketMs > 0)) return null;
  const points = [];
  for (const p of reply.points) {
    const mean = p && typeof p.value === "number" ? p.value : NaN;
    const at = p ? parseEpochMs(p.at) : NaN;
    if (!Number.isFinite(mean) || !Number.isFinite(at)) continue;
    const lo = typeof p.min === "number" && Number.isFinite(p.min) ? Math.min(p.min, mean) : mean;
    const hi = typeof p.max === "number" && Number.isFinite(p.max) ? Math.max(p.max, mean) : mean;
    points.push({ t: Math.min(at + bucketMs / 2, endMs), mean, min: lo, max: hi });
  }
  points.sort((a, b) => a.t - b.t);
  return { kind: "history", startMs, endMs, gapMs: bucketMs * HISTORY_GAP_BUCKETS, points };
}

/**
 * The browser ring's columns as chart data on the wall-clock axis. The tablet's clock only labels
 * the axis here (the ring itself is monotonic), so a skewed clock shifts labels, never the line.
 * @param {Array<{min: number, max: number, mean: number}|null>} cols `ring.columns(n, nowMono)`
 * @param {number} nowEpochMs right edge
 * @param {number} windowMs the ring's window
 */
export function liveSeries(cols, nowEpochMs, windowMs) {
  const colMs = windowMs / Math.max(1, cols.length);
  const points = [];
  for (let i = 0; i < cols.length; i += 1) {
    const c = cols[i];
    if (!c) continue;
    points.push({ t: nowEpochMs - windowMs + (i + 0.5) * colMs, mean: c.mean, min: c.min, max: c.max });
  }
  return { kind: "live", startMs: nowEpochMs - windowMs, endMs: nowEpochMs, gapMs: Math.max(LIVE_GAP_MS, 2.5 * colMs), points };
}

/** Split points into unbroken runs; a spacing over `gapMs` lifts the pen. */
export function splitRuns(points, gapMs) {
  const runs = [];
  let run = [];
  for (const p of points) {
    if (run.length && p.t - run[run.length - 1].t > gapMs) { runs.push(run); run = []; }
    run.push(p);
  }
  if (run.length) runs.push(run);
  return runs;
}

/** A point's min and max, each falling back to the mean when the bucket did not carry it. */
function rangeOf(p) {
  const lo = Number.isFinite(p.min) ? Math.min(p.min, p.mean) : p.mean;
  const hi = Number.isFinite(p.max) ? Math.max(p.max, p.mean) : p.mean;
  return [lo, hi];
}

/**
 * Points that stand alone (a run of one, so no envelope band can join them) but still span a
 * range: a short drive inside one 42-minute bucket. The canvas draws each as a min-max whisker.
 * @param {{t: number, mean: number, min: number, max: number}[][]} runs
 * @returns {{t: number, mean: number, min: number, max: number}[]}
 */
export function isolatedRanges(runs) {
  const out = [];
  for (const run of runs) {
    if (run.length !== 1) continue;
    const [min, max] = rangeOf(run[0]);
    if (max > min) out.push({ t: run[0].t, mean: run[0].mean, min, max });
  }
  return out;
}

/**
 * Everything the canvas needs except pixels.
 * @param {{series: object|null, name: string, spanKey: string, band: {lo: number|null, hi: number|null}|null,
 *   minSpan: number, maxTimeTicks: number, decimals?: number, offsetAt?: Function}} input
 * @returns {{empty: true, startMs: number, endMs: number, xTicks: object[]}|{empty: false, startMs: number,
 *   endMs: number, xTicks: object[], y: {lo: number, hi: number, step: number, ticks: {v: number, label: string}[]},
 *   runs: object[][], envelope: boolean, whiskers: object[], band: {lo: number, hi: number}|null}}
 */
export function buildChartModel({ series, name, spanKey, band, minSpan, maxTimeTicks, decimals, offsetAt }) {
  const startMs = series ? series.startMs : 0;
  const endMs = series ? series.endMs : 0;
  const xTicks = series ? timeTicks(spanKey, startMs, endMs, maxTimeTicks, offsetAt) : [];
  if (!series || !series.points.length) return { empty: true, startMs, endMs, xTicks };
  let lo = Infinity;
  let hi = -Infinity;
  for (const p of series.points) {
    const [pLo, pHi] = rangeOf(p);
    if (pLo < lo) lo = pLo;
    if (pHi > hi) hi = pHi;
  }
  // A per-metric minimum span (the sparkline's rule), so a steady value is not a dramatic wiggle.
  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }
  const runs = splitRuns(series.points, series.gapMs);
  const pad = (hi - lo) * 0.05;
  const nice = niceTicks(lo - pad, hi + pad, 6);
  const y = {
    lo: nice.lo,
    hi: nice.hi,
    step: nice.step,
    ticks: nice.ticks.map((v) => ({ v, label: formatTick(name, v, nice.step, decimals) })),
  };
  // The band is today's context-dependent normal range (charging voltage, RPM-dependent oil
  // pressure), so it only describes the live span; painted across a week it would be wrong.
  const inBand = series.kind === "live" && band && band.lo !== null && band.hi !== null && band.hi > y.lo && band.lo < y.hi
    ? { lo: Math.max(band.lo, y.lo), hi: Math.min(band.hi, y.hi) }
    : null;
  return {
    empty: false, startMs, endMs, xTicks, y,
    runs,
    // The min-max envelope only means something where a point summarises many samples.
    envelope: series.kind === "history",
    whiskers: series.kind === "history" ? isolatedRanges(runs) : [],
    band: inBand,
  };
}

/** What to say when the history route cannot answer. */
export function unavailableText(reason) {
  if (reason === "history_disabled") return "History recording is off on this listener.";
  if (reason === "timeout") return "History took too long to answer.";
  if (reason === "not_found") return "This broker does not serve longer spans yet.";
  return "History is not available right now.";
}
