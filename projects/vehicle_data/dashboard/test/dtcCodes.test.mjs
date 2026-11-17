import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { codesView } from "../src/views/health.helpers.js";
import { activeModules, filterByModules, moduleOptions } from "../src/views/dtcCodes.helpers.js";
import { excludeIgnored, ignoreState } from "../src/dtcIgnore.helpers.js";

const SUMMARY = JSON.parse(fs.readFileSync(new URL("./fixtures/summary-v2.json", import.meta.url), "utf8"));
const NOW = Date.parse("2026-09-22T19:00:00Z");
const DTCS = SUMMARY.dtcs;

const slice = (ignored) => ({ available: true, persistent: true, storage_error: null, limit: 500, ignored });
const LED = [
  { module_key: "bcm_ccan", fca_display: "B162A-15", note: "LED headlights", ignored_at: "2026-11-16T17:00:00Z" },
  { module_key: "bcm_ccan", fca_display: "B1632-15", note: "LED headlights", ignored_at: "2026-11-16T17:01:00Z" },
];
const view = (modules = [], ignores = null) => codesView(DTCS, NOW, false, { modules, ignores });
const group = (vm, key) => (key === "current" ? vm.current : vm.groups.find((g) => g.key === key));

test("with no ignores and no filter the card is what it was", () => {
  const plain = codesView(DTCS, NOW, false);
  assert.deepEqual(view(), plain);
  assert.equal(plain.ignored, null);
  assert.equal(plain.ignoreAvailable, false);
  assert.equal(plain.badge.text, "5 current");
  assert.equal(group(plain, "current").total, 5);
});

test("module options list the modules that have codes, with counts, by name", () => {
  const { options } = view().filter;
  const byKey = Object.fromEntries(options.map((o) => [o.key, o.count]));
  assert.equal(byKey.bcm_ccan, 10, "5 current plus 5 confirmed history");
  assert.equal(byKey.radar_acc, 7);
  assert.equal(byKey.telematics, 22);
  assert.equal(byKey.pcm, undefined, "a module with no codes is not listed");
  assert.deepEqual(options.map((o) => o.label), [...options.map((o) => o.label)].sort((a, b) => a.localeCompare(b)));
});

test("filtering by modules narrows every group and its count", () => {
  const vm = view(["bcm_ccan", "radar_acc"]);
  assert.equal(group(vm, "current").total, 5);
  assert.equal(group(vm, "incomplete_only").total, 7, "only radar_acc codes of that group");
  assert.equal(group(vm, "confirmed_history").total, 5);
  assert.ok(vm.groups.every((g) => g.rows.concat(g.older).every((r) => ["bcm_ccan", "radar_acc"].includes(r.moduleKey))));
  assert.deepEqual([...vm.filter.chosen].sort(), ["bcm_ccan", "radar_acc"]);
  assert.equal(vm.badge.text, "5 current", "the headline does not follow the view filter");
  assert.match(group(vm, "confirmed_history").moreText, /not returned and not filtered/, "a truncated group says its filter covers only the returned codes");
  assert.equal(group(vm, "current").moreText, null, "a complete group has no such note");
});

test("a filter that matches nothing says so; choosing every module is no filter", () => {
  const empty = view(["pcm"]);
  assert.equal(empty.filter.chosen.length, 0, "a chosen module with no codes is dropped");
  const modules = view().filter.options.map((o) => o.key);
  assert.deepEqual(view(modules), view());
  const options = moduleOptions(DTCS, []);
  assert.deepEqual(activeModules(["bcm_ccan", "gone"], options), ["bcm_ccan"]);
  const only = filterByModules(DTCS, ["emcm2_bcan"]);
  assert.equal(only.group_counts.current, 0);
  assert.equal(only.group_counts.confirmed_history, 1);
});

test("ignored codes leave their group, the counts and the badge, and fill the Ignored group", () => {
  const vm = view([], slice(LED));
  assert.equal(vm.ignoreAvailable, true);
  assert.equal(group(vm, "current").total, 3);
  assert.equal(vm.badge.text, "3 current");
  assert.ok(!group(vm, "current").rows.some((r) => /B162A|B1632/.test(r.code)));
  assert.equal(vm.ignored.summary, "Ignored (2)");
  const row = vm.ignored.rows.find((r) => r.code === "B162A-15");
  assert.equal(row.note, "LED headlights");
  assert.match(row.meaning, /Left low-beam circuit/);
  assert.match(row.meta, /^Body Control Module .* · Ignored (Nov 16, )?\d{1,2}:\d{2} [ap]m$/);
  assert.equal(row.ignored, true);
  assert.equal(row.fca, "B162A-15");
  assert.equal(row.moduleKey, "bcm_ccan");
  assert.equal(vm.filter.options.find((o) => o.key === "bcm_ccan").count, 8, "ignored codes are not counted in the module's count");
  assert.equal(vm.filter.options.find((o) => o.key === "bcm_ccan").detail, "2 ignored");
});

test("the module filter applies to the Ignored group too", () => {
  const ignores = slice([...LED, { module_key: "radar_acc", fca_display: "C1234-00", note: "", ignored_at: null }]);
  assert.equal(view([], ignores).ignored.total, 3);
  const only = view(["radar_acc"], ignores);
  assert.equal(only.ignored.total, 1);
  assert.equal(only.ignored.rows[0].meaning, "not in the saved list now");
  assert.equal(only.ignored.rows[0].meta.endsWith("Ignored"), true, "no date reads plainly");
});

test("no ignore slice or an unavailable one: no Ignored group and ignored codes stay in their groups", () => {
  for (const ignores of [null, undefined, { available: false }]) {
    const vm = view([], ignores);
    assert.equal(vm.ignored, null);
    assert.equal(vm.ignoreAvailable, false);
    assert.equal(group(vm, "current").total, 5);
  }
  assert.equal(view([], slice([])).ignored, null, "an empty list shows no group");
  assert.equal(view([], slice([])).ignoreAvailable, true);
});

test("when every saved code is ignored the card says so instead of claiming a clean read", () => {
  const groups = { current: DTCS.groups.current };
  const whole = { ...DTCS, groups, group_counts: { current: 5 }, group_returned_counts: { current: 5 }, groups_truncated: false };
  const everything = slice(groups.current.map((r) => ({ module_key: r.module_key, fca_display: r.fca_display, note: "", ignored_at: null })));
  const vm = codesView(whole, NOW, false, { ignores: everything });
  assert.equal(vm.emptyText, "No codes besides the ignored ones.");
  assert.equal(vm.current, null);
  assert.ok(vm.ignored.total >= 1);
  assert.match(vm.badge.text, /None current/);
});

test("hidden parked counts keep their wording with ignores, and a filter does not guess them", () => {
  const hiddenInput = {
    ...DTCS,
    in_vehicle_scan: { available: true, modules: [{ module_key: "bcm_ccan", observed_at: "2026-09-22T18:00:00Z", status_availability_mask: "0D", dtcs: [], dtc_count: 0 }] },
  };
  const plain = codesView(hiddenInput, NOW, false);
  const withIgnores = codesView(hiddenInput, NOW, false, { ignores: slice([{ module_key: "telematics", fca_display: "U1934-87", note: "", ignored_at: null }]) });
  const history = (vm) => vm.groups.find((g) => g.key === "confirmed_history");
  assert.match(history(plain).summary, /shown · \+\d+ more in the parked read/);
  assert.match(history(withIgnores).summary, /shown · \+\d+ more in the parked read/);
  assert.equal(history(withIgnores).returned, history(plain).returned - 1);
  const filtered = codesView(hiddenInput, NOW, false, { modules: ["telematics"] });
  assert.doesNotMatch(history(filtered).summary, /more in the parked read/);
  assert.match(history(filtered).moreText, /not returned and not filtered/);
});

test("the row model carries what the open panel needs", () => {
  const row = group(view(), "current").rows[0];
  assert.equal(row.moduleKey, "bcm_ccan");
  assert.equal(row.fca, row.code);
  assert.ok(row.id);
  assert.equal(row.extra, null, "the secondary line is empty until something supplies one");
  const labels = Object.fromEntries(row.details);
  assert.match(labels.Module, /^Body Control Module/);
  assert.match(labels["Last seen"], /\d{1,2}:\d{2} [ap]m$/, "the same clock format as the closed row");
  assert.equal(row.meta.includes("last seen " + labels["Last seen"]), true, "the row and the panel show one instant the same way");
  assert.match(labels.Status, /Current/);
  const bare = codesView({ ...DTCS, groups: { ...DTCS.groups, current: [{ module_key: "x", description: "d" }] }, group_counts: { ...DTCS.group_counts, current: 1 } }, NOW, false);
  assert.equal(bare.current.rows[0].id, null, "a record without an FCA code cannot be ignored");
});

test("excludeIgnored and the view agree on what an ignored record is", () => {
  const { found } = excludeIgnored(DTCS, ignoreState(slice(LED)).index);
  assert.equal(found.size, 2);
});
