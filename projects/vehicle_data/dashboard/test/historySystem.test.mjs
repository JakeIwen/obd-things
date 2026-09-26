// History and System view helpers (src/views/historySystem.helpers.js).
// Times are formatted in the van's zone; pin it so the suite is machine-independent.
process.env.TZ = "America/Denver";

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as H from "../src/views/historySystem.helpers.js";
import { RETAIN_LAST_READING } from "../src/store.js";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const SUMMARY = JSON.parse(readFileSync(here("./fixtures/summary-v2.json"), "utf8"));
const SNAPSHOT = JSON.parse(readFileSync(here("./fixtures/snapshot.json"), "utf8"));
const NOW = Date.parse("2026-09-22T19:00:00Z"); // 1:00 pm MDT, the fixtures' afternoon
const HISTORY = SUMMARY.history;
const FULL = SUMMARY.status_full;
const JARGON = /\b(stale|registered|alfa|mapped|unmapped|candidate|regime|episode|mad|deviation|null|undefined|NaN)\b|\d+\/\d+ LIVE/i;

const clone = (v) => JSON.parse(JSON.stringify(v));

/** Every string a card displays (ids, keys, tones and raw metric names are never prose). */
function shownText(value) {
  const out = [];
  const walk = (v, key) => {
    if (["id", "key", "tone", "name", "metric", "view", "points"].includes(key)) return;
    if (typeof v === "string") out.push(v);
    else if (Array.isArray(v)) v.forEach((item) => walk(item, null));
    else if (v && typeof v === "object") for (const k of Object.keys(v)) walk(v[k], k);
  };
  walk(value, null);
  return out.join("\n");
}

/** Status-lite as web_v2.status_lite() builds it (roles reduced to their lite keys). */
function liteOf(full) {
  const f = clone(full);
  const ri = f.interface.role_interfaces;
  const roles = {};
  for (const [name, role] of Object.entries(ri.roles)) {
    roles[name] = {};
    for (const k of ["channel", "resolution", "reason", "safe", "passive_ready"]) if (k in role) roles[name][k] = role[k];
  }
  return {
    vehicle_state: f.vehicle_state,
    radar_alignment: f.radar_alignment,
    current_owner: f.current_owner,
    interface: {
      mode: f.interface.mode,
      active_inhibits: f.interface.active_inhibits,
      adapter_present: f.interface.adapter_present,
      up: f.interface.up,
      role_interfaces: {
        ready: ri.ready,
        passive_ready: ri.passive_ready,
        resolved: ri.resolved,
        vehicle_buses_ready: ri.vehicle_buses_ready,
        mode: ri.mode,
        issues: ri.issues,
        generation: ri.generation,
        roles,
      },
    },
  };
}

// ---------------------------------------------------------------------------
// shared utilities

test("createStableGate returns the previous object for structurally equal input", () => {
  const gate = H.createStableGate();
  const a = gate({ x: 1, y: [1, 2] });
  const b = gate({ x: 1, y: [1, 2] });
  const c = gate({ x: 2 });
  assert.equal(a, b);
  assert.notEqual(a, c);
});

test("agoText buckets: just now under a minute, then coarse ages", () => {
  assert.equal(H.agoText(356), "just now");
  assert.equal(H.agoText(14999), "just now");
  assert.equal(H.agoText(45000), "just now");
  assert.equal(H.agoText(59999), "just now");
  assert.equal(H.agoText(90000), "1 min ago");
  assert.equal(H.agoText(null), "—");
});

// ---------------------------------------------------------------------------
// History: cards and ordering

test("trend metrics follow the owner's reading order with friendly titles", () => {
  const metrics = H.trendMetrics(HISTORY);
  assert.equal(metrics.length, 11);
  assert.deepEqual(metrics.slice(0, 5), [
    "engine.coolant_temperature",
    "transmission.oil_temperature",
    "engine.oil_pressure",
    "engine.vvt_oil_temperature",
    "battery.voltage",
  ]);
  assert.deepEqual(metrics.slice(-4), ["tire.pressure.fl", "tire.pressure.fr", "tire.pressure.rl", "tire.pressure.rr"]);
  const cards = H.historyCards(HISTORY);
  assert.equal(cards[0].id, "trips");
  assert.equal(cards[1].id, "trend:engine.coolant_temperature");
  assert.equal(cards[1].label, "Coolant trend");
  for (const card of cards) assert.doesNotMatch(card.label, /\w\.\w/, "no raw metric ids as labels: " + card.label);
  assert.equal(H.trendTitle("engine.vvt_oil_temperature"), "Oil temperature (VVT)");
  assert.equal(H.trendTitle("tire.pressure.rr"), "RR tire");
});

test("history cards fall back to the known list before the first summary", () => {
  const cards = H.historyCards(null);
  assert.equal(cards.length, 1 + H.TREND_ORDER.length);
  const extra = clone(HISTORY);
  extra.metric_trends["zeta.metric"] = { unit: "x" };
  assert.equal(H.trendMetrics(extra).at(-1), "zeta.metric");
});

// ---------------------------------------------------------------------------
// History: coverage line

test("coverage line: recording with the sample age, once", () => {
  const line = H.coverageLine(HISTORY, { extraMs: 0, nowMs: NOW });
  assert.equal(line.text, "Recording · last sample just now");
  assert.equal(line.tone, "");
  assert.deepEqual(line.problems, []);
  assert.equal(H.coverageLine(HISTORY, { extraMs: 95000, nowMs: NOW }).text, "Recording · last sample 1 min ago");
});

test("coverage line: degraded, stopped, nothing yet, unavailable and load errors", () => {
  const degraded = clone(HISTORY);
  degraded.coverage.status = "degraded";
  degraded.coverage.active_interface_gaps = [{ key: "b-can" }];
  const d = H.coverageLine(degraded, { nowMs: NOW });
  assert.equal(d.text, "Recording with gaps · 1 bus link missing · last sample just now");
  assert.equal(d.tone, "amber");

  const stopped = clone(HISTORY);
  stopped.coverage.status = "stale";
  stopped.coverage.age_seconds = 5400;
  const s = H.coverageLine(stopped, { nowMs: NOW });
  assert.equal(s.text, "Recording has stopped · last sample 12:57 pm");
  assert.doesNotMatch(s.text, /stale/i);

  const none = clone(HISTORY);
  none.coverage.status = "no_history";
  assert.equal(H.coverageLine(none, { nowMs: NOW }).text, "Nothing recorded yet");
  assert.equal(H.coverageLine({ available: false }, { nowMs: NOW }).text, "History is not available right now");
  assert.equal(H.coverageLine(null, {}).text, "Waiting for history…");
  assert.match(H.coverageLine(null, { error: { reason: "cache_unavailable" } }).text, /could not be loaded · cache unavailable/);
  assert.ok(H.coverageLine(HISTORY, { error: { reason: "timeout" }, nowMs: NOW }).problems.some((p) => /previous history/.test(p)));
});

test("coverage line: retention and maintenance errors appear when present", () => {
  const h = clone(HISTORY);
  h.coverage.retention.last_status = "partial";
  h.maintenance_hook.last_error = "database is locked";
  const line = H.coverageLine(h, { nowMs: NOW });
  assert.deepEqual(line.problems, ["Old-data cleanup only partly finished", "Cleanup check failed: database is locked"]);
});

// ---------------------------------------------------------------------------
// History: trips

test("trips: the last five with duration first, start, end and sample count", () => {
  const t = H.tripsModel(HISTORY, { nowMs: NOW });
  assert.equal(t.current, null);
  assert.equal(t.rows.length, 5);
  assert.deepEqual(t.rows[0], { id: "57", when: "10:22 am", duration: "2 min", meta: "ended 10:25 am · 79 samples" });
  assert.equal(t.rows[2].duration, "1 h 05");
  assert.equal(t.rows[2].when, "yesterday 10:21 pm");
  assert.equal(t.emptyText, null);
  const text = shownText(t);
  assert.doesNotMatch(text, JARGON);
});

test("trips: open trip, unusual end reasons, cap at five, empty", () => {
  const h = clone(HISTORY);
  h.current_trip = {
    id: 58,
    state: "open",
    started_at: "2026-09-22T18:30:00+00:00",
    duration_seconds: 1500,
    snapshot_count: 300,
  };
  h.recent_trips[1].end_reason = "broker_restart";
  h.recent_trips.push(clone(h.recent_trips[0]), clone(h.recent_trips[0]));
  h.recent_trips[5].id = 90;
  h.recent_trips[6].id = 91;
  const t = H.tripsModel(h, { nowMs: NOW, extraMs: 120000 });
  assert.deepEqual(t.current, { duration: "27 min", title: "In progress", meta: "started 12:30 pm · 300 samples" });
  assert.equal(t.rows.length, 5);
  assert.match(t.rows[1].meta, /ended when the broker restarted$/);
  assert.deepEqual(H.tripsModel({ recent_trips: [] }, { nowMs: NOW }).emptyText, "No trips recorded yet.");
  assert.equal(H.tripsModel(null, { nowMs: NOW }).emptyText, "Waiting for history…");
});

// ---------------------------------------------------------------------------
// History: trend cards

test("trend card: 7- and 30-day low/avg/high with units, usual trip, no dead current column", () => {
  const m = H.trendModel("engine.coolant_temperature", HISTORY.metric_trends["engine.coolant_temperature"], {
    nowMs: NOW,
    generatedAt: HISTORY.generated_at,
    tripOpen: false,
  });
  assert.equal(m.title, "Coolant");
  assert.equal(m.unit, "°F");
  assert.deepEqual(m.stats.map((r) => r.label), ["7 days", "30 days"]);
  const thirty = HISTORY.metric_trends["engine.coolant_temperature"].days_30;
  assert.equal(m.stats[1].low, String(Math.round(thirty.minimum)));
  assert.equal(m.stats[1].high, String(Math.round(thirty.maximum)));
  assert.equal(m.typical, "Usual trip 194 ± 5 °F avg · 10 trips");
  assert.equal(m.current, null);
  assert.equal(m.emptyText, null);
  assert.doesNotMatch(shownText(m), JARGON);
});

test("trend card: voltage keeps two decimals; the trip difference reads above/below/same", () => {
  const v = H.trendModel("battery.voltage", HISTORY.metric_trends["battery.voltage"], { nowMs: NOW });
  assert.equal(v.typical, "Usual trip 13.81 ± 0.17 V avg · 10 trips");
  assert.equal(v.stats[0].low, "11.20");

  const t = clone(HISTORY.metric_trends["engine.coolant_temperature"]);
  t.current_trip = { mean: 201.2, minimum: 150, maximum: 210 };
  t.current_minus_prior_median = 7.3;
  assert.equal(H.trendModel("engine.coolant_temperature", t, { nowMs: NOW }).current, "This trip 201 °F avg · 7 above usual");
  t.current_minus_prior_median = -3.4;
  assert.equal(H.trendModel("engine.coolant_temperature", t, { nowMs: NOW }).current, "This trip 201 °F avg · 3 below usual");
  t.current_minus_prior_median = 0.2;
  assert.equal(H.trendModel("engine.coolant_temperature", t, { nowMs: NOW }).current, "This trip 201 °F avg · same as usual");
  // A closed trip hides the current line even if a stale aggregate lingers.
  assert.equal(H.trendModel("engine.coolant_temperature", t, { nowMs: NOW, tripOpen: false }).current, null);
});

test("trend sparkline: time-based x axis over the series window, gaps break the line", () => {
  const trend = HISTORY.metric_trends["battery.voltage"];
  const m = H.trendModel("battery.voltage", trend, { nowMs: NOW, generatedAt: HISTORY.generated_at });
  assert.equal(m.spark.empty, false);
  assert.ok(m.spark.segments.length > 1, "the fixture has missing buckets, so the line breaks");
  for (const seg of m.spark.segments) {
    for (const pair of seg.points.split(" ")) {
      const [x, y] = pair.split(",").map(Number);
      assert.ok(x >= 0 && x <= m.spark.width && y >= 0 && y <= m.spark.height, pair);
    }
  }
  assert.match(m.sparkCaption, /^24 h range \d+\.\d\d to \d+\.\d\d V$/);
  assert.equal(m.axisStart, "yesterday 12:57 pm");
  assert.equal(m.axisEnd, "12:57 pm");

  const start = Date.parse("2026-09-22T00:00:00Z");
  const at = (min) => new Date(start + min * 60000).toISOString();
  const g = H.sparkGeometry(
    [
      { at: at(0), value: 1 },
      { at: at(15), value: 2 },
      { at: at(30), value: 3 },
      { at: at(90), value: 2 }, // two missing buckets before this one: a lone dash
      { at: at(1440), value: 5 },
    ],
    { startMs: start, endMs: start + 86400000, bucketMs: 900000, minSpan: 1 },
  );
  assert.equal(g.segments.length, 3);
  assert.equal(g.segments[0].points.split(" ")[0].split(",")[0], "0");
  assert.equal(g.segments[1].points.split(" ").length, 2, "a lone bucket is drawn as a short dash");
  assert.equal(g.segments[2].points.split(" ")[1].split(",")[0], "300");
  assert.deepEqual([g.lo, g.hi], [1, 5]);
  assert.ok(H.sparkGeometry([], {}).empty);
});

test("trend sparkline: a flat line uses the minimum span instead of exaggerating noise", () => {
  const start = Date.parse("2026-09-22T00:00:00Z");
  const g = H.sparkGeometry(
    [
      { at: new Date(start).toISOString(), value: 12.5 },
      { at: new Date(start + 900000).toISOString(), value: 12.52 },
    ],
    { startMs: start, endMs: start + 86400000, minSpan: 0.5 },
  );
  const ys = g.segments[0].points.split(" ").map((p) => Number(p.split(",")[1]));
  assert.ok(Math.abs(ys[0] - ys[1]) < 3, "0.02 V barely moves on a 0.5 V span");
});

test("trend card without data says so once", () => {
  const m = H.trendModel("engine.oil_pressure", null, { nowMs: NOW });
  assert.equal(m.emptyText, "No readings in the last 30 days.");
  assert.equal(m.sparkCaption, "No readings in the last 24 h");
});

test("every history trend card is free of jargon and raw ids", () => {
  for (const metric of H.trendMetrics(HISTORY)) {
    const m = H.trendModel(metric, HISTORY.metric_trends[metric], { nowMs: NOW, generatedAt: HISTORY.generated_at });
    assert.doesNotMatch(shownText(m), JARGON, metric);
    assert.doesNotMatch(m.title, /\./, metric);
  }
});

// ---------------------------------------------------------------------------
// System: vehicle state

test("vehicle line: one line with the plain evidence and the last drive", () => {
  const v = FULL.vehicle_state;
  assert.equal(H.vehicleLine(v, { running: false, lastDriveAt: HISTORY.recent_trips[0].ended_at, nowMs: NOW }), "Asleep · bus silent · last drive 10:25 am");
  assert.equal(
    H.vehicleLine({ state: "running", basis: "qualified_ccan_0x0fc_engine_speed" }, { running: true }),
    "Running · engine-speed signal",
  );
  assert.equal(H.vehicleLine({ state: "unknown", basis: "client_freshness_expired" }, { running: true }), "Running");
  assert.equal(H.vehicleLine({ state: "ignition_on", basis: "ccan_0x2ef_ignition_gate" }, {}), "Ignition on · ignition signal");
  assert.equal(H.vehicleLine(null, {}), "Unknown · not reported");
});

test("vehicle disclosure words: basis and confidence in plain English", () => {
  assert.equal(H.basisText("passive_bus_silence"), "bus silent");
  assert.equal(H.basisText("something_new"), "something new");
  assert.equal(H.confidenceText("inferred"), "inferred");
  assert.equal(H.confidenceText("verified"), "confirmed by a verified signal");
  assert.equal(H.confidenceText(null), "unknown");
  for (const text of Object.values(H.BASIS_TEXT).concat(Object.values(H.CONFIDENCE_TEXT))) {
    assert.doesNotMatch(text, /stale|0x|ccan/i, text);
  }
});

// ---------------------------------------------------------------------------
// System: buses

test("buses: three rows, listening, bit rate, controller normal instead of ERROR-ACTIVE", () => {
  const b = H.busesModel(liteOf(FULL), FULL);
  assert.deepEqual(b.rows.map((r) => r.id), ["c-can", "b-can", "can-ch"]);
  assert.deepEqual(b.rows.map((r) => r.main), [
    "C-CAN · listening · 500 kbit/s",
    "B-CAN · listening · 125 kbit/s",
    "CAN-CH · listening · 500 kbit/s",
  ]);
  for (const row of b.rows) {
    assert.equal(row.tone, "green");
    assert.equal(row.side, "controller normal");
    assert.equal(row.meta, null);
  }
  assert.deepEqual(b.inhibits, { text: "Safety inhibits: none", tone: "" });
  assert.equal(b.owner.text, "Bus use: listening only, nothing else is using the buses");
  assert.deepEqual(b.issues, []);
  assert.equal(b.tone, "");
  const cards = shownText({ rows: b.rows, inhibits: b.inhibits, owner: b.owner });
  assert.doesNotMatch(cards, /ERROR-ACTIVE/);
  // The disclosure carries identities (serial tail only) and the raw controller state, explained.
  const cRow = b.adapter.find((a) => a.id === "c-can");
  assert.match(cRow.text, /^C-CAN → can0 · board A CAN1 · pins 6\/14 · serial …250013 · dev 0 · controller ERROR-ACTIVE \(normal\)$/);
  assert.ok(b.facts.some(([k, v]) => k === "USB adapter watch" && v === "Running · no open incidents"));
});

test("buses: a deaf adapter is red with a plain fix, not 'controller normal'", () => {
  const full = clone(FULL);
  full.interface.role_interfaces.roles["c-can"].receive_watch = {
    receive_silent: true, silent_seconds: 400, reference_role: "can-ch", reference_active_seconds: 400,
  };
  const b = H.busesModel(liteOf(full), full);
  const row = b.rows.find((r) => r.id === "c-can");
  assert.equal(row.tone, "red");
  assert.equal(row.main, "C-CAN · hearing nothing · 500 kbit/s");
  assert.match(row.meta, /^No frames for 6 min while CAN-CH is busy\. Unplug the adapter's USB cable for 5 s, or reboot the Pi\.$/);
  assert.equal(b.rows.find((r) => r.id === "b-can").tone, "green");
});

test("buses: faults colour the dot and explain themselves", () => {
  const lite = liteOf(FULL);
  const full = clone(FULL);
  lite.interface.role_interfaces.roles["b-can"] = { resolution: "unresolved", safe: false, passive_ready: false, reason: "adapter_missing" };
  delete full.interface.role_interfaces.roles["b-can"];
  full.interface.role_interfaces.roles["can-ch"].actual.controller_state = "ERROR-PASSIVE";
  full.interface.role_interfaces.roles["c-can"].actual.controller_state = "BUS-OFF";
  lite.interface.active_inhibits = ["dtc_scan_in_progress"];
  full.current_owner = { kind: "participating_or_external_can_user", detail: "can0 is busy" };
  lite.interface.role_interfaces.issues = [{ reason: "spare_unsafe", detail: "spare adapter link is up" }];
  const b = H.busesModel(lite, full);
  const byId = Object.fromEntries(b.rows.map((r) => [r.id, r]));
  assert.equal(byId["c-can"].tone, "red");
  assert.equal(byId["c-can"].side, "controller off the bus");
  assert.equal(byId["b-can"].tone, "red");
  assert.equal(byId["b-can"].main, "B-CAN · adapter not found");
  assert.equal(byId["b-can"].meta, "Adapter missing");
  assert.equal(byId["can-ch"].tone, "amber");
  assert.equal(b.inhibits.text, "Safety inhibits: dtc scan in progress");
  assert.equal(b.inhibits.tone, "amber");
  assert.equal(b.owner.text, "Bus use: another CAN tool · can0 is busy");
  assert.deepEqual(b.issues, ["Spare adapter link is up"]);
  assert.equal(b.tone, "red");
});

test("buses: owner wording for every broker owner kind, and missing data", () => {
  const lite = liteOf(FULL);
  const withOwner = (owner) => {
    const f = clone(FULL);
    f.current_owner = owner;
    return H.busesModel(lite, f).owner.text;
  };
  assert.equal(withOwner({ kind: "broker_active_drive", roles: ["c-can", "b-can"] }), "Bus use: drive helper is reading on C-CAN and B-CAN");
  assert.equal(withOwner({ kind: "broker_auxiliary_drive", roles: ["b-can"] }), "Bus use: odometer helper is reading on B-CAN");
  assert.equal(withOwner({ kind: "external_inhibit", names: ["manual_capture"] }), "Bus use: blocked by manual_capture");
  assert.equal(withOwner({ kind: "broker", operations: ["battery_voltage"] }), "Bus use: broker is reading battery voltage");
  // Live broker shape: inflight entries are {metric, mode} objects, never "[object Object]".
  assert.equal(withOwner({ kind: "broker", operations: [{ metric: "battery.voltage", mode: "passive" }] }), "Bus use: broker is reading battery voltage");
  assert.equal(withOwner({ kind: "broker", operations: [{ metric: "tire.pressure.fl", mode: "passive" }, { metric: "engine.rpm" }] }), "Bus use: broker is reading FL tire, engine speed");
  assert.equal(withOwner({ kind: "broker", operations: [{}] }), "Bus use: broker is reading now");
  // Lite only (no status_full yet): mode from the interface, no bit rate, no owner line.
  const b = H.busesModel(lite, null);
  assert.equal(b.rows[0].main, "C-CAN · listening");
  assert.equal(b.owner, null);
  const empty = H.busesModel(null, null);
  assert.equal(empty.rows[0].main, "C-CAN · not reported");
  assert.equal(empty.inhibits.text, "Safety inhibits: not reported");
});

test("controller wording maps every CAN controller state to plain words", () => {
  assert.equal(H.controllerText("ERROR-ACTIVE").text, "controller normal");
  assert.equal(H.controllerText("ERROR-WARNING").tone, "amber");
  assert.equal(H.controllerText("BUS-OFF").tone, "red");
  assert.equal(H.controllerText(null).text, "");
});

// ---------------------------------------------------------------------------
// System: broker

test("broker: collector, recorder, cache age on the Pi clock, storage and helpers", () => {
  const b = H.brokerModel(FULL, NOW);
  const rows = Object.fromEntries(b.rows.map((r) => [r.k, r.v]));
  assert.equal(rows.Collector, "Running · every 1\u00a0s");
  assert.equal(rows.Cycles, "6,848");
  assert.equal(rows["Last cycle"], "12:58 pm");
  assert.equal(rows["History recorder"], "Running · every 5\u00a0s · 2,316 saved");
  assert.equal(rows["Last saved"], "12:58 pm");
  assert.equal(rows["Summary cache"], "Ready · updated 12:57 pm · 1 min old · every 60\u00a0s");
  assert.equal(rows["Last readings"], "Saved to disk");
  assert.equal(rows["Drive helper"], "Idle · waiting for the engine");
  assert.equal(rows["Odometer helper"], "Idle · waiting for the engine");
  assert.equal(rows["Broker started"], "9:17 am");
  assert.ok(b.rows.every((r) => r.tone === ""));
  assert.doesNotMatch(shownText(b), /T\d\d:\d\d:\d\d|\+00:00/, "no raw ISO timestamps");
  assert.doesNotMatch(shownText(b), JARGON);
});

test("broker: failures are red and named; missing status says waiting", () => {
  const f = clone(FULL);
  f.active_drive.restoration_failed = true;
  f.last_readings = { persistent: false, storage_error: "disk full" };
  f.history_recorder.last_error = "database is locked";
  f.auxiliary_drive.enabled = false;
  const rows = H.brokerModel(f, NOW).rows;
  const by = Object.fromEntries(rows.map((r) => [r.k, r]));
  assert.equal(by["Drive helper"].tone, "red");
  assert.match(by["Drive helper"].v, /Restore failed/);
  assert.equal(by["Last readings"].v, "Not saving · disk full");
  assert.equal(by["Last readings"].tone, "red");
  assert.equal(by["Recorder problem"].tone, "red");
  assert.equal(by["Odometer helper"].v, "Off");
  const memory = clone(FULL);
  memory.last_readings = { persistent: false, storage_error: null };
  assert.equal(H.brokerModel(memory, NOW).rows.find((r) => r.k === "Last readings").v, "Memory only (lost on restart)");
  assert.deepEqual(H.brokerModel(null, NOW), { rows: [], empty: true });
});

// ---------------------------------------------------------------------------
// System: radar alignment

test("radar: not live → retained values, neutral badge, last-read time once", () => {
  const r = H.radarModel({
    axes: { elevation: { fresh: false }, azimuth: { fresh: false } },
    windows: FULL.radar_alignment,
    polling: FULL.radar_alignment_polling,
    nowMs: NOW,
  });
  assert.deepEqual(r.badge, { text: "Within ±1°", tone: "" });
  assert.equal(r.note, "Not live · last read 10:25 am");
  const el = r.axes[0];
  const az = r.axes[1];
  assert.equal(el.label, "Vertical (elevation)");
  assert.equal(el.latest, "−0.02°");
  assert.equal(az.latest, "+0.16°");
  assert.equal(el.live, false);
  assert.deepEqual(el.rows, [
    ["1-min average", "−0.02° (6)"],
    ["5-min average", "−0.02° (16)"],
    ["5-min peak", "0.02°"],
    ["Margin to ±1.00°", "0.98° to spare"],
  ]);
  assert.equal(az.rows[3][1], "0.84° to spare");
  assert.doesNotMatch(shownText(r), JARGON);
});

test("radar: live values colour the badge; outside the reference shows the overshoot", () => {
  const live = (el, az) =>
    H.radarModel({
      axes: { elevation: { fresh: true, value: el }, azimuth: { fresh: true, value: az } },
      windows: {},
      polling: { commissioned: true },
      nowMs: NOW,
    });
  assert.deepEqual(live(0.1, -0.2).badge, { text: "Within ±1°", tone: "green" });
  assert.deepEqual(live(0.1, -0.85).badge, { text: "Approaching ±1°", tone: "amber" });
  const out = live(1.2, 0);
  assert.deepEqual(out.badge, { text: "Outside ±1°", tone: "red" });
  assert.equal(out.note, null);
  assert.equal(out.axes[0].rows[3][1], "0.20° beyond");
  assert.equal(out.axes[0].rows[0][1], "—");
});

test("radar: held reading beats the window latest; not set up and no data explain themselves", () => {
  const held = H.radarModel({
    axes: {
      elevation: { fresh: false, held: { value: 0.5, observed_at: "2026-09-22T17:00:00Z" } },
      azimuth: { fresh: false },
    },
    windows: FULL.radar_alignment,
    polling: FULL.radar_alignment_polling,
    nowMs: NOW,
  });
  assert.equal(held.axes[0].latest, "+0.50°");
  assert.equal(held.note, "Not live · last read 11:00 am");
  const off = H.radarModel({ axes: {}, windows: {}, polling: { commissioned: false, detail: "Radar reads are disabled." }, nowMs: NOW });
  assert.deepEqual(off.badge, { text: "Not set up", tone: "" });
  assert.equal(off.note, "Radar reads are disabled.");
  const none = H.radarModel({ axes: {}, windows: {}, polling: { commissioned: true }, nowMs: NOW });
  assert.equal(none.badge.text, "No readings yet");
  assert.equal(none.axes[0].latest, "—");
  const err = H.radarModel({ axes: {}, windows: {}, polling: { commissioned: true, retained_storage_error: "read-only" }, nowMs: NOW });
  assert.match(err.problem, /not being saved: read-only/);
});

// ---------------------------------------------------------------------------
// System: metric catalog

test("catalog: every metric listed; raw diagnostics and unconfirmed metrics are diagnostic-only", () => {
  const c = H.catalogModel(SNAPSHOT.catalog, RETAIN_LAST_READING);
  assert.equal(c.main.length + c.diagnostic.length, SNAPSHOT.catalog.length);
  const diag = c.diagnostic.map((r) => r.name).sort();
  assert.deepEqual(diag, [
    "diagnostics.cluster.did.0107.raw",
    "diagnostics.cluster.did.1000.raw",
    "diagnostics.cluster.did.1002.raw",
    "diagnostics.cluster.did.1005.raw",
    "radar.alignment.azimuth",
    "radar.alignment.elevation",
    "vehicle.odometer",
  ]);
  const main = c.main.map((r) => r.name);
  for (const name of ["engine.target_crankshaft_torque", "transmission.output_speed", "transmission.turbine_speed", "vehicle.ignition_on"]) {
    assert.ok(main.includes(name), name + " stays visible in the main list");
  }
  const coolant = c.main.find((r) => r.name === "engine.coolant_temperature");
  assert.equal(coolant.label, "Coolant");
  assert.deepEqual(coolant.sources, ["broadcast 0x2ED · C-CAN · matches scan tool"]);
  assert.equal(coolant.policy, "current for 5 s · keeps last reading");
  const rpm = c.main.find((r) => r.name === "engine.rpm");
  assert.equal(rpm.policy, "current for 5 s · live only");
  const battery = c.main.find((r) => r.name === "battery.voltage");
  assert.deepEqual(battery.sources, [
    "broadcast 0x46C · B-CAN · verified",
    "broadcast 0x41A · C-CAN · verified",
    "cluster read 1004 · C-CAN · matches scan tool",
  ]);
  assert.equal(c.diagnostic.find((r) => r.name === "diagnostics.cluster.did.0107.raw").label, "Cluster value 0107");
  for (const row of c.main.concat(c.diagnostic)) {
    assert.notEqual(row.label, row.name);
    assert.doesNotMatch(row.label + "\n" + row.sources.join("\n") + "\n" + row.policy, JARGON, row.name);
  }
  assert.deepEqual(H.catalogModel(null, RETAIN_LAST_READING), { main: [], diagnostic: [] });
});

test("catalog values: live, dated last reading, dash; ignition is on or a dash", () => {
  const def = (n) => SNAPSHOT.catalog.find((d) => d.name === n);
  const rec = (value, extra) => Object.assign({ available: true, stale: false, value, unit: null }, extra || {});
  assert.deepEqual(H.catalogValue("engine.coolant_temperature", rec(188.4, { unit: "°F" }), null, "°F", NOW), { text: "188 °F", held: false });
  assert.deepEqual(H.catalogValue("vehicle.ignition_on", rec(true), null, "boolean", NOW), { text: "on", held: false });
  assert.deepEqual(H.catalogValue("vehicle.ignition_on", rec(false), null, "boolean", NOW), { text: "—", held: false });
  assert.deepEqual(H.catalogValue("engine.rpm", rec(6303, { unit: "rpm" }), null, "rpm", NOW), { text: "6,303 rpm", held: false });
  assert.deepEqual(H.catalogValue("diagnostics.cluster.did.0107.raw", rec(37, { unit: "raw_u8" }), null, def("diagnostics.cluster.did.0107.raw").unit, NOW), {
    text: "37",
    held: false,
  });
  const held = { kind: "held", value: 12.6, unit: "V", retained: { value: 12.6, unit: "V", observed_at: "2026-09-22T18:00:17Z" } };
  assert.deepEqual(H.catalogValue("battery.voltage", rec(12.6, { stale: true }), held, "V", NOW), { text: "12.60 V · 12:00 pm", held: true });
  assert.deepEqual(H.catalogValue("radar.alignment.azimuth", rec(0.164, { unit: "deg" }), null, "deg", NOW), { text: "0.16°", held: false });
  assert.deepEqual(H.catalogValue("engine.rpm", null, { kind: "off" }, "rpm", NOW), { text: "—", held: false });
  assert.deepEqual(H.catalogValue("engine.rpm", rec(900, { available: false }), { kind: "off" }, "rpm", NOW), { text: "—", held: false });
});

test("source labels and quality words are plain", () => {
  assert.equal(H.sourceLabel("ccan.broadcast.0x0fc"), "broadcast 0x0FC");
  assert.equal(H.sourceLabel("pcm.did.06da"), "engine computer read 06DA");
  assert.equal(H.sourceLabel("rf_hub.did.31d0"), "RF hub read 31D0");
  assert.equal(H.sourceLabel("derived.pcm_06da_x_ccan_0x0fc"), "calculated");
  assert.equal(H.qualityWord("observed_alfa_scale"), "matches scan tool");
  assert.equal(H.qualityWord("candidate"), "unconfirmed");
  assert.equal(H.qualityWord(null), "quality not stated");
});

// ---------------------------------------------------------------------------
// System: customise and device

test("customise status per view: default, hidden, own order", () => {
  const cards = [{ id: "a" }, { id: "b" }, { id: "c" }];
  assert.equal(H.customiseStatus("parked", cards, {}), "Default layout");
  assert.equal(H.customiseStatus("parked", cards, { hidden: { parked: ["b", "zzz"] } }), "1 card hidden");
  assert.equal(H.customiseStatus("drive", cards, { hidden: { drive: ["a", "b"] } }, "tiles"), "2 tiles hidden");
  // Drive tiles keep fixed places, so a saved order there is not reported.
  assert.equal(H.customiseStatus("drive", cards, { order: { drive: ["c"] } }, "tiles"), "Default layout");
  assert.equal(H.customiseStatus("parked", cards, { order: { parked: ["c"] } }), "Own order");
  assert.equal(H.customiseStatus("parked", cards, { order: { parked: ["a"] } }), "Default layout");
  assert.equal(H.customiseStatus("parked", cards, { order: { parked: ["b", "a"] }, hidden: { parked: ["c"] } }), "1 card hidden · own order");
  assert.equal(H.customiseStatus("health", cards, { hidden: { parked: ["a"] } }), "Default layout");
  assert.deepEqual(H.CUSTOMISE_VIEWS.map((v) => v.view), ["drive", "parked", "health", "history", "system"]);
  assert.deepEqual(H.SYSTEM_CARD_IDS, ["vehicle", "buses", "broker", "radar", "catalog"]);
});

test("device and app rows: tablet facts, build id, listener and flags", () => {
  const device = Object.fromEntries(
    H.deviceRows({ userAgent: "Mozilla/5.0 (Linux; Android 11; SM-T500)", dpr: 1.5, width: 800, height: 1216, displayMode: "browser" }),
  );
  assert.equal(device.Screen, "800 × 1,216 px");
  assert.equal(device["Pixel ratio"], "1.50");
  assert.equal(device["Opened as"], "browser tab");
  assert.match(device.Browser, /SM-T500/);
  assert.equal(Object.fromEntries(H.deviceRows({ displayMode: "standalone" }))["Opened as"], "home-screen app (standalone)");
  assert.equal(Object.fromEntries(H.deviceRows({}))["Screen"], "—");

  const app = Object.fromEntries(H.appRows(SNAPSHOT.web, { build: "a1b2c3d4", builtAt: "2026-09-22T17:30:00Z" }, NOW));
  assert.equal(app["App build"], "a1b2c3d4 · built 11:30 am");
  assert.equal(app.Listener, "192.168.6.103:8765");
  assert.equal(app["Voltage read button"], "allowed here");
  assert.equal(app["Code scan"], "not on this listener");
  assert.equal(app["Codex advisor"], "available here");
  const fallback = Object.fromEntries(H.appRows({ build: "ffee", bind: "100.82.91.76:8766" }, null, NOW));
  assert.equal(fallback["App build"], "ffee");
  assert.equal(fallback["Code scan"], "not reported");
  assert.equal(Object.fromEntries(H.appRows(null, null, NOW))["App build"], "not reported");
});

// ---------------------------------------------------------------------------
// source contract: no inline styles, no timers, no per-second clock in the views

test("History and System views keep the performance contract", () => {
  for (const file of ["../src/views/History.jsx", "../src/views/System.jsx"]) {
    const src = readFileSync(here(file), "utf8");
    assert.doesNotMatch(src, /\bstyle=/, file + " has no inline styles");
    assert.doesNotMatch(src, /setInterval|setTimeout|requestAnimationFrame/, file + " has no timers");
    assert.doesNotMatch(src, /store\.clock/, file + " never reads the 1 Hz clock");
    assert.doesNotMatch(src, /getBoundingClientRect|offsetHeight|offsetWidth|scrollHeight/, file + " has no layout reads");
    assert.equal((src.match(/\/docs\/caveats\.html/g) || []).length, 0, file + " links docs through the helpers' DOCS table");
  }
});

// ---------------------------------------------------------------------------
// feature-parity additions (production app items that had no home in v2)

test("parity: the catalog says why a metric has no current value (was the tiles' status line)", () => {
  const m = SNAPSHOT.metrics;
  // Broker reasons from the September 22 snapshot, in plain words.
  assert.equal(H.catalogWhy(m["engine.crankshaft_torque"], { kind: "off" }), "No value: engine not running");
  assert.equal(H.catalogWhy(m["vehicle.odometer"], { kind: "held" }), "No value: adapter is not on this value's bus");
  assert.equal(H.catalogWhy(m["diagnostics.cluster.did.0107.raw"], { kind: "off" }), "No value: no recent reading");
  // The battery's retained value with a failed last read (production: "last attempt: …").
  assert.equal(H.catalogWhy(m["battery.voltage"], { kind: "held" }), "Last read failed: van asleep, no bus traffic");
  assert.equal(
    H.catalogWhy({ available: true, stale: false, value: 180, last_acquisition_error: { reason: "implausible_transition" } }, { kind: "live" }),
    "",
    "a current value needs no explanation",
  );
  assert.equal(
    H.catalogWhy({ available: true, stale: true, value: 180, last_acquisition_error: { reason: "implausible_transition" } }, { kind: "held" }),
    "Last read failed: reading rejected as implausible",
  );
  // Unknown reasons fall back to the broker's detail, then to the humanised reason.
  assert.equal(H.catalogWhy({ available: false, reason: "new_reason", detail: "the module was busy" }, null), "No value: the module was busy");
  assert.equal(H.catalogWhy({ available: false, reason: "new_reason" }, null), "No value: new reason");
  // Nothing to explain: no record, a fresh record, or a stale value with no error.
  assert.equal(H.catalogWhy(null, { kind: "off" }), "");
  assert.equal(H.catalogWhy(m["engine.rpm"], { kind: "live" }), "");
  assert.equal(H.catalogWhy({ available: true, stale: true, value: 12.6 }, { kind: "held" }), "");
  for (const name of Object.keys(m)) assert.doesNotMatch(H.catalogWhy(m[name], { kind: "off" }), JARGON, name);
});

test("parity: Device & app shows the broker link and its error (was the masthead's `Broker unavailable: …`)", () => {
  assert.equal(H.connectionText({ state: "live", reason: null, detail: null }), "Live");
  assert.equal(H.connectionText({ state: "resyncing", reason: "stream_error", detail: null }), "Refreshing");
  assert.equal(H.connectionText({ state: "connecting" }), "Connecting");
  assert.equal(H.connectionText({ state: "unavailable", reason: "fetch_failed", detail: "HTTP 502" }), "Broker unavailable · HTTP 502");
  assert.equal(H.connectionText({ state: "unavailable", reason: "broker_unavailable", detail: null }), "Broker unavailable", "no repeated words");
  assert.equal(H.connectionText({ state: "unavailable" }), "Broker unavailable");
  assert.equal(H.connectionText(null), "Unknown");
});

test("parity: an unavailable history product shows the broker's reason once", () => {
  const off = H.coverageLine({ available: false, reason: "history_disabled", detail: "telemetry history is disabled by broker configuration" }, { nowMs: NOW });
  assert.equal(off.text, "History is not available right now");
  assert.deepEqual(off.problems, ["Telemetry history is disabled by broker configuration"]);
  assert.deepEqual(H.coverageLine({ available: false }, { nowMs: NOW }).problems, []);
});

// ---------------------------------------------------------------------------
// System: the van's own health check fact row

test("van health check row: last ran, Pi quiet, not yet recorded, absent", () => {
  const now = Date.parse("2026-09-24T23:00:00Z");
  const dtcs = {
    available: true,
    in_vehicle_scan: {
      available: true,
      last_scan: { started_at: "2026-09-24T21:12:17.587Z", completed_at: "2026-09-24T21:27:08.649Z", pi_quiet: true },
    },
  };
  assert.deepEqual(H.vanCheckRow(dtcs, now), {
    k: "Van health check",
    v: "last ran 3:12 pm (while the Pi was not polling)",
    tone: "",
  });
  const busy = { in_vehicle_scan: { available: true, last_scan: { ...dtcs.in_vehicle_scan.last_scan, pi_quiet: false } } };
  assert.equal(H.vanCheckRow(busy, now).v, "last ran 3:12 pm");
  assert.equal(H.vanCheckRow({ in_vehicle_scan: { available: false, reason: "not_harvested" } }, now).v, "none recorded yet");
  assert.equal(H.vanCheckRow({ in_vehicle_scan: { available: false, reason: "in_vehicle_scan_unavailable" } }, now), null);
  assert.equal(H.vanCheckRow({ available: true }, now), null);
  assert.equal(H.vanCheckRow(null, now), null);
});
