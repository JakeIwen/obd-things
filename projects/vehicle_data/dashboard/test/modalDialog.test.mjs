import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { restoreFocus, showModal } from "../src/dialogs/modalDialog.js";

test("showModal opens the dialog, falls back to the attribute, and reports what had focus", () => {
  const opened = [];
  assert.equal(showModal({ showModal: () => opened.push("modal") }), null); // no document in node
  assert.deepEqual(opened, ["modal"]);
  const attributes = [];
  showModal({
    showModal: () => {
      throw new Error("not connected");
    },
    setAttribute: (name, value) => attributes.push([name, value]),
  });
  assert.deepEqual(attributes, [["open", ""]]);
});

test("restoreFocus focuses a connected opener and tolerates everything else", () => {
  let focused = 0;
  restoreFocus({ isConnected: true, focus: () => { focused += 1; } });
  restoreFocus({ isConnected: false, focus: () => { focused += 1; } });
  restoreFocus({ isConnected: true, focus: () => { throw new Error("detached"); } });
  restoreFocus(null);
  restoreFocus({ isConnected: true });
  assert.equal(focused, 1);
});

test("every dialog opens through modalDialog.js", () => {
  const dir = new URL("../src/dialogs/", import.meta.url);
  const dialogs = readdirSync(dir).filter((name) => name.endsWith(".jsx"));
  assert.ok(dialogs.length >= 5);
  for (const name of dialogs) {
    const src = readFileSync(new URL(name, dir), "utf8");
    assert.match(src, /from "\.\/modalDialog\.js"/, name + " does not use the shared open/close code");
    assert.doesNotMatch(src, /\.showModal\(\)/, name + " opens a dialog by hand");
  }
});
