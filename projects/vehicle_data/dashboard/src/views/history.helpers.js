/**
 * Pure view models for the History (design 3.4) views.
 * Node-testable: no Preact, no store, no DOM. The views wrap each builder in a module-level
 * `computed` over summary slices (60 s cadence), the stable status-lite projection, or one metric
 * record, and pass the result through `createStableGate()`, so a card re-renders only when its
 * visible text changed. Nothing here reads a clock; callers pass `nowMs` (wall, display only) and
 * any monotonic ages explicitly.
 *
 * Owner rules applied to every string built here: plain English, numbers first, no provenance
 * jargon (REGISTERED, ALFA SCALE, MAPPED, n/m LIVE, candidate prose), "stale" never used.
 */

import { DASH, fmtValue, fmtFixed, fmtUnit, fmtTime, fmtClock, fmtDuration, fmtDelta, decimalsFor, parseDate } from "../format.js";
import { metricLabel } from "../warningContext.js";

import { DOCS, obj, arr, num, str, wallMs, when, humanize, capital, plural, clip, createStableGate, agoText } from "./view.helpers.js";

export { DOCS, wallMs, humanize, createStableGate, agoText };

// ---------------------------------------------------------------------------
// History: cards and ordering

/** Trend metrics in the order the owner reads them; anything else follows alphabetically. */
export const TREND_ORDER = Object.freeze([
  "engine.coolant_temperature",
  "transmission.oil_temperature",
  "engine.oil_pressure",
  "engine.vvt_oil_temperature",
  "battery.voltage",
  "generator.field_duty",
  "engine.crankshaft_power",
  "tire.pressure.fl",
  "tire.pressure.fr",
  "tire.pressure.rl",
  "tire.pressure.rr",
]);

/** Friendly trend titles where warnings.js's short subject is ambiguous on its own. */
const TREND_TITLES = Object.freeze({
  "engine.vvt_oil_temperature": "Oil temperature (VVT)",
  "battery.voltage": "Battery voltage",
  "generator.field_duty": "Alternator duty",
});

/** Minimum y span of a 24 h trend so flat lines do not exaggerate noise. */
const MIN_SPAN_BY_UNIT = Object.freeze({ "°F": 20, psi: 10, V: 0.5, "%": 10, hp: 20, "lb-ft": 20 });

/** Trend card title: `Coolant`, `RR tire`, `Battery voltage`; never the raw metric id. */
export function trendTitle(metric) {
  return TREND_TITLES[metric] || metricLabel(metric);
}

/**
 * Metric names of `history.metric_trends` in display order (TREND_ORDER first).
 * @param {object|null} history `summary.history`
 * @returns {string[]}
 */
export function trendMetrics(history) {
  const h = obj(history);
  const trends = h ? obj(h.metric_trends) : null;
  if (!trends) return [];
  const names = Object.keys(trends).filter((name) => obj(trends[name]));
  const known = TREND_ORDER.filter((name) => names.indexOf(name) >= 0);
  const rest = names.filter((name) => TREND_ORDER.indexOf(name) < 0).sort();
  return known.concat(rest);
}

/** Customiser id of a trend card. */
export function trendId(metric) {
  return "trend:" + metric;
}

/**
 * History cards for the customiser and the view's default order: Trips, then one trend card per
 * metric. Before the first summary arrives the known trend list is used, so the ids are stable.
 * @param {object|null} history
 * @returns {Array<{id:string,label:string}>}
 */
export function historyCards(history) {
  const metrics = trendMetrics(history);
  const list = metrics.length ? metrics : TREND_ORDER.slice();
  return [{ id: "trips", label: "Trips" }].concat(
    list.map((metric) => ({ id: trendId(metric), label: trendTitle(metric) + " trend" })),
  );
}

// ---------------------------------------------------------------------------
// History: coverage line

const RETENTION_TROUBLE = Object.freeze({
  partial: "Old-data cleanup only partly finished",
  blocked_rollup_backlog: "Old-data cleanup is waiting for summaries to catch up",
  failed: "Old-data cleanup failed",
  error: "Old-data cleanup failed",
});

/**
 * The one coverage line of the History view plus any cleanup problems.
 * @param {object|null} history `summary.history`
 * @param {{extraMs?:number, error?:{reason?:string,detail?:string}|null}} [ctx]
 *   `extraMs`: time elapsed since the history product was generated (cache lag on the Pi clock
 *   plus time since the tablet fetched it, monotonic); `error`: `summary.error`.
 * @returns {{text:string, tone:''|'amber'|'red', problems:string[]}}
 */
export function coverageLine(history, ctx) {
  const c = ctx || {};
  const extra = num(c.extraMs) !== null && c.extraMs > 0 ? c.extraMs : 0;
  const h = obj(history);
  const problems = [];
  if (!h) {
    if (c.error) return { text: "History could not be loaded · " + humanize(c.error.reason || "no answer"), tone: "amber", problems };
    return { text: "Waiting for history…", tone: "", problems };
  }
  if (h.available === false) {
    // The broker's own reason (e.g. "telemetry history is disabled by broker configuration"), as the
    // former static app's history note printed it.
    if (str(h.detail)) problems.push(capital(clip(str(h.detail), 160)));
    return { text: "History is not available right now", tone: "amber", problems };
  }
  const cov = obj(h.coverage) || {};
  const ageSec = num(cov.age_seconds);
  const ageMs = ageSec === null ? null : ageSec * 1000 + extra;
  const lastAt = when(cov.last_snapshot_at, c.nowMs === undefined ? Date.now() : c.nowMs);
  const gaps = arr(cov.active_interface_gaps).length;
  let text;
  let tone = "";
  switch (cov.status) {
    case "current":
      text = "Recording · last sample " + agoText(ageMs);
      break;
    case "degraded":
      text =
        "Recording with gaps · " +
        (gaps ? plural(gaps, "bus link", "bus links") + " missing" : "a bus link is missing") +
        " · last sample " +
        agoText(ageMs);
      tone = "amber";
      break;
    case "stale":
      text = "Recording has stopped · last sample " + (lastAt || agoText(ageMs));
      tone = "amber";
      break;
    case "no_history":
      text = "Nothing recorded yet";
      break;
    default:
      text = lastAt ? "Last sample " + lastAt : "Recording state not reported";
  }
  const retention = obj(cov.retention) || {};
  const trouble = RETENTION_TROUBLE[retention.last_status];
  if (trouble) problems.push(trouble);
  const hook = obj(h.maintenance_hook) || {};
  if (str(hook.last_error)) problems.push("Cleanup check failed: " + clip(hook.last_error, 160));
  if (c.error) problems.push("Latest refresh failed · showing the previous history");
  return { text, tone, problems };
}

// ---------------------------------------------------------------------------
// History: trips

const END_REASONS = Object.freeze({
  activity_timeout: null, // the normal end: nothing to say
  broker_restart: "ended when the broker restarted",
  shutdown: "ended when the broker stopped",
  service_stop: "ended when the broker stopped",
});

function endNote(reason) {
  if (!str(reason)) return null;
  if (Object.prototype.hasOwnProperty.call(END_REASONS, reason)) return END_REASONS[reason];
  return "ended: " + humanize(reason);
}

/**
 * Trips card: the open trip (if any) and the last five completed trips.
 * @param {object|null} history `summary.history`
 * @param {{nowMs:number, extraMs?:number}} ctx `extraMs` as in `coverageLine`
 * @returns {{current:{duration:string, title:string, meta:string}|null, rows:Array<{id:string, when:string,
 *   duration:string, meta:string}>, emptyText:string|null}}
 */
export function tripsModel(history, ctx) {
  const c = ctx || {};
  const nowMs = num(c.nowMs) !== null ? c.nowMs : Date.now();
  const extra = num(c.extraMs) !== null && c.extraMs > 0 ? c.extraMs : 0;
  const h = obj(history);
  if (!h) return { current: null, rows: [], emptyText: "Waiting for history…" };
  let current = null;
  const open = obj(h.current_trip);
  if (open) {
    const started = when(open.started_at, nowMs);
    const secs = num(open.duration_seconds);
    const parts = [];
    if (started) parts.push("started " + started);
    if (num(open.snapshot_count) !== null) parts.push(plural(open.snapshot_count, "sample", "samples"));
    current = {
      duration: secs === null ? DASH : fmtDuration(secs + extra / 1000),
      title: "In progress",
      meta: parts.join(" · "),
    };
  }
  const rows = [];
  const recent = arr(h.recent_trips);
  for (let i = 0; i < recent.length && rows.length < 5; i += 1) {
    const t = obj(recent[i]);
    if (!t) continue;
    const started = when(t.started_at, nowMs);
    const secs = num(t.duration_seconds);
    const meta = [];
    const endClock = parseDate(t.ended_at);
    if (endClock) meta.push("ended " + fmtClock(endClock));
    if (num(t.snapshot_count) !== null) meta.push(plural(t.snapshot_count, "sample", "samples"));
    const note = endNote(t.end_reason);
    if (note) meta.push(note);
    rows.push({
      id: t.id !== undefined && t.id !== null ? String(t.id) : "trip-" + i,
      when: started || DASH,
      duration: secs === null ? DASH : fmtDuration(secs),
      meta: meta.join(" · "),
    });
  }
  const emptyText = !current && !rows.length ? "No trips recorded yet." : null;
  return { current, rows, emptyText };
}

// ---------------------------------------------------------------------------
// History: trend cards

const SPARK_W = 300;
const SPARK_H = 48;
const DAY_MS = 86400000;

function round1(v) {
  return Math.round(v * 10) / 10;
}

/**
 * 24 h sparkline geometry on a time axis. The line breaks wherever a 15-min bucket is missing
 * (gap > 1.5 buckets), so quiet periods stay visible; a lone bucket is a short dash.
 * @param {Array<{at:string, value:number}>} points
 * @param {{startMs?:number, endMs?:number, bucketMs?:number, minSpan?:number}} opts
 * @returns {{empty:boolean, width:number, height:number, segments:Array<{key:string, points:string}>,
 *   lo:number|null, hi:number|null, startMs:number|null, endMs:number|null}}
 */
export function sparkGeometry(points, opts) {
  const o = opts || {};
  const width = SPARK_W;
  const height = SPARK_H;
  const pts = [];
  const raw = arr(points);
  for (let i = 0; i < raw.length; i += 1) {
    const p = obj(raw[i]);
    if (!p) continue;
    const t = wallMs(p.at);
    const v = num(p.value);
    if (isFinite(t) && v !== null) pts.push({ t, v });
  }
  const none = { empty: true, width, height, segments: [], lo: null, hi: null, startMs: null, endMs: null };
  if (!pts.length) return none;
  pts.sort((a, b) => a.t - b.t);
  const end = num(o.endMs) !== null ? Math.max(o.endMs, pts[pts.length - 1].t) : pts[pts.length - 1].t;
  const start = num(o.startMs) !== null && o.startMs < end ? o.startMs : end - DAY_MS;
  const shown = pts.filter((p) => p.t >= start && p.t <= end);
  if (!shown.length) return none;
  let lo = Infinity;
  let hi = -Infinity;
  for (let i = 0; i < shown.length; i += 1) {
    if (shown[i].v < lo) lo = shown[i].v;
    if (shown[i].v > hi) hi = shown[i].v;
  }
  const dataLo = lo;
  const dataHi = hi;
  const minSpan = num(o.minSpan) !== null && o.minSpan > 0 ? o.minSpan : 1;
  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }
  const bucket = num(o.bucketMs) !== null && o.bucketMs > 0 ? o.bucketMs : 900000;
  const gapMs = bucket * 1.5;
  const span = end - start;
  const pad = 3;
  const x = (t) => round1(((t - start) / span) * width);
  const y = (v) => round1(pad + (height - 2 * pad) * (1 - (v - lo) / (hi - lo)));
  const segments = [];
  let run = [];
  const flush = () => {
    if (!run.length) return;
    let pointsText;
    if (run.length === 1) {
      const p = run[0];
      pointsText = round1(Math.max(0, p.x - 2)) + "," + p.y + " " + round1(Math.min(width, p.x + 2)) + "," + p.y;
    } else {
      pointsText = run.map((p) => p.x + "," + p.y).join(" ");
    }
    // Index keys: segment identity shifts with the sliding window, so reuse DOM nodes in order.
    segments.push({ key: "s" + segments.length, points: pointsText });
    run = [];
  };
  for (let i = 0; i < shown.length; i += 1) {
    const p = shown[i];
    if (i > 0 && p.t - shown[i - 1].t > gapMs) flush();
    run.push({ t: p.t, x: x(p.t), y: y(p.v) });
  }
  flush();
  return { empty: false, width, height, segments, lo: dataLo, hi: dataHi, startMs: start, endMs: end };
}

function statCells(metric, window) {
  const w = obj(window);
  if (!w) return null;
  const low = num(w.minimum);
  const avg = num(w.mean);
  const high = num(w.maximum);
  if (low === null && avg === null && high === null) return null;
  return { low: fmtValue(metric, low), avg: fmtValue(metric, avg), high: fmtValue(metric, high) };
}

function madText(metric, mad) {
  const d = decimalsFor(metric);
  const places = d === 0 && mad < 1 ? 1 : d;
  return fmtFixed(mad, places, false);
}

/**
 * One compact trend card.
 * @param {string} metric metric name
 * @param {object|null} trend `history.metric_trends[metric]`
 * @param {{nowMs:number, generatedAt?:string, tripOpen?:boolean}} ctx
 * @returns {{metric:string, title:string, unit:string, stats:Array<{label:string, low:string,
 *   avg:string, high:string}>, typical:string|null, current:string|null, spark:object,
 *   sparkCaption:string, axisStart:string, axisEnd:string, emptyText:string|null}}
 */
export function trendModel(metric, trend, ctx) {
  const c = ctx || {};
  const nowMs = num(c.nowMs) !== null ? c.nowMs : Date.now();
  const t = obj(trend) || {};
  const unit = fmtUnit(metric, str(t.unit));
  const u = unit ? " " + unit : "";
  const stats = [];
  const seven = statCells(metric, t.days_7);
  const thirty = statCells(metric, t.days_30);
  if (seven) stats.push({ label: "7 days", low: seven.low, avg: seven.avg, high: seven.high });
  if (thirty) stats.push({ label: "30 days", low: thirty.low, avg: thirty.avg, high: thirty.high });
  let typical = null;
  const prior = obj(t.prior_trips);
  if (prior && num(prior.median_of_trip_means) !== null) {
    const count = num(prior.trip_count);
    const mad = num(prior.mad_of_trip_means);
    typical =
      "Usual trip " +
      fmtValue(metric, prior.median_of_trip_means) +
      (mad !== null ? " ± " + madText(metric, mad) : "") +
      u +
      " avg" +
      (count !== null ? " · " + plural(count, "trip", "trips") : "");
  }
  let current = null;
  const cur = obj(t.current_trip);
  if (cur && num(cur.mean) !== null && c.tripOpen !== false) {
    const delta = num(t.current_minus_prior_median);
    let cmp = "";
    if (delta !== null) {
      const d = decimalsFor(metric);
      const text = fmtDelta(delta, d);
      if (text.charAt(0) === "+") cmp = " · " + text.slice(1) + " above usual";
      else if (text.charAt(0) === "−") cmp = " · " + text.slice(1) + " below usual";
      else cmp = " · same as usual";
    }
    current = "This trip " + fmtValue(metric, cur.mean) + u + " avg" + cmp;
  }
  const series = obj(t.series) || {};
  const bucketSec = num(series.bucket_seconds);
  const endMs = wallMs(series.end_at);
  const startMs = wallMs(series.start_at);
  const genMs = wallMs(c.generatedAt);
  const spark = sparkGeometry(t.sparkline, {
    startMs: isFinite(startMs) ? startMs : undefined,
    endMs: isFinite(endMs) ? endMs : isFinite(genMs) ? genMs : undefined,
    bucketMs: bucketSec === null ? undefined : bucketSec * 1000,
    minSpan: MIN_SPAN_BY_UNIT[unit],
  });
  let sparkCaption = "No readings in the last 24 h";
  let axisStart = "";
  let axisEnd = "";
  if (!spark.empty) {
    const lo = fmtValue(metric, spark.lo);
    const hi = fmtValue(metric, spark.hi);
    sparkCaption = "24 h range " + (lo === hi ? lo : lo + " to " + hi) + u;
    axisStart = fmtTime(spark.startMs, new Date(nowMs));
    axisEnd = fmtTime(spark.endMs, new Date(nowMs));
  }
  const emptyText = !stats.length && spark.empty && !typical ? "No readings in the last 30 days." : null;
  return {
    metric,
    title: trendTitle(metric),
    unit,
    stats,
    typical,
    current,
    spark: { empty: spark.empty, width: spark.width, height: spark.height, segments: spark.segments },
    sparkCaption,
    axisStart,
    axisEnd,
    emptyText,
  };
}
