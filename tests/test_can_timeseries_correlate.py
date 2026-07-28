import argparse
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import can_timeseries_correlate as correlate


def wire_row(
    timestamp_us,
    did,
    data,
    *,
    raw_line_sequence=0,
    can_id=0x18DAF160,
):
    payload = bytes((0x62, did >> 8, did & 0xFF)) + bytes(data)
    can_data = bytes((len(payload),)) + payload
    return {
        "schema_version": 1,
        "type": "wire_frame",
        "raw_line_sequence": raw_line_sequence,
        "timestamp_epoch_us": timestamp_us,
        "timestamp_source": "candump_kernel",
        "can_id": f"{can_id:X}",
        "direction": "cluster_to_tester",
        "can_data_hex": can_data.hex(" ").upper(),
        "classification": "exact_positive_response",
        "did": f"{did:04X}",
        "isotp_payload_hex": payload.hex(" ").upper(),
    }


def candump(timestamp_us, can_id, payload, *, extended=False):
    timestamp = f"{timestamp_us / 1_000_000:.6f}"
    identifier = f"{can_id:08X}" if extended else f"{can_id:03X}"
    return f"({timestamp}) can0 {identifier}#{bytes(payload).hex().upper()}\n"


def linked_evidence(samples, candidate_frames):
    """Return exact linked wire rows and the global synthetic candump stream."""
    entries = []
    order = 0
    for timestamp, can_id, payload, extended in candidate_frames:
        entries.append(
            (timestamp, order, can_id, bytes(payload), extended, None)
        )
        order += 1
    for sample_index, (timestamp, did, data) in enumerate(samples):
        isotp_payload = bytes((0x62, did >> 8, did & 0xFF)) + bytes(data)
        can_data = bytes((len(isotp_payload),)) + isotp_payload
        entries.append(
            (
                timestamp,
                order,
                0x18DAF160,
                can_data,
                True,
                sample_index,
            )
        )
        order += 1
    entries.sort(key=lambda item: (item[0], item[1]))
    rows_by_index = {}
    lines = []
    for raw_sequence, (
        timestamp,
        _,
        can_id,
        payload,
        extended,
        sample_index,
    ) in enumerate(entries):
        lines.append(candump(timestamp, can_id, payload, extended=extended))
        if sample_index is not None:
            _, did, data = samples[sample_index]
            rows_by_index[sample_index] = wire_row(
                timestamp,
                did,
                data,
                raw_line_sequence=raw_sequence,
                can_id=can_id,
            )
    return [rows_by_index[index] for index in range(len(samples))], lines


class EvidenceFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.wire = self.root / "cluster_wire.jsonl"
        self.capture = self.root / "capture.candump"

    def write_wire(self, rows):
        self.wire.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def write_capture(self, lines):
        self.capture.write_text("".join(lines), encoding="ascii")


def candidate(report, can_id, kind, offset):
    return next(
        row
        for row in report["ranking"]["candidates"]
        if row["can_id"] == can_id
        and row["field"]["kind"] == kind
        and row["field"]["offset"] == offset
    )


def bit_candidate(report, can_id, id_bits, start, length, order, signed):
    return next(
        row
        for row in report["ranking"]["candidates"]
        if row["can_id"] == can_id
        and row["id_bits"] == id_bits
        and row["field"].get("dbc_start_bit") == start
        and row["field"].get("length_bits") == length
        and row["field"]["byte_order"] == order
        and row["field"]["signed"] is signed
    )


class NearestCorrelationTests(unittest.TestCase):
    def test_nearest_affine_fit_and_default_identifier_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(directory)
            samples = []
            candidate_frames = []
            for index, x_value in enumerate((2, 4, 7, 9, 12), 1):
                timestamp = index * 1_000_000
                samples.append((timestamp, 0x1000, [2 * x_value + 5]))
                # The later frame is closer to the reference timestamp.
                candidate_frames.append(
                    (timestamp - 20_000, 0x123, [x_value + 50], False)
                )
                candidate_frames.append(
                    (timestamp + 10_000, 0x123, [x_value], False)
                )
                candidate_frames.append(
                    (
                        timestamp + 5_000,
                        0x18DAF160,
                        [x_value],
                        True,
                    )
                )
                candidate_frames.append(
                    (timestamp + 5_000, 0x7E0, [x_value], False)
                )
            rows, lines = linked_evidence(samples, candidate_frames)
            fixture.write_wire(rows)
            fixture.write_capture(lines)

            report = correlate.run_analysis(
                wire=fixture.wire,
                captures=[fixture.capture],
                did=0x1000,
                reference_field="auto",
                config=correlate.AnalysisConfig(
                    match_mode="nearest",
                    radius_us=50_000,
                    minimum_samples=3,
                    top_count=20,
                ),
            )

        row = candidate(report, 0x123, "byte", 0)
        self.assertEqual(report["classification"], "candidate_only")
        self.assertTrue(report["offline_only"])
        self.assertNotIn("regime_analysis", report)
        self.assertEqual(report["reference"]["module"]["key"], "cluster")
        self.assertEqual(
            report["reference"]["module"]["rxid_hex"], "18DAF160"
        )
        self.assertFalse(
            report["capture"]["provenance_limits"][
                "loss_accounting_validated"
            ]
        )
        self.assertEqual(row["sample_count"], 5)
        self.assertEqual(row["channel"], "can0")
        self.assertEqual(row["id_bits"], 11)
        self.assertEqual(row["dlc"], 1)
        self.assertAlmostEqual(row["affine_model"]["scale"], 2.0)
        self.assertAlmostEqual(row["affine_model"]["intercept"], 5.0)
        self.assertAlmostEqual(row["correlation"]["r_squared"], 1.0)
        self.assertAlmostEqual(
            row["timing"]["mean_contributing_abs_delta_ms"], 10.0
        )
        self.assertEqual(report["capture"]["excluded_extended_frames"], 10)
        self.assertEqual(report["capture"]["excluded_diagnostic_frames"], 5)
        self.assertEqual(
            report["analysis"]["candidate_field_profile"][
                "default_dlc8_field_count"
            ],
            39,
        )
        self.assertEqual(
            report["analysis"]["candidate_field_profile"][
                "targeted_bit_search_streams"
            ],
            [],
        )
        self.assertTrue(
            all(
                "dbc_start_bit" not in item["field"]
                for item in report["ranking"]["candidates"]
            )
        )
        for flag in (
            "candidate_only",
            "physical_identity_verified",
            "scale_verified",
            "telemetry_promotion_allowed",
        ):
            self.assertIn(flag, report)
            self.assertIn(flag, row)
        self.assertTrue(report["candidate_only"])
        self.assertEqual(row["classification"], "candidate_only")
        self.assertTrue(row["candidate_only"])
        self.assertFalse(row["physical_identity_verified"])
        self.assertFalse(row["scale_verified"])
        self.assertFalse(row["telemetry_promotion_allowed"])
        self.assertEqual(
            report["reference"]["global_candump_linkage"][
                "verified_sample_count"
            ],
            5,
        )
        self.assertNotIn(
            0x18DAF160,
            {item["can_id"] for item in report["ranking"]["candidates"]},
        )
        self.assertNotIn(
            0x7E0,
            {item["can_id"] for item in report["ranking"]["candidates"]},
        )

    def test_sparse_perfect_fit_cannot_outrank_full_coverage_candidate(self):
        references = []
        frames = []
        dense_values = (1, 2, 3, 4, 5, 6, 7, 8, 9, 11)
        for index, dense_value in enumerate(dense_values, 1):
            timestamp = index * 1_000_000
            references.append(
                correlate.ReferenceSample(timestamp, float(3 * index + 5))
            )
            frames.append(
                correlate.CanFrame(
                    timestamp, 0x123, 11, bytes([dense_value])
                )
            )
            if index <= 4:
                frames.append(
                    correlate.CanFrame(
                        timestamp, 0x124, 11, bytes([index])
                    )
                )
        frames.sort(key=lambda item: item.timestamp_us)

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                minimum_samples=3,
                minimum_coverage_ratio=0.5,
                minimum_distinct_values=3,
            ),
        )
        rows, rejected = analyzer.candidate_rows()

        self.assertIn(0x123, {row["can_id"] for row in rows})
        self.assertNotIn(0x124, {row["can_id"] for row in rows})
        self.assertGreater(rejected["below_minimum_coverage"], 0)

    def test_two_state_perfect_fit_fails_distinct_value_gate(self):
        references = []
        frames = []
        for index in range(1, 7):
            timestamp = index * 1_000_000
            candidate_value = index % 2
            references.append(
                correlate.ReferenceSample(
                    timestamp, float(candidate_value * 10 + 5)
                )
            )
            frames.append(
                correlate.CanFrame(
                    timestamp, 0x125, 11, bytes([candidate_value])
                )
            )

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                minimum_samples=4,
                minimum_distinct_values=3,
            ),
        )
        rows, rejected = analyzer.candidate_rows()

        self.assertEqual(rows, [])
        self.assertGreater(rejected["below_minimum_distinct_values"], 0)

    def test_extended_and_diagnostic_candidates_require_both_opt_ins(self):
        references = [
            correlate.ReferenceSample(index * 1_000_000, float(index * 4 + 1))
            for index in range(1, 5)
        ]
        frames = []
        for index in range(1, 5):
            frames.extend(
                [
                    correlate.CanFrame(
                        index * 1_000_000,
                        0x18DA1234,
                        29,
                        bytes([index]),
                    ),
                    correlate.CanFrame(
                        index * 1_000_000,
                        0x456,
                        11,
                        bytes([index]),
                    ),
                ]
            )
        frames.sort(key=lambda item: item.timestamp_us)

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                include_extended=True,
                include_diagnostic_ids=True,
                minimum_samples=3,
            ),
        )
        rows, _ = analyzer.candidate_rows()

        self.assertIn(0x18DA1234, {row["can_id"] for row in rows})
        self.assertIn(0x456, {row["can_id"] for row in rows})

    def test_packed_thirteen_bit_candidate_recovers_affine_scale(self):
        references = []
        frames = []
        for index, raw_value in enumerate((4000, 4200, 4400, 4600, 4800), 1):
            timestamp = index * 1_000_000
            references.append(
                correlate.ReferenceSample(timestamp, raw_value * 0.125)
            )
            frames.append(
                correlate.CanFrame(
                    timestamp,
                    0x100,
                    11,
                    bytes(
                        (
                            0xA0 | ((raw_value >> 8) & 0x1F),
                            raw_value & 0xFF,
                        )
                    ),
                )
            )

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                minimum_samples=3,
                minimum_distinct_values=3,
            ),
        )
        rows, _ = analyzer.candidate_rows()
        row = candidate(
            {"ranking": {"candidates": rows}},
            0x100,
            "u13be-low5",
            0,
        )

        self.assertAlmostEqual(row["affine_model"]["scale"], 0.125)
        self.assertAlmostEqual(row["affine_model"]["intercept"], 0.0)
        self.assertAlmostEqual(row["correlation"]["r_squared"], 1.0)

    def test_packed_seventeen_bit_candidate_recovers_affine_scale(self):
        references = []
        frames = []
        for index, raw_value in enumerate(
            (60_000, 70_000, 80_000, 90_000, 100_000),
            1,
        ):
            timestamp = index * 1_000_000
            references.append(
                correlate.ReferenceSample(timestamp, raw_value / 32.0)
            )
            frames.append(
                correlate.CanFrame(
                    timestamp,
                    0x1F7,
                    11,
                    bytes(
                        (
                            0xA0 | ((raw_value >> 16) & 0x01),
                            (raw_value >> 8) & 0xFF,
                            raw_value & 0xFF,
                        )
                    ),
                )
            )

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                minimum_samples=3,
                minimum_distinct_values=3,
            ),
        )
        rows, _ = analyzer.candidate_rows()
        row = candidate(
            {"ranking": {"candidates": rows}},
            0x1F7,
            "u17be-low1",
            0,
        )

        self.assertAlmostEqual(row["affine_model"]["scale"], 1.0 / 32.0)
        self.assertAlmostEqual(row["affine_model"]["intercept"], 0.0)
        self.assertAlmostEqual(row["correlation"]["r_squared"], 1.0)


class WindowCorrelationTests(unittest.TestCase):
    def test_window_mean_uses_frames_on_both_sides(self):
        references = []
        frames = []
        for index, x_value in enumerate((10, 20, 35, 50), 1):
            timestamp = index * 1_000_000
            references.append(
                correlate.ReferenceSample(timestamp, float(3 * x_value - 7))
            )
            frames.append(
                correlate.CanFrame(timestamp - 20_000, 0x321, 11, bytes([x_value - 2]))
            )
            frames.append(
                correlate.CanFrame(timestamp + 20_000, 0x321, 11, bytes([x_value + 2]))
            )
        frames.sort(key=lambda item: item.timestamp_us)

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                match_mode="window-mean",
                radius_us=25_000,
                minimum_samples=3,
            ),
        )
        rows, _ = analyzer.candidate_rows()
        report = {"ranking": {"candidates": rows}}
        row = candidate(report, 0x321, "byte", 0)

        self.assertAlmostEqual(row["affine_model"]["scale"], 3.0)
        self.assertAlmostEqual(row["affine_model"]["intercept"], -7.0)
        self.assertAlmostEqual(row["correlation"]["r_squared"], 1.0)
        self.assertEqual(row["timing"]["contributing_frame_count"], 8)
        self.assertEqual(
            row["timing"]["minimum_contributing_frames_per_sample"], 2
        )

    def test_overlapping_byte_u16_and_aligned_u32_fields_are_emitted(self):
        emitted = list(
            correlate.iter_payload_fields(
                bytes.fromhex("01 02 03 04 05 06 07 08")
            )
        )
        fields = dict(emitted)

        self.assertEqual(len(emitted), 39)
        self.assertEqual(fields[correlate.FieldSpec("byte", 2)], 3)
        self.assertEqual(
            fields[correlate.FieldSpec("u13be-low5", 0)],
            0x0102,
        )
        self.assertEqual(
            fields[correlate.FieldSpec("u17be-low1", 0)],
            0x010203,
        )
        self.assertEqual(fields[correlate.FieldSpec("u16be", 0)], 0x0102)
        self.assertEqual(fields[correlate.FieldSpec("u16le", 0)], 0x0201)
        self.assertEqual(fields[correlate.FieldSpec("u16be", 1)], 0x0203)
        self.assertEqual(
            fields[correlate.FieldSpec("u32be", 0)], 0x01020304
        )
        self.assertEqual(
            fields[correlate.FieldSpec("u32le", 0)], 0x04030201
        )
        self.assertEqual(
            fields[correlate.FieldSpec("u32be", 4)], 0x05060708
        )
        self.assertNotIn(correlate.FieldSpec("u32be", 1), fields)

    def test_stellantis_packed_thirteen_bit_field_masks_upper_bits(self):
        payload = bytes.fromhex("e1 23")
        spec = correlate.FieldSpec("u13be-low5", 0)

        self.assertEqual(spec.width_bytes, 2)
        self.assertEqual(spec.byte_order, "big")
        self.assertFalse(spec.signed)
        self.assertEqual(correlate._decode_field(payload, spec), 0x0123)

    def test_stellantis_packed_seventeen_bit_field_masks_upper_bits(self):
        payload = bytes.fromhex("fd 6f a1")
        spec = correlate.FieldSpec("u17be-low1", 0)

        self.assertEqual(spec.width_bytes, 3)
        self.assertEqual(spec.byte_order, "big")
        self.assertFalse(spec.signed)
        self.assertEqual(correlate._decode_field(payload, spec), 0x16FA1)

    def test_targeted_bit_search_finds_nonbyte_motorola_geometry_only_on_target(self):
        geometry = correlate.SignalField(4, 13, "big")
        references = []
        frames = []
        for index, raw in enumerate((17, 129, 511, 1025, 4093), 1):
            timestamp = index * 1_000_000
            payload = geometry.insert(b"\xE0\x00", raw)
            references.append(
                correlate.ReferenceSample(timestamp, float(raw * 3 - 7))
            )
            frames.extend(
                (
                    correlate.CanFrame(timestamp, 0x123, 11, payload),
                    correlate.CanFrame(timestamp, 0x124, 11, payload),
                )
            )
        frames.sort(key=lambda item: item.timestamp_us)

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                minimum_samples=3,
                minimum_distinct_values=3,
                top_count=500,
                bit_search_ids=frozenset(((0x123, 11, 2),)),
                bit_search_minimum_bits=13,
                bit_search_maximum_bits=13,
                bit_search_lengths=(13,),
                bit_search_byte_orders=("big",),
                bit_search_signedness=(False,),
            ),
        )
        rows, _ = analyzer.candidate_rows()
        report = {"ranking": {"candidates": rows}}
        row = bit_candidate(report, 0x123, 11, 4, 13, "big", False)

        self.assertAlmostEqual(row["affine_model"]["scale"], 3.0)
        self.assertAlmostEqual(row["affine_model"]["intercept"], -7.0)
        self.assertEqual(row["field"]["bit_numbering"], "dbc_cantools_sawtooth")
        target_rows = [item for item in rows if item["can_id"] == 0x123]
        control_rows = [item for item in rows if item["can_id"] == 0x124]
        self.assertTrue(
            all("dbc_start_bit" in item["field"] for item in target_rows)
        )
        self.assertTrue(
            all("dbc_start_bit" not in item["field"] for item in control_rows)
        )

    def test_targeted_signed_cross_byte_geometry_and_sff_eff_identity(self):
        geometry = correlate.SignalField(15, 12, "big", signed=True)
        references = []
        frames = []
        for index, raw in enumerate((-900, -100, 0, 211, 900), 1):
            timestamp = index * 1_000_000
            payload = geometry.insert(b"\xA5\x00\x0F", raw)
            references.append(
                correlate.ReferenceSample(timestamp, float(raw * 2 + 11))
            )
            frames.extend(
                (
                    correlate.CanFrame(timestamp, 0x123, 11, payload),
                    correlate.CanFrame(timestamp, 0x123, 29, payload),
                )
            )
        frames.sort(key=lambda item: item.timestamp_us)

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                include_extended=True,
                minimum_samples=3,
                minimum_distinct_values=3,
                top_count=500,
                bit_search_ids=frozenset(((0x123, 29, 3),)),
                bit_search_minimum_bits=12,
                bit_search_maximum_bits=12,
                bit_search_lengths=(12,),
                bit_search_byte_orders=("big",),
                bit_search_signedness=(True,),
            ),
        )
        rows, _ = analyzer.candidate_rows()
        report = {"ranking": {"candidates": rows}}
        row = bit_candidate(report, 0x123, 29, 15, 12, "big", True)

        self.assertAlmostEqual(row["affine_model"]["scale"], 2.0)
        self.assertAlmostEqual(row["affine_model"]["intercept"], 11.0)
        self.assertTrue(
            all(
                "dbc_start_bit" not in item["field"]
                for item in rows
                if item["can_id"] == 0x123 and item["id_bits"] == 11
            )
        )

    def test_same_identifier_with_different_dlc_is_not_merged(self):
        references = [
            correlate.ReferenceSample(index * 1_000_000, float(index * 2))
            for index in range(1, 6)
        ]
        frames = []
        for index in range(1, 6):
            timestamp = index * 1_000_000
            frames.extend(
                (
                    correlate.CanFrame(
                        timestamp, 0x222, 11, bytes((index,))
                    ),
                    correlate.CanFrame(
                        timestamp, 0x222, 11, bytes((0xA5, index))
                    ),
                )
            )
        frames.sort(key=lambda item: item.timestamp_us)

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                minimum_samples=3,
                minimum_distinct_values=3,
                top_count=100,
            ),
        )
        rows, _ = analyzer.candidate_rows()
        exact_rows = [
            row
            for row in rows
            if row["can_id"] == 0x222
            and row["field"]["kind"] == "byte"
            and (
                (row["dlc"] == 1 and row["field"]["offset"] == 0)
                or (row["dlc"] == 2 and row["field"]["offset"] == 1)
            )
        ]

        self.assertEqual({row["dlc"] for row in exact_rows}, {1, 2})
        self.assertTrue(
            all(row["correlation"]["r_squared"] == 1.0 for row in exact_rows)
        )

    def test_bit_refinement_targets_one_exact_dlc_stream(self):
        config = correlate.AnalysisConfig(
            bit_search_ids=frozenset(((0x222, 11, 1),)),
            bit_search_minimum_bits=8,
            bit_search_maximum_bits=8,
            bit_search_lengths=(8,),
            bit_search_byte_orders=("little",),
            bit_search_signedness=(False,),
        )
        analyzer = correlate.StreamingCorrelator(config)

        refined = list(analyzer._iter_payload_fields(0x222, 11, b"\x12"))
        coarse_other_dlc = list(
            analyzer._iter_payload_fields(0x222, 11, b"\x12\x34")
        )

        self.assertTrue(
            all(spec.geometry is not None for spec, _ in refined)
        )
        self.assertTrue(
            all(spec.geometry is None for spec, _ in coarse_other_dlc)
        )


class EvidenceValidationTests(unittest.TestCase):
    def test_exact_wire_link_preserves_sff_eff_namespace(self):
        reference = correlate.ReferenceSample(
            1_000_000,
            1.0,
            raw_line_sequence=0,
            expected_can_id=0x700,
            expected_id_bits=11,
            expected_channel="can0",
            expected_can_data=b"\x01",
        )
        frame = correlate.CanFrame(
            1_000_000,
            0x700,
            29,
            b"\x01",
            raw_line_sequence=0,
        )

        with self.assertRaisesRegex(
            correlate.CorrelateError, "exact global candump frame"
        ):
            correlate.analyze_streams(
                [reference],
                [frame],
                config=correlate.AnalysisConfig(minimum_samples=2),
            )

    def test_public_field_and_candidate_keys_remain_orderable(self):
        fields = [
            correlate.FieldSpec("u16be", 1),
            correlate.FieldSpec("byte", 0),
            correlate.FieldSpec.from_signal_field(
                correlate.SignalField(4, 13, "big")
            ),
        ]
        ordered_fields = sorted(fields)
        self.assertEqual(len(ordered_fields), len(fields))
        self.assertEqual(
            {field.label for field in ordered_fields},
            {"u16be:1", "byte:0", "u13be@4"},
        )

        keys = [
            correlate.CandidateKey("can0", 0x101, 11, 8, fields[0]),
            correlate.CandidateKey("can0", 0x100, 11, 8, fields[1]),
        ]
        self.assertEqual(sorted(keys)[0].can_id, 0x100)

    def test_stream_field_selector_parser_pins_namespace_dlc_and_geometry(self):
        selector = correlate._parse_stream_field_selector(
            "sff:101:8=bits:big:0:12:unsigned"
        )

        self.assertEqual(selector.stream_key, ("can0", 0x101, 11, 8))
        self.assertEqual(
            selector.field.geometry,
            correlate.SignalField(0, 12, "big"),
        )
        with self.assertRaises(argparse.ArgumentTypeError):
            correlate._parse_stream_field_selector(
                "sff:101:2=u32be:0"
            )

    def test_regime_config_rejects_projected_state_above_cap(self):
        def selector(can_id):
            return correlate.StreamFieldSelector(
                can_id,
                11,
                8,
                correlate.FieldSpec("byte", 0),
            )

        bit_search_streams = frozenset(
            ((0x100, 11, 8), (0x1F4, 11, 8))
        )
        regime = correlate.RegimeAnalysisConfig(
            speed=selector(0x101),
            rpm=selector(0x0FC),
            throttle=selector(0x41B),
            candidate_streams=bit_search_streams,
            stopped_speed_max=0.0,
            moving_speed_min=10.0,
            idle_rpm_min=500.0,
            pull_speed_rate_min=20.0,
            pull_throttle_min=200.0,
            steady_speed_rate_max=5.0,
            steady_throttle_rate_max=5.0,
            lift_throttle_rate_max=-50.0,
            overrun_speed_rate_max=0.0,
            overrun_throttle_max=100.0,
            minimum_samples=2,
        )
        config = correlate.AnalysisConfig(
            bit_search_ids=bit_search_streams,
            regime_analysis=regime,
        )

        with self.assertRaisesRegex(
            correlate.CorrelateError,
            "exceeding the 50000-regression safety cap",
        ):
            config.validate()

        accepted = correlate.AnalysisConfig(
            bit_search_ids=bit_search_streams,
            regime_analysis=replace(
                regime,
                candidate_streams=frozenset(((0x100, 11, 8),)),
            ),
        )
        accepted.validate()

    def test_opt_in_regime_analysis_slices_only_shortlisted_streams(self):
        selector = lambda can_id: correlate.StreamFieldSelector(
            can_id,
            11,
            2,
            correlate.FieldSpec("u16be", 0),
        )
        regime = correlate.RegimeAnalysisConfig(
            speed=selector(0x101),
            rpm=selector(0x0FC),
            throttle=selector(0x41B),
            candidate_streams=frozenset(((0x123, 11, 1),)),
            stopped_speed_max=0.0,
            moving_speed_min=10.0,
            idle_rpm_min=500.0,
            pull_speed_rate_min=20.0,
            pull_throttle_min=200.0,
            steady_speed_rate_max=5.0,
            steady_throttle_rate_max=5.0,
            lift_throttle_rate_max=-50.0,
            overrun_speed_rate_max=0.0,
            overrun_throttle_max=100.0,
            minimum_samples=2,
        )
        samples = (
            (0, 700, 50),
            (0, 700, 50),
            (50, 1000, 250),
            (100, 1100, 300),
            (102, 1100, 302),
            (104, 1100, 304),
            (103, 1100, 200),
            (100, 1100, 80),
            (90, 1100, 80),
            (80, 1100, 80),
        )
        references = []
        frames = []
        for index, (speed, rpm, throttle) in enumerate(samples, 1):
            timestamp = index * 1_000_000
            candidate = index
            references.append(
                correlate.ReferenceSample(
                    timestamp, float(candidate * 2 + 1)
                )
            )
            frames.extend(
                (
                    correlate.CanFrame(
                        timestamp,
                        0x101,
                        11,
                        speed.to_bytes(2, "big"),
                    ),
                    correlate.CanFrame(
                        timestamp,
                        0x0FC,
                        11,
                        rpm.to_bytes(2, "big"),
                    ),
                    correlate.CanFrame(
                        timestamp,
                        0x41B,
                        11,
                        throttle.to_bytes(2, "big"),
                    ),
                    correlate.CanFrame(
                        timestamp,
                        0x123,
                        11,
                        bytes((candidate,)),
                    ),
                )
            )

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(
                minimum_samples=2,
                minimum_coverage_ratio=0.5,
                minimum_distinct_values=2,
                regime_analysis=regime,
            ),
        )
        rows = analyzer.regime_rows()

        self.assertIsNotNone(rows)
        self.assertEqual(
            {
                name: analyzer.regime_classification_counts[name]
                for name in correlate.REGIME_NAMES
            },
            {
                "idle": 2,
                "positive_pull": 2,
                "steady_cruise": 2,
                "lift_transition": 2,
                "negative_overrun": 2,
            },
        )
        assert rows is not None
        for name in correlate.REGIME_NAMES:
            self.assertEqual(rows[name]["reported_candidate_count"], 1)
            candidate = rows[name]["candidates"][0]
            self.assertEqual(candidate["can_id"], 0x123)
            self.assertEqual(candidate["field"]["kind"], "byte")
            self.assertEqual(candidate["correlation"]["r_squared"], 1.0)

    def test_reference_requires_exact_payload_did_match_and_chronology(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster_wire.jsonl"
            bad = wire_row(1_000_000, 0x1000, [1])
            bad["isotp_payload_hex"] = "62 10 02 01"
            path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
            stats = correlate.StreamStats(str(path), "none")

            with self.assertRaisesRegex(
                correlate.CorrelateError, "payload/DID mismatch"
            ):
                list(
                    correlate.iter_reference_samples(
                        path,
                        did=0x1000,
                        decoder=correlate.ReferenceDecoder("auto"),
                        stats=stats,
                    )
                )

    def test_selected_wire_row_requires_provenance_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster_wire.jsonl"
            row = wire_row(1_000_000, 0x1000, [1])
            del row["direction"]
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                correlate.CorrelateError, "invalid direction"
            ):
                list(
                    correlate.iter_reference_samples(
                        path,
                        did=0x1000,
                        decoder=correlate.ReferenceDecoder("auto"),
                        stats=correlate.StreamStats(str(path), "none"),
                    )
                )

    def test_selected_wire_row_is_pinned_to_registered_cluster_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster_wire.jsonl"
            row = wire_row(
                1_000_000,
                0x1000,
                [1],
                can_id=0x18DAF110,
            )
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                correlate.CorrelateError, "registered cluster RX endpoint"
            ):
                list(
                    correlate.iter_reference_samples(
                        path,
                        did=0x1000,
                        decoder=correlate.ReferenceDecoder("auto"),
                        stats=correlate.StreamStats(str(path), "none"),
                    )
                )

    def test_reference_json_and_field_parser_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster_wire.jsonl"
            path.write_text('{"value": NaN}\\n', encoding="utf-8")
            with self.assertRaisesRegex(correlate.CorrelateError, "malformed JSON"):
                list(
                    correlate.iter_reference_samples(
                        path,
                        did=0x1000,
                        decoder=correlate.ReferenceDecoder("auto"),
                        stats=correlate.StreamStats(str(path), "none"),
                    )
                )

        with self.assertRaisesRegex(
            correlate.CorrelateError, "reference field must be"
        ):
            correlate.ReferenceDecoder("byte:" + "9" * 10_000)

    def test_schema_version_requires_an_integer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster_wire.jsonl"
            row = wire_row(1_000_000, 0x1000, [1])
            row["schema_version"] = 1.0
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                correlate.CorrelateError, "invalid schema_version"
            ):
                list(
                    correlate.iter_reference_samples(
                        path,
                        did=0x1000,
                        decoder=correlate.ReferenceDecoder("auto"),
                        stats=correlate.StreamStats(str(path), "none"),
                    )
                )

    def test_global_candump_link_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(directory)
            rows, lines = linked_evidence(
                [(1_000_000, 0x1000, [10]), (2_000_000, 0x1000, [20])],
                [
                    (1_000_000, 0x123, [5], False),
                    (2_000_000, 0x123, [10], False),
                ],
            )
            linked_sequence = rows[0]["raw_line_sequence"]
            lines[linked_sequence] = candump(
                1_000_000,
                0x18DAF160,
                bytes.fromhex("04 62 10 00 FF"),
                extended=True,
            )
            fixture.write_wire(rows)
            fixture.write_capture(lines)

            with self.assertRaisesRegex(
                correlate.CorrelateError,
                "exact global candump frame sequence/timestamp/ID/payload",
            ):
                correlate.run_analysis(
                    wire=fixture.wire,
                    captures=[fixture.capture],
                    did=0x1000,
                    reference_field="auto",
                    config=correlate.AnalysisConfig(minimum_samples=2),
                )

    def test_global_link_succeeds_across_chunks_and_missing_chunk_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(directory)
            samples = [
                (index * 1_000_000, 0x1000, [index * 2])
                for index in range(1, 5)
            ]
            rows, lines = linked_evidence(
                samples,
                [
                    (index * 1_000_000, 0x123, [index], False)
                    for index in range(1, 5)
                ],
            )
            fixture.write_wire(rows)
            midpoint = len(lines) // 2
            first = Path(directory) / "chunk_000000_full.candump"
            second = Path(directory) / "chunk_000001_full.candump"
            first.write_text("".join(lines[:midpoint]), encoding="ascii")
            second.write_text("".join(lines[midpoint:]), encoding="ascii")

            report = correlate.run_analysis(
                wire=fixture.wire,
                captures=[first, second],
                did=0x1000,
                reference_field="auto",
                config=correlate.AnalysisConfig(
                    minimum_samples=3,
                    minimum_distinct_values=3,
                ),
            )
            self.assertEqual(
                report["reference"]["global_candump_linkage"][
                    "verified_sample_count"
                ],
                4,
            )

            with self.assertRaisesRegex(
                correlate.CorrelateError, "missing from the supplied candump"
            ):
                correlate.run_analysis(
                    wire=fixture.wire,
                    captures=[first],
                    did=0x1000,
                    reference_field="auto",
                    config=correlate.AnalysisConfig(
                        minimum_samples=3,
                        minimum_distinct_values=3,
                    ),
                )

    def test_auto_reference_field_accepts_byte_u16_and_u32(self):
        one = correlate.ReferenceDecoder("auto")
        self.assertEqual(one.decode(b"\x12"), 0x12)
        self.assertEqual(one.resolved, correlate.FieldSpec("byte", 0))

        two = correlate.ReferenceDecoder("auto")
        self.assertEqual(two.decode(b"\x12\x34"), 0x1234)
        self.assertEqual(two.resolved, correlate.FieldSpec("u16be", 0))

        four = correlate.ReferenceDecoder("auto")
        self.assertEqual(four.decode(b"\x12\x34\x56\x78"), 0x12345678)
        self.assertEqual(four.resolved, correlate.FieldSpec("u32be", 0))

        with self.assertRaisesRegex(
            correlate.CorrelateError, "exactly one, two, or four"
        ):
            correlate.ReferenceDecoder("auto").decode(b"\x01\x02\x03")

    def test_explicit_signed_reference_fields_decode_twos_complement(self):
        negative_16 = correlate.ReferenceDecoder("i16be:0")
        self.assertEqual(negative_16.decode(b"\xff\x9c"), -100)
        self.assertEqual(
            negative_16.resolved.as_dict(),
            {
                "kind": "i16be",
                "offset": 0,
                "width_bytes": 2,
                "byte_order": "big",
                "signed": True,
            },
        )

        negative_32 = correlate.ReferenceDecoder("i32le:1")
        self.assertEqual(
            negative_32.decode(b"\x00\x9c\xff\xff\xff"), -100
        )

        packed = correlate.ReferenceDecoder("bits:big:4:13:unsigned")
        self.assertEqual(packed.decode(bytes.fromhex("E1 23")), 0x123)
        self.assertEqual(
            packed.resolved.as_dict()["bit_numbering"],
            "dbc_cantools_sawtooth",
        )

    def test_bit_search_identifier_parser_and_configuration_caps(self):
        self.assertEqual(
            correlate._parse_bit_search_id("sff:100:8"), (0x100, 11, 8)
        )
        self.assertEqual(
            correlate._parse_bit_search_id("eff:00100100:3"),
            (0x100100, 29, 3),
        )
        with self.assertRaises(argparse.ArgumentTypeError):
            correlate._parse_bit_search_id("sff:800:8")
        with self.assertRaises(argparse.ArgumentTypeError):
            correlate._parse_bit_search_id("100")
        with self.assertRaises(argparse.ArgumentTypeError):
            correlate._parse_bit_search_id("sff:100:0")

        with self.assertRaisesRegex(
            correlate.CorrelateError, "identifier count"
        ):
            correlate.AnalysisConfig(
                bit_search_ids=frozenset(
                    ((0x100, 11, 8), (0x101, 11, 8), (0x102, 11, 8))
                )
            ).validate()

        correlate._bit_search_specs.cache_clear()
        try:
            with mock.patch.object(
                correlate, "MAX_BIT_SEARCH_FIELDS_PER_IDENTIFIER", 10
            ):
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "field count"
                ):
                    correlate.AnalysisConfig(
                        bit_search_ids=frozenset(((0x100, 11, 8),))
                    ).validate()
        finally:
            correlate._bit_search_specs.cache_clear()

    def test_targeted_diagnostic_id_still_obeys_default_filter(self):
        references = [
            correlate.ReferenceSample(index * 1_000_000, float(index))
            for index in range(1, 5)
        ]
        frames = [
            correlate.CanFrame(
                index * 1_000_000, 0x7E0, 11, bytes((index, 0))
            )
            for index in range(1, 5)
        ]
        with self.assertRaisesRegex(
            correlate.CorrelateError, "no eligible candidate frames"
        ):
            correlate.analyze_streams(
                references,
                frames,
                config=correlate.AnalysisConfig(
                    minimum_samples=3,
                    bit_search_ids=frozenset(((0x7E0, 11, 2),)),
                    bit_search_minimum_bits=8,
                    bit_search_maximum_bits=8,
                    bit_search_lengths=(8,),
                    bit_search_signedness=(False,),
                ),
            )

    def test_candidate_identifier_cap_is_enforced(self):
        references = [correlate.ReferenceSample(1_000_000, 1.0)]
        frames = [
            correlate.CanFrame(1_000_000, 0x100, 11, b"\x01"),
            correlate.CanFrame(1_000_000, 0x101, 11, b"\x01"),
        ]
        with mock.patch.object(correlate, "MAX_CANDIDATE_IDS", 1):
            with self.assertRaisesRegex(
                correlate.CorrelateError, "identifier safety cap"
            ):
                correlate.analyze_streams(
                    references,
                    frames,
                    config=correlate.AnalysisConfig(minimum_samples=2),
                )

    def test_diagnostic_perfect_match_is_excluded_by_default(self):
        references = [
            correlate.ReferenceSample(index * 1_000_000, float(index * 3))
            for index in range(1, 5)
        ]
        frames = []
        for index in range(1, 5):
            frames.extend(
                [
                    correlate.CanFrame(
                        index * 1_000_000, 0x7E0, 11, bytes([index])
                    ),
                    correlate.CanFrame(
                        index * 1_000_000, 0x123, 11, bytes([index * index])
                    ),
                ]
            )
        frames.sort(key=lambda item: item.timestamp_us)

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(minimum_samples=3),
        )
        rows, _ = analyzer.candidate_rows()

        self.assertNotIn(0x7E0, {row["can_id"] for row in rows})
        self.assertEqual(analyzer.excluded_diagnostic_frames, 4)

    def test_passive_73a_is_retained_while_obd_ids_are_excluded(self):
        references = [
            correlate.ReferenceSample(index * 1_000_000, float(index * 2 + 1))
            for index in range(1, 5)
        ]
        frames = []
        for index in range(1, 5):
            for can_id in (0x73A, 0x7DF, 0x7E0):
                frames.append(
                    correlate.CanFrame(
                        index * 1_000_000, can_id, 11, bytes([index])
                    )
                )
        frames.sort(key=lambda item: item.timestamp_us)

        analyzer = correlate.analyze_streams(
            references,
            frames,
            config=correlate.AnalysisConfig(minimum_samples=3),
        )
        rows, _ = analyzer.candidate_rows()

        self.assertIn(0x73A, {row["can_id"] for row in rows})
        self.assertNotIn(0x7DF, {row["can_id"] for row in rows})
        self.assertNotIn(0x7E0, {row["can_id"] for row in rows})
        self.assertEqual(analyzer.excluded_diagnostic_frames, 8)

    def test_no_reference_does_not_consume_capture(self):
        def forbidden_frames():
            raise AssertionError("capture iterator was consumed")
            yield

        with self.assertRaisesRegex(
            correlate.CorrelateError, "no exact positive rows"
        ):
            correlate.analyze_streams(
                [],
                forbidden_frames(),
                config=correlate.AnalysisConfig(),
            )

    def test_pending_link_window_field_wire_and_file_caps(self):
        linked = [
            correlate.ReferenceSample(
                index,
                float(index),
                raw_line_sequence=10 + index,
                expected_can_id=correlate.CLUSTER_MODULE.rxid,
                expected_id_bits=29,
                expected_channel=correlate.CLUSTER_MODULE.channel,
                expected_can_data=b"\x04\x62\x10\x00\x01",
            )
            for index in (1, 2)
        ]
        with mock.patch.object(correlate, "MAX_PENDING_WIRE_LINKS", 1):
            with self.assertRaisesRegex(
                correlate.CorrelateError, "pending cluster-wire linkage"
            ):
                correlate.analyze_streams(
                    linked,
                    [
                        correlate.CanFrame(
                            100, 0x123, 11, b"\x01", raw_line_sequence=0
                        )
                    ],
                    config=correlate.AnalysisConfig(
                        minimum_samples=2,
                        minimum_distinct_values=2,
                    ),
                )

        with mock.patch.object(correlate, "MAX_ACTIVE_WINDOW_FIELDS", 1):
            with self.assertRaisesRegex(
                correlate.CorrelateError, "window-field accumulator"
            ):
                correlate.analyze_streams(
                    [correlate.ReferenceSample(1_000_000, 1.0)],
                    [
                        correlate.CanFrame(
                            1_000_000, 0x123, 11, b"\x01\x02"
                        )
                    ],
                    config=correlate.AnalysisConfig(
                        match_mode="window-mean",
                        minimum_samples=2,
                        minimum_distinct_values=2,
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster_wire.jsonl"
            path.write_bytes(b"\n\n")
            with mock.patch.object(correlate, "MAX_WIRE_STREAM_LINES", 1):
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "line-count safety cap"
                ):
                    list(
                        correlate.iter_reference_samples(
                            path,
                            did=0x1000,
                            decoder=correlate.ReferenceDecoder("auto"),
                            stats=correlate.StreamStats(str(path), "none"),
                        )
                    )
            with mock.patch.object(correlate, "MAX_WIRE_STREAM_BYTES", 1):
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "byte-count safety cap"
                ):
                    list(
                        correlate.iter_reference_samples(
                            path,
                            did=0x1000,
                            decoder=correlate.ReferenceDecoder("auto"),
                            stats=correlate.StreamStats(str(path), "none"),
                        )
                    )

            with mock.patch.object(correlate, "MAX_CAPTURE_FILES", 1):
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "file count"
                ):
                    correlate._validate_inputs(path, [path, path])

    def test_candump_interface_and_identifier_width_are_pinned(self):
        with self.assertRaisesRegex(correlate.CorrelateError, "interface"):
            correlate.parse_candump_frame(b"(1.000000) can1 123#01\n")
        with self.assertRaisesRegex(
            correlate.CorrelateError, "exactly three SFF or eight EFF"
        ):
            correlate.parse_candump_frame(b"(1.000000) can0 0123#01\n")

    def test_top_reference_frame_and_decompressed_byte_caps(self):
        with self.assertRaisesRegex(correlate.CorrelateError, "top count"):
            correlate.AnalysisConfig(
                top_count=correlate.MAX_TOP_COUNT + 1
            ).validate()

        with mock.patch.object(correlate, "MAX_REFERENCE_SAMPLES", 1):
            with self.assertRaisesRegex(
                correlate.CorrelateError, "reference sample safety cap"
            ):
                correlate.analyze_streams(
                    [
                        correlate.ReferenceSample(1_000_000, 1.0),
                        correlate.ReferenceSample(2_000_000, 2.0),
                    ],
                    [correlate.CanFrame(1_000_000, 0x123, 11, b"\x01")],
                    config=correlate.AnalysisConfig(minimum_samples=2),
                )

        with mock.patch.object(correlate, "MAX_CAPTURE_FRAMES", 1):
            with self.assertRaisesRegex(
                correlate.CorrelateError, "capture frame safety cap"
            ):
                correlate.analyze_streams(
                    [correlate.ReferenceSample(1_000_000, 1.0)],
                    [
                        correlate.CanFrame(1_000_000, 0x123, 11, b"\x01"),
                        correlate.CanFrame(2_000_000, 0x123, 11, b"\x02"),
                    ],
                    config=correlate.AnalysisConfig(minimum_samples=2),
                )

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(directory)
            rows, lines = linked_evidence(
                [(1_000_000, 0x1000, [2])],
                [(1_000_000, 0x123, [1], False)],
            )
            fixture.write_wire(rows)
            fixture.write_capture(lines)
            with mock.patch.object(
                correlate, "MAX_CAPTURE_DECOMPRESSED_BYTES", 1
            ):
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "decompressed-byte safety cap"
                ):
                    correlate.run_analysis(
                        wire=fixture.wire,
                        captures=[fixture.capture],
                        did=0x1000,
                        reference_field="auto",
                        config=correlate.AnalysisConfig(minimum_samples=2),
                    )

    def test_fake_typed_zstd_boundary_streams_compressed_named_input(self):
        class FakeZstd:
            def __init__(self, content):
                self.content = content
                self.paths = []

            @contextlib.contextmanager
            def open(self, path):
                self.paths.append(path)
                yield io.BytesIO(self.content)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(directory)
            samples = [
                (index * 1_000_000, 0x1000, [index * 2])
                for index in range(1, 5)
            ]
            rows, lines = linked_evidence(
                samples,
                [
                    (index * 1_000_000, 0x123, [index], False)
                    for index in range(1, 5)
                ],
            )
            fixture.write_wire(rows)
            compressed = Path(directory) / "chunk.candump.zst"
            compressed.write_bytes(b"synthetic-placeholder")
            content = "".join(lines).encode("ascii")
            fake = FakeZstd(content)

            report = correlate.run_analysis(
                wire=fixture.wire,
                captures=[compressed],
                did=0x1000,
                reference_field="auto",
                config=correlate.AnalysisConfig(minimum_samples=3),
                decompressor=fake,
            )

        self.assertEqual(fake.paths, [compressed])
        self.assertEqual(report["capture"]["sources"][0]["compression"], "zstd")
        self.assertEqual(
            candidate(report, 0x123, "byte", 0)["correlation"]["r_squared"],
            1.0,
        )


class CliTests(unittest.TestCase):
    def test_cli_plain_input_never_spawns_and_writes_exclusive_tmp_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = EvidenceFixture(root)
            rows, lines = linked_evidence(
                [
                    (index * 1_000_000, 0x1000, [index * 2 + 1])
                    for index in range(1, 5)
                ],
                [
                    (index * 1_000_000, 0x123, [index], False)
                    for index in range(1, 5)
                ],
            )
            fixture.write_wire(rows)
            fixture.write_capture(lines)
            tmp_root = root / "repo-tmp"
            output = tmp_root / "analysis" / "report.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--wire",
                str(fixture.wire),
                "--did",
                "1000",
                "--minimum-samples",
                "3",
                "--output",
                str(output),
                str(fixture.capture),
            ]
            with (
                mock.patch.object(correlate, "TMP_ROOT", tmp_root),
                mock.patch.object(
                    correlate.subprocess,
                    "Popen",
                    side_effect=AssertionError("plain input spawned a subprocess"),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                first = correlate.main(argv)
                original = output.read_bytes()
                second = correlate.main(argv)
                after_second_run = output.read_bytes()

        self.assertEqual(first, 0)
        self.assertEqual(second, 2)
        self.assertEqual(after_second_run, original)
        report = json.loads(original)
        self.assertEqual(report["classification"], "candidate_only")
        self.assertIn("refusing to overwrite", stderr.getvalue())
        self.assertIn("candidate-only correlations", stdout.getvalue())

    def test_output_must_be_explicit_json_below_tmp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tmp_root = root / "tmp"
            with mock.patch.object(correlate, "TMP_ROOT", tmp_root):
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "below"
                ):
                    correlate._validated_output_path(root / "outside.json")
                with self.assertRaisesRegex(
                    correlate.CorrelateError, r"end in \.json"
                ):
                    correlate._validated_output_path(tmp_root / "report.txt")

    def test_van_compute_result_root_requires_exact_sandbox_and_job_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "job" / "source"
            result_root = root / "job" / "result"
            wrong_result_root = root / "job" / "artifacts"
            other_result_root = root / "other-job" / "result"
            tmp_root = source_root / "tmp"
            source_root.mkdir(parents=True)
            result_root.mkdir(parents=True)
            wrong_result_root.mkdir()
            other_result_root.mkdir(parents=True)
            output = result_root / "report.json"
            with (
                mock.patch.object(correlate, "REPO", source_root),
                mock.patch.object(correlate, "TMP_ROOT", tmp_root),
            ):
                with mock.patch.dict(
                    correlate.os.environ,
                    {"VAN_COMPUTE_JOB_ID": "20260726T120000Z-abcdef12"},
                    clear=False,
                ):
                    self.assertEqual(
                        correlate._validated_output_path(
                            output, allow_van_compute_result=True
                        ),
                        output.resolve(),
                    )
                    with self.assertRaisesRegex(
                        correlate.CorrelateError, "below"
                    ):
                        correlate._validated_output_path(output)
                    with self.assertRaisesRegex(
                        correlate.CorrelateError, "named report.json"
                    ):
                        correlate._validated_output_path(
                            result_root / "other.json",
                            allow_van_compute_result=True,
                        )
                    with self.assertRaisesRegex(
                        correlate.CorrelateError,
                        "staged sibling result",
                    ):
                        correlate._validated_output_path(
                            wrong_result_root / "report.json",
                            allow_van_compute_result=True,
                        )
                    with self.assertRaisesRegex(
                        correlate.CorrelateError,
                        "staged sibling result",
                    ):
                        correlate._validated_output_path(
                            other_result_root / "report.json",
                            allow_van_compute_result=True,
                        )

                with mock.patch.dict(
                    correlate.os.environ,
                    {"VAN_COMPUTE_JOB_ID": "not-a-job"},
                    clear=False,
                ):
                    with self.assertRaisesRegex(
                        correlate.CorrelateError, "valid VAN_COMPUTE_JOB_ID"
                    ):
                        correlate._validated_output_path(
                            output, allow_van_compute_result=True
                        )

    def test_van_compute_staged_wire_name_requires_job_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "job" / "source"
            input_root = root / "job" / "inputs"
            other_input_root = root / "other-job" / "inputs"
            source_root.mkdir(parents=True)
            input_root.mkdir()
            other_input_root.mkdir(parents=True)
            wire = input_root / "000-cluster_wire.jsonl"
            capture = input_root / "001-chunk_000000_full.candump.zst"
            other_capture = (
                other_input_root / "001-chunk_000000_full.candump.zst"
            )
            wire.touch()
            capture.touch()
            other_capture.touch()
            with (
                mock.patch.object(correlate, "REPO", source_root),
                mock.patch.dict(
                    correlate.os.environ,
                    {"VAN_COMPUTE_JOB_ID": "20260726T120000Z-abcdef12"},
                    clear=False,
                ),
            ):
                correlate._validate_inputs(
                    wire,
                    [capture],
                    allow_van_compute_staging=True,
                )
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "recorder basename"
                ):
                    correlate._validate_inputs(wire, [capture])
                with self.assertRaisesRegex(
                    correlate.CorrelateError,
                    "staged sibling inputs",
                ):
                    correlate._validate_inputs(
                        wire,
                        [other_capture],
                        allow_van_compute_staging=True,
                    )

            with (
                mock.patch.object(correlate, "REPO", source_root),
                mock.patch.dict(
                    correlate.os.environ,
                    {"VAN_COMPUTE_JOB_ID": "not-a-job"},
                    clear=False,
                ),
            ):
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "valid VAN_COMPUTE_JOB_ID"
                ):
                    correlate._validate_inputs(
                        wire,
                        [capture],
                        allow_van_compute_staging=True,
                    )


if __name__ == "__main__":
    unittest.main()
