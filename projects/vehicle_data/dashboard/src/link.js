/**
 * Broker link for dashboard v2.
 *
 * Owns the HTTP baseline (`GET /v1/snapshot`), the `/v2/stream` EventSource,
 * the server-monotonic offset, the 1 s freshness watchdog, stall/error/
 * visibility resyncs and the `/v2/summary` supplemental schedule, exactly as
 * `docs/freshness-contract.md` rules 1–20 describe. It never touches the DOM
 * or globals directly: `fetch`, `EventSource`, timers, the monotonic clock and
 * the visibility query are injected, so the module runs unchanged under
 * `node --test`. Accepted payloads are handed to the injected store
 * (`applyBaseline`, `applyStream`, `applySummary`, `invalidate`, `tick`, and
 * the `connection` signal); the link keeps no copy of the metrics itself.
 *
 * Time discipline: every age uses the injected monotonic `now()`
 * (`performance.now()` in the browser). The wall clock is used only to build
 * `?fresh=` cache-busters.
 *
 * @module link
 */

export * from './link.protocol.js';
import { SNAPSHOT_PATH, STREAM_PATH, SUMMARY_PATH, FRESHNESS_TICK_MS, RESYNC_RETRY_MS,
  STREAM_STALL_RESYNC_MS, SUPPLEMENTAL_REFRESH_MS } from './link.protocol.js';
import { createBaseline } from './link.baseline.js';
import { createStream } from './link.stream.js';
import { createSummary } from './link.summary.js';

function describeError(error) {
  if (!error) return null;
  if (typeof error === 'string') return error;
  return error.message ? String(error.message) : String(error);
}

/**
 * Create the broker link. Nothing runs until `start()`.
 *
 * Connection states published to `store.connection`:
 * - `connecting`: no usable baseline in hand (initial, or invalidated while hidden)
 * - `resyncing`: a baseline exists and an HTTP re-fetch is in progress
 * - `live`: the stream is open (and the last event, if any, was accepted)
 * - `unavailable`: the last HTTP attempt failed or the broker reported an error
 *   on the stream; a retry is scheduled while the page is visible
 *
 * @param {object} deps
 * @param {Function} deps.fetch `fetch(url, init)` returning a Response-like promise
 * @param {Function} deps.EventSource EventSource constructor
 * @param {() => number} [deps.now] monotonic clock (defaults to `performance.now()`)
 * @param {() => number} [deps.wallClock] wall clock for cache-busters (defaults to `Date.now()`)
 * @param {Function} [deps.setTimeout] timer scheduler (defaults to the global)
 * @param {Function} [deps.clearTimeout] timer canceller (defaults to the global)
 * @param {() => string} [deps.visibility] returns `document.visibilityState`
 * @param {Function} [deps.addEventListener] `(type, handler)`; the caller routes
 *   `visibilitychange` to `document` and `pageshow` to `window`
 * @param {Function} [deps.removeEventListener] inverse of `addEventListener`
 * @param {object} deps.store sink with `applyBaseline`, `applyStream`,
 *   `applySummary`, `invalidate`, `tick` and the `connection` signal
 * @param {string} [deps.snapshotPath] baseline route (default `/v1/snapshot`)
 * @param {string} [deps.streamPath] stream route (default `/v2/stream`)
 * @param {string} [deps.summaryPath] supplemental route (default `/v2/summary`)
 * @returns {{start: () => Promise<boolean>, stop: () => void, resync: (reason: string) => Promise<boolean>, state: () => object, fetchSummary: (options?: {force?: boolean}) => Promise<boolean>}}
 *   `fetchSummary({force: true})` bypasses the 60 s throttle (manual refresh).
 */
export function createLink(deps) {
  const fetchImpl = deps.fetch;
  const EventSourceImpl = deps.EventSource;
  const now = deps.now || (() => performance.now());
  const wallClock = deps.wallClock || (() => Date.now());
  // Bare globals, not `globalThis` (ES2020; absent before Chrome 71, target chrome64).
  const setTimer = deps.setTimeout || ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = deps.clearTimeout || ((id) => clearTimeout(id));
  const visibility = deps.visibility || (() => document.visibilityState);
  const addListener = deps.addEventListener || null;
  const removeListener = deps.removeEventListener || null;
  const store = deps.store;
  const snapshotPath = deps.snapshotPath || SNAPSHOT_PATH;
  const streamPath = deps.streamPath || STREAM_PATH;
  const summaryPath = deps.summaryPath || SUMMARY_PATH;

  // Shared delivery facts: baseline and stream update these in the same callback order.
  const deliveryState = {
    accepted: null,
    offsets: null,
    retired: new Set(),
    catalogByName: new Map(),
    catalogHash: null,
    catalogCount: null,
    invalidated: true,
    lastAcceptedMono: null,
    baselineCount: 0,
    lastRejection: null,
  };
  const lifecycle = { retryTimer: null, watchdogTimer: null, started: false };

  // Reused acceptance context: no per-event allocation; only stream acceptance reads it.
  const context = {
    accepting: false,
    hidden: false,
    accepted: null,
    offsets: null,
    retired: deliveryState.retired,
    catalogByName: deliveryState.catalogByName,
    catalogHash: null,
    catalogCount: null,
  };
  const baseline = createBaseline({ deliveryState, context, fetchImpl, now, wallClock, snapshotPath, store });
  const stream = createStream({ deliveryState, context, EventSourceImpl, streamPath, now, hidden, store, resync, setConnection });
  const summary = createSummary({ fetchImpl, now, wallClock, summaryPath, store, describeError });
  const { fetchBaseline } = baseline;
  const { startStream, stopStream } = stream;
  const { fetchSummary } = summary;

  function hidden() {
    return visibility() === 'hidden';
  }

  function setConnection(state, reason, detail) {
    const signal = store.connection;
    if (!signal) return;
    // peek(): a resync started from inside an effect must not subscribe that
    // effect to the connection signal it is about to write (cycle).
    const current = (typeof signal.peek === 'function' ? signal.peek() : signal.value) || {};
    const nextReason = reason == null ? null : reason;
    const nextDetail = detail == null ? null : detail;
    if (current.state === state && current.reason === nextReason && current.detail === nextDetail) return;
    signal.value = {
      state,
      reason: nextReason,
      detail: nextDetail,
      sinceMono: current.state === state && typeof current.sinceMono === 'number' ? current.sinceMono : now(),
    };
  }

  function baselinePendingState() {
    return deliveryState.accepted && !deliveryState.invalidated ? 'resyncing' : 'connecting';
  }

  function clearRetry() {
    if (lifecycle.retryTimer != null) {
      clearTimer(lifecycle.retryTimer);
      lifecycle.retryTimer = null;
    }
  }

  // -- resync (rule 11) -------------------------------------------------------

  function scheduleRetry(reason) {
    clearRetry();
    const retryReason = /_retry$/.test(reason) ? reason : `${reason}_retry`;
    lifecycle.retryTimer = setTimer(() => {
      lifecycle.retryTimer = null;
      resync(retryReason);
    }, RESYNC_RETRY_MS);
  }

  /**
   * Close the stream, fetch a new HTTP baseline and, when accepted and the
   * page is visible, refresh supplementals and reopen the stream.
   * @param {string} reason audit reason (`initial`, `stream_stall`, …)
   * @returns {Promise<boolean>} true when a baseline was accepted for this generation
   */
  function resync(reason) {
    stopStream();
    clearRetry();
    const generation = ++baseline.state.resyncGeneration;
    setConnection(baselinePendingState(), reason, null);
    return fetchBaseline(generation).then(
      (ok) => {
        if (generation !== baseline.state.resyncGeneration || !ok) return false;
        if (!hidden()) {
          startStream();
          setConnection('live', null, null);
          fetchSummary();
        }
        return true;
      },
      (error) => {
        if (generation !== baseline.state.resyncGeneration) return false;
        const code = error && error.reason ? error.reason : 'fetch_failed';
        setConnection('unavailable', code, describeError(error));
        if (!hidden()) scheduleRetry(reason);
        return false;
      },
    );
  }

  // -- watchdog (rule 10) --------------------------------------------------------

  function watchdog() {
    lifecycle.watchdogTimer = null;
    const nowMono = now();
    // A healthy stream already advanced the store; tick only when nothing was
    // accepted in the last second so value bindings are not re-rendered by the clock.
    if (deliveryState.lastAcceptedMono == null || nowMono - deliveryState.lastAcceptedMono >= FRESHNESS_TICK_MS) {
      store.tick(nowMono);
    }
    if (!hidden()) {
      if (
        stream.state.streamAccepting &&
        stream.state.stream &&
        deliveryState.lastAcceptedMono != null &&
        nowMono - deliveryState.lastAcceptedMono > STREAM_STALL_RESYNC_MS
      ) {
        resync('stream_stall');
      } else if (
        deliveryState.accepted &&
        summary.state.summaryStartedMono != null &&
        nowMono - summary.state.summaryStartedMono >= SUPPLEMENTAL_REFRESH_MS
      ) {
        fetchSummary();
      }
    }
    if (lifecycle.started) lifecycle.watchdogTimer = setTimer(watchdog, FRESHNESS_TICK_MS);
  }

  // -- page lifecycle (rule 12) ------------------------------------------------------

  function invalidate(reason) {
    stopStream();
    clearRetry();
    baseline.state.resyncGeneration += 1; // any in-flight baseline is obsolete
    deliveryState.offsets = null;
    context.offsets = null;
    deliveryState.invalidated = true;
    summary.state.summaryStartedMono = null; // the next resync may refresh supplementals at once
    store.invalidate(reason, now());
  }

  function onVisibilityChange() {
    const isHidden = hidden();
    invalidate(isHidden ? 'client_page_hidden' : 'client_page_visible');
    if (isHidden) {
      setConnection('connecting', 'client_page_hidden', null);
    } else {
      resync('visibility');
    }
  }

  function onPageShow() {
    invalidate('client_page_restored');
    resync('pageshow');
  }

  // -- public surface ------------------------------------------------------------

  /**
   * Register lifecycle listeners, start the watchdog and fetch the first baseline.
   * @returns {Promise<boolean>} the initial resync result
   */
  function start() {
    if (lifecycle.started) return Promise.resolve(false);
    lifecycle.started = true;
    if (addListener) {
      addListener('visibilitychange', onVisibilityChange);
      addListener('pageshow', onPageShow);
    }
    lifecycle.watchdogTimer = setTimer(watchdog, FRESHNESS_TICK_MS);
    return resync('initial');
  }

  /** Close the stream, cancel timers and detach listeners. */
  function stop() {
    lifecycle.started = false;
    stopStream();
    clearRetry();
    baseline.state.resyncGeneration += 1;
    baseline.state.httpSequence += 1;
    summary.state.summarySequence += 1;
    if (lifecycle.watchdogTimer != null) {
      clearTimer(lifecycle.watchdogTimer);
      lifecycle.watchdogTimer = null;
    }
    if (removeListener) {
      removeListener('visibilitychange', onVisibilityChange);
      removeListener('pageshow', onPageShow);
    }
  }

  /**
   * Debug/test view of the link state (a fresh object each call; not for the hot path).
   * @returns {object}
   */
  function state() {
    return {
      started: lifecycle.started,
      accepted: deliveryState.accepted,
      offsets: deliveryState.offsets,
      retired: Array.from(deliveryState.retired),
      catalogHash: deliveryState.catalogHash,
      catalogCount: deliveryState.catalogCount,
      catalogSize: deliveryState.catalogByName.size,
      invalidated: deliveryState.invalidated,
      lastAcceptedMono: deliveryState.lastAcceptedMono,
      baselineCount: deliveryState.baselineCount,
      summaryCount: summary.state.summaryCount,
      summaryStartedMono: summary.state.summaryStartedMono,
      streamOpen: stream.state.stream != null,
      streamAccepting: stream.state.streamAccepting,
      resyncGeneration: baseline.state.resyncGeneration,
      retryScheduled: lifecycle.retryTimer != null,
      lastRejection: deliveryState.lastRejection,
    };
  }

  return { start, stop, resync, state, fetchSummary };
}
