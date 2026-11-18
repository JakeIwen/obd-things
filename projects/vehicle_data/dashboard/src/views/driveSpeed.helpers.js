import { accTileModel } from "../accDisplay.helpers.js";
import { DASH } from "../format.js";

/** Short status word per ACC state for the Speed tile's gap column (the Sensors tile keeps the long ones). */
const STATUS_WORDS = Object.freeze({
  ready: "ready",
  engaged: "engaged",
  override: "override",
  standby: "standby",
});

/**
 * The stacked lines above the gap readout in the Speed tile's third column (label, set speed when
 * there is one, state word), or null when the state is unknown; display information only. `label`
 * is the mode (`ACC`, `Cruise`), `value` the set speed shown below it as a big number (mph is the
 * tile's own unit; empty without one),
 * `status` the state word. The vehicle-ahead car icon
 * above the bars already says "vehicle ahead", so the text does not repeat it. The speed limit is
 * the sign beside the numeral (SpeedLimitSign.jsx), not a line here.
 * @returns {{label: string, value: string, status: string, saved: boolean}|null} `saved` marks a remembered
 *   (standby) set speed, which the column dims like the gap graphic
 */
export function speedAccLines(state, setSpeed, shown) {
  if (state === "off") return { label: "ACC off", value: "", status: "", saved: false };
  const acc = accTileModel(state, setSpeed, shown);
  if (!acc.sub) return null;
  const label = shown && shown.mode === "fixed" ? "Cruise" : "ACC";
  return {
    label,
    value: acc.value === DASH ? "" : acc.value,
    status: STATUS_WORDS[state],
    saved: state === "standby",
  };
}

/**
 * Speed-limit sign text: whole mph, or `—` when the cluster shows no limit (the broker never
 * publishes the frame's 0, so "none" arrives as no live value).
 * @param {number|null} mph live `vehicle.speed_limit`
 */
export function speedLimitText(mph) {
  return typeof mph === "number" && Number.isFinite(mph) && mph > 0 ? String(Math.round(mph)) : DASH;
}
