import { tankFigure, tankIsLive, tankRangeText, tankValueText } from "./fuelLevel.js";

/**
 * Pure model of the Fuel tank tile once the gauge may keep counting below the van's own 0 %.
 * The broker's `status.fuel_floor` block (fuel_floor.json is its contract) carries the floor and
 * the open below-empty episode; the van's own tank reading still comes from the metric.
 */

/** The block, or the neutral one (floor 0, no episode) when it is missing or malformed. */
export function floorBlock(block) {
  const floor = block && typeof block === "object" ? block.floor : null;
  if (!Number.isFinite(floor) || floor > 0) return { floor: 0, episode: null };
  const ep = block.episode;
  const episode = ep && typeof ep === "object" && Number.isFinite(ep.adjusted) &&
    Number.isFinite(ep.gallons) && ep.gallons >= 0
    ? { adjusted: ep.adjusted, gallons: ep.gallons, implausible: ep.implausible === true }
    : null;
  return { floor, episode };
}

/** The van's points mapped onto the gauge whose zero is `floor`; the van's 100 stays 100. */
export function rescale(raw, floor) {
  return ((raw - floor) / (100 - floor)) * 100;
}

/** Whether the tile is the plain, pre-floor one (the floor is the van's 0 and no episode is open). */
function plain(model) {
  return model.floor === 0 && model.episode === null;
}

function roundedText(percent) {
  const whole = Math.round(percent);
  return `~${whole === 0 ? 0 : whole}`;
}

/** Sub-line while an episode is open: `est. · 1.4 gal past empty`, then the range figure if any. */
function episodeSub(model, rangeText) {
  const past = model.episode.implausible
    ? "estimate unreliable · bound reached"
    : `est. · ${model.episode.gallons.toFixed(1)} gal past empty`;
  return rangeText ? `${past} · ${rangeText}` : past;
}

/**
 * The tile's numeral, sub-line, liveness and red state.
 * `stale` (status delivery stale) shows an open-episode estimate held, not live.
 * @param {{levelRec: object|null, levelObs: object|null, rangeRec: object|null, rangeObs: object|null, block: object|null, stale?: boolean}} input
 * @returns {{value: string, sub: string, live: boolean, negative: boolean}}
 */
export function tankTileModel({ levelRec, levelObs, rangeRec, rangeObs, block, stale = false }) {
  const model = floorBlock(block);
  const range = tankRangeText(levelRec, levelObs, rangeRec, rangeObs);
  if (plain(model)) {
    return { value: tankValueText(levelRec, levelObs), sub: range, live: tankIsLive(levelRec, levelObs), negative: false };
  }
  if (model.episode !== null) {
    return {
      value: roundedText(model.episode.adjusted),
      sub: episodeSub(model, range),
      live: !stale,
      negative: Math.round(model.episode.adjusted) < 0,
    };
  }
  const van = tankFigure(levelRec, levelObs);
  if (!van) return { value: "", sub: range, live: false, negative: false };
  const adjusted = rescale(van.value, model.floor);
  const own = `van ${van.value}%`;
  return {
    value: roundedText(adjusted),
    sub: range ? `${range} · ${own}` : own,
    live: van.live,
    negative: Math.round(adjusted) < 0,
  };
}
