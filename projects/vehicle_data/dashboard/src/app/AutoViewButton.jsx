import { useComputed } from "@preact/signals";
import { settings, saveSettings } from "../settings.js";
import { activeView, evaluateAutoView } from "./derive.js";

const LABEL = "Switch between Drive and Parked automatically";

function toggleAuto() {
  const s = settings.peek();
  saveSettings({ ...s, auto: !s.auto, view: activeView.peek() });
  evaluateAutoView();
}

/**
 * Automatic view selection (design 5.2) as two swapping arrows. An icon, not an AUTO label, so
 * it never reads as an automatic mode of the neighbouring Dim or theme controls; green when on.
 */
export function AutoViewButton() {
  const cls = useComputed(() => "topbar__btn topbar__icon topbar__auto" + (settings.value.auto ? " topbar__btn--on" : ""));
  const pressed = useComputed(() => (settings.value.auto ? "true" : "false"));
  return (
    <button type="button" class={cls} aria-pressed={pressed} aria-label={LABEL} title={LABEL} onClick={toggleAuto}>
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M4 8h16M16 4l4 4-4 4M20 16H4M8 12l-4 4 4 4" />
      </svg>
    </button>
  );
}
