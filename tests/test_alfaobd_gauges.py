import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools import alfaobd_gauges


FIXTURE = """\ufeff__________________________________________\r
Recording data for Profile A\r
_________________________\r
Date (YY/MM/DD): 24/01/02\r
Time,Voltage V,State,\r
10:00:00.000,12.500,NA,\r
10:00:01.000,12.700,ready,\r
10:00:02.000,12.800,\r
Recording data for\r
Date (YY/MM/DD): 24/01/03\r
Time,Voltage V,\r
10:01:00.000,13.100,extra,\r
10:01:01.000,13.200,\r
Recording data for Profile B\r
Date (YY/MM/DD): bad-date\r
Time,Voltage V,\r
10:02:00.000,99.000,\r
"""


class ParserTests(unittest.TestCase):
    def test_sections_inherit_profile_and_count_malformed_rows(self):
        inventory = alfaobd_gauges.parse_lines(
            io.StringIO(FIXTURE), source="fixture.csv"
        )

        self.assertEqual(inventory["section_count"], 3)
        self.assertEqual(inventory["first_date"], "2024-01-02")
        self.assertEqual(inventory["last_date"], "2024-01-03")
        first, second, third = inventory["sections"]
        self.assertEqual(first["profile_source"], "explicit")
        self.assertEqual(first["profile_marker"], "named")
        self.assertEqual(second["profile"], "Profile A")
        self.assertEqual(second["profile_source"], "inherited")
        self.assertEqual(second["profile_marker"], "blank")
        self.assertEqual(third["profile"], "Profile B")
        self.assertEqual(third["date_raw"], "bad-date")
        self.assertIsNone(third["date"])
        self.assertEqual(inventory["sample_rows"], 6)
        self.assertEqual(inventory["valid_rows"], 4)
        self.assertEqual(inventory["short_rows"], 1)
        self.assertEqual(inventory["long_rows"], 1)

        voltage = first["metrics"][0]
        self.assertEqual(voltage["name"], "Voltage V")
        self.assertEqual(voltage["numeric_count"], 2)
        self.assertEqual(voltage["minimum"], 12.5)
        self.assertEqual(voltage["maximum"], 12.7)
        state = first["metrics"][1]
        self.assertEqual(state["missing_count"], 1)
        self.assertEqual(state["nonnumeric_count"], 1)

    def test_metric_catalog_keeps_profile_namespaces_separate(self):
        inventory = alfaobd_gauges.parse_lines(io.StringIO(FIXTURE))
        voltage_rows = [
            row for row in inventory["metrics"] if row["metric"] == "Voltage V"
        ]

        self.assertEqual(len(voltage_rows), 2)
        by_profile = {row["profile"]: row for row in voltage_rows}
        self.assertEqual(by_profile["Profile A"]["section_count"], 2)
        self.assertEqual(by_profile["Profile A"]["maximum"], 13.2)
        self.assertEqual(by_profile["Profile B"]["minimum"], 99.0)

    def test_no_marker_is_reported_as_unknown_not_invented(self):
        inventory = alfaobd_gauges.parse_lines(
            [
                "Date (YY/MM/DD): 24/03/04\n",
                "Time,Value,\n",
                "01:02:03.004,1,\n",
            ]
        )

        self.assertEqual(
            inventory["sections"][0]["profile"], alfaobd_gauges.UNKNOWN_PROFILE
        )
        self.assertEqual(inventory["sections"][0]["profile_source"], "inherited")
        self.assertEqual(inventory["sections"][0]["profile_marker"], "absent")


class CliTests(unittest.TestCase):
    def test_cli_writes_json_and_both_csv_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "Gauges_Data.csv"
            archive.write_text(FIXTURE, encoding="utf-8", newline="")
            output_dir = root / "reports"
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                result = alfaobd_gauges.main(
                    [str(archive), "--out-dir", str(output_dir)]
                )

            self.assertEqual(result, 0)
            self.assertIn("Sections: 3", stdout.getvalue())
            payload = json.loads(
                (output_dir / "inventory.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["section_count"], 3)
            self.assertEqual(len(payload["source_sha256"]), 64)

            with (output_dir / "sections.csv").open(newline="", encoding="utf-8") as handle:
                section_rows = list(csv.DictReader(handle))
            with (output_dir / "metrics.csv").open(newline="", encoding="utf-8") as handle:
                metric_rows = list(csv.DictReader(handle))
            self.assertEqual(len(section_rows), 3)
            self.assertEqual(
                {(row["profile"], row["metric"]) for row in metric_rows},
                {
                    ("Profile A", "State"),
                    ("Profile A", "Voltage V"),
                    ("Profile B", "Voltage V"),
                },
            )


if __name__ == "__main__":
    unittest.main()
