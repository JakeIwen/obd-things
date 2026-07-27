import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from projects.ecu_mapping import alfalog
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


def local_identifier_fixture_texts(delimiter=";"):
    raw_values = [10, 20, 30, 40, 50, 60, 70]
    distractor = [3, 8, 2, 9, 4, 12, 1]
    debug = [
        f"Recording data for {PROFILE}\n",
        f"09:59:59.000 S: {encoded('ATSH DA10F1' + chr(13))}\n",
        f"09:59:59.010 R: {encoded('OK' + chr(13) + '>')}\n",
    ]
    gauge = [
        f"Recording data for {PROFILE}\n",
        "Date (YY/MM/DD): 24/01/02\n",
        delimiter.join(("Time", "Local scaled", "")) + "\n",
    ]
    for index, raw in enumerate(raw_values):
        second = index
        positive = f"61A1{raw:02X}"
        if index == 3:
            positive = "7F2178\r" + positive
        debug.append(
            exchange(
                f"10:00:{second:02d}.000",
                f"10:00:{second:02d}.100",
                "21A1",
                positive,
            )
        )
        # Numerically similar identifiers in different services must remain
        # distinct namespaces.
        debug.append(
            exchange(
                f"10:00:{second:02d}.200",
                f"10:00:{second:02d}.300",
                "2200A1",
                f"6200A1{distractor[index]:02X}",
            )
        )
        # A wrong local-identifier echo is retained as provenance but never as
        # a usable payload.
        debug.append(
            exchange(
                f"10:00:{second:02d}.400",
                f"10:00:{second:02d}.500",
                "21B2",
                f"61B3{raw:02X}",
            )
        )
        gauge.append(
            delimiter.join(
                (
                    f"10:00:{second:02d}.100",
                    f"{3 * raw - 5:.3f}",
                    "",
                )
            )
            + "\n"
        )
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
            diagnostic = list(
                joiner.iter_diagnostic_exchanges(
                    path,
                    date="2024-01-02",
                    profile=PROFILE,
                )
            )

        self.assertEqual(detailed[0]["completion_reason"], "next_request")
        self.assertFalse(detailed[0]["prompt_seen"])
        self.assertEqual(detailed[1]["completion_reason"], "prompt")
        self.assertIsNone(diagnostic[0].payload)
        self.assertEqual(diagnostic[1].payload, b"\x02")

    def test_fragmented_indexed_response_is_reassembled_and_length_trimmed(self):
        rows = [
            "61EA0001C000",
            "402E085B264103",
            "C0800000000000",
            "00000000000000",
            "010000004C3000",
            "002000040A0800",
            "00100884000000",
            "00000000000000",
            "00000000000000",
        ]
        debug = (
            f"Recording data for {PROFILE}\n"
            # Exact callback boundaries/timestamps from the current-van July
            # 22 trace, with indexed row 1 divided between two R records.
            f"00:20:52.870 S: {encoded('21EA' + chr(13))}\n"
            f"00:20:52.929 R: {encoded('03B' + chr(13) + '0:' + rows[0] + chr(13))}\n"
            f"00:20:52.932 R: {encoded('1:402E085B2')}\n"
            f"00:20:52.933 R: {encoded('64103' + chr(13))}\n"
            f"00:20:52.937 R: {encoded('2:' + rows[2] + chr(13))}\n"
            f"00:20:53.034 R: {encoded(''.join(f'{index:X}:{row}' + chr(13) for index, row in enumerate(rows[3:], 3)) + chr(13) + '>')}\n"
            "Recording closed 2024/01/02 00:20:53.040\n"
        )
        expected = "".join(rows)[:0x03B * 2]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "debug.txt"
            path.write_text(debug, encoding="latin-1")
            detailed = list(iter_exchanges_detailed(path))

        self.assertEqual(len(detailed), 1)
        self.assertEqual(detailed[0]["req"], "21EA")
        self.assertEqual(detailed[0]["resp"], expected)
        self.assertEqual(len(detailed[0]["resp"]), 0x03B * 2)
        self.assertEqual(len(detailed[0]["resp"]) % 2, 0)
        self.assertEqual(detailed[0]["completion_reason"], "prompt")
        self.assertEqual(detailed[0]["response_end_ts"], "00:20:53.034")
        self.assertEqual(detailed[0]["response_end_line"], 7)

    def test_malformed_indexed_response_fails_closed(self):
        debug = (
            f"Recording data for {PROFILE}\n"
            f"10:00:00.000 S: {encoded('22ABCD' + chr(13))}\n"
            f"10:00:00.050 R: {encoded('00D' + chr(13) + '0:62ABCD0102' + chr(13) + '2:03040506070809' + chr(13) + '>')}\n"
            "Recording closed 2024/01/02 10:00:00.080\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "debug.txt"
            path.write_text(debug, encoding="latin-1")
            detailed = list(iter_exchanges_detailed(path))

        self.assertEqual(len(detailed), 1)
        self.assertEqual(detailed[0]["resp"], "")
        self.assertEqual(detailed[0]["completion_reason"], "prompt")

    def test_response_buffer_overflow_fails_closed_at_prompt(self):
        debug = (
            f"Recording data for {PROFILE}\n"
            f"10:00:00.000 S: {encoded('22ABCD' + chr(13))}\n"
            f"10:00:00.050 R: {encoded('62ABCD' + '01' * 20 + chr(13) + '>')}\n"
            "Recording closed 2024/01/02 10:00:00.080\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "debug.txt"
            path.write_text(debug, encoding="latin-1")
            with mock.patch.object(
                alfalog,
                "MAX_RESPONSE_BUFFER_CHARS",
                16,
            ):
                detailed = list(iter_exchanges_detailed(path))

        self.assertEqual(len(detailed), 1)
        self.assertTrue(detailed[0]["response_buffer_overflow"])
        self.assertEqual(detailed[0]["resp"], "")
        self.assertEqual(detailed[0]["completion_reason"], "prompt")

    def test_response_pending_can_precede_segmented_positive_in_one_prompt(self):
        rows = [
            "62ABCD010203",
            "0405060708090A",
        ]
        debug = (
            f"Recording data for {PROFILE}\n"
            f"10:00:00.000 S: {encoded('22ABCD' + chr(13))}\n"
            f"10:00:00.050 R: {encoded('7F2278' + chr(13) + '00D' + chr(13) + '0:' + rows[0] + chr(13) + '1:' + rows[1] + chr(13) + '>')}\n"
            "Recording closed 2024/01/02 10:00:00.080\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "debug.txt"
            path.write_text(debug, encoding="latin-1")
            detailed = list(iter_exchanges_detailed(path))
            diagnostic = list(
                joiner.iter_diagnostic_exchanges(
                    path,
                    date="2024-01-02",
                    profile=PROFILE,
                )
            )

        self.assertEqual(
            detailed[0]["resp"],
            "7F2278" + "".join(rows)[:0x00D * 2],
        )
        self.assertEqual(diagnostic[0].pending_count, 1)
        self.assertEqual(diagnostic[0].payload, bytes.fromhex("0102030405060708090A"))


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

    def test_legacy_21_61_local_identifier_is_distinct_and_fittable(self):
        gauge, debug = local_identifier_fixture_texts()
        gauge_path = self.root / "Local_Gauges_Data.csv"
        debug_path = self.root / "local_debug.txt"
        gauge_path.write_text(gauge, encoding="utf-8")
        debug_path.write_text(debug, encoding="latin-1")

        section = next(joiner.iter_gauge_sections(gauge_path))
        exchanges = list(
            joiner.iter_diagnostic_exchanges(
                debug_path,
                date=section.date,
                profile=section.profile,
                address="DA10F1",
            )
        )
        inference = joiner.infer_boundary_identifier(
            exchanges,
            section.rows,
        )
        cycles = joiner.build_cycles(
            exchanges,
            boundary_identifier=inference["identifier_key"],
        )
        result = joiner.fit_metric(
            section,
            1,
            joiner.align_rows(section.rows, cycles),
            cycles,
            allowed_identifiers={"21:A1"},
            min_samples=6,
            source_scope="current-van",
        )

        self.assertEqual(section.delimiter, ";")
        self.assertEqual(inference["identifier_key"], "21:A1")
        self.assertEqual(inference["local_identifier"], "A1")
        self.assertIsNone(inference["did"])
        self.assertIn("21:A1", {item.identifier_key for item in exchanges})
        self.assertIn("22:00A1", {item.identifier_key for item in exchanges})
        wrong_echoes = [
            item
            for item in exchanges
            if item.identifier_key == "21:B2"
        ]
        self.assertTrue(wrong_echoes)
        self.assertTrue(all(item.payload is None for item in wrong_echoes))
        pending = [
            item
            for item in exchanges
            if item.identifier_key == "21:A1" and item.pending_count
        ]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].pending_count, 1)
        best = result["candidates"][0]
        self.assertEqual(best["key"], "21:A1")
        self.assertEqual(best["kind"], "local_identifier")
        self.assertEqual(best["local_identifier"], "A1")
        self.assertIsNone(best["did"])
        self.assertAlmostEqual(best["slope"], 3.0)
        self.assertAlmostEqual(best["intercept"], -5.0)

    def test_tab_delimited_gauges_section_is_parsed(self):
        gauge, _debug = local_identifier_fixture_texts(delimiter="\t")
        path = self.root / "tab_Gauges_Data.csv"
        path.write_text(gauge, encoding="utf-8")

        section = next(joiner.iter_gauge_sections(path))

        self.assertEqual(section.delimiter, "\t")
        self.assertEqual(section.columns, ["Time", "Local scaled"])
        self.assertEqual(len(section.rows), 7)

    def test_cli_reports_local_identifier_namespace(self):
        gauge, debug = local_identifier_fixture_texts()
        gauge_path = self.root / "Local_Gauges_Data.csv"
        debug_path = self.root / "local_debug.txt"
        output = self.root / "local-output"
        gauge_path.write_text(gauge, encoding="utf-8")
        debug_path.write_text(debug, encoding="latin-1")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = joiner.main(
                [
                    str(gauge_path),
                    str(debug_path),
                    "--section",
                    "1",
                    "--address",
                    "DA10F1",
                    "--metric",
                    "Local scaled",
                    "--local-id",
                    "A1",
                    "--source-scope",
                    "current-van",
                    "--out-dir",
                    str(output),
                ]
            )

        self.assertEqual(status, 0)
        report = json.loads(
            (output / "report.json").read_text(encoding="utf-8")
        )
        candidate = report["metrics"][0]["candidates"][0]
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(
            report["polling"]["cycle_boundary_key"],
            "21:A1",
        )
        self.assertIsNone(report["polling"]["cycle_boundary"])
        self.assertEqual(
            report["debug_filter"]["allowed_identifier_keys"],
            ["21:A1"],
        )
        self.assertEqual(report["polling"]["did_exchanges"], 7)
        self.assertEqual(
            report["polling"]["local_identifier_exchanges"],
            14,
        )
        self.assertEqual(candidate["kind"], "local_identifier")
        self.assertEqual(candidate["local_identifier"], "A1")
        self.assertIsNone(candidate["did"])
        self.assertIn("local identifier A1", stdout.getvalue())

    def test_cli_rejects_unobserved_local_boundary_override(self):
        gauge, debug = local_identifier_fixture_texts()
        gauge_path = self.root / "Local_Gauges_Data.csv"
        debug_path = self.root / "local_debug.txt"
        gauge_path.write_text(gauge, encoding="utf-8")
        debug_path.write_text(debug, encoding="latin-1")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = joiner.main(
                [
                    str(gauge_path),
                    str(debug_path),
                    "--section",
                    "1",
                    "--address",
                    "DA10F1",
                    "--boundary-local-id",
                    "FF",
                ]
            )

        self.assertEqual(status, 2)
        self.assertIn("21:FF does not occur", stderr.getvalue())

    def test_cli_rejects_unobserved_did_boundary_override(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = joiner.main(
                [
                    str(self.gauge_path),
                    str(self.debug_path),
                    "--section",
                    "1",
                    "--address",
                    "DA10F1",
                    "--boundary-did",
                    "FFFF",
                ]
            )

        self.assertEqual(status, 2)
        self.assertIn("22:FFFF does not occur", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
