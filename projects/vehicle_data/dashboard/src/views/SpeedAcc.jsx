import { computed } from "@preact/signals";
import { AccDistance } from "../components/AccDistance.jsx";
import { currentAcc } from "../accDisplay.js";
import { speedAccLines } from "./driveSpeed.helpers.js";

// Display records expire in the store; the current ACC state gates every line, as for the bars.
const lines = computed(() => {
  const acc = currentAcc.value;
  return speedAccLines(acc.state, acc.setSpeed, acc);
});
const hidden = computed(() => lines.value === null && currentAcc.value.bars === null);
const className = computed(() => "speed-acc" + (lines.value?.saved ? " speed-acc--saved" : ""));
const title = computed(() => lines.value?.title ?? "");
const status = computed(() => lines.value?.status ?? "");

/** Third column of the Speed tile: the ACC/Cruise text stacked above the gap readout. */
export function SpeedAcc() {
  return (
    <div class={className} hidden={hidden}>
      <span class="speed-acc__title">{title}</span>
      <span class="speed-acc__status">{status}</span>
      <AccDistance />
    </div>
  );
}
