import { test } from "node:test";
import assert from "node:assert/strict";
import { voltageFieldText } from "../src/views/driveGauges.helpers.js";

test("VOLTAGE/FIELD: live voltage with live field shows both", () => {
  assert.equal(voltageFieldText("13.6", "live", 42), "13.6v/42%");
  assert.equal(voltageFieldText("13.6", "live", 100), "13.6v/100%");
  assert.equal(voltageFieldText("13.6", "live", 41.6), "13.6v/42%");
  assert.equal(voltageFieldText("13.6", "live", 0), "13.6v/0%");
});

test("VOLTAGE/FIELD: a missing field leaves only its half as a dash", () => {
  assert.equal(voltageFieldText("13.6", "live", null), "13.6v/\u2014");
});

test("VOLTAGE/FIELD: held voltage never pairs with a field figure", () => {
  assert.equal(voltageFieldText("13.4", "held", 42), "13.4v/\u2014");
  assert.equal(voltageFieldText("13.4", "held", null), "13.4v/\u2014");
});

test("VOLTAGE/FIELD: missing voltage keeps a live field, else the whole value is a dash", () => {
  assert.equal(voltageFieldText("\u2014", "off", 42), "\u2014/42%");
  assert.equal(voltageFieldText("\u2014", "off", null), "\u2014");
});
