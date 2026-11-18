/**
 * Pairing by PIN, the effectful half (the pure half and the wording are in assistantAccess.js;
 * assistant_pairing.json is the contract shared with the Pi):
 *  - the four requests: createPin, cancelPin, pinStatus (owner route, with the access code) and
 *    redeemPin (no assistant headers);
 *  - createPinEntry: the PIN field of the unpaired form (AssistantPairing.jsx) redeems a PIN and
 *    hands the received code to the dialog's own `pair(code)`, so storing it and reconnecting stay
 *    in the dialog controllers;
 *  - createPairingPanel: "Pair another device" on a connected browser (PairAnotherDevice.jsx) makes
 *    a PIN, counts down from the reply's receipt, polls the PIN's status every statusPollSeconds
 *    and cancels it when closed.
 * fetch, the clock, timers and storage are injected so node can drive them. The PIN is held only
 * in memory and never put in a URL or in storage.
 */

import {
  ACCESS_TEXT, PAIRING, buildRequest, countdownText, isValidPin, minutesLeft, normalizePin, pairingStatusView,
  pinDisplay, redeemOutcome, redeemRequest, storedIdentity,
} from "./assistantAccess.js";
import { REQUEST_TIMEOUT_MS } from "./dialogs/WarningChat.helpers.js";

const PIN_ID_RE = new RegExp(PAIRING.pinIdPattern);
const TICK_MS = 1000;

/**
 * `{status, body}`; status 0 when the request failed or passed its deadline (the dialogs' 20 s
 * REQUEST_TIMEOUT_MS), body null unless it is one JSON object. The deadline is raced, so a fetch
 * that ignores the abort signal still ends the wait.
 */
async function exchange(doFetch, {url, init}, timing = {}) {
  const {
    setTimeout: setT = globalThis.setTimeout,
    clearTimeout: clearT = globalThis.clearTimeout,
    timeoutMs = REQUEST_TIMEOUT_MS,
  } = timing;
  const abort = typeof AbortController === "function" ? new AbortController() : null;
  const request = abort ? {...init, signal: abort.signal} : init;
  let timer = null;
  const deadline = new Promise((resolve) => {
    timer = setT(() => { if (abort) abort.abort(); resolve({status: 0, body: null}); }, timeoutMs);
  });
  const attempt = (async () => {
    try {
      const response = await doFetch(url, request);
      let body = null;
      try { body = await response.json(); } catch { body = null; }
      return {status: response.status, body: body && typeof body === "object" && !Array.isArray(body) ? body : null};
    } catch {
      return {status: 0, body: null};
    }
  })();
  try {
    return await Promise.race([attempt, deadline]);
  } finally {
    clearT(timer);
  }
}

export const createPin = (doFetch, identity, timing) =>
  exchange(doFetch, buildRequest("POST", PAIRING.routes.owner, {action: PAIRING.actions.create}, identity), timing);
export const cancelPin = (doFetch, identity, pinId, timing) =>
  exchange(doFetch, buildRequest("POST", PAIRING.routes.owner, {action: PAIRING.actions.cancel, pin_id: pinId}, identity), timing);
export const pinStatus = (doFetch, identity, timing) =>
  exchange(doFetch, buildRequest("GET", PAIRING.routes.owner, undefined, identity), timing);

/** `{code}` or `{message}` (see redeemOutcome). */
export async function redeemPin(doFetch, pin, timing) {
  const {status, body} = await exchange(doFetch, redeemRequest(pin), timing);
  return redeemOutcome(status, body);
}

/** The PIN field: `submit(raw)` redeems and calls `onCode(code)` on success. */
export function createPinEntry({fetch: doFetch, onCode, onChange = () => {}, ...timing}) {
  const s = {busy: false, message: "", error: false};
  const snapshot = () => ({busy: s.busy, message: s.message, error: s.error});
  const emit = () => onChange(snapshot());
  async function submit(raw) {
    if (s.busy) return false;
    const pin = normalizePin(raw);
    if (!isValidPin(pin)) {
      Object.assign(s, {message: ACCESS_TEXT.pinIncomplete, error: true});
      emit();
      return false;
    }
    Object.assign(s, {busy: true, message: ACCESS_TEXT.pinWorking, error: false});
    emit();
    const outcome = await redeemPin(doFetch, pin, timing);
    Object.assign(s, {busy: false, message: outcome.code ? "" : outcome.message, error: !outcome.code});
    emit();
    if (outcome.code) onCode(outcome.code);
    return Boolean(outcome.code);
  }
  return {snapshot, submit};
}

/** "Pair another device": `open()` makes a PIN, `newPin()` replaces it, `close()` cancels it. */
export function createPairingPanel(deps) {
  const {
    fetch: doFetch,
    storage,
    now = () => globalThis.performance.now(),
    setTimeout: setT = globalThis.setTimeout,
    clearTimeout: clearT = globalThis.clearTimeout,
    onChange = () => {},
    timeoutMs,
  } = deps;
  const timing = {setTimeout: setT, clearTimeout: clearT, timeoutMs};
  const pollMs = PAIRING.statusPollSeconds * 1000;
  const s = {open: false, phase: "idle", pin: "", pinId: null, expiresAt: 0, message: "", error: false,
    tick: null, poll: null, generation: 0};
  const identity = () => storedIdentity(storage);

  function snapshot() {
    const active = s.phase === "active";
    return {
      open: s.open,
      phase: s.phase,
      showPin: active,
      pin: active ? pinDisplay(s.pin) : "",
      countdown: active ? countdownText(s.expiresAt - now()) : "",
      message: s.message,
      error: s.error,
      canNew: s.open && s.phase !== "creating",
    };
  }
  const emit = () => onChange(snapshot());

  function stopTimers() {
    if (s.tick !== null) clearT(s.tick);
    if (s.poll !== null) clearT(s.poll);
    s.tick = null;
    s.poll = null;
  }

  function end(text, error = false) {
    stopTimers();
    Object.assign(s, {phase: "ended", pin: "", message: text, error});
    emit();
  }

  function scheduleTick(generation) {
    s.tick = setT(() => {
      s.tick = null;
      if (generation !== s.generation) return;
      if (s.expiresAt - now() <= 0) {
        void finishAtExpiry(generation);
        return;
      }
      emit();
      scheduleTick(generation);
    }, TICK_MS);
  }

  /**
   * The countdown ran out: read the PIN's status once more before saying it expired, so a
   * redemption in the last second shows as paired. A status reply already in flight may also land
   * meanwhile; the phase stays active until one of them ends it.
   */
  async function finishAtExpiry(generation) {
    if (s.poll !== null) clearT(s.poll);
    s.poll = null;
    const {status, body} = await pinStatus(doFetch, identity(), timing);
    if (generation !== s.generation || s.phase !== "active") return;
    const view = status === 200 ? pairingStatusView(body, s.pinId) : null;
    if (view && view.done) end(view.text, view.error);
    else end(ACCESS_TEXT.anotherExpired);
  }

  function schedulePoll(generation) {
    s.poll = setT(async () => {
      s.poll = null;
      if (generation !== s.generation) return;
      const {status, body} = await pinStatus(doFetch, identity(), timing);
      if (generation !== s.generation || s.phase !== "active") return;
      if (status === 401) {
        end(ACCESS_TEXT.pairingPrompt, true);
        return;
      }
      const view = status === 200 ? pairingStatusView(body, s.pinId) : null;
      if (view && view.done) {
        end(view.text, view.error);
        return;
      }
      schedulePoll(generation); // a failed poll is retried until the countdown ends
    }, pollMs);
  }

  async function create() {
    stopTimers();
    s.generation += 1;
    const generation = s.generation;
    Object.assign(s, {phase: "creating", pin: "", pinId: null, message: ACCESS_TEXT.anotherCreating, error: false});
    emit();
    const {status, body} = await createPin(doFetch, identity(), timing);
    const made = status === 200 && body && isValidPin(body.pin) && PIN_ID_RE.test(String(body.pin_id));
    if (generation !== s.generation) {
      if (made) void cancelPin(doFetch, identity(), body.pin_id, timing); // closed while it was being made
      return;
    }
    if (made) {
      Object.assign(s, {phase: "active", pin: body.pin, pinId: body.pin_id, message: "", error: false,
        expiresAt: now() + Number(body.expires_in_s) * 1000});
      emit();
      scheduleTick(generation);
      schedulePoll(generation);
    } else if (status === 429) {
      end(ACCESS_TEXT.anotherLocked(minutesLeft(body && body.retry_after_s)), true);
    } else if (status === 401) {
      end(ACCESS_TEXT.pairingPrompt, true);
    } else {
      end(ACCESS_TEXT.anotherFailed, true);
    }
  }

  function open() {
    if (s.open) return;
    s.open = true;
    void create();
  }

  function newPin() {
    if (s.open && s.phase !== "creating") void create();
  }

  function close() {
    if (!s.open) return;
    s.open = false;
    s.generation += 1;
    stopTimers();
    if (s.phase === "active" && s.pinId) void cancelPin(doFetch, identity(), s.pinId, timing); // best effort
    Object.assign(s, {phase: "idle", pin: "", pinId: null, message: "", error: false});
    emit();
  }

  return {snapshot, open, newPin, close};
}
