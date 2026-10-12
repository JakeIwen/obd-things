/**
 * Radar reference dialog — requests and every displayed string for dialogs/RadarReference.jsx.
 *
 * The dialog zeroes the radar card at the current angles, switches back to any earlier
 * reference, or shows raw angles only. The rules live on the Pi (radar_reference.py): it copies
 * its own last reading and accepts only the exact request bodies from the shared JSON file. No
 * timers; the state is read once when the dialog opens and comes back with every change.
 */

import { fmtTime } from "../format.js";
import { RADAR_REFERENCE, anglesText, exchange, isState, pointText, selectedPoint } from "../radarReference.js";

export const TEXT = Object.freeze({
  title: "Radar reference",
  intro:
    "Zeroing does not change the radar or its calibration. It saves the current angles so the System page can " +
    "show how far they have moved since. Every earlier reference stays in the list, and each finished drive " +
    "calibration saves one too.",
  now: "Angles now",
  since: "Showing change since",
  none: "Nothing: raw angles only",
  noReading: "No radar reading yet. Angles are read while the engine runs.",
  checking: "Checking…",
  zero: "Zero at these angles",
  zeroing: "Saving…",
  clear: "Show raw angles only",
  listTitle: "Saved references",
  empty: "Nothing saved yet.",
  use: "Use",
  inUse: "In use",
  readAt: " · read ",
  waiting: "A finished calibration is waiting for its first radar reading.",
  statusError: "Could not read the saved references: ",
  requestError: "Request failed: ",
});

/** A point's reading is called out when it is this much older than the moment it was saved. */
const OLD_READING_MS = 60000;

const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const message = (error) => String((error && error.message) || error);

export function initialState() {
  return { busy: null, error: null, notice: null };
}

function readingNote(point, now) {
  const saved = Date.parse(point.created_at);
  const read = Date.parse(point.observed_at);
  return isFinite(saved) && isFinite(read) && saved - read > OLD_READING_MS ? TEXT.readAt + fmtTime(point.observed_at, now) : "";
}

/**
 * Everything the dialog renders.
 * @param {object} ui controller state
 * @param {object|null} state last reference state from the Pi
 * @param {Date} now display clock
 */
export function viewModel(ui, state, now) {
  const s = isState(state) ? state : null;
  const selected = selectedPoint(s);
  const current = s ? obj(s.current) : null;
  const usable = Boolean(s) && s.available !== false && ui.busy === null;
  const storage = s && typeof s.storage_error === "string" ? s.storage_error : "";
  let currentText = TEXT.checking;
  if (current) currentText = anglesText(current.angles) + TEXT.readAt + fmtTime(current.observed_at, now);
  else if (s) currentText = TEXT.noReading;
  return {
    currentText,
    selectedText: selected ? pointText(selected, now) + " (" + anglesText(selected.angles) + ")" : TEXT.none,
    canZero: usable && Boolean(current),
    canClear: usable && Boolean(selected),
    zeroLabel: ui.busy === "zero" ? TEXT.zeroing : TEXT.zero,
    rows: (s ? s.points : []).filter(obj).map((point) => ({
      id: point.id,
      title: pointText(point, now),
      meta: anglesText(point.angles) + readingNote(point, now),
      selected: selected !== null && point.id === selected.id,
      canUse: usable,
    })),
    statusText: ui.error || ui.notice || storage || (s && obj(s.pending_calibration) ? TEXT.waiting : ""),
    statusIsError: Boolean(ui.error) || (!ui.notice && Boolean(storage)),
  };
}

/**
 * Controller for one open dialog.
 * @param {{fetch: Function, onState?: (state: object) => void, onChange?: (ui: object) => void}} deps
 *   `onState` receives every reference state the Pi returns.
 */
export function createRadarReferenceController(deps) {
  const d = { onState: () => {}, onChange: () => {}, ...deps };
  let ui = initialState();
  let disposed = false;

  function set(next) {
    ui = next;
    if (!disposed) d.onChange(ui);
  }

  async function run(kind, body) {
    if (disposed || ui.busy !== null) return false;
    set({ busy: kind, error: null, notice: null });
    try {
      const reply = await exchange(d.fetch, body);
      if (disposed) return false;
      if (isState(reply.payload)) d.onState(reply.payload);
      const refused = reply.ok ? null : typeof reply.payload.detail === "string" ? reply.payload.detail : "refused";
      set({ busy: null, error: null, notice: refused });
      return reply.ok;
    } catch (error) {
      if (disposed) return false;
      set({ busy: null, error: (body === undefined ? TEXT.statusError : TEXT.requestError) + message(error), notice: null });
      return false;
    }
  }

  return {
    refresh: () => run("load"),
    zero: () => run("zero", RADAR_REFERENCE.zero_request),
    clear: () => run("clear", RADAR_REFERENCE.clear_request),
    select: (id) => run("select", { action: RADAR_REFERENCE.select_action, id }),
    dispose() {
      disposed = true;
    },
    state: () => ui,
  };
}
