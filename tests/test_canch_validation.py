import json
from pathlib import Path
import tempfile
import unittest

from projects.ecu_mapping.canch_validation import main


def frame(stamp, channel, identifier, payload):
    return f"({stamp:.6f}) {channel} {identifier}#{payload.hex().upper()}\n"


def speed_payload(raw):
    return bytes(((raw >> 11) & 1, (raw >> 3) & 255, (raw & 7) << 5, 0, 0, 0, 0, 0))


def wheel_payload(values):
    word = 0
    for value in values:
        word = (word << 13) | value
    return (word << 12).to_bytes(8, "big")


class CanchValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_cli(self, reference, candidate):
        ccan, canch = self.root / "c.log", self.root / "ch.log"
        output = self.root / "report.json"
        ccan.write_text("".join(reference))
        canch.write_text("".join(candidate))
        result = main([str(ccan), str(canch), "--reference-count", "1",
                       "--reference-channel", "can0", "--candidate-channel", "can2",
                       "--output", str(output)])
        self.assertEqual(result, 0)
        return json.loads(output.read_text())

    def test_fixed_scale_scores_and_standstill_without_affine_refit(self):
        reference, candidate = [], []
        for i in range(70):
            raw = (0, 0, 8, 32, 80, 160, 320)[i % 7]
            stamp = 1000 + i / 10
            reference.append(frame(stamp, "can0", "101", speed_payload(raw)))
            candidate.append(frame(stamp + .002, "can2", "0EE", wheel_payload([raw] * 4)))
        report = self.run_cli(reference, candidate)
        self.assertFalse(report["scoring"]["formula_refitted"])
        self.assertFalse(report["telemetry_promotion_allowed"])
        for wheel in report["wheels"].values():
            scores = wheel["fixed_scale_errors_kph"]
            self.assertAlmostEqual(scores["sixteenth"]["moving"]["rmse"], 0)
            self.assertGreater(scores["giorgio"]["moving"]["rmse"], .1)
            self.assertEqual(scores["sixteenth"]["stopped"]["sample_count"], 20)
            self.assertEqual(wheel["zero_count"], 20)

    def test_wrong_namespace_dlc_and_suspect_values_are_not_valid_zeros(self):
        reference = [frame(1000 + i / 10, "can0", "101", speed_payload(160)) for i in range(4)]
        candidate = [
            frame(1000, "can2", "0EE", wheel_payload([160] * 4)),
            frame(1000.1, "can2", "000000EE", wheel_payload([0] * 4)),
            frame(1000.2, "can2", "0EE", b"\0" * 7),
            frame(1000.3, "can2", "0EE", wheel_payload([8191] * 4)),
        ]
        report = self.run_cli(reference, candidate)
        wheel = report["wheels"]["wheel_7"]
        self.assertEqual(wheel["count"], 2)
        self.assertEqual(wheel["wrong_dlc"], 1)
        self.assertEqual(wheel["raw_all_ones_count"], 1)
        self.assertEqual(wheel["zero_count"], 0)
        self.assertEqual(wheel["rejected"], {"unmatched": 2, "suspect_wheel_value": 1})
        self.assertEqual(wheel["fixed_scale_errors_kph"]["sixteenth"]["moving"]["coverage_ratio"], .25)

    def test_gap_does_not_hold_a_prior_value(self):
        reference = [frame(1000 + i / 10, "can0", "101", speed_payload(160)) for i in range(31)]
        candidate = [frame(1000 + i / 10, "can2", "0EE", wheel_payload([160] * 4))
                     for i in (0, 1, 29, 30)]
        wheel = self.run_cli(reference, candidate)["wheels"]["wheel_10"]
        self.assertEqual(wheel["gap_count"], 1)
        self.assertEqual(wheel["maximum_gap_ms"], 2800)
        self.assertEqual(wheel["rejected"]["unmatched"], 27)

    def test_stuck_wheel_at_standstill_is_reported_as_error(self):
        reference = [frame(1000 + i / 10, "can0", "101", speed_payload(0)) for i in range(4)]
        candidate = [frame(1000 + i / 10, "can2", "0EE", wheel_payload([160, 0, 0, 0]))
                     for i in range(4)]
        report = self.run_cli(reference, candidate)
        self.assertEqual(report["wheels"]["wheel_7"]["fixed_scale_errors_kph"]["sixteenth"]["stopped"]["rmse"], 10)
        self.assertIsNone(report["context"]["steering_angle"]["minimum"])

    def test_steering_rate_consistency_and_brake_transitions(self):
        reference, candidate = [], []
        previous_angle = None
        for i in range(41):
            stamp = 1000 + i / 10
            angle = (i - 20) ** 2 - 200
            velocity = 0 if previous_angle is None else (angle - previous_angle) * 10
            previous_angle = angle
            payload = (round((angle + 716.8) * 10).to_bytes(2, "big")
                       + round((velocity + 1000) * 2).to_bytes(2, "big") + b"\0\0")
            reference.append(frame(stamp, "can0", "101", speed_payload(160)))
            candidate.extend((
                frame(stamp, "can2", "0EE", wheel_payload([160] * 4)),
                frame(stamp, "can2", "0DE", payload),
                frame(stamp, "can2", "0FA", bytes([8 if i >= 20 else 0]) + b"\0" * 7),
            ))
        report = self.run_cli(reference, candidate)
        consistency = report["steering_consistency"]["angle_derivative_vs_rate"]
        self.assertEqual(consistency["count"], 40)
        self.assertAlmostEqual(consistency["pearson_r"], 1)
        self.assertEqual(report["brake_switch"]["sampled_transitions"], {"0->1": 1})
        self.assertEqual(report["brake_switch"]["sampled_states"], {"0": 20, "1": 21})
        self.assertEqual(report["brake_switch"]["full_rate_transitions"]["edges"], [
            {"from": 0, "to": 1, "previous_timestamp_us": 1001900000,
             "timestamp_us": 1002000000},
        ])

    def test_bad_order_or_wrong_channel_prevents_publication(self):
        for channel, stamps in (("can2", (1000.1, 1000)), ("can3", (1000, 1000.1))):
            with self.subTest(channel=channel):
                reference = [frame(1000 + i / 10, "can0", "101", speed_payload(160)) for i in range(2)]
                candidate = [frame(t, channel, "0EE", wheel_payload([160] * 4)) for t in stamps]
                with self.assertRaises(SystemExit) as context:
                    self.run_cli(reference, candidate)
                self.assertEqual(context.exception.code, 2)
                self.assertFalse((self.root / "report.json").exists())

    def test_full_rate_edges_preserve_a_pulse_between_scored_samples(self):
        reference = [frame(1000 + i / 10, "can0", "101", speed_payload(0)) for i in range(3)]
        candidate = [
            frame(1000, "can2", "0EE", wheel_payload([0] * 4)),
            frame(1000, "can2", "0FA", b"\0" * 8),
            frame(1000.02, "can2", "0FA", b"\x08" + b"\0" * 7),
            frame(1000.04, "can2", "0FA", b"\0" * 8),
            frame(1000.1, "can2", "0EE", wheel_payload([0] * 4)),
            frame(1000.1, "can2", "0FA", b"\0" * 8),
            frame(1000.2, "can2", "0EE", wheel_payload([0] * 4)),
            frame(1000.2, "can2", "0FA", b"\0" * 8),
        ]
        result = self.run_cli(reference, candidate)["brake_switch"]
        self.assertEqual(result["sampled_transitions"], {})
        self.assertEqual(result["full_rate_transitions"]["count"], 2)
        self.assertEqual(result["full_rate_transitions"]["edges"][0]["timestamp_us"], 1000020000)

    def test_held_steering_positions_split_at_recording_gaps(self):
        reference, candidate = [], []
        for i in range(65):
            stamp = 1000 + i / 10
            reference.append(frame(stamp, "can0", "101", speed_payload(0)))
            candidate.append(frame(stamp, "can2", "0EE", wheel_payload([0] * 4)))
            if i not in (30, 31, 32):
                candidate.append(frame(stamp, "can2", "0DE", bytes.fromhex("1C0007D00000")))
        report = self.run_cli(reference, candidate)
        holds = report["steering_consistency"]["held_angles"]["runs"]
        self.assertEqual(len(holds), 2)
        self.assertAlmostEqual(holds[0]["duration_seconds"], 2.9)
        self.assertAlmostEqual(holds[1]["duration_seconds"], 3.1)
        self.assertAlmostEqual(holds[0]["mean_candidate_degrees"], 0)


if __name__ == "__main__":
    unittest.main()
