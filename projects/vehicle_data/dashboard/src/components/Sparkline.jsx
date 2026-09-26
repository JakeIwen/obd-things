/**
 * 15-minute sparkline drawn on one <canvas>.
 *
 * Redraw policy (tablet budget): at most once every 10 s per sparkline, and
 * only when its ring changed or the time window slid by a column. Size is
 * read once on mount and on window resize, never in the redraw path.
 */

import { useEffect, useRef } from "preact/hooks";
import { ringFor, ringVersion } from "../app/rings.js";
import { band } from "../app/derive.js";

const REDRAW_MS = 10000;
/** Minimum vertical span per metric, in its display unit. */
const MIN_SPAN = {
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
const COLUMN_PX = 4;
const mounted = new Set();
let timer = null;
let colours = null;

function readColours() {
  if (colours) return colours;
  const style = getComputedStyle(document.documentElement);
  const get = (name, fallback) => (style.getPropertyValue(name) || "").trim() || fallback;
  colours = {
    line: get("--text-2", "#aab6b1"),
    band: get("--line-strong", "#34443f"),
    dim: get("--dim", "#7f8b87"),
  };
  return colours;
}

function draw(entry, now) {
  const { canvas, name } = entry;
  const ring = ringFor(name);
  const w = entry.width;
  const h = entry.height;
  if (!w || !h) return;
  const ctx = canvas.getContext("2d");
  const dpr = entry.dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const n = Math.max(10, Math.floor(w / COLUMN_PX));
  const cols = ring.columns(n, now);
  let lo = Infinity;
  let hi = -Infinity;
  for (let i = 0; i < cols.length; i += 1) {
    const c = cols[i];
    if (!c) continue;
    if (c.min < lo) lo = c.min;
    if (c.max > hi) hi = c.max;
  }
  if (!isFinite(lo)) return;
  // Scale to the data with a per-metric minimum span (design A10), so small wiggles do not look
  // dramatic; the normal band is clipped to that range (only its nearest edge shows).
  const minSpan = MIN_SPAN[name] || 1;
  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }
  const b = band(name).peek();
  const pad = 2;
  const y = (v) => pad + (h - 2 * pad) * (1 - (v - lo) / (hi - lo));
  const c = readColours();
  if (b && b.lo !== null && b.hi !== null && b.hi > lo && b.lo < hi) {
    ctx.fillStyle = c.band;
    const top = y(Math.min(b.hi, hi));
    ctx.fillRect(0, top, w, Math.max(1, y(Math.max(b.lo, lo)) - top));
  }
  const colW = w / n;
  ctx.strokeStyle = c.line;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let pen = false;
  for (let i = 0; i < cols.length; i += 1) {
    const col = cols[i];
    if (!col) {
      pen = false;
      continue;
    }
    const x = (i + 0.5) * colW;
    const yy = y(col.mean);
    if (pen) ctx.lineTo(x, yy);
    else ctx.moveTo(x, yy);
    pen = true;
  }
  ctx.stroke();
}

function measure(entry) {
  const rect = entry.canvas.getBoundingClientRect();
  entry.dpr = Math.min(2, window.devicePixelRatio || 1);
  entry.width = Math.round(rect.width);
  entry.height = Math.round(rect.height);
  entry.canvas.width = Math.round(entry.width * entry.dpr);
  entry.canvas.height = Math.round(entry.height * entry.dpr);
  entry.drawnVersion = -1;
}

function redrawAll(force) {
  const now = performance.now();
  const version = ringVersion.peek();
  mounted.forEach((entry) => {
    if (!force && entry.drawnVersion === version && now - entry.drawnAt < 60000) return;
    draw(entry, now);
    entry.drawnVersion = version;
    entry.drawnAt = now;
  });
}

function onResize() {
  mounted.forEach(measure);
  redrawAll(true);
}

function ensureTimer() {
  if (timer || !mounted.size) return;
  timer = setInterval(() => {
    if (document.visibilityState === "visible") redrawAll(false);
  }, REDRAW_MS);
  window.addEventListener("resize", onResize, { passive: true });
}

function releaseTimer() {
  if (mounted.size || !timer) return;
  clearInterval(timer);
  timer = null;
  window.removeEventListener("resize", onResize);
}

/** @param {{name: string, big?: boolean}} props */
export function Sparkline({ name, big = false }) {
  const ref = useRef(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return undefined;
    const entry = { canvas, name, width: 0, height: 0, dpr: 1, drawnVersion: -1, drawnAt: 0 };
    mounted.add(entry);
    // Measure after layout settles (one frame), then draw once.
    const raf = requestAnimationFrame(() => {
      measure(entry);
      draw(entry, performance.now());
      entry.drawnVersion = ringVersion.peek();
      entry.drawnAt = performance.now();
    });
    ensureTimer();
    return () => {
      cancelAnimationFrame(raf);
      mounted.delete(entry);
      releaseTimer();
    };
  }, [name]);
  return <canvas ref={ref} class={big ? "spark" : "tile__spark"} aria-hidden="true" />;
}
