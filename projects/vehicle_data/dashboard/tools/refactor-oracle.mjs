// Offline differential oracle. Run before/after a refactor with two checkout roots:
// node tools/refactor-oracle.mjs OLD_CHECKOUT NEW_CHECKOUT [TRACE_JSON]
// No test files are changed, no real fetch/EventSource/clock is used.
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [oldRoot, newRoot, out] = process.argv.slice(2);
if (!oldRoot || !newRoot) throw new Error('usage: refactor-oracle.mjs OLD_CHECKOUT NEW_CHECKOUT [TRACE_JSON]');
const dashboard = 'projects/vehicle_data/dashboard';
const load = (root, file) => import(pathToFileURL(resolve(root, dashboard, 'src', file)));
const fixtures = Object.fromEntries(readdirSync(resolve(oldRoot, dashboard, 'test/fixtures'))
  .filter((file) => file.endsWith('.json'))
  .map((file) => [file, JSON.parse(readFileSync(resolve(oldRoot, dashboard, 'test/fixtures', file), 'utf8'))]));
const snapshot = fixtures['snapshot.json'];
const summary = fixtures['summary-v2.json'];
const now = Date.parse('2026-09-23T12:00:00Z');
const ctx = { nowMs: now, extraMs: 1000, generatedAt: new Date(now).toISOString(), tripOpen: true };
// Freeze default wall-clock formatting as well as explicit context clocks.
const RealDate = Date;
globalThis.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return now; }
};
const values = [undefined, null, false, true, '', 'unknown', ' engine_not_running ', -1, -0,
  0, 0.5, 1, 999, 59999, 60000, 3600000, 86400000, NaN, Infinity, [], {},
  { available: false }, { available: true, stale: false, age_ms: 0, value: 12.6 },
  { available: true, stale: true, age_ms: 5001 }, { state: 'running', confidence: 'verified', age_ms: 5000 }];
// Include every fixture and each nested record/array (not just happy-path top-level shapes).
function walk(v) {
  if (v && typeof v === 'object') {
    values.push(v);
    for (const child of Object.values(v)) walk(child);
  }
}
Object.values(fixtures).forEach(walk);
const metricNames = [...new Set(snapshot.catalog.map((d) => d.name).concat([
  'engine.oil_life_remaining', 'vehicle.speed_limit', 'acc.set_speed', 'unknown.metric', '', 'toString',
]))];
const successful = {};
let comparisons = 0;
function result(fn, args) {
  try { return { value: fn(...structuredClone(args)) }; }
  catch (error) { return { error: [error.name, error.message] }; }
}
function compare(a, b, key, args) {
  const before = result(a, args);
  const after = result(b, args);
  assert.deepStrictEqual(after, before, key + ' args=' + JSON.stringify(args));
  comparisons += 1;
  if (!before.error) successful[key] = (successful[key] || 0) + 1;
}
const modules = ['views/historySystem.helpers.js', 'format.js', 'link.js'];
for (const file of modules) {
  const [before, after] = await Promise.all([load(oldRoot, file), load(newRoot, file)]);
  assert.deepStrictEqual(Object.keys(after), Object.keys(before), file + ' exports');
  for (const [name, fn] of Object.entries(before)) {
    const key = file + ':' + name;
    if (typeof fn !== 'function') {
      assert.deepStrictEqual(after[name], fn, key);
      continue;
    }
    if (name === 'createLink') continue; // full transport trace below
    if (name === 'createStableGate') {
      const a = fn(), b = after[name]();
      let lastA, lastB;
      for (const v of values) {
        for (let i = 0; i < 2; i += 1) {
          const x = a(structuredClone(v)), y = b(structuredClone(v));
          assert.deepStrictEqual(y, x, key);
          assert.equal(y === lastB, x === lastA, key + ' identity');
          lastA = x; lastB = y;
          comparisons += 1;
        }
      }
      successful[key] = values.length * 2;
      continue;
    }
    for (const value of values) compare(fn, after[name], key, [value, ctx, ctx, null, now]);
    // Signature-aware inputs ensure the generic malformed cases cannot hide missing valid coverage.
    const cases = {
      groupThousands: [['1234567'], ['0']], capFirst: [['hello']],
      fmtClock: [[new Date(now)]], fmtDate: [[new Date(now), 2026]],
      fmtTime: [[new Date(now), new Date(now)], ['2025-09-22T11:00:00Z', new Date(now)]],
      catalogModel: [[snapshot.catalog, new Set(metricNames)]],
      busesModel: [[snapshot.status, summary.status_full]],
      brokerModel: [[summary.status_full, now]], appRows: [[snapshot.web, { build: 'oracle', builtAt: new Date(now).toISOString() }, now]],
      customiseStatus: [['drive', [{ id: 'a' }, { id: 'b' }], { hidden: { drive: ['b'] }, order: { drive: ['b', 'a'] } }, 'tiles']],
      radarModel: [[{ ...ctx, axes: { azimuth: { fresh: true, value: 0.8 }, elevation: { held: { value: -1.01, observed_at: new Date(now).toISOString() } } } }]],
      sparkGeometry: [[[{ at: new Date(now).toISOString(), value: 1 }, { at: new Date(now - 900000).toISOString(), value: 2 }], { minSpan: 20 }]],
      coverageLine: [[summary.history, ctx]], tripsModel: [[summary.history, ctx]],
      vehicleLine: [[snapshot.status.vehicle_state, ctx]],
      deliveryAgeForStream: [[snapshot, { offsetMs: 50, uncertaintyMs: 10 }, now]],
      wouldExpire: [[snapshot.metrics, new Map(snapshot.catalog.map((d) => [d.name, d])), snapshot.status.vehicle_state, 5000]],
      evaluateStreamEvent: [[snapshot, { accepting: true, hidden: false, retired: new Set(), accepted: null }, 0]],
    };
    for (const args of cases[name] || []) compare(fn, after[name], key, args);
    for (const metric of metricNames) {
      for (const v of [undefined, null, true, -0, -12.5, 53892.8, NaN, Infinity, 'degF', 'boolean', 'raw_u8', 'degrees', '%']) {
        if (['fmtValue', 'fmtUnit', 'decimalsFor', 'trendTitle', 'trendId', 'catalogLabel'].includes(name)) compare(fn, after[name], key, [metric, v]);
        if (name === 'trendModel') compare(fn, after[name], key, [metric, summary.history?.metric_trends?.[metric], ctx]);
        if (name === 'catalogValue') compare(fn, after[name], key, [metric, { available: true, value: v }, { kind: 'held', retained: { value: v, observed_at: new Date(now).toISOString() } }, 'deg', now]);
      }
    }
    assert.ok(successful[key] > 0, 'no non-throwing coverage for ' + key);
  }
}

// Deliberately independent fake transport: log every call, timer, source event, connection
// publication, and public state at callbacks and settled checkpoints. Retain payloads in full.
async function trace(createLink) {
  const log = [];
  let mono = 0, seq = snapshot.web_delivery.sequence, timerId = 0, hidden = false, link;
  let baselineMode = 'ok', summaryMode = 'ok', instance = snapshot.web_delivery.instance_id;
  const timers = new Map(), listeners = new Map(), sources = [], pending = [];
  const record = (kind, ...args) => log.push(structuredClone({ kind, mono, args, state: link?.state() }));
  const store = Object.fromEntries(['applyBaseline', 'applyStream', 'applySummary', 'invalidate', 'tick']
    .map((name) => [name, (...args) => record(name, ...args)]));
  let connection = {};
  store.connection = {
    peek: () => connection,
    get value() { return connection; },
    set value(v) { connection = v; record('connection', v); },
  };
  const baseline = () => ({ ...structuredClone(snapshot), web_delivery: {
    instance_id: instance, sequence: ++seq, generated_at_ms: now + mono, generated_monotonic_ms: 100000 + mono,
  } });
  class Source {
    constructor(url) { this.id = sources.length; this.listeners = new Map(); sources.push(this); record('open', url, this.id); }
    addEventListener(type, fn) { this.listeners.set(type, fn); record('listen', this.id, type); }
    close() { record('close', this.id); }
    emit(type, data) { record('emit', this.id, type, data); this.listeners.get(type)?.(data === undefined ? {} : { data: typeof data === 'string' ? data : JSON.stringify(data) }); }
  }
  const fetch = (url, init) => {
    record('fetch', url, init);
    const isBaseline = url.startsWith('/v1/snapshot');
    const mode = isBaseline ? baselineMode : summaryMode;
    const body = isBaseline ? baseline() : structuredClone(fixtures['summary-v2.link.json']);
    if (mode === 'network') return Promise.reject(new Error('offline'));
    const response = { ok: mode !== 'http', status: mode === 'http' ? 503 : 200, json: async () => {
      mono += mode === 'slow' ? 2001 : 50;
      record('json', url);
      return mode === 'missing' ? {} : mode === 'http' ? { reason: 'unavailable', detail: 'offline' } : body;
    } };
    if (mode === 'pending') return new Promise((resolve) => pending.push(() => resolve(response)));
    return Promise.resolve(response);
  };
  link = createLink({ fetch, EventSource: Source, store, now: () => mono, wallClock: () => now + mono,
    visibility: () => hidden ? 'hidden' : 'visible',
    setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, at: mono + ms }); record('timer', id, ms); return id; },
    clearTimeout: (id) => { timers.delete(id); record('clear', id); },
    addEventListener: (type, fn) => { listeners.set(type, fn); record('addListener', type); },
    removeEventListener: (type) => { listeners.delete(type); record('removeListener', type); },
  });
  const flush = async () => { for (let n = 0; n < 12; n += 1) await Promise.resolve(); record('settled'); };
  const advance = async (ms) => {
    const target = mono + ms;
    while (true) {
      const due = [...timers].filter(([, v]) => v.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]); mono = Math.max(mono, due[1].at); record('fire', due[0]); due[1].fn(); await flush();
    }
    mono = Math.max(mono, target); await flush();
  };
  const source = () => sources[sources.length - 1];
  const emit = (extra = {}) => source().emit('snapshot', { ...baseline(), catalog_hash: 'hash', catalog_count: snapshot.catalog.length, ...extra });
  record('initial');
  await link.start(); await flush(); record('startAgain', await link.start());
  emit(); emit({ web_delivery: { ...baseline().web_delivery, sequence: 1 } });
  source().emit('snapshot', '{invalid'); source().emit('error', { reason: 'broker_unavailable', detail: 'offline' });
  source().emit('error', 'not json'); emit(); await flush();
  const oldSource = source(); source().emit('error'); await flush();
  oldSource.emit('snapshot', baseline()); oldSource.emit('error'); await flush();
  emit({ catalog_hash: 'changed' }); await flush();
  emit({ metrics: { 'engine.rpm': { available: true, stale: false, age_ms: 5001 } } }); await flush();
  emit({ status: { vehicle_state: { confidence: 'verified', age_ms: 5001 } } }); await flush();
  for (const mode of ['network', 'http', 'missing', 'slow', 'ok']) {
    baselineMode = mode; await link.resync('manual'); await flush(); await advance(2000);
  }
  await advance(4500); // stalled source, automatic baseline/reconnect
  for (let i = 0; i < 65; i += 1) { emit(); await advance(1000); } // summary timer with healthy stream
  record('throttled', await link.fetchSummary());
  for (const mode of ['http', 'network', 'ok']) {
    summaryMode = mode; record('forcedSummary', await link.fetchSummary({ force: true })); await flush();
  }
  for (let i = 0; i < 10; i += 1) { instance = 'instance-' + i; await link.resync('restart'); await flush(); }
  emit({ web_delivery: { ...baseline().web_delivery, instance_id: 'instance-8' } }); await flush();
  hidden = true; listeners.get('visibilitychange')(); await advance(5000);
  oldSource.emit('snapshot', baseline()); hidden = false; listeners.get('visibilitychange')(); await flush();
  listeners.get('pageshow')(); await flush();
  baselineMode = 'pending'; const obsolete = link.resync('pending');
  hidden = true; listeners.get('visibilitychange')(); pending.shift()(); await obsolete; await flush();
  hidden = false; baselineMode = 'ok'; listeners.get('visibilitychange')(); await flush();
  summaryMode = 'pending'; const a = link.fetchSummary({ force: true }), b = link.fetchSummary({ force: true });
  record('samePromise', a === b); link.stop(); pending.shift()(); record('obsoleteSummary', await a); await flush();
  summaryMode = 'ok'; await link.start(); await flush(); link.stop(); await advance(10000);
  return log;
}
const oldTrace = await trace((await load(oldRoot, 'link.js')).createLink);
const newTrace = await trace((await load(newRoot, 'link.js')).createLink);
assert.deepStrictEqual(newTrace, oldTrace, 'complete transport callback/state trace');
if (out) writeFileSync(out, JSON.stringify({ comparisons, successful, oldTrace, newTrace }, null, 2) + '\n');
console.log(`Oracle PASS: ${Object.keys(successful).length} functions, ${comparisons} output/identity comparisons, ${Object.keys(fixtures).length} fixtures, ${oldTrace.length} transport trace entries per checkout.`);
