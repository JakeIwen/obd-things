import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CHOICE_STORAGE_KEY, chooseEffort, chooseOption, choiceLabel, effortsOf, loadChoice, optionKey, pickerOptions, resolveChoice,
  saveChoice, unavailableNotes, validChoice,
} from "../src/components/modelPicker.helpers.js";

const CATALOG = {
  available: true,
  default: { provider: "codex", model: "gpt-6-astra", effort: "high" },
  groups: [
    { provider: "claude", label: "Claude", available: true, models: [
      { id: "opus", label: "Opus", efforts: ["low", "medium", "high"], default_effort: "high" },
      { id: "haiku", label: "Haiku", efforts: [], default_effort: null }] },
    { provider: "codex", label: "GPT (Codex)", available: true, models: [
      { id: "gpt-6-astra", label: "GPT-6-Astra", efforts: ["low", "high", "xhigh"], default_effort: "high" }] },
    { provider: "local", label: "Local", available: false, reason: "Not set up on this Pi", models: [] },
  ],
};

test("options are grouped by provider; an unavailable group has no models but a reason", () => {
  const options = pickerOptions(CATALOG);
  assert.deepEqual(options.map((o) => [o.group, o.label]), [["Claude", "Opus"], ["Claude", "Haiku"], ["GPT (Codex)", "GPT-6-Astra"]]);
  assert.deepEqual(unavailableNotes(CATALOG), [{ label: "Local", reason: "Not set up on this Pi" }]);
  assert.deepEqual(pickerOptions(null), []);
});

test("a choice is valid only for a model and effort on offer; no efforts means effort null", () => {
  assert.equal(validChoice({ provider: "claude", model: "opus", effort: "medium" }, CATALOG), true);
  assert.equal(validChoice({ provider: "claude", model: "opus", effort: "ultra" }, CATALOG), false);
  assert.equal(validChoice({ provider: "claude", model: "opus", effort: null }, CATALOG), false);
  assert.equal(validChoice({ provider: "claude", model: "haiku", effort: null }, CATALOG), true);
  assert.equal(validChoice({ provider: "claude", model: "haiku", effort: "high" }, CATALOG), false);
  assert.equal(validChoice({ provider: "local", model: "x", effort: null }, CATALOG), false, "an unavailable provider is not valid");
  assert.equal(validChoice(null, CATALOG), false);
});

test("the remembered choice wins when valid, else the catalog default, else the first model", () => {
  const saved = { provider: "claude", model: "opus", effort: "low" };
  assert.deepEqual(resolveChoice(saved, CATALOG), saved);
  assert.deepEqual(resolveChoice({ provider: "claude", model: "gone", effort: "low" }, CATALOG), CATALOG.default);
  assert.deepEqual(resolveChoice(null, CATALOG), CATALOG.default);
  assert.deepEqual(resolveChoice(null, { ...CATALOG, default: null }), { provider: "claude", model: "opus", effort: "high" });
  assert.equal(resolveChoice(null, { groups: [{ provider: "x", label: "X", available: false, models: [] }], default: null }), null);
});

test("choosing a model keeps the effort when it offers it, else its default; no efforts gives null", () => {
  const choice = { provider: "codex", model: "gpt-6-astra", effort: "low" };
  assert.deepEqual(chooseOption(CATALOG, choice, optionKey("claude", "opus")), { provider: "claude", model: "opus", effort: "low" });
  assert.deepEqual(chooseOption(CATALOG, { ...choice, effort: "xhigh" }, optionKey("claude", "opus")), { provider: "claude", model: "opus", effort: "high" });
  assert.deepEqual(chooseOption(CATALOG, choice, optionKey("claude", "haiku")), { provider: "claude", model: "haiku", effort: null });
  assert.equal(chooseOption(CATALOG, choice, "nope"), choice);
  assert.deepEqual(effortsOf(CATALOG, choice), ["low", "high", "xhigh"]);
  assert.deepEqual(effortsOf(CATALOG, { provider: "claude", model: "haiku", effort: null }), []);
  assert.deepEqual(chooseEffort(CATALOG, choice, "xhigh"), { ...choice, effort: "xhigh" });
  assert.equal(chooseEffort(CATALOG, choice, "max"), choice);
  assert.equal(choiceLabel(CATALOG, choice), "GPT-6-Astra · low");
  assert.equal(choiceLabel(CATALOG, { provider: "claude", model: "haiku", effort: null }), "Haiku");
  assert.equal(choiceLabel(CATALOG, null), "Choose a model");
});

test("the remembered choice survives a missing or broken storage", () => {
  const data = {};
  const backend = { getItem: (k) => data[k] ?? null, setItem: (k, v) => { data[k] = v; } };
  assert.equal(loadChoice(backend), null);
  saveChoice({ provider: "claude", model: "opus", effort: "high" }, backend);
  assert.deepEqual(loadChoice(backend), { provider: "claude", model: "opus", effort: "high" });
  data[CHOICE_STORAGE_KEY] = "{not json";
  assert.equal(loadChoice(backend), null);
  data[CHOICE_STORAGE_KEY] = JSON.stringify({ provider: 1 });
  assert.equal(loadChoice(backend), null);
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  assert.equal(loadChoice(broken), null);
  assert.doesNotThrow(() => saveChoice({ provider: "a", model: "b", effort: null }, broken));
  assert.equal(loadChoice(null), null);
});

test("a storage getter that throws means no remembered choice and a silent no-op save", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", { configurable: true, get() { throw new Error("denied"); } });
  try {
    assert.equal(loadChoice(), null);
    assert.doesNotThrow(() => saveChoice({ provider: "claude", model: "opus", effort: null }));
  } finally {
    if (original) Object.defineProperty(globalThis, "localStorage", original);
    else delete globalThis.localStorage;
  }
});
