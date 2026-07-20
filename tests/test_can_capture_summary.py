import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools import can_capture_summary


class ParseFrameTests(unittest.TestCase):
    def test_parses_long_candump_format(self):
        frame = can_capture_summary.parse_frame(
            " (1782677301.162015)  can0  100   [3]  50 19 A7\n"
        )

        self.assertIsNotNone(frame)
        self.assertEqual(frame.timestamp, 1782677301.162015)
        self.assertEqual(frame.interface, "can0")
        self.assertEqual(frame.can_id, 0x100)
        self.assertEqual(frame.id_bits, 11)
        self.assertEqual(frame.payload, bytes.fromhex("50 19 A7"))

    def test_parses_compact_format_and_extended_id(self):
        frame = can_capture_summary.parse_frame(
            "(1782677302.000001) can1 18DAF12A#62F187313233"
        )

        self.assertIsNotNone(frame)
        self.assertEqual(frame.can_id, 0x18DAF12A)
        self.assertEqual(frame.id_bits, 29)
        self.assertEqual(frame.dlc, 6)

    def test_eight_digit_width_preserves_low_numeric_extended_id(self):
        frame = can_capture_summary.parse_frame("(1.0) can0 00000123#AA")

        self.assertIsNotNone(frame)
        self.assertEqual(frame.can_id, 0x123)
        self.assertEqual(frame.id_bits, 29)

    def test_rejects_mismatched_dlc_and_unsupported_lines(self):
        self.assertIsNone(
            can_capture_summary.parse_frame("(1.0) can0 123 [2] AA")
        )
        self.assertIsNone(can_capture_summary.parse_frame("can0 123#AA"))
        self.assertIsNone(can_capture_summary.parse_frame("(1.0) can0 123#R8"))


class SummaryTests(unittest.TestCase):
    def test_summarizes_counts_timing_dlc_and_changed_bytes(self):
        lines = iter(
            [
                "(100.0) can0 100 [3] 01 02 03\n",
                "(101.0) can0 100#01FF03\n",
                "(102.0) can0 18DAF12A#1122\n",
                "(103.0) can1 00000123#AA\n",
                "not a candump frame\n",
                "\n",
            ]
        )

        summary = can_capture_summary.summarize_lines(lines, source="fixture.log")

        self.assertEqual(summary["total_frames"], 4)
        self.assertEqual(summary["total_lines"], 6)
        self.assertEqual(summary["unparsed_lines"], 1)
        self.assertEqual(summary["blank_lines"], 1)
        self.assertEqual(summary["first_timestamp"], 100.0)
        self.assertEqual(summary["last_timestamp"], 103.0)
        self.assertEqual(summary["duration_s"], 3.0)
        self.assertEqual(summary["average_rate_fps"], 1.0)
        self.assertEqual(summary["interfaces"], {"can0": 3, "can1": 1})
        self.assertEqual(summary["frame_formats"]["11bit"]["frames"], 2)
        self.assertEqual(summary["frame_formats"]["29bit"]["frames"], 2)

        standard = next(
            row
            for row in summary["ids"]
            if row["can_id"] == 0x100 and row["id_bits"] == 11
        )
        self.assertEqual(standard["count"], 2)
        self.assertEqual(standard["dlcs"], [3])
        self.assertEqual(standard["dlc_counts"], {"3": 2})
        self.assertEqual(standard["changed_byte_mask"], 0b010)
        self.assertEqual(standard["changed_byte_mask_hex"], "0x02")
        self.assertEqual(standard["byte_minimums"], [0x01, 0x02, 0x03])
        self.assertEqual(standard["byte_maximums"], [0x01, 0xFF, 0x03])
        self.assertEqual(standard["byte_presence_counts"], [2, 2, 2])
        self.assertEqual(standard["constant_byte_mask"], 0b101)
        self.assertEqual(summary["schema_version"], 2)
        self.assertIn("payload content", summary["payload_statistics_warning"])

    def test_dlc_change_marks_byte_presence_change(self):
        summary = can_capture_summary.summarize_lines(
            ["(1.0) can0 123#0102\n", "(2.0) can0 123#010203\n"]
        )

        row = summary["ids"][0]
        self.assertEqual(row["dlcs"], [2, 3])
        self.assertEqual(row["changed_byte_mask"], 0b100)
        self.assertEqual(row["changed_byte_mask_hex"], "0x04")
        self.assertEqual(row["byte_minimums"], [0x01, 0x02, 0x03])
        self.assertEqual(row["byte_maximums"], [0x01, 0x02, 0x03])
        self.assertEqual(row["byte_presence_counts"], [2, 2, 1])
        self.assertEqual(row["constant_byte_mask"], 0b011)

    def test_empty_stream_has_no_invented_timing_or_rate(self):
        summary = can_capture_summary.summarize_lines([])

        self.assertEqual(summary["total_frames"], 0)
        self.assertIsNone(summary["duration_s"])
        self.assertIsNone(summary["average_rate_fps"])
        self.assertEqual(summary["ids"], [])

    def test_bounded_lines_ignore_partial_snapshot_tail(self):
        state = {"trailing_partial_line_ignored": False}
        capture = io.BytesIO(
            b"(1.0) can0 123#AA\n(2.0) can0 123#BB\n(3.0) can0 123#CC"
        )

        lines = list(
            can_capture_summary._bounded_text_lines(
                capture, len(capture.getvalue()), state
            )
        )

        self.assertEqual(
            lines,
            ["(1.0) can0 123#AA\n", "(2.0) can0 123#BB\n"],
        )
        self.assertTrue(state["trailing_partial_line_ignored"])


class CliTests(unittest.TestCase):
    def test_human_stdout_and_explicit_json_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.log"
            capture.write_text(
                "(10.0) can0 123#AA\n(11.0) can0 123#AB\n",
                encoding="utf-8",
            )
            json_path = root / "nested" / "summary.json"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = can_capture_summary.main(
                    [str(capture), "--json", str(json_path)]
                )

            self.assertEqual(result, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("Frames: 2 parsed", stdout.getvalue())
            self.assertIn("123", stdout.getvalue())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], str(capture))
            self.assertEqual(payload["total_frames"], 2)
            self.assertEqual(payload["ids"][0]["changed_byte_mask_hex"], "0x01")

    def test_refuses_to_overwrite_capture_with_json(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.log"
            original = "(10.0) can0 123#AA\n"
            capture.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = can_capture_summary.main(
                    [str(capture), "--json", str(capture)]
                )

            self.assertEqual(result, 2)
            self.assertIn("must not overwrite", stderr.getvalue())
            self.assertEqual(capture.read_text(encoding="utf-8"), original)

    def test_snapshot_records_fixed_byte_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.log"
            complete = "(10.0) can0 123#AA\n"
            partial = "(11.0) can0 123#"
            capture.write_text(complete + partial, encoding="utf-8")
            json_path = Path(directory) / "snapshot.json"
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                result = can_capture_summary.main(
                    [str(capture), "--snapshot", "--json", str(json_path)]
                )

            self.assertEqual(result, 0)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["total_frames"], 1)
            self.assertEqual(payload["snapshot"]["byte_limit"], len(complete + partial))
            self.assertTrue(payload["snapshot"]["trailing_partial_line_ignored"])
            self.assertIn("Snapshot boundary:", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
