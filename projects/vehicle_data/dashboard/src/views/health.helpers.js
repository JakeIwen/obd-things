/**
 * Pure view models for the Health view (design 3.3; critique A11, M11, minor 16). Node-testable:
 * no Preact, no store, no DOM. Health.jsx wraps each builder in a module-level `computed`, so the
 * work here runs only when its summary slice changes (60 s cadence at most), never per second.
 *
 * - `warningsView()`  early-warning rows from the lazy `warnings.js` cards.
 * - `systemNotesView()` adapter, bus and data-quality notes plus recently recovered sample filters.
 * - `codesView()`     the saved diagnostic-code list from `summary.dtcs` (group counts, truncation,
 *                     the one-month split of confirmed history, module coverage).
 */

import { fmtTime, fmtInt, parseDate } from "../format.js";
import { OPEN_STATES, metricLabel } from "../warningContext.js";

/** Health cards in default order; ids are what the customiser stores. */
export const HEALTH_CARDS = Object.freeze([
  Object.freeze({ id: "warnings", label: "Early warning" }),
  Object.freeze({ id: "notes", label: "System notes" }),
  Object.freeze({ id: "codes", label: "Diagnostic codes" }),
  Object.freeze({ id: "service", label: "Service" }),
]);

export const HEALTH_CARD_IDS = Object.freeze(HEALTH_CARDS.map((c) => c.id));

/** Docs pages linked from the view (the caveats link appears once, on the codes card). */
export const DOCS = Object.freeze({ warnings: "/docs/warnings.html", caveats: "/docs/caveats.html" });

/** Plain group titles in display order. */
export const DTC_GROUPS = Object.freeze([
  ["current", "Current"],
  ["pending", "Pending"],
  ["confirmed_history", "Confirmed history"],
  ["incomplete_only", "Test not completed"],
  ["other", "Other status combinations"],
]);

/** Most recovered sample-filter incidents shown (as the former static app). */
export const RECOVERED_LIMIT = 3;

const BUS_NAMES = Object.freeze({ "c-can": "C-CAN", "b-can": "B-CAN", "can-ch": "CAN-CH" });

function obj(v) {
  return v && typeof v === "object" && !Array.isArray(v) ? v : null;
}

function arr(v) {
  return Array.isArray(v) ? v : [];
}

function num(v) {
  return typeof v === "number" && isFinite(v) ? v : null;
}

function str(v) {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function when(iso, nowMs) {
  return parseDate(iso) ? fmtTime(iso, new Date(nowMs)) : null;
}

function plural(n, one, many) {
  return fmtInt(n) + " " + (n === 1 ? one : many);
}

function busName(bus) {
  const b = str(bus);
  return b ? BUS_NAMES[b] || b.toUpperCase() : null;
}

function humanize(value) {
  const s = String(value || "").replace(/_/g, " ").trim();
  return s;
}

// ---------------------------------------------------------------------------
// early warning

/** Tier colour of a warning dot: critical red, warning amber, notice neutral. */
export function tierTone(card) {
  if (!card) return "";
  if (card.tier === "critical") return "red";
  if (card.tier === "warning") return "amber";
  return "";
}

/** Chat context for a card: its saved event when it has one, else its rule; null when neither. */
export function chatContext(card) {
  if (!card) return null;
  const title = card.title || null;
  if (typeof card.episodeId === "number") return { eventId: card.episodeId, title };
  if (card.category === "data_quality") {
    const id = typeof card.id === "string" && card.id.indexOf("data-quality:") === 0 ? card.id.slice(13) : "";
    return id ? { event: { kind: "quality", id }, title } : null;
  }
  if (typeof card.id === "string" && card.id.indexOf("rule:") === 0 && str(card.rule)) {
    return { assessment: { rule: card.rule, title }, title };
  }
  return null;
}

function stripUnconfirmed(line) {
  let s = typeof line === "string" ? line : "";
  if (s.indexOf("Unconfirmed · ") === 0) s = s.slice("Unconfirmed · ".length);
  if (s.charAt(s.length - 1) === ".") s = s.slice(0, -1);
  return s;
}

/**
 * True when the card's saved event was marked reviewed in the event dialog (the former static app's card
 * printed "acknowledged" in its evidence line). warnings.js sets `ackable` false for those.
 */
export function isReviewed(card) {
  return Boolean(card) && typeof card.episodeId === "number" && card.ackable === false;
}

/** Row for an open (watch/warning) card: the template sentence, when it started, and actions. */
export function openRow(card) {
  const meta = [card.sinceLabel ? "since " + card.sinceLabel : null, isReviewed(card) ? "reviewed" : null]
    .filter(Boolean)
    .join(" · ");
  return {
    id: card.id,
    line: card.line,
    meta: meta || null,
    tone: tierTone(card),
    episodeId: typeof card.episodeId === "number" ? card.episodeId : null,
    chat: chatContext(card),
    title: card.title,
  };
}

/** Row for an unconfirmed or recovering card: its title, what is known now, and when it opened. */
export function unconfirmedRow(card) {
  const meta = [
    stripUnconfirmed(card.line) || null,
    card.sinceLabel ? "opened " + card.sinceLabel : null,
    isReviewed(card) ? "reviewed" : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return {
    id: card.id,
    line: card.title || stripUnconfirmed(card.line),
    meta: meta || null,
    tone: "",
    episodeId: typeof card.episodeId === "number" ? card.episodeId : null,
    chat: chatContext(card),
    title: card.title,
  };
}

/**
 * Delivery line: shown only when phone alerts are switched off on the Pi (the former static app's note
 * "External warning delivery is disabled"), or when something is pending, failed or errored.
 * @param {object|null} delivery warnings.js delivery state
 * @param {boolean} [off] `notification_delivery.enabled === false` in the health slice
 */
export function deliveryLine(delivery, off) {
  const d = obj(delivery) || {};
  if (!off && !d.show) return null;
  const parts = [];
  if (off) parts.push("off on the Pi");
  if (d.pending > 0) parts.push(plural(d.pending, "alert", "alerts") + " waiting to send");
  if (d.failed > 0) parts.push(plural(d.failed, "alert", "alerts") + " failed to send");
  if (str(d.lastError)) parts.push("last error: " + d.lastError);
  return parts.length ? "Phone alerts: " + parts.join(" · ") : null;
}

/**
 * What the Pi's checks can see when nothing is open (the former static app's TRAINING / DATA
 * UNAVAILABLE / NO ASSESSMENTS states, in plain words). Null when every check is evaluating or
 * the slice carries no assessment list.
 * @param {object|null} health `summary.health` slice
 * @returns {string|null}
 */
export function checksNote(health) {
  const h = obj(health);
  if (!h || !Array.isArray(h.assessments)) return null;
  const list = h.assessments.filter(obj);
  if (!list.length) return "The Pi has not reported any checks yet.";
  let learning = 0;
  let waiting = 0;
  for (let i = 0; i < list.length; i += 1) {
    if (list[i].state === "insufficient_history") learning += 1;
    else if (list[i].state === "unavailable") waiting += 1;
  }
  const total = fmtInt(list.length);
  const parts = [];
  if (learning) parts.push(fmtInt(learning) + " of " + total + " checks are still learning normal readings");
  if (waiting) parts.push(fmtInt(waiting) + " of " + total + " checks are waiting for fresh readings");
  return parts.length ? parts.join(" · ") + "." : null;
}

/**
 * Early-warning card model.
 * @param {object} model warnings.buildWarningCards output
 * @param {object|null} health `summary.health` slice (null until the first summary arrives)
 * @returns {{status: 'loading'|'unavailable'|'ok', emptyText: string|null, checks: string|null,
 *   badge: {text: string, tone: string}|null, open: Array, unconfirmed: Array,
 *   delivery: string|null, rulesError: string|null}}
 */
export function warningsView(model, health) {
  const base = { status: "ok", emptyText: null, checks: null, badge: null, open: [], unconfirmed: [], delivery: null, rulesError: null };
  if (health === null || health === undefined) {
    return { ...base, status: "loading", emptyText: "Waiting for warning status…" };
  }
  const m = obj(model) || {};
  if (m.available === false) {
    return {
      ...base,
      status: "unavailable",
      emptyText:
        (obj(health) && str(health.detail)) ||
        "Warning status could not be loaded. This does not mean warnings are cleared or alerts are off.",
    };
  }
  const cards = arr(m.warnings);
  const open = [];
  const unconfirmed = [];
  let critical = false;
  for (let i = 0; i < cards.length; i += 1) {
    const card = cards[i];
    if (OPEN_STATES.has(card.state)) {
      open.push(openRow(card));
      if (card.tier === "critical") critical = true;
    } else {
      unconfirmed.push(unconfirmedRow(card));
    }
  }
  const customRules = obj(obj(health) && health.custom_rules);
  const nd = obj(obj(health) && health.notification_delivery);
  return {
    ...base,
    emptyText: open.length ? null : "Nothing open right now.",
    checks: open.length ? null : checksNote(health),
    badge: open.length ? { text: open.length + " open", tone: critical ? "red" : "amber" } : null,
    open,
    unconfirmed,
    delivery: deliveryLine(m.delivery, Boolean(nd && nd.enabled === false)),
    rulesError: customRules && str(customRules.error) ? "Your saved warnings could not be loaded: " + customRules.error : null,
  };
}

// ---------------------------------------------------------------------------
// system notes

/** Row for a system note (adapters, buses, data checks). */
export function noteRow(card) {
  return {
    id: card.id,
    line: card.line,
    meta: null,
    tone: card.severity === "critical" ? "red" : "",
    episodeId: typeof card.episodeId === "number" ? card.episodeId : null,
    chat: chatContext(card),
    title: card.title,
  };
}

/** Row for a recovered sample-filter incident. */
export function recoveredRow(incident, index, nowMs) {
  const label = metricLabel(str(incident.metric));
  const at = when(incident.resolved_at, nowMs) || when(incident.last_seen_at, nowMs);
  const n = num(incident.rejection_count);
  const id = str(incident.incident_id);
  return {
    id: "recovered:" + (id || index),
    line: label + " sample filter cleared" + (at ? " " + at : "") + ".",
    meta: (n !== null && n > 0 ? plural(n, "implausible reading", "implausible readings") + " ignored · " : "") + "last good value kept",
    tone: "",
    episodeId: null,
    chat: id ? { event: { kind: "quality", id }, title: label + " sample filter recovered" } : null,
    title: label + " sample filter recovered",
  };
}

/**
 * System-notes card model.
 * @param {object} model warnings.buildWarningCards output
 * @param {object|null} health `summary.health` slice
 * @param {number} nowMs wall clock for time labels
 */
export function systemNotesView(model, health, nowMs) {
  const m = obj(model) || {};
  const notes = arr(m.systemNotes).map(noteRow);
  const dq = obj(obj(health) && health.data_quality);
  const recovered = arr(dq && dq.recent)
    .filter((e) => obj(e) && e.status === "resolved")
    .slice(0, RECOVERED_LIMIT)
    .map((e, i) => recoveredRow(e, i, nowMs));
  let summary = null;
  if (notes.length) {
    summary = plural(notes.length, "note", "notes") + (recovered.length ? " · " + recovered.length + " recovered" : "");
  } else if (recovered.length) {
    summary = "Nothing active · " + recovered.length + " recovered";
  }
  return {
    loading: health === null || health === undefined,
    count: notes.length,
    notes,
    recovered,
    summary,
    emptyText: "Adapters, buses and data checks are quiet.",
  };
}

// ---------------------------------------------------------------------------
// diagnostic codes

/**
 * Cut-off for "older than one month" confirmed history: the same UTC day of the previous month
 * (clamped to its length), as the former static app's dtcHistoryCutoff.
 * @param {number} nowMs
 * @returns {number} epoch ms
 */
export function historyCutoff(nowMs) {
  const cutoff = new Date(nowMs);
  const day = cutoff.getUTCDate();
  cutoff.setUTCMonth(cutoff.getUTCMonth() - 1, 1);
  const lastDay = new Date(Date.UTC(cutoff.getUTCFullYear(), cutoff.getUTCMonth() + 1, 0)).getUTCDate();
  cutoff.setUTCDate(Math.min(day, lastDay));
  return cutoff.getTime();
}

/** Display code of a record (FCA form first). */
export function codeOf(entry) {
  const e = obj(entry) || {};
  return (
    str(e.fca_display) || str(e.display_code) || str(e.code) || str(e.dtc) || str(e.raw_dtc) || str(e.raw_code) || "—"
  );
}

/**
 * Plain note for a saved code whose status is not from the module's latest good read (the
 * former static app's row observation label). The normal case, `observed_in_latest_success`, says nothing.
 */
export const OBSERVATION_NOTES = Object.freeze({
  stale_after_unavailable_attempt: "module did not answer the latest read",
  retained_incompatible_status_mask: "kept from an older read",
});

/** True when the module asked for its warning light for this code (status bit or flag list). */
export function warningLightRequested(entry) {
  const e = obj(entry) || {};
  return e.warning_indicator_requested === true || arr(e.status_flags).indexOf("warning_indicator_requested") >= 0;
}

/** Row for one saved code: code, meaning, module, warning light, read note and when it was last seen. */
export function codeRow(entry, nowMs) {
  const e = obj(entry) || {};
  let meaning = str(e.label) || str(e.description);
  // The broker's placeholder for an unreviewed code names only the standard failure type.
  const subtype = meaning && e.description_reviewed === false ? /failure subtype:\s*(.+)$/i.exec(meaning) : null;
  if (subtype) meaning = "meaning not reviewed · " + subtype[1].trim();
  const module = str(e.module_name) || str(e.module_key) || str(e.module);
  const van = e.source === VAN_SOURCE;
  const seen = when(e.last_seen_at, nowMs);
  const note = !van && str(e.observation_state) ? OBSERVATION_NOTES[e.observation_state] || null : null;
  return {
    key: (str(e.module_key) || str(e.module) || "") + ":" + (str(e.raw_dtc) || codeOf(e)),
    code: codeOf(e),
    meaning: meaning || "meaning not reviewed",
    reviewed: Boolean(meaning) && e.description_reviewed !== false,
    meta:
      [
        module,
        warningLightRequested(e) ? "warning light requested" : null,
        note,
        van ? vanSourceLine(e.van_scan_at || e.last_seen_at, nowMs) : seen ? "last seen " + seen : null,
      ]
        .filter(Boolean)
        .join(" · ") || null,
  };
}

function uniqueKeys(rows) {
  const seen = new Map();
  for (let i = 0; i < rows.length; i += 1) {
    const base = rows[i].key;
    const n = seen.get(base) || 0;
    seen.set(base, n + 1);
    if (n > 0) rows[i].key = base + "#" + n;
  }
  return rows;
}

/** Plain status of one module's saved read. */
export function moduleStatus(module, nowMs) {
  const m = obj(module) || {};
  if (m.source === VAN_SOURCE) {
    const count = num(m.last_success_dtc_count);
    return {
      text: (count ? plural(count, "code", "codes") : "no codes") + " · " + vanSourceLine(m.van_scan_at || m.last_success_at, nowMs),
      tone: "",
      gap: false,
      rank: 3,
    };
  }
  if (m.availability === "never_scanned") return { text: "never read", tone: "", gap: true, rank: 2 };
  if (m.availability === "unavailable") {
    const reason = str(m.unavailable_reason);
    const good = when(m.last_success_at, nowMs);
    return {
      text: "no answer" + (reason ? " (" + humanize(reason) + ")" : "") + (good ? " · last good read " + good : " · never answered"),
      tone: "amber",
      gap: true,
      rank: 0,
    };
  }
  const count = num(m.last_success_dtc_count);
  if (m.result_state === "status_coverage_incomplete" || (count === 0 && m.absence_authoritative !== true)) {
    return { text: "incomplete answer; zero codes not proven", tone: "amber", gap: true, rank: 1 };
  }
  if (m.result_state === "no_dtcs" || count === 0) return { text: "no codes", tone: "", gap: false, rank: 3 };
  return { text: count === null ? "codes saved" : plural(count, "code", "codes"), tone: "", gap: false, rank: 3 };
}

function moduleList(dtcs) {
  const modules = dtcs.modules;
  if (Array.isArray(modules)) return modules.filter(obj);
  const o = obj(modules);
  return o ? Object.keys(o).map((k) => o[k]).filter(obj) : [];
}

/** Module coverage block: summary line and rows (gaps first, then the modules that answered). */
export function modulesView(dtcs, nowMs) {
  const modules = moduleList(dtcs);
  const coverage = obj(dtcs.coverage) || {};
  const rows = modules.map((m, i) => {
    const status = moduleStatus(m, nowMs);
    return {
      key: str(m.module_key) || "module:" + i,
      name: str(m.module_name) || str(m.module_key) || "Unknown module",
      meta: [busName(m.logical_bus), status.text].filter(Boolean).join(" · "),
      tone: status.tone,
      gap: status.gap,
      rank: status.rank,
      index: i,
    };
  });
  rows.sort((a, b) => a.rank - b.rank || a.index - b.index);
  const answered = num(coverage.available_modules) !== null
    ? coverage.available_modules
    : modules.filter((m) => m.availability === "available").length;
  const total = num(coverage.total_modules) !== null ? coverage.total_modules : modules.length;
  const gaps = rows.filter((r) => r.gap).length;
  return {
    summary: total > 0 ? "Modules · " + answered + " of " + total + " answered" : "Modules · no coverage yet",
    rows: rows.map((r) => ({ key: r.key, name: r.name, meta: r.meta, tone: r.tone })),
    gaps,
  };
}

function sumCounts(counts) {
  let total = 0;
  const keys = Object.keys(counts);
  for (let i = 0; i < keys.length; i += 1) total += num(counts[keys[i]]) || 0;
  return total;
}

// ---------------------------------------------------------------------------
// the van's own health check (docs/caveats.md "The van's own health check")

/** `source` marker on records and module rows taken from the van's own health check. */
export const VAN_SOURCE = "van";

/** Status bits the van's check asks for (test failed, pending, confirmed): mask 0D. */
export const VAN_STATUS_MASK = 0x0d;

/** Plain source line: `from the van's own health check · 3:12 pm`. */
export function vanSourceLine(iso, nowMs) {
  const at = when(iso, nowMs);
  return "from the van\u2019s own health check" + (at ? " · " + at : "");
}

function statusByte(record) {
  const s = str(record.status);
  const n = s ? parseInt(s, 16) : NaN;
  return isFinite(n) ? n : null;
}

/**
 * Per module, keep whichever read is newer: the Pi's saved scan or the van's own health check
 * (`dtcs.in_vehicle_scan`, harvested passively from drive recordings). When the van's read is
 * newer, the Pi's records for that module inside the van's status mask are replaced by the van's
 * records; Pi records outside the mask (for example "test not completed" only) stay, because the
 * van's check cannot see them. Group totals are adjusted by what was replaced among the returned
 * records. Returns the input unchanged when there is no usable in-vehicle result.
 * @param {object} d `summary.dtcs`
 * @returns {object} a dtcs-shaped object with `van: {modules, lastAt}` added
 */
export function mergeVanScan(d) {
  const van = obj(d.in_vehicle_scan);
  if (!van || van.available !== true || !Array.isArray(van.modules) || !van.modules.length) return d;
  const piRows = moduleList(d);
  const piByKey = new Map();
  for (let i = 0; i < piRows.length; i += 1) piByKey.set(piRows[i].module_key, piRows[i]);
  const use = new Map();
  let lastAt = null;
  let scanLast = null;
  for (let i = 0; i < van.modules.length; i += 1) {
    const vm = obj(van.modules[i]);
    const key = vm ? str(vm.module_key) : null;
    const at = vm ? parseDate(vm.observed_at) : null;
    if (!key || !at) continue;
    const pi = piByKey.get(key);
    const piAt = pi ? parseDate(pi.last_attempt_at) : null;
    if (piAt && piAt.getTime() >= at.getTime()) continue;
    use.set(key, vm);
    if (!lastAt || at.getTime() > parseDate(lastAt).getTime()) lastAt = vm.observed_at;
    const scanAt = parseDate(vm.scan_started_at) ? vm.scan_started_at : vm.observed_at;
    if (!scanLast || parseDate(scanAt).getTime() > parseDate(scanLast).getTime()) scanLast = scanAt;
  }
  if (!use.size) return d;

  const groups = obj(d.groups) || {};
  const counts = obj(d.group_counts) || {};
  const returnedCounts = obj(d.group_returned_counts) || {};
  const outGroups = {};
  const outCounts = {};
  const outReturned = {};
  for (let g = 0; g < DTC_GROUPS.length; g += 1) {
    const name = DTC_GROUPS[g][0];
    const entries = arr(groups[name]).filter(obj);
    const kept = entries.filter((e) => {
      if (!use.has(e.module_key)) return true;
      const status = statusByte(e);
      return status !== null && (status & VAN_STATUS_MASK) === 0;
    });
    const removed = entries.length - kept.length;
    outGroups[name] = kept;
    outCounts[name] = Math.max(0, (num(counts[name]) !== null ? counts[name] : entries.length) - removed);
    outReturned[name] = Math.max(0, (num(returnedCounts[name]) !== null ? returnedCounts[name] : entries.length) - removed);
  }
  use.forEach((vm, key) => {
    const records = arr(vm.dtcs).filter(obj);
    for (let i = 0; i < records.length; i += 1) {
      const r = records[i];
      const group = DTC_GROUPS.some((x) => x[0] === r.display_group) ? r.display_group : "other";
      outGroups[group].push({
        ...r,
        module_key: key,
        module_name: str(vm.module_name) || (piByKey.get(key) || {}).module_name || key,
        logical_bus: vm.logical_bus,
        last_seen_at: vm.observed_at,
        van_scan_at: str(vm.scan_started_at) || vm.observed_at,
        source: VAN_SOURCE,
      });
      outCounts[group] += 1;
      outReturned[group] += 1;
    }
  });
  const modules = piRows.map((m) => {
    const vm = use.get(m.module_key);
    if (!vm) return m;
    const count = num(vm.dtc_count) !== null ? vm.dtc_count : arr(vm.dtcs).length;
    return {
      ...m,
      availability: "available",
      result_state: count > 0 ? "dtcs_present" : "no_dtcs",
      last_attempt_at: vm.observed_at,
      last_success_at: vm.observed_at,
      last_success_dtc_count: count,
      absence_authoritative: true,
      van_scan_at: str(vm.scan_started_at) || vm.observed_at,
      source: VAN_SOURCE,
    };
  });
  use.forEach((vm, key) => {
    if (piByKey.has(key)) return;
    const count = num(vm.dtc_count) !== null ? vm.dtc_count : arr(vm.dtcs).length;
    modules.push({
      module_key: key,
      module_name: str(vm.module_name) || key,
      logical_bus: vm.logical_bus,
      availability: "available",
      result_state: count > 0 ? "dtcs_present" : "no_dtcs",
      last_attempt_at: vm.observed_at,
      last_success_at: vm.observed_at,
      last_success_dtc_count: count,
      absence_authoritative: true,
      van_scan_at: str(vm.scan_started_at) || vm.observed_at,
      source: VAN_SOURCE,
    });
  });
  const coverage = { ...(obj(d.coverage) || {}) };
  coverage.available_modules = modules.filter((m) => m.availability === "available").length;
  coverage.total_modules = modules.length;
  if (!str(coverage.last_attempt_at) || parseDate(coverage.last_attempt_at).getTime() < parseDate(lastAt).getTime()) {
    coverage.last_attempt_at = lastAt;
  }
  return {
    ...d,
    groups: outGroups,
    group_counts: outCounts,
    group_returned_counts: outReturned,
    modules,
    coverage,
    van: { modules: use.size, lastAt, scanAt: scanLast },
  };
}

/**
 * Diagnostic-codes card model. Totals come from `group_counts` (the broker truncates each group at
 * `per_group_limit`, 25 today); `showing n of m` appears when a group was cut.
 * @param {object|null} dtcs `summary.dtcs` slice
 * @param {number} nowMs wall clock for labels and the one-month cut-off
 * @param {boolean} scanEnabled this listener can queue the guarded scan
 */
export function codesView(dtcs, nowMs, scanEnabled) {
  const base = { status: "ok", badge: null, lastRead: null, source: null, emptyText: null, current: null, groups: [], modules: null };
  if (dtcs === null || dtcs === undefined) return { ...base, status: "loading", emptyText: "Waiting for the saved code list…" };
  let d = obj(dtcs) || {};
  if (d.available === false) {
    const van = obj(d.in_vehicle_scan);
    if (!van || van.available !== true) {
      return { ...base, status: "unavailable", emptyText: str(d.detail) || "The saved code list is unavailable." };
    }
    // The Pi's saved list is unavailable, but the van's own check can still be shown.
    d = { in_vehicle_scan: van, groups: {}, group_counts: {}, group_returned_counts: {}, coverage: {}, modules: [] };
  }
  d = mergeVanScan(d);
  const groups = obj(d.groups) || {};
  const counts = obj(d.group_counts) || {};
  const returnedCounts = obj(d.group_returned_counts) || {};
  const coverage = obj(d.coverage) || {};
  const newest = str(coverage.last_attempt_at) || str(coverage.last_success_at);
  const cutoff = historyCutoff(nowMs);

  const groupModels = [];
  for (let g = 0; g < DTC_GROUPS.length; g += 1) {
    const key = DTC_GROUPS[g][0];
    const title = DTC_GROUPS[g][1];
    const entries = arr(groups[key]).filter(obj);
    const total = num(counts[key]) !== null ? counts[key] : entries.length;
    const returned = Math.min(entries.length, num(returnedCounts[key]) !== null ? returnedCounts[key] : entries.length);
    if (key === "other" && total <= 0) continue;
    const recent = [];
    const older = [];
    for (let i = 0; i < returned; i += 1) {
      const row = codeRow(entries[i], nowMs);
      const seen = parseDate(entries[i].last_seen_at);
      if (key === "confirmed_history" && seen && seen.getTime() < cutoff) older.push(row);
      else recent.push(row);
    }
    uniqueKeys(recent.concat(older));
    const cut = returned < total;
    groupModels.push({
      key,
      title,
      total,
      returned,
      summary: title + " · " + fmtInt(total) + (cut ? " · showing " + returned + " of " + fmtInt(total) : ""),
      rows: recent,
      older,
      olderSummary: older.length ? "Older than one month · " + older.length : null,
      note: key === "incomplete_only" ? "Not a fault: the module had not finished that self-test when it was read." : null,
    });
  }
  const records = Object.keys(counts).length ? sumCounts(counts) : groupModels.reduce((n, g) => n + g.total, 0);
  const modules = modulesView(d, nowMs);
  const moduleRows = moduleList(d);
  const authoritativeZero =
    moduleRows.length > 0 &&
    records === 0 &&
    modules.gaps === 0 &&
    moduleRows.every((m) => m.result_state === "no_dtcs" && m.absence_authoritative === true);

  let emptyText = null;
  if (records === 0) {
    if (!newest) {
      emptyText =
        "No saved code read yet. " +
        (scanEnabled ? "The parked scan below can read the modules." : "This screen cannot start a scan.");
    } else if (authoritativeZero) {
      emptyText = "No codes in the last read of every module.";
    } else {
      emptyText = "No saved codes, but some modules did not answer fully, so a clean result is not proven.";
    }
  }
  const current = groupModels.find((g) => g.key === "current") || null;
  const currentCount = current ? current.total : 0;
  let badge = null;
  if (currentCount > 0) badge = { text: currentCount + " current", tone: "amber" };
  else if (newest) badge = { text: "None current", tone: "" };
  const van = obj(d.van);
  return {
    ...base,
    badge,
    lastRead: newest ? "Last read " + fmtTime(newest, new Date(nowMs)) : null,
    source: van
      ? plural(van.modules, "module", "modules") + " " + vanSourceLine(van.scanAt || van.lastAt, nowMs)
      : null,
    emptyText,
    current: records > 0 ? current : null,
    groups: records > 0 ? groupModels.filter((g) => g.key !== "current") : [],
    modules,
  };
}
