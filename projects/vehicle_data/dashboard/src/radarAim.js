/**
 * The radar's two alignment axes, the angle format, and the physical reading of the signed
 * elevation estimate, shared by the Drive radar tile, the System radar card and the reference
 * dialog so each is declared once.
 *
 * Owner-reported (corrected 2026-11-16): the original June 2026 C1418-78 fix aimed the ACC
 * radar up by about 1°, which moved the stored elevation estimate from −1.26° up through zero.
 * It was first recorded as a downward tilt, before the mount's pivot axis was accounted for.
 * So a positive elevation means the radar is aimed higher than its calibration and is corrected
 * by tilting it down; a negative elevation means aimed low, tilt up. No such mapping
 * has been established for azimuth (left/right), so it gets no physical reading.
 * Provenance: projects/radar/findings/did_map.md (0x0845) and the radar README.
 */

import { DASH, fmtDelta } from "./format.js";

/** The axes in display order: metric `name`, card `label`, and the short `word` used in sentences. */
export const RADAR_AXES = Object.freeze([
  Object.freeze({ id: "elevation", name: "radar.alignment.elevation", label: "Vertical (elevation)", word: "vertical" }),
  Object.freeze({ id: "azimuth", name: "radar.alignment.azimuth", label: "Horizontal (azimuth)", word: "horizontal" }),
]);

/** Signed angle: `+0.16°`, `−0.02°`, `0.00°`; `—` for no number. */
export function fmtAngle(value) {
  return typeof value === "number" && isFinite(value) ? fmtDelta(value, 2) + "°" : DASH;
}

/** Tooltip for the change across the park (`radar_alignment[metric].parked`), tile and System card. */
export const PARKED_CHANGE_TITLE =
  "Last reading before the previous shut-down compared with the first reading after this start-up.";

/** Change across the park as a signed angle, or `—` when the broker sent none (older broker, no earlier reading). */
export function parkedChangeText(parked) {
  const ok = parked !== null && typeof parked === "object" && typeof parked.change === "number";
  return ok ? fmtAngle(parked.change) : DASH;
}

/** Below this the two-decimal display reads 0.00°, so the aim is called level. */
const LEVEL_BELOW_DEGREES = 0.005;

/**
 * @param {string} axisId `elevation` or `azimuth`
 * @param {number|null} value signed degrees, or null with nothing to read
 * @returns {string} `aimed high · tilt down`, `aimed low · tilt up`, `level`, or `` when unknown
 */
export function aimText(axisId, value) {
  if (axisId !== "elevation" || typeof value !== "number" || !Number.isFinite(value)) return "";
  if (Math.abs(value) < LEVEL_BELOW_DEGREES) return "level";
  return value > 0 ? "aimed high · tilt down" : "aimed low · tilt up";
}

/** The System card's wording for the axis without a physical mapping. */
export const AZIMUTH_AIM_UNMAPPED = "left/right direction not known";
