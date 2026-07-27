from __future__ import annotations

import contextlib
from collections import Counter
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools import alfaobd_singleton_infer as infer


def distribution(values):
    counts = Counter(values)
    return [
        {"value": value, "count": counts[value]}
        for value in sorted(counts)
    ]


def numeric_values(raw, slope, intercept, unit):
    return [f"{slope * value + intercept:.2f} {unit}" for value in raw]


def segment(label, did, raw, rendered, *, width=1, debug_exact=True):
    raw_hex = [f"{value:0{width * 2}X}" for value in raw]
    responses = [f"62{did}{value}" for value in raw_hex]
    count_match = len(raw_hex) == len(rendered)
    return {
        "sequence": None,
        "label": label,
        "event_interval": {"before_epoch": 1.0, "after_epoch": 2.0},
        "artifact_offset_witnesses": {},
        "info": {
            "alignment": "contiguous_exact_label_run_by_schedule",
            "run_ordinal": None,
            "sample_count": len(rendered),
            "rendered_values": list(rendered),
            "rendered_distribution": distribution(rendered),
        },
        "wire": {
            "matched": True,
            "status": "authoritative_host_timed_pairs",
            "request": f"22{did}",
            "did": f"0x{did}",
            "pair_count": len(responses),
            "message_count": len(responses) * 2,
            "tester_present_messages_discarded": 0,
            "first_timestamp": 1.1,
            "last_timestamp": 1.9,
            "responses": responses,
            "response_distribution": distribution(responses),
            "raw_data_distribution": distribution(raw_hex),
        },
        "info_wire_count_match": count_match,
        "candidate": {
            "classification": "candidate",
            "label": label,
            "request": f"22{did}",
            "did": f"0x{did}",
            "info_sample_count": len(rendered),
            "wire_pair_count": len(responses),
            "count_match": count_match,
            "rendered_values": list(rendered),
            "rendered_distribution": distribution(rendered),
            "wire_responses": responses,
            "wire_response_distribution": distribution(responses),
            "raw_data_distribution": distribution(raw_hex),
            "sample_pairing": "not_attempted_buffered_artifacts",
            "reasons": [],
        },
        "_debug_exact": debug_exact,
    }


def report_for(segments, *, anchors=()):
    schedule = [item["label"] for item in segments]
    debug_runs = []
    for sequence, item in enumerate(segments):
        item["sequence"] = sequence
        item["info"]["run_ordinal"] = sequence
        exact = item.pop("_debug_exact")
        boundary = sequence in (0, len(segments) - 1)
        minimum_overlap = (
            min(3, item["wire"]["pair_count"])
            if boundary
            else item["wire"]["pair_count"]
        )
        debug_runs.append(
            {
                "sequence": sequence,
                "request": item["candidate"]["request"],
                "did": item["candidate"]["did"],
                "wire_pair_count": item["wire"]["pair_count"],
                "debug_request_count": item["wire"]["pair_count"],
                "debug_response_count": item["wire"]["pair_count"],
                "retention_counts_compatible": True,
                "request_alignment": {
                    "status": "exact_full_run",
                    "minimum_required_overlap": minimum_overlap,
                    "wire_match_offsets": [0],
                },
                "response_alignment": {
                    "status": (
                        "exact_full_run"
                        if exact
                        else "clipped_contiguous_subset"
                    ),
                    "minimum_required_overlap": minimum_overlap,
                    "wire_match_offsets": [0],
                },
            }
        )
    anchor_checks = []
    for label in anchors:
        occurrences = [
            index for index, value in enumerate(schedule) if value == label
        ]
        first = segments[occurrences[0]]["candidate"]
        anchor_checks.append(
            {
                "label": label,
                "occurrences": occurrences,
                "request": first["request"],
                "did": first["did"],
                "consistent": True,
            }
        )
    return {
        "schema_version": 2,
        "classification": "candidate_only",
        "buffered_artifact_mode": {
            "enabled": True,
            "segment_timing_authority": "host_event_intervals_plus_passive_wire",
            "info_alignment": (
                "whole_outer_interval_contiguous_exact_label_runs_by_schedule"
            ),
            "debug_alignment": (
                "whole_outer_interval_independent_stream_corroboration"
            ),
            "info_wire_sample_count_equality_required": False,
        },
        "campaign": {
            "campaign_id": "synthetic-cluster-drive",
            "module_key": "cluster",
            "expected_runtime": "Instrument panel Continental",
            "schedule": schedule,
            "repeat_anchors": list(anchors),
            "provenance": {},
        },
        "module_addressing": {
            "addressing_mode": "normal_29bits",
            "request_can_id": "0x18DA60F1",
            "response_can_id": "0x18DAF160",
            "bus": "c-can",
            "bitrate": 500000,
        },
        "passive_capture": {},
        "info_artifact": {
            "role": "whole_campaign_rendered_label_runs",
            "run_labels": schedule,
            "run_sample_counts": [
                item["info"]["sample_count"] for item in segments
            ],
        },
        "debug_artifact": {
            "role": "whole_campaign_transport_corroboration_only",
            "corroboration": {
                "matched": True,
                "status": "whole_campaign_independent_streams_corroborated",
                "planned_did_run_order_exact": True,
                "sample_pairing": "not_attempted_buffered_artifacts",
                "runs": debug_runs,
            },
        },
        "segments": segments,
        "anchor_checks": anchor_checks,
        "summary": {
            "segments": len(segments),
            "candidate_segments": len(segments),
            "unresolved_segments": 0,
            "anchors_consistent": True,
            "wire_sequences_corroborated": len(segments),
        },
        "interpretation": "candidate only",
    }


class SingletonInferenceTests(unittest.TestCase):
    def settings(self):
        return infer.Settings(
            min_samples=8,
            min_distinct=3,
            max_lag=2,
            min_enum_observations=2,
            min_enum_transitions=2,
            top=5,
        )

    def full_report(self):
        engine_a = [
            520,
            893,
            1311,
            1789,
            2407,
            3191,
            4022,
            4789,
            3511,
            2693,
            1877,
            1013,
        ]
        speed_a = [0, 7, 19, 34, 52, 71, 63, 41, 22, 11, 3, 0]
        gear_raw = [0] * 4 + [3] * 4 + [0] * 4 + [3] * 4
        gear_values = ["P"] * 4 + ["D"] * 4 + ["P"] * 4 + ["D"] * 4
        temperature = [109, 111, 114, 118, 123, 119, 116, 112, 110, 115, 121, 117]
        battery_a = [
            118,
            119,
            121,
            120,
            123,
            122,
            119,
            118,
            120,
            121,
            123,
            119,
        ]
        battery_b = [
            119,
            120,
            122,
            121,
            124,
            123,
            120,
            119,
            121,
            122,
            124,
            120,
        ]
        speed_b = [0, 5, 17, 29, 46, 68, 77, 59, 37, 21, 9, 0]
        engine_b = [
            601,
            947,
            1429,
            1997,
            2633,
            3371,
            4219,
            4921,
            3697,
            2819,
            1601,
            829,
        ]
        return report_for(
            [
                segment(
                    "Battery Voltage (+30)",
                    "1004",
                    battery_a,
                    numeric_values(battery_a, 0.1, 0, "V")
                    + ["12.40 V"],
                ),
                segment(
                    "Engine speed",
                    "1000",
                    engine_a,
                    numeric_values(engine_a, 0.25, 0, "rpm"),
                    width=2,
                ),
                segment(
                    "Vehicle speed",
                    "1002",
                    speed_a,
                    numeric_values(speed_a, 1, 0, "km/h"),
                ),
                segment("Actual Gear", "0107", gear_raw, gear_values),
                segment(
                    "Outside temperature",
                    "1005",
                    temperature,
                    numeric_values(temperature, 0.5, -40, "°C"),
                ),
                segment(
                    "Engine speed",
                    "1000",
                    engine_b,
                    numeric_values(engine_b, 0.25, 0, "rpm"),
                    width=2,
                ),
                segment(
                    "Vehicle speed",
                    "1002",
                    speed_b,
                    numeric_values(speed_b, 1, 0, "km/h"),
                ),
                segment(
                    "Battery Voltage (+30)",
                    "1004",
                    battery_b,
                    numeric_values(battery_b[:-1], 0.1, 0, "V"),
                ),
            ],
            anchors=(
                "Engine speed",
                "Vehicle speed",
                "Battery Voltage (+30)",
            ),
        )

    def test_numeric_formulas_repeated_anchors_and_enum_are_candidate_only(self):
        result = infer.infer_report(
            self.full_report(),
            settings=self.settings(),
        )
        signals = {item["label"]: item for item in result["signals"]}

        self.assertEqual(result["verification_status"], "candidate_only")
        self.assertFalse(result["physical_verification"])
        self.assertFalse(result["promotion_allowed"])
        self.assertEqual(
            signals["Engine speed"]["evidence_grade"],
            "repeated_anchor_corroborated_candidate",
        )
        self.assertAlmostEqual(
            signals["Engine speed"]["selected"]["slope"], 0.25
        )
        self.assertEqual(
            signals["Vehicle speed"]["evidence_grade"],
            "repeated_anchor_corroborated_candidate",
        )
        self.assertAlmostEqual(
            signals["Vehicle speed"]["selected"]["slope"], 1.0
        )
        self.assertAlmostEqual(
            signals["Outside temperature"]["selected"]["slope"], 0.5
        )
        self.assertAlmostEqual(
            signals["Outside temperature"]["selected"]["intercept"], -40.0
        )
        self.assertIsNone(signals["Battery Voltage (+30)"]["selected"])
        self.assertEqual(
            signals["Battery Voltage (+30)"]["failure_reasons"],
            ["count_mismatch"],
        )
        self.assertEqual(
            [
                row["sequence"]
                for row in result["segments"]
                if row["label"] in {"Engine speed", "Vehicle speed"}
                and row["boundary_segment"]
            ],
            [],
        )
        self.assertTrue(result["segments"][0]["boundary_segment"])
        self.assertTrue(result["segments"][-1]["boundary_segment"])
        self.assertFalse(result["segments"][0]["info_wire_count_match"])
        self.assertFalse(result["segments"][-1]["info_wire_count_match"])
        gear = signals["Actual Gear"]["selected"]
        self.assertEqual(
            gear["evidence_grade"], "enum_partial_ordinal_candidate"
        )
        self.assertEqual(
            {
                row["raw_hex"]: row["rendered"]
                for row in gear["observed_mapping"]
            },
            {"00": "P", "03": "D"},
        )
        self.assertEqual(gear["valid_lags"], [0])
        self.assertFalse(gear["lag_ambiguous"])
        self.assertFalse(gear["complete_enum"])
        self.assertTrue(
            all(not signal["physical_scale_verified"] for signal in signals.values())
        )

    def test_boundary_segments_never_gain_sequence_pairing(self):
        values_a = [520, 893, 1311, 1789, 2407, 3191, 4022, 4789]
        values_b = [601, 947, 1429, 1997, 2633, 3371, 4219, 4921]
        report = report_for(
            [
                segment(
                    "Engine speed",
                    "1000",
                    values_a,
                    numeric_values(values_a, 0.25, 0, "rpm"),
                    width=2,
                ),
                segment(
                    "Engine speed",
                    "1000",
                    values_b,
                    numeric_values(values_b, 0.25, 0, "rpm"),
                    width=2,
                ),
            ],
            anchors=("Engine speed",),
        )
        result = infer.infer_report(
            report,
            settings=self.settings(),
            selected_labels={"Engine speed"},
        )
        self.assertEqual(len(result["segments"]), 2)
        for row in result["segments"]:
            self.assertTrue(row["boundary_segment"])
            self.assertFalse(row["analysis"]["sequence"]["eligible"])
            self.assertEqual(
                row["analysis"]["sequence"]["reason"],
                "outer_boundary_sequence_disallowed",
            )

    def test_gear_enum_finds_one_sample_lag_even_with_wide_lag_search(self):
        state = 7
        raw = []
        for _index in range(80):
            state = (state * 17 + 11) % 97
            raw.append(0 if state < 48 else 3)
        rendered = ["P"] + [
            "P" if value == 0 else "D" for value in raw[:-1]
        ]
        padding_raw = [1, 4, 8, 13, 19, 26, 34, 43, 53, 64, 76, 89]
        report = report_for(
            [
                segment(
                    "Vehicle speed",
                    "1002",
                    padding_raw,
                    numeric_values(padding_raw, 1, 0, "km/h"),
                ),
                segment("Actual Gear", "0107", raw, rendered),
                segment(
                    "Outside temperature",
                    "1005",
                    padding_raw,
                    numeric_values(padding_raw, 0.5, -40, "°C"),
                ),
            ]
        )
        settings = infer.Settings(
            min_samples=8,
            min_distinct=3,
            max_lag=32,
            min_enum_observations=2,
            min_enum_transitions=2,
            top=5,
        )
        result = infer.infer_report(
            report,
            settings=settings,
            selected_labels={"Actual Gear"},
        )
        selected = result["signals"][0]["selected"]
        self.assertEqual(selected["valid_lags"], [1])
        self.assertFalse(selected["lag_ambiguous"])
        self.assertEqual(
            {
                row["raw_hex"]: row["rendered"]
                for row in selected["observed_mapping"]
            },
            {"00": "P", "03": "D"},
        )

    def test_enum_override_accepts_mixed_numeric_and_alpha_labels(self):
        padding_raw = [1, 4, 8, 13, 19, 26, 34, 43, 53, 64, 76, 89]
        raw = [0] * 3 + [1] * 3 + [2] * 3 + [3] * 3
        rendered = ["P"] * 3 + ["1"] * 3 + ["2"] * 3 + ["D"] * 3
        report = report_for(
            [
                segment(
                    "Vehicle speed",
                    "1002",
                    padding_raw,
                    numeric_values(padding_raw, 1, 0, "km/h"),
                ),
                segment("Actual Gear", "0107", raw, rendered),
                segment(
                    "Outside temperature",
                    "1005",
                    padding_raw,
                    numeric_values(padding_raw, 0.5, -40, "°C"),
                ),
            ]
        )
        result = infer.infer_report(
            report,
            settings=self.settings(),
            selected_labels={"Actual Gear"},
            kind_overrides={"Actual Gear": "enum"},
        )
        selected = result["signals"][0]["selected"]
        self.assertIsNotNone(selected)
        self.assertEqual(
            {
                row["raw_hex"]: row["rendered"]
                for row in selected["observed_mapping"]
            },
            {"00": "P", "01": "1", "02": "2", "03": "D"},
        )

    def test_enum_override_accepts_numeric_only_labels(self):
        padding_raw = [1, 4, 8, 13, 19, 26, 34, 43, 53, 64, 76, 89]
        raw = [1] * 4 + [2] * 4 + [3] * 4
        rendered = ["1"] * 4 + ["2"] * 4 + ["3"] * 4
        report = report_for(
            [
                segment(
                    "Vehicle speed",
                    "1002",
                    padding_raw,
                    numeric_values(padding_raw, 1, 0, "km/h"),
                ),
                segment("Actual Gear", "0107", raw, rendered),
                segment(
                    "Outside temperature",
                    "1005",
                    padding_raw,
                    numeric_values(padding_raw, 0.5, -40, "°C"),
                ),
            ]
        )
        result = infer.infer_report(
            report,
            settings=self.settings(),
            selected_labels={"Actual Gear"},
            kind_overrides={"Actual Gear": "enum"},
        )
        selected = result["signals"][0]["selected"]
        self.assertIsNotNone(selected)
        self.assertEqual(
            {
                row["raw_hex"]: row["rendered"]
                for row in selected["observed_mapping"]
            },
            {"01": "1", "02": "2", "03": "3"},
        )

    def test_count_mismatch_is_not_truncated_or_normalized(self):
        raw = [1, 4, 9, 15, 22, 31, 43, 58, 76, 97, 121, 148]
        rendered = numeric_values(raw[:-1], 1, 0, "km/h")
        report = report_for(
            [segment("Vehicle speed", "1002", raw, rendered)]
        )
        result = infer.infer_report(report, settings=self.settings())
        signal = result["signals"][0]
        self.assertEqual(signal["evidence_grade"], "unidentifiable")
        self.assertIn("count_mismatch", signal["failure_reasons"])
        self.assertIsNone(signal["selected"])

    def test_repeated_anchor_conflict_suppresses_selected_formula(self):
        first = [401, 719, 1031, 1499, 2081, 2719, 3491, 4211, 2971, 1831, 997, 503]
        second = [449, 761, 1093, 1559, 2179, 2819, 3613, 4391, 3011, 1901, 1039, 557]
        report = report_for(
            [
                segment(
                    "Engine speed",
                    "1000",
                    first,
                    numeric_values(first, 0.25, 0, "rpm"),
                    width=2,
                ),
                segment(
                    "Engine speed",
                    "1000",
                    second,
                    numeric_values(second, 0.5, 0, "rpm"),
                    width=2,
                ),
            ],
            anchors=("Engine speed",),
        )
        result = infer.infer_report(report, settings=self.settings())
        signal = result["signals"][0]
        self.assertEqual(signal["evidence_grade"], "conflicting_evidence")
        self.assertEqual(signal["failure_reasons"], ["anchor_formula_conflict"])
        self.assertIsNone(signal["selected"])

    def test_one_nonaffine_anchor_suppresses_two_agreeing_occurrences(self):
        first = [401, 719, 1031, 1499, 2081, 2719, 3491, 4211, 2971, 1831, 997, 503]
        second = [449, 761, 1093, 1559, 2179, 2819, 3613, 4391, 3011, 1901, 1039, 557]
        third = [487, 823, 1193, 1667, 2269, 2927, 3733, 4519, 3169, 2011, 1129, 619]
        nonlinear = [
            f"{value:.2f} rpm"
            for value in (1, 4, 9, 16, 25, 36, 49, 64, 81, 100, 121, 144)
        ]
        report = report_for(
            [
                segment(
                    "Engine speed",
                    "1000",
                    first,
                    numeric_values(first, 0.25, 0, "rpm"),
                    width=2,
                ),
                segment(
                    "Engine speed",
                    "1000",
                    second,
                    numeric_values(second, 0.25, 0, "rpm"),
                    width=2,
                ),
                segment(
                    "Engine speed",
                    "1000",
                    third,
                    nonlinear,
                    width=2,
                ),
            ],
            anchors=("Engine speed",),
        )
        result = infer.infer_report(report, settings=self.settings())
        signal = result["signals"][0]
        self.assertEqual(signal["evidence_grade"], "conflicting_evidence")
        self.assertEqual(signal["conflicting_occurrences"], [2])
        self.assertIsNone(signal["selected"])

    def test_single_gear_state_is_only_a_partial_observation(self):
        padding = segment(
            "Vehicle speed",
            "1002",
            list(range(12)),
            numeric_values(list(range(12)), 1, 0, "km/h"),
        )
        gear = segment("Actual Gear", "0107", [0] * 12, ["P"] * 12)
        report = report_for(
            [
                padding,
                gear,
                segment(
                    "Outside temperature",
                    "1005",
                    [90, 92, 95, 99, 104, 110, 107, 102, 97, 94, 91, 93],
                    numeric_values(
                        [90, 92, 95, 99, 104, 110, 107, 102, 97, 94, 91, 93],
                        0.5,
                        -40,
                        "°C",
                    ),
                ),
            ]
        )
        result = infer.infer_report(
            report,
            settings=self.settings(),
            selected_labels={"Actual Gear"},
        )
        signal = result["signals"][0]
        self.assertEqual(
            signal["evidence_grade"], "single_state_partial_observation"
        )
        self.assertEqual(
            signal["selected"]["observed_mapping"],
            [{"raw_hex": "00", "rendered": "P"}],
        )

    def test_repeated_enum_anchor_is_not_silently_consolidated(self):
        padding_raw = [1, 4, 8, 13, 19, 26, 34, 43, 53, 64, 76, 89]
        gear_raw = [0] * 4 + [3] * 4 + [0] * 4 + [3] * 4
        report = report_for(
            [
                segment(
                    "Vehicle speed",
                    "1002",
                    padding_raw,
                    numeric_values(padding_raw, 1, 0, "km/h"),
                ),
                segment(
                    "Actual Gear",
                    "0107",
                    gear_raw,
                    ["P"] * 4 + ["D"] * 4 + ["P"] * 4 + ["D"] * 4,
                ),
                segment(
                    "Actual Gear",
                    "0107",
                    gear_raw,
                    ["R"] * 4 + ["N"] * 4 + ["R"] * 4 + ["N"] * 4,
                ),
                segment(
                    "Outside temperature",
                    "1005",
                    padding_raw,
                    numeric_values(padding_raw, 0.5, -40, "°C"),
                ),
            ],
            anchors=("Actual Gear",),
        )
        result = infer.infer_report(
            report,
            settings=self.settings(),
            selected_labels={"Actual Gear"},
        )
        signal = result["signals"][0]
        self.assertEqual(signal["evidence_grade"], "unidentifiable")
        self.assertIsNone(signal["selected"])
        self.assertEqual(
            signal["failure_reasons"],
            ["repeated_nonnumeric_anchor_consolidation_not_supported"],
        )

    def test_enum_state_space_is_bounded_before_lag_materialization(self):
        padding_raw = [1, 4, 8, 13, 19, 26, 34, 43, 53, 64, 76, 89]
        enum_raw = list(range(infer.DEFAULT_MAX_ENUM_STATES + 1)) * 2
        enum_values = [
            f"State-{value:02d}"
            for value in range(infer.DEFAULT_MAX_ENUM_STATES + 1)
        ] * 2
        report = report_for(
            [
                segment(
                    "Vehicle speed",
                    "1002",
                    padding_raw,
                    numeric_values(padding_raw, 1, 0, "km/h"),
                ),
                segment("Actual Gear", "0107", enum_raw, enum_values),
                segment(
                    "Outside temperature",
                    "1005",
                    padding_raw,
                    numeric_values(padding_raw, 0.5, -40, "°C"),
                ),
            ]
        )
        result = infer.infer_report(
            report,
            settings=self.settings(),
            selected_labels={"Actual Gear"},
        )
        signal = result["signals"][0]
        self.assertIsNone(signal["selected"])
        self.assertEqual(signal["failure_reasons"], ["enum_state_cap_exceeded"])

    def test_malformed_echo_distribution_and_noncluster_reports_fail_closed(self):
        report = self.full_report()
        report["segments"][1]["candidate"]["wire_responses"][0] = "62100300"
        with self.assertRaisesRegex(
            infer.InferenceError, "non-exact positive response|response order differs"
        ):
            infer.infer_report(report, settings=self.settings())

        report = self.full_report()
        report["segments"][1]["candidate"]["rendered_distribution"][0][
            "count"
        ] += 1
        with self.assertRaisesRegex(infer.InferenceError, "does not match"):
            infer.infer_report(report, settings=self.settings())

        report = self.full_report()
        report["campaign"]["module_key"] = "radar_acc"
        with self.assertRaisesRegex(infer.InferenceError, "restricted"):
            infer.infer_report(report, settings=self.settings())

        report = self.full_report()
        report["module_addressing"]["addressing_mode"] = "extended"
        with self.assertRaisesRegex(infer.InferenceError, "addressing_mode"):
            infer.infer_report(report, settings=self.settings())

        report = self.full_report()
        report["debug_artifact"]["corroboration"]["runs"][1][
            "request_alignment"
        ]["status"] = "bogus"
        with self.assertRaisesRegex(infer.InferenceError, "status is unsupported"):
            infer.infer_report(report, settings=self.settings())

        report = self.full_report()
        report["segments"][0]["sequence"] = False
        with self.assertRaisesRegex(infer.InferenceError, "must be an integer"):
            infer.infer_report(report, settings=self.settings())

        with self.assertRaisesRegex(infer.InferenceError, "fixed safety cap"):
            infer.infer_report(
                self.full_report(),
                settings=self.settings(),
                limits=infer.Limits(
                    max_segments=infer.DEFAULT_MAX_SEGMENTS + 1
                ),
            )

    def test_cli_hashes_input_refuses_overwrite_and_never_claims_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "joined.json"
            output = root / "inferred.json"
            source.write_text(
                json.dumps(self.full_report(), sort_keys=True),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                first = infer.main(
                    [
                        str(source),
                        "--output",
                        str(output),
                        "--min-samples",
                        "8",
                        "--min-enum-observations",
                        "2",
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))
            original = output.read_bytes()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                second = infer.main(
                    [
                        str(source),
                        "--output",
                        str(output),
                        "--min-samples",
                        "8",
                        "--min-enum-observations",
                        "2",
                    ]
                )
            after_second = output.read_bytes()

        self.assertEqual(first, 0)
        self.assertEqual(second, 2)
        self.assertIn("refusing to overwrite", stderr.getvalue())
        self.assertEqual(original, after_second)
        self.assertRegex(payload["input_report"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(payload["physical_verification"])
        self.assertFalse(payload["promotion_allowed"])
        self.assertIn("Physical verification: NO", stdout.getvalue())

    def test_cli_kind_override_preserves_mixed_actual_gear_labels(self):
        padding_raw = [1, 4, 8, 13, 19, 26, 34, 43, 53, 64, 76, 89]
        gear_raw = [0] * 3 + [1] * 3 + [2] * 3 + [3] * 3
        gear_values = ["P"] * 3 + ["1"] * 3 + ["2"] * 3 + ["D"] * 3
        report = report_for(
            [
                segment(
                    "Vehicle speed",
                    "1002",
                    padding_raw,
                    numeric_values(padding_raw, 1, 0, "km/h"),
                ),
                segment("Actual Gear", "0107", gear_raw, gear_values),
                segment(
                    "Outside temperature",
                    "1005",
                    padding_raw,
                    numeric_values(padding_raw, 0.5, -40, "°C"),
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "joined.json"
            output = root / "inferred.json"
            source.write_text(json.dumps(report), encoding="utf-8")

            result = infer.main(
                [
                    str(source),
                    "--output",
                    str(output),
                    "--kind",
                    "Actual Gear=enum",
                    "--min-samples",
                    "8",
                    "--min-enum-observations",
                    "2",
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        gear = next(
            signal
            for signal in payload["signals"]
            if signal["label"] == "Actual Gear"
        )
        self.assertEqual(
            {
                row["raw_hex"]: row["rendered"]
                for row in gear["selected"]["observed_mapping"]
            },
            {"00": "P", "01": "1", "02": "2", "03": "D"},
        )


if __name__ == "__main__":
    unittest.main()
