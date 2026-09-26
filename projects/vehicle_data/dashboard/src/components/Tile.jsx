/**
 * Metric tile and band bar. Every changing piece is a signal bound directly
 * to a text node or attribute, so the component renders once and later
 * updates touch only the node whose text changed.
 */

import { useComputed } from "@preact/signals";
import { tileModel } from "../app/derive.js";
import { Sparkline } from "./Sparkline.jsx";

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
 *          extra?: any, onClick?: Function, decimals?: number, showUnit?: boolean}} props
 *   `sub` is an optional live sub-line (e.g. alternator duty); when the value is
 *   held, the held time stamp replaces it.
 */
export function Tile({ name, label, area, size = "value", band = false, spark = false, sub, extra, onClick, decimals, showUnit = true }) {
  const m = tileModel(name, decimals === undefined ? undefined : { decimals });
  const cls = useComputed(() => {
    let c = "tile tile--" + size + " tile--" + m.kind.value;
    if (area) c += " area-" + area;
    const colour = m.colour.value;
    if (colour) c += " tile--" + colour;
    return c;
  });
  const subText = useComputed(() => {
    const held = m.heldText.value;
    if (held) return held;
    return sub ? sub.value : "";
  });
  return (
    <section class={cls} aria-label={label} onClick={onClick}>
      <div class="tile__label">{label}</div>
      <div class="tile__value num">
        <span>{m.valueText}</span>
        {showUnit ? <span class="tile__unit">{m.unitText}</span> : null}
        {extra}
      </div>
      {band ? <BandBar name={name} /> : null}
      {spark ? <Sparkline name={name} /> : null}
      <div class="tile__sub">{subText}</div>
    </section>
  );
}
