import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools import alfaobd_dat


class ParserTests(unittest.TestCase):
    def test_parses_series_and_preserves_exact_tokens(self):
        rows = alfaobd_dat.parse_lines(
            io.StringIO("10\r\n1.0;2.50;NA;\r\n10\r\n;3.0;\r\n"),
            source="fixture.dat",
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].series_id, 10)
        self.assertEqual(rows[0].occurrence, 1)
        self.assertEqual(rows[0].values, ("1.0", "2.50", "NA"))
        self.assertEqual(rows[1].occurrence, 2)
        self.assertEqual(rows[1].values, ("", "3.0"))

        first = rows[0].as_dict()
        self.assertEqual(first["numeric_count"], 2)
        self.assertEqual(first["nonnumeric_count"], 1)
        self.assertEqual(first["minimum"], 1.0)
        self.assertEqual(first["maximum"], 2.5)

    def test_rejects_bad_id_and_missing_value_line(self):
        with self.assertRaisesRegex(alfaobd_dat.DatFormatError, "decimal series"):
            alfaobd_dat.parse_lines(["DID10\n", "1;\n"], source="bad.dat")
        with self.assertRaisesRegex(alfaobd_dat.DatFormatError, "no value line"):
            alfaobd_dat.parse_lines(["10\n"], source="short.dat")


class ComparisonTests(unittest.TestCase):
    def test_classifies_unchanged_repeat_append_truncate_and_change(self):
        self.assertEqual(
            alfaobd_dat.classify_values(("1", "2"), ("1", "2"))["status"],
            "unchanged",
        )
        repeated = alfaobd_dat.classify_values(
            ("1", "2", "1", "2"), ("1", "2")
        )
        self.assertEqual(repeated["status"], "exact_repetition")
        self.assertEqual(repeated["repeat_factor"], 2)
        appended = alfaobd_dat.classify_values(("1", "2", "3"), ("1", "2"))
        self.assertEqual(appended["status"], "baseline_prefix_with_append")
        self.assertEqual(appended["appended_samples"], 1)
        self.assertEqual(
            alfaobd_dat.classify_values(("1",), ("1", "2"))["status"],
            "truncated_baseline_prefix",
        )
        self.assertEqual(
            alfaobd_dat.classify_values(("9", "2"), ("1", "2"))["status"],
            "changed",
        )

    def test_inventory_compares_by_id_and_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "before.dat"
            current = root / "after.dat"
            baseline.write_text("10\n1;2;\n20\n3;\n30\n9;8;\n", encoding="utf-8")
            current.write_text(
                "10\n1;2;1;2;\n20\n3;\n30\n9;\n40\n5;\n", encoding="utf-8"
            )

            report = alfaobd_dat.inventory_file(current, baseline)
            by_id = {row["series_id"]: row for row in report["series"]}

            self.assertEqual(by_id[10]["comparison"]["status"], "exact_repetition")
            self.assertEqual(by_id[20]["comparison"]["status"], "unchanged")
            self.assertEqual(
                by_id[30]["comparison"]["status"], "truncated_baseline_prefix"
            )
            self.assertEqual(by_id[40]["comparison"]["status"], "new_series")
            self.assertEqual(
                report["comparison"]["status_counts"],
                {
                    "exact_repetition": 1,
                    "new_series": 1,
                    "truncated_baseline_prefix": 1,
                    "unchanged": 1,
                },
            )


class CliTests(unittest.TestCase):
    def test_cli_writes_explicit_json_and_refuses_input_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "before.dat"
            current = root / "after.dat"
            output = root / "reports" / "inventory.json"
            baseline.write_text("10\n1;2;\n", encoding="utf-8")
            current.write_text("10\n1;2;1;2;\n", encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                result = alfaobd_dat.main(
                    [str(current), "--baseline", str(baseline), "--json", str(output)]
                )

            self.assertEqual(result, 0)
            self.assertIn("exact_repetition x2", stdout.getvalue())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["comparison"]["status_counts"], {"exact_repetition": 1})
            self.assertEqual(len(payload["source_sha256"]), 64)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = alfaobd_dat.main([str(current), "--json", str(current)])
            self.assertEqual(result, 2)
            self.assertIn("must not overwrite", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
