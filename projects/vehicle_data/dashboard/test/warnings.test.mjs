import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  buildWarningCards,
  warningLine,
  unconfirmedLine,
  systemLine,
  systemTitle,
  templateFor,
  heldSeconds,
  warningFacts,
  metricLabel,
  wheelLabel,
  isSystemItem,
  tierOf,
  TEMPLATES,
  FORBIDDEN_WORDS,
  OPEN_STATES,
} from "../src/warnings.js";
import { fmtTime, fmtDurationLong } from "../src/format.js";

const here = dirname(fileURLToPath(import.meta.url));
const healthFixture = JSON.parse(readFileSync(join(here, "fixtures", "health-lite.json"), "utf8"));

// The fixture was generated at 2026-09-22T18:57:18Z; "now" is a few seconds later.
const NOW = Date.parse("2026-09-22T18:57:30Z");
const NOW_DATE = new Date(NOW);
const iso = (msAgo) => new Date(NOW - msAgo).toISOString();
const at = (isoString) => fmtTime(isoString, NOW_DATE);

function relative(rule, metric, value, unit, median, direction, state, extra) {
  return Object.assign(
    {
      rule,
      metric,
      category: "vehicle_health",
      severity: "warning",
      direction,
      state,
      title: "synthetic",
      current: { value, unit, observed_at: iso(2000) },
      baseline: { median, unit },
      persistence: { observed: state === "warning" ? 10 : 2, required: 10, window_seconds: 60 },
      evaluated_at: iso(1000),
      corroborators: [],
    },
    extra || {}
  );
}

function episode(id, rule, openedAgoMs, extra) {
  return Object.assign(
    { id, rule, status: "open", state: "warning", opened_at: iso(openedAgoMs), acknowledged: false, category: "vehicle_health" },
    extra || {}
  );
}

function everyLine(result) {
  const out = [];
  for (const card of result.warnings.concat(result.systemNotes)) out.push(card.line, card.title, card.action);
  return out;
}

function synthetic() {
  return {
    available: true,
    generated_at: iso(0),
    assessments: [
      relative("tire_pressure_rr_relative_low", "tire.pressure.rr", 74.0, "psi", 78, "low", "warning"),
      relative("engine_coolant_temperature_relative_high", "engine.coolant_temperature", 213.8, "°F", 190.4, "high", "warning"),
      relative("transmission_oil_temperature_relative_high", "transmission.oil_temperature", 195.2, "°F", 183.2, "high", "watch"),
      relative("battery_voltage_relative_low", "battery.voltage", 11.9, "V", 14.0, "low", "warning", {
        regime: "engine_running:road:rpm_low",
        corroborators: [{ metric: "generator.field_duty", state: "corroborating", current: { value: 100, unit: "%" } }],
      }),
      relative("engine_oil_pressure_relative_low", "engine.oil_pressure", 22.3, "psi", 30.2, "low", "warning"),
      {
        rule: "engine_oil_pressure_absolute_critical",
        metric: "engine.oil_pressure",
        category: "vehicle_health",
        severity: "critical",
        direction: "low",
        state: "warning",
        absolute_threshold: { operator: "below", unit: "psi", value: 12 },
        current: { value: 9.2, unit: "psi", observed_at: iso(1000) },
        running_evidence: { qualified: true, rpm: { value: 1850, unit: "rpm" } },
        persistence: { observed: 2, required: 2, window_seconds: 10 },
      },
      {
        rule: "custom_abc123",
        metric: "engine.coolant_temperature",
        category: "vehicle_health",
        severity: "warning",
        direction: "high",
        state: "warning",
        custom_rule: { operator: "above", threshold: 230, metric: "engine.coolant_temperature", title: "Coolant over 230" },
        current: { value: 232, unit: "°F", observed_at: iso(1000) },
        persistence: { observed: 6, required: 6, window_seconds: 30 },
      },
      {
        rule: "can_interface_role_c_can",
        category: "can_infrastructure",
        severity: "warning",
        state: "warning",
        title: "c-can interface role is unhealthy",
        reason: "interface health check failed: interface_role_absent",
        current: { role: "c-can", reason: "interface_role_absent", active_gap: { started_at: iso(300000) } },
      },
      {
        rule: "can_interface_role_b_can",
        category: "can_infrastructure",
        severity: "warning",
        state: "watch",
        title: "b-can interface role is unhealthy",
        reason: "interface health check failed: topology_unusable",
        current: { role: "b-can", reason: "topology_unusable", role_reason: "broker auxiliary-drive owner has the resolved B-CAN channel armed" },
        evaluated_at: iso(5000),
      },
      { rule: "usb_can_transient_disconnect", category: "can_infrastructure", severity: "warning", state: "warning", title: "USB CAN branch transiently disconnected", evaluated_at: iso(60000) },
      { rule: "can_restoration_inhibit", category: "can_infrastructure", severity: "critical", state: "warning", title: "CAN restoration inhibit is active", evaluated_at: iso(20000) },
      { rule: "telemetry_gap_engine_oil_pressure", metric: "engine.oil_pressure", category: "telemetry_quality", severity: "info", state: "warning", title: "Oil pressure readings missing while running", evaluated_at: iso(70000) },
      // A normal assessment for a rule that is also open: ignored, the open one wins.
      { rule: "engine_coolant_temperature_relative_high", state: "normal", category: "vehicle_health" },
      // Bounded-copy sentinel from web_v2.shrink(): must be skipped.
      { omitted_items: 4 },
    ],
    active: [relative("tire_pressure_rr_relative_low", "tire.pressure.rr", 74.0, "psi", 78, "low", "warning")],
    episodes: {
      active: [
        episode(700, "tire_pressure_rr_relative_low", 5 * 60000),
        episode(701, "engine_coolant_temperature_relative_high", 60000),
        episode(702, "engine_oil_pressure_absolute_critical", 10000, { acknowledged: true }),
        episode(703, "engine_vvt_oil_temperature_relative_high", 3600000, {
          state: "watch",
          latest_assessment: {
            rule: "engine_vvt_oil_temperature_relative_high",
            metric: "engine.vvt_oil_temperature",
            category: "vehicle_health",
            state: "recovering",
            current: { value: 198, unit: "°F", observed_at: iso(30000) },
          },
        }),
      ],
      notification_outbox: { pending: 1, failed: 0, delivered: 3, cancelled: 0 },
    },
    data_quality: {
      active: [{ incident_id: "x:1", metric: "transmission.oil_temperature", reason: "implausible_transition", rejection_count: 5, first_seen_at: iso(120000) }],
    },
    usb_can_incidents: {
      active: [{ incident_id: "u:1", kind: "usb_can_netdev_removed", affected_serials: ["207C3384413250013"], opened_at: iso(90000), state: "active" }],
    },
    notification_delivery: { enabled: true, last_error: null, last_attempt_at: iso(500) },
  };
}

test("real summary: two open episodes render as unconfirmed, nothing else", () => {
  const result = buildWarningCards(healthFixture, NOW);
  assert.equal(result.available, true);
  assert.equal(result.generatedAt, "2026-09-22T18:57:18.441057+00:00");
  assert.equal(result.warnings.length, 2);
  assert.equal(result.systemNotes.length, 0);
  assert.deepEqual(result.counts, { critical: 0, warning: 2, notice: 0, system: 0, open: 2, unconfirmed: 2 });

  const tire = result.warnings.find((c) => c.rule === "tire_pressure_rr_relative_low");
  const coolant = result.warnings.find((c) => c.rule === "engine_coolant_temperature_relative_high");
  assert.ok(tire && coolant);
  assert.equal(tire.id, "episode:614");
  assert.equal(tire.episodeId, 614);
  assert.equal(tire.state, "unconfirmed");
  assert.equal(tire.tier, "warning");
  assert.equal(tire.title, "RR tire low");
  assert.equal(tire.metric, "tire.pressure.rr");
  // Last fresh sample of the episode's latest assessment is when the engine went off.
  assert.equal(tire.line, "Unconfirmed · engine off since " + at("2026-09-22T16:25:17.576833+00:00"));
  assert.equal(tire.since, "2026-09-22T02:21:28.510364+00:00");
  assert.equal(tire.sinceLabel, at("2026-09-22T02:21:28.510364+00:00"));
  assert.equal(tire.ackable, true);
  assert.equal(tire.action, "");
  assert.equal(tire.value, 74.8, "the retained value is carried for the row, not the line");
  assert.equal(tire.unit, "psi");

  assert.equal(coolant.id, "episode:598");
  assert.equal(coolant.title, "Coolant hot");
  assert.equal(coolant.line, "Unconfirmed · engine off since " + at("2026-09-22T16:25:20.503046+00:00"));
  // Newest episode first among equals.
  assert.equal(result.warnings[0].id, "episode:614");

  for (const text of everyLine(result)) assert.doesNotMatch(text, FORBIDDEN_WORDS);
  assert.deepEqual(result.delivery, {
    enabled: true,
    pending: 0,
    failed: 0,
    delivered: 128,
    cancelled: 0,
    lastError: null,
    lastAttemptAt: "2026-09-22T18:57:12.756863+00:00",
    show: false,
  });
});

test("templates: one plain sentence per rule family, number first, action last", () => {
  const result = buildWarningCards(synthetic(), NOW);
  const byRule = new Map(result.warnings.map((c) => [c.rule, c]));

  assert.equal(
    byRule.get("tire_pressure_rr_relative_low").line,
    "RR tire 74 psi, 4 psi under its usual 78 for 5 min. Check pressure at the next stop."
  );
  assert.equal(
    byRule.get("engine_coolant_temperature_relative_high").line,
    "Coolant 214 °F, 24 °F over its usual 190 for 1 min. Ease off and watch the gauge."
  );
  assert.equal(
    byRule.get("transmission_oil_temperature_relative_high").line,
    "Transmission oil 195 °F, 12 °F over its usual 183 for 12 s. Ease off and let it cool."
  );
  assert.equal(
    byRule.get("battery_voltage_relative_low").line,
    "Battery 11.90 V, 2.10 V under its usual 14.00 with the alternator at 100 % for 1 min. Limit accessories and check charging."
  );
  assert.equal(
    byRule.get("engine_oil_pressure_relative_low").line,
    "Oil pressure 22 psi, 8 psi under its usual 30 for 1 min. Check the oil level at the next stop."
  );
  assert.equal(
    byRule.get("engine_oil_pressure_absolute_critical").line,
    "Oil pressure 9 psi at 1,850 rpm, below the 12 psi minimum for 10 s. Stop the engine when safe."
  );
  assert.equal(byRule.get("custom_abc123").line, "Coolant 232 °F, over your 230 °F limit for 30 s. Check it at the next stop.");
  assert.equal(byRule.get("custom_abc123").title, "Coolant over 230");
  assert.equal(
    byRule.get("engine_vvt_oil_temperature_relative_high").line,
    "Oil temperature back to 198 °F at " + at(iso(30000)) + "."
  );
  assert.equal(byRule.get("engine_vvt_oil_temperature_relative_high").state, "recovering");

  // Titles and actions.
  assert.equal(byRule.get("engine_oil_pressure_absolute_critical").title, "Oil pressure critical");
  assert.equal(byRule.get("engine_oil_pressure_absolute_critical").tier, "critical");
  assert.equal(byRule.get("engine_oil_pressure_absolute_critical").ackable, false, "already acknowledged");
  assert.equal(byRule.get("battery_voltage_relative_low").title, "Charging low");
  assert.equal(byRule.get("tire_pressure_rr_relative_low").action, "Check pressure at the next stop.");
  assert.equal(byRule.get("tire_pressure_rr_relative_low").episodeId, 700);
  assert.equal(byRule.get("tire_pressure_rr_relative_low").ackable, true);
  assert.equal(byRule.get("tire_pressure_rr_relative_low").sinceLabel, at(iso(5 * 60000)));
  assert.equal(byRule.get("transmission_oil_temperature_relative_high").state, "watch");
  assert.equal(byRule.get("transmission_oil_temperature_relative_high").id, "rule:transmission_oil_temperature_relative_high");

  // Every line ends with an imperative sentence and starts with the subject and number.
  for (const card of result.warnings) {
    if (!OPEN_STATES.has(card.state)) continue;
    assert.match(card.line, /^[A-Z][A-Za-z ]+ [\d.,]+ \S/, "subject then number: " + card.line);
    assert.match(card.line, /\. [A-Z][^.]+\.$/, "imperative sentence last: " + card.line);
    assert.ok(card.line.endsWith(" " + card.action), card.line);
  }
  for (const text of everyLine(result)) assert.doesNotMatch(text, FORBIDDEN_WORDS, text);
  assert.doesNotMatch(JSON.stringify(result.warnings.map((c) => c.line)), /unavailable/i);
});

test("counts, ordering and dedupe across assessments, active and episodes", () => {
  const result = buildWarningCards(synthetic(), NOW);
  assert.deepEqual(result.counts, { critical: 1, warning: 7, notice: 0, system: 7, open: 8, unconfirmed: 1 });
  const rules = result.warnings.map((c) => c.rule);
  assert.equal(new Set(rules).size, rules.length, "one card per rule");
  assert.equal(result.warnings[0].rule, "engine_oil_pressure_absolute_critical", "critical first");
  const states = result.warnings.map((c) => c.state);
  const firstWatch = states.indexOf("watch");
  const lastWarning = states.lastIndexOf("warning");
  assert.ok(lastWarning < firstWatch, "confirmed before watch");
  assert.equal(states[states.length - 1], "recovering", "unconfirmed/recovering last");
  assert.equal(result.delivery.show, true, "one pending notification");
  assert.equal(result.delivery.pending, 1);
});

test("system notes: infrastructure, coverage, data quality and USB incidents", () => {
  const result = buildWarningCards(synthetic(), NOW);
  const notes = new Map(result.systemNotes.map((c) => [c.rule, c]));
  assert.equal(result.systemNotes.length, 7);
  for (const card of result.systemNotes) assert.equal(card.tier, "system");
  assert.equal(result.systemNotes[0].rule, "can_restoration_inhibit", "critical system item first");

  assert.equal(notes.get("can_interface_role_c_can").line, "C-CAN adapter missing since " + at(iso(300000)) + " — engine data paused.");
  assert.equal(notes.get("can_interface_role_c_can").title, "C-CAN bus unhealthy");
  assert.equal(notes.get("can_interface_role_c_can").category, "can_infrastructure");
  assert.equal(
    notes.get("can_interface_role_b_can").line,
    "B-CAN in polling mode since " + at(iso(5000)) + " (normal while the broker reads diagnostics)."
  );
  assert.equal(notes.get("usb_can_transient_disconnect").line, "USB CAN branch dropped and reconnected at " + at(iso(60000)) + " — waiting for the roles to settle.");
  assert.equal(notes.get("can_restoration_inhibit").line, "CAN restoration inhibit active since " + at(iso(20000)) + " — active reads paused.");
  assert.equal(notes.get("can_restoration_inhibit").severity, "critical");
  assert.equal(notes.get("telemetry_gap_engine_oil_pressure").line, "Oil pressure readings missing while running since " + at(iso(70000)) + ".");
  assert.equal(notes.get("telemetry_gap_engine_oil_pressure").category, "telemetry_quality");
  assert.equal(
    notes.get("implausible_transition").line,
    "Transmission oil sample filtered (5) since " + at(iso(120000)) + " — value held until a plausible reading."
  );
  assert.equal(notes.get("implausible_transition").category, "data_quality");
  assert.equal(notes.get("implausible_transition").id, "data-quality:x:1");
  assert.equal(notes.get("usb_can_netdev_removed").line, "USB CAN adapter …0013 dropped off since " + at(iso(90000)) + " — waiting for it to come back.");
  assert.equal(notes.get("usb_can_netdev_removed").id, "usb:u:1");
  for (const text of everyLine(result)) assert.doesNotMatch(text, FORBIDDEN_WORDS, text);
});

test("system notes never appear in the warning list, whatever the category says", () => {
  const health = {
    available: true,
    assessments: [
      { rule: "can_interface_role_can_ch", category: "vehicle_health", severity: "warning", state: "warning", title: "can-ch interface role is unhealthy", current: { reason: "interface_down" } },
      { rule: "usb_can_topology_generation_changed", category: "vehicle_health", state: "warning", title: "USB CAN topology generation changed", evaluated_at: iso(7000) },
      { rule: "telemetry_gap_engine_coolant_temperature", metric: "engine.coolant_temperature", category: "vehicle_health", state: "watch", title: "x" },
      { rule: "something_new", category: "data_quality", state: "warning", title: "Odd b-can thing" },
      { rule: "can_interface_status_probe", category: "can_infrastructure", state: "warning", title: "Interface status probe failed", reason: "interface status probe failed; adapter health cannot be established" },
    ],
    episodes: {
      active: [
        { id: 900, rule: "can_interface_role_b_can", category: "can_infrastructure", status: "open", state: "watch", title: "b-can interface role is unhealthy", opened_at: iso(100000), last_observed_at: iso(40000), latest_assessment: { rule: "can_interface_role_b_can", category: "can_infrastructure", state: "unavailable" } },
      ],
    },
  };
  const result = buildWarningCards(health, NOW);
  assert.equal(result.warnings.length, 0);
  assert.equal(result.systemNotes.length, 6);
  const lines = result.systemNotes.map((c) => c.line);
  assert.ok(lines.includes("CAN-CH interface down — diagnostic reads paused."));
  assert.ok(lines.includes("USB CAN adapters re-enumerated at " + at(iso(7000)) + " (usually self-heals)."));
  assert.ok(lines.includes("Coolant readings missing while running."));
  assert.ok(lines.includes("Odd B-CAN thing."));
  assert.ok(lines.includes("Interface status probe failed — adapter health unknown."));
  assert.ok(lines.includes("Unconfirmed · B-CAN bus unhealthy, last seen " + at(iso(40000))));
  assert.equal(result.counts.system, 6);
  assert.equal(result.counts.open, 0);
  for (const text of everyLine(result)) assert.doesNotMatch(text, FORBIDDEN_WORDS, text);
});

test("backend-supplied action, tier and group are honoured", () => {
  const health = {
    available: true,
    assessments: [
      relative("tire_pressure_rr_relative_low", "tire.pressure.rr", 74.0, "psi", 78, "low", "warning", {
        action: "Top up the rear right before the pass",
        tier: 1,
        group: "tires",
      }),
      relative("tire_drift_rr", "tire.pressure.rr", 76, "psi", 77.4, "low", "warning", { tier: 2, severity: "notice", action: "Likely slow leak." }),
      relative("resting_voltage_trend", "battery.voltage", 12.1, "V", 12.45, "low", "warning", { severity: "info" }),
    ],
  };
  const result = buildWarningCards(health, NOW);
  const rr = result.warnings.find((c) => c.rule === "tire_pressure_rr_relative_low");
  assert.equal(rr.line, "RR tire 74 psi, 4 psi under its usual 78 for 1 min. Top up the rear right before the pass.");
  assert.equal(rr.action, "Top up the rear right before the pass.");
  assert.equal(rr.group, "tires");
  assert.equal(rr.tier, "warning");
  const drift = result.warnings.find((c) => c.rule === "tire_drift_rr");
  assert.equal(drift.tier, "notice");
  assert.equal(drift.line, "RR tire 76 psi, 1 psi under its usual 77 for 1 min. Likely slow leak.");
  assert.equal(result.warnings.find((c) => c.rule === "resting_voltage_trend").tier, "notice");
  assert.deepEqual(result.counts, { critical: 0, warning: 1, notice: 2, system: 0, open: 3, unconfirmed: 0 });
  assert.equal(result.warnings[0].rule, "tire_pressure_rr_relative_low", "warning tier sorts above notices");
});

test("unavailable, empty or malformed health", () => {
  for (const input of [null, undefined, "nope", 42, [], { available: false, reason: "early_warning_disabled" }]) {
    const result = buildWarningCards(input, NOW);
    assert.equal(result.available, false, String(input));
    assert.deepEqual(result.warnings, []);
    assert.deepEqual(result.systemNotes, []);
    assert.deepEqual(result.counts, { critical: 0, warning: 0, notice: 0, system: 0, open: 0, unconfirmed: 0 });
    assert.equal(result.delivery.show, false);
  }
  assert.equal(buildWarningCards({ available: false, reason: "early_warning_disabled" }, NOW).reason, "early_warning_disabled");
  assert.equal(buildWarningCards(null, NOW).reason, "health_unavailable");
  const empty = buildWarningCards({ available: true }, NOW);
  assert.equal(empty.available, true);
  assert.deepEqual(empty.warnings, []);
  assert.equal(empty.delivery.enabled, false);
  // Odd shapes inside a valid envelope are skipped rather than thrown on.
  const odd = buildWarningCards(
    { available: true, assessments: "x", active: [null, 3], episodes: { active: [{ rule: null }, { id: "7", rule: "engine_coolant_temperature_relative_high", status: "open" }] }, data_quality: { active: [{}] }, usb_can_incidents: { active: [{}] } },
    NOW
  );
  assert.equal(odd.warnings.length, 1);
  assert.equal(odd.warnings[0].line, "Unconfirmed · engine off");
  assert.equal(odd.warnings[0].episodeId, null);
  assert.equal(odd.systemNotes.length, 2);
  assert.equal(odd.systemNotes[0].line, "Reading sample filtered — value held until a plausible reading.");
  assert.equal(odd.systemNotes[1].line, "USB CAN adapter changed — waiting for it to come back.");
  // Default "now" is the wall clock.
  assert.equal(buildWarningCards({ available: true }).available, true);
});

test("unconfirmed lines by inconclusive state", () => {
  const ep = episode(1, "engine_coolant_temperature_relative_high", 3600000, { last_observed_at: iso(1800000) });
  const base = { rule: "engine_coolant_temperature_relative_high", metric: "engine.coolant_temperature", category: "vehicle_health" };
  assert.equal(unconfirmedLine(Object.assign({ state: "unavailable", current: { value: 123.8, unit: "°F", observed_at: iso(9000000) } }, base), ep, NOW_DATE).line, "Unconfirmed · engine off since " + at(iso(9000000)));
  assert.equal(unconfirmedLine(Object.assign({ state: "not_applicable" }, base), ep, NOW_DATE).line, "Unconfirmed · engine off since " + at(iso(1800000)));
  assert.equal(unconfirmedLine(Object.assign({ state: "insufficient_history" }, base), ep, NOW_DATE).line, "Unconfirmed · waiting for a fresh reading since " + at(iso(1800000)));
  assert.equal(unconfirmedLine(Object.assign({ state: "rejected" }, base), ep, NOW_DATE).line, "Unconfirmed · waiting for a fresh reading since " + at(iso(1800000)));
  assert.equal(unconfirmedLine(Object.assign({ state: "normal", current: { value: 198.4, unit: "°F", observed_at: iso(60000) } }, base), ep, NOW_DATE).line, "Coolant back to 198 °F at " + at(iso(60000)) + ".");
  assert.equal(unconfirmedLine(null, ep, NOW_DATE).line, "Unconfirmed · engine off since " + at(iso(1800000)));
  assert.equal(unconfirmedLine(null, ep, NOW_DATE).title, "Coolant hot");
});

test("heldSeconds prefers the episode age, then scales the persistence window", () => {
  const a = { persistence: { observed: 3, required: 10, window_seconds: 60 } };
  assert.equal(heldSeconds(a, { opened_at: iso(300000) }, NOW), 300);
  assert.equal(heldSeconds(a, { opened_at: "bad" }, NOW), 18);
  assert.equal(heldSeconds(a, null, NOW), 18);
  assert.equal(heldSeconds({ persistence: { observed: 12, required: 10, window_seconds: 60 } }, null, NOW), 60);
  assert.equal(heldSeconds({ persistence: { observed: 0, required: 10, window_seconds: 60 } }, null, NOW), null);
  assert.equal(heldSeconds({ persistence: { observed: 2, required: 0, window_seconds: 60 } }, null, NOW), null);
  assert.equal(heldSeconds({}, null, NOW), null);
  assert.equal(heldSeconds(null, { opened_at: iso(-5000) }, NOW), null, "an episode from the future is ignored");
  // A short hold leaves the duration clause out.
  const line = warningLine(relative("engine_coolant_temperature_relative_high", "engine.coolant_temperature", 214, "°F", 190, "high", "watch", { persistence: { observed: 0, required: 10, window_seconds: 60 } }), null, NOW).line;
  assert.equal(line, "Coolant 214 °F, 24 °F over its usual 190. Ease off and watch the gauge.");
  // No baseline (training) still yields a sentence.
  const noBaseline = warningLine({ rule: "engine_coolant_temperature_relative_high", metric: "engine.coolant_temperature", state: "watch", current: { value: 214, unit: "°F" }, persistence: { observed: 4, required: 10, window_seconds: 60 } }, null, NOW).line;
  assert.equal(noBaseline, "Coolant 214 °F for 24 s. Ease off and watch the gauge.");
  // No current value at all.
  const bare = warningLine({ rule: "tire_pressure_fl_relative_low", state: "watch" }, null, NOW).line;
  assert.equal(bare, "FL tire. Check pressure at the next stop.");
});

test("facts: parked battery, custom below, missing unit fallback", () => {
  const parked = warningLine(relative("battery_voltage_relative_low", "battery.voltage", 11.6, "V", 12.45, "low", "warning", { regime: "engine_off:stationary:rpm_off:cold" }), null, NOW);
  assert.equal(parked.line, "Battery 11.60 V, 0.85 V under its usual 12.45 for 1 min. Start the engine or charge it soon.");
  assert.equal(parked.title, "Battery low");
  const facts = warningFacts({ rule: "custom_x", custom_rule: { operator: "below", threshold: 11.8, metric: "battery.voltage" }, current: { value: 11.5 }, state: "warning" }, null, NOW);
  assert.equal(facts.metric, "battery.voltage");
  assert.equal(facts.unit, "V", "unit falls back to the metric's display unit");
  assert.equal(facts.direction, "low");
  const custom = warningLine({ rule: "custom_x", custom_rule: { operator: "below", threshold: 11.8, metric: "battery.voltage" }, current: { value: 11.5 }, state: "warning" }, null, NOW);
  assert.equal(custom.line, "Battery 11.50 V, under your 11.80 V limit. Check it at the next stop.");
  assert.equal(custom.title, "Battery low");
  const alt = warningLine(relative("battery_voltage_relative_low", "battery.voltage", 12.9, "V", 14.0, "low", "warning", { corroborators: [{ metric: "generator.field_duty", state: "unavailable" }] }), null, NOW);
  assert.equal(alt.line, "Battery 12.90 V, 1.10 V under its usual 14.00 for 1 min. Limit accessories and check charging.");
});

test("helpers: labels, wheels, routing, tiers, templates", () => {
  assert.equal(metricLabel("tire.pressure.rl"), "RL tire");
  assert.equal(metricLabel("engine.vvt_oil_temperature"), "Oil temperature");
  assert.equal(metricLabel("diagnostics.cluster.did.1000.raw"), "Raw");
  assert.equal(metricLabel("some.new_metric"), "New metric");
  assert.equal(metricLabel(null), "Reading");
  assert.equal(wheelLabel("tire_pressure_fr_relative_low"), "FR");
  assert.equal(wheelLabel("tire.pressure.rr"), "RR");
  assert.equal(wheelLabel("engine.rpm"), null);
  assert.equal(wheelLabel(undefined), null);
  assert.equal(isSystemItem({ category: "can_infrastructure" }), true);
  assert.equal(isSystemItem({ category: "vehicle_health", rule: "usb_can_transient_disconnect" }), true);
  assert.equal(isSystemItem({ category: "vehicle_health", rule: "telemetry_gap_x" }), true);
  assert.equal(isSystemItem({ category: "vehicle_health", rule: "custom_1" }), false);
  assert.equal(isSystemItem({ rule: "tire_pressure_rr_relative_low" }), false);
  assert.equal(isSystemItem(null), false);
  assert.equal(tierOf({ severity: "critical" }), "critical");
  assert.equal(tierOf({ severity: "warning" }), "warning");
  assert.equal(tierOf({ severity: "info" }), "notice");
  assert.equal(tierOf({ severity: "warning", tier: 2 }), "notice");
  assert.equal(tierOf({ severity: "warning", tier: 3 }), "system");
  assert.equal(tierOf({ category: "telemetry_quality", severity: "critical" }), "system");
  assert.equal(tierOf(null), "warning");
  assert.equal(templateFor("engine_oil_pressure_absolute_critical"), TEMPLATES.engine_oil_pressure_);
  assert.equal(templateFor("tire_pressure_fl_relative_low"), TEMPLATES.tire_pressure_);
  assert.equal(templateFor("custom_9f"), TEMPLATES.custom_);
  assert.equal(templateFor("brand_new_rule"), TEMPLATES.default);
  assert.equal(templateFor(null), TEMPLATES.default);
  assert.deepEqual(Object.keys(TEMPLATES).sort(), ["battery_voltage_", "custom_", "default", "engine_coolant_", "engine_oil_pressure_", "tire_pressure_", "transmission_oil_"]);
  assert.equal(systemTitle("c-can interface role is unhealthy"), "C-CAN bus unhealthy");
  assert.equal(systemTitle(null), "System");
  assert.equal(systemLine({ rule: "can_interface_role_c_can", current: { reason: "controller_not_error_active" } }, "4:00 pm"), "C-CAN controller error since 4:00 pm — engine data paused.");
  assert.equal(systemLine({ rule: "can_interface_role_c_can", current: { resolution: "ambiguous", reason: "ambiguous" } }, null), "C-CAN adapter identity ambiguous — engine data paused.");
  assert.equal(systemLine({ rule: "can_interface_role_b_can", current: { reason: "config_mismatch" } }, null), "B-CAN interface configuration mismatch.");
  assert.equal(systemLine({ rule: "can_interface_role_spare", reason: "awaiting initial interface discovery" }, null), "Spare CAN status not established yet.");
  assert.equal(systemLine({ rule: "can_interface_role_c_can", reason: "something else" }, null), "C-CAN bus check failed.");
  assert.equal(systemLine({ rule: "can_interface_status_probe", reason: "first interface status probe has not completed within 30 seconds" }, "4:00 pm"), "Interface discovery delayed since 4:00 pm.");
});

// Pair rules show the broker's pair sentence, not one wheel's reading
// ("RR tire 75 psi. Check both rear tires..." before 2026-09-26).
test("pair warning sentence uses the broker's lower-wheel comparison", () => {
  const a = {
    rule: "tire_pressure_pair_asymmetry", metric: "tire.pressure.rl", state: "warning", severity: "warning",
    tier: 0, group: "tires", title: "Rear tires uneven", action: "Check both rear tires at the next stop.",
    absolute_threshold: { operator: "above", value: 3, unit: "psi" },
    current: { value: 76.0, unit: "psi" },
    pair: { left: "rl", right: "rr", summary: "RL 76.0 psi is 3.6 under RR 79.6 (usually 0.8 over)" },
  };
  const out = warningLine(a, null, Date.now());
  assert.equal(out.line, "RL 76.0 psi is 3.6 under RR 79.6 (usually 0.8 over). Check both rear tires at the next stop.");
  assert.equal(out.title, "Rear tires uneven");
});

test("relative comparison: the word follows the numbers on screen, not the rule direction", () => {
  // Event 645: a low-direction rule whose reading (75.6) sat above its median (75.4) read
  // "76 psi, 1 psi under its usual 75".
  const above = warningLine(relative("tire_pressure_rr_relative_low", "tire.pressure.rr", 75.6, "psi", 75.4, "low", "warning"), null, NOW);
  assert.equal(above.line, "RR tire 76 psi, 1 psi over its usual 75 for 1 min. Check pressure at the next stop.");
  const below = warningLine(relative("engine_coolant_temperature_relative_high", "engine.coolant_temperature", 188.2, "°F", 190.4, "high", "watch"), null, NOW);
  assert.equal(below.line, "Coolant 188 °F, 2 °F under its usual 190 for 12 s. Ease off and watch the gauge.");
  // Both numbers round to the same value: no "0 psi under".
  const level = warningLine(relative("tire_pressure_rr_relative_low", "tire.pressure.rr", 78.2, "psi", 78.4, "low", "warning"), null, NOW);
  assert.equal(level.line, "RR tire 78 psi, at its usual 78 for 1 min. Check pressure at the next stop.");
  const volts = warningLine(relative("battery_voltage_relative_low", "battery.voltage", 14.2, "V", 14.0, "low", "warning"), null, NOW);
  assert.equal(volts.line, "Battery 14.20 V, 0.20 V over its usual 14.00 for 1 min. Limit accessories and check charging.");
});

test("durations inside a sentence carry every unit", () => {
  assert.equal(fmtDurationLong(56 * 3600 + 42 * 60), "2 d 8 h", "event 645 read 'for 56 h 42'");
  const a = relative("tire_pressure_rr_relative_low", "tire.pressure.rr", 74.0, "psi", 78, "low", "warning");
  const hour = warningLine(a, episode(1, a.rule, 65 * 60000), NOW).line;
  assert.equal(hour, "RR tire 74 psi, 4 psi under its usual 78 for 1 h 05 min. Check pressure at the next stop.");
  const days = warningLine(a, episode(1, a.rule, (56 * 60 + 42) * 60000), NOW).line;
  assert.equal(days, "RR tire 74 psi, 4 psi under its usual 78 for 2 d 8 h. Check pressure at the next stop.");
});

// Event 645 as the broker served it on 2026-09-27 (evidence arrays trimmed).
function event645() {
  return {
    rule: "tire_pressure_rr_slow_leak",
    metric: "tire.pressure.rr",
    category: "vehicle_health",
    severity: "notice",
    tier: 2,
    group: "tires",
    direction: "low",
    state: "warning",
    title: "RR tire slow leak",
    action: "Check the RR tire for a slow leak.",
    reason: "RR cold pressure 2.9 psi a week below RL's over 4 cold starts",
    regime: "cold_start",
    current: { metric: "tire.pressure.rr", value: 75.6, unit: "psi", observed_at: "2026-09-24T21:07:09.263704+00:00" },
    baseline: { median: 75.4, unit: "psi", bucket_count: 74, trip_count: 8, window_days: 30 },
    deviation: { signed_from_median: -0.1, effect_in_rule_direction: 0.1, threshold: 3.0 },
    persistence: { basis: "cold starts", evaluated: true, observed: 7, required: 4, satisfied: true },
    drift: { method: "cold_start_trend_axle_relative", mate: "rl", relative_slope_psi_per_week: -2.8991, usual_offset_psi: -1.2 },
    evaluated_at: "2026-09-26T22:42:57.727387+00:00",
  };
}

test("slow-drift notices say what the broker found, not value against baseline (event 645)", () => {
  const now = Date.parse("2026-09-28T04:48:00Z");
  const notice = event645();
  const ep = { id: 645, rule: notice.rule, status: "open", state: "warning", opened_at: "2026-09-25T20:06:04.774734+00:00", acknowledged: false, category: "vehicle_health" };
  const expected = "RR cold pressure 2.9 psi a week below RL's over 4 cold starts. Check the RR tire for a slow leak.";

  const built = warningLine(notice, ep, now);
  assert.equal(built.line, expected);
  assert.equal(built.title, "RR tire slow leak");
  assert.equal(built.action, "Check the RR tire for a slow leak.");

  const result = buildWarningCards({ available: true, assessments: [notice], active: [notice], episodes: { active: [ep] } }, now);
  assert.equal(result.warnings.length, 1);
  const card = result.warnings[0];
  assert.equal(card.line, expected);
  assert.equal(card.title, "RR tire slow leak");
  assert.equal(card.tier, "notice");
  assert.equal(card.episodeId, 645);
  assert.ok(card.sinceLabel, "the card still says since when");
  assert.doesNotMatch(card.line, /its usual|56 h/);
  assert.doesNotMatch(card.line, FORBIDDEN_WORDS);

  // Other drift findings start lower-case in the broker; a held notice has no numbers at all.
  const creep = warningLine({ rule: "engine_coolant_temperature_idle_creep", metric: "engine.coolant_temperature", tier: 2, severity: "notice", state: "warning", title: "Idle coolant creeping up", reason: "warm idle coolant +5.4 °F against the prior 10 trips", action: "Check the coolant level and the fan." }, null, now);
  assert.equal(creep.line, "Warm idle coolant +5.4 °F against the prior 10 trips. Check the coolant level and the fan.");
  assert.equal(creep.title, "Idle coolant creeping up");
  const held = Object.assign(event645(), {
    reason: "not enough paired cold starts (2 of 4 in 30 days; 0 cached readings skipped); the notice stays open until it can be re-checked",
    current: { metric: "tire.pressure.rr", value: null, unit: "psi", observed_at: null, source: null },
    baseline: { median: null, unit: "psi" },
    deviation: { signed_from_median: null, effect_in_rule_direction: null, threshold: null },
  });
  assert.equal(
    warningLine(held, ep, now).line,
    "Not enough paired cold starts (2 of 4 in 30 days; 0 cached readings skipped); the notice stays open until it can be re-checked. Check the RR tire for a slow leak."
  );

  // A reason carrying evaluator vocabulary, or no reason, falls back to the template.
  const jargon = warningLine(Object.assign(event645(), { reason: "deviation persisted in the cold regime" }), null, now);
  assert.equal(jargon.line, "RR tire 76 psi, 1 psi over its usual 75. Check the RR tire for a slow leak.");
  assert.equal(jargon.title, "RR tire low");
  const silent = warningLine(Object.assign(event645(), { reason: null }), null, now);
  assert.equal(silent.line, jargon.line);
  // Tier 0/1 keep the template even when the broker sends a reason.
  const tierOne = warningLine(relative("tire_pressure_rr_relative_low", "tire.pressure.rr", 74.0, "psi", 78, "low", "warning", { tier: 1, reason: "stayed below its usual pressure for this trip phase long enough to confirm" }), null, NOW);
  assert.equal(tierOne.line, "RR tire 74 psi, 4 psi under its usual 78 for 1 min. Check pressure at the next stop.");
});
