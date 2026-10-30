import { computed, effect, signal } from "@preact/signals-core";
import { settings, saveSettings } from "./settings.js";
import { THEME_REFRESH_MS } from "./theme.config.js";
import { dimToggleSettings, nextThemeMode, themeSchedule } from "./theme.helpers.js";

// store.clock is monotonic telemetry time; sun scheduling needs civil (wall) time.
const themeClock = signal(Date.now());
export const displayTheme = computed(() => themeSchedule(themeClock.value, settings.value.themeMode, settings.value));

export function toggleDim() {
  const now = Date.now();
  themeClock.value = now;
  saveSettings(dimToggleSettings(settings.peek(), now));
}

export function cycleTheme() {
  themeClock.value = Date.now();
  const current = settings.peek();
  saveSettings({ ...current, themeMode: nextThemeMode(current.themeMode), dimOverrideUntil: null });
}

/** One minute timer, refreshed immediately on wake; no dependency on the 1 Hz telemetry clock. */
export function startTheme({
  document = globalThis.document,
  now = Date.now,
  setInterval = globalThis.setInterval,
  clearInterval = globalThis.clearInterval,
} = {}) {
  const refresh = () => { themeClock.value = now(); };
  refresh();
  let applied = "";
  const dispose = effect(() => {
    const { theme, dim } = displayTheme.value;
    const key = `${theme}:${dim}`;
    if (applied === key) return;
    const root = document.documentElement;
    root.classList.toggle("light", theme === "light");
    root.classList.toggle("dim", dim);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", getComputedStyle(root).getPropertyValue("--bg").trim());
    applied = key;
  });
  const timer = setInterval(refresh, THEME_REFRESH_MS);
  document.addEventListener("visibilitychange", refresh);
  return () => {
    dispose();
    clearInterval(timer);
    document.removeEventListener("visibilitychange", refresh);
  };
}
