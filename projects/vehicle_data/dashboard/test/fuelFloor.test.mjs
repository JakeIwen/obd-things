import test from "node:test";
import assert from "node:assert/strict";
import { FUEL_FLOOR } from "../src/fuelFloor.js";
import { floorBlock, rescale, tankTileModel } from "../src/fuelFloor.helpers.js";
import { tankRangeText, tankValueText } from "../src/fuelLevel.js";

const live = (value) => ({ available: true, stale: false, value });
const held = (value) => [{ available: false }, { kind: "held", value }];
const NO_EPISODE = { floor: 0, gal_per_point: 0.182582, episode: null };
const model = (over) => tankTileModel({
  levelRec: live(12), levelObs: null, rangeRec: live(40), rangeObs: null, block: NO_EPISODE, ...over,
});

test("the contract the dashboard shares names the status key", () => {
  assert.equal(FUEL_FLOOR.status_key, "fuel_floor");
  assert.equal(FUEL_FLOOR.implausible_raw, -30);
});

test("with floor 0 and no episode the tile is exactly the plain one", () => {
  for (const block of [NO_EPISODE, null, undefined, {}, { floor: "x" }, { floor: 3 }]) {
    const m = model({ block });
    assert.equal(m.value, tankValueText(live(12), null));
    assert.equal(m.sub, tankRangeText(live(12), null, live(40), null));
    assert.equal(m.negative, false);
    assert.equal(m.live, true);
  }
  const [rec, obs] = held(12);
  const h = model({ levelRec: rec, levelObs: obs });
  assert.equal(h.live, false);
  assert.equal(h.value, "~12");
  assert.equal(h.sub, "40 mi to empty (last)");
});

test("a floor below zero rescales the van's reading and shows the van's own percent", () => {
  const m = model({ block: { floor: -10, episode: null }, levelRec: live(10) });
  assert.equal(m.value, `~${Math.round(rescale(10, -10))}`);
  assert.equal(m.value, "~18");
  assert.equal(m.sub, "40 mi to empty · van 10%");
  assert.equal(m.negative, false);
  assert.equal(m.live, true);
  assert.equal(rescale(100, -10), 100);
  assert.equal(rescale(-10, -10), 0);
});

test("the van's own percent stands alone without a range figure, and a retained reading is held", () => {
  const [rec, obs] = held(30);
  const m = model({ block: { floor: -4, episode: null }, levelRec: rec, levelObs: obs, rangeRec: null });
  assert.equal(m.sub, "van 30%");
  assert.equal(m.live, false);
  assert.equal(model({ block: { floor: -4, episode: null }, levelRec: null }).value, "");
});

test("an open episode shows the estimate, the gallons past empty and a red negative numeral", () => {
  const block = { floor: 0, episode: { gallons: 1.44, adjusted: -8.2, raw: -8.2, implausible: false } };
  const m = model({ block, levelRec: null, rangeRec: live(7) });
  assert.equal(m.value, "~-8");
  assert.equal(m.sub, "est. · 1.4 gal past empty · 7 mi to empty");
  assert.equal(m.negative, true);
  assert.equal(m.live, true);
  assert.equal(model({ block, levelRec: null, rangeRec: null }).sub, "est. · 1.4 gal past empty");
});

test("red follows the rounded numeral and never shows a negative zero", () => {
  const near = (adjusted) => model({ block: { floor: 0, episode: { gallons: 0.01, adjusted } } });
  assert.equal(near(-0.4).value, "~0");
  assert.equal(near(-0.4).negative, false);
  assert.equal(near(-0.6).value, "~-1");
  assert.equal(near(-0.6).negative, true);
});

test("an episode above the floor in force reads as positive and is not red", () => {
  const block = { floor: -6, episode: { gallons: 0.2, adjusted: 3.1 } };
  const m = model({ block });
  assert.equal(m.value, "~3");
  assert.equal(m.negative, false);
});

test("malformed blocks fall back to the neutral one", () => {
  assert.deepEqual(floorBlock({ floor: -2, episode: { adjusted: "x", gallons: 1 } }), { floor: -2, episode: null });
  assert.deepEqual(floorBlock({ floor: -2, episode: { adjusted: -3, gallons: -1 } }), { floor: -2, episode: null });
  assert.deepEqual(floorBlock({ floor: Infinity }), { floor: 0, episode: null });
  assert.deepEqual(floorBlock({ floor: -2, episode: { adjusted: -3, gallons: 1, implausible: true } }),
    { floor: -2, episode: { adjusted: -3, gallons: 1, implausible: true } });
});

test("a stale status delivery shows the open estimate held, not live", () => {
  const block = { floor: 0, episode: { gallons: 1.44, adjusted: -8.2, implausible: false } };
  assert.equal(model({ block, stale: true }).live, false);
  assert.equal(model({ block, stale: false }).live, true);
  assert.equal(model({ block, stale: true }).value, "~-8");
});

test("an implausible episode says the estimate is unreliable", () => {
  const block = { floor: 0, episode: { gallons: 5.5, adjusted: -30, implausible: true } };
  assert.match(model({ block }).sub, /^estimate unreliable/);
});
