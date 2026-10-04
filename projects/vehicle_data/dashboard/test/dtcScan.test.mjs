import { test } from "node:test";
import assert from "node:assert/strict";
import * as H from "../src/components/dtcScan.helpers.js";

function reply(status, data, { json = true } = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => {
      if (!json) throw new SyntaxError("Unexpected token <");
      return data;
    },
  };
}

const idle = { available: true, enabled: true, state: "idle", job: null };
const job = (state, extra = {}) => ({
  available: true,
  enabled: true,
  state,
  job: {
    job_id: "dtc-web-20260923T010203Z-abcd1234",
    state,
    progress: { requestable: 15, queried: 4, imported: 3, unavailable: 0 },
    current_bus: "c-can",
    current_module: "tcm",
    cancel_requested: false,
    failure: null,
    restoration_failure: null,
    ...extra,
  },
});

/** Controller harness with a scripted fetch, manual timers, visibility and confirm. */
function harness({ replies = [], visible = true, confirm = true } = {}) {
  const calls = [];
  const queue = replies.slice();
  const timers = new Map();
  let nextTimer = 1;
  const env = {
    visible,
    confirmAnswer: confirm,
    confirms: [],
    finished: [],
    changes: 0,
    calls,
    queue,
    timers,
  };
  env.ctrl = H.createDtcScanController({
    fetch: async (url, init) => {
      calls.push({ url, init: init || {} });
      const next = queue.shift();
      if (!next) throw new Error("no scripted reply for " + url);
      if (next instanceof Error) throw next;
      if (typeof next.then === "function") return next;
      return next;
    },
    isVisible: () => env.visible,
    setTimeout: (fn, ms) => {
      const id = nextTimer++;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimeout: (id) => {
      timers.delete(id);
    },
    confirm: (message) => {
      env.confirms.push(message);
      if (env.onConfirm) env.onConfirm();
      return env.confirmAnswer;
    },
    onChange: () => {
      env.changes += 1;
    },
    onFinished: (payload) => env.finished.push(payload),
    now: () => 1758589323000,
  });
  env.pendingDelays = () => Array.from(timers.values()).map((t) => t.ms);
  env.fire = async () => {
    const entries = Array.from(timers.entries());
    assert.equal(entries.length, 1, "exactly one poll timer is pending");
    const [id, t] = entries[0];
    timers.delete(id);
    t.fn();
    await flush();
  };
  return env;
}

async function flush() {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
}

const ENABLED = { dtc_jobs_enabled: true };

async function enabledHarness(first, options = {}) {
  const env = harness({ replies: [reply(200, first)], ...options });
  env.ctrl.configure(ENABLED);
  await flush();
  return env;
}

// ---------------------------------------------------------------------------
// pure gates

test("start body is exactly the three parked confirmations", () => {
  const body = H.startBody({ ...H.initialState(), enabled: true, confirmed: true });
  assert.deepEqual(body, { confirm_parked: true, confirm_park_gear: true, confirm_ignition_on_engine_off: true });
  assert.deepEqual(Object.keys(body).sort(), ["confirm_ignition_on_engine_off", "confirm_park_gear", "confirm_parked"]);
});

test("legacy panel state cannot add fields to the fixed start body", () => {
  assert.deepEqual(H.startBody({ token: "a".repeat(40), tokenRequired: true }), H.startBody());
  assert.equal("token" in H.initialState(), false);
  assert.equal("tokenRequired" in H.viewModel(H.initialState()), false);
});

test("cancel body is exactly {action: 'cancel'}", () => {
  assert.deepEqual(H.cancelBody(), { action: "cancel" });
});

test("every Scan gate blocks in order and names its reason", () => {
  const ready = { ...H.initialState(), enabled: true, confirmed: true, state: "idle" };
  assert.equal(H.startBlock(ready), null);
  assert.equal(H.canStart(ready), true);
  assert.equal(H.startBlock({ ...ready, enabled: false }), "disabled");
  assert.equal(H.startBlock({ ...ready, busy: "start" }), "busy");
  assert.equal(H.startBlock({ ...ready, busy: "cancel" }), "busy");
  for (const state of ["queued", "starting", "created", "running"]) {
    assert.equal(H.startBlock({ ...ready, state }), "active", state);
  }
  assert.equal(H.startBlock({ ...ready, state: "restoration_failed" }), "lockout");
  assert.equal(H.startBlock({ ...ready, confirmed: false }), "confirm");
  for (const state of ["completed", "cancelled", "failed", null]) {
    assert.equal(H.startBlock({ ...ready, state }), null, String(state));
  }
});

test("Cancel is offered only for an active job with no cancel pending and nothing in flight", () => {
  const base = { ...H.initialState(), enabled: true, state: "running" };
  assert.equal(H.canCancel(base), true);
  for (const state of ["queued", "starting", "created"]) assert.equal(H.canCancel({ ...base, state }), true, state);
  for (const state of ["idle", "completed", "cancelled", "failed", "restoration_failed", null]) {
    assert.equal(H.canCancel({ ...base, state }), false, String(state));
  }
  assert.equal(H.canCancel({ ...base, cancelRequested: true }), false);
  assert.equal(H.canCancel({ ...base, busy: "cancel" }), false);
  assert.equal(H.canCancel({ ...base, enabled: false }), false);
});

test("view model hints and lockout", () => {
  const s = { ...H.initialState(), enabled: true, state: "idle" };
  assert.equal(H.viewModel(s).hint, H.TEXT.hintConfirm);
  assert.equal(H.viewModel({ ...s, state: "running" }).hint, H.TEXT.hintActive);
  const locked = H.viewModel({ ...s, confirmed: true, state: "restoration_failed" });
  assert.equal(locked.lockout, true);
  assert.equal(locked.canStart, false);
  assert.equal(locked.hint, null);
  assert.match(H.TEXT.lockout, /restoration unverified — inspect before retry/i);
  assert.equal(H.viewModel({ ...s, confirmed: true }).canStart, true);
  assert.equal(H.viewModel({ ...s, state: null }).summaryText, "Parked code scan · checking");
  assert.equal(H.viewModel({ ...s, state: "running" }).summaryText, "Parked code scan · running");
});

test("status line keeps the legacy parts in plain words", () => {
  assert.equal(H.jobStatusText(idle), H.TEXT.idle);
  assert.equal(
    H.jobStatusText(job("running", { cancel_requested: true })),
    "Running · 4 of 15 modules read · 3 saved · C-CAN / tcm · cancel requested",
  );
  const withGap = H.jobStatusText(job("running", { progress: { requestable: 15, queried: 6, imported: 4, unavailable: 2 } }));
  assert.match(withGap, /6 of 15 modules read · 4 saved · 2 no answer/);
  const failed = H.jobStatusText(job("failed", { failure: "broker gate: vehicle not stationary", current_bus: null, current_module: null }));
  assert.match(failed, /^Failed · 4 of 15 modules read · 3 saved · broker gate: vehicle not stationary$/);
  const restoration = H.jobStatusText(job("restoration_failed", { restoration_failure: "c-can not restored", current_bus: null, current_module: null }));
  assert.match(restoration, /^Stopped, restoration unverified/);
  assert.match(restoration, /restoration unverified — inspect before retry/);
  const noProgress = H.jobStatusText({ state: "queued", job: { state: "queued" } });
  assert.equal(noProgress, "Queued");
  assert.equal(H.jobStatusText({ state: "weird_state", job: {} }), "Weird state");
});

test("finished jobs carry their finish time", () => {
  const text = H.jobStatusText(job("completed", { completed_at: "2026-09-23T01:10:00Z", current_bus: null, current_module: null }), Date.parse("2026-09-23T02:00:00Z"));
  assert.match(text, /^Finished \S+/);
  assert.doesNotMatch(text, /null|undefined/);
});

test("summary refresh only when a watched active job reaches a final state", () => {
  const next = (state, imported = 0) => ({ state, job: { progress: { imported } } });
  assert.equal(H.shouldRefreshSummary("running", next("completed")), true);
  assert.equal(H.shouldRefreshSummary("queued", next("completed")), true);
  assert.equal(H.shouldRefreshSummary("running", next("cancelled", 0)), false);
  assert.equal(H.shouldRefreshSummary("running", next("cancelled", 2)), true);
  assert.equal(H.shouldRefreshSummary("running", next("failed", 1)), true);
  assert.equal(H.shouldRefreshSummary(null, next("completed")), false, "a job already finished at load is not refetched");
  assert.equal(H.shouldRefreshSummary("completed", next("completed")), false);
  assert.equal(H.shouldRefreshSummary("running", next("running")), false);
});

test("poll delays: 2 s while active, 5 s after an error, none otherwise", () => {
  const s = { ...H.initialState(), enabled: true };
  assert.equal(H.nextPollDelay({ ...s, state: "running" }, false), H.POLL_ACTIVE_MS);
  assert.equal(H.POLL_ACTIVE_MS, 2000);
  assert.equal(H.nextPollDelay({ ...s, state: "idle" }, true), H.POLL_ERROR_MS);
  assert.equal(H.POLL_ERROR_MS, 5000);
  assert.equal(H.nextPollDelay({ ...s, state: "completed" }, false), null);
  assert.equal(H.nextPollDelay({ ...s, enabled: false, state: "running" }, true), null);
});

// ---------------------------------------------------------------------------
// controller

test("enabling reads the current job without caching and does not poll an idle job", async () => {
  const env = await enabledHarness(idle);
  assert.equal(env.calls.length, 1);
  assert.match(env.calls[0].url, /^\/v1\/diagnostics\/dtc-jobs\/current\?fresh=\d+$/);
  assert.equal(env.calls[0].init.cache, "no-store");
  assert.equal(env.calls[0].init.method, undefined);
  assert.deepEqual(env.pendingDelays(), []);
  assert.equal(env.ctrl.snapshot().statusText, H.TEXT.idle);
  assert.equal(env.ctrl.snapshot().canStart, false, "unconfirmed");
});

test("a disabled listener never requests anything", async () => {
  const env = harness();
  env.ctrl.configure({ dtc_jobs_enabled: false });
  await flush();
  assert.equal(env.calls.length, 0);
  env.ctrl.setConfirmed(true);
  assert.equal(await env.ctrl.start(), false);
  assert.equal(env.confirms.length, 0);
  assert.equal(env.calls.length, 0);
});

test("an active job polls every 2 s and a completed job refetches the summary once", async () => {
  const env = await enabledHarness(job("running"));
  assert.deepEqual(env.pendingDelays(), [2000]);
  assert.equal(env.ctrl.snapshot().canCancel, true);
  env.queue.push(reply(200, job("running", { progress: { requestable: 15, queried: 9, imported: 8 } })));
  await env.fire();
  assert.equal(env.calls.length, 2);
  assert.deepEqual(env.pendingDelays(), [2000]);
  assert.match(env.ctrl.snapshot().statusText, /9 of 15 modules read/);
  env.queue.push(reply(200, job("completed", { current_bus: null, current_module: null })));
  await env.fire();
  assert.deepEqual(env.pendingDelays(), [], "polling stops at a final state");
  assert.equal(env.finished.length, 1);
  assert.equal(env.ctrl.snapshot().active, false);
});

test("a job already completed when the panel opens does not trigger a refresh", async () => {
  const env = await enabledHarness(job("completed"));
  assert.equal(env.finished.length, 0);
  assert.deepEqual(env.pendingDelays(), []);
});

test("a failed status read shows the error and retries after 5 s", async () => {
  // A 200 with an unreadable body is treated as a failed read.
  const env = await enabledHarness(null);
  assert.equal(env.ctrl.snapshot().statusText, "Scan status unavailable: unreadable reply");
  assert.match(env.ctrl.snapshot().statusText, /^Scan status unavailable: /);
  assert.equal(env.ctrl.snapshot().error, "status");
  assert.deepEqual(env.pendingDelays(), [5000]);
  env.queue.push(reply(503, { available: false, detail: "job pointer is malformed" }));
  await env.fire();
  assert.equal(env.ctrl.snapshot().statusText, "Scan status unavailable: job pointer is malformed");
  assert.deepEqual(env.pendingDelays(), [5000]);
  env.queue.push(reply(200, job("running")));
  await env.fire();
  assert.equal(env.ctrl.snapshot().error, null);
  assert.deepEqual(env.pendingDelays(), [2000]);
});

test("non-JSON error replies fall back to the HTTP status", async () => {
  const env = await enabledHarness(idle);
  env.queue.push(reply(502, null, { json: false }));
  await env.ctrl.refresh();
  assert.equal(env.ctrl.snapshot().statusText, "Scan status unavailable: HTTP 502");
});

test("a hidden page stops polling until it is visible again", async () => {
  const env = await enabledHarness(job("running"));
  env.visible = false;
  await env.fire();
  assert.equal(env.calls.length, 1, "no request while hidden");
  assert.deepEqual(env.pendingDelays(), [], "and no timer left behind");
  env.ctrl.resume();
  await flush();
  assert.equal(env.calls.length, 1, "resume does nothing while still hidden");
  env.visible = true;
  env.queue.push(reply(200, job("running")));
  env.ctrl.resume();
  await flush();
  assert.equal(env.calls.length, 2);
  assert.deepEqual(env.pendingDelays(), [2000]);
});

test("resume does not double-poll when a timer is already pending", async () => {
  const env = await enabledHarness(job("running"));
  env.ctrl.resume();
  await flush();
  assert.equal(env.calls.length, 1);
  assert.deepEqual(env.pendingDelays(), [2000]);
});

test("Scan needs the parked checkbox before the confirm dialog is even shown", async () => {
  const env = await enabledHarness(idle);
  assert.equal(await env.ctrl.start(), false);
  assert.equal(env.confirms.length, 0);
  assert.equal(env.calls.length, 1);
});

test("declining the confirm dialog sends nothing", async () => {
  const env = await enabledHarness(idle, { confirm: false });
  env.ctrl.setConfirmed(true);
  assert.equal(await env.ctrl.start(), false);
  assert.deepEqual(env.confirms, [H.TEXT.confirmPrompt]);
  assert.equal(env.calls.length, 1);
});

test("the confirm prompt is the former static app's wording", () => {
  assert.equal(
    H.TEXT.confirmPrompt,
    "Start the fixed read-only DTC batch now? Confirm Park, ignition ON, engine OFF, and stationary.",
  );
});

test("a confirmed Scan posts the exact body, polls the queued job and asks for a fresh confirmation", async () => {
  const env = await enabledHarness(idle);
  env.ctrl.setConfirmed(true);
  env.queue.push(reply(202, { available: true, enabled: true, state: "queued", job: { job_id: "dtc-web-x", state: "queued" } }));
  const started = env.ctrl.start();
  assert.equal(env.ctrl.snapshot().busy, "start");
  assert.equal(env.ctrl.snapshot().canStart, false, "no double submit while in flight");
  assert.equal(env.ctrl.snapshot().startLabel, H.TEXT.starting);
  assert.equal(await started, true);
  const post = env.calls[1];
  assert.equal(post.url, "/v1/diagnostics/dtc-jobs");
  assert.equal(post.init.method, "POST");
  assert.deepEqual(post.init.headers, { "Content-Type": "application/json" });
  assert.deepEqual(JSON.parse(post.init.body), {
    confirm_parked: true,
    confirm_park_gear: true,
    confirm_ignition_on_engine_off: true,
  });
  const vm = env.ctrl.snapshot();
  assert.equal(vm.busy, null);
  assert.equal(vm.confirmed, false);
  assert.equal(vm.active, true);
  assert.equal(vm.canStart, false);
  assert.equal(vm.canCancel, true);
  assert.equal(vm.statusText, "Queued");
  assert.deepEqual(env.pendingDelays(), [2000]);
});

test("a refused Scan shows the listener's reason and keeps the confirmation", async () => {
  const env = await enabledHarness(idle);
  env.ctrl.setConfirmed(true);
  env.queue.push(reply(409, { available: false, reason: "dtc_job_rejected", detail: "a DTC batch request is already queued" }));
  assert.equal(await env.ctrl.start(), false);
  const vm = env.ctrl.snapshot();
  assert.equal(vm.statusText, "The scan was not queued: a DTC batch request is already queued");
  assert.equal(vm.error, "start");
  assert.equal(vm.busy, null);
  assert.equal(vm.confirmed, true);
  assert.equal(vm.canStart, true);
});

test("a network failure on Scan is reported and leaves Scan available", async () => {
  const env = await enabledHarness(idle);
  env.ctrl.setConfirmed(true);
  env.queue.push(new TypeError("Failed to fetch"));
  assert.equal(await env.ctrl.start(), false);
  assert.equal(env.ctrl.snapshot().statusText, "The scan was not queued: Failed to fetch");
  assert.equal(env.ctrl.snapshot().canStart, true);
});

test("restoration_failed locks Scan out even when confirmed", async () => {
  const env = await enabledHarness(job("restoration_failed", { restoration_failure: "c-can", current_bus: null, current_module: null }));
  env.ctrl.setConfirmed(true);
  const vm = env.ctrl.snapshot();
  assert.equal(vm.lockout, true);
  assert.equal(vm.canStart, false);
  assert.equal(await env.ctrl.start(), false);
  assert.equal(env.confirms.length, 0);
  assert.equal(env.calls.length, 1);
  assert.deepEqual(env.pendingDelays(), []);
});

test("an active job blocks Scan", async () => {
  const env = await enabledHarness(job("running"));
  env.ctrl.setConfirmed(true);
  assert.equal(env.ctrl.snapshot().canStart, false);
  assert.equal(await env.ctrl.start(), false);
  assert.equal(env.confirms.length, 0);
});

test("flags that change behind the confirm dialog are re-checked before posting", async () => {
  const env = await enabledHarness(idle);
  env.ctrl.setConfirmed(true);
  env.onConfirm = () => env.ctrl.configure({ dtc_jobs_enabled: false });
  assert.equal(await env.ctrl.start(), false);
  assert.equal(env.confirms.length, 1);
  assert.equal(env.calls.length, 1, "no POST after the listener turned scanning off");
});

test("Cancel posts exactly {action: 'cancel'} and cannot be repeated while pending", async () => {
  const env = await enabledHarness(job("running"));
  env.queue.push(reply(202, job("running", { cancel_requested: true })));
  assert.equal(await env.ctrl.cancel(), true);
  const post = env.calls[1];
  assert.equal(post.url, "/v1/diagnostics/dtc-jobs/current/cancel");
  assert.equal(post.init.method, "POST");
  assert.deepEqual(post.init.headers, { "Content-Type": "application/json" });
  assert.deepEqual(JSON.parse(post.init.body), { action: "cancel" });
  const vm = env.ctrl.snapshot();
  assert.equal(vm.canCancel, false);
  assert.match(vm.statusText, /cancel requested/);
  assert.equal(await env.ctrl.cancel(), false);
  assert.equal(env.calls.length, 2);
  assert.deepEqual(env.pendingDelays(), [2000], "polling continues until the job ends");
});

test("a cancel before the worker claims the request ends the job", async () => {
  const env = await enabledHarness(job("queued"));
  env.queue.push(reply(202, { available: true, enabled: true, state: "cancelled", job: { state: "cancelled", cancel_requested: true, failure: "cancelled before the worker claimed the request" } }));
  await env.ctrl.cancel();
  assert.match(env.ctrl.snapshot().statusText, /^Cancelled/);
  assert.deepEqual(env.pendingDelays(), []);
  assert.equal(env.finished.length, 0, "nothing was imported");
});

test("a refused cancel is reported", async () => {
  const env = await enabledHarness(job("running"));
  env.queue.push(reply(409, { detail: "there is no current DTC batch" }));
  assert.equal(await env.ctrl.cancel(), false);
  assert.equal(env.ctrl.snapshot().statusText, "Cancel was not accepted: there is no current DTC batch");
  assert.equal(env.ctrl.snapshot().canCancel, true);
});

test("Cancel is not offered for idle or finished jobs", async () => {
  const env = await enabledHarness(idle);
  assert.equal(await env.ctrl.cancel(), false);
  assert.equal(env.calls.length, 1);
});

test("disabling the listener stops polling, resets the panel and drops late replies", async () => {
  const env = await enabledHarness(job("running"));
  env.ctrl.setConfirmed(true);
  let resolveLate;
  env.queue.push(new Promise((r) => { resolveLate = r; }));
  const late = env.ctrl.refresh();
  env.ctrl.configure({ dtc_jobs_enabled: false });
  assert.deepEqual(env.pendingDelays(), []);
  resolveLate(reply(200, job("running")));
  await late;
  await flush();
  const vm = env.ctrl.snapshot();
  assert.equal(vm.enabled, false);
  assert.equal(vm.confirmed, false);
  assert.equal(vm.active, false);
  assert.deepEqual(env.pendingDelays(), [], "the late reply scheduled nothing");
});

test("an older status reply cannot overwrite a newer Scan reply", async () => {
  const env = await enabledHarness(idle);
  env.ctrl.setConfirmed(true);
  let resolveOld;
  env.queue.push(new Promise((r) => { resolveOld = r; }));
  const old = env.ctrl.refresh();
  env.queue.push(reply(202, { state: "queued", job: { state: "queued" } }));
  await env.ctrl.start();
  assert.equal(env.ctrl.snapshot().active, true);
  resolveOld(reply(200, idle));
  await old;
  await flush();
  assert.equal(env.ctrl.snapshot().active, true, "stale idle reply dropped");
  assert.equal(env.ctrl.snapshot().canStart, false);
});

test("dispose clears the poll and ignores in-flight replies", async () => {
  const env = await enabledHarness(job("running"));
  let resolveLate;
  env.queue.push(new Promise((r) => { resolveLate = r; }));
  const late = env.ctrl.refresh();
  env.ctrl.dispose();
  const changes = env.changes;
  resolveLate(reply(200, job("completed")));
  await late;
  await flush();
  assert.deepEqual(env.pendingDelays(), []);
  assert.equal(env.changes, changes, "no render after dispose");
  assert.equal(env.finished.length, 0);
});

test("re-configuring with the same flags restarts polling only for an active job without a timer", async () => {
  const env = await enabledHarness(job("running"));
  env.ctrl.configure(ENABLED);
  await flush();
  assert.equal(env.calls.length, 1, "timer pending: no extra request");
  env.visible = false;
  await env.fire();
  env.visible = true;
  env.queue.push(reply(200, job("running")));
  env.ctrl.configure(ENABLED);
  await flush();
  assert.equal(env.calls.length, 2);
});
