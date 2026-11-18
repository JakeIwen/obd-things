/**
 * Pure rules for a tile that shades yellow → orange → red as a value climbs: its numeral, and its
 * sparkline wherever the line is above the range's start. Display only: nothing here raises a
 * warning, chime or card colour. The colours are the `--heat-*` palette tokens; `components/Tile.jsx`
 * mixes them for the numeral (read through `src/paletteTokens.js`) and `components/Sparkline.jsx`
 * lays them along the line's value axis.
 */

/** Metric → the value range it shades across: yellow at `from`, orange midway, red at `to` and above. */
export const TILE_HEAT = Object.freeze({
  // Alternator field duty near 100 % means the alternator is at its limit (owner request 2026-11-17).
  "generator.field_duty": Object.freeze({ from: 60, to: 100 }),
});

/** The palette tokens for the three stops, in order. */
export const HEAT_TOKENS = Object.freeze(["--heat-yellow", "--heat-orange", "--heat-red"]);

/** The metric's heat range, or null when its numeral keeps the normal colour. */
export function heatRangeFor(name) {
  return Object.hasOwn(TILE_HEAT, name) ? TILE_HEAT[name] : null;
}

/** Where a value sits in the metric's heat range, 0 (at `from`) to 1 (at or above `to`); null below it. */
export function heatFraction(name, value) {
  const range = heatRangeFor(name);
  if (range === null || typeof value !== "number" || !Number.isFinite(value) || value < range.from) return null;
  return Math.min(1, (value - range.from) / (range.to - range.from));
}

function parseHex(text) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(typeof text === "string" ? text.trim() : "");
  if (!m) return null;
  const hex = m[1].length === 3 ? m[1].replace(/./g, (c) => c + c) : m[1];
  return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
}

/**
 * The colour at `fraction` along three hex stops (0 → first, 0.5 → second, 1 → third), as
 * `rgb(r, g, b)`; "" when a stop is not a hex colour, so the numeral falls back to its normal colour.
 * @param {string[]} stops three `#rrggbb` (or `#rgb`) colours
 * @param {number} fraction 0..1
 */
export function mixHeat(stops, fraction) {
  const rgb = Array.isArray(stops) && stops.length === 3 ? stops.map(parseHex) : [null];
  if (rgb.some((c) => c === null) || typeof fraction !== "number" || !Number.isFinite(fraction)) return "";
  const f = Math.max(0, Math.min(1, fraction));
  const [a, b, t] = f <= 0.5 ? [rgb[0], rgb[1], f * 2] : [rgb[1], rgb[2], (f - 0.5) * 2];
  const mix = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return "rgb(" + mix.join(", ") + ")";
}
