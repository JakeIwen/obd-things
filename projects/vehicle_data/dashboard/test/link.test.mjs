// Tests for src/link.js: freshness-contract rules 1–20 against a fake store,
// fake fetch/EventSource and a controllable monotonic clock.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  createLink,
  validateDelivery,
  deliveryAgeForStream,
  wouldExpire,
  metricWouldExpire,
  evaluateStreamEvent,
  catalogChanged,
  indexCatalog,
  MAX_STREAM_DELIVERY_AGE_MS,
  MAX_HTTP_ROUND_TRIP_MS,
  RESYNC_RETRY_MS,
  SUPPLEMENTAL_REFRESH_MS,
  MAX_RETIRED_INSTANCES,
} from '../src/link.js';

const SNAPSHOT = JSON.parse(readFileSync(new URL('./fixtures/snapshot.json', import.meta.url), 'utf8'));
const SUMMARY = JSON.parse(readFileSync(new URL('./fixtures/summary-v2.link.json', import.meta.url), 'utf8'));
const CATALOG_BY_NAME = indexCatalog(SNAPSHOT.catalog);
const INSTANCE = SNAPSHOT.web_delivery.instance_id;
const BASE_MONO = SNAPSHOT.web_delivery.generated_monotonic_ms;
const HASH = 'a1b2c3d4e5f60718';

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

/** Snapshot copy with a chosen delivery record. */
function snapshotWith(delivery, extra) {
  const copy = clone(SNAPSHOT);
  copy.web_delivery = Object.assign({}, SNAPSHOT.web_delivery, delivery || {});
  if (extra) Object.assign(copy, extra);
  return copy;
}

/** A `/v2/stream` event body (status-lite + metrics + catalog_hash). */
function streamEvent(overrides) {
  const o = overrides || {};
  return {
    status: { vehicle_state: o.vehicleState || clone(SNAPSHOT.status.vehicle_state) },
    metrics: o.metrics || clone(SNAPSHOT.metrics),
    catalog_hash: o.catalogHash === undefined ? HASH : o.catalogHash,
    catalog_count: o.catalogCount === undefined ? SNAPSHOT.catalog.length : o.catalogCount,
    web: { active_acquisition_enabled: true, dtc_jobs_enabled: false, warning_chat_enabled: true },
    web_delivery: {
      instance_id: o.instanceId || INSTANCE,
      sequence: o.sequence,
      generated_at_ms: o.generatedAtMs || 1790103502381,
      generated_monotonic_ms: o.generatedMonotonicMs,
    },
  };
}

function makeStore() {
  const calls = [];
  const connectionHistory = [];
  let connectionValue = { state: 'connecting', reason: null, detail: null, sinceMono: 0 };
  return {
    calls,
    connectionHistory,
    connection: {
      get value() {
        return connectionValue;
      },
      set value(next) {
        connectionValue = next;
        connectionHistory.push(next);
      },
    },
    applyBaseline(snapshot, deliveryAgeMs, nowMono) {
      calls.push({ fn: 'applyBaseline', snapshot, deliveryAgeMs, nowMono });
    },
    applyStream(event, deliveryAgeMs, nowMono) {
      calls.push({ fn: 'applyStream', event, deliveryAgeMs, nowMono });
    },
    applySummary(bundle, nowMono) {
      calls.push({ fn: 'applySummary', bundle, nowMono });
    },
    invalidate(reason, nowMono) {
      calls.push({ fn: 'invalidate', reason, nowMono });
    },
    tick(nowMono) {
      calls.push({ fn: 'tick', nowMono });
    },
    of(fn) {
      return calls.filter((call) => call.fn === fn);
    },
  };
}

class FakeEventSource {
  constructor(url, sink) {
    this.url = url;
    this.listeners = new Map();
    this.closed = false;
    sink.push(this);
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  close() {
    this.closed = true;
  }

  emit(type, data) {
    const event = data === undefined ? {} : { data: typeof data === 'string' ? data : JSON.stringify(data) };
    for (const handler of this.listeners.get(type) || []) handler(event);
  }
}

/**
 * Fake browser: controllable monotonic clock, timers, fetch, EventSource, visibility.
 * `h.responder(url)` decides each fetch: {body, ok, status, latency, error, pending}.
 */
function harness(options) {
  const o = options || {};
  const h = {
    mono: 0,
    epoch: 1790103600000,
    visibilityState: o.visibility || 'visible',
    timers: [],
    nextTimer: 1,
    fetches: [],
    sources: [],
    listeners: new Map(),
    store: makeStore(),
    fetchLatency: 100,
    responder: null,
    serverSeq: SNAPSHOT.web_delivery.sequence - 1,
  };
  // Default server: the process-global sequence grows with every snapshot and
  // stream event, and the server monotonic clock runs alongside `h.mono`.
  h.responder = (url) => {
    if (url.startsWith('/v1/snapshot')) {
      h.serverSeq += 1;
      return { body: snapshotWith({ sequence: h.serverSeq, generated_monotonic_ms: BASE_MONO + h.mono }) };
    }
    if (url.startsWith('/v2/summary')) return { body: clone(SUMMARY) };
    return { ok: false, status: 404, body: { available: false, reason: 'not_found' } };
  };
  h.fetch = (url, init) => {
    const record = { url, init, startedMono: h.mono, pending: null };
    h.fetches.push(record);
    const reply = h.responder(url, record) || {};
    if (reply.error) return Promise.reject(reply.error);
    const latency = reply.latency != null ? reply.latency : h.fetchLatency;
    const response = {
      ok: reply.ok !== false,
      status: reply.status || (reply.ok === false ? 500 : 200),
      json() {
        h.mono += latency;
        return Promise.resolve(reply.body);
      },
    };
    if (reply.pending) {
      return new Promise((resolve, reject) => {
        record.pending = { resolve: () => resolve(response), reject };
      });
    }
    return Promise.resolve(response);
  };
  h.EventSource = class extends FakeEventSource {
    constructor(url) {
      super(url, h.sources);
    }
  };
  h.setTimeout = (fn, ms) => {
    const id = h.nextTimer++;
    h.timers.push({ id, at: h.mono + ms, fn });
    return id;
  };
  h.clearTimeout = (id) => {
    h.timers = h.timers.filter((timer) => timer.id !== id);
  };
  h.addEventListener = (type, fn) => {
    if (!h.listeners.has(type)) h.listeners.set(type, []);
    h.listeners.get(type).push(fn);
  };
  h.removeEventListener = (type, fn) => {
    h.listeners.set(type, (h.listeners.get(type) || []).filter((entry) => entry !== fn));
  };
  h.dispatch = (type) => {
    for (const fn of h.listeners.get(type) || []) fn({ type });
  };
  h.flush = async () => {
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  /** Advance the monotonic clock, firing due timers in order and settling promises. */
  h.advance = async (ms) => {
    const target = h.mono + ms;
    for (;;) {
      await h.flush();
      const due = h.timers.filter((timer) => timer.at <= target).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      h.timers = h.timers.filter((timer) => timer !== due);
      if (due.at > h.mono) h.mono = due.at;
      due.fn();
    }
    if (target > h.mono) h.mono = target;
    await h.flush();
  };
  h.link = createLink({
    fetch: h.fetch,
    EventSource: h.EventSource,
    now: () => h.mono,
    wallClock: () => h.epoch + h.mono,
    setTimeout: h.setTimeout,
    clearTimeout: h.clearTimeout,
    visibility: () => h.visibilityState,
    addEventListener: h.addEventListener,
    removeEventListener: h.removeEventListener,
    store: h.store,
  });
  h.source = () => h.sources[h.sources.length - 1];
  h.snapshotFetches = () => h.fetches.filter((f) => f.url.startsWith('/v1/snapshot'));
  h.summaryFetches = () => h.fetches.filter((f) => f.url.startsWith('/v2/summary'));
  h.connection = () => h.store.connection.value;
  /** Emit a stream event generated `ageMs` ago on the server clock (next sequence unless given). */
  h.emitEvent = (overrides) => {
    const ov = overrides || {};
    const offsets = h.link.state().offsets;
    const ageMs = ov.ageMs || 0;
    let sequence = ov.sequence;
    if (sequence == null) {
      h.serverSeq += 1;
      sequence = h.serverSeq;
    }
    h.serverSeq = Math.max(h.serverSeq, sequence);
    const event = streamEvent(Object.assign({}, ov, {
      sequence,
      generatedMonotonicMs: ov.generatedMonotonicMs != null
        ? ov.generatedMonotonicMs
        : Math.round(h.mono - offsets.offsetMs - ageMs),
    }));
    if (ov.mutate) ov.mutate(event);
    h.source().emit('snapshot', event);
    return event;
  };
  /** Advance `ms` in 1 s steps while a healthy stream delivers one event per second. */
  h.keepAlive = async (ms) => {
    let remaining = ms;
    while (remaining > 0) {
      const step = Math.min(1000, remaining);
      await h.advance(step);
      remaining -= step;
      const source = h.source();
      if (source && !source.closed && h.link.state().offsets) h.emitEvent();
    }
  };
  /** Boot: start, settle the baseline at t=0 (100 ms round trip). */
  h.boot = async () => {
    const promise = h.link.start();
    await h.flush();
    return promise;
  };
  return h;
}

// ---------------------------------------------------------------------------
// Rule 1: delivery validation

test('validateDelivery accepts the fixture delivery and normalises names', () => {
  const delivery = validateDelivery(SNAPSHOT);
  assert.deepEqual(delivery, {
    instanceId: INSTANCE,
    sequence: 273,
    generatedAtMs: 1790103502381,
    generatedMonotonicMs: 1129733584,
  });
});

test('validateDelivery rejects every malformed field', () => {
  const base = SNAPSHOT.web_delivery;
  const bad = [
    null,
    undefined,
    {},
    { web_delivery: null },
    { web_delivery: 'x' },
    { web_delivery: Object.assign({}, base, { instance_id: '' }) },
    { web_delivery: Object.assign({}, base, { instance_id: 12 }) },
    { web_delivery: Object.assign({}, base, { sequence: 0 }) },
    { web_delivery: Object.assign({}, base, { sequence: 1.5 }) },
    { web_delivery: Object.assign({}, base, { sequence: '273' }) },
    { web_delivery: Object.assign({}, base, { generated_at_ms: 0 }) },
    { web_delivery: Object.assign({}, base, { generated_at_ms: Number.MAX_SAFE_INTEGER + 2 }) },
    { web_delivery: Object.assign({}, base, { generated_monotonic_ms: -1 }) },
    { web_delivery: Object.assign({}, base, { generated_monotonic_ms: NaN }) },
  ];
  for (const payload of bad) assert.equal(validateDelivery(payload), null, JSON.stringify(payload));
  assert.ok(validateDelivery({ web_delivery: Object.assign({}, base, { generated_monotonic_ms: 0 }) }));
});

// ---------------------------------------------------------------------------
// Rules 3/4: offset math

test('deliveryAgeForStream applies offset, clamps at zero and adds uncertainty', () => {
  const event = streamEvent({ sequence: 5, generatedMonotonicMs: 1000 });
  const offsets = { offsetMs: 50, uncertaintyMs: 40 };
  assert.equal(deliveryAgeForStream(event, offsets, 1050), 40);
  assert.equal(deliveryAgeForStream(event, offsets, 1350), 340);
  assert.equal(deliveryAgeForStream(event, offsets, 900), 40, 'never negative');
  assert.equal(deliveryAgeForStream(event, null, 1350), null);
  assert.equal(deliveryAgeForStream(event, { offsetMs: NaN, uncertaintyMs: 1 }, 1350), null);
  assert.equal(deliveryAgeForStream({}, offsets, 1350), null);
});

// ---------------------------------------------------------------------------
// Rule 6: would-expire

test('metricWouldExpire only considers available non-stale observations', () => {
  const def = { stale_after_seconds: 5 };
  assert.equal(metricWouldExpire({ available: false, age_ms: 99999 }, def, 0), false);
  assert.equal(metricWouldExpire({ available: true, stale: true, age_ms: 99999 }, def, 0), false);
  assert.equal(metricWouldExpire({ available: true, stale: false, age_ms: 4000 }, def, 999), false);
  assert.equal(metricWouldExpire({ available: true, stale: false, age_ms: 4000 }, def, 1001), true);
  assert.equal(metricWouldExpire({ available: true, stale: false, age_ms: null }, def, 0), true);
  assert.equal(metricWouldExpire({ available: true, stale: false, age_ms: -1 }, def, 0), true);
  assert.equal(metricWouldExpire({ available: true, stale: false, age_ms: 1 }, undefined, 0), true);
  assert.equal(metricWouldExpire({ available: true, stale: false, age_ms: 1 }, { stale_after_seconds: -1 }, 0), true);
  assert.equal(metricWouldExpire({ available: true, stale: false, age_ms: 1 }, { stale_after_seconds: null }, 0), true);
});

test('wouldExpire covers metrics and verified vehicle state', () => {
  const fresh = { 'engine.rpm': { available: true, stale: false, age_ms: 1000 } };
  assert.equal(wouldExpire(fresh, CATALOG_BY_NAME, null, 3999), false);
  assert.equal(wouldExpire(fresh, CATALOG_BY_NAME, null, 4001), true);
  assert.equal(wouldExpire({ 'not.in.catalog': { available: true, stale: false, age_ms: 1 } }, CATALOG_BY_NAME, null, 0), true);
  assert.equal(wouldExpire(SNAPSHOT.metrics, CATALOG_BY_NAME, null, 999999), false, 'fixture metrics are all stale already');
  // verified state expires at 5 s, other confidences never do on their own
  assert.equal(wouldExpire({}, CATALOG_BY_NAME, { confidence: 'verified', age_ms: 4500 }, 499), false);
  assert.equal(wouldExpire({}, CATALOG_BY_NAME, { confidence: 'verified', age_ms: 4500 }, 501), true);
  assert.equal(wouldExpire({}, CATALOG_BY_NAME, { confidence: 'Verified', age_ms: null }, 0), true);
  assert.equal(wouldExpire({}, CATALOG_BY_NAME, { confidence: 'inferred', age_ms: 99999 }, 0), false);
  assert.equal(wouldExpire({}, CATALOG_BY_NAME, { confidence: 'stale', age_ms: null }, 0), false);
  // plain-object catalogs work too
  assert.equal(wouldExpire(fresh, { 'engine.rpm': { stale_after_seconds: 5 } }, null, 4001), true);
});

// ---------------------------------------------------------------------------
// Rules 4/5/19 as a pure decision

test('evaluateStreamEvent returns each rejection reason in audited order', () => {
  const accepted = { instanceId: INSTANCE, sequence: 10, generatedAtMs: 1, generatedMonotonicMs: 5000 };
  const ctx = {
    accepting: true,
    hidden: false,
    accepted,
    offsets: { offsetMs: 100, uncertaintyMs: 25 },
    retired: new Set(['dead']),
    catalogByName: CATALOG_BY_NAME,
    catalogHash: HASH,
    catalogCount: SNAPSHOT.catalog.length,
  };
  const ok = streamEvent({ sequence: 11, generatedMonotonicMs: 6000 });
  const now = 6000 + 100 + 200;
  const verdict = evaluateStreamEvent(ok, ctx, now);
  assert.equal(verdict.accepted, true);
  assert.equal(verdict.deliveryAgeMs, 225);

  assert.equal(evaluateStreamEvent({}, ctx, now).reason, 'missing_delivery_metadata');
  assert.equal(evaluateStreamEvent(streamEvent({ sequence: 11, generatedMonotonicMs: 6000, instanceId: 'dead' }), ctx, now).reason, 'retired_instance');
  assert.equal(evaluateStreamEvent(ok, Object.assign({}, ctx, { accepting: false }), now).reason, 'stream_blocked');
  assert.equal(evaluateStreamEvent(ok, Object.assign({}, ctx, { hidden: true }), now).reason, 'stream_blocked');
  assert.equal(evaluateStreamEvent(streamEvent({ sequence: 11, generatedMonotonicMs: 6000, instanceId: 'other' }), ctx, now).reason, 'instance_changed');
  assert.equal(evaluateStreamEvent(ok, Object.assign({}, ctx, { accepted: null }), now).reason, 'instance_changed');
  assert.equal(evaluateStreamEvent(ok, Object.assign({}, ctx, { offsets: null }), now).reason, 'http_resync_required');
  assert.equal(evaluateStreamEvent(ok, ctx, now + MAX_STREAM_DELIVERY_AGE_MS).reason, 'queued_stream_event');
  const expiring = streamEvent({ sequence: 11, generatedMonotonicMs: 6000, metrics: { 'engine.rpm': { available: true, stale: false, age_ms: 4900 } } });
  assert.equal(evaluateStreamEvent(expiring, ctx, now).reason, 'queued_stream_event');
  const verifiedOld = streamEvent({ sequence: 11, generatedMonotonicMs: 6000, vehicleState: { state: 'running', confidence: 'verified', age_ms: 4900 } });
  assert.equal(evaluateStreamEvent(verifiedOld, ctx, now).reason, 'queued_stream_event');
  assert.equal(evaluateStreamEvent(streamEvent({ sequence: 10, generatedMonotonicMs: 6000 }), ctx, now).reason, 'out_of_order');
  assert.equal(evaluateStreamEvent(streamEvent({ sequence: 11, generatedMonotonicMs: 4999 }), ctx, now).reason, 'out_of_order');
  assert.equal(evaluateStreamEvent(streamEvent({ sequence: 11, generatedMonotonicMs: 6000, catalogHash: 'ffff' }), ctx, now).reason, 'catalog_changed');
  assert.equal(evaluateStreamEvent(streamEvent({ sequence: 11, generatedMonotonicMs: 6000, catalogCount: 3 }), ctx, now).reason, 'catalog_changed');
  // no hash held yet: first event is adopted rather than rejected
  assert.equal(evaluateStreamEvent(ok, Object.assign({}, ctx, { catalogHash: null }), now).accepted, true);
  assert.equal(catalogChanged(streamEvent({ sequence: 1, generatedMonotonicMs: 1, catalogHash: null }), ctx), false);
});

// ---------------------------------------------------------------------------
// Rules 2/3/11/13: HTTP baseline, offsets, stream open, supplemental

test('start fetches the baseline, records offsets, opens the stream and fetches the summary', async () => {
  const h = harness();
  const accepted = await h.boot();
  assert.equal(accepted, true);

  const fetches = h.snapshotFetches();
  assert.equal(fetches.length, 1);
  assert.match(fetches[0].url, /^\/v1\/snapshot\?fresh=\d+-1$/);
  assert.deepEqual(fetches[0].init, { cache: 'no-store' });

  const baseline = h.store.of('applyBaseline');
  assert.equal(baseline.length, 1);
  assert.equal(baseline[0].deliveryAgeMs, 100, 'HTTP delivery age = full round trip');
  assert.equal(baseline[0].nowMono, 100, 'receipt time is passed as the monotonic cursor');
  assert.equal(baseline[0].snapshot.web_delivery.sequence, 273);

  const state = h.link.state();
  assert.deepEqual(state.offsets, { offsetMs: 50 - 1129733584, uncertaintyMs: 50 });
  assert.equal(state.accepted.instanceId, INSTANCE);
  assert.equal(state.catalogSize, 25);
  assert.equal(state.catalogHash, null, 'HTTP snapshot carries no hash; adopted from the stream');

  assert.equal(h.sources.length, 1);
  assert.equal(h.source().url, '/v2/stream');
  assert.equal(h.connection().state, 'live');

  const summaries = h.summaryFetches();
  assert.equal(summaries.length, 1);
  assert.match(summaries[0].url, /^\/v2\/summary\?fresh=\d+-1$/);
  const applied = h.store.of('applySummary');
  assert.equal(applied.length, 1);
  assert.equal(applied[0].bundle.available, true);
  assert.ok(applied[0].bundle.maintenance);
});

test('baseline slower than 2 s is rejected and retried every 2 s while visible', async () => {
  const h = harness();
  h.fetchLatency = MAX_HTTP_ROUND_TRIP_MS + 1;
  const accepted = await h.boot();
  assert.equal(accepted, false);
  assert.equal(h.store.of('applyBaseline').length, 0);
  assert.equal(h.connection().state, 'unavailable');
  assert.equal(h.connection().reason, 'http_response_delayed');
  assert.equal(h.sources.length, 0);
  assert.equal(h.link.state().retryScheduled, true);

  h.fetchLatency = 300;
  await h.advance(RESYNC_RETRY_MS - 1);
  assert.equal(h.snapshotFetches().length, 1);
  await h.advance(1);
  assert.equal(h.snapshotFetches().length, 2);
  assert.equal(h.store.of('applyBaseline').length, 1);
  assert.equal(h.store.of('applyBaseline')[0].deliveryAgeMs, 300);
  assert.equal(h.link.state().offsets.uncertaintyMs, 150);
  assert.equal(h.connection().state, 'live');
  assert.equal(h.sources.length, 1);
});

test('baseline HTTP errors, network failures and missing delivery metadata are reported and retried', async () => {
  const h = harness();
  let mode = 'error';
  h.responder = (url) => {
    if (url.startsWith('/v2/summary')) return { body: clone(SUMMARY) };
    if (mode === 'error') return { error: new TypeError('Failed to fetch') };
    if (mode === 'status') return { ok: false, status: 503, body: { available: false, detail: 'broker down' } };
    if (mode === 'nodelivery') return { body: clone({ catalog: SNAPSHOT.catalog, metrics: SNAPSHOT.metrics, status: SNAPSHOT.status }) };
    return { body: clone(SNAPSHOT) };
  };
  await h.boot();
  assert.equal(h.connection().state, 'unavailable');
  assert.equal(h.connection().reason, 'fetch_failed');
  assert.match(h.connection().detail, /Failed to fetch/);

  mode = 'status';
  await h.advance(RESYNC_RETRY_MS);
  assert.equal(h.connection().reason, 'http_error');
  assert.equal(h.connection().detail, 'broker down');

  mode = 'nodelivery';
  await h.advance(RESYNC_RETRY_MS);
  assert.equal(h.connection().reason, 'missing_delivery_metadata');
  assert.equal(h.store.of('applyBaseline').length, 0);

  mode = 'ok';
  await h.advance(RESYNC_RETRY_MS);
  assert.equal(h.store.of('applyBaseline').length, 1);
  assert.equal(h.connection().state, 'live');
  assert.equal(h.snapshotFetches().length, 4);
});

test('no retry is scheduled while the page is hidden', async () => {
  const h = harness({ visibility: 'hidden' });
  h.fetchLatency = 5000;
  await h.boot();
  assert.equal(h.connection().state, 'unavailable');
  assert.equal(h.link.state().retryScheduled, false);
  await h.advance(10000);
  assert.equal(h.snapshotFetches().length, 1);
});

test('an accepted baseline while hidden opens no stream and fetches no summary', async () => {
  const h = harness({ visibility: 'hidden' });
  const accepted = await h.boot();
  assert.equal(accepted, true);
  assert.equal(h.store.of('applyBaseline').length, 1);
  assert.equal(h.sources.length, 0);
  assert.equal(h.summaryFetches().length, 0);
  assert.notEqual(h.connection().state, 'live');
});

test('an obsolete baseline response is never applied', async () => {
  const h = harness();
  h.responder = (url) => {
    if (url.startsWith('/v1/snapshot')) return { body: clone(SNAPSHOT), pending: true };
    return { body: clone(SUMMARY) };
  };
  h.link.start();
  await h.flush();
  const first = h.snapshotFetches()[0];
  assert.ok(first.pending);
  // Page hidden while the fetch is in flight: generation bumps, response must be dropped.
  h.visibilityState = 'hidden';
  h.dispatch('visibilitychange');
  first.pending.resolve();
  await h.flush();
  assert.equal(h.store.of('applyBaseline').length, 0);
  assert.equal(h.sources.length, 0);
  assert.equal(h.store.of('invalidate')[0].reason, 'client_page_hidden');
});

// ---------------------------------------------------------------------------
// Rule 4/5: stream acceptance and rejection through the live link

test('stream events are accepted with the computed delivery age and keep the link live', async () => {
  const h = harness();
  await h.boot();
  await h.advance(900);
  const event = h.emitEvent({ sequence: 274, ageMs: 40 });
  const applied = h.store.of('applyStream');
  assert.equal(applied.length, 1);
  assert.deepEqual(applied[0].event, event);
  assert.equal(applied[0].deliveryAgeMs, 40 + 50, 'age since generation + uncertainty');
  assert.equal(applied[0].nowMono, h.mono);
  assert.equal(h.link.state().accepted.sequence, 274);
  assert.equal(h.link.state().catalogHash, HASH, 'hash adopted from the first accepted event');
  assert.equal(h.connection().state, 'live');
  assert.equal(h.snapshotFetches().length, 1, 'no resync on a healthy event');
});

test('stream_blocked and out_of_order drop the event without resync', async () => {
  const h = harness();
  await h.boot();
  await h.advance(500);
  h.emitEvent({ sequence: 274 });
  assert.equal(h.store.of('applyStream').length, 1);

  h.emitEvent({ sequence: 274 });
  assert.equal(h.link.state().lastRejection.reason, 'out_of_order');
  h.emitEvent({ sequence: 200 });
  assert.equal(h.link.state().lastRejection.reason, 'out_of_order');
  h.emitEvent({ sequence: 275, mutate: (e) => { e.web_delivery.generated_monotonic_ms -= 5000; } });
  assert.equal(h.link.state().lastRejection.reason, 'out_of_order');

  h.visibilityState = 'hidden'; // no event dispatched: the flag alone blocks acceptance
  h.emitEvent({ sequence: 276 });
  assert.equal(h.link.state().lastRejection.reason, 'stream_blocked');
  h.visibilityState = 'visible';

  assert.equal(h.store.of('applyStream').length, 1);
  assert.equal(h.snapshotFetches().length, 1);
  assert.equal(h.source().closed, false);
});

test('instance_changed on the stream forces an HTTP resync and retires the old instance', async () => {
  const h = harness();
  await h.boot();
  const first = h.source();
  h.responder = (url) => {
    if (url.startsWith('/v1/snapshot')) return { body: snapshotWith({ instance_id: 'b'.repeat(32), sequence: 3, generated_monotonic_ms: 500 }) };
    return { body: clone(SUMMARY) };
  };
  await h.advance(500);
  h.emitEvent({ sequence: 274, instanceId: 'b'.repeat(32) });
  assert.equal(h.store.of('applyStream').length, 0);
  assert.equal(h.link.state().lastRejection.reason, 'instance_changed');
  assert.equal(first.closed, true);
  assert.equal(h.connection().state, 'resyncing');
  assert.equal(h.connection().reason, 'instance_changed');
  await h.flush();
  assert.equal(h.snapshotFetches().length, 2);
  assert.equal(h.store.of('applyBaseline').length, 2);
  assert.deepEqual(h.link.state().retired, [INSTANCE]);
  assert.equal(h.link.state().accepted.instanceId, 'b'.repeat(32));
  assert.equal(h.sources.length, 2);
  assert.equal(h.connection().state, 'live');

  // a straggler from the retired instance is dropped silently
  h.emitEvent({ sequence: 999, instanceId: INSTANCE });
  assert.equal(h.link.state().lastRejection.reason, 'retired_instance');
  assert.equal(h.snapshotFetches().length, 2);
  assert.equal(h.source().closed, false);
});

test('the retired set is bounded to eight instances', async () => {
  const h = harness();
  let n = 0;
  h.responder = (url) => {
    if (url.startsWith('/v1/snapshot')) {
      n += 1;
      return { body: snapshotWith({ instance_id: String(n).padStart(32, '0'), sequence: n, generated_monotonic_ms: 1000 * n }) };
    }
    return { body: clone(SUMMARY) };
  };
  await h.boot();
  for (let i = 0; i < 12; i += 1) {
    await h.link.resync('test');
  }
  const retired = h.link.state().retired;
  assert.equal(retired.length, MAX_RETIRED_INSTANCES);
  assert.equal(retired[0], String(5).padStart(32, '0'), 'oldest entries are evicted first');
});

test('queued_stream_event: delivery age over 10 s or a would-expire metric resyncs', async () => {
  const h = harness();
  await h.boot();
  await h.advance(500);
  h.emitEvent({ sequence: 274, ageMs: MAX_STREAM_DELIVERY_AGE_MS });
  assert.equal(h.link.state().lastRejection.reason, 'queued_stream_event');
  await h.flush();
  assert.equal(h.snapshotFetches().length, 2);
  assert.equal(h.sources.length, 2);

  await h.advance(500);
  h.emitEvent({
    sequence: 280,
    ageMs: 200,
    metrics: { 'engine.rpm': { metric: 'engine.rpm', available: true, stale: false, age_ms: 4800, value: 800 } },
  });
  assert.equal(h.link.state().lastRejection.reason, 'queued_stream_event');
  await h.flush();
  assert.equal(h.snapshotFetches().length, 3);
  assert.equal(h.store.of('applyStream').length, 0);
});

test('queued_stream_event: verified vehicle state that would pass 5 s resyncs', async () => {
  const h = harness();
  await h.boot();
  await h.advance(500);
  h.emitEvent({
    sequence: 274,
    ageMs: 100,
    vehicleState: { state: 'running', running: true, confidence: 'verified', basis: 'qualified_ccan_0x0fc_engine_speed', age_ms: 4900 },
  });
  assert.equal(h.link.state().lastRejection.reason, 'queued_stream_event');
  await h.flush();
  assert.equal(h.snapshotFetches().length, 2);

  await h.advance(500);
  h.emitEvent({
    sequence: 280,
    ageMs: 100,
    vehicleState: { state: 'running', running: true, confidence: 'verified', basis: 'qualified_ccan_0x0fc_engine_speed', age_ms: 4800 },
  });
  assert.equal(h.store.of('applyStream').length, 1, '4800 + 150 ≤ 5000 is accepted');
});

test('catalog hash change on the stream forces an HTTP resync', async () => {
  const h = harness();
  await h.boot();
  await h.advance(500);
  h.emitEvent({ sequence: 274 });
  assert.equal(h.store.of('applyStream').length, 1);
  await h.advance(1000);
  h.emitEvent({ sequence: 275, catalogHash: 'deadbeefdeadbeef' });
  assert.equal(h.link.state().lastRejection.reason, 'catalog_changed');
  await h.flush();
  assert.equal(h.snapshotFetches().length, 2);
  assert.equal(h.link.state().catalogHash, null, 'baseline reset the adopted hash');
  await h.advance(500);
  h.emitEvent({ sequence: 300, catalogHash: 'deadbeefdeadbeef' });
  assert.equal(h.store.of('applyStream').length, 2);
  assert.equal(h.link.state().catalogHash, 'deadbeefdeadbeef');
});

test('http_resync_required is unreachable on a live stream because invalidation closes it first', async () => {
  const h = harness();
  await h.boot();
  h.visibilityState = 'hidden';
  h.dispatch('visibilitychange');
  assert.equal(h.link.state().offsets, null);
  assert.equal(h.source().closed, true);
  // an event from the closed source is ignored by generation, not evaluated
  h.source().emit('snapshot', streamEvent({ sequence: 274, generatedMonotonicMs: 1 }));
  assert.equal(h.link.state().lastRejection, null);
});

// ---------------------------------------------------------------------------
// Rules 10/11: stall, error, forced close

test('stall over 3 s without an accepted event resyncs', async () => {
  const h = harness();
  await h.boot();
  const first = h.source();
  await h.advance(1000);
  h.emitEvent({ sequence: 274 });
  await h.advance(3000);
  assert.equal(h.snapshotFetches().length, 1, 'exactly 3 s is not a stall');
  await h.advance(1000);
  assert.equal(first.closed, true);
  assert.equal(h.snapshotFetches().length, 2);
  assert.equal(h.sources.length, 2);
  assert.equal(h.connection().state, 'live');
});

test('EventSource connection error (forced 300 s close) resyncs', async () => {
  const h = harness();
  await h.boot();
  const first = h.source();
  await h.keepAlive(300000);
  assert.equal(h.sources.length, 1, 'a healthy stream ran 300 s without resync');
  first.emit('error');
  assert.equal(first.closed, true);
  assert.equal(h.connection().reason, 'stream_error');
  await h.flush();
  assert.equal(h.sources.length, 2);
  assert.equal(h.connection().state, 'live');
  // events from the closed source are ignored even if it fires again
  first.emit('error');
  first.emit('snapshot', streamEvent({ sequence: 900, generatedMonotonicMs: 1 }));
  await h.flush();
  assert.equal(h.sources.length, 2);
});

test('server-sent error payload marks the broker unavailable without closing the stream', async () => {
  const h = harness();
  await h.boot();
  const source = h.source();
  source.emit('error', { reason: 'broker_unavailable', detail: 'socket refused' });
  assert.equal(source.closed, false);
  assert.equal(h.sources.length, 1);
  assert.deepEqual(
    [h.connection().state, h.connection().reason, h.connection().detail],
    ['unavailable', 'broker_unavailable', 'socket refused'],
  );
  await h.advance(1000);
  h.emitEvent({ sequence: 274 });
  assert.equal(h.connection().state, 'live');
  source.emit('error', 'not json');
  assert.equal(h.connection().state, 'unavailable');
  assert.equal(h.connection().reason, 'broker_unavailable');
  assert.equal(h.connection().detail, 'not json');
});

test('invalid JSON on the stream is dropped without resync', async () => {
  const h = harness();
  await h.boot();
  h.source().emit('snapshot', '{not json');
  assert.equal(h.link.state().lastRejection.reason, 'invalid_json');
  await h.flush();
  assert.equal(h.snapshotFetches().length, 1);
});

// ---------------------------------------------------------------------------
// Rule 12: visibility and pageshow

test('hide invalidates and stops; show invalidates and resyncs; pageshow restores', async () => {
  const h = harness();
  await h.boot();
  const first = h.source();

  h.visibilityState = 'hidden';
  h.dispatch('visibilitychange');
  assert.equal(first.closed, true);
  assert.deepEqual(h.store.of('invalidate').map((c) => c.reason), ['client_page_hidden']);
  assert.equal(h.connection().state, 'connecting');
  assert.equal(h.connection().reason, 'client_page_hidden');
  await h.advance(5000);
  assert.equal(h.snapshotFetches().length, 1, 'nothing fetched while hidden');

  h.visibilityState = 'visible';
  h.dispatch('visibilitychange');
  assert.deepEqual(h.store.of('invalidate').map((c) => c.reason), ['client_page_hidden', 'client_page_visible']);
  assert.equal(h.connection().reason, 'visibility');
  await h.flush();
  assert.equal(h.snapshotFetches().length, 2);
  assert.equal(h.sources.length, 2);
  assert.equal(h.connection().state, 'live');
  assert.equal(h.store.of('applyBaseline').length, 2);

  const second = h.source();
  h.dispatch('pageshow');
  assert.equal(second.closed, true);
  assert.equal(h.store.of('invalidate')[2].reason, 'client_page_restored');
  await h.flush();
  assert.equal(h.snapshotFetches().length, 3);
  assert.equal(h.sources.length, 3);
  assert.equal(h.connection().state, 'live');
});

// ---------------------------------------------------------------------------
// Rule 13: supplemental throttling

test('summary is throttled to one start per 60 s and refreshed by the watchdog while visible', async () => {
  const h = harness();
  await h.boot();
  assert.equal(h.summaryFetches().length, 1);
  const firstStart = h.summaryFetches()[0].startedMono;

  await h.keepAlive(10000);
  h.source().emit('error');
  await h.flush();
  assert.equal(h.snapshotFetches().length, 2);
  assert.equal(h.summaryFetches().length, 1, 'resync within 60 s does not restart the summary');

  await h.keepAlive(SUPPLEMENTAL_REFRESH_MS - 10000 - 2000);
  assert.equal(h.summaryFetches().length, 1);
  await h.keepAlive(3000);
  assert.equal(h.summaryFetches().length, 2);
  assert.ok(h.summaryFetches()[1].startedMono - firstStart >= SUPPLEMENTAL_REFRESH_MS);
  assert.equal(h.store.of('applySummary').length, 2);

  h.visibilityState = 'hidden';
  h.dispatch('visibilitychange');
  await h.advance(SUPPLEMENTAL_REFRESH_MS * 2);
  assert.equal(h.summaryFetches().length, 2, 'no refresh while hidden');

  h.visibilityState = 'visible';
  h.dispatch('visibilitychange');
  await h.flush();
  assert.equal(h.summaryFetches().length, 3, 'visibility invalidation allows an immediate refresh');
});

test('fetchSummary({force: true}) bypasses the 60 s throttle for a manual refresh', async () => {
  const h = harness();
  await h.boot();
  assert.equal(h.summaryFetches().length, 1);
  await h.keepAlive(5000);
  await h.link.fetchSummary();
  await h.flush();
  assert.equal(h.summaryFetches().length, 1, 'plain call inside 60 s is throttled');
  await h.link.fetchSummary({ force: true });
  await h.flush();
  assert.equal(h.summaryFetches().length, 2, 'forced call starts immediately');
});

test('summary failures are delivered as cache_unavailable and do not block later fetches', async () => {
  const h = harness();
  let fail = true;
  h.responder = (url) => {
    if (url.startsWith('/v1/snapshot')) return { body: clone(SNAPSHOT) };
    if (fail) return { ok: false, status: 503, body: { available: false, reason: 'broker_unavailable', detail: 'down' } };
    return { body: clone(SUMMARY) };
  };
  await h.boot();
  let applied = h.store.of('applySummary');
  assert.equal(applied.length, 1);
  assert.equal(applied[0].bundle.available, false);
  assert.equal(applied[0].bundle.reason, 'broker_unavailable');
  assert.equal(applied[0].bundle.status_code, 503);

  fail = false;
  await h.keepAlive(SUPPLEMENTAL_REFRESH_MS + 1000);
  applied = h.store.of('applySummary');
  assert.equal(applied.length, 2);
  assert.equal(applied[1].bundle.available, true);

  const snapshotResponder = h.responder;
  h.responder = (url) => (url.startsWith('/v1/snapshot') ? snapshotResponder(url) : { error: new Error('offline') });
  await h.keepAlive(SUPPLEMENTAL_REFRESH_MS);
  applied = h.store.of('applySummary');
  assert.equal(applied.length, 3);
  assert.equal(applied[2].bundle.reason, 'cache_unavailable');
  assert.equal(applied[2].bundle.detail, 'offline');
});

// ---------------------------------------------------------------------------
// Rule 10: watchdog tick discipline

test('tick runs once per second only when no delivery was accepted in that second', async () => {
  const h = harness();
  await h.boot();
  assert.equal(h.store.of('tick').length, 0);

  // healthy stream: an event every second at t = 1500, 2500, ... keeps the watchdog quiet
  await h.advance(400);
  for (let i = 0; i < 6; i += 1) {
    await h.advance(1000);
    h.emitEvent({ ageMs: 20 });
  }
  assert.equal(h.store.of('tick').length, 0, 'no watchdog work while events land every second');
  assert.equal(h.store.of('applyStream').length, 6);

  // the stream goes quiet after t = 6500: ticks at 8000, 9000, 10000; stall (> 3 s) at 10000
  const before = h.snapshotFetches().length;
  await h.advance(500);
  assert.equal(h.store.of('tick').length, 0, '7000: last accepted 500 ms ago');
  await h.advance(1000);
  assert.equal(h.store.of('tick').length, 1);
  await h.advance(1000);
  assert.equal(h.store.of('tick').length, 2);
  assert.equal(h.snapshotFetches().length, before, '2.5 s quiet is not a stall');
  await h.advance(1000);
  assert.equal(h.store.of('tick').length, 3);
  assert.equal(h.snapshotFetches().length, before + 1, '3.5 s quiet is a stall resync');
  const ticks = h.store.of('tick').map((c) => c.nowMono);
  for (let i = 1; i < ticks.length; i += 1) assert.equal(ticks[i] - ticks[i - 1], 1000);
});

test('ticks continue before any baseline is accepted and while hidden', async () => {
  const h = harness();
  h.responder = () => ({ error: new Error('offline') });
  await h.boot();
  await h.advance(3000);
  assert.equal(h.store.of('tick').length, 3);
  h.visibilityState = 'hidden';
  h.dispatch('visibilitychange');
  await h.advance(2000);
  assert.equal(h.store.of('tick').length, 5);
});

// ---------------------------------------------------------------------------
// stop / state

test('stop closes the stream, cancels timers and detaches listeners', async () => {
  const h = harness();
  await h.boot();
  const source = h.source();
  h.link.stop();
  assert.equal(source.closed, true);
  assert.equal(h.timers.length, 0);
  assert.equal((h.listeners.get('visibilitychange') || []).length, 0);
  assert.equal((h.listeners.get('pageshow') || []).length, 0);
  await h.advance(10000);
  assert.equal(h.store.of('tick').length, 0);
  assert.equal(h.snapshotFetches().length, 1);
  assert.equal(h.link.state().started, false);
});

test('connection signal is only republished when something changed', async () => {
  const h = harness();
  await h.boot();
  const before = h.store.connectionHistory.length;
  for (let seq = 274; seq < 280; seq += 1) {
    await h.advance(1000);
    h.emitEvent({ sequence: seq });
  }
  assert.equal(h.store.connectionHistory.length, before, 'six accepted events, zero connection publishes');
  const states = h.store.connectionHistory.map((c) => c.state);
  assert.deepEqual(states, ['connecting', 'live']);
});
