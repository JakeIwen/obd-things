/**
 * Fetches the credits view for the System card: one request with the assistant access code, then
 * every `card.pollMs` while the card is mounted. An optional card never reports an error: any
 * refusal (401/403/404/503) or network failure just hides it until a later poll succeeds.
 */

import { CREDITS } from "./apiCredits.helpers.js";
import { buildRequest, createStorage, ensureClientId } from "./assistantAccess.js";
import { readReply } from "./jsonReply.js";

/**
 * @param {{fetch?: Function, storage?: object, setTimeout?: Function, clearTimeout?: Function,
 *   onChange: (state: {view: object|null, hasAccessCode: boolean}) => void}} deps
 */
export function createCreditsPoller({
  fetch = (url, init) => globalThis.fetch(url, init),
  storage = createStorage(),
  setTimeout: later = (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: cancel = (id) => globalThis.clearTimeout(id),
  onChange,
}) {
  let timer = null;
  let running = false;
  let generation = 0;
  const state = { view: null, hasAccessCode: false };
  const report = () => onChange({ ...state });

  async function load() {
    const key = storage.get("access") || "";
    state.hasAccessCode = key !== "";
    if (!key) { state.view = null; return; }
    const { url, init } = buildRequest("GET", CREDITS.route, undefined, { key, client: ensureClientId(storage) });
    try {
      const { ok, payload } = await readReply(await fetch(url, init));
      state.view = ok ? payload : null;
    } catch {
      state.view = null;
    }
  }

  async function tick() {
    const mine = generation;
    await load();
    if (!running || mine !== generation) return;
    report();
    timer = later(tick, CREDITS.pollMs);
  }

  return {
    start() {
      if (running) return;
      running = true;
      generation += 1;
      tick();
    },
    stop() {
      running = false;
      generation += 1;
      if (timer !== null) cancel(timer);
      timer = null;
    },
    snapshot: () => ({ ...state }),
  };
}
