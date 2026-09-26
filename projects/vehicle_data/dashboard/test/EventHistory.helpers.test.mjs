import { test } from "node:test";
import assert from "node:assert/strict";
import * as H from "../src/dialogs/EventHistory.helpers.js";

// Local-time stamps so the fixed formatter is exercised in whatever zone the test runs in.
const local = (h, m, s, day = 22) => new Date(2026, 8, day, h, m, s).toISOString();
const NOW = new Date(2026, 8, 22, 16, 25, 0);

function assessment(overrides = {}) {
  return {
    state: "watch", reason: "sustained deviation", direction: "high", regime: "running:warm",
    current: { value: 212.5, unit: "°F", observed_at: local(16, 20, 5), captured_at: local(16, 20, 6), source: "ccan", quality: "observed", sample_id: "s1" },
    baseline: { median: 200, unit: "°F", bucket_count: 40, trip_count: 6, first_at: local(9, 0, 0), last_at: local(15, 0, 0), regime: "running|warm=1" },
    deviation: { signed_from_median: 12.5, threshold: 10 },
    persistence: { observed: 3, required: 5, evaluated: true },
    recovery: { count: 1, required: 4 },
    evaluated_at: local(16, 20, 7),
    ...overrides,
  };
}

function sample(offsetSeconds, value, source = "ccan") {
  return { observed_at: new Date(2026, 8, 22, 16, 18, offsetSeconds).toISOString(), value, unit: "°F", source, evidence_ref: "sample:" + offsetSeconds };
}

function event(overrides = {}) {
  return {
    id: 579, title: "Coolant above its usual range", rule: "coolant_high", status: "open", outcome: null, evidence_state: "retained",
    evidence_ref: "event:579", revision: 3, opened_at: local(16, 20, 5), last_evaluated_at: local(16, 24, 0),
    first_assessment: assessment(),
    latest_assessment: assessment({ state: "warning", persistence: { observed: 5, required: 5 } }),
    first_warning: { at: local(16, 21, 0), evidence_ref: "event:579:warning", assessment: assessment({ state: "warning" }) },
    timeline: [
      { id: "t3", type: "escalated", at: local(16, 21, 0), new_state: "warning", assessment: assessment({ state: "warning" }), evidence_ref: "event:579:t3" },
      { id: "t2", type: "evidence_inconclusive", at: local(16, 20, 40), assessment: assessment({ state: "unavailable", current: { value: 210, unit: "°F", effective_age_seconds: 31 } }) },
      { id: "t1", type: "opened", at: local(16, 20, 5), new_state: "watch", assessment: assessment(), evidence_ref: "event:579:t1" },
    ],
    next_event_before: 41,
    sample_window: [sample(0, 198), sample(5, 205), sample(30, 213), sample(33, 214, "other")],
    completeness: { opening: "retained", sample_window: "complete", checkpoints: "collecting", rule_revision: "retained" },
    duration: { elapsed_open_seconds: 7200, observed_abnormal_seconds: 90, unobserved_seconds: 30, evaluator_gap_seconds: 0, coverage_since: local(16, 20, 5), legacy_prefix_unknown: true },
    annotations: [{ id: "a1", kind: "acknowledged", at: local(16, 22, 0), note: "Checked the reservoir", links: ["maintenance:12", "dtc:P0128"] }],
    notifications: [{ channel: "ntfy", sent_at: local(16, 21, 1), delivered: true }],
    related: { trip: { id: 88, started_at: local(15, 0, 0) }, dtc_observations_within_five_minutes: [], maintenance_records: [{ id: 12, date: "2026-09-01" }] },
    recurrences: { last_30_days: { episode_openings: 2, recorded_trips: 9, recorded_trip_hours: 12.25, openings_per_recorded_trip: 0.22, denominator_caveat: "Trip spans, not measured hours." },
      events: [{ id: 401, opened_at: local(10, 0, 0, 20), status: "resolved" }] },
    baseline_archives: [{ digest: "d".repeat(64), status: "retained", role: "opening" }, { digest: "e".repeat(64), status: "pruned" }],
    evidence: {
      last_evaluable: { coverage: "saved_transition_only", evidence_ref: "event:579:last", assessment: assessment() },
      peak_value: { evidence_ref: "event:579:peak", assessment: assessment({ current: { value: 220, unit: "°F" } }) },
      peak_deviation: null,
    },
    ...overrides,
  };
}

// ---------- URL and body builders ----------

test("normalizeFilters trims, bounds and defaults", () => {
  assert.deepEqual(H.normalizeFilters(null), { q: "", status: "all", rule: "" });
  assert.deepEqual(H.normalizeFilters({ q: "  cool ", status: "open", rule: " r1 " }), { q: "cool", status: "open", rule: "r1" });
  assert.equal(H.normalizeFilters({ status: "bogus" }).status, "all");
  assert.equal(H.normalizeFilters({ q: "x".repeat(150) }).q.length, H.Q_MAX);
  assert.equal(H.normalizeFilters({ rule: "y".repeat(300) }).rule.length, H.RULE_MAX);
});

test("listUrl sets status always, q/rule/before only when present", () => {
  assert.equal(H.listUrl({}), "/v1/events?status=all");
  assert.equal(H.listUrl({ q: "coolant 2", status: "resolved", rule: "coolant_high" }, 41), "/v1/events?status=resolved&q=coolant+2&rule=coolant_high&before=41");
  assert.equal(H.listUrl({ status: "open" }, null), "/v1/events?status=open");
  assert.equal(H.listUrl({ status: "open" }, ""), "/v1/events?status=open");
});

test("detail, annotation, replay and baselines URLs", () => {
  assert.equal(H.detailUrl(579), "/v1/events/579");
  assert.equal(H.detailUrl(579, 41), "/v1/events/579?before=41");
  assert.equal(H.detailUrl("579", null), "/v1/events/579");
  assert.equal(H.annotationsUrl(579), "/v1/events/579/annotations");
  assert.equal(H.replayUrl(579), "/v1/events/579/replay");
  assert.equal(H.baselinesUrl(579, "abc", 128), "/v1/events/579/baselines?digest=abc&offset=128");
  assert.equal(H.baselinesUrl(579, "abc"), "/v1/events/579/baselines?digest=abc&offset=0");
});

test("parseLinks splits on commas and drops blanks", () => {
  assert.deepEqual(H.parseLinks(" maintenance:123, dtc:456 ,, "), ["maintenance:123", "dtc:456"]);
  assert.deepEqual(H.parseLinks(null), []);
});

test("annotationBody matches the backend body exactly", () => {
  const body = H.annotationBody({ kind: "note", note: "Checked", links: "trip:1, dtc:2", requestId: "req_123456789012" });
  assert.deepEqual(body, { kind: "note", note: "Checked", links: ["trip:1", "dtc:2"], request_id: "req_123456789012" });
  assert.deepEqual(Object.keys(body), ["kind", "note", "links", "request_id"]);
  assert.deepEqual(H.annotationBody({ kind: "note", note: "x", links: ["episode:9"], requestId: "r" }).links, ["episode:9"]);
});

test("validateAnnotation enforces kind, note 1-1000, ≤10 typed links, request id", () => {
  const ok = { kind: "acknowledged", note: "Looked at it", links: [], request_id: "abcdefghijkl" };
  assert.equal(H.validateAnnotation(ok), null);
  assert.match(H.validateAnnotation({ ...ok, kind: "resolved" }), /review action/);
  assert.match(H.validateAnnotation({ ...ok, note: "   " }), /Write a note/);
  assert.equal(H.validateAnnotation({ ...ok, note: "a".repeat(1000) }), null);
  assert.match(H.validateAnnotation({ ...ok, note: "a".repeat(1001) }), /1000/);
  assert.equal(H.validateAnnotation({ ...ok, links: Array.from({ length: 10 }, (_, i) => "trip:" + i) }), null);
  assert.match(H.validateAnnotation({ ...ok, links: Array.from({ length: 11 }, (_, i) => "trip:" + i) }), /at most 10/);
  assert.match(H.validateAnnotation({ ...ok, links: ["bogus:1"] }), /bogus:1/);
  assert.match(H.validateAnnotation({ ...ok, links: ["trip:" + "x".repeat(121)] }), /references/);
  assert.match(H.validateAnnotation({ ...ok, request_id: "short" }), /request id/);
  for (const kind of H.ANNOTATION_KINDS) assert.equal(H.validateAnnotation({ ...ok, kind }), null);
});

test("replayBody and validateReplay keep the old payload shape and bounds", () => {
  assert.deepEqual(H.replayBody({ threshold: "215.5", operator: "below", persistence: "10", runningOnly: true }),
    { threshold: 215.5, operator: "below", persistence: 10, max_gap_seconds: 10, running_only: true });
  assert.equal(H.replayBody({ threshold: 1, operator: "sideways", persistence: 1 }).operator, "above");
  assert.equal(H.replayBody({ threshold: 1, operator: "above", persistence: 1 }).running_only, false);
  assert.equal(H.validateReplay(H.replayBody({ threshold: "1", operator: "above", persistence: "3" })), null);
  assert.match(H.validateReplay(H.replayBody({ threshold: "", operator: "above", persistence: "3" })), /numeric threshold/);
  assert.match(H.validateReplay(H.replayBody({ threshold: "  ", operator: "above", persistence: "3" })), /numeric threshold/);
  assert.match(H.validateReplay(H.replayBody({ threshold: null, operator: "above", persistence: "3" })), /numeric threshold/);
  assert.match(H.validateReplay(H.replayBody({ threshold: "abc", operator: "above", persistence: "3" })), /numeric threshold/);
  assert.match(H.validateReplay(H.replayBody({ threshold: "1", operator: "above", persistence: "" })), /1 to 60/);
  assert.equal(H.replayBody({ threshold: " 2.5 ", operator: "above", persistence: " 4 " }).threshold, 2.5);
  assert.match(H.validateReplay(H.replayBody({ threshold: "1", operator: "above", persistence: "0" })), /1 to 60/);
  assert.match(H.validateReplay(H.replayBody({ threshold: "1", operator: "above", persistence: "61" })), /1 to 60/);
  assert.match(H.validateReplay(H.replayBody({ threshold: "1", operator: "above", persistence: "2.5" })), /whole number/);
});

test("newRequestId uses randomUUID when present and always satisfies the backend pattern", () => {
  assert.equal(H.newRequestId({ randomUUID: () => "11111111-2222-3333-4444-555555555555" }), "11111111-2222-3333-4444-555555555555");
  const fallback = H.newRequestId({ getRandomValues: arr => { for (let i = 0; i < arr.length; i++) arr[i] = i * 7; return arr; } });
  assert.match(fallback, /^[A-Za-z0-9_-]{12,80}$/);
  assert.match(H.newRequestId({}), /^[A-Za-z0-9_-]{12,80}$/);
  assert.match(H.newRequestId(), /^[A-Za-z0-9_-]{12,80}$/);
});

// ---------- response classification and the 202 poller ----------

test("classifyResponse: 202 and 429 are pending, ok is data, else the detail text", () => {
  assert.deepEqual(H.classifyResponse(202, true, { available: false, pending: true }), { kind: "pending" });
  assert.deepEqual(H.classifyResponse(429, false, { detail: "Event reader is busy" }), { kind: "pending" });
  assert.deepEqual(H.classifyResponse(200, true, { available: true }), { kind: "ok", data: { available: true } });
  assert.deepEqual(H.classifyResponse(400, false, { detail: "bad note" }), { kind: "error", message: "bad note" });
  assert.deepEqual(H.classifyResponse(503, false, null), { kind: "error", message: H.FAILED_MESSAGE });
  assert.deepEqual(H.classifyResponse(500, false, { detail: "" }), { kind: "error", message: H.FAILED_MESSAGE });
});

function scriptedFetch(steps) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    const step = steps[Math.min(calls.length - 1, steps.length - 1)];
    return { status: step.status, ok: step.status >= 200 && step.status < 300, json: async () => { if (step.throws) throw new Error("not json"); return step.body; } };
  };
  return { calls, fetchImpl };
}

test("requestEvidence re-requests the identical URL on 202 then returns the 200 body", async () => {
  const { calls, fetchImpl } = scriptedFetch([{ status: 202, body: { available: false, pending: true } }, { status: 202, body: { pending: true } }, { status: 200, body: { available: true, events: [] } }]);
  const sleeps = [];
  const data = await H.requestEvidence("/v1/events?status=all", null, { fetch: fetchImpl, sleep: async ms => { sleeps.push(ms); } });
  assert.deepEqual(data, { available: true, events: [] });
  assert.equal(calls.length, 3);
  assert.ok(calls.every(c => c.url === "/v1/events?status=all"));
  assert.ok(calls.every(c => c.init.cache === "no-store"));
  assert.ok(calls.every(c => c.init.method === undefined));
  assert.deepEqual(sleeps, [200, 200]);
});

test("requestEvidence posts JSON with the same body on every retry and treats 429 as pending", async () => {
  const { calls, fetchImpl } = scriptedFetch([{ status: 429, body: { detail: "Event reader is busy" } }, { status: 202, body: { pending: true } }, { status: 200, body: { event: { id: 579 } } }]);
  const body = { kind: "note", note: "x", links: [], request_id: "abcdefghijkl" };
  const data = await H.requestEvidence("/v1/events/579/annotations", body, { fetch: fetchImpl, sleep: async () => {} });
  assert.deepEqual(data, { event: { id: 579 } });
  assert.equal(calls.length, 3);
  for (const c of calls) {
    assert.equal(c.init.method, "POST");
    assert.deepEqual(c.init.headers, { "Content-Type": "application/json" });
    assert.equal(c.init.body, JSON.stringify(body));
    assert.equal(c.init.cache, "no-store");
  }
});

test("requestEvidence gives up after 35 pending tries with the retry message", async () => {
  const { calls, fetchImpl } = scriptedFetch([{ status: 202, body: { pending: true } }]);
  let slept = 0;
  await assert.rejects(H.requestEvidence("/v1/events/1", null, { fetch: fetchImpl, sleep: async () => { slept++; } }), { message: H.PENDING_MESSAGE });
  assert.equal(calls.length, 35);
  assert.equal(slept, 35);
  assert.equal(H.POLL_MAX_TRIES, 35);
  assert.equal(H.POLL_INTERVAL_MS, 200);
});

test("requestEvidence surfaces the backend detail or a generic message on failure", async () => {
  const bad = scriptedFetch([{ status: 400, body: { detail: "Notes are limited" } }]);
  await assert.rejects(H.requestEvidence("/v1/events/1/annotations", { kind: "note" }, { fetch: bad.fetchImpl, sleep: async () => {} }), { message: "Notes are limited" });
  assert.equal(bad.calls.length, 1);
  const broken = scriptedFetch([{ status: 503, body: null, throws: true }]);
  await assert.rejects(H.requestEvidence("/v1/events/1", null, { fetch: broken.fetchImpl, sleep: async () => {} }), { message: H.FAILED_MESSAGE });
  const hadFetch = Object.prototype.hasOwnProperty.call(globalThis, "fetch");
  const savedFetch = globalThis.fetch;
  try {
    globalThis.fetch = undefined;
    await assert.rejects(H.requestEvidence("/v1/events/1", null, { sleep: async () => {} }), /fetch is not available/);
  } finally {
    if (hadFetch) globalThis.fetch = savedFetch; else delete globalThis.fetch;
  }
});

test("requestEvidence retries a timed-out read like a 202 but never re-sends a timed-out write", async () => {
  const timeout = () => { const e = new Error("The operation timed out."); e.name = "TimeoutError"; return e; };
  let reads = 0;
  const readFetch = async () => {
    reads++;
    if (reads < 3) throw timeout();
    return { status: 200, ok: true, json: async () => ({ events: [] }) };
  };
  assert.deepEqual(await H.requestEvidence("/v1/events?status=all", null, { fetch: readFetch, sleep: async () => {} }), { events: [] });
  assert.equal(reads, 3);

  let slowReads = 0;
  await assert.rejects(
    H.requestEvidence("/v1/events/1", null, { fetch: async () => { slowReads++; throw timeout(); }, sleep: async () => {}, tries: 4 }),
    { message: H.PENDING_MESSAGE },
  );
  assert.equal(slowReads, 4);

  let writes = 0;
  await assert.rejects(
    H.requestEvidence("/v1/events/1/annotations", { kind: "note" }, { fetch: async () => { writes++; throw timeout(); }, sleep: async () => {} }),
    { message: H.WRITE_TIMEOUT_MESSAGE },
  );
  assert.equal(writes, 1);

  const offline = async () => { throw new TypeError("NetworkError when attempting to fetch resource."); };
  await assert.rejects(H.requestEvidence("/v1/events/1", null, { fetch: offline, sleep: async () => {} }), TypeError);
});

test("requestEvidence honours injected tries and interval", async () => {
  const { calls, fetchImpl } = scriptedFetch([{ status: 202, body: { pending: true } }]);
  const sleeps = [];
  await assert.rejects(H.requestEvidence("/x", null, { fetch: fetchImpl, sleep: async ms => { sleeps.push(ms); }, tries: 3, intervalMs: 50 }));
  assert.equal(calls.length, 3);
  assert.deepEqual(sleeps, [50, 50, 50]);
});

// ---------- formatting ----------

test("formatTime is a fixed 12-hour local formatter with a year only when it differs", () => {
  assert.equal(H.formatTime(local(16, 25, 7), NOW), "Sep 22, 4:25:07 pm");
  assert.equal(H.formatTime(local(0, 5, 0), NOW), "Sep 22, 12:05:00 am");
  assert.equal(H.formatTime(local(12, 0, 0), NOW), "Sep 22, 12:00:00 pm");
  assert.equal(H.formatTime(new Date(2025, 0, 3, 9, 1, 2).toISOString(), NOW), "Jan 3, 2025, 9:01:02 am");
  assert.equal(H.formatTime(null, NOW), "Not recorded");
  assert.equal(H.formatTime("", NOW), "Not recorded");
  assert.equal(H.formatTime("not a date", NOW), "Not recorded");
  assert.equal(H.formatTime(new Date(2026, 8, 22, 4, 25, 0), NOW), "Sep 22, 4:25:00 am");
});

test("num rounds to two decimals with thousands separators and no locale", () => {
  assert.equal(H.num(1234.567), "1,234.57");
  assert.equal(H.num(1000), "1,000");
  assert.equal(H.num(-0.5), "-0.5");
  assert.equal(H.num(0), "0");
  assert.equal(H.num(12.1), "12.1");
  assert.equal(H.num(1234567.891), "1,234,567.89");
  assert.equal(H.num(NaN), "Not recorded");
  assert.equal(H.num("3"), "Not recorded");
  assert.equal(H.num(null), "Not recorded");
});

test("reading, persistence, state labels, durations and coverage text", () => {
  assert.equal(H.reading(assessment()), "212.5 °F");
  assert.equal(H.reading({ current: { value: null } }), "Not available");
  assert.equal(H.reading(null), "Not available");
  assert.equal(H.reading({ current: { value: 3 } }), "3 ");
  assert.equal(H.persistenceText(assessment()), "3 of 5 readings");
  assert.equal(H.persistenceText(assessment({ state: "unavailable" })), "Not evaluated");
  assert.equal(H.persistenceText(assessment({ persistence: { evaluated: false } })), "Not evaluated");
  assert.equal(H.persistenceText(null), "Not evaluated");
  assert.equal(H.persistenceText({ state: "watch", persistence: {} }), "? of ? readings");
  assert.equal(H.repeatedReadingsText(assessment()), "3 of 5");
  assert.equal(H.unevaluated(assessment({ state: "recovering" })), true);
  assert.equal(H.unevaluated(assessment({ state: "normal" })), false);
  assert.equal(H.stateLabel("unavailable"), "Fresh data unavailable");
  assert.equal(H.stateLabel("normal"), "Within monitoring reference");
  assert.equal(H.stateLabel("something_else"), "something else");
  assert.equal(H.stateLabel(undefined), "Not recorded");
  assert.equal(H.durationText(null), "Not recorded");
  assert.equal(H.durationText(45), "45 s");
  assert.equal(H.durationText(90), "1.5 min");
  assert.equal(H.durationText(7200), "2 hr");
  assert.equal(H.coverageText("retained"), "Saved");
  assert.equal(H.coverageText("truncated"), "Partial — sample limit reached");
  assert.equal(H.coverageText("some_new_state"), "some new state");
  assert.equal(H.coverageText(undefined), "Not recorded");
  assert.equal(H.human("a_b_c"), "a b c");
  assert.equal(H.human(null), "Not recorded");
  assert.equal(H.isDateLike("2026-09-22T10:00:00Z"), true);
  assert.equal(H.isDateLike("2026-09-22"), false);
});

test("outcomeLabel distinguishes unconfirmed, unresolved and closed", () => {
  assert.equal(H.outcomeLabel({ outcome: "unconfirmed", status: "resolved" }), "Unconfirmed · Monitoring ended");
  assert.equal(H.outcomeLabel({ status: "open" }), "Unresolved");
  assert.equal(H.outcomeLabel({ status: "resolved" }), "Closed");
});

test("emphasize marks matching terms case-insensitively, longest first, regex-safe", () => {
  assert.deepEqual(H.emphasize("A watch can open", ["watch"]), [{ text: "A ", strong: false }, { text: "watch", strong: true }, { text: " can open", strong: false }]);
  assert.deepEqual(H.emphasize("Watch it", ["watch"]), [{ text: "Watch", strong: true }, { text: " it", strong: false }]);
  const longest = H.emphasize("enough repeated readings", ["readings", "enough repeated readings"]);
  assert.deepEqual(longest, [{ text: "enough repeated readings", strong: true }]);
  assert.deepEqual(H.emphasize("size 128 MiB (max)", ["(max)"]), [{ text: "size 128 MiB ", strong: false }, { text: "(max)", strong: true }]);
  assert.deepEqual(H.emphasize("plain", []), [{ text: "plain", strong: false }]);
  assert.deepEqual(H.emphasize(null, ["x"]), [{ text: "", strong: false }]);
});

// ---------- facts, assessments, overview ----------

test("factRows skips null values and bolds important, non-negative values", () => {
  const rows = H.factRows([["Saved reading", "212.5 °F"], ["Persistence", "Not evaluated"], ["Source", "ccan"], ["Recovery", null], ["Measured", undefined]]);
  assert.deepEqual(rows, [
    { label: "Saved reading", value: "212.5 °F", important: true, strong: true },
    { label: "Persistence", value: "Not evaluated", important: true, strong: false },
    { label: "Source", value: "ccan", important: false, strong: false },
  ]);
  assert.deepEqual(H.factRows(null), []);
});

test("assessmentModel exposes state, reason, key facts and the evidence reference", () => {
  assert.equal(H.assessmentModel(null, "x"), null);
  const m = H.assessmentModel(assessment(), "event:579:opening");
  assert.equal(m.state, "Watch");
  assert.equal(m.reason, " · sustained deviation");
  const facts = Object.fromEntries(m.facts.map(r => [r.label, r.value]));
  assert.equal(facts["Saved reading"], "212.5 °F");
  assert.equal(facts["Persistence"], "3 of 5 readings");
  assert.equal(facts["Typical reading"], "200 °F");
  assert.equal(facts["Deviation / required"], "12.5 / 10 °F");
  assert.equal(facts["Observed conditions"], "running · warm");
  assert.equal(facts["Baseline matches"], "running · warm: 1");
  assert.equal(facts["Recovery"], "1 of 4 comparable normal readings");
  assert.equal(facts["Baseline support"], "40 minute buckets · 6 prior trips");
  assert.equal(facts["Measured"], H.formatTime(local(16, 20, 5)));
  const ref = Object.fromEntries(m.reference.map(r => [r.label, r.value]));
  assert.equal(ref["Source"], "ccan");
  assert.equal(ref["Quality"], "observed");
  assert.equal(ref["Evidence reference"], "event:579:opening");
  assert.ok(ref["Baseline period"].includes(" – "));
  const sparse = H.assessmentModel({ state: "watch", current: { sample_id: "s9" } });
  const sparseRef = Object.fromEntries(sparse.reference.map(r => [r.label, r.value]));
  assert.equal(sparseRef["Evidence reference"], "sample:s9");
  assert.equal(sparse.reason, "");
  assert.equal(Object.fromEntries(sparse.facts.map(r => [r.label, r.value]))["Typical reading"], "Not evaluated");
  assert.equal(sparse.facts.some(r => r.label === "Recovery"), false);
});

test("overviewModel separates the watch, the first warning and the latest evaluation", () => {
  const m = H.overviewModel(event());
  assert.deepEqual(m.tags, ["Unresolved", "Opened as watch"]);
  assert.equal(m.lead, "A warning was recorded on " + H.formatTime(local(16, 21, 0)) + ".");
  assert.equal(m.unconfirmed, null);
  assert.deepEqual(m.keyReadings, [["Reading at opening", "212.5 °F"], ["Typical reading", "200 °F"], ["Repeated readings", "3 of 5"]]);
  assert.equal(m.deviationLine, "12.5 °F above the baseline (10 °F required).");
  assert.ok(m.openedLine.endsWith(" · Saved, not live"));
  assert.equal(m.latest.line, "Warning · 212.5 °F");
  assert.equal(m.latest.closed, null);
});

test("overviewModel handles unconfirmed watches, unavailable latest state and closure", () => {
  const e = event({ outcome: "unconfirmed", status: "resolved", first_warning: null, resolved_at: local(17, 0, 0), resolution_reason: "quiet_window",
    first_assessment: assessment({ deviation: {}, reason: "brief deviation" }),
    latest_assessment: assessment({ state: "unavailable", reason: "stale" }), monitoring_note: { detail: "Fresh readings stopped" } });
  const m = H.overviewModel(e);
  assert.equal(m.tags[0], "Unconfirmed · Monitoring ended");
  assert.equal(m.lead, "No warning escalation is recorded for this event.");
  assert.match(m.unconfirmed, /archived/);
  assert.equal(m.deviationLine, "brief deviation");
  assert.equal(m.latest.line, "Fresh data unavailable · Fresh readings stopped");
  assert.equal(m.latest.closed, null, "unconfirmed watches are not shown as closed");
  const closed = H.overviewModel(event({ status: "resolved", resolved_at: local(17, 0, 0), resolution_reason: "recovered_normal" }));
  assert.match(closed.latest.closed, /^Closed .* · recovered normal\. Closure alone does not establish mechanical recovery\.$/);
  const below = H.overviewModel(event({ first_assessment: assessment({ deviation: { signed_from_median: -4, threshold: 3 } }) }));
  assert.equal(below.deviationLine, "4 °F below the baseline (3 °F required).");
  const noLatestReason = H.overviewModel(event({ latest_assessment: assessment({ state: "unavailable", reason: "" }), monitoring_note: null }));
  assert.equal(noLatestReason.latest.line, "Fresh data unavailable");
});

// ---------- timeline ----------

test("monitoringNote produces the quiet titles for each transition type", () => {
  assert.equal(H.monitoringNote({ type: "watch_unconfirmed" }).title, "Watch archived as unconfirmed");
  assert.deepEqual(H.monitoringNote({ type: "x", monitoring_note: { title: "T", detail: "D" } }), { title: "T", detail: "D" });
  assert.equal(H.monitoringNote({ type: "evidence_restored" }).title, "Monitoring resumed");
  const paused = H.monitoringNote({ type: "evidence_inconclusive", assessment: { state: "unavailable", current: { value: 210, unit: "°F", effective_age_seconds: 31 } } });
  assert.equal(paused.title, "Monitoring paused");
  assert.match(paused.detail, /The last reading was 210 °F, recorded 31 seconds before this evaluation/);
  const pausedNoAge = H.monitoringNote({ type: "evidence_inconclusive", assessment: { state: "unavailable" } });
  assert.match(pausedNoAge.detail, /No usable fresh reading/);
  assert.deepEqual(H.monitoringNote({ type: "evidence_inconclusive", assessment: { state: "not_applicable", reason: "parked" } }), { title: "Monitoring coverage changed", detail: "parked" });
  assert.equal(H.monitoringNote({ type: "evidence_inconclusive" }).detail, "The rule could not continue its comparison.");
});

test("timelineModel splits visible transitions from quiet monitoring notes", () => {
  const m = H.timelineModel(event().timeline);
  assert.deepEqual(m.rows.map(r => r.title), ["Warning escalated", "Event opened"]);
  assert.deepEqual(m.rows.map(r => r.key), ["t3", "t1"]);
  assert.equal(m.rows[0].detail, "sustained deviation");
  assert.equal(m.rows[0].evidenceRef, "event:579:t3");
  assert.equal(m.notes.length, 1);
  assert.equal(m.notes[0].title, "Monitoring paused");
  assert.equal(H.isQuietTransition({ presentation: "monitoring_note", type: "opened" }), true);
  assert.equal(H.isQuietTransition({ type: "rule_replaced" }), false);
  const fallback = H.timelineModel([{ type: "custom_thing", at: null, new_state: "watch", assessment: {} }]);
  assert.equal(fallback.rows[0].title, "Custom thing");
  assert.equal(fallback.rows[0].detail, "watch");
  assert.equal(fallback.rows[0].at, "Not recorded");
  assert.equal(fallback.rows[0].key, 0);
  assert.deepEqual(H.timelineModel(undefined), { rows: [], notes: [] });
});

// ---------- sample chart ----------

test("chartModel is empty with the coverage text when no numeric samples exist", () => {
  const m = H.chartModel(event({ sample_window: [], completeness: { sample_window: "never_recorded_legacy" } }));
  assert.deepEqual(m, { empty: true, message: "No sample chart is available. Not saved for this older event." });
  assert.equal(H.chartModel(event({ sample_window: [{ value: "x", observed_at: local(1, 0, 0) }, { value: 1, observed_at: "bad" }] })).empty, true);
});

test("chartModel joins only consecutive same-source samples within 15 s and draws the opening boundary", () => {
  const m = H.chartModel(event());
  assert.equal(m.empty, false);
  assert.equal(m.viewBox, "0 0 700 220");
  assert.equal(m.dots.length, 4);
  assert.equal(m.segments.length, 1, "0→5 s joined; 5→30 s gap and 30→33 s source change are not");
  assert.equal(m.segments[0].x1, m.dots[0].cx);
  assert.equal(m.segments[0].x2, m.dots[1].cx);
  assert.equal(m.dots[0].cx, H.CHART.left);
  assert.equal(m.dots[3].cx, H.CHART.right);
  assert.ok(m.dots[0].cy > m.dots[2].cy, "higher values are drawn higher (smaller y)");
  assert.equal(m.boundary.value, 210);
  assert.equal(m.boundary.x1, H.CHART.left);
  assert.equal(m.boundary.x2, H.CHART.right);
  assert.ok(m.boundary.y > H.CHART.top && m.boundary.y < H.CHART.bottom);
  assert.match(m.dots[0].title, /: 198 °F \(sample:0\)$/);
  assert.match(m.caption, /198–214 °F\. Dashed line: opening boundary 210 °F\. Gaps over 15 seconds are not joined\. Complete\.$/);
  assert.match(m.label, /Gaps are not joined/);
});

test("chartModel omits the boundary when direction or threshold is missing", () => {
  const noDir = H.chartModel(event({ first_assessment: assessment({ direction: "either" }) }));
  assert.equal(noDir.boundary, null);
  assert.doesNotMatch(noDir.caption, /Dashed line/);
  const low = H.chartModel(event({ first_assessment: assessment({ direction: "low", deviation: { threshold: 15 } }) }));
  assert.equal(low.boundary.value, 185);
  const single = H.chartModel(event({ sample_window: [sample(0, 200)] }));
  assert.equal(single.dots.length, 1);
  assert.equal(single.segments.length, 0);
  assert.ok(Number.isFinite(single.dots[0].cy));
});

// ---------- notes, replay, evidence sections ----------

test("annotationRows and notesSummary", () => {
  const rows = H.annotationRows(event());
  assert.deepEqual(rows, [{ key: "a1", kind: "acknowledged", at: H.formatTime(local(16, 22, 0)), note: "Checked the reservoir", links: "maintenance:12 · dtc:P0128" }]);
  assert.deepEqual(H.annotationRows({}), []);
  assert.equal(H.annotationRows({ annotations: [{ kind: "note", note: "n" }] })[0].links, null);
  assert.equal(H.notesSummary(event()), "Notes & Review · 1");
  assert.equal(H.notesSummary({}), "Notes & Review");
});

test("replayResultModel summarises eligible readings and coverage", () => {
  const m = H.replayResultModel({ coverage: "complete", points: [{ at: local(16, 0, 0), value: 201, would_warn: false }, { at: local(16, 0, 5), value: 216.25, would_warn: true }] });
  assert.equal(m.headline, "2 eligible readings; 1 would satisfy the persistence requirement.");
  assert.equal(m.coverage, "Coverage: Complete.");
  assert.deepEqual(m.rows[1], { key: 1, at: H.formatTime(local(16, 0, 5)), value: "216.25", wouldWarn: "Yes" });
  const empty = H.replayResultModel({ coverage: "truncated", points: [] });
  assert.match(empty.headline, /^No eligible readings/);
  assert.equal(H.replayResultModel(null).rows.length, 0);
});

test("coverageModel lists durations, coverage states, the caution and retention copy", () => {
  const m = H.coverageModel(event(), { retention: "There is no automatic age deletion; seven days of windows." });
  const facts = Object.fromEntries(m.facts.map(r => [r.label, r.value]));
  assert.equal(facts["Elapsed open time"], "2 hr");
  assert.equal(facts["Observed abnormal intervals"], "1.5 min");
  assert.equal(facts["Unobserved intervals"], "30 s");
  assert.equal(facts["Evaluator gaps"], "0 s");
  assert.equal(facts["Opening evidence"], "Saved");
  assert.equal(facts["Checkpoints"], "Still collecting");
  assert.equal(m.facts.find(r => r.label === "Elapsed open time").strong, true);
  assert.ok(m.caution.some(s => s.strong && s.text === "Earlier coverage is unknown"));
  assert.ok(m.retention.some(s => s.strong && s.text === "no automatic age deletion"));
  const noGuide = H.coverageModel(event({ duration: {} }), {});
  assert.equal(noGuide.retention.map(s => s.text).join(""), "Missing evidence remains explicitly unknown.");
  assert.equal(noGuide.caution.some(s => s.text === "Earlier coverage is unknown"), false);
});

test("assessmentsModel orders opening, first warning, last usable and latest", () => {
  const m = H.assessmentsModel(event());
  assert.deepEqual(m.items.map(i => i.title), ["Opening", "First Warning", "Last Usable Assessment", "Latest Evaluation"]);
  assert.deepEqual(m.items.map(i => i.ref), ["event:579:opening", "event:579:warning", "event:579:last", "event:579:latest"]);
  assert.match(m.transitionOnly, /saved transition/);
  const noWarn = H.assessmentsModel(event({ first_warning: null, evidence: {} }));
  assert.deepEqual(noWarn.items.map(i => i.title), ["Opening", "Last Usable Assessment", "Latest Evaluation"]);
  assert.equal(noWarn.items[1].assessment, undefined);
  assert.equal(noWarn.transitionOnly, null);
});

test("peaksModel, recurrenceModel and identifierFacts", () => {
  const peaks = H.peaksModel(event());
  assert.deepEqual(peaks.map(p => p.title), ["Highest Recorded Value", "Largest Recorded Deviation"]);
  assert.equal(peaks[0].ref, "event:579:peak");
  assert.equal(peaks[1].assessment, undefined);
  const r = H.recurrenceModel(event());
  assert.deepEqual(r.facts.map(f => [f.label, f.value]), [["Openings in 30 days", "2"], ["Recorded trips", "9"], ["Recorded trip hours", "12.25"], ["Openings per recorded trip", "0.22"]]);
  assert.equal(r.caveat, "Trip spans, not measured hours.");
  assert.deepEqual(r.events, [{ id: 401, label: "Event 401 · " + H.formatTime(local(10, 0, 0, 20)) + " · Closed" }]);
  assert.equal(H.recurrenceModel({ recurrences: { interpretation: "Counts include watches." } }).caveat, "Counts include watches.");
  assert.equal(H.recurrenceModel({}).caveat, "These counts include watches.");
  assert.deepEqual(H.recurrenceModel({}).facts.map(f => f.value), ["Not recorded", "Not recorded", "Not recorded", "Not recorded"]);
  assert.deepEqual(H.identifierFacts(event()).map(f => [f.label, f.value]), [["Event", "event:579"], ["Rule", "coolant_high"], ["Evidence revision", "3"]]);
});

test("recordModel renders less common evidence as labelled facts with bounded nesting", () => {
  assert.deepEqual(H.recordModel(null), { kind: "empty", text: "Not recorded" });
  assert.deepEqual(H.recordModel(true), { kind: "text", text: "Yes" });
  assert.deepEqual(H.recordModel("hello"), { kind: "text", text: "hello" });
  assert.deepEqual(H.recordModel([]), { kind: "empty", text: "No saved records" });
  const list = H.recordModel([{ a: 1 }, "plain"]);
  assert.equal(list.kind, "list");
  assert.deepEqual(list.items[0], { key: 0, label: "Record 1", value: { a: 1 } });
  assert.deepEqual(list.items[1], { key: 1, text: "plain" });
  assert.equal(list.truncated, null);
  const big = H.recordModel(Array.from({ length: 31 }, (_, i) => i));
  assert.equal(big.items.length, 30);
  assert.match(big.truncated, /Showing 30 records/);
  const obj = H.recordModel({ sent_at: local(16, 21, 1), delivered: true, retries: 2, note: null, channel_name: "ntfy", nested: { deep: 1 } });
  assert.equal(obj.kind, "object");
  assert.deepEqual(obj.facts.map(f => [f.label, f.value]), [
    ["sent at", H.formatTime(local(16, 21, 1))], ["delivered", "Yes"], ["retries", "2"], ["note", "Not recorded"], ["channel name", "ntfy"],
  ]);
  assert.deepEqual(obj.nested, [{ key: "nested", label: "nested", value: { deep: 1 } }]);
  assert.deepEqual(H.recordModel({ nested: { deep: 1 } }, 4).nested, [], "nesting stops at depth 4");
  assert.equal(H.recordModel({ nested: { deep: 1 } }, 3).nested.length, 1);
});

// ---------- list view ----------

test("eventRowModel and listStatusText", () => {
  assert.deepEqual(H.eventRowModel({ id: 7, title: "T", opened_at: local(1, 2, 3), status: "open", evidence_state: "retained" }),
    { id: 7, title: "T", meta: "Event 7 · " + H.formatTime(local(1, 2, 3)), state: "Unresolved · retained" });
  assert.equal(H.eventRowModel({ id: 8, status: "resolved", evidence_state: "legacy_partial" }).state, "Closed · legacy partial");
  assert.equal(H.eventRowModel({ id: 9, outcome: "unconfirmed", status: "resolved" }).state, "Unconfirmed · Monitoring ended");
  assert.equal(H.listStatusText(0), "No events match these filters.");
  assert.equal(H.listStatusText(1), "1 saved event shown. Closed events remain available.");
  assert.equal(H.listStatusText(25), "25 saved events shown. Closed events remain available.");
  assert.equal(typeof H.LIST_EMPTY_HINT, "string");
  assert.deepEqual(H.STATUS_OPTIONS.map(o => o[1]), H.STATUS_VALUES);
});

// ---------- guide ----------

test("guideModel keeps the five topics and nests the backend guide as a technical reference", () => {
  const m = H.guideModel({ version: 4, retention: "There is no automatic age deletion.", limits: { samples: 256, bytes: "128 MiB" } });
  assert.deepEqual(m.topics.map(t => t.heading), ["Watch vs Warning", "What the Baseline Means", "Saved vs Live", "Review vs Recovery", "Explore Without Changing Anything"]);
  assert.ok(m.topics[0].segments.some(s => s.strong && s.text.toLowerCase() === "watch"));
  assert.equal(m.referenceTitle, "Technical reference · guide 4");
  assert.deepEqual(m.entries.map(e => e.key), ["retention", "limits"]);
  assert.equal(m.entries[0].title, "retention");
  assert.ok(m.entries[0].segments.some(s => s.strong && s.text === "no automatic age deletion"));
  assert.equal(m.entries[0].record, null);
  assert.equal(m.entries[1].segments, null);
  assert.deepEqual(m.entries[1].record, { samples: 256, bytes: "128 MiB" });
  assert.equal(H.guideModel(undefined).referenceTitle, "Technical reference · guide unknown");
  assert.deepEqual(H.guideModel(null).entries, []);
});

// ---------- export ----------

test("filenames and JSON serialisation", () => {
  assert.equal(H.exportFilename(579), "telemetry-event-579.json");
  assert.equal(H.comparisonFilename(579), "telemetry-event-579-comparison.json");
  assert.equal(H.INDEX_FILENAME, "telemetry-event-index.json");
  assert.equal(H.toJson({ a: 1 }), '{\n  "a": 1\n}');
});

function exportRequester(pages, baselines) {
  const calls = [];
  const request = async path => {
    calls.push(path);
    if (path.includes("/baselines?")) {
      const p = new URL(path, "http://x");
      const key = p.searchParams.get("digest") + ":" + p.searchParams.get("offset");
      assert.ok(baselines[key], "unexpected baselines page " + key);
      return baselines[key];
    }
    const before = new URL(path, "http://x").searchParams.get("before");
    assert.ok(pages[before], "unexpected timeline page " + before);
    return pages[before];
  };
  return { calls, request };
}

test("collectExport gathers every timeline page and complete retained baseline inputs without mutating the packet", async () => {
  const packet = { generated_at: local(16, 25, 0), event: event(), system_guide: { version: 1 } };
  const original = JSON.stringify(packet);
  const digest = "d".repeat(64);
  const { calls, request } = exportRequester(
    { 41: { event: { timeline: [{ id: "t0b" }, { id: "t0a" }], next_event_before: 40 } }, 40: { event: { timeline: [{ id: "t-1" }], next_event_before: null } } },
    { [digest + ":0"]: { inputs: [1, 2], next_offset: 2 }, [digest + ":2"]: { inputs: [3], next_offset: null } });
  const out = await H.collectExport(packet, request);
  assert.equal(out.truncated, false);
  assert.equal(out.status, H.EXPORT_COMPLETE_STATUS);
  assert.deepEqual(out.result.event.timeline.map(t => t.id), ["t3", "t2", "t1", "t0b", "t0a", "t-1"]);
  assert.equal(out.result.event.next_event_before, null);
  assert.equal(out.result.event.completeness.timeline, "all_retained_transitions");
  assert.equal(out.result.event.completeness.opening, "retained", "other completeness keys are kept");
  assert.deepEqual(out.result.baseline_inputs, { [digest]: [1, 2, 3] });
  assert.deepEqual(calls, ["/v1/events/579?before=41", "/v1/events/579?before=40",
    "/v1/events/579/baselines?digest=" + digest + "&offset=0", "/v1/events/579/baselines?digest=" + digest + "&offset=2"]);
  assert.equal(JSON.stringify(packet), original);
  assert.equal(out.result.system_guide.version, 1);
});

test("collectExport stops at the page bound and marks the export as truncated", async () => {
  const packet = { event: event({ baseline_archives: [] }) };
  let served = 0;
  const request = async () => { served++; return { event: { timeline: [{ id: "more" + served }], next_event_before: 1000 + served } }; };
  const out = await H.collectExport(packet, request, { maxPages: 3 });
  assert.equal(served, 3);
  assert.equal(out.truncated, true);
  assert.equal(out.status, H.EXPORT_TRUNCATED_STATUS);
  assert.equal(out.result.event.completeness.timeline, "export_truncated_200_pages");
  assert.equal(out.result.event.next_event_before, 1003);
  assert.deepEqual(out.result.baseline_inputs, {});
  assert.equal(H.EXPORT_MAX_TIMELINE_PAGES, 200);
});

test("collectExport tolerates a packet without pagination or timeline", async () => {
  const out = await H.collectExport({ event: { id: 1, next_event_before: null, baseline_archives: null } }, async () => { throw new Error("must not request"); });
  assert.deepEqual(out.result.event.timeline, []);
  assert.equal(out.result.event.completeness.timeline, "all_retained_transitions");
  assert.equal(out.truncated, false);
});

// ---------- dialog chrome ----------

test("headerModel reflects the list, a loading detail and a loaded event", () => {
  assert.deepEqual(H.headerModel("list", null), { eyebrow: "EARLY WARNING", title: "Saved Events" });
  assert.deepEqual(H.headerModel("detail", null), { eyebrow: "EARLY WARNING", title: "Event Details" });
  assert.deepEqual(H.headerModel("detail", { event: { id: 579, title: "Coolant above its usual range" } }), { eyebrow: "EVENT 579", title: "Coolant above its usual range" });
  assert.deepEqual(H.headerModel("detail", { event: { id: 5 } }), { eyebrow: "EVENT 5", title: "Event Details" });
});

test("recordModel list items: null is 'Not recorded' and booleans are Yes/No, never raw JSON words", () => {
  const model = H.recordModel([null, true, 3, { a: 1 }]);
  assert.equal(model.kind, "list");
  assert.deepEqual(model.items.slice(0, 3).map(item => item.text), ["Not recorded", "Yes", "3"]);
  assert.equal(model.items[3].label, "Record 4");
});

test("plain-English event prose: ages, transition names, titles and coverage words", () => {
  assert.equal(H.agePhrase(31), "31 seconds");
  assert.equal(H.agePhrase(1), "1 second");
  assert.equal(H.agePhrase(600), "10 minutes");
  assert.equal(H.agePhrase(130215.8), "36 hours");
  assert.equal(H.agePhrase(5 * 86400), "5 days");
  assert.equal(H.agePhrase(null), null);
  assert.equal(H.plainDetail("The last reading was 74.8 psi, recorded 130215.8 seconds before this evaluation. This rule requires a reading no older than 35 seconds."),
    "The last reading was 74.8 psi, recorded 36 hours before this evaluation. This rule requires a reading no older than 35 seconds.");
  const note = H.monitoringNote({ type: "x", monitoring_note: { title: "Monitoring paused", detail: "recorded 7200 seconds before" } });
  assert.deepEqual(note, { title: "Monitoring paused", detail: "recorded 2 hours before" });
  const paused = H.monitoringNote({ type: "evidence_inconclusive", assessment: { state: "unavailable", current: { value: 74.8, unit: "psi", effective_age_seconds: 130215.8 } } });
  assert.match(paused.detail, /recorded 36 hours before this evaluation/);
  const rows = H.timelineModel([{ id: 1, type: "deescalated", at: null }, { id: 2, type: "notification_repeat_due", at: null }, { id: 3, type: "context_updated", at: null }]).rows;
  assert.deepEqual(rows.map(r => r.title), ["Warning eased", "Reminder due", "Context updated"]);
  assert.equal(H.titleText("b-can interface status flagged during polling"), "B-CAN interface status flagged during polling");
  assert.equal(H.eventRowModel({ id: 9, title: "c-can and can-ch quiet", opened_at: null, status: "resolved", evidence_state: "normal" }).title, "C-CAN and CAN-CH quiet");
  assert.equal(H.headerModel("detail", { event: { id: 9, title: "b-can flagged" } }).title, "B-CAN flagged");
  assert.equal(H.coverageText("retained_partial_coverage"), "Saved, possibly with gaps");
  assert.equal(H.coverageText("truncated_at_sample_limit"), "Partial — sample limit reached");
});

test("legacy evaluator reasons are shown in plain words; saved text is untouched", () => {
  assert.equal(H.plainDetail("persistent history-relative deviation"), "Stayed outside its usual range long enough to confirm");
  assert.equal(H.plainDetail("current value is inside the learned relative-deviation band"), "Back inside its usual range");
  assert.equal(H.plainDetail("something new from the broker"), "something new from the broker");
  const model = H.timelineModel([{ id: 1, type: "escalated", at: "2026-09-21T16:55:41Z", assessment: { reason: "persistent history-relative deviation" } }]);
  assert.equal(model.rows[0].detail, "Stayed outside its usual range long enough to confirm");
});

test("recovery progress renders legacy counts and tiered timed recovery, never undefined", () => {
  assert.equal(H.recoveryText({ count: 2, required: 3 }), "2 of 3 comparable normal readings");
  assert.equal(H.recoveryText({ held_seconds: 245, required_seconds: 600 }), "4 of 10 minutes held normal");
  assert.equal(H.recoveryText({}), null);
  assert.equal(H.recoveryText(null), null);
});
