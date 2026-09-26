import { test } from "node:test";
import assert from "node:assert/strict";

import {
  BAND_SPANS,
  LIMITS,
  BAND_STATES,
  evaluateBand,
  tireBand,
  tireBandState,
  oilPressureFloor,
  hasBand,
  isFrontTire,
  isTire,
} from "../src/bands.js";

const RUNNING = { rpm: 1800, coolant: 195, running: true };
const PARKED = { rpm: null, coolant: null, running: false };

function state(name, value, ctx) {
  return evaluateBand(name, value, ctx).state;
}

test("BAND_SPANS match design section 5", () => {
  assert.deepEqual(BAND_SPANS["engine.coolant_temperature"], { min: 60, max: 240 });
  assert.deepEqual(BAND_SPANS["transmission.oil_temperature"], { min: 60, max: 240 });
  assert.deepEqual(BAND_SPANS["engine.oil_pressure"], { min: 0, max: 100 });
  assert.deepEqual(BAND_SPANS["battery.voltage"], { min: 10, max: 16 });
  assert.deepEqual(BAND_SPANS["engine.vvt_oil_temperature"], { min: 60, max: 240 });
  assert.deepEqual(BAND_SPANS["generator.field_duty"], { min: 0, max: 100 });
  assert.deepEqual(BAND_SPANS["engine.rpm"], { min: 0, max: 4500 });
  assert.deepEqual(BAND_SPANS["vehicle.speed"], { min: 0, max: 90 });
  assert.deepEqual(BAND_SPANS["engine.crankshaft_power"], { min: -30, max: 260 });
  assert.deepEqual(BAND_SPANS["engine.crankshaft_torque"], { min: -60, max: 240 });
  for (const w of ["fl", "fr", "rl", "rr"]) assert.ok(BAND_SPANS["tire.pressure." + w]);
  assert.ok(Object.isFrozen(BAND_SPANS));
  assert.deepEqual(BAND_STATES, ["none", "neutral", "normal", "amber", "red"]);
});

test("result shape is stable and frozen", () => {
  const r = evaluateBand("engine.coolant_temperature", 195);
  assert.deepEqual(Object.keys(r).sort(), ["hi", "label", "lo", "max", "min", "state"]);
  assert.ok(Object.isFrozen(r));
  const none = evaluateBand("engine.rpm", 1800);
  assert.deepEqual(none, { state: "none", lo: null, hi: null, min: 0, max: 4500, label: null });
  const unknown = evaluateBand("no.such.metric", 1);
  assert.deepEqual(unknown, { state: "none", lo: null, hi: null, min: null, max: null, label: null });
});

test("no-band metrics are 'none' regardless of value", () => {
  for (const name of ["engine.rpm", "vehicle.speed", "engine.crankshaft_power", "engine.crankshaft_torque", "generator.field_duty"]) {
    assert.equal(state(name, 0), "none", name);
    assert.equal(state(name, 99999), "none", name);
    assert.equal(state(name, null), "none", name);
    assert.equal(hasBand(name), false, name);
  }
  assert.equal(hasBand("engine.coolant_temperature"), true);
  assert.equal(hasBand("tire.pressure.rr"), true);
  assert.equal(hasBand("no.such.metric"), false);
});

test("coolant: warm-up neutral, 160-220 normal (hot-idle plateau), amber to 230, red at 230, critical at 240", () => {
  const n = "engine.coolant_temperature";
  assert.equal(state(n, 123.8), "neutral");
  assert.equal(evaluateBand(n, 123.8).label, "warm-up");
  assert.equal(state(n, 159.9), "neutral");
  assert.equal(state(n, 160), "normal");
  assert.equal(state(n, 195), "normal");
  assert.equal(state(n, 212), "normal");
  assert.equal(state(n, 219.2), "normal");
  assert.equal(state(n, 220), "normal");
  assert.equal(state(n, 220.1), "amber");
  assert.equal(state(n, 221), "amber");
  assert.equal(state(n, 229.9), "amber");
  assert.equal(state(n, 230), "red");
  assert.equal(evaluateBand(n, 230).label, null);
  assert.equal(state(n, 239.9), "red");
  assert.equal(state(n, 240), "red");
  assert.equal(evaluateBand(n, 240).label, "critical");
  assert.equal(state(n, null), "neutral");
  assert.equal(state(n, NaN), "neutral");
  const r = evaluateBand(n, 195);
  assert.equal(r.lo, 160);
  assert.equal(r.hi, 220);
  assert.equal(r.min, 60);
  assert.equal(r.max, 240);
});

test("transmission oil: cold neutral, 80-183 normal, amber to 230, red at 230", () => {
  const n = "transmission.oil_temperature";
  assert.equal(state(n, 75.2), "neutral");
  assert.equal(evaluateBand(n, 75.2).label, "cold");
  assert.equal(state(n, 80), "normal");
  assert.equal(state(n, 183), "normal");
  assert.equal(state(n, 183.5), "amber");
  assert.equal(state(n, 184), "amber");
  assert.equal(state(n, 229), "amber");
  assert.equal(state(n, 230), "red");
  assert.equal(state(n, null), "neutral");
  const r = evaluateBand(n, 150);
  assert.equal(r.lo, 80);
  assert.equal(r.hi, 183);
});

test("VVT oil temperature: <= 219 normal, 220-229 amber, >= 230 red", () => {
  const n = "engine.vvt_oil_temperature";
  assert.equal(state(n, 100), "normal");
  assert.equal(state(n, 219), "normal");
  assert.equal(state(n, 219.5), "amber");
  assert.equal(state(n, 220), "amber");
  assert.equal(state(n, 229.9), "amber");
  assert.equal(state(n, 230), "red");
  assert.equal(state(n, undefined), "neutral");
  const r = evaluateBand(n, 100);
  assert.equal(r.lo, 60);
  assert.equal(r.hi, 219);
});

test("oilPressureFloor by rpm band", () => {
  assert.equal(oilPressureFloor(650), 15);
  assert.equal(oilPressureFloor(850), 15);
  assert.equal(oilPressureFloor(999), 15);
  assert.equal(oilPressureFloor(1000), 22);
  assert.equal(oilPressureFloor(3000), 22);
  assert.equal(oilPressureFloor(3499), 22);
  assert.equal(oilPressureFloor(3500), 22); // "> 3,500" is strict; the gap keeps the lower floor
  assert.equal(oilPressureFloor(3500.5), 55);
  assert.equal(oilPressureFloor(3501), 55);
  assert.equal(oilPressureFloor(6303), 55);
});

test("oil pressure is neutral without fresh rpm, with the engine off, or with coolant below 160", () => {
  const n = "engine.oil_pressure";
  assert.equal(state(n, 5, { rpm: null, coolant: 195 }), "neutral");
  assert.equal(evaluateBand(n, 5, { rpm: null, coolant: 195 }).label, "no rpm");
  assert.equal(state(n, 5, { rpm: undefined, coolant: 195 }), "neutral");
  assert.equal(state(n, 5, { rpm: NaN, coolant: 195 }), "neutral");
  assert.equal(state(n, 5, {}), "neutral");
  assert.equal(state(n, 5), "neutral");
  assert.equal(state(n, 0, { rpm: 0, coolant: 195 }), "neutral");
  assert.equal(evaluateBand(n, 0, { rpm: 0, coolant: 195 }).label, "engine off");
  assert.equal(state(n, 5, { rpm: 399, coolant: 195 }), "neutral");
  assert.equal(state(n, 5, { rpm: 800, coolant: 159.9 }), "neutral");
  assert.equal(evaluateBand(n, 5, { rpm: 800, coolant: 159.9 }).label, "warm-up");
  assert.equal(state(n, 5, { rpm: 800, coolant: null }), "neutral");
  // No band can be placed while not evaluated.
  const r = evaluateBand(n, 30, { rpm: null, coolant: null });
  assert.equal(r.lo, null);
  assert.equal(r.hi, null);
  assert.equal(r.min, 0);
  assert.equal(r.max, 100);
});

test("oil pressure rpm bands when running and warm", () => {
  const n = "engine.oil_pressure";
  // Idle band: floor 15.
  assert.equal(state(n, 15, { rpm: 800, coolant: 160 }), "normal");
  assert.equal(state(n, 14.9, { rpm: 800, coolant: 160 }), "amber");
  assert.equal(evaluateBand(n, 14.9, { rpm: 800, coolant: 160 }).label, "low for rpm");
  assert.equal(state(n, 12, { rpm: 800, coolant: 160 }), "amber");
  assert.equal(state(n, 11.9, { rpm: 800, coolant: 160 }), "red");
  assert.equal(evaluateBand(n, 11.9, { rpm: 800, coolant: 160 }).label, "critical");
  assert.equal(state(n, 0, { rpm: 800, coolant: 200 }), "red");
  // Mid band: floor 22.
  assert.equal(state(n, 22, { rpm: 1000, coolant: 195 }), "normal");
  assert.equal(state(n, 21.9, { rpm: 1000, coolant: 195 }), "amber");
  assert.equal(state(n, 21.9, { rpm: 3000, coolant: 195 }), "amber");
  assert.equal(state(n, 30, { rpm: 2500, coolant: 195 }), "normal");
  // Gap 3000-3500 keeps the lower floor.
  assert.equal(state(n, 30, { rpm: 3400, coolant: 195 }), "normal");
  // High band: floor 55.
  assert.equal(state(n, 54.9, { rpm: 3501, coolant: 195 }), "amber");
  assert.equal(state(n, 55, { rpm: 3501, coolant: 195 }), "normal");
  assert.equal(state(n, 70, { rpm: 4000, coolant: 195 }), "normal");
  // Two-stage pump high mode is normal; above it is neutral, never amber.
  assert.equal(state(n, 85, { rpm: 2500, coolant: 195 }), "normal");
  assert.equal(state(n, 85.1, { rpm: 2500, coolant: 195 }), "neutral");
  assert.equal(evaluateBand(n, 90, { rpm: 2500, coolant: 195 }).label, "high");
  assert.equal(state(n, null, { rpm: 2500, coolant: 195 }), "neutral");
  const r = evaluateBand(n, 30, { rpm: 2500, coolant: 195 });
  assert.equal(r.lo, 22);
  assert.equal(r.hi, 85);
  assert.equal(evaluateBand(n, 60, { rpm: 4000, coolant: 195 }).lo, 55);
  assert.equal(evaluateBand(n, 30, { rpm: 700, coolant: 195 }).lo, 15);
});

test("voltage running table: 13.6-14.6 normal, < 12.8 amber 'reduced charge', red only via the charging rule", () => {
  const n = "battery.voltage";
  assert.equal(state(n, 14.1, RUNNING), "normal");
  assert.equal(state(n, 13.6, RUNNING), "normal");
  assert.equal(state(n, 14.6, RUNNING), "normal");
  assert.equal(state(n, 14.61, RUNNING), "neutral");
  assert.equal(state(n, 13.59, RUNNING), "neutral");
  assert.equal(state(n, 12.8, RUNNING), "neutral");
  assert.equal(state(n, 12.79, RUNNING), "amber");
  assert.equal(evaluateBand(n, 12.79, RUNNING).label, "reduced charge");
  // Running voltage never goes red on its own, however low.
  assert.equal(state(n, 11.0, RUNNING), "amber");
  assert.equal(state(n, 10.0, RUNNING), "amber");
  // ...only when the charging-failure rule fires.
  const failing = { rpm: 1800, coolant: 195, running: true, chargingFailure: true };
  assert.equal(state(n, 11.9, failing), "red");
  assert.equal(evaluateBand(n, 11.9, failing).label, "charging failure");
  assert.equal(state(n, null, RUNNING), "neutral");
  const r = evaluateBand(n, 14.1, RUNNING);
  assert.equal(r.lo, 13.6);
  assert.equal(r.hi, 14.6);
  assert.equal(r.min, 10);
  assert.equal(r.max, 16);
});

test("voltage parked table: >= 12.2 normal, 11.8-12.2 amber, < 11.8 red, < 11.5 critical", () => {
  const n = "battery.voltage";
  assert.equal(state(n, 12.6, PARKED), "normal");
  assert.equal(state(n, 12.2, PARKED), "normal");
  assert.equal(state(n, 12.19, PARKED), "amber");
  assert.equal(state(n, 11.8, PARKED), "amber");
  assert.equal(state(n, 11.79, PARKED), "red");
  assert.equal(evaluateBand(n, 11.79, PARKED).label, null);
  assert.equal(state(n, 11.5, PARKED), "red");
  assert.equal(state(n, 11.49, PARKED), "red");
  assert.equal(evaluateBand(n, 11.49, PARKED).label, "critical");
  // Anything but running === true uses the parked table.
  assert.equal(state(n, 12.6, { running: null }), "normal");
  assert.equal(state(n, 12.6, {}), "normal");
  assert.equal(state(n, 12.6), "normal");
  assert.equal(state(n, 12.0, { running: undefined }), "amber");
  assert.equal(state(n, null, PARKED), "neutral");
  const r = evaluateBand(n, 12.6, PARKED);
  assert.equal(r.lo, 12.2);
  assert.equal(r.hi, 16);
});

test("tire helpers", () => {
  assert.equal(isFrontTire("tire.pressure.fl"), true);
  assert.equal(isFrontTire("tire.pressure.fr"), true);
  assert.equal(isFrontTire("tire.pressure.rl"), false);
  assert.equal(isTire("tire.pressure.rr"), true);
  assert.equal(isTire("engine.rpm"), false);
  assert.equal(isTire(null), false);
});

test("tireBandState against the cold baseline", () => {
  const front = { coldBaseline: 55, isFront: true };
  assert.equal(tireBandState(55, front), "normal");
  assert.equal(tireBandState(63, front), "normal"); // +8
  assert.equal(tireBandState(63.1, front), "neutral"); // above +8: outside envelope, no rule
  assert.equal(tireBandState(52, front), "normal"); // -3
  assert.equal(tireBandState(51.9, front), "amber");
  assert.equal(tireBandState(50, front), "amber"); // -5, still at the floor
  assert.equal(tireBandState(49.9, front), "red"); // below -5 and below the 50 floor
  const rear = { coldBaseline: 78, isFront: false };
  assert.equal(tireBandState(78, rear), "normal");
  assert.equal(tireBandState(75, rear), "normal");
  assert.equal(tireBandState(74.9, rear), "amber");
  assert.equal(tireBandState(74, rear), "amber");
  assert.equal(tireBandState(73, rear), "amber"); // exactly -5 is amber
  assert.equal(tireBandState(72.9, rear), "red"); // below -5
  assert.equal(tireBandState(null, rear), "neutral");
  assert.equal(tireBandState(NaN, rear), "neutral");
});

test("tire absolute floors apply regardless of baseline: 50 front, 68 rear", () => {
  // A high baseline does not rescue a wheel under the floor.
  assert.equal(tireBandState(49.9, { coldBaseline: 52, isFront: true }), "red");
  assert.equal(tireBandState(67.9, { coldBaseline: 70, isFront: false }), "red");
  assert.equal(tireBand(67.9, { coldBaseline: 70, isFront: false }).label, "under 68 psi floor");
  // Without a baseline only the floor can colour the wheel red...
  assert.equal(tireBandState(49.9, { isFront: true }), "red");
  assert.equal(tireBandState(50, { isFront: true }), "neutral");
  assert.equal(tireBandState(67.9, { isFront: false }), "red");
  assert.equal(tireBandState(68, { coldBaseline: null, isFront: false }), "neutral");
  assert.equal(tireBand(68, { coldBaseline: null, isFront: false }).label, "no baseline");
  // ...and the front/rear choice matters.
  assert.equal(tireBandState(60, { isFront: true }), "neutral");
  assert.equal(tireBandState(60, { isFront: false }), "red");
});

test("tire pair-delta rule: excess over the usual offset > 3 psi is amber", () => {
  const rear = { coldBaseline: 78, isFront: false, pairDeltaExcess: 3 };
  assert.equal(tireBandState(78, rear), "normal");
  assert.equal(tireBandState(78, { ...rear, pairDeltaExcess: 3.1 }), "amber");
  assert.equal(tireBand(78, { ...rear, pairDeltaExcess: 3.1 }).label, "pair mismatch");
  // Pair mismatch never downgrades a red, and applies without a baseline too.
  assert.equal(tireBandState(72, { ...rear, pairDeltaExcess: 6 }), "red");
  assert.equal(tireBandState(74, { ...rear, pairDeltaExcess: 6 }), "amber");
  assert.equal(tireBandState(75, { isFront: false, pairDeltaExcess: 6 }), "amber");
  assert.equal(tireBandState(75, { isFront: false, pairDeltaExcess: null }), "neutral");
});

test("tireBand exposes delta, floor and the normal window", () => {
  const r = tireBand(54.1, { coldBaseline: 57.5, isFront: true, pairDeltaExcess: 0.4 });
  assert.equal(r.state, "amber");
  assert.ok(Math.abs(r.delta - -3.4) < 1e-9);
  assert.equal(r.floor, 50);
  assert.equal(r.lo, 54.5);
  assert.equal(r.hi, 65.5);
  const none = tireBand(54.1, { isFront: true });
  assert.equal(none.delta, null);
  assert.equal(none.lo, null);
  assert.equal(none.hi, null);
  assert.equal(tireBand(54.1).floor, 68); // rear when unspecified
});

test("evaluateBand for tire metrics uses ctx.baseline and infers front/rear from the name", () => {
  const fl = "tire.pressure.fl";
  const rr = "tire.pressure.rr";
  assert.equal(state(fl, 54.1, { baseline: { cold: 55, pairDelta: 0.5 } }), "normal");
  assert.equal(state(fl, 51.5, { baseline: { cold: 55, pairDelta: 0.5 } }), "amber");
  assert.equal(state(fl, 49.5, { baseline: { cold: 55, pairDelta: 0.5 } }), "red");
  assert.equal(state(fl, 55, { baseline: { cold: 55, pairDelta: 3.5 } }), "amber");
  assert.equal(state(rr, 74.8, { baseline: { cold: 78, pairDelta: null } }), "amber");
  assert.equal(state(rr, 74.8, { baseline: { cold: 76, pairDelta: null } }), "normal");
  assert.equal(state(rr, 74.8, { baseline: { cold: null, pairDelta: null } }), "neutral");
  assert.equal(state(rr, 74.8, {}), "neutral");
  assert.equal(state(rr, 67, {}), "red");
  assert.equal(state(fl, 67, {}), "neutral");
  // An explicit isFront in the baseline wins over the name.
  assert.equal(state(fl, 60, { baseline: { cold: null, pairDelta: null, isFront: false } }), "red");
  const r = evaluateBand(rr, 74.8, { baseline: { cold: 78, pairDelta: null } });
  assert.equal(r.lo, 75);
  assert.equal(r.hi, 86);
  assert.equal(r.min, 60);
  assert.equal(r.max, 90);
  assert.equal(r.label, "low");
});

test("evaluateBand is reference-stable while nothing visible changes", () => {
  const n = "engine.coolant_temperature";
  const a = evaluateBand(n, 195.2);
  const b = evaluateBand(n, 196.7);
  assert.equal(a, b);
  const c = evaluateBand(n, 225);
  assert.notEqual(a, c);
  assert.equal(c.state, "amber");
  const d = evaluateBand(n, 195);
  assert.equal(d.state, "normal");
  assert.notEqual(d, c);
  // Oil pressure: same state and floor -> same object; new rpm band -> new object.
  const o = "engine.oil_pressure";
  const o1 = evaluateBand(o, 40, { rpm: 1500, coolant: 195 });
  const o2 = evaluateBand(o, 41, { rpm: 2900, coolant: 196 });
  assert.equal(o1, o2);
  const o3 = evaluateBand(o, 41, { rpm: 3600, coolant: 196 });
  assert.notEqual(o1, o3);
  assert.equal(o3.state, "amber");
  // Tires: a drifting baseline changes lo/hi and therefore the object.
  const t = "tire.pressure.rl";
  const t1 = evaluateBand(t, 74, { baseline: { cold: 76, pairDelta: 0 } });
  const t2 = evaluateBand(t, 74.5, { baseline: { cold: 76, pairDelta: 0.2 } });
  assert.equal(t1, t2);
  const t3 = evaluateBand(t, 74.5, { baseline: { cold: 76.5, pairDelta: 0.2 } });
  assert.notEqual(t1, t3);
  // Each metric name has its own memo.
  const c1 = evaluateBand("engine.coolant_temperature", 195);
  const tr1 = evaluateBand("transmission.oil_temperature", 150);
  assert.notEqual(c1, tr1);
  assert.equal(evaluateBand("engine.coolant_temperature", 200), c1);
});

test("LIMITS carry the design numbers", () => {
  assert.equal(LIMITS.coolant.red, 230);
  assert.equal(LIMITS.coolant.critical, 240);
  assert.equal(LIMITS.transmission.red, 230);
  assert.equal(LIMITS.oil.critical, 12);
  assert.equal(LIMITS.oil.minCoolant, 160);
  assert.equal(LIMITS.oil.minRpm, 400);
  assert.equal(LIMITS.voltageRunning.reducedCharge, 12.8);
  assert.equal(LIMITS.voltageParked.amberLo, 11.8);
  assert.equal(LIMITS.voltageParked.critical, 11.5);
  assert.equal(LIMITS.tires.floorFront, 50);
  assert.equal(LIMITS.tires.floorRear, 68);
  assert.equal(LIMITS.tires.pairExcess, 3);
  assert.equal(LIMITS.oil.idleFloor, 15);
  assert.equal(LIMITS.oil.midFloor, 22);
  assert.equal(LIMITS.oil.highFloor, 55);
  assert.ok(Object.isFrozen(LIMITS.oil));
});

test("voltage: explicit modes select running, parked or neutral tables", () => {
  const n = "battery.voltage";
  assert.equal(evaluateBand(n, 11.9, { voltageMode: "neutral" }).state, "neutral");
  assert.equal(evaluateBand(n, 11.9, { voltageMode: "neutral" }).label, "settling");
  assert.equal(evaluateBand(n, 11.9, { voltageMode: "parked" }).state, "amber");
  assert.equal(evaluateBand(n, 11.7, { voltageMode: "parked" }).state, "red");
  assert.equal(evaluateBand(n, 12.5, { voltageMode: "parked" }).state, "normal");
  assert.equal(evaluateBand(n, 14.0, { voltageMode: "running" }).state, "normal");
  assert.equal(evaluateBand(n, 12.6, { voltageMode: "running" }).state, "amber");
  // voltageMode wins over the legacy running flag
  assert.equal(evaluateBand(n, 12.6, { voltageMode: "neutral", running: true }).state, "neutral");
});
