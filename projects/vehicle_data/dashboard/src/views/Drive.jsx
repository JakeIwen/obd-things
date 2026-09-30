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
import { settings, saveSettings } from "../settings.js";
import { DRIVE_PAGE_NAMES, swipeTarget } from "./drivePager.js";

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

// Each page loads once, keeping both gauge additions and sensors out of core.
const pageViews = { 1: signal(null), 2: signal(null) };
const loading = {};
function loadPage(page) {
  if (pageViews[page].peek() || loading[page]) return;
  loading[page] = true;
  const promise = page === 1 ? import("./DriveGauges.jsx") : import("./DriveMore.jsx");
  promise.then((mod) => { pageViews[page].value = mod.default; }).catch((error) => {
    loading[page] = false;
    console.error("failed to load Drive page", page, error);
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
    loadPage(page);
  }, [page]);
  const View = pageViews[page].value;
  return (
    <div class="view view--drive" role="main" ref={ref}>
      {View ? <View /> : <div class={page === 2 ? "drive2" : "drive"} />}
      <Pager page={page} />
    </div>
  );
}
