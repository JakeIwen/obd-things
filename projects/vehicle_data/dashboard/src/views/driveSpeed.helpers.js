import { accTileModel } from "../accDisplay.helpers.js";
import { DASH } from "../format.js";

/**
 * The cruise line in the Speed footer, or "" when the state is unknown; display information only.
 * The speed limit is the sign beside the numeral (SpeedLimitSign.jsx), not a footer line.
 */
export function speedFooter(state, setSpeed, shown) {
  if (state === "off") return "ACC off";
  const acc = accTileModel(state, setSpeed, shown);
  if (!acc.sub) return "";
  const label = shown && shown.mode === "fixed" ? "Cruise" : "ACC";
  const speed = acc.unit ? " " + acc.value + " " + acc.unit : "";
  const status = state === "override" ? "override" : state === "standby" ? "standby · saved" : state;
  const ahead = shown && shown.mode === "adaptive" && shown.lead === true &&
    (state === "engaged" || state === "override") ? " · vehicle ahead" : "";
  return label + speed + " · " + status + ahead;
}

/**
 * Speed-limit sign text: whole mph, or `—` when the cluster shows no limit (the broker never
 * publishes the frame's 0, so "none" arrives as no live value).
 * @param {number|null} mph live `vehicle.speed_limit`
 */
export function speedLimitText(mph) {
  return typeof mph === "number" && Number.isFinite(mph) && mph > 0 ? String(Math.round(mph)) : DASH;
}
