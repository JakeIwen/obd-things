/**
 * Derived view models shared by every view.
 *
 * Everything here is a `computed` over the store. Outputs that reach the DOM
 * are primitives (strings, numbers, booleans) or reference-stable objects, so
 * a tile bound to them is updated only when its visible text or class really
 * changes. The 1 Hz stream refreshes timing fields in place without
 * publishing, and `store.tick()` publishes only stale flips, so nothing here
 * re-evaluates once per second unless something visible changed.
 */

import { computed, signal, effect } from "@preact/signals";
import * as store from "../store.js";
import { settings, saveSettings, autoView } from "../settings.js";
import { fmtValue, fmtFixed, fmtUnit, fmtTime, fmtDuration, DASH, THOUSANDS } from "../format.js";
import { evaluateBand } from "../bands.js";
import { buildWarningContext, OPEN_STATES, PAIR_RULE, metricLabel } from "../warningContext.js";
import { canAvailabilityLabel, canAvailabilityDetail, isUnavailableCanState } from "../canAvailability.js";

/** Monotonic now; wrapped so tests can stub it. */
export const monoNow = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

// ---------------------------------------------------------------------------
// per-metric observation state

const obsCache = new Map();

/**
 * Computed `observationState(name)` for one metric (memoised per name).
 * Re-evaluates only when the metric's record or the catalog changes.
 * @param {string} name
 */
export function obs(name) {
  let c = obsCache.get(name);
  if (!c) {
    c = computed(() => store.observationState(name, monoNow()));
    obsCache.set(name, c);
  }
  return c;
}

/**
 * Live, driver-qualified numeric value of a metric or null.
 * @param {string} name
 */
const liveCache = new Map();
export function liveValue(name) {
  let c = liveCache.get(name);
  if (!c) {
    c = computed(() => {
      const o = obs(name).value;
      return o.kind === "live" && typeof o.value === "number" && isFinite(o.value) ? o.value : null;
    });
    liveCache.set(name, c);
  }
  return c;
}

// ---------------------------------------------------------------------------
// vehicle, trip and warnings context

/** True when the engine is verifiably running (live RPM ≥ 400 or verified running state). */
export const engineRunning = computed(() => {
  const v = store.vehicle.value;
  if (isUnavailableCanState(v)) return false;
  const rpm = liveValue("engine.rpm").value;
  if (rpm !== null) return rpm >= 400;
  return v.confidence === "verified" && v.state === "running";
});

/** Warning state needed by always-loaded bands, badges and top-bar alerts. */
export const warningContext = computed(() => buildWarningContext(store.summary.health.value));
// Compatibility for pure consumers that inspect the structural warning list.
export const warningModel = warningContext;

/** Open vehicle warnings per metric name → 'red' | 'amber'. */
export const metricAlertColour = computed(() => {
  const map = new Map();
  const cards = warningContext.value.warnings;
  for (let i = 0; i < cards.length; i += 1) {
    const card = cards[i];
    // Numerals change colour only for a confirmed warning (or critical), never a watch.
    if (!card.metric || !OPEN_STATES.has(card.state)) continue;
    // Tier-2 notices (slow drifts) stay on the Health list; they never colour a numeral.
    if (!(card.tier === "critical" || (card.state === "warning" && card.tier === "warning"))) continue;
    const colour = card.tier === "critical" ? "red" : "amber";
    if (map.get(card.metric) !== "red") map.set(card.metric, colour);
  }
  return map;
});

/** True while a charging-failure style warning is open. */
const chargingFailure = computed(() => {
  const cards = warningContext.value.warnings;
  for (let i = 0; i < cards.length; i += 1) {
    const rule = cards[i].rule || "";
    if (rule.indexOf("charging") >= 0 && OPEN_STATES.has(cards[i].state)) return true;
  }
  return false;
});

/**
 * Tire band context. v2.0 colours wheels only by the absolute floors (front
 * < 50 psi, rear < 68 psi) and by an active warning rule: a per-wheel cold
 * baseline is a server product that does not exist yet, and every client-side
 * stand-in from 30-day aggregates is wrong by 2–3 psi (design critique B2).
 */
function tireContext(name) {
  return {
    baseline: {
      cold: null,
      pairDelta: null,
      isFront: name === "tire.pressure.fl" || name === "tire.pressure.fr",
    },
  };
}

// ---------------------------------------------------------------------------
// engine evidence timeline (voltage band selection, design 5 / critique M8)

/** Monotonic time when live RPM ≥ 400 began, or null when not running. */
export const runningSinceMono = signal(null);
/**
 * Pi-clock ms (the rpm sample's `observed_at`) of the sample that ended this
 * page's last running spell (0 = none). Kept in the Pi's clock domain so it
 * compares with battery `observed_at` without tablet clock skew.
 */
const lastRunningWall = signal(0);

/** Vehicle states that prove the engine is stopped when rpm is not live. */
const STOPPED_STATES = new Set(["asleep", "parked", "awake"]);

let engineTrackingStarted = false;
/** Track engine start/stop on this page; idempotent. Also expires manual view mode. */
export function startEngineTracking() {
  if (engineTrackingStarted) return;
  engineTrackingStarted = true;
  let previous = null;
  effect(() => {
    if (isUnavailableCanState(store.vehicle.value)) {
      runningSinceMono.value = null;
      return;
    }
    const rpm = liveValue("engine.rpm").value;
    const running = rpm !== null && rpm >= 400;
    if (running) {
      if (runningSinceMono.peek() === null) runningSinceMono.value = monoNow();
    } else if (runningSinceMono.peek() !== null) {
      runningSinceMono.value = null;
      const rec = store.metricSignal("engine.rpm").peek();
      lastRunningWall.value = wallOf(rec && rec.observed_at) || Date.now();
    }
    // A manual view choice lasts until the engine next starts or stops. Only
    // evidence counts: live rpm, or (rpm not live) a state that says stopped.
    // A data gap (rpm stale after a stall or a page hide, state unknown) is
    // neither, so it does not throw the driver out of a chosen view.
    const evidence = rpm !== null ? running : STOPPED_STATES.has(store.vehicle.value.state) ? false : null;
    if (evidence === null) return;
    if (previous !== null && previous !== evidence) {
      const s = settings.peek();
      if (!s.auto) saveSettings({ ...s, auto: true });
    }
    previous = evidence;
  });
}

function wallOf(iso) {
  const t = typeof iso === "string" ? Date.parse(iso) : NaN;
  return isFinite(t) ? t : 0;
}

/**
 * Pi-clock ms of a metric record's last sample when `test(value)` holds (the
 * record survives staleness, so this is the last such evidence), else 0.
 */
function evidenceWall(name, test) {
  const rec = store.metricSignal(name).value;
  return rec && test(rec.value) ? wallOf(rec.observed_at) : 0;
}
const isRunningRpm = (v) => typeof v === "number" && v >= 400;
const isIgnitionOn = (v) => v === true;

/**
 * Which voltage table applies: 'running' once live RPM has been ≥ 400 for
 * 10 s; 'parked' only for samples taken ≥ 30 s after the last running or
 * ignition evidence (critique A14); otherwise 'neutral' (key on, cranking,
 * the first 10 s after start, the first 30 s after stop or key-off).
 *
 * Every time compared here is on the Pi's clock (sample `observed_at`, the
 * rpm/ignition records' `observed_at`, the historian's `last_active_at`), so a
 * tablet clock offset cannot move the 30 s boundary.
 */
export const voltageMode = computed(() => {
  if (isUnavailableCanState(store.vehicle.value)) return "neutral";
  const since = runningSinceMono.value;
  if (since !== null) {
    if (monoNow() - since >= 10000) return "running";
    void store.clock.value; // re-check each second only inside the 10 s window
    return monoNow() - since >= 10000 ? "running" : "neutral";
  }
  if (store.vehicle.value.state === "ignition_on") return "neutral";
  const o = obs("battery.voltage").value;
  const rec = store.metricSignal("battery.voltage").value;
  const sampleAt = o.retained ? wallOf(o.retained.observed_at) : wallOf(rec && rec.observed_at);
  const history = store.summary.history.value;
  const trip = history && history.current_trip;
  const recent = history && Array.isArray(history.recent_trips) ? history.recent_trips[0] : null;
  const engineAt = Math.max(
    lastRunningWall.value,
    evidenceWall("engine.rpm", isRunningRpm),
    evidenceWall("vehicle.ignition_on", isIgnitionOn),
    wallOf(trip && trip.last_active_at),
    wallOf(recent && recent.last_active_at),
  );
  if (!sampleAt) return "parked";
  return sampleAt - engineAt >= 30000 ? "parked" : "neutral";
});

/**
 * Band context for a metric: voltage mode, tire floors, and live rpm/coolant
 * for oil pressure only. Each band reads only the signals its table uses, so
 * an rpm or coolant change re-evaluates the oil-pressure band and nothing else.
 */
function bandContext(name) {
  if (name.startsWith("tire.pressure.")) return tireContext(name);
  if (name === "battery.voltage") {
    return { voltageMode: voltageMode.value, chargingFailure: chargingFailure.value };
  }
  if (name === "engine.oil_pressure") {
    return { rpm: liveValue("engine.rpm").value, coolant: liveValue("engine.coolant_temperature").value };
  }
  return null;
}

const bandCache = new Map();
/**
 * Band evaluation for a metric's displayed value (live or held); reference-stable.
 * @param {string} name
 */
export function band(name) {
  let c = bandCache.get(name);
  if (!c) {
    c = computed(() => {
      const o = obs(name).value;
      const value = o.kind === "off" ? null : o.value;
      return evaluateBand(name, value, bandContext(name));
    });
    bandCache.set(name, c);
  }
  return c;
}

// ---------------------------------------------------------------------------
// tile view model (all primitives)

const tileCache = new Map();

/**
 * Text/class view model for a metric tile.
 * @param {string} name
 * @returns {{valueText, unitText, subText, kind, colour, bandState, markerPct, loPct, hiPct, showBand}}
 */
export function tileModel(name, options) {
  const decimals = options && typeof options.decimals === "number" ? options.decimals : null;
  const key = decimals === null ? name : name + "|" + decimals;
  let entry = tileCache.get(key);
  if (entry) return entry;
  const o = obs(name);
  const b = band(name);
  const valueText = computed(() => {
    const s = o.value;
    if (s.kind === "off") return DASH;
    return decimals === null ? fmtValue(name, s.value) : fmtFixed(s.value, decimals, THOUSANDS.has(name));
  });
  const unitText = computed(() => fmtUnit(name, o.value.unit));
  const heldText = computed(() => {
    const s = o.value;
    return s.kind === "held" && s.retained ? fmtTime(s.retained.observed_at) : "";
  });
  const kind = computed(() => o.value.kind);
  const colour = computed(() => {
    const s = o.value;
    if (s.kind !== "live") return "";
    const bandState = b.value.state;
    if (bandState === "red") return "red";
    const alert = metricAlertColour.value.get(name);
    return alert || "";
  });
  const geometry = computed(() => {
    const r = b.value;
    const s = o.value;
    if (r.state === "none" || r.min === null || r.max === null) return null;
    const span = r.max - r.min;
    const pct = (v) => Math.max(0, Math.min(100, ((v - r.min) / span) * 100));
    const value = s.kind === "off" ? null : s.value;
    return {
      lo: r.lo === null ? 0 : pct(r.lo),
      hi: r.hi === null ? 0 : pct(r.hi),
      marker: typeof value === "number" && isFinite(value) ? pct(value) : null,
      state: r.state,
    };
  });
  entry = { name, obs: o, band: b, valueText, unitText, heldText, kind, colour, geometry };
  tileCache.set(key, entry);
  return entry;
}

// ---------------------------------------------------------------------------
// top bar

/** Changes once a minute; lets minute-resolution text avoid 1 Hz work downstream. */
export const minuteClock = computed(() => Math.floor(store.clock.value / 60000));

/**
 * Local calendar day as `yyyymmdd`, checked once a minute and changing at local
 * midnight. For models whose only time dependence is "today / yesterday" or a
 * day cut-off, so they refresh overnight without re-rendering every minute.
 */
export const dayClock = computed(() => {
  void minuteClock.value;
  const d = new Date(Date.now());
  return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
});

/** Top-bar surface tone: warnings win; CAN loss is an amber degraded state. */
export function topBarClass(alert, vehicle) {
  if (alert) return "topbar" + (alert.tier === "critical" ? " topbar--red" : " topbar--amber");
  return "topbar" + (isUnavailableCanState(vehicle) ? " topbar--amber" : "");
}

/** Vehicle state word for the top bar ("Running", "Asleep", ...). */
export const vehicleHead = computed(() => {
  const conn = store.connection.value.state;
  if (conn === "connecting") return "Connecting";
  if (conn === "unavailable") return "Broker unavailable";
  const v = store.vehicle.value;
  if (isUnavailableCanState(v)) return canAvailabilityLabel(v);
  if (engineRunning.value) return "Running";
  switch (v.state) {
    case "ignition_on":
      return "Ignition on";
    case "awake":
      return "Awake";
    case "asleep":
      return "Asleep";
    case "parked":
      return "Parked";
    default:
      return "Waiting for data";
  }
});

/** Secondary top-bar text: trip duration while running, else the last drive time. */
export const vehicleTail = computed(() => {
  const conn = store.connection.value.state;
  if (conn === "unavailable") return "showing last data";
  const v = store.vehicle.value;
  if (isUnavailableCanState(v)) return canAvailabilityDetail(v);
  const history = store.summary.history.value;
  void minuteClock.value;
  if (engineRunning.value) {
    const trip = history && history.current_trip ? history.current_trip : null;
    const since = trip ? Date.parse(trip.started_at) : NaN;
    return isFinite(since) ? fmtDuration((Date.now() - since) / 1000) : "";
  }
  if (v.state === "ignition_on") return "engine off";
  const recent = history && Array.isArray(history.recent_trips) ? history.recent_trips[0] : null;
  return recent && recent.ended_at ? "last drive " + fmtTime(recent.ended_at) : "";
});

// ---------------------------------------------------------------------------
// alert strip

const LIVE_ALERTS = [
  { metric: "engine.oil_pressure", tier: "critical", action: "stop the engine when safe" },
  { metric: "engine.coolant_temperature", tier: "warning", action: "ease off" },
  { metric: "transmission.oil_temperature", tier: "warning", action: "ease off" },
];

function shortValue(metric, value, unit) {
  if (typeof value !== "number" || !isFinite(value)) return "";
  const u = fmtUnit(metric, unit);
  return fmtValue(metric, value) + (u ? " " + u : "");
}

/**
 * Top-bar alert: live red bands first (immediate), then broker items in
 * `warning` state or critical severity (60 s cadence). `watch` items stay on
 * the Health card. Short form "Metric value · action"; null when nothing needs
 * attention. The top bar never changes height.
 */
export const alertStrip = computed(() => {
  if (isUnavailableCanState(store.vehicle.value)) return null;
  const items = [];
  for (let i = 0; i < LIVE_ALERTS.length; i += 1) {
    const rule = LIVE_ALERTS[i];
    const o = obs(rule.metric).value;
    if (o.kind !== "live") continue;
    if (band(rule.metric).value.state !== "red") continue;
    items.push({
      tier: rule.tier,
      text: metricLabel(rule.metric) + " " + shortValue(rule.metric, o.value, o.unit) + " · " + rule.action,
      id: "live:" + rule.metric,
      episodeId: null,
    });
  }
  const cards = warningContext.value.warnings;
  for (let i = 0; i < cards.length; i += 1) {
    const card = cards[i];
    // Only confirmed tier-0/1 warnings and criticals reach the top bar; notices and system items do not.
    const confirmed = card.tier === "critical" || (card.state === "warning" && card.tier === "warning");
    if (!confirmed || !OPEN_STATES.has(card.state)) continue;
    const value = shortValue(card.metric || "", card.value, card.unit);
    items.push({
      tier: card.tier === "critical" ? "critical" : "warning",
      // A pair rule's metric names only its lower wheel; show "Rear tires uneven".
      text: card.metric && value && !PAIR_RULE.test(card.rule) ? metricLabel(card.metric) + " " + value : card.title,
      id: card.id,
      episodeId: card.episodeId,
    });
  }
  if (!items.length) return null;
  items.sort((a, b) => (a.tier === "critical" ? 0 : 1) - (b.tier === "critical" ? 0 : 1));
  return { tier: items[0].tier, text: items[0].text, count: items.length, first: items[0] };
});

/** Badge count for the Health tab. */
export const healthBadge = computed(() => {
  const counts = warningContext.value.counts;
  const open = warningContext.value.warnings.filter((c) => OPEN_STATES.has(c.state)).length;
  return { count: open, red: counts.critical > 0 };
});

// ---------------------------------------------------------------------------
// active view (automatic selection, design 5.2)

/** The view on screen. Manual taps set it; automatic mode moves it between Drive and Parked. */
export const activeView = signal(settings.peek().view || "drive");

/** Re-evaluate automatic selection; called from an effect in App. */
export function evaluateAutoView() {
  const s = settings.value;
  if (!s.auto) return;
  const v = store.vehicle.value;
  void store.clock.value;
  const next = autoView({
    rpmLive: liveValue("engine.rpm").value !== null,
    vehicle: { state: v.state, confidence: v.confidence, basis: v.basis, ageMs: store.ageMs(v, monoNow()) },
    current: activeView.peek(),
  });
  if (next !== activeView.peek()) activeView.value = next;
}

/** True when a card id is hidden on a view by the device customisation. */
export function isHidden(view, id) {
  const hidden = settings.value.hidden && settings.value.hidden[view];
  return Array.isArray(hidden) && hidden.indexOf(id) >= 0;
}

/** Card ids of a view in the device's chosen order (unknown ids keep their default order). */
export function orderedIds(view, defaults) {
  const order = (settings.value.order && settings.value.order[view]) || [];
  const known = order.filter((id) => defaults.indexOf(id) >= 0);
  const rest = defaults.filter((id) => known.indexOf(id) < 0);
  return known.concat(rest);
}
