/**
 * A tile title at the largest size that keeps it on one line (rule: tileLabel.helpers.js).
 *
 * Measured on mount, when the web font arrives and when the tile's size changes (a
 * ResizeObserver on the parent), never on a data update: the title text is static. The size is set
 * through the CSSOM, which the CSP (style-src 'self') allows; a style attribute it blocks.
 */

import { useLayoutEffect, useRef } from "preact/hooks";
import { fitLabelSize } from "../tileLabel.helpers.js";

/** Measure at the CSS maximum, then settle on the size that fits. */
function fit(el) {
  el.style.removeProperty("font-size");
  const style = getComputedStyle(el);
  const max = parseFloat(style.fontSize);
  const min = parseFloat(style.getPropertyValue("--tile-label-min"));
  const size = fitLabelSize({ natural: el.scrollWidth, available: el.clientWidth, max, min: Number.isFinite(min) ? min : max });
  if (size < max) el.style.setProperty("font-size", size + "px");
}

/** @param {{text: string|{value: string}}} props `text` may be a signal (the ACC/Cruise title) */
export function TileLabel({ text }) {
  const ref = useRef(null);
  const label = text !== null && typeof text === "object" ? text.value : text;
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    fit(el);
    let watch = null;
    const parent = el.parentElement;
    if (parent && typeof ResizeObserver === "function") {
      let width = parent.clientWidth;
      watch = new ResizeObserver(() => {
        if (parent.clientWidth === width) return;
        width = parent.clientWidth;
        fit(el);
      });
      watch.observe(parent);
    }
    // The first fit may run before the web font is in; text widths change when it lands.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { if (el.isConnected) fit(el); });
    return () => { if (watch) watch.disconnect(); };
  }, [label]);
  return <div class="tile__label" ref={ref}>{label}</div>;
}
