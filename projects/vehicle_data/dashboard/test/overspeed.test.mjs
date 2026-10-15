import { test } from "node:test";
import assert from "node:assert/strict";
import {
  IDLE_CHIME_STATE, OVERSPEED_CHIME_DELAY_MS, OVERSPEED_REARM_MS, isOverspeed, stepOverspeedChime,
} from "../src/overspeed.helpers.js";

/** Feed [timeMs, speed, limit] samples; returns the times at which a chime sounded. */
function run(samples) {
  let state = IDLE_CHIME_STATE;
  const chimes = [];
  for (const [t, speed, limit] of samples) {
    const step = stepOverspeedChime(state, speed, limit, t);
    state = step.state;
    if (step.chime) chimes.push(t);
  }
  return chimes;
}
const every = (from, to, speed, limit, stepMs = 1000) => {
  const out = [];
  for (let t = from; t <= to; t += stepMs) out.push([t, speed, limit]);
  return out;
};

test("isOverspeed: strictly more than 15 % over a current limit", () => {
  assert.equal(isOverspeed(63.25, 55), false);
  assert.equal(isOverspeed(63.3, 55), true);
  assert.equal(isOverspeed(80, 70), false);
  assert.equal(isOverspeed(80.6, 70), true);
  for (const limit of [null, 0, -5, NaN, undefined, "55"]) assert.equal(isOverspeed(90, limit), false);
  for (const speed of [null, NaN, undefined, "90"]) assert.equal(isOverspeed(speed, 55), false);
});

test("chimes once, five seconds into a continuous episode", () => {
  assert.equal(OVERSPEED_CHIME_DELAY_MS, 5000);
  assert.deepEqual(run(every(0, 60000, 70, 55)), [5000]);
  // Speed changing within the episode is not a new episode.
  assert.deepEqual(run([...every(0, 6000, 70, 55), ...every(7000, 30000, 75, 55)]), [5000]);
});

test("the five seconds must be continuous and the limit current", () => {
  // Break at 3 s (back under) restarts the wait.
  assert.deepEqual(run([...every(0, 3000, 70, 55), [4000, 60, 55], ...every(5000, 12000, 70, 55)]), [10000]);
  // A stale limit (null) breaks the condition the same way.
  assert.deepEqual(run([...every(0, 4000, 70, 55), [4500, 70, null], ...every(5000, 9000, 70, 55)]), []);
  assert.deepEqual(run(every(0, 30000, 90, null)), []);
});

test("a new speed limit while still over chimes again after its own five seconds", () => {
  const samples = [...every(0, 10000, 70, 55), ...every(11000, 20000, 70, 45)];
  assert.deepEqual(run(samples), [5000, 16000]);
  // Same limit re-read: no new chime.
  assert.deepEqual(run([...every(0, 10000, 70, 55), ...every(11000, 20000, 70, 55)]), [5000]);
});

test("brief dips after a chime stay in the episode; a long clear spell re-arms", () => {
  const dip = [...every(0, 6000, 70, 55), [7000, 60, 55], [9000, 60, 55], ...every(10000, 20000, 70, 55)];
  assert.deepEqual(run(dip), [5000]);
  const clear = [...every(0, 6000, 70, 55), ...every(7000, 7000 + OVERSPEED_REARM_MS, 60, 55),
    ...every(13000, 20000, 70, 55)];
  assert.deepEqual(run(clear), [5000, 18000]);
});

test("stepOverspeedChime keeps the same state object while nothing changes", () => {
  const idle = stepOverspeedChime(IDLE_CHIME_STATE, 40, 55, 0);
  assert.equal(idle.state, IDLE_CHIME_STATE);
  const started = stepOverspeedChime(IDLE_CHIME_STATE, 70, 55, 0).state;
  assert.equal(stepOverspeedChime(started, 71, 55, 1000).state, started);
});
