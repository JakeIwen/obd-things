import { test } from "node:test";
import assert from "node:assert/strict";
import { cruiseLines } from "../src/views/cruise.helpers.js";

test("cruise summaries separate no recording, zero counts and provisional brake association", () => {
  assert.deepEqual(cruiseLines(null), ["Cruise summary not recorded"]);
  assert.deepEqual(cruiseLines({coverage_percent: 0}), ["Cruise summary not recorded"]);
  const lines = cruiseLines({coverage_percent: 95.25, engaged_seconds: 600, following_seconds: 60,
    cancels: {button: 0, brake_associated: 2, unknown: 1, ambiguous: 1},
    comparison: {paired_seconds: 500, mean_delta_mph: -3.5}});
  assert.match(lines[0], /Cruise engaged 10 min.*vehicle ahead 1 min/);
  assert.match(lines[1], /button 0.*possible brake 2.*unknown 1.*both 1/);
  assert.match(lines[2], /3.5 mph below/);
  assert.match(lines[3], /95.3%/);
  assert.doesNotMatch(lines.join(" "), /NaN|undefined|null/);
});
