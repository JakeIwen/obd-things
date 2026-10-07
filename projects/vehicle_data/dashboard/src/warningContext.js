/**
 * Pure warning state/context shared by always-loaded indicators and the lazy
 * Health/Parked prose formatter. Keep sentence construction in warnings.js so
 * the core bundle only pays for the fields needed by badges, bands and alerts.
 */

import { fmtUnit, parseDate } from "./format.js";

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

/** Same-axle tire asymmetry rules (`tire_pressure_pair_asymmetry`). */
export const PAIR_RULE = /^tire_pressure_pair_/;

export function num(value) {
  return typeof value === "number" && isFinite(value) ? value : null;
}

export function str(value) {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function obj(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

export function list(value) {
  if (!Array.isArray(value)) return [];
  const out = [];
  for (let i = 0; i < value.length; i += 1) {
    const item = obj(value[i]);
    if (item && item.omitted_items === undefined) out.push(item);
  }
  return out;
}

/** Human-readable metric subject. */
export function metricLabel(name) {
  if (typeof name !== "string" || name.length === 0) return "Reading";
  const known = METRIC_LABELS[name];
  if (known) return known;
  const tail = name.slice(name.lastIndexOf(".") + 1).replace(/_/g, " ");
  return tail.charAt(0).toUpperCase() + tail.slice(1);
}

/** Wheel code from a tire rule or metric name, or `null`. */
export function wheelLabel(ruleOrMetric) {
  if (typeof ruleOrMetric !== "string") return null;
  const match = /(?:^|[._])tire[._]pressure[._]([fr][lr])(?:$|[._])/.exec(ruleOrMetric);
  return match ? WHEELS[match[1]] || null : null;
}

/** Whether an item belongs on System notes rather than the warning list. */
export function isSystemItem(item) {
  if (!item || typeof item !== "object") return false;
  if (typeof item.category === "string" && SYSTEM_CATEGORIES.has(item.category)) return true;
  const rule = typeof item.rule === "string" ? item.rule : "";
  for (let i = 0; i < SYSTEM_RULE_PREFIXES.length; i += 1) {
    if (rule.indexOf(SYSTEM_RULE_PREFIXES[i]) === 0) return true;
  }
  return false;
}

/** Resolve the display tier without constructing any warning prose. */
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

/** Join live assessments to their open episode without formatting either. */
export function collectWarningEntries(health) {
  const byRule = new Map();
  const assessments = list(health.assessments).concat(list(health.active));
  for (let i = 0; i < assessments.length; i += 1) {
    const assessment = assessments[i];
    const rule = str(assessment.rule);
    if (!rule || !OPEN_STATES.has(assessment.state) || byRule.has(rule)) continue;
    byRule.set(rule, { rule, assessment, episode: null });
  }
  const episodes = obj(health.episodes);
  const active = episodes ? list(episodes.active) : [];
  for (let i = 0; i < active.length; i += 1) {
    const episode = active[i];
    const rule = str(episode.rule);
    if (!rule) continue;
    if (str(episode.status) && episode.status !== "open" && episode.status !== "active") continue;
    const entry = byRule.get(rule);
    if (entry) entry.episode = episode;
    else byRule.set(rule, { rule, assessment: obj(episode.latest_assessment), episode, first: obj(episode.first_assessment) });
  }
  return Array.from(byRule.values());
}

export function warningCategory(entry) {
  const assessment = entry.assessment;
  return (assessment && str(assessment.category)) || (entry.episode && str(entry.episode.category)) || null;
}

export function episodeId(episode) {
  const id = episode ? episode.id : null;
  return typeof id === "number" && isFinite(id) ? id : null;
}

export function firstIso(values) {
  for (let i = 0; i < values.length; i += 1) {
    if (parseDate(values[i]) !== null) return values[i];
  }
  return null;
}

const TIER_RANK = { critical: 0, warning: 1, notice: 2, system: 3 };
const STATE_RANK = { warning: 0, watch: 1 };

export function cardOrder(a, b) {
  const tier = (TIER_RANK[a.tier] || 0) - (TIER_RANK[b.tier] || 0);
  if (tier !== 0) return tier;
  const stateA = STATE_RANK[a.state] === undefined ? 2 : STATE_RANK[a.state];
  const stateB = STATE_RANK[b.state] === undefined ? 2 : STATE_RANK[b.state];
  if (stateA !== stateB) return stateA - stateB;
  const dateA = parseDate(a.since);
  const dateB = parseDate(b.since);
  return (dateB ? dateB.getTime() : 0) - (dateA ? dateA.getTime() : 0);
}

function directionOf(assessment) {
  const absolute = obj(assessment.absolute_threshold);
  const custom = obj(assessment.custom_rule);
  const operator = (absolute && str(absolute.operator)) || (custom && str(custom.operator));
  return str(assessment.direction) || (operator === "below" ? "low" : "high");
}

export const WARNING_TITLES = Object.freeze({
  oil: (f) => f.absolute || f.severity === "critical" ? "Oil pressure critical" : "Oil pressure low",
  coolant: () => "Coolant hot",
  transmission: () => "Transmission hot",
  battery: (f) => f.parked ? "Battery low" : "Charging low",
  tire: (f) => {
    // A pair rule's metric is only its lower wheel: use "Rear tires uneven".
    if (f.title && PAIR_RULE.test(f.rule)) return f.title;
    const wheel = wheelLabel(f.rule) || wheelLabel(f.metric);
    return (wheel ? wheel + " tire " : "Tire ") + (f.direction === "high" ? "high" : "low");
  },
  custom: (f) => f.customTitle || f.label + (f.direction === "high" ? " high" : " low"),
  fallback: (f) => f.label + (f.direction === "high" ? " high" : " low"),
});

function contextTitle(assessment, episode, facts) {
  const supplied = str(assessment.title) || (episode ? str(episode.title) : null);
  const reason = str(assessment.reason);
  if (OPEN_STATES.has(assessment.state) && assessment.tier === 2 && reason && !FORBIDDEN_WORDS.test(reason) && supplied) return supplied;
  if (facts.rule.indexOf("engine_oil_pressure_") === 0) return WARNING_TITLES.oil(facts);
  if (facts.rule.indexOf("engine_coolant_") === 0) return WARNING_TITLES.coolant(facts);
  if (facts.rule.indexOf("transmission_oil_") === 0) return WARNING_TITLES.transmission(facts);
  if (facts.rule.indexOf("battery_voltage_") === 0) return WARNING_TITLES.battery(facts);
  if (facts.rule.indexOf("tire_pressure_") === 0) return WARNING_TITLES.tire({ ...facts, title: supplied });
  if (facts.rule.indexOf("custom_") === 0) {
    const custom = obj(assessment.custom_rule);
    return WARNING_TITLES.custom({ ...facts, customTitle: custom && str(custom.title) });
  }
  return WARNING_TITLES.fallback(facts);
}

/** Structural fields the core warning indicators need for one vehicle item. */
export function warningContextForEntry(entry) {
  const assessment = obj(entry.assessment) || {};
  const episode = obj(entry.episode);
  const current = obj(assessment.current);
  const custom = obj(assessment.custom_rule);
  const metric = str(assessment.metric) || (custom ? str(custom.metric) : null) || (episode ? str(episode.metric) : null);
  const rule = str(assessment.rule) || (episode ? str(episode.rule) : null) || entry.rule || "";
  const rawState = str(assessment.state) || (episode ? str(episode.state) : null) || "unknown";
  const live = entry.assessment !== null && OPEN_STATES.has(assessment.state);
  const severity = str(assessment.severity) || "warning";
  const facts = {
    rule,
    metric,
    label: metricLabel(metric),
    direction: directionOf(assessment),
    severity,
    absolute: obj(assessment.absolute_threshold) !== null,
    parked: (str(assessment.regime) || "").indexOf("engine_off") === 0,
  };
  const category = warningCategory(entry);
  const tierSource = entry.assessment || episode || {};
  const since = firstIso([episode ? episode.opened_at : null, assessment.evaluated_at, current ? current.observed_at : null]);
  return {
    id: episode ? "episode:" + episodeId(episode) : "rule:" + rule,
    tier: tierOf({ category, rule, tier: tierSource.tier, severity: tierSource.severity }),
    metric,
    title: contextTitle(assessment, episode, facts),
    state: live ? assessment.state : rawState === "recovering" || rawState === "normal" ? "recovering" : "unconfirmed",
    episodeId: episodeId(episode),
    rule,
    category,
    severity,
    group: str(assessment.group),
    value: current ? num(current.value) : null,
    unit: fmtUnit(metric || "", current ? str(current.unit) : null),
    since,
  };
}

/** Build the core-only warning context; prose and system notes stay lazy. */
export function buildWarningContext(healthLite) {
  const health = obj(healthLite);
  const counts = { critical: 0, warning: 0, notice: 0, system: 0, open: 0, unconfirmed: 0 };
  if (!health || health.available === false) {
    return { available: false, reason: (health && str(health.reason)) || "health_unavailable", warnings: [], counts };
  }
  const warnings = [];
  const entries = collectWarningEntries(health);
  for (let i = 0; i < entries.length; i += 1) {
    const entry = entries[i];
    if (isSystemItem({ category: warningCategory(entry), rule: entry.rule })) continue;
    warnings.push(warningContextForEntry(entry));
  }
  warnings.sort(cardOrder);
  for (let i = 0; i < warnings.length; i += 1) {
    const card = warnings[i];
    counts[card.tier] = (counts[card.tier] || 0) + 1;
    if (!OPEN_STATES.has(card.state)) counts.unconfirmed += 1;
  }
  counts.open = warnings.length;
  return { available: true, reason: null, warnings, counts };
}
