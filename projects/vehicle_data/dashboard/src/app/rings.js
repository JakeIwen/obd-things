/**
 * Client-side 15-minute ring buffers for Drive sparklines, plus the trip
 * distance estimate.
 *
 * One effect per metric subscribes to that metric's record signal. The store
 * publishes a new record only when the sample changes, so each effect runs
 * about once per new observation (every 1–4 s while driving, never while
 * parked) and pushes one point into a preallocated typed-array ring.
 */

import { effect, signal } from "@preact/signals";
import * as store from "../store.js";
import { createRing } from "../ring.js";
import { DRIVER_QUALITIES } from "../store.js";

export const SPARK_METRICS = [
  "engine.coolant_temperature",
  "transmission.oil_temperature",
  "engine.oil_pressure",
  "battery.voltage",
  "engine.crankshaft_power",
  "engine.vvt_oil_temperature",
  "vehicle.speed",
  "engine.rpm",
  "generator.field_duty", // Drive page 2
];

const rings = new Map();

/** Bumped (at most once per push) so sparklines know a redraw is worthwhile. */
export const ringVersion = signal(0);

let pendingBump = false;
function bump() {
  if (pendingBump) return;
  pendingBump = true;
  // Coalesce the several pushes of one delivery into one version change.
  Promise.resolve().then(() => {
    pendingBump = false;
    ringVersion.value = ringVersion.peek() + 1;
  });
}

/**
 * Ring for a metric (created on first use).
 * @param {string} name
 */
export function ringFor(name) {
  let r = rings.get(name);
  if (!r) {
    r = createRing({ windowMs: 900000, maxPoints: 1200 });
    rings.set(name, r);
  }
  return r;
}

// Trip distance: integrate live speed samples while the engine runs. The
// historian ends a trip after 5 min without activity, so the integral resets
// when the engine starts again after a stop longer than that. The value is
// kept in localStorage per trip start so a page reload mid-trip keeps it.
export const tripDistanceMiles = signal(null);
const TRIP_KEY = "van-telemetry.v2.trip";
const TRIP_GAP_MS = 300000;
let lastSpeed = null; // {t, mph}
let distance = 0;
let tripStartWall = null;
let lastStopMono = null;

function saveTrip() {
  try {
    localStorage.setItem(TRIP_KEY, JSON.stringify({ start: tripStartWall, miles: distance }));
  } catch (_error) {
    // storage denied: the in-memory integral still works
  }
}

function loadTrip() {
  try {
    const raw = JSON.parse(localStorage.getItem(TRIP_KEY) || "null");
    return raw && typeof raw.miles === "number" ? raw : null;
  } catch (_error) {
    return null;
  }
}

function publishDistance() {
  const rounded = Math.round(distance * 10) / 10;
  if (tripDistanceMiles.peek() !== rounded) {
    tripDistanceMiles.value = rounded;
    saveTrip();
  }
}

function onSpeed(rec, tObs) {
  const mph = rec.value;
  if (lastSpeed && tObs > lastSpeed.t) {
    const dtHours = (tObs - lastSpeed.t) / 3600000;
    // Ignore gaps longer than 30 s rather than inventing distance across them.
    if (dtHours * 3600 <= 30) distance += ((mph + lastSpeed.mph) / 2) * dtHours;
  }
  lastSpeed = { t: tObs, mph };
  publishDistance();
}

/** Engine start/stop hook (called from startRings' effect). */
function onEngine(running, nowMono, tripStartedAt) {
  if (running) {
    const newTrip = lastStopMono === null ? tripStartWall === null : nowMono - lastStopMono > TRIP_GAP_MS;
    if (newTrip) {
      const stored = loadTrip();
      const start = tripStartedAt || new Date().toISOString();
      if (stored && tripStartedAt && stored.start === tripStartedAt) {
        distance = stored.miles; // reload mid-trip
      } else {
        distance = 0;
      }
      tripStartWall = start;
      lastSpeed = null;
      publishDistance();
    }
    lastStopMono = null;
  } else if (lastStopMono === null) {
    lastStopMono = nowMono;
    lastSpeed = null;
  }
}

let started = false;

/** Start feeding the rings; idempotent. */
export function startRings() {
  if (started) return;
  started = true;
  for (let i = 0; i < SPARK_METRICS.length; i += 1) {
    const name = SPARK_METRICS[i];
    const sig = store.metricSignal(name);
    const ring = ringFor(name);
    effect(() => {
      const rec = sig.value;
      if (!rec || !rec.available || rec.stale) return;
      if (typeof rec.value !== "number" || !isFinite(rec.value)) return;
      if (rec.quality && !DRIVER_QUALITIES.has(rec.quality)) return;
      const age = Number.isFinite(rec.ageAtReceiptMs) ? rec.ageAtReceiptMs : 0;
      const tObs = rec.receivedMono - age;
      if (ring.push(tObs, rec.value, rec.observed_at)) {
        bump();
        if (name === "vehicle.speed") onSpeed(rec, tObs);
      }
    });
  }
  effect(() => {
    const rpm = store.metricSignal("engine.rpm").value;
    const running = Boolean(rpm && rpm.available && !rpm.stale && typeof rpm.value === "number" && rpm.value >= 400);
    const history = store.summary.history.peek();
    const trip = history && history.current_trip ? history.current_trip.started_at : null;
    onEngine(running, performance.now(), trip);
  });
  // When the historian's trip id arrives after a reload, adopt the stored integral for it.
  effect(() => {
    const history = store.summary.history.value;
    const start = history && history.current_trip ? history.current_trip.started_at : null;
    if (!start || start === tripStartWall) return;
    const stored = loadTrip();
    if (stored && stored.start === start && stored.miles > distance) distance = stored.miles;
    tripStartWall = start;
    publishDistance();
  });
}
