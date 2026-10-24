import { computed, useComputed } from "@preact/signals";
import { liveValue } from "../app/derive.js";
import * as store from "../store.js";
import { instantFigure, mpgFigure } from "../fuelEconomy.helpers.js";

// Instant economy is live speed ÷ live fuel rate; the trip figure is the broker's estimate for
// the open trip, refreshed with the history summary about once a minute. Both are candidate
// quality, so they carry "≈" and never colour the tile or raise a warning.
const nowFigure = computed(() =>
  instantFigure(liveValue("vehicle.speed").value, store.metricSignal("engine.fuel_rate").value),
);
const tripFigure = computed(() => {
  const history = store.summary.history.value;
  return mpgFigure(history && history.current_trip ? history.current_trip.fuel_economy : null);
});

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

/** Estimated economy under the Speed numeral: `now ≈ 19 mpg · trip ≈ 16.4 mpg`. */
export function SpeedEconomy() {
  return (
    <div class="speed-economy" role="group" aria-label="Fuel economy">
      <Figure name="now" figure={nowFigure} />
      <Figure name="trip" figure={tripFigure} />
    </div>
  );
}
