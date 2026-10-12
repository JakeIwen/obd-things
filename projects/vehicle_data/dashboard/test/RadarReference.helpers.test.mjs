// Radar reference: the shared request and wording (src/radarReference.js) and the dialog's
// controller and view model (src/dialogs/RadarReference.helpers.js).
process.env.TZ = "America/Denver";

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as R from "../src/radarReference.js";
import * as H from "../src/dialogs/RadarReference.helpers.js";

const NOW = new Date("2026-10-12T02:00:00Z"); // 8:00 pm MDT on October 11

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

const point = (id, extra = {}) => ({
  id: id.repeat(32),
  source: "manual",
  created_at: "2026-10-12T01:40:00+00:00",
  observed_at: "2026-10-12T01:39:55+00:00",
  angles: { elevation: -3.766, azimuth: -0.068 },
  request_id: null,
  ...extra,
});
const state = (extra = {}) => ({
  available: true,
  persistent: true,
  storage_error: null,
  current: { angles: { elevation: -3.7, azimuth: -0.05 }, observed_at: "2026-10-12T01:55:00+00:00" },
  selected: null,
  selected_at: null,
  points: [],
  pending_calibration: null,
  ...extra,
});

/** A controller on scripted replies; records requests, published states and reported ui states. */
function harness(replies) {
  const calls = [];
  const states = [];
  const seen = [];
  const ctrl = H.createRadarReferenceController({
    fetch: async (url, init) => {
      calls.push([url, init]);
      const next = replies.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    onState: (s) => states.push(s),
    onChange: (ui) => seen.push(ui),
  });
  return { ctrl, calls, states, seen };
}

test("the request bodies and path are the ones the Pi loads from the same file", () => {
  const raw = JSON.parse(readFileSync(new URL("../../radar_reference.json", import.meta.url), "utf8"));
  assert.deepEqual(R.RADAR_REFERENCE, raw);
  assert.equal(raw.path, "/v1/radar/alignment-reference");
});

test("exchange reads without a body, posts JSON with one, and rejects a reply that is not an object", async () => {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push([url, init]);
    return reply(200, state());
  };
  assert.equal((await R.exchange(fetchFn)).ok, true);
  await R.exchange(fetchFn, { action: "zero" });
  assert.deepEqual(calls[0], [R.RADAR_REFERENCE.path, { cache: "no-store" }]);
  assert.deepEqual(calls[1][1], {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: '{"action":"zero"}',
    cache: "no-store",
  });
  await assert.rejects(R.exchange(async () => reply(502, null, { json: false })), /HTTP 502/);
  await assert.rejects(R.exchange(async () => reply(200, [1])), /unreadable reply/);
});

test("wording: angles, saved points and the change since one", () => {
  assert.equal(R.anglesText({ elevation: -3.766, azimuth: -0.068 }), "vertical −3.77° · horizontal −0.07°");
  assert.equal(R.anglesText(null), "vertical — · horizontal —");
  assert.equal(R.pointText(point("a"), NOW), "Zeroed here · 7:40 pm");
  assert.equal(R.pointText(point("b", { source: "calibration", request_id: "c".repeat(32) }), NOW), "After calibration · 7:40 pm");
  assert.equal(R.pointText({ source: "later-kind", created_at: "2026-10-09T18:00:00+00:00" }, NOW), "Saved · Oct 9, 12:00 pm");
  assert.ok(Math.abs(R.sinceReference(point("a"), "elevation", -3.5) - 0.266) < 1e-9);
  assert.equal(R.sinceReference(point("a"), "elevation", null), null);
  assert.equal(R.sinceReference(null, "elevation", 1), null);
  assert.equal(R.sinceReference({ angles: {} }, "azimuth", 1), null);
});

test("selectedPoint finds the selected point and nothing else", () => {
  const a = point("a");
  assert.equal(R.selectedPoint(state({ points: [point("b"), a], selected: a.id })), a);
  assert.equal(R.selectedPoint(state({ points: [a] })), null);
  assert.equal(R.selectedPoint(state({ points: [a], selected: "f".repeat(32) })), null);
  assert.equal(R.selectedPoint({ available: false, detail: "refused" }), null);
  assert.equal(R.selectedPoint(null), null);
});

test("controller: opening reads the state; zero, select and clear post the exact bodies", async () => {
  const a = point("a");
  const zeroed = state({ points: [a], selected: a.id });
  const h = harness([reply(200, state()), reply(201, zeroed), reply(200, state({ points: [a] })), reply(200, zeroed)]);
  assert.equal(await h.ctrl.refresh(), true);
  assert.equal(await h.ctrl.zero(), true);
  assert.equal(await h.ctrl.clear(), true);
  assert.equal(await h.ctrl.select(a.id), true);
  assert.deepEqual(h.calls.map(([, init]) => init.body), [
    undefined,
    '{"action":"zero"}',
    '{"action":"clear"}',
    JSON.stringify({ action: "select", id: a.id }),
  ]);
  assert.deepEqual(h.states.map((s) => s.selected), [null, a.id, null, a.id]);
  assert.deepEqual(h.ctrl.state(), H.initialState());
  assert.deepEqual(h.seen[2], { busy: "zero", error: null, notice: null });
});

test("controller: refusals explain themselves and only real states are published", async () => {
  const h = harness([
    reply(409, state({ available: false, current: null, reason: "no_reading", detail: "the radar angles have not been read yet" })),
    reply(403, { available: false, detail: "Radar references require a same-origin request" }),
    reply(500, { available: false }),
    new Error("Failed to fetch"),
    reply(502, null, { json: false }),
  ]);
  assert.equal(await h.ctrl.zero(), false);
  assert.equal(h.ctrl.state().notice, "the radar angles have not been read yet");
  assert.equal(h.states.length, 1); // the broker's refusal still carries the state
  assert.equal(await h.ctrl.zero(), false);
  assert.equal(h.ctrl.state().notice, "Radar references require a same-origin request");
  assert.equal(await h.ctrl.clear(), false);
  assert.equal(h.ctrl.state().notice, "refused");
  assert.equal(h.states.length, 1);
  assert.equal(await h.ctrl.zero(), false);
  assert.deepEqual(h.ctrl.state(), { busy: null, error: "Request failed: Failed to fetch", notice: null });
  assert.equal(await h.ctrl.refresh(), false);
  assert.equal(h.ctrl.state().error, "Could not read the saved references: HTTP 502");
});

test("controller: one request at a time, and nothing is reported after the dialog closes", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const calls = [];
  const seen = [];
  const ctrl = H.createRadarReferenceController({
    fetch: async (url, init) => {
      calls.push(init);
      await gate;
      return reply(201, state());
    },
    onState: (s) => seen.push(s),
    onChange: (ui) => seen.push(ui),
  });
  const first = ctrl.zero();
  assert.equal(await ctrl.zero(), false); // second tap while the first is in flight
  assert.equal(await ctrl.clear(), false);
  assert.equal(calls.length, 1);
  ctrl.dispose();
  const reported = seen.length;
  release();
  assert.equal(await first, false);
  assert.equal(seen.length, reported);
  assert.equal(await ctrl.refresh(), false);
});

test("view model: before the first answer, without a reading, and with points", () => {
  const idle = H.initialState();
  const before = H.viewModel(idle, null, NOW);
  assert.deepEqual(
    [before.currentText, before.selectedText, before.canZero, before.canClear, before.rows, before.statusText],
    [H.TEXT.checking, H.TEXT.none, false, false, [], ""],
  );
  const empty = H.viewModel(idle, state({ current: null }), NOW);
  assert.deepEqual([empty.currentText, empty.canZero], [H.TEXT.noReading, false]);

  const a = point("a");
  const parked = point("b", { created_at: "2026-10-11T20:00:00+00:00", observed_at: "2026-10-11T18:30:00+00:00" });
  const vm = H.viewModel(idle, state({ points: [a, parked], selected: a.id }), NOW);
  assert.equal(vm.currentText, "vertical −3.70° · horizontal −0.05° · read 7:55 pm");
  assert.equal(vm.selectedText, "Zeroed here · 7:40 pm (vertical −3.77° · horizontal −0.07°)");
  assert.deepEqual([vm.canZero, vm.canClear, vm.zeroLabel], [true, true, H.TEXT.zero]);
  assert.deepEqual(vm.rows, [
    { id: a.id, title: "Zeroed here · 7:40 pm", meta: "vertical −3.77° · horizontal −0.07°", selected: true, canUse: true },
    // Zeroed while parked: the reading it copied was 90 minutes old, so its time is shown.
    { id: parked.id, title: "Zeroed here · 2:00 pm", meta: "vertical −3.77° · horizontal −0.07° · read 12:30 pm", selected: false, canUse: true },
  ]);
});

test("view model: busy, storage trouble and a waiting calibration", () => {
  const a = point("a");
  const busy = H.viewModel({ busy: "zero", error: null, notice: null }, state({ points: [a], selected: a.id }), NOW);
  assert.deepEqual([busy.canZero, busy.canClear, busy.rows[0].canUse, busy.zeroLabel], [false, false, false, H.TEXT.zeroing]);

  const broken = H.viewModel(H.initialState(), state({ available: false, storage_error: "Radar reference storage could not be read: bad file" }), NOW);
  assert.deepEqual([broken.canZero, broken.statusIsError], [false, true]);
  assert.match(broken.statusText, /could not be read/);

  const waiting = H.viewModel(H.initialState(), state({ pending_calibration: { request_id: "c".repeat(32), completed_at: "2026-10-12T01:50:00+00:00" } }), NOW);
  assert.deepEqual([waiting.statusText, waiting.statusIsError], [H.TEXT.waiting, false]);

  const refused = H.viewModel({ busy: null, error: null, notice: "no saved reference has that id" }, state(), NOW);
  assert.deepEqual([refused.statusText, refused.statusIsError], ["no saved reference has that id", false]);
});
