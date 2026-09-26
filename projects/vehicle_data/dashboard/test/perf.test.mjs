/**
 * Tablet performance and live-state harness (skeptic pass, tablet-perf.md section C).
 *
 * Replays synthetic `/v2/stream` events built from test/fixtures/snapshot.json through the real
 * store and derive modules, with every tile/top-bar computed "mounted" (subscribed by an effect,
 * as a signals-in-JSX binding is) and instrumented:
 *
 * - `evaluations(c)` wraps a computed's callback and counts how often it re-evaluates;
 * - `publishes(s)` counts value changes of a signal/computed (for a primitive bound to a text node
 *   or attribute this is exactly the number of DOM writes the binding performs).
 *
 * Scenarios: (1) 60 stream events with unchanged samples → zero metric/status/vehicle publishes,
 * zero tile computed evaluations, zero DOM-write proxies; (2) one metric changing every 3.5 s →
 * only that tile's computeds (plus the oil-pressure band, which uses coolant) re-evaluate;
 * (3) a stream stall → each live metric flips exactly once into its held/off state per freshness
 * rules 8 and 15, then silence; (4) rule 12 invalidation; (5) a static scan that no view or
 * component reads the 1 Hz clock or starts timers/layout reads outside the documented places.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { effect, computed } from "@preact/signals-core";

// Deterministic clocks: derive.monoNow() reads performance.now() and helpers read Date.now().
const clk = { mono: 200000, wall: Date.parse("2026-09-22T19:00:00Z") };
performance.now = () => clk.mono;
Date.now = () => clk.wall;

const store = await import("../src/store.js");
const derive = await import("../src/app/derive.js");
const { settings } = await import("../src/settings.js");

const fixture = (name) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)), "utf8"));
const clone = (v) => JSON.parse(JSON.stringify(v));
const iso = (ms) => new Date(ms).toISOString();

const SNAPSHOT = fixture("snapshot.json");
const SUMMARY = fixture("summary-v2.json");

// ---------------------------------------------------------------------------
// instrumentation

const instrumented = new WeakMap();
/** Count re-evaluations of a computed by wrapping its callback (its only own function property). */
function evaluations(sig) {
  if (instrumented.has(sig)) return instrumented.get(sig);
  const keys = Object.keys(sig).filter((k) => typeof sig[k] === "function");
  assert.equal(keys.length, 1, "a Computed instance carries exactly one callback");
  const fn = sig[keys[0]];
  const box = { n: 0 };
  sig[keys[0]] = function counted() {
    box.n += 1;
    return fn.call(this);
  };
  instrumented.set(sig, box);
  return box;
}

/** Count value changes of a signal (an active subscription, like a mounted binding). */
function publishes(sig) {
  const box = { n: -1 };
  box.dispose = effect(() => {
    void sig.value;
    box.n += 1;
  });
  return box;
}

/** Snapshot every counter's current value so a phase can assert deltas. */
function mark(table) {
  const out = {};
  for (const [k, box] of Object.entries(table)) out[k] = box.n;
  return out;
}
function delta(table, since) {
  const out = {};
  for (const [k, box] of Object.entries(table)) out[k] = box.n - since[k];
  return out;
}
const nonZero = (d) => Object.fromEntries(Object.entries(d).filter(([, v]) => v !== 0));

// ---------------------------------------------------------------------------
// synthetic broker: a live, running van built on the fixture's catalog

const CATALOG = new Map(SNAPSHOT.catalog.map((d) => [d.name, d]));
const LIVE = {
  "vehicle.speed": 45,
  "engine.rpm": 1800,
  "engine.coolant_temperature": 195,
  "transmission.oil_temperature": 150,
  "engine.oil_pressure": 45,
  "battery.voltage": 14.1,
  "engine.crankshaft_power": 60,
  "engine.crankshaft_torque": 150,
  "engine.vvt_oil_temperature": 200,
  "generator.field_duty": 40,
  "tire.pressure.fl": 55.1,
  "tire.pressure.fr": 55.4,
  "tire.pressure.rl": 75.2,
  "tire.pressure.rr": 75,
  "vehicle.ignition_on": true,
  "transmission.output_speed": 1500,
  "transmission.turbine_speed": 1700,
  "vehicle.odometer": 41234.5, // candidate: never live (rule 8), retained as an estimate (rule 15)
  "diagnostics.cluster.did.0107.raw": 12, // candidate raw DID: never live, never held
};

/** Mirrors web_v2.status_lite (STATUS_LITE_KEYS and the interface projection). */
const STATUS_LITE_KEYS = [
  "service", "started_at", "vehicle_state", "collector", "active_drive", "auxiliary_drive",
  "engine_off_voltage", "radar_alignment", "radar_alignment_polling", "last_readings",
  "supplemental_cache", "current_owner", "interface_probe", "history_recorder", "inflight",
  "last_acquisition_errors", "active_acquisition_permitted",
];
function statusLite(status) {
  const lite = {};
  for (const k of STATUS_LITE_KEYS) if (k in status) lite[k] = clone(status[k]);
  const i = status.interface || {};
  lite.interface = { mode: i.mode, active_inhibits: i.active_inhibits, adapter_present: i.adapter_present, up: i.up };
  for (const k of ["data_quality", "usb_can_monitor"]) {
    const s = status[k] || {};
    lite[k] = { state: s.state, active_count: s.active_count, enabled: s.enabled };
  }
  lite.web = clone(status.web || SNAPSHOT.web);
  return lite;
}

/** Broker state carried between events. */
const broker = {
  metrics: {}, // name -> sample (without age_ms)
  ages: {}, // name -> age at generation for stale metrics
  vehicle: null,
  cycles: 7688,
  lite: statusLite(SNAPSHOT.status),
};

function liveSample(name, value, observedMs) {
  const def = CATALOG.get(name);
  const src = def.sources[0];
  return {
    metric: name,
    available: true,
    stale: false,
    value,
    unit: def.unit,
    quality: src.quality,
    source: src.name,
    bus: src.bus || "c-can",
    acquisition: "passive_broadcast",
    interface_mode: null,
    observed_at: iso(observedMs),
  };
}

function resetBroker() {
  broker.metrics = {};
  broker.ages = {};
  for (const [name, m] of Object.entries(SNAPSHOT.metrics)) {
    if (Object.prototype.hasOwnProperty.call(LIVE, name)) {
      broker.metrics[name] = liveSample(name, LIVE[name], clk.wall - 300);
    } else {
      const copy = clone(m);
      broker.ages[name] = typeof copy.age_ms === "number" ? copy.age_ms : null;
      delete copy.age_ms;
      broker.metrics[name] = copy;
    }
  }
  broker.vehicle = {
    state: "running",
    running: true,
    confidence: "verified",
    basis: "qualified_ccan_0x0fc_engine_speed",
    detail: "qualified passive 0x0FC engine speed is 1800 rpm",
    observed_at: iso(clk.wall - 200),
  };
}

/** One broker delivery at the current fake time (`i` drives the volatile status fields). */
function delivery(i) {
  const metrics = {};
  for (const [name, sample] of Object.entries(broker.metrics)) {
    const m = clone(sample);
    if (sample.available && !sample.stale) m.age_ms = 300 + ((i * 137) % 400);
    else if (broker.ages[name] !== undefined && broker.ages[name] !== null) m.age_ms = broker.ages[name] + i * 1000;
    metrics[name] = m;
  }
  const lite = clone(broker.lite);
  // Volatile fields exactly as the real stream moves them (scratchpad stream-sample.txt):
  broker.cycles += i % 2;
  lite.collector = Object.assign({}, lite.collector, { cycles: broker.cycles, last_cycle_at: iso(clk.wall - 100) });
  lite.interface_probe = Object.assign({}, lite.interface_probe, { elapsed_seconds: 40000 + i });
  lite.history_recorder = Object.assign({}, lite.history_recorder, { last_stored_at: iso(clk.wall), snapshots_stored: 900 + i });
  const polling = i % 2 === 0;
  lite.active_acquisition_permitted = !polling;
  lite.current_owner = polling ? { kind: "broker", operations: [{ metric: "battery.voltage", mode: "passive" }] } : null;
  lite.inflight = polling ? [{ metric: "battery.voltage", mode: "passive" }] : [];
  // The broker restamps vehicle_state.observed_at every collector cycle (~2 s).
  if (i % 2 === 0) broker.vehicle.observed_at = iso(clk.wall - 150);
  lite.vehicle_state = Object.assign({}, broker.vehicle, { age_ms: 150 + ((i * 211) % 700) });
  return { catalog_hash: "fixture", metrics, status: lite, web: clone(SNAPSHOT.web) };
}

// Emulated link.js timing: stream events are applied as they arrive; the watchdog runs every
// 1000 ms on its own phase and ticks only when nothing was accepted in the last second (rule 10).
let lastAccepted = null;
let nextWatchdog = null;
let ticks = 0;
function advance(ms) {
  const end = clk.mono + ms;
  while (nextWatchdog !== null && nextWatchdog <= end) {
    const step = nextWatchdog - clk.mono;
    clk.mono += step;
    clk.wall += step;
    if (lastAccepted === null || clk.mono - lastAccepted >= 1000) {
      store.tick(clk.mono);
      ticks += 1;
    }
    nextWatchdog += 1000;
  }
  const rest = end - clk.mono;
  clk.mono += rest;
  clk.wall += rest;
}
let seq = 0;
function streamEvent() {
  seq += 1;
  const deliveryAge = 100 + ((seq * 53) % 300);
  store.applyStream(delivery(seq), deliveryAge, clk.mono);
  lastAccepted = clk.mono;
}
/** `n` stream events at a ~1 s cadence with ±40 ms jitter; `before(i)` may change the broker. */
function runEvents(n, before) {
  for (let i = 0; i < n; i += 1) {
    advance(1000 + (((seq * 37) % 80) - 40));
    if (before) before(i);
    streamEvent();
  }
}

// ---------------------------------------------------------------------------
// mounted page model: Drive tiles, tires, the Parked battery, the top bar

const DRIVE = [
  ["vehicle.speed", undefined],
  ["engine.rpm", undefined],
  ["engine.coolant_temperature", undefined],
  ["transmission.oil_temperature", undefined],
  ["engine.oil_pressure", undefined],
  ["battery.voltage", 1],
  ["engine.crankshaft_power", undefined],
  ["engine.vvt_oil_temperature", undefined],
  ["tire.pressure.fl", undefined],
  ["tire.pressure.fr", undefined],
  ["tire.pressure.rl", undefined],
  ["tire.pressure.rr", undefined],
  ["battery.voltage", undefined], // Parked battery card (2 decimals)
];
const TILE_FIELDS = ["valueText", "unitText", "heldText", "kind", "colour", "geometry"];

const evals = {}; // "<metric>|<field>" -> evaluation counter
const writes = {}; // "<metric>|<binding>" -> publish counter (DOM-write proxy)
const tiles = {};
const metricPub = {};
const top = {};

function mountPage() {
  for (const [name, decimals] of DRIVE) {
    const key = decimals === undefined ? name : name + "|" + decimals;
    const m = derive.tileModel(name, decimals === undefined ? undefined : { decimals });
    tiles[key] = m;
    // BandBar's attribute bindings (Tile.jsx), mirrored as computeds.
    const g = m.geometry;
    const okX = computed(() => (g.value ? g.value.lo.toFixed(2) : "0"));
    const okW = computed(() => (g.value ? Math.max(0, g.value.hi - g.value.lo).toFixed(2) : "0"));
    const markX = computed(() =>
      g.value && g.value.marker !== null ? Math.max(0, Math.min(98.5, g.value.marker - 0.75)).toFixed(2) : "-10",
    );
    const markCls = computed(() => (g.value ? g.value.state : "none"));
    for (const f of TILE_FIELDS) {
      evals[key + "|" + f] = evaluations(m[f]);
      if (f !== "geometry") writes[key + "|" + f] = publishes(m[f]);
    }
    evals[name + "|obs"] = evaluations(m.obs);
    evals[name + "|band"] = evaluations(m.band);
    writes[key + "|okX"] = publishes(okX);
    writes[key + "|okW"] = publishes(okW);
    writes[key + "|markX"] = publishes(markX);
    writes[key + "|markCls"] = publishes(markCls);
  }
  for (const name of Object.keys(LIVE)) {
    evals[name + "|live"] = evaluations(derive.liveValue(name));
  }
  // Top bar (App.jsx TopBar) text/class bindings.
  const barCls = computed(() => {
    const a = derive.alertStrip.value;
    return "topbar" + (a ? (a.tier === "critical" ? " topbar--red" : " topbar--amber") : "");
  });
  const headText = computed(() => (derive.alertStrip.value ? derive.alertStrip.value.text : derive.vehicleHead.value));
  const tailText = computed(() => {
    const a = derive.alertStrip.value;
    if (a) return a.count > 1 ? " +" + (a.count - 1) : "";
    return derive.vehicleTail.value ? " · " + derive.vehicleTail.value : "";
  });
  writes["top|barCls"] = publishes(barCls);
  writes["top|headText"] = publishes(headText);
  writes["top|tailText"] = publishes(tailText);
  const tabBadge = publishes(derive.healthBadge);
  writes["tab|badge"] = tabBadge;
  for (const k of [
    "engineRunning", "voltageMode", "alertStrip", "vehicleHead", "vehicleTail",
    "metricAlertColour", "warningModel", "healthBadge",
  ]) {
    evals["derive|" + k] = evaluations(derive[k]);
  }
  top.minute = publishes(derive.minuteClock);
  top.clock = publishes(store.clock);
  top.vehicle = publishes(store.vehicle);
  top.statusLite = publishes(store.statusLite);
  top.web = publishes(store.web);
  top.metricNames = publishes(store.metricNames);
  top.catalog = publishes(store.catalog);
  top.activeView = publishes(derive.activeView);
  top.settings = publishes(settings);
  for (const name of Object.keys(SNAPSHOT.metrics)) metricPub[name] = publishes(store.metricSignal(name));
}

// ---------------------------------------------------------------------------
// scenario

let steadyBase = null;

test("setup: HTTP baseline of a running van, summary bundle, mounted Drive/Parked/top bar", () => {
  resetBroker();
  derive.startEngineTracking();
  effect(() => derive.evaluateAutoView()); // App.jsx
  const baseline = delivery(0);
  baseline.catalog = SNAPSHOT.catalog;
  store.applyBaseline(baseline, 120, clk.mono);
  lastAccepted = clk.mono;
  nextWatchdog = clk.mono + 500;
  store.applySummary(SUMMARY, clk.mono);
  mountPage();
  assert.equal(derive.obs("vehicle.speed").value.kind, "live");
  assert.equal(derive.obs("tire.pressure.fl").value.kind, "live");
  assert.equal(derive.activeView.value, "drive");
  // Rule 8: candidate quality is never live; rule 15: the odometer estimate may be held.
  assert.equal(derive.obs("vehicle.odometer").value.kind, "held");
  assert.equal(derive.obs("diagnostics.cluster.did.0107.raw").value.kind, "off");
  // Start-up: the running voltage table only after 10 s of live rpm ≥ 400.
  assert.equal(derive.voltageMode.value, "neutral");
  runEvents(12);
  assert.equal(derive.voltageMode.value, "running");
  assert.equal(derive.band("battery.voltage").value.state, "normal");
  steadyBase = { evals: mark(evals), writes: mark(writes), top: mark(top), metrics: mark(metricPub), ticks };
});

test("(a) 60 stream events with unchanged samples: no publishes, no tile evaluations, no DOM writes", () => {
  const e0 = mark(evals);
  const w0 = mark(writes);
  const t0 = mark(top);
  const m0 = mark(metricPub);
  const tick0 = ticks;
  const minute0 = derive.minuteClock.value;
  runEvents(29);
  advance(600); // one late event (1.6 s gap): the watchdog ticks once in between
  runEvents(31);
  const minutes = derive.minuteClock.value - minute0;

  assert.deepEqual(nonZero(delta(metricPub, m0)), {}, "metric records must not republish");
  const top1 = delta(top, t0);
  assert.equal(top1.vehicle, 0, "vehicle state republished (observed_at restamp)");
  assert.equal(top1.statusLite, 0, "status-lite republished (per-poll ownership flags)");
  assert.equal(top1.web, 0);
  assert.equal(top1.metricNames, 0);
  assert.equal(top1.catalog, 0);
  assert.equal(top1.activeView, 0);
  assert.equal(top1.settings, 0);
  assert.ok(top1.clock >= 59 && top1.clock <= 61, "the coarse clock is the only 1 Hz signal: " + top1.clock);
  assert.equal(top1.minute, minutes);
  assert.ok(ticks - tick0 >= 1, "the late event let the watchdog tick");

  const e1 = nonZero(delta(evals, e0));
  // Minute-resolution text may re-evaluate once per minute boundary, nothing else may.
  const allowed = new Set(["derive|vehicleTail"]);
  for (const [k, n] of Object.entries(e1)) {
    assert.ok(allowed.has(k), "unexpected re-evaluation " + k + " ×" + n);
    assert.ok(n <= minutes, k + " re-evaluated " + n + " times across " + minutes + " minute boundaries");
  }
  assert.deepEqual(nonZero(delta(writes, w0)), {}, "no text/attribute binding may change");
});

test("(a) one metric changing every 3.5 s: only that tile's computeds (and the oil-pressure band) re-evaluate", () => {
  const e0 = mark(evals);
  const w0 = mark(writes);
  const t0 = mark(top);
  const m0 = mark(metricPub);
  const start = clk.mono;
  let step = 0;
  let changes = 0;
  runEvents(60, () => {
    const s = Math.floor((clk.mono - start) / 3500);
    if (s === step) return;
    step = s;
    changes += 1;
    broker.metrics["engine.coolant_temperature"] = liveSample("engine.coolant_temperature", 195 + s, clk.wall - 300);
  });
  assert.ok(changes >= 16 && changes <= 17, "coolant changed " + changes + " times");

  assert.deepEqual(nonZero(delta(metricPub, m0)), { "engine.coolant_temperature": changes });
  const top1 = delta(top, t0);
  assert.equal(top1.vehicle, 0);
  assert.equal(top1.statusLite, 0);

  const c = "engine.coolant_temperature";
  const e1 = nonZero(delta(evals, e0));
  const expected = {
    [c + "|obs"]: changes,
    [c + "|live"]: changes,
    [c + "|band"]: changes,
    [c + "|valueText"]: changes,
    [c + "|unitText"]: changes,
    [c + "|heldText"]: changes,
    [c + "|kind"]: changes,
    [c + "|colour"]: changes,
    [c + "|geometry"]: changes,
    // Oil pressure's band table uses live coolant (cold-engine neutral); it re-evaluates and
    // returns the same frozen result, so nothing downstream of it runs.
    "engine.oil_pressure|band": changes,
    // The top-bar alert watches live coolant for a red band; it stays null (no publish).
    "derive|alertStrip": changes,
  };
  for (const [k, n] of Object.entries(e1)) {
    if (k === "derive|vehicleTail") continue; // minute boundary
    assert.equal(n, expected[k], "unexpected re-evaluation count for " + k);
  }
  for (const k of Object.keys(expected)) assert.ok(k in e1, "expected " + k + " to re-evaluate");

  const w1 = nonZero(delta(writes, w0));
  assert.equal(w1[c + "|valueText"], changes, "each new coolant value is one text write");
  assert.ok(w1[c + "|markX"] >= changes - 1, "the band marker moves with the value");
  for (const k of Object.keys(w1)) assert.ok(k.startsWith(c + "|"), "binding outside the coolant tile changed: " + k);
  assert.equal(w1[c + "|unitText"], undefined);
  assert.equal(w1[c + "|kind"], undefined);
  assert.equal(w1[c + "|colour"], undefined);
});

test("(c) stream stall: each live metric flips once into held/off per rules 8 and 15, then silence", () => {
  const m0 = mark(metricPub);
  const t0 = mark(top);
  const lastSampleWall = {};
  for (const [name, s] of Object.entries(broker.metrics)) lastSampleWall[name] = s.observed_at;

  advance(10000); // no deliveries: the watchdog ticks every second
  const at10 = (name) => derive.obs(name).value;
  // 5 s metrics are no longer live. Fresh-only metrics show a dash; retained ones their sample.
  for (const name of ["vehicle.speed", "engine.rpm", "engine.oil_pressure", "engine.crankshaft_power", "vehicle.ignition_on"]) {
    assert.equal(at10(name).kind, "off", name);
    assert.equal(tiles[name] ? tiles[name].valueText.value : "—", "—", name);
  }
  for (const name of ["engine.coolant_temperature", "transmission.oil_temperature"]) {
    const o = at10(name);
    assert.equal(o.kind, "held", name);
    assert.equal(o.retained.observed_at, lastSampleWall[name], name + " keeps its own sample time");
    assert.notEqual(tiles[name].heldText.value, "", name + " shows its time");
    assert.equal(tiles[name].colour.value, "", name + " held numerals never take a band colour");
  }
  // Longer freshness limits are still live at 10 s.
  assert.equal(at10("engine.vvt_oil_temperature").kind, "live");
  assert.equal(at10("battery.voltage").kind, "live");
  assert.equal(at10("tire.pressure.fl").kind, "live");
  // Verified vehicle state older than 5 s is unknown; awake/unknown never flip the view.
  assert.equal(store.vehicle.value.state, "unknown");
  assert.equal(derive.activeView.value, "drive");

  advance(25000); // 35 s: 15 s and 30 s limits have passed
  assert.equal(derive.obs("engine.vvt_oil_temperature").value.kind, "held");
  assert.equal(derive.obs("battery.voltage").value.kind, "held");
  for (const w of ["fl", "fr", "rl", "rr"]) assert.equal(derive.obs("tire.pressure." + w).value.kind, "held");
  assert.equal(tiles["tire.pressure.fl"].heldText.value !== "", true);

  // Every live record flipped exactly once; stale ones never republished.
  const flips = delta(metricPub, m0);
  for (const [name, n] of Object.entries(flips)) {
    const wasLive = broker.metrics[name].available && !broker.metrics[name].stale;
    assert.equal(n, wasLive ? 1 : 0, name + " published " + n + " times during the stall");
  }
  assert.equal(delta(top, t0).vehicle, 1, "vehicle state published once (verified → unknown)");

  // After the flips the stalled page is quiet: only the clock moves.
  const m1 = mark(metricPub);
  const e1 = mark(evals);
  const w1 = mark(writes);
  advance(10000);
  assert.deepEqual(nonZero(delta(metricPub, m1)), {});
  const e2 = nonZero(delta(evals, e1));
  for (const k of Object.keys(e2)) assert.equal(k, "derive|vehicleTail", "per-second evaluation while stalled: " + k);
  const w2 = nonZero(delta(writes, w1));
  for (const k of Object.keys(w2)) assert.equal(k, "top|tailText", "per-second write while stalled: " + k);
});

test("(c) rule 12: invalidation marks every live record stale once; retained metrics fall back to held", () => {
  resetBroker();
  const baseline = delivery(seq + 1);
  baseline.catalog = SNAPSHOT.catalog;
  store.applyBaseline(baseline, 100, clk.mono);
  lastAccepted = clk.mono;
  assert.equal(derive.obs("vehicle.speed").value.kind, "live");
  const m0 = mark(metricPub);
  store.invalidate("client_page_hidden", clk.mono);
  const flips = delta(metricPub, m0);
  for (const [name, n] of Object.entries(flips)) {
    const wasLive = broker.metrics[name].available && !broker.metrics[name].stale;
    assert.equal(n, wasLive ? 1 : 0, name);
  }
  assert.equal(store.vehicle.value.basis, "client_page_hidden");
  assert.equal(derive.obs("vehicle.speed").value.kind, "off");
  assert.equal(derive.obs("battery.voltage").value.kind, "held");
  assert.equal(derive.obs("tire.pressure.rr").value.kind, "held");
  assert.equal(tiles["vehicle.speed"].valueText.value, "—");
  // A second invalidation with the same reason publishes nothing.
  const m1 = mark(metricPub);
  const v1 = top.vehicle.n;
  store.invalidate("client_page_hidden", clk.mono);
  assert.deepEqual(nonZero(delta(metricPub, m1)), {});
  assert.equal(top.vehicle.n, v1);
});

// ---------------------------------------------------------------------------
// (b) static contract: scrolling views and components never read the 1 Hz clock directly, start
// no timers and read no layout. Together with the runtime result above (the coarse clock is the
// only per-second publish in a steady stream), this means a scrolled view does no per-second work.

test("(b) views/components read the clock only through minuteClock; no timers, layout reads or inline styles", () => {
  const root = new URL("../src/", import.meta.url);
  const files = [];
  for (const dir of ["views", "components"]) {
    for (const f of readdirSync(new URL(dir + "/", root))) {
      if (/\.(jsx|js)$/.test(f)) files.push(dir + "/" + f);
    }
  }
  const TIMER_OK = new Set(["components/Sparkline.jsx", "components/dtcScan.helpers.js", "components/DtcScan.jsx"]);
  const LAYOUT_OK = new Set(["components/Sparkline.jsx"]); // Drive only: measured on mount/resize
  for (const file of files) {
    const src = readFileSync(new URL(file, root), "utf8").replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
    assert.ok(!/\bstore\.clock\b|\bclock\.value\b/.test(src), file + " reads the 1 Hz clock");
    if (!TIMER_OK.has(file)) {
      assert.ok(!/\bset(Interval|Timeout)\s*\(|requestAnimationFrame\s*\(/.test(src), file + " starts a timer");
    }
    if (!LAYOUT_OK.has(file)) {
      assert.ok(
        !/getBoundingClientRect|offset(Width|Height|Top)|client(Width|Height)|getComputedStyle/.test(src),
        file + " reads layout",
      );
    }
    assert.ok(!/\sstyle=\{|\sstyle="/.test(src), file + " uses an inline style (CSP style-src 'self')");
  }
});
