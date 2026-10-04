/** Pure transport acceptance rules. Re-exported by link.js; keep freshness checks independent of the store. */

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
