import { useComputed } from "@preact/signals";
import { currentAcc } from "../accDisplay.js";
import { accDistanceLabel } from "../accDisplay.helpers.js";

// The bars sit below the cluster-style vehicle-ahead icon, so they start at this SVG offset.
const BARS_TOP = 17;

function DistanceBar({ level }) {
  const className = useComputed(() => "acc-distance__bar" +
    (currentAcc.value.bars >= level ? " acc-distance__bar--selected" : ""));
  return <rect class={className} x={4 + (level - 1) * 3} y={BARS_TOP + (4 - level) * 9}
    width={52 - (level - 1) * 6} height="5" rx="1" />;
}

/** Rear view of a car above the bars, shown while the cluster shows its vehicle-ahead icon.
 * Its space stays reserved so the bars never move when it appears. */
function LeadVehicle() {
  const className = useComputed(() => "acc-distance__lead" +
    (currentAcc.value.lead === true ? " acc-distance__lead--on" : ""));
  return (
    <g class={className}>
      <path d="M20 6 L23 1 H37 L40 6 Z" />
      <rect x="16" y="6" width="28" height="7" rx="1.5" />
      <rect x="17.5" y="12" width="5" height="3" rx="0.8" />
      <rect x="37.5" y="12" width="5" height="3" rx="0.8" />
      <path class="acc-distance__lead-window" d="M22.4 5.2 L24.4 2.4 H35.6 L37.6 5.2 Z" />
    </g>
  );
}

/** The selected setting and the cluster's vehicle-ahead icon; never a distance measurement. */
export function AccDistance() {
  const hidden = useComputed(() => currentAcc.value.bars === null);
  const label = useComputed(() => accDistanceLabel(currentAcc.value.bars, currentAcc.value.lead));
  const count = useComputed(() => currentAcc.value.bars);
  const className = useComputed(() => "acc-distance" +
    (["ready", "standby"].includes(currentAcc.value.state) ? " acc-distance--saved" : ""));
  return (
    <div class={className} hidden={hidden} role="img" aria-label={label}>
      <svg class="acc-distance__graphic" viewBox="0 0 60 50" aria-hidden="true">
        <LeadVehicle />
        {[4, 3, 2, 1].map(level => <DistanceBar key={level} level={level} />)}
      </svg>
      <span class="acc-distance__count">Gap <b>{count}</b><span> / 4</span></span>
    </div>
  );
}
