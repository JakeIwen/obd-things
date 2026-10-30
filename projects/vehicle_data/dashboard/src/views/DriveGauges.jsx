import { computed } from "@preact/signals";
import { useRef, useState } from "preact/hooks";
import { useModalDialog } from "../dialogs/modalDialog.js";
import { DRIVE_INFO, tripRowsModel } from "./driveTrip.helpers.js";
import { Tile } from "../components/Tile.jsx";
import { TireGrid } from "../components/TireGrid.jsx";
import { AccDistance } from "../components/AccDistance.jsx";
import { currentAcc } from "../accDisplay.js";
import { overspeed } from "../overspeed.js";
import { DriveFooter } from "./DriveFooter.jsx";
import { SpeedEconomy } from "./SpeedEconomy.jsx";
import { liveValue, isHidden, minuteClock, engineRunning } from "../app/derive.js";
import { tripDistanceMiles } from "../app/rings.js";
import * as store from "../store.js";
import { fmtValue } from "../format.js";
import { speedFooter } from "./driveSpeed.helpers.js";

// Sub-lines computed once at module scope (shared by every mount).
const alternatorSub = computed(() => {
  const duty = liveValue("generator.field_duty").value;
  return duty === null ? "" : "alternator " + fmtValue("generator.field_duty", duty) + " %";
});
// Display records expire in the store; the current ACC state gates every detail.
const speedSub = computed(() => {
  const acc = currentAcc.value;
  return speedFooter(liveValue("vehicle.speed_limit").value, acc.state, acc.setSpeed, acc);
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
  return tripRowsModel(store.summary.history.value, {
    running: engineRunning.value,
    miles: tripDistanceMiles.value,
    nowMs: Date.now(),
  });
});

function TripTile() {
  const rows = tripRows.value;
  const [infoOpen, setInfoOpen] = useState(false);
  const closeRef = useRef(null);
  const { dialogRef, close } = useModalDialog({ open: infoOpen, onClose: () => setInfoOpen(false), focusRef: closeRef });
  return (
    <>
      <section class="tile area-trip" aria-label="This drive">
        <div class="drive-trip__head">
          <div class="tile__label">This drive</div>
          <button type="button" class="drive-trip__info" title={DRIVE_INFO} aria-label="About this drive" onClick={() => setInfoOpen(true)}>
            <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
              <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.5" />
              <path d="M12 10v7M12 7v1" fill="none" stroke="currentColor" stroke-width="2" />
            </svg>
          </button>
        </div>
        <div class="trip">
          {rows.map(([k, v]) => (
            <div class="trip__row" key={k}>
              <span>{k}</span>
              <b class="num">{v}</b>
            </div>
          ))}
        </div>
      </section>
      <dialog ref={dialogRef} class="dialog" aria-labelledby="drive-info-title" onClick={(ev) => { if (ev.target === dialogRef.current) close(); }}>
        <header class="dialog__head">
          <h2 id="drive-info-title" class="dialog__title">This drive</h2>
          <button type="button" class="dialog__close" aria-label="Close drive information" ref={closeRef} onClick={close}>×</button>
        </header>
        <div class="dialog__body"><p>{DRIVE_INFO}</p></div>
      </dialog>
    </>
  );
}

/** Page 1 gauges, loaded on first use. */
export default function DriveGauges({ page, onPageChange }) {
  const show = (id) => !isHidden("drive", id);
  return (
    <>
      <div class="drive">
        {show("speed") && <Tile name="vehicle.speed" label="Speed" area="speed" size="hero" sub={speedSub} extra={<AccDistance />} detail={<SpeedEconomy />} overspeed={overspeed} />}
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
      <DriveFooter page={page} onPageChange={onPageChange} />
    </>
  );
}
