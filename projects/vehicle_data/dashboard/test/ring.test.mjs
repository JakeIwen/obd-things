import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { createRing, parseEpochMs, epochToMono, DEFAULT_WINDOW_MS, DEFAULT_MAX_POINTS } from "../src/ring.js";

const here = dirname(fileURLToPath(import.meta.url));
const sparklines = JSON.parse(readFileSync(join(here, "fixtures", "sparklines.json"), "utf8"));

const BASE = 10_000; // arbitrary monotonic origin

test("defaults and rejections", () => {
  const ring = createRing();
  assert.equal(ring.windowMs, DEFAULT_WINDOW_MS);
  assert.equal(ring.capacity, DEFAULT_MAX_POINTS);
  assert.equal(ring.size(), 0);
  assert.equal(ring.latest(), null);
  assert.equal(ring.push(NaN, 1, "a"), false);
  assert.equal(ring.push(BASE, NaN, "a"), false);
  assert.equal(ring.push(BASE, Infinity, "a"), false);
  assert.equal(ring.push(BASE, "12", "a"), false);
  assert.equal(ring.size(), 0);
  assert.equal(ring.push(BASE, 12.6, "2026-09-22T16:25:20+00:00"), true);
  assert.equal(ring.size(), 1);
  const latest = ring.latest();
  assert.deepEqual({ t: latest.t, value: latest.value, observedAt: latest.observedAt }, {
    t: BASE,
    value: 12.6,
    observedAt: "2026-09-22T16:25:20+00:00",
  });
});

test("push dedupes by observedAt and keeps time order", () => {
  const ring = createRing();
  assert.equal(ring.push(BASE, 1, "t1"), true);
  assert.equal(ring.push(BASE + 1000, 2, "t1"), false, "same observedAt as the last push is ignored");
  assert.equal(ring.push(BASE + 1000, 2, "t2"), true);
  assert.equal(ring.push(BASE + 500, 3, "t3"), false, "out-of-order time is rejected");
  assert.equal(ring.push(BASE + 1000, 3, "t3"), true, "equal time is accepted");
  // A null/undefined key never dedupes.
  assert.equal(ring.push(BASE + 2000, 4), true);
  assert.equal(ring.push(BASE + 3000, 5), true);
  assert.equal(ring.push(BASE + 4000, 6, null), true);
  assert.equal(ring.push(BASE + 5000, 7, null), true);
  assert.equal(ring.size(), 7);
  assert.equal(ring.latest().value, 7);
  assert.equal(ring.latest().observedAt, null);
});

test("900 one-second pushes produce 90 columns with min/max/mean and nulls for gaps", () => {
  const ring = createRing({ windowMs: 900_000, maxPoints: 1200 });
  const now = BASE + 900_000;
  for (let i = 0; i < 900; i += 1) {
    if (i >= 300 && i < 360) continue; // a one-minute gap
    assert.equal(ring.push(BASE + i * 1000, i, "obs-" + i), true);
  }
  assert.equal(ring.size(), 840);
  const cols = ring.columns(90, now);
  assert.equal(cols.length, 90);
  for (let c = 0; c < 90; c += 1) {
    const col = cols[c];
    if (c >= 30 && c < 36) {
      assert.equal(col, null, "column " + c + " should be a gap");
      continue;
    }
    assert.ok(col, "column " + c + " should have data");
    assert.equal(col.count, 10);
    assert.equal(col.min, c * 10);
    assert.equal(col.max, c * 10 + 9);
    assert.equal(col.mean, c * 10 + 4.5);
  }
});

test("columns: right edge, window start, and points at now", () => {
  const ring = createRing({ windowMs: 100_000, maxPoints: 100 });
  const now = BASE + 100_000;
  ring.push(BASE, 1, "a"); // exactly window start -> column 0
  ring.push(BASE + 99_999, 2, "b"); // last column
  ring.push(now, 3, "c"); // at now -> clamped into last column
  const cols = ring.columns(10, now);
  assert.equal(cols[0].count, 1);
  assert.equal(cols[0].min, 1);
  assert.equal(cols[9].count, 2);
  assert.equal(cols[9].min, 2);
  assert.equal(cols[9].max, 3);
  assert.equal(cols[9].mean, 2.5);
  for (let i = 1; i < 9; i += 1) assert.equal(cols[i], null);
  // Points older than the window are not summarised even if still stored.
  const later = ring.columns(10, now + 50_000);
  assert.equal(later[0], null);
  assert.equal(later[4].count, 1, "BASE+99,999 is 49,999 ms into the new window -> column 4");
  assert.equal(later[5].count, 1, "BASE+100,000 is 50,000 ms into the new window -> column 5");
  assert.equal(later[9], null);
  assert.deepEqual(ring.columns(0, now), []);
  assert.deepEqual(ring.columns(-3, now), []);
});

test("columns output is pooled and rewritten on every call", () => {
  const ring = createRing({ windowMs: 10_000, maxPoints: 50 });
  ring.push(BASE + 5000, 5, "a");
  const first = ring.columns(5, BASE + 10_000);
  assert.equal(first[2].count, 1);
  const second = ring.columns(5, BASE + 10_000);
  assert.equal(second, first, "same array instance for the same n");
  ring.clear();
  const third = ring.columns(5, BASE + 10_000);
  assert.equal(third, first);
  assert.deepEqual(third, [null, null, null, null, null]);
  const other = ring.columns(3, BASE + 10_000);
  assert.notEqual(other, first, "a different n gets a new pool");
  assert.equal(other.length, 3);
});

test("window eviction on push and capacity wrap-around", () => {
  const ring = createRing({ windowMs: 5000, maxPoints: 100 });
  for (let i = 0; i < 20; i += 1) ring.push(BASE + i * 1000, i, "k" + i);
  // Window [t-5000, t]: points at 14..19 s remain (14 s is exactly the cutoff and is kept).
  assert.equal(ring.size(), 6);
  const cols = ring.columns(6, BASE + 19_000);
  assert.equal(cols[0].min, 14);
  assert.equal(cols[5].max, 19);

  const small = createRing({ windowMs: 1_000_000, maxPoints: 10 });
  for (let i = 0; i < 25; i += 1) small.push(BASE + i * 1000, i, "s" + i);
  assert.equal(small.size(), 10);
  assert.equal(small.latest().value, 24);
  const all = small.columns(1, BASE + 24_000);
  assert.equal(all[0].count, 10);
  assert.equal(all[0].min, 15);
  assert.equal(all[0].max, 24);
  // The wrapped ring still dedupes against the newest key and keeps order.
  assert.equal(small.push(BASE + 25_000, 25, "s24"), false);
  assert.equal(small.push(BASE + 23_000, 25, "s25"), false);
  assert.equal(small.push(BASE + 25_000, 25, "s25"), true);
  assert.equal(small.size(), 10);
  assert.equal(small.columns(1, BASE + 25_000)[0].min, 16);
});

test("clear() empties the ring", () => {
  const ring = createRing({ maxPoints: 5 });
  ring.push(BASE, 1, "a");
  ring.push(BASE + 1, 2, "b");
  ring.clear();
  assert.equal(ring.size(), 0);
  assert.equal(ring.latest(), null);
  assert.equal(ring.push(BASE - 5000, 3, "b"), true, "after clear nothing constrains order or dedupe");
});

test("epoch helpers", () => {
  assert.equal(parseEpochMs("2026-09-22T18:57:18.190916+00:00"), Date.parse("2026-09-22T18:57:18.190+00:00"));
  assert.equal(parseEpochMs("2026-09-22T18:57:18Z"), Date.parse("2026-09-22T18:57:18Z"));
  assert.equal(parseEpochMs(1234), 1234);
  assert.equal(parseEpochMs(new Date(5678)), 5678);
  assert.ok(Number.isNaN(parseEpochMs("nope")));
  assert.ok(Number.isNaN(parseEpochMs(null)));
  assert.ok(Number.isNaN(parseEpochMs(Infinity)));
  assert.equal(epochToMono(1_000_000, 500, 1_000_500), 0);
  assert.equal(epochToMono(1_000_500, 500, 1_000_500), 500);
});

test("seed() maps summary sparkline points onto the monotonic axis", () => {
  const trend = sparklines.metric_trends["battery.voltage"];
  const nowEpochMs = Date.parse(trend.series.end_at.replace(/(\.\d{3})\d+/, "$1"));
  const nowMono = 5_000_000;
  const ring = createRing({ windowMs: 86_400_000, maxPoints: 200 });
  const accepted = ring.seed(trend.sparkline, { nowMono, nowEpochMs });
  assert.equal(accepted, trend.sparkline.length);
  assert.equal(ring.size(), trend.sparkline.length);
  const last = trend.sparkline[trend.sparkline.length - 1];
  const latest = ring.latest();
  assert.equal(latest.t, nowMono - (nowEpochMs - parseEpochMs(last.at)));
  assert.equal(latest.value, last.value);
  assert.equal(latest.observedAt, last.at);
  const cols = ring.columns(96, nowMono);
  let total = 0;
  let filled = 0;
  for (const col of cols) {
    if (col) {
      total += col.count;
      filled += 1;
    }
  }
  assert.equal(total, trend.sparkline.length, "every point lands in exactly one column");
  assert.ok(filled < 96, "the parked night leaves gaps");
  // The first bucket is 23.75 h before the end: column 0 (0..15 min into the window) is empty,
  // column 1 holds it.
  assert.equal(cols[0], null);
  assert.equal(cols[1].count, 1);
  assert.equal(cols[1].min, trend.sparkline[0].value);
  // Coolant series: a dozen buckets inside one drive with the warm-up minimum preserved.
  const coolant = sparklines.metric_trends["engine.coolant_temperature"];
  const cRing = createRing({ windowMs: 86_400_000, maxPoints: 200 });
  cRing.seed(coolant.sparkline, { nowMono, nowEpochMs: parseEpochMs(coolant.series.end_at) });
  const cCols = cRing.columns(96, nowMono);
  let min = Infinity;
  for (const col of cCols) if (col && col.min < min) min = col.min;
  assert.equal(min, Math.min(...coolant.sparkline.map((p) => p.value)));
});

test("seed() keeps live points, drops history newer than the oldest live point, and replaces earlier seeds", () => {
  const ring = createRing({ windowMs: 3_600_000, maxPoints: 100 });
  const nowMono = 100_000;
  const nowEpochMs = Date.parse("2026-09-22T18:00:00Z");
  ring.push(nowMono - 60_000, 12.4, "live-1"); // one minute ago
  ring.push(nowMono, 12.5, "live-2");
  const history = [
    { at: "2026-09-22T17:59:30Z", value: 99 }, // 30 s ago: newer than the oldest live point -> dropped
    { at: "2026-09-22T17:30:00Z", value: 12.2 },
    { at: "2026-09-22T17:45:00Z", value: 12.3 },
    { at: "2026-09-22T17:15:00Z", value: 12.1 },
    { at: "2026-09-22T16:00:00Z", value: 11.0 }, // 2 h ago: outside the window
    { at: "garbage", value: 1 },
    { at: "2026-09-22T17:20:00Z", value: null },
    null,
  ];
  assert.equal(ring.seed(history, { nowMono, nowEpochMs }), 3);
  assert.equal(ring.size(), 5);
  const cols = ring.columns(4, nowMono); // 15-minute columns over the hour 17:00-18:00
  assert.equal(cols[0], null); // 17:00-17:15 holds nothing
  assert.equal(cols[1].count, 1); // 17:15
  assert.equal(cols[2].count, 1); // 17:30
  assert.equal(cols[3].count, 3); // 17:45 plus the two live points
  assert.equal(cols[3].min, 12.3);
  assert.equal(cols[3].max, 12.5);
  assert.equal(ring.latest().value, 12.5);
  assert.equal(ring.latest().observedAt, "live-2");

  // Re-seed with the same buckets shifted (the broker realigns bucket edges on every refresh):
  // the previous seeds are replaced, not accumulated.
  const shifted = [
    { at: "2026-09-22T17:31:00Z", value: 12.2 },
    { at: "2026-09-22T17:46:00Z", value: 12.3 },
    { at: "2026-09-22T17:16:00Z", value: 12.1 },
    { at: "2026-09-22T17:01:00Z", value: 12.0 },
  ];
  assert.equal(ring.seed(shifted, { nowMono, nowEpochMs }), 4);
  assert.equal(ring.size(), 6);
  const again = ring.columns(4, nowMono);
  assert.equal(again[0].count, 1); // 17:01
  assert.equal(again[0].min, 12.0);
  assert.equal(again[1].count, 1); // 17:16
  assert.equal(again[2].count, 1); // 17:31
  assert.equal(again[3].count, 3); // 17:46 plus the two live points
  // Live pushes continue to work after seeding (order and dedupe intact).
  assert.equal(ring.push(nowMono + 1000, 12.6, "live-2"), false);
  assert.equal(ring.push(nowMono + 1000, 12.6, "live-3"), true);
  assert.equal(ring.size(), 7);
});

test("seed() into an empty ring respects capacity and unsorted input", () => {
  const ring = createRing({ windowMs: 1_000_000, maxPoints: 3 });
  const points = [
    { t: 5000, value: 5 },
    { t: 1000, value: 1 },
    { t: 4000, value: 4 },
    { t: 2000, value: 2 },
    { t: 3000, value: 3 },
  ];
  assert.equal(ring.seed(points, { nowMono: 5000, nowEpochMs: 5000 }), 3, "only the newest three fit");
  assert.equal(ring.size(), 3);
  const cols = ring.columns(1, 5000);
  assert.equal(cols[0].min, 3);
  assert.equal(cols[0].max, 5);
  assert.equal(ring.latest().t, 5000);
  assert.equal(ring.seed([], { nowMono: 5000, nowEpochMs: 5000 }), 0);
  assert.equal(ring.size(), 0, "an empty seed clears earlier seeds but keeps live points (none here)");
  assert.equal(ring.seed(points, null), 0, "no mapping maps against now; 1970 epoch points fall outside the window");
  assert.equal(ring.seed(points, { nowMono: NaN, nowEpochMs: 1 }), 0);
});

test("seed(points) without a mapping maps wall-clock points against performance.now()/Date.now()", () => {
  const ring = createRing({ windowMs: 900000, maxPoints: 100 });
  const wall = Date.now();
  const accepted = ring.seed([
    { at: new Date(wall - 60000).toISOString(), value: 1 },
    { t: wall - 30000, value: 2 },
  ]);
  const mono = performance.now();
  assert.equal(accepted, 2);
  assert.equal(ring.latest().value, 2);
  assert.ok(Math.abs(ring.latest().t - (mono - 30000)) < 1000, "mapped onto the monotonic axis");
});
