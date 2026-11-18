/**
 * Metric tile and band bar. Every changing piece is a signal bound directly
 * to a text node or attribute, so the component renders once and later
 * updates touch only the node whose text changed.
 */

import { computed, useComputed, useSignalEffect } from "@preact/signals";
import { useRef } from "preact/hooks";
import { liveValue, tileModel } from "../app/derive.js";
import { readTokens } from "../paletteTokens.js";
import { HEAT_TOKENS, heatFraction, heatRangeFor, mixHeat } from "../tileHeat.helpers.js";
import { Sparkline } from "./Sparkline.jsx";

/** The heat stops' colours in the current palette, re-read on a theme change. */
const heatStops = computed(() => readTokens(HEAT_TOKENS));

/**
 * Horizontal band bar (SVG, attribute-bound). Hidden for metrics without a band.
 * @param {{name: string}} props
 */
export function BandBar({ name }) {
  const m = tileModel(name);
  const hidden = useComputed(() => m.geometry.value === null);
  const okX = useComputed(() => (m.geometry.value ? m.geometry.value.lo.toFixed(2) : "0"));
  const okW = useComputed(() => {
    const g = m.geometry.value;
    return g ? Math.max(0, g.hi - g.lo).toFixed(2) : "0";
  });
  const markX = useComputed(() => {
    const g = m.geometry.value;
    return g && g.marker !== null ? Math.max(0, Math.min(98.5, g.marker - 0.75)).toFixed(2) : "-10";
  });
  const markClass = useComputed(() => {
    const g = m.geometry.value;
    const s = g ? g.state : "none";
    return s === "red" ? "band__marker band__marker--red" : s === "amber" ? "band__marker band__marker--amber" : "band__marker";
  });
  return (
    <svg class="tile__band" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true" hidden={hidden}>
      <rect class="band__track" x="0" y="2" width="100" height="2" />
      <rect class="band__ok" x={okX} y="2" width={okW} height="2" />
      <rect class={markClass} x={markX} y="0" width="1.5" height="6" />
    </svg>
  );
}

/**
 * A metric tile.
 * @param {{name: string, label: string, area?: string, size?: 'hero'|'large'|'value',
 *          band?: boolean, spark?: boolean, sub?: import('@preact/signals').ReadonlySignal<string>,
 *          extra?: any, detail?: any, onClick?: Function, decimals?: number, showUnit?: boolean,
 *          overspeed?: import('@preact/signals').ReadonlySignal<boolean>, value?: any}} props
 *   `sub` is an optional live sub-line (e.g. the torque under Power); when the value is
 *   held, the held time stamp replaces it. `overspeed` tints the value soft red while true.
 *   `value` is an optional node replacing the numeral text (state, held time stamp, colour and band stay the metric's, so it must not imply fresher data than that state).
 *   `extra` renders beside the numeral; `detail` renders under it, above the band and sub-line.
 */
export function Tile({ name, label, area, size = "value", band = false, spark = false, sub, extra, detail, onClick, decimals, showUnit = true, overspeed, value }) {
  const m = tileModel(name, decimals === undefined ? undefined : { decimals });
  // Metrics with a heat range (tileHeat.helpers.js) shade their numeral yellow → orange → red while live.
  const hasHeat = heatRangeFor(name) !== null;
  const cls = useComputed(() => {
    let c = "tile tile--" + size + " tile--" + m.kind.value;
    if (area) c += " area-" + area;
    const colour = m.colour.value;
    if (colour) c += " tile--" + colour;
    if (overspeed && overspeed.value) c += " tile--overspeed";
    return c;
  });
  // Set through the CSSOM, which the CSP (style-src 'self') allows; a style attribute it blocks.
  const valueRef = useRef(null);
  useSignalEffect(() => {
    if (!hasHeat) return;
    const fraction = heatFraction(name, liveValue(name).value);
    const colour = fraction === null ? "" : mixHeat(heatStops.value, fraction);
    const el = valueRef.current;
    if (!el) return;
    if (colour) el.style.setProperty("color", colour);
    else el.style.removeProperty("color");
  });
  const subText = useComputed(() => {
    const held = m.heldText.value;
    if (held) return held;
    return sub ? sub.value : "";
  });
  return (
    <section class={cls} aria-label={label} onClick={onClick}>
      <div class="tile__label">{label}</div>
      <div class="tile__value num" ref={valueRef}>
        <span>{value || m.valueText}</span>
        {showUnit ? <span class="tile__unit">{m.unitText}</span> : null}
        {extra}
      </div>
      {detail}
      {band ? <BandBar name={name} /> : null}
      {spark ? <Sparkline name={name} /> : null}
      <div class="tile__sub">{subText}</div>
    </section>
  );
}
