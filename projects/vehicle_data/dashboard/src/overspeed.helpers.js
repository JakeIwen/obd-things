/**
 * Speeding display rule, pure and node-testable: more than 15 % over the cluster's speed limit
 * turns the Speed and Speed-limit tiles soft red, and once that has held for five seconds against
 * a current limit, one warning chime sounds. Display information only; it never feeds warnings.
 */

/** Red above `limit × 115 %` (strictly greater), compared in whole percent so 55 → 63.25 is exact. */
export const OVERSPEED_PERCENT = 115;
/** The condition must hold this long, without a break, before the chime. */
export const OVERSPEED_CHIME_DELAY_MS = 5000;
/**
 * After a chime, dips below the threshold shorter than this belong to the same episode, so
 * hovering around the threshold does not chime again; a longer clear spell ends the episode.
 */
export const OVERSPEED_REARM_MS = 5000;

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

/**
 * @param {number|null} speed live vehicle speed, mph (null when not current)
 * @param {number|null} limit live cluster speed limit, mph (null when stale or not shown)
 */
export function isOverspeed(speed, limit) {
  return isNum(speed) && isNum(limit) && limit > 0 && speed * 100 > limit * OVERSPEED_PERCENT;
}

/**
 * @typedef {{limit: number|null, since: number|null, chimed: boolean, clearSince: number|null}} ChimeState
 * `limit` is the limit of the current episode (null: no episode); `since` when the condition
 * started under that limit; `clearSince` when it last stopped holding after a chime.
 */
export const IDLE_CHIME_STATE = Object.freeze({ limit: null, since: null, chimed: false, clearSince: null });

/**
 * Advance the chime episode. One chime per episode; a different speed limit while still over
 * starts a new episode (and so a new five-second wait and chime). Speed changes alone do not.
 * @param {ChimeState} prev
 * @param {number|null} speed live speed or null
 * @param {number|null} limit live (not stale) limit or null
 * @param {number} nowMs monotonic time
 * @returns {{state: ChimeState, chime: boolean}}
 */
export function stepOverspeedChime(prev, speed, limit, nowMs) {
  const s = prev || IDLE_CHIME_STATE;
  if (!isOverspeed(speed, limit)) {
    if (s.limit === null) return { state: s, chime: false };
    // Before the chime the five seconds must be continuous, so any break starts over.
    if (!s.chimed) return { state: IDLE_CHIME_STATE, chime: false };
    const clearSince = s.clearSince === null ? nowMs : s.clearSince;
    if (nowMs - clearSince >= OVERSPEED_REARM_MS) return { state: IDLE_CHIME_STATE, chime: false };
    return { state: clearSince === s.clearSince ? s : { ...s, clearSince }, chime: false };
  }
  if (s.limit !== limit) {
    return { state: { limit, since: nowMs, chimed: false, clearSince: null }, chime: false };
  }
  if (!s.chimed && nowMs - s.since >= OVERSPEED_CHIME_DELAY_MS) {
    return { state: { ...s, chimed: true, clearSince: null }, chime: true };
  }
  return { state: s.clearSince === null ? s : { ...s, clearSince: null }, chime: false };
}
