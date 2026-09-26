import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { effect } from "@preact/signals-core";

import {
  catalog,
  catalogByName,
  metricSignal,
  metricNames,
  vehicle,
  statusLite,
  web,
  clock,
  summary,
  applyBaseline,
  applyStream,
  applySummary,
  invalidate,
  tick,
  ageMs,
  observationState,
  RETAIN_LAST_READING,
  RETAIN_CANDIDATE_ESTIMATES,
  DRIVER_QUALITIES,
} from "../src/store.js";

const fixture = (name) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)), "utf8"));

const SNAPSHOT = fixture("snapshot.json");
const SUMMARY = fixture("summary-v2.json");
const NOW_WALL = Date.parse("2026-09-22T19:00:00Z");

const clone = (value) => JSON.parse(JSON.stringify(value));

/** Count publishes of a signal (the first run of the effect is subtracted). */
function counter(sig) {
  const box = { runs: -1 };
  const dispose = effect(() => {
    sig.value;
    box.runs += 1;
  });
  box.dispose = dispose;
  return box;
}

/**
 * A snapshot with a few fresh, live metrics and a verified running vehicle.
 * Ages before delivery: rpm 300, speed 2000, coolant 100, battery 1000.
 */
function freshSnapshot() {
  const snap = clone(SNAPSHOT);
  const m = snap.metrics;
  Object.assign(m["engine.rpm"], { age_ms: 300, stale: false, value: 2100, observed_at: "2026-09-22T18:59:59.700000+00:00" });
  Object.assign(m["vehicle.speed"], { age_ms: 2000, stale: false, value: 45, observed_at: "2026-09-22T18:59:58.000000+00:00" });
  Object.assign(m["engine.coolant_temperature"], { age_ms: 100, stale: false, value: 190, observed_at: "2026-09-22T18:59:59.900000+00:00" });
  Object.assign(m["battery.voltage"], { age_ms: 1000, stale: false, value: 14.1, observed_at: "2026-09-22T18:59:59.000000+00:00" });
  delete m["battery.voltage"].last_acquisition_error;
  snap.status.vehicle_state = {
    state: "running",
    running: true,
    confidence: "verified",
    basis: "qualified_ccan_0x0fc_engine_speed",
    detail: null,
    observed_at: "2026-09-22T18:59:59.700000+00:00",
    age_ms: 400,
  };
  return snap;
}

function liteEvent(snap, overrides) {
  const status = snap.status;
  return Object.assign(
    {
      status: {
        vehicle_state: status.vehicle_state,
        collector: status.collector,
        active_drive: status.active_drive,
        web: snap.web,
      },
      metrics: snap.metrics,
      catalog_hash: "0123456789abcdef",
      web: snap.web,
      web_delivery: snap.web_delivery,
      status_code: 200,
    },
    overrides || {},
  );
}

test("exports the retention and quality sets", () => {
  assert.ok(DRIVER_QUALITIES.has("verified") && DRIVER_QUALITIES.has("observed_alfa_scale"));
  assert.ok(!DRIVER_QUALITIES.has("candidate"));
  assert.ok(RETAIN_LAST_READING.has("battery.voltage") && RETAIN_LAST_READING.has("tire.pressure.rr"));
  assert.ok(!RETAIN_LAST_READING.has("vehicle.speed") && !RETAIN_LAST_READING.has("engine.rpm"));
  assert.deepEqual([...RETAIN_CANDIDATE_ESTIMATES].sort(), [
    "radar.alignment.azimuth",
    "radar.alignment.elevation",
    "vehicle.odometer",
  ]);
});

test("applyBaseline loads the real snapshot: 25 metrics, 14 available, all stale, catalog + status + web", () => {
  applyBaseline(SNAPSHOT, 1200, 50000);

  assert.equal(catalog.value.length, 25);
  assert.equal(catalogByName.value.size, 25);
  assert.equal(catalogByName.value.get("engine.rpm").stale_after_seconds, 5);
  assert.equal(metricNames.value.length, 25);
  assert.deepEqual(metricNames.value, [...metricNames.value].sort());

  let available = 0;
  let stale = 0;
  for (const name of metricNames.value) {
    const rec = metricSignal(name).value;
    assert.equal(rec.name, name);
    assert.equal(rec.receivedMono, 50000);
    if (rec.available) available += 1;
    if (rec.stale) stale += 1;
  }
  assert.equal(available, 14);
  assert.equal(stale, 25, "every observation in the parked fixture is stale");

  const battery = metricSignal("battery.voltage").value;
  assert.equal(battery.value, 12.6);
  assert.equal(battery.ageAtReceiptMs, 3485192 + 1200);
  assert.equal(battery.staleAfterMs, 30000);
  assert.equal(battery.last_acquisition_error.reason, "bus_asleep");
  assert.equal(battery.last_recorded, null);

  const odo = metricSignal("vehicle.odometer").value;
  assert.equal(odo.available, false);
  assert.equal(odo.value, null);
  assert.equal(odo.ageAtReceiptMs, null);
  assert.equal(odo.reason, "wrong_bus");
  assert.equal(odo.last_recorded.value, 53892.766);

  const raw = metricSignal("diagnostics.cluster.did.0107.raw").value;
  assert.equal(raw.available, false);
  assert.equal(raw.bus, null);
  assert.equal(raw.reason, "stale");

  assert.equal(statusLite.value, SNAPSHOT.status);
  assert.deepEqual(web.value, SNAPSHOT.web);

  const v = vehicle.value;
  assert.equal(v.state, "asleep");
  assert.equal(v.confidence, "inferred");
  assert.equal(v.running, false);
  assert.equal(v.ageAtReceiptMs, 619 + 1200);
  assert.equal(v.receivedMono, 50000);
});

test("a repeated identical delivery publishes no metric, catalog or web change; timing is refreshed in place", () => {
  applyBaseline(SNAPSHOT, 1200, 50000);
  const counters = metricNames.value.map((name) => counter(metricSignal(name)));
  const catalogRuns = counter(catalog);
  const webRuns = counter(web);
  const namesRuns = counter(metricNames);
  const vehicleRuns = counter(vehicle);

  applyBaseline(clone(SNAPSHOT), 900, 51000);

  assert.equal(counters.reduce((n, c) => n + c.runs, 0), 0);
  assert.equal(catalogRuns.runs, 0);
  assert.equal(webRuns.runs, 0);
  assert.equal(namesRuns.runs, 0);
  assert.equal(vehicleRuns.runs, 0);
  const battery = metricSignal("battery.voltage").value;
  assert.equal(battery.receivedMono, 51000);
  assert.equal(battery.ageAtReceiptMs, 3485192 + 900);
  assert.equal(vehicle.value.receivedMono, 51000);
  assert.equal(vehicle.value.ageAtReceiptMs, 619 + 900);

  counters.forEach((c) => c.dispose());
  [catalogRuns, webRuns, namesRuns, vehicleRuns].forEach((c) => c.dispose());
});

test("rule 7: delivery age is added and the stale flag recomputed; verified vehicle state expires", () => {
  const snap = freshSnapshot();
  applyBaseline(snap, 500, 100000);

  const rpm = metricSignal("engine.rpm").value;
  assert.equal(rpm.ageAtReceiptMs, 800);
  assert.equal(rpm.stale, false);
  assert.equal(metricSignal("vehicle.speed").value.stale, false);
  assert.equal(metricSignal("battery.voltage").value.stale, false);
  assert.equal(vehicle.value.state, "running");
  assert.equal(vehicle.value.confidence, "verified");
  assert.equal(vehicle.value.ageAtReceiptMs, 900);

  // speed: 2000 + 3500 delivery = 5500 > 5000 → stale on acceptance.
  applyBaseline(freshSnapshot(), 3500, 110000);
  assert.equal(metricSignal("vehicle.speed").value.stale, true);
  assert.equal(metricSignal("engine.rpm").value.stale, false);
  // vehicle 400 + 3500 = 3900 stays verified.
  assert.equal(vehicle.value.confidence, "verified");

  // 400 + 4700 > 5000 → unknown / client_freshness_expired.
  applyBaseline(freshSnapshot(), 4700, 120000);
  assert.equal(vehicle.value.state, "unknown");
  assert.equal(vehicle.value.running, null);
  assert.equal(vehicle.value.confidence, "stale");
  assert.equal(vehicle.value.basis, "client_freshness_expired");

  const invalid = freshSnapshot();
  invalid.status.vehicle_state.age_ms = null;
  applyBaseline(invalid, 100, 130000);
  assert.equal(vehicle.value.basis, "client_freshness_invalid");

  // Inferred asleep is never expired by the client (rule 7 applies to verified only).
  const asleep = freshSnapshot();
  asleep.status.vehicle_state = SNAPSHOT.status.vehicle_state;
  applyBaseline(asleep, 9000, 140000);
  assert.equal(vehicle.value.state, "asleep");
  assert.equal(vehicle.value.confidence, "inferred");
  assert.equal(vehicle.value.ageAtReceiptMs, 619 + 9000);

  // An available metric with an invalid age or a missing catalog entry is stale.
  const odd = freshSnapshot();
  odd.metrics["engine.rpm"].age_ms = null;
  odd.metrics["ghost.metric"] = { metric: "ghost.metric", available: true, value: 1, unit: "x", age_ms: 10, stale: false, quality: "verified" };
  applyBaseline(odd, 100, 150000);
  assert.equal(metricSignal("engine.rpm").value.stale, true);
  assert.equal(metricSignal("engine.rpm").value.ageAtReceiptMs, null);
  assert.equal(metricSignal("ghost.metric").value.stale, true);
  assert.equal(metricSignal("ghost.metric").value.staleAfterMs, null);
  assert.ok(metricNames.value.includes("ghost.metric"));

  // A metric that disappears from the next delivery is marked stale.
  applyBaseline(freshSnapshot(), 100, 160000);
  assert.equal(metricSignal("ghost.metric").value.stale, true);
  assert.ok(!metricNames.value.includes("ghost.metric"));
});

test("tick publishes only the observations whose stale flag flips, and rounds the clock", () => {
  applyBaseline(freshSnapshot(), 500, 200000);
  // ages at 200000: rpm 800, speed 2500, coolant 600, battery 1500, vehicle 900
  const rpmRuns = counter(metricSignal("engine.rpm"));
  const speedRuns = counter(metricSignal("vehicle.speed"));
  const coolantRuns = counter(metricSignal("engine.coolant_temperature"));
  const batteryRuns = counter(metricSignal("battery.voltage"));
  const tireRuns = counter(metricSignal("tire.pressure.rr")); // already stale
  const vehicleRuns = counter(vehicle);
  const clockRuns = counter(clock);

  tick(201000);
  assert.equal(clock.value, 201000);
  assert.equal(clockRuns.runs, 1);
  assert.equal(rpmRuns.runs + speedRuns.runs + coolantRuns.runs + batteryRuns.runs + tireRuns.runs, 0);
  assert.equal(vehicleRuns.runs, 0);

  tick(201400); // rounds to the same second → no clock publish
  assert.equal(clockRuns.runs, 1);

  tick(203000); // speed 5500 > 5000 flips; rpm 3800, coolant 3600, battery 4500 do not
  assert.equal(speedRuns.runs, 1);
  assert.equal(metricSignal("vehicle.speed").value.stale, true);
  assert.equal(rpmRuns.runs, 0);
  assert.equal(coolantRuns.runs, 0);
  assert.equal(batteryRuns.runs, 0);
  assert.equal(tireRuns.runs, 0);
  assert.equal(vehicleRuns.runs, 0);
  assert.equal(vehicle.value.state, "running");

  tick(204200); // vehicle 5100 > 5000 → unknown; rpm 5000 is not > 5000
  assert.equal(vehicleRuns.runs, 1);
  assert.equal(vehicle.value.state, "unknown");
  assert.equal(vehicle.value.basis, "client_freshness_expired");
  assert.equal(rpmRuns.runs, 0);

  tick(205000); // rpm 5800 flips, coolant 5600 flips, battery 6500 (limit 30000) does not
  assert.equal(rpmRuns.runs, 1);
  assert.equal(coolantRuns.runs, 1);
  assert.equal(batteryRuns.runs, 0);
  assert.equal(speedRuns.runs, 1, "an already-stale record is not republished");
  assert.equal(vehicleRuns.runs, 1);

  tick(206000);
  assert.equal(rpmRuns.runs + speedRuns.runs + coolantRuns.runs + batteryRuns.runs + tireRuns.runs, 3);

  [rpmRuns, speedRuns, coolantRuns, batteryRuns, tireRuns, vehicleRuns, clockRuns].forEach((c) => c.dispose());
});

test("accepted deliveries advance the coarse clock (link.js skips tick while events arrive)", () => {
  applyBaseline(freshSnapshot(), 500, 250000);
  assert.equal(clock.value, 250000);
  const clockRuns = counter(clock);
  applyStream(liteEvent(freshSnapshot()), 300, 251100);
  assert.equal(clock.value, 251000);
  assert.equal(clockRuns.runs, 1);
  applyStream(liteEvent(freshSnapshot()), 300, 251300); // same second → no publish
  assert.equal(clockRuns.runs, 1);
  applyStream(liteEvent(freshSnapshot()), 300, 252200);
  assert.equal(clock.value, 252000);
  assert.equal(clockRuns.runs, 2);
  clockRuns.dispose();
});

test("ageMs extrapolates monotonically and returns null for an invalid age", () => {
  const rec = { ageAtReceiptMs: 800, receivedMono: 1000 };
  assert.equal(ageMs(rec, 1000), 800);
  assert.equal(ageMs(rec, 3500), 3300);
  assert.equal(ageMs(rec, 900), 800, "a clock that did not advance never lowers the age");
  assert.equal(ageMs({ ageAtReceiptMs: null, receivedMono: 1000 }, 2000), null);
  assert.equal(ageMs(null, 2000), null);
});

test("applyStream updates metrics, status-lite, vehicle and web but never the catalog", () => {
  const snap = freshSnapshot();
  applyBaseline(snap, 500, 300000);
  const catalogRuns = counter(catalog);
  const rpmRuns = counter(metricSignal("engine.rpm"));
  const batteryRuns = counter(metricSignal("battery.voltage"));
  const statusRuns = counter(statusLite);
  const webRuns = counter(web);

  // Same samples one second later: nothing but status-lite publishes.
  const same = freshSnapshot();
  for (const m of Object.values(same.metrics)) if (typeof m.age_ms === "number") m.age_ms += 1000;
  same.status.vehicle_state.age_ms += 1000;
  const event1 = liteEvent(same);
  applyStream(event1, 300, 301000);
  assert.equal(catalogRuns.runs, 0);
  assert.equal(rpmRuns.runs, 0);
  assert.equal(batteryRuns.runs, 0);
  assert.equal(webRuns.runs, 0);
  assert.equal(statusRuns.runs, 1);
  assert.equal(statusLite.value, event1.status);
  assert.equal(metricSignal("engine.rpm").value.ageAtReceiptMs, 1600);
  assert.equal(metricSignal("engine.rpm").value.receivedMono, 301000);
  assert.equal(vehicle.value.ageAtReceiptMs, 1700);
  assert.equal(catalog.value.length, 25, "catalog untouched by a lite event");

  // A new rpm sample publishes only rpm; a web flag change publishes web.
  const changed = freshSnapshot();
  Object.assign(changed.metrics["engine.rpm"], { value: 2350, age_ms: 120, observed_at: "2026-09-22T19:00:01.880000+00:00" });
  changed.web = Object.assign({}, changed.web, { dtc_jobs_enabled: true });
  applyStream(liteEvent(changed), 300, 302000);
  assert.equal(rpmRuns.runs, 1);
  assert.equal(metricSignal("engine.rpm").value.value, 2350);
  assert.equal(batteryRuns.runs, 0);
  assert.equal(webRuns.runs, 1);
  assert.equal(web.value.dtc_jobs_enabled, true);
  assert.equal(catalogRuns.runs, 0);

  // Vehicle state change publishes vehicle.
  const vehicleRuns = counter(vehicle);
  const stopped = freshSnapshot();
  stopped.status.vehicle_state = Object.assign({}, stopped.status.vehicle_state, { state: "ignition_on", running: false, age_ms: 50 });
  applyStream(liteEvent(stopped), 100, 303000);
  assert.equal(vehicleRuns.runs, 1);
  assert.equal(vehicle.value.state, "ignition_on");
  assert.equal(vehicle.value.running, false);

  [catalogRuns, rpmRuns, batteryRuns, statusRuns, webRuns, vehicleRuns].forEach((c) => c.dispose());
});

test("applySummary publishes a slice only when its JSON changed", () => {
  const runs = {
    history: counter(summary.history),
    health: counter(summary.health),
    dtcs: counter(summary.dtcs),
    maintenance: counter(summary.maintenance),
    statusFull: counter(summary.statusFull),
    error: counter(summary.error),
    fetchedMono: counter(summary.fetchedMono),
  };

  applySummary(SUMMARY, 400000);
  assert.equal(runs.history.runs, 1);
  assert.equal(runs.health.runs, 1);
  assert.equal(runs.dtcs.runs, 1);
  assert.equal(runs.maintenance.runs, 1);
  assert.equal(runs.statusFull.runs, 1);
  assert.equal(summary.history.value, SUMMARY.history);
  assert.equal(summary.statusFull.value, SUMMARY.status_full);
  assert.equal(summary.fetchedMono.value, 400000);
  assert.equal(summary.error.value, null);
  assert.equal(runs.error.runs, 0);

  applySummary(clone(SUMMARY), 460000);
  assert.equal(runs.history.runs, 1);
  assert.equal(runs.health.runs, 1);
  assert.equal(runs.dtcs.runs, 1);
  assert.equal(runs.maintenance.runs, 1);
  assert.equal(runs.statusFull.runs, 1);
  assert.equal(runs.error.runs, 0);
  assert.equal(runs.fetchedMono.runs, 2);
  assert.equal(summary.history.value, SUMMARY.history, "identical slice keeps the previous object");

  const changed = clone(SUMMARY);
  changed.maintenance.record_count = (changed.maintenance.record_count || 0) + 1;
  applySummary(changed, 520000);
  assert.equal(runs.maintenance.runs, 2);
  assert.equal(runs.history.runs + runs.health.runs + runs.dtcs.runs + runs.statusFull.runs, 4);

  // An error envelope records the error and leaves every slice alone.
  applySummary({ available: false, reason: "broker_unavailable", detail: "socket refused" }, 580000);
  assert.deepEqual(summary.error.value, { reason: "broker_unavailable", detail: "socket refused" });
  assert.equal(runs.error.runs, 1);
  assert.equal(runs.maintenance.runs, 2);
  assert.equal(summary.fetchedMono.value, 580000);

  // Missing slices are left alone; a good bundle clears the error.
  applySummary({ available: true, maintenance: changed.maintenance }, 640000);
  assert.equal(summary.error.value, null);
  assert.equal(runs.maintenance.runs, 2);
  assert.equal(runs.history.runs, 1);

  Object.values(runs).forEach((c) => c.dispose());
});

test("invalidate marks every live observation stale and the vehicle unknown, publishing only what flips", () => {
  applyBaseline(freshSnapshot(), 500, 700000);
  const live = metricNames.value.filter((name) => {
    const rec = metricSignal(name).value;
    return rec.available && !rec.stale;
  });
  assert.equal(live.length, 4);
  const counters = metricNames.value.map((name) => counter(metricSignal(name)));
  const vehicleRuns = counter(vehicle);

  invalidate("client_page_hidden", 700500);
  assert.equal(counters.reduce((n, c) => n + c.runs, 0), 4);
  for (const name of metricNames.value) assert.equal(metricSignal(name).value.stale, true);
  assert.equal(vehicleRuns.runs, 1);
  assert.deepEqual(
    [vehicle.value.state, vehicle.value.running, vehicle.value.confidence, vehicle.value.basis],
    ["unknown", null, "stale", "client_page_hidden"],
  );
  assert.equal(vehicle.value.receivedMono, 700500);

  invalidate("client_page_hidden", 700600);
  assert.equal(counters.reduce((n, c) => n + c.runs, 0), 4, "a second invalidation publishes nothing");
  assert.equal(vehicleRuns.runs, 1);

  invalidate("client_page_visible", 700700);
  assert.equal(vehicleRuns.runs, 2);
  assert.equal(vehicle.value.basis, "client_page_visible");

  // A fresh baseline after invalidation republishes the same sample because stale flipped back.
  applyBaseline(freshSnapshot(), 500, 701000);
  assert.equal(metricSignal("engine.rpm").value.stale, false);
  assert.equal(vehicle.value.state, "running");

  counters.forEach((c) => c.dispose());
  vehicleRuns.dispose();
});

test("observationState on the parked fixture: held, off and retained samples per rule 15", () => {
  applyBaseline(SNAPSHOT, 1200, 800000);
  const now = 800000;

  // Live-but-stale retained metric: the metric itself is the retained sample.
  const battery = observationState("battery.voltage", now, NOW_WALL);
  assert.equal(battery.kind, "held");
  assert.equal(battery.value, 12.6);
  assert.equal(battery.unit, "V");
  assert.equal(battery.quality, "verified");
  assert.equal(battery.stale, true);
  assert.deepEqual(battery.retained, { value: 12.6, unit: "V", observed_at: "2026-09-22T18:00:17.175324+00:00" });
  assert.equal(battery.definition.name, "battery.voltage");
  assert.equal(battery.ageMs, 3485192 + 1200);

  const coolant = observationState("engine.coolant_temperature", now, NOW_WALL);
  assert.equal(coolant.kind, "held");
  assert.equal(coolant.value, 123.8);
  assert.equal(coolant.quality, "observed_alfa_scale");

  const rr = observationState("tire.pressure.rr", now, NOW_WALL);
  assert.equal(rr.kind, "held");
  assert.equal(rr.value, 74.8);
  assert.equal(rr.retained.observed_at, "2026-09-22T16:25:17.576833+00:00");

  // Failed metric with last_recorded (VVT oil temperature, engine_not_running).
  const vvt = observationState("engine.vvt_oil_temperature", now, NOW_WALL);
  assert.equal(vvt.kind, "held");
  assert.equal(vvt.value, 86);
  assert.equal(vvt.unit, "°F");
  assert.equal(vvt.retained.observed_at, "2026-09-22T16:25:16.589430+00:00");

  // Candidate odometer is held only because it is in RETAIN_CANDIDATE_ESTIMATES.
  const odo = observationState("vehicle.odometer", now, NOW_WALL);
  assert.equal(odo.kind, "held");
  assert.equal(odo.value, 53892.766);
  assert.equal(odo.quality, "candidate");
  const azimuth = observationState("radar.alignment.azimuth", now, NOW_WALL);
  assert.equal(azimuth.kind, "held");
  assert.equal(azimuth.value, 0.164197);

  // Never-observed cluster raw DID → off with the catalog unit.
  const raw = observationState("diagnostics.cluster.did.0107.raw", now, NOW_WALL);
  assert.equal(raw.kind, "off");
  assert.equal(raw.value, null);
  assert.equal(raw.unit, "raw_u8");
  assert.equal(raw.quality, "candidate");
  assert.equal(raw.retained, null);

  // Speed is fresh-only: stale → off even though a value exists.
  const speed = observationState("vehicle.speed", now, NOW_WALL);
  assert.equal(speed.kind, "off");
  assert.equal(speed.value, null);
  assert.equal(speed.stale, true);
  assert.equal(speed.unit, "mph");

  // Power has a last_recorded sample but is not in the retention list → off.
  const power = observationState("engine.crankshaft_power", now, NOW_WALL);
  assert.equal(power.kind, "off");
  assert.equal(power.retained, null);

  // A metric the store has never seen → off.
  const nothing = observationState("no.such.metric", now, NOW_WALL);
  assert.equal(nothing.kind, "off");
  assert.equal(nothing.definition, undefined);
  assert.equal(nothing.unit, null);
});

test("observationState on fresh data: live driver-qualified, candidate held, retained newest wins", () => {
  const snap = freshSnapshot();
  const m = snap.metrics;
  // Failed tire with a valid last_recorded sample.
  m["tire.pressure.fl"] = {
    metric: "tire.pressure.fl", available: false, unit: "psi", reason: "sensor_unavailable", detail: "no reply",
    bus: "c-can", acquisition: "physical_read_data_by_identifier", interface_mode: "listen_only",
    last_recorded: { value: 55.2, unit: "psi", source: "rf_hub.did.31d0", bus: "c-can", quality: "verified", acquisition: "physical_read_data_by_identifier", observed_at: "2026-09-22T17:00:00.000000+00:00" },
  };
  // Live candidate odometer: available and fresh but never driver-qualified.
  m["vehicle.odometer"] = {
    metric: "vehicle.odometer", available: true, unit: "mi", value: 53900.5, source: "ics.did.2001", bus: "b-can",
    acquisition: "physical_read_data_by_identifier", interface_mode: "listen_only", quality: "candidate",
    observed_at: "2026-09-22T18:59:59.000000+00:00", age_ms: 100, stale: false,
    last_recorded: { value: 53892.766, unit: "mi", source: "ics.did.2001", bus: "b-can", quality: "candidate", acquisition: "physical_read_data_by_identifier", observed_at: "2026-09-22T06:09:39.646302+00:00" },
  };
  // Live-but-stale coolant whose last_recorded is newer than the live sample.
  Object.assign(m["engine.coolant_temperature"], { age_ms: 9000, stale: true, value: 150, observed_at: "2026-09-22T18:50:00.000000+00:00" });
  m["engine.coolant_temperature"].last_recorded = { value: 160, unit: "°F", source: "ccan.broadcast.0x2ed", bus: "c-can", quality: "observed_alfa_scale", acquisition: "passive_broadcast", observed_at: "2026-09-22T18:55:00.000000+00:00" };
  // Invalid retained samples: wrong unit, out of range, source quality mismatch, future time.
  m["tire.pressure.fr"] = { metric: "tire.pressure.fr", available: false, unit: "psi", reason: "stale", detail: "x",
    last_recorded: { value: 55, unit: "kPa", source: "rf_hub.did.31d1", bus: "c-can", quality: "verified", acquisition: "physical_read_data_by_identifier", observed_at: "2026-09-22T17:00:00.000000+00:00" } };
  m["tire.pressure.rl"] = { metric: "tire.pressure.rl", available: false, unit: "psi", reason: "stale", detail: "x",
    last_recorded: { value: 400, unit: "psi", source: "rf_hub.did.31d3", bus: "c-can", quality: "verified", acquisition: "physical_read_data_by_identifier", observed_at: "2026-09-22T17:00:00.000000+00:00" } };
  m["tire.pressure.rr"] = { metric: "tire.pressure.rr", available: false, unit: "psi", reason: "stale", detail: "x",
    last_recorded: { value: 70, unit: "psi", source: "rf_hub.did.31d2", bus: "c-can", quality: "candidate", acquisition: "physical_read_data_by_identifier", observed_at: "2026-09-22T17:00:00.000000+00:00" } };
  m["transmission.oil_temperature"] = { metric: "transmission.oil_temperature", available: false, unit: "°F", reason: "stale", detail: "x",
    last_recorded: { value: 120, unit: "°F", source: "ccan.broadcast.0x1f7", bus: "c-can", quality: "observed_alfa_scale", acquisition: "passive_broadcast", observed_at: "2026-09-23T12:00:00.000000+00:00" } };
  applyBaseline(snap, 500, 900000);
  const now = 900000;

  const rpm = observationState("engine.rpm", now, NOW_WALL);
  assert.equal(rpm.kind, "live");
  assert.equal(rpm.value, 2100);
  assert.equal(rpm.unit, "rpm");
  assert.equal(rpm.quality, "observed_alfa_scale");
  assert.equal(rpm.stale, false);
  assert.equal(rpm.ageMs, 800);
  assert.equal(rpm.retained, null);

  const speed = observationState("vehicle.speed", now, NOW_WALL);
  assert.equal(speed.kind, "live");
  assert.equal(speed.value, 45);
  // The same speed evaluated 3 s later is past stale_after (2500 + 3000) even before tick flips it.
  const later = observationState("vehicle.speed", now + 3000, NOW_WALL);
  assert.equal(later.kind, "off");
  assert.equal(later.stale, true);
  assert.equal(later.ageMs, 5500);

  const battery = observationState("battery.voltage", now, NOW_WALL);
  assert.equal(battery.kind, "live");
  assert.equal(battery.value, 14.1);

  const fl = observationState("tire.pressure.fl", now, NOW_WALL);
  assert.equal(fl.kind, "held");
  assert.equal(fl.value, 55.2);
  assert.equal(fl.quality, "verified");
  assert.equal(fl.retained.observed_at, "2026-09-22T17:00:00.000000+00:00");

  const odo = observationState("vehicle.odometer", now, NOW_WALL);
  assert.equal(odo.kind, "held", "candidate quality never renders live");
  assert.equal(odo.value, 53900.5, "the newer live candidate sample wins over last_recorded");
  assert.equal(odo.stale, true);

  const coolant = observationState("engine.coolant_temperature", now, NOW_WALL);
  assert.equal(coolant.kind, "held");
  assert.equal(coolant.value, 160, "newest observed_at wins");

  assert.equal(observationState("tire.pressure.fr", now, NOW_WALL).kind, "off", "unit mismatch");
  assert.equal(observationState("tire.pressure.rl", now, NOW_WALL).kind, "off", "out of range");
  assert.equal(observationState("tire.pressure.rr", now, NOW_WALL).kind, "off", "source quality mismatch");
  assert.equal(observationState("transmission.oil_temperature", now, NOW_WALL).kind, "off", "future observed_at");

  // Fresh but not driver-qualified and not retainable → off.
  const cluster = clone(snap);
  cluster.metrics["diagnostics.cluster.did.1000.raw"] = { metric: "diagnostics.cluster.did.1000.raw", available: true, unit: "raw_u16_be", value: 17, source: "cluster.did.1000", quality: "candidate", bus: "c-can", acquisition: "physical_read_data_by_identifier", interface_mode: "armed_diagnostic", observed_at: "2026-09-22T18:59:59.000000+00:00", age_ms: 50, stale: false };
  applyBaseline(cluster, 100, 901000);
  const rawLive = observationState("diagnostics.cluster.did.1000.raw", 901000, NOW_WALL);
  assert.equal(rawLive.kind, "off");
  assert.equal(rawLive.stale, false);
  assert.equal(rawLive.quality, "candidate");
});
