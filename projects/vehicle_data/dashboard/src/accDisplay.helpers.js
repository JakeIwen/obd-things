import { DASH } from "./format.js";

/** @typedef {{state: string|null, mode: string|null, bars: number|null,
 * lead: boolean|null, setSpeed: number|null}} AccDisplay */

/** Sub-line word for each published ACC state (`off` has its own value). */
export const ACC_STATE_WORDS = Object.freeze({
  ready: "ready",
  engaged: "engaged",
  override: "accelerator override",
  standby: "standby · saved speed",
});

/**
 * Text beside the state. The distance setting has its own four-bar graphic;
 * the lead-vehicle indication appears only during adaptive engagement/override.
 * @param {string|null} st a state with a word in `ACC_STATE_WORDS`, or null
 * @param {{mode?: string|null, lead?: boolean|null}|null|undefined} shown
 *   current cruise mode and lead-vehicle record values
 * @returns {string[]}
 */
export function accShownParts(st, shown) {
  if (st === null || !shown) return [];
  if (shown.mode === "fixed") return ["fixed cruise"];
  if (shown.mode !== "adaptive") return [];
  const parts = [];
  if (shown.lead === true && (st === "engaged" || st === "override")) parts.push("vehicle ahead");
  return parts;
}

/**
 * ACC tile model.
 * @param {string|null} state current `acc.state` record value, or null when there is no current
 *   record
 * @param {number|null} setSpeed live `acc.set_speed` in mph, or null
 * @param {{mode?: string|null, lead?: boolean|null}|null} [shown] current
 *   cruise mode and lead-vehicle icon (see `accShownParts`)
 * @returns {{value: string, unit: string, sub: string, kind: "live"|"held"|"off"}}
 *   `held` dims the number: standby keeps the set speed in memory but is not controlling.
 */
export function accTileModel(state, setSpeed, shown) {
  const st = typeof state === "string" && Object.prototype.hasOwnProperty.call(ACC_STATE_WORDS, state) ? state : null;
  if (state === "off") return { value: "Off", unit: "", sub: "", kind: "off" };
  if (st === null) return { value: DASH, unit: "", sub: "", kind: "off" };
  const extra = accShownParts(st, shown);
  const sub = (first) => [first].concat(extra).join(" · ");
  if (st === "ready") return { value: DASH, unit: "", sub: sub(extra.length ? "ready" : "ready · no set speed"), kind: "off" };
  if (!Number.isFinite(setSpeed) || setSpeed <= 0) return { value: DASH, unit: "", sub: st ? sub(ACC_STATE_WORDS[st]) : "", kind: "off" };
  return {
    value: String(Math.round(setSpeed)),
    unit: "mph",
    sub: sub(ACC_STATE_WORDS[st]),
    kind: st === "standby" ? "held" : "live",
  };
}

export const ACC_METRICS = Object.freeze({
  state: "acc.state", mode: "acc.mode", bars: "acc.follow_distance",
  lead: "acc.lead_vehicle", setSpeed: "acc.set_speed",
});
const EMPTY = Object.freeze({ state: null, mode: null, bars: null, lead: null, setSpeed: null });

/** One current cluster observation; never combine details cached from an older frame.
 * @returns {AccDisplay}
 */
export function accDisplayModel(records, connectionState) {
  if (connectionState !== "live") return EMPTY;
  const stateRecord = records[ACC_METRICS.state];
  const fresh = (record) => record?.available === true && !record.stale &&
    record.quality === "verified" && record.source === "ccan.broadcast.0x5a0";
  if (!fresh(stateRecord) || typeof stateRecord.observed_at !== "string" ||
      !stateRecord.observed_at) return EMPTY;
  const state = stateRecord.value;
  if (state === "off") return { ...EMPTY, state };
  if (typeof state !== "string" || !Object.prototype.hasOwnProperty.call(ACC_STATE_WORDS, state)) return EMPTY;
  const read = (key, type) => {
    const record = records[ACC_METRICS[key]];
    return fresh(record) && record.observed_at === stateRecord.observed_at &&
      typeof record.value === type ? record.value : null;
  };
  const rawMode = read("mode", "string");
  const mode = rawMode === "adaptive" || rawMode === "fixed" ? rawMode : null;
  const rawBars = read("bars", "number");
  const bars = mode === "adaptive" && Number.isInteger(rawBars) && rawBars >= 1 && rawBars <= 4
    ? rawBars : null;
  const lead = mode === "adaptive" && (state === "engaged" || state === "override")
    ? read("lead", "boolean") : null;
  const speed = read("setSpeed", "number");
  const setSpeed = state !== "ready" && Number.isFinite(speed) && speed > 0 ? speed : null;
  return { state, mode, bars, lead, setSpeed };
}
