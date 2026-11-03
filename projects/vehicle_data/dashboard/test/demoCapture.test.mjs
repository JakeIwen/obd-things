import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DEFAULT_CAPTURE_TIME, writeDemoCapture } from "../tools/demo_capture.mjs";

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
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
