/**
 * The browser side of the assistant access code, shared by the warning chat and the code-research
 * dialog: per-origin storage of the access code and the random chat-client id, the two request
 * headers every /v1/assistant route needs, the wording of the pairing form, and the pure half of
 * pairing by PIN (assistant_pairing.json is the contract; assistantPairing.js makes the requests).
 * Pure: no DOM, no store; the storage backend is injectable.
 */

import pairing from "../../assistant_pairing.json" with { type: "json" };

export const PAIRING = Object.freeze(pairing);
const PIN_RE = new RegExp(`^[0-9]{${PAIRING.pinDigits}}$`);
/** Whole minutes, rounded up, at least 1: "try again in N min". */
export const minutesLeft = (seconds) => Math.max(1, Math.ceil(Number(seconds) / 60) || 1);
const minutes = minutesLeft;

export const STORAGE_PREFIX = "van-warning-chat.";
export const ACCESS_CODE_RE = /^[A-Za-z0-9_-]{32,128}$/;
export const CLIENT_ID_RE = /^[a-f0-9]{64}$/;

export const ACCESS_TEXT = Object.freeze({
  pairingLabel: "Assistant access code",
  pairingHintBefore: "On the Pi, read ",
  pairingHintPath: "/var/lib/van-telemetry-advisor/access-code",
  pairingHintAfter: " and paste the code here. This is not an OpenAI API key.",
  pairingPrompt: "Connect this browser once using the assistant access code from the Pi",
  incompleteCode: "Enter the complete assistant access code",
  pinLabel: "Pairing PIN",
  pinHint: "On a browser that is already connected, open Pair another device and type the PIN it shows.",
  pinButton: "Pair",
  pinWorking: "Pairing…",
  pinIncomplete: `Enter the ${PAIRING.pinDigits}-digit PIN`,
  pinRejected: "That PIN was not accepted. Make a new one on a connected browser.",
  pinLocked: (n) => `Too many wrong PINs recently. Try again in ${n} min, or paste the access code.`,
  pinUnavailable: "The Pi's assistant did not answer. Try again, or paste the access code.",
  pasteHeading: "Or paste the access code",
  anotherTitle: "Pair another device",
  anotherHint: `Type this on the new device within ${minutes(PAIRING.pinLifetimeSeconds)} minutes. It works once.`,
  anotherNew: "New PIN",
  anotherCreating: "Making a PIN…",
  anotherFailed: "Could not make a PIN. Try again.",
  anotherPaired: "Device paired",
  anotherExpired: "This PIN expired. Make a new one if you still need it.",
  anotherEnded: "This PIN is no longer valid. Make a new one if you still need it.",
  anotherCancelled: "This PIN was cancelled.",
  anotherReplaced: "Replaced by a newer PIN",
  anotherWrongTries: "A wrong PIN was tried; if that wasn't you, consider rotating the access code.",
  anotherLocked: (n) => `Pairing is paused after wrong PINs. Try again in ${n} min.`,
});

/** Per-origin localStorage wrapper; every access is guarded (private mode, denied, missing). */
export function createStorage(backend) {
  const store = () => (backend === undefined ? globalThis.localStorage : backend);
  return {
    get(key) {
      try {
        const value = store()?.getItem(STORAGE_PREFIX + key);
        return value === undefined ? null : value;
      } catch (_error) {
        return null;
      }
    },
    set(key, value) {
      try { store()?.setItem(STORAGE_PREFIX + key, value); } catch (_error) { /* ignore */ }
    },
    remove(key) {
      try { store()?.removeItem(STORAGE_PREFIX + key); } catch (_error) { /* ignore */ }
    },
  };
}

/** Lower-case hex string of `bytes` random bytes. */
export function randomId(bytes, cryptoLike = globalThis.crypto) {
  const values = new Uint8Array(bytes);
  if (cryptoLike && typeof cryptoLike.getRandomValues === "function") cryptoLike.getRandomValues(values);
  else for (let index = 0; index < bytes; index += 1) values[index] = Math.floor(Math.random() * 256);
  return Array.from(values, (value) => value.toString(16).padStart(2, "0")).join("");
}

/** The browser's random chat-client id (64 hex), created once and kept per origin. */
export function ensureClientId(storage, random = randomId) {
  let client = storage.get("client");
  if (!CLIENT_ID_RE.test(client || "")) {
    client = random(32);
    storage.set("client", client);
  }
  return client;
}

export const isValidAccessCode = (code) => ACCESS_CODE_RE.test(String(code || ""));

export function requestHeaders(key, client) {
  return {
    "Content-Type": "application/json",
    "X-Van-Assistant-Key": key || "",
    "X-Van-Chat-Client": client || "",
  };
}

export function buildRequest(method, path, payload, identity) {
  const init = {method, cache: "no-store", headers: requestHeaders(identity?.key, identity?.client)};
  if (payload !== undefined && payload !== null) init.body = JSON.stringify(payload);
  return {url: path, init};
}

/** The stored access code and this browser's client id, for requests made outside a dialog controller. */
export function storedIdentity(storage) {
  return {key: storage.get("access") || "", client: ensureClientId(storage)};
}

/* ---------- pairing by PIN (pure) ---------- */

/** What the user typed, without spaces (the shown PIN is grouped "042 917"). */
export const normalizePin = (raw) => String(raw ?? "").replace(/\s+/g, "");
export const isValidPin = (pin) => PIN_RE.test(String(pin ?? ""));

/** "042917" → "042 917". */
export const pinDisplay = (pin) => String(pin || "").replace(/^(\d{3})(\d{3})$/, "$1 $2");

/** Remaining milliseconds as "m:ss", rounded up so a fresh PIN shows its full lifetime. */
export function countdownText(ms) {
  const total = Math.max(0, Math.ceil(Number(ms) / 1000) || 0);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** The redeem request: no assistant headers, and the PIN only in the body (never a URL). */
export function redeemRequest(pin) {
  const headers = {"Content-Type": "application/json"};
  return {url: PAIRING.routes.redeem, init: {method: "POST", cache: "no-store", headers, body: JSON.stringify({pin})}};
}

/** `{code}` for a usable access code, else `{message}`; status 0 is a network failure. */
export function redeemOutcome(status, body) {
  if (status === 200) {
    return body && isValidAccessCode(body.access_code) ? {code: body.access_code} : {message: ACCESS_TEXT.pinUnavailable};
  }
  if (status === 403) return {message: ACCESS_TEXT.pinRejected};
  if (status === 429) return {message: ACCESS_TEXT.pinLocked(minutes(body && body.retry_after_s))};
  if (status === 400) return {message: ACCESS_TEXT.pinIncomplete};
  return {message: ACCESS_TEXT.pinUnavailable};
}

/** What the "Pair another device" panel shows for a status reply about its own PIN `pinId`. */
export function pairingStatusView(status, pinId) {
  const s = PAIRING.states;
  const ended = {done: true, text: ACCESS_TEXT.anotherEnded, error: false};
  if (!status || typeof status !== "object") return {done: false, text: "", error: false};
  const views = {
    [s.active]: {done: false, text: "", error: false},
    [s.redeemed]: {done: true, text: ACCESS_TEXT.anotherPaired, error: false},
    [s.expired]: {done: true, text: ACCESS_TEXT.anotherExpired, error: false},
    [s.wrongTries]: {done: true, text: ACCESS_TEXT.anotherWrongTries, error: true},
    [s.cancelled]: {done: true, text: ACCESS_TEXT.anotherCancelled, error: false},
  };
  // This PIN's own ending wins over a lock that came later.
  const own = status.pin_id != null && status.pin_id === pinId;
  if (own && status.state !== s.active && views[status.state]) return views[status.state];
  if (status.locked_for_s > 0) return {done: true, text: ACCESS_TEXT.anotherLocked(minutes(status.locked_for_s)), error: true};
  if (status.pin_id == null) return ended;
  if (!own) return {done: true, text: ACCESS_TEXT.anotherReplaced, error: false};
  return views[status.state] || ended;
}
