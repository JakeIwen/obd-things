/**
 * Parked view and Service card: pure view-model helpers (no DOM, no signals).
 *
 * Every function takes plain payload slices (`/v2/summary` history, dtcs,
 * maintenance, status_full; the store's observation state; the derived
 * warning model) plus an explicit `now`, and returns small plain objects of
 * display strings. The components wrap them in computeds whose output is
 * compared structurally, so a card re-renders only when its text changes.
 * Covered by test/parked.test.mjs.
 *
 * Wall-clock times are display-only (design 6): nothing here decides
 * freshness; that comes from derive.js / store.js.
 */

import { fmtFixed, fmtTime, fmtDuration, parseDate, DASH } from "../format.js";
import { OPEN_STATES } from "../warnings.js";
import {
  lastOilChangeSummary,
  milesSinceService,
  historyRows,
  formatOdometer,
  odometerSourceKind,
  TEXT as OIL_TEXT,
} from "../dialogs/OilChange.helpers.js";

// ---------------------------------------------------------------------------
// constants

/** The one docs link per view (provenance and caveats live there, not on cards). */
export const DOCS_URL = "/docs/caveats.html";

/** Passive voltage read (web.py accepts exactly this body). */
export const ACQUIRE_URL = "/v1/acquisitions/battery.voltage";
export const ACQUIRE_BODY = JSON.stringify({ mode: "passive" });
export const ACQUIRE_TIMEOUT_MS = 20000;

/** Parked cards in default order; ids are the customiser's card ids for the view. */
export const PARKED_CARDS = Object.freeze([
  Object.freeze({ id: "battery", label: "Battery" }),
  Object.freeze({ id: "tires", label: "Tires" }),
  Object.freeze({ id: "trip", label: "Last trip" }),
  Object.freeze({ id: "service", label: "Service" }),
  Object.freeze({ id: "warnings", label: "Warnings" }),
  Object.freeze({ id: "codes", label: "Codes" }),
]);
export const PARKED_IDS = Object.freeze(PARKED_CARDS.map((card) => card.id));

/** Hours of battery trend drawn from the summary's 15-minute buckets. */
export const TREND_WINDOW_MS = 86400000;
/** A gap longer than three buckets breaks the trend line (gaps stay visible). */
export const TREND_GAP_MS = 2700000;
/** Smallest vertical span of the trend, so a flat 12.6 V does not look like noise. */
export const TREND_MIN_SPAN = 0.5;
/** Parked normal band's lower edge (bands.js LIMITS.voltageParked.normalLo), drawn as a guide. */
export const TREND_REF_V = 12.2;

/** On-screen copy. */
export const TEXT = Object.freeze({
  noBattery: "No reading yet",
  now: "now",
  running: "engine running",
  readPrefix: "read ",
  afterOff: " after engine off",
  engineOffPrefix: "After engine off: ",
  restingDue: "Resting reading due ",
  low: "Low",
  veryLow: "Very low",
  trendTitle: "Last 24 h",
  trendEmpty: "No readings in the last 24 h",
  readNow: "Read voltage now",
  reading: "Reading…",
  done: "Done",
  refused: "Refused: ",
  wakeNote: "Listens only: nothing is sent to the van, so a read never wakes it or the dashcam.",
  acquireChecking: "Checking whether this screen may ask for a reading…",
  acquireOff: "This screen only shows saved readings; reading on request is turned off here.",
  tripLoading: "Loading trips…",
  tripUnavailable: "Trip history is not available right now.",
  noTrips: "No trips recorded yet.",
  serviceLoading: "Loading service records…",
  noOdometer: "No odometer reading yet",
  odoLive: "current reading",
  checkEntry: "Check the entry",
  checkEntrySub: "service mileage is above the odometer",
  notCompared: "Not compared",
  notComparedSub: "different mileage sources",
  noServiceMileage: "Not known",
  noServiceMileageSub: "the last oil change has no mileage",
  warningsUnavailable: "Warnings are not available right now.",
  nothingOpen: "Nothing open",
  codesLoading: "Loading codes…",
  codesUnavailable: "Codes are not available right now.",
  noCodes: "No current codes",
});

/** Plain-English endings for refused voltage reads, by broker/web reason. */
const REFUSED_REASONS = Object.freeze({
  bus_asleep: "the van is asleep, so there is no voltage traffic to read. Unlock with the fob or turn the key, then try again.",
  web_acquisition_disabled: "this screen can only show saved readings.",
  can_busy: "another tool is using the van's data bus. Try again in a minute.",
  acquisition_timeout: "the Pi did not answer in time.",
  source_unavailable: "the voltage reader is not available right now.",
  wrong_bus: "the adapter is not listening to the right bus right now.",
  invalid_request: "the Pi did not accept the request.",
  unsupported_mode: "the Pi did not accept the request.",
});

const DRIVER_QUALITIES = new Set(["verified", "observed_alfa_scale"]);

// ---------------------------------------------------------------------------
// small utilities

const finite = (v) => typeof v === "number" && isFinite(v);
const str = (v) => (typeof v === "string" && v.length > 0 ? v : null);
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);

/** Epoch ms of an ISO time, or NaN. */
export function wallMs(iso) {
  const d = parseDate(iso);
  return d === null ? NaN : d.getTime();
}

function asDate(now) {
  if (now instanceof Date && !isNaN(now.getTime())) return now;
  if (finite(now)) return new Date(now);
  return new Date();
}

function humanize(reason) {
  const s = String(reason).replace(/_/g, " ").trim();
  return s ? s.charAt(0).toLowerCase() + s.slice(1) : "";
}

/**
 * Gap between two events, coarse: `30 s`, `25 min`, `1.5 h` (floored to a
 * tenth, no trailing `.0`), `3 d`. Non-finite or negative input → dash.
 * @param {number} ms
 * @returns {string}
 */
export function fmtGap(ms) {
  if (!finite(ms) || ms < 0) return DASH;
  if (ms < 60000) return Math.floor(ms / 1000) + " s";
  if (ms < 3600000) return Math.floor(ms / 60000) + " min";
  if (ms < 86400000) {
    const tenths = Math.floor(ms / 360000);
    const whole = Math.floor(tenths / 10);
    const rest = tenths - whole * 10;
    return (rest === 0 ? String(whole) : whole + "." + rest) + " h";
  }
  return Math.floor(ms / 86400000) + " d";
}

/**
 * Stable, order-preserving layout: Battery and Tires share a row (2/3 + 1/3)
 * only when they are adjacent in the device's card order; everything else is
 * full width. Returns id → 'two' | 'one' | 'full'.
 * @param {string[]} ids visible card ids in display order
 */
export function layoutSpans(ids) {
  const spans = {};
  const list = Array.isArray(ids) ? ids : [];
  for (let i = 0; i < list.length; i += 1) spans[list[i]] = "full";
  for (let i = 0; i + 1 < list.length; i += 1) {
    const a = list[i];
    const b = list[i + 1];
    if ((a === "battery" && b === "tires") || (a === "tires" && b === "battery")) {
      spans.battery = "two";
      spans.tires = "one";
      break;
    }
  }
  return spans;
}

// ---------------------------------------------------------------------------
// battery

/**
 * The latest engine stop at or before a sample: the engine-off sampler's
 * `engine_stopped_at`, the open trip's last activity (engine just stopped),
 * or the last completed trip's end. Returns epoch ms or null.
 */
export function engineStopBefore(sampleMs, ctx) {
  if (!finite(sampleMs)) return null;
  const c = ctx || {};
  const candidates = [];
  const eov = obj(c.engineOff);
  const sample = eov ? obj(eov.last_sample) : null;
  if (sample) candidates.push(wallMs(sample.engine_stopped_at));
  const current = obj(c.currentTrip);
  if (current && !c.running) candidates.push(wallMs(current.last_active_at));
  const recent = obj(c.recentTrip);
  if (recent) candidates.push(wallMs(str(recent.ended_at) || recent.last_active_at));
  let best = null;
  for (let i = 0; i < candidates.length; i += 1) {
    const t = candidates[i];
    // 1 s tolerance: the sampler stamps the stop a moment before its own sample.
    if (!finite(t) || t > sampleMs + 1000) continue;
    if (best === null || t > best) best = t;
  }
  return best;
}

/**
 * Battery sub-line from the displayed sample itself (critique M8/A13):
 * `read 4:25 pm · 2 h after engine off`, `now · 12 min after engine off`,
 * `engine running`, or `No reading yet`.
 * @param {{kind:'live'|'held'|'off', observedAt?:string|null, running?:boolean,
 *   engineOff?:object|null, recentTrip?:object|null, currentTrip?:object|null}} input
 * @param {Date|number} [now]
 */
export function batterySubline(input, now) {
  const i = input || {};
  if (i.kind !== "live" && i.kind !== "held") return TEXT.noBattery;
  if (i.kind === "live" && i.running) return TEXT.running;
  const nowDate = asDate(now);
  const at = wallMs(i.observedAt);
  let head;
  if (i.kind === "live") head = TEXT.now;
  else head = finite(at) ? TEXT.readPrefix + fmtTime(i.observedAt, nowDate) : "";
  if (i.running || !finite(at)) return head || TEXT.noBattery;
  const stop = engineStopBefore(at, i);
  if (stop === null) return head;
  const tail = fmtGap(at - stop) + TEXT.afterOff;
  return head ? head + " · " + tail : tail;
}

/**
 * Second battery line: the broker's settled engine-off reading when it is not
 * the sample already on screen (`After engine off: 12.70 V · 4:25 pm`), or
 * the time a pending resting reading is due. Empty string otherwise.
 * @param {object|null} engineOff `status_full.engine_off_voltage`
 * @param {string|null} displayedAt `observed_at` of the displayed sample
 * @param {Date|number} [now]
 */
export function engineOffLine(engineOff, displayedAt, now) {
  const eov = obj(engineOff);
  if (!eov || eov.enabled === false) return "";
  const nowDate = asDate(now);
  if (eov.state === "collecting" && str(eov.settles_at)) {
    return TEXT.restingDue + fmtTime(eov.settles_at, nowDate);
  }
  const s = obj(eov.last_sample);
  if (!s || !finite(s.value) || (s.unit && s.unit !== "V")) return "";
  if (!str(s.observed_at) || s.observed_at === displayedAt) return "";
  return TEXT.engineOffPrefix + fmtFixed(s.value, 2) + " V · " + fmtTime(s.observed_at, nowDate);
}

/**
 * Card badge for the parked battery band (derive gates settled/neutral).
 * @param {string} state band state
 * @returns {{text:string, tone:''|'amber'|'red'}}
 */
export function batteryBadge(state) {
  if (state === "red") return { text: TEXT.veryLow, tone: "red" };
  if (state === "amber") return { text: TEXT.low, tone: "amber" };
  return { text: "", tone: "" };
}

/**
 * Class list for the big battery reading.
 * @param {'live'|'held'|'off'} kind
 * @param {string} bandState
 */
export function readingClass(kind, bandState) {
  let c = "reading num";
  if (kind === "held") c += " reading--held";
  if (kind === "off") return c + " parked-reading--off";
  if (bandState === "red") c += " reading--red";
  else if (bandState === "amber") c += " reading--amber";
  return c;
}

function round1(v) {
  return Math.round(v * 10) / 10;
}

/**
 * 24 h trend geometry for an inline SVG polyline (attributes only).
 * Buckets come from `history.metric_trends[metric].sparkline` (15-min means);
 * the window ends at `history.generated_at` (or the newest bucket). The line
 * breaks across gaps longer than `gapMs`; a lone bucket is drawn as a short
 * dash. The y range has a minimum span and always includes nothing but data;
 * the parked 12.2 V guide is returned only when it falls inside that range.
 * @returns {{empty:boolean, width:number, height:number,
 *   segments:Array<{key:string, points:string}>, refY:number|null, caption:string}}
 */
export function trendModel(history, options) {
  const o = options || {};
  const metric = o.metric || "battery.voltage";
  const width = o.width || 300;
  const height = o.height || 56;
  const windowMs = o.windowMs || TREND_WINDOW_MS;
  const gapMs = o.gapMs || TREND_GAP_MS;
  const minSpan = o.minSpan || TREND_MIN_SPAN;
  const ref = finite(o.ref) ? o.ref : TREND_REF_V;
  const unit = o.unit || "V";
  const empty = { empty: true, width, height, segments: [], refY: null, caption: TEXT.trendEmpty };
  const h = obj(history);
  const trends = h ? obj(h.metric_trends) : null;
  const trend = trends ? obj(trends[metric]) : null;
  const raw = trend && Array.isArray(trend.sparkline) ? trend.sparkline : [];
  const pts = [];
  for (let i = 0; i < raw.length; i += 1) {
    const p = obj(raw[i]);
    if (!p) continue;
    const t = wallMs(p.at);
    if (finite(t) && finite(p.value)) pts.push({ t, v: p.value });
  }
  if (!pts.length) return empty;
  pts.sort((a, b) => a.t - b.t);
  const generated = h ? wallMs(h.generated_at) : NaN;
  const end = Math.max(finite(generated) ? generated : -Infinity, pts[pts.length - 1].t);
  const start = end - windowMs;
  const shown = pts.filter((p) => p.t >= start);
  if (!shown.length) return empty;
  let lo = Infinity;
  let hi = -Infinity;
  for (let i = 0; i < shown.length; i += 1) {
    if (shown[i].v < lo) lo = shown[i].v;
    if (shown[i].v > hi) hi = shown[i].v;
  }
  const dataLo = lo;
  const dataHi = hi;
  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }
  const pad = 3;
  const x = (t) => round1(((t - start) / windowMs) * width);
  const y = (v) => round1(pad + (height - 2 * pad) * (1 - (v - lo) / (hi - lo)));
  const segments = [];
  let current = [];
  let firstT = null;
  const flush = () => {
    if (!current.length) return;
    let points;
    if (current.length === 1) {
      const p = current[0];
      points = round1(Math.max(0, p.x - 1.5)) + "," + p.y + " " + round1(Math.min(width, p.x + 1.5)) + "," + p.y;
    } else {
      points = current.map((p) => p.x + "," + p.y).join(" ");
    }
    segments.push({ key: String(firstT), points });
    current = [];
  };
  for (let i = 0; i < shown.length; i += 1) {
    const p = shown[i];
    if (i > 0 && p.t - shown[i - 1].t > gapMs) flush();
    if (!current.length) firstT = p.t;
    current.push({ x: x(p.t), y: y(p.v) });
  }
  flush();
  const refY = ref >= lo && ref <= hi ? y(ref) : null;
  const caption = TEXT.trendTitle + " · " + fmtFixed(dataLo, 1) + "–" + fmtFixed(dataHi, 1) + " " + unit;
  return { empty: false, width, height, segments, refY, caption };
}

// ---------------------------------------------------------------------------
// read voltage now

/**
 * Whether this listener lets the page ask for a passive read.
 * @param {object} web `store.web` flags
 * @returns {{enabled:boolean, note:string}}
 */
export function acquireAvailability(web) {
  const w = obj(web);
  if (!w || !Object.prototype.hasOwnProperty.call(w, "active_acquisition_enabled")) {
    return { enabled: false, note: TEXT.acquireChecking };
  }
  if (w.active_acquisition_enabled === true) return { enabled: true, note: "" };
  return { enabled: false, note: TEXT.acquireOff };
}

/** Fetch options for the read: same-origin JSON with exactly `{"mode":"passive"}`. */
export function acquireInit(signal) {
  const init = {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: ACQUIRE_BODY,
  };
  if (signal) init.signal = signal;
  return init;
}

/**
 * Outcome of a read: `done` (with the value when the broker returned one) or
 * `refused` with a plain-English reason.
 * @returns {{kind:'done'|'refused', text:string}}
 */
export function classifyAcquire(status, ok, data) {
  const d = obj(data) || {};
  if (ok && d.available === true) {
    const u = str(d.unit) || "V";
    return { kind: "done", text: finite(d.value) ? TEXT.done + " · " + fmtFixed(d.value, 2) + " " + u : TEXT.done };
  }
  const reason = str(d.reason);
  let why = null;
  if (reason === "rate_limited") {
    const m = /([0-9]+(?:\.[0-9]+)?)\s*seconds?/.exec(str(d.detail) || "");
    why = m ? "asked too recently. Try again in " + Math.max(1, Math.ceil(Number(m[1]))) + " s." : "asked too recently. Try again shortly.";
  } else if (reason && REFUSED_REASONS[reason]) {
    why = REFUSED_REASONS[reason];
  } else {
    why = str(d.detail) || (reason ? humanize(reason) : "the Pi answered HTTP " + status + ".");
  }
  return { kind: "refused", text: TEXT.refused + why };
}

function timeoutSignal(ms) {
  if (typeof AbortSignal === "undefined" || typeof AbortSignal.timeout !== "function") return null;
  return AbortSignal.timeout(ms);
}

/**
 * POST the passive read. Never throws: resolves `{kind, text}`.
 * @param {{fetch?:Function, signal?:AbortSignal|null}} [deps]
 */
export async function readVoltage(deps) {
  const d = deps || {};
  const doFetch = d.fetch || (typeof fetch === "function" ? fetch : null);
  if (!doFetch) return { kind: "refused", text: TEXT.refused + "this browser cannot send the request." };
  const signal = d.signal !== undefined ? d.signal : timeoutSignal(ACQUIRE_TIMEOUT_MS);
  let response;
  try {
    response = await doFetch(ACQUIRE_URL, acquireInit(signal));
  } catch (error) {
    const timedOut = Boolean(error) && (error.name === "TimeoutError" || error.name === "AbortError");
    return { kind: "refused", text: TEXT.refused + (timedOut ? "the Pi did not answer in time." : "the Pi could not be reached.") };
  }
  let data = null;
  try {
    data = await response.json();
  } catch (_error) {
    data = null;
  }
  return classifyAcquire(response.status, response.ok, data);
}

// ---------------------------------------------------------------------------
// last trip

/**
 * Last completed trip (`65 min · ended 4:25 pm`) and, while one is open, the
 * current trip. Per-trip stats wait for the broker (critique minor 14).
 * @param {object|null} history `summary.history`
 * @param {{running?:boolean, now?:Date|number}} [ctx]
 * @returns {{state:'loading'|'unavailable'|'ok', text:string,
 *   last:{duration:string, ended:string}|null, current:{duration:string, text:string}|null}}
 */
export function lastTripModel(history, ctx) {
  const c = ctx || {};
  const nowDate = asDate(c.now);
  const h = obj(history);
  if (!h) return { state: "loading", text: TEXT.tripLoading, last: null, current: null };
  if (h.available === false) return { state: "unavailable", text: TEXT.tripUnavailable, last: null, current: null };
  let current = null;
  const cur = obj(h.current_trip);
  if (cur && cur.state !== "complete") {
    const start = wallMs(cur.started_at);
    const lastActive = wallMs(cur.last_active_at);
    const end = c.running ? nowDate.getTime() : finite(lastActive) ? lastActive : nowDate.getTime();
    let seconds = finite(start) ? (end - start) / 1000 : null;
    if (seconds === null && finite(cur.duration_seconds)) seconds = cur.duration_seconds;
    const text = c.running
      ? finite(start) ? "started " + fmtTime(cur.started_at, nowDate) : "under way"
      : finite(lastActive) ? "engine off " + fmtTime(cur.last_active_at, nowDate) : "";
    current = { duration: seconds === null ? DASH : fmtDuration(Math.max(0, seconds)), text };
  }
  let last = null;
  const trips = Array.isArray(h.recent_trips) ? h.recent_trips : [];
  for (let i = 0; i < trips.length; i += 1) {
    const t = obj(trips[i]);
    if (!t || (t.state && t.state !== "complete")) continue;
    const endIso = str(t.ended_at) || str(t.last_active_at);
    let seconds = finite(t.duration_seconds) ? t.duration_seconds : null;
    if (seconds === null && endIso && finite(wallMs(t.started_at))) seconds = (wallMs(endIso) - wallMs(t.started_at)) / 1000;
    last = {
      duration: seconds === null ? DASH : fmtDuration(Math.max(0, seconds)),
      ended: endIso ? "ended " + fmtTime(endIso, nowDate) : "",
    };
    break;
  }
  return { state: "ok", text: !current && !last ? TEXT.noTrips : "", last, current };
}

/** `last drive 4:25 pm` from the newest completed trip, or "". */
export function lastDriveText(history, now) {
  const h = obj(history);
  const trips = h && Array.isArray(h.recent_trips) ? h.recent_trips : [];
  const t = obj(trips[0]);
  const endIso = t ? str(t.ended_at) || str(t.last_active_at) : null;
  return endIso ? "last drive " + fmtTime(endIso, asDate(now)) : "";
}

// ---------------------------------------------------------------------------
// service (shared by Parked and Health)

function newer(a, b) {
  if (!a) return b;
  if (!b) return a;
  return wallMs(b.observed_at) > wallMs(a.observed_at) ? b : a;
}

/**
 * The odometer reading the Service card shows and hands to the oil-change
 * sheet: `{value, unit, source, observed_at, quality, live}` or null.
 * Live = the broker's record is available and not stale (any quality; the
 * odometer is a candidate estimate and is marked with `*`, never hidden).
 * Otherwise the newest of the derived held sample and the maintenance
 * payload's retained `last_known_odometer`.
 * @param {{rec?:object|null, state?:object|null, maintenance?:object|null}} input
 *   `rec` = raw store record, `state` = derive `obs('vehicle.odometer')` value
 */
export function odometerReading(input) {
  const i = input || {};
  const rec = obj(i.rec);
  if (rec && rec.available === true && rec.stale !== true && finite(rec.value)) {
    return {
      value: rec.value,
      unit: str(rec.unit) || "mi",
      source: str(rec.source),
      observed_at: str(rec.observed_at),
      quality: str(rec.quality),
      live: true,
    };
  }
  let held = null;
  const state = obj(i.state);
  if (state && (state.kind === "held" || state.kind === "live") && state.retained && finite(state.retained.value)) {
    const lr = rec ? obj(rec.last_recorded) : null;
    const source = lr && lr.observed_at === state.retained.observed_at ? str(lr.source) : rec ? str(rec.source) : null;
    held = {
      value: state.retained.value,
      unit: str(state.retained.unit) || "mi",
      source,
      observed_at: str(state.retained.observed_at),
      quality: str(state.quality),
      live: false,
    };
  }
  const m = obj(i.maintenance);
  const kept = m ? obj(m.last_known_odometer) : null;
  const retained = kept && finite(kept.value)
    ? {
        value: kept.value,
        unit: str(kept.unit) || "mi",
        source: str(kept.source),
        observed_at: str(kept.observed_at),
        quality: str(kept.quality),
        live: false,
      }
    : null;
  return newer(held, retained);
}

/** Odometer text: value, `*` for estimates, held flag and its time line. */
export function odometerView(reading, now) {
  const r = obj(reading);
  if (!r || !finite(r.value)) return { text: DASH, star: false, held: false, sub: TEXT.noOdometer };
  const unit = str(r.unit) || "mi";
  return {
    text: formatOdometer(r.value) + " " + unit,
    star: !DRIVER_QUALITIES.has(r.quality),
    held: r.live !== true,
    sub: r.live === true ? TEXT.odoLive : r.observed_at ? "last read " + fmtTime(r.observed_at, asDate(now)) : "",
  };
}

/**
 * Miles since the last oil change, only when the odometer and the record
 * share a mileage source (legacy renderServiceMileage rule, via the sheet's
 * helper). Different sources or a record without mileage give a short
 * "Not compared" / "Not known" row; null when there is no record or odometer.
 * @returns {{text:string, sub:string, warn:boolean}|null}
 */
export function sinceServiceView(reading, lastOilChange) {
  const r = obj(reading);
  const result = milesSinceService(r, lastOilChange);
  if (result.kind === "distance") {
    const ics = odometerSourceKind(r ? r.source : null) === "ics_estimate";
    const parts = [];
    if (ics) parts.push("estimated");
    if (!r || r.live !== true) parts.push("at last reading");
    return { text: fmtFixed(result.miles, 0, true) + " mi", sub: parts.join(" · "), warn: false };
  }
  if (result.kind === "negative") return { text: TEXT.checkEntry, sub: TEXT.checkEntrySub, warn: true };
  // Legacy printed why no distance is shown; keep that as one short row rather than dropping it.
  if (result.kind === "across_sources") return { text: TEXT.notCompared, sub: TEXT.notComparedSub, warn: false };
  if (result.kind === "no_mileage") return { text: TEXT.noServiceMileage, sub: TEXT.noServiceMileageSub, warn: false };
  return null;
}

/**
 * Oil-life row, only when the maintenance payload says a source exists or the
 * `engine.oil_life_remaining` metric has a live or dated reading.
 * @param {object|null} maintenance
 * @param {object|null} state derive `obs('engine.oil_life_remaining')` value
 * @returns {{text:string, sub:string}|null}
 */
export function oilLifeView(maintenance, state, now) {
  const m = obj(maintenance);
  const ol = m ? obj(m.oil_life) : null;
  const s = obj(state);
  // Parity: the production card showed `engine.oil_life_remaining` whenever the catalog metric had
  // a live or dated reading, even while the maintenance payload still reports no source.
  const metricHasValue = Boolean(s && s.kind !== "off" && finite(s.value));
  if ((!ol || ol.available !== true) && !metricHasValue) return null;
  let value = null;
  let at = null;
  const keys = ["value", "percent", "remaining_percent"];
  for (let i = 0; ol && ol.available === true && i < keys.length; i += 1) {
    if (finite(ol[keys[i]])) {
      value = ol[keys[i]];
      at = str(ol.observed_at);
      break;
    }
  }
  if (value === null && metricHasValue) {
    value = s.value;
    at = s.kind === "held" && s.retained ? str(s.retained.observed_at) : null;
  }
  return {
    text: value === null ? DASH : fmtFixed(value, 0) + " %",
    sub: at ? "last read " + fmtTime(at, asDate(now)) : "",
  };
}

/**
 * Everything the Service card prints.
 * @param {object|null} maintenance `summary.maintenance`
 * @param {object|null} reading from `odometerReading`
 * @param {object|null} oilState derive `obs('engine.oil_life_remaining')` value
 * @param {Date|number} [now]
 */
export function serviceModel(maintenance, reading, oilState, now) {
  const nowDate = asDate(now);
  const m = obj(maintenance);
  const odometer = odometerView(reading, nowDate);
  if (!m) {
    return { state: "loading", note: TEXT.serviceLoading, odometer, last: null, since: null, oilLife: null, history: { rows: [], more: "" } };
  }
  if (m.available === false) {
    const note = str(m.storage_error) || str(m.detail) || OIL_TEXT.unavailable;
    return { state: "unavailable", note, odometer, last: null, since: null, oilLife: null, history: { rows: [], more: "" } };
  }
  const summary = lastOilChangeSummary(m);
  const last = {
    recorded: summary.recorded,
    text: summary.recorded ? summary.date + (summary.mileage && summary.mileage !== DASH ? " · " + summary.mileage : "") : summary.date,
    notes: summary.notes,
  };
  const rows = historyRows(m);
  return {
    state: "ok",
    note: str(m.storage_error) || "",
    odometer,
    last,
    since: summary.recorded ? sinceServiceView(reading, m.last_oil_change) : null,
    oilLife: oilLifeView(m, oilState, nowDate),
    history: {
      rows: rows.rows.map((row) => ({ key: row.key, date: row.date, meta: row.meta, notes: row.notes })),
      more: rows.more,
    },
  };
}

// ---------------------------------------------------------------------------
// warnings and codes summaries (both link to Health)

/**
 * Warnings summary from derive's `warningModel`: open count or
 * `Nothing open · last drive 4:25 pm`, and the first two items.
 * @returns {{available:boolean, line:string, badge:{text:string, tone:string}|null,
 *   items:Array<{id:string, title:string, meta:string, tone:string}>, more:string}}
 */
export function warningsSummary(model, history, now) {
  const m = obj(model);
  if (!m || m.available === false) {
    return { available: false, line: TEXT.warningsUnavailable, badge: null, items: [], more: "" };
  }
  const cards = Array.isArray(m.warnings) ? m.warnings : [];
  let open = 0;
  let critical = false;
  for (let i = 0; i < cards.length; i += 1) {
    if (!OPEN_STATES.has(cards[i].state)) continue;
    open += 1;
    if (cards[i].tier === "critical") critical = true;
  }
  let line;
  if (open > 0) line = open + " open";
  else {
    const drive = lastDriveText(history, now);
    line = drive ? TEXT.nothingOpen + " · " + drive : TEXT.nothingOpen;
  }
  const items = [];
  for (let i = 0; i < cards.length && items.length < 2; i += 1) {
    const card = cards[i];
    const isOpen = OPEN_STATES.has(card.state);
    items.push({
      id: String(card.id),
      title: str(card.title) || "Warning",
      meta: isOpen ? (card.sinceLabel ? "since " + card.sinceLabel : "") : str(card.line) || "",
      tone: card.tier === "critical" && isOpen ? "red" : isOpen ? "amber" : "",
    });
  }
  return {
    available: true,
    line,
    badge: open > 0 ? { text: String(open), tone: critical ? "red" : "amber" } : null,
    items,
    more: cards.length > items.length ? "+" + (cards.length - items.length) + " more" : "",
  };
}

function dtcGroup(dtcs, name) {
  const groups = obj(dtcs.groups);
  if (groups && Array.isArray(groups[name])) return groups[name];
  return Array.isArray(dtcs[name]) ? dtcs[name] : [];
}

/**
 * Codes summary: current count (from `group_counts`, since groups are
 * truncated) with the first two current titles and the last scan time.
 * @returns {{available:boolean, line:string, sub:string, badge:{text:string, tone:string}|null,
 *   items:Array<{key:string, code:string, title:string}>, more:string}}
 */
export function codesSummary(dtcs, now) {
  const d = obj(dtcs);
  if (!d) return { available: false, line: TEXT.codesLoading, sub: "", badge: null, items: [], more: "" };
  if (d.available === false) return { available: false, line: TEXT.codesUnavailable, sub: "", badge: null, items: [], more: "" };
  const counts = obj(d.group_counts) || {};
  const list = dtcGroup(d, "current");
  const current = finite(counts.current) ? counts.current : list.length;
  const pending = finite(counts.pending) ? counts.pending : 0;
  let line = current > 0 ? current + " current" : TEXT.noCodes;
  if (pending > 0) line += " · " + pending + " pending";
  const coverage = obj(d.coverage);
  const scanned = coverage ? str(coverage.last_success_at) : null;
  const items = [];
  for (let i = 0; i < list.length && items.length < 2; i += 1) {
    const r = obj(list[i]);
    if (!r) continue;
    const code = str(r.fca_display) || str(r.raw_dtc) || "";
    items.push({
      key: (str(r.module_key) || "module") + ":" + (str(r.raw_dtc) || code || String(i)),
      code,
      title: str(r.description) || str(r.module_name) || "",
    });
  }
  return {
    available: true,
    line,
    sub: scanned ? "last scan " + fmtTime(scanned, asDate(now)) : "",
    badge: current > 0 ? { text: String(current), tone: "amber" } : null,
    items,
    more: current > items.length ? "+" + (current - items.length) + " more" : "",
  };
}

/**
 * Structural-equality gate for computed view models: returns the previous
 * object when the new one serialises identically, so a signal holding it does
 * not notify its subscribers. One instance per model.
 */
export function createStableGate() {
  let lastKey = null;
  let last;
  return (next) => {
    const key = JSON.stringify(next);
    if (key === lastKey) return last;
    lastKey = key;
    last = next;
    return next;
  };
}
