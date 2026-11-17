/**
 * Research summaries for the Codes card: the newest run per code from GET
 * /v1/assistant/dtc-research, held in signals so rows can show `AI: <summary>` and the panel
 * button can read `Open research`. Fetched when the card mounts and after a research dialog
 * closes or a run finishes. Without an access code in this browser nothing is fetched and rows
 * show no AI line; a 404 means this Pi has no research, and the research controls are hidden.
 */

import { signal } from "@preact/signals";
import { RESEARCH, researchIndex } from "./dtcResearch.helpers.js";
import { buildRequest, createStorage, ensureClientId } from "./assistantAccess.js";

/** Items of the last list reply by code (see `researchIndex`). */
export const researchItems = signal(new Map());
/** `unknown` until the first answer, `ready`, or `missing` (the Pi has no research routes). */
export const researchStatus = signal("unknown");

let inFlight = null;

/** Fetch the list once at a time; failures leave the last list as it was. */
export function refreshResearchList(fetchFn = (url, init) => globalThis.fetch(url, init), storage = createStorage()) {
  const key = storage.get("access") || "";
  if (!key) return Promise.resolve(false);
  if (inFlight) return inFlight;
  const { url, init } = buildRequest("GET", RESEARCH.routes.list, undefined, { key, client: ensureClientId(storage) });
  inFlight = (async () => {
    try {
      const response = await fetchFn(url, init);
      let body = null;
      try { body = await response.json(); } catch { body = null; }
      if (response.status === 404 && !(body && body.reason)) {
        researchStatus.value = "missing";
        researchItems.value = new Map();
        return false;
      }
      if (!response.ok || !body || body.available !== true) return false;
      researchItems.value = researchIndex(body);
      researchStatus.value = "ready";
      return true;
    } catch {
      return false;
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}
