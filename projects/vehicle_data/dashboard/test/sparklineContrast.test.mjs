import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Why: owner request 2026-11-17 ("raise contrast"): the Drive sparkline strokes are canvas colours
// read from tokens, so the Dim theme's contrast is a property of the token values in the CSS.

const css = (name) => readFileSync(new URL(`../src/styles/${name}.css`, import.meta.url), "utf8");

/** Token → value from one theme sheet's declarations (later wins), resolving `var(--x)` chains. */
function tokens(...sheets) {
  const out = {};
  for (const text of sheets) {
    for (const m of text.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) out[m[1]] = m[2].trim();
  }
  const resolve = (name, depth = 0) => {
    const v = out[name];
    const ref = v && v.match(/^var\((--[a-z0-9-]+)\)$/);
    return ref && depth < 5 ? resolve(ref[1], depth + 1) : v;
  };
  return (name) => resolve(name);
}

function luminance(hex) {
  const n = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG 2.x contrast ratio of two opaque #rrggbb colours. */
function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const dim = tokens(css("palette"), css("night"));
const dark = tokens(css("palette"));
const light = tokens(css("palette"), css("light"));

test("Dim sparkline strokes reach 4.5:1 on both tile backgrounds", () => {
  for (const bg of ["--card", "--card-2"]) {
    assert.ok(contrast(dim("--spark-line"), dim(bg)) >= 4.5, `normal line on ${bg}`);
    assert.ok(contrast(dim("--spark-red"), dim(bg)) >= 4.5, `red line on ${bg}`);
  }
});

test("Dim red and normal sparkline strokes are different colours", () => {
  assert.notEqual(dim("--spark-line"), dim("--spark-red"));
});

test("heat numeral stops reach 4.5:1 on both tile backgrounds in every theme", () => {
  for (const [theme, t] of [["dark", dark], ["light", light], ["dim", dim]]) {
    for (const stop of ["--heat-yellow", "--heat-orange", "--heat-red"]) {
      for (const bg of ["--card", "--card-2"]) {
        assert.ok(contrast(t(stop), t(bg)) >= 4.5, `${theme} ${stop} on ${bg}`);
      }
    }
  }
});

test("the default dark and light themes keep a normal line of at least 3:1", () => {
  assert.ok(contrast(dark("--spark-line"), dark("--card")) >= 3);
  assert.ok(contrast(light("--spark-line"), light("--card")) >= 3);
});
