/**
 * Canvas painter for the full chart dialog's model (`chart.helpers.js: buildChartModel`).
 *
 * Canvas rather than SVG: the sparklines are canvas too, a 7-day chart is a few hundred points
 * (no per-node cost either way), but one bitmap redraw per update is the cheapest thing for the
 * tablet's browser, with no DOM to diff while the live span refreshes. The cost is that canvas
 * pixels do not follow CSS tokens, so the dialog re-reads `readChartColours()` on a palette change.
 */

import { splitByThreshold } from "./sparkline.helpers.js";
import { HEAT_TOKENS } from "./tileHeat.helpers.js";

const AXIS_FONT_PX = 14;
const PAD = { top: 12, right: 16, bottom: 30, left: 12 };
const LABEL_GAP = 8;
const ENVELOPE_ALPHA = 0.2;
const WHISKER_ALPHA = 0.3;

/** Space a time label needs along the axis: a clock time like "12:45 pm" plus breathing room. */
export const TIME_LABEL_SLOT_PX = 96;

/** The palette's colours as this painter uses them (strings; "" for an unset token). */
export function readChartColours() {
  const style = getComputedStyle(document.documentElement);
  const get = (name) => style.getPropertyValue(name).trim();
  return {
    line: get("--spark-line"),
    band: get("--band-ok"),
    grid: get("--line"),
    axis: get("--line-strong"),
    text: get("--text-2"),
    heat: HEAT_TOKENS.map(get),
    font: getComputedStyle(document.body).fontFamily,
  };
}

/** Room the time axis has for labels at this canvas width, before the y-axis width is known. */
export function maxTimeTicksFor(widthPx) {
  return Math.max(2, Math.floor((widthPx - PAD.left - PAD.right) / TIME_LABEL_SLOT_PX));
}

/**
 * Paint one chart.
 * @param {CanvasRenderingContext2D} ctx already scaled to CSS pixels
 * @param {number} w canvas width in CSS px
 * @param {number} h canvas height in CSS px
 * @param {ReturnType<import("./chart.helpers.js").buildChartModel>} model
 * @param {ReturnType<typeof readChartColours>} colours
 * @param {{from: number, to: number}|null} heat the metric's heat range, if any
 */
export function drawChart(ctx, w, h, model, colours, heat) {
  ctx.clearRect(0, 0, w, h);
  ctx.font = `${AXIS_FONT_PX}px ${colours.font}`;
  const yLabels = model.empty ? [] : model.y.ticks.map((t) => t.label);
  const labelWidth = yLabels.reduce((m, text) => Math.max(m, ctx.measureText(text).width), 0);
  const left = PAD.left + (labelWidth ? labelWidth + LABEL_GAP : 0);
  const right = w - PAD.right;
  const top = PAD.top;
  const bottom = h - PAD.bottom;
  if (right - left < 20 || bottom - top < 20) return;
  const span = model.endMs - model.startMs;
  const x = (t) => left + ((t - model.startMs) / span) * (right - left);

  ctx.textBaseline = "top";
  ctx.textAlign = "center";
  ctx.lineWidth = 1;
  // Time axis: faint vertical grid and the labels under the plot.
  for (const tick of model.xTicks) {
    const px = Math.round(x(tick.t)) + 0.5;
    ctx.strokeStyle = colours.grid;
    ctx.beginPath();
    ctx.moveTo(px, top);
    ctx.lineTo(px, bottom);
    ctx.stroke();
    ctx.fillStyle = colours.text;
    // Keep the end labels inside the canvas rather than clipped.
    const half = ctx.measureText(tick.label).width / 2;
    ctx.fillText(tick.label, Math.min(w - half - 2, Math.max(half + 2, px)), bottom + 8);
  }
  ctx.strokeStyle = colours.axis;
  ctx.beginPath();
  ctx.moveTo(left, bottom + 0.5);
  ctx.lineTo(right, bottom + 0.5);
  ctx.stroke();
  if (model.empty) return;

  const { y } = model;
  const py = (v) => bottom - ((v - y.lo) / (y.hi - y.lo)) * (bottom - top);
  if (model.band) {
    ctx.fillStyle = colours.band;
    ctx.globalAlpha = 0.3;
    const yTop = py(model.band.hi);
    ctx.fillRect(left, yTop, right - left, Math.max(1, py(model.band.lo) - yTop));
    ctx.globalAlpha = 1;
  }
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (const tick of y.ticks) {
    const ty = Math.round(py(tick.v)) + 0.5;
    ctx.strokeStyle = colours.grid;
    ctx.beginPath();
    ctx.moveTo(left, ty);
    ctx.lineTo(right, ty);
    ctx.stroke();
    ctx.fillStyle = colours.text;
    ctx.fillText(tick.label, left - LABEL_GAP, ty);
  }

  if (model.envelope) {
    ctx.fillStyle = colours.line;
    ctx.globalAlpha = ENVELOPE_ALPHA;
    for (const run of model.runs) {
      if (run.length < 2) continue;
      ctx.beginPath();
      run.forEach((p, i) => (i ? ctx.lineTo(x(p.t), py(p.max)) : ctx.moveTo(x(p.t), py(p.max))));
      for (let i = run.length - 1; i >= 0; i -= 1) ctx.lineTo(x(run[i].t), py(run[i].min));
      ctx.closePath();
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  // A bucket with no neighbour has no band to join, so its range is a whisker behind the mean dot.
  if (model.whiskers.length) {
    ctx.strokeStyle = colours.line;
    ctx.globalAlpha = WHISKER_ALPHA;
    ctx.lineWidth = 5;
    ctx.lineCap = "round";
    for (const w of model.whiskers) {
      ctx.beginPath();
      ctx.moveTo(x(w.t), py(w.max));
      ctx.lineTo(x(w.t), py(w.min));
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.lineCap = "butt";
  }

  // The same yellow, orange, red as the numeral, laid along the value axis above the range's start.
  let hotStyle = colours.line;
  if (heat && colours.heat.every(Boolean)) {
    hotStyle = ctx.createLinearGradient(0, py(heat.from), 0, py(heat.to));
    colours.heat.forEach((c, i) => hotStyle.addColorStop(i / (colours.heat.length - 1), c));
  }
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  for (const run of model.runs) {
    if (run.length === 1) {
      ctx.fillStyle = colours.line;
      ctx.beginPath();
      ctx.arc(x(run[0].t), py(run[0].mean), 2.5, 0, Math.PI * 2);
      ctx.fill();
      continue;
    }
    const pts = run.map((p) => ({ x: x(p.t), v: p.mean }));
    for (const part of splitByThreshold(pts, heat ? heat.from : null)) {
      ctx.strokeStyle = part.hot ? hotStyle : colours.line;
      ctx.beginPath();
      part.points.forEach((p, i) => (i ? ctx.lineTo(p.x, py(p.v)) : ctx.moveTo(p.x, py(p.v))));
      ctx.stroke();
    }
  }
}
