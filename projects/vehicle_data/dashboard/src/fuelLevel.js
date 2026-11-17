import level from "../../fuel_level.json" with { type: "json" };
import range from "../../fuel_range.json" with { type: "json" };

export { level as FUEL_LEVEL, range as FUEL_RANGE };

/**
 * The shown figure of a candidate metric: the live value, else the retained last reading.
 * @returns {{value: number, live: boolean}|null} null when neither is a valid integer in range
 */
function figure(contract, rec, observation) {
  const live = Boolean(rec && rec.available === true && !rec.stale);
  const value = live ? rec.value : observation?.kind === "held" ? observation.value : null;
  if (!Number.isInteger(value) || value < contract.minimum || value > contract.maximum) return null;
  return { value, live };
}

/** Tank percent for the Fuel tank tile's value: `~49` (the tile adds the `%` unit); the `~` marks candidate quality. "" with no figure. */
export function tankValueText(levelRec, levelObservation) {
  const f = figure(level, levelRec, levelObservation);
  return f ? `~${f.value}` : "";
}

/**
 * Distance to empty for the Fuel tank tile's sub-line: `126 mi to empty`, with ` (last)` when the
 * tank figure or this one is a retained reading (`held`). "" with no figure.
 */
export function tankRangeText(levelRec, levelObservation, rangeRec, rangeObservation) {
  const miles = figure(range, rangeRec, rangeObservation);
  if (!miles) return "";
  const tank = figure(level, levelRec, levelObservation);
  return `${miles.value} mi to empty` + (!miles.live || (tank && !tank.live) ? " (last)" : "");
}

/** Whether the tank percent shown is a live reading (false: retained, or none). */
export function tankIsLive(levelRec, levelObservation) {
  const f = figure(level, levelRec, levelObservation);
  return Boolean(f && f.live);
}
