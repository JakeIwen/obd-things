import test from "node:test";
import assert from "node:assert/strict";
import { mergeVanScan, filteredTime } from "../src/views/dtcSources.helpers.js";
import { codesView } from "../src/views/health.helpers.js";

const T0 = "2026-11-15T16:00:00Z";
const T1 = "2026-11-15T17:00:00Z";
const T2 = "2026-11-15T18:00:00Z";
const T3 = "2026-11-15T19:00:00Z";
const NOW = Date.parse(T3);
const record = (code = "541878", status = "08", at = T0) => ({
  module_key: "radar_acc", raw_dtc: code, status, fca_display: code,
  display_group: "confirmed_history", last_seen_at: at,
  observation_state: "retained_incompatible_status_mask",
});
const read = (at, dtcs = [], mask = "FF", outcome = "complete") => ({
  observed_at: at, dtcs, status_availability_mask: mask, outcome, count: dtcs.length,
});
function input({ records = [record()], van, drive = [], total = records.length } = {}) {
  return { available: true, modules: [{ module_key: "radar_acc", module_name: "Radar",
    availability: "unavailable", last_attempt_at: T3, last_success_at: T2,
    result_state: "unavailable", absence_authoritative: false }],
    groups: { confirmed_history: records }, group_counts: { confirmed_history: total },
    group_returned_counts: { confirmed_history: records.length }, groups_truncated: total > records.length,
    coverage: { total_modules: 1, available_modules: 0 },
    in_vehicle_scan: van ? { available: true, modules: [{ module_key: "radar_acc", ...van, dtc_count: van.dtcs.length }] } : undefined,
    drive_reads: { available: true, modules: [{ module_key: "radar_acc", reads: drive }] } };
}
const all = (d) => Object.values(d.groups).flat();

test("effective coverage intersects request and availability; null and zero never clear", () => {
  for (const mask of [null, "00", "40"]) {
    const merged = mergeVanScan(input({ van: read(T1, [], mask), drive: [read(T2, [], mask)] }));
    assert.equal(merged.groups.confirmed_history.length, 1);
    assert.equal(all(merged)[0].last_seen_at, T0);
    const meta = codesView(input({ van: read(T1, [], mask), drive: [read(T2, [], mask)] }), NOW, false).modules.rows[0].meta;
    assert.match(meta, /no returned status coverage; older codes cannot be ruled out/);
  }
  assert.match(codesView(input({ drive: [read(T1, [], "08")] }), NOW, false).modules.rows[0].meta,
    /limited returned status coverage/);
  const merged = mergeVanScan(input({ drive: [read(T1, [record("541878", "01")], null)] }));
  assert.equal(all(merged)[0].status, "01", "listed codes update even without absence coverage");
  assert.equal(all(merged)[0].last_seen_at, T1);
});

test("newer failed drive/parked attempts remain separate from older complete success", () => {
  const failure = { ...read(T3), outcome: "no_response", count: null, reason: "timeout" };
  const d = input({ drive: [failure, read(T1, [record("541878", "01")])] });
  const merged = mergeVanScan(d);
  assert.equal(all(merged)[0].status, "01");
  assert.equal(merged.modules[0].drive_read.observed_at, T1);
  for (const field of Object.keys(d.modules[0])) assert.deepEqual(merged.modules[0][field], d.modules[0][field]);
  const meta = codesView(d, NOW, false).modules.rows[0].meta;
  assert.match(meta, /parked read failed/);
  assert.match(meta, /drive read failed \(timeout\).*MST/);
  assert.match(meta, /1 code reported/);
});

test("chronological latest complete and van success win in either source order", () => {
  for (const [vanAt, driveAt, wanted] of [[T1, T2, "04"], [T2, T1, "01"]]) {
    const merged = mergeVanScan(input({ van: read(vanAt, [record("541878", "01")]),
      drive: [read(T0, [record("541878", "80")]), read(driveAt, [record("541878", "04")])] }));
    assert.equal(all(merged)[0].status, wanted);
    assert.equal(all(merged)[0].last_seen_at, T2);
  }
});

test("base record own date, not module success, governs updates; newer record never regresses", () => {
  const merged = mergeVanScan(input({ records: [record(), record("AAAAAA", "08", T3)],
    van: read(T1, [record("541878", "04"), record("AAAAAA", "01")]) }));
  assert.equal(all(merged).find((r) => r.raw_dtc === "541878").last_seen_at, T1);
  assert.equal(all(merged).find((r) => r.raw_dtc === "AAAAAA").last_seen_at, T3);
  const retained = mergeVanScan(input({ records: [record("AAAAAA", "40")], drive: [read(T2)] }));
  assert.equal(all(retained)[0].status, "40");
  assert.equal(all(retained)[0].last_seen_at, T0);
  assert.equal(all(retained)[0].observation_state, "retained_incompatible_status_mask");
});

test("covered missing records become explicitly not reported, never vanished or shown current", () => {
  const d = input({ records: [{ ...record("AAAAAA", "01"), display_group: "current" }], van: read(T1) });
  const merged = mergeVanScan(d);
  assert.equal(all(merged).length, 1);
  assert.equal(merged.groups.not_reported[0].not_reported_source, "van");
  assert.equal(merged.groups.not_reported[0].not_reported_at, T1);
  assert.equal(merged.groups.not_reported[0].last_seen_at, T0);
  const vm = codesView(d, NOW, false);
  assert.equal(vm.current.total, 0);
  assert.match(vm.groups.find((g) => g.key === "not_reported").rows[0].meta, /not reported by the van read.*MST/);
});

test("truncated latest newer than complete changes no code; mismatch counts only covered shown records", () => {
  const partial = { ...read(T2, [], "08", "truncated"), count: 2, first_code: { fca_display: "C1418-78" } };
  const d = input({ records: [record(), record("AAAAAA", "40")], total: 30,
    drive: [partial, read(T1, [record()])] });
  const merged = mergeVanScan(d);
  assert.equal(all(merged).length, 2);
  assert.equal(all(merged).find((r) => r.raw_dtc === "541878").last_seen_at, T1);
  assert.equal(merged.modules[0].drive_truncated.count_mismatch, true);
  const meta = codesView(d, NOW, false).modules.rows[0].meta;
  assert.match(meta, /2 codes reported, list not retrieved; first C1418-78/);
  assert.match(meta, /count differs from the codes shown/);
  assert.match(meta, /Nov 15, 2026.*MST/);
  assert.equal(mergeVanScan(input({ drive: [partial, read(T3)] })).modules[0].drive_truncated, undefined);
  const matched = { ...partial, count: 1 };
  const notReported = mergeVanScan(input({ drive: [read(T1), matched] }));
  assert.equal(notReported.modules[0].drive_truncated.count_mismatch, false,
    "not-reported records still shown with a covered old status count as shown");
  assert.equal(mergeVanScan(input({ records: [record(), record("AAAAAA", "40")], drive: [matched] })).modules[0].drive_truncated.count_mismatch, false);
});

test("empty filtered reads never claim no DTCs or mutate parked result fields", () => {
  for (const source of ["van", "drive"]) {
    const d = input({ records: [], van: source === "van" ? read(T1) : undefined,
      drive: source === "drive" ? [read(T1)] : [] });
    const merged = mergeVanScan(d);
    assert.equal(merged.modules[0].result_state, "unavailable");
    assert.equal(merged.modules[0].absence_authoritative, false);
    const vm = codesView(d, NOW, false);
    assert.match(vm.modules.rows[0].meta, source === "van" ? /no failed, pending or confirmed codes/ : /no failed, pending, confirmed or warning-lamp codes/);
    assert.doesNotMatch(vm.modules.rows[0].meta, /no DTCs|no codes reported/);
    assert.notEqual(vm.emptyText, "No codes in the last read of every module.");
  }
});

test("parked truncation retains unreturned counts while only returned records move", () => {
  const d = input({ total: 30, drive: [read(T1)] });
  const merged = mergeVanScan(d);
  assert.equal(merged.groups_truncated, true);
  assert.equal(merged.group_counts.confirmed_history, 29);
  assert.equal(merged.group_counts.not_reported, 1);
  assert.match(codesView(d, NOW, false).groups.find((g) => g.key === "confirmed_history").moreText, /\+29 more from the parked read/);
  assert.equal(codesView(d, NOW, false).groups.find((g) => g.key === "confirmed_history").summary,
    "Confirmed history · 29 · showing 0 of 29");
});

test("Mountain dates include explicit source, coverage, date and seasonal zone", () => {
  assert.match(filteredTime(T1), /Nov 15, 2026.*10:00 AM MST/);
  assert.match(filteredTime("2026-07-01T16:00:00Z"), /Jul 1, 2026.*10:00 AM MDT/);
  const vm = codesView(input({ drive: [read(T1, [record()])] }), NOW, false);
  assert.match(vm.modules.rows[0].meta, /from the Pi’s drive read.*codes only.*Nov 15, 2026.*MST/);
});

test("optional absent and unavailable drive blocks render exactly the existing parked model", () => {
  const d = input();
  delete d.drive_reads;
  const expected = codesView(d, NOW, false);
  assert.deepEqual(codesView({ ...d, drive_reads: { available: false } }, NOW, false), expected);
  assert.deepEqual(codesView({ ...d, drive_reads: { available: true, modules: [] } }, NOW, false), expected);
});
