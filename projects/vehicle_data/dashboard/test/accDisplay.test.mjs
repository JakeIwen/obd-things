import { test } from "node:test";
import assert from "node:assert/strict";
import { ACC_METRICS, accDisplayModel } from "../src/accDisplay.helpers.js";
import { currentAcc } from "../src/accDisplay.js";
import { connection, metricSignal } from "../src/store.js";

const AT = "2026-10-14T18:27:45.154000+00:00";
const LATER = "2026-10-14T18:27:47.034000+00:00";
function records(overrides = {}) {
  const values = { state: "engaged", mode: "adaptive", bars: 1, lead: true, setSpeed: 67, ...overrides };
  return Object.fromEntries(Object.entries(ACC_METRICS).map(([key, metric]) => [metric, {
    available: true, stale: false, value: values[key], quality: "verified",
    source: "ccan.broadcast.0x5a0", observed_at: AT,
  }]));
}

test("all four video-verified settings work independently of the lead icon", () => {
  for (const bars of [1, 2, 3, 4]) {
    for (const lead of [true, false]) {
      assert.deepEqual(accDisplayModel(records({ bars, lead }), "live"), {
        state: "engaged", mode: "adaptive", bars, lead, setSpeed: 67,
      });
    }
  }
});

test("ready and standby preserve the selected gap without implying active tracking", () => {
  assert.deepEqual(accDisplayModel(records({ state: "ready", bars: 4 }), "live"), {
    state: "ready", mode: "adaptive", bars: 4, lead: null, setSpeed: null,
  });
  assert.deepEqual(accDisplayModel(records({ state: "standby", bars: 4 }), "live"), {
    state: "standby", mode: "adaptive", bars: 4, lead: null, setSpeed: 67,
  });
  assert.equal(accDisplayModel(records({ state: "override", bars: 3 }), "live").bars, 3);
});

test("off, fixed cruise, unknown states and connection loss cannot show an adaptive gap", () => {
  for (const state of ["off", "mystery", null, "constructor"]) {
    const display = accDisplayModel(records({ state }), "live");
    assert.equal(display.bars, null);
    assert.equal(display.lead, null);
    assert.equal(display.setSpeed, null);
  }
  const fixed = accDisplayModel(records({ mode: "fixed", bars: 4 }), "live");
  assert.equal(fixed.bars, null);
  assert.equal(fixed.lead, null);
  assert.equal(fixed.setSpeed, 67);
  for (const status of ["connecting", "unavailable", "reconnecting", undefined]) {
    assert.equal(accDisplayModel(records(), status).state, null);
    assert.equal(accDisplayModel(records(), status).bars, null);
  }
});

test("state values must be strings, even when property-key coercion names a known state", () => {
  for (const state of [["engaged"], ["standby"], { value: "engaged" }, true, 2]) {
    const display = accDisplayModel(records({ state }), "live");
    assert.equal(display.state, null);
    assert.equal(display.bars, null);
    assert.equal(display.lead, null);
    assert.equal(display.setSpeed, null);
  }
});

test("stale, unverified, missing or invalid distance records never turn into a bar count", () => {
  for (const patch of [
    { stale: true }, { available: false }, { quality: "candidate" }, { source: "other" },
    { value: 0 }, { value: 5 }, { value: 2.5 }, { value: "3" }, { value: NaN },
  ]) {
    const input = records();
    Object.assign(input[ACC_METRICS.bars], patch);
    assert.equal(accDisplayModel(input, "live").bars, null);
  }
  for (const key of ["state", "mode", "bars"]) {
    const input = records();
    delete input[ACC_METRICS[key]];
    assert.equal(accDisplayModel(input, "live").bars, null);
  }
});

test("a new state observation cannot borrow still-fresh details from an earlier frame", () => {
  const input = records();
  input[ACC_METRICS.state].observed_at = LATER;
  assert.deepEqual(accDisplayModel(input, "live"), {
    state: "engaged", mode: null, bars: null, lead: null, setSpeed: null,
  });
  input[ACC_METRICS.mode].observed_at = LATER;
  assert.equal(accDisplayModel(input, "live").bars, null);
  input[ACC_METRICS.bars].observed_at = LATER;
  assert.equal(accDisplayModel(input, "live").bars, 1);
});

test("shared reactive presentation responds to frame changes, staleness and reconnects", () => {
  connection.value = { state: "live" };
  for (const [name, record] of Object.entries(records({ bars: 2 }))) metricSignal(name).value = record;
  assert.equal(currentAcc.value.bars, 2);
  const state = metricSignal(ACC_METRICS.state);
  state.value = { ...state.value, observed_at: LATER };
  assert.equal(currentAcc.value.bars, null);
  for (const [name, record] of Object.entries(records({ bars: 3 }))) {
    metricSignal(name).value = { ...record, observed_at: LATER };
  }
  assert.equal(currentAcc.value.bars, 3);
  connection.value = { state: "unavailable" };
  assert.equal(currentAcc.value.bars, null);
  connection.value = { state: "live" };
  assert.equal(currentAcc.value.bars, 3);
  const distance = metricSignal(ACC_METRICS.bars);
  distance.value = { ...distance.value, stale: true };
  assert.equal(currentAcc.value.bars, null);
});
