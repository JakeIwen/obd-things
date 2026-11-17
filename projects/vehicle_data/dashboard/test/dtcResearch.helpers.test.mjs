import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AI_LINE_TITLE, REASON_TEXT, RESEARCH, TEXT, askBody, cancelBody, codePath, encodedLength, errorText, itemKey, needsRunFetch, newRequestId,
  progressLine, progressPath, questionProblem, researchButtonText, researchIndex, researchable, rowExtraLine, runFooter, runOption,
  runPath, runPhase, shouldPoll, startBody, turnRows,
} from "../src/dtcResearch.helpers.js";
import { refreshResearchList, researchItems, researchStatus } from "../src/dtcResearch.js";
import { createStorage } from "../src/assistantAccess.js";
import vocabulary from "../../dtc_research.json" with { type: "json" };

const NOW = Date.parse("2026-10-28T20:30:00Z");
const RUN = "a".repeat(32);

test("the contract is the shared vocabulary file, not a copy", () => {
  assert.deepEqual({ ...RESEARCH.routes }, vocabulary.routes);
  assert.deepEqual({ ...RESEARCH.states }, vocabulary.states);
  assert.deepEqual({ ...RESEARCH.turnStates }, vocabulary.turnStates);
  assert.deepEqual({ ...RESEARCH.modes }, vocabulary.modes);
  assert.deepEqual({ ...RESEARCH.summaryNotes }, vocabulary.summaryNotes);
  assert.equal(RESEARCH.progressPollMs, vocabulary.progressPollMs);
  assert.equal(RESEARCH.modelsPollMs, vocabulary.modelsPollMs);
  assert.equal(RESEARCH.modelsPollMax, vocabulary.modelsPollMax);
  assert.equal(RESEARCH.questionMax, vocabulary.questionMax);
  assert.equal(RESEARCH.questionEncodedBytes, vocabulary.sizes.questionEncodedBytes);
  assert.equal(RESEARCH.summaryMax, vocabulary.summaryMax);
  assert.equal(RESEARCH.maxTurns, vocabulary.maxTurns);
  assert.equal(RESEARCH.moduleKeyRe.source, vocabulary.patterns.moduleKey);
  assert.equal(RESEARCH.codeRe.source, vocabulary.patterns.code);
  assert.equal(RESEARCH.runIdRe.source, vocabulary.patterns.runId);
  assert.equal(RESEARCH.requestIdRe.source, vocabulary.patterns.requestId);
  assert.equal(progressPath(RUN), RESEARCH.routes.runPrefix + RUN + vocabulary.routes.progressSuffix);
});

test("identity and routes only accept what the service accepts", () => {
  assert.equal(researchable("bcm_ccan", "B162E-15"), true);
  assert.equal(researchable("bcm_ccan", "C1418"), true);
  for (const [m, c] of [["BCM", "B162E-15"], ["bcm_ccan", "b162e-15"], ["bcm_ccan", "X162E-15"], ["bcm_ccan", "B162E-15/../x"], ["", "B162E-15"], ["bcm_ccan", null]]) {
    assert.equal(researchable(m, c), false, `${m} ${c}`);
  }
  assert.equal(codePath("bcm_ccan", "B162E-15"), "/v1/assistant/dtc-research/code/bcm_ccan/B162E-15");
  assert.throws(() => codePath("bcm_ccan", "../x"));
  assert.equal(runPath(RUN), "/v1/assistant/dtc-research/run/" + RUN);
  assert.equal(progressPath(RUN), "/v1/assistant/dtc-research/run/" + RUN + "/progress");
  assert.throws(() => runPath("nope"));
});

test("request bodies are exactly the contract's fields", () => {
  const id = "0123456789abcdef0123456789abcdef";
  const target = { module_key: "bcm_ccan", fca_display: "B162E-15", meaning: "ignored extra" };
  assert.deepEqual(startBody(target, { provider: "claude", model: "opus", effort: "high" }, id), {
    action: "start", module_key: "bcm_ccan", fca_display: "B162E-15", provider: "claude", model: "opus", effort: "high", request_id: id,
  });
  assert.equal(startBody(target, { provider: "claude", model: "haiku", effort: null }, id).effort, null, "a model with no efforts sends null");
  assert.deepEqual(askBody(RUN, "  why?  ", id), { action: "ask", run_id: RUN, question: "why?", request_id: id });
  assert.deepEqual(cancelBody(RUN, id), { action: "cancel", run_id: RUN, request_id: id });
  assert.match(newRequestId((n) => "ab".repeat(n)), RESEARCH.requestIdRe);
});

test("question limits: characters, then the service's encoded bytes", () => {
  assert.equal(questionProblem("ok"), null);
  assert.match(questionProblem("   "), /Type a question/);
  assert.equal(questionProblem("a".repeat(2000)), null, "2000 ASCII characters are under both limits");
  assert.match(questionProblem("a".repeat(2001)), /2000 characters \(now 2001\)/);
  // Under the character limit but over the 12288-byte limit: 1024 emoji are 1024 characters and 12290 encoded bytes.
  assert.equal(questionProblem("\u{1F600}".repeat(1023)), null, "1023 emoji are 12278 encoded bytes, under the limit");
  assert.equal(questionProblem("\u{1F600}".repeat(1024)), "That question is too large to send; shorten it.");
  assert.equal(questionProblem("\u{1F600}".repeat(2000)), "That question is too large to send; shorten it.", "over the bytes, not the characters");
  // Over the character limit: the count is characters (code points), not UTF-16 units, so 2001 emoji are refused on count.
  assert.match(questionProblem("\u{1F600}".repeat(2001)), /2000 characters \(now 2001\)/);
});

test("encoded length matches the service's json.dumps count", () => {
  assert.equal(encodedLength("a"), 3, "two quotes and one byte");
  assert.equal(encodedLength("é"), 8, "one non-ASCII character is six bytes");
  assert.equal(encodedLength("\u{1F600}"), 14, "a non-BMP character is 12 bytes");
  assert.equal(encodedLength("line\n\"q\"\\"), 15, "four letters, then \\n, two quotes, q, and a backslash: 4 + 2 + 2 + 1 + 2 + 2 plus the outer quotes");
  assert.equal(encodedLength("\x7f"), 8, "DEL is escaped by the service");
});

test("every reason in the contract has an owner-facing sentence; unknown ones fall back to the detail", () => {
  const reasons = [...Object.keys(vocabulary.errorReasons), ...vocabulary.turnReasons];
  assert.ok(reasons.length > 20, "the walk covers the contract's reason lists");
  for (const r of reasons) assert.ok(REASON_TEXT[r] && REASON_TEXT[r].length < 120, r);
  assert.match(REASON_TEXT.turn_limit, new RegExp(`limit of ${vocabulary.maxTurns} turns`));
  assert.equal(errorText("busy", "A warning chat is answering a question."), "A warning chat is answering a question.", "busy shows its detail");
  assert.equal(errorText("provider_unavailable", "Claude is not signed in for research yet"), "Claude is not signed in for research yet");
  assert.equal(errorText("busy", ""), REASON_TEXT.busy);
  assert.equal(errorText("timeout", "detail words"), REASON_TEXT.timeout, "a turn reason uses the table");
  assert.equal(errorText("brand_new_reason", "The Pi says so."), "The Pi says so.");
  assert.equal(errorText(undefined, undefined), TEXT.unreachable);
});

test("row text: AI line, researching, nothing; the panel button follows the run", () => {
  assert.equal(rowExtraLine(null), null);
  assert.equal(rowExtraLine({ busy: false, summary: null }), null);
  assert.equal(rowExtraLine({ busy: false, summary: "Likely the LED bulbs." }), "AI: Likely the LED bulbs.");
  assert.equal(rowExtraLine({ busy: true, summary: "older" }), "AI: researching…");
  assert.equal(researchButtonText(null), "Research with AI");
  assert.equal(researchButtonText({ run_id: RUN }), "Open research");
  assert.match(AI_LINE_TITLE, /not a reviewed meaning/);
});

test("researchIndex keys items by code and ignores a refusal", () => {
  const index = researchIndex({ available: true, items: [{ module_key: "bcm_ccan", fca_display: "B162E-15", summary: "s" }, { bad: 1 }] });
  assert.equal(index.size, 1);
  assert.equal(index.get(itemKey("bcm_ccan", "B162E-15")).summary, "s");
  assert.equal(researchIndex({ available: false }).size, 0);
  assert.equal(researchIndex(null).size, 0);
});

test("polling: fetch the run only when turn count, state or busy changed", () => {
  const known = { turn_count: 1, state: "running", busy: true };
  assert.equal(needsRunFetch(known, { turn_count: 1, state: "running", busy: true }), false);
  assert.equal(needsRunFetch(known, { turn_count: 2, state: "running", busy: true }), true);
  assert.equal(needsRunFetch(known, { turn_count: 1, state: "done", busy: false }), true);
  assert.equal(needsRunFetch(known, { turn_count: 1, state: "running", busy: false }), true);
  assert.equal(needsRunFetch(null, { turn_count: 1 }), true);
  assert.equal(needsRunFetch(known, null), false);
  assert.equal(shouldPoll({ busy: true, open: true, visible: true }), true);
  assert.equal(shouldPoll({ busy: true, open: true, visible: false }), false, "paused while the page is hidden");
  assert.equal(shouldPoll({ busy: true, open: false, visible: true }), false, "stopped when the dialog closes");
  assert.equal(shouldPoll({ busy: false, open: true, visible: true }), false);
  assert.equal(RESEARCH.progressPollMs, 1000);
});

test("progress line and run phase", () => {
  assert.equal(progressLine({ progress: "Searching the web · 3 searches, 1 page", elapsed_s: 47 }), "Searching the web · 3 searches, 1 page · 47 s");
  assert.equal(progressLine({ progress: null, elapsed_s: null }), TEXT.working);
  assert.equal(progressLine(null), TEXT.working);
  assert.equal(runPhase({ state: "done" }), "done");
  assert.equal(runPhase({ state: "weird" }), "failed");
});

const turns = [
  { role: "assistant", kind: "report", text: "## What it means\nx", at: "2026-10-28T20:00:00Z", state: "complete", mode: "initial" },
  { role: "user", kind: "question", text: "Why?", at: "2026-10-28T20:05:00Z" },
  { role: "assistant", kind: "answer", text: "Because.", at: "2026-10-28T20:06:00Z", state: "complete", mode: "replayed" },
  { role: "user", kind: "question", text: "More?", at: "2026-10-28T20:07:00Z" },
  { role: "assistant", kind: "answer", text: null, at: "2026-10-28T20:07:30Z", state: "failed", reason: "timeout", error: "x", mode: "resumed" },
  { role: "assistant", kind: "answer", text: null, at: "2026-10-28T20:08:00Z", state: "running" },
];

test("turn rows: report first, a replay note under a replayed answer, failures as sentences", () => {
  const rows = turnRows({ turns }, NOW);
  assert.deepEqual(rows.map((r) => r.heading), ["Report", "You", "Answer", "You", "Answer"], "the running turn has no row");
  assert.equal(rows[0].markdown, true);
  assert.equal(rows[1].markdown, false, "a question is plain text");
  assert.equal(rows[2].note, TEXT.replayNote);
  assert.equal(rows[0].note, null);
  assert.equal(rows[4].error, REASON_TEXT.timeout);
  assert.deepEqual(turnRows(null, NOW), []);
});

test("turn times use the Codes card's formatter", () => {
  const row = turnRows({ turns: [turns[0]] }, NOW)[0];
  assert.match(row.at, /^\d{1,2}:\d{2} [ap]m$|^[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [ap]m$/);
});

test("footer: model, effort, time, duration; summary and format notes stay in the footer", () => {
  const run = { model: "opus", effort: "high", updated_at: "2026-10-28T20:00:00Z", duration_s: 214, summary_note: "truncated", format_warnings: ["Sources"] };
  const footer = runFooter(run, "Opus", NOW);
  assert.match(footer.line, /^Opus · high · .* · 3 min$/);
  assert.deepEqual(footer.notes, [TEXT.summaryTruncated, "The report is missing: Sources."]);
  assert.deepEqual(runFooter({ model: "m", summary_note: "missing", format_warnings: [] }, null, NOW).notes, [TEXT.summaryMissing]);
  assert.equal(runFooter({ model: "haiku", effort: null }, null, NOW).line, "haiku");
  assert.deepEqual(runFooter(null, null, NOW), { line: "", notes: [] });
});

test("runs picker labels", () => {
  const option = runOption({ run_id: RUN, created_at: "2026-10-28T20:00:00Z", model: "opus", effort: "high", state: "done" }, NOW);
  assert.equal(option.id, RUN);
  assert.match(option.label, / · opus · high$/);
  assert.match(runOption({ run_id: RUN, model: "m", state: "failed" }, NOW).label, /m · failed$/);
});

function memoryStorage(values = {}) {
  const data = { ...values };
  return { getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = v; }, removeItem: (k) => { delete data[k]; } };
}

test("the list fetch needs an access code, fills the signals, and a 404 hides the controls", async () => {
  const key = "k".repeat(40);
  let calls = [];
  const ok = (body, status = 200) => async (url, init) => { calls.push({ url, init }); return { ok: status < 300, status, json: async () => body }; };
  assert.equal(await refreshResearchList(ok({}), createStorage(memoryStorage())), false);
  assert.equal(calls.length, 0, "no access code, no request");
  const storage = createStorage(memoryStorage({ "van-warning-chat.access": key }));
  const items = [{ module_key: "bcm_ccan", fca_display: "B162E-15", summary: "s", busy: false }];
  assert.equal(await refreshResearchList(ok({ available: true, items }), storage), true);
  assert.equal(calls[0].url, "/v1/assistant/dtc-research");
  assert.equal(calls[0].init.headers["X-Van-Assistant-Key"], key);
  assert.equal(researchStatus.value, "ready");
  assert.equal(researchItems.value.size, 1);
  assert.equal(await refreshResearchList(ok({ available: false, reason: "worker_unavailable" }, 503), storage), false);
  assert.equal(researchItems.value.size, 1, "a failed refresh keeps the last list");
  assert.equal(await refreshResearchList(ok({ available: false, detail: "Unknown assistant request" }, 404), storage), false);
  assert.equal(researchStatus.value, "missing");
  assert.equal(researchItems.value.size, 0);
});
