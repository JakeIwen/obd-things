// Illustrative responses for the screenshot views beyond the two Drive pages: the summary bundle
// (History, Health, System, Parked), the parked metric baseline, service records, radar
// reference and service-alignment state and the DTC-scan state. Everything is synthetic or comes
// from the tracked test fixtures with timestamps moved to the capture time.
import {DAY_MS, HOUR_MS, MINUTE_MS, buildDemoScene, iso} from "./demo_data.mjs";
import {DEMO_OPEN_EVENT_ID, DEMO_OPEN_EVENT_RULE, DEMO_RR_BASELINE, OPENED_MINUTES_AGO} from "./demo_events.mjs";
import radarService from "../../radar_service_alignment.json" with {type: "json"};
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;

/** Minutes after the demo drive ends at which the parked captures are taken. */
export const PARKED_MINUTES_AFTER_DRIVE = 50;


/** Move every ISO timestamp and `*_ms` epoch in a fixture by `shiftMs`, so saved data reads as recent. */
function rebaseTimestamps(value, shiftMs, key = "") {
  if (Array.isArray(value)) return value.map((item) => rebaseTimestamps(item, shiftMs));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, rebaseTimestamps(v, shiftMs, k)]));
  }
  if (typeof value === "string" && ISO_RE.test(value)) return iso(Date.parse(value) + shiftMs);
  if (typeof value === "number" && key.endsWith("_ms") && value > 1e12) return value + shiftMs;
  return value;
}

/** Newest value of the named ISO timestamp fields anywhere in `value`, in epoch ms. */
function latestTimestamp(value, keys) {
  let latest = -Infinity;
  const walk = (node) => {
    if (Array.isArray(node)) node.forEach(walk);
    else if (node && typeof node === "object") {
      for (const [k, v] of Object.entries(node)) {
        if (keys.includes(k) && typeof v === "string" && ISO_RE.test(v)) latest = Math.max(latest, Date.parse(v));
        else walk(v);
      }
    }
  };
  walk(value);
  return latest;
}

/** The saved code list, last read by the van's health check two days before the capture. */
const DTC_READ_DAYS_AGO = 2;

/** Offline cruise record for one trip, in the shape `cruise_harvest.py` saves. */
function cruise({minutes, engaged, ahead, button, brake, compared, delta, coverage}) {
  const seconds = (n) => n * 60;
  return {
    brake_association_verified: false,
    cancels: {ambiguous: 0, brake_associated: brake, button, unknown: 0},
    comparison: {above_seconds: 0, below_seconds: seconds(compared), equal_seconds: 0, mean_delta_mph: delta,
      mean_limit_mph: 65, mean_set_mph: 65 + delta, paired_seconds: seconds(compared), unpaired_seconds: seconds(engaged - compared)},
    coverage_percent: coverage, engaged_seconds: seconds(engaged), faults: {}, following_seconds: seconds(ahead),
    override_seconds: 0, recording_complete: true, unknown_seconds: seconds(minutes) * (1 - coverage / 100),
  };
}

/** Completed trips before the demo drive: [hours before it started, minutes, mpg, cruise]. */
const EARLIER_TRIPS = Object.freeze([
  [5, 52, 16.8, {engaged: 33, ahead: 16, button: 3, brake: 1, compared: 31, delta: -1.1, coverage: 98.0}],
  [21, 38, 17.2, {engaged: 24, ahead: 11, button: 1, brake: 2, compared: 22, delta: -0.8, coverage: 98.6}],
  [46, 240, 15.9, {engaged: 156, ahead: 76, button: 2, brake: 2, compared: 144, delta: -0.6, coverage: 98.3}],
  [74, 26, 14.2, {engaged: 9, ahead: 4, button: 1, brake: 0, compared: 8, delta: -0.4, coverage: 97.9}],
]);

function trip(id, startedMs, minutes, mpg, cruiseSpec) {
  const ended = startedMs + minutes * MINUTE_MS;
  return {
    cruise_summary: cruise({minutes, ...cruiseSpec}), duration_seconds: minutes * 60, end_reason: "activity_timeout",
    ended_at: iso(ended), fuel_economy: {available: true, mpg, samples: minutes * 12}, id,
    last_active_at: iso(ended), snapshot_count: minutes * 12, start_basis: "fresh_engine_rpm",
    started_at: iso(startedMs), state: "complete",
  };
}

/** Trip list newest first; the demo drive is in progress while running and the newest trip once parked. */
function demoTrips(scene, driveStartedMs, parked) {
  const earlier = EARLIER_TRIPS.map(([hoursBefore, minutes, mpg, spec], i) =>
    trip(140 - i, driveStartedMs - hoursBefore * HOUR_MS, minutes, mpg, spec));
  if (!parked) return earlier;
  const drive = trip(141, driveStartedMs, scene.trip.elapsedMinutes, scene.trip.mpg,
    {engaged: 27, ahead: 13, button: 1, brake: 2, compared: 25, delta: -0.8, coverage: 98.6});
  return [drive, ...earlier];
}

const BUCKET_MS = 15 * MINUTE_MS;
const SERIES_POINTS = 96;
/**
 * Usual-drive baselines [median, MAD] near the scene's highway values, so History compares this
 * drive with plausible history. Coolant stays a few degrees above usual (the Drive scene's watch),
 * and RR sits at the 82 psi it has drifted below (event 614).
 */
const TREND_BASELINES = Object.freeze({
  "engine.coolant_temperature": [195, 2], "transmission.oil_temperature": [177, 3], "engine.oil_pressure": [41, 0.8],
  "engine.vvt_oil_temperature": [200, 3], "engine.crankshaft_power": [58, 6], "generator.field_duty": [40, 4],
  "tire.pressure.fl": [64.8, 0.6], "tire.pressure.fr": [65.7, 0.6], "tire.pressure.rl": [79.6, 0.6], "tire.pressure.rr": [82, 0.6],
});

/**
 * Re-centre a saved trend's usual drive on its demo baseline. Tire 7- and 30-day statistics move
 * with it, since the whole range follows the inflation; other metrics keep theirs, which span
 * idle to full load and must stay internally ordered (low ≤ average ≤ high).
 */
function rebaseTrend(name, trend) {
  const prior = trend.prior_trips && trend.prior_trips.median_of_trip_means;
  if (!TREND_BASELINES[name] || typeof prior !== "number") return trend;
  const [median, mad] = TREND_BASELINES[name];
  const delta = median - prior;
  const shift = (stats) => (stats ? Object.fromEntries(Object.entries(stats).map(([k, v]) =>
    [k, typeof v === "number" && /mean|median|minimum|maximum/.test(k) ? v + delta : v])) : stats);
  const windows = name.startsWith("tire.") ? {days_7: shift(trend.days_7), days_30: shift(trend.days_30)} : {};
  return {...trend, ...windows, prior_trips: {...shift(trend.prior_trips), mad_of_trip_means: mad}};
}

/**
 * Last-24-hour series in 15-minute buckets. The broker records while the engine runs, so a reading
 * appears only inside trips, at the scene's highway values; the battery is also read at rest,
 * settling from its engine-off voltage toward 12.6 V.
 */
function demoSeries(name, scene, trips) {
  const end = Math.floor(scene.nowMs / BUCKET_MS) * BUCKET_MS;
  const values = scene.sparklines[name] || [scene.metrics[name].value];
  const points = [];
  for (let i = 0; i < SERIES_POINTS; i += 1) {
    const at = end - (SERIES_POINTS - 1 - i) * BUCKET_MS;
    const inTrip = trips.some(([start, stop]) => at + BUCKET_MS > start && at < stop);
    const lastStop = Math.max(...trips.map(([, stop]) => stop).filter((stop) => stop <= at), -Infinity);
    let value = null;
    if (inTrip) value = values[i % values.length];
    else if (name === "battery.voltage") value = 12.6 + 0.2 * Math.exp(-(at - lastStop) / (3 * HOUR_MS));
    if (value === null) continue;
    points.push({at: iso(at), value, minimum: value, maximum: value, sample_count: inTrip ? 180 : 3});
  }
  return points;
}

function demoTrends(fixtureTrends, scene, trips) {
  return Object.fromEntries(Object.entries(fixtureTrends).map(([name, trend]) => {
    const shifted = rebaseTrend(name, trend);
    const sparkline = demoSeries(name, scene, trips);
    return [name, {...shifted, sparkline, series: {...trend.series,
      start_at: iso(scene.nowMs - 24 * HOUR_MS), end_at: iso(scene.nowMs), bucket_seconds: BUCKET_MS / 1000}}];
  }));
}

/** Current-trip statistics for the History trend cards, from the Drive scene's sparklines. */
function withCurrentTrip(trends, scene) {
  return Object.fromEntries(Object.entries(trends).map(([name, trend]) => {
    const values = scene.sparklines[name];
    const spec = scene.metrics[name];
    const series = values || (spec && typeof spec.value === "number" ? [spec.value] : null);
    if (!series) return [name, trend];
    const mean = series.reduce((sum, v) => sum + v, 0) / series.length;
    const current = {minimum: Math.min(...series), mean, maximum: Math.max(...series), sample_count: 504};
    const prior = trend.prior_trips && trend.prior_trips.median_of_trip_means;
    return [name, {...trend, current_trip: current,
      current_minus_prior_median: typeof prior === "number" ? mean - prior : null}];
  }));
}

function demoHistory(fixtureHistory, scene, parked) {
  const nowMs = scene.nowMs;
  const driveStarted = parked
    ? nowMs - (PARKED_MINUTES_AFTER_DRIVE + scene.trip.elapsedMinutes) * MINUTE_MS
    : nowMs - scene.trip.elapsedMinutes * MINUTE_MS;
  const trips = demoTrips(scene, driveStarted, parked);
  const windows = [...trips.map((t) => [Date.parse(t.started_at), Date.parse(t.ended_at)]),
    ...(parked ? [] : [[driveStarted, nowMs]])];
  const saved = demoTrends(fixtureHistory.metric_trends, scene, windows);
  const trends = parked ? saved : withCurrentTrip(saved, scene);
  return {
    ...fixtureHistory,
    // The app adds generated-to-collector lag to every age, so the product is as fresh as the scene.
    generated_at: iso(nowMs),
    coverage: scene.history.coverage,
    current_trip: parked ? null : scene.history.current_trip,
    recent_trips: trips,
    fuel_economy: {...scene.history.fuel_economy,
      detail: "Estimated: passive 0x2ED fuel rate scaled to match the cluster's fuel figure; not yet checked against a fill-up."},
    trip_comparison: parked ? fixtureHistory.trip_comparison : scene.history.trip_comparison,
    metric_trends: trends,
  };
}

/** Running: every check evaluates the scene's reading; the RR tire check is demo_events.mjs's open event. */
function runningAssessment(assessment, scene) {
  const nowIso = iso(scene.nowMs);
  const spec = scene.metrics[assessment.metric];
  const current = spec ? {...assessment.current, observed_at: nowIso, captured_at: nowIso, value: spec.value,
    effective_age_seconds: 1, freshness: "fresh"} : assessment.current;
  if (assessment.rule !== DEMO_OPEN_EVENT_RULE) {
    return {...assessment, current, state: "normal", reason: "within the comparable-history band", evaluated_at: nowIso};
  }
  const b = DEMO_RR_BASELINE;
  const threshold = b.robustSigma * b.madMultiplier;
  const signed = spec.value - b.median;
  return {...assessment, current, state: "warning", evaluated_at: nowIso,
    reason: "sustained deviation below the comparable-history band",
    baseline: {median: b.median, robust_sigma: b.robustSigma, mad: b.robustSigma / 1.4826,
      metric: assessment.metric, regime: "motion=highway", trip_count: b.tripCount, unit: "psi", quality: "verified", source: spec.source},
    baseline_regime: "motion=highway",
    deviation: {effect_in_rule_direction: -signed, mad_multiplier: b.madMultiplier, minimum_effect: b.minimumEffect,
      signed_from_median: signed, threshold},
    persistence: {observed: 3, required: 3, window_seconds: 180, satisfied: true, evaluated: true}};
}

function demoHealth(fixtureHealth, scene, parked, openedMs) {
  const assessments = parked ? fixtureHealth.assessments : fixtureHealth.assessments.map((a) => runningAssessment(a, scene));
  const open = fixtureHealth.episodes.active.find((e) => e.id === DEMO_OPEN_EVENT_ID);
  // Parked, the last evidence is the final reading of the drive, taken when the engine stopped.
  const lastSeen = iso(parked ? scene.nowMs - PARKED_MINUTES_AFTER_DRIVE * MINUTE_MS : scene.nowMs);
  const latest = parked
    ? {...open.latest_assessment, current: {...open.latest_assessment.current, observed_at: lastSeen, captured_at: lastSeen,
      value: scene.metrics["tire.pressure.rr"].value}}
    : assessments.find((a) => a.rule === open.rule);
  const episode = {...open, opened_at: iso(openedMs), duration_seconds: (scene.nowMs - openedMs) / 1000,
    last_evaluated_at: iso(scene.nowMs), last_observed_at: lastSeen, latest_assessment: latest};
  return {...fixtureHealth, generated_at: iso(scene.nowMs), assessments,
    active: parked ? [] : [latest],
    episodes: {...fixtureHealth.episodes, active: [episode], recent: [], recent_total: 0}};
}

/** Listener flags as `web.py` reports them; `bind` is never a real host. */
function demoWeb(build, overrides = {}) {
  // Voltage reads on request are the LAN listener's default; the capture browser blocks the POST.
  return {active_acquisition_enabled: true, bind: "loopback demo", build, dtc_jobs_enabled: false,
    radar_service_enabled: false, warning_chat_enabled: true, ...overrides};
}

function vehicleState(nowMs, parked) {
  return parked
    ? {basis: "passive_bus_silence", confidence: "inferred", detail: "no frames arrived at the approved bitrate; consistent with a sleeping vehicle",
      observed_at: iso(nowMs), running: false, state: "asleep", age_ms: 0}
    : {basis: "fresh_engine_rpm", confidence: "verified", detail: null, observed_at: iso(nowMs), running: true, state: "awake", age_ms: 0};
}

function demoStatus(fixtureStatus, scene, parked, web) {
  const nowMs = scene.nowMs;
  const stoppedMs = nowMs - PARKED_MINUTES_AFTER_DRIVE * MINUTE_MS;
  const helper = parked ? {state: "idle"} : {state: "running"};
  return {
    ...fixtureStatus,
    ...scene.statusLite,
    active_drive: {...fixtureStatus.active_drive, ...helper},
    auxiliary_drive: {...fixtureStatus.auxiliary_drive, ...helper},
    interface: {...fixtureStatus.interface, ...scene.statusLite.interface},
    engine_off_voltage: {enabled: true, armed_at: iso(stoppedMs), last_error: null, settle_seconds: 30, settles_at: null, state: "complete",
      last_sample: {acquisition: "passive", bus: "c-can", engine_stopped_at: iso(stoppedMs), observed_at: iso(stoppedMs + 30_000),
        quality: "verified", role: "engine_off_voltage", sample_type: "engine_off", saved_at: iso(stoppedMs + 36_000),
        service: "van-telemetry", source: "ccan.broadcast.0x41a", unit: "V", value: 12.8, version: 1}},
    vehicle_state: vehicleState(nowMs, parked),
    web,
  };
}

/** Oil-change records and odometer, consistent with the Drive scene's 48,217 mi. */
function demoMaintenance(scene) {
  const nowMs = scene.nowMs;
  const record = (daysAgo, mileage, notes) => ({date: iso(nowMs - daysAgo * DAY_MS).slice(0, 10), mileage_mi: mileage,
    mileage_source: "cluster_or_receipt", notes, recorded_at: iso(nowMs - daysAgo * DAY_MS + 3 * HOUR_MS),
    request_id: "oil_" + String(daysAgo).padStart(32, "0")});
  const odometer = scene.metrics["vehicle.odometer"];
  const reading = {observed_at: iso(nowMs - PARKED_MINUTES_AFTER_DRIVE * MINUTE_MS), quality: "candidate",
    source: odometer.source, unit: "mi", value: odometer.value};
  const changes = [record(68, 45_210, "Full synthetic 5W-20, OEM filter. DIY."), record(187, 39_480, "Dealer service, oil and filter.")];
  return {
    available: true, persistent: true, storage_error: null, record_count: changes.length,
    last_known_odometer: reading, last_oil_change: changes[0], oil_changes: changes,
    odometer: {available: false, metric: "vehicle.odometer", reason: "engine_not_running", unit: "mi", last_recorded: reading},
    oil_life: {available: true, value: scene.metrics["engine.oil_life_remaining"].value, observed_at: reading.observed_at,
      metric: "engine.oil_life_remaining"},
  };
}

/** Saved radar reference: a manual zero taken a week ago, selected, so the card shows change since it. */
function demoRadarReference(scene) {
  const nowMs = scene.nowMs;
  const angles = (name) => scene.metrics["radar.alignment." + name].value;
  const point = {id: "5f0c2a9e7b3d4c18a6e2f1b0d9c8a7e6", created_at: iso(nowMs - 7 * DAY_MS), source: "manual",
    observed_at: iso(nowMs - 7 * DAY_MS - 40_000), angles: {azimuth: 0.05, elevation: -0.1}, request_id: null};
  return {available: true, persistent: true, storage_error: null,
    current: {angles: {azimuth: angles("azimuth"), elevation: angles("elevation")}, observed_at: iso(nowMs)},
    selected: point.id, selected_at: point.created_at, points: [point], pending_calibration: null};
}

/** Service Drive Alignment as an enabled listener reports it before any run: armed helper, no run. */
function demoServiceAlignment() {
  return {available: true, helper: {running: true, ready: true, detail: "the engine-running helper is armed and can run the routine"},
    run: null, limits: {stationary_max_mph: 0.5, stall_seconds: 600, max_run_seconds: 3600}, log_error: null};
}

/** The parked DTC scan with nothing queued or running. */
const DTC_JOB_IDLE = Object.freeze({available: true, state: "idle", job: null});

const get = (pathname, json, status = 200) => ({method: "GET", pathname, prefix: false, status, json});

/**
 * The listener's own read routes for one variant, in the route-table shape demo_events.mjs and
 * demo_assistant.mjs use. Opt-in features answer with the refusal a listener without the flag sends.
 */
function listenerRoutes(scene, summary, web) {
  return [
    get("/v2/summary", summary),
    get("/v1/maintenance", summary.maintenance),
    get("/v1/radar/alignment-reference", demoRadarReference(scene)),
    web.radar_service_enabled
      ? get(radarService.path, demoServiceAlignment())
      : get(radarService.path, {available: false, reason: radarService.listener_disabled_reason,
        detail: "radar service alignment is not enabled on this listener"}, 403),
    web.dtc_jobs_enabled
      ? get("/v1/diagnostics/dtc-jobs/current", DTC_JOB_IDLE)
      : get("/v1/diagnostics/dtc-jobs/current", {available: false, reason: "dtc_jobs_disabled", detail: "this listener is cache-only"}, 403),
  ];
}

/** Parked metric records: last readings from the end of the demo drive, as the broker retains them. */
function parkedMetrics(scene) {
  const nowMs = scene.nowMs;
  const endedAt = iso(nowMs - PARKED_MINUTES_AFTER_DRIVE * MINUTE_MS);
  // Tires cool after the drive; the battery rests below its charging voltage.
  const parkedValues = {"battery.voltage": 12.64, "tire.pressure.fl": 61, "tire.pressure.fr": 62, "tire.pressure.rl": 76, "tire.pressure.rr": 75};
  return Object.fromEntries(Object.entries(scene.metrics).map(([name, spec]) => {
    const value = parkedValues[name] ?? spec.value;
    const base = {metric: name, unit: spec.unit, quality: spec.quality, source: spec.source, bus: null, acquisition: "passive",
      interface_mode: "listen_only"};
    if (name === "battery.voltage") return [name, {...base, value, available: true, stale: false, observed_at: iso(nowMs - 20_000), age_ms: 20_000}];
    return [name, {...base, available: false, reason: "engine_not_running", detail: null,
      last_recorded: {...base, value, observed_at: endedAt}}];
  }));
}

/**
 * Stopped in Park with the engine idling and cruise off, as Service Drive Alignment must start.
 * Power agrees with torque × RPM; the shaft-ratio gear estimate has no value with the output
 * shaft stopped, so it is omitted.
 */
const STATIONARY_IDLE = Object.freeze({
  "vehicle.speed": 0, "engine.rpm": 720, "transmission.turbine_speed": 0, "transmission.output_speed": 0,
  "engine.crankshaft_torque": 22, "engine.target_crankshaft_torque": 24, "engine.crankshaft_power": 3, "engine.fuel_rate": 0.55,
  "acc.state": "off", "acc.lead_vehicle": false,
});
const STATIONARY_OMITTED = new Set(["transmission.gear_estimate"]);

function runningMetrics(scene, stationary = false) {
  const observed = iso(scene.nowMs);
  return Object.fromEntries(Object.entries(scene.metrics)
    .filter(([name]) => !(stationary && STATIONARY_OMITTED.has(name)))
    .map(([name, spec]) => [name, {
      metric: name, value: stationary && name in STATIONARY_IDLE ? STATIONARY_IDLE[name] : spec.value,
      unit: spec.unit, quality: spec.quality, source: spec.source, bus: null,
      acquisition: "passive", interface_mode: "illustrative_demo", available: true, stale: false, observed_at: observed, age_ms: 0,
    }]));
}

/**
 * Every response the capture routes serve, for one capture time.
 * @param {{nowMs?: number, build: string, summaryFixture: object}} options summaryFixture is
 *   test/fixtures/summary-v2.json; build is dist/build.json's id
 */
export function buildDemoViews({nowMs = Date.now(), build, summaryFixture}) {
  const scene = buildDemoScene(nowMs);
  const base = rebaseTimestamps(summaryFixture, nowMs - summaryFixture.generated_at_ms);
  const lastRead = latestTimestamp(summaryFixture.dtcs, ["last_attempt_at", "last_seen_at"]);
  base.dtcs = rebaseTimestamps(summaryFixture.dtcs, nowMs - DTC_READ_DAYS_AGO * DAY_MS - lastRead);
  const variant = ({parked = false, flags = {}, stationary = false, health = null}) => {
    const web = demoWeb(build, flags);
    // Parked captures come after the drive ends, so the same opening is that much further back.
    const openedMs = nowMs - (OPENED_MINUTES_AGO + (parked ? PARKED_MINUTES_AFTER_DRIVE : 0)) * MINUTE_MS;
    const summary = {...base, generated_at_ms: nowMs, web, maintenance: demoMaintenance(scene),
      health: health || demoHealth(base.health, scene, parked, openedMs),
      history: demoHistory(base.history, scene, parked), status_full: demoStatus(base.status_full, scene, parked, web)};
    return {web, parked, summary, metrics: parked ? parkedMetrics(scene) : runningMetrics(scene, stationary),
      routes: listenerRoutes(scene, summary, web)};
  };
  return {
    nowMs, scene,
    variants: {
      // The Drive pages keep the scene's own health slice (one check on watch, so no top-bar alert).
      drive: variant({health: scene.health}),
      running: variant({}),
      parked: variant({parked: true}),
      // 15 and 18 show controls only an opted-in listener offers; nothing is started in either.
      parkedScan: variant({parked: true, flags: {dtc_jobs_enabled: true}}),
      stationaryRadarService: variant({flags: {radar_service_enabled: true}, stationary: true}),
    },
  };
}
