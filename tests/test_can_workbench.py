"""Offline adapter compatibility, loss accounting and publication contracts."""
from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.can_capture_summary import summarize_file
from tools.can_capture_compare import compare_summaries
from tools.can_workbench import adapter as a
from tools.can_workbench import report as r

FIXTURES = Path(__file__).parent / "fixtures/can_workbench"


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "investigation.json"
        for source in FIXTURES.iterdir():
            if source.is_file():
                shutil.copyfile(source, self.root / source.name)
        self.config = a.load_json(self.manifest)

    def save(self):
        self.manifest.write_text(json.dumps(self.config))

    def build(self, **kwargs):
        self.save()
        output = r.build(self.manifest, self.root / "report", **kwargs)
        return a.load_json(output / "bundle.json")

    def capture(self, path=None, window=None, max_points=2000):
        return a.capture_report(path or self.root / "ramp.candump", self.config["captures"][0],
                                self.config["fields"], window=window, max_points=max_points, gap=1)

    def test_summary_and_comparison_match_existing_tools(self):
        bundle = self.build()
        captures = bundle["captures"]
        for c in captures:
            self.assertEqual(c["summary"], summarize_file(Path(c["source"]["path"])))
        # can1 was only mapped in one input, so compare the explicitly shared can0 scope.
        baseline = dict(captures[0]["summary"])
        baseline["ids"] = [i for i in baseline["ids"] if i["interface"] == "can0"]
        baseline["interfaces"] = {"can0": baseline["interfaces"]["can0"]}
        self.assertEqual(bundle["comparisons"][0]["result"], compare_summaries(baseline, captures[1]["summary"]))

    def test_exact_stream_geometry_precision_and_gaps(self):
        result = self.capture()
        series = result["series"]["ramp"]
        self.assertEqual(series["count"], 5)
        self.assertEqual([p["value"] for p in series["points"]], [9, 13, 19, 23, 29])
        self.assertEqual(series["first_timestamp"], "1.000000000001")
        self.assertEqual([p["seconds"] for p in series["points"]], [0, 1, 2, 5, 6])
        self.assertEqual(series["gap_count"], 1)
        self.assertNotEqual(series["points"][2]["segment"], series["points"][3]["segment"])
        self.assertEqual(result["summary"]["ids"][0]["dlcs"], [1, 2])

    def test_window_counts_are_not_downsampled(self):
        result = self.capture(window=(Decimal(0), Decimal(2)), max_points=4)
        self.assertEqual(result["summary"]["total_frames"], 6)
        self.assertEqual(result["coverage"]["frames_outside_window"], 2)
        self.assertEqual(result["series"]["ramp"]["count"], 3)

    def test_bounded_decimation_keeps_extrema_and_gap_segments(self):
        series = a.Series(16, 1)
        for n in range(10000):
            series.add(Decimal(n + (10 if n > 4500 else 0)), 999999 if n == 4999 else n)
        result = series.report(Decimal(0))
        self.assertEqual(result["count"], 10000)
        self.assertLessEqual(len(result["points"]), 16)
        self.assertEqual(result["maximum"], 999999)
        self.assertEqual(result["gap_count"], 1)
        self.assertEqual({p["segment"] for p in result["points"]}, {0, 1})

    def test_absent_constant_insufficient_and_reference(self):
        bundle = self.build()
        self.assertEqual(bundle["captures"][1]["series"]["ramp"]["status"], "constant")
        absent = bundle["captures"][0]["series"]["absent"]
        self.assertEqual(absent["status"], "missing")
        self.assertIsNone(absent["minimum"])
        self.assertEqual(absent["points"], [])
        self.assertEqual(bundle["references"][0]["series"]["points"], bundle["captures"][0]["series"]["ramp"]["points"])
        series = a.Series(4, 1)
        series.add(Decimal(0), 0)
        self.assertEqual(series.report(Decimal(0))["status"], "insufficient")

    def test_no_reference_and_empty_observations_are_not_zero(self):
        path = self.root / "reference.json"
        data = a.load_json(path)
        data["observations"] = []
        path.write_text(json.dumps(data))
        bundle = self.build()
        self.assertEqual(bundle["references"][0]["series"]["status"], "missing")
        self.assertIsNone(bundle["references"][0]["series"]["minimum"])
        self.assertFalse(any(r["capture"] == "constant" for r in bundle["references"]))

    def test_selection_excludes_unselected_references(self):
        bundle = self.build(capture_names=["constant"], field_names=["ramp"])
        self.assertEqual(len(bundle["captures"]), 1)
        self.assertEqual(bundle["references"], [])
        self.assertEqual(bundle["excluded_references"], ["reference.json"])

    def test_malformed_dlc_and_unterminated_data_accounting(self):
        path = self.root / "ramp.candump"
        path.write_text("\nmalformed\n(1) can0 123 [2] FF\n(2) can0 123#02")
        result = self.capture()
        self.assertEqual(result["summary"], summarize_file(path))
        self.assertEqual(result["coverage"]["malformed_lines"], 2)
        self.assertEqual(result["coverage"]["unterminated_lines"], 1)
        self.assertEqual(result["summary"]["total_frames"], 1)

    def test_decreasing_timestamps_rejected(self):
        (self.root / "ramp.candump").write_text("(2) can0 123#01\n(1) can0 123#02\n")
        with self.assertRaisesRegex(ValueError, "timestamps decrease"):
            self.build()
        self.assertFalse((self.root / "report").exists())

    def test_unmapped_channel_rejected(self):
        self.config["captures"][0]["channels"].pop("can1")
        with self.assertRaisesRegex(ValueError, "no explicit bus mapping"):
            self.build()

    def test_same_channel_on_different_buses_is_not_compared(self):
        self.config["captures"][1]["channels"]["can0"] = "another-bus"
        bundle = self.build()
        self.assertIsNone(bundle["comparisons"][0]["result"])
        self.assertEqual(bundle["captures"][1]["series"], {})

    @unittest.skipUnless(shutil.which("zstd"), "zstd executable unavailable")
    def test_compressed_input_and_truncated_compression(self):
        source = self.root / "ramp.candump"
        path = self.root / "ramp.candump.zst"
        subprocess.run(["zstd", "-q", str(source), "-o", str(path)], check=True)
        plain, compressed = self.capture(), self.capture(path)
        self.assertEqual(plain["series"], compressed["series"])
        self.assertEqual(compressed["summary"], summarize_file(path))
        path.write_bytes(path.read_bytes()[:10])
        with self.assertRaisesRegex(ValueError, "zstd failed"):
            self.capture(path)

    def test_unknown_schemas_and_reference_units_fail_closed(self):
        self.config["schema_version"] = 99
        with self.assertRaisesRegex(ValueError, "schema"):
            self.build()
        self.config["schema_version"] = 1
        data = a.load_json(self.root / "reference.json")
        data["unit"] = "different"
        (self.root / "reference.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "units"):
            self.build()
        path = self.root / "correlation.json"
        data = a.load_json(path)
        data["schema_version"] = 2
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "schema"):
            a.import_analysis(path)

    def test_correlation_import_retains_evidence_without_promotion(self):
        imported = a.import_analysis(self.root / "correlation.json")
        self.assertEqual(imported["report"], a.load_json(self.root / "correlation.json"))
        self.assertEqual(imported["evidence_tier"], "exploratory_candidate")
        self.assertFalse(imported["telemetry_promotion_allowed"])
        candidate = imported["report"]["ranking"]["candidates"][0]
        self.assertIn("field", candidate)
        self.assertAlmostEqual(candidate["affine_model"]["scale"], 2)

    def test_input_hashes_unchanged_and_output_immutable(self):
        self.save()
        before = {p: a.sha256(p) for p in self.root.iterdir() if p.is_file()}
        r.build(self.manifest, self.root / "report")
        self.assertEqual(before, {p: a.sha256(p) for p in before})
        output = self.root / "report/index.html"
        digest = a.sha256(output)
        with self.assertRaisesRegex(ValueError, "already exists"):
            r.build(self.manifest, self.root / "report")
        self.assertEqual(a.sha256(output), digest)

    def test_render_failure_leaves_no_partial_report(self):
        self.save()
        with patch.object(r, "render", side_effect=ValueError("render failed")):
            with self.assertRaisesRegex(ValueError, "render failed"):
                r.build(self.manifest, self.root / "report")
        self.assertFalse((self.root / "report").exists())
        self.assertEqual(list(self.root.glob(".can-workbench-*")), [])

    def test_input_change_detected_before_publication(self):
        self.save()
        original = a.capture_report
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            with Path(args[0]).open("a") as f:
                f.write("\n")
            return result
        with patch.object(a, "capture_report", side_effect=changing):
            with self.assertRaisesRegex(ValueError, "input changed"):
                r.build(self.manifest, self.root / "report")
        self.assertFalse((self.root / "report").exists())

    def test_html_escapes_script_closing_user_text(self):
        self.config["title"] = '</script><script>throw Error("injected")</script>'
        self.build()
        html = (self.root / "report/index.html").read_text()
        self.assertNotIn(self.config["title"], html)
        self.assertIn("\\u003c/script>", html)
        self.assertIn("connect-src 'none'", html)

    def test_invalid_limits_geometry_and_selection(self):
        for kwargs in ({"max_points": 1}, {"gap": float("nan")},
                       {"window": ["2", "1"]}, {"field_names": ["unknown"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.build(**kwargs)
        self.config["fields"][0]["field"]["length_bits"] = 16
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.build()

    def test_nonfinite_reference_and_excessive_line_rejected(self):
        with self.assertRaises(ValueError):
            a.stamp("NaN")
        (self.root / "ramp.candump").write_text("x" * (a.MAX_LINE + 1))
        with self.assertRaisesRegex(ValueError, "line exceeds"):
            self.capture()

    def test_cli_failure_is_useful(self):
        proc = subprocess.run(["python3", str(r.REPO / "tools/can_workbench_report.py"),
                               str(self.manifest), "--field", "missing"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown or empty field", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_epoch_picoseconds_survive_float_summary_precision(self):
        (self.root / "ramp.candump").write_text(
            "(1782677541.000000000001) can0 123#02\n"
            "(1782677541.000000000002) can0 123#03\n")
        result = self.capture()
        self.assertEqual(result["series"]["ramp"]["points"][1]["seconds"], 1e-12)
        self.assertEqual(result["coverage"]["last_timestamp"], "1782677541.000000000002")

    def test_missing_reference_file_fails_without_publishing(self):
        (self.root / "reference.json").unlink()
        with self.assertRaises(OSError):
            self.build()
        self.assertFalse((self.root / "report").exists())

    def test_empty_candidate_list_and_invalid_stream(self):
        path = self.root / "correlation.json"
        report = a.load_json(path)
        report["ranking"]["candidates"][0]["can_id"] = None
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "namespace"):
            a.import_analysis(path)
        report["ranking"]["candidates"] = []
        path.write_text(json.dumps(report))
        self.assertEqual(a.import_analysis(path)["report"]["ranking"]["candidates"], [])

    def test_generated_default_location_is_ignored(self):
        proc = subprocess.run(["git", "check-ignore", "tmp/can_workbench/report/index.html"],
                              cwd=r.REPO, capture_output=True)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
