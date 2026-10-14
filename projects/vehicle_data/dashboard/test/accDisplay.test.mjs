import { test } from "node:test";
import assert from "node:assert/strict";
import { ACC_METRICS, accDisplayModel, accTileModel, accShownParts } from "../src/accDisplay.helpers.js";
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

test("accTileModel: set speed with the state word; Off and ready have no number", () => {
  assert.deepEqual(accTileModel("engaged", 66), { value: "66", unit: "mph", sub: "engaged", kind: "live" });
  assert.deepEqual(accTileModel("override", 61), { value: "61", unit: "mph", sub: "accelerator override", kind: "live" });
  assert.deepEqual(accTileModel("standby", 66), { value: "66", unit: "mph", sub: "standby · saved speed", kind: "held" });
  // A cached set speed never shows once the state says off or ready.
  assert.deepEqual(accTileModel("off", 66), { value: "Off", unit: "", sub: "", kind: "off" });
  assert.deepEqual(accTileModel("ready", 66), { value: "—", unit: "", sub: "ready · no set speed", kind: "off" });
  // No current state record: hide even a still-fresh cached set speed.
  assert.deepEqual(accTileModel(null, 66), { value: "—", unit: "", sub: "", kind: "off" });
  assert.deepEqual(accTileModel(null, null), { value: "—", unit: "", sub: "", kind: "off" });
  assert.deepEqual(accTileModel("engaged", null), { value: "—", unit: "", sub: "engaged", kind: "off" });
  assert.deepEqual(accTileModel("mystery", 50), { value: "—", unit: "", sub: "", kind: "off" });
  for (const state of ["off", "ready", "engaged", "override", "standby", null]) {
    const m = accTileModel(state, 60);
    for (const t of [m.value, m.sub]) assert.doesNotMatch(t, /candidate|0x5a0|quality|stale/i, t);
  }
});

test("accTileModel: lead icon stays separate from the distance graphic and fixed cruise", () => {
  const adaptive = (bars, lead) => ({ mode: "adaptive", bars, lead });
  // 7:44 PM: following a vehicle at four bars, set 66.
  assert.deepEqual(accTileModel("engaged", 66, adaptive(4, true)), { value: "66", unit: "mph", sub: "engaged · vehicle ahead", kind: "live" });
  // 7:00 PM: one bar, no vehicle.
  assert.deepEqual(accTileModel("engaged", 73, adaptive(1, false)), { value: "73", unit: "mph", sub: "engaged", kind: "live" });
  assert.deepEqual(accTileModel("override", 40, adaptive(4, true)), { value: "40", unit: "mph", sub: "accelerator override · vehicle ahead", kind: "live" });
  // Standby and ready never show a lead icon; distance is drawn separately.
  assert.deepEqual(accTileModel("standby", 61, adaptive(4, true)), { value: "61", unit: "mph", sub: "standby · saved speed", kind: "held" });
  assert.deepEqual(accTileModel("ready", null, adaptive(1, null)), { value: "—", unit: "", sub: "ready · no set speed", kind: "off" });
  // 7:05 PM: regular cruise on, set 65, cancel.
  const fixed = { mode: "fixed", bars: 3, lead: true };
  assert.deepEqual(accTileModel("ready", null, fixed), { value: "—", unit: "", sub: "ready · fixed cruise", kind: "off" });
  assert.deepEqual(accTileModel("engaged", 65, fixed), { value: "65", unit: "mph", sub: "engaged · fixed cruise", kind: "live" });
  assert.deepEqual(accTileModel("standby", 65, fixed), { value: "65", unit: "mph", sub: "standby · saved speed · fixed cruise", kind: "held" });
  // Off hides everything, whatever is still cached.
  assert.deepEqual(accTileModel("off", 65, fixed), { value: "Off", unit: "", sub: "", kind: "off" });
  assert.deepEqual(accTileModel("off", 66, adaptive(4, true)), { value: "Off", unit: "", sub: "", kind: "off" });
  // Missing mode details still retain a known cruise state.
  for (const shown of [null, undefined, {}, { mode: null, bars: null, lead: null }, { mode: "mystery", bars: 2, lead: true }]) {
    assert.deepEqual(accTileModel("engaged", 66, shown), { value: "66", unit: "mph", sub: "engaged", kind: "live" });
    assert.deepEqual(accTileModel("ready", 66, shown), { value: "—", unit: "", sub: "ready · no set speed", kind: "off" });
  }
  assert.deepEqual(accTileModel("engaged", 66, adaptive(7, false)), { value: "66", unit: "mph", sub: "engaged", kind: "live" }, "distance is not repeated in the status text");
  assert.deepEqual(accTileModel("engaged", 66, adaptive(null, true)), { value: "66", unit: "mph", sub: "engaged · vehicle ahead", kind: "live" });
  // Without a current state nothing beside it is shown either.
  assert.deepEqual(accTileModel(null, 66, adaptive(4, true)), { value: "—", unit: "", sub: "", kind: "off" });
  assert.deepEqual(accShownParts("engaged", adaptive(2, true)), ["vehicle ahead"]);
  assert.deepEqual(accShownParts(null, adaptive(2, true)), []);
  for (const st of ["ready", "engaged", "override", "standby"]) {
    for (const shown of [adaptive(3, true), fixed]) {
      assert.doesNotMatch(accTileModel(st, 60, shown).sub, /candidate|0x5a0|quality|stale|index|hud/i);
    }
  }
});
