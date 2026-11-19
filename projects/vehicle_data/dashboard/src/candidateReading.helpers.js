/**
 * Page-2 tiles for candidate-quality readings that carry no driver-qualified claim
 * (intake air temperature, A/C high-side pressure). The generic tile model treats only
 * `verified` and `observed_alfa_scale` as live (store.js `DRIVER_QUALITIES`) and retains no
 * candidate reading, so these two read the record directly, like the fuel-rate tile:
 * live while current, `—` otherwise, never a retained or stale figure. Pure and node-testable.
 */

import { DASH, fmtUnit, fmtValue } from "./format.js";

/** The metrics shown this way; the format tables own their decimals and units. */
export const CANDIDATE_READING_METRICS = Object.freeze([
  "engine.intake_air_temperature",
  "hvac.ac_high_side_pressure",
]);

/**
 * @param {string} name metric name in `CANDIDATE_READING_METRICS`
 * @param {{available?: boolean, stale?: boolean, value?: *}|null} rec the metric's store record
 * @returns {{kind: 'live'|'off', text: string, unit: string}}
 */
export function candidateReadingModel(name, rec) {
  const current = rec && rec.available === true && !rec.stale
    && typeof rec.value === "number" && Number.isFinite(rec.value);
  return current
    ? { kind: "live", text: fmtValue(name, rec.value), unit: fmtUnit(name, rec.unit) }
    : { kind: "off", text: DASH, unit: "" };
}
