import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as H from "../src/dialogs/RadarCalibration.helpers.js";

const S = H.RADAR_SERVICE.states;

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

const LIMITS = { stationary_max_mph: 0.5, stall_seconds: 600, max_run_seconds: 3600 };
const status = (helper, run = null, extra = {}) => ({
  available: true,
  helper: { running: false, ready: false, detail: "", ...helper },
  run,
  limits: LIMITS,
  log_error: null,
  ...extra,
});
const READY = { running: true, ready: true };
const run = (state, extra = {}) => ({
  request_id: "a".repeat(32),
  state,
  reason: null,
  detail: "test",
  progress: null,
  routine_status: null,
  elapsed_seconds: null,
  speed_mph: null,
  requested_at: "2026-10-10T23:00:00Z",
  updated_at: "2026-10-10T23:00:01Z",
  finished_at: null,
  stop_requested: false,
  ...extra,
});

/** Controller harness with a scripted fetch and manual timers. */
function harness(replies = []) {
  const env = { calls: [], queue: replies.slice(), timers: new Map(), visible: true, changes: 0 };
  let nextTimer = 1;
  env.ctrl = H.createRadarCalibrationController({
    fetch: async (url, init) => {
      env.calls.push({ url, init: init || {} });
      const next = env.queue.shift();
      if (!next) throw new Error("no scripted reply for " + url);
      if (next instanceof Error) throw next;
      return next;
    },
    isVisible: () => env.visible,
    setTimeout: (fn, ms) => {
      const id = nextTimer++;
      env.timers.set(id, { fn, ms });
      return id;
    },
    clearTimeout: (id) => env.timers.delete(id),
    onChange: () => {
      env.changes += 1;
    },
  });
  env.vm = (speedMph = 0) => H.viewModel(env.ctrl.state(), { speedMph });
  env.tick = async () => {
    const [id, timer] = [...env.timers.entries()][0];
    env.timers.delete(id);
    await timer.fn();
  };
  return env;
}

test("the request bodies and path are the broker's shared vocabulary", () => {
  const raw = JSON.parse(readFileSync(new URL("../../radar_service_alignment.json", import.meta.url), "utf8"));
  assert.deepEqual(H.RADAR_SERVICE, raw);
  assert.equal(raw.path, "/v1/radar/service-alignment");
  assert.deepEqual(Object.keys(raw.start_request).sort(), [
    "action",
    "confirm_drive_without_acc",
    "confirm_stationary_engine_running",
  ]);
  for (const reason of raw.reasons) assert.ok(H.REASON_TEXT[reason], "no sentence for " + reason);
  for (const state of raw.active_states) assert.ok(H.isActive({ state }), state);
  assert.equal(H.isActive({ state: S.completed }), false);
});

test("durations read as minutes and seconds", () => {
  assert.equal(H.durationText(45.9), "45 s");
  assert.equal(H.durationText(1013.4), "16 min 53 s");
  assert.equal(H.durationText(null), "");
  assert.equal(H.durationText(-1), "");
});

test("start is offered only when the Pi is ready, the van is stopped and the box is ticked", async () => {
  const env = harness([reply(200, status({ running: false }))]);
  assert.equal(env.vm().hint, H.TEXT.checking);
  assert.equal(env.vm().canStart, false);
  await env.ctrl.refresh();
  assert.equal(env.vm().hint, H.TEXT.needEngine);
  env.queue.push(reply(200, status({ running: true, ready: false })));
  await env.tick();
  assert.equal(env.vm().hint, H.TEXT.needRadar);
  env.queue.push(reply(200, status(READY)));
  await env.tick();
  assert.equal(env.vm(null).hint, H.TEXT.needSpeed);
  assert.equal(env.vm(12).hint, H.TEXT.needStop);
  assert.equal(env.vm(0).hint, H.TEXT.needConfirm);
  assert.equal(env.vm(0).canStart, false);
  env.ctrl.setConfirmed(true);
  assert.equal(env.vm(0).hint, H.TEXT.ready);
  assert.equal(env.vm(0).canStart, true);
  assert.equal(env.vm(0.4).canStart, true);
  assert.equal(env.vm(3).canStart, false);
  assert.equal(env.vm(0).showRun, false);
});

test("start posts the exact body, clears the tick and shows the run", async () => {
  const env = harness([reply(200, status(READY)), reply(202, status(READY, run(S.requested)))]);
  await env.ctrl.refresh();
  env.ctrl.setConfirmed(true);
  assert.equal(await env.ctrl.start(), true);
  const post = env.calls[1];
  assert.equal(post.url, "/v1/radar/service-alignment");
  assert.equal(post.init.method, "POST");
  assert.equal(post.init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(post.init.body), {
    action: "start",
    confirm_stationary_engine_running: true,
    confirm_drive_without_acc: true,
  });
  const vm = env.vm();
  assert.equal(vm.active, true);
  assert.equal(vm.headline, "Starting…");
  assert.equal(vm.confirmed, false);
  assert.equal(vm.canStart, false);
  assert.equal(vm.canStop, true);
  assert.equal(vm.progressText, "—");
});

test("a running calibration shows progress, time and speed; stop posts once", async () => {
  const running = run(S.running, { progress: 37, elapsed_seconds: 185.2 });
  const env = harness([reply(200, status(READY, running))]);
  await env.ctrl.refresh();
  let vm = env.vm(41.6);
  assert.equal(vm.headline, "Calibrating");
  assert.equal(vm.progressText, "37%");
  assert.equal(vm.progress, 37);
  assert.equal(vm.elapsedText, "3 min 5 s");
  assert.equal(vm.speedText, "42 mph");
  assert.equal(vm.hint, H.TEXT.drive);
  assert.equal(vm.canConfirm, false);
  env.queue.push(reply(202, status(READY, { ...running, stop_requested: true })));
  assert.equal(await env.ctrl.stop(), true);
  assert.deepEqual(JSON.parse(env.calls[1].init.body), { action: "stop" });
  vm = env.vm(41.6);
  assert.equal(vm.canStop, false);
  assert.equal(vm.stopLabel, H.TEXT.stopping);
});

test("finished runs read plainly and allow another start", async () => {
  const done = run(S.completed, { progress: 100, elapsed_seconds: 1013.4, finished_at: "2026-10-10T23:17:00Z" });
  const env = harness([reply(200, status(READY, done))]);
  await env.ctrl.refresh();
  let vm = env.vm(30);
  assert.equal(vm.headline, "Calibration complete");
  assert.equal(vm.tone, "ok");
  assert.equal(vm.active, false);
  assert.equal(vm.finishedAt, "2026-10-10T23:17:00Z");
  assert.equal(vm.hint, H.TEXT.needStop);
  env.queue.push(reply(200, status(READY, run(S.failed, { reason: "stalled", progress: 55, detail: "progress stayed at 55% for 10 minutes" }))));
  await env.tick();
  vm = env.vm(0);
  assert.equal(vm.tone, "red");
  assert.equal(vm.headline, "Calibration did not finish");
  assert.match(vm.reasonText, /10 minutes/);
  assert.equal(vm.detail, "progress stayed at 55% for 10 minutes");
  assert.equal(vm.canConfirm, true);
});

test("a broker refusal keeps the status and shows its reason for a few polls", async () => {
  const refusal = status({ running: false }, null, { available: false, reason: "helper_not_ready", detail: "the engine is not running" });
  const env = harness([reply(200, status(READY)), reply(409, refusal)]);
  await env.ctrl.refresh();
  env.ctrl.setConfirmed(true);
  assert.equal(await env.ctrl.start(), false);
  let vm = env.vm();
  assert.equal(vm.statusText, "the engine is not running");
  assert.equal(vm.statusIsError, false);
  assert.equal(vm.hint, H.TEXT.needEngine);
  assert.equal(vm.confirmed, true);
  for (let i = 0; i < H.NOTICE_POLLS; i += 1) {
    assert.equal(env.vm().statusText, "the engine is not running");
    env.queue.push(reply(200, status(READY)));
    await env.tick();
  }
  assert.equal(env.vm().statusText, "");
});

test("a listener refusal without status fields does not enable anything", async () => {
  const env = harness([
    reply(200, status(READY)),
    reply(403, { available: false, reason: "origin_rejected", detail: "requires a trusted same-origin request" }),
  ]);
  await env.ctrl.refresh();
  env.ctrl.setConfirmed(true);
  assert.equal(await env.ctrl.start(), false);
  const vm = env.vm();
  assert.equal(vm.statusText, "requires a trusted same-origin request");
  assert.equal(vm.active, false);
  assert.deepEqual(env.ctrl.state().payload.helper, { running: true, ready: true, detail: "" });
});

test("a listener without the feature says so and stops polling", async () => {
  const env = harness([reply(403, { available: false, reason: "radar_service_disabled", detail: "not enabled" })]);
  await env.ctrl.refresh();
  const vm = env.vm();
  assert.equal(vm.disabled, true);
  assert.equal(vm.hint, H.TEXT.disabled);
  assert.equal(vm.canStart, false);
  assert.equal(vm.canConfirm, false);
  assert.equal(env.ctrl.hasTimer(), false);
});

test("status errors are shown, retried more slowly, and never reuse a stale reply", async () => {
  const env = harness([new Error("network down")]);
  await env.ctrl.refresh();
  assert.match(env.vm().statusText, /network down/);
  assert.equal(env.vm().statusIsError, true);
  assert.equal([...env.timers.values()][0].ms, H.POLL_ERROR_MS);
  env.queue.push(reply(502, null, { json: false }));
  await env.tick();
  assert.match(env.vm().statusText, /HTTP 502/);
  env.queue.push(reply(200, status(READY)));
  await env.tick();
  assert.equal(env.vm().statusText, "");
  assert.equal([...env.timers.values()][0].ms, H.POLL_MS);
});

test("hidden pages do not fetch, and a disposed controller goes quiet", async () => {
  const env = harness([reply(200, status(READY))]);
  env.visible = false;
  await env.ctrl.refresh();
  assert.equal(env.calls.length, 0);
  assert.equal(env.ctrl.hasTimer(), true);
  env.visible = true;
  await env.tick();
  assert.equal(env.calls.length, 1);
  env.ctrl.dispose();
  assert.equal(env.ctrl.hasTimer(), false);
  const before = env.changes;
  assert.equal(await env.ctrl.refresh(), false);
  assert.equal(await env.ctrl.start(), false);
  assert.equal(env.changes, before);
});

test("the dialog has no inline styles and the System card offers it only behind the web flag", () => {
  const dialog = readFileSync(new URL("../src/dialogs/RadarCalibration.jsx", import.meta.url), "utf8");
  assert.doesNotMatch(dialog, /\sstyle=/);
  const system = readFileSync(new URL("../src/views/System.jsx", import.meta.url), "utf8");
  assert.match(system, /radar_service_enabled === true/);
  assert.match(system, /openDialog\("radar"\)/);
});
