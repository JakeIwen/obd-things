/**
 * Pure view models for the System (design 3.5, critique A12) views.
 * Node-testable: no Preact, no store, no DOM. The views wrap each builder in a module-level
 * `computed` over summary slices (60 s cadence), the stable status-lite projection, or one metric
 * record, and pass the result through `createStableGate()`, so a card re-renders only when its
 * visible text changed. Nothing here reads a clock; callers pass `nowMs` (wall, display only) and
 * any monotonic ages explicitly.
 *
 * Owner rules applied to every string built here: plain English, numbers first, no provenance
 * jargon (REGISTERED, ALFA SCALE, MAPPED, n/m LIVE, candidate prose), "stale" never used.
 */

import { DASH, fmtValue, fmtFixed, fmtUnit, fmtTime, fmtAgeCoarse, fmtInt, fmtDelta } from "../format.js";
import { metricLabel } from "../warningContext.js";

import { DOCS, obj, arr, num, str, wallMs, when, humanize, capital, plural, clip, createStableGate, agoText } from "./view.helpers.js";

export { DOCS, wallMs, humanize, createStableGate, agoText };
export { BASIS_TEXT, CONFIDENCE_TEXT, basisText, confidenceText, vehicleLine, canAvailabilityLine } from "./systemVehicle.helpers.js";

// ---------------------------------------------------------------------------
// System: cards

/** Customisable System cards in default order. Customise and Device & app always follow them. */
export const SYSTEM_CARDS = Object.freeze([
  Object.freeze({ id: "vehicle", label: "Vehicle state" }),
  Object.freeze({ id: "buses", label: "Buses" }),
  Object.freeze({ id: "broker", label: "Broker" }),
  Object.freeze({ id: "radar", label: "Radar alignment" }),
  Object.freeze({ id: "catalog", label: "Metric catalog" }),
]);

export const SYSTEM_CARD_IDS = Object.freeze(SYSTEM_CARDS.map((c) => c.id));

// ---------------------------------------------------------------------------
// System: buses

export const BUS_ROLES = Object.freeze(["c-can", "b-can", "can-ch"]);
const ROLE_NAMES = Object.freeze({ "c-can": "C-CAN", "b-can": "B-CAN", "can-ch": "CAN-CH", spare: "Spare" });

const CONTROLLER_TEXT = Object.freeze({
  "ERROR-ACTIVE": { text: "controller normal", tone: "" },
  "ERROR-WARNING": { text: "controller seeing errors", tone: "amber" },
  "ERROR-PASSIVE": { text: "controller seeing many errors", tone: "amber" },
  "BUS-OFF": { text: "controller off the bus", tone: "red" },
  STOPPED: { text: "controller stopped", tone: "red" },
  SLEEPING: { text: "controller sleeping", tone: "amber" },
});

function roleName(role) {
  return ROLE_NAMES[role] || String(role).toUpperCase();
}

/** Plain controller wording; `ERROR-ACTIVE` is the normal CAN state and reads `controller normal`. */
export function controllerText(state) {
  const s = str(state);
  if (!s) return { text: "", tone: "" };
  return CONTROLLER_TEXT[s.toUpperCase()] || { text: "controller " + s.toLowerCase(), tone: "amber" };
}

function modeWord(actual, full, interfaceMode) {
  if (actual.listen_only === true) return "listening";
  if (actual.listen_only === false) {
    const armed = (full && full.operating_mode === "armed_diagnostic") || interfaceMode === "armed_diagnostic";
    return armed ? "armed for diagnostics" : "transmit enabled";
  }
  if (interfaceMode === "listen_only") return "listening";
  if (interfaceMode === "armed_diagnostic") return "armed for diagnostics";
  return "";
}

function worse(a, b) {
  const rank = { "": 0, green: 0, amber: 1, red: 2 };
  return rank[b] > rank[a] ? b : a;
}

function busRow(role, lite, full, interfaceMode) {
  const name = roleName(role);
  const l = obj(lite);
  const f = obj(full);
  if (!l && !f) return { id: role, tone: "", main: name + " · not reported", side: "", meta: null };
  const src = l || f;
  const actual = (f && obj(f.actual)) || {};
  const expected = (f && obj(f.expected)) || {};
  let tone = "green";
  let link = "";
  if (src.resolution && src.resolution !== "resolved") {
    tone = "red";
    link = "adapter not found";
  }
  if (src.safe === false) tone = "red";
  if (actual.present === false) {
    tone = "red";
    link = "adapter missing";
  } else if (actual.up === false) {
    tone = "red";
    link = "link down";
  }
  // Board A went deaf on 2026-09-22 with every link check passing; the broker's
  // receive watch (receive_watch.py) is the only evidence of that failure.
  const watch = (f && obj(f.receive_watch)) || null;
  const deaf = !!(watch && watch.receive_silent === true && !link);
  if (deaf) {
    tone = "red";
    link = "hearing nothing";
  }
  if (src.passive_ready === false) tone = worse(tone, "amber");
  if (actual.fd_enabled === true) tone = worse(tone, "amber");
  const controller = controllerText(actual.controller_state);
  if (controller.tone) tone = worse(tone, controller.tone);
  const bitrate = num(actual.bitrate) !== null ? actual.bitrate : num(expected.bitrate);
  const parts = [name, link || modeWord(actual, f, interfaceMode)];
  if (bitrate !== null) parts.push(fmtInt(bitrate / 1000) + " kbit/s");
  let meta = null;
  if (tone !== "green") {
    const bits = [];
    if (actual.fd_enabled === true) bits.push("unexpected CAN FD");
    if (deaf) {
      const mins = Math.max(2, Math.floor((num(watch.silent_seconds) || 0) / 60));
      bits.push("No frames for " + mins + " min while CAN-CH is busy. Unplug the adapter's USB cable for 5 s, or reboot the Pi.");
    }
    const detail = str(f && f.detail) || (str(src.reason) && src.reason !== "ready" ? capital(humanize(src.reason)) : null);
    if (detail && !deaf) bits.push(detail);
    meta = bits.length ? bits.join(" · ") : null;
  }
  return { id: role, tone, main: parts.filter(Boolean).join(" · "), side: controller.text, meta };
}

/** One in-flight broker operation as plain words: `{metric: "battery.voltage"}` → `battery voltage`. */
function operationText(op) {
  if (typeof op === "string") return humanize(op);
  const o = obj(op);
  if (!o) return null;
  if (str(o.metric)) {
    const label = catalogLabel(o.metric);
    if (!label) return humanize(o.metric);
    // Lower-case a leading capital word ("Battery voltage") but keep acronyms ("FL tire").
    return /^[A-Z][a-z]/.test(label) ? label.charAt(0).toLowerCase() + label.slice(1) : label;
  }
  return str(o.kind) ? humanize(o.kind) : null;
}

function ownerText(owner) {
  const o = obj(owner);
  if (owner === null) return { text: "Bus use: listening only, nothing else is using the buses", tone: "" };
  if (!o) return null;
  const roles = arr(o.roles).map(roleName).join(" and ");
  switch (o.kind) {
    case "broker_active_drive":
      return { text: "Bus use: drive helper is reading" + (roles ? " on " + roles : ""), tone: "" };
    case "broker_auxiliary_drive":
      return { text: "Bus use: odometer helper is reading" + (roles ? " on " + roles : ""), tone: "" };
    case "external_inhibit":
      return { text: "Bus use: blocked by " + (arr(o.names).join(", ") || "a safety inhibit"), tone: "amber" };
    case "broker":
      // The broker's `inflight` entries are {metric, mode} objects; older payloads used strings.
      return { text: "Bus use: broker is reading " + (arr(o.operations).map(operationText).filter(Boolean).join(", ") || "now"), tone: "" };
    case "participating_or_external_can_user":
      return { text: "Bus use: another CAN tool · " + (str(o.detail) || "busy"), tone: "amber" };
    default:
      return { text: "Bus use: " + humanize(o.kind || "unknown"), tone: "" };
  }
}

function serialTail(serial) {
  const s = str(serial);
  return s ? "…" + s.slice(-6) : null;
}

/**
 * Buses card: three rows (C-CAN, B-CAN, CAN-CH) with a dot colour, safety inhibits, current bus
 * owner, issues, and adapter details for the disclosure.
 * @param {object|null} lite `store.statusLite` (roles, inhibits, interface mode)
 * @param {object|null} full `summary.statusFull` (per-role adapter facts, owner)
 */
export function busesModel(lite, full) {
  const liteIf = obj(obj(lite) && lite.interface) || {};
  const liteRI = obj(liteIf.role_interfaces) || {};
  const liteRoles = obj(liteRI.roles) || {};
  const fullObj = obj(full);
  const fullIf = obj(fullObj && fullObj.interface) || {};
  const fullRI = obj(fullIf.role_interfaces) || {};
  const reconcile = obj(fullObj && fullObj.interface_reconcile) || {};
  const fullRoles = obj(fullRI.roles) || obj(reconcile.roles) || {};
  const interfaceMode = str(liteIf.mode) || str(fullIf.mode);
  const rows = BUS_ROLES.map((role) => busRow(role, liteRoles[role], fullRoles[role], interfaceMode));

  const inhibitsRaw = Array.isArray(liteIf.active_inhibits) ? liteIf.active_inhibits : fullIf.active_inhibits;
  let inhibits;
  if (Array.isArray(inhibitsRaw)) {
    inhibits = inhibitsRaw.length
      ? { text: "Safety inhibits: " + inhibitsRaw.map(humanize).join(", "), tone: "amber" }
      : { text: "Safety inhibits: none", tone: "" };
  } else {
    inhibits = { text: "Safety inhibits: not reported", tone: "" };
  }
  const owner = fullObj && Object.prototype.hasOwnProperty.call(fullObj, "current_owner") ? ownerText(fullObj.current_owner) : null;

  const issues = [];
  const issueList = arr(liteRI.issues).length ? liteRI.issues : arr(fullRI.issues);
  for (let i = 0; i < issueList.length; i += 1) {
    const issue = issueList[i];
    const text = obj(issue) ? str(issue.detail) || humanize(issue.reason) : str(issue);
    if (text) issues.push(capital(text));
  }
  const spare = obj(liteRoles.spare) || obj(fullRoles.spare);
  if (spare && spare.safe !== true) issues.push("Spare adapter: " + (str(spare.detail) || humanize(spare.reason) || "needs attention"));

  const adapter = [];
  const roleKeys = Object.keys(fullRoles);
  for (let i = 0; i < roleKeys.length; i += 1) {
    const role = roleKeys[i];
    const f = obj(fullRoles[role]) || {};
    const e = obj(f.expected) || {};
    const a = obj(f.actual) || {};
    const bits = [roleName(role) + " → " + (str(f.channel) || "not found")];
    if (str(e.board) || str(e.connector)) bits.push("board " + [e.board, e.connector].filter(Boolean).join(" "));
    if (str(e.pair)) bits.push("pins " + e.pair);
    const serial = serialTail(e.usb_serial);
    if (serial) bits.push("serial " + serial);
    if (num(e.dev_id) !== null) bits.push("dev " + e.dev_id);
    if (str(a.controller_state)) {
      const word = controllerText(a.controller_state).text.replace(/^controller /, "");
      bits.push("controller " + a.controller_state + " (" + word + ")");
    }
    if (e.passive_required === false) bits.push("unconnected spare");
    adapter.push({ id: role, text: bits.join(" · ") });
  }
  const facts = [];
  if (typeof fullIf.adapter_present === "boolean") facts.push(["Adapter present", fullIf.adapter_present ? "yes" : "no"]);
  const usb = obj(fullObj && fullObj.usb_can_monitor);
  if (usb) {
    const count = num(usb.active_count) || 0;
    facts.push(["USB adapter watch", capital(humanize(usb.state || "unknown")) + " · " + (count ? plural(count, "open incident", "open incidents") : "no open incidents")]);
  }
  if (str(reconcile.interaction)) facts.push(["Last check", capital(reconcile.interaction)]);
  if (str(fullRI.mode)) facts.push(["Role setup", capital(humanize(fullRI.mode))]);

  let tone = "";
  for (let i = 0; i < rows.length; i += 1) tone = worse(tone, rows[i].tone === "green" ? "" : rows[i].tone);
  return { rows, inhibits, owner, issues, adapter, facts, tone };
}

// ---------------------------------------------------------------------------
// System: broker

const HELPER_REASONS = Object.freeze({
  engine_not_running: "waiting for the engine",
  engine_running: "engine running",
  disabled: "turned off",
});

function helperRow(label, helper) {
  const h = obj(helper);
  if (!h) return null;
  if (h.restoration_failed === true) {
    return { k: label, v: "Restore failed · inspect before the next drive", tone: "red" };
  }
  if (h.enabled === false) return { k: label, v: "Off", tone: "" };
  const state = capital(humanize(h.state || "unknown"));
  const reason = str(h.reason) ? HELPER_REASONS[h.reason] || humanize(h.reason) : null;
  return { k: label, v: state + (reason ? " · " + reason : ""), tone: h.state === "failed" || h.state === "error" ? "red" : "" };
}

/**
 * Broker card rows from `summary.statusFull` (refreshed once a minute, never per second).
 * Ages use the Pi's own clock (last collector cycle minus the event) so a tablet clock offset
 * cannot distort them.
 * @param {object|null} full `summary.statusFull`
 * @param {number} nowMs wall clock, for day labels only
 * @returns {{rows:Array<{k:string, v:string, tone:string}>, empty:boolean}}
 */
export function brokerModel(full, nowMs) {
  const f = obj(full);
  if (!f) return { rows: [], empty: true };
  const rows = [];
  const col = obj(f.collector) || {};
  const piNow = wallMs(col.last_cycle_at);
  const interval = num(col.interval_seconds);
  rows.push({
    k: "Collector",
    v: capital(humanize(col.state || "unknown")) + (interval !== null ? " · every " + fmtFixed(interval, interval % 1 ? 1 : 0) + "\u00a0s" : ""),
    tone: col.state && col.state !== "running" ? "amber" : "",
  });
  if (num(col.cycles) !== null) rows.push({ k: "Cycles", v: fmtInt(col.cycles), tone: "" });
  const lastCycle = when(col.last_cycle_at, nowMs);
  if (lastCycle) rows.push({ k: "Last cycle", v: lastCycle, tone: "" });
  if (str(col.failure_detail)) rows.push({ k: "Collector problem", v: clip(col.failure_detail, 200), tone: "red" });

  const rec = obj(f.history_recorder);
  if (rec) {
    const bits = [capital(humanize(rec.state || "unknown"))];
    if (num(rec.interval_seconds) !== null) bits.push("every " + fmtFixed(rec.interval_seconds, 0) + "\u00a0s");
    if (num(rec.snapshots_stored) !== null) bits.push(fmtInt(rec.snapshots_stored) + " saved");
    rows.push({ k: "History recorder", v: rec.enabled === false ? "Off" : bits.join(" · "), tone: "" });
    const saved = when(rec.last_stored_at, nowMs);
    if (saved) rows.push({ k: "Last saved", v: saved, tone: "" });
    if (str(rec.last_error)) rows.push({ k: "Recorder problem", v: clip(rec.last_error, 200), tone: "red" });
  }

  const cache = obj(f.supplemental_cache);
  if (cache) {
    const refreshed = wallMs(cache.last_refreshed_at);
    const bits = [capital(humanize(cache.state || "unknown"))];
    if (isFinite(refreshed)) {
      bits.push("updated " + fmtTime(refreshed, new Date(nowMs)));
      if (isFinite(piNow)) {
        const age = Math.max(0, piNow - refreshed);
        bits.push(age < 60000 ? "under a minute old" : fmtAgeCoarse(age) + " old");
      }
    }
    if (num(cache.refresh_interval_seconds) !== null) bits.push("every " + fmtFixed(cache.refresh_interval_seconds, 0) + "\u00a0s");
    rows.push({ k: "Summary cache", v: cache.enabled === false ? "Off" : bits.join(" · "), tone: cache.state === "ready" || cache.state === undefined ? "" : "amber" });
    if (str(cache.last_error)) rows.push({ k: "Cache problem", v: clip(cache.last_error, 200), tone: "red" });
  }

  const last = obj(f.last_readings);
  if (last) {
    if (str(last.storage_error)) rows.push({ k: "Last readings", v: "Not saving · " + clip(last.storage_error, 160), tone: "red" });
    else rows.push({ k: "Last readings", v: last.persistent ? "Saved to disk" : "Memory only (lost on restart)", tone: last.persistent ? "" : "amber" });
  }

  const drive = helperRow("Drive helper", f.active_drive);
  if (drive) rows.push(drive);
  const aux = helperRow("Odometer helper", f.auxiliary_drive);
  if (aux) rows.push(aux);

  const started = when(f.started_at, nowMs);
  if (started) rows.push({ k: "Broker started", v: started, tone: "" });
  return { rows, empty: false };
}

/**
 * Broker-card fact row for the van's own health check, from `summary.dtcs.in_vehicle_scan`
 * (harvested passively from drive recordings): `Van health check · last ran 3:12 pm (while the
 * Pi was not polling)`. Null when the broker does not report it at all.
 * @param {object|null} dtcs `summary.dtcs`
 * @param {number} nowMs wall clock, for day labels only
 * @returns {{k:string, v:string, tone:string}|null}
 */
export function vanCheckRow(dtcs, nowMs) {
  const d = obj(dtcs);
  const van = d ? obj(d.in_vehicle_scan) : null;
  if (!van) return null;
  const last = van.available === true ? obj(van.last_scan) : null;
  const at = last ? when(last.started_at || last.completed_at, nowMs) : null;
  if (!at) {
    return van.available === true || van.reason === "not_harvested"
      ? { k: "Van health check", v: "none recorded yet", tone: "" }
      : null;
  }
  return {
    k: "Van health check",
    v: "last ran " + at + (last.pi_quiet === true ? " (while the Pi was not polling)" : ""),
    tone: "",
  };
}

// ---------------------------------------------------------------------------
// System: radar alignment

export const RADAR_AXES = Object.freeze([
  Object.freeze({ id: "elevation", name: "radar.alignment.elevation", label: "Vertical (elevation)" }),
  Object.freeze({ id: "azimuth", name: "radar.alignment.azimuth", label: "Horizontal (azimuth)" }),
]);

/** Reference limit for the alignment badge (degrees). */
export const RADAR_LIMIT = 1;
/** "Approaching" starts here (degrees). */
export const RADAR_APPROACH = 0.8;

/** Signed angle: `+0.16°`, `−0.02°`, `0.00°`. */
export function fmtAngle(value) {
  const v = num(value);
  return v === null ? DASH : fmtDelta(v, 2) + "°";
}

function windowText(w) {
  const x = obj(w);
  if (!x || num(x.mean) === null || !(num(x.count) > 0)) return DASH;
  return fmtAngle(x.mean) + " (" + fmtInt(x.count) + ")";
}

/**
 * Radar alignment card.
 * @param {{axes:Object<string,{fresh:boolean, value:number|null, held:{value:number,
 *   observed_at:string}|null}>, windows:object|null, polling:object|null, nowMs:number}} input
 *   `axes[id]`: `fresh` (record available and current), `value` (fresh value), `held` (dated last
 *   reading from derive.obs); `windows`: `statusFull.radar_alignment`; `polling`:
 *   `statusFull.radar_alignment_polling`.
 */
export function radarModel(input) {
  const i = input || {};
  const nowMs = num(i.nowMs) !== null ? i.nowMs : Date.now();
  const windows = obj(i.windows) || {};
  const polling = obj(i.polling);
  const axes = [];
  let live = 0;
  let maxAbs = null;
  let newestAt = NaN;
  for (let a = 0; a < RADAR_AXES.length; a += 1) {
    const axis = RADAR_AXES[a];
    const src = obj(obj(i.axes) && i.axes[axis.id]) || {};
    const w = obj(windows[axis.name]) || {};
    let value = null;
    let at = null;
    const fresh = src.fresh === true && num(src.value) !== null;
    if (fresh) {
      value = src.value;
      live += 1;
    } else if (obj(src.held) && num(src.held.value) !== null) {
      value = src.held.value;
      at = src.held.observed_at;
    } else if (num(w.latest_value) !== null) {
      value = w.latest_value;
      at = w.observed_at;
    }
    if (value !== null) maxAbs = maxAbs === null ? Math.abs(value) : Math.max(maxAbs, Math.abs(value));
    const atMs = wallMs(at);
    if (isFinite(atMs) && !(atMs <= newestAt)) newestAt = atMs;
    let margin = DASH;
    if (value !== null) {
      const left = RADAR_LIMIT - Math.abs(value);
      margin = left >= 0 ? fmtFixed(left, 2) + "° to spare" : fmtFixed(-left, 2) + "° beyond";
    }
    const w300 = obj(w["300"]);
    axes.push({
      id: axis.id,
      label: axis.label,
      latest: fmtAngle(value),
      live: fresh,
      rows: [
        ["1-min average", windowText(w["60"])],
        ["5-min average", windowText(w300)],
        ["5-min peak", w300 && num(w300.peak_abs) !== null ? fmtFixed(w300.peak_abs, 2) + "°" : DASH],
        ["Margin to ±1.00°", margin],
      ],
    });
  }
  let badge;
  let note = null;
  if (polling && polling.commissioned === false) {
    badge = { text: "Not set up", tone: "" };
    note = str(polling.detail) || "Radar angle reads are not commissioned.";
  } else if (maxAbs === null) {
    badge = { text: "No readings yet", tone: "" };
    note = "Angles are read while the engine runs.";
  } else {
    const text = maxAbs >= RADAR_LIMIT ? "Outside ±1°" : maxAbs >= RADAR_APPROACH ? "Approaching ±1°" : "Within ±1°";
    const tone = live ? (maxAbs >= RADAR_LIMIT ? "red" : maxAbs >= RADAR_APPROACH ? "amber" : "green") : "";
    badge = { text, tone };
    if (!live) note = "Not live · last read " + (isFinite(newestAt) ? fmtTime(newestAt, new Date(nowMs)) : "earlier");
  }
  const problem = polling && str(polling.retained_storage_error) ? "Last angles are not being saved: " + clip(polling.retained_storage_error, 160) : null;
  return { badge, note, axes, problem };
}

// ---------------------------------------------------------------------------
// System: metric catalog

const QUALITY_WORDS = Object.freeze({
  verified: "verified",
  observed_alfa_scale: "matches scan tool",
  candidate: "unconfirmed",
});

/** Plain quality word for the catalog. */
export function qualityWord(quality) {
  const q = str(quality);
  if (!q) return "quality not stated";
  return QUALITY_WORDS[q] || humanize(q);
}

const MODULE_NAMES = Object.freeze({
  pcm: "engine computer",
  rf_hub: "RF hub",
  cluster: "cluster",
  ics: "instrument cluster",
  radar_acc: "radar",
  bcm: "body computer",
});

/** `ccan.broadcast.0x2ed` → `broadcast 0x2ED`; `pcm.did.06da` → `engine computer read 06DA`. */
export function sourceLabel(name) {
  const s = str(name);
  if (!s) return "source not stated";
  const parts = s.split(".");
  if (parts[1] === "broadcast" && parts[2]) return "broadcast 0x" + parts[2].replace(/^0x/i, "").toUpperCase();
  if (parts[1] === "did" && parts[2]) return (MODULE_NAMES[parts[0]] || humanize(parts[0])) + " read " + parts[2].toUpperCase();
  if (parts[0] === "derived") return "calculated";
  return s;
}

/** Catalog label: warnings.js subject, with clearer names for the catalog-only metrics. */
const CATALOG_LABELS = Object.freeze({
  "vehicle.ignition_on": "Ignition",
  "battery.voltage": "Battery voltage",
  "engine.rpm": "Engine speed",
  "engine.vvt_oil_temperature": "Oil temperature (VVT)",
  "generator.field_duty": "Alternator duty",
  "engine.target_crankshaft_torque": "Torque request",
  "transmission.output_speed": "Output shaft speed",
  "transmission.turbine_speed": "Turbine speed",
  "radar.alignment.azimuth": "Radar horizontal angle",
  "radar.alignment.elevation": "Radar vertical angle",
  "engine.oil_life_remaining": "Oil life",
});

export function catalogLabel(name) {
  if (CATALOG_LABELS[name]) return CATALOG_LABELS[name];
  const m = /^diagnostics\.([a-z_]+)\.did\.([0-9a-f]+)\.raw$/i.exec(String(name || ""));
  if (m) return capital(MODULE_NAMES[m[1]] || humanize(m[1])) + " value " + m[2].toUpperCase();
  return metricLabel(name);
}

/** True when a definition belongs under "Diagnostic-only": raw diagnostics and unconfirmed sources. */
export function isDiagnosticOnly(def) {
  const d = obj(def);
  if (!d) return false;
  if (String(d.name || "").indexOf("diagnostics.") === 0) return true;
  const sources = arr(d.sources);
  if (!sources.length) return false;
  for (let i = 0; i < sources.length; i += 1) {
    if (!obj(sources[i]) || sources[i].quality !== "candidate") return false;
  }
  return true;
}

function busName(bus) {
  const b = str(bus);
  return b ? ROLE_NAMES[b] || b.toUpperCase() : null;
}

/**
 * Static catalog rows (definitions only; values are bound separately per metric).
 * @param {Array<object>} defs `store.catalog`
 * @param {Set<string>} retainSet store.RETAIN_LAST_READING
 * @returns {{main:Array<{name:string,label:string,sources:string[],policy:string}>, diagnostic:Array}}
 */
export function catalogModel(defs, retainSet) {
  const main = [];
  const diagnostic = [];
  const list = arr(defs);
  for (let i = 0; i < list.length; i += 1) {
    const d = obj(list[i]);
    if (!d || !str(d.name)) continue;
    const sources = arr(d.sources)
      .filter(obj)
      .map((s) => [sourceLabel(s.name), busName(s.bus), qualityWord(s.quality)].filter(Boolean).join(" · "));
    const stale = num(d.stale_after_seconds);
    const policy = [
      stale === null ? "no freshness limit" : "current for " + fmtFixed(stale, stale % 1 ? 1 : 0) + " s",
      retainSet && retainSet.has(d.name) ? "keeps last reading" : "live only",
    ].join(" · ");
    const row = { name: d.name, label: catalogLabel(d.name), sources: sources.length ? sources : ["source not stated"], policy };
    (isDiagnosticOnly(d) ? diagnostic : main).push(row);
  }
  return { main, diagnostic };
}

/**
 * Catalog value text for one metric: the current value with its unit; else the dated last reading
 * (`12.60 V · 12:00 pm`, shown dimmed); else a dash. `vehicle.ignition_on` is positive-presence
 * only: `on` or a dash.
 * @param {string} name
 * @param {object|null} rec the metric record (`store.metricSignal(name).value`)
 * @param {{kind:string, value:*, unit:string|null, retained:object|null}|null} state derive.obs(name)
 * @param {string|null} defUnit catalog unit
 * @param {number} nowMs
 * @returns {{text:string, held:boolean}}
 */
export function catalogValue(name, rec, state, defUnit, nowMs) {
  const r = obj(rec);
  const fresh = r && r.available === true && r.stale !== true && r.value !== null && r.value !== undefined;
  if (fresh) return { text: valueText(name, r.value, r.unit || defUnit), held: false };
  const s = obj(state);
  if (s && s.kind === "live") return { text: valueText(name, s.value, s.unit || defUnit), held: false };
  if (s && s.kind === "held" && obj(s.retained)) {
    const at = when(s.retained.observed_at, nowMs);
    return { text: valueText(name, s.retained.value, s.retained.unit || defUnit) + (at ? " · " + at : ""), held: true };
  }
  return { text: DASH, held: false };
}

/** Plain words for the broker's per-metric reasons; unknown reasons fall back to the broker detail. */
const WHY_WORDS = Object.freeze({
  engine_not_running: "engine not running",
  bus_asleep: "van asleep, no bus traffic",
  wrong_bus: "adapter is not on this value's bus",
  stale: "no recent reading",
  observation_expired: "no recent reading",
  not_sampled: "not read yet",
  mapping_pending: "not decoded yet",
  source_unavailable: "reader not available",
  sensor_unavailable: "sensor not reporting",
  can_busy: "another tool was using the bus",
  acquisition_timeout: "no answer in time",
  response_timeout: "no answer in time",
  malformed_response: "unreadable answer",
  response_rejected: "module refused the read",
  implausible_transition: "reading rejected as implausible",
  invalid_observation: "reading rejected as implausible",
  interface_down: "bus link down",
  rate_limited: "asked too recently",
});

function whyWords(reason, detail) {
  const r = str(reason);
  if (r && WHY_WORDS[r]) return WHY_WORDS[r];
  const d = str(detail);
  if (d) return clip(d, 140);
  return r ? humanize(r) : null;
}

/**
 * Why a catalog metric shows no current value (the former static app's tiles' status line: "engine not
 * running", the battery's "last attempt: …", the charging card's State/Detail). Empty when the
 * value is current, or when there is no record to explain.
 * @param {object|null} rec the metric record (`store.metricSignal(name).value`)
 * @param {{kind:string}|null} state derive.obs(name)
 * @returns {string}
 */
export function catalogWhy(rec, state) {
  const r = obj(rec);
  if (!r) return "";
  const s = obj(state);
  const fresh = r.available === true && r.stale !== true && r.value !== null && r.value !== undefined;
  if (fresh || (s && s.kind === "live")) return "";
  const err = obj(r.last_acquisition_error);
  if (err && (str(err.reason) || str(err.detail))) {
    const words = whyWords(err.reason, err.detail);
    return words ? "Last read failed: " + words : "";
  }
  if (r.available === false) {
    const words = whyWords(r.reason, r.detail);
    return words ? "No value: " + words : "";
  }
  return "";
}

const CONNECTION_WORDS = Object.freeze({
  live: "Live",
  connecting: "Connecting",
  resyncing: "Refreshing",
  unavailable: "Broker unavailable",
});

/**
 * The broker link as one line for the Device & app card, with the error the former static app's masthead
 * printed (`Broker unavailable: …`) when the link is down.
 * @param {{state:string, reason?:string|null, detail?:string|null}|null} conn `store.connection`
 * @returns {string}
 */
export function connectionText(conn) {
  const c = obj(conn) || {};
  const head = CONNECTION_WORDS[c.state] || capital(humanize(c.state || "unknown"));
  if (c.state !== "unavailable") return head;
  const why = str(c.detail) ? clip(c.detail, 160) : str(c.reason) ? humanize(c.reason) : null;
  return why && why.toLowerCase() !== head.toLowerCase() ? head + " · " + why : head;
}

function valueText(name, value, unit) {
  if (name === "vehicle.ignition_on") return value === true ? "on" : DASH;
  if (typeof value === "boolean") return value ? "on" : "off";
  const text = fmtValue(name, value);
  if (text === DASH) return DASH;
  const u = typeof value === "number" ? fmtUnit(name, unit) : "";
  if (!u) return text;
  return u === "°" ? text + u : text + " " + u;
}

// ---------------------------------------------------------------------------
// System: customise and device

/** Views offered on the Customise card, with their card-noun. */
export const CUSTOMISE_VIEWS = Object.freeze([
  Object.freeze({ view: "drive", label: "Drive", noun: "tiles" }),
  Object.freeze({ view: "parked", label: "Parked", noun: "cards" }),
  Object.freeze({ view: "health", label: "Health", noun: "cards" }),
  Object.freeze({ view: "history", label: "History", noun: "cards" }),
  Object.freeze({ view: "system", label: "System", noun: "cards" }),
]);

/**
 * One-line state of a view's customisation on this device: `Default layout`,
 * `2 tiles hidden`, `Own order`, `1 card hidden · own order`.
 * @param {string} view
 * @param {Array<{id:string}>} cards default order
 * @param {object} settings settings.js object
 * @param {string} [noun] `cards` or `tiles`
 */
export function customiseStatus(view, cards, settings, noun) {
  const ids = arr(cards).map((c) => c && c.id).filter((id) => typeof id === "string");
  const s = obj(settings) || {};
  const hiddenList = arr(obj(s.hidden) && s.hidden[view]).filter((id) => ids.indexOf(id) >= 0);
  const order = arr(obj(s.order) && s.order[view]).filter((id) => ids.indexOf(id) >= 0);
  const effective = order.concat(ids.filter((id) => order.indexOf(id) < 0));
  let reordered = false;
  for (let i = 0; i < ids.length; i += 1) if (effective[i] !== ids[i]) reordered = true;
  const one = noun === "tiles" ? "tile" : "card";
  const many = noun === "tiles" ? "tiles" : "cards";
  const parts = [];
  if (hiddenList.length) parts.push(plural(hiddenList.length, one, many) + " hidden");
  // Drive tiles keep fixed places (Drive.jsx ignores a saved order), so only hiding counts there.
  if (reordered && noun !== "tiles") parts.push(parts.length ? "own order" : "Own order");
  return parts.length ? parts.join(" · ") : "Default layout";
}

/**
 * Tablet facts for the Device & app card, from values read once on mount.
 * @param {{userAgent?:string, dpr?:number, width?:number, height?:number, displayMode?:string}} env
 * @returns {Array<[string,string]>}
 */
export function deviceRows(env) {
  const e = obj(env) || {};
  const rows = [];
  const w = num(e.width);
  const h = num(e.height);
  rows.push(["Screen", w !== null && h !== null ? fmtInt(w) + " × " + fmtInt(h) + " px" : DASH]);
  rows.push(["Pixel ratio", num(e.dpr) !== null ? fmtFixed(e.dpr, e.dpr % 1 ? 2 : 0) : DASH]);
  rows.push(["Opened as", e.displayMode === "browser" || !str(e.displayMode) ? "browser tab" : "home-screen app (" + e.displayMode + ")"]);
  rows.push(["Browser", str(e.userAgent) || DASH]);
  return rows;
}

/**
 * Listener facts for the Device & app card from `store.web` and `/build.json`.
 * @param {object|null} web `store.web`
 * @param {{build?:string, builtAt?:string}|null} buildInfo parsed `/build.json`, or null
 * @param {number} nowMs
 * @returns {Array<[string,string]>}
 */
export function appRows(web, buildInfo, nowMs) {
  const w = obj(web) || {};
  const b = obj(buildInfo) || {};
  const rows = [];
  const build = str(b.build) || str(w.build);
  const built = when(b.builtAt, nowMs);
  rows.push(["App build", build ? build + (built ? " · built " + built : "") : "not reported"]);
  rows.push(["Listener", str(w.bind) || "not reported"]);
  const flag = (v, on, off) => (v === true ? on : v === false ? off : "not reported");
  rows.push(["Voltage read button", flag(w.active_acquisition_enabled, "allowed here", "off on this listener")]);
  rows.push(["Code scan", flag(w.dtc_jobs_enabled, "available here", "not on this listener")]);
  rows.push(["Codex advisor", flag(w.warning_chat_enabled, "available here", "not on this listener")]);
  return rows;
}
