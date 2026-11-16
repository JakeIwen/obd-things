import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DEFAULT_CAPTURE_TIME, catalogWithDemoMetrics, writeDemoCapture } from "../tools/demo_capture.mjs";

test("the demo capture names the build it is given and writes a setup Playwright can evaluate", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "demo-capture-test-"));
  try {
    const dist = path.join(dir, "dist");
    fs.mkdirSync(dist);
    fs.writeFileSync(path.join(dist, "build.json"), JSON.stringify({ build: "abc123def456" }));
    const out = path.join(dir, "demo");
    const summary = await writeDemoCapture({ outDir: out, bundleUrl: "http://127.0.0.1:9/demo-bundle.json", distDir: dist });
    assert.equal(summary.build, "abc123def456");
    assert.equal(summary.nowMs, DEFAULT_CAPTURE_TIME);
    const bundle = JSON.parse(fs.readFileSync(path.join(out, "demo-bundle.json"), "utf8"));
    assert.deepEqual(Object.keys(bundle.variants).sort(), ["drive", "parked", "parkedScan", "running", "stationaryRadarService"]);
    const install = fs.readFileSync(path.join(out, "install-demo.js"), "utf8");
    assert.match(install, /page\.request\.get\("http:\/\/127\.0\.0\.1:9\/demo-bundle\.json"\)/);
    assert.equal(typeof (0, eval)(install), "function");
    assert.match(install, /function catalogWithDemoMetrics/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("demo metrics missing from the broker's catalog get an entry; known ones keep the broker's", () => {
  const known = { name: "engine.rpm", unit: "rpm", value_type: "integer", sources: [] };
  const catalog = catalogWithDemoMetrics([known], {
    "engine.rpm": { value: 2280, unit: "rpm", quality: "observed_alfa_scale", source: "ccan.broadcast.0x0fc" },
    "fuel.tank_level": { value: 62, unit: "%", quality: "candidate", source: "bcan.broadcast.0x356" },
    "acc.lead_vehicle": { value: true, unit: "boolean", quality: "verified", source: "ccan.broadcast.0x5a0" },
  });
  assert.deepEqual(catalog.map((entry) => entry.name), ["engine.rpm", "fuel.tank_level", "acc.lead_vehicle"]);
  assert.equal(catalog[0], known);
  assert.deepEqual([catalog[1].value_type, catalog[1].unit, catalog[1].sources[0].quality], ["integer", "%", "candidate"]);
  assert.equal(catalog[2].value_type, "boolean");
});
