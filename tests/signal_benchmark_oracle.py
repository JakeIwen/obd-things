#!/usr/bin/env python3
"""Capture stable public behavior for the offline signal benchmark split."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable


SHA_A = "a" * 64
SHA_B = "b" * 64
WORKSPACE = Path("tmp/signal-benchmark-oracle")


class OracleError(RuntimeError):
    pass


def _environment(repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update(
        HOME=str(repo / WORKSPACE / "home"),
        XDG_CACHE_HOME=str(repo / WORKSPACE / "cache"),
        TMPDIR=str(repo / WORKSPACE / "scratch"),
        PYTHONDONTWRITEBYTECODE="1",
        TZ="UTC",
    )
    return env


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _manifest(*, maximum_rmse: float = 2.0) -> dict[str, object]:
    return {
        "schema_version": 1,
        "classification": "offline_candidate_benchmark",
        "bit_numbering": "dbc_cantools_sawtooth",
        "artifacts": {
            "wire": {
                "path_hint": "tmp/oracle-wire.jsonl",
                "kind": "wire_jsonl",
                "sha256": SHA_A,
            },
            "capture": {
                "path_hint": "tmp/oracle-capture.candump.zst",
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
                "id": "torque_proxy",
                "hypothesis": "target_torque",
                "dataset": "leg",
                "reference": {"did": "101B", "field": "u16be:0"},
                "expectation": {
                    "kind": "operational_proxy",
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
                    "intended_use": "trend",
                    "error_unit": "lb-ft",
                    "units_per_reference_raw": 0.1,
                    "minimum_coverage": 0.95,
                    "maximum_rmse": maximum_rmse,
                    "maximum_p95_absolute_error": 3.0,
                    "maximum_absolute_mean_bias": 1.0,
                },
            }
        ],
    }


def _legacy_field() -> dict[str, object]:
    return {
        "kind": "u13be-low5",
        "offset": 0,
        "width_bytes": 2,
        "byte_order": "big",
        "signed": False,
    }


def _candidate() -> dict[str, object]:
    return {
        "channel": "can0",
        "can_id": 0x100,
        "can_id_hex": "100",
        "id_bits": 11,
        "dlc": 8,
        "field": _legacy_field(),
        "classification": "candidate_only",
        "evidence_tier": "exploratory_candidate",
        "candidate_only": True,
        "physical_identity_verified": False,
        "scale_verified": False,
        "telemetry_promotion_allowed": False,
        "rank": 1,
        "sample_count": 20,
        "coverage_ratio": 1.0,
        "correlation": {"r_squared": 0.8},
    }


def _fixed_formula_evaluation() -> dict[str, object]:
    return {
        "classification": "candidate_only",
        "evidence_tier": "operational_proxy_evaluation",
        "candidate_only": True,
        "physical_identity_verified": False,
        "scale_verified": False,
        "telemetry_promotion_allowed": False,
        "result": {
            "formula": {
                "candidate": {
                    "channel": "can0",
                    "can_id_hex": "100",
                    "id_bits": 11,
                    "dlc": 8,
                    "field": _legacy_field(),
                },
                "scale": 1.0,
                "intercept": 0.0,
            },
            "sample_count": 20,
            "coverage_ratio": 1.0,
            "rmse": 10.0,
            "p95_absolute_error": 20.0,
            "absolute_mean_bias": 5.0,
        },
    }


def _report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "classification": "candidate_only",
        "evidence_tier": "exploratory_candidate",
        "candidate_only": True,
        "physical_identity_verified": False,
        "scale_verified": False,
        "telemetry_promotion_allowed": False,
        "offline_only": True,
        "analysis": {
            "maximum_candidate_staleness_ms": 100.0,
            "candidate_stream_identity": [
                "channel", "SFF/EFF namespace", "CAN ID", "DLC"
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
            "did": "101B",
            "requested_field": "u16be:0",
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
            "eligible_candidate_maximum_r_squared": 0.8,
            "candidates": [_candidate()],
        },
        "fixed_formula_evaluation": _fixed_formula_evaluation(),
    }


def _job_manifest(
    report_path: Path, *, report_sha: str | None = None
) -> dict[str, object]:
    raw = report_path.read_bytes()
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
                "sha256": SHA_B,
            },
        ],
        "results": [
            {
                "path": "report.json",
                "sha256": report_sha or hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        ],
    }


def _run(
    repo: Path,
    case_id: str,
    arguments: list[str],
    expected_exit: int,
    check: Callable[[subprocess.CompletedProcess[str]], dict[str, object]],
) -> dict[str, object]:
    command = [sys.executable, "-m", "tools.can_signal_benchmark", *arguments]
    completed = subprocess.run(
        command,
        cwd=repo,
        env=_environment(repo),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != expected_exit:
        raise OracleError(
            f"{case_id}: expected exit {expected_exit}, got "
            f"{completed.returncode}: {completed.stderr!r}"
        )
    assertions = check(completed)
    return {
        "id": case_id,
        "arguments": arguments,
        "expected_exit": expected_exit,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "assertions": assertions,
    }


def _plan_check(
    completed: subprocess.CompletedProcess[str],
    *, count: int,
    selected: str | None = None,
) -> dict[str, object]:
    if completed.stderr:
        raise OracleError(f"plan unexpectedly wrote stderr: {completed.stderr!r}")
    payload = json.loads(completed.stdout)
    if payload.get("operation") != "plan_only" or payload.get("case_count") != count:
        raise OracleError("plan payload lacks the expected operation/count")
    if payload.get("runs_full_saved_log_search") is not False:
        raise OracleError("plan unexpectedly permits a saved-log search")
    case_ids = [case.get("case_id") for case in payload.get("cases", [])]
    if selected is not None and case_ids != [selected]:
        raise OracleError(f"selected plan returned {case_ids!r}")
    return {"operation": "plan_only", "case_count": count, "case_ids": case_ids}


def _error_check(
    completed: subprocess.CompletedProcess[str], expected_text: str
) -> dict[str, object]:
    if completed.stdout or expected_text not in completed.stderr:
        raise OracleError(
            f"expected empty stdout and stderr containing {expected_text!r}"
        )
    return {"stdout_empty": True, "stderr_contains": expected_text}


def _evaluation_check(
    completed: subprocess.CompletedProcess[str],
    display_path: Path,
    output_path: Path,
    *,
    passed: bool,
    status: str,
) -> dict[str, object]:
    if completed.stderr:
        raise OracleError(f"evaluation unexpectedly wrote stderr: {completed.stderr!r}")
    expected_stdout = (
        "Wrote 1 whole-leg benchmark results to "
        f"{display_path.as_posix()}\n"
    )
    if completed.stdout != expected_stdout:
        raise OracleError(f"unexpected evaluation stdout: {completed.stdout!r}")
    raw = output_path.read_bytes()
    payload = json.loads(raw)
    case = payload.get("cases", [{}])[0]
    if payload.get("passed") is not passed or case.get("status") != status:
        raise OracleError("evaluation payload has the wrong pass/status result")
    if case.get("observed_error", {}).get("rmse") != 1.0:
        raise OracleError("evaluation did not retain the expected converted RMSE")
    return {
        "passed": passed,
        "status": status,
        "output_bytes_utf8": raw.decode("utf-8"),
    }


FIELD_CONTRACT_SCRIPT = """
import json
from tools import can_timeseries_correlate as correlate

rows = []
specs = (
    ("byte", 2),
    ("u13be-low5", 0),
    ("u17be-low1", 1),
    ("i16be", 2),
    ("u32le", 1),
)
for kind, offset in specs:
    spec = correlate.FieldSpec(kind, offset)
    field = correlate._legacy_signal_field(spec)
    rows.append({
        "spec": spec.as_dict(),
        "geometry": {
            "dbc_start_bit": field.dbc_start_bit,
            "length_bits": field.length_bits,
            "byte_order": field.byte_order,
            "signed": field.signed,
        },
    })
errors = []
for operation in (
    lambda: correlate.FieldSpec("byte", -1),
    lambda: correlate.FieldSpec("bogus", 0).width_bytes,
    lambda: correlate._legacy_signal_field(correlate.FieldSpec("bogus", 0)),
):
    try:
        operation()
    except correlate.CorrelateError as exc:
        errors.append(str(exc))
print(json.dumps({"rows": rows, "errors": errors}, sort_keys=True))
"""


def _field_contract(repo: Path) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-c", FIELD_CONTRACT_SCRIPT],
        cwd=repo,
        env=_environment(repo),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or completed.stderr:
        raise OracleError(
            f"field contract failed: exit={completed.returncode} "
            f"stderr={completed.stderr!r}"
        )
    payload = json.loads(completed.stdout)
    expected_geometry = [
        {
            "dbc_start_bit": 16,
            "length_bits": 8,
            "byte_order": "little",
            "signed": False,
        },
        {
            "dbc_start_bit": 4,
            "length_bits": 13,
            "byte_order": "big",
            "signed": False,
        },
        {
            "dbc_start_bit": 8,
            "length_bits": 17,
            "byte_order": "big",
            "signed": False,
        },
        {
            "dbc_start_bit": 23,
            "length_bits": 16,
            "byte_order": "big",
            "signed": True,
        },
        {
            "dbc_start_bit": 8,
            "length_bits": 32,
            "byte_order": "little",
            "signed": False,
        },
    ]
    if [row["geometry"] for row in payload.get("rows", [])] != expected_geometry:
        raise OracleError("legacy field geometry changed")
    expected_errors = [
        "field offset must be a non-negative integer",
        "unsupported field kind 'bogus'",
        "unsupported field kind 'bogus'",
    ]
    if payload.get("errors") != expected_errors:
        raise OracleError("legacy field failures changed")
    return payload


def _prepare_workspace(repo: Path) -> dict[str, Path]:
    work = repo / WORKSPACE
    for name in ("home", "cache", "scratch"):
        (work / name).mkdir()
    paths = {
        "proxy_manifest": work / "proxy-manifest.json",
        "failing_manifest": work / "failing-manifest.json",
        "invalid_manifest": work / "invalid-manifest.json",
        "report": work / "report.json",
        "job": work / "job.json",
        "bad_job": work / "bad-job.json",
        "pass_output": work / "pass-output.json",
        "fail_output": work / "fail-output.json",
        "digest_output": work / "digest-output.json",
    }
    _write_json(paths["proxy_manifest"], _manifest())
    _write_json(paths["failing_manifest"], _manifest(maximum_rmse=0.5))
    invalid = _manifest()
    invalid["cases"][0]["expectation"]["intended_use"] = "safety_alert"
    _write_json(paths["invalid_manifest"], invalid)
    _write_json(paths["report"], _report())
    _write_json(paths["job"], _job_manifest(paths["report"]))
    _write_json(
        paths["bad_job"],
        _job_manifest(paths["report"], report_sha="c" * 64),
    )
    return paths


def _plan_cases(repo: Path, paths: dict[str, Path]) -> list[dict[str, object]]:
    manifest = "projects/ecu_mapping/configs/signal_field_benchmark_v1.json"
    rel = lambda path: path.relative_to(repo).as_posix()
    return [
        _run(
            repo,
            "full_plan",
            ["plan", manifest],
            0,
            lambda completed: _plan_check(completed, count=26),
        ),
        _run(
            repo,
            "selected_plan",
            ["plan", manifest, "--case", "tcm_temp_development_04fe"],
            0,
            lambda completed: _plan_check(
                completed, count=1, selected="tcm_temp_development_04fe"
            ),
        ),
        _run(
            repo,
            "unknown_case",
            ["plan", manifest, "--case", "does-not-exist"],
            2,
            lambda completed: _error_check(
                completed, "unknown selected cases"
            ),
        ),
        _run(
            repo,
            "invalid_proxy_use",
            ["plan", rel(paths["invalid_manifest"])],
            2,
            lambda completed: _error_check(completed, "intended_use"),
        ),
    ]


def _evaluate_arguments(
    repo: Path,
    paths: dict[str, Path],
    manifest_key: str,
    job_key: str,
    output_key: str,
) -> list[str]:
    rel = lambda path: path.relative_to(repo).as_posix()
    return [
        "evaluate",
        rel(paths[manifest_key]),
        "--report",
        f"torque_proxy={rel(paths['report'])}",
        "--job-manifest",
        f"torque_proxy={rel(paths[job_key])}",
        "--output",
        rel(paths[output_key]),
    ]


def _evaluation_cases(
    repo: Path, paths: dict[str, Path]
) -> list[dict[str, object]]:
    rel = lambda path: path.relative_to(repo)
    cases = [
        _run(
            repo,
            "passing_proxy",
            _evaluate_arguments(
                repo, paths, "proxy_manifest", "job", "pass_output"
            ),
            0,
            lambda completed: _evaluation_check(
                completed,
                rel(paths["pass_output"]),
                paths["pass_output"],
                passed=True,
                status="qualified_operational_proxy",
            ),
        ),
        _run(
            repo,
            "failing_proxy",
            _evaluate_arguments(
                repo, paths, "failing_manifest", "job", "fail_output"
            ),
            1,
            lambda completed: _evaluation_check(
                completed,
                rel(paths["fail_output"]),
                paths["fail_output"],
                passed=False,
                status="operational_proxy_tolerance_failed",
            ),
        ),
        _run(
            repo,
            "digest_mismatch",
            _evaluate_arguments(
                repo, paths, "proxy_manifest", "bad_job", "digest_output"
            ),
            2,
            lambda completed: _error_check(
                completed, "digest does not match"
            ),
        ),
    ]
    if paths["digest_output"].exists():
        raise OracleError("digest mismatch unexpectedly created an output file")
    return cases


def capture(repo: Path) -> dict[str, object]:
    repo = repo.resolve(strict=True)
    work = repo / WORKSPACE
    # Fixed relative names make captures comparable without normalization.
    # Exclusive creation prevents overwriting another capture's evidence.
    work.mkdir(parents=True, exist_ok=False)
    try:
        paths = _prepare_workspace(repo)
        return {
            "schema_version": 1,
            "cases": [*_plan_cases(repo, paths), *_evaluation_cases(repo, paths)],
            "field_contract": _field_contract(repo),
        }
    finally:
        shutil.rmtree(work)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        payload = capture(args.repo_root)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        _write_json(args.output, payload)
        return 0
    except (OSError, subprocess.SubprocessError, ValueError, OracleError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
