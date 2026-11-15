import { test } from "node:test";
import assert from "node:assert/strict";
import { voltageFieldParts } from "../src/views/driveGauges.helpers.js";

/** The parts joined as the tile reads them, with each unit in brackets. */
const read = (p) => p.volts + (p.voltUnit && "[" + p.voltUnit + "]") + p.sep + p.field + (p.fieldUnit && "[" + p.fieldUnit + "]");

test("VOLTAGE / ALT FIELD: live voltage with live field shows both, units apart", () => {
  assert.equal(read(voltageFieldParts("13.6", "V", "live", 42)), "13.6[V]/42[%]");
  assert.equal(read(voltageFieldParts("13.6", "V", "live", 100)), "13.6[V]/100[%]");
  assert.equal(read(voltageFieldParts("13.6", "V", "live", 41.6)), "13.6[V]/42[%]");
  assert.equal(read(voltageFieldParts("13.6", "V", "live", 0)), "13.6[V]/0[%]");
});

test("VOLTAGE / ALT FIELD: a missing field leaves only its half as a dash, with no unit", () => {
  assert.equal(read(voltageFieldParts("13.6", "V", "live", null)), "13.6[V]/—");
});

test("VOLTAGE / ALT FIELD: held voltage never pairs with a field figure", () => {
  assert.equal(read(voltageFieldParts("13.4", "V", "held", 42)), "13.4[V]/—");
  assert.equal(read(voltageFieldParts("13.4", "V", "held", null)), "13.4[V]/—");
});

test("VOLTAGE / ALT FIELD: missing voltage keeps a live field, else the whole value is a dash", () => {
  assert.equal(read(voltageFieldParts("—", "V", "off", 42)), "—/42[%]");
  assert.equal(read(voltageFieldParts("—", "V", "off", null)), "—");
});
