import { test } from "node:test";
import assert from "node:assert/strict";
import {
  allKeys, chipsOf, clearKeys, initialActive, listboxKeyAction, moveActive, startsGroup, toggleKey, triggerText, validKeys,
} from "../src/components/multiSelect.helpers.js";

const OPTIONS = [
  { key: "a", label: "Alpha", count: 3 },
  { key: "b", label: "Bravo", count: 1 },
  { key: "c", label: "Charlie", count: 7 },
];

test("toggling adds and removes a key and keeps option order", () => {
  assert.deepEqual(toggleKey([], "c", OPTIONS), ["c"]);
  assert.deepEqual(toggleKey(["c"], "a", OPTIONS), ["a", "c"]);
  assert.deepEqual(toggleKey(["a", "c"], "a", OPTIONS), ["c"]);
  assert.deepEqual(toggleKey(["gone"], "b", OPTIONS), ["b"], "keys that are no longer options are dropped");
});

test("All and Clear", () => {
  assert.deepEqual(allKeys(OPTIONS), ["a", "b", "c"]);
  assert.deepEqual(clearKeys(), []);
  assert.deepEqual(validKeys(["c", "x", "a"], OPTIONS), ["a", "c"]);
});

test("the button text says all, the one name or the number", () => {
  assert.equal(triggerText([], OPTIONS), "All modules");
  assert.equal(triggerText(["a", "b", "c"], OPTIONS), "All modules", "every option chosen is no filter");
  assert.equal(triggerText(["b"], OPTIONS), "Bravo");
  assert.equal(triggerText(["a", "c"], OPTIONS), "2 modules");
  assert.equal(triggerText(["x"], OPTIONS), "All modules", "a stale key counts for nothing");
  assert.equal(triggerText(["a", "c"], OPTIONS, "model", "Any model"), "2 models");
  assert.equal(triggerText([], OPTIONS, "model", "Any model"), "Any model");
});

test("chips show the chosen options only while the filter narrows", () => {
  assert.deepEqual(chipsOf(["c", "a"], OPTIONS), [{ key: "a", label: "Alpha", text: "Alpha" }, { key: "c", label: "Charlie", text: "Charlie" }]);
  assert.deepEqual(chipsOf([], OPTIONS), []);
  assert.deepEqual(chipsOf(["a", "b", "c"], OPTIONS), []);
});

test("keyboard: arrows, Home, End move and stop at the ends; Space and Enter choose", () => {
  assert.equal(listboxKeyAction("ArrowDown"), "next");
  assert.equal(listboxKeyAction("ArrowUp"), "prev");
  assert.equal(listboxKeyAction("Home"), "first");
  assert.equal(listboxKeyAction("End"), "last");
  assert.equal(listboxKeyAction(" "), "toggle");
  assert.equal(listboxKeyAction("Enter"), "toggle");
  assert.equal(listboxKeyAction("Tab"), null, "Tab and Escape are not the list's");
  assert.equal(listboxKeyAction("Escape"), null);
  assert.equal(moveActive(0, "next", 3), 1);
  assert.equal(moveActive(2, "next", 3), 2);
  assert.equal(moveActive(0, "prev", 3), 0);
  assert.equal(moveActive(1, "last", 3), 2);
  assert.equal(moveActive(2, "first", 3), 0);
  assert.equal(moveActive(5, "prev", 3), 1, "an out-of-range index is clamped first");
  assert.equal(moveActive(0, "next", 0), -1);
});

test("the list opens on the first chosen option, else the first", () => {
  assert.equal(initialActive([], OPTIONS), 0);
  assert.equal(initialActive(["c", "b"], OPTIONS), 1);
  assert.equal(initialActive([], []), -1);
});

test("a group heading starts where the group does", () => {
  const grouped = [
    { key: "1", label: "One", group: "Claude" },
    { key: "2", label: "Two", group: "Claude" },
    { key: "3", label: "Three", group: "GPT" },
    { key: "4", label: "Four" },
  ];
  assert.deepEqual(grouped.map((_, i) => startsGroup(grouped, i)), [true, false, true, false]);
});

test("chips drop a trailing parenthetical, unless two chosen names would read alike", () => {
  const options = [
    { key: "a", label: "Body Control Module (C-CAN diagnostic endpoint)" },
    { key: "b", label: "Marelli Instrument Panel Cluster (IPC)" },
    { key: "c", label: "Radio (RFH)" },
    { key: "d", label: "Radio (RFH2)" },
    { key: "e", label: "(only parenthetical)" },
  ];
  const chips = chipsOf(["a", "b", "c"], options);
  assert.deepEqual(chips.map((c) => c.text), ["Body Control Module", "Marelli Instrument Panel Cluster", "Radio"]);
  assert.equal(chips[0].label, options[0].label, "the full name stays for the title and aria-label");
  const same = chipsOf(["c", "d"], options);
  assert.deepEqual(same.map((c) => c.text), ["Radio (RFH)", "Radio (RFH2)"]);
  assert.equal(chipsOf(["e", "a"], options)[1].text, "(only parenthetical)");
});
