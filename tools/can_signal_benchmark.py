#!/usr/bin/env python3
"""Evaluate whole-drive CAN signal-search reports against tracked expectations.

This tool performs no CAN I/O and never runs a saved-log search itself. Heavy
correlations are produced by named ``pi_compute`` tasks; this evaluator checks
their candidate-only JSON reports as independent development, validation, or
blind-test legs. Adjacent samples from one drive are never split into
train/validation sets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Sequence

from lib.signal_fields import SignalField, SignalFieldError
from lib.modules import MODULES
from tools import can_timeseries_correlate as correlate
from tools.signal_benchmark_policy import (
    ASSERTIVE_EXPECTATION_KINDS,
    CLASSIFICATION,
    EXPECTATION_KINDS,
    PROXY_INTENDED_USES,
    SCHEMA_VERSION,
    SPLITS,
    BenchmarkError,
    _CASE_ID,
    _EXACT_STREAM,
    _HEX_SHA256,
    _array,
    _candidate_geometry,
    _evaluate_operational_proxy,
    _expected_geometry,
    _finite_number,
    _object,
    _validate_case_search,
    _validate_operational_proxy_expectation,
    _validate_search_config,
    evaluate_case,
)


def _load_json_with_sha256(
    path: Path, context: str
) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(f"cannot read {context} {path}: {exc}") from exc
    if len(raw) > 32 * 1024 * 1024:
        raise BenchmarkError(f"{context} exceeds the 32 MiB safety cap")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise BenchmarkError(f"{context} is not valid JSON: {path}") from exc
    return _object(value, context), hashlib.sha256(raw).hexdigest()


def _load_json(path: Path, context: str) -> dict[str, object]:
    return _load_json_with_sha256(path, context)[0]


def load_manifest(path: Path) -> dict[str, object]:
    manifest = _load_json(path, "benchmark manifest")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BenchmarkError("unsupported benchmark manifest schema_version")
    if manifest.get("classification") != CLASSIFICATION:
        raise BenchmarkError(
            f"benchmark classification must be {CLASSIFICATION!r}"
        )
    if manifest.get("bit_numbering") != "dbc_cantools_sawtooth":
        raise BenchmarkError(
            "benchmark bit_numbering must be dbc_cantools_sawtooth"
        )
    artifacts = _object(manifest.get("artifacts"), "manifest artifacts")
    for artifact_id, raw_artifact in artifacts.items():
        if not _CASE_ID.fullmatch(artifact_id):
            raise BenchmarkError(f"invalid artifact ID {artifact_id!r}")
        artifact = _object(raw_artifact, f"artifact {artifact_id}")
        path_hint = artifact.get("path_hint")
        if (
            not isinstance(path_hint, str)
            or not path_hint
            or Path(path_hint).is_absolute()
            or ".." in Path(path_hint).parts
        ):
            raise BenchmarkError(
                f"artifact {artifact_id} path_hint must be repository-relative"
            )
        digest = artifact.get("sha256")
        if digest is not None and (
            not isinstance(digest, str) or not _HEX_SHA256.fullmatch(digest)
        ):
            raise BenchmarkError(f"artifact {artifact_id} has invalid sha256")

    datasets = _object(manifest.get("datasets"), "manifest datasets")
    artifact_identities: dict[str, set[str]] = {}
    for artifact_id, raw_artifact in artifacts.items():
        artifact = _object(raw_artifact, f"artifact {artifact_id}")
        path_hint = str(artifact["path_hint"])
        identities = {
            f"id:{artifact_id}",
            f"path:{Path(path_hint).as_posix()}",
        }
        digest = artifact.get("sha256")
        if isinstance(digest, str):
            identities.add(f"sha256:{digest}")
        artifact_identities[artifact_id] = identities

    dataset_artifacts: dict[str, set[str]] = {}
    for dataset_id, raw_dataset in datasets.items():
        if not _CASE_ID.fullmatch(dataset_id):
            raise BenchmarkError(f"invalid dataset ID {dataset_id!r}")
        dataset = _object(raw_dataset, f"dataset {dataset_id}")
        if dataset.get("split") not in SPLITS:
            raise BenchmarkError(f"dataset {dataset_id} has invalid split")
        if dataset.get("module") not in MODULES:
            raise BenchmarkError(f"dataset {dataset_id} has unknown module")
        wire = dataset.get("wire")
        if wire is not None and wire not in artifacts:
            raise BenchmarkError(f"dataset {dataset_id} has unknown wire")
        captures = _array(
            dataset.get("captures"), f"dataset {dataset_id} captures"
        )
        if not captures or any(item not in artifacts for item in captures):
            raise BenchmarkError(
                f"dataset {dataset_id} has unknown or empty captures"
            )
        if dataset.get("independent_drive_leg") is not True:
            raise BenchmarkError(
                f"dataset {dataset_id} must be one complete independent drive leg"
            )
        referenced_artifacts = [str(item) for item in captures]
        if wire is not None:
            referenced_artifacts.append(str(wire))
        dataset_artifacts[dataset_id] = set()
        for artifact_id in referenced_artifacts:
            dataset_artifacts[dataset_id].update(
                artifact_identities[artifact_id]
            )

    dataset_items = list(datasets.items())
    for index, (left_id, left_raw) in enumerate(dataset_items):
        left = _object(left_raw, f"dataset {left_id}")
        for right_id, right_raw in dataset_items[index + 1 :]:
            right = _object(right_raw, f"dataset {right_id}")
            if left["split"] == right["split"]:
                continue
            overlap = dataset_artifacts[left_id] & dataset_artifacts[right_id]
            if overlap:
                raise BenchmarkError(
                    f"datasets {left_id} and {right_id} reuse artifacts "
                    "across splits via matching identities: "
                    f"{', '.join(sorted(overlap))}"
                )

    cases = _array(manifest.get("cases"), "manifest cases")
    seen_cases: set[str] = set()
    for index, raw_case in enumerate(cases):
        case = _object(raw_case, f"case {index}")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            raise BenchmarkError(f"case {index} has invalid ID")
        if case_id in seen_cases:
            raise BenchmarkError(f"duplicate case ID {case_id}")
        seen_cases.add(case_id)
        if case.get("dataset") not in datasets:
            raise BenchmarkError(f"case {case_id} has unknown dataset")
        reference = _object(case.get("reference"), f"case {case_id} reference")
        if (
            not isinstance(reference.get("did"), str)
            or not re.fullmatch(r"[0-9A-F]{4}", reference["did"])
            or not isinstance(reference.get("field"), str)
        ):
            raise BenchmarkError(f"case {case_id} has invalid reference")
        _validate_case_search(case, case_id)
        expectation = _object(
            case.get("expectation"), f"case {case_id} expectation"
        )
        if expectation.get("kind") not in EXPECTATION_KINDS:
            raise BenchmarkError(f"case {case_id} has invalid expectation kind")
        if expectation.get("kind") in (
            "verified_positive",
            "carrier_only",
            "operational_proxy",
        ):
            _expected_geometry(expectation, case_id)
        if expectation.get("kind") == "operational_proxy":
            _validate_operational_proxy_expectation(expectation, case_id)
    return manifest


def _validate_job_manifest(
    case_id: str,
    dataset: dict[str, object],
    artifacts: dict[str, object],
    job_manifest: dict[str, object],
) -> tuple[list[dict[str, object]], str]:
    if job_manifest.get("schema_version") != 1:
        raise BenchmarkError(
            f"case {case_id} compute job has unsupported schema_version"
        )
    wire = dataset.get("wire")
    if wire is None:
        raise BenchmarkError(
            f"case {case_id} dataset has no exact diagnostic wire artifact"
        )
    expected_ids = [wire, *_array(dataset["captures"], "dataset captures")]
    expected_hashes = []
    for artifact_id in expected_ids:
        digest = _object(
            artifacts[artifact_id], f"artifact {artifact_id}"
        ).get("sha256")
        if not isinstance(digest, str) or not _HEX_SHA256.fullmatch(digest):
            raise BenchmarkError(
                f"case {case_id} artifact {artifact_id} lacks a pinned sha256"
            )
        expected_hashes.append(digest)
    inputs = [
        _object(item, f"case {case_id} compute input")
        for item in _array(
            job_manifest.get("inputs"), f"case {case_id} compute inputs"
        )
    ]
    for index, item in enumerate(inputs):
        if item.get("index") != index or not isinstance(item.get("name"), str):
            raise BenchmarkError(
                f"case {case_id} compute inputs lack deterministic indexes/names"
            )
    observed_hashes = [item.get("sha256") for item in inputs]
    if observed_hashes != expected_hashes:
        raise BenchmarkError(
            f"case {case_id} compute inputs do not match its dataset artifacts"
        )
    if (
        job_manifest.get("state") != "done"
        or job_manifest.get("exit_code") != 0
        or job_manifest.get("task") != dataset.get("compute_task")
    ):
        raise BenchmarkError(
            f"case {case_id} compute job did not complete the planned task"
        )
    results = [
        _object(item, f"case {case_id} compute result")
        for item in _array(
            job_manifest.get("results"),
            f"case {case_id} compute results",
        )
    ]
    report_results = [
        item for item in results if item.get("path") == "report.json"
    ]
    if len(report_results) != 1:
        raise BenchmarkError(
            f"case {case_id} compute job must declare exactly one report.json result"
        )
    report_digest = report_results[0].get("sha256")
    if (
        not isinstance(report_digest, str)
        or not _HEX_SHA256.fullmatch(report_digest)
    ):
        raise BenchmarkError(
            f"case {case_id} compute report result lacks a valid sha256"
        )
    return inputs, report_digest


def _validate_report_job_binding(
    case_id: str,
    report: dict[str, object],
    inputs: list[dict[str, object]],
) -> None:
    reference = _object(
        report.get("reference"), f"case {case_id} report reference"
    )
    reference_source = _object(
        reference.get("source"), f"case {case_id} reference source"
    )
    capture = _object(report.get("capture"), f"case {case_id} capture")
    capture_sources = [
        _object(item, f"case {case_id} capture source")
        for item in _array(
            capture.get("sources"), f"case {case_id} capture sources"
        )
    ]
    observed_paths = [
        reference_source.get("path"),
        *(source.get("path") for source in capture_sources),
    ]
    expected_paths = [
        f"{{job}}/inputs/{index:03d}-{item['name']}"
        for index, item in enumerate(inputs)
    ]
    if observed_paths != expected_paths:
        raise BenchmarkError(
            f"case {case_id} report sources are not bound to its compute job"
        )


def _case_map(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(case["id"]): case
        for case in (
            _object(item, "benchmark case")
            for item in _array(manifest["cases"], "manifest cases")
        )
    }


def build_plan(
    manifest: dict[str, object], selected_cases: set[str] | None = None
) -> dict[str, object]:
    datasets = _object(manifest["datasets"], "manifest datasets")
    planned = []
    for case_id, case in _case_map(manifest).items():
        if selected_cases is not None and case_id not in selected_cases:
            continue
        dataset = _object(datasets[case["dataset"]], "dataset")
        expectation = _object(case["expectation"], "expectation")
        expectation_kind = expectation["kind"]
        planned.append(
            {
                "case_id": case_id,
                "hypothesis": case.get("hypothesis"),
                "split": dataset["split"],
                "independent_drive_leg": dataset["independent_drive_leg"],
                "compute_status": dataset.get("compute_status"),
                "compute_task": dataset.get("compute_task"),
                "module": dataset["module"],
                "reference": case["reference"],
                "search": case.get("search", {}),
                "expectation_kind": expectation_kind,
                "target_evidence_tier": (
                    "operational_proxy"
                    if expectation_kind == "operational_proxy"
                    else "verified_decode_candidate"
                    if expectation_kind in ("verified_positive", "carrier_only")
                    else "exploratory_candidate"
                ),
                **(
                    {
                        "intended_use": expectation["intended_use"],
                        "error_unit": expectation["error_unit"],
                    }
                    if expectation_kind == "operational_proxy"
                    else {}
                ),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "classification": CLASSIFICATION,
        "operation": "plan_only",
        "offline_only": True,
        "runs_full_saved_log_search": False,
        "sample_split_policy": "whole_independent_drive_legs_only",
        "case_count": len(planned),
        "cases": planned,
    }


def build_evaluation(
    manifest: dict[str, object],
    report_paths: dict[str, Path],
    job_manifest_paths: dict[str, Path],
) -> dict[str, object]:
    cases = _case_map(manifest)
    datasets = _object(manifest["datasets"], "manifest datasets")
    unknown = set(report_paths) - set(cases)
    if unknown:
        raise BenchmarkError(
            "reports reference unknown cases: " + ", ".join(sorted(unknown))
        )
    if set(job_manifest_paths) != set(report_paths):
        raise BenchmarkError(
            "every report requires exactly one matching compute job manifest"
        )
    artifacts = _object(manifest["artifacts"], "manifest artifacts")
    results = []
    for case_id, report_path in sorted(report_paths.items()):
        case = cases[case_id]
        kind = _object(case["expectation"], "expectation")["kind"]
        if kind not in ASSERTIVE_EXPECTATION_KINDS:
            raise BenchmarkError(
                f"case {case_id} is {kind} and is not evaluable"
            )
        dataset = _object(datasets[case["dataset"]], "dataset")
        job_manifest = _load_json(
            job_manifest_paths[case_id],
            f"compute job manifest for {case_id}",
        )
        inputs, expected_report_digest = _validate_job_manifest(
            case_id,
            dataset,
            artifacts,
            job_manifest,
        )
        report, observed_report_digest = _load_json_with_sha256(
            report_path, f"report for {case_id}"
        )
        if observed_report_digest != expected_report_digest:
            raise BenchmarkError(
                f"case {case_id} supplied report digest does not match "
                "the compute manifest result"
            )
        _validate_report_job_binding(case_id, report, inputs)
        results.append(
            evaluate_case(
                case,
                dataset,
                report,
            )
        )
    hypotheses: dict[str, dict[str, object]] = {}
    planned_by_hypothesis: dict[str, list[dict[str, object]]] = {}
    for case in cases.values():
        hypothesis = case.get("hypothesis")
        if isinstance(hypothesis, str):
            planned_by_hypothesis.setdefault(hypothesis, []).append(case)
    for result in results:
        hypothesis = result.get("hypothesis")
        if not isinstance(hypothesis, str):
            continue
        summary = hypotheses.setdefault(
            hypothesis,
            {"evaluated_splits": [], "case_ids": [], "passed": True},
        )
        summary["evaluated_splits"].append(result["split"])
        summary["case_ids"].append(result["case_id"])
        summary["passed"] = bool(summary["passed"]) and bool(result["passed"])
    for hypothesis, planned_cases in planned_by_hypothesis.items():
        summary = hypotheses.setdefault(
            hypothesis,
            {"evaluated_splits": [], "case_ids": [], "passed": True},
        )
        planned_ids = {str(case["id"]) for case in planned_cases}
        evaluated_ids = set(summary["case_ids"])
        planned_splits = {
            str(
                _object(
                    datasets[case["dataset"]], "dataset"
                )["split"]
            )
            for case in planned_cases
        }
        evaluated_splits = set(summary["evaluated_splits"])
        summary["planned_splits"] = sorted(planned_splits)
        summary["evaluated_splits"] = sorted(evaluated_splits)
        summary["missing_case_ids"] = sorted(planned_ids - evaluated_ids)
        summary["all_planned_cases_evaluated"] = planned_ids == evaluated_ids
        summary["independent_holdout_evaluated"] = bool(
            evaluated_splits & {"validation", "blind_test"}
        )
        summary["heldout_passed"] = (
            summary["independent_holdout_evaluated"]
            and bool(summary["passed"])
        )
    benchmark_complete = (
        bool(cases)
        and all(
            _object(case["expectation"], "expectation")["kind"]
            in ASSERTIVE_EXPECTATION_KINDS
            for case in cases.values()
        )
        and set(report_paths) == set(cases)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "classification": CLASSIFICATION,
        "operation": "evaluate_reports",
        "offline_only": True,
        "candidate_only": True,
        "telemetry_promotion_allowed": False,
        "evidence_policy": (
            "operational proxies may pass explicit whole-leg tolerances "
            "without asserting physical identity or verified-decode promotion"
        ),
        "sample_split_policy": "whole_independent_drive_legs_only",
        "evaluated_case_count": len(results),
        "qualified_operational_proxy_count": sum(
            result.get("evidence_tier") == "operational_proxy"
            and result.get("passed") is True
            for result in results
        ),
        "passed": bool(results) and all(result["passed"] for result in results),
        "benchmark_complete": benchmark_complete,
        "cases": results,
        "hypotheses": hypotheses,
    }


def _report_assignment(value: str) -> tuple[str, Path]:
    case_id, separator, path = value.partition("=")
    if not separator or not _CASE_ID.fullmatch(case_id) or not path:
        raise argparse.ArgumentTypeError("report must be CASE_ID=PATH")
    return case_id, Path(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or evaluate whole-drive, offline CAN signal benchmarks; "
            "never runs full saved-log searches on vanpi."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("manifest", type=Path)
    plan.add_argument("--case", action="append", default=[])

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("manifest", type=Path)
    evaluate.add_argument(
        "--report",
        action="append",
        type=_report_assignment,
        required=True,
        metavar="CASE_ID=PATH",
    )
    evaluate.add_argument(
        "--job-manifest",
        action="append",
        type=_report_assignment,
        required=True,
        metavar="CASE_ID=PATH",
        help="matching pi_compute manifest that pins the report's inputs",
    )
    evaluate.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "plan":
            selected = set(args.case) if args.case else None
            if selected is not None:
                unknown = selected - set(_case_map(manifest))
                if unknown:
                    raise BenchmarkError(
                        "unknown selected cases: " + ", ".join(sorted(unknown))
                    )
            payload = build_plan(manifest, selected)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        assignments = dict(args.report)
        if len(assignments) != len(args.report):
            raise BenchmarkError("each report case may be assigned only once")
        job_assignments = dict(args.job_manifest)
        if len(job_assignments) != len(args.job_manifest):
            raise BenchmarkError(
                "each compute job manifest case may be assigned only once"
            )
        payload = build_evaluation(
            manifest, assignments, job_assignments
        )
        correlate._exclusive_write_json(args.output, payload)
        print(
            f"Wrote {payload['evaluated_case_count']} whole-leg benchmark "
            f"results to {args.output}"
        )
        return 0 if payload["passed"] else 1
    except (BenchmarkError, correlate.CorrelateError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
