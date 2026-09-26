import { test, describe } from "node:test";
import assert from "node:assert/strict";

import * as H from "../src/dialogs/WarningChat.helpers.js";

// ---------- fixtures ----------

const CHAT_ID = "0123456789abcdef0123456789abcdef";
const WARNING_ID = "a".repeat(64);
const CODE = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEF";
const SETTINGS = { default_model: "gpt-6-astra", models: { "gpt-6-astra": ["low", "medium", "high"], "gpt-6-mini": ["medium"] } };
const STATUS_OK = { available: true, settings: SETTINGS, warnings: [], warning_config_error: null };

function turn(overrides) {
  return { user: "Explain this", assistant: null, error: null, state: "running", settings: null, ...overrides };
}
function chat(turns, extra) {
  return { id: CHAT_ID, title: "Coolant hot", event: { kind: "episode", id: "12" }, evidence_at: "2026-09-22T16:25:00+00:00", turns, ...extra };
}
const doneChat = () => chat([turn({ assistant: "It is fine.", state: "complete", settings: { resolved_model: "gpt-6-astra", effort: "high" } })]);

function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return { getItem: k => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, String(v)), removeItem: k => map.delete(k), map };
}

const fixedRandom = bytes => "ab".repeat(bytes);

/** Route table fake fetch: routes are [method, path|RegExp, handler(body) → {status, body}]. */
function fakeServer(routes) {
  const calls = [];
  const fetch = async (url, init) => {
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ method: init.method, url, headers: init.headers, body, cache: init.cache });
    const route = routes.find(([method, path]) => method === init.method && (path instanceof RegExp ? path.test(url) : path === url));
    const result = route ? await route[2](body, url) : { status: 404, body: { available: false, detail: "no route " + url } };
    return { status: result.status, ok: result.status < 300, json: async () => result.body };
  };
  return { fetch, calls };
}

/** Fake timers: `fire(ms)` runs every timer queued with that delay. */
function fakeTimers() {
  const queue = [];
  let seq = 0;
  return {
    setTimeout: (fn, ms) => { const id = ++seq; queue.push({ id, fn, ms }); return id; },
    clearTimeout: id => { const at = queue.findIndex(t => t.id === id); if (at >= 0) queue.splice(at, 1); },
    pending: ms => queue.filter(t => ms === undefined || t.ms === ms),
    fire(ms) { const due = queue.filter(t => t.ms === ms); for (const t of due) { this.clearTimeout(t.id); t.fn(); } return due.length; },
  };
}

const flush = async (n = 6) => { for (let i = 0; i < n; i += 1) await new Promise(r => setImmediate(r)); };

function makeController(routes, options = {}) {
  const server = fakeServer(routes);
  const timers = fakeTimers();
  const storage = memoryStorage(options.storage || { "van-warning-chat.access": CODE });
  const snapshots = [];
  const warningsChanged = [];
  const visible = { value: true };
  const ctrl = H.createChatController({
    fetch: server.fetch,
    sleep: async () => {},
    storage: H.createStorage(storage),
    random: fixedRandom,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    isVisible: () => visible.value,
    now: () => new Date(2026, 8, 22, 12, 0, 0),
    onChange: s => snapshots.push(s),
    onWarningsChanged: () => warningsChanged.push(1),
  });
  return { ctrl, server, timers, storage, snapshots, warningsChanged, visible, last: () => snapshots[snapshots.length - 1] };
}

const okRoutes = (chatResult, statusInfo = STATUS_OK) => [
  ["GET", H.STATUS_PATH, () => ({ status: 200, body: statusInfo })],
  ["POST", H.CHATS_PATH, () => ({ status: 200, body: chatResult })],
];

// ---------- storage and identity ----------

describe("storage and identity", () => {
  test("createStorage prefixes keys and swallows backend failures", () => {
    const backend = memoryStorage();
    const storage = H.createStorage(backend);
    storage.set("access", "x");
    assert.equal(backend.getItem("van-warning-chat.access"), "x");
    assert.equal(storage.get("access"), "x");
    storage.remove("access");
    assert.equal(storage.get("access"), null);
    const broken = H.createStorage({ getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } });
    assert.equal(broken.get("access"), null);
    assert.doesNotThrow(() => broken.set("access", "x"));
    assert.doesNotThrow(() => broken.remove("access"));
    assert.equal(H.createStorage(null).get("access"), null);
  });

  test("randomId yields lower-case hex of the requested byte count", () => {
    const id = H.randomId(32);
    assert.match(id, /^[a-f0-9]{64}$/);
    assert.match(H.randomId(16, { getRandomValues: arr => arr.fill(255) }), /^f{32}$/);
    assert.match(H.randomId(4, null), /^[a-f0-9]{8}$/);
  });

  test("ensureClientId keeps a valid stored id and replaces an invalid one", () => {
    const backend = memoryStorage({ "van-warning-chat.client": "not-hex" });
    const storage = H.createStorage(backend);
    const id = H.ensureClientId(storage, fixedRandom);
    assert.equal(id, "ab".repeat(32));
    assert.equal(backend.getItem("van-warning-chat.client"), id);
    assert.equal(H.ensureClientId(storage, () => "zz".repeat(32)), id, "existing valid id is kept");
  });

  test("access-code validation follows the header rule", () => {
    assert.equal(H.isValidAccessCode(CODE), true);
    assert.equal(H.isValidAccessCode("short"), false);
    assert.equal(H.isValidAccessCode("x".repeat(129)), false);
    assert.equal(H.isValidAccessCode("has space" + "x".repeat(40)), false);
    assert.equal(H.isValidAccessCode(null), false);
  });
});

// ---------- event selection and titles ----------

describe("event selection", () => {
  test("add-warning mode always targets the setup chat", () => {
    assert.deepEqual(H.eventSelector("add-warning", { eventId: 5 }), { kind: "setup", id: "new-warning" });
    assert.equal(H.dialogTitle("add-warning", { title: "ignored" }), "Add Early Warning");
  });
  test("explain mode maps the card context to the advisor kinds", () => {
    assert.deepEqual(H.eventSelector("explain", { eventId: 12 }), { kind: "episode", id: "12" });
    assert.deepEqual(H.eventSelector("explain", { assessment: { episode_id: 7, rule: "r" } }), { kind: "episode", id: "7" });
    assert.deepEqual(H.eventSelector("explain", { assessment: { incident_id: "q1" } }), { kind: "quality", id: "q1" });
    assert.deepEqual(H.eventSelector("explain", { assessment: { rule: "coolant_hot" } }), { kind: "assessment", id: "coolant_hot" });
    assert.deepEqual(H.eventSelector("explain", { event: { kind: "quality", id: 3 } }), { kind: "quality", id: "3" });
    assert.equal(H.eventSelector("explain", {}), null);
    assert.equal(H.eventSelector("explain", null), null);
  });
  test("title falls back to the assessment title, then the default", () => {
    assert.equal(H.dialogTitle("explain", { title: "RR tire low" }), "RR tire low");
    assert.equal(H.dialogTitle("explain", { assessment: { title: "Coolant" } }), "Coolant");
    assert.equal(H.dialogTitle("explain", { title: "  " }), "Warning explanation");
    // App.jsx DialogHost hands the Ask Codex event title over as `summary`.
    assert.equal(H.dialogTitle("explain", { eventId: 7, summary: "Coolant above baseline" }), "Coolant above baseline");
  });
  test("contextKey is stable across object identity and changes with the event", () => {
    assert.equal(H.contextKey("explain", { eventId: 1, title: "A" }), H.contextKey("explain", { eventId: "1", title: "A" }));
    assert.notEqual(H.contextKey("explain", { eventId: 1 }), H.contextKey("explain", { eventId: 2 }));
    assert.notEqual(H.contextKey("explain", { eventId: 1 }), H.contextKey("add-warning", { eventId: 1 }));
  });
});

// ---------- URL and body builders ----------

describe("requests", () => {
  test("headers carry the key and client id, requests are no-store JSON", () => {
    const { url, init } = H.buildRequest("POST", "/x", { a: 1 }, { key: "K", client: "C" });
    assert.equal(url, "/x");
    assert.equal(init.cache, "no-store");
    assert.equal(init.body, '{"a":1}');
    assert.deepEqual(init.headers, { "Content-Type": "application/json", "X-Van-Assistant-Key": "K", "X-Van-Chat-Client": "C" });
    assert.equal("body" in H.buildRequest("GET", "/x", undefined, {}).init, false);
    assert.equal(H.buildRequest("POST", "/x", {}, {}).init.body, "{}", "cancel sends an empty object");
    assert.equal(H.requestHeaders(undefined, undefined)["X-Van-Assistant-Key"], "");
  });
  test("chat and warning paths validate ids and actions", () => {
    assert.equal(H.chatPath(CHAT_ID), `/v1/assistant/chats/${CHAT_ID}`);
    assert.equal(H.chatPath(CHAT_ID, "messages"), `/v1/assistant/chats/${CHAT_ID}/messages`);
    assert.equal(H.chatPath(CHAT_ID, "apply-warning"), `/v1/assistant/chats/${CHAT_ID}/apply-warning`);
    assert.throws(() => H.chatPath("nope"), /Invalid conversation id/);
    assert.throws(() => H.chatPath(CHAT_ID, "delete"), /Invalid chat action/);
    assert.equal(H.warningRemovePath(WARNING_ID), `/v1/assistant/warnings/${WARNING_ID}/remove`);
    assert.throws(() => H.warningRemovePath(CHAT_ID), /Invalid warning id/);
  });
  test("bodies match the contract exactly", () => {
    const settings = { model: "default", effort: "high" };
    assert.deepEqual(H.openChatBody({ kind: "episode", id: "1" }, settings, "rid"), { event: { kind: "episode", id: "1" }, request_id: "rid", settings });
    assert.deepEqual(H.openChatBody({ kind: "setup", id: "new-warning" }, settings, "rid", true), { event: { kind: "setup", id: "new-warning" }, request_id: "rid", settings, new_chat: true });
    assert.deepEqual(H.messageBody(settings, "rid", { message: "hi" }), { request_id: "rid", settings, message: "hi" });
    assert.deepEqual(H.messageBody(settings, "rid", { retry: true }), { request_id: "rid", settings, retry: true });
    assert.deepEqual(H.applyBody("p1"), { proposal_id: "p1", confirmed: true });
    assert.deepEqual(H.removeSavedBody(), { confirmed: true });
  });
  test("responses classify as ok / pending / error", () => {
    assert.equal(H.classifyResponse(200, {}), "ok");
    assert.equal(H.classifyResponse(202, { pending: true }), "pending");
    assert.equal(H.classifyResponse(202, { available: true }), "ok");
    assert.equal(H.classifyResponse(401, { detail: "x" }), "error");
    const err = H.requestError(503, { detail: "Down" });
    assert.equal(err.message, "Down");
    assert.equal(err.status, 503);
    assert.equal(H.requestError(502, null).message, H.TEXT.unavailable);
    assert.equal(H.isDisabledError(H.requestError(503, { detail: "Warning chat is disabled on this listener" })), true);
    assert.equal(H.isDisabledError(H.requestError(503, { detail: "Local Codex assistant is not running or is unavailable on the Pi" })), false);
    assert.equal(H.isDisabledError(null), false);
  });
});

describe("createRequester", () => {
  const identity = { getKey: () => "K", getClient: () => "C" };
  test("returns the JSON body on 200 and passes error details with status", async () => {
    const server = fakeServer([
      ["GET", "/ok", () => ({ status: 200, body: { hello: 1 } })],
      ["POST", "/bad", () => ({ status: 400, body: { detail: "Bad body" } })],
    ]);
    const request = H.createRequester({ fetch: server.fetch, sleep: async () => {}, ...identity });
    assert.deepEqual(await request("GET", "/ok"), { hello: 1 });
    await assert.rejects(request("POST", "/bad", { a: 1 }), err => err.message === "Bad body" && err.status === 400);
  });
  test("re-requests the identical URL and body on 202 pending until 200", async () => {
    let n = 0;
    const server = fakeServer([["POST", "/slow", () => (++n < 3 ? { status: 202, body: { available: false, pending: true } } : { status: 200, body: { done: true } })]]);
    const sleeps = [];
    const request = H.createRequester({ fetch: server.fetch, sleep: async ms => sleeps.push(ms), ...identity });
    assert.deepEqual(await request("POST", "/slow", { q: 1 }), { done: true });
    assert.equal(server.calls.length, 3);
    assert.ok(server.calls.every(c => c.url === "/slow" && JSON.stringify(c.body) === '{"q":1}'));
    assert.deepEqual(sleeps, [H.PENDING_RETRY_MS, H.PENDING_RETRY_MS]);
  });
  test("gives up after the maximum tries with the still-loading message", async () => {
    const server = fakeServer([["GET", "/slow", () => ({ status: 202, body: { pending: true } })]]);
    const request = H.createRequester({ fetch: server.fetch, sleep: async () => {}, maxTries: 4, ...identity });
    await assert.rejects(request("GET", "/slow"), err => err.message === H.TEXT.stillLoading && err.status === 202);
    assert.equal(server.calls.length, 4);
  });
  test("a non-JSON error body falls back to the generic message", async () => {
    const fetch = async () => ({ status: 502, ok: false, json: async () => { throw new SyntaxError("html"); } });
    const request = H.createRequester({ fetch, sleep: async () => {}, ...identity });
    await assert.rejects(request("GET", "/x"), err => err.message === H.TEXT.unavailable && err.status === 502);
  });
  test("an aborted request reports the timeout message and clears its timer", async () => {
    const timers = fakeTimers();
    const fetch = async (_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => { const e = new Error("aborted"); e.name = "AbortError"; reject(e); });
      timers.fire(H.REQUEST_TIMEOUT_MS);
    });
    const request = H.createRequester({ fetch, sleep: async () => {}, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, ...identity });
    await assert.rejects(request("GET", "/x"), err => err.message === H.TEXT.timedOut);
    assert.equal(timers.pending().length, 0);
  });
});

// ---------- formatting ----------

describe("formatting", () => {
  test("evidence line distinguishes setup chats and shows the capture time", () => {
    assert.equal(H.evidenceLine(null), H.TEXT.intro);
    assert.equal(H.evidenceLine({ event: { kind: "setup" } }), H.TEXT.setupEvidence);
    const now = new Date(2026, 8, 22, 12, 0, 0);
    const at = new Date(2026, 8, 22, 16, 25, 0);
    assert.equal(H.evidenceLine({ event: { kind: "episode" }, evidence_at: at.toISOString() }, now), "Evidence captured 4:25 pm · Explanation only; no vehicle actions");
    assert.equal(H.formatDateTime(new Date(2026, 8, 21, 9, 5, 0), now), "yesterday 9:05 am");
    assert.equal(H.formatDateTime("garbage", now), "—");
  });

  test("timingText summarises phase, outcome, milestones and totals", () => {
    assert.equal(H.timingText(null), "");
    assert.equal(H.timingText({ phase: "starting" }), "Starting Codex");
    assert.equal(H.timingText({ phase: "waiting_for_model", outcome: "timeout" }), "Timed out: Waiting for response");
    assert.equal(H.timingText({ phase: "weird" }), "Timing available");
    const text = H.timingText({
      outcome: "complete", phase: "finishing",
      milestones_ms: { process_started_ms: 120, thread_started_ms: 1500, turn_started_ms: 2000, first_model_item_ms: 9000, turn_completed_ms: 30500, process_exit_ms: 31000, ignored: 5 },
      elapsed_ms: 31200, child_cpu_ms: 4000, cgroup_throttled_ms: 0, signals: ["rate_limit", "retrying"],
    });
    assert.equal(text, "Completed · Launched +0.1s · Thread ready +1.5s · Turn started +2.0s · First response +9.0s · Turn completed +30.5s · Exited +31.0s · Elapsed 31.2s · CPU 4.0s · Throttled 0.0s · Signals: rate_limit, retrying");
    assert.equal(H.timingText({ outcome: "cancelled", phase: "receiving_response", milestones_ms: { turn_started_ms: "x" } }), "Stopped: Response activity received");
  });

  test("turn state helpers", () => {
    assert.equal(H.lastTurn(null), null);
    assert.equal(H.lastTurn({ turns: [] }), null);
    assert.equal(H.isBusy(chat([turn()])), true);
    assert.equal(H.isBusy(doneChat()), false);
    assert.equal(H.canRetry(chat([turn({ state: "error", error: "boom" })])), true);
    assert.equal(H.canRetry(chat([turn({ state: "cancelled" })])), true);
    assert.equal(H.canRetry(doneChat()), false);
  });

  test("transcript signature ignores diagnostics heartbeats and rows carry roles", () => {
    const a = chat([turn({ diagnostics: { phase: "starting" } })]);
    const b = chat([turn({ diagnostics: { phase: "waiting_for_model", elapsed_ms: 5000 } })]);
    assert.equal(H.transcriptSignature(a), H.transcriptSignature(b));
    assert.notEqual(H.transcriptSignature(a), H.transcriptSignature(doneChat()));
    assert.equal(H.transcriptSignature(null), null);
    const rows = H.transcriptRows(chat([
      turn({ user: "Q1" }),
      turn({ user: "Q2", assistant: "A2", state: "complete", settings: { resolved_model: "gpt-6-astra", effort: "high" } }),
      turn({ user: "Q3", error: "Codex took too long. You can retry this question", state: "error" }),
    ]));
    assert.equal(rows.length, 6);
    assert.deepEqual(rows[0], { key: "0-you", role: "you", kind: "user", heading: "You", text: "Q1" });
    assert.deepEqual(rows[1], { key: "0-codex", role: "codex", kind: "progress", heading: "Codex", text: H.TEXT.reviewing });
    assert.equal(rows[3].kind, "answer");
    assert.equal(rows[3].heading, "Codex · gpt-6-astra / high");
    assert.equal(rows[5].kind, "error");
    assert.equal(rows[5].text, "Codex took too long. You can retry this question");
    assert.deepEqual(H.transcriptRows(null), []);
  });

  test("proposal text and applied text", () => {
    const t = turn({ state: "complete", proposal_id: "p1", proposal_unit: "°F", proposal: {
      title: "Coolant above 230", metric: "engine.coolant_temperature", operator: "above", threshold: 230,
      persistence_observations: 5, window_seconds: 60, max_age_seconds: 10, engine_running: true,
    } });
    assert.equal(H.proposalText(t), "Coolant above 230\nengine.coolant_temperature: above 230 °F\n5 distinct observations within 60s; sample age/gap ≤ 10s.\nEngine running required (fresh RPM > 400).");
    t.proposal.operator = "absolute_magnitude_above"; t.proposal.engine_running = false; delete t.proposal_unit;
    assert.match(H.proposalText(t), /absolute magnitude above 230\n/);
    assert.match(H.proposalText(t), /Any engine state\.$/);
    assert.equal(H.proposalText(turn()), "");
    assert.equal(H.appliedText(true), H.TEXT.applied);
    assert.equal(H.appliedText(false), H.TEXT.notInstalled);
  });

  test("status text while busy uses the first timing phase", () => {
    assert.equal(H.statusText(chat([turn()])), H.TEXT.working);
    assert.equal(H.statusText(chat([turn({ diagnostics: { phase: "thread_ready", elapsed_ms: 100 } })])), "Thread ready… (up to 5 minutes)");
    assert.equal(H.statusText(doneChat()), H.TEXT.ready);
    assert.equal(H.statusText(chat([turn({ state: "error", error: "Failed" })])), "Failed");
    assert.equal(H.statusText(null), H.TEXT.ready);
  });
});

// ---------- model / effort choices ----------

describe("model and effort choices", () => {
  test("choices come from the advisor catalog with the old fallbacks", () => {
    assert.deepEqual(H.modelChoices(null), [{ value: "default", label: "Default" }]);
    assert.deepEqual(H.modelChoices(SETTINGS), [
      { value: "default", label: "Default (gpt-6-astra)" }, { value: "gpt-6-astra", label: "gpt-6-astra" }, { value: "gpt-6-mini", label: "gpt-6-mini" },
    ]);
    assert.deepEqual(H.effortValues(null, "default"), ["high"]);
    assert.deepEqual(H.effortValues(SETTINGS, "default"), ["low", "medium", "high"]);
    assert.deepEqual(H.effortChoices(SETTINGS, "gpt-6-mini"), [{ value: "medium", label: "Medium" }]);
  });
  test("normalizeChoice keeps a still-valid choice, else high, else the first", () => {
    assert.deepEqual(H.normalizeChoice(SETTINGS, "gpt-6-astra", "low"), { model: "gpt-6-astra", effort: "low" });
    assert.deepEqual(H.normalizeChoice(SETTINGS, "gpt-6-mini", "low"), { model: "gpt-6-mini", effort: "medium" });
    assert.deepEqual(H.normalizeChoice(SETTINGS, "gone-model", "low"), { model: "default", effort: "low" });
    assert.deepEqual(H.normalizeChoice(SETTINGS, "default", undefined), { model: "default", effort: "high" });
    assert.deepEqual(H.normalizeChoice(null, "x", "y"), { model: "default", effort: "high" });
  });
});

// ---------- saved warnings and polling ----------

describe("saved warnings and polling", () => {
  test("savedWarningsView hides when empty, shows rows and config errors", () => {
    assert.deepEqual(H.savedWarningsView({}), { visible: false, error: null, rows: [] });
    const view = H.savedWarningsView({ warnings: [{ id: WARNING_ID, rule: { title: "Coolant hot" }, loaded: true }, { id: "b".repeat(64), rule: {}, loaded: false }], warning_config_error: "" });
    assert.equal(view.visible, true);
    assert.equal(view.rows[0].text, "Coolant hot · Loaded by broker");
    assert.equal(view.rows[1].text, `${"b".repeat(64)} · Saved; waiting for upgraded broker`);
    assert.equal(H.savedWarningsView({ warning_config_error: "Malformed" }).visible, true);
    assert.equal(H.confirmRemoveSaved("X"), "Remove “X”?");
  });
  test("shouldPoll only while busy, open and visible", () => {
    assert.equal(H.shouldPoll({ busy: true, open: true, visible: true }), true);
    assert.equal(H.shouldPoll({ busy: false, open: true, visible: true }), false);
    assert.equal(H.shouldPoll({ busy: true, open: false, visible: true }), false);
    assert.equal(H.shouldPoll({ busy: true, open: true, visible: false }), false);
  });
  test("the pairing hint names the access-code file exactly", () => {
    assert.equal(H.TEXT.pairingHintPath, "/var/lib/van-telemetry-advisor/access-code");
    assert.equal(H.TEXT.pairingHintBefore + H.TEXT.pairingHintPath + H.TEXT.pairingHintAfter,
      "On the Pi, read /var/lib/van-telemetry-advisor/access-code and paste the code here. This is not an OpenAI API key.");
  });
});

// ---------- controller ----------

describe("createChatController", () => {
  test("opening with no key shows the pairing form; pairing connects and restores the chat", async () => {
    // No stored access code: the listener's header gate answers 401 until the browser is paired.
    const t = makeController(okRoutes(doneChat()), { storage: {} });
    const authed = t.server.fetch;
    t.server.fetch = async (url, init) => (init.headers["X-Van-Assistant-Key"] ? authed(url, init)
      : { status: 401, ok: false, json: async () => ({ available: false, detail: "Connect this browser using the local assistant access code" }) });
    const withAuth = H.createChatController({
      fetch: (url, init) => t.server.fetch(url, init), sleep: async () => {}, storage: H.createStorage(t.storage), random: fixedRandom,
      setTimeout: t.timers.setTimeout, clearTimeout: t.timers.clearTimeout, isVisible: () => true,
      now: () => new Date(2026, 8, 22, 12, 0, 0), onChange: s => t.snapshots.push(s), onWarningsChanged: () => t.warningsChanged.push(1),
    });
    assert.equal(t.ctrl.client, withAuth.client, "client id is created once and stored per origin");
    assert.equal(t.storage.getItem("van-warning-chat.client"), "ab".repeat(32));
    withAuth.open("explain", { eventId: 12, title: "Coolant hot" });
    await flush();
    let snap = t.last();
    assert.equal(snap.pairing, true);
    assert.equal(snap.status, H.TEXT.pairingPrompt);
    assert.equal(snap.reconnect, false);
    assert.equal(snap.hasChat, false);
    assert.equal(snap.canSend, false);
    assert.equal(snap.title, "Coolant hot");
    assert.equal(snap.evidence, H.TEXT.intro);
    assert.deepEqual(snap.models, [{ value: "default", label: "Default" }]);
    assert.deepEqual(snap.efforts, [{ value: "high", label: "High" }]);

    assert.equal(withAuth.pair("short"), false);
    assert.equal(t.last().status, H.TEXT.incompleteCode);
    assert.equal(t.last().statusError, true);

    assert.equal(withAuth.pair(`  ${CODE}  `), true);
    await flush();
    assert.equal(t.storage.getItem("van-warning-chat.access"), CODE);
    snap = t.last();
    assert.equal(snap.pairing, false);
    assert.equal(snap.hasChat, true);
    assert.equal(snap.canSend, true);
    assert.equal(snap.canNew, true);
    assert.equal(snap.showStop, false);
    assert.equal(snap.status, H.TEXT.ready);
    assert.equal(snap.statusError, false);
    assert.equal(snap.evidence, "Evidence captured 4:25 pm · Explanation only; no vehicle actions".replace("4:25 pm", H.formatDateTime("2026-09-22T16:25:00+00:00", new Date(2026, 8, 22, 12, 0, 0))));
    assert.equal(snap.transcript.rows.length, 2);
    assert.equal(snap.transcript.newMessage, true);
    assert.equal(snap.models.length, 3);
    assert.equal(snap.model, "default");
    assert.equal(snap.effort, "high");
    const post = t.server.calls.find(c => c.method === "POST");
    assert.deepEqual(post.body, { event: { kind: "episode", id: "12" }, request_id: "ab".repeat(16), settings: { model: "default", effort: "high" } });
    assert.equal(post.headers["X-Van-Assistant-Key"], CODE);
    assert.equal(post.headers["X-Van-Chat-Client"], "ab".repeat(32));
    assert.equal(post.cache, "no-store");
    assert.equal(t.timers.pending(H.POLL_MS).length, 0, "no polling when the answer is complete");
  });

  test("a running answer polls every second only while open and visible, heartbeats keep the transcript", async () => {
    let polls = 0;
    const running = chat([turn({ diagnostics: { phase: "starting", elapsed_ms: 200 } })]);
    const t = makeController([
      ...okRoutes(running),
      ["GET", `/v1/assistant/chats/${CHAT_ID}`, () => {
        polls += 1;
        if (polls === 1) return { status: 200, body: chat([turn({ diagnostics: { phase: "waiting_for_model", elapsed_ms: 1300 } })]) };
        return { status: 200, body: doneChat() };
      }],
    ]);
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    let snap = t.last();
    assert.equal(snap.busy, true);
    assert.equal(snap.showStop, true);
    assert.equal(snap.canSend, false);
    assert.equal(snap.locked, true);
    assert.equal(snap.status, "Starting Codex… (up to 5 minutes)");
    assert.equal(snap.timing.visible, true);
    assert.equal(snap.timing.open, false);
    const version = snap.transcript.version;
    assert.equal(t.timers.pending(H.POLL_MS).length, 1);

    t.timers.fire(H.POLL_MS);
    await flush();
    snap = t.last();
    assert.equal(polls, 1);
    assert.equal(snap.transcript.version, version, "progress heartbeat does not rebuild the transcript");
    assert.equal(snap.status, "Waiting for response… (up to 5 minutes)");
    assert.equal(snap.timing.text, "Waiting for response · Elapsed 1.3s");
    assert.equal(t.timers.pending(H.POLL_MS).length, 1);

    t.visible.value = false;
    t.ctrl.visibilityChanged();
    assert.equal(t.timers.pending(H.POLL_MS).length, 0, "hidden page stops the poll timer");
    t.visible.value = true;
    t.ctrl.visibilityChanged();
    await flush();
    snap = t.last();
    assert.equal(polls, 2, "visible again polls immediately");
    assert.equal(snap.busy, false);
    assert.equal(snap.status, H.TEXT.ready);
    assert.equal(snap.transcript.version, version + 1);
    assert.equal(snap.transcript.rows[1].kind, "answer");
    assert.equal(t.timers.pending(H.POLL_MS).length, 0);

    // A busy chat polls again; closing clears the timer and later replies are ignored.
    polls = 0;
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    assert.equal(t.timers.pending(H.POLL_MS).length, 1);
    t.ctrl.close();
    assert.equal(t.timers.pending(H.POLL_MS).length, 0);
    assert.equal(t.last().open, false);
  });

  test("send reuses the pending request id after a failure and clears it on success", async () => {
    let fail = true;
    const t = makeController([
      ...okRoutes(doneChat()),
      ["POST", `/v1/assistant/chats/${CHAT_ID}/messages`, body => {
        if (fail) return { status: 503, body: { available: false, detail: "Local Codex assistant is not running or is unavailable on the Pi" } };
        return { status: 200, body: chat([doneChat().turns[0], turn({ user: body.message })]) };
      }],
    ]);
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    assert.equal(await t.ctrl.send("   "), false, "blank messages are ignored");
    assert.equal(await t.ctrl.send("Why?"), false);
    let snap = t.last();
    assert.equal(snap.status, "Local Codex assistant is not running or is unavailable on the Pi");
    assert.equal(snap.statusError, true);
    assert.equal(snap.reconnect, true);
    assert.equal(snap.disabled, false);
    assert.equal(snap.requesting, false);
    fail = false;
    assert.equal(await t.ctrl.send("Why?"), true);
    const posts = t.server.calls.filter(c => c.url.endsWith("/messages"));
    assert.equal(posts.length, 2);
    assert.deepEqual(posts[0].body, posts[1].body, "identical retry reuses the same request_id");
    assert.deepEqual(posts[1].body, { request_id: "ab".repeat(16), settings: { model: "default", effort: "high" }, message: "Why?" });
    snap = t.last();
    assert.equal(snap.reconnect, false);
    assert.equal(snap.busy, true);
    assert.equal(snap.transcript.rows.length, 4);
    assert.equal(snap.transcript.newMessage, true);
    assert.equal(t.timers.pending(H.POLL_MS).length, 1);
    assert.equal(await t.ctrl.send("Again"), false, "cannot send while busy");
    t.ctrl.close();
  });

  test("model and effort selections travel with the next question", async () => {
    const t = makeController([
      ...okRoutes(doneChat()),
      ["POST", `/v1/assistant/chats/${CHAT_ID}/messages`, () => ({ status: 200, body: doneChat() })],
    ]);
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    t.ctrl.setModel("gpt-6-mini");
    let snap = t.last();
    assert.equal(snap.model, "gpt-6-mini");
    assert.equal(snap.effort, "medium", "effort re-validated against the model's list");
    assert.deepEqual(snap.efforts, [{ value: "medium", label: "Medium" }]);
    t.ctrl.setModel("gpt-6-astra");
    t.ctrl.setEffort("low");
    snap = t.last();
    assert.equal(snap.effort, "low");
    t.ctrl.setEffort("bogus");
    assert.equal(t.last().effort, "high");
    await t.ctrl.send("Q");
    const post = t.server.calls.filter(c => c.url.endsWith("/messages"))[0];
    assert.deepEqual(post.body.settings, { model: "gpt-6-astra", effort: "high" });
  });

  test("retry, stop and reconnect hit their routes", async () => {
    const errored = chat([turn({ state: "error", error: "Codex took too long. You can retry this question" })]);
    const t = makeController([
      ...okRoutes(errored),
      ["POST", `/v1/assistant/chats/${CHAT_ID}/messages`, () => ({ status: 200, body: chat([turn()]) })],
      ["POST", `/v1/assistant/chats/${CHAT_ID}/cancel`, () => ({ status: 200, body: chat([turn({ state: "cancelled", diagnostics: { outcome: "cancelled", phase: "waiting_for_model" } })]) })],
      ["GET", `/v1/assistant/chats/${CHAT_ID}`, () => ({ status: 200, body: doneChat() })],
    ]);
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    let snap = t.last();
    assert.equal(snap.showRetry, true);
    assert.equal(snap.status, "Codex took too long. You can retry this question");
    assert.equal(snap.transcript.rows[1].kind, "error");
    assert.equal(await t.ctrl.retry(), true);
    const retryPost = t.server.calls.find(c => c.url.endsWith("/messages"));
    assert.deepEqual(retryPost.body, { request_id: "ab".repeat(16), settings: { model: "default", effort: "high" }, retry: true });
    snap = t.last();
    assert.equal(snap.busy, true);
    assert.equal(snap.showStop, true);
    assert.equal(snap.stopEnabled, true);

    assert.equal(await t.ctrl.stop(), true);
    const cancel = t.server.calls.find(c => c.url.endsWith("/cancel"));
    assert.deepEqual(cancel.body, {});
    snap = t.last();
    assert.equal(snap.busy, false);
    assert.equal(snap.showRetry, true, "a cancelled turn can be retried");
    assert.equal(snap.timing.text, "Stopped: Waiting for response");
    assert.equal(t.timers.pending(H.POLL_MS).length, 0);

    t.ctrl.reconnect();
    await flush();
    assert.equal(t.server.calls.filter(c => c.method === "GET" && c.url === `/v1/assistant/chats/${CHAT_ID}`).length, 1);
    assert.equal(t.last().status, H.TEXT.ready);
  });

  test("new chat re-opens with new_chat and an empty transcript", async () => {
    const t = makeController(okRoutes(doneChat()));
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    assert.equal(t.ctrl.newChat(), true);
    const reset = t.snapshots.find(s => s.hasChat === false && s.transcript.rows.length === 0 && s.transcript.version === 0 && t.snapshots.indexOf(s) > 2);
    assert.ok(reset, "transcript cleared before reconnecting");
    await flush();
    const posts = t.server.calls.filter(c => c.method === "POST");
    assert.equal(posts.length, 2);
    assert.equal(posts[1].body.new_chat, true);
    assert.equal(t.last().hasChat, true);
    assert.equal(t.ctrl.newChat(), true);
    await flush();
    assert.equal(await t.ctrl.send("x"), false, "send is refused while a new chat is being requested");
  });

  test("error while opening shows Reconnect, which retries the connection", async () => {
    let down = true;
    const t = makeController([
      ["GET", H.STATUS_PATH, () => (down ? { status: 503, body: { available: false, detail: "Local Codex assistant is not running or is unavailable on the Pi" } } : { status: 200, body: STATUS_OK })],
      ["POST", H.CHATS_PATH, () => ({ status: 200, body: doneChat() })],
    ]);
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    assert.equal(t.last().reconnect, true);
    assert.equal(t.last().hasChat, false);
    down = false;
    t.ctrl.reconnect();
    await flush();
    assert.equal(t.last().hasChat, true);
    assert.equal(t.last().reconnect, false);
  });

  test("a listener with warning chat disabled shows the disabled message without Reconnect", async () => {
    const t = makeController([["GET", H.STATUS_PATH, () => ({ status: 503, body: { available: false, detail: "Warning chat is disabled on this listener" } })]]);
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    const snap = t.last();
    assert.equal(snap.disabled, true);
    assert.equal(snap.status, H.TEXT.disabled);
    assert.equal(snap.reconnect, false);
    assert.equal(snap.pairing, false);
  });

  test("an old advisor without settings asks for a restart", async () => {
    const t = makeController([["GET", H.STATUS_PATH, () => ({ status: 200, body: { available: true } })]]);
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    assert.equal(t.last().status, H.TEXT.restartAdvisor);
    assert.equal(t.last().reconnect, true);
  });

  test("a rejected key is forgotten and the pairing form returns with the server's message", async () => {
    const t = makeController([["GET", H.STATUS_PATH, () => ({ status: 401, body: { available: false, detail: "Connect this browser using the local assistant access code" } })]]);
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    const snap = t.last();
    assert.equal(snap.pairing, true);
    assert.equal(snap.reconnect, false);
    assert.equal(snap.status, "Connect this browser using the local assistant access code");
    assert.equal(t.storage.getItem("van-warning-chat.access"), null);
  });

  test("a card without a saved event cannot open a chat", async () => {
    const t = makeController(okRoutes(doneChat()));
    t.ctrl.open("explain", { title: "Orphan" });
    await flush();
    assert.equal(t.last().status, H.TEXT.noEvent);
    assert.equal(t.server.calls.length, 0);
  });

  test("proposal review: approve, remove, and saved-warning removal notify the parent", async () => {
    const proposal = { title: "Coolant above 230", metric: "engine.coolant_temperature", operator: "above", threshold: 230, persistence_observations: 5, window_seconds: 60, max_age_seconds: 10, engine_running: true };
    const proposed = chat([turn({ user: "Warn when coolant is over 230", assistant: "Proposal ready", state: "complete", proposal_id: "p1", proposal_unit: "°F", proposal })], { event: { kind: "setup", id: "new-warning" }, title: "Add Early Warning" });
    const applied = chat([{ ...proposed.turns[0], applied: true }], { event: { kind: "setup", id: "new-warning" } });
    let statusCalls = 0;
    const t = makeController([
      ["GET", H.STATUS_PATH, () => { statusCalls += 1; return { status: 200, body: { ...STATUS_OK, warnings: statusCalls >= 2 ? [{ id: WARNING_ID, rule: { title: "Coolant above 230" }, loaded: false }] : [] } }; }],
      ["POST", H.CHATS_PATH, () => ({ status: 200, body: proposed })],
      ["POST", `/v1/assistant/chats/${CHAT_ID}/apply-warning`, () => ({ status: 200, body: applied })],
      ["POST", `/v1/assistant/chats/${CHAT_ID}/remove-warning`, () => ({ status: 200, body: proposed })],
      ["POST", `/v1/assistant/warnings/${WARNING_ID}/remove`, () => ({ status: 200, body: { ...STATUS_OK, warnings: [] } })],
    ]);
    t.ctrl.open("add-warning", {});
    await flush();
    let snap = t.last();
    assert.equal(snap.mode, "add-warning");
    assert.equal(snap.title, "Add Early Warning");
    assert.equal(snap.evidence, H.TEXT.setupEvidence);
    assert.equal(snap.proposal.visible, true);
    assert.equal(snap.proposal.open, true);
    assert.equal(snap.proposal.applied, false);
    assert.equal(snap.proposal.appliedText, H.TEXT.notInstalled);
    assert.match(snap.proposal.text, /^Coolant above 230\n/);
    assert.equal(snap.saved.visible, false);
    const openPost = t.server.calls.find(c => c.method === "POST");
    assert.deepEqual(openPost.body.event, { kind: "setup", id: "new-warning" });

    t.ctrl.setProposalOpen(false);
    assert.equal(t.last().proposal.open, false);

    assert.equal(await t.ctrl.applyWarning(), true);
    const apply = t.server.calls.find(c => c.url.endsWith("/apply-warning"));
    assert.deepEqual(apply.body, { proposal_id: "p1", confirmed: true });
    snap = t.last();
    assert.equal(snap.proposal.applied, true);
    assert.equal(snap.proposal.open, false, "an applied proposal collapses");
    assert.equal(snap.proposal.appliedText, H.TEXT.applied);
    assert.equal(t.warningsChanged.length, 1);
    assert.equal(statusCalls, 2, "settings and saved warnings reload after applying");
    assert.equal(snap.saved.visible, true);
    assert.equal(snap.saved.rows[0].text, "Coolant above 230 · Saved; waiting for upgraded broker");
    assert.equal(snap.saved.rows[0].removing, false);

    assert.equal(await t.ctrl.removeWarning(), true);
    const remove = t.server.calls.find(c => c.url.endsWith("/remove-warning"));
    assert.deepEqual(remove.body, { proposal_id: "p1", confirmed: true });
    snap = t.last();
    assert.equal(snap.proposal.applied, false);
    assert.equal(snap.proposal.open, true, "an un-applied proposal re-expands");
    assert.equal(t.warningsChanged.length, 2);

    assert.equal(await t.ctrl.removeSaved("not-hex"), false);
    assert.equal(await t.ctrl.removeSaved(WARNING_ID), true);
    const removeSaved = t.server.calls.find(c => c.url === `/v1/assistant/warnings/${WARNING_ID}/remove`);
    assert.deepEqual(removeSaved.body, { confirmed: true });
    assert.ok(t.snapshots.some(s => s.saved.rows[0]?.removing === true), "row shows its removal in progress");
    assert.equal(t.last().saved.visible, false);
    assert.equal(t.warningsChanged.length, 3);
  });

  test("timing details expand automatically on an error turn", async () => {
    const errored = chat([turn({ state: "error", error: "Failed", diagnostics: { outcome: "error", phase: "starting", elapsed_ms: 900 } })]);
    const t = makeController(okRoutes(errored));
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    let snap = t.last();
    assert.equal(snap.timing.visible, true);
    assert.equal(snap.timing.open, true);
    assert.equal(snap.timing.text, "Failed: Starting Codex · Elapsed 0.9s");
    t.ctrl.setTimingOpen(false);
    assert.equal(t.last().timing.open, false);
    t.ctrl.open("explain", { eventId: 12 });
    snap = t.snapshots[t.snapshots.length - 1];
    assert.equal(snap.timing.visible, false, "reopen resets the timing panel");
    await flush();
  });

  test("replies that arrive after close or reopen are ignored", async () => {
    let release;
    const gate = new Promise(r => { release = r; });
    const t = makeController([
      ["GET", H.STATUS_PATH, () => ({ status: 200, body: STATUS_OK })],
      ["POST", H.CHATS_PATH, async () => { await gate; return { status: 200, body: doneChat() }; }],
    ]);
    t.ctrl.open("explain", { eventId: 12 });
    await flush();
    t.ctrl.close();
    release();
    await flush();
    assert.equal(t.last().hasChat, false);
    assert.equal(t.last().open, false);
  });
});
