// Synthetic /v1/events payloads for README screenshots of the Saved Events list and one event's
// detail. Every value is illustrative: titles and rule keys are the app's own, the numbers agree
// with the Drive scene in demo_data.mjs (RR 79 psi, RL 80 psi), and nothing was recorded.
// Routes use the capture harness contract: {method, pathname, prefix, status, json}, first match wins.
import {DAY_MS, HOUR_MS, MINUTE_MS, iso} from "./demo_data.mjs";

/** The open event the detail and Ask Codex screenshots show. */
export const DEMO_OPEN_EVENT_ID = 614;
export const DEMO_OPEN_EVENT_TITLE = "Rear-right tire pressure drifting below comparable history";
export const DEMO_OPEN_EVENT_RULE = "tire_pressure_rr_relative_low";

// RR baseline: the opening boundary is median - mad_multiplier * robust_sigma = 82 - 2.67 psi.
export const DEMO_RR_BASELINE = Object.freeze({
  median: 82, robustSigma: 0.59304, madMultiplier: 4.5, minimumEffect: 1.5, bucketCount: 341, tripCount: 13,
});
const THRESHOLD = DEMO_RR_BASELINE.madMultiplier * DEMO_RR_BASELINE.robustSigma;

// The 20 saved samples around the opening (index 9 is the third reading below the boundary).
const SAMPLE_PSI = Object.freeze([80.0, 80.0, 79.9, 79.9, 79.8, 79.6, 79.5, 79.3, 79.2, 79.0,
  79.1, 79.0, 79.1, 79.0, 79.2, 79.0, 79.1, 79.0, 79.2, 79.1]);
const SAMPLE_SPACING_MS = 14_000;
const OPENING_SAMPLE_INDEX = 9;
/** The event opens this long before capture time, 8 minutes into the demo drive. */
export const OPENED_MINUTES_AGO = 34;

const TITLES = Object.freeze({
  rr: DEMO_OPEN_EVENT_TITLE,
  rl: "Rear-left tire pressure drifting below comparable history",
  fr: "Front-right tire pressure drifting below comparable history",
  fl: "Front-left tire pressure drifting below comparable history",
  rearUneven: "Rear tires uneven",
  battery: "Battery voltage below its comparable-history band",
  coolant: "Coolant temperature above its comparable-history band",
  trans: "Transmission oil temperature above its comparable-history band",
  oil: "Oil pressure below its comparable-history band",
  oilGap: "Oil pressure readings missing while running",
  coolantGap: "Coolant temperature readings missing while running",
  transGap: "Transmission temperature readings missing while running",
  inhibit: "CAN restoration inhibit is active",
  cCan: "c-can interface configuration mismatch",
  bCan: "b-can interface configuration mismatch",
  canCh: "can-ch interface configuration mismatch",
});

const RULES = Object.freeze({
  rr: DEMO_OPEN_EVENT_RULE, rl: "tire_pressure_rl_relative_low", fr: "tire_pressure_fr_relative_low",
  fl: "tire_pressure_fl_relative_low", rearUneven: "tire_pair_rear_asymmetry",
  battery: "battery_voltage_relative_low", coolant: "engine_coolant_temperature_relative_high",
  trans: "transmission_oil_temperature_relative_high", oil: "engine_oil_pressure_relative_low",
  oilGap: "telemetry_gap_engine_oil_pressure", coolantGap: "telemetry_gap_engine_coolant_temperature",
  transGap: "telemetry_gap_transmission_oil_temperature", inhibit: "can_restoration_inhibit",
  cCan: "can_interface_role_c_can", bCan: "can_interface_role_b_can", canCh: "can_interface_role_can_ch",
});

const CLOSED = Object.freeze({
  recovered: {state: "normal", evidence: "retained", reason: "recovered"},
  suppressed: {state: "suppressed", evidence: "suppressed", reason: "parked_closed"},
  unconfirmed: {state: "normal", evidence: "retained", reason: "unconfirmed_monitoring_ended"},
});

// [id, kind, daysAgo opened, hours open, how it closed]; newest first, ~5 weeks, 614 is the only open one.
const HISTORY = Object.freeze([
  [611, "coolant", 2.1, 0.4, "recovered"],
  [609, "cCan", 3.3, 5, "suppressed"],
  [607, "rearUneven", 4.2, 1.6, "recovered"],
  [604, "battery", 6.0, 0.9, "recovered"],
  [602, "inhibit", 7.4, 3, "suppressed"],
  [600, "trans", 9.2, 0.7, "recovered"],
  [598, "rl", 10.5, 2.2, "recovered"],
  [596, "oilGap", 11.3, 0.5, "suppressed"],
  [593, "coolant", 12.6, 0.3, "recovered"],
  [591, "bCan", 13.8, 4, "suppressed"],
  [589, "rr", 17.2, 6.5, "recovered"],
  [587, "battery", 19.4, 1.1, "recovered"],
  [585, "coolantGap", 21.0, 0.6, "suppressed"],
  [583, "fr", 22.5, 3.4, "recovered"],
  [581, "rearUneven", 24.1, 2.0, "recovered"],
  [578, "oil", 25.8, 0.4, "recovered"],
  [576, "canCh", 26.9, 2.5, "suppressed"],
  [573, "trans", 28.3, 1.2, "recovered"],
  [571, "coolant", 30.2, 0.5, "recovered"],
  [569, "battery", 31.6, 0.3, "unconfirmed"],
  [566, "fl", 33.1, 4.8, "recovered"],
  [564, "transGap", 35.0, 0.8, "suppressed"],
]);

function openSummary(nowMs) {
  return {
    id: DEMO_OPEN_EVENT_ID, rule_key: RULES.rr, title: TITLES.rr, status: "open", current_state: "warning",
    evidence_state: "retained", opened_at: iso(nowMs - OPENED_MINUTES_AGO * MINUTE_MS), resolved_at: null,
    resolution_reason: null, outcome: "unresolved",
  };
}

function closedSummary(nowMs, [id, kind, daysAgo, hoursOpen, how]) {
  const opened = nowMs - daysAgo * DAY_MS;
  const closed = CLOSED[how];
  return {
    id, rule_key: RULES[kind], title: TITLES[kind], status: "resolved", current_state: closed.state,
    evidence_state: closed.evidence, opened_at: iso(opened), resolved_at: iso(opened + hoursOpen * HOUR_MS),
    resolution_reason: closed.reason, outcome: how === "unconfirmed" ? "unconfirmed" : "closed",
  };
}

const retentionText = "Compact episode/event evidence has no automatic age deletion. Content-addressed baseline input "
  + "archives are retained with decisions. Raw metric/interface samples default to seven days with "
  + "rollup-before-prune. Per-event windows cover two minutes before/three after opening (256 primary "
  + "samples max), globally budgeted to 128 MiB; pruned windows leave explicit tombstones. Export pages "
  + "and event JSON for independent backups.";

const GUIDE = Object.freeze({
  version: 3,
  baseline: "Relative rules compare a value to the median of comparable prior-trip minute medians. The "
    + "boundary is the larger of the minimum effect and the MAD multiplier times the robust spread. A "
    + "relative band is a monitoring reference, not an OEM limit.",
  lifecycle: "Acknowledged and dismissed are owner dispositions, not mechanical recovery. Confirmed warnings "
    + "retain unresolved history during missing or inapplicable evidence.",
  retention: retentionText,
});

const BASELINE_DIGEST = "5e1c0d9a47b3f862a0c4d7e19b8f3a2564c7d0e1b9a8f7c6d5e4b3a291807f6e";
const OPENING_EVIDENCE_REF = "event:7301";

function baselineAt(nowMs) {
  const b = DEMO_RR_BASELINE;
  return {
    median: b.median, mad: 0.4, robust_sigma: b.robustSigma, unit: "psi", bucket_count: b.bucketCount,
    trip_count: b.tripCount, first_at: iso(nowMs - 29 * DAY_MS), last_at: iso(nowMs - 20 * HOUR_MS),
    regime: "motion=highway", metric: "tire.pressure.rr", source: "rf_hub.did.31d2", quality: "verified",
    input_digest: BASELINE_DIGEST,
  };
}

function assessmentAt({state, reason, value, atMs, observed, ageSeconds = 2}) {
  const signed = Math.round((value - DEMO_RR_BASELINE.median) * 100) / 100;
  return {
    rule: RULES.rr, metric: "tire.pressure.rr", title: TITLES.rr, category: "vehicle_health", severity: "warning",
    direction: "low", state, reason, regime: "engine_running:highway:rpm_mid:warm", baseline_regime: "motion=highway",
    evaluator_revision: "relative-v2", rule_revision: "demo-rule-revision",
    current: {value, unit: "psi", observed_at: iso(atMs - ageSeconds * 1000), captured_at: iso(atMs - ageSeconds * 1000 + 300),
      source: "rf_hub.did.31d2", quality: "verified", sample_id: 4400000 + Math.round(value * 10), effective_age_seconds: ageSeconds},
    baseline: baselineAt(atMs),
    deviation: {signed_from_median: signed, threshold: THRESHOLD, mad_multiplier: DEMO_RR_BASELINE.madMultiplier,
      minimum_effect: DEMO_RR_BASELINE.minimumEffect, effect_in_rule_direction: -signed},
    persistence: {evaluated: true, observed, required: 3, window_seconds: 180, satisfied: observed >= 3},
    evaluated_at: iso(atMs),
  };
}

const STAYED = "stayed outside its usual range long enough to confirm";

function sampleWindow(openedMs) {
  const start = openedMs - OPENING_SAMPLE_INDEX * SAMPLE_SPACING_MS;
  return SAMPLE_PSI.map((value, i) => ({
    observed_at: iso(start + i * SAMPLE_SPACING_MS), value, unit: "psi", source: "rf_hub.did.31d2",
    evidence_ref: "sample:" + (4400000 + i),
  }));
}

function timelineAt(openedMs) {
  const opening = assessmentAt({state: "warning", reason: STAYED, value: 79.0, atMs: openedMs, observed: 3});
  const paused = assessmentAt({state: "unavailable", reason: "latest observation is undated or older than the rule permits",
    value: 79.1, atMs: openedMs + 14 * MINUTE_MS, observed: 3, ageSeconds: 41});
  const restored = assessmentAt({state: "warning", reason: STAYED, value: 79.0, atMs: openedMs + 15.5 * MINUTE_MS, observed: 3});
  return [
    {id: 7303, at: iso(openedMs + 15.5 * MINUTE_MS), type: "evidence_restored", previous_state: "unavailable", new_state: "warning",
      assessment: restored, evidence_ref: "event:7303", presentation: "monitoring_note", monitoring_note: null},
    {id: 7302, at: iso(openedMs + 14 * MINUTE_MS), type: "evidence_inconclusive", previous_state: "warning", new_state: "unavailable",
      assessment: paused, evidence_ref: "event:7302", presentation: "monitoring_note", monitoring_note: null},
    {id: 7301, at: iso(openedMs), type: "opened", previous_state: null, new_state: "warning",
      assessment: opening, evidence_ref: OPENING_EVIDENCE_REF, presentation: "event", monitoring_note: null},
  ];
}

function recurrencesAt(nowMs) {
  const rr = [openSummary(nowMs), ...HISTORY.filter((row) => row[1] === "rr").map((row) => closedSummary(nowMs, row))];
  return {
    events: rr.map(({id, opened_at, status}) => ({id, opened_at, status})),
    truncated: false,
    interpretation: "Event openings, not continuous fault duration. Different operating regimes and rule revisions may occur.",
    last_30_days: {
      start_at: iso(nowMs - 30 * DAY_MS), end_at: iso(nowMs), episode_openings: rr.length, recorded_trips: 31,
      recorded_trip_hours: 24.5, openings_per_recorded_trip: Math.round(rr.length / 31 * 1000) / 1000,
    },
  };
}

/** The saved-event packet for the open RR event, shaped like event_history.detail(). */
export function demoEventDetail(nowMs) {
  const openedMs = nowMs - OPENED_MINUTES_AGO * MINUTE_MS;
  const first = assessmentAt({state: "warning", reason: STAYED, value: 79.0, atMs: openedMs, observed: 3});
  const latest = assessmentAt({state: "warning", reason: STAYED, value: 79.0, atMs: nowMs - 3000, observed: 3});
  const peak = {assessment: first, evidence_ref: "event:7301:peak"};
  const event = {
    ...openSummary(nowMs), rule: RULES.rr, evidence_ref: "episode:" + DEMO_OPEN_EVENT_ID,
    revision: "demo-revision", first_assessment: first, latest_assessment: latest, last_evaluated_at: iso(nowMs - 3000),
    monitoring_note: null, acknowledged: false,
    first_warning: {at: iso(openedMs), assessment: first, evidence_ref: OPENING_EVIDENCE_REF},
    evidence: {last_evaluable: {assessment: latest, evidence_ref: "event:7301:latest", coverage: "complete"},
      peak_value: peak, peak_deviation: peak},
    timeline: timelineAt(openedMs), next_event_before: null,
    sample_window: sampleWindow(openedMs),
    completeness: {opening: "retained", first_warning: "retained", checkpoints: "retained", sample_window: "complete",
      timeline: "page_complete", rule_revision: "retained"},
    duration: {elapsed_open_seconds: OPENED_MINUTES_AGO * 60, observed_abnormal_seconds: 1980, unobserved_seconds: 0,
      evaluator_gap_seconds: 0, coverage_since: iso(openedMs), legacy_prefix_unknown: false, since_last_evaluation_seconds: 3},
    notifications: [{id: 1, event_id: 7301, status: "delivered", created_at: iso(openedMs + 1000),
      delivered_at: iso(openedMs + 2000), attempt_count: 1, last_error: null}],
    annotations: [],
    related: {trip: {id: 71, started_at: iso(nowMs - 42 * MINUTE_MS)}, data_quality: [],
      meaning: "Temporal association only; not evidence of causation. DTC and maintenance links can be recorded in owner notes."},
    recurrences: recurrencesAt(nowMs),
    baseline_archives: [{digest: BASELINE_DIGEST, status: "retained", bucket_count: DEMO_RR_BASELINE.bucketCount,
      access: "Included in browser event export and offline event backup; bounded preview is in each assessment"}],
  };
  return {available: true, schema_version: 3, event, system_guide: GUIDE, generated_at: iso(nowMs)};
}

/** The Saved Events list: 23 events, newest first, one page (no next_before). */
export function demoEventList(nowMs) {
  const events = [openSummary(nowMs), ...HISTORY.map((row) => closedSummary(nowMs, row))];
  return {available: true, schema_version: 3, events, next_before: null, system_guide: GUIDE};
}

const notRetained = {available: false, reason: "not_found", detail: "Event is not retained in this demo"};

/** Harness routes for the events dialog; everything under /v1/events is answered here. */
export function demoEventRoutes(nowMs) {
  return [
    {method: "GET", pathname: "/v1/events/" + DEMO_OPEN_EVENT_ID, prefix: false, status: 200, json: demoEventDetail(nowMs)},
    // Any other event, annotations, replay or baselines read gets a local refusal, never the real broker.
    {method: "GET", pathname: "/v1/events/", prefix: true, status: 404, json: notRetained},
    {method: "GET", pathname: "/v1/events", prefix: false, status: 200, json: demoEventList(nowMs)},
  ];
}
