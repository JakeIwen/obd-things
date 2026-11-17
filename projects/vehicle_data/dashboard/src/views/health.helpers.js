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
import {
  DTC_GROUPS, VAN_SOURCE, obj, arr, num, str, moduleList, mergeVanScan,
  filteredSourceLine, filteredReadText, driveReadLines, filteredTime, newestSuccess, unlistedReports,
} from "./dtcSources.helpers.js";
import { excludeIgnored, ignoreState, recordIdentity } from "../dtcIgnore.helpers.js";
import { moduleOptions, activeModules, filterByModules, unreturnedNote, ignoredGroup } from "./dtcCodes.helpers.js";
export { DTC_GROUPS, VAN_SOURCE, VAN_STATUS_MASK, mergeVanScan } from "./dtcSources.helpers.js";

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

/** Most recovered sample-filter incidents shown (as the former static app). */
export const RECOVERED_LIMIT = 3;

const BUS_NAMES = Object.freeze({ "c-can": "C-CAN", "b-can": "B-CAN", "can-ch": "CAN-CH" });

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

/** Plain labels of a record's own status flags, for the row's detail panel. */
function statusLabels(e) {
  return [
    e.current === true ? "Current" : null,
    e.pending === true ? "Pending" : null,
    e.confirmed === true ? "Confirmed" : null,
    e.incomplete_only === true ? "Test not completed" : null,
    warningLightRequested(e) ? "Warning light requested" : null,
    str(e.status) ? "status byte " + e.status : null,
  ].filter(Boolean);
}

/** Label/value pairs the open row shows: module, bus, first and last seen (Mountain), status flags. */
function rowDetails(e, module) {
  const first = str(e.first_seen_at) && parseDate(e.first_seen_at) ? filteredTime(e.first_seen_at) : null;
  const last = str(e.last_seen_at) && parseDate(e.last_seen_at) ? filteredTime(e.last_seen_at) : null;
  const labels = statusLabels(e);
  return [
    ["Module", [module, busName(e.logical_bus)].filter(Boolean).join(" · ") || null],
    ["First seen", first],
    ["Last seen", last],
    ["Status", labels.length ? labels.join(" · ") : null],
  ].filter((pair) => pair[1] !== null);
}

/**
 * Row for one saved code: code, meaning, module, warning light, read note and when it was last seen.
 * `id` is the code's identity (module and code; null when the record has no FCA code, so it cannot
 * be ignored), `details` fill the open row's panel and `extra` is an optional secondary line.
 */
export function codeRow(entry, nowMs) {
  const e = obj(entry) || {};
  let meaning = str(e.label) || str(e.description);
  // The broker's placeholder for an unreviewed code names only the standard failure type.
  const subtype = meaning && e.description_reviewed === false ? /failure subtype:\s*(.+)$/i.exec(meaning) : null;
  if (subtype) meaning = "meaning not reviewed · " + subtype[1].trim();
  const module = str(e.module_name) || str(e.module_key) || str(e.module);
  const filtered = e.source === VAN_SOURCE || e.source === "drive";
  const seen = when(e.last_seen_at, nowMs);
  const note = e.observation_state === "not_reported_by_source"
    ? "not reported by the " + (e.not_reported_source === VAN_SOURCE ? "van" : "drive") +
      " read · " + filteredSourceLine(e.not_reported_source, e.not_reported_at)
    : str(e.observation_state) ? OBSERVATION_NOTES[e.observation_state] || null : null;
  return {
    key: (str(e.module_key) || str(e.module) || "") + ":" + (str(e.raw_dtc) || codeOf(e)),
    code: codeOf(e),
    meaning: meaning || "meaning not reviewed",
    reviewed: Boolean(meaning) && e.description_reviewed !== false,
    moduleKey: str(e.module_key),
    fca: str(e.fca_display),
    id: recordIdentity(e),
    details: rowDetails(e, module),
    extra: null,
    meta:
      [
        module,
        warningLightRequested(e) ? "warning light requested" : null,
        note,
        filtered ? filteredSourceLine(e.source, e.last_seen_at) : seen ? "last seen " + seen : null,
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

/** Module coverage block: summary line and rows (gaps first, then the modules that answered). */
export function modulesView(dtcs, nowMs) {
  const modules = moduleList(dtcs);
  const coverage = obj(dtcs.coverage) || {};
  const rows = modules.map((m, i) => {
    const status = moduleStatus(m, nowMs);
    const vanRead = obj(m.van_read);
    const vanText = vanRead ? filteredReadText(vanRead) : null;
    const filtered = vanRead || m.drive_read || m.drive_truncated || m.drive_failure;
    const failedAt = filtered && m.availability === "unavailable" ? when(m.last_attempt_at, nowMs) : null;
    // Beside newer filtered lines, a parked conclusion carries its own date.
    const parkedAt = filtered && m.availability === "available" && parseDate(m.last_success_at)
      ? " · parked read " + filteredTime(m.last_success_at) : "";
    const parkedText = failedAt ? status.text + " · parked read failed " + failedAt : status.text + parkedAt;
    return {
      key: str(m.module_key) || "module:" + i,
      name: str(m.module_name) || str(m.module_key) || "Unknown module",
      meta: [busName(m.logical_bus), parkedText, vanText, ...driveReadLines(m)].filter(Boolean).join(" · "),
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
  const total = num(coverage.total_modules) !== null ? coverage.total_modules : modules.filter((m) => m.parked_module !== false).length;
  const gaps = rows.filter((r) => r.gap).length;
  return {
    summary: (modules.some((m) => m.van_read || m.drive_read || m.drive_truncated || m.drive_failure) ? "Parked reads" : "Modules") +
      (total > 0 ? " · " + answered + " of " + total + " answered" : " · no coverage yet"),
    rows: rows.map((r) => ({ key: r.key, name: r.name, meta: r.meta, tone: r.tone })),
    gaps,
  };
}

/** A group's unique count, or its bounds when hidden parked records may repeat shown ones. */
function countRange(group) {
  if (!group.hidden) return fmtInt(group.total);
  const low = Math.max(group.returned, group.hidden);
  return low === group.total ? fmtInt(low) : fmtInt(low) + "–" + fmtInt(group.total);
}

function sumCounts(counts) {
  let total = 0;
  const keys = Object.keys(counts);
  for (let i = 0; i < keys.length; i += 1) total += num(counts[keys[i]]) || 0;
  return total;
}

// ---------------------------------------------------------------------------
// the van's own health check (docs/caveats.md "The van's own health check")

/** Filtered source line with coverage and an explicit Mountain date. */
export function vanSourceLine(iso, nowMs) {
  return filteredSourceLine(VAN_SOURCE, iso);
}

function codeGroups(d, nowMs) {
  const groups = obj(d.groups) || {};
  const counts = obj(d.group_counts) || {};
  const returnedCounts = obj(d.group_returned_counts) || {};
  const hiddenCounts = obj(d.group_hidden_counts);
  const cutoff = historyCutoff(nowMs);
  const groupModels = [];
  for (let g = 0; g < DTC_GROUPS.length; g += 1) {
    const key = DTC_GROUPS[g][0];
    const title = DTC_GROUPS[g][1];
    const entries = arr(groups[key]).filter(obj);
    const hidden = hiddenCounts ? num(hiddenCounts[key]) || 0 : 0;
    const total = (num(counts[key]) !== null ? counts[key] : entries.length) + hidden;
    const returned = Math.min(entries.length, num(returnedCounts[key]) !== null ? returnedCounts[key] : entries.length);
    if ((key === "other" || key === "not_reported") && total <= 0) continue;
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
    // Merged with filtered reads, `total` is shown + hidden, an upper bound, never shown as exact.
    const summary = hidden > 0
      ? title + " · " + fmtInt(returned) + " shown · +" + fmtInt(hidden) + " more in the parked read"
      : title + " · " + fmtInt(total) + (cut ? " · showing " + returned + " of " + fmtInt(total) : "");
    const unreturned = unreturnedNote(num(d.filter_unreturned?.[key]) || 0);
    const moreText = unreturned ? unreturned : hidden > 0
      ? "The parked read counted " + fmtInt(hidden) + " more it did not return; some may be codes shown here."
      : cut && d.drive_reads?.available === true ? "+" + fmtInt(total - returned) + " more from the parked read (not returned in this list)" : null;
    groupModels.push({
      key,
      title,
      total,
      returned,
      hidden,
      summary,
      rows: recent,
      moreText,
      older,
      olderSummary: older.length ? "Older than one month · " + older.length : null,
      note: key === "incomplete_only" ? "Not a fault: the module had not finished that self-test when it was read." : null,
    });
  }
  return groupModels;
}

/**
 * Diagnostic-codes card model. Totals come from `group_counts` (the broker truncates each group at
 * `per_group_limit`, 25 today); `showing n of m` appears when a group was cut. Ignored codes leave
 * their group and every count and the badge; the module filter narrows the groups but not the badge.
 * @param {object|null} dtcs `summary.dtcs` slice
 * @param {number} nowMs wall clock for labels and the one-month cut-off
 * @param {boolean} scanEnabled this listener can queue the guarded scan
 * @param {{ignores?: object|null, modules?: string[]}} [view] `summary.dtc_ignores` slice and the
 *   chosen module keys (none: every module)
 */
export function codesView(dtcs, nowMs, scanEnabled, view = {}) {
  const base = { status: "ok", badge: null, lastRead: null, source: null, emptyText: null, current: null, groups: [], modules: null,
    filter: null, ignored: null, ignoreAvailable: false };
  if (dtcs === null || dtcs === undefined) return { ...base, status: "loading", emptyText: "Waiting for the saved code list…" };
  let d = obj(dtcs) || {};
  if (d.available === false) {
    const van = obj(d.in_vehicle_scan);
    if ((!van || van.available !== true) && d.drive_reads?.available !== true) {
      return { ...base, status: "unavailable", emptyText: str(d.detail) || "The saved code list is unavailable." };
    }
    // The Pi's saved list is unavailable, but the van's own check can still be shown.
    d = { in_vehicle_scan: van, drive_reads: d.drive_reads, groups: {}, group_counts: {}, group_returned_counts: {}, coverage: {}, modules: [] };
  }
  d = mergeVanScan(d);
  const ignores = ignoreState(view.ignores);
  const { dtcs: kept, found } = excludeIgnored(d, ignores.index);
  const options = moduleOptions(kept, ignores.entries);
  const chosen = activeModules(arr(view.modules), options);
  const keptGroups = codeGroups(kept, nowMs);
  const groupModels = chosen.length ? codeGroups(filterByModules(kept, chosen), nowMs) : keptGroups;
  const newest = newestSuccess(d);
  const coverage = obj(d.coverage) || {};
  const anyRead = newest || str(coverage.last_attempt_at);
  const unlisted = unlistedReports(d);
  const records = keptGroups.reduce((n, g) => n + g.total, 0);
  const shownRecords = groupModels.reduce((n, g) => n + g.total, 0);
  const modules = modulesView(d, nowMs);
  const moduleRows = moduleList(d);
  const authoritativeZero =
    moduleRows.length > 0 &&
    records === 0 &&
    found.size === 0 &&
    modules.gaps === 0 &&
    moduleRows.every((m) => m.result_state === "no_dtcs" && m.absence_authoritative === true);

  let emptyText = null;
  if (records === 0) {
    if (!anyRead) {
      emptyText =
        "No saved code read yet. " +
        (scanEnabled ? "The parked scan below can read the modules." : "This screen cannot start a scan.");
    } else if (found.size > 0) {
      emptyText = "No codes besides the ignored ones.";
    } else if (unlisted.length) {
      emptyText = "No codes listed, but " + unlisted.map((u) => u.name + " reported " +
        plural(u.count, "code", "codes") + " " + filteredTime(u.at)).join("; ") + " (list not retrieved).";
    } else if (authoritativeZero) {
      emptyText = "No codes in the last parked read of every module" +
        (parseDate(coverage.last_success_at) ? " · " + filteredTime(coverage.last_success_at) : "") + ".";
    } else {
      emptyText = "No saved codes, but some modules did not answer fully, so a clean result is not proven.";
    }
  } else if (shownRecords === 0) {
    emptyText = "No saved codes in the chosen modules.";
  }
  const current = groupModels.find((g) => g.key === "current") || null;
  const keptCurrent = keptGroups.find((g) => g.key === "current") || null;
  const currentCount = keptCurrent ? keptCurrent.total : 0;
  let badge = null;
  if (currentCount > 0) badge = { text: countRange(keptCurrent) + " current", tone: "amber" };
  else if (anyRead) badge = { text: moduleList(d).some((m) => m.van_read || m.drive_read || m.drive_truncated)
    ? "None shown current" : "None current", tone: "" };
  const van = obj(d.van);
  return {
    ...base,
    badge,
    lastRead: newest ? "Last read " + fmtTime(newest, new Date(nowMs))
      : anyRead ? "Last attempt " + fmtTime(anyRead, new Date(nowMs)) : null,
    source: van
      ? plural(van.modules, "module", "modules") + " " + vanSourceLine(van.scanAt || van.lastAt, nowMs)
      : null,
    emptyText,
    current: shownRecords > 0 ? current : null,
    groups: shownRecords > 0 ? groupModels.filter((g) => g.key !== "current") : [],
    modules,
    filter: options.length ? { options, chosen } : null,
    ignoreAvailable: ignores.available,
    ignored: ignores.available && ignores.entries.length ? ignoredGroup(ignores.entries, found, chosen, nowMs, codeRow) : null,
  };
}
