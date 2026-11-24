import { fmtFixed, roundHalfAway } from "./format.js";

export const BAROMETRIC_PRESSURE_METRIC = "engine.barometric_pressure";

const STANDARD_SEA_LEVEL_KPA = 101.325;
const ATMOSPHERE_HEIGHT_FT = 145366.45;
const PRESSURE_EXPONENT = 0.190284;
const DISPLAY_STEP_FT = 10;

export function pressureAltitudeFeet(kPa) {
  if (!Number.isFinite(kPa) || kPa <= 0) return null;
  return ATMOSPHERE_HEIGHT_FT * (1 - (kPa / STANDARD_SEA_LEVEL_KPA) ** PRESSURE_EXPONENT);
}

export function pressureAltitudeDisplay(observation, connectionState) {
  if (observation?.kind !== "live" && observation?.kind !== "held") return null;
  const feet = pressureAltitudeFeet(observation.value);
  if (feet === null) return null;
  const rounded = roundHalfAway(feet / DISPLAY_STEP_FT) * DISPLAY_STEP_FT;
  return {
    text: `${fmtFixed(rounded, 0, true)} ft`,
    stale: observation.kind !== "live" || connectionState !== "live",
  };
}
