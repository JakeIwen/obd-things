import { computed, useComputed } from "@preact/signals";
import { liveValue } from "../app/derive.js";
import * as store from "../store.js";
import { hasFigure, instantFigure } from "../fuelEconomy.helpers.js";

// Candidate-quality instant economy never colours the tile or raises a warning.
const nowFigure = computed(() =>
  instantFigure(liveValue("vehicle.speed").value, store.metricSignal("engine.fuel_rate").value),
);

function Figure({ name, figure }) {
  const value = useComputed(() => figure.value.value);
  const unit = useComputed(() => figure.value.unit);
  const empty = useComputed(() => !hasFigure(figure.value));
  return (
    <span class="speed-economy__item" hidden={empty}>
      <span class="speed-economy__name">{name}</span>
      <b class="speed-economy__value num">{value}</b>
      <span class="speed-economy__unit">{unit}</span>
    </span>
  );
}

/** Instant economy under the Speed numeral: `now 19.4 mpg`; no text without a figure (the row keeps its height). */
export function SpeedEconomy() {
  return (
    <div class="speed-economy" role="group" aria-label="Fuel economy">
      <Figure name="now" figure={nowFigure} />
    </div>
  );
}
