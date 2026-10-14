import { useComputed } from "@preact/signals";
import { currentAcc } from "../accDisplay.js";

function DistanceBar({ level }) {
  const className = useComputed(() => "acc-distance__bar" +
    (currentAcc.value.bars >= level ? " acc-distance__bar--selected" : ""));
  return <rect class={className} x={4 + (level - 1) * 3} y={(4 - level) * 9 + 1}
    width={52 - (level - 1) * 6} height="5" rx="1" />;
}

/** The selected setting, separate from the lead icon and never a distance measurement. */
export function AccDistance() {
  const hidden = useComputed(() => currentAcc.value.bars === null);
  const label = useComputed(() => `Following distance: ${currentAcc.value.bars} of 4 bars`);
  const count = useComputed(() => currentAcc.value.bars);
  const className = useComputed(() => "acc-distance" +
    (["ready", "standby"].includes(currentAcc.value.state) ? " acc-distance--saved" : ""));
  return (
    <div class={className} hidden={hidden} role="img" aria-label={label}>
      <svg class="acc-distance__graphic" viewBox="0 0 60 36" aria-hidden="true">
        {[4, 3, 2, 1].map(level => <DistanceBar key={level} level={level} />)}
      </svg>
      <span class="acc-distance__count">Gap <b>{count}</b><span> / 4</span></span>
    </div>
  );
}
