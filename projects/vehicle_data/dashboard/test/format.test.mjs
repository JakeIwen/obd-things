import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  DASH,
  MINUS,
  fmtValue,
  fmtUnit,
  fmtAgeCoarse,
  fmtTime,
  fmtDuration,
  fmtInt,
  fmtFixed,
  fmtDelta,
  fmtClock,
  parseDate,
  groupThousands,
  roundHalfAway,
  decimalsFor,
} from "../src/format.js";

const here = dirname(fileURLToPath(import.meta.url));
const snapshot = JSON.parse(readFileSync(join(here, "fixtures", "snapshot.json"), "utf8"));

// A fixed "now": 2026-09-22 16:25 local time, whatever zone the test runs in.
const NOW = new Date(2026, 8, 22, 16, 25, 0);

function local(y, m, d, h, min, s) {
  return new Date(y, m, d, h, min, s || 0);
}

test("fmtValue: per-metric decimals from design 5.1", () => {
  assert.equal(fmtValue("vehicle.speed", 64.4), "64");
  assert.equal(fmtValue("engine.coolant_temperature", 123.8), "124");
  assert.equal(fmtValue("transmission.oil_temperature", 75.2), "75");
  assert.equal(fmtValue("engine.vvt_oil_temperature", 219.49), "219");
  assert.equal(fmtValue("engine.oil_pressure", 30.16784944788352), "30");
  assert.equal(fmtValue("tire.pressure.fl", 54.1), "54");
  assert.equal(fmtValue("tire.pressure.rr", 74.8), "75");
  assert.equal(fmtValue("engine.crankshaft_power", 132.6), "133");
  assert.equal(fmtValue("engine.crankshaft_torque", 165.4), "165");
  assert.equal(fmtValue("generator.field_duty", 42.49), "42");
  assert.equal(fmtValue("battery.voltage", 12.6), "12.60");
  assert.equal(fmtValue("battery.voltage", 14.123), "14.12");
  assert.equal(fmtValue("radar.alignment.azimuth", -0.5), MINUS + "0.50");
  assert.equal(fmtValue("radar.alignment.elevation", 1.234), "1.23");
});

test("fmtValue: thousands separator for rpm and odometer", () => {
  assert.equal(fmtValue("engine.rpm", 6303), "6,303");
  assert.equal(fmtValue("engine.rpm", 799), "799");
  assert.equal(fmtValue("engine.rpm", 1000), "1,000");
  // Truncated, never rounded up: legacy app.js and the Service card read 53,892.
  assert.equal(fmtValue("vehicle.odometer", 53892.766), "53,892");
  assert.equal(fmtValue("vehicle.odometer", 1234567.4), "1,234,567");
  assert.equal(fmtValue("transmission.turbine_speed", 2450.2), "2,450");
  // Speed never gets a separator (it cannot reach 1,000 anyway).
  assert.equal(fmtValue("vehicle.speed", 999), "999");
});

test("fmtValue: negative torque, negative zero and rounding half away from zero", () => {
  assert.equal(fmtValue("engine.crankshaft_torque", -12.6), MINUS + "13");
  assert.equal(fmtValue("engine.crankshaft_torque", -12.5), MINUS + "13");
  assert.equal(fmtValue("engine.crankshaft_torque", 12.5), "13");
  assert.equal(fmtValue("engine.crankshaft_torque", -0.4), "0");
  assert.equal(fmtValue("engine.crankshaft_torque", -0), "0");
  assert.equal(fmtValue("engine.crankshaft_power", -30), MINUS + "30");
  assert.equal(fmtValue("battery.voltage", -0.004), "0.00");
});

test("fmtValue: null, undefined, NaN, Infinity, booleans and strings", () => {
  assert.equal(fmtValue("engine.rpm", null), DASH);
  assert.equal(fmtValue("engine.rpm", undefined), DASH);
  assert.equal(fmtValue("engine.rpm", NaN), DASH);
  assert.equal(fmtValue("engine.rpm", Infinity), DASH);
  assert.equal(fmtValue("engine.rpm", -Infinity), DASH);
  assert.equal(fmtValue("engine.rpm", "7"), "7");
  assert.equal(fmtValue("engine.rpm", "   "), DASH);
  assert.equal(fmtValue("engine.rpm", {}), DASH);
  assert.equal(fmtValue("vehicle.ignition_on", true), "ON");
  assert.equal(fmtValue("vehicle.ignition_on", false), "OFF");
  assert.equal(fmtValue("diagnostics.cluster.did.0107.raw", 255), "255");
  assert.equal(fmtValue("some.future.metric", 12.34), "12");
});

test("fmtValue formats every live value of the real snapshot without throwing", () => {
  for (const [name, m] of Object.entries(snapshot.metrics)) {
    const out = fmtValue(name, m.value);
    assert.equal(typeof out, "string");
    assert.ok(out.length > 0, name);
    if (m.value === null) assert.equal(out, DASH, name);
  }
});

test("decimalsFor falls back to 0 for unknown metrics", () => {
  assert.equal(decimalsFor("battery.voltage"), 2);
  assert.equal(decimalsFor("radar.alignment.elevation"), 2);
  assert.equal(decimalsFor("transmission.gear_estimate"), 0);
});

test("fmtFixed and helpers", () => {
  assert.equal(fmtFixed(1234.5678, 2, true), "1,234.57");
  assert.equal(fmtFixed(0.5, 0, false), "1");
  assert.equal(fmtFixed(2.345, 1), "2.3");
  assert.equal(fmtFixed(-1234, 0, true), MINUS + "1,234");
  assert.equal(fmtFixed(null, 0), DASH);
  assert.equal(fmtFixed(Number.MAX_VALUE, 2), DASH);
  assert.equal(groupThousands("1"), "1");
  assert.equal(groupThousands("123"), "123");
  assert.equal(groupThousands("1234"), "1,234");
  assert.equal(groupThousands("123456"), "123,456");
  assert.equal(groupThousands("1234567"), "1,234,567");
  assert.equal(roundHalfAway(-12.5), -13);
  assert.equal(roundHalfAway(12.5), 13);
  assert.equal(roundHalfAway(-12.4), -12);
});

test("fmtInt", () => {
  assert.equal(fmtInt(6303), "6,303");
  assert.equal(fmtInt(53892.766), "53,893");
  assert.equal(fmtInt(0), "0");
  assert.equal(fmtInt(-1500), MINUS + "1,500");
  assert.equal(fmtInt(NaN), DASH);
  assert.equal(fmtInt(null), DASH);
});

test("fmtDelta", () => {
  assert.equal(fmtDelta(3.4), "+3.4");
  assert.equal(fmtDelta(-3.4), MINUS + "3.4");
  assert.equal(fmtDelta(0), "0.0");
  assert.equal(fmtDelta(0.04), "0.0");
  assert.equal(fmtDelta(4, 0), "+4");
  assert.equal(fmtDelta(NaN), DASH);
});

test("fmtUnit: display units by broker unit and by name", () => {
  assert.equal(fmtUnit("vehicle.speed", "mph"), "mph");
  assert.equal(fmtUnit("engine.rpm", "rpm"), "rpm");
  assert.equal(fmtUnit("engine.coolant_temperature", "°F"), "°F");
  assert.equal(fmtUnit("engine.oil_pressure", "psi"), "psi");
  assert.equal(fmtUnit("battery.voltage", "V"), "V");
  assert.equal(fmtUnit("engine.crankshaft_power", "hp"), "hp");
  assert.equal(fmtUnit("engine.crankshaft_torque", "lb-ft"), "lb-ft");
  assert.equal(fmtUnit("generator.field_duty", "%"), "%");
  assert.equal(fmtUnit("vehicle.odometer", "mi"), "mi");
  assert.equal(fmtUnit("radar.alignment.azimuth", "deg"), "°");
  assert.equal(fmtUnit("vehicle.ignition_on", "boolean"), "");
  assert.equal(fmtUnit("diagnostics.cluster.did.0107.raw", "raw_u8"), "");
  assert.equal(fmtUnit("diagnostics.cluster.did.1000.raw", "raw_u16_be"), "");
  // Missing unit falls back to the metric name; unknown metric -> empty.
  assert.equal(fmtUnit("battery.voltage", null), "V");
  assert.equal(fmtUnit("radar.alignment.elevation", undefined), "°");
  assert.equal(fmtUnit("engine.rpm", ""), "rpm");
  assert.equal(fmtUnit("some.future.metric", null), "");
  // Unlisted units display as sent.
  assert.equal(fmtUnit("some.future.metric", "kPa"), "kPa");
});

test("fmtUnit covers every unit in the real catalog", () => {
  for (const def of snapshot.catalog) {
    const out = fmtUnit(def.name, def.unit);
    assert.equal(typeof out, "string", def.name);
    assert.ok(!/^raw_|^boolean$|^deg$/.test(out), `${def.name}: ${out} should be a display unit`);
  }
});

test("fmtAgeCoarse boundaries", () => {
  assert.equal(fmtAgeCoarse(0), "now");
  assert.equal(fmtAgeCoarse(4900), "now");
  assert.equal(fmtAgeCoarse(4999), "now");
  // Minute resolution (design 5.1): no per-second buckets.
  assert.equal(fmtAgeCoarse(5000), "now");
  assert.equal(fmtAgeCoarse(12400), "now");
  assert.equal(fmtAgeCoarse(59000), "now");
  assert.equal(fmtAgeCoarse(59999), "now");
  assert.equal(fmtAgeCoarse(60000), "1 min");
  assert.equal(fmtAgeCoarse(2 * 60000 + 30000), "2 min");
  assert.equal(fmtAgeCoarse(3599 * 1000), "59 min");
  assert.equal(fmtAgeCoarse(3600 * 1000), "1 h");
  assert.equal(fmtAgeCoarse(5040 * 1000), "1.4 h");
  assert.equal(fmtAgeCoarse(5075 * 1000), "1.4 h");
  assert.equal(fmtAgeCoarse(2 * 3600 * 1000), "2 h");
  assert.equal(fmtAgeCoarse(23.95 * 3600 * 1000), "23.9 h");
  assert.equal(fmtAgeCoarse(24 * 3600 * 1000), "1 d");
  assert.equal(fmtAgeCoarse(2 * 86400 * 1000), "2 d");
  assert.equal(fmtAgeCoarse(2 * 86400 * 1000 + 5 * 3600 * 1000), "2 d");
  assert.equal(fmtAgeCoarse(-1500), "now");
  assert.equal(fmtAgeCoarse(NaN), DASH);
  assert.equal(fmtAgeCoarse(null), DASH);
  assert.equal(fmtAgeCoarse(Infinity), DASH);
});

test("fmtClock: 12-hour, lowercase am/pm, midnight and noon", () => {
  assert.equal(fmtClock(local(2026, 8, 22, 16, 25)), "4:25 pm");
  assert.equal(fmtClock(local(2026, 8, 22, 0, 0)), "12:00 am");
  assert.equal(fmtClock(local(2026, 8, 22, 0, 5)), "12:05 am");
  assert.equal(fmtClock(local(2026, 8, 22, 12, 0)), "12:00 pm");
  assert.equal(fmtClock(local(2026, 8, 22, 12, 30)), "12:30 pm");
  assert.equal(fmtClock(local(2026, 8, 22, 11, 59)), "11:59 am");
  assert.equal(fmtClock(local(2026, 8, 22, 23, 59)), "11:59 pm");
  assert.equal(fmtClock(local(2026, 8, 22, 9, 7)), "9:07 am");
});

test("fmtTime: today shows the clock only", () => {
  assert.equal(fmtTime(local(2026, 8, 22, 16, 25), NOW), "4:25 pm");
  assert.equal(fmtTime(local(2026, 8, 22, 0, 0), NOW), "12:00 am");
  assert.equal(fmtTime(local(2026, 8, 22, 23, 59, 59), NOW), "11:59 pm");
});

test("fmtTime: yesterday boundary", () => {
  assert.equal(fmtTime(local(2026, 8, 21, 23, 59, 59), NOW), "yesterday 11:59 pm");
  assert.equal(fmtTime(local(2026, 8, 21, 0, 0), NOW), "yesterday 12:00 am");
  assert.equal(fmtTime(local(2026, 8, 20, 23, 59, 59), NOW), "Sep 20, 11:59 pm");
  // Month and year roll-overs.
  assert.equal(fmtTime(local(2026, 8, 30, 8, 0), local(2026, 9, 1, 3, 0)), "yesterday 8:00 am");
  assert.equal(fmtTime(local(2025, 11, 31, 22, 0), local(2026, 0, 1, 3, 0)), "yesterday 10:00 pm");
});

test("fmtTime: other days and other years", () => {
  assert.equal(fmtTime(local(2026, 8, 15, 4, 25), NOW), "Sep 15, 4:25 am");
  assert.equal(fmtTime(local(2026, 0, 3, 13, 5), NOW), "Jan 3, 1:05 pm");
  assert.equal(fmtTime(local(2025, 8, 22, 16, 25), NOW), "Sep 22, 2025, 4:25 pm");
  // The future is just another date.
  assert.equal(fmtTime(local(2026, 8, 23, 16, 25), NOW), "Sep 23, 4:25 pm");
});

test("fmtTime: ISO strings, epoch numbers and invalid input", () => {
  const d = local(2026, 8, 22, 16, 25, 17);
  assert.equal(fmtTime(d.toISOString(), NOW), "4:25 pm");
  assert.equal(fmtTime(d.getTime(), NOW), "4:25 pm");
  // Broker timestamps carry microseconds and an explicit +00:00 offset.
  const iso = d.toISOString().replace("Z", "175+00:00");
  assert.equal(fmtTime(iso, NOW), "4:25 pm");
  assert.equal(parseDate(iso).getTime(), d.getTime());
  assert.equal(fmtTime(null, NOW), DASH);
  assert.equal(fmtTime(undefined, NOW), DASH);
  assert.equal(fmtTime("", NOW), DASH);
  assert.equal(fmtTime("not a date", NOW), DASH);
  assert.equal(fmtTime(new Date(NaN), NOW), DASH);
  assert.equal(fmtTime(NaN, NOW), DASH);
  // An invalid reference falls back to the real clock rather than throwing.
  assert.equal(typeof fmtTime(d, new Date(NaN)), "string");
  assert.equal(typeof fmtTime(d), "string");
});

test("fmtTime parses every observed_at in the real snapshot", () => {
  for (const [name, m] of Object.entries(snapshot.metrics)) {
    if (typeof m.observed_at !== "string") continue;
    assert.notEqual(fmtTime(m.observed_at, NOW), DASH, name);
  }
});

test("fmtDuration", () => {
  assert.equal(fmtDuration(0), "0 s");
  assert.equal(fmtDuration(45), "45 s");
  assert.equal(fmtDuration(59.9), "59 s");
  assert.equal(fmtDuration(60), "1 min");
  assert.equal(fmtDuration(32 * 60 + 40), "32 min");
  assert.equal(fmtDuration(3599), "59 min");
  assert.equal(fmtDuration(3600), "1 h 00");
  assert.equal(fmtDuration(3900), "1 h 05");
  assert.equal(fmtDuration(65 * 60), "1 h 05");
  assert.equal(fmtDuration(26 * 3600 + 12 * 60), "26 h 12");
  assert.equal(fmtDuration(-1), DASH);
  assert.equal(fmtDuration(NaN), DASH);
  assert.equal(fmtDuration(null), DASH);
});

test("formatters never call Intl or toLocaleString", () => {
  const raw = readFileSync(join(here, "..", "src", "format.js"), "utf8");
  // Judge the code, not the comments.
  const src = raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.ok(!/\bIntl\b/.test(src));
  assert.ok(!/toLocale/.test(src));
  assert.ok(!/\bDate\.prototype\.toString\b|\.toDateString\(|\.toTimeString\(/.test(src));
});
