/** HTTP baseline acceptance and request ordering; all mutable transport state is explicit. */
import { validateDelivery, indexCatalog, MAX_HTTP_ROUND_TRIP_MS, MAX_RETIRED_INSTANCES } from './link.protocol.js';

const REJECTION_TEXT = {
  missing_delivery_metadata: 'snapshot is missing web delivery metadata',
  http_response_delayed: 'snapshot HTTP response exceeded the 2 s freshness bound',
  out_of_order: 'snapshot arrived out of order',
  retired_instance: 'snapshot came from a retired web instance',
  http_error: 'snapshot request failed',
};

function linkError(reason, detail) {
  const error = new Error(detail || REJECTION_TEXT[reason] || `snapshot rejected: ${reason}`);
  error.reason = reason;
  return error;
}

export function createBaseline({ deliveryState, context, fetchImpl, now, wallClock, snapshotPath, store }) {
  const state = { resyncGeneration: 0, httpSequence: 0, latestHttpResponseSequence: 0 };

  function retireInstance(instanceId) {
    deliveryState.retired.add(instanceId);
    if (deliveryState.retired.size > MAX_RETIRED_INSTANCES) {
      deliveryState.retired.delete(deliveryState.retired.values().next().value);
    }
  }

  function adoptCatalog(catalog, hash) {
    deliveryState.catalogByName = indexCatalog(catalog);
    deliveryState.catalogCount = Array.isArray(catalog) ? catalog.length : null;
    deliveryState.catalogHash = typeof hash === 'string' ? hash : null;
    context.catalogByName = deliveryState.catalogByName;
    context.catalogCount = deliveryState.catalogCount;
    context.catalogHash = deliveryState.catalogHash;
  }

  // -- HTTP baseline (rules 1, 2, 3, 5, 7) ---------------------------------

  function acceptBaseline(payload, timing) {
    const delivery = validateDelivery(payload);
    if (!delivery) return { accepted: false, reason: 'missing_delivery_metadata' };
    if (deliveryState.retired.has(delivery.instanceId)) return { accepted: false, reason: 'retired_instance' };
    if (timing.roundTripMs > MAX_HTTP_ROUND_TRIP_MS) return { accepted: false, reason: 'http_response_delayed' };
    if (deliveryState.accepted && delivery.instanceId === deliveryState.accepted.instanceId) {
      if (
        delivery.sequence <= deliveryState.accepted.sequence ||
        delivery.generatedMonotonicMs < deliveryState.accepted.generatedMonotonicMs
      ) {
        return { accepted: false, reason: 'out_of_order' };
      }
    } else if (deliveryState.accepted) {
      retireInstance(deliveryState.accepted.instanceId);
    }
    deliveryState.offsets = {
      offsetMs: timing.midpointMono - delivery.generatedMonotonicMs,
      uncertaintyMs: timing.roundTripMs / 2,
    };
    context.offsets = deliveryState.offsets;
    adoptCatalog(payload.catalog, payload.catalog_hash);
    // The full bounded round trip is the conservative age at receipt (rule 3);
    // the store performs the rule 7 ageing with it.
    store.applyBaseline(payload, timing.roundTripMs, timing.receivedMono);
    deliveryState.accepted = delivery;
    context.accepted = deliveryState.accepted;
    deliveryState.invalidated = false;
    deliveryState.lastAcceptedMono = timing.receivedMono;
    deliveryState.baselineCount += 1;
    return { accepted: true, reason: 'accepted' };
  }

  async function fetchBaseline(generation) {
    const sequence = ++state.httpSequence;
    const startedMono = now();
    const url = `${snapshotPath}?fresh=${wallClock()}-${sequence}`;
    const response = await fetchImpl(url, { cache: 'no-store' });
    const payload = await response.json();
    const receivedMono = now();
    const roundTripMs = Math.max(0, receivedMono - startedMono);
    if (
      generation !== state.resyncGeneration ||
      sequence !== state.httpSequence ||
      sequence <= state.latestHttpResponseSequence
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
    state.latestHttpResponseSequence = sequence;
    return true;
  }

  return { state, fetchBaseline };
}
