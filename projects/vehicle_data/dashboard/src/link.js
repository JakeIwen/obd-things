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

/** Verified vehicle state older than this (ms) is unknown (rule 6/7). */
export const MAX_STATE_FALLBACK_AGE_MS = 5000;
/** Stream events delivered later than this (ms) are rejected (rule 4). */
export const MAX_STREAM_DELIVERY_AGE_MS = 10000;
/** HTTP baselines slower than this round trip (ms) are rejected (rule 2). */
export const MAX_HTTP_ROUND_TRIP_MS = 2000;
/** No accepted stream event for longer than this (ms) forces a resync (rule 10). */
export const STREAM_STALL_RESYNC_MS = 3000;
/** Watchdog period (ms) (rule 10). */
export const FRESHNESS_TICK_MS = 1000;
/** Retry period (ms) while the baseline cannot be established (rule 2). */
export const RESYNC_RETRY_MS = 2000;
/** Supplemental refresh period and throttle (ms) (rule 13). */
export const SUPPLEMENTAL_REFRESH_MS = 60000;
/** Bound on the retired-instance set (rule 5). */
export const MAX_RETIRED_INSTANCES = 8;

/** Route of the HTTP baseline snapshot. */
export const SNAPSHOT_PATH = '/v1/snapshot';
/** Route of the lite server-sent event stream. */
export const STREAM_PATH = '/v2/stream';
/** Route of the supplemental bundle. */
export const SUMMARY_PATH = '/v2/summary';

/**
 * Stream rejection reasons that require a fresh HTTP baseline (rules 4, 5, 19).
 * Every other rejection simply drops the event.
 */
export const RESYNC_REASONS = new Set([
  'instance_changed',
  'http_resync_required',
  'queued_stream_event',
  'catalog_changed',
]);

const REJECTION_TEXT = {
  missing_delivery_metadata: 'snapshot is missing web delivery metadata',
  http_response_delayed: 'snapshot HTTP response exceeded the 2 s freshness bound',
  out_of_order: 'snapshot arrived out of order',
  retired_instance: 'snapshot came from a retired web instance',
  http_error: 'snapshot request failed',
};

/**
 * @typedef {object} Delivery
 * @property {string} instanceId web process instance (32 hex)
 * @property {number} sequence process-global increasing sequence
 * @property {number} generatedAtMs wall-clock generation time (display only)
 * @property {number} generatedMonotonicMs web process CLOCK_MONOTONIC ms
 */

/**
 * @typedef {object} Offsets
 * @property {number} offsetMs client midpoint monotonic − server monotonic (rule 3)
 * @property {number} uncertaintyMs half the bounded HTTP round trip (rule 3)
 */

/**
 * @typedef {object} StreamContext
 * @property {boolean} accepting the stream-accepting flag (cleared by stopStream)
 * @property {boolean} hidden `document.visibilityState === 'hidden'`
 * @property {Delivery|null} accepted the last accepted delivery
 * @property {Offsets|null} offsets the current offset pair, or null before a baseline
 * @property {Set<string>} retired retired web instances
 * @property {Map<string, object>|object} catalogByName name → catalog definition
 * @property {string|null} catalogHash hash adopted from the baseline or first event
 * @property {number|null} catalogCount catalog length from the baseline
 */

/**
 * @typedef {object} StreamVerdict
 * @property {boolean} accepted
 * @property {string} reason `accepted` or the rejection reason
 * @property {Delivery|null} delivery validated delivery metadata (null when missing)
 * @property {number|null} deliveryAgeMs computed delivery age when reachable
 */

/**
 * True when a value is a usable `age_ms`: a finite, non-negative number.
 * @param {*} value
 * @returns {boolean}
 */
export function isValidAge(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

/**
 * Rule 1: validate `web_delivery` before any use.
 * @param {*} payload snapshot or stream event
 * @returns {Delivery|null} normalised delivery, or null when rejected
 */
export function validateDelivery(payload) {
  const delivery = payload && typeof payload === 'object' ? payload.web_delivery : null;
  if (!delivery || typeof delivery !== 'object') return null;
  if (typeof delivery.instance_id !== 'string' || !delivery.instance_id) return null;
  if (!Number.isSafeInteger(delivery.sequence) || delivery.sequence < 1) return null;
  if (!Number.isSafeInteger(delivery.generated_at_ms) || delivery.generated_at_ms < 1) return null;
  if (!Number.isSafeInteger(delivery.generated_monotonic_ms) || delivery.generated_monotonic_ms < 0) {
    return null;
  }
  return {
    instanceId: delivery.instance_id,
    sequence: delivery.sequence,
    generatedAtMs: delivery.generated_at_ms,
    generatedMonotonicMs: delivery.generated_monotonic_ms,
  };
}

/**
 * Rule 4: delivery age of a stream event, `max(0, now − (generated + offset)) +
 * uncertainty`. The uncertainty keeps calibration from ever making an event
 * younger than it can be; wall-clock steps cannot influence the result.
 * @param {object} event stream event (or anything carrying `web_delivery`)
 * @param {Offsets|null} offsets offsets from the last HTTP baseline
 * @param {number} nowMono current monotonic ms
 * @returns {number|null} delivery age, or null when the delivery or offsets are unusable
 */
export function deliveryAgeForStream(event, offsets, nowMono) {
  const delivery = validateDelivery(event);
  if (!delivery || !offsets) return null;
  if (!Number.isFinite(offsets.offsetMs) || !Number.isFinite(offsets.uncertaintyMs)) return null;
  const age = Math.max(0, nowMono - (delivery.generatedMonotonicMs + offsets.offsetMs));
  return age + offsets.uncertaintyMs;
}

function lookupDefinition(catalogByName, name) {
  if (!catalogByName) return undefined;
  if (typeof catalogByName.get === 'function') return catalogByName.get(name);
  return Object.prototype.hasOwnProperty.call(catalogByName, name) ? catalogByName[name] : undefined;
}

function normalizedQuality(value) {
  return String(value || 'unknown').toLowerCase().replace(/ /g, '_');
}

/**
 * Rule 6 for one metric: an available, non-stale observation expires when its
 * age plus the added delivery age passes the catalog's `stale_after_seconds`,
 * or when the age or the catalog limit is unusable.
 * @param {object} metric observation as delivered
 * @param {object|undefined} definition catalog definition for the metric
 * @param {number} addedAgeMs delivery age about to be added
 * @returns {boolean}
 */
export function metricWouldExpire(metric, definition, addedAgeMs) {
  if (!metric || !metric.available || metric.stale) return false;
  if (!isValidAge(metric.age_ms)) return true;
  if (!definition || definition.stale_after_seconds == null) return true;
  const staleAfterMs = Number(definition.stale_after_seconds) * 1000;
  return !Number.isFinite(staleAfterMs) || staleAfterMs < 0 || metric.age_ms + addedAgeMs > staleAfterMs;
}

/**
 * Rule 6: would adding `addedAgeMs` expire any available metric, or a verified
 * vehicle state (5 s window)? Allocation-free so it can run per event.
 * @param {object} metrics `metrics` object of the event
 * @param {Map<string, object>|object} catalogByName name → catalog definition
 * @param {object|null|undefined} vehicleState `status.vehicle_state`
 * @param {number} addedAgeMs delivery age about to be added
 * @returns {boolean}
 */
export function wouldExpire(metrics, catalogByName, vehicleState, addedAgeMs) {
  if (metrics && typeof metrics === 'object') {
    for (const name in metrics) {
      if (!Object.prototype.hasOwnProperty.call(metrics, name)) continue;
      if (metricWouldExpire(metrics[name], lookupDefinition(catalogByName, name), addedAgeMs)) {
        return true;
      }
    }
  }
  if (vehicleState && normalizedQuality(vehicleState.confidence) === 'verified') {
    if (!isValidAge(vehicleState.age_ms)) return true;
    return vehicleState.age_ms + addedAgeMs > MAX_STATE_FALLBACK_AGE_MS;
  }
  return false;
}

/**
 * Rule 19 helper: does the event announce a catalog other than the one held?
 * The HTTP baseline carries no `catalog_hash`, so the first stream event's
 * hash is adopted and later events are compared against it; `catalog_count`
 * (when present) is compared against the baseline catalog length as well.
 * @param {object} event stream event
 * @param {StreamContext} ctx
 * @returns {boolean}
 */
export function catalogChanged(event, ctx) {
  const hash = typeof event.catalog_hash === 'string' ? event.catalog_hash : null;
  if (hash != null && ctx.catalogHash != null && hash !== ctx.catalogHash) return true;
  if (
    Number.isSafeInteger(event.catalog_count) &&
    ctx.catalogCount != null &&
    event.catalog_count !== ctx.catalogCount
  ) {
    return true;
  }
  return false;
}

/**
 * Rules 4, 5, 6 and 19 as one pure decision: classify a `/v2/stream` event
 * against the link's current context. The checks run in the audited order
 * (retired → blocked → instance → offsets → delivery age/would-expire →
 * ordering → catalog).
 * @param {object} event parsed stream event
 * @param {StreamContext} ctx
 * @param {number} nowMono current monotonic ms
 * @returns {StreamVerdict}
 */
export function evaluateStreamEvent(event, ctx, nowMono) {
  const delivery = validateDelivery(event);
  if (!delivery) return { accepted: false, reason: 'missing_delivery_metadata', delivery: null, deliveryAgeMs: null };
  if (ctx.retired && ctx.retired.has(delivery.instanceId)) {
    return { accepted: false, reason: 'retired_instance', delivery, deliveryAgeMs: null };
  }
  if (!ctx.accepting || ctx.hidden) {
    return { accepted: false, reason: 'stream_blocked', delivery, deliveryAgeMs: null };
  }
  if (!ctx.accepted || delivery.instanceId !== ctx.accepted.instanceId) {
    return { accepted: false, reason: 'instance_changed', delivery, deliveryAgeMs: null };
  }
  const deliveryAgeMs = deliveryAgeForStream(event, ctx.offsets, nowMono);
  if (deliveryAgeMs == null) {
    return { accepted: false, reason: 'http_resync_required', delivery, deliveryAgeMs: null };
  }
  const vehicleState = event.status && typeof event.status === 'object' ? event.status.vehicle_state : null;
  if (
    deliveryAgeMs > MAX_STREAM_DELIVERY_AGE_MS ||
    wouldExpire(event.metrics, ctx.catalogByName, vehicleState, deliveryAgeMs)
  ) {
    return { accepted: false, reason: 'queued_stream_event', delivery, deliveryAgeMs };
  }
  if (
    delivery.sequence <= ctx.accepted.sequence ||
    delivery.generatedMonotonicMs < ctx.accepted.generatedMonotonicMs
  ) {
    return { accepted: false, reason: 'out_of_order', delivery, deliveryAgeMs };
  }
  if (catalogChanged(event, ctx)) {
    return { accepted: false, reason: 'catalog_changed', delivery, deliveryAgeMs };
  }
  return { accepted: true, reason: 'accepted', delivery, deliveryAgeMs };
}

/**
 * Build the name → definition map from a snapshot catalog.
 * @param {Array<object>|*} catalog
 * @returns {Map<string, object>}
 */
export function indexCatalog(catalog) {
  const map = new Map();
  if (Array.isArray(catalog)) {
    for (const definition of catalog) {
      if (definition && typeof definition.name === 'string') map.set(definition.name, definition);
    }
  }
  return map;
}

function linkError(reason, detail) {
  const error = new Error(detail || REJECTION_TEXT[reason] || `snapshot rejected: ${reason}`);
  error.reason = reason;
  return error;
}

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

  /** @type {Delivery|null} */
  let accepted = null;
  /** @type {Offsets|null} */
  let offsets = null;
  const retired = new Set();
  let catalogByName = new Map();
  let catalogHash = null;
  let catalogCount = null;
  let invalidated = true;
  let lastAcceptedMono = null;
  let baselineCount = 0;

  let stream = null;
  let streamAccepting = false;
  let streamGeneration = 0;
  let resyncGeneration = 0;
  let httpSequence = 0;
  let latestHttpResponseSequence = 0;
  let retryTimer = null;
  let watchdogTimer = null;
  let started = false;

  let summarySequence = 0;
  let summaryStartedMono = null;
  let summaryInFlight = null;
  let summaryCount = 0;

  let lastRejection = null;

  // One reused context object keeps the per-second path allocation-free.
  const context = {
    accepting: false,
    hidden: false,
    accepted: null,
    offsets: null,
    retired,
    catalogByName,
    catalogHash: null,
    catalogCount: null,
  };

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
    return accepted && !invalidated ? 'resyncing' : 'connecting';
  }

  function retireInstance(instanceId) {
    retired.add(instanceId);
    if (retired.size > MAX_RETIRED_INSTANCES) {
      retired.delete(retired.values().next().value);
    }
  }

  function adoptCatalog(catalog, hash) {
    catalogByName = indexCatalog(catalog);
    catalogCount = Array.isArray(catalog) ? catalog.length : null;
    catalogHash = typeof hash === 'string' ? hash : null;
    context.catalogByName = catalogByName;
    context.catalogCount = catalogCount;
    context.catalogHash = catalogHash;
  }

  function clearRetry() {
    if (retryTimer != null) {
      clearTimer(retryTimer);
      retryTimer = null;
    }
  }

  // -- HTTP baseline (rules 1, 2, 3, 5, 7) ---------------------------------

  function acceptBaseline(payload, timing) {
    const delivery = validateDelivery(payload);
    if (!delivery) return { accepted: false, reason: 'missing_delivery_metadata' };
    if (retired.has(delivery.instanceId)) return { accepted: false, reason: 'retired_instance' };
    if (timing.roundTripMs > MAX_HTTP_ROUND_TRIP_MS) return { accepted: false, reason: 'http_response_delayed' };
    if (accepted && delivery.instanceId === accepted.instanceId) {
      if (
        delivery.sequence <= accepted.sequence ||
        delivery.generatedMonotonicMs < accepted.generatedMonotonicMs
      ) {
        return { accepted: false, reason: 'out_of_order' };
      }
    } else if (accepted) {
      retireInstance(accepted.instanceId);
    }
    offsets = {
      offsetMs: timing.midpointMono - delivery.generatedMonotonicMs,
      uncertaintyMs: timing.roundTripMs / 2,
    };
    context.offsets = offsets;
    adoptCatalog(payload.catalog, payload.catalog_hash);
    // The full bounded round trip is the conservative age at receipt (rule 3);
    // the store performs the rule 7 ageing with it.
    store.applyBaseline(payload, timing.roundTripMs, timing.receivedMono);
    accepted = delivery;
    context.accepted = accepted;
    invalidated = false;
    lastAcceptedMono = timing.receivedMono;
    baselineCount += 1;
    return { accepted: true, reason: 'accepted' };
  }

  async function fetchBaseline(generation) {
    const sequence = ++httpSequence;
    const startedMono = now();
    const url = `${snapshotPath}?fresh=${wallClock()}-${sequence}`;
    const response = await fetchImpl(url, { cache: 'no-store' });
    const payload = await response.json();
    const receivedMono = now();
    const roundTripMs = Math.max(0, receivedMono - startedMono);
    if (
      generation !== resyncGeneration ||
      sequence !== httpSequence ||
      sequence <= latestHttpResponseSequence
    ) {
      return false; // obsolete callback (rule 11): never applied
    }
    if (!response.ok) {
      throw linkError('http_error', (payload && payload.detail) || `HTTP ${response.status}`);
    }
    const result = acceptBaseline(payload, {
      roundTripMs,
      midpointMono: startedMono + roundTripMs / 2,
      receivedMono,
    });
    if (!result.accepted) throw linkError(result.reason);
    latestHttpResponseSequence = sequence;
    return true;
  }

  // -- stream (rules 4, 5, 6, 19, 20) ---------------------------------------

  function stopStream() {
    streamAccepting = false;
    streamGeneration += 1;
    if (stream) {
      const closing = stream;
      stream = null;
      try {
        closing.close();
      } catch (error) {
        // A closed or half-constructed source must not block the resync.
      }
    }
  }

  function handleStreamEvent(payload) {
    const nowMono = now();
    context.accepting = streamAccepting;
    context.hidden = hidden();
    const verdict = evaluateStreamEvent(payload, context, nowMono);
    if (!verdict.accepted) {
      lastRejection = { reason: verdict.reason, atMono: nowMono };
      if (RESYNC_REASONS.has(verdict.reason)) resync(verdict.reason);
      return;
    }
    if (catalogHash == null && typeof payload.catalog_hash === 'string') {
      catalogHash = payload.catalog_hash;
      context.catalogHash = catalogHash;
    }
    store.applyStream(payload, verdict.deliveryAgeMs, nowMono);
    accepted = verdict.delivery;
    context.accepted = accepted;
    lastAcceptedMono = nowMono;
    setConnection('live', null, null);
  }

  function handleBrokerError(data) {
    let payload = null;
    try {
      payload = JSON.parse(data);
    } catch (error) {
      payload = null;
    }
    const reason = payload && typeof payload.reason === 'string' ? payload.reason : 'broker_unavailable';
    const detail = payload && payload.detail != null ? String(payload.detail) : (payload ? null : String(data));
    // The connection is still open: the server keeps looping and the next
    // snapshot event restores `live`. The stall watchdog covers a long outage.
    setConnection('unavailable', reason, detail);
  }

  function startStream() {
    const generation = ++streamGeneration;
    const source = new EventSourceImpl(streamPath);
    stream = source;
    streamAccepting = true;
    source.addEventListener('snapshot', (event) => {
      if (generation !== streamGeneration || source !== stream) return;
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        lastRejection = { reason: 'invalid_json', atMono: now() };
        return;
      }
      handleStreamEvent(payload);
    });
    source.addEventListener('error', (event) => {
      if (generation !== streamGeneration || source !== stream) return;
      if (event && typeof event.data === 'string') {
        handleBrokerError(event.data);
        return;
      }
      resync('stream_error');
    });
  }

  // -- resync (rule 11) -------------------------------------------------------

  function scheduleRetry(reason) {
    clearRetry();
    const retryReason = /_retry$/.test(reason) ? reason : `${reason}_retry`;
    retryTimer = setTimer(() => {
      retryTimer = null;
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
    const generation = ++resyncGeneration;
    setConnection(baselinePendingState(), reason, null);
    return fetchBaseline(generation).then(
      (ok) => {
        if (generation !== resyncGeneration || !ok) return false;
        if (!hidden()) {
          startStream();
          setConnection('live', null, null);
          fetchSummary();
        }
        return true;
      },
      (error) => {
        if (generation !== resyncGeneration) return false;
        const code = error && error.reason ? error.reason : 'fetch_failed';
        setConnection('unavailable', code, describeError(error));
        if (!hidden()) scheduleRetry(reason);
        return false;
      },
    );
  }

  // -- supplementals (rule 13) ---------------------------------------------------

  /**
   * Fetch `/v2/summary` once, throttled to one start per 60 s; obsolete
   * responses are dropped. Failures become `{available:false, reason:'cache_unavailable'}`.
   * @returns {Promise<boolean>} true when a bundle was handed to the store
   */
  function fetchSummary(options) {
    if (summaryInFlight) return summaryInFlight;
    const startedMono = now();
    const force = Boolean(options && options.force);
    if (!force && summaryStartedMono != null && startedMono - summaryStartedMono < SUPPLEMENTAL_REFRESH_MS) {
      return Promise.resolve(false);
    }
    summaryStartedMono = startedMono;
    const sequence = ++summarySequence;
    const url = `${summaryPath}?fresh=${wallClock()}-${sequence}`;
    const operation = (async () => {
      let bundle;
      try {
        const response = await fetchImpl(url, { cache: 'no-store' });
        const payload = await response.json();
        if (response.ok) {
          bundle = payload;
        } else {
          bundle = {
            available: false,
            reason: (payload && payload.reason) || 'cache_unavailable',
            detail: (payload && payload.detail) || `HTTP ${response.status}`,
            status_code: response.status,
          };
        }
      } catch (error) {
        bundle = { available: false, reason: 'cache_unavailable', detail: describeError(error) };
      }
      if (sequence !== summarySequence) return false;
      summaryCount += 1;
      store.applySummary(bundle, now());
      return true;
    })();
    const tracked = operation.then(
      (value) => {
        if (summaryInFlight === tracked) summaryInFlight = null;
        return value;
      },
      (error) => {
        if (summaryInFlight === tracked) summaryInFlight = null;
        throw error;
      },
    );
    summaryInFlight = tracked;
    return tracked;
  }

  // -- watchdog (rule 10) --------------------------------------------------------

  function watchdog() {
    watchdogTimer = null;
    const nowMono = now();
    // A healthy stream already advanced the store; tick only when nothing was
    // accepted in the last second so value bindings are not re-rendered by the clock.
    if (lastAcceptedMono == null || nowMono - lastAcceptedMono >= FRESHNESS_TICK_MS) {
      store.tick(nowMono);
    }
    if (!hidden()) {
      if (
        streamAccepting &&
        stream &&
        lastAcceptedMono != null &&
        nowMono - lastAcceptedMono > STREAM_STALL_RESYNC_MS
      ) {
        resync('stream_stall');
      } else if (
        accepted &&
        summaryStartedMono != null &&
        nowMono - summaryStartedMono >= SUPPLEMENTAL_REFRESH_MS
      ) {
        fetchSummary();
      }
    }
    if (started) watchdogTimer = setTimer(watchdog, FRESHNESS_TICK_MS);
  }

  // -- page lifecycle (rule 12) ------------------------------------------------------

  function invalidate(reason) {
    stopStream();
    clearRetry();
    resyncGeneration += 1; // any in-flight baseline is obsolete
    offsets = null;
    context.offsets = null;
    invalidated = true;
    summaryStartedMono = null; // the next resync may refresh supplementals at once
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
    if (started) return Promise.resolve(false);
    started = true;
    if (addListener) {
      addListener('visibilitychange', onVisibilityChange);
      addListener('pageshow', onPageShow);
    }
    watchdogTimer = setTimer(watchdog, FRESHNESS_TICK_MS);
    return resync('initial');
  }

  /** Close the stream, cancel timers and detach listeners. */
  function stop() {
    started = false;
    stopStream();
    clearRetry();
    resyncGeneration += 1;
    httpSequence += 1;
    summarySequence += 1;
    if (watchdogTimer != null) {
      clearTimer(watchdogTimer);
      watchdogTimer = null;
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
      started,
      accepted,
      offsets,
      retired: Array.from(retired),
      catalogHash,
      catalogCount,
      catalogSize: catalogByName.size,
      invalidated,
      lastAcceptedMono,
      baselineCount,
      summaryCount,
      summaryStartedMono,
      streamOpen: stream != null,
      streamAccepting,
      resyncGeneration,
      retryScheduled: retryTimer != null,
      lastRejection,
    };
  }

  return { start, stop, resync, state, fetchSummary };
}
