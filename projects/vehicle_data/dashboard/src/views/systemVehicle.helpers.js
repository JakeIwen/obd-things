/** Pure vehicle-state presentation for the System view. */

import { canAvailabilityLine, isUnavailableCanState } from "../canAvailability.js";
import { obj, num, str, when, humanize, capital } from "./view.helpers.js";

const STATE_WORDS = Object.freeze({
  running: "Running",
  moving: "Moving",
  ignition_on: "Ignition on",
  awake: "Awake",
  asleep: "Asleep",
  parked: "Parked",
  unknown: "Unknown",
});

/** Plain phrase for the broker's (or the tablet's) evidence basis. */
export const BASIS_TEXT = Object.freeze({
  passive_bus_silence: "bus silent",
  passive_bus_activity: "bus traffic seen",
  passive_can_ch_activity: "diagnostic-bus traffic seen",
  passive_can_ch_activity_c_can_silent: "chassis-bus traffic seen, but the C-CAN adapter hears nothing",
  wrong_rate_rx_activity: "traffic at an unexpected bus speed",
  ccan_0x2ef_ignition_gate: "ignition signal",
  qualified_ccan_0x0fc_engine_speed: "engine-speed signal",
  stale_ccan_0x2ef_ignition_gate: "ignition signal went quiet",
  stale_ccan_0x0fc_engine_speed: "engine-speed signal went quiet",
  no_passive_observation: "no bus data yet",
  client_freshness_expired: "no update in the last 5 s",
  client_freshness_invalid: "update time unreadable",
  client_page_hidden: "screen was off; refreshing",
  client_page_visible: "screen woke; refreshing",
  client_page_restored: "page restored; refreshing",
  no_snapshot: "waiting for data",
  no_vehicle_state: "not reported",
});

/** Plain word for the broker's confidence. */
export const CONFIDENCE_TEXT = Object.freeze({
  verified: "confirmed by a verified signal",
  observed: "seen on the bus",
  inferred: "inferred",
  stale: "out of date",
  unknown: "unknown",
});

export function basisText(basis) {
  const value = str(basis);
  if (!value) return "not reported";
  return BASIS_TEXT[value] || humanize(value);
}

export function confidenceText(confidence) {
  const value = str(confidence);
  if (!value) return "unknown";
  return CONFIDENCE_TEXT[value] || humanize(value);
}

/**
 * The one-line vehicle state: `Asleep · bus silent · last drive 4:25 pm`.
 * @param {{state?:string, basis?:string, confidence?:string, detail?:string}|null} vehicle `store.vehicle` value
 * @param {{running?:boolean, lastDriveAt?:string|null, nowMs?:number}} [ctx]
 * @returns {string}
 */
export function vehicleLine(vehicle, ctx) {
  const context = ctx || {};
  const value = obj(vehicle) || {};
  if (isUnavailableCanState(value)) return canAvailabilityLine(value);
  const head = context.running ? "Running" : STATE_WORDS[value.state] || capital(humanize(value.state)) || "Unknown";
  const parts = [head];
  if (!context.running || value.state === "running") parts.push(basisText(value.basis));
  if (!context.running) {
    const last = when(context.lastDriveAt, num(context.nowMs) !== null ? context.nowMs : Date.now());
    if (last) parts.push("last drive " + last);
  }
  return parts.join(" · ");
}

export { canAvailabilityLine };
