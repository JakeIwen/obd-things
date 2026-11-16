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
 * The two stacked lines above the gap readout in the Speed tile's third column, or null when the
 * state is unknown; display information only. `title` is the mode and set speed (`ACC 66`,
 * `Cruise 65`; mph is the tile's own unit), `status` the state word. The vehicle-ahead car icon
 * above the bars already says "vehicle ahead", so the text does not repeat it. The speed limit is
 * the sign beside the numeral (SpeedLimitSign.jsx), not a line here.
 * @returns {{title: string, status: string, saved: boolean}|null} `saved` marks a remembered
 *   (standby) set speed, which the column dims like the gap graphic
 */
export function speedAccLines(state, setSpeed, shown) {
  if (state === "off") return { title: "ACC off", status: "", saved: false };
  const acc = accTileModel(state, setSpeed, shown);
  if (!acc.sub) return null;
  const label = shown && shown.mode === "fixed" ? "Cruise" : "ACC";
  return {
    title: acc.value === DASH ? label : label + " " + acc.value,
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
