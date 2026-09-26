/* Oil-change sheet — pure helpers (no DOM). Validation and body building for
   POST /v1/maintenance/oil-changes (api-contract.md A.3, maintenance.py validate()), the stable
   request id, the response classifier and 202 poller with injectable fetch/sleep, the refresh of
   /v1/maintenance after a save, and every piece of text the sheet shows (history rows, last oil
   change, odometer line, distance since service). Covered by test/OilChange.helpers.test.mjs.
   Ported from static/index.html #oil-change-form and app.js renderMaintenance /
   renderServiceMileage / saveOilChange; user-facing messages are the old ones unless noted. */
import { fmtFixed, fmtTime } from "../format.js";

// ---------- bounds from the backend contract ----------
export const DATE_MIN = "2000-01-01";
export const MILEAGE_MAX = 2000000;
export const MILEAGE_STEP = 0.1;
export const NOTES_MAX = 800;
export const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{12,80}$/;
export const SAVE_URL = "/v1/maintenance/oil-changes";
export const MAINTENANCE_URL = "/v1/maintenance";
export const POLL_INTERVAL_MS = 200;
export const POLL_MAX_TRIES = 35;
export const REQUEST_TIMEOUT_MS = 10000;
export const HISTORY_LIMIT = 20;

/** Select options, in the old order; `unknown` pairs with an empty mileage field. */
export const SOURCE_OPTIONS = Object.freeze([
  ["cluster_or_receipt", "Instrument cluster or service receipt"],
  ["ics_estimate", "ICS estimate shown on this dashboard"],
  ["unknown", "Not recorded (leave mileage empty)"],
]);
export const SOURCES = SOURCE_OPTIONS.map((option) => option[0]);
export const DEFAULT_SOURCE = "cluster_or_receipt";

/** History labels exactly as app.js serviceMileageLabel(). */
export const SOURCE_LABELS = Object.freeze({
  ics_estimate: "ICS estimate*",
  cluster_or_receipt: "Cluster / service receipt",
  unknown: "Mileage not recorded",
});

/** On-screen copy. The first four strings are verbatim from the old UI. */
export const TEXT = Object.freeze({
  note: "Saves a service record on the Pi for all your devices. Does not reset the vehicle’s oil-change indicator. Existing records are retained.",
  saving: "Saving…",
  saved: "Oil change saved on the Pi. The vehicle’s oil-change indicator was not reset.",
  refreshFailed: "Record saved; reload the page to view it.",
  noService: "No service recorded",
  mileageNotRecorded: "Mileage not recorded",
  odometerNote: "ICS module estimate. Recorded comparisons were about 11 miles below the cluster; no offset has been applied.",
  noOdometer: "No ICS mileage has been recorded yet.",
  acrossSources: "Distance since service is not calculated across different mileage sources.",
  // New in v2 (states the old UI left silent or reported with a generic error):
  pendingExhausted: "The Pi is still saving this record. Tap Save again; it will not be saved twice.",
  timeout: "The Pi did not answer in time. Tap Save again; it will not be saved twice.",
  loading: "Loading service records…",
  unavailable: "Service records are unavailable on the Pi right now.",
  notPersistent: "This listener cannot save service records.",
  sourceNeeded: "Choose where the mileage came from, or leave the mileage empty.",
  sourceIgnored: "No mileage entered: the record is saved without mileage.",
  mileageBad: "Enter the mileage as a number.",
  mileageStep: "Mileage can have at most one decimal place.",
  noOdometerYet: "Distance since service appears after the next odometer reading.",
  noServiceMileage: "Distance since service needs the mileage of the last oil change.",
  dateNeeded: "Enter the service date to save.",
});

// Server messages (maintenance.py validate) reused for the same conditions on the client.
export const ERRORS = Object.freeze({
  dateFormat: "Service date must be YYYY-MM-DD",
  dateRange: "Service date must be between 2000-01-01 and today",
  mileage: "Mileage must be a finite number from 0 to 2,000,000 miles",
  notes: "Notes must be at most 800 characters",
});

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad2 = (n) => (n < 10 ? "0" : "") + n;
const finite = (v) => typeof v === "number" && isFinite(v);

// ---------- dates ----------
/** The browser's local calendar date as YYYY-MM-DD (the van and the Pi share US/Mountain). */
export function localToday(now) {
  const d = now instanceof Date && !isNaN(now.getTime()) ? now : new Date();
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}

/** Parse a strict YYYY-MM-DD calendar date without time-zone shifts; null when invalid. */
export function parseServiceDate(value) {
  if (typeof value !== "string") return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]);
  const day = Number(m[3]);
  if (month < 1 || month > 12 || day < 1) return null;
  const days = new Date(year, month, 0).getDate();
  if (day > days) return null;
  return { year, month, day };
}

/** `2026-07-24` → `Jul 24, 2026`; anything unparseable is shown as given (or the dash). */
export function formatServiceDate(value) {
  const d = parseServiceDate(value);
  if (!d) return typeof value === "string" && value ? value : "—";
  return MONTHS[d.month - 1] + " " + d.day + ", " + d.year;
}

// ---------- numbers ----------
/** Miles with a thousands separator and at most one decimal: `52,000`, `52,000.5`. */
export function formatMiles(value) {
  if (!finite(value)) return "—";
  const s = fmtFixed(value, 1, true);
  return s.slice(-2) === ".0" ? s.slice(0, -2) : s;
}

/** Odometer reading as the old UI showed it: truncated whole miles, `53,892`. */
export function formatOdometer(value) {
  if (!finite(value)) return "—";
  return fmtFixed(Math.trunc(value), 0, true);
}

// ---------- validation ----------
/**
 * Validate the form fields and build the POST body (without `request_id`).
 * @param {{date?:string, mileage?:string|number|null, mileageBad?:boolean, source?:string, notes?:string}} fields
 *   `mileage` is the raw input value (empty = not recorded); `mileageBad` mirrors the number
 *   input's `validity.badInput` (text the browser could not parse).
 * @param {{today?:string}} [options] `today` as YYYY-MM-DD (defaults to the local date)
 * @returns {{ok:boolean, errors:{date?:string, mileage?:string, source?:string, notes?:string}, body:object|null}}
 */
export function validateOilChange(fields, options = {}) {
  const f = fields || {};
  const today = typeof options.today === "string" ? options.today : localToday();
  const errors = {};

  const date = typeof f.date === "string" ? f.date.trim() : "";
  if (!parseServiceDate(date)) errors.date = ERRORS.dateFormat;
  else if (date < DATE_MIN || date > today) errors.date = ERRORS.dateRange;

  let mileage = null;
  const raw = f.mileage === null || f.mileage === undefined ? "" : String(f.mileage).trim();
  if (f.mileageBad) errors.mileage = TEXT.mileageBad;
  else if (raw !== "") {
    const n = Number(raw);
    if (!finite(n) || n < 0 || n > MILEAGE_MAX) errors.mileage = ERRORS.mileage;
    else if (Math.abs(Math.round(n * 10) - n * 10) > 1e-6) errors.mileage = TEXT.mileageStep;
    else mileage = n;
  }

  const chosen = SOURCES.includes(f.source) ? f.source : DEFAULT_SOURCE;
  let source = "unknown";
  if (raw !== "" && !f.mileageBad) {
    if (chosen === "unknown") errors.source = TEXT.sourceNeeded;
    else source = chosen;
  }

  const notes = typeof f.notes === "string" ? f.notes.trim() : "";
  if (notes.length > NOTES_MAX) errors.notes = ERRORS.notes;

  const ok = Object.keys(errors).length === 0;
  return {
    ok,
    errors,
    body: ok ? { date, mileage_mi: mileage, mileage_source: source, notes } : null,
  };
}

/** Hint under the source select when the choice will not be stored. */
export function sourceHint(fields) {
  const f = fields || {};
  const raw = f.mileage === null || f.mileage === undefined ? "" : String(f.mileage).trim();
  return raw === "" && !f.mileageBad ? TEXT.sourceIgnored : "";
}

// ---------- request id ----------
export function isValidRequestId(id) {
  return typeof id === "string" && REQUEST_ID_PATTERN.test(id);
}

/** `oil_` + 32 hex digits from four random 32-bit words, like the old UI. */
export function makeRequestId(getRandomValues) {
  const words = new Uint32Array(4);
  const fill = typeof getRandomValues === "function" ? getRandomValues
    : typeof globalThis.crypto !== "undefined" && globalThis.crypto && typeof globalThis.crypto.getRandomValues === "function"
      ? (array) => globalThis.crypto.getRandomValues(array)
      : (array) => { for (let i = 0; i < array.length; i += 1) array[i] = Math.floor(Math.random() * 0x100000000); return array; };
  fill(words);
  let hex = "";
  for (let i = 0; i < words.length; i += 1) hex += (words[i] >>> 0).toString(16).padStart(8, "0");
  return "oil_" + hex;
}

/** Canonical signature of a body (fixed key order), used to decide whether a retry reuses its id. */
export function bodySignature(body) {
  const b = body || {};
  return JSON.stringify([b.date, b.mileage_mi, b.mileage_source, b.notes]);
}

/**
 * The submission to send: the previous pending one when the content is unchanged (so a retry after
 * a lost answer reuses its `request_id` and the Pi stores it once), otherwise a new one.
 * @returns {{signature:string, payload:object}}
 */
export function nextPending(pending, body, makeId = makeRequestId) {
  const signature = bodySignature(body);
  if (pending && pending.signature === signature && pending.payload && isValidRequestId(pending.payload.request_id)) return pending;
  return {
    signature,
    payload: {
      date: body.date,
      mileage_mi: body.mileage_mi,
      mileage_source: body.mileage_source,
      notes: body.notes,
      request_id: makeId(),
    },
  };
}

// ---------- responses and the poller ----------
/** `pending` → re-send the identical body; `ok` → saved; `error` → show `message`. */
export function classifySaveResponse(status, ok, data) {
  if (status === 202 && data && data.pending === true) return { kind: "pending" };
  if (ok && (status === 200 || status === 201) && data && data.record && typeof data.record === "object") {
    return { kind: "ok", record: data.record };
  }
  const detail = data && typeof data.detail === "string" && data.detail ? data.detail : "";
  return { kind: "error", message: detail || "HTTP " + status };
}

/** Fetch options for the POST: same-origin JSON so the Pi's Origin/Sec-Fetch-Site checks pass. */
export function saveInit(payload, timeoutMs) {
  const init = {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    mode: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
  const signal = timeoutSignal(timeoutMs);
  if (signal) init.signal = signal;
  return init;
}

function timeoutSignal(timeoutMs) {
  if (!(timeoutMs > 0) || typeof AbortSignal === "undefined" || typeof AbortSignal.timeout !== "function") return null;
  return AbortSignal.timeout(timeoutMs);
}

function isTimeout(error) {
  return Boolean(error) && (error.name === "TimeoutError" || error.name === "AbortError");
}

async function readJson(response) {
  try { return await response.json(); } catch { return null; }
}

/**
 * POST the record; a 202 {pending:true} is re-sent with the identical body every `intervalMs`, up
 * to `tries` attempts. Resolves with the saved record; rejects with a user-facing Error.
 * `deps.fetch`, `deps.sleep`, `deps.tries`, `deps.intervalMs`, `deps.timeoutMs` are injectable.
 */
export async function postOilChange(payload, deps = {}) {
  const doFetch = deps.fetch || (typeof fetch === "function" ? fetch : null);
  if (!doFetch) throw new Error("fetch is not available");
  const sleep = deps.sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const tries = deps.tries ?? POLL_MAX_TRIES;
  const intervalMs = deps.intervalMs ?? POLL_INTERVAL_MS;
  const timeoutMs = deps.timeoutMs ?? REQUEST_TIMEOUT_MS;
  for (let attempt = 0; attempt < tries; attempt += 1) {
    let response;
    try {
      response = await doFetch(SAVE_URL, saveInit(payload, timeoutMs));
    } catch (error) {
      throw new Error(isTimeout(error) ? TEXT.timeout : String((error && error.message) || error));
    }
    const data = await readJson(response);
    const verdict = classifySaveResponse(response.status, response.ok, data);
    if (verdict.kind === "pending") { await sleep(intervalMs); continue; }
    if (verdict.kind === "error") throw new Error(verdict.message);
    return verdict.record;
  }
  throw new Error(TEXT.pendingExhausted);
}

/** GET /v1/maintenance after a save; resolves with the payload or null when it cannot be read. */
export async function refreshMaintenance(deps = {}) {
  const doFetch = deps.fetch || (typeof fetch === "function" ? fetch : null);
  if (!doFetch) return null;
  try {
    const init = { cache: "no-store", credentials: "same-origin" };
    const signal = timeoutSignal(deps.timeoutMs ?? REQUEST_TIMEOUT_MS);
    if (signal) init.signal = signal;
    const response = await doFetch(MAINTENANCE_URL, init);
    if (!response.ok) return null;
    const data = await readJson(response);
    return data && typeof data === "object" && !Array.isArray(data) ? data : null;
  } catch {
    return null;
  }
}

/**
 * The whole save as the old saveOilChange did it: POST (with 202 polling), then refresh
 * /v1/maintenance. Resolves `{record, maintenance, message}`; `maintenance` is null and `message`
 * says to reload when the refresh failed (the record is saved either way). Rejects on save failure.
 */
export async function saveOilChange(payload, deps = {}) {
  const record = await postOilChange(payload, deps);
  const maintenance = await refreshMaintenance(deps);
  return { record, maintenance, message: maintenance ? TEXT.saved : TEXT.refreshFailed };
}

// ---------- maintenance payload → display ----------
/**
 * Why Save is unavailable regardless of the form, or null. `storage_error` is shown verbatim like
 * the old UI (it can be set while saving still works, so it is also returned by `storageNote`).
 */
export function saveBlockReason(maintenance) {
  if (!maintenance || typeof maintenance !== "object") return TEXT.loading;
  if (maintenance.available !== true) {
    return (typeof maintenance.storage_error === "string" && maintenance.storage_error)
      || (typeof maintenance.detail === "string" && maintenance.detail) || TEXT.unavailable;
  }
  if (maintenance.persistent !== true) return TEXT.notPersistent;
  return null;
}

/** The storage error text to show beside the form (old `oil-change-result`), or "". */
export function storageNote(maintenance) {
  return maintenance && typeof maintenance.storage_error === "string" ? maintenance.storage_error : "";
}

/** Whether Save can be pressed. */
export function canSave(maintenance, saving, validation) {
  return !saving && Boolean(validation && validation.ok) && saveBlockReason(maintenance) === null;
}

export function sourceLabel(source) {
  return SOURCE_LABELS[source] || SOURCE_LABELS.unknown;
}

/**
 * One history row as the old list printed it — `date · 52,000 mi · Cluster / service receipt ·
 * notes` — plus the parts for a two-line layout.
 */
export function historyRow(record, index = 0) {
  const r = record || {};
  const date = formatServiceDate(r.date);
  const mileage = finite(r.mileage_mi) ? formatMiles(r.mileage_mi) + " mi" : "";
  const source = sourceLabel(r.mileage_source);
  const notes = typeof r.notes === "string" ? r.notes : "";
  const meta = (mileage ? mileage + " · " : "") + source;
  return {
    key: isValidRequestId(r.request_id) ? r.request_id : "row-" + index,
    date,
    mileage,
    source,
    notes,
    meta,
    text: date + " · " + meta + (notes ? " · " + notes : ""),
  };
}

/** Newest-first rows (the broker sends ≤ 20) and a count line when older records exist. */
export function historyRows(maintenance) {
  const list = maintenance && Array.isArray(maintenance.oil_changes) ? maintenance.oil_changes : [];
  const rows = list.filter((r) => r && typeof r === "object").slice(0, HISTORY_LIMIT).map(historyRow);
  const total = maintenance && finite(maintenance.record_count) ? maintenance.record_count : rows.length;
  return {
    rows,
    more: total > rows.length ? "Showing the newest " + rows.length + " of " + total + " records." : "",
  };
}

/** `Last oil change: <date> · <mileage>` pieces as the old panel showed them, plus its notes. */
export function lastOilChangeSummary(maintenance) {
  const last = maintenance && maintenance.last_oil_change && typeof maintenance.last_oil_change === "object"
    ? maintenance.last_oil_change : null;
  if (!last) return { recorded: false, date: TEXT.noService, mileage: "—", source: "", notes: "" };
  return {
    recorded: true,
    date: formatServiceDate(last.date),
    mileage: finite(last.mileage_mi) ? formatMiles(last.mileage_mi) + " mi" : TEXT.mileageNotRecorded,
    source: finite(last.mileage_mi) ? sourceLabel(last.mileage_source) : "",
    notes: typeof last.notes === "string" ? last.notes : "",
  };
}

/**
 * Which record source an odometer reading is comparable with. The broker's `vehicle.odometer` is
 * the ICS estimate (`ics.did.2001`); a reading without a source is that metric too.
 */
export function odometerSourceKind(source) {
  if (source === null || source === undefined || source === "") return "ics_estimate";
  const s = String(source).toLowerCase();
  if (s === "ics_estimate" || s === "ics" || s.startsWith("ics.") || s.startsWith("ics_")) return "ics_estimate";
  if (s.includes("cluster")) return "cluster_or_receipt";
  return null;
}

function usableOdometer(odometer) {
  return odometer && typeof odometer === "object" && finite(odometer.value) ? odometer : null;
}

/**
 * The reading the sheet uses: the `odometer` prop when it has a value, else the maintenance
 * payload's retained `last_known_odometer` (old renderServiceMileage: live → recorded → retained).
 */
export function effectiveOdometer(odometer, maintenance) {
  const retained = maintenance && typeof maintenance === "object" ? maintenance.last_known_odometer : null;
  return usableOdometer(odometer) || usableOdometer(retained);
}

/** Odometer shown in the sheet (old `service-odometer` + its tooltip, as text). */
export function odometerLine(odometer, now) {
  const odo = usableOdometer(odometer);
  if (!odo) return { value: "—", detail: TEXT.noOdometer, note: TEXT.odometerNote };
  const unit = typeof odo.unit === "string" && odo.unit ? odo.unit : "mi";
  const when = odo.live === true ? "Latest ICS reading" : odo.observed_at ? "Last reading " + fmtTime(odo.observed_at, now) : "Last reading";
  return { value: formatOdometer(odo.value) + " " + unit, detail: when, note: TEXT.odometerNote };
}

/**
 * The derived "miles since service" line (old `oil-distance`). Returns `{kind, text}`:
 * `none` (no service recorded, text ""), `distance`, `negative`, `across_sources`,
 * `no_odometer` or `no_mileage`. A distance is only computed when the odometer and the record
 * share a source (today: both the ICS estimate). `odometer.live === true` drops the
 * " at last reading" suffix, as the old UI did for a reading ≤ 15 s old.
 */
export function milesSinceService(odometer, lastOilChange) {
  const last = lastOilChange && typeof lastOilChange === "object" ? lastOilChange : null;
  if (!last) return { kind: "none", text: "", miles: null };
  if (!finite(last.mileage_mi) || last.mileage_source === "unknown") {
    return { kind: "no_mileage", text: TEXT.noServiceMileage, miles: null };
  }
  const odo = usableOdometer(odometer);
  const kind = odo ? odometerSourceKind(odo.source) : "ics_estimate";
  if (kind !== last.mileage_source) return { kind: "across_sources", text: TEXT.acrossSources, miles: null };
  if (!odo) return { kind: "no_odometer", text: TEXT.noOdometerYet, miles: null };
  if (odo.unit && odo.unit !== "mi") return { kind: "across_sources", text: TEXT.acrossSources, miles: null };
  const distance = odo.value - last.mileage_mi;
  const ics = kind === "ics_estimate";
  if (distance < 0) {
    return {
      kind: "negative",
      text: ics ? "Service mileage exceeds the latest ICS reading; check the entry."
        : "Service mileage exceeds the latest odometer reading; check the entry.",
      miles: distance,
    };
  }
  return {
    kind: "distance",
    text: formatMiles(distance) + (ics ? " estimated miles since service" : " miles since service") + (odo.live === true ? "" : " at last reading"),
    miles: distance,
  };
}

function newestRecordedAt(maintenance) {
  if (!maintenance || typeof maintenance !== "object") return "";
  const list = Array.isArray(maintenance.oil_changes) ? maintenance.oil_changes : [];
  let best = "";
  for (const r of list) {
    const t = r && typeof r.recorded_at === "string" ? r.recorded_at : "";
    if (t > best) best = t;
  }
  const last = maintenance.last_oil_change;
  if (last && typeof last.recorded_at === "string" && last.recorded_at > best) best = last.recorded_at;
  return best;
}

/**
 * The fresher of two maintenance payloads (the prop from the store and the copy the sheet fetched
 * right after a save): more records wins, then the newest `recorded_at`; ties keep `a`.
 */
export function newerMaintenance(a, b) {
  if (!b || typeof b !== "object") return a || null;
  if (!a || typeof a !== "object") return b;
  const ca = finite(a.record_count) ? a.record_count : -1;
  const cb = finite(b.record_count) ? b.record_count : -1;
  if (cb !== ca) return cb > ca ? b : a;
  return newestRecordedAt(b) > newestRecordedAt(a) ? b : a;
}

/** Characters used in the notes field, for the `n / 800` hint. */
export function notesCount(notes) {
  const n = typeof notes === "string" ? notes.length : 0;
  return n + " / " + NOTES_MAX;
}
