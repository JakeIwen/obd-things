import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { effect } from "@preact/signals-core";

import * as store from "../src/store.js";
import * as derive from "../src/app/derive.js";
import { settings, saveSettings } from "../src/settings.js";
import * as can from "../src/canAvailability.js";
import * as system from "../src/views/system.helpers.js";
import * as parked from "../src/views/parked.helpers.js";

const fixture = (name) => JSON.parse(readFileSync(fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)), "utf8"));
const snapshot = fixture("snapshot.json");
const clone = (value) => JSON.parse(JSON.stringify(value));
const clock = { mono: 100000, wall: Date.parse("2026-10-07T18:00:00Z") };
performance.now = () => clock.mono;
Date.now = () => clock.wall;

const missing = (detail) => ({
  state: can.CAN_AVAILABILITY.state,
  confidence: can.CAN_AVAILABILITY.confidence,
  running: null,
  basis: can.CAN_AVAILABILITY.missing_basis,
  detail,
});

function healthyRunning() {
  return {
    state: "running",
    confidence: "verified",
    running: true,
    basis: "qualified_ccan_0x0fc_engine_speed",
    detail: "fresh engine-speed evidence",
  };
}

function unavailable(basis, detail) {
  return {
    state: can.CAN_AVAILABILITY.state,
    confidence: can.CAN_AVAILABILITY.confidence,
    running: null,
    basis,
    detail,
  };
}

function applyState(vehicle) {
  const body = clone(snapshot);
  const observedAt = new Date(clock.wall - 100).toISOString();
  body.metrics["engine.rpm"] = {
    ...body.metrics["engine.rpm"],
    available: true,
    stale: false,
    age_ms: 100,
    value: 800,
    observed_at: observedAt,
  };
  body.status.vehicle_state = { ...vehicle, observed_at: observedAt, age_ms: 100 };
  store.applyBaseline(body, 0, clock.mono);
}

test("CAN availability vocabulary and labels cover all, one and multiple missing roles", () => {
  assert.equal(can.isUnavailableCanBasis("can_adapter_missing"), true);
  assert.equal(can.isUnavailableCanBasis("can_adapter_future"), false);
  assert.equal(can.isUnavailableCanBasis("passive_bus_silence"), false);
  assert.equal(
    can.canAvailabilityLabel(missing("CAN adapters missing; all required roles are absent")),
    "CAN adapters missing",
  );
  assert.equal(
    can.canAvailabilityLabel(missing("B-CAN adapter missing; one role is absent")),
    "B-CAN adapter missing",
  );
  assert.equal(
    can.canAvailabilityLabel(missing("C-CAN / B-CAN adapters missing; two roles are absent")),
    "C-CAN / B-CAN adapters missing",
  );
  assert.equal(
    system.canAvailabilityLine(unavailable(can.CAN_AVAILABILITY.recovering_basis, "CAN adapters returning; awaiting fresh passive vehicle evidence")),
    "CAN adapters returning · awaiting fresh passive vehicle evidence",
  );
  assert.equal(
    system.canAvailabilityLine(unavailable(can.CAN_AVAILABILITY.initializing_basis, "Checking CAN adapters; initial interface discovery is still in progress")),
    "Checking CAN adapters · initial interface discovery is still in progress",
  );
});

test("missing adapters outrank fresh surviving-role RPM across top bar, System and Parked", () => {
  applyState(healthyRunning());
  store.connection.value = { state: "live", reason: null, detail: null, sinceMono: clock.mono };
  derive.activeView.value = "health";
  saveSettings({ ...settings.peek(), view: "health", auto: false });
  derive.startEngineTracking();

  const autoEffect = effect(() => derive.evaluateAutoView());
  applyState(missing("B-CAN adapter missing; one role is absent"));

  assert.equal(derive.engineRunning.value, false);
  assert.equal(derive.voltageMode.value, "neutral");
  assert.equal(derive.vehicleHead.value, "B-CAN adapter missing");
  assert.equal(derive.vehicleTail.value, "vehicle state unknown");
  assert.equal(derive.alertStrip.value, null, "cached vehicle alerts do not outrank adapter loss");
  assert.equal(system.vehicleLine(store.vehicle.value, { running: true }), "B-CAN adapter missing · vehicle state unknown");
  assert.equal(derive.topBarClass(null, store.vehicle.value), "topbar topbar--amber");
  assert.equal(parked.batterySubline({ kind: "held", observedAt: "2026-10-07T17:00:00Z", unavailable: true }, clock.wall), "vehicle state unknown");
  assert.deepEqual(parked.acquireAvailability(snapshot.web, store.vehicle.value), { enabled: false, note: parked.TEXT.acquireUnavailable });
  assert.equal(settings.value.auto, false);
  assert.equal(derive.activeView.value, "health");

  applyState(unavailable(can.CAN_AVAILABILITY.recovering_basis, "CAN adapters returning; awaiting fresh passive vehicle evidence"));
  assert.equal(derive.vehicleHead.value, "CAN adapters returning");
  assert.equal(derive.vehicleTail.value, "awaiting fresh passive vehicle evidence");
  applyState(unavailable(can.CAN_AVAILABILITY.initializing_basis, "Checking CAN adapters; initial interface discovery is still in progress"));
  assert.equal(derive.vehicleHead.value, "Checking CAN adapters");
  assert.equal(derive.vehicleTail.value, "initial interface discovery is still in progress");

  clock.mono += 60000;
  clock.wall += 60000;
  applyState(healthyRunning());
  assert.equal(derive.engineRunning.value, true);
  assert.equal(derive.voltageMode.value, "neutral", "a data gap breaks continuous running qualification");
  assert.equal(settings.value.auto, false, "returning adapters do not expire a manual view");
  assert.equal(derive.activeView.value, "health");

  saveSettings({ ...settings.peek(), view: "health", auto: true });
  derive.activeView.value = "health";
  applyState(missing("CAN adapters missing; all required roles are absent"));
  derive.evaluateAutoView();
  assert.equal(derive.activeView.value, "health", "automatic mode does not flip on adapter loss");
  assert.equal(derive.topBarClass({ tier: "critical" }, store.vehicle.value), "topbar topbar--red");
  autoEffect();
});
