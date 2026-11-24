import { BAROMETRIC_PRESSURE_METRIC } from "../src/pressureAltitude.helpers.js";

export const MINUTE_MS = 60_000;
export const HOUR_MS = 60 * MINUTE_MS;
export const DAY_MS = 24 * HOUR_MS;

/** Metrics needed to fill every tile and indicator on both Drive pages. */
export const DRIVE_DEMO_METRICS = Object.freeze([
  "acc.follow_distance",
  "acc.lead_vehicle",
  "acc.mode",
  "acc.set_speed",
  "acc.state",
  "battery.voltage",
  BAROMETRIC_PRESSURE_METRIC,
  "engine.coolant_temperature",
  "engine.crankshaft_power",
  "engine.crankshaft_torque",
  "engine.fuel_rate",
  "engine.intake_air_temperature",
  "engine.oil_pressure",
  "engine.rpm",
  "engine.target_crankshaft_torque",
  "engine.vvt_oil_temperature",
  "fuel.range",
  "fuel.tank_level",
  "generator.field_duty",
  "hvac.ac_high_side_pressure",
  "lights.high_beam_on",
  "lights.parking_headlights_on",
  "radar.alignment.azimuth",
  "radar.alignment.elevation",
  "tire.pressure.fl",
  "tire.pressure.fr",
  "tire.pressure.rl",
  "tire.pressure.rr",
  "transmission.gear_estimate",
  "transmission.oil_temperature",
  "transmission.output_speed",
  "transmission.turbine_speed",
  "vehicle.compass_direction",
  "vehicle.odometer",
  "vehicle.speed",
  "vehicle.speed_limit",
]);

const metric = (value, unit, quality, source) => Object.freeze({value, unit, quality, source});

/**
 * One illustrative highway scene. Values are synthetic and contain no location,
 * VIN, or recorded vehicle observation.
 */
export const DEMO_SCENE = Object.freeze({
  name: "illustrative-highway",
  staleAfterMs: HOUR_MS,
  metrics: Object.freeze({
    "acc.follow_distance": metric(3, "bars", "verified", "ccan.broadcast.0x5a0"),
    "acc.lead_vehicle": metric(true, "boolean", "verified", "ccan.broadcast.0x5a0"),
    "acc.mode": metric("adaptive", "mode", "verified", "ccan.broadcast.0x5a0"),
    "acc.set_speed": metric(65, "mph", "verified", "ccan.broadcast.0x5a0"),
    "acc.state": metric("engaged", "state", "verified", "ccan.broadcast.0x5a0"),
    "battery.voltage": metric(14.1, "V", "verified", "bcan.broadcast.0x46c"),
    [BAROMETRIC_PRESSURE_METRIC]: metric(81.107838, "kPa", "observed_alfa_scale", "pcm.did.0109"),
    "engine.coolant_temperature": metric(203, "°F", "observed_alfa_scale", "ccan.broadcast.0x2ed"),
    "engine.crankshaft_power": metric(64, "hp", "observed_alfa_scale", "derived.pcm_06da_x_ccan_0x0fc"),
    "engine.crankshaft_torque": metric(148, "lb-ft", "observed_alfa_scale", "pcm.did.06da"),
    "engine.fuel_rate": metric(3.3, "gal/h", "candidate", "ccan.broadcast.0x2ed"),
    "engine.intake_air_temperature": metric(97, "°F", "candidate", "ccan.broadcast.0x417"),
    "engine.oil_life_remaining": metric(76, "%", "observed_alfa_scale", "pcm.did.2185"),
    "engine.oil_pressure": metric(43, "psi", "observed_alfa_scale", "ccan.broadcast.0x41d"),
    "engine.rpm": metric(2280, "rpm", "observed_alfa_scale", "ccan.broadcast.0x0fc"),
    "engine.target_crankshaft_torque": metric(156, "lb-ft", "observed_alfa_scale", "ccan.broadcast.0x100"),
    "engine.vvt_oil_temperature": metric(205, "°F", "observed_alfa_scale", "pcm.did.069f"),
    "fuel.range": metric(160, "mi", "candidate", "bcan.broadcast.0x760"),
    "fuel.tank_level": metric(62, "%", "candidate", "bcan.broadcast.0x356"),
    "hvac.ac_high_side_pressure": metric(215, "psi", "candidate", "ccan.broadcast.0x41b"),
    "generator.field_duty": metric(39, "%", "observed_alfa_scale", "pcm.did.01a1"),
    "lights.high_beam_on": metric(false, "boolean", "verified", "ccan.broadcast.0x73e"),
    "lights.parking_headlights_on": metric(true, "boolean", "verified", "ccan.broadcast.0x73e"),
    "radar.alignment.azimuth": metric(0.12, "deg", "candidate", "radar_acc.did.0845"),
    "radar.alignment.elevation": metric(-0.18, "deg", "candidate", "radar_acc.did.0845"),
    "tire.pressure.fl": metric(65, "psi", "verified", "rf_hub.did.31d0"),
    "tire.pressure.fr": metric(66, "psi", "verified", "rf_hub.did.31d1"),
    "tire.pressure.rl": metric(80, "psi", "verified", "rf_hub.did.31d3"),
    "tire.pressure.rr": metric(79, "psi", "verified", "rf_hub.did.31d2"),
    // Turbine / output = 0.70, the 948TE's seventh-gear ratio shown under the gear estimate.
    "transmission.gear_estimate": metric("7", "gear", "candidate", "derived.ccan_0x1f7_shaft_ratio"),
    "transmission.oil_temperature": metric(182, "°F", "observed_alfa_scale", "ccan.broadcast.0x1f7"),
    "transmission.output_speed": metric(3240, "rpm", "observed_alfa_scale", "ccan.broadcast.0x1f7"),
    "transmission.turbine_speed": metric(2265, "rpm", "observed_alfa_scale", "ccan.broadcast.0x1f7"),
    "vehicle.compass_direction": metric(1, "direction", "verified", "bcan.broadcast.0x088"),
    "vehicle.ignition_on": metric(true, "boolean", "verified", "ccan.broadcast.0x2ef"),
    // The generic tile only renders driver-qualified current values; keep the illustrative tile live.
    "vehicle.odometer": metric(48217.4, "mi", "observed_alfa_scale", "ics.did.2001"),
    "vehicle.speed": metric(63, "mph", "observed_alfa_scale", "ccan.broadcast.0x101"),
    "vehicle.speed_limit": metric(65, "mph", "verified", "ccan.broadcast.0x0e0"),
  }),
  sparklines: Object.freeze({
    "engine.coolant_temperature": Object.freeze([198, 199, 200, 201, 201, 202, 202, 203]),
    "engine.crankshaft_power": Object.freeze([58, 61, 60, 63, 66, 62, 65, 64]),
    "engine.oil_pressure": Object.freeze([41, 42, 42, 43, 42, 43, 43, 43]),
    "engine.rpm": Object.freeze([2200, 2250, 2230, 2300, 2340, 2260, 2310, 2280]),
    "engine.vvt_oil_temperature": Object.freeze([201, 202, 202, 203, 203, 204, 204, 205]),
    // A short heavy electrical load pushes the field past the 80 % red line, then it settles at 39 %.
    "generator.field_duty": Object.freeze([36, 38, 41, 86, 92, 84, 52, 39]),
    "battery.voltage": Object.freeze([14.0, 14.1, 14.1, 14.0, 14.1, 14.1, 14.0, 14.1]),
    "transmission.oil_temperature": Object.freeze([176, 177, 178, 179, 180, 180, 181, 182]),
    "vehicle.speed": Object.freeze([61, 62, 62, 63, 64, 63, 62, 63]),
  }),
  trip: Object.freeze({elapsedMinutes: 42, distanceMiles: 39.8, mpg: 16.4}),
  historyFuel: Object.freeze({days7Mpg: 16.1, days30Mpg: 15.7}),
});

export const iso = (ms) => new Date(ms).toISOString();

function historyAt(nowMs, scene) {
  const started = nowMs - scene.trip.elapsedMinutes * MINUTE_MS;
  return {
    available: true,
    coverage: {status: "current", age_seconds: 0, last_snapshot_at: iso(nowMs), active_interface_gaps: []},
    current_trip: {
      id: "demo-current",
      started_at: iso(started),
      duration_seconds: scene.trip.elapsedMinutes * 60,
      snapshot_count: scene.trip.elapsedMinutes * 12,
      fuel_economy: {available: true, mpg: scene.trip.mpg},
    },
    recent_trips: [
      {id: "demo-previous-1", started_at: iso(nowMs - 25 * HOUR_MS), ended_at: iso(nowMs - 24.3 * HOUR_MS), duration_seconds: 2520, snapshot_count: 504, fuel_economy: {available: true, mpg: 17.2}},
      {id: "demo-previous-2", started_at: iso(nowMs - 49 * HOUR_MS), ended_at: iso(nowMs - 48.4 * HOUR_MS), duration_seconds: 2160, snapshot_count: 432, fuel_economy: {available: true, mpg: 15.8}},
    ],
    fuel_economy: {
      available: true,
      days_7: {available: true, mpg: scene.historyFuel.days7Mpg},
      days_30: {available: true, mpg: scene.historyFuel.days30Mpg},
    },
    trip_comparison: {metrics: {
      "engine.coolant_temperature": {current_trip: {maximum: 209}},
      "engine.crankshaft_power": {current_trip: {mean: 48}},
    }},
    metric_trends: Object.fromEntries(["engine.coolant_temperature", "transmission.oil_temperature", "battery.voltage", "engine.oil_pressure"]
      .map((name) => [name, trendAt(nowMs, scene, name)])),
  };
}

function trendAt(nowMs, scene, name) {
  const values = scene.sparklines[name];
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  const stats = {minimum: Math.min(...values), mean, maximum: Math.max(...values)};
  return {
    unit: scene.metrics[name].unit, current_trip: stats, days_7: stats, days_30: stats,
    current_minus_prior_median: 0,
    prior_trips: {median_of_trip_means: mean, mad_of_trip_means: 0.5, trip_count: 12},
    series: {start_at: iso(nowMs - 24 * HOUR_MS), end_at: iso(nowMs), bucket_seconds: 900},
    sparkline: Array.from({length: 96}, (_, i) => ({at: iso(nowMs - (95 - i) * 15 * MINUTE_MS), value: values[i % values.length]})),
  };
}

function maintenanceAt(nowMs) {
  const record = {id: "demo-oil", date: iso(nowMs - 20 * 24 * HOUR_MS).slice(0, 10),
    mileage_mi: 46750, mileage_source: "ics_estimate", notes: "Illustrative demo record"};
  return {available: true, persistent: true, storage_error: null, record_count: 1,
    last_oil_change: record, oil_changes: [record], oil_life: {available: true, value: 76, observed_at: iso(nowMs)}};
}

function radarStatusAt(nowMs) {
  const axis = (latest, means, changes, startValue, parkedChange) => ({
    latest_value: latest,
    observed_at: iso(nowMs),
    "60": {count: 12, mean: means[0], change: changes[0]},
    "300": {count: 60, mean: means[1], change: changes[1], peak_abs: Math.abs(latest) + 0.03},
    "1800": {count: 180, mean: means[2], change: changes[2]},
    drive: {change: Math.round((latest - startValue) * 100) / 100, start_value: startValue, started_at: iso(nowMs - 42 * MINUTE_MS),
      count: 252, span_seconds: 2520},
    parked: {change: parkedChange, previous_value: Math.round((startValue - parkedChange) * 100) / 100,
      previous_observed_at: iso(nowMs - 15 * HOUR_MS), observed_at: iso(nowMs - 42 * MINUTE_MS)},
  });
  return {
    radar_alignment_polling: {commissioned: true},
    radar_alignment: {
      "radar.alignment.azimuth": axis(0.12, [0.11, 0.10, 0.09], [0.01, 0.02, 0.04], 0.05, 0.03),
      "radar.alignment.elevation": axis(-0.18, [-0.17, -0.16, -0.15], [-0.01, -0.02, -0.03], -0.11, -0.02),
    },
  };
}

/** Materialize relative demo timestamps immediately before browser injection. */
export function buildDemoScene(nowMs = Date.now()) {
  if (!Number.isFinite(nowMs)) throw new TypeError("nowMs must be finite");
  const scene = {
    ...DEMO_SCENE,
    nowMs,
    history: historyAt(nowMs, DEMO_SCENE),
    statusLite: {
      ...radarStatusAt(nowMs), service: "van-telemetry", started_at: iso(nowMs - 2 * HOUR_MS),
      // Floor 0 and no below-empty episode: the Fuel tank tile shows the van's own 62 %, as before.
      fuel_floor: {floor: 0, gal_per_point: 0.182582, episode: null},
      collector: {state: "running", interval_seconds: 1, last_cycle_at: iso(nowMs)},
      history_recorder: {enabled: true, state: "recording"},
      active_drive: {state: "running"}, auxiliary_drive: {state: "running"},
      supplemental_cache: {state: "ready", last_refreshed_at: iso(nowMs)}, last_readings: {persistent: true},
      interface: {mode: "listen_only", active_inhibits: [], role_interfaces: {
        ready: true, roles: Object.fromEntries(["c-can", "b-can", "can-ch"].map((role, i) => [role,
          {safe: true, resolution: "resolved", channel: `demo${i}`, passive_ready: true}])),
      }},
    },
    maintenance: maintenanceAt(nowMs),
    dtcs: {available: true, group_counts: {current: 0, pending: 0}, current: [], pending: [], modules: []},
    health: {available: true, generated_at: iso(nowMs), assessments: [{
      rule: "engine_coolant_temperature_relative_high", metric: "engine.coolant_temperature",
      category: "vehicle_health", severity: "warning", direction: "high", state: "watch",
      current: {value: 203, unit: "°F", observed_at: iso(nowMs)}, baseline: {median: 190, unit: "°F"},
      persistence: {observed: 2, required: 10, window_seconds: 60},
    }], active: [], episodes: {active: []}},
  };
  validateDemoScene(scene);
  return scene;
}

/** Fail fast when a future edit makes the illustrative scene implausible or self-contradictory. */
export function validateDemoScene(scene) {
  const missing = DRIVE_DEMO_METRICS.filter((name) => !scene.metrics[name]);
  if (missing.length) throw new Error("demo scene is missing Drive metrics: " + missing.join(", "));
  const value = (name) => scene.metrics[name].value;
  const instantMpg = value("vehicle.speed") / value("engine.fuel_rate");
  if (!(instantMpg >= 10 && instantMpg <= 30)) throw new Error("demo instant economy must be a plausible 10-30 mpg");
  if (value("acc.set_speed") < value("vehicle.speed")) throw new Error("ACC set speed must not trail speed");
  // At a steady cruise the torque converter is locked, so turbine and engine speeds agree.
  if (Math.abs(value("transmission.turbine_speed") / value("engine.rpm") - 1) > 0.03) {
    throw new Error("demo turbine speed must match engine RPM");
  }
  const power = value("engine.crankshaft_torque") * value("engine.rpm") / 5252.113;
  if (Math.abs(power - value("engine.crankshaft_power")) > 1) {
    throw new Error("demo power must agree with RPM and torque");
  }
  // Intake air runs a little above ambient and well below coolant; with the A/C
  // on, the high side sits in the usual 150-250 psi band at highway speed.
  const intake = value("engine.intake_air_temperature");
  if (!(intake >= 60 && intake <= 130 && intake < value("engine.coolant_temperature") - 50)) {
    throw new Error("demo intake air must be a plausible warm-air reading well below coolant");
  }
  const acPsi = value("hvac.ac_high_side_pressure");
  if (!(acPsi >= 150 && acPsi <= 250)) throw new Error("demo A/C high-side pressure must be in the 150-250 psi running band");
  const volts = value("battery.voltage");
  if (!(volts >= 13.6 && volts <= 14.8)) throw new Error("demo running voltage must be in the charging range");
  for (const [name, knots] of Object.entries(scene.sparklines)) {
    const now = scene.metrics[name]?.value;
    if (typeof now === "number" && Math.abs(knots[knots.length - 1] - now) > Math.max(3, Math.abs(now) * 0.05)) {
      throw new Error("demo sparkline for " + name + " must end at its current value");
    }
  }
  return true;
}
