import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import * as H from "../src/views/health.helpers.js";
import { buildWarningCards, FORBIDDEN_WORDS } from "../src/warnings.js";
import { fmtTime } from "../src/format.js";

const SUMMARY = JSON.parse(fs.readFileSync(new URL("./fixtures/summary-v2.json", import.meta.url), "utf8"));
const NOW = Date.parse("2026-09-22T19:00:00Z");
const JARGON = /\b(REGISTERED|ALFA|MAPPED|candidate|regime|MAD|deviation|persisten\w*|episode|unavailable|null|undefined|NaN)\b|\d+\/\d+ LIVE/i;

function textsOf(value, out = [], skip = []) {
  if (typeof value === "string") out.push(value);
  else if (Array.isArray(value)) value.forEach((v) => textsOf(v, out, skip));
  else if (value && typeof value === "object") {
    for (const [k, v] of Object.entries(value)) {
      // ids, keys and chat contexts are not rendered as text
      if (k === "id" || k === "key" || k === "chat" || k === "rule" || skip.includes(k)) continue;
      textsOf(v, out, skip);
    }
  }
  return out;
}

function card(over = {}) {
  return {
    id: "episode:7",
    tier: "warning",
    metric: "tire.pressure.rr",
    title: "RR tire low",
    line: "RR tire 74 psi, 4 psi under its usual 78 for 5 min. Check pressure at the next stop.",
    since: "2026-09-22T18:40:00Z",
    sinceLabel: "12:40 pm",
    state: "warning",
    episodeId: 7,
    rule: "tire_pressure_rr_relative_low",
    category: "vehicle_health",
    action: "Check pressure at the next stop.",
    ackable: true,
    severity: "warning",
    group: null,
    value: 74,
    unit: "psi",
    ...over,
  };
}

function model(warnings, extra = {}) {
  return {
    available: true,
    reason: null,
    generatedAt: null,
    warnings,
    systemNotes: [],
    counts: {},
    delivery: { enabled: true, pending: 0, failed: 0, delivered: 0, cancelled: 0, lastError: null, lastAttemptAt: null, show: false },
    ...extra,
  };
}

// ---------------------------------------------------------------------------
// card registry

test("Health cards have stable customiser ids in design order", () => {
  assert.deepEqual(H.HEALTH_CARD_IDS, ["warnings", "notes", "codes", "service"]);
  assert.ok(H.HEALTH_CARDS.every((c) => typeof c.label === "string" && c.label.length > 0));
  assert.equal(H.DOCS.warnings, "/docs/warnings.html");
  assert.equal(H.DOCS.caveats, "/docs/caveats.html");
});

// ---------------------------------------------------------------------------
// early warning

test("warnings: waiting before the first summary, honest when the health slice is unavailable", () => {
  const loading = H.warningsView(buildWarningCards(null, NOW), null);
  assert.equal(loading.status, "loading");
  assert.match(loading.emptyText, /^Waiting/);
  const down = H.warningsView(buildWarningCards({ available: false, reason: "x" }, NOW), { available: false });
  assert.equal(down.status, "unavailable");
  assert.match(down.emptyText, /does not mean warnings are cleared/);
  assert.equal(down.badge, null);
  const detail = H.warningsView(buildWarningCards({ available: false }, NOW), { available: false, detail: "Broker cache is cold." });
  assert.equal(detail.emptyText, "Broker cache is cold.");
});

test("warnings: the September 22 payload has two unconfirmed items and nothing open", () => {
  const m = buildWarningCards(SUMMARY.health, NOW);
  const vm = H.warningsView(m, SUMMARY.health);
  assert.equal(vm.status, "ok");
  assert.equal(vm.open.length, 0);
  assert.equal(vm.badge, null);
  assert.equal(vm.emptyText, "Nothing open right now.");
  assert.equal(vm.unconfirmed.length, 2);
  const rr = vm.unconfirmed.find((r) => r.episodeId === 614);
  assert.equal(rr.line, "RR tire low");
  assert.match(rr.meta, /^engine off since .+ · opened .+$/);
  assert.doesNotMatch(rr.meta, /Unconfirmed/);
  assert.deepEqual(rr.chat, { eventId: 614, title: "RR tire low" });
  assert.equal(rr.tone, "");
  assert.equal(vm.delivery, null, "delivery line only when something is pending or failed");
  assert.equal(vm.rulesError, null);
});

test("warnings: open rows carry the sentence, since-when, tier dot and actions", () => {
  const vm = H.warningsView(
    model([
      card(),
      card({ id: "rule:engine_oil_pressure_low", episodeId: null, tier: "critical", state: "watch", severity: "critical", rule: "engine_oil_pressure_low", title: "Oil pressure critical", line: "Oil pressure 9 psi at 1,800 rpm, below the 12 psi minimum. Stop the engine when safe.", sinceLabel: null }),
      card({ id: "rule:custom_x", episodeId: null, tier: "notice", state: "watch", rule: "custom_x", title: "Oil temperature high" }),
    ]),
    {},
  );
  assert.equal(vm.open.length, 3);
  assert.deepEqual(vm.badge, { text: "3 open", tone: "red" });
  const [tire, oil, notice] = vm.open;
  assert.equal(tire.line, card().line);
  assert.equal(tire.meta, "since 12:40 pm");
  assert.equal(tire.tone, "amber");
  assert.equal(tire.episodeId, 7);
  assert.deepEqual(tire.chat, { eventId: 7, title: "RR tire low" });
  assert.equal(oil.tone, "red");
  assert.equal(oil.meta, null);
  assert.equal(oil.episodeId, null, "no Details without a saved event");
  assert.deepEqual(oil.chat, { assessment: { rule: "engine_oil_pressure_low", title: "Oil pressure critical" }, title: "Oil pressure critical" });
  assert.equal(notice.tone, "");
});

test("warnings: badge is amber without a critical item and absent when nothing is open", () => {
  assert.deepEqual(H.warningsView(model([card(), card({ id: "episode:8", episodeId: 8, state: "watch" })]), {}).badge, { text: "2 open", tone: "amber" });
  assert.equal(H.warningsView(model([card({ state: "unconfirmed" })]), {}).badge, null);
});

test("warnings: recovering items sit with the unconfirmed ones without a trailing full stop", () => {
  const vm = H.warningsView(model([card({ state: "recovering", line: "RR tire back to 78 psi at 2:11 pm." })]), {});
  assert.equal(vm.open.length, 0);
  assert.equal(vm.unconfirmed[0].meta, "RR tire back to 78 psi at 2:11 pm · opened 12:40 pm");
});

test("warnings: delivery line appears only for pending, failed or errored alerts", () => {
  assert.equal(H.deliveryLine({ show: false, pending: 0, failed: 0 }), null);
  assert.equal(H.deliveryLine({ show: true, pending: 2, failed: 0, lastError: null }), "Phone alerts: 2 alerts waiting to send");
  assert.equal(
    H.deliveryLine({ show: true, pending: 1, failed: 1, lastError: "ntfy HTTP 502" }),
    "Phone alerts: 1 alert waiting to send · 1 alert failed to send · last error: ntfy HTTP 502",
  );
  const vm = H.warningsView(model([], { delivery: { show: true, pending: 0, failed: 3, lastError: null } }), {});
  assert.equal(vm.delivery, "Phone alerts: 3 alerts failed to send");
});

test("warnings: a broken owner rule file is reported", () => {
  const vm = H.warningsView(model([]), { custom_rules: { count: 1, error: "rules file unreadable" } });
  assert.equal(vm.rulesError, "Your saved warnings could not be loaded: rules file unreadable");
});

test("warnings: no provenance or evaluator jargon reaches the card", () => {
  const vm = H.warningsView(buildWarningCards(SUMMARY.health, NOW), SUMMARY.health);
  for (const t of textsOf(vm)) {
    assert.doesNotMatch(t, JARGON, t);
    assert.doesNotMatch(t, FORBIDDEN_WORDS, t);
  }
});

// ---------------------------------------------------------------------------
// system notes

test("notes: the payload has no active notes but lists recovered sample filters", () => {
  const m = buildWarningCards(SUMMARY.health, NOW);
  const vm = H.systemNotesView(m, SUMMARY.health, NOW);
  assert.equal(vm.loading, false);
  assert.equal(vm.count, 0);
  assert.equal(vm.notes.length, 0);
  const resolved = SUMMARY.health.data_quality.recent.filter((e) => e.status === "resolved");
  assert.equal(vm.recovered.length, Math.min(resolved.length, H.RECOVERED_LIMIT));
  assert.equal(vm.summary, "Nothing active · " + vm.recovered.length + " recovered");
  const first = vm.recovered[0];
  assert.equal(first.line, "Transmission oil sample filter cleared " + fmtTime(resolved[0].resolved_at, new Date(NOW)) + ".");
  assert.equal(first.meta, "5 implausible readings ignored · last good value kept");
  assert.deepEqual(first.chat, { event: { kind: "quality", id: resolved[0].incident_id }, title: "Transmission oil sample filter recovered" });
  for (const t of textsOf(vm)) assert.doesNotMatch(t, JARGON, t);
});

test("notes: at most three recovered incidents, as the former static app", () => {
  const recent = Array.from({ length: 6 }, (_, i) => ({ status: i === 1 ? "active" : "resolved", incident_id: "i" + i, metric: "transmission.oil_temperature", resolved_at: "2026-09-15T23:23:46Z", rejection_count: 1 }));
  const vm = H.systemNotesView(model([]), { data_quality: { recent } }, NOW);
  assert.deepEqual(vm.recovered.map((r) => r.id), ["recovered:i0", "recovered:i2", "recovered:i3"]);
  assert.equal(vm.recovered[0].meta, "1 implausible reading ignored · last good value kept");
});

test("notes: active data-quality and infrastructure items, with Ask Codex on the right event", () => {
  const health = {
    available: true,
    assessments: [
      { rule: "can_interface_role_b_can", category: "can_infrastructure", state: "warning", severity: "warning", title: "b-can interface role is unhealthy", current: { role: "b-can", reason: "adapter_missing" }, evaluated_at: "2026-09-22T18:00:00Z" },
    ],
    episodes: { active: [] },
    data_quality: {
      active: [{ incident_id: "dq-1", metric: "transmission.oil_temperature", rejection_count: 3, first_seen_at: "2026-09-22T18:30:00Z", status: "active" }],
      recent: [],
    },
  };
  const vm = H.systemNotesView(buildWarningCards(health, NOW), health, NOW);
  assert.equal(vm.count, 2);
  assert.equal(vm.summary, "2 notes");
  const dq = vm.notes.find((r) => r.id === "data-quality:dq-1");
  assert.deepEqual(dq.chat, { event: { kind: "quality", id: "dq-1" }, title: "Transmission oil sample filtered" });
  const bus = vm.notes.find((r) => r.id === "rule:can_interface_role_b_can");
  assert.match(bus.line, /^B-CAN adapter missing/);
  assert.deepEqual(bus.chat, { assessment: { rule: "can_interface_role_b_can", title: bus.title }, title: bus.title });
  assert.equal(bus.episodeId, null);
});

test("notes: loading and quiet states", () => {
  assert.equal(H.systemNotesView(model([]), null, NOW).loading, true);
  const quiet = H.systemNotesView(model([]), { data_quality: { recent: [] } }, NOW);
  assert.equal(quiet.summary, null);
  assert.match(quiet.emptyText, /quiet/);
});

test("chat context: USB incidents have no advisor event", () => {
  assert.equal(H.chatContext({ id: "usb:abc", rule: "usb_can_netdev_removed", category: "can_infrastructure", episodeId: null }), null);
  assert.equal(H.chatContext({ id: "data-quality:", category: "data_quality", episodeId: null }), null);
});

// ---------------------------------------------------------------------------
// diagnostic codes

test("history cut-off is the same UTC day one month back, clamped to month length", () => {
  assert.equal(new Date(H.historyCutoff(Date.parse("2026-09-23T05:00:00Z"))).toISOString(), "2026-08-23T05:00:00.000Z");
  assert.equal(new Date(H.historyCutoff(Date.parse("2026-03-31T12:00:00Z"))).toISOString(), "2026-02-28T12:00:00.000Z");
  assert.equal(new Date(H.historyCutoff(Date.parse("2026-01-15T00:00:00Z"))).toISOString(), "2025-12-15T00:00:00.000Z");
});

test("codes: the September payload — 5 current, truncated groups, one-month split, coverage", () => {
  const vm = H.codesView(SUMMARY.dtcs, Date.parse("2026-09-23T12:00:00Z"), false);
  assert.equal(vm.status, "ok");
  assert.deepEqual(vm.badge, { text: "5 current", tone: "amber" });
  assert.match(vm.lastRead, /^Last read /);
  assert.equal(vm.emptyText, null);
  assert.equal(vm.current.total, 5);
  assert.equal(vm.current.rows.length, 5);
  assert.equal(vm.current.summary, "Current · 5");
  const byKey = Object.fromEntries(vm.groups.map((g) => [g.key, g]));
  assert.deepEqual(Object.keys(byKey), ["pending", "confirmed_history", "incomplete_only"], "other is hidden at zero");
  assert.equal(byKey.pending.total, 0);
  assert.equal(byKey.confirmed_history.summary, "Confirmed history · 38 · showing 25 of 38");
  assert.equal(byKey.confirmed_history.rows.length + byKey.confirmed_history.older.length, 25);
  assert.equal(byKey.confirmed_history.older.length, 25, "all July records are older than one month on Sep 23");
  assert.equal(byKey.confirmed_history.olderSummary, "Older than one month · 25");
  assert.equal(byKey.incomplete_only.summary, "Test not completed · 213 · showing 25 of 213");
  assert.match(byKey.incomplete_only.note, /^Not a fault/);
  assert.equal(vm.modules.summary, "Modules · 11 of 16 answered");
  assert.equal(vm.modules.rows.length, 16);
  assert.equal(vm.modules.rows[0].key, "pcm", "modules that did not answer come first");
  assert.equal(vm.modules.rows[0].tone, "amber");
  assert.match(vm.modules.rows[0].meta, /^C-CAN · no answer \(timeout\) · never answered$/);
  assert.deepEqual(vm.modules.rows.slice(1, 5).map((r) => r.meta), Array(4).fill("CAN-CH · never read"));
  const tcm = vm.modules.rows.find((r) => r.key === "tcm");
  assert.equal(tcm.meta, "C-CAN · 54 codes");
  const ics = vm.modules.rows.find((r) => r.key === "ics_bcan");
  assert.equal(ics.meta, "B-CAN · no codes");
  // OEM code titles are shown verbatim (some say "unavailable"); everything the view composes is checked.
  for (const t of textsOf(vm, [], ["meaning"])) assert.doesNotMatch(t, JARGON, t);
});

test("codes: a row shows code, meaning, module and last seen", () => {
  const entry = SUMMARY.dtcs.groups.current[0];
  const now = Date.parse("2026-09-23T12:00:00Z");
  const row = H.codeRow(entry, now);
  assert.equal(row.code, "B1632-15");
  assert.equal(row.meaning, "Left high-beam circuit — Short to battery or open");
  assert.equal(row.reviewed, true);
  assert.equal(row.meta, "Body Control Module (C-CAN diagnostic endpoint) · last seen " + fmtTime(entry.last_seen_at, new Date(now)));
  assert.equal(row.key, "bcm_ccan:963215");
});

test("codes: missing or unreviewed meanings are marked, never blank", () => {
  const bare = H.codeRow({ raw_dtc: "C12345", module_key: "abs" }, NOW);
  assert.equal(bare.code, "C12345");
  assert.equal(bare.meaning, "meaning not reviewed");
  assert.equal(bare.reviewed, false);
  assert.equal(bare.meta, "abs");
  const unreviewed = H.codeRow({ fca_display: "U0100-00", description: "Lost communication with ECM/PCM", description_reviewed: false }, NOW);
  assert.equal(unreviewed.reviewed, false);
  assert.equal(unreviewed.meaning, "Lost communication with ECM/PCM");
  assert.equal(H.codeRow({}, NOW).code, "—");
  const placeholder = H.codeRow({ fca_display: "P1C73-24", description: "No reviewed module-specific FCA component meaning; failure subtype: Signal stuck high", description_reviewed: false }, NOW);
  assert.equal(placeholder.meaning, "meaning not reviewed · Signal stuck high");
  assert.equal(placeholder.reviewed, false);
});

test("codes: totals come from group_counts and duplicate keys stay unique", () => {
  const dtcs = {
    available: true,
    coverage: { last_attempt_at: "2026-09-20T10:00:00Z", total_modules: 1, available_modules: 1 },
    group_counts: { current: 3, pending: 2, confirmed_history: 0, incomplete_only: 0, other: 1 },
    group_returned_counts: { current: 2, pending: 2, other: 1 },
    groups: {
      current: [{ raw_dtc: "1", module_key: "m" }, { raw_dtc: "1", module_key: "m" }],
      pending: [{ raw_dtc: "2", module_key: "m" }, { raw_dtc: "3", module_key: "m" }],
      other: [{ raw_dtc: "4", module_key: "m" }],
    },
    modules: [{ module_key: "m", availability: "available", result_state: "dtcs_present", last_success_dtc_count: 6, absence_authoritative: true }],
  };
  const vm = H.codesView(dtcs, NOW, false);
  assert.deepEqual(vm.badge, { text: "3 current", tone: "amber" });
  assert.equal(vm.current.summary, "Current · 3 · showing 2 of 3");
  assert.deepEqual(vm.current.rows.map((r) => r.key), ["m:1", "m:1#1"]);
  const other = vm.groups.find((g) => g.key === "other");
  assert.equal(other.title, "Other status combinations");
  assert.equal(other.summary, "Other status combinations · 1");
  assert.equal(vm.groups.find((g) => g.key === "pending").summary, "Pending · 2");
});

test("codes: loading, unavailable, never read, clean and unproven-clean states", () => {
  assert.equal(H.codesView(null, NOW, false).status, "loading");
  const down = H.codesView({ available: false, detail: "DTC cache unreadable" }, NOW, false);
  assert.equal(down.status, "unavailable");
  assert.equal(down.emptyText, "DTC cache unreadable");
  assert.equal(H.codesView({ available: false }, NOW, false).emptyText, "The saved code list is unavailable.");

  const never = { available: true, coverage: {}, group_counts: {}, groups: {}, modules: [] };
  assert.equal(H.codesView(never, NOW, false).emptyText, "No saved code read yet. This screen cannot start a scan.");
  assert.equal(H.codesView(never, NOW, true).emptyText, "No saved code read yet. The parked scan below can read the modules.");
  assert.equal(H.codesView(never, NOW, true).badge, null);
  assert.equal(H.codesView(never, NOW, true).current, null);

  const clean = {
    available: true,
    coverage: { last_success_at: "2026-09-20T10:00:00Z", total_modules: 2, available_modules: 2 },
    group_counts: { current: 0, pending: 0, confirmed_history: 0, incomplete_only: 0, other: 0 },
    groups: {},
    modules: [
      { module_key: "a", availability: "available", result_state: "no_dtcs", last_success_dtc_count: 0, absence_authoritative: true },
      { module_key: "b", availability: "available", result_state: "no_dtcs", last_success_dtc_count: 0, absence_authoritative: true },
    ],
  };
  const cleanVm = H.codesView(clean, NOW, false);
  assert.equal(cleanVm.emptyText, "No codes in the last read of every module.");
  assert.deepEqual(cleanVm.badge, { text: "None current", tone: "" });
  assert.equal(cleanVm.groups.length, 0);

  const gap = { ...clean, modules: [...clean.modules, { module_key: "c", availability: "unavailable", unavailable_reason: "no_response", last_success_at: null }] };
  assert.match(H.codesView(gap, NOW, false).emptyText, /clean result is not proven/);
});

test("module status words", () => {
  assert.equal(H.moduleStatus({ availability: "never_scanned" }, NOW).text, "never read");
  const lastGood = H.moduleStatus({ availability: "unavailable", unavailable_reason: "no_response", last_success_at: "2026-09-20T10:00:00Z" }, NOW);
  assert.equal(lastGood.text, "no answer (no response) · last good read " + fmtTime("2026-09-20T10:00:00Z", new Date(NOW)));
  assert.equal(H.moduleStatus({ availability: "available", result_state: "status_coverage_incomplete" }, NOW).tone, "amber");
  assert.equal(H.moduleStatus({ availability: "available", last_success_dtc_count: 0, absence_authoritative: false }, NOW).gap, true);
  assert.equal(H.moduleStatus({ availability: "available", result_state: "dtcs_present", last_success_dtc_count: 1 }, NOW).text, "1 code");
  assert.equal(H.moduleStatus({ availability: "available", result_state: "dtcs_present", last_success_dtc_count: 1234 }, NOW).text, "1,234 codes");
});

test("modules given as an object map are accepted", () => {
  const vm = H.modulesView({ modules: { a: { module_key: "a", module_name: "A", availability: "available", result_state: "no_dtcs", last_success_dtc_count: 0, absence_authoritative: true, logical_bus: "b-can" } }, coverage: {} }, NOW);
  assert.equal(vm.summary, "Modules · 1 of 1 answered");
  assert.deepEqual(vm.rows, [{ key: "a", name: "A", meta: "B-CAN · no codes", tone: "" }]);
  assert.equal(H.modulesView({ modules: [], coverage: {} }, NOW).summary, "Modules · no coverage yet");
});

// ---------------------------------------------------------------------------
// feature-parity additions (former static app items that had no home in v2)

test("parity: warnings say when phone alerts are switched off on the Pi", () => {
  const off = { notification_delivery: { enabled: false } };
  assert.equal(H.deliveryLine({ show: false, pending: 0, failed: 0 }, true), "Phone alerts: off on the Pi");
  assert.equal(H.warningsView(model([]), off).delivery, "Phone alerts: off on the Pi");
  assert.equal(
    H.warningsView(model([], { delivery: { show: true, pending: 2, failed: 0, lastError: null } }), off).delivery,
    "Phone alerts: off on the Pi · 2 alerts waiting to send",
  );
  // Enabled (the September 22 payload) and a slice without the delivery object stay quiet.
  assert.equal(H.warningsView(buildWarningCards(SUMMARY.health, NOW), SUMMARY.health).delivery, null);
  assert.equal(H.warningsView(model([]), {}).delivery, null);
});

test("parity: with nothing open, the card says what the checks can see (was TRAINING / DATA UNAVAILABLE)", () => {
  const vm = H.warningsView(buildWarningCards(SUMMARY.health, NOW), SUMMARY.health);
  assert.equal(vm.emptyText, "Nothing open right now.");
  assert.equal(vm.checks, "12 of 12 checks are waiting for fresh readings.");
  const mixed = { assessments: [{ state: "insufficient_history" }, { state: "unavailable" }, { state: "normal" }] };
  assert.equal(
    H.checksNote(mixed),
    "1 of 3 checks are still learning normal readings · 1 of 3 checks are waiting for fresh readings.",
  );
  assert.equal(H.checksNote({ assessments: [{ state: "normal" }] }), null, "all evaluating: nothing to add");
  assert.equal(H.checksNote({ assessments: [] }), "The Pi has not reported any checks yet.");
  assert.equal(H.checksNote({}), null, "no assessment list: say nothing");
  assert.equal(H.warningsView(model([card()]), mixed).checks, null, "only when nothing is open");
  for (const t of [vm.checks, H.checksNote(mixed)]) {
    assert.doesNotMatch(t, JARGON, t);
    assert.doesNotMatch(t, FORBIDDEN_WORDS, t);
  }
});

test("parity: rows whose saved event was marked reviewed say so", () => {
  const reviewed = card({ ackable: false });
  assert.equal(H.isReviewed(reviewed), true);
  assert.equal(H.isReviewed(card()), false);
  assert.equal(H.isReviewed(card({ episodeId: null, ackable: false })), false, "no saved event, nothing to review");
  assert.equal(H.openRow(reviewed).meta, "since 12:40 pm · reviewed");
  assert.equal(H.openRow(card({ ackable: false, sinceLabel: null })).meta, "reviewed");
  assert.equal(H.unconfirmedRow(card({ state: "unconfirmed", ackable: false, line: "Unconfirmed · engine off since 1:00 pm." })).meta, "engine off since 1:00 pm · opened 12:40 pm · reviewed");
});

test("parity: a saved code notes a stale module read and a requested warning light", () => {
  const entry = SUMMARY.dtcs.groups.current[0];
  const stale = H.codeRow({ ...entry, observation_state: "stale_after_unavailable_attempt", warning_indicator_requested: true }, NOW);
  assert.equal(
    stale.meta,
    "Body Control Module (C-CAN diagnostic endpoint) · warning light requested · module did not answer the latest read · last seen " +
      fmtTime(entry.last_seen_at, new Date(NOW)),
  );
  const older = H.codeRow({ module_key: "abs", observation_state: "retained_incompatible_status_mask", status_flags: ["confirmed", "warning_indicator_requested"] }, NOW);
  assert.equal(older.meta, "abs · warning light requested · kept from an older read");
  assert.equal(H.warningLightRequested({ status_flags: ["confirmed"] }), false);
  assert.equal(H.codeRow({ module_key: "abs", observation_state: "observed_in_latest_success" }, NOW).meta, "abs");
});

// ---------------------------------------------------------------------------
// the van's own health check (in_vehicle_scan) merged per module

function vanRecord(fca, raw, status, group) {
  return {
    raw_dtc: raw, fca_display: fca, status, display_group: group,
    current: group === "current", pending: group === "pending", confirmed: true,
    warning_indicator_requested: false, incomplete_only: false,
    description: "Reviewed " + fca, description_reviewed: true, description_source: "test",
  };
}

const VAN_SCAN = {
  available: true,
  source: "in_vehicle_scan",
  provenance: "in-vehicle scan (source F1), passive",
  scan_count: 1,
  last_scan: { started_at: "2026-09-24T21:12:17.587Z", completed_at: "2026-09-24T21:27:08.649Z", pi_quiet: true },
  modules: [
    {
      module_key: "bcm_ccan", module_name: "Body Control Module (C-CAN diagnostic endpoint)", logical_bus: "c-can",
      observed_at: "2026-09-24T21:25:43.308Z", scan_started_at: "2026-09-24T21:12:17.587Z", dtc_count: 2,
      dtcs: [vanRecord("B1632-15", "963215", "4D", "current"), vanRecord("B1636-15", "963615", "4D", "current")],
    },
    {
      module_key: "abs_canch", module_name: "Antilock Brake System (ABS/ESC)", logical_bus: "can-ch",
      observed_at: "2026-09-24T21:25:32.759Z", scan_started_at: "2026-09-24T21:12:17.587Z", dtc_count: 1,
      dtcs: [vanRecord("C1200-17", "520017", "4C", "pending")],
    },
    {
      module_key: "ics_bcan", module_name: "Integrated Center Stack", logical_bus: "b-can",
      observed_at: "2026-06-01T10:00:00Z", scan_started_at: "2026-06-01T09:55:00Z", dtc_count: 0, dtcs: [],
    },
  ],
};
const VAN_NOW = Date.parse("2026-09-24T23:00:00Z");

test("codes: the van's newer health check replaces that module's codes inside its status mask", () => {
  const dtcs = { ...SUMMARY.dtcs, in_vehicle_scan: VAN_SCAN };
  const vm = H.codesView(dtcs, VAN_NOW, false);
  const at = fmtTime("2026-09-24T21:12:17.587Z", new Date(VAN_NOW));
  // Pi: 5 current (all BCM, 0x4D) -> van: 2 current BCM codes.
  assert.equal(vm.current.total, 2);
  assert.deepEqual(vm.current.rows.map((r) => r.code).sort(), ["B1632-15", "B1636-15"]);
  const row = vm.current.rows.find((r) => r.code === "B1636-15");
  assert.equal(row.meta, "Body Control Module (C-CAN diagnostic endpoint) · from the van’s own health check · " + at);
  assert.deepEqual(vm.badge, { text: "2 current", tone: "amber" });
  const byKey = Object.fromEntries(vm.groups.map((g) => [g.key, g]));
  // BCM's five 0x08 confirmed-history records were inside the van's mask, so they are gone.
  assert.equal(byKey.confirmed_history.total, 38 - 5);
  assert.equal(byKey.pending.total, 1);
  // Modules: ABS (never read by the Pi) now answered; ICS keeps the Pi's newer read.
  assert.equal(vm.modules.summary, "Modules · 12 of 16 answered");
  const abs = vm.modules.rows.find((r) => r.key === "abs_canch");
  assert.equal(abs.meta, "CAN-CH · 1 code · from the van’s own health check · " + at);
  assert.equal(vm.modules.rows.find((r) => r.key === "ics_bcan").meta, "B-CAN · no codes");
  assert.equal(vm.source, "2 modules from the van’s own health check · " + at);
  for (const t of textsOf(vm, [], ["meaning"])) {
    assert.doesNotMatch(t, JARGON, t);
    assert.doesNotMatch(t, /\bF1\b|19 02|0D\b|in-vehicle scan/, t);
  }
});

test("codes: Pi records outside the van's mask stay; a newer Pi read wins", () => {
  const dtcs = {
    available: true,
    coverage: { last_attempt_at: "2026-07-25T02:31:34Z", total_modules: 1, available_modules: 1 },
    group_counts: { current: 0, pending: 0, confirmed_history: 0, incomplete_only: 1, other: 0 },
    group_returned_counts: { current: 0, pending: 0, confirmed_history: 0, incomplete_only: 1, other: 0 },
    groups: { incomplete_only: [{ raw_dtc: "C01100", fca_display: "U0011-00", module_key: "bcm_ccan", status: "50", last_seen_at: "2026-07-25T02:31:34Z" }] },
    modules: [{ module_key: "bcm_ccan", module_name: "BCM", logical_bus: "c-can", availability: "available", result_state: "dtcs_present", last_attempt_at: "2026-07-25T02:31:34Z", last_success_at: "2026-07-25T02:31:34Z", last_success_dtc_count: 1, absence_authoritative: true }],
    in_vehicle_scan: VAN_SCAN,
  };
  const vm = H.codesView(dtcs, VAN_NOW, false);
  assert.equal(vm.groups.find((g) => g.key === "incomplete_only").total, 1);
  assert.equal(vm.current.total, 2);
  const newer = { ...dtcs, modules: [{ ...dtcs.modules[0], last_attempt_at: "2026-09-25T00:00:00Z" }] };
  const kept = H.codesView(newer, VAN_NOW, false);
  assert.equal(kept.current.total, 0, "the Pi's newer BCM read has no current codes");
  assert.equal(kept.source, "2 modules from the van’s own health check · " + fmtTime("2026-09-24T21:12:17.587Z", new Date(VAN_NOW)));
});

test("codes: the van's check still shows when the Pi's saved list is unavailable; absent or not harvested changes nothing", () => {
  const vm = H.codesView({ available: false, detail: "DTC cache unreadable", in_vehicle_scan: VAN_SCAN }, VAN_NOW, false);
  assert.equal(vm.status, "ok");
  assert.equal(vm.current.total, 2);
  assert.equal(vm.modules.summary, "Modules · 3 of 3 answered");
  const plain = H.codesView(SUMMARY.dtcs, VAN_NOW, false);
  const off = H.codesView({ ...SUMMARY.dtcs, in_vehicle_scan: { available: false, reason: "not_harvested" } }, VAN_NOW, false);
  assert.deepEqual(off, plain);
  assert.equal(plain.source, null);
});
