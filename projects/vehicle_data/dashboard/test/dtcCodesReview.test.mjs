import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { codesView } from "../src/views/health.helpers.js";
import { toggleKey, clearKeys } from "../src/components/multiSelect.helpers.js";

const SUMMARY = JSON.parse(fs.readFileSync(new URL("./fixtures/summary-v2.json", import.meta.url), "utf8"));
const NOW = Date.parse("2026-09-22T19:00:00Z");
const DTCS = SUMMARY.dtcs;
const slice = (ignored) => ({ available: true, persistent: true, storage_error: null, limit: 500, ignored });
const view = (modules = [], ignores = null) => codesView(DTCS, NOW, false, { modules, ignores });
const group = (vm, key) => (key === "current" ? vm.current : vm.groups.find((g) => g.key === key));

const withPcmCode = () => ({
  ...DTCS,
  groups: { ...DTCS.groups, current: [...DTCS.groups.current, { ...DTCS.groups.current[0], module_key: "pcm", module_name: "Powertrain Control Module", fca_display: "P0300-00", raw_dtc: "P030000" }] },
  group_counts: { ...DTCS.group_counts, current: DTCS.group_counts.current + 1 },
  group_returned_counts: { ...DTCS.group_returned_counts, current: DTCS.group_returned_counts.current + 1 },
});

test("All is the enduring no-filter state: a module that gains codes later stays visible", () => {
  assert.deepEqual(clearKeys(), [], "the All button stores no keys, not today's keys");
  const options = view().filter.options;
  let chosen = [];
  for (const o of options) chosen = toggleKey(chosen, o.key, options);
  assert.deepEqual(chosen, [], "every module chosen one by one is no filter too");
  const later = codesView(withPcmCode(), NOW, false, { modules: chosen });
  assert.equal(later.badge.text, "6 current");
  assert.equal(group(later, "current").total, 6, "the list agrees with the badge");
  assert.ok(later.filter.options.some((o) => o.key === "pcm"));
});

test("a real subset is unaffected by a module that gains codes", () => {
  const later = codesView(withPcmCode(), NOW, false, { modules: ["bcm_ccan", "radar_acc"] });
  assert.equal(group(later, "current").total, 5);
});

test("a module filter never reads as proven absence while records were not returned", () => {
  const vm = view(["radar_acc"]);
  const history = group(vm, "confirmed_history");
  assert.equal(history.total, 0);
  assert.match(history.moreText, /13 more were not returned/);
  // A module that exists only through an ignore entry has no returned codes in any group.
  const ghost = slice([{ module_key: "pcm", fca_display: "P0300-00", note: "", ignored_at: null }]);
  const none = view(["pcm"], ghost);
  assert.equal(none.groups.length > 0, true, "the groups and their qualifications survive an all-empty filtered view");
  assert.ok(none.groups.some((g) => g.moreText));
  assert.match(none.emptyText, /not proven/);
});

test("the ignored-codes list that could not be read is explained and its controls stay hidden", () => {
  const vm = view([], { available: false, storage_error: "ignore file is corrupt", ignored: [] });
  assert.equal(vm.ignoreAvailable, false);
  assert.equal(vm.ignored, null);
  assert.match(vm.ignoreNote, /could not be read on the Pi.*shown again/);
  assert.equal(view([], { available: false }).ignoreNote, null);
  assert.equal(view([], slice([])).ignoreNote, null);
});
