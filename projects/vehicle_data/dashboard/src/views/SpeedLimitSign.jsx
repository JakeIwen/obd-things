import { computed } from "@preact/signals";
import { liveValue } from "../app/derive.js";
import { DASH } from "../format.js";
import { speedLimitText } from "./driveSpeed.helpers.js";

// Hidden while the cluster shows no limit; the broker publishes nothing for the frame's 0.
const limit = computed(() => speedLimitText(liveValue("vehicle.speed_limit").value));
const hidden = computed(() => limit.value === DASH);
const label = computed(() => "Speed limit " + limit.value + " mph");
// Three digits only on a corrupt frame; shrink them rather than overrun the outline.
const valueCls = computed(() => "speed-limit__value" + (limit.value.length > 2 ? " speed-limit__value--wide" : ""));

/**
 * The cluster's speed limit as a US regulatory sign, between the Speed numeral and the ACC gap
 * readout. Outline only, so night mode stays dark, as on the page-2 Speed limit tile.
 */
export function SpeedLimitSign() {
  return (
    <div class="speed-limit" hidden={hidden} role="img" aria-label={label}>
      <svg class="speed-limit__sign" viewBox="0 0 48 60" aria-hidden="true">
        <rect class="speed-limit__outline" x="1.5" y="1.5" width="45" height="57" rx="5" />
        <text class="speed-limit__words" x="24" y="16">SPEED</text>
        <text class="speed-limit__words" x="24" y="27.5">LIMIT</text>
        <text class={valueCls} x="24" y="52">{limit}</text>
      </svg>
    </div>
  );
}
