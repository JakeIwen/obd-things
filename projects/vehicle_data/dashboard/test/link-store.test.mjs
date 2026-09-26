// Reviewer integration check: link.js driving the real store.js (not a fake
// sink), so argument orders, the connection signal and the clock stay in step.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { effect } from '@preact/signals-core';

import * as store from '../src/store.js';
import { createLink } from '../src/link.js';

const SNAPSHOT = JSON.parse(readFileSync(new URL('./fixtures/snapshot.json', import.meta.url), 'utf8'));
const SUMMARY = JSON.parse(readFileSync(new URL('./fixtures/summary-v2.link.json', import.meta.url), 'utf8'));
const clone = (value) => JSON.parse(JSON.stringify(value));
const settle = async () => {
  for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

function harness() {
  const h = { mono: 10000, server: 500000, seq: 1, visibility: 'visible', sources: [], timers: [], listeners: {} };
  h.snapshot = () => {
    const snap = clone(SNAPSHOT);
    Object.assign(snap.metrics['engine.rpm'], { age_ms: 300, stale: false, value: 2100, observed_at: '2026-09-22T18:59:59.700000+00:00' });
    snap.web_delivery = { instance_id: 'a'.repeat(32), sequence: h.seq++, generated_at_ms: 1790103502381, generated_monotonic_ms: h.server };
    return snap;
  };
  h.event = (rpm) => {
    h.mono += 1000;
    h.server += 1000;
    const snap = h.snapshot();
    snap.metrics['engine.rpm'].value = rpm;
    snap.metrics['engine.rpm'].observed_at = `2026-09-22T19:00:0${rpm % 10}.000000+00:00`;
    return { status: { vehicle_state: snap.status.vehicle_state }, metrics: snap.metrics, catalog_hash: 'hash-1', catalog_count: snap.catalog.length, web: snap.web, web_delivery: snap.web_delivery, status_code: 200 };
  };
  h.link = createLink({
    store,
    now: () => h.mono,
    wallClock: () => 1790103502381,
    visibility: () => h.visibility,
    setTimeout: (fn, ms) => h.timers.push({ fn, ms }),
    clearTimeout: () => {},
    addEventListener: (type, fn) => {
      h.listeners[type] = fn;
    },
    fetch: async (url) => {
      const body = url.startsWith('/v1/snapshot') ? h.snapshot() : SUMMARY;
      return { ok: true, status: 200, json: async () => body };
    },
    EventSource: class {
      constructor(url) {
        this.url = url;
        this.handlers = {};
        h.sources.push(this);
      }
      addEventListener(type, fn) {
        this.handlers[type] = fn;
      }
      close() {
        this.closed = true;
      }
    },
  });
  return h;
}

test('link + real store: baseline, summary, accepted stream event, clock, invalidation', async () => {
  const h = harness();
  assert.equal(await h.link.start(), true);
  await settle();
  assert.equal(store.catalog.value.length, 25);
  assert.equal(store.connection.value.state, 'live');
  assert.equal(h.sources.length, 1);
  assert.equal(h.sources[0].url, '/v2/stream');
  assert.ok(store.summary.history.value, 'summary slice applied');
  assert.equal(store.metricSignal('engine.rpm').value.stale, false);
  assert.equal(store.clock.value, 10000);

  h.sources[0].handlers.snapshot({ data: JSON.stringify(h.event(2345)) });
  assert.equal(h.link.state().lastRejection, null);
  assert.equal(store.metricSignal('engine.rpm').value.value, 2345);
  assert.equal(store.clock.value, 11000, 'accepted events advance the clock');
  assert.equal(store.connection.value.state, 'live');

  h.visibility = 'hidden';
  h.listeners.visibilitychange();
  assert.equal(store.metricSignal('engine.rpm').value.stale, true);
  assert.equal(store.vehicle.value.basis, 'client_page_hidden');
  assert.equal(store.connection.value.state, 'connecting');
  h.link.stop();
});

test('link.resync from inside a signals effect does not subscribe the effect to connection', async () => {
  const h = harness();
  await h.link.start();
  await settle();
  let runs = 0;
  const dispose = effect(() => {
    runs += 1;
    if (runs > 3) return; // fail instead of looping forever on a regression
    h.link.resync('manual');
  });
  await settle();
  assert.equal(runs, 1);
  assert.equal(store.connection.value.state, 'live');
  dispose();
  h.link.stop();
});
