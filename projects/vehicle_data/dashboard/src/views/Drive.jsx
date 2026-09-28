/**
 * Drive view (design 3.1): one screen, no scrolling, at 800×1280 portrait and
 * 1280×800 landscape. Page 1 (Gauges): speed and RPM on top; the six health
 * gauges with band bars and 15-minute sparklines; tires and trip at the bottom.
 * Page 2 (Sensors, design 3.1.1) is `DriveMore.jsx`, loaded on first use. A
 * two-segment pager under the grid (or a horizontal swipe) switches pages; the
 * choice is kept per device. Only the page on screen is mounted.
 */

import { computed, signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { Tile } from "../components/Tile.jsx";
import { TireGrid } from "../components/TireGrid.jsx";
import { liveValue, obs, isHidden, minuteClock, engineRunning } from "../app/derive.js";
import { tripDistanceMiles } from "../app/rings.js";
import * as store from "../store.js";
import { fmtValue, fmtDuration, DASH } from "../format.js";
import { settings, saveSettings } from "../settings.js";
import { DRIVE_PAGE_NAMES, swipeTarget } from "./drivePager.js";

// Sub-lines computed once at module scope (shared by every mount).
const alternatorSub = computed(() => {
  const duty = liveValue("generator.field_duty").value;
  return duty === null ? "" : "alternator " + fmtValue("generator.field_duty", duty) + " %";
});
// The cluster's speed limit (page 2 has the sign) as the Speed tile's sub-line.
const speedSub = computed(() => {
  const mph = liveValue("vehicle.speed_limit").value;
  return mph === null ? "" : "limit " + mph + " mph";
});
const torqueSub = computed(() => {
  const t = liveValue("engine.crankshaft_torque").value;
  return t === null ? "" : fmtValue("engine.crankshaft_torque", t) + " lb-ft";
});
// Reserved for a broker gear metric (design section 9); empty until one is live and
// driver-qualified. The production GEAR tile also accepted these catalog names.
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

/** Drive tile registry: id → element factory (ids are used by the customiser). */
export const DRIVE_TILES = [
  { id: "speed", label: "Speed" },
  { id: "rpm", label: "RPM" },
  { id: "coolant", label: "Coolant" },
  { id: "trans", label: "Trans oil" },
  { id: "oilp", label: "Oil pressure" },
  { id: "volt", label: "Voltage" },
  { id: "power", label: "Power" },
  { id: "oilt", label: "Oil temp (VVT)" },
  { id: "tires", label: "Tires" },
  { id: "trip", label: "Trip" },
  // Page 2 (DriveMore.jsx), in screen order; ids must not collide with page 1.
  ...[
    ["limit", "Speed limit"],
    ["acc", "ACC"],
    ["field", "Alternator field"],
    ["gear", "Gear"],
    ["radar", "Radar aim"],
    ["turbine", "Turbine"],
    ["output", "Output shaft"],
    ["power2", "Power"],
    ["torque", "Torque"],
    ["target", "Target torque"],
    ["odometer", "Odometer"],
  ].map(([id, label]) => ({ id, label: label + " (page 2)" })),
];

/** The Drive page on screen (1 or 2), per device. */
const drivePage = computed(() => (settings.value.drivePage === 2 ? 2 : 1));

function setDrivePage(page) {
  const s = settings.peek();
  if (s.drivePage !== page) saveSettings({ ...s, drivePage: page });
}

// Page 2 is its own chunk, fetched the first time it is shown.
let DriveMore = null;
let moreLoading = false;
const moreReady = signal(false);
function loadMore() {
  if (DriveMore || moreLoading) return;
  moreLoading = true;
  import("./DriveMore.jsx")
    .then((mod) => {
      DriveMore = mod.default;
      moreReady.value = true;
    })
    .catch((error) => {
      moreLoading = false;
      console.error("failed to load Drive page 2", error);
    });
}

/** Passive horizontal-swipe listeners on the Drive view; returns the cleanup. */
function attachSwipe(el) {
  if (!el) return undefined;
  let x0 = 0;
  let y0 = 0;
  let t0 = 0;
  let tracking = false;
  const start = (ev) => {
    tracking = ev.touches.length === 1;
    if (!tracking) return;
    x0 = ev.touches[0].clientX;
    y0 = ev.touches[0].clientY;
    t0 = ev.timeStamp;
  };
  const end = (ev) => {
    if (!tracking) return;
    tracking = false;
    const t = ev.changedTouches[0];
    if (!t) return;
    const current = drivePage.peek();
    const next = swipeTarget(t.clientX - x0, t.clientY - y0, ev.timeStamp - t0, current);
    if (next !== current) setDrivePage(next);
  };
  const opts = { passive: true };
  el.addEventListener("touchstart", start, opts);
  el.addEventListener("touchend", end, opts);
  return () => {
    el.removeEventListener("touchstart", start, opts);
    el.removeEventListener("touchend", end, opts);
  };
}

function Pager({ page }) {
  return (
    <nav class="drive-pager" aria-label="Drive pages">
      {DRIVE_PAGE_NAMES.map(([n, name]) => (
        <button
          key={n}
          type="button"
          class={"drive-pager__btn" + (page === n ? " drive-pager__btn--on" : "")}
          aria-current={page === n ? "page" : undefined}
          onClick={() => setDrivePage(n)}
        >
          <span class="drive-pager__num">{n}</span>
          {name}
        </button>
      ))}
    </nav>
  );
}

export default function Drive() {
  const page = drivePage.value;
  const ref = useRef(null);
  useEffect(() => attachSwipe(ref.current), []);
  useEffect(() => {
    if (page === 2) loadMore();
  }, [page]);
  const More = page === 2 && moreReady.value ? DriveMore : null;
  return (
    <div class="view view--drive" role="main" ref={ref}>
      {page === 2 ? More ? <More /> : <div class="drive2" /> : <DriveGauges />}
      <Pager page={page} />
    </div>
  );
}

/** Page 1, unchanged from the single-page Drive view. */
function DriveGauges() {
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
