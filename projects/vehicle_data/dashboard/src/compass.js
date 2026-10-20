import contract from "../../compass.json" with { type: "json" };

export { contract as COMPASS };

export function compassLetters(value) {
  return Number.isInteger(value) && value >= 0 && value < contract.directions.length
    ? contract.directions[value] : "";
}

/** Retain the actual last report; a disconnect can only dim it, never refresh it. */
export function compassDisplay(observation, connectionState) {
  const letters = observation?.kind === "live" || observation?.kind === "held"
    ? compassLetters(observation.value) : "";
  return {
    letters,
    stale: !letters || observation.kind !== "live" || connectionState !== "live",
  };
}
