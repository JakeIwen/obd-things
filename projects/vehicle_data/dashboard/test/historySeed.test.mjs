import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { signal } from "@preact/signals";

import { createRing } from "../src/ring.js";
import { splitByThreshold, redAboveFor } from "../src/sparkline.helpers.js";
import { SPARK_METRICS } from "../src/app/rings.js";
import { loadHistorySeed, startHistorySeed } from "../src/app/historySeed.js";
import { BUCKET_MS, JOIN_STEP_MS, RECENT_METRICS, WINDOW_MS, joinBuckets, seedPlan } from "../src/historySeed.helpers.js";
import { buildDemoViews } from "../tools/demo_views.mjs";

const PI_NOW = Date.UTC(2026, 10, 17, 19, 0, 0);
const iso = (ms) => new Date(ms).toISOString();
/** A bucket that started `minutesAgo` minutes before PI_NOW. */
const bucket = (minutesAgo, value) => ({ at: iso(PI_NOW - minutesAgo * 60000), value });

test("the ring list is the route's contract list, so the two cannot drift", () => {
  assert.deepEqual([...SPARK_METRICS], [...RECENT_METRICS]);
  assert.ok(SPARK_METRICS.includes("generator.field_duty"));
});

test("adjacent minute buckets are joined by linear interpolation at the join step", () => {
  const points = joinBuckets([bucket(3, 10), bucket(2, 20)], PI_NOW);
  assert.equal(points[0].value, 10);
  assert.equal(points[points.length - 1].value, 20);
  assert.equal(points.length, BUCKET_MS / JOIN_STEP_MS + 1);
  const mid = points[(points.length - 1) / 2];
  assert.equal(mid.t, points[0].t + BUCKET_MS / 2);
  assert.equal(mid.value, 15);
  for (let i = 1; i < points.length; i += 1) assert.equal(points[i].t - points[i - 1].t, JOIN_STEP_MS);
});

test("a missing minute is a gap: buckets two minutes apart are not joined", () => {
  const points = joinBuckets([bucket(5, 10), bucket(3, 20)], PI_NOW);
  assert.deepEqual(points.map((p) => p.value), [10, 20]);
  assert.equal(points[1].t - points[0].t, 2 * BUCKET_MS);
});

test("one bucket is one point, no input is none, and junk is skipped", () => {
  assert.equal(joinBuckets([bucket(4, 7)], PI_NOW).length, 1);
  assert.deepEqual(joinBuckets([], PI_NOW), []);
  assert.deepEqual(joinBuckets(null, PI_NOW), []);
  const junk = [{ at: "garbage", value: 1 }, { at: iso(PI_NOW - 120000), value: NaN }, null, { at: iso(PI_NOW - 120000), value: "5" }];
  assert.deepEqual(joinBuckets(junk, PI_NOW), []);
});

test("buckets older than the 15-minute window are dropped", () => {
  const points = joinBuckets([bucket(30, 1), bucket(16, 2), bucket(10, 3)], PI_NOW);
  assert.deepEqual(points.map((p) => p.value), [3]);
  assert.ok(points.every((p) => p.t >= PI_NOW - WINDOW_MS));
});

test("a bucket in the Pi's future is clamped to now, never placed after it", () => {
  const points = joinBuckets([bucket(0.5, 4), bucket(-5, 9)], PI_NOW);
  assert.ok(points.every((p) => p.t <= PI_NOW));
});

function reply(overrides = {}) {
  return {
    available: true,
    generated_at: iso(PI_NOW),
    window_seconds: 900,
    bucket_seconds: 60,
    at_basis: "bucket_start",
    metrics: Object.fromEntries(RECENT_METRICS.map((name) => [name, []])),
    ...overrides,
  };
}

test("the plan maps Pi ages onto the monotonic axis whatever the tablet's wall clock says", () => {
  const r = reply();
  r.metrics["engine.rpm"] = [bucket(5, 1500), bucket(4, 1600)];
  // 40 ms round trip: the Pi's time at receipt is generated_at + 20 ms. The tablet's own clock is
  // never read by the plan, so an hour of skew changes nothing.
  const plan = seedPlan(r, { sentMono: 1000, receivedMono: 1040 });
  assert.equal(plan.mapping.nowMono, 1040);
  assert.equal(plan.mapping.nowEpochMs, PI_NOW + 20);
  const ring = createRing({ windowMs: WINDOW_MS });
  assert.ok(ring.seed(plan.metrics["engine.rpm"], plan.mapping) > 0);
  // The first bucket started 5 minutes before generated_at; its middle is 4.5 minutes before it, so
  // it lands 4.5 minutes and the 20 ms of reply latency before the receipt time.
  const first = plan.metrics["engine.rpm"][0];
  assert.equal(plan.mapping.nowMono - (plan.mapping.nowEpochMs - first.t), 1040 - (270000 + 20));
  assert.ok(first.t <= plan.mapping.nowEpochMs, "no seed point is in the future");
});

test("a reply that is not the current contract seeds nothing", () => {
  const receipt = { sentMono: 0, receivedMono: 10 };
  assert.equal(seedPlan(null, receipt), null);
  assert.equal(seedPlan({ available: false, reason: "history_recent_cache_starting" }, receipt), null);
  assert.equal(seedPlan(reply({ bucket_seconds: 300 }), receipt), null);
  assert.equal(seedPlan(reply({ window_seconds: 3600 }), receipt), null);
  assert.equal(seedPlan(reply({ metrics: null }), receipt), null);
  assert.equal(seedPlan(reply({ at_basis: "bucket_end" }), receipt), null);
  const noBasis = reply();
  delete noBasis.at_basis;
  assert.equal(seedPlan(noBasis, receipt), null);
  assert.equal(seedPlan(reply({ generated_at: "soon" }), receipt), null);
});

test("a van parked for an hour gives a plan with no metrics", () => {
  assert.deepEqual(seedPlan(reply(), { sentMono: 0, receivedMono: 5 }).metrics, {});
});

test("seeded alternator points obey the red-above-80 rule, with the colour change at the threshold", () => {
  const r = reply();
  r.metrics["generator.field_duty"] = [bucket(5, 60), bucket(4, 100), bucket(3, 100), bucket(2, 60)];
  const plan = seedPlan(r, { sentMono: 0, receivedMono: 10 });
  const ring = createRing({ windowMs: WINDOW_MS });
  ring.seed(plan.metrics["generator.field_duty"], plan.mapping);
  const cols = ring.columns(60, 10);
  const run = [];
  cols.forEach((c, i) => c && run.push({ x: i, v: c.mean }));
  const parts = splitByThreshold(run, redAboveFor("generator.field_duty"));
  assert.ok(parts.some((p) => p.red) && parts.some((p) => !p.red));
  for (const part of parts) {
    for (const p of part.points) assert.ok(part.red ? p.v >= 80 : p.v <= 80, `${p.v} in a ${part.red ? "red" : "normal"} part`);
  }
});

test("seeding after live points neither duplicates nor reorders them, and re-seeding is safe", () => {
  const ring = createRing({ windowMs: WINDOW_MS });
  const nowMono = 2000000;
  const mapping = { nowMono, nowEpochMs: PI_NOW };
  ring.push(nowMono - 30000, 70, "live-1");
  ring.push(nowMono - 20000, 71, "live-2");
  const points = joinBuckets([bucket(3, 50), bucket(2, 60), bucket(1, 65), bucket(0.5, 69)], PI_NOW);
  const first = ring.seed(points, mapping);
  assert.ok(first > 0);
  const sizeAfter = ring.size();
  assert.equal(ring.seed(points, mapping), first, "a second seed replaces the first");
  assert.equal(ring.size(), sizeAfter);
  // Every seeded point precedes the oldest live point, and the live points are intact.
  const cols = ring.columns(900, nowMono);
  const present = cols.map((c, i) => (c ? { i, mean: c.mean } : null)).filter(Boolean);
  assert.deepEqual(present.map((p) => p.i), [...present.map((p) => p.i)].sort((a, b) => a - b));
  assert.equal(ring.latest().value, 71);
  assert.equal(ring.latest().observedAt, "live-2");
  assert.ok(present.some((p) => p.mean === 70) && present.some((p) => p.mean === 71));
});

const okResponse = (body) => ({ ok: true, status: 200, json: async () => body });

test("a good reply seeds every metric it has points for, once", async () => {
  const r = reply();
  r.metrics["vehicle.speed"] = [bucket(3, 60), bucket(2, 62)];
  const seeded = [];
  const outcome = await loadHistorySeed({
    fetchImpl: async (path) => { assert.equal(path, "/v1/history/recent"); return okResponse(r); },
    now: (() => { let t = 0; return () => (t += 10); })(),
    seed: (name, points) => seeded.push([name, points.length]),
  });
  assert.equal(outcome, "seeded");
  assert.deepEqual(seeded.map((s) => s[0]), ["vehicle.speed"]);
});

test("an empty reply never calls seed (it would erase an earlier seed)", async () => {
  let calls = 0;
  const outcome = await loadHistorySeed({ fetchImpl: async () => okResponse(reply()), now: () => 0, seed: () => { calls += 1; } });
  assert.equal(outcome, "empty");
  assert.equal(calls, 0);
});

test("a 404 from an older broker, a bad body and a refusal are all silent no-seed outcomes", async () => {
  const run = (response) => loadHistorySeed({ fetchImpl: async () => response, now: () => 0, seed: () => assert.fail("seeded") });
  assert.equal(await run({ ok: false, status: 404, json: async () => ({ reason: "not_found" }) }), "unavailable");
  assert.equal(await run({ ok: true, status: 200, json: async () => { throw new SyntaxError("x"); } }), "unavailable");
  assert.equal(await run(okResponse({ available: true })), "unavailable");
  assert.equal(await run({ ok: false, status: 403, json: async () => ({}) }), "unavailable");
  assert.equal(await run({ ok: false, status: 503, json: async () => ({}) }), "retry");
  assert.equal(await loadHistorySeed({ fetchImpl: async () => { throw new TypeError("network"); }, now: () => 0, seed: () => {} }), "retry");
});

test("a body that stalls after the headers is aborted at the timeout and retried, not left pending", async () => {
  const stalled = (signal) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
  const outcome = await loadHistorySeed({
    timeoutMs: 20,
    now: () => 0,
    seed: () => assert.fail("seeded"),
    fetchImpl: async (_path, { signal }) => ({ ok: true, status: 200, json: () => stalled(signal) }),
  });
  assert.equal(outcome, "retry");
});

test("the page fetches once when the link first goes live and not again, and cancel stops it", async () => {
  const connection = signal({ state: "connecting" });
  const store = { connection };
  let fetches = 0;
  const r = reply();
  r.metrics["engine.rpm"] = [bucket(3, 1000), bucket(2, 1100)];
  const seeded = [];
  const handle = startHistorySeed({
    store, now: () => 5, seed: (name) => seeded.push(name),
    fetchImpl: async () => { fetches += 1; return okResponse(r); },
  });
  assert.equal(fetches, 0);
  connection.value = { state: "live" };
  connection.value = { state: "stale" };
  connection.value = { state: "live" };
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(fetches, 1);
  assert.deepEqual(seeded, ["engine.rpm"]);
  handle.cancel();

  const cancelled = signal({ state: "connecting" });
  let late = 0;
  const second = startHistorySeed({
    store: { connection: cancelled }, now: () => 5, seed: () => { late += 1; },
    fetchImpl: async () => { await new Promise((resolve) => setTimeout(resolve, 5)); return okResponse(r); },
  });
  cancelled.value = { state: "live" };
  second.cancel();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(late, 0, "a reply that lands after cancel seeds nothing");
});

test("every demo capture variant answers the route with an empty, contract-shaped reply", () => {
  const summaryFixture = JSON.parse(fs.readFileSync(new URL("./fixtures/summary-v2.json", import.meta.url), "utf8"));
  const views = buildDemoViews({ nowMs: PI_NOW, build: "t", summaryFixture });
  for (const variant of Object.values(views.variants)) {
    const route = variant.routes.find((r) => r.pathname === "/v1/history/recent");
    assert.ok(route, "route answered");
    assert.deepEqual(seedPlan(route.json, { sentMono: 0, receivedMono: 0 }).metrics, {});
  }
});
