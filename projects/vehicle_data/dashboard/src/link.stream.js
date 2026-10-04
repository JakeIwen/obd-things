/** EventSource lifecycle and dispatch, separate from HTTP acceptance and page  */
import { evaluateStreamEvent, RESYNC_REASONS } from './link.protocol.js';

export function createStream({ deliveryState, context, EventSourceImpl, streamPath, now, hidden, store, resync, setConnection }) {
  const state = { stream: null, streamAccepting: false, streamGeneration: 0 };

  // -- stream (rules 4, 5, 6, 19, 20) ---------------------------------------

  function stopStream() {
    state.streamAccepting = false;
    state.streamGeneration += 1;
    if (state.stream) {
      const closing = state.stream;
      state.stream = null;
      try {
        closing.close();
      } catch (error) {
        // A closed or half-constructed source must not block the resync.
      }
    }
  }

  function handleStreamEvent(payload) {
    const nowMono = now();
    context.accepting = state.streamAccepting;
    context.hidden = hidden();
    const verdict = evaluateStreamEvent(payload, context, nowMono);
    if (!verdict.accepted) {
      deliveryState.lastRejection = { reason: verdict.reason, atMono: nowMono };
      if (RESYNC_REASONS.has(verdict.reason)) resync(verdict.reason);
      return;
    }
    if (deliveryState.catalogHash == null && typeof payload.catalog_hash === 'string') {
      deliveryState.catalogHash = payload.catalog_hash;
      context.catalogHash = deliveryState.catalogHash;
    }
    store.applyStream(payload, verdict.deliveryAgeMs, nowMono);
    deliveryState.accepted = verdict.delivery;
    context.accepted = deliveryState.accepted;
    deliveryState.lastAcceptedMono = nowMono;
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
    const generation = ++state.streamGeneration;
    const source = new EventSourceImpl(streamPath);
    state.stream = source;
    state.streamAccepting = true;
    source.addEventListener('snapshot', (event) => {
      if (generation !== state.streamGeneration || source !== state.stream) return;
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        deliveryState.lastRejection = { reason: 'invalid_json', atMono: now() };
        return;
      }
      handleStreamEvent(payload);
    });
    source.addEventListener('error', (event) => {
      if (generation !== state.streamGeneration || source !== state.stream) return;
      if (event && typeof event.data === 'string') {
        handleBrokerError(event.data);
        return;
      }
      resync('stream_error');
    });
  }

  return { state, startStream, stopStream };
}
