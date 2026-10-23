"""Pure per-case policy for offline CAN signal benchmarks."""

from __future__ import annotations

import math
import re

from lib.signal_fields import SignalField, SignalFieldError
from lib.signal_search_contract import (
    CorrelateError,
    EXPLORATORY_EVIDENCE_TIER,
    FieldSpec,
    MAX_BIT_SEARCH_IDENTIFIERS,
    MAX_RADIUS_MS,
    SCHEMA_VERSION as CORRELATE_SCHEMA_VERSION,
    legacy_signal_field,
)


SCHEMA_VERSION = 1
CLASSIFICATION = "offline_candidate_benchmark"
SPLITS = frozenset(("development", "validation", "blind_test"))
EXPECTATION_KINDS = frozenset(
    (
        "verified_positive",
        "carrier_only",
        "no_defensible_match",
        "operational_proxy",
        "proxy_challenge",
        "pending",
    )
)
ASSERTIVE_EXPECTATION_KINDS = frozenset(
    (
        "verified_positive",
        "carrier_only",
        "no_defensible_match",
        "operational_proxy",
    )
)
PROXY_INTENDED_USES = frozenset(
    ("trend", "state_detection", "approximate_display")
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
_EXACT_STREAM = re.compile(
    r"(?:sff:[0-9A-F]{3}:[1-8]|eff:[0-9A-F]{8}:[1-8])"
)


class BenchmarkError(RuntimeError):
    """Raised when a manifest or candidate report violates the benchmark gate."""


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BenchmarkError(f"{context} must be an object")
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise BenchmarkError(f"{context} must be an array")
    return value


def _validate_case_search(case: dict[str, object], case_id: str) -> None:
    search = _object(case.get("search", {}), f"case {case_id} search")
    staleness = _finite_number(
        search.get("maximum_staleness_ms", 100),
        f"case {case_id} maximum_staleness_ms",
    )
    if not 0.0 < staleness <= MAX_RADIUS_MS:
        raise BenchmarkError(
            f"case {case_id} maximum staleness is outside the analyzer bounds"
        )
    streams = _array(
        search.get("bit_search_ids", []),
        f"case {case_id} bit_search_ids",
    )
    if (
        len(streams) > MAX_BIT_SEARCH_IDENTIFIERS
        or any(
            not isinstance(item, str) or not _EXACT_STREAM.fullmatch(item)
            for item in streams
        )
        or len(set(streams)) != len(streams)
    ):
        raise BenchmarkError(
            f"case {case_id} has invalid or duplicate exact bit-search streams"
        )
    lengths = _array(search.get("lengths", []), f"case {case_id} lengths")
    if (
        any(type(item) is not int or not 1 <= item <= 32 for item in lengths)
        or len(set(lengths)) != len(lengths)
    ):
        raise BenchmarkError(f"case {case_id} has invalid bit-search lengths")
    if streams:
        if search.get("byte_order", "both") not in ("little", "big", "both"):
            raise BenchmarkError(
                f"case {case_id} has invalid bit-search byte order"
            )
        if search.get("signedness", "both") not in (
            "unsigned",
            "signed",
            "both",
        ):
            raise BenchmarkError(
                f"case {case_id} has invalid bit-search signedness"
            )
    elif any(
        key in search for key in ("lengths", "byte_order", "signedness")
    ):
        raise BenchmarkError(
            f"case {case_id} bit-search options require bit_search_ids"
        )


def _expected_geometry(
    expectation: dict[str, object], case_id: str
) -> SignalField:
    field = _object(expectation.get("field"), f"case {case_id} field")
    try:
        return SignalField(
            dbc_start_bit=int(field["dbc_start_bit"]),
            length_bits=int(field["length_bits"]),
            byte_order=str(field["byte_order"]),
            signed=field["signed"],
        )
    except (KeyError, TypeError, ValueError, SignalFieldError) as exc:
        raise BenchmarkError(
            f"case {case_id} has invalid expected field geometry"
        ) from exc


def _candidate_geometry(row: dict[str, object]) -> SignalField:
    field = _object(row.get("field"), "candidate field")
    try:
        if "dbc_start_bit" in field:
            return SignalField(
                int(field["dbc_start_bit"]),
                int(field["length_bits"]),
                str(field["byte_order"]),
                field["signed"],
            )
        spec = FieldSpec(str(field["kind"]), int(field["offset"]))
        return legacy_signal_field(spec)
    except (
        KeyError,
        TypeError,
        ValueError,
        SignalFieldError,
        CorrelateError,
    ) as exc:
        raise BenchmarkError("candidate report has invalid field geometry") from exc


def _finite_number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise BenchmarkError(f"{context} must be finite")
    return number


def _validate_operational_proxy_expectation(
    expectation: dict[str, object], case_id: str
) -> None:
    intended_use = expectation.get("intended_use")
    if intended_use not in PROXY_INTENDED_USES:
        choices = ", ".join(sorted(PROXY_INTENDED_USES))
        raise BenchmarkError(
            f"case {case_id} operational proxy intended_use must be one of "
            f"{choices}"
        )
    error_unit = expectation.get("error_unit")
    if (
        not isinstance(error_unit, str)
        or not error_unit.strip()
        or len(error_unit) > 64
    ):
        raise BenchmarkError(
            f"case {case_id} operational proxy requires a short error_unit"
        )
    units_per_raw = _finite_number(
        expectation.get("units_per_reference_raw", 1.0),
        f"case {case_id} units_per_reference_raw",
    )
    if units_per_raw <= 0:
        raise BenchmarkError(
            f"case {case_id} units_per_reference_raw must be positive"
        )
    minimum_coverage = _finite_number(
        expectation.get("minimum_coverage", 0.9),
        f"case {case_id} minimum_coverage",
    )
    if not 0.0 < minimum_coverage <= 1.0:
        raise BenchmarkError(
            f"case {case_id} minimum_coverage must be in (0, 1]"
        )
    for key in ("maximum_rmse", "maximum_p95_absolute_error"):
        threshold = _finite_number(
            expectation.get(key), f"case {case_id} {key}"
        )
        if threshold < 0:
            raise BenchmarkError(
                f"case {case_id} {key} must be nonnegative"
            )
    if "maximum_absolute_mean_bias" in expectation:
        maximum_bias = _finite_number(
            expectation["maximum_absolute_mean_bias"],
            f"case {case_id} maximum_absolute_mean_bias",
        )
        if maximum_bias < 0:
            raise BenchmarkError(
                f"case {case_id} maximum_absolute_mean_bias must be "
                "nonnegative"
            )


def _validate_search_config(
    case_id: str,
    case: dict[str, object],
    report: dict[str, object],
) -> None:
    search = _object(case.get("search", {}), f"case {case_id} search")
    analysis = _object(report.get("analysis"), f"case {case_id} analysis")
    expected_staleness = _finite_number(
        search.get("maximum_staleness_ms", 100),
        f"case {case_id} planned maximum staleness",
    )
    observed_staleness = _finite_number(
        analysis.get("maximum_candidate_staleness_ms"),
        f"case {case_id} maximum candidate staleness",
    )
    if observed_staleness != expected_staleness:
        raise BenchmarkError(f"case {case_id} staleness configuration mismatch")

    profile = _object(
        analysis.get("candidate_field_profile"),
        f"case {case_id} candidate field profile",
    )
    raw_expected_streams = search.get("bit_search_ids", [])
    if not isinstance(raw_expected_streams, list) or not all(
        isinstance(item, str) for item in raw_expected_streams
    ):
        raise BenchmarkError(
            f"case {case_id} bit_search_ids must be an array of strings"
        )
    expected_streams = sorted(raw_expected_streams)
    observed_streams = profile.get("targeted_bit_search_streams")
    if (
        not isinstance(observed_streams, list)
        or not all(isinstance(item, str) for item in observed_streams)
        or sorted(observed_streams) != expected_streams
    ):
        raise BenchmarkError(
            f"case {case_id} targeted bit-search stream mismatch"
        )
    if expected_streams:
        expected_lengths = search.get("lengths", [])
        if (
            not isinstance(expected_lengths, list)
            or profile.get("selected_lengths") != expected_lengths
        ):
            raise BenchmarkError(
                f"case {case_id} bit-search length mismatch"
            )
        expected_orders = {
            "little": ["little"],
            "big": ["big"],
            "both": ["little", "big"],
        }.get(search.get("byte_order", "both"))
        if profile.get("byte_orders") != expected_orders:
            raise BenchmarkError(
                f"case {case_id} bit-search byte-order mismatch"
            )
        expected_signedness = {
            "unsigned": ["unsigned"],
            "signed": ["signed"],
            "both": ["unsigned", "signed"],
        }.get(search.get("signedness", "both"))
        if profile.get("signedness") != expected_signedness:
            raise BenchmarkError(
                f"case {case_id} bit-search signedness mismatch"
            )


def _validated_proxy_candidate(
    case_id: str,
    expectation: dict[str, object],
    report: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    evaluation = _object(
        report.get("fixed_formula_evaluation"),
        f"case {case_id} fixed formula evaluation",
    )
    if (
        evaluation.get("classification") != "candidate_only"
        or evaluation.get("candidate_only") is not True
        or evaluation.get("physical_identity_verified") is not False
        or evaluation.get("scale_verified") is not False
        or evaluation.get("telemetry_promotion_allowed") is not False
    ):
        raise BenchmarkError(
            f"case {case_id} fixed formula violates candidate-only gates"
        )
    result = _object(
        evaluation.get("result"), f"case {case_id} fixed formula result"
    )
    formula = _object(
        result.get("formula"), f"case {case_id} fixed formula"
    )
    _finite_number(
        formula.get("scale"), f"case {case_id} fixed formula scale"
    )
    _finite_number(
        formula.get("intercept"), f"case {case_id} fixed formula intercept"
    )
    candidate = _object(
        formula.get("candidate"), f"case {case_id} fixed formula candidate"
    )
    stream = _object(expectation.get("stream"), f"case {case_id} stream")
    if (
        candidate.get("channel") != stream.get("channel")
        or candidate.get("can_id_hex") != stream.get("can_id")
        or candidate.get("id_bits") != stream.get("id_bits")
        or candidate.get("dlc") != stream.get("dlc")
    ):
        raise BenchmarkError(
            f"case {case_id} fixed formula candidate stream mismatch"
        )
    expected_field = _expected_geometry(expectation, case_id)
    observed_field = _candidate_geometry(candidate)
    if (
        observed_field.signed != expected_field.signed
        or observed_field.value_signature() != expected_field.value_signature()
    ):
        raise BenchmarkError(
            f"case {case_id} fixed formula candidate geometry mismatch"
        )
    return result, candidate


def _proxy_observation(
    case_id: str,
    expectation: dict[str, object],
    result: dict[str, object],
) -> tuple[float, int, float, dict[str, float]]:
    coverage = _finite_number(
        result.get("coverage_ratio"), f"case {case_id} proxy coverage_ratio"
    )
    if not 0.0 <= coverage <= 1.0:
        raise BenchmarkError(
            f"case {case_id} proxy coverage_ratio must be in [0, 1]"
        )
    sample_count = result.get("sample_count")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count <= 0
    ):
        raise BenchmarkError(
            f"case {case_id} proxy sample_count must be positive"
        )
    units_per_raw = _finite_number(
        expectation.get("units_per_reference_raw", 1.0),
        f"case {case_id} units_per_reference_raw",
    )
    raw_metrics = {
        "rmse": _finite_number(
            result.get("rmse"), f"case {case_id} proxy rmse"
        ),
        "p95_absolute_error": _finite_number(
            result.get("p95_absolute_error"),
            f"case {case_id} proxy p95_absolute_error",
        ),
        "absolute_mean_bias": _finite_number(
            result.get("absolute_mean_bias"),
            f"case {case_id} proxy absolute_mean_bias",
        ),
    }
    if any(value < 0 for value in raw_metrics.values()):
        raise BenchmarkError(
            f"case {case_id} proxy error metrics must be nonnegative"
        )
    observed = {
        key: value * units_per_raw for key, value in raw_metrics.items()
    }
    return coverage, sample_count, units_per_raw, observed


def _proxy_limits(
    case_id: str, expectation: dict[str, object]
) -> tuple[dict[str, float], float | None]:
    limits = {
        "minimum_coverage": _finite_number(
            expectation.get("minimum_coverage", 0.9),
            f"case {case_id} minimum_coverage",
        ),
        "maximum_rmse": _finite_number(
            expectation.get("maximum_rmse"),
            f"case {case_id} maximum_rmse",
        ),
        "maximum_p95_absolute_error": _finite_number(
            expectation.get("maximum_p95_absolute_error"),
            f"case {case_id} maximum_p95_absolute_error",
        ),
    }
    bias_limit = None
    if "maximum_absolute_mean_bias" in expectation:
        bias_limit = _finite_number(
            expectation["maximum_absolute_mean_bias"],
            f"case {case_id} maximum_absolute_mean_bias",
        )
        limits["maximum_absolute_mean_bias"] = bias_limit
    return limits, bias_limit


def _evaluate_operational_proxy(
    case_id: str,
    expectation: dict[str, object],
    report: dict[str, object],
) -> dict[str, object]:
    _validate_operational_proxy_expectation(expectation, case_id)
    proxy_result, candidate = _validated_proxy_candidate(
        case_id, expectation, report
    )
    coverage, sample_count, units_per_raw, observed = _proxy_observation(
        case_id, expectation, proxy_result
    )
    limits, bias_limit = _proxy_limits(case_id, expectation)
    passed = (
        coverage >= limits["minimum_coverage"]
        and observed["rmse"] <= limits["maximum_rmse"]
        and observed["p95_absolute_error"]
        <= limits["maximum_p95_absolute_error"]
        and (
            bias_limit is None
            or observed["absolute_mean_bias"] <= bias_limit
        )
    )
    return {
        "passed": passed,
        "status": (
            "qualified_operational_proxy"
            if passed
            else "operational_proxy_tolerance_failed"
        ),
        "evidence_tier": (
            "operational_proxy" if passed else EXPLORATORY_EVIDENCE_TIER
        ),
        "operational_proxy_use_allowed": passed,
        "physical_identity_verified": False,
        "telemetry_promotion_allowed": False,
        "intended_use": expectation["intended_use"],
        "error_unit": expectation["error_unit"],
        "units_per_reference_raw": units_per_raw,
        "sample_count": sample_count,
        "coverage_ratio": coverage,
        "observed_error": observed,
        "limits": limits,
        "field": candidate["field"],
        "stream": {
            "channel": candidate["channel"],
            "can_id": candidate["can_id_hex"],
            "id_bits": candidate["id_bits"],
            "dlc": candidate["dlc"],
        },
    }


def _validate_report_source(
    case_id: str, source: object, context: str
) -> None:
    item = _object(source, context)
    if (
        not isinstance(item.get("path"), str)
        or not isinstance(item.get("decompressed_stream_sha256"), str)
        or not _HEX_SHA256.fullmatch(item["decompressed_stream_sha256"])
    ):
        raise BenchmarkError(
            f"case {case_id} lacks exact {context} provenance"
        )


def _validate_report_evidence(
    case_id: str,
    case: dict[str, object],
    dataset: dict[str, object],
    report: dict[str, object],
) -> None:
    reference = _object(case["reference"], f"case {case_id} reference")
    if (
        report.get("schema_version") != CORRELATE_SCHEMA_VERSION
        or report.get("classification") != "candidate_only"
        or report.get("candidate_only") is not True
        or report.get("physical_identity_verified") is not False
        or report.get("scale_verified") is not False
        or report.get("telemetry_promotion_allowed") is not False
        or report.get("offline_only") is not True
    ):
        raise BenchmarkError(
            f"case {case_id} report is not a current candidate-only report"
        )
    report_reference = _object(
        report.get("reference"), f"case {case_id} report reference"
    )
    module = _object(
        report_reference.get("module"), f"case {case_id} report module"
    )
    if (
        module.get("key") != dataset["module"]
        or report_reference.get("did") != reference["did"]
        or report_reference.get("requested_field") != reference["field"]
    ):
        raise BenchmarkError(f"case {case_id} report reference mismatch")
    linkage = _object(
        report_reference.get("global_candump_linkage"),
        f"case {case_id} linkage",
    )
    if (
        linkage.get("required") is not True
        or linkage.get("linked_sample_count")
        != linkage.get("verified_sample_count")
        or linkage.get("verified_sample_count")
        != report_reference.get("sample_count")
        or not isinstance(report_reference.get("sample_count"), int)
        or report_reference["sample_count"] <= 0
    ):
        raise BenchmarkError(f"case {case_id} lacks exact wire linkage")
    _validate_search_config(case_id, case, report)
    analysis = _object(report.get("analysis"), f"case {case_id} analysis")
    stream_identity = _array(
        analysis.get("candidate_stream_identity"),
        f"case {case_id} stream identity",
    )
    for required in ("channel", "SFF/EFF namespace", "CAN ID", "DLC"):
        if required not in stream_identity:
            raise BenchmarkError(
                f"case {case_id} report lacks {required} stream identity"
            )
    _finite_number(
        analysis.get("maximum_candidate_staleness_ms"),
        f"case {case_id} maximum candidate staleness",
    )
    _validate_report_sources(case_id, dataset, report_reference, report)


def _validate_report_sources(
    case_id: str,
    dataset: dict[str, object],
    reference: dict[str, object],
    report: dict[str, object],
) -> None:
    _validate_report_source(
        case_id, reference.get("source"), "reference source"
    )
    capture = _object(report.get("capture"), f"case {case_id} capture")
    sources = _array(
        capture.get("sources"), f"case {case_id} capture sources"
    )
    if not sources:
        raise BenchmarkError(f"case {case_id} has no capture provenance")
    for index, source in enumerate(sources):
        _validate_report_source(case_id, source, f"capture source {index}")
    if len(sources) != len(dataset["captures"]):
        raise BenchmarkError(
            f"case {case_id} capture source count does not match its dataset"
        )


def _validated_candidates(
    case_id: str, report: dict[str, object]
) -> tuple[dict[str, object], list[dict[str, object]]]:
    ranking = _object(report.get("ranking"), f"case {case_id} ranking")
    candidates = [
        _object(item, f"case {case_id} candidate")
        for item in _array(
            ranking.get("candidates"), f"case {case_id} candidates"
        )
    ]
    for row in candidates:
        if (
            row.get("classification") != "candidate_only"
            or row.get("candidate_only") is not True
            or row.get("physical_identity_verified") is not False
            or row.get("scale_verified") is not False
            or row.get("telemetry_promotion_allowed") is not False
        ):
            raise BenchmarkError(
                f"case {case_id} candidate row violates candidate-only gates"
            )
    return ranking, candidates


def _base_result(
    case: dict[str, object], dataset: dict[str, object], kind: object
) -> dict[str, object]:
    return {
        "case_id": str(case["id"]),
        "hypothesis": case.get("hypothesis"),
        "split": dataset["split"],
        "expectation_kind": kind,
        "evidence_tier": EXPLORATORY_EVIDENCE_TIER,
        "operational_proxy_use_allowed": False,
        "telemetry_promotion_allowed": False,
        "passed": False,
    }


def _evaluate_negative_control(
    case_id: str,
    expectation: dict[str, object],
    ranking: dict[str, object],
    candidates: list[dict[str, object]],
) -> dict[str, object]:
    maximum = _finite_number(
        expectation.get("maximum_r_squared", 0.90),
        f"case {case_id} maximum_r_squared",
    )
    if "eligible_candidate_maximum_r_squared" not in ranking:
        raise BenchmarkError(
            f"case {case_id} report lacks the complete-search maximum r_squared"
        )
    observed_value = ranking.get("eligible_candidate_maximum_r_squared")
    if observed_value is None:
        if candidates:
            raise BenchmarkError(
                f"case {case_id} reports candidates but a null "
                "complete-search maximum r_squared"
            )
        return {
            "passed": True,
            "status": "negative_control_empty_eligible_set",
            "maximum_observed_r_squared": None,
            "allowed_maximum_r_squared": maximum,
        }
    observed = _finite_number(
        observed_value, f"case {case_id} complete-search maximum r_squared"
    )
    return {
        "passed": observed <= maximum,
        "status": "negative_control",
        "maximum_observed_r_squared": observed,
        "allowed_maximum_r_squared": maximum,
    }


def _evaluate_expected_field(
    case_id: str,
    expectation: dict[str, object],
    candidates: list[dict[str, object]],
) -> dict[str, object]:
    expected_field = _expected_geometry(expectation, case_id)
    expected_signature = expected_field.value_signature()
    stream = _object(expectation.get("stream"), f"case {case_id} stream")
    matches = []
    for row in candidates:
        if (
            row.get("channel") == stream.get("channel")
            and row.get("can_id_hex") == stream.get("can_id")
            and row.get("id_bits") == stream.get("id_bits")
            and row.get("dlc") == stream.get("dlc")
        ):
            geometry = _candidate_geometry(row)
            if (
                geometry.signed == expected_field.signed
                and geometry.value_signature() == expected_signature
            ):
                matches.append(row)
    if not matches:
        return {"status": "expected_field_not_reported"}
    row = min(matches, key=lambda item: int(item["rank"]))
    minimum_r_squared = _finite_number(
        expectation.get("minimum_r_squared", 0.0),
        f"case {case_id} minimum_r_squared",
    )
    minimum_coverage = _finite_number(
        expectation.get("minimum_coverage", 0.0),
        f"case {case_id} minimum_coverage",
    )
    maximum_rank = int(expectation.get("maximum_rank", 100))
    observed_r_squared = _finite_number(
        _object(row["correlation"], "correlation")["r_squared"],
        "candidate r_squared",
    )
    observed_coverage = _finite_number(
        row["coverage_ratio"], "candidate coverage"
    )
    passed = (
        int(row["rank"]) <= maximum_rank
        and observed_r_squared >= minimum_r_squared
        and observed_coverage >= minimum_coverage
    )
    return {
        "passed": passed,
        "status": "expected_field_evaluated",
        "rank": row["rank"],
        "r_squared": observed_r_squared,
        "coverage_ratio": observed_coverage,
        "field": row["field"],
        "stream": {
            "channel": row["channel"],
            "can_id": row["can_id_hex"],
            "id_bits": row["id_bits"],
            "dlc": row["dlc"],
        },
    }


def evaluate_case(
    case: dict[str, object],
    dataset: dict[str, object],
    report: dict[str, object],
) -> dict[str, object]:
    case_id = str(case["id"])
    expectation = _object(case["expectation"], f"case {case_id} expectation")
    _validate_report_evidence(case_id, case, dataset, report)
    ranking, candidates = _validated_candidates(case_id, report)
    kind = expectation["kind"]
    result = _base_result(case, dataset, kind)
    if kind == "pending":
        result.update({"passed": False, "status": "pending_not_evaluated"})
    elif kind == "no_defensible_match":
        result.update(
            _evaluate_negative_control(
                case_id, expectation, ranking, candidates
            )
        )
    elif kind == "proxy_challenge":
        result.update(
            {
                "passed": False,
                "status": "non_asserting_proxy_challenge",
                "candidate_count": len(candidates),
            }
        )
    elif kind == "operational_proxy":
        result.update(
            _evaluate_operational_proxy(case_id, expectation, report)
        )
    else:
        result.update(
            _evaluate_expected_field(case_id, expectation, candidates)
        )
    return result
