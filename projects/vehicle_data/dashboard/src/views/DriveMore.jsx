/**
 * Drive page 2, "Sensors" (design 3.1.1): the cluster's speed limit and ACC set speed, alternator
 * field duty, the gear estimate, radar aim, transmission shaft speeds, power, torque and target
 * torque, and the odometer. One screen, no
 * scrolling, at 800×1280 portrait and 1280×800 landscape. Loaded lazily the first time page 2
 * is shown; while page 1 is on screen none of this is mounted.
 *
 * Every changing piece is a module-level computed string bound to a text node or attribute, so
 * the tree renders once and later updates touch only the text that changed.
 */

import { computed } from "@preact/signals";
import { Tile } from "../components/Tile.jsx";
import { liveValue, obs, isHidden, dayClock } from "../app/derive.js";
import * as store from "../store.js";
import { DASH, fmtFixed } from "../format.js";
import {
  RADAR_TILE_AXES,
  radarTileModel,
  gearText,
  ratioText,
  speedLimitText,
  accTileModel,
} from "./driveMore.helpers.js";

// Speed limit: the number the cluster's traffic-sign display shows, in a sign outline; `—` when
// it shows none (the broker publishes nothing for the frame's 0, so the record goes stale).
const limit = computed(() => speedLimitText(liveValue("vehicle.speed_limit").value));
const limitOn = computed(() => limit.value !== DASH);
const limitCls = computed(() => "tile area-d2limit " + (limitOn.value ? "tile--live" : "tile--off"));
const limitUnit = computed(() => (limitOn.value ? "mph" : ""));

function LimitTile() {
  return (
    <section class={limitCls} aria-label="Speed limit">
      <div class="tile__label">Speed limit</div>
      <div class="drive2-limit">
        <div class="drive2-limit__sign num">
          <span class="drive2-limit__value">{limit}</span>
          <span class="drive2-limit__unit">{limitUnit}</span>
        </div>
      </div>
    </section>
  );
}

// ACC: set speed with the state word and what the cluster shows beside it (vehicle ahead,
// following-distance bars, fixed cruise). These are strings, booleans and small integers, read
// from their records; a value that is not current reads as null.
function shownNow(name, type) {
  const rec = store.metricSignal(name).value;
  return rec && rec.available === true && !rec.stale && typeof rec.value === type ? rec.value : null;
}
const acc = computed(() =>
  accTileModel(shownNow("acc.state", "string"), liveValue("acc.set_speed").value, {
    mode: shownNow("acc.mode", "string"),
    bars: shownNow("acc.follow_distance", "number"),
    lead: shownNow("acc.lead_vehicle", "boolean"),
  })
);
const accCls = computed(() => "tile area-d2acc tile--" + acc.value.kind);
const accValue = computed(() => acc.value.value);
const accUnit = computed(() => acc.value.unit);
const accSub = computed(() => acc.value.sub);

function AccTile() {
  return (
    <section class={accCls} aria-label="Adaptive cruise">
      <div class="tile__label">ACC</div>
      <div class="tile__value num">
        <span>{accValue}</span>
        <span class="tile__unit">{accUnit}</span>
      </div>
      <div class="tile__sub">{accSub}</div>
    </section>
  );
}

// Alternator field duty pairs with the battery voltage it is regulating.
const batterySub = computed(() => {
  const v = liveValue("battery.voltage").value;
  return v === null ? "" : "battery " + fmtFixed(v, 1) + " V";
});

// Gear estimate: read from the record (its quality is below the driver-qualified set, so
// derive.obs would never call it live). Shown with "~" and only while current.
const gear = computed(() => gearText(store.metricSignal("transmission.gear_estimate").value));
const gearCls = computed(() => "tile tile--large area-d2gear " + (gear.value === DASH ? "tile--off" : "tile--live"));
const ratio = computed(() =>
  ratioText(liveValue("transmission.turbine_speed").value, liveValue("transmission.output_speed").value),
);

function GearTile() {
  return (
    <section class={gearCls} aria-label="Gear">
      <div class="tile__label">Gear</div>
      <div class="tile__value num">
        <span>{gear}</span>
      </div>
      <div class="tile__sub">{ratio}</div>
    </section>
  );
}

// Radar aim: latest angle per axis (current record, else the dated last reading), plus the
// broker's 1-minute and 5-minute means. The windows ride on status-lite, which republishes only
// when its stable projection (these windows included) changes; status_full is the fallback.
const radar = computed(() => {
  void dayClock.value; // "last 3:10 pm" becomes "yesterday 3:10 pm" after midnight
  const axes = {};
  for (let i = 0; i < RADAR_TILE_AXES.length; i += 1) {
    const axis = RADAR_TILE_AXES[i];
    const rec = store.metricSignal(axis.name).value;
    const o = obs(axis.name).value;
    axes[axis.id] = {
      fresh: Boolean(rec && rec.available && !rec.stale && typeof rec.value === "number"),
      value: rec && typeof rec.value === "number" ? rec.value : null,
      held: o.kind === "held" && o.retained ? o.retained : null,
    };
  }
  const lite = store.statusLite.value;
  const full = store.summary.statusFull.value;
  const pick = (key) => (lite && lite[key]) || (full && full[key]) || null;
  return radarTileModel({
    axes,
    windows: pick("radar_alignment"),
    polling: pick("radar_alignment_polling"),
    nowMs: Date.now(),
  });
});
const radarCls = computed(() => "tile area-d2radar drive2-radar" + (radar.value.live ? "" : " drive2-radar--held"));
const radarSub = computed(() => radar.value.sub);
const radarText = RADAR_TILE_AXES.map((axis) => ({
  id: axis.id,
  label: axis.label,
  latest: computed(() => radar.value.axes[axis.id].latest),
  m1: computed(() => radar.value.axes[axis.id].m1),
  m5: computed(() => radar.value.axes[axis.id].m5),
}));

function RadarTile() {
  return (
    <section class={radarCls} aria-label="Radar aim">
      <div class="tile__label">Radar aim</div>
      <div class="drive2-radar__axes">
        {radarText.map((t) => (
          <div class="drive2-radar__axis" key={t.id}>
            <div class="drive2-radar__name">{t.label}</div>
            <div class="drive2-radar__value num">{t.latest}</div>
            <div class="drive2-radar__row">
              <span>1-min avg</span>
              <b class="num">{t.m1}</b>
            </div>
            <div class="drive2-radar__row">
              <span>5-min avg</span>
              <b class="num">{t.m5}</b>
            </div>
          </div>
        ))}
      </div>
      <div class="tile__sub">{radarSub}</div>
    </section>
  );
}

export default function DriveMore() {
  const show = (id) => !isHidden("drive", id);
  return (
    <div class="drive2">
      {show("limit") && <LimitTile />}
      {show("acc") && <AccTile />}
      {show("field") && <Tile name="generator.field_duty" label="Alternator field" area="d2field" spark sub={batterySub} />}
      {show("gear") && <GearTile />}
      {show("radar") && <RadarTile />}
      {show("turbine") && <Tile name="transmission.turbine_speed" label="Turbine" area="d2turb" />}
      {show("output") && <Tile name="transmission.output_speed" label="Output shaft" area="d2out" />}
      {show("power2") && <Tile name="engine.crankshaft_power" label="Power" area="d2power" spark />}
      {show("torque") && <Tile name="engine.crankshaft_torque" label="Torque" area="d2torque" />}
      {show("target") && <Tile name="engine.target_crankshaft_torque" label="Target torque" area="d2target" />}
      {show("odometer") && <Tile name="vehicle.odometer" label="Odometer" area="d2odo" />}
    </div>
  );
}
