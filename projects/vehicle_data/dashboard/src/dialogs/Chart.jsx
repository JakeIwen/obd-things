/* Full chart dialog: a tile's graph opened large, with axis labels and a choice of time span. Opened
   by tapping a Drive tile that has a sparkline (components/Tile.jsx). Lazily loaded:
     const Chart = (await import("./dialogs/Chart.jsx")).default;
   Props: { open, onClose(), name, label, decimals? }.
   15 min is the browser's own ring, redrawn at most every 5 s while open; longer spans come from
   GET /v1/history/series (contract: history_series.json), refetched on its refresh interval and
   aborted when the span changes or the dialog closes. Rules in chart.helpers.js, painting in
   chartDraw.js, styles in styles/chart.css. A tap outside the pane, the × button and Esc close it.
   No inline styles (CSP): the canvas is sized through its width/height properties. */
import { useEffect, useRef, useState } from "preact/hooks";
import { band, tileModel } from "../app/derive.js";
import { ringFor } from "../app/rings.js";
import { drawChart, maxTimeTicksFor, readChartColours } from "../chartDraw.js";
import {
  DEFAULT_SPAN, LIVE_REDRAW_MS, SERIES_REFRESH_MS, SPANS, buildChartModel, historySeries, liveSeries, seriesUrl,
  spanByKey, unavailableText,
} from "../chart.helpers.js";
import { readReply } from "../jsonReply.js";
import { MIN_SPAN } from "../sparkline.helpers.js";
import { heatRangeFor } from "../tileHeat.helpers.js";
import { useModalDialog } from "./modalDialog.js";

/** The span last chosen, kept for the page's lifetime only (not storage), as the owner asked. */
let lastSpan = DEFAULT_SPAN;
/** Live columns: one per ~6 px of plot, bounded so a wide desktop pane stays cheap. */
const LIVE_COLUMN_PX = 6;
const LIVE_MIN_COLUMNS = 40;
const LIVE_MAX_COLUMNS = 160;

/** Fetch one history span; resolves `{series}` or `{note}` (what to say instead of a chart). */
async function loadHistory(name, spanKey, signal) {
  let response;
  try {
    response = await fetch(seriesUrl(name, spanKey), { cache: "no-store", signal });
  } catch (error) {
    if (signal.aborted) return null;
    return { note: unavailableText("") };
  }
  if (response.status === 404) return { note: unavailableText("not_found") };
  let reply;
  try {
    reply = (await readReply(response)).payload;
  } catch (error) {
    return { note: unavailableText("") };
  }
  if (reply.available === false) return { note: unavailableText(reply.reason) };
  const series = historySeries(reply);
  return series ? { series } : { note: unavailableText("") };
}

export default function Chart({ open, onClose, name, label, decimals }) {
  const closeRef = useRef(null);
  const canvasRef = useRef(null);
  const frameRef = useRef(null);
  const [spanKey, setSpanKey] = useState(lastSpan);
  const [note, setNote] = useState("");
  const live = useRef({ series: null, size: { w: 0, h: 0 }, colours: null, spanKey });
  const model = tileModel(name, decimals === undefined ? undefined : { decimals });
  const { dialogRef, close } = useModalDialog({ open, onClose, focusRef: closeRef });
  const onBackdrop = (ev) => { if (ev.target === dialogRef.current) close(); };

  // Paint from the state in `live`; cheap, so every trigger (data, size, palette) calls it.
  const paint = () => {
    const canvas = canvasRef.current;
    const state = live.current;
    const { w, h } = state.size;
    if (!canvas || !w || !h) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!state.colours) state.colours = readChartColours();
    const chart = buildChartModel({
      series: state.series, name, spanKey: state.spanKey, band: band(name).peek(), minSpan: MIN_SPAN[name] || 1,
      maxTimeTicks: maxTimeTicksFor(w), decimals,
    });
    drawChart(ctx, w, h, chart, state.colours, heatRangeFor(name));
    if (chart.empty && state.series) setNote("No data in this span");
    else if (!chart.empty) setNote("");
  };

  // Size, theme and resize follow-ups, for the dialog's lifetime.
  useEffect(() => {
    const frame = frameRef.current;
    const size = () => {
      live.current.size = { w: Math.round(frame.clientWidth), h: Math.round(frame.clientHeight) };
      paint();
    };
    const resizeWatch = typeof ResizeObserver === "function" ? new ResizeObserver(size) : null;
    if (resizeWatch) resizeWatch.observe(frame);
    // Canvas pixels do not inherit changed CSS tokens: repaint once when the palette class changes.
    const paletteWatch = new MutationObserver(() => { live.current.colours = null; paint(); });
    paletteWatch.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    size();
    return () => {
      if (resizeWatch) resizeWatch.disconnect();
      paletteWatch.disconnect();
    };
  }, [name]);

  // Data for the chosen span: the ring for the live span, the history route for the others.
  useEffect(() => {
    const spec = spanByKey(spanKey);
    live.current.spanKey = spanKey;
    live.current.series = null;
    setNote("");
    if (!spec) return undefined;
    if (spec.source === "live") {
      const ring = ringFor(name);
      const refresh = () => {
        const { w } = live.current.size;
        const n = Math.max(LIVE_MIN_COLUMNS, Math.min(LIVE_MAX_COLUMNS, Math.floor(w / LIVE_COLUMN_PX)));
        live.current.series = liveSeries(ring.columns(n, performance.now()), Date.now(), ring.windowMs);
        paint();
      };
      refresh();
      const timer = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, LIVE_REDRAW_MS);
      return () => clearInterval(timer);
    }
    let controller = null;
    const fetchSpan = async () => {
      if (controller) controller.abort();
      controller = new AbortController();
      const mine = controller;
      const result = await loadHistory(name, spanKey, mine.signal);
      if (!result || mine.signal.aborted) return;
      if (result.series) {
        live.current.series = result.series;
        paint();
      } else if (!live.current.series) {
        setNote(result.note);
        paint();
      }
    };
    setNote("Loading…");
    fetchSpan();
    const timer = setInterval(() => { if (document.visibilityState === "visible") fetchSpan(); }, SERIES_REFRESH_MS);
    return () => {
      clearInterval(timer);
      if (controller) controller.abort();
    };
  }, [name, spanKey]);

  const choose = (key) => { lastSpan = key; setSpanKey(key); };

  return (
    <dialog ref={dialogRef} class="dialog chart-dialog" aria-labelledby="chart-title" onClick={onBackdrop}>
      <header class="dialog__head chart__head">
        <h2 id="chart-title" class="dialog__title">{label}</h2>
        <div class="chart__now num" aria-label="Current value">
          <span>{model.valueText}</span>
          <span class="chart__unit">{model.unitText}</span>
        </div>
        <button type="button" class="dialog__close" aria-label={"Close " + label + " chart"} ref={closeRef} onClick={close}>×</button>
      </header>
      <div class="chart__spans" role="group" aria-label="Time span">
        {SPANS.map((span) => (
          <button
            key={span.key}
            type="button"
            class={"chart__span" + (span.key === spanKey ? " chart__span--on" : "")}
            aria-pressed={span.key === spanKey ? "true" : "false"}
            onClick={() => choose(span.key)}
          >
            {span.label}
          </button>
        ))}
      </div>
      <div class="chart__frame" ref={frameRef}>
        <canvas ref={canvasRef} class="chart__canvas" role="img" aria-label={label + " over the last " + (spanByKey(spanKey) ? spanByKey(spanKey).label : "")} />
        {note ? <p class="chart__note" role="status">{note}</p> : null}
      </div>
    </dialog>
  );
}
