import { useComputed } from "@preact/signals";
import { settings } from "../settings.js";
import { cycleTheme } from "../theme.js";
import { themeButtonLabel } from "../theme.helpers.js";

const ICONS = {
  auto: <><circle cx="12" cy="12" r="8" /><path d="M12 4a8 8 0 0 0 0 16Z" fill="currentColor" stroke="none" /></>,
  light: <><circle cx="12" cy="12" r="4" /><path d="M12 1v3m0 16v3M1 12h3m16 0h3M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2" /></>,
  dark: <path d="M20 15A8.5 8.5 0 0 1 9 4a8.5 8.5 0 1 0 11 11Z" />,
};

/** A distinct icon, never a second bare AUTO beside the view-selection button. */
export function ThemeButton() {
  const label = useComputed(() => themeButtonLabel(settings.value.themeMode));
  return (
    <button type="button" class="topbar__btn topbar__theme" aria-label={label} title={label} onClick={cycleTheme}>
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
        {ICONS[settings.value.themeMode]}
      </svg>
    </button>
  );
}
