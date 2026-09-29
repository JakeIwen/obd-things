/**
 * Pure helpers for Drive page 2 ("Sensors", design 3.1.1): the radar aim tile, the gear
 * estimate and the shaft-speed ratio. No DOM, no signals; node-testable.
 */

import { DASH, fmtFixed, fmtDelta, fmtTime } from "../format.js";

/** Radar axes in tile order (horizontal first, as the owner reads "az … el"). */
export const RADAR_TILE_AXES = Object.freeze([
  Object.freeze({ id: "azimuth", name: "radar.alignment.azimuth", label: "Horizontal" }),
  Object.freeze({ id: "elevation", name: "radar.alignment.elevation", label: "Vertical" }),
]);

/** Reference limit (degrees) and where "near" starts; the same numbers as the System card. */
export const RADAR_TILE_LIMIT = 1;
export const RADAR_TILE_NEAR = 0.8;

const isNum = (v) => typeof v === "number" && isFinite(v);
const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

/** Signed angle with two decimals: `+0.15°`, `−0.02°`, `0.00°`; `—` for no number. */
export function fmtAngleSigned(value) {
  return isNum(value) ? fmtDelta(value, 2) + "°" : DASH;
}

/** Mean of one broker window (`{count, mean}`) as a signed angle, or `—` with no samples. */
export function windowMean(w) {
  if (!isObj(w) || !isNum(w.mean) || !(isNum(w.count) && w.count > 0)) return DASH;
  return fmtAngleSigned(w.mean);
}

/**
 * Radar aim tile model.
 * @param {{axes?: Object<string, {fresh?: boolean, value?: number|null,
 *   held?: {value: number, observed_at: string}|null}>, windows?: object|null,
 *   polling?: object|null, nowMs?: number}} input
 *   `axes[id]`: `fresh` is a current record, `value` its number, `held` the dated last reading;
 *   `windows` is `status.radar_alignment` (per metric name: `latest_value`, `observed_at` and the
 *   `"60"`/`"300"` second windows); `polling` is `status.radar_alignment_polling`.
 * @returns {{live: boolean, sub: string, axes: Object<string, {latest: string, m1: string, m5: string}>}}
 */
export function radarTileModel(input) {
  const i = isObj(input) ? input : {};
  const axesIn = isObj(i.axes) ? i.axes : {};
  const windows = isObj(i.windows) ? i.windows : {};
  const polling = isObj(i.polling) ? i.polling : null;
  const nowMs = isNum(i.nowMs) ? i.nowMs : Date.now();
  const off = polling !== null && polling.commissioned === false;
  const axes = {};
  let live = false;
  let maxAbs = null;
  let newest = NaN;
  for (let a = 0; a < RADAR_TILE_AXES.length; a += 1) {
    const axis = RADAR_TILE_AXES[a];
    const src = isObj(axesIn[axis.id]) ? axesIn[axis.id] : {};
    const w = isObj(windows[axis.name]) ? windows[axis.name] : {};
    let value = null;
    let at = null;
    if (!off && src.fresh === true && isNum(src.value)) {
      value = src.value;
      live = true;
    } else if (isObj(src.held) && isNum(src.held.value)) {
      value = src.held.value;
      at = src.held.observed_at;
    } else if (isNum(w.latest_value)) {
      value = w.latest_value;
      at = w.observed_at;
    }
    if (value !== null) maxAbs = maxAbs === null ? Math.abs(value) : Math.max(maxAbs, Math.abs(value));
    const atMs = typeof at === "string" ? Date.parse(at) : NaN;
    if (isFinite(atMs) && !(atMs <= newest)) newest = atMs;
    axes[axis.id] = {
      latest: fmtAngleSigned(value),
      m1: windowMean(w["60"]),
      m5: windowMean(w["300"]),
    };
  }
  let sub;
  if (live) {
    sub = maxAbs >= RADAR_TILE_LIMIT ? "outside ±1°" : maxAbs >= RADAR_TILE_NEAR ? "near the ±1° limit" : "within ±1°";
  } else if (maxAbs !== null && isFinite(newest)) {
    sub = "Not reading · last " + fmtTime(newest, new Date(nowMs));
  } else {
    sub = "Not reading";
  }
  return { live, sub, axes };
}

/**
 * Gear estimate text: `~7` while the record is current, else `—`. The broker publishes the
 * estimate only while moving; its quality is below the driver-qualified set, so the tilde stays.
 * @param {{available?: boolean, stale?: boolean, value?: *}|null} rec metric record from the store
 */
export function gearText(rec) {
  if (!rec || rec.available !== true || rec.stale) return DASH;
  const v = typeof rec.value === "string" ? rec.value.trim() : isNum(rec.value) ? String(rec.value) : "";
  return v ? "~" + v : DASH;
}

/**
 * Turbine ÷ output shaft speed as `ratio 2.10`, or "" when either is missing or the output shaft
 * turns too slowly for the ratio to mean anything (< 100 rpm).
 */
export function ratioText(turbine, output) {
  if (!isNum(turbine) || !isNum(output) || output < 100 || turbine < 0) return "";
  return "ratio " + fmtFixed(turbine / output, 2);
}

/**
 * Speed-limit sign text: whole mph, or `—` when the cluster shows no limit (the broker never
 * publishes the frame's 0, so "none" arrives as no live value).
 * @param {number|null} mph live `vehicle.speed_limit`
 */
export function speedLimitText(mph) {
  return isNum(mph) && mph > 0 ? String(Math.round(mph)) : DASH;
}

/** Sub-line word for each published ACC state (`off` has its own value). */
export const ACC_STATE_WORDS = Object.freeze({
  ready: "ready",
  engaged: "engaged",
  override: "accelerator override",
  standby: "standby",
});

/**
 * What the cluster shows beside the ACC state, as sub-line parts: `vehicle ahead` first (it is
 * what changes while driving), then `gap 3 of 4`, or `fixed cruise`. The broker keeps each value
 * until it goes stale, so every part is gated on the state that shows it: a vehicle ahead only
 * while engaged or in override, a gap in every adaptive state, nothing without a state.
 * @param {string|null} st a state with a word in `ACC_STATE_WORDS`, or null
 * @param {{mode?: string|null, bars?: number|null, lead?: boolean|null}|null|undefined} shown
 *   current `acc.mode`, `acc.follow_distance` and `acc.lead_vehicle` record values
 * @returns {string[]}
 */
export function accShownParts(st, shown) {
  if (st === null || !shown) return [];
  if (shown.mode === "fixed") return ["fixed cruise"];
  if (shown.mode !== "adaptive") return [];
  const parts = [];
  if (shown.lead === true && (st === "engaged" || st === "override")) parts.push("vehicle ahead");
  if (isNum(shown.bars) && shown.bars >= 1 && shown.bars <= 4) parts.push("gap " + Math.round(shown.bars) + " of 4");
  return parts;
}

/**
 * ACC tile model.
 * @param {string|null} state current `acc.state` record value, or null when there is no current
 *   record
 * @param {number|null} setSpeed live `acc.set_speed` in mph, or null
 * @param {{mode?: string|null, bars?: number|null, lead?: boolean|null}|null} [shown] current
 *   cruise mode, following-distance bars and lead-vehicle icon (see `accShownParts`)
 * @returns {{value: string, unit: string, sub: string, kind: "live"|"held"|"off"}}
 *   `held` dims the number: standby keeps the set speed in memory but is not controlling.
 */
export function accTileModel(state, setSpeed, shown) {
  const st = typeof state === "string" && Object.prototype.hasOwnProperty.call(ACC_STATE_WORDS, state) ? state : null;
  if (state === "off") return { value: "Off", unit: "", sub: "", kind: "off" };
  const extra = accShownParts(st, shown);
  const sub = (first) => [first].concat(extra).join(" · ");
  if (st === "ready") return { value: DASH, unit: "", sub: sub(extra.length ? "ready" : "ready · no set speed"), kind: "off" };
  if (!isNum(setSpeed) || setSpeed <= 0) return { value: DASH, unit: "", sub: st ? sub(ACC_STATE_WORDS[st]) : "", kind: "off" };
  return {
    value: String(Math.round(setSpeed)),
    unit: "mph",
    sub: st ? sub(extra.length ? ACC_STATE_WORDS[st] : "set · " + ACC_STATE_WORDS[st]) : "set speed",
    kind: st === "standby" ? "held" : "live",
  };
}
