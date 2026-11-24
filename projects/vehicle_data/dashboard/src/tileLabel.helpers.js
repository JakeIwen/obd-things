/**
 * Pure rule for the tile title size (owner request: titles about 50 % larger than the old 14 px,
 * unless that would make a title wrap or ellipsize). The CSS holds the two bounds as the
 * `--tile-label-size` (largest) and `--tile-label-min` tokens in `styles/tile-label.css`;
 * `components/TileLabel.jsx` reads them and measures once per layout change.
 */

/** Smallest size step the fit moves in, in px: keeps the result stable under sub-pixel measuring. */
export const FIT_STEP_PX = 0.5;

/**
 * The largest size from `min` to `max` px at which a title fits on one line. A title's width scales
 * linearly with its font size (letter spacing is in em), so one measurement at `max` decides.
 * @param {{natural: number, available: number, max: number, min: number}} input `natural` is the
 *   title's one-line width at `max` px, `available` the width its box has
 * @returns {number} px; `max` when it fits or when nothing can be measured (a hidden label)
 */
export function fitLabelSize({ natural, available, max, min }) {
  if (![natural, available, max, min].every((n) => Number.isFinite(n)) || natural <= 0 || available <= 0) return max;
  if (natural <= available) return max;
  const fitted = Math.floor((max * available / natural) / FIT_STEP_PX) * FIT_STEP_PX;
  return Math.max(min, Math.min(max, fitted));
}
