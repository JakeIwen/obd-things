/**
 * Pure geometry for the Drive sparklines' heat display. Which metrics shade, and across which range,
 * is the numeral's rule (`TILE_HEAT` in tileHeat.helpers.js); `components/Sparkline.jsx` draws the
 * part of the line above the range's start in the same yellow → orange → red gradient. Display only:
 * nothing here raises a warning, chime or alert colour.
 */

/** Minimum vertical span per metric, in its display unit. */
export const MIN_SPAN = {
  "engine.coolant_temperature": 20,
  "transmission.oil_temperature": 20,
  "engine.vvt_oil_temperature": 20,
  "engine.oil_pressure": 10,
  "battery.voltage": 0.5,
  "engine.crankshaft_power": 20,
  "engine.rpm": 500,
  "vehicle.speed": 10,
  "generator.field_duty": 20,
};

/**
 * Split one continuous run of points into polylines on either side of a threshold, adding a point
 * at each crossing so the colour changes exactly where the line meets the threshold.
 * @param {{x: number, v: number}[]} points one unbroken run, left to right
 * @param {number|null} limit hot above this value; null draws one normal run
 * @returns {{hot: boolean, points: {x: number, v: number}[]}[]}
 */
export function splitByThreshold(points, limit) {
  if (!points.length) return [];
  if (limit === null) return [{ hot: false, points: points.slice() }];
  const runs = [];
  const add = (hot, from, to) => {
    const last = runs[runs.length - 1];
    if (last && last.hot === hot) last.points.push(to);
    else runs.push({ hot, points: [from, to] });
  };
  if (points.length === 1) return [{ hot: points[0].v > limit, points: points.slice() }];
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
