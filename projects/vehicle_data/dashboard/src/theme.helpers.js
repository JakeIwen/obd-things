import { DAY_MS, MINUTE_MS, SUN_OFFSETS_MINUTES, THEME_MODES } from "./theme.config.js";
import { DRIVING_STATES, MAX_AUTOMATIC_STATE_AGE_MS } from "./settings.js";
import { mountainDay, sunTimes } from "./sunTimes.js";

export function sunBoundaries(day) {
  const { sunrise, sunset } = sunTimes(day);
  const offset = SUN_OFFSETS_MINUTES;
  return {
    undim: sunrise + offset.undimBeforeSunrise * MINUTE_MS,
    light: sunrise + offset.lightAfterSunrise * MINUTE_MS,
    dark: sunset + offset.darkBeforeSunset * MINUTE_MS,
    dim: sunset + offset.dimAfterSunset * MINUTE_MS,
  };
}

/** Half-open windows: each boundary applies its new state at that exact millisecond. */
export function themeSchedule(now, mode, { dim = false, dimOverrideUntil = null } = {}) {
  if (mode === "light" || mode === "dark") {
    return { theme: mode, dim: Boolean(dim), nextBoundary: null, nextDimBoundary: null };
  }
  const day = mountainDay(now);
  const today = sunBoundaries(day);
  const tomorrow = sunBoundaries(day + DAY_MS);
  const nextDimBoundary = [today.undim, today.dim, tomorrow.undim].find((time) => time > now);
  const nextBoundary = [...Object.values(today), ...Object.values(tomorrow)].filter((time) => time > now)
    .reduce((earliest, time) => Math.min(earliest, time), Infinity);
  const overridden = Number.isFinite(dimOverrideUntil) && dimOverrideUntil > now;
  return {
    theme: now >= today.light && now < today.dark ? "light" : "dark",
    dim: overridden ? Boolean(dim) : now < today.undim || now >= today.dim,
    nextBoundary: overridden ? Math.min(nextBoundary, dimOverrideUntil) : nextBoundary,
    nextDimBoundary,
  };
}

/**
 * One DIM tap. `displayedDim` is what the screen shows now (the headlight nudge can differ from
 * the schedule); the tap inverts that and, in Auto, holds it until the next dim boundary.
 */
export function dimToggleSettings(settings, now, displayedDim) {
  const current = themeSchedule(now, settings.themeMode, settings);
  return {
    ...settings,
    dim: !(displayedDim ?? current.dim),
    dimOverrideUntil: settings.themeMode === "auto" ? current.nextDimBoundary : null,
  };
}

export function nextThemeMode(mode) {
  return THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
}

export function themeButtonLabel(mode) {
  return `Theme: ${mode === "auto" ? "Auto (Denver sun schedule)" : mode}. Tap for ${nextThemeMode(mode)} theme.`;
}

/**
 * Headlight state from current observations (`observationState` results): "on" when any is
 * live true, "off" when all are live false, else "unknown" (missing, held or stale).
 */
export function headlightReading(observations, connectionState) {
  if (connectionState !== "live" || !observations.length) return "unknown";
  const live = (o) => o?.kind === "live" && typeof o.value === "boolean";
  if (observations.some((o) => live(o) && o.value)) return "on";
  return observations.every(live) ? "off" : "unknown";
}

/** The van is on: verified, fresh driving-class vehicle state (same bar as automatic view). */
export function vanIsOn({ state, confidence, ageMs } = {}) {
  return confidence === "verified" && DRIVING_STATES.has(state) &&
    Number.isFinite(ageMs) && ageMs >= 0 && ageMs <= MAX_AUTOMATIC_STATE_AGE_MS;
}

/** True while the lights are known off with the van on; the caller applies the hold. */
export function headlightsOffWhileOn(reading, vanOn) {
  return reading === "off" && Boolean(vanOn);
}

/**
 * Auto mode only: a held headlights-off nudge shows the bright (light, not dim) palette. A
 * manual theme and an active owner DIM override are never overridden.
 */
export function applyHeadlightNudge(schedule, mode, { dimOverrideUntil = null } = {}, now, nudge) {
  if (!nudge || mode !== "auto") return schedule;
  if (Number.isFinite(dimOverrideUntil) && dimOverrideUntil > now) return schedule;
  return { ...schedule, theme: "light", dim: false };
}
