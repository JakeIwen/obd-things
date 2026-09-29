// Drive page 2 helpers (design 3.1.1): radar aim tile, gear estimate, shaft ratio, swipe paging,
// and the page-2 entries of the Drive customiser registry.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as H from "../src/views/driveMore.helpers.js";
import { swipeTarget, DRIVE_PAGE_NAMES } from "../src/views/drivePager.js";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const MINUS = "−";

const WINDOWS = {
  "radar.alignment.azimuth": {
    60: { count: 6, mean: 0.150841, peak_abs: 0.150841, span_seconds: 54.9 },
    300: { count: 20, mean: 0.15084, peak_abs: 0.150841, span_seconds: 208 },
    latest_value: 0.150841,
    observed_at: "2026-09-24T21:10:38.001564+00:00",
  },
  "radar.alignment.elevation": {
    60: { count: 6, mean: -0.0173, peak_abs: 0.0174, span_seconds: 54.9 },
    300: { count: 20, mean: -0.0177, peak_abs: 0.0184, span_seconds: 208 },
    latest_value: -0.017161,
    observed_at: "2026-09-24T21:10:38.001188+00:00",
  },
};

test("fmtAngleSigned and windowMean", () => {
  assert.equal(H.fmtAngleSigned(0.150841), "+0.15°");
  assert.equal(H.fmtAngleSigned(-0.0173), MINUS + "0.02°");
  assert.equal(H.fmtAngleSigned(0.001), "0.00°");
  assert.equal(H.fmtAngleSigned(null), "—");
  assert.equal(H.windowMean({ count: 3, mean: 0.5 }), "+0.50°");
  assert.equal(H.windowMean({ count: 0, mean: 0.5 }), "—");
  assert.equal(H.windowMean(null), "—");
  assert.equal(H.windowMean({ count: 2 }), "—");
});

test("radar tile: live readings show latest, means and the ±1° reference in words", () => {
  const m = H.radarTileModel({
    axes: { azimuth: { fresh: true, value: 0.150841 }, elevation: { fresh: true, value: -0.017161 } },
    windows: WINDOWS,
    polling: { commissioned: true },
  });
  assert.equal(m.live, true);
  assert.equal(m.sub, "within ±1°");
  assert.deepEqual(m.axes.azimuth, { latest: "+0.15°", m1: "+0.15°", m5: "+0.15°" });
  assert.deepEqual(m.axes.elevation, { latest: MINUS + "0.02°", m1: MINUS + "0.02°", m5: MINUS + "0.02°" });
  const near = H.radarTileModel({ axes: { azimuth: { fresh: true, value: -0.85 } } });
  assert.equal(near.sub, "near the ±1° limit");
  const out = H.radarTileModel({ axes: { elevation: { fresh: true, value: 1.2 } } });
  assert.equal(out.sub, "outside ±1°");
  assert.equal(out.axes.azimuth.latest, "—");
});

test("radar tile: not reading shows the last reading once, with its time", () => {
  const nowMs = Date.parse("2026-09-24T21:30:00Z");
  const held = H.radarTileModel({
    axes: {
      azimuth: { fresh: false, value: null, held: { value: 0.150841, observed_at: "2026-09-24T21:10:38Z" } },
      elevation: { fresh: false, value: null, held: null },
    },
    windows: WINDOWS,
    nowMs,
  });
  assert.equal(held.live, false);
  assert.match(held.sub, /^Not reading · last \d{1,2}:\d{2} (am|pm)$/);
  assert.equal(held.sub.split("stale").length, 1, "no 'stale' word");
  assert.equal(held.axes.azimuth.latest, "+0.15°");
  // Elevation falls back to the broker's latest window value.
  assert.equal(held.axes.elevation.latest, MINUS + "0.02°");
  assert.equal(held.axes.elevation.m5, MINUS + "0.02°");
});

test("radar tile: nothing to show, or reads switched off, says Not reading", () => {
  const empty = H.radarTileModel({});
  assert.equal(empty.live, false);
  assert.equal(empty.sub, "Not reading");
  for (const axis of H.RADAR_TILE_AXES) assert.deepEqual(empty.axes[axis.id], { latest: "—", m1: "—", m5: "—" });
  const off = H.radarTileModel({
    axes: { azimuth: { fresh: true, value: 0.2 } },
    polling: { commissioned: false, detail: "off" },
  });
  assert.equal(off.live, false, "a record is never live when the reads are not commissioned");
  assert.equal(off.sub, "Not reading");
  assert.equal(H.radarTileModel(null).sub, "Not reading");
});

test("radar tile text carries no provenance jargon", () => {
  const texts = [];
  for (const input of [{}, { axes: { azimuth: { fresh: true, value: 0.3 } }, windows: WINDOWS }]) {
    const m = H.radarTileModel(input);
    texts.push(m.sub, ...Object.values(m.axes).flatMap((a) => Object.values(a)));
  }
  texts.push(...H.RADAR_TILE_AXES.map((a) => a.label));
  for (const t of texts) assert.doesNotMatch(t, /candidate|0845|did|radar_acc|stale|quality/i, t);
});

test("gearText shows ~gear only while the record is current", () => {
  assert.equal(H.gearText({ available: true, stale: false, value: "7" }), "~7");
  assert.equal(H.gearText({ available: true, stale: false, value: " R " }), "~R");
  assert.equal(H.gearText({ available: true, stale: false, value: 4 }), "~4");
  assert.equal(H.gearText({ available: true, stale: true, value: "7" }), "—");
  assert.equal(H.gearText({ available: false, value: "7" }), "—");
  assert.equal(H.gearText({ available: true, stale: false, value: "" }), "—");
  assert.equal(H.gearText(null), "—");
});

test("ratioText needs both shafts and a turning output", () => {
  assert.equal(H.ratioText(2100, 2500), "ratio 0.84");
  assert.equal(H.ratioText(2100, 1000), "ratio 2.10");
  assert.equal(H.ratioText(2100, 50), "");
  assert.equal(H.ratioText(null, 2000), "");
  assert.equal(H.ratioText(2000, null), "");
});

test("speedLimitText: whole mph, or — when the cluster shows none", () => {
  assert.equal(H.speedLimitText(55), "55");
  assert.equal(H.speedLimitText(30), "30");
  assert.equal(H.speedLimitText(0), "—");
  assert.equal(H.speedLimitText(null), "—");
  assert.equal(H.speedLimitText(NaN), "—");
});

test("accTileModel: set speed with the state word; Off and ready have no number", () => {
  assert.deepEqual(H.accTileModel("engaged", 66), { value: "66", unit: "mph", sub: "set · engaged", kind: "live" });
  assert.deepEqual(H.accTileModel("override", 61), { value: "61", unit: "mph", sub: "set · accelerator override", kind: "live" });
  assert.deepEqual(H.accTileModel("standby", 66), { value: "66", unit: "mph", sub: "set · standby", kind: "held" });
  // A cached set speed never shows once the state says off or ready.
  assert.deepEqual(H.accTileModel("off", 66), { value: "Off", unit: "", sub: "", kind: "off" });
  assert.deepEqual(H.accTileModel("ready", 66), { value: "—", unit: "", sub: "ready · no set speed", kind: "off" });
  // No current state record: the verified set speed alone, or nothing.
  assert.deepEqual(H.accTileModel(null, 66), { value: "66", unit: "mph", sub: "set speed", kind: "live" });
  assert.deepEqual(H.accTileModel(null, null), { value: "—", unit: "", sub: "", kind: "off" });
  assert.deepEqual(H.accTileModel("engaged", null), { value: "—", unit: "", sub: "engaged", kind: "off" });
  assert.deepEqual(H.accTileModel("mystery", 50), { value: "50", unit: "mph", sub: "set speed", kind: "live" });
  for (const state of ["off", "ready", "engaged", "override", "standby", null]) {
    const m = H.accTileModel(state, 60);
    for (const t of [m.value, m.sub]) assert.doesNotMatch(t, /candidate|0x5a0|quality|stale/i, t);
  }
});

test("accTileModel: vehicle ahead, following distance and fixed cruise (2026-09-28 callouts)", () => {
  const adaptive = (bars, lead) => ({ mode: "adaptive", bars, lead });
  // 7:44 PM: following a vehicle at four bars, set 66.
  assert.deepEqual(H.accTileModel("engaged", 66, adaptive(4, true)), { value: "66", unit: "mph", sub: "engaged · vehicle ahead · gap 4 of 4", kind: "live" });
  // 7:00 PM: one bar, no vehicle.
  assert.deepEqual(H.accTileModel("engaged", 73, adaptive(1, false)), { value: "73", unit: "mph", sub: "engaged · gap 1 of 4", kind: "live" });
  assert.deepEqual(H.accTileModel("override", 40, adaptive(4, true)), { value: "40", unit: "mph", sub: "accelerator override · vehicle ahead · gap 4 of 4", kind: "live" });
  // Standby and ready show the gap but never a vehicle: the broker may still hold an old `true`.
  assert.deepEqual(H.accTileModel("standby", 61, adaptive(4, true)), { value: "61", unit: "mph", sub: "standby · gap 4 of 4", kind: "held" });
  assert.deepEqual(H.accTileModel("ready", null, adaptive(1, null)), { value: "—", unit: "", sub: "ready · gap 1 of 4", kind: "off" });
  // 7:05 PM: regular cruise on, set 65, cancel.
  const fixed = { mode: "fixed", bars: 3, lead: true };
  assert.deepEqual(H.accTileModel("ready", null, fixed), { value: "—", unit: "", sub: "ready · fixed cruise", kind: "off" });
  assert.deepEqual(H.accTileModel("engaged", 65, fixed), { value: "65", unit: "mph", sub: "engaged · fixed cruise", kind: "live" });
  assert.deepEqual(H.accTileModel("standby", 65, fixed), { value: "65", unit: "mph", sub: "standby · fixed cruise", kind: "held" });
  // Off hides everything, whatever is still cached.
  assert.deepEqual(H.accTileModel("off", 65, fixed), { value: "Off", unit: "", sub: "", kind: "off" });
  assert.deepEqual(H.accTileModel("off", 66, adaptive(4, true)), { value: "Off", unit: "", sub: "", kind: "off" });
  // Nothing current, or values the tile does not know: the earlier wording stands.
  for (const shown of [null, undefined, {}, { mode: null, bars: null, lead: null }, { mode: "mystery", bars: 2, lead: true }]) {
    assert.deepEqual(H.accTileModel("engaged", 66, shown), { value: "66", unit: "mph", sub: "set · engaged", kind: "live" });
    assert.deepEqual(H.accTileModel("ready", 66, shown), { value: "—", unit: "", sub: "ready · no set speed", kind: "off" });
  }
  assert.deepEqual(H.accTileModel("engaged", 66, adaptive(7, false)), { value: "66", unit: "mph", sub: "set · engaged", kind: "live" }, "bars outside 1-4 are not shown");
  assert.deepEqual(H.accTileModel("engaged", 66, adaptive(null, true)), { value: "66", unit: "mph", sub: "engaged · vehicle ahead", kind: "live" });
  // Without a current state nothing beside it is shown either.
  assert.deepEqual(H.accTileModel(null, 66, adaptive(4, true)), { value: "66", unit: "mph", sub: "set speed", kind: "live" });
  assert.deepEqual(H.accShownParts("engaged", adaptive(2, true)), ["vehicle ahead", "gap 2 of 4"]);
  assert.deepEqual(H.accShownParts(null, adaptive(2, true)), []);
  for (const st of ["ready", "engaged", "override", "standby"]) {
    for (const shown of [adaptive(3, true), fixed]) {
      assert.doesNotMatch(H.accTileModel(st, 60, shown).sub, /candidate|0x5a0|quality|stale|index|hud/i);
    }
  }
});

test("swipeTarget pages only on a deliberate horizontal swipe", () => {
  assert.equal(swipeTarget(-120, 10, 300, 1), 2);
  assert.equal(swipeTarget(120, -10, 300, 2), 1);
  assert.equal(swipeTarget(-120, 10, 300, 2), 2, "left on page 2 stays");
  assert.equal(swipeTarget(-60, 0, 300, 1), 1, "too short");
  assert.equal(swipeTarget(-120, 80, 300, 1), 1, "too diagonal (vertical scroll on a phone)");
  assert.equal(swipeTarget(-120, 0, 1200, 1), 1, "too slow");
  assert.equal(swipeTarget(NaN, 0, 100, 1), 1);
  assert.deepEqual(DRIVE_PAGE_NAMES.map(([n]) => n), [1, 2]);
});

test("Drive registry lists page-2 tiles with unique ids and every id is rendered", () => {
  const drive = readFileSync(here("../src/views/Drive.jsx"), "utf8");
  const more = readFileSync(here("../src/views/DriveMore.jsx"), "utf8");
  const block = drive.slice(drive.indexOf("export const DRIVE_TILES"), drive.indexOf("];", drive.indexOf("export const DRIVE_TILES")));
  const ids = [...block.matchAll(/(?:id: |\[)"([^"]+)"/g)].map((m) => m[1]);
  assert.equal(new Set(ids).size, ids.length, "ids are unique across both pages");
  const page2 = ["limit", "acc", "field", "gear", "radar", "turbine", "output", "power2", "torque", "target", "odometer"];
  for (const id of page2) {
    assert.ok(ids.includes(id), id + " registered");
    assert.ok(more.includes('show("' + id + '")'), id + " rendered through isHidden");
  }
  // Page 2 is a lazy chunk: Drive.jsx must not import it statically.
  assert.doesNotMatch(drive, /^import .*DriveMore/m);
  assert.match(drive, /import\("\.\/DriveMore\.jsx"\)/);
  // CSP: no inline style props on either page.
  assert.doesNotMatch(drive + more, /style=/);
});
