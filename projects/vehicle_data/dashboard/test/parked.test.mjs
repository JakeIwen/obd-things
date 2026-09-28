// Parked view and Service card helpers (src/views/parked.helpers.js).
// Times are formatted in the van's zone; pin it so the suite is machine-independent.
process.env.TZ = "America/Denver";

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as H from "../src/views/parked.helpers.js";
import * as store from "../src/store.js";
import { buildWarningCards } from "../src/warnings.js";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const SUMMARY = JSON.parse(readFileSync(here("./fixtures/summary-v2.json"), "utf8"));
const SNAPSHOT = JSON.parse(readFileSync(here("./fixtures/snapshot.json"), "utf8"));
const NOW = Date.parse("2026-09-22T19:00:00Z"); // 1:00 pm MDT, the fixtures' afternoon
const HISTORY = SUMMARY.history;
const EOV = SUMMARY.status_full.engine_off_voltage;
const BATTERY_AT = SNAPSHOT.metrics["battery.voltage"].observed_at; // 18:00:17Z, held
const JARGON = /\b(stale|registered|alfa|mapped|unmapped|candidate|regime|episode)\b/i;

const clone = (v) => JSON.parse(JSON.stringify(v));

/** Every string a card displays (ids, keys and class tones are never shown). */
function shownText(value) {
  const out = [];
  const walk = (v, key) => {
    if (key === "id" || key === "key" || key === "tone" || key === "state" || key === "kind") return;
    if (typeof v === "string") out.push(v);
    else if (Array.isArray(v)) v.forEach((item) => walk(item, null));
    else if (v && typeof v === "object") for (const k of Object.keys(v)) walk(v[k], k);
  };
  walk(value, null);
  return out.join("\n");
}
const iso = (ms) => new Date(ms).toISOString();

// ---------------------------------------------------------------------------
// registry and layout

test("card registry: six unique ids with labels, in the design order", () => {
  assert.deepEqual(H.PARKED_IDS, ["battery", "tires", "trip", "service", "warnings", "codes"]);
  assert.equal(new Set(H.PARKED_IDS).size, H.PARKED_IDS.length);
  for (const card of H.PARKED_CARDS) assert.ok(card.label.length > 0);
  assert.equal(H.DOCS_URL, "/docs/caveats.html");
});

test("layoutSpans pairs Battery and Tires only when adjacent, whatever the order", () => {
  assert.deepEqual(H.layoutSpans(H.PARKED_IDS), {
    battery: "two", tires: "one", trip: "full", service: "full", warnings: "full", codes: "full",
  });
  const swapped = H.layoutSpans(["trip", "tires", "battery", "codes"]);
  assert.equal(swapped.battery, "two");
  assert.equal(swapped.tires, "one");
  const apart = H.layoutSpans(["battery", "trip", "tires"]);
  assert.deepEqual(apart, { battery: "full", trip: "full", tires: "full" });
  assert.deepEqual(H.layoutSpans(["battery"]), { battery: "full" });
  assert.deepEqual(H.layoutSpans(null), {});
});

// ---------------------------------------------------------------------------
// battery

test("fmtGap: seconds, minutes, floored tenths of hours, days", () => {
  assert.equal(H.fmtGap(0), "0 s");
  assert.equal(H.fmtGap(29000), "29 s");
  assert.equal(H.fmtGap(60000), "1 min");
  assert.equal(H.fmtGap(59 * 60000 + 59000), "59 min");
  assert.equal(H.fmtGap(3600000), "1 h");
  assert.equal(H.fmtGap(5692000), "1.5 h");
  assert.equal(H.fmtGap(2 * 86400000 + 5), "2 d");
  assert.equal(H.fmtGap(-1), "—");
  assert.equal(H.fmtGap(NaN), "—");
});

test("engineStopBefore picks the latest stop at or before the sample", () => {
  const sample = Date.parse(BATTERY_AT);
  const stop = H.engineStopBefore(sample, { engineOff: EOV, recentTrip: HISTORY.recent_trips[0] });
  assert.equal(stop, Date.parse(HISTORY.recent_trips[0].ended_at)); // 16:25:25 beats 16:25:22
  // A stop after the sample (e.g. a later drive) is ignored.
  const later = { ended_at: iso(sample + 600000) };
  assert.equal(H.engineStopBefore(sample, { recentTrip: later }), null);
  // An open trip counts only when the engine is not running.
  const open = { state: "open", last_active_at: iso(sample - 60000) };
  assert.equal(H.engineStopBefore(sample, { currentTrip: open }), sample - 60000);
  assert.equal(H.engineStopBefore(sample, { currentTrip: open, running: true }), null);
  assert.equal(H.engineStopBefore(NaN, { recentTrip: later }), null);
});

test("batterySubline describes the displayed sample itself", () => {
  const ctx = { engineOff: EOV, recentTrip: HISTORY.recent_trips[0] };
  assert.equal(
    H.batterySubline({ kind: "held", observedAt: BATTERY_AT, ...ctx }, NOW),
    "read 12:00 pm · 1.5 h after engine off",
  );
  // The engine-off sampler's own reading, 29 s after the stop.
  assert.equal(
    H.batterySubline({ kind: "held", observedAt: EOV.last_sample.observed_at, engineOff: EOV }, NOW),
    "read 10:25 am · 28 s after engine off",
  );
  const liveAt = iso(NOW - 3000);
  assert.equal(H.batterySubline({ kind: "live", observedAt: liveAt, ...ctx }, NOW), "now · 2.5 h after engine off");
  assert.equal(H.batterySubline({ kind: "live", observedAt: liveAt, running: true, ...ctx }, NOW), "engine running");
  assert.equal(H.batterySubline({ kind: "held", observedAt: BATTERY_AT }, NOW), "read 12:00 pm");
  assert.equal(
    H.batterySubline({ kind: "held", observedAt: "2026-09-21T18:00:17Z" }, NOW),
    "read yesterday 12:00 pm",
  );
  assert.equal(H.batterySubline({ kind: "off" }, NOW), "No reading yet");
  assert.equal(H.batterySubline(null, NOW), "No reading yet");
});

test("engineOffLine shows the settled reading only when it is not already on screen", () => {
  assert.equal(H.engineOffLine(EOV, BATTERY_AT, NOW), "After engine off: 12.70 V · 10:25 am");
  assert.equal(H.engineOffLine(EOV, EOV.last_sample.observed_at, NOW), "");
  const collecting = { enabled: true, state: "collecting", settles_at: "2026-09-22T18:59:52Z", last_sample: EOV.last_sample };
  assert.equal(H.engineOffLine(collecting, BATTERY_AT, NOW), "Resting reading due 12:59 pm");
  assert.equal(H.engineOffLine({ ...EOV, enabled: false }, BATTERY_AT, NOW), "");
  assert.equal(H.engineOffLine({ ...EOV, last_sample: { ...EOV.last_sample, unit: "mV" } }, BATTERY_AT, NOW), "");
  assert.equal(H.engineOffLine(null, BATTERY_AT, NOW), "");
});

test("battery badge and reading classes follow the band; held values stay dimmed", () => {
  assert.deepEqual(H.batteryBadge("red"), { text: "Very low", tone: "red" });
  assert.deepEqual(H.batteryBadge("amber"), { text: "Low", tone: "amber" });
  for (const state of ["normal", "neutral", "none", undefined]) assert.deepEqual(H.batteryBadge(state), { text: "", tone: "" });
  assert.equal(H.readingClass("live", "normal"), "reading num");
  assert.equal(H.readingClass("held", "amber"), "reading num reading--held reading--amber");
  assert.equal(H.readingClass("live", "red"), "reading num reading--red");
  assert.equal(H.readingClass("off", "red"), "reading num parked-reading--off");
});

test("trendModel draws the fixture's 24 h buckets inside the box with gaps visible", () => {
  const t = H.trendModel(HISTORY);
  assert.equal(t.empty, false);
  assert.equal(t.caption, "Last 24 h · 11.9–14.2 V");
  assert.ok(t.segments.length >= 2, "the fixture has gaps longer than 45 min");
  const keys = new Set(t.segments.map((s) => s.key));
  assert.equal(keys.size, t.segments.length, "segment keys are unique");
  let count = 0;
  for (const s of t.segments) {
    for (const pair of s.points.split(" ")) {
      const [x, y] = pair.split(",").map(Number);
      assert.ok(x >= 0 && x <= t.width, "x in box: " + x);
      assert.ok(y >= 0 && y <= t.height, "y in box: " + y);
      count += 1;
    }
  }
  assert.ok(count >= HISTORY.metric_trends["battery.voltage"].sparkline.length);
  assert.ok(t.refY !== null && t.refY > 0 && t.refY < t.height, "12.2 V guide inside the range");
});

test("trendModel: gaps split the line, lone buckets are short dashes, flat data keeps a minimum span", () => {
  const end = Date.parse("2026-09-22T18:00:00Z");
  const history = {
    generated_at: iso(end),
    metric_trends: {
      "battery.voltage": {
        sparkline: [
          { at: iso(end - 6 * 3600000), value: 12.6 },
          { at: iso(end - 6 * 3600000 + 900000), value: 12.6 },
          { at: iso(end - 3600000), value: 12.6 }, // lone bucket after a 4.75 h gap
          { at: iso(end - 2 * 86400000), value: 11.0 }, // outside the window
          { at: "garbage", value: 12.0 },
          { at: iso(end), value: null },
        ],
      },
    },
  };
  const t = H.trendModel(history);
  assert.equal(t.segments.length, 2);
  const [a, b] = t.segments;
  assert.equal(a.points.split(" ").length, 2);
  const dash = b.points.split(" ").map((p) => p.split(",").map(Number));
  assert.equal(dash.length, 2);
  assert.equal(dash[0][1], dash[1][1]);
  assert.ok(Math.abs(dash[1][0] - dash[0][0] - 3) < 0.11);
  // Flat 12.6 V sits mid-box with a 0.5 V span: the 12.2 V guide is outside it.
  assert.equal(dash[0][1], 28);
  assert.equal(t.refY, null);
  assert.equal(t.caption, "Last 24 h · 12.6–12.6 V");
});

test("trendModel is empty without usable buckets", () => {
  for (const h of [null, {}, { metric_trends: {} }, { metric_trends: { "battery.voltage": { sparkline: [] } } }]) {
    const t = H.trendModel(h);
    assert.equal(t.empty, true);
    assert.deepEqual(t.segments, []);
    assert.equal(t.caption, "No readings in the last 24 h");
  }
});

// ---------------------------------------------------------------------------
// read voltage now

test("acquireAvailability: checking until flags arrive, disabled with a reason, enabled", () => {
  assert.deepEqual(H.acquireAvailability({}), { enabled: false, note: H.TEXT.acquireChecking });
  assert.deepEqual(H.acquireAvailability(null), { enabled: false, note: H.TEXT.acquireChecking });
  assert.deepEqual(H.acquireAvailability({ active_acquisition_enabled: false }), { enabled: false, note: H.TEXT.acquireOff });
  assert.deepEqual(H.acquireAvailability(SNAPSHOT.web), { enabled: true, note: "" });
  // The web read is receive-only ({"mode":"passive"}); the note must never claim it can wake the van.
  assert.ok(H.TEXT.wakeNote.includes("nothing is sent"));
  assert.ok(!/can wake|will wake|may wake/i.test(H.TEXT.wakeNote));
});

test("readVoltage POSTs exactly {\"mode\":\"passive\"} same-origin and reports done", async () => {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    return { status: 200, ok: true, json: async () => ({ metric: "battery.voltage", available: true, unit: "V", value: 12.64 }) };
  };
  const out = await H.readVoltage({ fetch, signal: null });
  assert.deepEqual(out, { kind: "done", text: "Done · 12.64 V" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/v1/acquisitions/battery.voltage");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, '{"mode":"passive"}');
  assert.deepEqual(JSON.parse(calls[0].init.body), { mode: "passive" });
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(calls[0].init.signal, undefined);
});

test("refused reads explain themselves in plain English", async () => {
  const reply = (status, data) => async () => ({ status, ok: status < 300, json: async () => data });
  const asleep = await H.readVoltage({
    fetch: reply(503, { available: false, reason: "bus_asleep", detail: "resolved c-can produced no passive traffic" }),
    signal: null,
  });
  assert.equal(asleep.kind, "refused");
  assert.match(asleep.text, /^Refused: the van is asleep/);
  assert.doesNotMatch(asleep.text, /c-can|passive traffic/);
  const limited = H.classifyAcquire(429, false, { available: false, reason: "rate_limited", detail: "retry in 3.2 seconds" });
  assert.equal(limited.text, "Refused: asked too recently. Try again in 4 s.");
  const off = H.classifyAcquire(403, false, { available: false, reason: "web_acquisition_disabled", detail: "the web service was started cache-only" });
  assert.equal(off.text, "Refused: this screen can only show saved readings.");
  assert.equal(H.classifyAcquire(500, false, { reason: "restoration_failed", detail: "Adapter restore unverified" }).text, "Refused: Adapter restore unverified");
  assert.equal(H.classifyAcquire(502, false, { reason: "broker_down" }).text, "Refused: broker down");
  assert.equal(H.classifyAcquire(502, false, null).text, "Refused: the Pi answered HTTP 502.");
  // A 200 without a sample is not "done".
  assert.equal(H.classifyAcquire(200, true, { available: false, reason: "bus_asleep" }).kind, "refused");
  assert.deepEqual(H.classifyAcquire(200, true, { available: true }), { kind: "done", text: "Done" });
});

test("readVoltage never throws: network errors, timeouts and bad JSON are refusals", async () => {
  const down = await H.readVoltage({ fetch: async () => { throw new TypeError("Failed to fetch"); }, signal: null });
  assert.deepEqual(down, { kind: "refused", text: "Refused: the Pi could not be reached." });
  const slow = await H.readVoltage({
    fetch: async () => { const e = new Error("t"); e.name = "TimeoutError"; throw e; },
    signal: null,
  });
  assert.deepEqual(slow, { kind: "refused", text: "Refused: the Pi did not answer in time." });
  const html = await H.readVoltage({ fetch: async () => ({ status: 502, ok: false, json: async () => { throw new SyntaxError("<"); } }), signal: null });
  assert.deepEqual(html, { kind: "refused", text: "Refused: the Pi answered HTTP 502." });
});

// ---------------------------------------------------------------------------
// last trip

test("lastTripModel: last completed trip, the open trip, loading and unavailable", () => {
  assert.deepEqual(H.lastTripModel(HISTORY, { now: NOW }), {
    state: "ok", text: "", last: { duration: "2 min", ended: "ended 10:25 am" }, current: null,
  });
  const started = NOW - 32 * 60000;
  const running = { ...HISTORY, current_trip: { state: "open", started_at: iso(started), last_active_at: iso(NOW - 1000) } };
  const r = H.lastTripModel(running, { running: true, now: NOW });
  assert.deepEqual(r.current, { duration: "32 min", text: "started 12:28 pm" });
  assert.equal(r.last.duration, "2 min");
  const stopped = H.lastTripModel(running, { running: false, now: NOW + 120000 });
  assert.deepEqual(stopped.current, { duration: "31 min", text: "engine off 12:59 pm" });
  const long = { ...HISTORY, recent_trips: [{ ...HISTORY.recent_trips[0], duration_seconds: 3900 }] };
  assert.equal(H.lastTripModel(long, { now: NOW }).last.duration, "1 h 05");
  assert.equal(H.lastTripModel(null, { now: NOW }).state, "loading");
  assert.equal(H.lastTripModel({ available: false }, { now: NOW }).text, "Trip history is not available right now.");
  assert.equal(H.lastTripModel({ recent_trips: [], current_trip: null }, { now: NOW }).text, "No trips recorded yet.");
});

// ---------------------------------------------------------------------------
// service

function odometerStateFromSnapshot() {
  store.applyBaseline(clone(SNAPSHOT), 0, 1000);
  return {
    rec: store.metricSignal("vehicle.odometer").peek(),
    state: store.observationState("vehicle.odometer", 1000, NOW),
  };
}

test("odometerReading: the held estimate from the store, dimmed with its time and starred", () => {
  const { rec, state } = odometerStateFromSnapshot();
  assert.equal(state.kind, "held");
  const reading = H.odometerReading({ rec, state, maintenance: SUMMARY.maintenance });
  assert.deepEqual(reading, {
    value: 53892.766, unit: "mi", source: "ics.did.2001", observed_at: "2026-09-22T06:09:39.646302+00:00",
    quality: "candidate", live: false,
  });
  assert.deepEqual(H.odometerView(reading, NOW), { text: "53,892 mi", star: true, held: true, sub: "last read 12:09 am" });
});

test("odometerReading: live record wins; otherwise the newest retained reading", () => {
  const live = { available: true, stale: false, value: 53950.2, unit: "mi", source: "ics.did.2001", quality: "candidate", observed_at: iso(NOW) };
  const r = H.odometerReading({ rec: live, state: { kind: "held" }, maintenance: SUMMARY.maintenance });
  assert.equal(r.live, true);
  assert.equal(r.value, 53950.2);
  assert.deepEqual(H.odometerView(r, NOW), { text: "53,950 mi", star: true, held: false, sub: "current reading" });
  // Stale record: fall back to the maintenance payload's last known reading.
  const fallback = H.odometerReading({ rec: { ...live, stale: true }, state: { kind: "off" }, maintenance: SUMMARY.maintenance });
  assert.equal(fallback.value, 53892.766);
  assert.equal(fallback.live, false);
  // A newer held sample beats an older retained one.
  const held = { kind: "held", quality: "candidate", retained: { value: 54000, unit: "mi", observed_at: iso(NOW - 60000) } };
  assert.equal(H.odometerReading({ rec: null, state: held, maintenance: SUMMARY.maintenance }).value, 54000);
  assert.equal(H.odometerReading({}), null);
  assert.deepEqual(H.odometerView(null, NOW), { text: "—", star: false, held: false, sub: "No odometer reading yet" });
  const verified = { ...live, quality: "verified" };
  assert.equal(H.odometerView(H.odometerReading({ rec: verified }), NOW).star, false);
});

test("serviceModel on the fixture: last change, notes, history; no miles across sources; no oil life", () => {
  const { rec, state } = odometerStateFromSnapshot();
  const reading = H.odometerReading({ rec, state, maintenance: SUMMARY.maintenance });
  const m = H.serviceModel(SUMMARY.maintenance, reading, null, NOW);
  assert.equal(m.state, "ok");
  assert.equal(m.note, "");
  assert.deepEqual(m.last, { recorded: true, text: "Jul 24, 2026 · 52,000 mi", notes: "DIY" });
  assert.deepEqual(m.since, { text: "Not compared", sub: "different mileage sources", warn: false }, "cluster record vs ICS odometer: not compared (legacy rule)");
  assert.equal(m.oilLife, null, "oil_life.available is false");
  assert.equal(m.history.rows.length, 2);
  assert.equal(new Set(m.history.rows.map((r) => r.key)).size, 2, "duplicate records keep distinct keys");
  assert.deepEqual(m.history.rows[0], {
    key: "oil_5277b50188954a3bae963e49edbe0100", date: "Jul 24, 2026", meta: "52,000 mi · Cluster / service receipt", notes: "DIY",
  });
  assert.doesNotMatch(shownText(m), JARGON);
});

test("miles since service only when the sources match; negative distances ask for a check", () => {
  const maintenance = clone(SUMMARY.maintenance);
  maintenance.last_oil_change.mileage_source = "ics_estimate";
  const held = { value: 53892.766, unit: "mi", source: "ics.did.2001", observed_at: "2026-09-22T06:09:39Z", quality: "candidate", live: false };
  assert.deepEqual(H.serviceModel(maintenance, held, null, NOW).since, { text: "1,893 mi", sub: "estimated · at last reading", warn: false });
  assert.deepEqual(H.sinceServiceView({ ...held, live: true }, maintenance.last_oil_change), { text: "1,893 mi", sub: "estimated", warn: false });
  const ahead = { ...maintenance.last_oil_change, mileage_mi: 60000 };
  assert.deepEqual(H.sinceServiceView(held, ahead), { text: "Check the entry", sub: "service mileage is above the odometer", warn: true });
  assert.equal(H.sinceServiceView(null, maintenance.last_oil_change), null, "no odometer yet");
  assert.deepEqual(H.sinceServiceView(held, { ...maintenance.last_oil_change, mileage_mi: null, mileage_source: "unknown" }), { text: "Not known", sub: "the last oil change has no mileage", warn: false });
  assert.deepEqual(H.sinceServiceView(held, { ...maintenance.last_oil_change, mileage_source: "cluster_or_receipt" }), { text: "Not compared", sub: "different mileage sources", warn: false });
  assert.equal(H.sinceServiceView(held, null), null);
});

test("serviceModel: loading, unavailable, storage errors and no records", () => {
  assert.equal(H.serviceModel(null, null, null, NOW).state, "loading");
  assert.equal(H.serviceModel(null, null, null, NOW).note, "Loading service records…");
  const down = H.serviceModel({ available: false }, null, null, NOW);
  assert.equal(down.state, "unavailable");
  assert.equal(down.note, "Service records are unavailable on the Pi right now.");
  assert.equal(H.serviceModel({ available: false, storage_error: "disk full" }, null, null, NOW).note, "disk full");
  const warn = H.serviceModel({ ...SUMMARY.maintenance, storage_error: "last write failed" }, null, null, NOW);
  assert.equal(warn.state, "ok");
  assert.equal(warn.note, "last write failed");
  const empty = H.serviceModel({ available: true, persistent: true, oil_changes: [], record_count: 0, last_oil_change: null }, null, null, NOW);
  assert.deepEqual(empty.last, { recorded: false, text: "No service recorded", notes: "" });
  assert.equal(empty.since, null);
  assert.deepEqual(empty.history, { rows: [], more: "" });
  const noMiles = clone(SUMMARY.maintenance);
  noMiles.last_oil_change.mileage_mi = null;
  assert.equal(H.serviceModel(noMiles, null, null, NOW).last.text, "Jul 24, 2026 · Mileage not recorded");
});

test("oil life row appears only when the broker has a source", () => {
  assert.equal(H.oilLifeView(SUMMARY.maintenance, null, NOW), null);
  const on = { ...SUMMARY.maintenance, oil_life: { available: true, value: 17.4, observed_at: "2026-09-22T16:25:00Z" } };
  assert.deepEqual(H.oilLifeView(on, null, NOW), { text: "17 %", sub: "last read 10:25 am" });
  const viaMetric = { ...SUMMARY.maintenance, oil_life: { available: true } };
  const held = { kind: "held", value: 64, retained: { value: 64, unit: "%", observed_at: "2026-09-22T16:00:00Z" } };
  assert.deepEqual(H.oilLifeView(viaMetric, held, NOW), { text: "64 %", sub: "last read 10:00 am" });
  assert.deepEqual(H.oilLifeView(viaMetric, { kind: "live", value: 63 }, NOW), { text: "63 %", sub: "" });
  assert.deepEqual(H.oilLifeView(viaMetric, { kind: "off", value: null }, NOW), { text: "—", sub: "" });
  assert.equal(H.serviceModel(on, null, null, NOW).oilLife.text, "17 %");
});

// ---------------------------------------------------------------------------
// warnings and codes summaries

test("warningsSummary on the fixture: nothing open, last drive, unconfirmed items listed", () => {
  const model = buildWarningCards(SUMMARY.health, NOW);
  const w = H.warningsSummary(model, HISTORY, NOW);
  assert.equal(w.available, true);
  assert.equal(w.line, "Nothing open · last drive 10:25 am");
  assert.equal(w.badge, null);
  assert.equal(w.items.length, 2);
  for (const item of w.items) {
    assert.equal(item.tone, "");
    assert.match(item.meta, /^Unconfirmed · engine off since/);
  }
  assert.equal(w.more, "");
  assert.doesNotMatch(shownText(w), JARGON);
});

test("warningsSummary counts open items, red for critical, and caps the list at two", () => {
  const card = (id, state, tier, title) => ({ id, state, tier, title, sinceLabel: "4:25 pm", line: "x" });
  const model = {
    available: true,
    warnings: [
      card("a", "warning", "critical", "Oil pressure low"),
      card("b", "watch", "warning", "RR tire low"),
      card("c", "unconfirmed", "warning", "Coolant hot"),
    ],
  };
  const w = H.warningsSummary(model, HISTORY, NOW);
  assert.equal(w.line, "2 open");
  assert.deepEqual(w.badge, { text: "2", tone: "red" });
  assert.deepEqual(w.items, [
    { id: "a", title: "Oil pressure low", meta: "since 4:25 pm", tone: "red" },
    { id: "b", title: "RR tire low", meta: "since 4:25 pm", tone: "amber" },
  ]);
  assert.equal(w.more, "+1 more");
  const amber = H.warningsSummary({ available: true, warnings: [card("b", "watch", "warning", "RR tire low")] }, null, NOW);
  assert.deepEqual(amber.badge, { text: "1", tone: "amber" });
  const quiet = H.warningsSummary({ available: true, warnings: [] }, { recent_trips: [] }, NOW);
  assert.equal(quiet.line, "Nothing open");
  assert.equal(H.warningsSummary({ available: false }, HISTORY, NOW).line, "Warnings are not available right now.");
  assert.equal(H.warningsSummary(null, HISTORY, NOW).available, false);
});

test("codesSummary on the fixture: count from group_counts, first two titles, last scan", () => {
  const c = H.codesSummary(SUMMARY.dtcs, NOW);
  assert.equal(c.available, true);
  assert.equal(c.line, "5 current");
  assert.equal(c.sub, "last scan Jul 24, 8:32 pm");
  assert.deepEqual(c.badge, { text: "5", tone: "amber" });
  assert.deepEqual(c.items, [
    { key: "bcm_ccan:963215", code: "B1632-15", title: "Left high-beam circuit — Short to battery or open" },
    { key: "bcm_ccan:962E15", code: "B162E-15", title: "Right low-beam circuit — Short to battery or open" },
  ]);
  assert.equal(c.more, "+3 more");
});

test("codesSummary: pending, none, truncated groups, loading and unavailable", () => {
  const dtcs = clone(SUMMARY.dtcs);
  dtcs.group_counts = { current: 30, pending: 2 };
  dtcs.groups.current = dtcs.groups.current.slice(0, 1);
  const c = H.codesSummary(dtcs, NOW);
  assert.equal(c.line, "30 current · 2 pending");
  assert.equal(c.items.length, 1);
  assert.equal(c.more, "+29 more");
  const none = H.codesSummary({ available: true, group_counts: { current: 0 }, groups: { current: [] }, coverage: {} }, NOW);
  assert.equal(none.line, "No current codes");
  assert.equal(none.badge, null);
  assert.equal(none.sub, "");
  assert.equal(none.more, "");
  const flat = H.codesSummary({ available: true, current: [{ raw_dtc: "C150400", module_name: "RF Hub" }] }, NOW);
  assert.equal(flat.line, "1 current");
  assert.deepEqual(flat.items, [{ key: "module:C150400", code: "C150400", title: "RF Hub" }]);
  assert.equal(H.codesSummary(null, NOW).line, "Loading codes…");
  assert.equal(H.codesSummary({ available: false }, NOW).line, "Codes are not available right now.");
});

// ---------------------------------------------------------------------------
// rendering discipline

test("createStableGate returns the previous object while the content is unchanged", () => {
  const gate = H.createStableGate();
  const a = gate({ x: 1, list: [1, 2] });
  const b = gate({ x: 1, list: [1, 2] });
  assert.equal(a, b);
  const c = gate({ x: 2, list: [1, 2] });
  assert.notEqual(c, a);
  assert.deepEqual(c, { x: 2, list: [1, 2] });
  // Gates are independent.
  const other = H.createStableGate();
  assert.notEqual(other({ x: 2, list: [1, 2] }), c);
});

test("owner rules: no provenance jargon in any card text built from the fixtures", () => {
  const { rec, state } = odometerStateFromSnapshot();
  const reading = H.odometerReading({ rec, state, maintenance: SUMMARY.maintenance });
  const everything = [
    H.batterySubline({ kind: "held", observedAt: BATTERY_AT, engineOff: EOV, recentTrip: HISTORY.recent_trips[0] }, NOW),
    H.engineOffLine(EOV, BATTERY_AT, NOW),
    H.trendModel(HISTORY).caption,
    H.lastTripModel(HISTORY, { now: NOW }),
    H.serviceModel(SUMMARY.maintenance, reading, null, NOW),
    H.warningsSummary(buildWarningCards(SUMMARY.health, NOW), HISTORY, NOW),
    H.codesSummary(SUMMARY.dtcs, NOW),
    H.acquireAvailability({ active_acquisition_enabled: false }),
    H.classifyAcquire(503, false, { reason: "bus_asleep", detail: "resolved c-can produced no passive traffic" }),
    H.TEXT,
  ];
  const text = shownText(everything);
  assert.ok(text.length > 500);
  assert.doesNotMatch(text, JARGON);
  assert.doesNotMatch(text, /\b(null|undefined|NaN)\b/);
});

test("parity: oil life shows from the catalog metric even before the maintenance payload lists a source", () => {
  // The former static app's card read engine.oil_life_remaining directly; the broker still reports
  // maintenance.oil_life.available = false (September 22 payload).
  assert.equal(SUMMARY.maintenance.oil_life.available, false);
  assert.deepEqual(H.oilLifeView(SUMMARY.maintenance, { kind: "live", value: 63.4 }, NOW), { text: "63 %", sub: "" });
  const held = { kind: "held", value: 64, retained: { value: 64, unit: "%", observed_at: "2026-09-22T16:00:00Z" } };
  assert.deepEqual(H.oilLifeView(SUMMARY.maintenance, held, NOW), { text: "64 %", sub: "last read 10:00 am" });
  assert.equal(H.oilLifeView(SUMMARY.maintenance, { kind: "off", value: null }, NOW), null);
  assert.equal(H.oilLifeView(null, null, NOW), null);
  assert.equal(H.serviceModel(SUMMARY.maintenance, null, { kind: "live", value: 63.4 }, NOW).oilLife.text, "63 %");
});
