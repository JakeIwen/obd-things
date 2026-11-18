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

// Headlights-off nudge (owner request 2026-11-17, like the cluster): in Auto, a bright palette
// until the headlights are on. The off reading must hold this long before it overrides.
export const HEADLIGHT_OFF_HOLD_MS = 5_000;
// Metrics that count as "headlights on" (lighting.json): the 0x73E parking/headlights bit
// (it cannot tell parking lights from low beam) and high beam. DRL does not set either.
export const HEADLIGHT_METRICS = Object.freeze(["lights.parking_headlights_on", "lights.high_beam_on"]);
