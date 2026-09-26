from pathlib import Path
import tempfile
import unittest

from tools import can_gear_ratio_analyze as gear


def shaft_payload(turbine, output):
    raw = round(output * 32)
    return bytes((raw >> 16, raw >> 8 & 255, raw & 255, 0)) + round(turbine * 2).to_bytes(2, 'big') + b'\0\0'


class GearRatioTests(unittest.TestCase):
    def test_established_high_output_bit_and_same_frame_ratio(self):
        self.assertAlmostEqual(gear.shaft_ratio(shaft_payload(2100, 3000)), 0.7)
        self.assertIsNone(gear.shaft_ratio(shaft_payload(800, 0)))
        self.assertIsNone(gear.shaft_ratio(shaft_payload(800, 10)))
        with self.assertRaises(ValueError):
            gear.shaft_ratio(b'\0' * 7)

    def test_ratio_groups_keep_fields_and_capture_freshness_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'saved.candump'
            p.write_text(
                '(1.000000) can7 1F4#0000000030000000\n'
                f'(1.010000) can7 1F7#{shaft_payload(1900,1000).hex()}\n'
                f'(1.500000) can7 1F7#{shaft_payload(1380,1000).hex()}\n'
                '(1.510000) can7 1F4#0000000040000000\n'
                f'(1.520000) can7 1F7#{shaft_payload(1380,1000).hex()}\n'
                f'(1.530000) can8 1F7#{shaft_payload(4714,1000).hex()}\n'
            )
            report = gear.analyze(p, 'can7')
            high4 = report['candidate_fields'][9]
            self.assertEqual(report['counts']['matched'], 2)
            self.assertEqual(report['counts']['without_fresh_candidate'], 1)
            self.assertEqual(high4['dbc_start_bit'], 39)
            self.assertAlmostEqual(high4['values']['3']['median_ratio'], 1.9)
            self.assertAlmostEqual(high4['values']['4']['median_ratio'], 1.38)

    def test_loss_marker_is_not_accepted_as_clean_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'loss.candump'
            p.write_text('DROPCOUNT: dropped 1 CAN frame\n')
            with self.assertRaisesRegex(ValueError, 'socket loss'):
                gear.analyze(p, 'can7')
