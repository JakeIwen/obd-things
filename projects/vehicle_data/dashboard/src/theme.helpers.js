import { DAY_MS, MINUTE_MS, SUN_OFFSETS_MINUTES, THEME_MODES } from "./theme.config.js";
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

export function dimToggleSettings(settings, now) {
  const current = themeSchedule(now, settings.themeMode, settings);
  return {
    ...settings,
    dim: !current.dim,
    dimOverrideUntil: settings.themeMode === "auto" ? current.nextDimBoundary : null,
  };
}

export function nextThemeMode(mode) {
  return THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
}

export function themeButtonLabel(mode) {
  return `Theme: ${mode === "auto" ? "Auto (Denver sun schedule)" : mode}. Tap for ${nextThemeMode(mode)} theme.`;
}
