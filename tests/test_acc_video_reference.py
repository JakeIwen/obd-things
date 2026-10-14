"""Public ACC decoder checked against independently counted owner-video bars.

The fixture preserves video and raw-capture hashes, actual still timestamps and
source line numbers. Labels were read from images, not computed from CAN fields.
See projects/radar/findings/2026-10-14_acc_distance_video.md for timing limits.
"""

import json
from pathlib import Path
import unittest

from projects.vehicle_data.ccan_powertrain import decode_frame_observations


REFERENCE = (
    Path(__file__).resolve().parent / "fixtures/acc_distance_video.json"
)


class AccVideoReferenceTests(unittest.TestCase):
    def test_video_labeled_display_samples(self):
        reference = json.loads(REFERENCE.read_text())
        for sample in reference["samples"]:
            with self.subTest(video_seconds=sample["requested_video_seconds"]):
                observations = {
                    item.metric: item for item in decode_frame_observations(
                        0x5A0, bytes.fromhex(sample["payload_hex"]),
                    )
                }
                for metric, expected in sample["expected_metrics"].items():
                    if expected is None:
                        self.assertNotIn(metric, observations)
                    else:
                        observed = observations[metric]
                        self.assertEqual(observed.value, expected)
                        self.assertIs(type(observed.value), type(expected))
                        self.assertEqual(observed.quality, "verified")
                        self.assertEqual(observed.source, "ccan.broadcast.0x5a0")
