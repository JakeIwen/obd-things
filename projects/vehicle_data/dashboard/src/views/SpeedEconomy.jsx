import { computed, useComputed } from "@preact/signals";
import { liveValue } from "../app/derive.js";
import * as store from "../store.js";
import { instantFigure } from "../fuelEconomy.helpers.js";

// Candidate-quality instant economy carries "≈" and never colours the tile or raises a warning.
const nowFigure = computed(() =>
  instantFigure(liveValue("vehicle.speed").value, store.metricSignal("engine.fuel_rate").value),
);

function Figure({ name, figure }) {
  const value = useComputed(() => figure.value.value);
  const unit = useComputed(() => figure.value.unit);
  return (
    <span class="speed-economy__item">
      <span class="speed-economy__name">{name}</span>
      <b class="speed-economy__value num">{value}</b>
      <span class="speed-economy__unit">{unit}</span>
    </span>
  );
}

/** Estimated instant economy under the Speed numeral: `now ≈ 19 mpg`. */
export function SpeedEconomy() {
  return (
    <div class="speed-economy" role="group" aria-label="Fuel economy">
      <Figure name="now" figure={nowFigure} />
    </div>
  );
}
