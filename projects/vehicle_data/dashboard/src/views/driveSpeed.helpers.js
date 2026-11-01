import { accTileModel } from "../accDisplay.helpers.js";
import { DASH } from "../format.js";

/** Two short lines in the existing Speed footer; display information only. */
export function speedFooter(limit, state, setSpeed, shown) {
  const lines = [];
  if (typeof limit === "number" && Number.isFinite(limit) && limit > 0) lines.push("limit " + Math.round(limit) + " mph");
  const acc = accTileModel(state, setSpeed, shown);
  if (state === "off") lines.push("ACC off");
  else if (acc.sub) {
    const label = shown && shown.mode === "fixed" ? "Cruise" : "ACC";
    const speed = acc.unit ? " " + acc.value + " " + acc.unit : "";
    const status = state === "override" ? "override" : state === "standby" ? "standby · saved" : state;
    const ahead = shown && shown.mode === "adaptive" && shown.lead === true &&
      (state === "engaged" || state === "override") ? " · vehicle ahead" : "";
    lines.push(label + speed + " · " + status + ahead);
  }
  return lines.join("\n");
}

/**
 * Speed-limit sign text: whole mph, or `—` when the cluster shows no limit (the broker never
 * publishes the frame's 0, so "none" arrives as no live value).
 * @param {number|null} mph live `vehicle.speed_limit`
 */
export function speedLimitText(mph) {
  return typeof mph === "number" && Number.isFinite(mph) && mph > 0 ? String(Math.round(mph)) : DASH;
}
