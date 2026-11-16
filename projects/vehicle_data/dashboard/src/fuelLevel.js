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

/**
 * Tank-level text for the fuel tile's sub-line: `tank ~49%`, or `tank ~49% (last)` when the
 * figure is a retained earlier reading. The `~` marks candidate quality, as the gear does.
 * @param {{available?: boolean, stale?: boolean, value?: *}|null} rec `fuel.tank_level` record
 * @param {{kind?: string, value?: *}|null} observation store `observationState` of the metric
 */
export function fuelLevelText(rec, observation) {
  const f = figure(level, rec, observation);
  return f ? `tank ~${f.value}%` + (f.live ? "" : " (last)") : "";
}

/**
 * Tank level and distance to empty on one short line: `tank ~49% · 126 mi`. Either part may be
 * missing. A single `(last)` closes the line when any shown part is a retained reading, which
 * keeps the retained form (`tank ~49% · 126 mi (last)`) short enough for the tile at 800 px.
 */
export function fuelGaugeText(levelRec, levelObservation, rangeRec, rangeObservation) {
  const tank = figure(level, levelRec, levelObservation);
  const miles = figure(range, rangeRec, rangeObservation);
  const parts = [];
  if (tank) parts.push(`tank ~${tank.value}%`);
  if (miles) parts.push(`${miles.value} mi`);
  if (!parts.length) return "";
  const retained = (tank && !tank.live) || (miles && !miles.live);
  return parts.join(" · ") + (retained ? " (last)" : "");
}
