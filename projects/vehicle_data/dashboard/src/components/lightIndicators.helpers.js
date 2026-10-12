/** Presentation state for a current, driver-qualified light observation. */
export const LIGHT_STATES = Object.freeze({ ON: "on", OFF: "off" });

export function lightIndicatorState(observation, connectionState) {
  if (connectionState !== "live" || observation?.kind !== "live" ||
      typeof observation.value !== "boolean") return LIGHT_STATES.OFF;
  return observation.value ? LIGHT_STATES.ON : LIGHT_STATES.OFF;
}
