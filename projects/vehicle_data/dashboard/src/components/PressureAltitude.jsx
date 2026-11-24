import { obs } from "../app/derive.js";
import { connection } from "../store.js";
import { BAROMETRIC_PRESSURE_METRIC, pressureAltitudeDisplay } from "../pressureAltitude.helpers.js";

const DESCRIPTION = "Pressure altitude from the engine computer’s barometric reading. Weather can shift it by a few hundred feet.";

export function PressureAltitude() {
  const reading = obs(BAROMETRIC_PRESSURE_METRIC).value;
  const display = pressureAltitudeDisplay(reading, connection.value.state);
  if (!display) return null;
  return (
    <span class={"pressure-altitude" + (display.stale ? " pressure-altitude--stale" : "")}
      role="img" aria-label={`${display.text}, ${DESCRIPTION}${display.stale ? " Last reported reading." : ""}`}
      title={DESCRIPTION}>
      {display.text}
    </span>
  );
}
