/**
 * Estimated fuel figures as the Drive and History pages show them; pure and node-testable.
 * `engine.fuel_rate` is candidate quality (scaled to match the cluster, not yet checked
 * against a fill-up), so every figure carries "≈" and none is a driver-qualified tile value.
 * Trip and 7/30-day economy come from the broker's history `fuel_economy` objects.
 */

import { DASH, fmtDate, parseDate } from "./format.js";

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

/** Below this speed an instant economy figure means nothing. */
export const INSTANT_MIN_MPH = 5;
/** Instant figures above this read as `99+`, like the cluster's capped display. */
export const INSTANT_CAP_MPG = 99;

const NO_FIGURE = Object.freeze({ value: DASH, unit: "" });
const figure = (text) => ({ value: "≈ " + text, unit: "mpg" });

/**
 * One history `fuel_economy` object as a Speed-tile figure, `{value: "≈ 14.1", unit: "mpg"}`, or
 * `—` with no unit when it holds none (too few running samples, a trip from before fuel was
 * recorded, or no fuel used).
 */
export function mpgFigure(economy) {
  if (!economy || economy.available !== true || !isNum(economy.mpg) || economy.mpg < 0) return NO_FIGURE;
  return figure(economy.mpg.toFixed(1));
}

/** `≈ 14.1 mpg` from one history `fuel_economy` object, or null when it holds no figure. */
export function mpgText(economy) {
  const f = mpgFigure(economy);
  return f.unit ? f.value + " " + f.unit : null;
}

/** Live fuel rate as `≈ 1.2` (gal/h), or `—` unless the record is current. */
export function fuelRateText(rec) {
  if (!rec || rec.available !== true || rec.stale || !isNum(rec.value) || rec.value < 0) return DASH;
  return "≈ " + rec.value.toFixed(1);
}

/**
 * Instant economy in mpg while moving on a current fuel record; Infinity during the fuel cut
 * the engine computer makes when coasting in gear; null when stopped or either value is missing.
 * @param {number|null} mph live `vehicle.speed`
 * @param {{available?: boolean, stale?: boolean, value?: *}|null} rec `engine.fuel_rate` record
 */
function instantMpg(mph, rec) {
  if (!isNum(mph) || mph < INSTANT_MIN_MPH || fuelRateText(rec) === DASH) return null;
  return rec.value === 0 ? Infinity : mph / rec.value;
}
const capped = (mpg) => (mpg > INSTANT_CAP_MPG ? INSTANT_CAP_MPG + "+" : String(Math.round(mpg)));

/** Speed-tile figure `{value: "≈ 19", unit: "mpg"}`; `≈ 99+` from the cap up and while coasting. */
export function instantFigure(mph, rec) {
  const mpg = instantMpg(mph, rec);
  return mpg === null ? NO_FIGURE : figure(capped(mpg));
}

/** Sub-line under the live rate on page 2: `now ≈ 20 mpg`, `coasting, no fuel`, or empty. */
export function instantText(mph, rec) {
  const mpg = instantMpg(mph, rec);
  if (mpg === null) return "";
  return mpg === Infinity ? "coasting, no fuel" : "now ≈ " + capped(mpg) + " mpg";
}

/**
 * History line for the broker's 7- and 30-day figures, e.g.
 * `Fuel economy ≈ 15.2 mpg over 7 days · ≈ 15.0 mpg over 30 days`; null when neither has one.
 * A window the broker started late, at the first recorded fuel rate, reads `since Oct 20`;
 * when both did, they hold the same figure and it is shown once.
 * @param {{days_7?: object, days_30?: object}|null} summary history `fuel_economy`
 * @param {number} [nowMs] wall clock, for leaving out the current year
 */
export function windowsText(summary, nowMs = Date.now()) {
  if (!summary || summary.available !== true) return null;
  const parts = [];
  for (const [key, days] of [["days_7", 7], ["days_30", 30]]) {
    const text = mpgText(summary[key]);
    const since = text && parseDate(summary[key].since);
    const label = since ? "since " + fmtDate(since, new Date(nowMs).getFullYear()) : "over " + days + " days";
    if (text && !parts.includes(text + " " + label)) parts.push(text + " " + label);
  }
  return parts.length ? "Fuel economy " + parts.join(" · ") : null;
}
