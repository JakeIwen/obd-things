/**
 * Drive page switching (design 3.1.1). Kept apart from the page-2 helpers so the always-loaded
 * Drive view pulls in only this.
 */

/** Pager segments: page number and its short name. */
export const DRIVE_PAGE_NAMES = Object.freeze([
  Object.freeze([1, "Gauges"]),
  Object.freeze([2, "Sensors"]),
]);

/**
 * Horizontal swipe → Drive page. Returns the page to show, or `current` when the gesture was not
 * a deliberate horizontal swipe (≥ 80 px, at least twice as wide as tall, under 800 ms).
 * Swiping left (negative dx) goes to page 2, right goes back to page 1.
 */
export function swipeTarget(dx, dy, ms, current) {
  const ok = (v) => typeof v === "number" && isFinite(v);
  if (!ok(dx) || !ok(dy) || !ok(ms)) return current;
  if (ms > 800 || Math.abs(dx) < 80 || Math.abs(dx) < 2 * Math.abs(dy)) return current;
  return dx < 0 ? 2 : 1;
}
