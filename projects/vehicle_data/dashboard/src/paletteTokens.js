/**
 * CSS colour tokens for code that has to compute a colour itself. Themes switch by toggling classes
 * on <html> (theme.js); an observer on that attribute bumps a signal, so a computed that reads tokens
 * here re-reads them after every palette change, once the new class is in place.
 */

import { signal } from "@preact/signals";

const paletteVersion = signal(0);
let observer = null;

function watchPalette() {
  if (observer || typeof MutationObserver !== "function" || typeof document === "undefined") return;
  observer = new MutationObserver(() => { paletteVersion.value += 1; });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
}

/**
 * The current values of `names` (trimmed strings, "" for an unset token). Reading subscribes the
 * caller's computed to palette changes.
 * @param {readonly string[]} names custom property names, e.g. `--red`
 */
export function readTokens(names) {
  paletteVersion.value; // subscribe
  if (typeof document === "undefined") return names.map(() => "");
  watchPalette();
  const style = getComputedStyle(document.documentElement);
  return names.map((name) => style.getPropertyValue(name).trim());
}
