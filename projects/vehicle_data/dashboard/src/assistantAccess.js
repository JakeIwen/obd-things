/**
 * The browser side of the assistant access code, shared by the warning chat and the code-research
 * dialog: per-origin storage of the access code and the random chat-client id, the two request
 * headers every /v1/assistant route needs, and the wording of the pairing form. Pure: no DOM, no
 * store; the storage backend is injectable.
 */

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
