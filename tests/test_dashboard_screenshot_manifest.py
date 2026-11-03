"""The README screenshot manifest describes exactly the committed images.

``tools/readme_shots.mjs --promote`` (in the dashboard) refreshes each entry's
size, byte count and hash when it copies a reviewed capture into
``screenshots/``; this checks that no image changed without its entry and that
every image carries a caption. It lives here rather than in the dashboard's
Node suite because the dashboard-test-build compute task ships no screenshots.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import struct
import unittest


SCREENSHOTS = Path(__file__).resolve().parents[1] / "projects/vehicle_data/dashboard/screenshots"


class ScreenshotManifestTest(unittest.TestCase):
    def test_every_image_matches_its_manifest_entry(self) -> None:
        manifest = json.loads((SCREENSHOTS / "manifest.json").read_text(encoding="utf-8"))
        listed = [image["file"] for image in manifest["images"]]
        self.assertEqual(
            sorted(listed),
            sorted(path.name for path in SCREENSHOTS.glob("*.png")),
            "each committed image needs exactly one manifest entry",
        )
        for image in manifest["images"]:
            with self.subTest(file=image["file"]):
                data = (SCREENSHOTS / image["file"]).read_bytes()
                self.assertEqual(data[12:16], b"IHDR", "not a PNG")
                width, height = struct.unpack(">II", data[16:24])
                self.assertEqual(
                    (image["width"], image["height"], image["bytes"], image["sha256"]),
                    (width, height, len(data), hashlib.sha256(data).hexdigest()),
                )
                self.assertTrue(image["caption"].strip(), "every image needs a caption")


if __name__ == "__main__":
    unittest.main()
