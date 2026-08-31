import json
from pathlib import Path
import tempfile
import unittest

from tools import can_cross_bus_speed as correlate
from lib.signal_fields import SignalField


def frame(timestamp, channel, can_id, payload):
    return (
        f"({timestamp:.6f}) {channel} {can_id:03X} [{len(payload)}] "
        + " ".join(f"{value:02X}" for value in payload)
        + "\n"
    )


def speed_payload(raw):
    return bytes(((raw >> 11) & 1, (raw >> 3) & 0xFF, (raw & 7) << 5, 0, 0, 0, 0, 0))


class CrossBusSpeedTests(unittest.TestCase):
    def test_targeted_bit_fields_use_shared_dbc_geometry(self):
        payload = bytes.fromhex("12 34 56 78 9A BC DE F0")
        rows = dict(correlate._bit_candidate_fields(payload, (13,)))
        label = "bits:big:4:13:unsigned"
        self.assertIn(label, rows)
        self.assertEqual(
            rows[label],
            SignalField(4, 13, "big", signed=False).extract(payload),
        )

    def test_bounded_cross_bus_ranking_recovers_linear_u16_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ccan = root / "ccan.log"
            canch = root / "canch.log"
            reference_lines = []
            candidate_lines = []
            for index in range(200):
                timestamp = 1000.0 + index * 0.01
                raw_speed = 160 + index
                speed_kph = raw_speed / 16.0
                candidate = round(speed_kph * 100)
                reference_lines.append(
                    frame(timestamp, "can0", 0x101, speed_payload(raw_speed))
                )
                candidate_lines.append(
                    frame(
                        timestamp + 0.002,
                        "can2",
                        0x0DA,
                        bytes((candidate >> 8, candidate & 0xFF, 0x55, 0xAA)),
                    )
                )
            ccan.write_text("".join(reference_lines))
            canch.write_text("".join(candidate_lines))

            report = correlate.correlate(
                ccan,
                canch,
                reference_channel="can0",
                candidate_channel="can2",
                candidate_ids=frozenset((0x0DA,)),
                radius_ms=20,
                minimum_speed_kph=5,
                minimum_samples=100,
                minimum_distinct=8,
                top=20,
            )

            recovered = next(
                row
                for row in report["ranked_fields"]
                if row["can_id"] == "0x0DA" and row["field"] == "u16be:0"
            )
            self.assertGreater(recovered["r_squared"], 0.999)
            self.assertAlmostEqual(
                recovered["reference_kph_per_candidate_count"], 0.01, places=3
            )
            self.assertEqual(report["candidate"]["matched_frames"], 200)

    def test_cli_writes_json_and_rejects_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ccan = root / "ccan.log"
            canch = root / "canch.log"
            output = root / "report.json"
            ccan.write_text(
                "".join(
                    frame(1000 + i * 0.01, "can0", 0x101, speed_payload(160 + i))
                    for i in range(20)
                )
            )
            canch.write_text(
                "".join(
                    frame(1000.002 + i * 0.01, "can2", 0x0DA, bytes((i, i + 1)))
                    for i in range(20)
                )
            )
            args = [
                str(ccan),
                str(canch),
                "--reference-channel",
                "can0",
                "--candidate-channel",
                "can2",
                "--candidate-id",
                "0DA",
                "--minimum-samples",
                "10",
                "--minimum-distinct",
                "4",
                "--output",
                str(output),
            ]
            self.assertEqual(correlate.main(args), 0)
            self.assertEqual(json.loads(output.read_text())["schema_version"], 1)
            with self.assertRaises(SystemExit):
                correlate.main(args)


if __name__ == "__main__":
    unittest.main()
