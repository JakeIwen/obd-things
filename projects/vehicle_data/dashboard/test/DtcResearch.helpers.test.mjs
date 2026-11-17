import { test } from "node:test";
import assert from "node:assert/strict";
import { createResearchController } from "../src/dialogs/DtcResearch.helpers.js";
import { createStorage } from "../src/assistantAccess.js";
import { REASON_TEXT, RESEARCH, TEXT } from "../src/dtcResearch.helpers.js";

const KEY = "k".repeat(40);
const RUN = "a".repeat(32);
const TARGET = { module_key: "bcm_ccan", fca_display: "B162E-15", code: "B162E-15", meaning: "Right low-beam circuit" };
const CATALOG = {
  available: true, refreshing: false, default: { provider: "claude", model: "opus", effort: "high" },
  groups: [{ provider: "claude", label: "Claude", available: true, models: [{ id: "opus", label: "Opus", efforts: ["low", "high"], default_effort: "high" }] }],
};
const turn = (over) => ({ role: "assistant", kind: "report", text: "## What it means\nx", at: "2026-10-28T20:00:00Z", state: "complete", mode: "initial", ...over });
const run = (over = {}) => ({
  run_id: RUN, module_key: "bcm_ccan", fca_display: "B162E-15", provider: "claude", model: "opus", effort: "high", state: "done", busy: false,
  summary: "s", turn_count: 1, can_continue: true, created_at: "2026-10-28T20:00:00Z", updated_at: "2026-10-28T20:00:00Z", duration_s: 60,
  turns: [turn()], ...over,
});

/** A scripted Pi: routes by "METHOD path"; each handler gets the parsed body and returns [status, body]. */
function pi(handlers) {
  const log = [];
  const fetch = async (url, init) => {
    const method = init.method;
    const body = init.body ? JSON.parse(init.body) : null;
    log.push({ method, url, body, headers: init.headers });
    const handler = handlers[method + " " + url] || handlers[method + " *"];
    if (!handler) return { ok: false, status: 404, json: async () => ({ available: false, detail: "Unknown assistant request" }) };
    const [status, reply] = handler(body, log.length);
    return { ok: status < 300, status, json: async () => reply };
  };
  return { fetch, log };
}

function setup(handlers, { key = KEY, visible = true } = {}) {
  const server = pi(handlers);
  const store = {};
  const backend = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } };
  if (key) store["van-warning-chat.access"] = key;
  const timers = [];
  const state = { visible, finished: 0, snaps: [] };
  const ctrl = createResearchController({
    fetch: server.fetch, storage: createStorage(backend), choiceBackend: backend, random: (n) => "ab".repeat(n),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    isVisible: () => state.visible, now: () => Date.parse("2026-10-28T20:30:00Z"),
    onChange: (snap) => state.snaps.push(snap), onFinished: () => { state.finished += 1; },
  });
  const tick = async () => {
    const t = timers.filter((x) => x.fn).pop();
    assert.ok(t, "a timer is pending");
    const fn = t.fn;
    t.fn = null;
    fn();
    await settle();
  };
  const pending = () => timers.filter((x) => x.fn);
  return { ctrl, server, store, state, tick, pending, timers };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
const routes = (extra = {}) => ({
  "GET /v1/assistant/models": () => [200, CATALOG],
  "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15": () => [200, { available: true, runs: [], selected: null }],
  ...extra,
});

test("no access code: the dialog asks for it, and pairing then loads", async () => {
  const t = setup(routes(), { key: "" });
  t.ctrl.open(TARGET);
  assert.equal(t.ctrl.snapshot().phase, "pairing");
  assert.equal(t.server.log.length, 0);
  assert.equal(t.ctrl.pair("short"), false);
  assert.match(t.ctrl.snapshot().pairingError, /complete/);
  assert.equal(t.ctrl.pair(KEY), true);
  await settle();
  assert.equal(t.ctrl.snapshot().phase, "setup");
  assert.equal(t.server.log[0].headers["X-Van-Assistant-Key"], KEY);
  assert.equal(t.store["van-warning-chat.access"], KEY);
});

test("a 401 sends the browser back to pairing and forgets the code", async () => {
  const t = setup(routes({ "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15": () => [401, { available: false, detail: "no" }] }));
  t.ctrl.open(TARGET);
  await settle();
  assert.equal(t.ctrl.snapshot().phase, "pairing");
  assert.equal(t.store["van-warning-chat.access"], undefined);
});

test("a Pi without the research routes: unavailable, no controls", async () => {
  const t = setup({});
  t.ctrl.open(TARGET);
  await settle();
  assert.equal(t.ctrl.snapshot().phase, "unavailable");
  assert.equal(t.ctrl.snapshot().canStart, false);
});

test("no run yet: setup with the default choice; start sends the exact body and shows the run", async () => {
  const started = run({ state: "running", busy: true, summary: null, can_continue: false, turns: [turn({ text: null, state: "running" })], progress: "Starting" });
  const t = setup(routes({ "POST /v1/assistant/dtc-research": () => [200, { available: true, run: started }] }));
  t.ctrl.open(TARGET);
  await settle();
  let snap = t.ctrl.snapshot();
  assert.equal(snap.phase, "setup");
  assert.deepEqual(snap.choice, { provider: "claude", model: "opus", effort: "high" });
  assert.equal(snap.canStart, true);
  t.ctrl.pickEffort("low");
  assert.equal(t.ctrl.snapshot().choice.effort, "low");
  assert.equal(t.store["van-dtc-research.choice"].includes('"low"'), true, "the choice is remembered");
  assert.equal(await t.ctrl.start(), true);
  const post = t.server.log.find((e) => e.method === "POST");
  assert.deepEqual(post.body, { action: "start", module_key: "bcm_ccan", fca_display: "B162E-15", provider: "claude", model: "opus", effort: "low",
    request_id: "ab".repeat(16) });
  snap = t.ctrl.snapshot();
  assert.equal(snap.phase, "running");
  assert.equal(snap.busy, true);
  assert.equal(snap.showStop, true);
  assert.equal(snap.canAsk, false);
  assert.equal(t.pending().length, 1, "polling starts");
  assert.equal(t.pending()[0].ms, 1000);
});

test("polling: progress every second, the run only when it changed, and the list is refreshed when it finishes", async () => {
  const busy = run({ state: "running", busy: true, summary: null, can_continue: false, turns: [turn({ text: null, state: "running" })] });
  let progress = { run_id: RUN, state: "running", busy: true, progress: "Searching the web · 1 search", elapsed_s: 5, turn_count: 1 };
  const t = setup(routes({
    "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15": () => [200, { available: true, runs: [busy], selected: busy }],
    ["GET /v1/assistant/dtc-research/run/" + RUN + "/progress"]: () => [200, { available: true, progress }],
    ["GET /v1/assistant/dtc-research/run/" + RUN]: () => [200, { available: true, run: run() }],
  }));
  t.ctrl.open(TARGET);
  await settle();
  assert.equal(t.ctrl.snapshot().phase, "running");
  await t.tick();
  assert.equal(t.server.log.filter((e) => e.url.endsWith("/progress")).length, 1);
  assert.equal(t.server.log.filter((e) => e.url === "/v1/assistant/dtc-research/run/" + RUN).length, 0, "unchanged: no run fetch");
  assert.equal(t.ctrl.snapshot().progress, "Searching the web · 1 search · 5 s");
  progress = { ...progress, state: "done", busy: false, turn_count: 1 };
  await t.tick();
  assert.equal(t.server.log.filter((e) => e.url === "/v1/assistant/dtc-research/run/" + RUN).length, 1);
  const snap = t.ctrl.snapshot();
  assert.equal(snap.phase, "done");
  assert.equal(snap.busy, false);
  assert.equal(snap.canAsk, true);
  assert.equal(t.state.finished, 1, "busy to not busy refreshes the list");
  assert.equal(t.pending().length, 0, "polling stops");
});

test("polling pauses while the page is hidden and resumes with a poll", async () => {
  const busy = run({ state: "running", busy: true, can_continue: false, turns: [turn({ text: null, state: "running" })] });
  const t = setup(routes({
    "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15": () => [200, { available: true, runs: [busy], selected: busy }],
    ["GET /v1/assistant/dtc-research/run/" + RUN + "/progress"]: () => [200, { available: true, progress: { run_id: RUN, state: "running", busy: true, turn_count: 1 } }],
  }));
  t.ctrl.open(TARGET);
  await settle();
  assert.equal(t.pending().length, 1);
  t.state.visible = false;
  t.ctrl.visibilityChanged();
  assert.equal(t.pending().length, 0);
  t.state.visible = true;
  t.ctrl.visibilityChanged();
  await settle();
  assert.equal(t.server.log.filter((e) => e.url.endsWith("/progress")).length, 1);
  assert.equal(t.pending().length, 1);
  t.ctrl.close();
  assert.equal(t.pending().length, 0, "closing stops polling");
  assert.equal(t.state.finished, 1);
});

test("done: a follow-up is sent with the run id, a busy follow-up blocks another, and Stop sends cancel", async () => {
  const asking = run({ busy: true, turn_count: 3, can_continue: false, turns: [turn(), { role: "user", kind: "question", text: "Why?", at: "2026-10-28T20:05:00Z" },
    turn({ kind: "answer", text: null, state: "running" })] });
  const cancelled = run({ turn_count: 3, turns: [turn(), asking.turns[1], turn({ kind: "answer", text: null, state: "cancelled", reason: "cancelled" })] });
  const t = setup(routes({
    "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15": () => [200, { available: true, runs: [run()], selected: run() }],
    "POST /v1/assistant/dtc-research": (body) => [200, { available: true, run: body.action === "ask" ? asking : cancelled }],
  }));
  t.ctrl.open(TARGET);
  await settle();
  assert.equal(t.ctrl.snapshot().canAsk, true);
  assert.equal(await t.ctrl.ask("   "), false);
  assert.match(t.ctrl.snapshot().error, /Type a question/);
  assert.equal(await t.ctrl.ask("Why?"), true);
  assert.deepEqual(t.server.log.at(-1).body, { action: "ask", run_id: RUN, question: "Why?", request_id: "ab".repeat(16) });
  let snap = t.ctrl.snapshot();
  assert.equal(snap.phase, "done");
  assert.equal(snap.busy, true, "a done run is busy while a follow-up runs");
  assert.equal(snap.canAsk, false);
  assert.equal(snap.showStop, true);
  assert.equal(await t.ctrl.cancel(), true);
  assert.deepEqual(t.server.log.at(-1).body, { action: "cancel", run_id: RUN, request_id: "ab".repeat(16) });
  snap = t.ctrl.snapshot();
  assert.equal(snap.busy, false);
  assert.equal(snap.turns.at(-1).error, REASON_TEXT.cancelled);
});

test("failed and cancelled runs say why and cannot be continued", async () => {
  const failed = run({ state: "failed", can_continue: false, reason: "provider_error", error: "Provider said no.", turns: [turn({ text: null, state: "failed", reason: "auth" })] });
  const t = setup(routes({ "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15": () => [200, { available: true, runs: [failed], selected: failed }] }));
  t.ctrl.open(TARGET);
  await settle();
  let snap = t.ctrl.snapshot();
  assert.equal(snap.phase, "failed");
  assert.equal(snap.runError, REASON_TEXT.provider_error);
  assert.equal(snap.turns[0].error, REASON_TEXT.auth);
  assert.equal(snap.canAsk, false);
  assert.equal(snap.canNew, true);
  const stopped = setup(routes({ "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15": () => [200, { available: true, runs: [run({ state: "cancelled", can_continue: false })], selected: run({ state: "cancelled", can_continue: false }) }] }));
  stopped.ctrl.open(TARGET);
  await settle();
  snap = stopped.ctrl.snapshot();
  assert.equal(snap.phase, "cancelled");
  assert.equal(snap.runError, REASON_TEXT.cancelled);
});

test("API errors become the table's sentence; a lost answer retries with the same request id", async () => {
  let attempts = 0;
  const t = setup(routes({
    "POST /v1/assistant/dtc-research": () => {
      attempts += 1;
      return attempts === 1 ? [429, { available: false, reason: "rate_limited", detail: "10 starts in 60 s" }] : [200, { available: true, run: run({ state: "running", busy: true, turns: [turn({ text: null, state: "running" })] }) }];
    },
  }));
  t.ctrl.open(TARGET);
  await settle();
  assert.equal(await t.ctrl.start(), false);
  assert.equal(t.ctrl.snapshot().error, REASON_TEXT.rate_limited);
  assert.equal(t.ctrl.snapshot().phase, "setup");
  assert.equal(await t.ctrl.start(), true);
  const posts = t.server.log.filter((e) => e.method === "POST");
  assert.equal(posts[0].body.request_id, posts[1].body.request_id, "the same change keeps its request id");
  assert.equal(t.ctrl.snapshot().error, null);
});

test("the catalog: refreshing disables Start and re-fetches every 2 s, at most 15 times", async () => {
  let fetches = 0;
  const t = setup(routes({ "GET /v1/assistant/models": () => { fetches += 1; return [200, { ...CATALOG, refreshing: true }]; } }));
  t.ctrl.open(TARGET);
  await settle();
  assert.equal(t.ctrl.snapshot().refreshing, true);
  assert.equal(t.ctrl.snapshot().canStart, false);
  assert.equal(t.pending()[0].ms, 2000);
  for (let i = 0; i < 20 && t.pending().length; i += 1) await t.tick();
  assert.equal(fetches, 16, "the first fetch plus 15 re-fetches");
  assert.equal(t.pending().length, 0);
});

test("a remembered choice the catalog dropped falls back to the default; a model with no efforts starts with effort null", async () => {
  const catalog = { ...CATALOG, groups: [{ provider: "claude", label: "Claude", available: true, models: [
    { id: "opus", label: "Opus", efforts: ["low", "high"], default_effort: "high" }, { id: "haiku", label: "Haiku", efforts: [], default_effort: null }] }] };
  const t = setup(routes({ "GET /v1/assistant/models": () => [200, catalog], "POST /v1/assistant/dtc-research": () => [200, { available: true, run: run({ busy: true, state: "running" }) }] }));
  t.store["van-dtc-research.choice"] = JSON.stringify({ provider: "codex", model: "gone", effort: "high" });
  t.ctrl.open(TARGET);
  await settle();
  assert.deepEqual(t.ctrl.snapshot().choice, { provider: "claude", model: "opus", effort: "high" });
  t.ctrl.setChoice({ provider: "claude", model: "haiku", effort: null });
  await t.ctrl.start();
  assert.equal(t.server.log.find((e) => e.method === "POST").body.effort, null);
  assert.equal(t.ctrl.setChoice({ provider: "claude", model: "opus", effort: "ultra" }), undefined, "an invalid choice is ignored");
  assert.equal(t.ctrl.snapshot().choice.model, "haiku");
});

test("no usable model: Start stays off and the picker says so", async () => {
  const none = { available: true, refreshing: false, default: null, groups: [{ provider: "claude", label: "Claude", available: false, reason: "Claude is not signed in for research yet", models: [] }] };
  const t = setup(routes({ "GET /v1/assistant/models": () => [200, none] }));
  t.ctrl.open(TARGET);
  await settle();
  const snap = t.ctrl.snapshot();
  assert.equal(snap.noModels, true);
  assert.equal(snap.canStart, false);
});

test("Runs: pick an earlier run; New research opens setup and Back returns", async () => {
  const OLD = "d".repeat(32);
  const older = run({ run_id: OLD, model: "sonnet", created_at: "2026-10-27T20:00:00Z" });
  const t = setup(routes({
    "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15": () => [200, { available: true, runs: [run(), older], selected: run() }],
    ["GET /v1/assistant/dtc-research/run/" + OLD]: () => [200, { available: true, run: older }],
  }));
  t.ctrl.open(TARGET);
  await settle();
  assert.equal(t.ctrl.snapshot().runs.length, 2);
  await t.ctrl.selectRun(OLD);
  assert.equal(t.ctrl.snapshot().selectedRunId, OLD);
  t.ctrl.newResearch();
  await settle();
  assert.equal(t.ctrl.snapshot().phase, "setup");
  assert.equal(t.ctrl.snapshot().canBack, true);
  t.ctrl.back();
  assert.equal(t.ctrl.snapshot().phase, "done");
});

test("footer carries the model label, notes and the replay note shows under a replayed answer", async () => {
  const replayed = run({ summary_note: "missing", format_warnings: ["Sources"], turn_count: 3,
    turns: [turn(), { role: "user", kind: "question", text: "q", at: "2026-10-28T20:05:00Z" }, turn({ kind: "answer", mode: "replayed", text: "a" })] });
  const t = setup(routes({ "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15": () => [200, { available: true, runs: [replayed], selected: replayed }] }));
  t.ctrl.open(TARGET);
  await settle();
  const snap = t.ctrl.snapshot();
  assert.match(snap.footer.line, /^Opus · high · /);
  assert.deepEqual(snap.footer.notes, [TEXT.summaryMissing, "The report is missing: Sources."]);
  assert.equal(snap.turns[2].note, TEXT.replayNote);
});

/* ---- review fixes: request deadline, kept draft, reasonless replies ---- */

const CODE_PATH = "GET /v1/assistant/dtc-research/code/bcm_ccan/B162E-15";
const PROGRESS = "GET /v1/assistant/dtc-research/runs/" + RUN + "/progress";
const busyRun = () => run({ state: "running", busy: true, can_continue: false });

/** A controller whose fetch can be switched to never answering; timers are recorded, not run. */
function stallable(handlers) {
  const server = pi(handlers);
  const timers = [];
  const store = { "van-warning-chat.access": KEY };
  const backend = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } };
  const h = { stall: false, signals: [], timers };
  h.ctrl = createResearchController({
    fetch: (url, init) => { h.signals.push(init.signal); return h.stall ? new Promise(() => {}) : server.fetch(url, init); },
    storage: createStorage(backend), choiceBackend: backend, random: (n) => "ab".repeat(n),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    now: () => Date.parse("2026-10-28T20:30:00Z"),
  });
  h.fire = (ms) => timers.filter((x) => x.fn && x.ms === ms).pop().fn();
  return h;
}

test("a request that never answers times out at the contract's deadline; polling carries on", async () => {
  const h = stallable(routes({
    [CODE_PATH]: () => [200, { available: true, runs: [busyRun()], selected: busyRun() }],
    [PROGRESS]: () => [200, { available: true, progress: { run_id: RUN, state: "running" } }],
  }));
  h.ctrl.open(TARGET);
  await settle();
  assert.equal(h.ctrl.snapshot().phase, "running");
  h.stall = true;
  h.fire(RESEARCH.progressPollMs);
  await settle();
  h.fire(RESEARCH.requestTimeoutMs);
  await settle();
  assert.equal(h.signals.at(-1).aborted, true);
  assert.equal(h.ctrl.snapshot().error, TEXT.timedOut);
  assert.ok(h.timers.some((x) => x.fn && x.ms === RESEARCH.progressPollMs), "the next poll is scheduled");
});

test("a stalled POST says the outcome is unknown and frees the dialog", async () => {
  const h = stallable(routes());
  h.ctrl.open(TARGET);
  await settle();
  h.stall = true;
  const started = h.ctrl.start();
  await settle();
  h.fire(RESEARCH.requestTimeoutMs);
  assert.equal(await started, false);
  const snap = h.ctrl.snapshot();
  assert.equal(snap.error, TEXT.timedOutPost);
  assert.equal(snap.requesting, false);
});

test("a refused follow-up keeps the draft; an accepted one clears it", async () => {
  let refuse = true;
  const t = setup(routes({
    [CODE_PATH]: () => [200, { available: true, runs: [run()], selected: run() }],
    "POST /v1/assistant/dtc-research": () => (refuse
      ? [503, { available: false, reason: "provider_unavailable", detail: "Provider is down" }]
      : [200, { available: true, run: busyRun() }]),
  }));
  t.ctrl.open(TARGET);
  await settle();
  t.ctrl.setDraft("Why is it intermittent?");
  assert.equal(await t.ctrl.ask(t.ctrl.snapshot().draft), false);
  assert.equal(t.ctrl.snapshot().draft, "Why is it intermittent?");
  refuse = false;
  assert.equal(await t.ctrl.ask(t.ctrl.snapshot().draft), true);
  assert.equal(t.ctrl.snapshot().draft, "");
});

test("replies with no reason still read as a sentence, never undefined", async () => {
  let n = 0;
  const t = setup(routes({
    [CODE_PATH]: () => [200, { available: true, runs: [run()], selected: run() }],
    "POST /v1/assistant/dtc-research": () => (++n === 1 ? [503, { available: false, detail: "Local assistant unavailable" }] : [400, {}]),
  }));
  t.ctrl.open(TARGET);
  await settle();
  assert.equal(await t.ctrl.ask("Why?"), false);
  assert.equal(t.ctrl.snapshot().error, "Local assistant unavailable");
  assert.equal(await t.ctrl.ask("Why?"), false);
  assert.equal(t.ctrl.snapshot().error, TEXT.unreachable);
});
