/**
 * Radar alignment reference: the saved reading the System radar card measures change from.
 *
 * The points are kept on the Pi (projects/vehicle_data/radar_reference.py), so every device shows
 * the same reference and clearing a browser loses nothing. Zeroing never touches the radar or its
 * calibration; it only changes what the dashboard subtracts. This module is the one request the
 * System card and the reference dialog both make, and the wording they share. The last state is
 * held in `store.radarReference`. Node-testable: no Preact, no store, no DOM.
 */

import vocabulary from "../../radar_reference.json" with { type: "json" };
import { fmtTime } from "./format.js";
import { readReply } from "./jsonReply.js";
import { RADAR_AXES, fmtAngle } from "./radarAim.js";

export const RADAR_REFERENCE = Object.freeze(vocabulary);

const SOURCE_TEXT = Object.freeze({
  [RADAR_REFERENCE.sources.manual]: "Zeroed here",
  [RADAR_REFERENCE.sources.calibration]: "After calibration",
});

const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);

/** Whether a reply body is the reference state (the web listener's own refusals are not). */
export function isState(payload) {
  return Boolean(obj(payload) && Array.isArray(payload.points));
}

/**
 * One request to the Pi: a read without `body`, a change (zero, select, clear) with one.
 * The broker answers every request with the full state, refusals included.
 * @param {Function} fetchFn
 * @param {object} [body]
 * @returns {Promise<{ok: boolean, payload: object}>} rejects when the reply is not a JSON object
 */
export async function exchange(fetchFn, body) {
  const init =
    body === undefined
      ? { cache: "no-store" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store" };
  return readReply(await fetchFn(RADAR_REFERENCE.path, init));
}

/** The selected point of a state, or null when nothing is selected. */
export function selectedPoint(state) {
  if (!isState(state) || typeof state.selected !== "string") return null;
  return state.points.find((point) => obj(point) && point.id === state.selected) || null;
}

/** `vertical −3.77° · horizontal −0.07°` */
export function anglesText(angles) {
  const a = obj(angles) || {};
  return RADAR_AXES.map((axis) => axis.word + " " + fmtAngle(a[axis.id])).join(" · ");
}

/** `Zeroed here · 7:40 pm` (when it was saved); an unknown source reads `Saved`. */
export function pointText(point, now) {
  const p = obj(point) || {};
  return (SOURCE_TEXT[p.source] || "Saved") + " · " + fmtTime(p.created_at, now);
}

/** Change of one axis since `point`, in degrees, or null without both numbers. */
export function sinceReference(point, axisId, value) {
  const p = obj(point);
  const was = p && obj(p.angles) ? num(p.angles[axisId]) : null;
  return was === null || num(value) === null ? null : value - was;
}
