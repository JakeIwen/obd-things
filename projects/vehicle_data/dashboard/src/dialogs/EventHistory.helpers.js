/* Event history dialog — pure helpers (no DOM). URL/body builders, response
   classification, the 202-poller, formatting and view models for the saved-event
   evidence. Every function here is covered by test/EventHistory.helpers.test.mjs.
   API schemas and evaluation stay owned by the event backend (docs/event-history.md). */

// ---------- bounds from the backend contract (api-contract.md A.5) ----------
export const Q_MAX = 100;
export const RULE_MAX = 200;
export const NOTE_MIN = 1;
export const NOTE_MAX = 1000;
export const LINKS_MAX = 10;
export const POLL_INTERVAL_MS = 200;
export const POLL_MAX_TRIES = 35;
export const REQUEST_TIMEOUT_MS = 5000;
export const EXPORT_MAX_TIMELINE_PAGES = 200;
export const EXPORT_MAX_BASELINE_PAGES = 1000;
export const RECORD_LIST_LIMIT = 30;
export const RECORD_MAX_DEPTH = 4;
export const CHART_GAP_MS = 15000;
export const REPLAY_MAX_GAP_SECONDS = 10;

export const STATUS_VALUES = ["all", "open", "resolved"];
export const STATUS_OPTIONS = [["All events", "all"], ["Unresolved", "open"], ["Closed", "resolved"]];
export const ANNOTATION_KINDS = ["note", "acknowledged", "dismissed", "reopened_for_review"];
export const ANNOTATION_OPTIONS = [["Add a note", "note"], ["Mark reviewed", "acknowledged"],
  ["Record dismissal", "dismissed"], ["Reopen for review", "reopened_for_review"]];
export const REPLAY_OPERATORS = [["Above", "above"], ["Below", "below"]];
export const LINK_PATTERN = /^(trip|dtc|maintenance|quality|episode):[A-Za-z0-9_.:-]{1,120}$/;
export const DEFAULT_FILTERS = Object.freeze({ q: "", status: "all", rule: "" });

// ---------- URL and body builders ----------
export function normalizeFilters(filters) {
  const f = filters || {};
  const status = STATUS_VALUES.includes(f.status) ? f.status : "all";
  return { q: String(f.q ?? "").trim().slice(0, Q_MAX), status, rule: String(f.rule ?? "").trim().slice(0, RULE_MAX) };
}

export function listUrl(filters, before) {
  const f = normalizeFilters(filters);
  const params = new URLSearchParams({ status: f.status });
  if (f.q) params.set("q", f.q);
  if (f.rule) params.set("rule", f.rule);
  if (before != null && before !== "") params.set("before", String(before));
  return "/v1/events?" + params.toString();
}

export function detailUrl(id, before) {
  const base = "/v1/events/" + encodeURIComponent(String(id));
  return before != null && before !== "" ? base + "?before=" + encodeURIComponent(String(before)) : base;
}

export function annotationsUrl(id) { return "/v1/events/" + encodeURIComponent(String(id)) + "/annotations"; }
export function replayUrl(id) { return "/v1/events/" + encodeURIComponent(String(id)) + "/replay"; }
export function baselinesUrl(id, digest, offset) {
  const params = new URLSearchParams({ digest: String(digest), offset: String(offset ?? 0) });
  return "/v1/events/" + encodeURIComponent(String(id)) + "/baselines?" + params.toString();
}

export function parseLinks(text) {
  return String(text ?? "").split(",").map(v => v.trim()).filter(Boolean);
}

export function annotationBody({ kind, note, links, requestId }) {
  return { kind: String(kind ?? ""), note: String(note ?? ""), links: Array.isArray(links) ? links.slice() : parseLinks(links), request_id: String(requestId ?? "") };
}

/** Returns null when the body satisfies the backend bounds, otherwise a user-facing problem. */
export function validateAnnotation(body) {
  if (!ANNOTATION_KINDS.includes(body.kind)) return "Choose a review action.";
  const note = String(body.note ?? "");
  if (note.trim().length < NOTE_MIN) return "Write a note before saving.";
  if (note.length > NOTE_MAX) return "Notes are limited to " + NOTE_MAX + " characters.";
  if (!Array.isArray(body.links)) return "Record references must be a list.";
  if (body.links.length > LINKS_MAX) return "Link at most " + LINKS_MAX + " records.";
  const bad = body.links.find(link => !LINK_PATTERN.test(link));
  if (bad) return "Use trip:, dtc:, maintenance:, quality: or episode: references (" + bad + " is not one).";
  if (!/^[A-Za-z0-9_-]{12,80}$/.test(body.request_id)) return "A request id could not be generated.";
  return null;
}

/** Blank form fields become NaN (Number("") would be 0) so validateReplay rejects them. */
function numberField(value) {
  if (value == null) return NaN;
  const text = String(value).trim();
  return text === "" ? NaN : Number(text);
}

export function replayBody({ threshold, operator, persistence, runningOnly }) {
  return { threshold: numberField(threshold), operator: operator === "below" ? "below" : "above",
    persistence: numberField(persistence), max_gap_seconds: REPLAY_MAX_GAP_SECONDS, running_only: Boolean(runningOnly) };
}

export function validateReplay(body) {
  if (!Number.isFinite(body.threshold)) return "Enter a numeric threshold.";
  if (!Number.isInteger(body.persistence) || body.persistence < 1 || body.persistence > 60) return "Consecutive readings must be a whole number from 1 to 60.";
  return null;
}

export function newRequestId(cryptoImpl) {
  const c = cryptoImpl || (typeof globalThis !== "undefined" ? globalThis.crypto : undefined);
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  let out = "";
  const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
  if (c && typeof c.getRandomValues === "function") {
    const bytes = c.getRandomValues(new Uint8Array(24));
    for (const b of bytes) out += alphabet[b % alphabet.length];
    return out;
  }
  while (out.length < 24) out += alphabet[Math.floor(Math.random() * alphabet.length)];
  return out;
}

// ---------- responses and the 202 poller ----------
export const PENDING_MESSAGE = "Saved evidence is still loading. Retry shortly.";
export const FAILED_MESSAGE = "Saved evidence could not be loaded";
export const WRITE_TIMEOUT_MESSAGE = "The Pi did not answer in time. Check whether it was saved before trying again.";

/** 'pending' → re-request the identical URL/body; 'ok' → use data; 'error' → surface message. */
export function classifyResponse(status, ok, data) {
  if (status === 202 || status === 429) return { kind: "pending" };
  if (ok) return { kind: "ok", data };
  return { kind: "error", message: (data && typeof data.detail === "string" && data.detail) || FAILED_MESSAGE };
}

function requestInit(payload, timeoutMs) {
  const init = { cache: "no-store" };
  if (payload) { init.method = "POST"; init.headers = { "Content-Type": "application/json" }; init.body = JSON.stringify(payload); }
  if (timeoutMs > 0 && typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") init.signal = AbortSignal.timeout(timeoutMs);
  return init;
}

/**
 * Fetch a saved-evidence route. A 202 {pending:true} (or 429 busy) answer is retried with the
 * identical URL/body every `intervalMs`, up to `tries` attempts, as the previous UI did.
 * `deps.fetch` and `deps.sleep` are injectable for tests.
 */
export async function requestEvidence(path, payload, deps = {}) {
  const doFetch = deps.fetch || (typeof fetch === "function" ? fetch : null);
  if (!doFetch) throw new Error("fetch is not available");
  const sleep = deps.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const tries = deps.tries ?? POLL_MAX_TRIES;
  const intervalMs = deps.intervalMs ?? POLL_INTERVAL_MS;
  const timeoutMs = deps.timeoutMs ?? REQUEST_TIMEOUT_MS;
  for (let attempt = 0; attempt < tries; attempt++) {
    let response;
    try {
      response = await doFetch(path, requestInit(payload, timeoutMs));
    } catch (err) {
      const timedOut = Boolean(err && (err.name === "TimeoutError" || err.name === "AbortError"));
      if (!timedOut) throw err;
      // A slow read (the broker was busy) is re-requested like a 202; a write is never re-sent
      // blindly, and neither case shows the browser's own "The operation timed out." text.
      if (!payload) { await sleep(intervalMs); continue; }
      throw new Error(WRITE_TIMEOUT_MESSAGE);
    }
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    const verdict = classifyResponse(response.status, response.ok, data);
    if (verdict.kind === "pending") { await sleep(intervalMs); continue; }
    if (verdict.kind === "error") throw new Error(verdict.message);
    return verdict.data;
  }
  throw new Error(PENDING_MESSAGE);
}

// ---------- formatting ----------
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad2 = n => (n < 10 ? "0" : "") + n;

export function human(value) { return String(value ?? "Not recorded").replaceAll("_", " "); }

/** Capitalises the first letter (broker titles and transition types arrive lower-case). */
export function sentence(value) { const s = String(value ?? ""); return s.charAt(0).toUpperCase() + s.slice(1); }

/** Broker event titles: bus names in their usual capitals and a capital first letter. */
export function titleText(value) {
  if (typeof value !== "string" || !value) return value;
  return sentence(value.replace(/\b(?:[bc]-can|can-ch)\b/gi, m => m.toUpperCase()));
}

/** Plain age for prose: "31 seconds", "12 minutes", "36 hours", "3 days". */
export function agePhrase(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return null;
  const unit = (n, word) => n + " " + word + (n === 1 ? "" : "s");
  if (seconds < 90) return unit(Math.round(seconds), "second");
  if (seconds < 90 * 60) return unit(Math.round(seconds / 60), "minute");
  if (seconds < 48 * 3600) return unit(Math.round(seconds / 3600), "hour");
  return unit(Math.round(seconds / 86400), "day");
}

/**
 * Evaluator phrases stored by the pre-September-24 rules, rewritten in plain words. Saved evidence
 * is never altered; only its display. The redesigned evaluator already writes plain reasons.
 */
export const LEGACY_REASONS = Object.freeze({
  "persistent history-relative deviation": "stayed outside its usual range long enough to confirm",
  "deviation is present but has not met the persistence requirement": "outside its usual range; waiting to confirm",
  "current value is inside the learned relative-deviation band": "back inside its usual range",
  "latest observation is undated or older than the rule permits": "no fresh reading",
  "latest observation is undated or too old": "no fresh reading",
  "current operating regime does not establish recovery in the opening regime":
    "driving conditions differ from when it opened, so recovery cannot be confirmed yet",
  "latest observation is from a different operating regime": "driving conditions differ from the comparison",
  "no fresh numeric observation": "no fresh reading",
  "no fresh numeric observation is stored": "no fresh reading",
  "comparison awaits continuous running evidence": "waiting for the engine to run long enough to compare",
});

/** Rewrites legacy evaluator phrases and long raw second counts ("130215.8 seconds" → "36 hours"). */
export function plainDetail(text) {
  if (typeof text !== "string") return text;
  const legacy = LEGACY_REASONS[text.trim().toLowerCase()];
  if (legacy) return sentence(legacy);
  return text.replace(/(\d+(?:\.\d+)?) seconds\b/g, (m, n) => (Number(n) >= 90 ? agePhrase(Number(n)) : m));
}

export function isDateLike(value) { return typeof value === "string" && /^\d{4}-\d\d-\d\dT/.test(value); }

/** Fixed 12-hour local formatter (no toLocaleString): `Sep 22, 4:25:07 pm`, year added when it differs from `now`. */
export function formatTime(value, now) {
  if (value == null || value === "") return "Not recorded";
  const d = new Date(value);
  const t = d.getTime();
  if (!Number.isFinite(t)) return "Not recorded";
  const ref = now instanceof Date ? now : new Date(now ?? Date.now());
  let h = d.getHours();
  const suffix = h >= 12 ? "pm" : "am";
  h = h % 12 || 12;
  const year = d.getFullYear() === ref.getFullYear() ? "" : ", " + d.getFullYear();
  return MONTHS[d.getMonth()] + " " + d.getDate() + year + ", " + h + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds()) + " " + suffix;
}

/** Up to two decimals, thousands separators, no locale dependency. */
export function num(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Not recorded";
  const rounded = Math.round(value * 100) / 100;
  const sign = rounded < 0 ? "-" : "";
  const parts = Math.abs(rounded).toFixed(2).split(".");
  const frac = parts[1].replace(/0+$/, "");
  const int = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return sign + int + (frac ? "." + frac : "");
}

export function reading(a) {
  return a?.current?.value == null ? "Not available" : num(a.current.value) + " " + (a.current.unit || "");
}

export const UNEVALUATED_STATES = ["unavailable", "not_applicable", "recovering", "insufficient_history", "rejected"];

export function unevaluated(a) {
  return !a || a.persistence?.evaluated === false || UNEVALUATED_STATES.includes(a.state);
}

export function persistenceText(a) {
  return unevaluated(a) ? "Not evaluated" : (a.persistence?.observed ?? "?") + " of " + (a.persistence?.required ?? "?") + " readings";
}

export function repeatedReadingsText(a) {
  return unevaluated(a) ? "Not evaluated" : (a.persistence?.observed ?? "?") + " of " + (a.persistence?.required ?? "?");
}

const STATE_LABELS = {
  unavailable: "Fresh data unavailable", not_applicable: "Different operating conditions",
  recovering: "Recovery not yet confirmed", insufficient_history: "Not enough comparable history",
  rejected: "Reading rejected", watch: "Watch", warning: "Warning", normal: "Within monitoring reference",
};
export function stateLabel(state) { return STATE_LABELS[state] || human(state); }

export function durationText(seconds) {
  if (seconds == null || typeof seconds !== "number" || !Number.isFinite(seconds)) return "Not recorded";
  if (seconds < 60) return num(seconds) + " s";
  if (seconds < 3600) return num(seconds / 60) + " min";
  return num(seconds / 3600) + " hr";
}

const COVERAGE_LABELS = {
  retained: "Saved", complete: "Complete", collecting: "Still collecting",
  never_recorded: "Not recorded", never_recorded_legacy: "Not saved for this older event",
  legacy_partial: "Some older evidence was not saved", saved_transition_only: "Saved transition only",
  page_complete: "All saved changes shown", paginated: "More changes available",
  pruned_budget: "Removed under the storage limit",
  collection_incomplete_pending_recorder: "Collection incomplete; waiting for recorder",
  truncated: "Partial — sample limit reached",
  truncated_at_sample_limit: "Partial — sample limit reached",
  retained_partial_coverage: "Saved, possibly with gaps",
};
export function coverageText(value) { return COVERAGE_LABELS[value] || human(value); }

export function outcomeLabel(e) {
  return e?.outcome === "unconfirmed" ? "Unconfirmed · Monitoring ended" : e?.status === "open" ? "Unresolved" : "Closed";
}

/** Split `text` into segments; those matching one of `terms` (case-insensitive, longest first) are `strong`. */
export function emphasize(text, terms) {
  const source = String(text ?? "");
  const list = (terms || []).filter(Boolean);
  if (!list.length || !source) return [{ text: source, strong: false }];
  const escaped = list.slice().sort((a, b) => b.length - a.length).map(term => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const parts = source.split(new RegExp("(" + escaped.join("|") + ")", "gi"));
  return parts.map((part, i) => ({ text: part, strong: i % 2 === 1 })).filter(seg => seg.text !== "");
}

// ---------- facts and assessments ----------
export const IMPORTANT_FACTS = new Set(["Saved reading", "Typical reading", "Persistence", "Deviation / required", "Recovery",
  "Elapsed open time", "Observed abnormal intervals", "Unobserved intervals", "Evaluator gaps"]);

/** rows: [label, value] pairs; null values are skipped. `strong` = important and not a negative sentinel. */
export function factRows(rows) {
  const out = [];
  for (const [label, value] of rows || []) {
    if (value == null) continue;
    const text = String(value);
    const important = IMPORTANT_FACTS.has(label);
    out.push({ label, value: text, important, strong: important && !/^(Not|No|Unknown)/i.test(text) });
  }
  return out;
}

export function assessmentModel(a, ref) {
  if (!a) return null;
  const c = a.current || {}, b = a.baseline || {}, d = a.deviation || {};
  return {
    state: stateLabel(a.state),
    reason: a.reason ? " · " + a.reason : "",
    facts: factRows([
      ["Saved reading", reading(a)], ["Measured", formatTime(c.observed_at)],
      ["Evaluated", formatTime(a.evaluated_at || c.captured_at)],
      ["Persistence", persistenceText(a)],
      ["Typical reading", b.median == null ? "Not evaluated" : num(b.median) + " " + (b.unit || c.unit || "")],
      ["Deviation / required", d.signed_from_median == null ? "Not evaluated" : num(d.signed_from_median) + " / " + num(d.threshold) + " " + (c.unit || "")],
      ["Observed conditions", human(a.regime).replaceAll(":", " · ")],
      ["Baseline matches", human(a.baseline_regime || b.regime || "Not evaluated").replaceAll("|", " · ").replaceAll("=", ": ")],
      ["Recovery", recoveryText(a.recovery)],
      ["Baseline support", b.bucket_count == null ? null : b.bucket_count + " minute buckets · " + b.trip_count + " prior trips"],
    ]),
    reference: factRows([
      ["Source", c.source || "Not recorded"], ["Quality", human(c.quality)],
      ["Baseline period", b.first_at ? formatTime(b.first_at) + " – " + formatTime(b.last_at) : null],
      ["Evidence reference", ref || (c.sample_id ? "sample:" + c.sample_id : "Not recorded")],
    ]),
  };
}

// ---------- overview ----------
export function overviewModel(e) {
  const first = e.first_assessment || {}, b = first.baseline || {}, deviation = first.deviation || {};
  const unit = first.current?.unit || "";
  const latest = e.latest_assessment || {};
  let deviationLine = null;
  if (deviation.signed_from_median != null) {
    deviationLine = num(Math.abs(deviation.signed_from_median)) + " " + unit + (deviation.signed_from_median < 0 ? " below" : " above") +
      " the baseline (" + num(deviation.threshold) + " " + unit + " required).";
  } else if (first.reason) deviationLine = first.reason;
  const latestDetail = unevaluated(latest)
    ? (e.monitoring_note?.detail ? " · " + plainDetail(e.monitoring_note.detail) : latest.reason ? " · " + latest.reason : "")
    : " · " + reading(latest);
  return {
    tags: [outcomeLabel(e), "Opened as " + human(first.state)],
    lead: e.first_warning ? "A warning was recorded on " + formatTime(e.first_warning.at) + "." : "No warning escalation is recorded for this event.",
    unconfirmed: e.outcome === "unconfirmed"
      ? "A brief deviation was observed; monitoring ended before a warning was established. This watch is archived, with its evidence retained. Recovery was not established."
      : null,
    keyReadings: [
      ["Reading at opening", reading(first)],
      ["Typical reading", b.median == null ? "Not recorded" : num(b.median) + " " + (b.unit || unit)],
      ["Repeated readings", repeatedReadingsText(first)],
    ],
    deviationLine,
    openedLine: "Opened " + formatTime(e.opened_at) + " · Saved, not live",
    latest: {
      line: stateLabel(latest.state) + latestDetail,
      time: formatTime(e.last_evaluated_at),
      closed: e.status === "resolved" && e.outcome !== "unconfirmed"
        ? "Closed " + formatTime(e.resolved_at) + (e.resolution_reason ? " · " + human(e.resolution_reason) : "") + ". Closure alone does not establish mechanical recovery."
        : null,
    },
  };
}

/**
 * Recovery progress for both evaluator generations: the legacy count of comparable normal
 * readings, or the tiered timed recovery (`held_seconds` of `required_seconds`).
 */
export function recoveryText(r) {
  if (!r || typeof r !== "object") return null;
  if (typeof r.held_seconds === "number" && typeof r.required_seconds === "number") {
    const minutes = (s) => Math.floor(Math.max(0, s) / 60);
    return minutes(r.held_seconds) + " of " + minutes(r.required_seconds) + " minutes held normal";
  }
  if (typeof r.count === "number" && typeof r.required === "number") {
    return r.count + " of " + r.required + " comparable normal readings";
  }
  return null;
}

// ---------- timeline ----------
export function monitoringNote(v) {
  if (v.type === "watch_unconfirmed") return { title: "Watch archived as unconfirmed", detail: "Monitoring ended before a warning was established. Earlier evidence is retained; recovery was not established." };
  if (v.monitoring_note) return typeof v.monitoring_note.detail === "string" ? { ...v.monitoring_note, detail: plainDetail(v.monitoring_note.detail) } : v.monitoring_note;
  if (v.type === "evidence_restored") return { title: "Monitoring resumed", detail: "Usable observations became available again." };
  const a = v.assessment || {}, c = a.current || {}, age = c.effective_age_seconds;
  if (a.state === "unavailable") {
    return { title: "Monitoring paused", detail: typeof age === "number"
      ? "The last reading was " + num(c.value) + " " + (c.unit || "") + ", recorded " + agePhrase(age) + " before this evaluation. It no longer met the freshness check. Earlier evidence is retained; the reason readings stopped is not established."
      : "No usable fresh reading was available. Earlier evidence is retained; the reason is not established." };
  }
  return { title: "Monitoring coverage changed", detail: a.reason ? plainDetail(a.reason) : "The rule could not continue its comparison." };
}

export const TRANSITION_NAMES = { opened: "Event opened", resolved: "Event closed",
  escalated: "Warning escalated", deescalated: "Warning eased", notification_repeat_due: "Reminder due",
  rule_replaced: "Rule replaced", rule_retired: "Rule retired" };

export function isQuietTransition(v) {
  return v.presentation === "monitoring_note" || ["evidence_inconclusive", "evidence_restored", "watch_unconfirmed"].includes(v.type);
}

/** Splits transitions into visible rows and collapsed monitoring notes, preserving order (most recent first as delivered). */
export function timelineModel(entries) {
  const rows = [], notes = [];
  (entries || []).forEach((v, index) => {
    const quiet = isQuietTransition(v);
    const note = quiet ? monitoringNote(v) : null;
    const row = {
      key: v.id ?? v.evidence_ref ?? index,
      title: note ? note.title : TRANSITION_NAMES[v.type] || sentence(human(v.type)),
      at: formatTime(v.at),
      detail: note ? note.detail : v.assessment?.reason ? plainDetail(v.assessment.reason) : human(v.new_state),
      assessment: v.assessment || null,
      evidenceRef: v.evidence_ref || null,
    };
    (quiet ? notes : rows).push(row);
  });
  return { rows, notes };
}

// ---------- sample chart ----------
export const CHART = Object.freeze({ width: 700, height: 220, left: 45, right: 685, top: 20, bottom: 190 });

export function chartModel(event) {
  const coverage = coverageText(event.completeness?.sample_window);
  const stamp = p => new Date(p.observed_at).getTime();
  const points = (event.sample_window || []).filter(p => typeof p.value === "number" && Number.isFinite(p.value) && p.observed_at && Number.isFinite(stamp(p)));
  if (!points.length) return { empty: true, message: "No sample chart is available. " + coverage + "." };
  const xs = points.map(stamp), ys = points.map(p => p.value);
  const opening = event.first_assessment || {}, baseline = opening.baseline || {}, delta = opening.deviation || {};
  let boundary = null;
  if (baseline.median != null && delta.threshold != null && ["high", "low"].includes(opening.direction)) {
    boundary = baseline.median + (opening.direction === "low" ? -1 : 1) * delta.threshold;
    ys.push(boundary);
  }
  const xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(...ys) - 1, ymax = Math.max(...ys) + 1;
  const spanX = CHART.right - CHART.left, spanY = CHART.bottom - CHART.top;
  const x = t => CHART.left + (t - xmin) / Math.max(1, xmax - xmin) * spanX;
  const y = v => CHART.bottom - (v - ymin) / (ymax - ymin) * spanY;
  const round = v => Math.round(v * 100) / 100;
  const segments = [], dots = [];
  points.forEach((p, i) => {
    if (i) {
      const gap = stamp(p) - stamp(points[i - 1]);
      if (gap > 0 && gap <= CHART_GAP_MS && p.source === points[i - 1].source) {
        segments.push({ x1: round(x(stamp(points[i - 1]))), y1: round(y(points[i - 1].value)), x2: round(x(stamp(p))), y2: round(y(p.value)) });
      }
    }
    dots.push({ cx: round(x(stamp(p))), cy: round(y(p.value)), title: formatTime(p.observed_at) + ": " + p.value + " " + (p.unit || "") + " (" + (p.evidence_ref || "no reference") + ")" });
  });
  const unit = points[0].unit || "";
  const values = points.map(p => p.value);
  const caption = formatTime(points[0].observed_at) + " – " + formatTime(points[points.length - 1].observed_at) + " · " +
    num(Math.min(...values)) + "–" + num(Math.max(...values)) + " " + unit + ". " +
    (boundary == null ? "" : "Dashed line: opening boundary " + num(boundary) + " " + unit + ". ") +
    "Gaps over 15 seconds are not joined. " + coverage + ".";
  return {
    empty: false,
    viewBox: "0 0 " + CHART.width + " " + CHART.height,
    label: "Retained samples around event opening. Gaps are not joined.",
    boundary: boundary == null ? null : { y: round(y(boundary)), x1: CHART.left, x2: CHART.right, value: boundary },
    segments, dots, caption,
  };
}

// ---------- notes, replay, evidence sections ----------
export function annotationRows(e) {
  return (e.annotations || []).map((a, i) => ({ key: a.id ?? i, kind: human(a.kind), at: formatTime(a.at), note: a.note ?? "", links: a.links?.length ? a.links.join(" · ") : null }));
}

export function notesSummary(e) {
  const count = e.annotations?.length || 0;
  return "Notes & Review" + (count ? " · " + count : "");
}

export function replayResultModel(result) {
  const points = Array.isArray(result?.points) ? result.points : [];
  const warn = points.filter(p => p.would_warn).length;
  return {
    headline: points.length
      ? points.length + " eligible readings; " + warn + " would satisfy the persistence requirement."
      : "No eligible readings. This saved window cannot answer the comparison with these settings.",
    coverage: "Coverage: " + coverageText(result?.coverage) + ".",
    rows: points.map((p, i) => ({ key: i, at: formatTime(p.at), value: num(p.value), wouldWarn: p.would_warn ? "Yes" : "No" })),
  };
}

export const RETENTION_TERMS = ["no automatic age deletion", "seven days", "two minutes before/three after opening", "256 primary samples max", "128 MiB", "tombstones"];
export const CAUTION_TERMS = ["not the duration of a fault", "last evaluation", "Earlier coverage is unknown"];

export function coverageModel(e, guide) {
  const d = e.duration || {};
  return {
    facts: factRows([
      ["Elapsed open time", durationText(d.elapsed_open_seconds)], ["Observed abnormal intervals", durationText(d.observed_abnormal_seconds)],
      ["Unobserved intervals", durationText(d.unobserved_seconds)], ["Evaluator gaps", durationText(d.evaluator_gap_seconds)],
      ["Coverage starts", formatTime(d.coverage_since)], ["Last evaluation", formatTime(e.last_evaluated_at)],
      ["Opening evidence", coverageText(e.completeness?.opening)], ["Sample window", coverageText(e.completeness?.sample_window)],
      ["Checkpoints", coverageText(e.completeness?.checkpoints)], ["Rule revision", coverageText(e.completeness?.rule_revision)],
    ]),
    caution: emphasize("Open time includes parked and unobserved time; it is not the duration of a fault. Totals stop at the last evaluation." +
      (d.legacy_prefix_unknown ? " Earlier coverage is unknown." : ""), CAUTION_TERMS),
    retention: emphasize(guide?.retention || "Missing evidence remains explicitly unknown.", RETENTION_TERMS),
  };
}

export function assessmentsModel(e) {
  const last = e.evidence?.last_evaluable;
  const items = [{ title: "Opening", assessment: e.first_assessment, ref: (e.evidence_ref || "") + ":opening" }];
  if (e.first_warning) items.push({ title: "First Warning", assessment: e.first_warning.assessment, ref: e.first_warning.evidence_ref });
  items.push({ title: "Last Usable Assessment", assessment: last?.assessment, ref: last?.evidence_ref });
  items.push({ title: "Latest Evaluation", assessment: e.latest_assessment, ref: (e.evidence_ref || "") + ":latest" });
  return { items, transitionOnly: last?.coverage === "saved_transition_only"
    ? "The last usable assessment comes from a saved transition, not a full evaluation history." : null };
}

export function peaksModel(e) {
  return [["peak_value", "Highest Recorded Value"], ["peak_deviation", "Largest Recorded Deviation"]].map(([key, title]) => {
    const peak = e.evidence?.[key];
    return { title, assessment: peak?.assessment, ref: peak?.evidence_ref };
  });
}

export function recurrenceModel(e) {
  const recurrence = e.recurrences || {}, stats = recurrence.last_30_days || {};
  return {
    facts: factRows([
      ["Openings in 30 days", num(stats.episode_openings)], ["Recorded trips", num(stats.recorded_trips)],
      ["Recorded trip hours", num(stats.recorded_trip_hours)], ["Openings per recorded trip", num(stats.openings_per_recorded_trip)],
    ]),
    caveat: stats.denominator_caveat || recurrence.interpretation || "These counts include watches.",
    events: (recurrence.events || []).map(r => ({ id: r.id, label: "Event " + r.id + " · " + formatTime(r.opened_at) + " · " + (r.status === "open" ? "Unresolved" : "Closed") })),
  };
}

export function identifierFacts(e) {
  return factRows([["Event", e.evidence_ref], ["Rule", e.rule], ["Evidence revision", e.revision]]);
}

/** Generic labeled-record model (less common evidence shown as facts, never raw JSON). */
export function recordModel(value, depth = 0) {
  if (value == null) return { kind: "empty", text: "Not recorded" };
  if (typeof value !== "object") return { kind: "text", text: typeof value === "boolean" ? (value ? "Yes" : "No") : String(value) };
  if (Array.isArray(value)) {
    if (!value.length) return { kind: "empty", text: "No saved records" };
    return {
      kind: "list",
      items: value.slice(0, RECORD_LIST_LIMIT).map((item, i) => item != null && typeof item === "object"
        ? { key: i, label: "Record " + (i + 1), value: item }
        : { key: i, text: item == null ? "Not recorded" : typeof item === "boolean" ? (item ? "Yes" : "No") : String(item) }),
      truncated: value.length > RECORD_LIST_LIMIT ? "Showing " + RECORD_LIST_LIMIT + " records. Export includes the complete saved data." : null,
    };
  }
  const rows = [], nested = [];
  for (const [key, item] of Object.entries(value)) {
    if (item != null && typeof item === "object") {
      if (depth < RECORD_MAX_DEPTH) nested.push({ key, label: human(key), value: item });
    } else {
      rows.push([human(key), item == null ? "Not recorded" : typeof item === "boolean" ? (item ? "Yes" : "No") :
        typeof item === "number" ? num(item) : isDateLike(item) ? formatTime(item) : String(item)]);
    }
  }
  return { kind: "object", facts: factRows(rows), nested };
}

// ---------- list view ----------
export function eventRowModel(e) {
  return {
    id: e.id,
    title: titleText(e.title),
    meta: "Event " + e.id + " · " + formatTime(e.opened_at),
    state: e.outcome === "unconfirmed" ? "Unconfirmed · Monitoring ended" : (e.status === "open" ? "Unresolved" : "Closed") + " · " + human(e.evidence_state),
  };
}

export function listStatusText(total) {
  return total ? total + " saved event" + (total === 1 ? "" : "s") + " shown. Closed events remain available." : "No events match these filters.";
}

export const LIST_EMPTY_HINT = "Try a different title, event number or status.";

// ---------- guide ----------
export const GUIDE_TOPICS = [
  ["Watch vs Warning", "A watch can open before enough repeated readings qualify it as a warning. An interrupted watch that never warns is archived as unconfirmed after its quiet window. Neither is a mechanical diagnosis.", ["watch", "warning", "enough repeated readings", "Neither is a mechanical diagnosis"]],
  ["What the Baseline Means", "History-based rules compare similar operating conditions across prior trips. Their boundaries are monitoring references, not manufacturer limits.", ["similar operating conditions", "prior trips", "monitoring references", "not manufacturer limits"]],
  ["Saved vs Live", "Readings keep their original timestamps. Routine freshness expiry is a quiet monitoring note, not a new vehicle-health event. Missing or stale data cannot prove recovery or continuous abnormal operation. Confirmed warnings retain their unresolved history.", ["original timestamps", "Missing or stale data", "cannot prove recovery"]],
  ["Review vs Recovery", "Notes, review and dismissal record your judgment. They do not resolve the event or silence its notifications.", ["Notes, review and dismissal", "do not resolve the event", "silence its notifications"]],
  ["Explore Without Changing Anything", "Threshold comparisons use saved samples only. Viewing, exporting and discussing an event do not request vehicle readings or change a warning rule.", ["saved samples only", "do not request vehicle readings", "change a warning rule"]],
];

export const GUIDE_REFERENCE_TERMS = ["first_assessment", "first_warning", "latest_assessment", "observed_at", "captured_at", "evaluated_at",
  "persistence", "baseline", "recovery", "hysteresis", "not an OEM limit", "no automatic age deletion", "seven days", "128 MiB", "counterfactual", "legacy"];

export function guideModel(guide) {
  const g = guide && typeof guide === "object" ? guide : {};
  return {
    topics: GUIDE_TOPICS.map(([heading, text, terms]) => ({ heading, segments: emphasize(text, terms) })),
    referenceTitle: "Technical reference · guide " + (g.version ?? "unknown"),
    entries: Object.entries(g).filter(([key]) => key !== "version").map(([key, value]) => ({
      key, title: human(key),
      segments: typeof value === "string" ? emphasize(value, GUIDE_REFERENCE_TERMS) : null,
      record: typeof value === "string" ? null : value,
    })),
  };
}

// ---------- export ----------
export function exportFilename(id) { return "telemetry-event-" + id + ".json"; }
export function comparisonFilename(id) { return "telemetry-event-" + id + "-comparison.json"; }
export const INDEX_FILENAME = "telemetry-event-index.json";
export function toJson(value) { return JSON.stringify(value, null, 2); }

export const EXPORT_TRUNCATED_STATUS = "Export saved with explicit timeline limit; older pages remain available.";
export const EXPORT_COMPLETE_STATUS = "Event evidence exported. Keep this file with your backups.";

/**
 * Assemble the full event export: all immutable older transition pages (explicit 200-page bound)
 * and complete archived baseline inputs. `request(path)` is the injectable poller.
 */
export async function collectExport(packet, request, opts = {}) {
  const maxPages = opts.maxPages ?? EXPORT_MAX_TIMELINE_PAGES;
  const maxBaselinePages = opts.maxBaselinePages ?? EXPORT_MAX_BASELINE_PAGES;
  const result = JSON.parse(JSON.stringify(packet));
  const id = result.event.id;
  result.event.timeline = Array.isArray(result.event.timeline) ? result.event.timeline : [];
  let before = result.event.next_event_before;
  for (let page = 0; before && page < maxPages; page++) {
    const more = await request(detailUrl(id, before));
    result.event.timeline.push(...(more?.event?.timeline || []));
    before = more?.event?.next_event_before ?? null;
  }
  result.baseline_inputs = {};
  for (const archive of result.event.baseline_archives || []) {
    if (archive?.status !== "retained") continue;
    let offset = 0, pages = 0;
    const inputs = [];
    do {
      const page = await request(baselinesUrl(id, archive.digest, offset));
      inputs.push(...(page?.inputs || []));
      offset = page?.next_offset ?? null;
      pages++;
    } while (offset != null && pages < maxBaselinePages);
    result.baseline_inputs[archive.digest] = inputs;
  }
  result.event.next_event_before = before;
  result.event.completeness = { ...(result.event.completeness || {}), timeline: before ? "export_truncated_200_pages" : "all_retained_transitions" };
  return { result, truncated: Boolean(before), status: before ? EXPORT_TRUNCATED_STATUS : EXPORT_COMPLETE_STATUS };
}

// ---------- dialog chrome ----------
export function headerModel(view, packet) {
  if (view === "detail") {
    const e = packet?.event;
    return e ? { eyebrow: "EVENT " + e.id, title: titleText(e.title) || "Event Details" } : { eyebrow: "EARLY WARNING", title: "Event Details" };
  }
  return { eyebrow: "EARLY WARNING", title: "Saved Events" };
}
