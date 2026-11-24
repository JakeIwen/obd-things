import { test } from "node:test";
import assert from "node:assert/strict";
import { buildDemoScene } from "../tools/demo_data.mjs";
import { demoSeriesReply, demoSeriesRoutes } from "../tools/demo_series.mjs";
import { historySeries, seriesUrl, SPANS } from "../src/chart.helpers.js";

// Why: the demo captures must answer the chart dialog's history spans, with data the chart accepts
// and that agrees with the scene's Drive tile.

const NOW = Date.UTC(2026, 9, 28, 20, 30);
const scene = buildDemoScene(NOW);
const routes = demoSeriesRoutes(scene);

test("every scene metric has a route for each history span, matching the page's own request", () => {
  const history = SPANS.filter((s) => s.source === "history");
  assert.equal(routes.length, Object.keys(scene.sparklines).length * history.length);
  for (const span of history) {
    const url = seriesUrl("engine.coolant_temperature", span.key);
    const route = routes.find((r) => r.pathname + "?" + r.query === url);
    assert.ok(route, "a route answers " + url);
    assert.equal(route.json.span, span.key);
  }
});

test("the replies are chart data: a window, gaps between drives, and values around the scene's", () => {
  const span = SPANS.find((s) => s.key === "24h");
  const series = historySeries(demoSeriesReply(scene, "engine.coolant_temperature", span));
  assert.ok(series && series.points.length > 10);
  assert.equal(series.endMs, NOW);
  const gaps = series.points.filter((p, i) => i && p.t - series.points[i - 1].t > series.gapMs);
  assert.ok(gaps.length >= 1, "the engine-off time between drives is a gap");
  const last = series.points[series.points.length - 1];
  assert.ok(Math.abs(last.mean - scene.metrics["engine.coolant_temperature"].value) < 6, "ends near the Drive tile's value");
  assert.ok(series.points.every((p) => p.min <= p.mean && p.mean <= p.max));
});

test("a cold start warms up: the first reading of a drive is well below its highway value", () => {
  const span = SPANS.find((s) => s.key === "6h");
  const series = historySeries(demoSeriesReply(scene, "engine.coolant_temperature", span));
  const lowest = Math.min(...series.points.map((p) => p.min));
  assert.ok(lowest < 150);
});
