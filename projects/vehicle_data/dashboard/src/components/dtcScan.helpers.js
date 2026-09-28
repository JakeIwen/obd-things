/**
 * Guarded parked DTC scan (dashboard v2, design 3.3 / critique A11): the pure state machine and a
 * small controller with injected fetch, timers, visibility and confirm, so every gate is
 * node-testable. Ported from the former static app (static/app.js dtcJobIsActive,
 * updateDtcJobButtons, renderDtcJob, fetchDtcJobStatus, configureDtcJobs and the
 * #dtc-scan-controls handlers):
 *
 * - Scan needs the listener's `dtc_jobs_enabled`, the parked-confirmation checkbox, no active job,
 *   no `restoration_failed` lockout, no request in flight, a confirm() dialog, and (only when the
 *   listener still sets `dtc_jobs_require_local_one_use_arm`) a one-use arm token.
 * - POST /v1/diagnostics/dtc-jobs with exactly {confirm_parked, confirm_park_gear,
 *   confirm_ignition_on_engine_off} (all true), plus `token` only when the listener requires it.
 * - Cancel: POST /v1/diagnostics/dtc-jobs/current/cancel with exactly {action: "cancel"} while the
 *   job is queued, starting, created or running and no cancel is pending.
 * - Poll GET /v1/diagnostics/dtc-jobs/current every 2 s while a job is active and the page is
 *   visible, 5 s after an error; a hidden page stops polling until `resume()`.
 * - When a job this page watched while active reaches a final state, `onFinished` runs (the view
 *   passes runtime.manualRefresh so the saved code list is refetched).
 *
 * No DOM access and no module state: one controller per mounted scan panel.
 */

import { fmtTime, parseDate } from "../format.js";

export const JOBS_URL = "/v1/diagnostics/dtc-jobs";
export const CURRENT_URL = "/v1/diagnostics/dtc-jobs/current";
export const CANCEL_URL = "/v1/diagnostics/dtc-jobs/current/cancel";

/** Poll interval while a job is active, and after a failed status read. */
export const POLL_ACTIVE_MS = 2000;
export const POLL_ERROR_MS = 5000;

/** Job states in which a scan is queued or running (legacy dtcJobIsActive). */
export const ACTIVE_STATES = new Set(["queued", "starting", "created", "running"]);
/** Terminal job states (lib/dtc_batch.FINAL_JOB_STATES). */
export const FINAL_STATES = new Set(["completed", "cancelled", "failed", "restoration_failed"]);

/** Length bounds of a legacy one-use arm token (lib/dtc_web.ArmTokenStore.consume). */
export const TOKEN_MIN = 32;
export const TOKEN_MAX = 128;

/** On-screen copy; the component only renders these. */
export const TEXT = Object.freeze({
  summary: "Parked code scan",
  intro:
    "Reads the saved codes from 15 modules with one fixed, read-only request each. It never clears a code, " +
    "and the PCM is not included. Before every request the scanner re-checks ignition, engine speed and vehicle speed.",
  confirmLabel: "I confirm the van is stationary, selector in Park, ignition ON, and engine OFF.",
  tokenLabel: "One-use arm token",
  tokenHint: "This listener still asks for a local token. Create one on the Pi with python3 tools/dtc_web_arm.py.",
  start: "Scan modules",
  starting: "Queuing…",
  cancel: "Cancel scan",
  cancelling: "Cancelling…",
  // Kept word for word from the former static app: this is the safety prompt the owner knows.
  confirmPrompt: "Start the fixed read-only DTC batch now? Confirm Park, ignition ON, engine OFF, and stationary.",
  checking: "Checking for a running scan…",
  idle: "No scan is running.",
  lockout: "Restoration unverified — inspect before retry. The last scan could not confirm the CAN adapters were put back; check them before scanning again.",
  hintConfirm: "Tick the parked confirmation to enable Scan.",
  hintActive: "A scan is already queued or running.",
  hintToken: "Enter the one-use arm token to enable Scan.",
  statusError: "Scan status unavailable: ",
  startError: "The scan was not queued: ",
  cancelError: "Cancel was not accepted: ",
});

/** Plain words for job states; unknown states are humanised. */
export const STATE_WORDS = Object.freeze({
  idle: "No scan running",
  queued: "Queued",
  starting: "Starting",
  created: "Starting",
  running: "Running",
  completed: "Finished",
  cancelled: "Cancelled",
  failed: "Failed",
  restoration_failed: "Stopped, restoration unverified",
});

const BUS_NAMES = Object.freeze({ "c-can": "C-CAN", "b-can": "B-CAN", "can-ch": "CAN-CH" });

function obj(v) {
  return v && typeof v === "object" && !Array.isArray(v) ? v : null;
}

function num(v) {
  return typeof v === "number" && isFinite(v) ? v : null;
}

function str(v) {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function humanize(value) {
  const s = String(value || "").replace(/_/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "Unknown";
}

/** True while a job is queued, starting, created or running. */
export function isActive(state) {
  return ACTIVE_STATES.has(state);
}

/** Plain word for a job state. */
export function stateWord(state) {
  return STATE_WORDS[state] || humanize(state);
}

/** A legacy arm token of plausible length (whitespace around it is ignored). */
export function tokenValid(token) {
  if (typeof token !== "string") return false;
  const t = token.trim();
  return t.length >= TOKEN_MIN && t.length <= TOKEN_MAX;
}

/** Job state from a status/start/cancel reply (`state`, else `job.state`, else idle). */
export function jobState(payload) {
  const p = obj(payload) || {};
  const job = obj(p.job) || {};
  return str(p.state) || str(job.state) || "idle";
}

/** Fresh state of a scan panel. */
export function initialState() {
  return {
    enabled: false,
    tokenRequired: false,
    confirmed: false,
    token: "",
    state: null,
    job: null,
    cancelRequested: false,
    statusText: TEXT.idle,
    error: null,
    busy: null,
  };
}

/**
 * Why Scan is disabled, or null when it may be pressed. Order matters: the first gate that fails
 * decides the hint shown under the buttons.
 * @returns {null|'disabled'|'busy'|'active'|'lockout'|'confirm'|'token'}
 */
export function startBlock(s) {
  if (!s.enabled) return "disabled";
  if (s.busy) return "busy";
  if (isActive(s.state)) return "active";
  if (s.state === "restoration_failed") return "lockout";
  if (!s.confirmed) return "confirm";
  if (s.tokenRequired && !tokenValid(s.token)) return "token";
  return null;
}

export function canStart(s) {
  return startBlock(s) === null;
}

/** Cancel is offered while a job is active, no cancel is pending and nothing is in flight. */
export function canCancel(s) {
  return s.enabled && isActive(s.state) && !s.cancelRequested && !s.busy;
}

/** The exact start body the listener accepts (token only when the listener requires one). */
export function startBody(s) {
  const body = { confirm_parked: true, confirm_park_gear: true, confirm_ignition_on_engine_off: true };
  if (s && s.tokenRequired && tokenValid(s.token)) body.token = s.token.trim();
  return body;
}

/** The exact cancel body. */
export function cancelBody() {
  return { action: "cancel" };
}

/**
 * One status line for a job reply, the legacy renderDtcJob parts in plain words:
 * `Running · 4 of 15 modules read · 3 saved · C-CAN / tcm · cancel requested`.
 * @param {object} payload reply of the status, start or cancel request
 * @param {number} [nowMs] reference for the finish time label
 */
export function jobStatusText(payload, nowMs) {
  const p = obj(payload) || {};
  const job = obj(p.job);
  const state = jobState(p);
  if (!job && state === "idle") return TEXT.idle;
  const j = job || {};
  let head = stateWord(state);
  if (FINAL_STATES.has(state)) {
    const at = str(j.completed_at) || str(j.updated_at);
    if (at && parseDate(at)) head += " " + fmtTime(at, new Date(typeof nowMs === "number" ? nowMs : Date.now()));
  }
  const parts = [head];
  const progress = obj(j.progress) || {};
  const requestable = num(progress.requestable);
  if (requestable !== null) {
    parts.push((num(progress.queried) || 0) + " of " + requestable + " modules read");
    parts.push((num(progress.imported) || 0) + " saved");
    const unavailable = num(progress.unavailable) || 0;
    if (unavailable > 0) parts.push(unavailable + " no answer");
  }
  const bus = str(j.current_bus);
  const where = [bus ? BUS_NAMES[bus] || bus : null, str(j.current_module)].filter(Boolean).join(" / ");
  if (where) parts.push(where);
  if (j.cancel_requested === true) parts.push("cancel requested");
  if (j.restoration_failure) parts.push("restoration unverified — inspect before retry");
  if (str(j.failure)) parts.push(j.failure);
  return parts.join(" · ");
}

/** State after a job reply. */
export function applyJob(s, payload, nowMs) {
  const p = obj(payload) || {};
  const job = obj(p.job);
  return {
    ...s,
    state: jobState(p),
    job: job,
    cancelRequested: Boolean(job && job.cancel_requested === true),
    statusText: jobStatusText(p, nowMs),
    error: null,
  };
}

/**
 * Whether a reply finishes a job this page saw active: completed always; cancelled, failed or
 * restoration_failed only when some module results were imported.
 */
export function shouldRefreshSummary(prevState, next) {
  if (!isActive(prevState) || !FINAL_STATES.has(next.state)) return false;
  if (next.state === "completed") return true;
  const progress = obj(next.job && next.job.progress) || {};
  return (num(progress.imported) || 0) > 0;
}

/** Delay before the next status poll, or null for none. */
export function nextPollDelay(s, hadError) {
  if (!s.enabled) return null;
  if (hadError) return POLL_ERROR_MS;
  return isActive(s.state) ? POLL_ACTIVE_MS : null;
}

const HINTS = { confirm: TEXT.hintConfirm, active: TEXT.hintActive, token: TEXT.hintToken };

/** Everything the component renders, as plain values. */
export function viewModel(s) {
  const block = startBlock(s);
  return {
    enabled: s.enabled,
    confirmed: s.confirmed,
    token: s.token,
    tokenRequired: s.tokenRequired,
    active: isActive(s.state),
    busy: s.busy,
    canStart: block === null,
    canCancel: canCancel(s),
    hint: HINTS[block] || null,
    lockout: s.state === "restoration_failed",
    statusText: s.statusText,
    error: s.error,
    summaryText: TEXT.summary + " · " + (s.state === null ? "checking" : stateWord(s.state).toLowerCase()),
    startLabel: s.busy === "start" ? TEXT.starting : TEXT.start,
    cancelLabel: s.busy === "cancel" ? TEXT.cancelling : TEXT.cancel,
  };
}

function message(error) {
  if (error && typeof error.message === "string" && error.message) return error.message;
  return String(error);
}

async function readReply(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? str(payload.detail) : null;
    throw new Error(detail || "HTTP " + response.status);
  }
  if (!obj(payload)) throw new Error("unreadable reply");
  return payload;
}

/**
 * Controller for one mounted scan panel.
 * @param {{fetch: Function, isVisible?: () => boolean, setTimeout?: Function, clearTimeout?: Function,
 *   confirm?: (message: string) => boolean, onChange?: (vm: object) => void,
 *   onFinished?: (payload: object) => void, now?: () => number}} deps
 */
export function createDtcScanController(deps) {
  const d = {
    isVisible: () => true,
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    confirm: () => false,
    onChange: () => {},
    onFinished: () => {},
    now: () => Date.now(),
    ...deps,
  };
  let s = initialState();
  let timer = null;
  let disposed = false;
  let generation = 0; // bumped on disable/dispose: replies from before are dropped
  let issued = 0; // request sequence
  let applied = 0; // newest request whose reply was applied
  let statusInFlight = 0;

  function set(next) {
    s = next;
    if (!disposed) d.onChange(viewModel(s));
  }

  function clear() {
    if (timer !== null) {
      d.clearTimeout(timer);
      timer = null;
    }
  }

  function schedule(ms) {
    clear();
    if (ms === null || disposed) return;
    timer = d.setTimeout(() => {
      timer = null;
      refresh();
    }, ms);
  }

  /** A reply is current when nothing newer was applied and the panel was not reset. */
  function current(seq, gen) {
    if (disposed || gen !== generation || seq < applied) return false;
    applied = seq;
    return true;
  }

  function accept(base, payload) {
    const prev = base.state;
    const next = applyJob(base, payload, d.now());
    set(next);
    if (shouldRefreshSummary(prev, next)) d.onFinished(payload);
    if (isActive(next.state)) {
      if (timer === null) schedule(POLL_ACTIVE_MS);
    } else {
      clear();
    }
  }

  async function refresh() {
    clear();
    if (disposed || !s.enabled || !d.isVisible()) return false;
    const seq = ++issued;
    const gen = generation;
    statusInFlight += 1;
    try {
      const response = await d.fetch(CURRENT_URL + "?fresh=" + d.now(), { cache: "no-store" });
      const payload = await readReply(response);
      if (!current(seq, gen)) return false;
      accept(s, payload);
      return true;
    } catch (error) {
      if (!current(seq, gen)) return false;
      set({ ...s, statusText: TEXT.statusError + message(error), error: "status" });
      schedule(nextPollDelay(s, true));
      return false;
    } finally {
      statusInFlight -= 1;
    }
  }

  function configure(web) {
    const w = obj(web) || {};
    const enabled = w.dtc_jobs_enabled === true;
    const tokenRequired = w.dtc_jobs_require_local_one_use_arm === true;
    if (tokenRequired !== s.tokenRequired) set({ ...s, tokenRequired });
    if (enabled === s.enabled) {
      if (enabled && isActive(s.state) && timer === null && statusInFlight === 0) refresh();
      return;
    }
    if (!enabled) {
      generation += 1;
      clear();
      set({ ...initialState(), tokenRequired });
      return;
    }
    set({ ...s, enabled: true, statusText: TEXT.checking });
    refresh();
  }

  async function start() {
    if (!canStart(s)) return false;
    if (!d.confirm(TEXT.confirmPrompt)) return false;
    if (!canStart(s)) return false; // the job or the flags may have changed behind the dialog
    const body = startBody(s);
    const seq = ++issued;
    const gen = generation;
    set({ ...s, busy: "start" });
    try {
      const response = await d.fetch(JOBS_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
      });
      const payload = await readReply(response);
      if (disposed || gen !== generation) return false;
      // A queued scan needs a fresh confirmation (and token) before the next one.
      const base = { ...s, busy: null, confirmed: false, token: "" };
      if (current(seq, gen)) accept(base, payload);
      else set(base);
      return true;
    } catch (error) {
      if (disposed || gen !== generation) return false;
      set({ ...s, busy: null, statusText: TEXT.startError + message(error), error: "start" });
      return false;
    }
  }

  async function cancel() {
    if (!canCancel(s)) return false;
    const seq = ++issued;
    const gen = generation;
    set({ ...s, busy: "cancel" });
    try {
      const response = await d.fetch(CANCEL_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cancelBody()),
        cache: "no-store",
      });
      const payload = await readReply(response);
      if (disposed || gen !== generation) return false;
      const base = { ...s, busy: null };
      if (current(seq, gen)) accept(base, payload);
      else set(base);
      return true;
    } catch (error) {
      if (disposed || gen !== generation) return false;
      set({ ...s, busy: null, statusText: TEXT.cancelError + message(error), error: "cancel" });
      return false;
    }
  }

  /** Page became visible again: pick polling back up when a job is active or status is unknown. */
  function resume() {
    if (disposed || !s.enabled || timer !== null || statusInFlight > 0) return;
    if (s.state === null || isActive(s.state) || s.error === "status") refresh();
  }

  function setConfirmed(value) {
    const confirmed = value === true;
    if (confirmed !== s.confirmed) set({ ...s, confirmed });
  }

  function setToken(value) {
    const token = typeof value === "string" ? value : "";
    if (token !== s.token) set({ ...s, token });
  }

  function dispose() {
    disposed = true;
    generation += 1;
    clear();
  }

  return {
    configure,
    refresh,
    start,
    cancel,
    resume,
    setConfirmed,
    setToken,
    dispose,
    snapshot: () => viewModel(s),
    state: () => s,
    hasTimer: () => timer !== null,
  };
}
