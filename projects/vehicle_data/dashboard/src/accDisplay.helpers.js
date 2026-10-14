import { ACC_STATE_WORDS } from "./views/driveMore.helpers.js";

export const ACC_METRICS = Object.freeze({
  state: "acc.state", mode: "acc.mode", bars: "acc.follow_distance",
  lead: "acc.lead_vehicle", setSpeed: "acc.set_speed",
});
const EMPTY = Object.freeze({ state: null, mode: null, bars: null, lead: null, setSpeed: null });

/** One current cluster observation; never combine details cached from an older frame. */
export function accDisplayModel(records, connectionState) {
  if (connectionState !== "live") return EMPTY;
  const stateRecord = records[ACC_METRICS.state];
  const fresh = (record) => record?.available === true && !record.stale &&
    record.quality === "verified" && record.source === "ccan.broadcast.0x5a0";
  if (!fresh(stateRecord) || typeof stateRecord.observed_at !== "string" ||
      !stateRecord.observed_at) return EMPTY;
  const state = stateRecord.value;
  if (state === "off") return { ...EMPTY, state };
  if (!Object.prototype.hasOwnProperty.call(ACC_STATE_WORDS, state)) return EMPTY;
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
