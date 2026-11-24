import { test } from "node:test";
import assert from "node:assert/strict";
import { fitLabelSize } from "../src/tileLabel.helpers.js";

// Why: owner request 2026-11-23: titles ~50 % larger (14 to 21 px) unless that would wrap or ellipsize.

const base = { max: 21, min: 14 };

test("a title that fits at the largest size keeps it", () => {
  assert.equal(fitLabelSize({ ...base, natural: 120, available: 200 }), 21);
  assert.equal(fitLabelSize({ ...base, natural: 200, available: 200 }), 21);
});

test("a title that overflows shrinks in half-pixel steps to the largest size that fits", () => {
  // 21 px is 252 wide; 200 of room allows 21 * 200 / 252 = 16.67, so 16.5.
  const size = fitLabelSize({ ...base, natural: 252, available: 200 });
  assert.equal(size, 16.5);
  assert.ok((252 * size) / 21 <= 200, "the shrunk title fits");
});

test("the size never drops below the old 14 px", () => {
  assert.equal(fitLabelSize({ ...base, natural: 400, available: 100 }), 14);
});

test("an unmeasurable title (hidden, zero width, NaN) keeps the largest size", () => {
  assert.equal(fitLabelSize({ ...base, natural: 0, available: 0 }), 21);
  assert.equal(fitLabelSize({ ...base, natural: 100, available: 0 }), 21);
  assert.equal(fitLabelSize({ ...base, natural: NaN, available: 100 }), 21);
});
