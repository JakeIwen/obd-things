export const THEME_MODES = Object.freeze(["auto", "light", "dark"]);
export const MINUTE_MS = 60_000;
export const DAY_MS = 24 * 60 * MINUTE_MS;
export const THEME_REFRESH_MS = MINUTE_MS;

// The owner specified Mountain time, not a live location. Denver is a fixed reference:
// longitude moves sunrise about 4 min/degree; the broad margins absorb ordinary travel.
export const SUN_REFERENCE = Object.freeze({
  latitude: 39.7392,
  longitude: -104.9903,
  timeZone: "America/Denver",
  zenith: 90.833,
});
export const SUN_OFFSETS_MINUTES = Object.freeze({
  darkBeforeSunset: -90,
  lightAfterSunrise: 90,
  dimAfterSunset: 30,
  undimBeforeSunrise: -30,
});
