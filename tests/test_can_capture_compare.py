import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools import can_capture_compare, can_capture_summary


def summary(lines, source):
    return can_capture_summary.summarize_lines(lines, source=source)


class CompareTests(unittest.TestCase):
    def test_finds_new_missing_rate_and_newly_variable_bytes(self):
        baseline = summary(
            [
                "(0.0) can0 100#0000\n",
                "(1.0) can0 100#0000\n",
                "(2.0) can0 100#0000\n",
                "(0.0) can0 101#AA\n",
                "(2.0) can0 101#AA\n",
                "(0.0) can0 00000123#01\n",
                "(2.0) can0 00000123#01\n",
            ],
            "baseline.log",
        )
        current = summary(
            [
                "(10.0) can0 100#0000\n",
                "(10.1) can0 100#0100\n",
                "(10.2) can0 100#0200\n",
                "(10.0) can0 102#BB\n",
                "(11.0) can0 102#BC\n",
                "(10.0) can0 00000123#01\n",
                "(12.0) can0 00000123#01\n",
            ],
            "drive.log",
        )

        comparison = can_capture_compare.compare_summaries(baseline, current)

        self.assertEqual(
            [(row["can_id_hex"], row["id_bits"]) for row in comparison["new_ids"]],
            [("102", 11)],
        )
        self.assertEqual(
            [(row["can_id_hex"], row["id_bits"]) for row in comparison["missing_ids"]],
            [("101", 11)],
        )
        candidate = next(
            row for row in comparison["newly_variable_candidates"] if row["can_id"] == 0x100
        )
        self.assertEqual(candidate["newly_variable_byte_mask"], 0x01)
        self.assertIn("newly_variable_bytes", candidate["significant_reasons"])
        self.assertIn("within_activity_rate", candidate["significant_reasons"])
        self.assertIn("activity_coverage", candidate["significant_reasons"])
        self.assertEqual(candidate["baseline"]["capture_rate_fps"], 1.5)
        self.assertEqual(candidate["current"]["capture_rate_fps"], 1.5)
        self.assertAlmostEqual(candidate["within_activity_rate_ratio"], 10.0)

        common_extended = next(
            row
            for row in comparison["common_ids"]
            if row["can_id"] == 0x123 and row["id_bits"] == 29
        )
        self.assertEqual(common_extended["can_id_hex"], "00000123")

    def test_rejects_bad_thresholds_and_duplicate_ids(self):
        empty = summary([], "empty.log")
        with self.assertRaisesRegex(ValueError, "greater than 1"):
            can_capture_compare.compare_summaries(empty, empty, rate_factor=1)
        duplicate = dict(empty)
        duplicate["ids"] = [
            {"can_id": 0x100, "id_bits": 11, "count": 1},
            {"can_id": 0x100, "id_bits": 11, "count": 1},
        ]
        with self.assertRaisesRegex(ValueError, "repeats"):
            can_capture_compare.compare_summaries(duplicate, empty)

    def test_snapshot_metadata_is_preserved_and_warned(self):
        baseline = summary(["(0.0) can0 100#00\n", "(1.0) can0 100#00\n"], "b")
        current = summary(["(0.0) can0 100#00\n", "(1.0) can0 100#00\n"], "c")
        current["snapshot"] = {
            "byte_limit": 123,
            "trailing_partial_line_ignored": False,
        }

        comparison = can_capture_compare.compare_summaries(baseline, current)
        output = io.StringIO()
        can_capture_compare.print_human(comparison, output=output)

        self.assertIsNone(comparison["baseline"]["snapshot"])
        self.assertEqual(comparison["current"]["snapshot"]["byte_limit"], 123)
        self.assertIn("bounded snapshot input(s): current", output.getvalue())

    def test_detects_constant_value_change_that_variability_masks_miss(self):
        baseline = summary(
            ["(0.0) can0 200#1020\n", "(1.0) can0 200#1020\n"],
            "parked.log",
        )
        current = summary(
            ["(2.0) can0 200#1030\n", "(3.0) can0 200#1030\n"],
            "event.log",
        )

        comparison = can_capture_compare.compare_summaries(baseline, current)

        self.assertEqual(comparison["schema_version"], 2)
        self.assertEqual(len(comparison["constant_value_change_candidates"]), 1)
        changed = comparison["constant_value_change_candidates"][0]
        self.assertEqual(changed["constant_value_change_byte_mask"], 0b10)
        self.assertEqual(changed["newly_variable_byte_mask"], 0)
        self.assertIn("constant_value_change", changed["significant_reasons"])

        output = io.StringIO()
        can_capture_compare.print_human(comparison, output=output)
        self.assertIn("Constant-value changes: 1", output.getvalue())

    def test_schema_v1_remains_accepted_without_constant_value_claims(self):
        baseline = summary(
            ["(0.0) can0 200#10\n", "(1.0) can0 200#10\n"],
            "old-a.log",
        )
        current = summary(
            ["(0.0) can0 200#20\n", "(1.0) can0 200#20\n"],
            "old-b.log",
        )
        for report in (baseline, current):
            report["schema_version"] = 1
            report.pop("payload_statistics_warning", None)
            for row in report["ids"]:
                row.pop("byte_minimums", None)
                row.pop("byte_maximums", None)
                row.pop("byte_presence_counts", None)
                row.pop("constant_byte_mask", None)
                row.pop("constant_byte_mask_hex", None)

        comparison = can_capture_compare.compare_summaries(baseline, current)

        self.assertEqual(comparison["constant_value_change_candidates"], [])
        self.assertEqual(comparison["baseline"]["summary_schema_version"], 1)


class CliTests(unittest.TestCase):
    def test_cli_prints_and_writes_explicit_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_path = root / "baseline.json"
            current_path = root / "current.json"
            output_path = root / "nested" / "comparison.json"
            baseline_path.write_text(
                json.dumps(summary(["(0.0) can0 100#00\n", "(1.0) can0 100#00\n"], "b")),
                encoding="utf-8",
            )
            current_path.write_text(
                json.dumps(summary(["(0.0) can0 101#00\n", "(1.0) can0 101#01\n"], "c")),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = can_capture_compare.main(
                    [str(baseline_path), str(current_path), "--json", str(output_path)]
                )

            self.assertEqual(result, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("New IDs: 1", stdout.getvalue())
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["new_ids"][0]["can_id_hex"], "101")


if __name__ == "__main__":
    unittest.main()
