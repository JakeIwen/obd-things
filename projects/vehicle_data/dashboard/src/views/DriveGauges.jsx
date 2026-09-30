import { computed } from "@preact/signals";
import { Tile } from "../components/Tile.jsx";
import { TireGrid } from "../components/TireGrid.jsx";
import { liveValue, isHidden, minuteClock, engineRunning } from "../app/derive.js";
import { tripDistanceMiles } from "../app/rings.js";
import * as store from "../store.js";
import { fmtValue, fmtDuration, DASH } from "../format.js";
import { speedFooter } from "./driveSpeed.helpers.js";

// Sub-lines computed once at module scope (shared by every mount).
const alternatorSub = computed(() => {
  const duty = liveValue("generator.field_duty").value;
  return duty === null ? "" : "alternator " + fmtValue("generator.field_duty", duty) + " %";
});
// Display records expire in the store; the current ACC state gates every detail.
const speedSub = computed(() => {
  const current = (name) => {
    const r = store.metricSignal(name).value;
    return r && r.available === true && !r.stale ? r.value : null;
  };
  return speedFooter(liveValue("vehicle.speed_limit").value, current("acc.state"),
    liveValue("acc.set_speed").value, {
      mode: current("acc.mode"), lead: current("acc.lead_vehicle"),
    });
});
const torqueSub = computed(() => {
  const t = liveValue("engine.crankshaft_torque").value;
  return t === null ? "" : fmtValue("engine.crankshaft_torque", t) + " lb-ft";
});
// Reserved for a broker gear metric (design section 9); empty until one is live and
// driver-qualified. The former static app's GEAR tile also accepted these catalog names.
const GEAR_METRICS = ["transmission.gear_estimate", "transmission.gear", "cluster.actual_gear"];
const gearText = computed(() => {
  // Estimated gear (broker metric transmission.gear_estimate, candidate quality): shown with "~"
  // only while it is fresh; the broker publishes it only while moving (design section 9).
  const rec = store.metricSignal("transmission.gear_estimate").value;
  if (!rec || !rec.available || rec.stale || typeof rec.value !== "string") return "";
  return "~" + rec.value;
});
const rpmSub = computed(() => (gearText.value ? "gear " + gearText.value : ""));

const tripRows = computed(() => {
  void minuteClock.value;
  const history = store.summary.history.value;
  const trip = history && history.current_trip ? history.current_trip : null;
  const running = engineRunning.value;
  const since = trip ? Date.parse(trip.started_at) : NaN;
  const duration = running && isFinite(since) ? fmtDuration((Date.now() - since) / 1000) : DASH;
  const miles = tripDistanceMiles.value;
  const distance = running && miles !== null ? "≈ " + miles.toFixed(1) + " mi" : DASH;
  const cmp = history && history.trip_comparison && history.trip_comparison.metrics;
  const maxOf = (name) => {
    const cur = cmp && cmp[name] && cmp[name].current_trip;
    return cur && typeof cur.maximum === "number" ? fmtValue(name, cur.maximum) : null;
  };
  const coolMax = maxOf("engine.coolant_temperature");
  const powerAvg = (() => {
    const cur = cmp && cmp["engine.crankshaft_power"] && cmp["engine.crankshaft_power"].current_trip;
    return cur && typeof cur.mean === "number" ? fmtValue("engine.crankshaft_power", cur.mean) : null;
  })();
  return [
    ["Time", duration],
    ["Distance", distance],
    ["Coolant max", coolMax ? coolMax + " °F" : DASH],
    ["Avg power", powerAvg ? powerAvg + " hp" : DASH],
  ];
});

function TripTile() {
  // Four rows of short text; the whole tile re-renders at most once a minute.
  const rows = tripRows.value;
  return (
    <section class="tile area-trip" aria-label="Trip">
      <div class="tile__label">Trip</div>
      <div class="trip">
        {rows.map(([k, v]) => (
          <div class="trip__row" key={k}>
            <span>{k}</span>
            <b class="num">{v}</b>
          </div>
        ))}
      </div>
    </section>
  );
}

/** Page 1 gauges, loaded on first use. */
export default function DriveGauges() {
  const show = (id) => !isHidden("drive", id);
  return (
    <div class="drive">
      {show("speed") && <Tile name="vehicle.speed" label="Speed" area="speed" size="hero" sub={speedSub} />}
      {show("rpm") && <Tile name="engine.rpm" label="RPM" area="rpm" size="large" sub={rpmSub} spark showUnit={false} />}
      {show("coolant") && <Tile name="engine.coolant_temperature" label="Coolant" area="coolant" band spark />}
      {show("trans") && <Tile name="transmission.oil_temperature" label="Trans oil" area="trans" band spark />}
      {show("oilp") && <Tile name="engine.oil_pressure" label="Oil pressure" area="oilp" band spark />}
      {show("volt") && <Tile name="battery.voltage" label="Voltage" area="volt" band spark sub={alternatorSub} decimals={1} />}
      {show("power") && <Tile name="engine.crankshaft_power" label="Power" area="power" spark sub={torqueSub} />}
      {show("oilt") && <Tile name="engine.vvt_oil_temperature" label="Oil temp (VVT)" area="oilt" band spark />}
      {show("tires") && <TireGrid area="tires" />}
      {show("trip") && <TripTile />}
    </div>
  );
}
