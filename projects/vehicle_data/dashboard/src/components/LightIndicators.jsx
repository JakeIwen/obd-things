import { useComputed } from "@preact/signals";
import { obs } from "../app/derive.js";
import { connection } from "../store.js";
import contract from "../../../lighting.json";
import { lightIndicatorState } from "./lightIndicators.helpers.js";

function LightIcon({ highBeam }) {
  return (
    <svg viewBox="0 0 40 28" class="light-indicator__icon" aria-hidden="true">
      {highBeam ? <>
        <path d="M23 4C38 4 38 24 23 24Z" />
        <path d="M4 5H18M4 11H18M4 17H18M4 23H18" />
      </> : <>
        <path d="M16 6C6 6 6 22 16 22ZM24 6C34 6 34 22 24 22Z" />
        <path d="M2 5L6 8M1 14H5M2 23L6 20M38 5L34 8M39 14H35M38 23L34 20" />
      </>}
    </svg>
  );
}

function Indicator({ definition }) {
  const reading = obs(definition.metric);
  const state = useComputed(() => lightIndicatorState(reading.value, connection.value.state));
  const className = useComputed(() =>
    `light-indicator light-indicator--${definition.key} light-indicator--${state.value}`);
  const label = useComputed(() => `${definition.label}: ${state.value}`);
  return (
    <div class={className} role="img" aria-label={label}>
      <LightIcon highBeam={definition.key === "high_beam"} />
    </div>
  );
}

export function LightIndicators() {
  return (
    <div class="light-indicators" role="group" aria-label="Exterior lights">
      {contract.indicators.map(definition => <Indicator key={definition.key} definition={definition} />)}
    </div>
  );
}
