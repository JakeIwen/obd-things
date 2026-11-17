/**
 * Pure rules for the Drive sparklines' "red above N" display. Display only: nothing here raises a
 * warning, chime or alert colour; the line and the tile numeral just turn red while a value is high.
 */

/** Metric → value above which its sparkline is drawn red and its tile numeral reads red. */
export const SPARK_RED_ABOVE = Object.freeze({
  // The alternator field's duty near 100 % means the alternator is at its limit.
  "generator.field_duty": 80,
});

/** The red threshold for a metric, or null when it has none. */
export function redAboveFor(name) {
  return Object.hasOwn(SPARK_RED_ABOVE, name) ? SPARK_RED_ABOVE[name] : null;
}

/** True while a live value is strictly above the metric's red threshold. */
export function isAboveRed(name, value) {
  const limit = redAboveFor(name);
  return limit !== null && typeof value === "number" && Number.isFinite(value) && value > limit;
}

/**
 * Split one continuous run of points into polylines coloured by the threshold, adding a point at
 * each crossing so the colour changes exactly where the line meets the threshold.
 * @param {{x: number, v: number}[]} points one unbroken run, left to right
 * @param {number|null} limit red above this value; null draws one normal run
 * @returns {{red: boolean, points: {x: number, v: number}[]}[]}
 */
export function splitByThreshold(points, limit) {
  if (!points.length) return [];
  if (limit === null) return [{ red: false, points: points.slice() }];
  const runs = [];
  const add = (red, from, to) => {
    const last = runs[runs.length - 1];
    if (last && last.red === red) last.points.push(to);
    else runs.push({ red, points: [from, to] });
  };
  if (points.length === 1) return [{ red: points[0].v > limit, points: points.slice() }];
  for (let i = 1; i < points.length; i += 1) {
    const a = points[i - 1];
    const b = points[i];
    if ((a.v - limit) * (b.v - limit) < 0) {
      const t = (limit - a.v) / (b.v - a.v);
      const cross = { x: a.x + t * (b.x - a.x), v: limit };
      add(a.v > limit, a, cross);
      add(b.v > limit, cross, b);
    } else {
      add((a.v + b.v) / 2 > limit, a, b);
    }
  }
  return runs;
}
