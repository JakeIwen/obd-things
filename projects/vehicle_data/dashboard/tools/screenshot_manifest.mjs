// screenshots/manifest.json bookkeeping for promoted README shots: each image's size, byte count and
// hash, plus the set's capture date and build. Captions and the prose fields stay hand-written.
import crypto from "node:crypto";

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** Width and height from a PNG's IHDR chunk, which the format requires to come first. */
export function pngSize(buffer) {
  if (buffer.length < 24 || !buffer.subarray(0, 8).equals(PNG_SIGNATURE) || buffer.toString("latin1", 12, 16) !== "IHDR") {
    throw new Error("not a PNG");
  }
  return {width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20)};
}

/** The manifest fields that describe one image file. */
export function imageRecord(file, buffer) {
  return {file, ...pngSize(buffer), bytes: buffer.length, sha256: crypto.createHash("sha256").update(buffer).digest("hex")};
}

/**
 * A copy of the manifest with each recorded image's fields replaced. The set's captured_on and
 * build move only when the records cover every image, as after a full recapture: a partial refresh
 * leaves them naming the rest of the set (3414fb0 and f84faef). An image the manifest has no entry
 * for is refused, since its caption has to be written first.
 * @returns {{manifest: object, wholeSet: boolean}}
 */
export function refreshManifest(manifest, records, {capturedOn, build}) {
  const known = new Set(manifest.images.map((image) => image.file));
  const unknown = records.filter((record) => !known.has(record.file)).map((record) => record.file);
  if (unknown.length) throw new Error(`manifest.json has no entry for ${unknown.join(", ")}; add each with its caption first`);
  const byFile = new Map(records.map((record) => [record.file, record]));
  const images = manifest.images.map((image) => {
    const record = byFile.get(image.file);
    return record ? {...image, width: record.width, height: record.height, bytes: record.bytes, sha256: record.sha256} : image;
  });
  const wholeSet = known.size === byFile.size;
  return {manifest: wholeSet ? {...manifest, captured_on: capturedOn, build, images} : {...manifest, images}, wholeSet};
}
