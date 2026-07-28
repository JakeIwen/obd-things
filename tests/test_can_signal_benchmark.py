import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools import can_signal_benchmark as benchmark


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def job_manifest(report_path, *, capture_sha=SHA_B):
    report_bytes = report_path.read_bytes()
    return {
        "schema_version": 1,
        "state": "done",
        "exit_code": 0,
        "task": "can-timeseries-correlate-tcm-four-chunks",
        "inputs": [
            {"index": 0, "name": "wire.jsonl", "sha256": SHA_A},
            {
                "index": 1,
                "name": "capture.candump.zst",
                "sha256": capture_sha,
            },
        ],
        "results": [
            {
                "path": "report.json",
                "sha256": hashlib.sha256(report_bytes).hexdigest(),
                "size": len(report_bytes),
            }
        ],
    }


def manifest():
    return {
        "schema_version": 1,
        "classification": "offline_candidate_benchmark",
        "bit_numbering": "dbc_cantools_sawtooth",
        "artifacts": {
            "wire": {
                "path_hint": "tmp/wire.jsonl",
                "kind": "wire_jsonl",
                "sha256": SHA_A,
            },
            "capture": {
                "path_hint": "tmp/capture.candump.zst",
                "kind": "candump_zstd",
                "sha256": SHA_B,
            },
        },
        "datasets": {
            "leg": {
                "wire": "wire",
                "captures": ["capture"],
                "module": "tcm",
                "bus": "c-can",
                "split": "validation",
                "independent_drive_leg": True,
                "compute_status": "ready",
                "compute_task": "can-timeseries-correlate-tcm-four-chunks",
            }
        },
        "cases": [
            {
                "id": "packed_torque",
                "hypothesis": "target_torque",
                "dataset": "leg",
                "reference": {"did": "101B", "field": "u16be:0"},
                "expectation": {
                    "kind": "verified_positive",
                    "stream": {
                        "channel": "can0",
                        "can_id": "100",
                        "id_bits": 11,
                        "dlc": 8,
                    },
                    "field": {
                        "dbc_start_bit": 4,
                        "length_bits": 13,
                        "byte_order": "big",
                        "signed": False,
                    },
                    "maximum_rank": 3,
                    "minimum_coverage": 0.9,
                    "minimum_r_squared": 0.99,
                },
            },
            {
                "id": "negative",
                "hypothesis": "converter_slip",
                "dataset": "leg",
                "reference": {"did": "0500", "field": "i16be:0"},
                "expectation": {
                    "kind": "no_defensible_match",
                    "maximum_r_squared": 0.9,
                },
            },
        ],
    }


def report(did, field, r_squared):
    return {
        "schema_version": 1,
        "classification": "candidate_only",
        "candidate_only": True,
        "physical_identity_verified": False,
        "scale_verified": False,
        "telemetry_promotion_allowed": False,
        "offline_only": True,
        "analysis": {
            "maximum_candidate_staleness_ms": 100.0,
            "candidate_stream_identity": [
                "channel",
                "SFF/EFF namespace",
                "CAN ID",
                "DLC",
                "capture source path and decompressed SHA-256",
            ],
            "candidate_field_profile": {
                "targeted_bit_search_streams": [],
                "selected_lengths": [],
                "byte_orders": ["little", "big"],
                "signedness": ["unsigned", "signed"],
            },
        },
        "reference": {
            "module": {"key": "tcm"},
            "did": did,
            "requested_field": (
                "u16be:0" if did == "101B" else "i16be:0"
            ),
            "sample_count": 20,
            "global_candump_linkage": {
                "required": True,
                "linked_sample_count": 20,
                "verified_sample_count": 20,
            },
            "source": {
                "path": "{job}/inputs/000-wire.jsonl",
                "decompressed_stream_sha256": SHA_A,
            },
        },
        "capture": {
            "sources": [
                {
                    "path": "{job}/inputs/001-capture.candump.zst",
                    "decompressed_stream_sha256": SHA_B,
                }
            ]
        },
        "ranking": {
            "eligible_candidate_maximum_r_squared": r_squared,
            "candidates": [
                {
                    "rank": 1,
                    "channel": "can0",
                    "can_id": 0x100,
                    "can_id_hex": "100",
                    "id_bits": 11,
                    "dlc": 8,
                    "field": field,
                    "classification": "candidate_only",
                    "candidate_only": True,
                    "physical_identity_verified": False,
                    "scale_verified": False,
                    "telemetry_promotion_allowed": False,
                    "sample_count": 20,
                    "coverage_ratio": 1.0,
                    "correlation": {"r_squared": r_squared},
                }
            ]
        },
    }


class BenchmarkTests(unittest.TestCase):
    def test_manifest_and_plan_require_independent_drive_legs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest()), encoding="utf-8")
            loaded = benchmark.load_manifest(path)

        plan = benchmark.build_plan(loaded)
        self.assertEqual(plan["sample_split_policy"], "whole_independent_drive_legs_only")
        self.assertEqual(plan["case_count"], 2)
        self.assertFalse(plan["runs_full_saved_log_search"])

        invalid = manifest()
        invalid["datasets"]["leg"]["independent_drive_leg"] = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(
                benchmark.BenchmarkError, "complete independent drive leg"
            ):
                benchmark.load_manifest(path)

        invalid_search = manifest()
        invalid_search["cases"][0]["search"] = {
            "bit_search_ids": ["sff:100"]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(invalid_search), encoding="utf-8")
            with self.assertRaisesRegex(
                benchmark.BenchmarkError, "exact bit-search streams"
            ):
                benchmark.load_manifest(path)

    def test_legacy_and_generic_fields_compare_by_exact_bit_geometry(self):
        case = manifest()["cases"][0]
        dataset = manifest()["datasets"]["leg"]
        legacy_field = {
            "kind": "u13be-low5",
            "offset": 0,
            "width_bytes": 2,
            "byte_order": "big",
            "signed": False,
        }
        result = benchmark.evaluate_case(
            case, dataset, report("101B", legacy_field, 0.999)
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["rank"], 1)

    def test_negative_control_uses_complete_report_top_score(self):
        case = manifest()["cases"][1]
        dataset = manifest()["datasets"]["leg"]
        ordinary_field = {
            "kind": "byte",
            "offset": 0,
            "width_bytes": 1,
            "byte_order": None,
            "signed": False,
        }

        passed = benchmark.evaluate_case(
            case, dataset, report("0500", ordinary_field, 0.18)
        )
        failed = benchmark.evaluate_case(
            case, dataset, report("0500", ordinary_field, 0.95)
        )

        self.assertTrue(passed["passed"])
        self.assertFalse(failed["passed"])

    def test_negative_control_accepts_an_empty_eligible_set(self):
        case = manifest()["cases"][1]
        dataset = manifest()["datasets"]["leg"]
        payload = report(
            "0500",
            {
                "kind": "byte",
                "offset": 0,
                "width_bytes": 1,
                "byte_order": None,
                "signed": False,
            },
            0.18,
        )
        payload["ranking"]["eligible_candidate_maximum_r_squared"] = None
        payload["ranking"]["candidates"] = []

        result = benchmark.evaluate_case(case, dataset, payload)

        self.assertTrue(result["passed"])
        self.assertEqual(
            result["status"], "negative_control_empty_eligible_set"
        )
        self.assertIsNone(result["maximum_observed_r_squared"])

        payload["ranking"]["candidates"] = [
            report(
                "0500",
                {
                    "kind": "byte",
                    "offset": 0,
                    "width_bytes": 1,
                    "byte_order": None,
                    "signed": False,
                },
                0.18,
            )["ranking"]["candidates"][0]
        ]
        with self.assertRaisesRegex(
            benchmark.BenchmarkError, "reports candidates but a null"
        ):
            benchmark.evaluate_case(case, dataset, payload)

    def test_evaluation_reports_holdout_status_without_sample_splitting(self):
        data = manifest()
        packed_field = {
            "kind": "u13be-low5",
            "offset": 0,
            "width_bytes": 2,
            "byte_order": "big",
            "signed": False,
        }
        ordinary_field = {
            "kind": "byte",
            "offset": 0,
            "width_bytes": 1,
            "byte_order": None,
            "signed": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            packed_path = Path(directory) / "packed.json"
            negative_path = Path(directory) / "negative.json"
            packed_job = Path(directory) / "packed-job.json"
            negative_job = Path(directory) / "negative-job.json"
            packed_path.write_text(
                json.dumps(report("101B", packed_field, 0.999)),
                encoding="utf-8",
            )
            negative_path.write_text(
                json.dumps(report("0500", ordinary_field, 0.18)),
                encoding="utf-8",
            )
            packed_job.write_text(
                json.dumps(job_manifest(packed_path)), encoding="utf-8"
            )
            negative_job.write_text(
                json.dumps(job_manifest(negative_path)), encoding="utf-8"
            )
            evaluation = benchmark.build_evaluation(
                data,
                {
                    "packed_torque": packed_path,
                    "negative": negative_path,
                },
                {
                    "packed_torque": packed_job,
                    "negative": negative_job,
                },
            )

        summary = evaluation["hypotheses"]["target_torque"]
        self.assertEqual(summary["planned_splits"], ["validation"])
        self.assertTrue(summary["independent_holdout_evaluated"])
        self.assertTrue(summary["heldout_passed"])
        self.assertTrue(evaluation["benchmark_complete"])

    def test_evaluation_rejects_job_or_report_provenance_mismatch(self):
        data = manifest()
        data["cases"] = [data["cases"][0]]
        packed_field = {
            "kind": "u13be-low5",
            "offset": 0,
            "width_bytes": 2,
            "byte_order": "big",
            "signed": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            job_path = Path(directory) / "job.json"
            report_path.write_text(
                json.dumps(report("101B", packed_field, 0.999)),
                encoding="utf-8",
            )
            wrong_hash_job = job_manifest(
                report_path, capture_sha=SHA_C
            )
            job_path.write_text(json.dumps(wrong_hash_job), encoding="utf-8")
            with self.assertRaisesRegex(
                benchmark.BenchmarkError, "dataset artifacts"
            ):
                benchmark.build_evaluation(
                    data,
                    {"packed_torque": report_path},
                    {"packed_torque": job_path},
                )

            wrong_hash_job["inputs"][1]["sha256"] = SHA_B
            job_path.write_text(json.dumps(wrong_hash_job), encoding="utf-8")
            payload = report("101B", packed_field, 0.5)
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                benchmark.BenchmarkError, "digest does not match"
            ):
                benchmark.build_evaluation(
                    data,
                    {"packed_torque": report_path},
                    {"packed_torque": job_path},
                )

            payload = report("101B", packed_field, 0.999)
            payload["capture"]["sources"][0]["path"] = (
                "{job}/inputs/099-capture.candump.zst"
            )
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            wrong_hash_job = job_manifest(report_path)
            job_path.write_text(json.dumps(wrong_hash_job), encoding="utf-8")
            with self.assertRaisesRegex(
                benchmark.BenchmarkError, "not bound"
            ):
                benchmark.build_evaluation(
                    data,
                    {"packed_torque": report_path},
                    {"packed_torque": job_path},
                )

    def test_manifest_rejects_capture_reuse_across_splits(self):
        data = manifest()
        data["datasets"]["blind"] = {
            **data["datasets"]["leg"],
            "split": "blind_test",
        }
        data["cases"].append(
            {
                **data["cases"][0],
                "id": "blind_packed_torque",
                "dataset": "blind",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(
                benchmark.BenchmarkError, "reuse artifacts across splits"
            ):
                benchmark.load_manifest(path)

    def test_manifest_rejects_artifact_aliases_across_splits(self):
        for alias_kind in ("sha256", "path_hint"):
            with self.subTest(alias_kind=alias_kind):
                data = manifest()
                data["artifacts"]["blind_wire"] = {
                    "path_hint": "tmp/blind-wire.jsonl",
                    "kind": "wire_jsonl",
                    "sha256": SHA_C,
                }
                data["artifacts"]["capture_alias"] = {
                    "path_hint": (
                        "tmp/alias-capture.candump.zst"
                        if alias_kind == "sha256"
                        else "tmp/capture.candump.zst"
                    ),
                    "kind": "candump_zstd",
                    "sha256": (
                        SHA_B if alias_kind == "sha256" else SHA_C
                    ),
                }
                data["datasets"]["blind"] = {
                    **data["datasets"]["leg"],
                    "wire": "blind_wire",
                    "captures": ["capture_alias"],
                    "split": "blind_test",
                }
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "manifest.json"
                    path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(
                        benchmark.BenchmarkError,
                        "reuse artifacts across splits",
                    ):
                        benchmark.load_manifest(path)

    def test_report_search_profile_must_match_case_plan(self):
        case = manifest()["cases"][0]
        case["search"] = {
            "bit_search_ids": ["sff:100:8"],
            "lengths": [13],
            "byte_order": "big",
            "signedness": "unsigned",
            "maximum_staleness_ms": 50,
        }
        dataset = manifest()["datasets"]["leg"]
        packed_field = {
            "kind": "u13be-low5",
            "offset": 0,
            "width_bytes": 2,
            "byte_order": "big",
            "signed": False,
        }
        with self.assertRaisesRegex(
            benchmark.BenchmarkError, "staleness configuration mismatch"
        ):
            benchmark.evaluate_case(
                case, dataset, report("101B", packed_field, 0.999)
            )


if __name__ == "__main__":
    unittest.main()
