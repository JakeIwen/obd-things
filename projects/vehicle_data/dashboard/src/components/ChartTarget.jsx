/**
 * The graph region of a History trend or the Parked battery card as a tap target that opens the full
 * chart dialog (`dialogs/Chart.jsx`) for one metric. A div with a button role, not a <button>: the
 * graphs hold a caption paragraph, which is not allowed inside a button, and the Parked card keeps
 * its separate voltage-read button beside it. Styles: `.chart-target` in `styles/chart.css`.
 */

import { openDialog } from "../app/dialogs.js";
import { RINGLESS_DEFAULT_SPAN as TARGET_SPAN } from "../chart.helpers.js";

/** @param {{name: string, label: string, decimals?: number, children?: any}} props metric name and chart title */
export function ChartTarget({ name, label, decimals, children }) {
  // These graphs are 24 hour graphs, so their chart opens on 24 hours whatever span was used last.
  const open = () => openDialog("chart", { name, label, decimals, startSpan: TARGET_SPAN });
  const onKeyDown = (ev) => {
    if (ev.target === ev.currentTarget && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); open(); }
  };
  return (
    <div
      class="chart-target"
      role="button"
      tabIndex={0}
      aria-haspopup="dialog"
      aria-label={"Open " + label + " chart"}
      onClick={open}
      onKeyDown={onKeyDown}
    >
      {children}
    </div>
  );
}
