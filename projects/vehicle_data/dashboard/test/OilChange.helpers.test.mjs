import { test } from "node:test";
import assert from "node:assert/strict";
import * as H from "../src/dialogs/OilChange.helpers.js";

const TODAY = "2026-09-23";
const valid = (over = {}) => ({ date: "2026-07-24", mileage: "52000", source: "cluster_or_receipt", notes: "  DIY  ", ...over });

// The September 22 /v1/maintenance payload (scratchpad maintenance.json), trimmed.
const MAINTENANCE = {
  available: true,
  persistent: true,
  storage_error: null,
  record_count: 2,
  last_known_odometer: { observed_at: "2026-09-22T06:09:39.646302+00:00", quality: "candidate", source: "ics.did.2001", unit: "mi", value: 53892.766 },
  last_oil_change: { date: "2026-07-24", mileage_mi: 52000, mileage_source: "cluster_or_receipt", notes: "DIY", recorded_at: "2026-09-20T18:31:53.381803+00:00", request_id: "oil_5277b50188954a3bae963e49edbe0100" },
  oil_changes: [
    { date: "2026-07-24", mileage_mi: 52000, mileage_source: "cluster_or_receipt", notes: "DIY", recorded_at: "2026-09-20T18:31:53.381803+00:00", request_id: "oil_5277b50188954a3bae963e49edbe0100" },
    { date: "2026-07-24", mileage_mi: 52000, mileage_source: "cluster_or_receipt", notes: "DIY", recorded_at: "2026-09-20T18:29:51.245181+00:00", request_id: "oil_729f9d7aaaefc04387e7d1ff9b382632" },
  ],
};

function response(status, data, { json = true } = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => {
      if (!json) throw new SyntaxError("Unexpected token <");
      return data;
    },
  };
}

function fakeFetch(answers) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    const next = answers.shift();
    if (next instanceof Error) throw next;
    if (typeof next === "function") return next(url, init);
    return next;
  };
  fn.calls = calls;
  return fn;
}

const noSleep = async () => {};

// ---------- dates and numbers ----------
test("localToday formats the local calendar date", () => {
  assert.equal(H.localToday(new Date(2026, 8, 3, 23, 59)), "2026-09-03");
  assert.equal(H.localToday(new Date(2026, 0, 1, 0, 0)), "2026-01-01");
  assert.match(H.localToday(), /^\d{4}-\d{2}-\d{2}$/);
});

test("parseServiceDate is strict and zone-free", () => {
  assert.deepEqual(H.parseServiceDate("2026-07-24"), { year: 2026, month: 7, day: 24 });
  assert.deepEqual(H.parseServiceDate("2024-02-29"), { year: 2024, month: 2, day: 29 });
  for (const bad of ["2025-02-29", "2026-13-01", "2026-00-10", "2026-7-24", "2026-07-24T00:00", "", null, 20260724]) {
    assert.equal(H.parseServiceDate(bad), null, String(bad));
  }
});

test("formatServiceDate never shifts the day across time zones", () => {
  assert.equal(H.formatServiceDate("2026-07-24"), "Jul 24, 2026");
  assert.equal(H.formatServiceDate("2000-01-01"), "Jan 1, 2000");
  assert.equal(H.formatServiceDate("garbage"), "garbage");
  assert.equal(H.formatServiceDate(undefined), "—");
});

test("formatMiles keeps one decimal only when needed; formatOdometer truncates", () => {
  assert.equal(H.formatMiles(52000), "52,000");
  assert.equal(H.formatMiles(52000.5), "52,000.5");
  assert.equal(H.formatMiles(1892.766), "1,892.8");
  assert.equal(H.formatMiles(0), "0");
  assert.equal(H.formatMiles(NaN), "—");
  assert.equal(H.formatOdometer(53892.766), "53,892");
  assert.equal(H.formatOdometer(null), "—");
});

// ---------- validation ----------
test("validateOilChange builds the exact body and trims notes", () => {
  const v = H.validateOilChange(valid(), { today: TODAY });
  assert.equal(v.ok, true);
  assert.deepEqual(v.errors, {});
  assert.deepEqual(v.body, { date: "2026-07-24", mileage_mi: 52000, mileage_source: "cluster_or_receipt", notes: "DIY" });
  assert.deepEqual(Object.keys(v.body), ["date", "mileage_mi", "mileage_source", "notes"]);
});

test("empty mileage is saved as null with source unknown whatever the select says", () => {
  for (const source of ["cluster_or_receipt", "ics_estimate", "unknown", "bogus"]) {
    const v = H.validateOilChange(valid({ mileage: "  ", source }), { today: TODAY });
    assert.equal(v.ok, true, source);
    assert.equal(v.body.mileage_mi, null);
    assert.equal(v.body.mileage_source, "unknown");
  }
  assert.equal(H.validateOilChange(valid({ mileage: null }), { today: TODAY }).body.mileage_source, "unknown");
  assert.equal(H.sourceHint(valid({ mileage: "" })), H.TEXT.sourceIgnored);
  assert.equal(H.sourceHint(valid()), "");
});

test("a mileage with source unknown is refused (null mileage iff unknown)", () => {
  const v = H.validateOilChange(valid({ source: "unknown" }), { today: TODAY });
  assert.equal(v.ok, false);
  assert.equal(v.errors.source, H.TEXT.sourceNeeded);
  assert.equal(v.body, null);
});

test("ics_estimate is accepted and an unknown select value falls back to the default", () => {
  assert.equal(H.validateOilChange(valid({ source: "ics_estimate" }), { today: TODAY }).body.mileage_source, "ics_estimate");
  assert.equal(H.validateOilChange(valid({ source: "weird" }), { today: TODAY }).body.mileage_source, "cluster_or_receipt");
});

test("date must be YYYY-MM-DD between 2000-01-01 and today", () => {
  const at = (date) => H.validateOilChange(valid({ date }), { today: TODAY });
  assert.equal(at("").errors.date, H.ERRORS.dateFormat);
  assert.equal(at("2026-02-30").errors.date, H.ERRORS.dateFormat);
  assert.equal(at("1999-12-31").errors.date, H.ERRORS.dateRange);
  assert.equal(at("2026-09-24").errors.date, H.ERRORS.dateRange);
  assert.equal(at("2000-01-01").ok, true);
  assert.equal(at(TODAY).ok, true);
  assert.equal(at(" 2026-07-24 ").ok, true);
});

test("mileage bounds, step and unparseable input", () => {
  const at = (mileage, extra = {}) => H.validateOilChange(valid({ mileage, ...extra }), { today: TODAY });
  assert.equal(at("0").body.mileage_mi, 0);
  assert.equal(at("2000000").body.mileage_mi, 2000000);
  assert.equal(at("52000.3").body.mileage_mi, 52000.3);
  assert.equal(at(52000.1).body.mileage_mi, 52000.1);
  assert.equal(at("-1").errors.mileage, H.ERRORS.mileage);
  assert.equal(at("2000000.1").errors.mileage, H.ERRORS.mileage);
  assert.equal(at("abc").errors.mileage, H.ERRORS.mileage);
  assert.equal(at("Infinity").errors.mileage, H.ERRORS.mileage);
  assert.equal(at("52000.25").errors.mileage, H.TEXT.mileageStep);
  const bad = at("", { mileageBad: true });
  assert.equal(bad.errors.mileage, H.TEXT.mileageBad);
  assert.equal(bad.ok, false);
  assert.equal(bad.errors.source, undefined, "no source error on top of a bad number");
});

test("notes are limited to 800 characters after trimming", () => {
  assert.equal(H.validateOilChange(valid({ notes: "x".repeat(800) }), { today: TODAY }).ok, true);
  assert.equal(H.validateOilChange(valid({ notes: " " + "x".repeat(800) + " " }), { today: TODAY }).ok, true);
  assert.equal(H.validateOilChange(valid({ notes: "x".repeat(801) }), { today: TODAY }).errors.notes, H.ERRORS.notes);
  assert.equal(H.validateOilChange(valid({ notes: undefined }), { today: TODAY }).body.notes, "");
  assert.equal(H.notesCount("abc"), "3 / 800");
});

test("validateOilChange defaults today to the local date and tolerates no fields", () => {
  const v = H.validateOilChange(null);
  assert.equal(v.ok, false);
  assert.equal(v.errors.date, H.ERRORS.dateFormat);
  assert.equal(H.validateOilChange(valid({ date: H.localToday() })).ok, true);
});

// ---------- request id ----------
test("makeRequestId matches the backend pattern and the old oil_ + 32 hex shape", () => {
  const id = H.makeRequestId();
  assert.match(id, /^oil_[0-9a-f]{32}$/);
  assert.equal(H.isValidRequestId(id), true);
  const fixed = H.makeRequestId((a) => { a[0] = 1; a[1] = 0xffffffff; a[2] = 0xabc; a[3] = 0; return a; });
  assert.equal(fixed, "oil_00000001ffffffff00000abc00000000");
  assert.equal(H.isValidRequestId("short"), false);
  assert.equal(H.isValidRequestId("x".repeat(81)), false);
  assert.equal(H.isValidRequestId("has space here!"), false);
  assert.equal(H.isValidRequestId("a".repeat(12)), true);
});

test("nextPending reuses the id for identical content and renews it after an edit", () => {
  let n = 0;
  const make = () => "oil_" + String(++n).padStart(32, "0");
  const body = H.validateOilChange(valid(), { today: TODAY }).body;
  const first = H.nextPending(null, body, make);
  assert.equal(first.payload.request_id, "oil_" + "1".padStart(32, "0"));
  assert.deepEqual(Object.keys(first.payload), ["date", "mileage_mi", "mileage_source", "notes", "request_id"]);
  const retry = H.nextPending(first, { ...body }, make);
  assert.equal(retry, first, "same attempt, same request_id");
  const edited = H.nextPending(first, { ...body, notes: "Mobil 1" }, make);
  assert.notEqual(edited.payload.request_id, first.payload.request_id);
  assert.equal(edited.payload.notes, "Mobil 1");
  const broken = H.nextPending({ signature: first.signature, payload: { request_id: "bad" } }, body, make);
  assert.match(broken.payload.request_id, /^oil_\d{32}$/);
});

// ---------- responses ----------
test("classifySaveResponse maps the contract's statuses", () => {
  const record = { date: "2026-07-24", recorded_at: "x" };
  assert.deepEqual(H.classifySaveResponse(201, true, { available: true, record }), { kind: "ok", record });
  assert.deepEqual(H.classifySaveResponse(202, true, { available: false, pending: true }), { kind: "pending" });
  assert.deepEqual(H.classifySaveResponse(403, false, { available: false, detail: "Service records require a same-origin request" }),
    { kind: "error", message: "Service records require a same-origin request" });
  assert.deepEqual(H.classifySaveResponse(415, false, { detail: "Service records require JSON" }), { kind: "error", message: "Service records require JSON" });
  assert.deepEqual(H.classifySaveResponse(400, false, { detail: "Record ID already exists with different contents" }),
    { kind: "error", message: "Record ID already exists with different contents" });
  assert.deepEqual(H.classifySaveResponse(503, false, null), { kind: "error", message: "HTTP 503" });
  assert.deepEqual(H.classifySaveResponse(202, true, { pending: false }), { kind: "error", message: "HTTP 202" });
  assert.deepEqual(H.classifySaveResponse(201, true, { available: true }), { kind: "error", message: "HTTP 201" });
});

test("saveInit sends same-origin JSON without caching", () => {
  const init = H.saveInit({ a: 1 }, 0);
  assert.equal(init.method, "POST");
  assert.equal(init.cache, "no-store");
  assert.equal(init.credentials, "same-origin");
  assert.equal(init.mode, "same-origin");
  assert.deepEqual(init.headers, { "Content-Type": "application/json" });
  assert.equal(init.body, '{"a":1}');
  assert.equal(init.signal, undefined);
  assert.ok(H.saveInit({}, 1000).signal, "timeout signal when supported");
});

test("postOilChange posts once and returns the record on 201", async () => {
  const payload = { date: "2026-07-24", mileage_mi: 52000, mileage_source: "cluster_or_receipt", notes: "DIY", request_id: "oil_" + "a".repeat(32) };
  const record = { ...payload, recorded_at: "2026-09-23T00:00:00+00:00" };
  const f = fakeFetch([response(201, { available: true, record })]);
  assert.deepEqual(await H.postOilChange(payload, { fetch: f, sleep: noSleep, timeoutMs: 0 }), record);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].url, "/v1/maintenance/oil-changes");
  assert.deepEqual(JSON.parse(f.calls[0].init.body), payload);
});

test("postOilChange re-sends the identical body while the answer is 202 pending", async () => {
  const payload = { date: "2026-07-24", mileage_mi: null, mileage_source: "unknown", notes: "", request_id: "oil_" + "b".repeat(32) };
  const slept = [];
  const f = fakeFetch([response(202, { pending: true }), response(202, { pending: true }), response(201, { record: { ok: 1 } })]);
  const record = await H.postOilChange(payload, { fetch: f, sleep: async (ms) => { slept.push(ms); }, timeoutMs: 0 });
  assert.deepEqual(record, { ok: 1 });
  assert.equal(f.calls.length, 3);
  assert.deepEqual(slept, [200, 200]);
  assert.ok(f.calls.every((c) => c.url === f.calls[0].url && c.init.body === f.calls[0].init.body));
});

test("postOilChange gives up after 35 pending answers", async () => {
  const answers = Array.from({ length: 40 }, () => response(202, { pending: true }));
  const f = fakeFetch(answers);
  await assert.rejects(H.postOilChange({}, { fetch: f, sleep: noSleep, timeoutMs: 0 }), { message: H.TEXT.pendingExhausted });
  assert.equal(f.calls.length, 35);
});

test("postOilChange surfaces the server detail, or HTTP <status> for a non-JSON body", async () => {
  await assert.rejects(H.postOilChange({}, { fetch: fakeFetch([response(403, { detail: "Service records require a same-origin request" })]), sleep: noSleep, timeoutMs: 0 }),
    { message: "Service records require a same-origin request" });
  await assert.rejects(H.postOilChange({}, { fetch: fakeFetch([response(503, null, { json: false })]), sleep: noSleep, timeoutMs: 0 }),
    { message: "HTTP 503" });
});

test("postOilChange reports network errors and timeouts", async () => {
  await assert.rejects(H.postOilChange({}, { fetch: fakeFetch([new TypeError("Failed to fetch")]), sleep: noSleep, timeoutMs: 0 }),
    { message: "Failed to fetch" });
  const timeout = new Error("signal timed out");
  timeout.name = "TimeoutError";
  await assert.rejects(H.postOilChange({}, { fetch: fakeFetch([timeout]), sleep: noSleep, timeoutMs: 0 }), { message: H.TEXT.timeout });
});

test("saveOilChange refreshes /v1/maintenance after saving", async () => {
  const record = { date: "2026-09-23" };
  const f = fakeFetch([response(201, { record }), response(200, MAINTENANCE)]);
  const result = await H.saveOilChange({}, { fetch: f, sleep: noSleep, timeoutMs: 0 });
  assert.deepEqual(result, { record, maintenance: MAINTENANCE, message: H.TEXT.saved });
  assert.equal(f.calls[1].url, "/v1/maintenance");
  assert.equal(f.calls[1].init.cache, "no-store");
  assert.equal(f.calls[1].init.method, undefined);
});

test("saveOilChange keeps the saved record when the refresh fails", async () => {
  for (const second of [response(503, { detail: "x" }), new TypeError("offline"), response(200, null, { json: false }), response(200, [1])]) {
    const f = fakeFetch([response(201, { record: { id: 1 } }), second]);
    const result = await H.saveOilChange({}, { fetch: f, sleep: noSleep, timeoutMs: 0 });
    assert.deepEqual(result, { record: { id: 1 }, maintenance: null, message: "Record saved; reload the page to view it." });
  }
});

test("saveOilChange rejects without refreshing when the save fails", async () => {
  const f = fakeFetch([response(400, { detail: "Service date must be between 2000-01-01 and today" })]);
  await assert.rejects(H.saveOilChange({}, { fetch: f, sleep: noSleep, timeoutMs: 0 }), { message: "Service date must be between 2000-01-01 and today" });
  assert.equal(f.calls.length, 1);
});

test("old user-facing messages are kept verbatim", () => {
  assert.equal(H.TEXT.saving, "Saving…");
  assert.equal(H.TEXT.saved, "Oil change saved on the Pi. The vehicle’s oil-change indicator was not reset.");
  assert.ok(H.TEXT.note.startsWith("Saves a service record on the Pi for all your devices. Does not reset the vehicle’s oil-change indicator."));
});

// ---------- availability ----------
test("saveBlockReason and canSave follow available/persistent like the old button", () => {
  const ok = H.validateOilChange(valid(), { today: TODAY });
  assert.equal(H.saveBlockReason(MAINTENANCE), null);
  assert.equal(H.canSave(MAINTENANCE, false, ok), true);
  assert.equal(H.canSave(MAINTENANCE, true, ok), false, "while saving");
  assert.equal(H.canSave(MAINTENANCE, false, H.validateOilChange(valid({ date: "" }), { today: TODAY })), false, "until valid");
  assert.equal(H.saveBlockReason(null), H.TEXT.loading);
  assert.equal(H.saveBlockReason({ available: false, storage_error: "Maintenance storage could not be read: boom" }), "Maintenance storage could not be read: boom");
  assert.equal(H.saveBlockReason({ available: false, detail: "broker down" }), "broker down");
  assert.equal(H.saveBlockReason({ available: false }), H.TEXT.unavailable);
  assert.equal(H.saveBlockReason({ available: true, persistent: false }), H.TEXT.notPersistent);
  assert.equal(H.canSave({ available: true, persistent: false }, false, ok), false);
  assert.equal(H.storageNote({ available: true, persistent: true, storage_error: "Last-known mileage was not persisted: disk" }),
    "Last-known mileage was not persisted: disk");
  assert.equal(H.storageNote(MAINTENANCE), "");
});

// ---------- history ----------
test("historyRow matches the old `date · mileage · source · notes` line", () => {
  const row = H.historyRow(MAINTENANCE.oil_changes[0]);
  assert.equal(row.text, "Jul 24, 2026 · 52,000 mi · Cluster / service receipt · DIY");
  assert.equal(row.key, "oil_5277b50188954a3bae963e49edbe0100");
  assert.equal(row.meta, "52,000 mi · Cluster / service receipt");
  const bare = H.historyRow({ date: "2026-01-02", mileage_mi: null, mileage_source: "unknown", notes: "" }, 3);
  assert.equal(bare.text, "Jan 2, 2026 · Mileage not recorded");
  assert.equal(bare.key, "row-3");
  assert.equal(H.historyRow({ date: "2026-01-02", mileage_mi: 1000.5, mileage_source: "ics_estimate" }).text, "Jan 2, 2026 · 1,000.5 mi · ICS estimate*");
});

test("historyRows lists newest first and notes older records", () => {
  const { rows, more } = H.historyRows(MAINTENANCE);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].key, MAINTENANCE.oil_changes[0].request_id);
  assert.equal(more, "");
  const many = { record_count: 45, oil_changes: Array.from({ length: 25 }, (_, i) => ({ date: "2026-01-01", request_id: "oil_" + String(i).padStart(12, "0") })) };
  const out = H.historyRows(many);
  assert.equal(out.rows.length, 20);
  assert.equal(out.more, "Showing the newest 20 of 45 records.");
  assert.deepEqual(H.historyRows(null), { rows: [], more: "" });
  assert.deepEqual(H.historyRows({ oil_changes: [null, 5] }).rows, []);
});

test("lastOilChangeSummary mirrors the old last-oil-change line", () => {
  assert.deepEqual(H.lastOilChangeSummary(MAINTENANCE), { recorded: true, date: "Jul 24, 2026", mileage: "52,000 mi", source: "Cluster / service receipt", notes: "DIY" });
  assert.deepEqual(H.lastOilChangeSummary({ last_oil_change: null }), { recorded: false, date: "No service recorded", mileage: "—", source: "", notes: "" });
  const noMiles = H.lastOilChangeSummary({ last_oil_change: { date: "2026-07-24", mileage_mi: null, mileage_source: "unknown", notes: "" } });
  assert.equal(noMiles.mileage, "Mileage not recorded");
  assert.equal(noMiles.source, "");
});

// ---------- odometer and distance since service ----------
test("odometerSourceKind maps broker sources to record sources", () => {
  assert.equal(H.odometerSourceKind("ics.did.2001"), "ics_estimate");
  assert.equal(H.odometerSourceKind(undefined), "ics_estimate");
  assert.equal(H.odometerSourceKind("ICS_estimate"), "ics_estimate");
  assert.equal(H.odometerSourceKind("cluster.did.x"), "cluster_or_receipt");
  assert.equal(H.odometerSourceKind("bcm.odo"), null);
});

test("odometerLine shows truncated miles, the reading time and the ICS note", () => {
  const now = new Date("2026-09-22T12:00:00Z");
  const line = H.odometerLine(MAINTENANCE.last_known_odometer, now);
  assert.equal(line.value, "53,892 mi");
  assert.match(line.detail, /^Last reading /);
  assert.equal(line.note, H.TEXT.odometerNote);
  assert.equal(H.odometerLine({ ...MAINTENANCE.last_known_odometer, live: true }, now).detail, "Latest ICS reading");
  assert.deepEqual(H.odometerLine(null), { value: "—", detail: H.TEXT.noOdometer, note: H.TEXT.odometerNote });
});

test("milesSinceService computes a distance only when odometer and record share a source", () => {
  const odo = { value: 53892.766, unit: "mi", source: "ics.did.2001", observed_at: "2026-09-22T06:09:39Z" };
  const ics = { date: "2026-07-24", mileage_mi: 52000, mileage_source: "ics_estimate" };
  assert.deepEqual(H.milesSinceService(odo, ics), { kind: "distance", text: "1,892.8 estimated miles since service at last reading", miles: 53892.766 - 52000 });
  assert.equal(H.milesSinceService({ ...odo, live: true }, ics).text, "1,892.8 estimated miles since service");
  assert.equal(H.milesSinceService({ ...odo, source: undefined }, ics).kind, "distance", "vehicle.odometer is the ICS estimate");

  const cluster = { ...ics, mileage_source: "cluster_or_receipt" };
  assert.deepEqual(H.milesSinceService(odo, cluster), { kind: "across_sources", text: H.TEXT.acrossSources, miles: null });
  const clusterOdo = { ...odo, source: "cluster.odometer" };
  assert.equal(H.milesSinceService(clusterOdo, cluster).text, "1,892.8 miles since service at last reading");

  assert.deepEqual(H.milesSinceService(odo, { ...ics, mileage_mi: 60000 }),
    { kind: "negative", text: "Service mileage exceeds the latest ICS reading; check the entry.", miles: 53892.766 - 60000 });
  assert.equal(H.milesSinceService(clusterOdo, { ...cluster, mileage_mi: 60000 }).text, "Service mileage exceeds the latest odometer reading; check the entry.");
  assert.equal(H.milesSinceService(odo, { ...ics, mileage_mi: 53892.766 }).text, "0 estimated miles since service at last reading");
});

test("milesSinceService explains the missing pieces", () => {
  const ics = { date: "2026-07-24", mileage_mi: 52000, mileage_source: "ics_estimate" };
  assert.deepEqual(H.milesSinceService({ value: 1 }, null), { kind: "none", text: "", miles: null });
  assert.equal(H.milesSinceService(null, ics).kind, "no_odometer");
  assert.equal(H.milesSinceService({ value: NaN }, ics).kind, "no_odometer");
  assert.equal(H.milesSinceService(null, { ...ics, mileage_source: "cluster_or_receipt" }).kind, "across_sources");
  assert.equal(H.milesSinceService({ value: 5 }, { date: "2026-07-24", mileage_mi: null, mileage_source: "unknown" }).kind, "no_mileage");
  assert.equal(H.milesSinceService({ value: 5, unit: "km", source: "ics.did.2001" }, ics).kind, "across_sources");
});

test("newerMaintenance prefers the payload with the newer records", () => {
  const older = { record_count: 2, oil_changes: MAINTENANCE.oil_changes };
  const newer = { record_count: 3, oil_changes: [{ recorded_at: "2026-09-23T00:00:00+00:00" }, ...MAINTENANCE.oil_changes] };
  assert.equal(H.newerMaintenance(older, newer), newer);
  assert.equal(H.newerMaintenance(newer, older), newer);
  assert.equal(H.newerMaintenance(older, null), older);
  assert.equal(H.newerMaintenance(null, newer), newer);
  assert.equal(H.newerMaintenance(null, null), null);
  const sameCountNewer = { record_count: 2, oil_changes: [{ recorded_at: "2026-09-23T00:00:00+00:00" }] };
  assert.equal(H.newerMaintenance(older, sameCountNewer), sameCountNewer);
  const copy = { ...older };
  assert.equal(H.newerMaintenance(older, copy), older, "ties keep the prop");
});

test("effectiveOdometer falls back to the retained last_known_odometer like the old panel", () => {
  const retained = { value: 53892.766, unit: "mi", source: "ics.did.2001", observed_at: "2026-09-22T06:09:39.646302+00:00" };
  const maintenance = { available: true, last_known_odometer: retained };
  assert.equal(H.effectiveOdometer(null, maintenance), retained);
  assert.equal(H.effectiveOdometer({ value: null }, maintenance), retained);
  const live = { value: 53900, unit: "mi", source: "ics.did.2001", live: true };
  assert.equal(H.effectiveOdometer(live, maintenance), live);
  assert.equal(H.effectiveOdometer(null, null), null);
  assert.equal(H.effectiveOdometer(null, { last_known_odometer: { value: "x" } }), null);
  assert.equal(H.odometerLine(H.effectiveOdometer(null, maintenance)).value, "53,892 mi");
});
