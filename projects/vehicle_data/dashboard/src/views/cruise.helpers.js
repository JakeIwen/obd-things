import { fmtDuration } from "../format.js";

/** Offline recorded-display summary; absent evidence must not look like zero. */
export function cruiseLines(summary) {
  if (!summary || typeof summary !== "object" || !(summary.coverage_percent > 0)) return ["Cruise summary not recorded"];
  const duration = (n) => typeof n === "number" && Number.isFinite(n) && n >= 0 ? fmtDuration(n) : "—";
  const count = (n) => Number.isInteger(n) && n >= 0 ? n : "—";
  const lines = ["Cruise engaged " + duration(summary.engaged_seconds) + " · vehicle ahead " + duration(summary.following_seconds)];
  const c = summary.cancels || {};
  lines.push("Cancels: button " + count(c.button) + " · possible brake " + count(c.brake_associated) +
    " · unknown " + count(c.unknown) + (c.ambiguous > 0 ? " · both " + c.ambiguous : ""));
  const p = summary.comparison || {};
  if (p.paired_seconds > 0 && Number.isFinite(p.mean_delta_mph)) {
    const delta = Math.abs(p.mean_delta_mph).toFixed(1);
    const relation = Math.abs(p.mean_delta_mph) < 0.05 ? "matched the limit on average" :
      "averaged " + delta + " mph " + (p.mean_delta_mph < 0 ? "below" : "above") + " the limit";
    lines.push("Set speed " + relation + " · compared " + duration(p.paired_seconds));
  } else lines.push("No set speed / speed limit comparison recorded");
  lines.push("Recorded " + summary.coverage_percent.toFixed(1) + "% of trip" +
    (summary.recording_complete === false ? " · recording ended early or lost frames" : "") +
    (summary.override_seconds > 0 ? " · accelerator override " + duration(summary.override_seconds) : ""));
  return lines;
}
