import contract from "../../fuel_level.json" with { type: "json" };

export { contract as FUEL_LEVEL };

/**
 * Tank-level text for the fuel tile's sub-line: `tank ~49%`, or `tank ~49% (last)` when the
 * figure is a retained earlier reading. The `~` marks candidate quality, as the gear does.
 * @param {{available?: boolean, stale?: boolean, value?: *}|null} rec `fuel.tank_level` record
 * @param {{kind?: string, value?: *}|null} observation store `observationState` of the metric
 */
export function fuelLevelText(rec, observation) {
  const live = Boolean(rec && rec.available === true && !rec.stale);
  const value = live ? rec.value : observation?.kind === "held" ? observation.value : null;
  if (!Number.isInteger(value) || value < contract.minimum || value > contract.maximum) return "";
  return `tank ~${value}%` + (live ? "" : " (last)");
}
