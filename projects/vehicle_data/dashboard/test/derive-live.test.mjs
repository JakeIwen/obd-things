/**
 * Live-state behaviour of derive.js under realistic broker sequences (skeptic pass):
 *
 * (e) voltage band gating (`voltageMode`, design 5 / critique A14) at start, idle and stop:
 *     neutral for key-on, cranking, the first 10 s after start and the first 30 s after the last
 *     rpm or ignition evidence; the running table after 10 s of live rpm ≥ 400; the parked table
 *     only for samples ≥ 30 s after that evidence, independent of the tablet's clock offset.
 * (d) automatic view selection (design 5.2): awake/unknown/stale states never flip the view, and a
 *     manual choice ends on a real engine start/stop, not on a data gap.
 *
 * Every sample time is on the "Pi clock"; `Date.now()` (the tablet clock) is offset by `skew`.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { effect } from "@preact/signals-core";

const clk = { mono: 500000, pi: Date.parse("2026-09-22T19:00:00Z"), skew: 0 };
performance.now = () => clk.mono;
Date.now = () => clk.pi + clk.skew;

const store = await import("../src/store.js");
const derive = await import("../src/app/derive.js");
const { settings, saveSettings } = await import("../src/settings.js");

const fixture = (name) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)), "utf8"));
const clone = (v) => JSON.parse(JSON.stringify(v));
const iso = (ms) => new Date(ms).toISOString();

const SNAPSHOT = fixture("snapshot.json");
const SUMMARY = fixture("summary-v2.json");
const CATALOG = new Map(SNAPSHOT.catalog.map((d) => [d.name, d]));

// ---------------------------------------------------------------------------
// a small broker model

const broker = { live: {}, vehicle: null };

function sample(name, value, atPi) {
  const def = CATALOG.get(name);
  const src = def.sources.find((s) => s.name.startsWith("ccan")) || def.sources[0];
  return { available: true, stale: false, value, unit: def.unit, quality: src.quality, source: src.name, observed_at: iso(atPi) };
}

/** Set (or with `undefined`, stop) a live metric; a stopped metric keeps its last sample and ages. */
function feed(name, value) {
  if (value === undefined) {
    const s = broker.live[name];
    if (s) s.frozenAt = s.frozenAt || clk.pi - 200; // the last delivered sample's time
    return;
  }
  broker.live[name] = { value, frozenAt: null };
}

function vehicleState(state, confidence, basis, detail) {
  broker.vehicle = state === null ? null : { state, confidence, basis, detail, running: state === "running" ? true : null, t: clk.pi };
}

let seq = 0;
function delivery() {
  const metrics = clone(SNAPSHOT.metrics);
  for (const [name, m] of Object.entries(metrics)) {
    if (typeof m.age_ms === "number") m.age_ms += 3600000; // the fixture's asleep records keep ageing
  }
  for (const [name, s] of Object.entries(broker.live)) {
    const at = s.frozenAt || clk.pi - 200;
    const m = sample(name, s.value, at);
    m.age_ms = clk.pi - at;
    m.stale = m.age_ms > CATALOG.get(name).stale_after_seconds * 1000;
    metrics[name] = m;
  }
  const status = { collector: { cycles: seq } };
  if (broker.vehicle) {
    const v = broker.vehicle;
    status.vehicle_state = { state: v.state, confidence: v.confidence, basis: v.basis, detail: v.detail, running: v.running, observed_at: iso(clk.pi - 100), age_ms: 100 };
  }
  return { catalog_hash: "fixture", metrics, status, web: clone(SNAPSHOT.web) };
}

/**
 * Advance `seconds` of 1 Hz deliveries (or, with `stalled`, watchdog ticks only). `each(i)` runs
 * before a delivery (to change the broker), `after(i)` after it (to record what the page shows).
 */
function run(seconds, { stalled = false, each, after } = {}) {
  for (let i = 0; i < seconds; i += 1) {
    clk.mono += 1000;
    clk.pi += 1000;
    if (each) each(i);
    if (stalled) store.tick(clk.mono);
    else {
      seq += 1;
      store.applyStream(delivery(), 150, clk.mono);
    }
    if (after) after(i);
  }
}

/** Battery sample time (Pi clock) of the last delivery: samples are stamped 200 ms before it. */
const sampleAt = () => clk.pi - 200;

const voltBand = derive.band("battery.voltage");
const modeLog = [];
effect(() => modeLog.push(derive.voltageMode.value));
const views = [];
effect(() => views.push(derive.activeView.value));

function resetToAsleep() {
  broker.live = {};
  vehicleState(null);
  const base = clone(SNAPSHOT);
  store.applyBaseline(base, 100, clk.mono);
}

test("setup: page opened on a sleeping van → Parked, parked voltage table on the held 12.6 V", () => {
  derive.startEngineTracking();
  effect(() => derive.evaluateAutoView());
  store.applySummary(SUMMARY, clk.mono);
  resetToAsleep();
  assert.equal(store.vehicle.value.state, "asleep");
  assert.equal(derive.activeView.value, "parked");
  assert.equal(derive.obs("battery.voltage").value.kind, "held");
  assert.equal(derive.voltageMode.value, "parked");
  assert.equal(voltBand.value.state, "normal");
});

test("(d) fob wakes, voltage reads and stale sleep never bounce the view", () => {
  const flips = views.length;
  for (let k = 0; k < 5; k += 1) {
    vehicleState("awake", "observed", "passive_bus_activity", "b-can traffic is present");
    feed("battery.voltage", 12.55);
    run(20);
    vehicleState("asleep", "inferred", "passive_bus_silence", "no frames");
    feed("battery.voltage");
    run(10);
    run(8, { stalled: true }); // stale asleep state: freshness lost, view kept
    vehicleState(null);
    run(3); // no vehicle state at all → unknown
  }
  assert.equal(views.length, flips, "view changed during awake/unknown/stale states: " + views.slice(flips));
  assert.equal(derive.activeView.value, "parked");
});

test("(e) key on, cranking and the first 10 s after start stay neutral; then the running table", () => {
  const mode0 = modeLog.length;
  // Key on, engine off: 0x2EF ignition gate, rpm 0, key-on load 11.85 V.
  vehicleState("ignition_on", "verified", "ccan_0x2ef_ignition_gate", "verified C-CAN ignition-on gate is present");
  feed("vehicle.ignition_on", true);
  feed("engine.rpm", 0);
  feed("battery.voltage", 11.85);
  run(5);
  assert.equal(derive.voltageMode.value, "neutral");
  assert.equal(voltBand.value.state, "neutral", "key-on reading is never coloured");
  assert.equal(derive.activeView.value, "drive", "verified fresh ignition_on (and live rpm) selects Drive");
  // Cranking: rpm below 400, the voltage dips hard.
  vehicleState("ignition_on", "verified", "qualified_ccan_0x0fc_engine_speed", "qualified passive 0x0FC engine speed is 250 rpm");
  feed("engine.rpm", 250);
  feed("battery.voltage", 10.4);
  run(2);
  assert.equal(voltBand.value.state, "neutral", "cranking dip is never coloured");
  // Start: rpm ≥ 400. Neutral for 10 s, then the running table.
  vehicleState("running", "verified", "qualified_ccan_0x0fc_engine_speed", "qualified passive 0x0FC engine speed is 720 rpm");
  feed("engine.rpm", 720);
  feed("battery.voltage", 14.2);
  const seen = [];
  run(12, { after: () => seen.push(derive.voltageMode.value) });
  const firstRunning = seen.indexOf("running");
  assert.ok(firstRunning >= 9 && firstRunning <= 11, "running table after ~10 s: " + seen.join(","));
  assert.equal(voltBand.value.state, "normal");
  assert.ok(!modeLog.slice(mode0).includes("parked"), "the parked table never applied from key-on to start");
});

/** Count re-evaluations of a computed by wrapping its callback (its only own function property). */
function evaluations(sig) {
  const keys = Object.keys(sig).filter((k) => typeof sig[k] === "function");
  assert.equal(keys.length, 1);
  const fn = sig[keys[0]];
  const box = { n: 0 };
  sig[keys[0]] = function counted() {
    box.n += 1;
    return fn.call(this);
  };
  return box;
}

test("(e) idle: running table holds; voltageMode is not re-evaluated per second", () => {
  const mode0 = modeLog.length;
  const evals = evaluations(derive.voltageMode);
  run(60, {
    each: (i) => {
      feed("engine.rpm", 700 + (i % 7)); // idle rpm wobbles every second
      feed("battery.voltage", i % 20 < 10 ? 14.1 : 13.2); // PCM lowers charge voltage now and then
    },
  });
  assert.equal(evals.n, 0, "voltageMode re-evaluated while idling");
  assert.equal(modeLog.length, mode0, "voltageMode changed while idling");
  assert.equal(derive.voltageMode.value, "running");
  // 13.2 V is the running table's neutral zone (12.8–13.6), not amber.
  assert.ok(voltBand.value.state === "normal" || voltBand.value.state === "neutral");
});

for (const skew of [0, 120000, -90000]) {
  test(`(e) stop and key-off: no colour until 30 s after the last rpm sample, then parked (tablet clock ${skew / 1000} s)`, () => {
    clk.skew = skew;
    vehicleState("running", "verified", "qualified_ccan_0x0fc_engine_speed", "qualified passive 0x0FC engine speed is 710 rpm");
    feed("vehicle.ignition_on", true);
    feed("engine.rpm", 710);
    feed("battery.voltage", 14.1);
    run(15);
    assert.equal(derive.voltageMode.value, "running");
    // Key off: rpm and ignition frames stop; the bus stays awake and voltage keeps arriving.
    const stopPi = clk.pi - 200; // the last rpm/ignition sample
    feed("engine.rpm");
    feed("vehicle.ignition_on");
    vehicleState("awake", "observed", "passive_bus_activity", "c-can traffic is present");
    const seen = [];
    run(45, {
      each: () => {
        const since = sampleAt() - stopPi;
        // Surface charge, then a post-shutdown fan load dips to 12.05 V, then it recovers.
        feed("battery.voltage", since < 10000 ? 12.9 : since < 28000 ? 12.05 : 12.45);
      },
      after: () => seen.push([sampleAt() - stopPi, derive.voltageMode.value, voltBand.value.state]),
    });
    for (const [since, mode, band] of seen) {
      if (since < 5000) {
        // Rule 8: the last rpm sample is live for up to 5 s (stale_after), so the running table
        // may still apply; 12.9 V sits in its neutral zone.
        assert.ok(mode === "running" || mode === "neutral", `sample ${since} ms after stop used the ${mode} table`);
      } else if (since < 30000) {
        assert.equal(mode, "neutral", `sample ${since} ms after stop used the ${mode} table`);
      } else {
        assert.equal(mode, "parked", `sample ${since} ms after stop used the ${mode} table`);
      }
      if (since < 30000) assert.equal(band, "neutral", `sample ${since} ms after stop coloured ${band}`);
    }
    assert.equal(voltBand.value.state, "normal");
    clk.skew = 0;
  });
}

test("(e) A14: after a long key-on without starting, key-off readings stay neutral for 30 s", () => {
  // Engine has been off a long time; ignition on for two minutes, then key off.
  broker.live = {};
  vehicleState("ignition_on", "verified", "ccan_0x2ef_ignition_gate", "verified C-CAN ignition-on gate is present");
  feed("vehicle.ignition_on", true);
  feed("engine.rpm", 0);
  feed("battery.voltage", 11.85);
  run(120);
  const offPi = sampleAt();
  feed("vehicle.ignition_on");
  feed("engine.rpm");
  vehicleState("awake", "observed", "passive_bus_activity", "c-can traffic is present");
  const seen = [];
  run(40, {
    each: () => feed("battery.voltage", sampleAt() - offPi < 20000 ? 11.9 : 12.35),
    after: () => seen.push([sampleAt() - offPi, voltBand.value.state]),
  });
  for (const [since, band] of seen) {
    if (since < 30000) assert.equal(band, "neutral", `${since} ms after key-off coloured ${band}`);
  }
  assert.equal(derive.voltageMode.value, "parked");
  assert.equal(voltBand.value.state, "normal");
});

test("(d) a manual view survives data gaps; it ends when the engine really starts or stops", () => {
  // Engine running, automatic mode puts Drive up; the owner taps Health (App.jsx selectView).
  saveSettings({ ...settings.peek(), auto: true });
  vehicleState("running", "verified", "qualified_ccan_0x0fc_engine_speed", "qualified passive 0x0FC engine speed is 1500 rpm");
  feed("vehicle.ignition_on", true);
  feed("engine.rpm", 1500);
  feed("battery.voltage", 14.1);
  run(12);
  assert.equal(derive.activeView.value, "drive");
  derive.activeView.value = "health";
  saveSettings({ ...settings.peek(), view: "health", auto: false });

  // A Wi-Fi stall: no deliveries for 8 s; rpm goes stale, the vehicle state expires to unknown.
  run(8, { stalled: true });
  assert.equal(derive.liveValue("engine.rpm").value, null);
  assert.equal(store.vehicle.value.state, "unknown");
  assert.equal(settings.value.auto, false, "a data gap is not an engine stop");
  // The page is hidden and shown again (rule 12 invalidation), then the stream resumes.
  store.invalidate("client_page_hidden", clk.mono);
  store.invalidate("client_page_visible", clk.mono);
  assert.equal(settings.value.auto, false, "a page hide is not an engine stop");
  run(5);
  assert.equal(derive.liveValue("engine.rpm").value, 1500);
  assert.equal(settings.value.auto, false);
  assert.equal(derive.activeView.value, "health", "the manual view survived the gap");

  // A real stop (key off: rpm gone, the bus reports awake) ends the manual choice.
  feed("engine.rpm");
  feed("vehicle.ignition_on");
  vehicleState("awake", "observed", "passive_bus_activity", "c-can traffic is present");
  run(8);
  assert.equal(settings.value.auto, true, "engine stop re-enables automatic mode");
  // Awake keeps the current view; sleep then selects Parked.
  assert.equal(derive.activeView.value, "health");
  vehicleState("asleep", "inferred", "passive_bus_silence", "no frames");
  feed("battery.voltage");
  run(3);
  assert.equal(derive.activeView.value, "parked");
});

test("dayClock follows the local calendar day at minute resolution, not per second", () => {
  const seen = [];
  const stop = effect(() => seen.push(derive.dayClock.value));
  const evals = evaluations(derive.dayClock);
  // Move the wall clock to 23:57 local time next day and let one minute boundary absorb the jump.
  const d = new Date(clk.pi);
  clk.pi = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1, 23, 57, 0).getTime();
  run(61, { stalled: true });
  const first = seen.length;
  evals.n = 0;
  run(60, { stalled: true }); // 23:58:01 → 23:59:01
  assert.equal(seen.length, first, "no day change before midnight");
  const before = evals.n;
  assert.ok(before <= 2, "dayClock re-evaluates once a minute, not per second: " + before);
  run(120, { stalled: true });
  assert.equal(seen.length, first + 1, "one change at local midnight");
  assert.ok(evals.n - before <= 3);
  stop();
});

// ---------------------------------------------------------------------------
// P1 (warnings skeptic 2): tier-2 notices stay on the Health list only.

test("a slow-leak notice (tier 2, state warning) never reaches the top bar or a numeral colour", () => {
  const health = clone(SUMMARY.health);
  health.assessments = [
    {
      rule: "tire_pressure_rr_slow_leak",
      metric: "tire.pressure.rr",
      category: "vehicle_health",
      state: "warning",
      severity: "notice",
      tier: 2,
      group: "tires",
      title: "RR tire slow leak",
      current: { value: 71.6, unit: "psi", observed_at: iso(clk.pi - 3600000) },
      notification_eligible: true,
    },
  ];
  health.episodes = { active: [], recent: [], notification_outbox: {} };
  store.applySummary({ ...clone(SUMMARY), health }, clk.mono);
  const cards = derive.warningModel.value.warnings;
  assert.ok(cards.some((c) => c.rule === "tire_pressure_rr_slow_leak" && c.tier === "notice"), "the notice is listed on Health");
  assert.equal(derive.alertStrip.value, null, "no top-bar alert for a notice");
  assert.equal(derive.metricAlertColour.value.get("tire.pressure.rr"), undefined, "the RR numeral stays uncoloured");
  store.applySummary(clone(SUMMARY), clk.mono);
});

// ---------------------------------------------------------------------------
// Episode 642 (2026-09-24): the pair rule's metric is only its lower wheel, so the top bar read
// "RR tire 74.8 psi" for "Rear tires uneven". Pair rules show the event title instead.

test("a confirmed pair-asymmetry warning shows the axle title in the top bar, not one wheel", () => {
  const health = clone(SUMMARY.health);
  health.assessments = [
    {
      rule: "tire_pressure_pair_asymmetry",
      metric: "tire.pressure.rr",
      category: "vehicle_health",
      state: "warning",
      severity: "warning",
      tier: 0,
      group: "tires",
      title: "Rear tires uneven",
      action: "Check both rear tires at the next stop.",
      absolute_threshold: { operator: "above", value: 3, unit: "psi" },
      current: { value: 74.8, unit: "psi", observed_at: iso(clk.pi - 5000) },
      notification_eligible: true,
    },
  ];
  health.episodes = { active: [], recent: [], notification_outbox: {} };
  store.applySummary({ ...clone(SUMMARY), health }, clk.mono);
  const card = derive.warningModel.value.warnings.find((c) => c.rule === "tire_pressure_pair_asymmetry");
  assert.equal(card.title, "Rear tires uneven");
  assert.equal(derive.alertStrip.value.text, "Rear tires uneven");
  assert.equal(derive.alertStrip.value.tier, "warning");
  store.applySummary(clone(SUMMARY), clk.mono);
});
