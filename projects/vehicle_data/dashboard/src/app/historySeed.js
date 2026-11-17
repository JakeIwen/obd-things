/**
 * Fill the Drive sparklines from history once per page load (owner request 2026-11-17).
 *
 * After the link first reports `live`, fetch `GET /v1/history/recent` once and seed each metric's
 * ring (`rings.js: seedRing`). Any failure is silent: an older broker answers 404 (the frontend is
 * deployed before the broker restarts), which means "no seed" and is never retried; a network
 * error or 5xx is retried once a minute later. Nothing polls.
 */

import { effect } from "@preact/signals";
import { RECENT_PATH, seedPlan } from "../historySeed.helpers.js";

const RETRY_MS = 60000;
const TIMEOUT_MS = 8000;

/**
 * One fetch-and-seed attempt.
 * @param {{fetchImpl: Function, now: () => number, seed: Function}} deps
 * @returns {Promise<"seeded"|"empty"|"unavailable"|"retry">}
 */
export async function loadHistorySeed({ fetchImpl, now, seed, timeoutMs = TIMEOUT_MS }) {
  const sentMono = now();
  let response;
  let receivedMono;
  let reply;
  // The timer stays armed until the body is read: a server that sends headers and then stalls must
  // not leave seeding pending forever.
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    try {
      response = await fetchImpl(RECENT_PATH, { cache: "no-store", signal: controller && controller.signal });
    } catch (_error) {
      return "retry";
    }
    receivedMono = now();
    if (!response || response.status === 404) return "unavailable";
    if (!response.ok) return response.status >= 500 ? "retry" : "unavailable";
    try {
      reply = await response.json();
    } catch (error) {
      // A malformed body is a contract problem; an abort or a body cut short is network-class.
      return error instanceof SyntaxError ? "unavailable" : "retry";
    }
  } finally {
    if (timer) clearTimeout(timer);
  }
  const plan = seedPlan(reply, { sentMono, receivedMono });
  if (!plan) return "unavailable";
  const names = Object.keys(plan.metrics);
  for (const name of names) seed(name, plan.metrics[name], plan.mapping);
  return names.length ? "seeded" : "empty";
}

/**
 * Seed once when the link first goes live. `cancel()` stops a pending attempt and any later one, so
 * the demo harnesses (which seed their own scene) are never overwritten.
 * @param {{store: object, fetchImpl: Function, now?: () => number, seed: Function}} deps
 */
export function startHistorySeed({ store, fetchImpl, now = () => performance.now(), seed }) {
  let cancelled = false;
  let started = false;
  let retryTimer = null;
  const attempt = async () => {
    const outcome = await loadHistorySeed({ fetchImpl, now, seed: (...args) => !cancelled && seed(...args) });
    if (outcome === "retry" && !cancelled) retryTimer = setTimeout(retryOnce, RETRY_MS);
  };
  const retryOnce = () => {
    retryTimer = null;
    if (!cancelled) loadHistorySeed({ fetchImpl, now, seed: (...args) => !cancelled && seed(...args) });
  };
  const stop = effect(() => {
    if (started || store.connection.value.state !== "live") return;
    started = true;
    attempt();
  });
  return {
    cancel() {
      cancelled = true;
      stop();
      if (retryTimer !== null) clearTimeout(retryTimer);
    },
  };
}
