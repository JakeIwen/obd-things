import { fmtValue, fmtDuration, DASH } from "../format.js";
import { mpgFigure } from "../fuelEconomy.helpers.js";

export const DRIVE_INFO = "The dashboard’s own drive starts when the van starts moving or running and ends after 5 minutes parked. It is separate from the instrument cluster’s Trip A/B. MPG is an estimate.";

/** The current automatic drive, with API and saved-setting trip names left intact. */
export function tripRowsModel(history, { running, miles, nowMs }) {
  const trip = history && history.current_trip ? history.current_trip : null;
  const since = trip ? Date.parse(trip.started_at) : NaN;
  const duration = running && isFinite(since) ? fmtDuration((nowMs - since) / 1000) : DASH;
  const distance = running && miles !== null ? "≈ " + miles.toFixed(1) + " mi" : DASH;
  const mpg = mpgFigure(trip ? trip.fuel_economy : null);
  const cmp = history && history.trip_comparison && history.trip_comparison.metrics;
  const maxOf = (name) => {
    const cur = cmp && cmp[name] && cmp[name].current_trip;
    return cur && typeof cur.maximum === "number" ? fmtValue(name, cur.maximum) : null;
  };
  const coolMax = maxOf("engine.coolant_temperature");
  const power = cmp && cmp["engine.crankshaft_power"] && cmp["engine.crankshaft_power"].current_trip;
  const powerAvg = power && typeof power.mean === "number" ? fmtValue("engine.crankshaft_power", power.mean) : null;
  return [
    ["Time", duration],
    ["Distance", distance],
    ["MPG", mpg.value + (mpg.unit ? " " + mpg.unit : "")],
    ["Coolant max", coolMax ? coolMax + " °F" : DASH],
    ["Avg power", powerAvg ? powerAvg + " hp" : DASH],
  ];
}
