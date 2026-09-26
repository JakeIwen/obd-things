/**
 * Warning cards for the Health view and alert strip (design.md section 7.1,
 * warnings-audit.md section E).
 *
 * `buildWarningCards()` turns the `health` slice of `/v2/summary` (the
 * `web_v2.health_lite` shape: `assessments`, `active`, `episodes.active`,
 * `data_quality`, `usb_can_incidents`, `notification_delivery`) into plain
 * English rows:
 *
 *   `<Metric> <value> <unit>, <comparison> for <duration>. <Action>.`
 *
 * Vehicle-health and owner (`custom_*`) rules become warnings; interface,
 * USB, coverage and data-quality items become system notes. An open episode
 * whose latest assessment is inconclusive renders as
 * `Unconfirmed · engine off since 4:25 pm` and never repeats the evaluator's
 * vocabulary (regime, MAD, deviation, persistence counts, episode numbers).
 *
 * When the redesigned backend adds `tier`, `group` and `action` fields
 * (docs/warnings-redesign.md) they are honoured; otherwise the templates here
 * decide. Wall-clock times are display-only. Not on the 1 Hz path: this runs
 * only when the summary's health slice changes.
 */

import { fmtValue, fmtUnit, fmtFixed, fmtTime, fmtDuration, fmtInt, decimalsFor, parseDate } from "./format.js";

/** Card tiers in display order. */
export const TIERS = ["critical", "warning", "notice", "system"];

/** Assessment states that count as an open finding. */
export const OPEN_STATES = new Set(["watch", "warning"]);

/** Assessment states in which an open episode is unconfirmed right now. */
export const INCONCLUSIVE_STATES = new Set([
  "unavailable",
  "not_applicable",
  "insufficient_history",
  "rejected",
  "suppressed",
]);

/** Categories routed to system notes rather than warnings. */
export const SYSTEM_CATEGORIES = new Set(["can_infrastructure", "telemetry_quality", "data_quality", "system"]);

/** Rule-id prefixes routed to system notes whatever their category. */
export const SYSTEM_RULE_PREFIXES = ["can_", "usb_", "telemetry"];

/** Words that must never reach a card line (checked by tests). */
export const FORBIDDEN_WORDS = /\b(regime|mad|deviation|persisten\w*|episode|advisory|unavailable)\b/i;

/** Plain-English subject per metric. */
export const METRIC_LABELS = Object.freeze({
  "engine.oil_pressure": "Oil pressure",
  "engine.coolant_temperature": "Coolant",
  "transmission.oil_temperature": "Transmission oil",
  "engine.vvt_oil_temperature": "Oil temperature",
  "battery.voltage": "Battery",
  "generator.field_duty": "Alternator",
  "engine.rpm": "Engine speed",
  "vehicle.speed": "Speed",
  "vehicle.odometer": "Odometer",
  "engine.crankshaft_power": "Power",
  "engine.crankshaft_torque": "Torque",
  "engine.target_crankshaft_torque": "Torque request",
  "transmission.output_speed": "Output shaft",
  "transmission.turbine_speed": "Turbine",
  "tire.pressure.fl": "FL tire",
  "tire.pressure.fr": "FR tire",
  "tire.pressure.rl": "RL tire",
  "tire.pressure.rr": "RR tire",
});

const WHEELS = Object.freeze({ fl: "FL", fr: "FR", rl: "RL", rr: "RR" });

const ROLE_NAMES = Object.freeze({ "c-can": "C-CAN", "b-can": "B-CAN", "can-ch": "CAN-CH", spare: "Spare CAN" });

const ROLE_PAUSED = Object.freeze({
  "c-can": "engine data paused",
  "b-can": "body data paused",
  "can-ch": "diagnostic reads paused",
  spare: "spare adapter idle",
});

/**
 * Subject label for a metric: `RR tire`, `Coolant`, or a humanised fallback
 * built from the last path segment (`vvt_oil_temperature` -> `Vvt oil temperature`).
 * @param {string|null|undefined} name metric name
 * @returns {string}
 */
export function metricLabel(name) {
  if (typeof name !== "string" || name.length === 0) return "Reading";
  const known = METRIC_LABELS[name];
  if (known) return known;
  const tail = name.slice(name.lastIndexOf(".") + 1).replace(/_/g, " ");
  return tail.charAt(0).toUpperCase() + tail.slice(1);
}

/**
 * Wheel code from a tire rule or metric name (`tire_pressure_rr_relative_low`
 * or `tire.pressure.rr` -> `RR`), or `null`.
 * @param {string|null|undefined} ruleOrMetric
 * @returns {string|null}
 */
export function wheelLabel(ruleOrMetric) {
  if (typeof ruleOrMetric !== "string") return null;
  const m = /(?:^|[._])tire[._]pressure[._]([fr][lr])(?:$|[._])/.exec(ruleOrMetric);
  return m ? WHEELS[m[1]] || null : null;
}

/**
 * Whether an assessment, episode or incident belongs on the System notes
 * card rather than the warning list.
 * @param {{category?: string, rule?: string}|null|undefined} item
 * @returns {boolean}
 */
export function isSystemItem(item) {
  if (!item || typeof item !== "object") return false;
  if (typeof item.category === "string" && SYSTEM_CATEGORIES.has(item.category)) return true;
  const rule = typeof item.rule === "string" ? item.rule : "";
  for (let i = 0; i < SYSTEM_RULE_PREFIXES.length; i += 1) {
    if (rule.indexOf(SYSTEM_RULE_PREFIXES[i]) === 0) return true;
  }
  return false;
}

/**
 * Card tier for an item: `system` for system items; otherwise the backend's
 * numeric `tier` (2 -> notice) or `severity` (`critical`, `notice`/`info` ->
 * notice, anything else -> warning).
 * @param {{category?: string, rule?: string, tier?: number|string, severity?: string}|null|undefined} item
 * @returns {'critical'|'warning'|'notice'|'system'}
 */
export function tierOf(item) {
  if (isSystemItem(item)) return "system";
  const severity = item && typeof item.severity === "string" ? item.severity : "";
  const tier = item ? item.tier : undefined;
  if (tier === 3 || tier === "system") return "system";
  if (tier === 2 || tier === "notice") return "notice";
  if (severity === "critical") return "critical";
  if (severity === "notice" || severity === "info") return "notice";
  return "warning";
}

function num(v) {
  return typeof v === "number" && isFinite(v) ? v : null;
}

function roundTo(value, decimals) {
  const scale = Math.pow(10, decimals);
  return Math.round(value * scale) / scale;
}

function str(v) {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function obj(v) {
  return v && typeof v === "object" && !Array.isArray(v) ? v : null;
}

function list(v) {
  if (!Array.isArray(v)) return [];
  const out = [];
  for (let i = 0; i < v.length; i += 1) {
    const item = obj(v[i]);
    if (item && item.omitted_items === undefined) out.push(item);
  }
  return out;
}

function hasPrefix(rule, prefix) {
  return typeof rule === "string" && rule.indexOf(prefix) === 0;
}

function timeLabel(iso, nowDate) {
  const d = parseDate(iso);
  return d === null ? null : fmtTime(d, nowDate);
}

function thresholdText(f) {
  const t = f.threshold;
  if (!t || t.value === null) return null;
  return fmtFixed(t.value, decimalsFor(f.metric), false) + (f.unit ? " " + f.unit : "");
}

function relativeComparison(f) {
  if (f.delta === null || f.median === null) return null;
  const unit = f.unit ? " " + f.unit : "";
  const word = f.direction === "high" ? "over" : "under";
  return f.deltaText + unit + " " + word + " its usual " + f.medianText;
}

function customComparison(f) {
  const t = thresholdText(f);
  if (t === null) return null;
  const op = f.threshold.operator;
  return (op === "below" ? "under" : "over") + " your " + t + " limit";
}

/** Same-axle tire asymmetry rules (`tire_pressure_pair_asymmetry`). */
export const PAIR_RULE = /^tire_pressure_pair_/;


/**
 * Sentence templates keyed by rule prefix. The longest matching prefix wins;
 * `default` covers unknown rules. Each template gives the subject, an
 * optional qualifier written before the comma, the comparison, and the
 * closing imperative. All take the facts object built by `warningFacts()`.
 * @type {Object<string, {subject: function(Object): string, qualifier?: function(Object): (string|null), comparison: function(Object): (string|null), action: function(Object): string, title: function(Object): string}>}
 */
export const TEMPLATES = Object.freeze({
  engine_oil_pressure_: {
    subject: function () {
      return "Oil pressure";
    },
    qualifier: function (f) {
      return f.absolute && f.rpm !== null ? "at " + fmtInt(f.rpm) + " rpm" : null;
    },
    comparison: function (f) {
      if (f.absolute) {
        const t = thresholdText(f);
        return t === null ? "below the operating minimum" : "below the " + t + " minimum";
      }
      return relativeComparison(f);
    },
    action: function (f) {
      return f.absolute || f.severity === "critical"
        ? "Stop the engine when safe."
        : "Check the oil level at the next stop.";
    },
    title: function (f) {
      return f.absolute || f.severity === "critical" ? "Oil pressure critical" : "Oil pressure low";
    },
  },
  engine_coolant_: {
    subject: function () {
      return "Coolant";
    },
    comparison: relativeComparison,
    action: function () {
      return "Ease off and watch the gauge.";
    },
    title: function () {
      return "Coolant hot";
    },
  },
  transmission_oil_: {
    subject: function () {
      return "Transmission oil";
    },
    comparison: relativeComparison,
    action: function () {
      return "Ease off and let it cool.";
    },
    title: function () {
      return "Transmission hot";
    },
  },
  battery_voltage_: {
    subject: function () {
      return "Battery";
    },
    comparison: function (f) {
      const base = relativeComparison(f);
      if (f.parked || f.alternator === null) return base;
      const alt = "with the alternator at " + fmtFixed(f.alternator, 0, false) + " %";
      return base === null ? alt : base + " " + alt;
    },
    action: function (f) {
      return f.parked ? "Start the engine or charge it soon." : "Limit accessories and check charging.";
    },
    title: function (f) {
      return f.parked ? "Battery low" : "Charging low";
    },
  },
  tire_pressure_: {
    subject: function (f) {
      const wheel = wheelLabel(f.rule) || wheelLabel(f.metric);
      return wheel ? wheel + " tire" : "Tire";
    },
    comparison: relativeComparison,
    action: function () {
      return "Check pressure at the next stop.";
    },
    title: function (f) {
      // A pair rule's metric is only its lower wheel: use "Rear tires uneven".
      if (f.title && PAIR_RULE.test(f.rule)) return f.title;
      const wheel = wheelLabel(f.rule) || wheelLabel(f.metric);
      return (wheel ? wheel + " tire " : "Tire ") + (f.direction === "high" ? "high" : "low");
    },
  },
  custom_: {
    subject: function (f) {
      return f.label;
    },
    comparison: customComparison,
    action: function () {
      return "Check it at the next stop.";
    },
    title: function (f) {
      return f.customTitle || f.label + (f.direction === "high" ? " high" : " low");
    },
  },
  default: {
    subject: function (f) {
      return f.label;
    },
    comparison: function (f) {
      return relativeComparison(f) || customComparison(f);
    },
    action: function () {
      return "Check at the next stop.";
    },
    title: function (f) {
      return f.label + (f.direction === "high" ? " high" : " low");
    },
  },
});

/**
 * Pick the template for a rule id (longest matching prefix, else `default`).
 * @param {string|null|undefined} rule
 * @returns {{subject: Function, qualifier?: Function, comparison: Function, action: Function, title: Function}}
 */
export function templateFor(rule) {
  let best = null;
  let bestLen = 0;
  const keys = Object.keys(TEMPLATES);
  for (let i = 0; i < keys.length; i += 1) {
    const key = keys[i];
    if (key !== "default" && hasPrefix(rule, key) && key.length > bestLen) {
      best = TEMPLATES[key];
      bestLen = key.length;
    }
  }
  return best || TEMPLATES.default;
}

/**
 * How long a finding has been held, in seconds: the open episode's age when
 * one exists, otherwise the persistence window scaled by observed/required.
 * @param {Object|null} assessment
 * @param {Object|null} episode
 * @param {number} nowMs wall-clock epoch ms
 * @returns {number|null}
 */
export function heldSeconds(assessment, episode, nowMs) {
  if (episode) {
    const opened = parseDate(episode.opened_at);
    if (opened !== null && typeof nowMs === "number" && nowMs >= opened.getTime()) {
      return (nowMs - opened.getTime()) / 1000;
    }
  }
  const p = assessment ? obj(assessment.persistence) : null;
  if (p) {
    const window = num(p.window_seconds);
    const required = num(p.required);
    const observed = num(p.observed);
    if (window !== null && required !== null && required > 0 && observed !== null && observed > 0) {
      return (window * Math.min(observed, required)) / required;
    }
  }
  return null;
}

function corroboratingAlternator(assessment) {
  const items = list(assessment.corroborators);
  for (let i = 0; i < items.length; i += 1) {
    const c = items[i];
    if (c.metric !== "generator.field_duty") continue;
    const current = obj(c.current);
    const value = current ? num(current.value) : null;
    if (value !== null && (c.state === "corroborating" || c.state === "not_corroborating")) return value;
  }
  return null;
}

function runningRpm(assessment) {
  const evidence = obj(assessment.running_evidence);
  if (!evidence) return null;
  const rpm = obj(evidence.rpm);
  return rpm ? num(rpm.value) : null;
}

function thresholdOf(assessment) {
  const absolute = obj(assessment.absolute_threshold);
  if (absolute && num(absolute.value) !== null) {
    return { operator: str(absolute.operator) || "below", value: absolute.value, unit: str(absolute.unit) };
  }
  const custom = obj(assessment.custom_rule);
  if (custom && num(custom.threshold) !== null) {
    return { operator: str(custom.operator) || "above", value: custom.threshold, unit: null };
  }
  return null;
}

/**
 * Reduce an assessment (plus its open episode, if any) to the facts the
 * templates read. Everything is pre-formatted with the metric's decimals.
 * @param {Object} assessment
 * @param {Object|null} episode
 * @param {number} nowMs wall-clock epoch ms
 * @returns {{rule: string, metric: string|null, label: string, direction: string, severity: string, state: string,
 *   value: number|null, valueText: string|null, unit: string, median: number|null, medianText: string|null,
 *   delta: number|null, deltaText: string|null, threshold: {operator: string, value: number, unit: string|null}|null,
 *   absolute: boolean, rpm: number|null, alternator: number|null, parked: boolean, heldSeconds: number|null,
 *   heldText: string|null, customTitle: string|null, group: string|null}}
 */
export function warningFacts(assessment, episode, nowMs) {
  const a = obj(assessment) || {};
  const rule = str(a.rule) || (episode ? str(episode.rule) : null) || "";
  const current = obj(a.current);
  const custom = obj(a.custom_rule);
  const metric = str(a.metric) || (custom ? str(custom.metric) : null) || (episode ? str(episode.metric) : null);
  const value = current ? num(current.value) : null;
  const unitRaw = (current ? str(current.unit) : null) || null;
  const unit = fmtUnit(metric || "", unitRaw);
  const baseline = obj(a.baseline);
  const median = baseline ? num(baseline.median) : null;
  const decimals = decimalsFor(metric || "");
  // Difference of the displayed (rounded) numbers, so the sentence's
  // arithmetic always adds up on screen.
  const delta = value !== null && median !== null ? Math.abs(roundTo(value, decimals) - roundTo(median, decimals)) : null;
  const threshold = thresholdOf(a);
  const regime = str(a.regime) || "";
  const held = heldSeconds(a, episode, nowMs);
  const direction = str(a.direction) || (threshold && threshold.operator === "below" ? "low" : "high");
  return {
    rule: rule,
    metric: metric,
    label: metricLabel(metric),
    direction: direction,
    severity: str(a.severity) || "warning",
    state: str(a.state) || (episode ? str(episode.state) : null) || "unknown",
    value: value,
    valueText: value === null ? null : fmtValue(metric || "", value),
    unit: unit,
    median: median,
    medianText: median === null ? null : fmtFixed(median, decimals, false),
    delta: delta,
    deltaText: delta === null ? null : fmtFixed(delta, decimals, false),
    threshold: threshold,
    absolute: obj(a.absolute_threshold) !== null,
    rpm: runningRpm(a),
    alternator: corroboratingAlternator(a),
    parked: regime.indexOf("engine_off") === 0,
    heldSeconds: held,
    heldText: held === null || held < 1 ? null : fmtDuration(held),
    customTitle: custom ? str(custom.title) : null,
    title: str(a.title) || (episode ? str(episode.title) : null),
    group: str(a.group),
  };
}

/**
 * Compose the one-line card sentence for a live watch/warning assessment:
 * `<Subject> <value> <unit>[ qualifier], <comparison> for <duration>. <Action>`.
 * A backend-supplied `action` replaces the template's closing sentence.
 * @param {Object} assessment
 * @param {Object|null} episode open episode for the same rule, if any
 * @param {number} nowMs wall-clock epoch ms
 * @returns {{line: string, action: string, title: string, facts: Object}}
 */
export function warningLine(assessment, episode, nowMs) {
  const f = warningFacts(assessment, episode, nowMs);
  const tpl = templateFor(f.rule);
  // Pair rules: the broker writes the lower wheel against its mate (early_warning._pair_summary).
  const pairHead = PAIR_RULE.test(f.rule) ? str((obj(assessment && assessment.pair) || {}).summary) : null;
  let head = pairHead || tpl.subject(f);
  if (pairHead) f.valueText = null;
  if (f.valueText !== null) head += " " + f.valueText + (f.unit ? " " + f.unit : "");
  const qualifier = tpl.qualifier ? tpl.qualifier(f) : null;
  if (qualifier) head += " " + qualifier;
  const comparison = pairHead ? null : tpl.comparison(f);
  let sentence = head + (comparison ? ", " + comparison : "") + (f.heldText ? " for " + f.heldText : "") + ".";
  const supplied = str(assessment && assessment.action);
  let action = supplied || tpl.action(f);
  if (action.charAt(action.length - 1) !== ".") action += ".";
  sentence += " " + action;
  return { line: sentence, action: action, title: tpl.title(f), facts: f };
}

/**
 * Line for an open episode whose latest assessment is not a live finding:
 * `Unconfirmed · engine off since 4:25 pm`, or, when the value is back in
 * range, `Coolant back to 198 °F at 2:11 pm.`
 * @param {Object|null} assessment latest assessment (may be null)
 * @param {Object} episode
 * @param {Date} nowDate reference for time labels
 * @returns {{line: string, action: string, title: string, facts: Object}}
 */
export function unconfirmedLine(assessment, episode, nowDate) {
  const f = warningFacts(assessment, episode, nowDate.getTime());
  const tpl = templateFor(f.rule);
  const current = assessment ? obj(assessment.current) : null;
  const lastSeen = (current ? str(current.observed_at) : null) || str(episode.last_observed_at) || str(episode.opened_at);
  const when = timeLabel(lastSeen, nowDate);
  // Without a latest assessment the episode's own state is the opening
  // state, not the current evidence: treat it as unknown.
  const state = obj(assessment) ? f.state : "unknown";
  let line;
  if ((state === "recovering" || state === "normal") && f.valueText !== null) {
    line = tpl.subject(f) + " back to " + f.valueText + (f.unit ? " " + f.unit : "") + (when ? " at " + when : "") + ".";
  } else if (state === "unavailable" || state === "not_applicable" || state === "unknown") {
    line = "Unconfirmed · engine off" + (when ? " since " + when : "");
  } else {
    line = "Unconfirmed · waiting for a fresh reading" + (when ? " since " + when : "");
  }
  return { line: line, action: "", title: tpl.title(f), facts: f };
}

function roleOf(assessment) {
  const current = obj(assessment.current);
  const role = current ? str(current.role) : null;
  if (role) return role;
  const m = /^can_interface_role_(c_can|b_can|can_ch|spare)$/.exec(str(assessment.rule) || "");
  return m ? m[1].replace(/_/g, "-") : null;
}

function roleReason(assessment) {
  const current = obj(assessment.current);
  const parts = [];
  if (current) {
    if (str(current.reason)) parts.push(current.reason);
    if (str(current.role_reason)) parts.push(current.role_reason);
    const gap = obj(current.active_gap);
    if (gap && str(gap.reason)) parts.push(gap.reason);
  }
  if (str(assessment.reason)) parts.push(assessment.reason);
  return parts.join(",").toLowerCase();
}

function roleLine(assessment, since) {
  const role = roleOf(assessment) || "CAN";
  const name = ROLE_NAMES[role] || role.toUpperCase();
  const paused = ROLE_PAUSED[role] || "bus data paused";
  const reason = roleReason(assessment);
  const tail = since ? " since " + since : "";
  if (/absent|missing|adapter_missing|no adapter/.test(reason)) return name + " adapter missing" + tail + " — " + paused + ".";
  if (/ambiguous/.test(reason)) return name + " adapter identity ambiguous" + tail + " — " + paused + ".";
  if (/controller/.test(reason)) return name + " controller error" + tail + " — " + paused + ".";
  if (/interface_down|is down/.test(reason)) return name + " interface down" + tail + " — " + paused + ".";
  if (/mismatch/.test(reason)) return name + " interface configuration mismatch" + tail + ".";
  if (/topology_unusable|one_shot|armed|polling/.test(reason)) {
    return name + " in polling mode" + tail + " (normal while the broker reads diagnostics).";
  }
  if (/pending|probe|initial/.test(reason)) return name + " status not established yet" + tail + ".";
  return name + " bus check failed" + tail + ".";
}

/**
 * Plain-English line for a system item (infrastructure, coverage or
 * data-quality assessment). Cause-specific where the backend gives a cause;
 * otherwise the backend's title with the bus names cleaned up.
 * @param {Object} assessment
 * @param {string|null} since formatted time label or null
 * @returns {string}
 */
export function systemLine(assessment, since) {
  const rule = str(assessment.rule) || "";
  const tail = since ? " since " + since : "";
  if (hasPrefix(rule, "can_interface_role_")) return roleLine(assessment, since);
  if (rule === "can_interface_status_probe") {
    const reason = (str(assessment.reason) || "").toLowerCase();
    if (/failed/.test(reason)) return "Interface status probe failed" + tail + " — adapter health unknown.";
    return "Interface discovery delayed" + tail + ".";
  }
  if (rule === "usb_can_topology_generation_changed") {
    return "USB CAN adapters re-enumerated" + (since ? " at " + since : "") + " (usually self-heals).";
  }
  if (rule === "usb_can_transient_disconnect") {
    return "USB CAN branch dropped and reconnected" + (since ? " at " + since : "") + " — waiting for the roles to settle.";
  }
  if (rule === "can_restoration_inhibit") return "CAN restoration inhibit active" + tail + " — active reads paused.";
  if (hasPrefix(rule, "telemetry_gap_")) {
    return metricLabel(str(assessment.metric)) + " readings missing while running" + tail + ".";
  }
  return systemTitle(str(assessment.title) || "System check") + tail + ".";
}

function dataQualityLine(incident, since) {
  const label = metricLabel(str(incident.metric));
  const n = num(incident.rejection_count);
  const count = n !== null && n > 1 ? " (" + fmtInt(n) + ")" : "";
  return label + " sample filtered" + count + (since ? " since " + since : "") + " — value held until a plausible reading.";
}

function usbIncidentLine(incident, since) {
  const serials = Array.isArray(incident.affected_serials) ? incident.affected_serials : [];
  const serial = typeof serials[0] === "string" && serials[0].length >= 4 ? " …" + serials[0].slice(-4) : "";
  const kind = str(incident.kind) || "";
  const what = /removed|disconnect/.test(kind) ? "dropped off" : "changed";
  return "USB CAN adapter" + serial + " " + what + (since ? " since " + since : "") + " — waiting for it to come back.";
}

/**
 * Backend system titles with bus names upper-cased and the role jargon
 * replaced: `c-can interface role is unhealthy` -> `C-CAN bus unhealthy`.
 * @param {string|null|undefined} title
 * @returns {string}
 */
export function systemTitle(title) {
  const t = str(title) || "System";
  return t
    .replace(/ interface role is unhealthy/i, " bus unhealthy")
    .replace(/\b(c-can|b-can|can-ch)\b/gi, function (m) {
      return ROLE_NAMES[m.toLowerCase()] || m;
    });
}

function makeCard(fields) {
  return {
    id: fields.id,
    tier: fields.tier,
    metric: fields.metric === undefined ? null : fields.metric,
    title: fields.title,
    line: fields.line,
    since: fields.since === undefined ? null : fields.since,
    sinceLabel: fields.sinceLabel === undefined ? null : fields.sinceLabel,
    state: fields.state,
    episodeId: fields.episodeId === undefined ? null : fields.episodeId,
    rule: fields.rule === undefined ? null : fields.rule,
    category: fields.category === undefined ? null : fields.category,
    action: fields.action || "",
    ackable: fields.ackable === true,
    severity: fields.severity || null,
    group: fields.group === undefined ? null : fields.group,
    value: fields.value === undefined ? null : fields.value,
    unit: fields.unit === undefined ? null : fields.unit,
  };
}

function episodeId(episode) {
  const id = episode ? episode.id : null;
  return typeof id === "number" && isFinite(id) ? id : null;
}

function firstIso(values) {
  for (let i = 0; i < values.length; i += 1) {
    if (parseDate(values[i]) !== null) return values[i];
  }
  return null;
}

const TIER_RANK = { critical: 0, warning: 1, notice: 2, system: 3 };
const STATE_RANK = { warning: 0, watch: 1 };

function cardOrder(a, b) {
  const t = (TIER_RANK[a.tier] || 0) - (TIER_RANK[b.tier] || 0);
  if (t !== 0) return t;
  const sa = STATE_RANK[a.state] === undefined ? 2 : STATE_RANK[a.state];
  const sb = STATE_RANK[b.state] === undefined ? 2 : STATE_RANK[b.state];
  if (sa !== sb) return sa - sb;
  const da = parseDate(a.since);
  const db = parseDate(b.since);
  return (db ? db.getTime() : 0) - (da ? da.getTime() : 0);
}

function collectEntries(health) {
  const byRule = new Map();
  const assessments = list(health.assessments).concat(list(health.active));
  for (let i = 0; i < assessments.length; i += 1) {
    const a = assessments[i];
    const rule = str(a.rule);
    if (!rule || !OPEN_STATES.has(a.state) || byRule.has(rule)) continue;
    byRule.set(rule, { rule: rule, assessment: a, episode: null });
  }
  const episodes = obj(health.episodes);
  const active = episodes ? list(episodes.active) : [];
  for (let i = 0; i < active.length; i += 1) {
    const ep = active[i];
    const rule = str(ep.rule);
    if (!rule) continue;
    if (str(ep.status) && ep.status !== "open" && ep.status !== "active") continue;
    const entry = byRule.get(rule);
    if (entry) {
      entry.episode = ep;
      continue;
    }
    const latest = obj(ep.latest_assessment);
    byRule.set(rule, { rule: rule, assessment: latest, episode: ep, first: obj(ep.first_assessment) });
  }
  return Array.from(byRule.values());
}

function categoryOf(entry) {
  const a = entry.assessment;
  return (a && str(a.category)) || (entry.episode && str(entry.episode.category)) || null;
}

function buildVehicleCard(entry, nowDate) {
  const a = entry.assessment;
  const ep = entry.episode;
  const nowMs = nowDate.getTime();
  const live = a !== null && OPEN_STATES.has(a.state);
  const built = live ? warningLine(a, ep, nowMs) : unconfirmedLine(a, ep, nowDate);
  const f = built.facts;
  const since = firstIso([ep ? ep.opened_at : null, a ? a.evaluated_at : null, f && a && a.current ? a.current.observed_at : null]);
  const tierSource = a || ep || {};
  return makeCard({
    id: ep ? "episode:" + episodeId(ep) : "rule:" + entry.rule,
    tier: tierOf({ category: categoryOf(entry), rule: entry.rule, tier: tierSource.tier, severity: tierSource.severity }),
    metric: f.metric,
    title: built.title,
    line: built.line,
    since: since,
    sinceLabel: timeLabel(since, nowDate),
    state: live ? a.state : f.state === "recovering" || f.state === "normal" ? "recovering" : "unconfirmed",
    episodeId: episodeId(ep),
    rule: entry.rule,
    category: categoryOf(entry),
    action: built.action,
    ackable: ep !== null && ep.acknowledged !== true,
    severity: f.severity,
    group: f.group,
    value: f.value,
    unit: f.unit,
  });
}

function buildSystemCard(entry, nowDate) {
  const a = entry.assessment || entry.first || {};
  const ep = entry.episode;
  const current = obj(a.current);
  const gap = current ? obj(current.active_gap) : null;
  const since = firstIso([ep ? ep.opened_at : null, gap ? gap.started_at : null, a.evaluated_at, current ? current.observed_at : null]);
  const sinceLabel = timeLabel(since, nowDate);
  const live = a && OPEN_STATES.has(a.state);
  let line;
  if (live || !ep) {
    line = systemLine(a, sinceLabel);
  } else {
    const lastSeen = firstIso([ep.last_observed_at, ep.opened_at]);
    const when = timeLabel(lastSeen, nowDate);
    line = "Unconfirmed · " + systemTitle(str(ep.title) || "system check") + (when ? ", last seen " + when : "");
  }
  return makeCard({
    id: ep ? "episode:" + episodeId(ep) : "rule:" + entry.rule,
    tier: "system",
    metric: str(a.metric),
    title: systemTitle(str(a.title) || str(ep && ep.title)),
    line: line,
    since: since,
    sinceLabel: sinceLabel,
    state: live ? a.state : "unconfirmed",
    episodeId: episodeId(ep),
    rule: entry.rule,
    category: categoryOf(entry) || "system",
    action: "",
    ackable: ep !== null && ep !== undefined && ep.acknowledged !== true,
    severity: str(a.severity) || "info",
    group: str(a.group) || "system",
  });
}

function buildDeliveryState(health) {
  const nd = obj(health.notification_delivery) || {};
  const episodes = obj(health.episodes);
  const outbox = (episodes && obj(episodes.notification_outbox)) || {};
  const pending = num(outbox.pending) || 0;
  const failed = num(outbox.failed) || 0;
  const lastError = str(nd.last_error);
  return {
    enabled: nd.enabled === true,
    pending: pending,
    failed: failed,
    delivered: num(outbox.delivered) || 0,
    cancelled: num(outbox.cancelled) || 0,
    lastError: lastError,
    lastAttemptAt: str(nd.last_attempt_at),
    show: pending > 0 || failed > 0 || lastError !== null,
  };
}

/**
 * Build the Health view's warning and system-note cards from
 * `summary.health`.
 *
 * @param {Object|null|undefined} healthLite the `health` slice of `/v2/summary`
 * @param {number} [nowMs] wall-clock epoch ms used for `since` labels and
 *   held durations (defaults to `Date.now()`; display-only)
 * @returns {{
 *   available: boolean, reason: string|null, generatedAt: string|null,
 *   warnings: Array<Object>, systemNotes: Array<Object>,
 *   counts: {critical: number, warning: number, notice: number, system: number, open: number, unconfirmed: number},
 *   delivery: {enabled: boolean, pending: number, failed: number, delivered: number, cancelled: number,
 *     lastError: string|null, lastAttemptAt: string|null, show: boolean}
 * }}
 *   Each card is `{id, tier, metric, title, line, since, sinceLabel, state,
 *   episodeId, rule, category, action, ackable, severity, group, value, unit}`.
 *   Warnings are sorted critical first, confirmed before watch before
 *   unconfirmed, newest first. `counts.open` is the badge number.
 */
export function buildWarningCards(healthLite, nowMs) {
  const health = obj(healthLite);
  const nowDate = new Date(typeof nowMs === "number" && isFinite(nowMs) ? nowMs : Date.now());
  const counts = { critical: 0, warning: 0, notice: 0, system: 0, open: 0, unconfirmed: 0 };
  if (!health || health.available === false) {
    return {
      available: false,
      reason: (health && str(health.reason)) || "health_unavailable",
      generatedAt: null,
      warnings: [],
      systemNotes: [],
      counts: counts,
      delivery: buildDeliveryState(health || {}),
    };
  }

  const warnings = [];
  const systemNotes = [];
  const entries = collectEntries(health);
  for (let i = 0; i < entries.length; i += 1) {
    const entry = entries[i];
    const probe = { category: categoryOf(entry), rule: entry.rule };
    if (isSystemItem(probe)) systemNotes.push(buildSystemCard(entry, nowDate));
    else warnings.push(buildVehicleCard(entry, nowDate));
  }

  const dq = obj(health.data_quality);
  const dqActive = dq ? list(dq.active) : [];
  for (let i = 0; i < dqActive.length; i += 1) {
    const incident = dqActive[i];
    const since = firstIso([incident.first_seen_at, incident.last_seen_at]);
    const sinceLabel = timeLabel(since, nowDate);
    systemNotes.push(
      makeCard({
        id: "data-quality:" + (str(incident.incident_id) || str(incident.metric) || i),
        tier: "system",
        metric: str(incident.metric),
        title: metricLabel(str(incident.metric)) + " sample filtered",
        line: dataQualityLine(incident, sinceLabel),
        since: since,
        sinceLabel: sinceLabel,
        state: "active",
        rule: str(incident.reason) || "data_quality",
        category: "data_quality",
        severity: "info",
        group: "system",
      })
    );
  }

  const usb = obj(health.usb_can_incidents);
  const usbActive = usb ? list(usb.active) : [];
  for (let i = 0; i < usbActive.length; i += 1) {
    const incident = usbActive[i];
    const since = firstIso([incident.opened_at, incident.last_seen_at]);
    const sinceLabel = timeLabel(since, nowDate);
    systemNotes.push(
      makeCard({
        id: "usb:" + (str(incident.incident_id) || i),
        tier: "system",
        title: "USB CAN adapter disconnected",
        line: usbIncidentLine(incident, sinceLabel),
        since: since,
        sinceLabel: sinceLabel,
        state: str(incident.state) || "active",
        rule: str(incident.kind) || "usb_can_incident",
        category: "can_infrastructure",
        severity: "warning",
        group: "system",
      })
    );
  }

  warnings.sort(cardOrder);
  systemNotes.sort(function (a, b) {
    const ca = a.severity === "critical" ? 0 : 1;
    const cb = b.severity === "critical" ? 0 : 1;
    if (ca !== cb) return ca - cb;
    return cardOrder(a, b);
  });

  for (let i = 0; i < warnings.length; i += 1) {
    const card = warnings[i];
    counts[card.tier] = (counts[card.tier] || 0) + 1;
    if (!OPEN_STATES.has(card.state)) counts.unconfirmed += 1;
  }
  counts.open = warnings.length;
  counts.system = systemNotes.length;

  return {
    available: true,
    reason: null,
    generatedAt: str(health.generated_at),
    warnings: warnings,
    systemNotes: systemNotes,
    counts: counts,
    delivery: buildDeliveryState(health),
  };
}
