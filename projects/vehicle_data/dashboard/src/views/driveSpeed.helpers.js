import { accTileModel } from "./driveMore.helpers.js";

/** Two short lines in the existing Speed footer; display information only. */
export function speedFooter(limit, state, setSpeed, shown) {
  const lines = [];
  if (typeof limit === "number" && Number.isFinite(limit) && limit > 0) lines.push("limit " + Math.round(limit) + " mph");
  const acc = accTileModel(state, setSpeed, shown);
  if (state === "off") lines.push("ACC off");
  else if (acc.sub) {
    const label = shown && shown.mode === "fixed" ? "Cruise" : "ACC";
    const speed = acc.unit ? " " + acc.value + " " + acc.unit : "";
    const status = state === "override" ? "override" : state;
    const ahead = shown && shown.mode === "adaptive" && shown.lead === true &&
      (state === "engaged" || state === "override") ? " · vehicle ahead" : "";
    lines.push(label + speed + " · " + status + ahead);
  }
  return lines.join("\n");
}
