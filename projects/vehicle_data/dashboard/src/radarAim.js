/**
 * Physical reading of the signed radar elevation estimate, shared by the Drive radar tile and
 * the System radar card so the sign convention is declared once.
 *
 * Owner-reported, June 2026: the original C1418-78 fix was to tilt the ACC radar module
 * downward by about 1°, which moved the stored elevation estimate from −1.26° up through zero.
 * So a negative elevation means the radar is aimed higher than its calibration and is corrected
 * by tilting the module down; a positive elevation means aimed low, tilt up. No such mapping
 * has been established for azimuth (left/right), so it gets no physical reading.
 * Provenance: projects/radar/findings/did_map.md (0x0845) and the radar README.
 */

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
  return value < 0 ? "aimed high · tilt down" : "aimed low · tilt up";
}

/** The System card's wording for the axis without a physical mapping. */
export const AZIMUTH_AIM_UNMAPPED = "left/right direction not known";
