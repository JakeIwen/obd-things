import { test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { imageRecord, pngSize, refreshManifest } from "../tools/screenshot_manifest.mjs";

const u32 = (n) => { const b = Buffer.alloc(4); b.writeUInt32BE(n); return b; };
/** A PNG's signature and IHDR chunk, which is all pngSize reads. */
const pngHeader = (width, height) => Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), u32(13), Buffer.from("IHDR"),
  u32(width), u32(height), Buffer.from([8, 2, 0, 0, 0]), u32(0),
]);

const manifest = () => ({
  captured_on: "2026-11-01",
  app: "Pi-hosted Van telemetry dashboard",
  build: "00d8780d2c87",
  capture_method: "browser_clean",
  images: [
    { file: "01-drive-gauges.png", caption: "Drive", width: 800, height: 1280, bytes: 1, sha256: "a" },
    { file: "02-drive-sensors.png", caption: "Sensors", width: 800, height: 1280, bytes: 2, sha256: "b" },
  ],
});

test("an image record carries the PNG's size, byte count and sha256", () => {
  const png = pngHeader(800, 1280);
  assert.deepEqual(imageRecord("01-drive-gauges.png", png), {
    file: "01-drive-gauges.png", width: 800, height: 1280, bytes: png.length,
    sha256: crypto.createHash("sha256").update(png).digest("hex"),
  });
  assert.deepEqual(pngSize(pngHeader(1280, 800)), { width: 1280, height: 800 });
  assert.throws(() => pngSize(Buffer.from("GIF89a and then some more bytes")), /not a PNG/);
});

test("a partial refresh replaces only the captured images' fields and leaves the set's date and build", () => {
  const before = manifest();
  const record = { file: "02-drive-sensors.png", width: 800, height: 1280, bytes: 99, sha256: "c" };
  const { manifest: after, wholeSet } = refreshManifest(before, [record], { capturedOn: "2026-11-02", build: "0d4f1aa8d7a9" });
  assert.equal(wholeSet, false);
  assert.deepEqual(after, { ...manifest(), images: [manifest().images[0], { ...manifest().images[1], bytes: 99, sha256: "c" }] });
  assert.deepEqual(before, manifest(), "the input is not modified");
});

test("refreshing every image also dates the set and names its build, keeping the field order", () => {
  const records = manifest().images.map((image, i) => ({ file: image.file, width: 800, height: 1280, bytes: 10 + i, sha256: "n" + i }));
  const { manifest: after, wholeSet } = refreshManifest(manifest(), records, { capturedOn: "2026-11-02", build: "0d4f1aa8d7a9" });
  assert.equal(wholeSet, true);
  assert.equal(after.captured_on, "2026-11-02");
  assert.equal(after.build, "0d4f1aa8d7a9");
  assert.deepEqual(after.images.map((image) => [image.caption, image.bytes, image.sha256]), [["Drive", 10, "n0"], ["Sensors", 11, "n1"]]);
  assert.deepEqual(Object.keys(after), Object.keys(manifest()), "field order is kept, so the file diff stays small");
});

test("a shot the manifest does not list is refused until its caption is written", () => {
  const record = { file: "19-new-feature.png", width: 800, height: 1280, bytes: 5, sha256: "d" };
  assert.throws(() => refreshManifest(manifest(), [record], { capturedOn: "2026-11-02", build: "x" }),
    /no entry for 19-new-feature\.png; add each with its caption first/);
});
