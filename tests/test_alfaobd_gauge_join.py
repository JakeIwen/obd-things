import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from projects.ecu_mapping.alfalog import iter_exchanges, iter_exchanges_detailed
from tools import alfaobd_gauge_join as joiner


PROFILE = "Synthetic Engine Profile"


def encoded(text):
    return text.encode("latin-1").hex().upper()


def exchange(request_ts, response_ts, request, response):
    return (
        f"{request_ts} S: {encoded(request + chr(13))}\n"
        f"{response_ts} R: {encoded(response + chr(13) + '>')}\n"
    )


def fixture_texts():
    raw_values = [10, 20, 30, 40, 50, 60, 70]
    debug = [
        f"Recording data for {PROFILE}\n",
        f"09:59:59.000 S: {encoded('ATSH DA10F1' + chr(13))}\n",
        f"09:59:59.010 R: {encoded('OK' + chr(13) + '>')}\n",
        exchange("09:59:59.100", "09:59:59.150", "22F190", "62F19001"),
        exchange("09:59:59.200", "09:59:59.250", "22F187", "62F18702"),
    ]
    gauge = [
        f"Recording data for {PROFILE}\n",
        "Date (YY/MM/DD): 24/01/02\n",
        "Time,Scaled value,Constant,\n",
    ]
    distractor = [3, 8, 2, 9, 4, 12, 1]
    for index, raw in enumerate(raw_values):
        second = index
        positive = f"62ABCD{raw:02X}"
        if index == 3:
            positive = "7F2278\r" + positive
        debug.append(
            exchange(
                f"10:00:{second:02d}.000",
                f"10:00:{second:02d}.100",
                "22ABCD",
                positive,
            )
        )
        debug.append(
            exchange(
                f"10:00:{second:02d}.200",
                f"10:00:{second:02d}.300",
                "22EEEE",
                f"62EEEE{distractor[index]:02X}",
            )
        )
        # A response with the wrong echoed DID must never become a payload candidate.
        debug.append(
            exchange(
                f"10:00:{second:02d}.400",
                f"10:00:{second:02d}.500",
                "22BEEF",
                f"7F2231\r62BEEF{raw:02X}",
            )
        )
        if index > 0:
            previous = raw_values[index - 1]
            gauge.append(f"10:00:{second:02d}.100,{2 * previous - 40:.3f},7.000,\n")
    debug.append("Recording closed 2024/01/02 10:00:07.600\n")
    return "".join(gauge), "".join(debug)


class DetailedExchangeTests(unittest.TestCase):
    def test_response_end_provenance_is_added_without_changing_legacy_shape(self):
        _, debug = fixture_texts()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "debug.txt"
            path.write_text(debug, encoding="latin-1")
            detailed = [item for item in iter_exchanges_detailed(path) if item["req"] == "22ABCD"]
            legacy = [item for item in iter_exchanges(path) if item["req"] == "22ABCD"]

        self.assertEqual(detailed[1]["request_ts"], "10:00:01.000")
        self.assertEqual(detailed[1]["response_end_ts"], "10:00:01.100")
        self.assertEqual(detailed[1]["completion_reason"], "prompt")
        self.assertTrue(detailed[1]["prompt_seen"])
        self.assertLess(detailed[1]["request_line"], detailed[1]["response_end_line"])
        self.assertEqual(
            set(legacy[0]),
            {"ts", "date", "addr", "module", "req", "resp"},
        )
        self.assertEqual(legacy[0]["ts"], detailed[0]["request_ts"])

    def test_detailed_exchange_distinguishes_next_request_from_prompt_completion(self):
        debug = (
            f"Recording data for {PROFILE}\n"
            + exchange("10:00:00.000", "10:00:00.100", "22ABCD", "62ABCD01").replace(
                encoded(chr(13) + ">"), encoded(chr(13))
            )
            + exchange("10:00:00.200", "10:00:00.300", "22EEEE", "62EEEE02")
            + "Recording closed 2024/01/02 10:00:00.400\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "debug.txt"
            path.write_text(debug, encoding="latin-1")
            detailed = list(iter_exchanges_detailed(path))

        self.assertEqual(detailed[0]["completion_reason"], "next_request")
        self.assertFalse(detailed[0]["prompt_seen"])
        self.assertEqual(detailed[1]["completion_reason"], "prompt")


class JoinTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        gauge, debug = fixture_texts()
        self.gauge_path = self.root / "Gauges_Data.csv"
        self.debug_path = self.root / "debug.txt"
        self.gauge_path.write_text(gauge, encoding="utf-8")
        self.debug_path.write_text(debug, encoding="latin-1")

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_echo_cycles_and_preceding_cycle_lag_recover_affine_mapping(self):
        section = next(joiner.iter_gauge_sections(self.gauge_path))
        exchanges = list(
            joiner.iter_did_exchanges(
                self.debug_path,
                date=section.date,
                profile=section.profile,
                address="atsh da10f1",
            )
        )
        inference = joiner.infer_boundary_did(exchanges, section.rows)
        self.assertEqual(inference["did"], "ABCD")
        cycles = joiner.build_cycles(exchanges, boundary_did=inference["did"])
        self.assertEqual(len(joiner.build_cycles(exchanges)), 7)
        alignments = joiner.align_rows(section.rows, cycles)
        result = joiner.fit_metric(
            section,
            1,
            alignments,
            cycles,
            min_samples=6,
            source_scope="historical-other-vehicle",
        )

        self.assertEqual(len(cycles), 7)
        duplicate_index = next(index for index, item in enumerate(exchanges) if item.did == "EEEE")
        duplicate_nonboundary = (
            exchanges[:duplicate_index + 1]
            + [exchanges[duplicate_index]]
            + exchanges[duplicate_index + 1:]
        )
        self.assertEqual(
            len(joiner.build_cycles(duplicate_nonboundary, boundary_did="ABCD")), 7
        )
        self.assertTrue(all(item.offset_ms == 0 for item in alignments))
        reversed_alignments = joiner.align_rows(section.rows, list(reversed(cycles)))
        self.assertTrue(all(item.offset_ms == 0 for item in reversed_alignments))
        duplicate_anchor = joiner.align_rows([section.rows[0]], [cycles[1], cycles[1]])[0]
        self.assertTrue(duplicate_anchor.ambiguous_time)
        self.assertTrue(all(item.payload is None for item in exchanges if item.did == "BEEF"))
        pending = [item for item in exchanges if item.did == "ABCD" and item.pending_count]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].pending_count, 1)
        best = result["candidates"][0]
        self.assertEqual(best["did"], "ABCD")
        self.assertEqual(best["cycle_lag"], -1)
        self.assertAlmostEqual(best["slope"], 2.0)
        self.assertAlmostEqual(best["intercept"], -40.0)
        self.assertTrue(best["exact_to_0_001"])
        self.assertIn("historical_reference_candidate", result["status"])

    def test_constant_column_is_explicitly_unidentifiable(self):
        section = next(joiner.iter_gauge_sections(self.gauge_path))
        exchanges = list(
            joiner.iter_did_exchanges(
                self.debug_path, date=section.date, profile=section.profile
            )
        )
        cycles = joiner.build_cycles(exchanges, boundary_did="ABCD")
        result = joiner.fit_metric(
            section, 2, joiner.align_rows(section.rows, cycles), cycles, min_samples=6
        )

        self.assertEqual(result["status"], "unidentifiable")
        self.assertIn("three varying", result["reason"])

    def test_two_value_series_is_not_accepted_as_an_affine_fit(self):
        section = next(joiner.iter_gauge_sections(self.gauge_path))
        for index, row in enumerate(section.rows):
            row.fields[1] = str(index % 2)
        exchanges = list(
            joiner.iter_did_exchanges(
                self.debug_path, date=section.date, profile=section.profile
            )
        )
        cycles = joiner.build_cycles(exchanges)
        result = joiner.fit_metric(
            section, 1, joiner.align_rows(section.rows, cycles), cycles, min_samples=6
        )

        self.assertEqual(result["status"], "unidentifiable")
        self.assertEqual(result["distinct_display_values"], 2)

    def test_cli_preserves_rows_and_emits_candidate_only_provenance(self):
        output = self.root / "output"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = joiner.main(
                [
                    str(self.gauge_path),
                    str(self.debug_path),
                    "--section", "1",
                    "--address", "DA10F1",
                    "--metric", "Scaled value",
                    "--source-scope", "historical-other-vehicle",
                    "--out-dir", str(output),
                ]
            )

        self.assertEqual(status, 0)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["verification_status"], "candidate_only")
        self.assertEqual(report["source_scope"], "historical-other-vehicle")
        self.assertEqual(report["polling"]["cycle_boundary"], "ABCD")
        self.assertEqual(
            report["polling"]["cycle_boundary_source"],
            "gauge_response_timestamp_inference",
        )
        self.assertEqual(report["polling"]["unassigned_startup_or_partial_exchanges"], 2)
        rows = [json.loads(line) for line in (output / "gauge_rows.jsonl").read_text().splitlines()]
        cycles = [json.loads(line) for line in (output / "cycles.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["fields"], ["10:00:01.100", "-20.000", "7.000"])
        self.assertEqual(len(cycles), 7)

    def test_cli_bounds_matching_exchange_retention(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = joiner.main(
                [
                    str(self.gauge_path),
                    str(self.debug_path),
                    "--section", "1",
                    "--address", "DA10F1",
                    "--max-exchanges", "2",
                ]
            )

        self.assertEqual(status, 2)
        self.assertIn("exceed --max-exchanges 2", stderr.getvalue())

    def test_cli_requires_boundary_override_when_timestamps_cannot_infer_one(self):
        shifted = self.root / "shifted.csv"
        shifted.write_text(
            self.gauge_path.read_text(encoding="utf-8").replace("10:00:", "11:00:"),
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = joiner.main(
                [
                    str(shifted),
                    str(self.debug_path),
                    "--section", "1",
                    "--address", "DA10F1",
                ]
            )

        self.assertEqual(status, 2)
        self.assertIn("--boundary-did", stderr.getvalue())

    def test_cli_bounds_candidate_hypotheses(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = joiner.main(
                [
                    str(self.gauge_path),
                    str(self.debug_path),
                    "--section", "1",
                    "--address", "DA10F1",
                    "--metric", "Scaled value",
                    "--max-hypotheses-per-metric", "1",
                ]
            )

        self.assertEqual(status, 2)
        self.assertIn("candidate hypotheses exceed 1", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
