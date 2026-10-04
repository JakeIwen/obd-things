/**
 * Pure view models shared by the History and System views.
 * Node-testable: no Preact, no store, no DOM. The views wrap each builder in a module-level
 * `computed` over summary slices (60 s cadence), the stable status-lite projection, or one metric
 * record, and pass the result through `createStableGate()`, so a card re-renders only when its
 * visible text changed. Nothing here reads a clock; callers pass `nowMs` (wall, display only) and
 * any monotonic ages explicitly.
 *
 * Owner rules applied to every string built here: plain English, numbers first, no provenance
 * jargon (REGISTERED, ALFA SCALE, MAPPED, n/m LIVE, candidate prose), "stale" never used.
 */

import { DASH, fmtTime, fmtAgeCoarse, fmtInt, parseDate } from "../format.js";

/** Docs page linked once per view. */
export const DOCS = Object.freeze({ caveats: "/docs/caveats.html", warnings: "/docs/warnings.html" });

// ---------------------------------------------------------------------------
// small utilities

export function obj(v) {
  return v && typeof v === "object" && !Array.isArray(v) ? v : null;
}

export function arr(v) {
  return Array.isArray(v) ? v : [];
}

export function num(v) {
  return typeof v === "number" && isFinite(v) ? v : null;
}

export function str(v) {
  return typeof v === "string" && v.trim().length > 0 ? v.trim() : null;
}

/** Wall-clock ms of an ISO string, or NaN. */
export function wallMs(iso) {
  const d = parseDate(iso);
  return d ? d.getTime() : NaN;
}

export function when(iso, nowMs) {
  return parseDate(iso) ? fmtTime(iso, new Date(nowMs)) : null;
}

/** `engine_not_running` → `engine not running`. */
export function humanize(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function capital(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

export function plural(n, one, many) {
  return fmtInt(n) + " " + (n === 1 ? one : many);
}

export function clip(s, max) {
  const t = String(s);
  return t.length > max ? t.slice(0, max - 1) + "…" : t;
}

/**
 * Identity gate for computed view models: returns the previous object when the next one is
 * structurally equal (JSON), so a `computed` wrapping it keeps its value and nothing re-renders.
 */
export function createStableGate() {
  let lastKey = null;
  let last;
  return (next) => {
    const key = JSON.stringify(next);
    if (key === lastKey) return last;
    lastKey = key;
    last = next;
    return next;
  };
}

/**
 * Coarse "how long ago" wording for status lines, at minute resolution (design 5.1): `just now`
 * under a minute, then `2 min ago`, `1.4 h ago`, `2 d ago`; `DASH` when unknown.
 */
export function agoText(ms) {
  const v = num(ms);
  if (v === null) return DASH;
  if (v < 60000) return "just now";
  return fmtAgeCoarse(v) + " ago";
}
