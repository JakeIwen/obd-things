import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

import { buildWarningCards } from "../src/warnings.js";
import { tripsModel } from "../src/views/history.helpers.js";
import { radarModel } from "../src/views/system.helpers.js";
import { selectedPoint } from "../src/radarReference.js";
import { buildDemoViews, PARKED_MINUTES_AFTER_DRIVE } from "../tools/demo_views.mjs";
import { OPENED_MINUTES_AGO } from "../tools/demo_events.mjs";

const NOW = Date.UTC(2026, 9, 28, 20, 30);
const fixture = JSON.parse(fs.readFileSync(new URL("./fixtures/summary-v2.json", import.meta.url), "utf8"));
const views = buildDemoViews({ nowMs: NOW, build: "test-build", summaryFixture: fixture });
const { running, parked, parkedScan, stationaryRadarService } = views.variants;
/** The reply a variant's listener route gives, as the capture browser serves it. */
const served = (variant, pathname) => variant.routes.find((r) => r.method === "GET" && r.pathname === pathname);

test("the running Health page has one open warning: the RR tire the Drive scene shows at 79 psi", () => {
  const cards = buildWarningCards(running.summary.health, NOW);
  assert.equal(cards.counts.open, 1);
  assert.equal(cards.warnings.length, 1);
  assert.match(cards.warnings[0].line, /^RR tire 79 psi, 3 psi under its usual 82 for 34 min\./);
  const opened = Date.parse(running.summary.health.episodes.active[0].opened_at);
  assert.equal(NOW - opened, OPENED_MINUTES_AGO * 60_000);
});

test("parked, the same warning is unconfirmed since the engine stopped at the end of the demo drive", () => {
  const cards = buildWarningCards(parked.summary.health, NOW);
  assert.equal(cards.warnings.length, 1);
  assert.equal(cards.warnings[0].state, "unconfirmed");
  const stopped = Date.parse(parked.summary.status_full.engine_off_voltage.last_sample.engine_stopped_at);
  assert.equal(NOW - stopped, PARKED_MINUTES_AFTER_DRIVE * 60_000);
});

test("History lists the drive in progress while running and as the newest trip once parked", () => {
  const live = tripsModel(running.summary.history, { nowMs: NOW });
  assert.equal(live.current.title, "In progress");
  assert.equal(live.current.duration, "42 min");
  const after = tripsModel(parked.summary.history, { nowMs: NOW });
  assert.equal(after.current, null);
  assert.equal(after.rows[0].duration, "42 min");
  assert.ok(after.rows.every((row) => !/not recorded/.test(row.meta)), "every trip carries a cruise summary");
});

test("rebased trend statistics stay ordered and tire baselines sit at the scene's pressures", () => {
  for (const [name, trend] of Object.entries(running.summary.history.metric_trends)) {
    for (const window of [trend.days_7, trend.days_30].filter(Boolean)) {
      assert.ok(window.minimum <= window.mean && window.mean <= window.maximum, name + " window out of order");
    }
  }
  const usual = (name) => running.summary.history.metric_trends[name].prior_trips.median_of_trip_means;
  assert.equal(Math.round(usual("tire.pressure.fl")), views.scene.metrics["tire.pressure.fl"].value);
  assert.equal(usual("tire.pressure.rr"), 82);
});

test("opt-in controls appear only in the variants that show them, and no listener names a real host", () => {
  assert.equal(served(parkedScan, "/v1/diagnostics/dtc-jobs/current").json.state, "idle");
  assert.equal(served(running, "/v1/diagnostics/dtc-jobs/current").status, 403);
  assert.equal(served(stationaryRadarService, "/v1/radar/service-alignment").json.helper.ready, true);
  assert.equal(served(running, "/v1/radar/service-alignment").json.reason, "radar_service_disabled");
  assert.equal(stationaryRadarService.metrics["vehicle.speed"].value, 0);
  for (const variant of Object.values(views.variants)) assert.equal(variant.web.bind, "loopback demo");
});

test("the saved radar reference is selected, so System shows angles as change since it", () => {
  const reference = selectedPoint(served(running, "/v1/radar/alignment-reference").json);
  assert.ok(reference, "a selected reference point");
  const axes = Object.fromEntries([["vertical", "elevation"], ["horizontal", "azimuth"]].map(([id, axis]) =>
    [id, { fresh: true, value: views.scene.metrics["radar.alignment." + axis].value, held: null }]));
  const model = radarModel({ axes, windows: running.summary.status_full.radar_alignment,
    polling: running.summary.status_full.radar_alignment_polling, reference, nowMs: NOW });
  const since = model.axes.flatMap((axis) => axis.rows).filter(([label]) => label === "Since reference");
  assert.deepEqual(since.map(([, value]) => value), ["−0.08°", "+0.07°"]);
});
