import { computed, effect, signal } from "@preact/signals-core";
import { monoNow as defaultMonoNow } from "./app/derive.js";
import * as store from "./store.js";
import { settings, saveSettings } from "./settings.js";
import { HEADLIGHT_METRICS, HEADLIGHT_OFF_HOLD_MS, THEME_REFRESH_MS } from "./theme.config.js";
import { applyHeadlightNudge, dimToggleSettings, headlightReading, headlightsOffWhileOn, nextThemeMode, themeSchedule, vanIsOn } from "./theme.helpers.js";

// store.clock is monotonic telemetry time; sun scheduling needs civil (wall) time.
const themeClock = signal(Date.now());
// True once the lights have read off, with the van on, for HEADLIGHT_OFF_HOLD_MS (see startHeadlightNudge).
const headlightNudge = signal(false);
export const displayTheme = computed(() => {
  const { themeMode } = settings.value;
  const schedule = themeSchedule(themeClock.value, themeMode, settings.value);
  return applyHeadlightNudge(schedule, themeMode, settings.value, themeClock.value, headlightNudge.value);
});

/** Hold the off state, then raise the nudge; lights on or any unknown drops it at once. */
function startHeadlightNudge({ now, monoNow, setTimeout, clearTimeout }) {
  // Read the store directly, never through the memoised `obs()`: its cached age does not move
  // between metric updates, and the hold timer must judge freshness at the moment it fires.
  const lightsOffWhileOn = () => {
    const mono = monoNow();
    const v = store.vehicle.value;
    const reading = headlightReading(HEADLIGHT_METRICS.map((name) => store.observationState(name, mono)),
      store.connection.value.state);
    return headlightsOffWhileOn(reading,
      vanIsOn({ state: v.state, confidence: v.confidence, ageMs: store.ageMs(v, mono) }));
  };
  let offSince = null;
  let timer = null;
  const drop = () => {
    offSince = null;
    clearTimeout(timer);
    timer = null;
    headlightNudge.value = false;
  };
  const stop = effect(() => {
    void store.clock.value;
    if (!lightsOffWhileOn()) { drop(); return; }
    if (offSince === null) offSince = now();
    if (timer === null && !headlightNudge.peek()) {
      timer = setTimeout(() => {
        timer = null;
        // The hold can outlast the readings' freshness; the watchdog may not have ticked yet.
        if (!lightsOffWhileOn()) { drop(); return; }
        headlightNudge.value = true;
        themeClock.value = now();
      }, Math.max(0, offSince + HEADLIGHT_OFF_HOLD_MS - now()));
    }
  });
  return () => { stop(); clearTimeout(timer); headlightNudge.value = false; };
}

/** Toggle what the screen shows (nudge included), so one tap always changes the display. */
export function toggleDim() {
  const now = Date.now();
  themeClock.value = now;
  saveSettings(dimToggleSettings(settings.peek(), now, displayTheme.peek().dim));
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
  monoNow = defaultMonoNow,
  setInterval = globalThis.setInterval,
  clearInterval = globalThis.clearInterval,
  setTimeout = globalThis.setTimeout,
  clearTimeout = globalThis.clearTimeout,
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
  const stopNudge = startHeadlightNudge({ now, monoNow, setTimeout, clearTimeout });
  const timer = setInterval(refresh, THEME_REFRESH_MS);
  document.addEventListener("visibilitychange", refresh);
  return () => {
    dispose();
    stopNudge();
    clearInterval(timer);
    document.removeEventListener("visibilitychange", refresh);
  };
}
