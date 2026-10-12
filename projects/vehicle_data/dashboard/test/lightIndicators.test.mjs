import { test } from "node:test";
import assert from "node:assert/strict";
import { LIGHT_STATES, lightIndicatorState } from "../src/components/lightIndicators.helpers.js";

test("light indicators illuminate only a current true value", () => {
  assert.equal(lightIndicatorState({ kind: "live", value: true }, "live"), LIGHT_STATES.ON);
  assert.equal(lightIndicatorState({ kind: "live", value: false }, "live"), LIGHT_STATES.OFF);
});

test("expired, retained, missing and non-boolean observations cannot illuminate a light", () => {
  for (const observation of [null, {}, { kind: "off", value: true },
    { kind: "held", value: true }, { kind: "live", value: 1 },
    { kind: "live", value: "true" }, { kind: "live", value: null }]) {
    assert.equal(lightIndicatorState(observation, "live"), LIGHT_STATES.OFF);
  }
});

test("connection loss hides even an otherwise current lights-on observation", () => {
  for (const connection of ["connecting", "unavailable", undefined]) {
    assert.equal(lightIndicatorState({ kind: "live", value: true }, connection), LIGHT_STATES.OFF);
  }
});
