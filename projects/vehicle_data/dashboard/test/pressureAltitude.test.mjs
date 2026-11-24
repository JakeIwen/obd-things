import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import { pressureAltitudeFeet, pressureAltitudeDisplay } from "../src/pressureAltitude.helpers.js";

test("standard-atmosphere altitude and 10-foot display rounding", () => {
  assert.equal(pressureAltitudeFeet(101.325), 0);
  assert.ok(Math.abs(pressureAltitudeFeet(81.3076) - 5962.27) < 0.01);
  assert.ok(Math.abs(pressureAltitudeFeet(80.7083) - 6158.38) < 0.01);
  assert.deepEqual(pressureAltitudeDisplay({kind: "live", value: 81.3076}, "live"),
    {text: "5,960 ft", stale: false});
  assert.deepEqual(pressureAltitudeDisplay({kind: "live", value: 80.7083}, "unavailable"),
    {text: "6,160 ft", stale: true});
  for (const invalid of [0, -1, "81", NaN, Infinity, null]) {
    assert.equal(pressureAltitudeFeet(invalid), null);
  }
});

test("the shared Drive footer renders altitude immediately before the compass and dims expired data", async () => {
  const compiled = await build({
    stdin: {
      contents: `export { DriveFooter } from "./src/views/DriveFooter.jsx";
        export { Compass } from "./src/components/Compass.jsx";
        export { applyBaseline, tick, connection } from "./src/store.js";`,
      resolveDir: fileURLToPath(new URL("..", import.meta.url)),
    },
    bundle: true, write: false, format: "esm", jsx: "automatic", jsxImportSource: "preact",
  });
  const url = "data:text/javascript;base64," + Buffer.from(compiled.outputFiles[0].text).toString("base64");
  const { DriveFooter, Compass, applyBaseline, tick, connection } = await import(url);
  for (const page of [1, 2]) {
    const [, , readouts] = DriveFooter({page}).props.children;
    const [altitude, compass] = readouts.props.children;
    assert.equal(compass.type, Compass);
    assert.equal(altitude.type(altitude.props), null);
  }

  const name = "engine.barometric_pressure";
  const source = "pcm.did.0109";
  const quality = "observed_alfa_scale";
  const observed = new Date(Date.now() - 1_000).toISOString();
  const now = performance.now();
  applyBaseline({
    catalog: [{
      name, unit: "kPa", value_type: "number", minimum: 50, maximum: 110, stale_after_seconds: 30,
      sources: [{name: source, quality}],
    }],
    metrics: {[name]: {
      metric: name, value: 81.107838, unit: "kPa", quality, source, available: true,
      observed_at: observed, age_ms: 0, stale: false,
    }},
  }, 0, now);
  connection.value = {state: "live"};
  const [altitude] = DriveFooter({page: 1}).props.children[2].props.children;
  const live = altitude.type(altitude.props);
  assert.equal(live.props.children, "6,030 ft");
  assert.equal(live.props.class, "pressure-altitude");
  assert.match(live.props.title, /barometric reading.*Weather/);

  tick(now + 31_000);
  const stale = altitude.type(altitude.props);
  assert.equal(stale.props.children, live.props.children);
  assert.equal(stale.props.class, "pressure-altitude pressure-altitude--stale");
});
