import { test } from "node:test";
import assert from "node:assert/strict";
import * as H from "../src/dialogs/Customizer.helpers.js";

const CARDS = [
  { id: "battery", label: "Battery" },
  { id: "tires", label: "Tires" },
  { id: "trip", label: "Last trip" },
  { id: "service", label: "Service" },
];

function base(extra = {}) {
  return { version: 1, view: "parked", auto: true, hidden: {}, order: {}, timeFormat: "12h", ...extra };
}

const ids = (rows) => rows.map((row) => row.id);

test("viewTitle names known views and falls back for unknown ones", () => {
  assert.equal(H.viewTitle("drive"), "Customise Drive");
  assert.equal(H.viewTitle("system"), "Customise System");
  assert.equal(H.viewTitle("mystery"), "Customise mystery");
  assert.equal(H.viewTitle(undefined), "Customise view");
});

test("cardList drops invalid and duplicate cards and defaults the label to the id", () => {
  const list = H.cardList([{ id: "a", label: "A" }, { id: "a", label: "dup" }, null, { label: "x" }, { id: "" }, { id: "b" }, { id: "c", label: 0 }]);
  assert.deepEqual(list, [{ id: "a", label: "A" }, { id: "b", label: "b" }, { id: "c", label: "0" }]);
  assert.deepEqual(H.cardList(null), []);
});

test("orderedCards uses the default order when nothing is saved", () => {
  const rows = H.orderedCards("parked", CARDS, base());
  assert.deepEqual(ids(rows), ["battery", "tires", "trip", "service"]);
  assert.ok(rows.every((row) => row.hidden === false));
  assert.equal(rows[2].label, "Last trip");
});

test("orderedCards applies the saved order, skips unknown ids and appends new cards", () => {
  const settings = base({ order: { parked: ["service", "gone", "battery", "service"] }, hidden: { parked: ["trip"] } });
  const rows = H.orderedCards("parked", CARDS, settings);
  assert.deepEqual(ids(rows), ["service", "battery", "tires", "trip"]);
  assert.deepEqual(rows.map((row) => row.hidden), [false, false, false, true]);
});

test("orderedCards tolerates missing or malformed settings", () => {
  for (const settings of [null, undefined, {}, { hidden: [], order: "x" }, { hidden: { parked: "trip" } }]) {
    assert.deepEqual(ids(H.orderedCards("parked", CARDS, settings)), ["battery", "tires", "trip", "service"]);
  }
});

test("orderedCards keeps views separate", () => {
  const settings = base({ order: { health: ["service", "battery"] }, hidden: { health: ["tires"] } });
  assert.deepEqual(ids(H.orderedCards("parked", CARDS, settings)), ["battery", "tires", "trip", "service"]);
  assert.deepEqual(H.visibleIds("health", CARDS, settings), ["service", "battery", "trip"]);
});

test("visibleIds removes hidden cards and keeps order", () => {
  const settings = base({ order: { parked: ["trip", "battery"] }, hidden: { parked: ["battery"] } });
  assert.deepEqual(H.visibleIds("parked", CARDS, settings), ["trip", "tires", "service"]);
});

test("toggleHidden hides, shows and toggles without mutating the input", () => {
  const settings = base({ hidden: { drive: ["x"] } });
  const frozen = JSON.stringify(settings);
  const hidden = H.toggleHidden("parked", settings, "tires", true);
  assert.deepEqual(hidden.hidden, { drive: ["x"], parked: ["tires"] });
  assert.equal(JSON.stringify(settings), frozen, "input untouched");
  assert.equal(hidden.view, "parked", "other keys kept");
  assert.equal(hidden.timeFormat, "12h");

  const toggledBack = H.toggleHidden("parked", hidden, "tires");
  assert.deepEqual(toggledBack.hidden, { drive: ["x"] }, "empty list removed");
  const shown = H.toggleHidden("parked", hidden, "tires", false);
  assert.deepEqual(shown.hidden, { drive: ["x"] });
  const toggledOn = H.toggleHidden("parked", settings, "trip");
  assert.deepEqual(toggledOn.hidden.parked, ["trip"]);
});

test("toggleHidden returns the same object for a no-op", () => {
  const settings = base({ hidden: { parked: ["tires"] } });
  assert.equal(H.toggleHidden("parked", settings, "tires", true), settings);
  assert.equal(H.toggleHidden("parked", settings, "battery", false), settings);
  assert.equal(H.toggleHidden("parked", settings, "", true), settings);
  assert.equal(H.toggleHidden("parked", settings, null), settings);
});

test("toggleHidden works from empty or missing settings", () => {
  assert.deepEqual(H.toggleHidden("drive", undefined, "rpm", true), { hidden: { drive: ["rpm"] } });
  assert.deepEqual(H.toggleHidden("drive", { hidden: null }, "rpm").hidden, { drive: ["rpm"] });
});

test("showAll clears only the view's hidden list", () => {
  const settings = base({ hidden: { parked: ["tires", "trip"], drive: ["rpm"] }, order: { parked: ["trip"] } });
  const next = H.showAll("parked", settings);
  assert.deepEqual(next.hidden, { drive: ["rpm"] });
  assert.deepEqual(next.order, { parked: ["trip"] });
  assert.equal(H.showAll("health", settings), settings);
});

test("move swaps with the neighbour and stores the full order", () => {
  const settings = base();
  const down = H.move("parked", CARDS, settings, "battery", 1);
  assert.deepEqual(down.order.parked, ["tires", "battery", "trip", "service"]);
  const up = H.move("parked", CARDS, down, "service", -1);
  assert.deepEqual(up.order.parked, ["tires", "battery", "service", "trip"]);
  assert.deepEqual(settings.order, {}, "input untouched");
  assert.deepEqual(H.moveDown("parked", CARDS, settings, "battery"), down);
  assert.deepEqual(H.moveUp("parked", CARDS, down, "service"), up);
});

test("move skips over hidden cards so they keep their slot", () => {
  const settings = base({ hidden: { parked: ["tires", "trip"] } });
  const next = H.move("parked", CARDS, settings, "battery", 1);
  assert.deepEqual(next.order.parked, ["service", "tires", "trip", "battery"]);
  assert.deepEqual(H.visibleIds("parked", CARDS, next), ["service", "battery"]);
  assert.deepEqual(next.hidden, settings.hidden);
});

test("move is a no-op at the ends, for hidden or unknown cards", () => {
  const settings = base({ hidden: { parked: ["trip"] } });
  assert.equal(H.move("parked", CARDS, settings, "battery", -1), settings);
  assert.equal(H.move("parked", CARDS, settings, "service", 1), settings);
  assert.equal(H.move("parked", CARDS, settings, "trip", -1), settings);
  assert.equal(H.move("parked", CARDS, settings, "nope", 1), settings);
});

test("canMove reports visible neighbours only", () => {
  const rows = H.orderedCards("parked", CARDS, base({ hidden: { parked: ["battery"] } }));
  assert.equal(H.canMove(rows, "tires", -1), false, "only a hidden card above");
  assert.equal(H.canMove(rows, "tires", 1), true);
  assert.equal(H.canMove(rows, "service", 1), false);
  assert.equal(H.canMove(rows, "battery", 1), false, "hidden cards never move");
  assert.equal(H.canMove(rows, "missing", 1), false);
});

test("reset forgets hidden and order for one view only", () => {
  const settings = base({ hidden: { parked: ["tires"], drive: ["rpm"] }, order: { parked: ["trip"], health: ["x"] } });
  const next = H.reset("parked", settings);
  assert.deepEqual(next.hidden, { drive: ["rpm"] });
  assert.deepEqual(next.order, { health: ["x"] });
  assert.equal(next.view, "parked");
  assert.equal(H.reset("history", settings), settings, "already default");
  const onlyOrder = H.reset("health", settings);
  assert.deepEqual(onlyOrder.order, { parked: ["trip"] });
  assert.equal(onlyOrder.hidden, settings.hidden, "untouched table reused");
});

test("isCustomised detects hidden cards and a changed effective order", () => {
  assert.equal(H.isCustomised("parked", CARDS, base()), false);
  assert.equal(H.isCustomised("parked", CARDS, base({ hidden: { parked: ["trip"] } })), true);
  assert.equal(H.isCustomised("parked", CARDS, base({ order: { parked: ["tires"] } })), true);
  // A saved order that equals the default (or names only unknown cards) is not a customisation.
  assert.equal(H.isCustomised("parked", CARDS, base({ order: { parked: ["battery", "tires"] } })), false);
  assert.equal(H.isCustomised("parked", CARDS, base({ order: { parked: ["gone"] }, hidden: { parked: ["gone"] } })), false);
});

test("editorModel precomputes rows, move affordances and notes", () => {
  const settings = base({ hidden: { parked: ["trip"] }, order: { parked: ["tires"] } });
  const model = H.editorModel("parked", CARDS, settings);
  assert.equal(model.title, "Customise Parked");
  assert.equal(model.customised, true);
  assert.equal(model.note, H.TEXT.noteCustom);
  assert.equal(model.empty, false);
  assert.deepEqual(model.visible, [
    { id: "tires", label: "Tires", canUp: false, canDown: true },
    { id: "battery", label: "Battery", canUp: true, canDown: true },
    { id: "service", label: "Service", canUp: true, canDown: false },
  ]);
  assert.deepEqual(model.hidden, [{ id: "trip", label: "Last trip" }]);

  const plain = H.editorModel("drive", CARDS, base());
  assert.equal(plain.customised, false);
  assert.equal(plain.note, H.TEXT.noteDefault);
  assert.deepEqual(plain.hidden, []);

  const none = H.editorModel("system", [], base());
  assert.equal(none.empty, true);
  assert.deepEqual(none.visible, []);
});

test("editorModel in fixed-order mode (Drive) offers show/hide only and ignores a saved order", () => {
  const settings = base({ hidden: { drive: ["trip"] }, order: { drive: ["tires", "service"] } });
  const model = H.editorModel("drive", CARDS, settings, true);
  assert.equal(model.fixedOrder, true);
  assert.equal(model.intro, H.TEXT.introFixed);
  assert.equal(model.visibleTitle, H.TEXT.visibleTitleFixed);
  assert.equal(model.hiddenTitle, H.TEXT.hiddenTitleFixed);
  assert.deepEqual(model.visible.map((row) => row.id), CARDS.map((card) => card.id).filter((id) => id !== "trip"));
  assert.ok(model.visible.every((row) => row.canUp === false && row.canDown === false));
  assert.deepEqual(model.hidden, [{ id: "trip", label: "Last trip" }]);
  assert.equal(model.customised, true);

  const orderOnly = H.editorModel("drive", CARDS, base({ order: { drive: ["tires"] } }), true);
  assert.equal(orderOnly.customised, false);
  assert.equal(orderOnly.note, H.TEXT.noteDefault);
  assert.equal(H.isCustomised("drive", CARDS, base({ order: { drive: ["tires"] } }), true), false);
  assert.equal(H.isCustomised("drive", CARDS, base({ order: { drive: ["tires"] } })), true);

  const movable = H.editorModel("parked", CARDS, base());
  assert.equal(movable.fixedOrder, false);
  assert.equal(movable.intro, H.TEXT.intro);
});

test("editorModel with every card hidden has no visible rows", () => {
  const settings = base({ hidden: { parked: CARDS.map((card) => card.id) } });
  const model = H.editorModel("parked", CARDS, settings);
  assert.deepEqual(model.visible, []);
  assert.equal(model.hidden.length, 4);
  assert.equal(model.empty, false);
});

test("a hide → move → show sequence restores the card where it was", () => {
  let settings = base();
  settings = H.toggleHidden("parked", settings, "tires", true);
  settings = H.move("parked", CARDS, settings, "battery", 1); // jumps over hidden tires
  assert.deepEqual(H.visibleIds("parked", CARDS, settings), ["trip", "battery", "service"]);
  settings = H.toggleHidden("parked", settings, "tires", false);
  assert.deepEqual(H.visibleIds("parked", CARDS, settings), ["trip", "tires", "battery", "service"]);
  settings = H.reset("parked", settings);
  assert.deepEqual(H.visibleIds("parked", CARDS, settings), ["battery", "tires", "trip", "service"]);
  assert.equal(H.isCustomised("parked", CARDS, settings), false);
});

test("focusKey names the control that should keep focus after an edit", () => {
  assert.equal(H.focusKey("tires", "up"), "tires:up");
  assert.equal(H.focusKey("tires", "check"), "tires:check");
});

test("focusFallback moves focus to the other arrow when one becomes disabled", () => {
  const model = H.editorModel("parked", CARDS, base());
  assert.equal(H.focusFallback(model, "battery:up"), "battery:down", "battery is first, up disabled");
  assert.equal(H.focusFallback(model, "tires:up"), "tires:up");
  assert.equal(H.focusFallback(model, "service:down"), "service:up");
  assert.equal(H.focusFallback(model, "service:check"), "service:check");
  const single = H.editorModel("parked", [CARDS[0]], base());
  assert.equal(H.focusFallback(single, "battery:down"), "battery:check", "no arrow enabled");
  const hiddenModel = H.editorModel("parked", CARDS, base({ hidden: { parked: ["trip"] } }));
  assert.equal(H.focusFallback(hiddenModel, "trip:check"), "trip:check", "hidden rows keep their checkbox key");
  assert.equal(H.focusFallback(hiddenModel, "gone:up"), null);
  assert.equal(H.focusFallback(hiddenModel, null), null);
});
