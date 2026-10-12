/**
 * Radar drive calibration (ACC radar Service Drive Alignment) — requests, polling and every
 * displayed string for dialogs/RadarCalibration.jsx.
 *
 * This starts a routine in the radar itself, so the rules live on the Pi: the listener must
 * have it enabled, the broker accepts only the exact request bodies below, and the helper that
 * owns the vehicle bus refuses to start unless the engine is running and the van is stopped.
 * The checks here only decide which button to offer and what to say. State names, end reasons
 * and both request bodies come from the JSON file the broker and helper load as well.
 *
 * The only timer is the status poll while the dialog is open (2 s, 5 s after an error).
 */

import vocabulary from "../../../radar_service_alignment.json" with { type: "json" };
import { readReply } from "../jsonReply.js";

export const RADAR_SERVICE = Object.freeze(vocabulary);
export const POLL_MS = 2000;
export const POLL_ERROR_MS = 5000;
/** A refusal's explanation stays for this many status polls, then gives way to the live hint. */
export const NOTICE_POLLS = 4;
const S = RADAR_SERVICE.states;
const ACTIVE = new Set(RADAR_SERVICE.active_states);
/** The one refusal recognised by name: this listener does not offer the feature at all. */
const isOff = (payload) => payload.reason === RADAR_SERVICE.listener_disabled_reason;

export const TEXT = Object.freeze({
  title: "Radar drive calibration",
  intro:
    "Starts the radar's own drive alignment (the routine that cleared the ACC fault in June 2026). " +
    "Start it stopped with the engine running, then drive straight and steady at about 30–45 mph on a clear road. " +
    "It took about 17 minutes of steady driving last time; there is no fixed length.",
  caution:
    "Cruise control and forward-collision warning may be unavailable or wrong while this runs, and a finished " +
    "calibration changes how the radar aims. Check that the radar is seated in its bracket before calibrating.",
  confirm: "I am stopped with the engine running, and I will drive without relying on cruise control or collision warning until it finishes.",
  start: "Start calibration",
  starting: "Starting…",
  stop: "Stop calibration",
  stopping: "Stopping…",
  checking: "Checking…",
  disabled: "Drive calibration is not enabled on this dashboard address.",
  needEngine: "Start the engine and wait a few seconds for telemetry.",
  needRadar: "Telemetry is running but the radar link is not ready yet.",
  needStop: "Stop the van to start.",
  needSpeed: "Waiting for a vehicle speed reading.",
  needConfirm: "Tick the box above to start.",
  ready: "Ready to start.",
  drive: "Keep driving straight and steady. Stopping the engine ends the calibration.",
  statusError: "Could not read the calibration status: ",
  requestError: "Request failed: ",
});

const HEADLINE = Object.freeze({
  [S.requested]: "Starting…",
  [S.starting]: "Starting…",
  [S.running]: "Calibrating",
  [S.stopping]: "Stopping…",
  [S.completed]: "Calibration complete",
  [S.stopped]: "Stopped",
  [S.failed]: "Calibration did not finish",
});

/** Plain sentence per end reason; the routine's own detail is shown beneath it. */
export const REASON_TEXT = Object.freeze({
  owner_stopped: "Stopped from the dashboard before it finished.",
  start_refused_moving: "Not started: the van has to be stopped when you start.",
  not_supported: "Not started: the radar link is not available on this engine run.",
  session_rejected: "Not started: the radar refused the diagnostic session.",
  start_rejected: "Not started: the radar refused to start the routine.",
  no_response: "Ended: the radar stopped answering.",
  routine_reset: "Ended: the radar dropped the routine before it finished.",
  routine_ended: "Ended: the radar closed the routine below 100%.",
  stalled: "Ended: progress did not advance for 10 minutes. Try a steadier, straighter stretch.",
  time_limit: "Ended at the one-hour limit.",
  helper_ended: "Ended: the engine stopped, or telemetry restarted, before it finished.",
  not_delivered: "Not started: the request did not reach the telemetry helper.",
});

const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** `12 min 5 s`, `45 s`, or `` without a duration. */
export function durationText(seconds) {
  const s = num(seconds);
  if (s === null || s < 0) return "";
  const whole = Math.floor(s);
  const minutes = Math.floor(whole / 60);
  return minutes > 0 ? minutes + " min " + (whole % 60) + " s" : whole + " s";
}

export const isActive = (run) => Boolean(run && ACTIVE.has(run.state));

export function initialState() {
  return { payload: null, error: null, notice: null, noticePolls: 0, busy: null, confirmed: false, loaded: false };
}

/**
 * Why Start is not offered yet, in the order the owner can fix them (empty when it is).
 * @param {object} s controller state
 * @param {{speedMph: number|null}} live readings from the dashboard's own stream
 */
export function startBlockers(s, live) {
  const payload = obj(s.payload);
  const helper = payload ? obj(payload.helper) : null;
  if (!helper) return [TEXT.checking];
  const blockers = [];
  if (!helper.running) blockers.push(TEXT.needEngine);
  else if (!helper.ready) blockers.push(TEXT.needRadar);
  const limit = num(obj(payload.limits) ? payload.limits.stationary_max_mph : null);
  const speed = num(live ? live.speedMph : null);
  if (helper.running) {
    if (speed === null) blockers.push(TEXT.needSpeed);
    else if (limit !== null && speed > limit) blockers.push(TEXT.needStop);
  }
  if (!s.confirmed) blockers.push(TEXT.needConfirm);
  return blockers;
}

/** Everything the dialog renders, from the controller state and the live speed. */
export function viewModel(s, live) {
  const payload = obj(s.payload);
  const disabled = Boolean(payload && isOff(payload));
  const run = payload ? obj(payload.run) : null;
  const active = isActive(run);
  const blockers = active || disabled ? [] : startBlockers(s, live);
  const progress = run ? num(run.progress) : null;
  const reasonText = run && run.reason ? REASON_TEXT[run.reason] || "" : "";
  const speed = num(live ? live.speedMph : null);
  let hint = "";
  if (disabled) hint = TEXT.disabled;
  else if (!s.loaded) hint = TEXT.checking;
  else if (active) hint = run.state === S.running ? TEXT.drive : "";
  else hint = blockers.length ? blockers[0] : TEXT.ready;
  return {
    disabled,
    active,
    showRun: Boolean(run),
    headline: run ? HEADLINE[run.state] || "" : "",
    tone: !run ? "" : run.state === S.completed ? "ok" : run.state === S.failed ? "red" : "",
    progress,
    progressText: progress === null ? "—" : progress + "%",
    elapsedText: run ? durationText(run.elapsed_seconds) : "",
    speedText: speed === null ? "" : Math.round(speed) + " mph",
    reasonText,
    detail: run && typeof run.detail === "string" ? run.detail : "",
    finishedAt: run && typeof run.finished_at === "string" ? run.finished_at : null,
    hint,
    canConfirm: !active && !disabled,
    confirmed: s.confirmed,
    canStart: s.loaded && !disabled && !active && blockers.length === 0 && s.busy === null,
    canStop: active && s.busy === null && !(run && run.stop_requested === true),
    startLabel: s.busy === "start" ? TEXT.starting : TEXT.start,
    stopLabel: s.busy === "stop" || (run && run.stop_requested === true) ? TEXT.stopping : TEXT.stop,
    statusText: s.error || s.notice || "",
    statusIsError: Boolean(s.error),
  };
}

const message = (error) => String((error && error.message) || error);

/**
 * Controller for one open dialog.
 * @param {{fetch: Function, isVisible?: () => boolean, setTimeout?: Function, clearTimeout?: Function,
 *   onChange?: (state: object) => void}} deps
 */
export function createRadarCalibrationController(deps) {
  const d = {
    isVisible: () => true,
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    onChange: () => {},
    ...deps,
  };
  let s = initialState();
  let timer = null;
  let disposed = false;
  let issued = 0; // request sequence: an older status reply never overwrites a newer one
  let applied = 0;

  function set(next) {
    s = next;
    if (!disposed) d.onChange(s);
  }

  function schedule(ms) {
    if (timer !== null) d.clearTimeout(timer);
    timer = null;
    if (disposed) return;
    timer = d.setTimeout(() => {
      timer = null;
      return refresh();
    }, ms);
  }

  /** A refusal's explanation shown for the next few polls, or cleared. */
  const noticeFor = (text) => ({ notice: text, noticePolls: text ? NOTICE_POLLS : 0 });

  /**
   * Take a reply's status fields and apply `changes`, unless a newer reply was already applied.
   * The broker's refusals carry the status fields too; the listener's own (origin, body) do not.
   */
  function adopt(seq, reply, changes) {
    if (disposed || seq < applied) return false;
    applied = seq;
    const off = isOff(reply.payload);
    const payload = off || obj(reply.payload.helper) ? reply.payload : s.payload;
    set({ ...s, payload, loaded: true, error: null, ...changes });
    if (!off) schedule(POLL_MS);
    return true;
  }

  async function refresh() {
    if (disposed) return false;
    if (!d.isVisible()) {
      schedule(POLL_MS);
      return false;
    }
    const seq = ++issued;
    try {
      const reply = await readReply(await d.fetch(RADAR_SERVICE.path, { cache: "no-store" }));
      const polls = Math.max(0, s.noticePolls - 1);
      return adopt(seq, reply, { noticePolls: polls, notice: polls > 0 ? s.notice : null });
    } catch (error) {
      if (disposed || seq < applied) return false;
      set({ ...s, loaded: true, error: TEXT.statusError + message(error) });
      schedule(POLL_ERROR_MS);
      return false;
    }
  }

  async function post(kind, body) {
    if (disposed || s.busy !== null) return false;
    const seq = ++issued;
    set({ ...s, busy: kind, error: null, ...noticeFor(null) });
    try {
      const reply = await readReply(
        await d.fetch(RADAR_SERVICE.path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          cache: "no-store",
        }),
      );
      if (disposed) return false;
      const refused = reply.ok ? null : typeof reply.payload.detail === "string" ? reply.payload.detail : "refused";
      // A started run needs a fresh tick before the next one.
      const outcome = { busy: null, ...noticeFor(refused), ...(reply.ok && kind === "start" ? { confirmed: false } : {}) };
      if (!adopt(seq, reply, outcome)) set({ ...s, ...outcome });
      return reply.ok;
    } catch (error) {
      if (disposed) return false;
      set({ ...s, busy: null, error: TEXT.requestError + message(error) });
      schedule(POLL_ERROR_MS);
      return false;
    }
  }

  return {
    refresh,
    start: () => post("start", RADAR_SERVICE.start_request),
    stop: () => post("stop", RADAR_SERVICE.stop_request),
    setConfirmed(value) {
      if ((value === true) !== s.confirmed) set({ ...s, confirmed: value === true });
    },
    dispose() {
      disposed = true;
      if (timer !== null) d.clearTimeout(timer);
      timer = null;
    },
    state: () => s,
    hasTimer: () => timer !== null,
  };
}
