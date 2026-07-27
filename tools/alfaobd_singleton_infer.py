#!/usr/bin/env python3
"""Infer candidate AlfaOBD renderings from a schema-2 singleton join report.

This tool is strictly offline.  It consumes only the JSON emitted by
``alfaobd_singleton_join.py`` and never opens ADB, SocketCAN, a service, the
network, or a subprocess.

The input preserves ordered AlfaOBD Info values and ordered authoritative wire
responses, but the Android Info rows have no timestamps.  Results therefore
remain candidates for AlfaOBD's observed rendering.  They do not establish a
physical scale, a complete enum, or promotion-ready vehicle telemetry.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Iterable, Sequence


REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO / "tmp" / "sweeps"
DEFAULT_MAX_REPORT_BYTES = 64 * 1024**2
DEFAULT_MAX_SEGMENTS = 64
DEFAULT_MAX_SAMPLES_PER_SEGMENT = 20_000
DEFAULT_MAX_HYPOTHESES = 20_000
DEFAULT_MIN_SAMPLES = 12
DEFAULT_MIN_DISTINCT = 3
DEFAULT_MAX_LAG = 2
DEFAULT_MIN_ENUM_OBSERVATIONS = 3
DEFAULT_MIN_ENUM_TRANSITIONS = 2
DEFAULT_MAX_ENUM_STATES = 64
MAX_TOP = 100
HEX_DID_RE = re.compile(r"^0x([0-9A-F]{4})$")
HEX_REQUEST_RE = re.compile(r"^22([0-9A-F]{4})$")
HEX_BYTES_RE = re.compile(r"^(?:[0-9A-F]{2})+$")
NUMERIC_RENDER_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s+(.+?))?\s*$"
)


class InferenceError(RuntimeError):
    """A malformed, contradictory, unsupported, or unbounded input report."""


@dataclass(frozen=True)
class Limits:
    max_report_bytes: int = DEFAULT_MAX_REPORT_BYTES
    max_segments: int = DEFAULT_MAX_SEGMENTS
    max_samples_per_segment: int = DEFAULT_MAX_SAMPLES_PER_SEGMENT
    max_hypotheses: int = DEFAULT_MAX_HYPOTHESES


@dataclass(frozen=True)
class Settings:
    min_samples: int = DEFAULT_MIN_SAMPLES
    min_distinct: int = DEFAULT_MIN_DISTINCT
    max_lag: int = DEFAULT_MAX_LAG
    min_enum_observations: int = DEFAULT_MIN_ENUM_OBSERVATIONS
    min_enum_transitions: int = DEFAULT_MIN_ENUM_TRANSITIONS
    top: int = 5


@dataclass(frozen=True)
class SegmentInput:
    sequence: int
    label: str
    did: str
    request: str
    raw_hex: tuple[str, ...]
    rendered: tuple[str, ...]
    count_match: bool
    boundary: bool
    debug_exact: bool


@dataclass(frozen=True)
class NumericSeries:
    values: tuple[float, ...]
    unit: str
    quantum: float


def _require_dict(value: object, context: str) -> dict:
    if not isinstance(value, dict):
        raise InferenceError(f"{context} must be an object")
    return value


def _require_list(value: object, context: str) -> list:
    if not isinstance(value, list):
        raise InferenceError(f"{context} must be an array")
    return value


def _require_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InferenceError(f"{context} must be an integer")
    return value


def _distribution(values: Iterable[str]) -> list[dict[str, object]]:
    counts = Counter(values)
    return [
        {"value": value, "count": counts[value]}
        for value in sorted(counts)
    ]


def _exact_string_list(
    value: object,
    context: str,
    *,
    maximum: int,
) -> list[str]:
    rows = _require_list(value, context)
    if not rows:
        raise InferenceError(f"{context} must not be empty")
    if len(rows) > maximum:
        raise InferenceError(
            f"{context} exceeds sample cap: {len(rows)} > {maximum}"
        )
    if any(not isinstance(row, str) or not row for row in rows):
        raise InferenceError(f"{context} must contain non-empty strings")
    return rows


def _check_distribution(
    declared: object,
    values: Sequence[str],
    context: str,
) -> None:
    if declared != _distribution(values):
        raise InferenceError(f"{context} does not match its ordered values")


def _validate_settings(settings: Settings, limits: Limits) -> None:
    for name, value in (
        ("min_samples", settings.min_samples),
        ("min_distinct", settings.min_distinct),
        ("max_lag", settings.max_lag),
        ("min_enum_observations", settings.min_enum_observations),
        ("min_enum_transitions", settings.min_enum_transitions),
        ("top", settings.top),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise InferenceError(f"{name} must be an integer")
    if settings.min_samples < 3:
        raise InferenceError("minimum samples must be at least 3")
    if settings.min_distinct < 3:
        raise InferenceError("minimum distinct values must be at least 3")
    if settings.max_lag < 0 or settings.max_lag > 32:
        raise InferenceError("maximum lag must be between 0 and 32")
    if settings.min_enum_observations < 1:
        raise InferenceError("minimum enum observations must be positive")
    if settings.min_enum_transitions < 1:
        raise InferenceError("minimum enum transitions must be positive")
    if not 1 <= settings.top <= MAX_TOP:
        raise InferenceError(f"top candidate count must be 1..{MAX_TOP}")
    for name, value, hard_maximum in (
        (
            "max_report_bytes",
            limits.max_report_bytes,
            DEFAULT_MAX_REPORT_BYTES,
        ),
        ("max_segments", limits.max_segments, DEFAULT_MAX_SEGMENTS),
        (
            "max_samples_per_segment",
            limits.max_samples_per_segment,
            DEFAULT_MAX_SAMPLES_PER_SEGMENT,
        ),
        (
            "max_hypotheses",
            limits.max_hypotheses,
            DEFAULT_MAX_HYPOTHESES,
        ),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise InferenceError(f"{name} must be an integer")
        if value < 1:
            raise InferenceError(f"{name} must be positive")
        if value > hard_maximum:
            raise InferenceError(
                f"{name} cannot exceed fixed safety cap {hard_maximum}"
            )


def _validate_report(
    report: dict,
    *,
    limits: Limits,
) -> tuple[list[SegmentInput], list[str], dict[str, list[int]]]:
    if report.get("schema_version") != 2:
        raise InferenceError("only singleton join schema_version 2 is supported")
    if report.get("classification") != "candidate_only":
        raise InferenceError("singleton join classification must be candidate_only")

    buffered = _require_dict(
        report.get("buffered_artifact_mode"), "buffered_artifact_mode"
    )
    if buffered.get("enabled") is not True:
        raise InferenceError("buffered artifact mode must be enabled")
    if buffered.get("info_wire_sample_count_equality_required") is not False:
        raise InferenceError(
            "unexpected singleton join sample-count policy"
        )

    campaign = _require_dict(report.get("campaign"), "campaign")
    if (
        not isinstance(campaign.get("campaign_id"), str)
        or not campaign["campaign_id"]
    ):
        raise InferenceError("campaign.campaign_id must be a non-empty string")
    if campaign.get("module_key") != "cluster":
        raise InferenceError(
            "schema-2 v1 is restricted to the current cluster module"
        )
    schedule = _require_list(campaign.get("schedule"), "campaign.schedule")
    if (
        not schedule
        or len(schedule) > limits.max_segments
        or any(not isinstance(label, str) or not label for label in schedule)
    ):
        raise InferenceError("campaign.schedule is empty, malformed, or over cap")
    repeat_anchors = _require_list(
        campaign.get("repeat_anchors"), "campaign.repeat_anchors"
    )
    if any(
        not isinstance(label, str) or label not in schedule
        for label in repeat_anchors
    ):
        raise InferenceError("campaign.repeat_anchors is malformed")
    if len(set(repeat_anchors)) != len(repeat_anchors):
        raise InferenceError("campaign.repeat_anchors contains duplicates")
    duplicate_labels = {
        label for label, count in Counter(schedule).items() if count > 1
    }
    if duplicate_labels != set(repeat_anchors):
        raise InferenceError(
            "every repeated schedule label must be declared as one repeat anchor"
        )

    addressing = _require_dict(
        report.get("module_addressing"), "module_addressing"
    )
    expected_addressing = {
        "addressing_mode": "normal_29bits",
        "request_can_id": "0x18DA60F1",
        "response_can_id": "0x18DAF160",
        "bus": "c-can",
        "bitrate": 500000,
    }
    for field, expected in expected_addressing.items():
        if addressing.get(field) != expected:
            raise InferenceError(
                f"module_addressing.{field} is not the current cluster value"
            )

    debug = _require_dict(report.get("debug_artifact"), "debug_artifact")
    corroboration = _require_dict(
        debug.get("corroboration"), "debug_artifact.corroboration"
    )
    if (
        corroboration.get("matched") is not True
        or corroboration.get("status")
        != "whole_campaign_independent_streams_corroborated"
        or corroboration.get("planned_did_run_order_exact") is not True
        or corroboration.get("sample_pairing")
        != "not_attempted_buffered_artifacts"
    ):
        raise InferenceError("Debug transport corroboration is incomplete")
    debug_runs = _require_list(
        corroboration.get("runs"), "debug_artifact.corroboration.runs"
    )
    if len(debug_runs) != len(schedule):
        raise InferenceError("Debug run count does not match schedule")

    segments = _require_list(report.get("segments"), "segments")
    if len(segments) != len(schedule):
        raise InferenceError("segment count does not match schedule")

    normalized: list[SegmentInput] = []
    for sequence, expected_label in enumerate(schedule):
        context = f"segments[{sequence}]"
        segment = _require_dict(segments[sequence], context)
        if _require_int(segment.get("sequence"), f"{context}.sequence") != sequence:
            raise InferenceError(f"{context}.sequence is not contiguous")
        if segment.get("label") != expected_label:
            raise InferenceError(f"{context}.label does not match schedule")
        info = _require_dict(segment.get("info"), f"{context}.info")
        wire = _require_dict(segment.get("wire"), f"{context}.wire")
        candidate = _require_dict(
            segment.get("candidate"), f"{context}.candidate"
        )
        if info.get("alignment") != "contiguous_exact_label_run_by_schedule":
            raise InferenceError(f"{context}.info alignment is unsupported")
        if (
            _require_int(info.get("run_ordinal"), f"{context}.info.run_ordinal")
            != sequence
        ):
            raise InferenceError(f"{context}.info run ordinal is inconsistent")
        if (
            wire.get("matched") is not True
            or wire.get("status") != "authoritative_host_timed_pairs"
            or candidate.get("classification") != "candidate"
            or candidate.get("sample_pairing")
            != "not_attempted_buffered_artifacts"
        ):
            raise InferenceError(f"{context} lacks authoritative candidate evidence")
        if candidate.get("label") != expected_label:
            raise InferenceError(f"{context}.candidate label is inconsistent")

        did_match = HEX_DID_RE.fullmatch(str(candidate.get("did")))
        request_match = HEX_REQUEST_RE.fullmatch(str(candidate.get("request")))
        if did_match is None or request_match is None:
            raise InferenceError(f"{context} has malformed DID/request")
        did = did_match.group(1)
        request = request_match.group(0)
        if (
            request_match.group(1) != did
            or wire.get("did") != f"0x{did}"
            or wire.get("request") != request
        ):
            raise InferenceError(f"{context} DID/request fields disagree")

        rendered = _exact_string_list(
            candidate.get("rendered_values"),
            f"{context}.candidate.rendered_values",
            maximum=limits.max_samples_per_segment,
        )
        responses = _exact_string_list(
            candidate.get("wire_responses"),
            f"{context}.candidate.wire_responses",
            maximum=limits.max_samples_per_segment,
        )
        wire_responses = _exact_string_list(
            wire.get("responses"),
            f"{context}.wire.responses",
            maximum=limits.max_samples_per_segment,
        )
        if responses != wire_responses:
            raise InferenceError(f"{context} candidate/wire response order differs")
        raw_hex: list[str] = []
        prefix = f"62{did}"
        for response in responses:
            if not HEX_BYTES_RE.fullmatch(response) or not response.startswith(prefix):
                raise InferenceError(
                    f"{context} contains a non-exact positive response"
                )
            raw = response[len(prefix) :]
            if len(raw) not in (2, 4) or not HEX_BYTES_RE.fullmatch(raw):
                raise InferenceError(
                    f"{context} payload is outside the one/two-byte v1 scope"
                )
            raw_hex.append(raw)
        if len({len(value) for value in raw_hex}) != 1:
            raise InferenceError(f"{context} payload length varies")

        if (
            _require_int(
                candidate.get("info_sample_count"),
                f"{context}.candidate.info_sample_count",
            )
            != len(rendered)
        ):
            raise InferenceError(f"{context} candidate Info count is inconsistent")
        if (
            _require_int(
                candidate.get("wire_pair_count"),
                f"{context}.candidate.wire_pair_count",
            )
            != len(responses)
        ):
            raise InferenceError(f"{context} candidate wire count is inconsistent")
        if (
            _require_int(wire.get("pair_count"), f"{context}.wire.pair_count")
            != len(responses)
        ):
            raise InferenceError(f"{context} wire pair count is inconsistent")
        count_match = len(rendered) == len(responses)
        if (
            candidate.get("count_match") is not count_match
            or segment.get("info_wire_count_match") is not count_match
        ):
            raise InferenceError(f"{context} count-match flags are inconsistent")
        if (
            _require_int(info.get("sample_count"), f"{context}.info.sample_count")
            != len(rendered)
        ):
            raise InferenceError(f"{context} Info sample count is inconsistent")
        if info.get("rendered_values") != rendered:
            raise InferenceError(f"{context} Info/candidate value order differs")

        _check_distribution(
            info.get("rendered_distribution"),
            rendered,
            f"{context}.info.rendered_distribution",
        )
        _check_distribution(
            candidate.get("rendered_distribution"),
            rendered,
            f"{context}.candidate.rendered_distribution",
        )
        _check_distribution(
            candidate.get("wire_response_distribution"),
            responses,
            f"{context}.candidate.wire_response_distribution",
        )
        _check_distribution(
            wire.get("response_distribution"),
            responses,
            f"{context}.wire.response_distribution",
        )
        _check_distribution(
            candidate.get("raw_data_distribution"),
            raw_hex,
            f"{context}.candidate.raw_data_distribution",
        )
        _check_distribution(
            wire.get("raw_data_distribution"),
            raw_hex,
            f"{context}.wire.raw_data_distribution",
        )

        debug_run = _require_dict(
            debug_runs[sequence],
            f"debug_artifact.corroboration.runs[{sequence}]",
        )
        request_alignment = _require_dict(
            debug_run.get("request_alignment"),
            f"debug_artifact.corroboration.runs[{sequence}].request_alignment",
        )
        response_alignment = _require_dict(
            debug_run.get("response_alignment"),
            f"debug_artifact.corroboration.runs[{sequence}].response_alignment",
        )
        request_count = _require_int(
            debug_run.get("debug_request_count"),
            f"debug_artifact.corroboration.runs[{sequence}].debug_request_count",
        )
        response_count = _require_int(
            debug_run.get("debug_response_count"),
            f"debug_artifact.corroboration.runs[{sequence}].debug_response_count",
        )
        if (
            _require_int(
                debug_run.get("sequence"),
                f"debug_artifact.corroboration.runs[{sequence}].sequence",
            )
            != sequence
            or debug_run.get("request") != request
            or debug_run.get("did") != f"0x{did}"
            or _require_int(
                debug_run.get("wire_pair_count"),
                f"debug_artifact.corroboration.runs[{sequence}].wire_pair_count",
            )
            != len(responses)
            or debug_run.get("retention_counts_compatible") is not True
        ):
            raise InferenceError(f"{context} Debug run provenance disagrees")
        boundary = sequence in (0, len(schedule) - 1)
        if sequence == 0 and sequence == len(schedule) - 1:
            retention_compatible = abs(request_count - response_count) <= 1
        elif sequence == 0:
            retention_compatible = response_count - request_count in (0, 1)
        elif sequence == len(schedule) - 1:
            retention_compatible = request_count - response_count in (0, 1)
        else:
            retention_compatible = request_count == response_count
        if not retention_compatible:
            raise InferenceError(f"{context} Debug retention counts disagree")
        for direction, count, alignment in (
            ("request", request_count, request_alignment),
            ("response", response_count, response_alignment),
        ):
            status = alignment.get("status")
            if status not in {"exact_full_run", "clipped_contiguous_subset"}:
                raise InferenceError(
                    f"{context} Debug {direction} alignment status is unsupported"
                )
            offsets = _require_list(
                alignment.get("wire_match_offsets"),
                f"{context} Debug {direction} wire_match_offsets",
            )
            if (
                not offsets
                or any(
                    not isinstance(offset, int)
                    or isinstance(offset, bool)
                    or offset < 0
                    or offset + count > len(responses)
                    for offset in offsets
                )
            ):
                raise InferenceError(
                    f"{context} Debug {direction} match offsets disagree"
                )
            minimum_declared = _require_int(
                alignment.get("minimum_required_overlap"),
                f"{context} Debug {direction} minimum_required_overlap",
            )
            minimum_expected = (
                min(3, len(responses)) if boundary else len(responses)
            )
            if minimum_declared != minimum_expected:
                raise InferenceError(
                    f"{context} Debug {direction} minimum overlap disagrees"
                )
            if not boundary and status != "exact_full_run":
                raise InferenceError(
                    f"{context} interior Debug {direction} run is not exact"
                )
            if status == "exact_full_run" and count != len(responses):
                raise InferenceError(
                    f"{context} exact Debug {direction} count disagrees"
                )
            if status == "exact_full_run" and offsets != [0]:
                raise InferenceError(
                    f"{context} exact Debug {direction} offsets disagree"
                )
            minimum_overlap = min(3, len(responses))
            if status == "clipped_contiguous_subset" and not (
                boundary and minimum_overlap <= count < len(responses)
            ):
                raise InferenceError(
                    f"{context} clipped Debug {direction} count disagrees"
                )

        normalized.append(
            SegmentInput(
                sequence=sequence,
                label=expected_label,
                did=did,
                request=request,
                raw_hex=tuple(raw_hex),
                rendered=tuple(rendered),
                count_match=count_match,
                boundary=boundary,
                debug_exact=(
                    request_alignment.get("status") == "exact_full_run"
                    and response_alignment.get("status") == "exact_full_run"
                ),
            )
        )

    summary = _require_dict(report.get("summary"), "summary")
    if (
        _require_int(summary.get("segments"), "summary.segments")
        != len(segments)
        or _require_int(
            summary.get("candidate_segments"), "summary.candidate_segments"
        )
        != len(segments)
        or _require_int(
            summary.get("unresolved_segments"), "summary.unresolved_segments"
        )
        != 0
        or summary.get("anchors_consistent") is not True
        or _require_int(
            summary.get("wire_sequences_corroborated"),
            "summary.wire_sequences_corroborated",
        )
        != len(segments)
    ):
        raise InferenceError("summary does not describe the validated segments")

    anchor_rows = _require_list(report.get("anchor_checks"), "anchor_checks")
    if len(anchor_rows) != len(repeat_anchors):
        raise InferenceError("anchor-check count does not match campaign plan")
    anchors: dict[str, list[int]] = {}
    seen_anchor_labels: set[str] = set()
    for index, expected_label in enumerate(repeat_anchors):
        anchor = _require_dict(anchor_rows[index], f"anchor_checks[{index}]")
        label = anchor.get("label")
        if label != expected_label or label in seen_anchor_labels:
            raise InferenceError("anchor checks are out of order or duplicated")
        seen_anchor_labels.add(label)
        occurrences = [
            position for position, value in enumerate(schedule) if value == label
        ]
        first = normalized[occurrences[0]]
        declared_occurrences = _require_list(
            anchor.get("occurrences"),
            f"anchor_checks[{index}].occurrences",
        )
        if any(
            not isinstance(position, int) or isinstance(position, bool)
            for position in declared_occurrences
        ):
            raise InferenceError(
                f"anchor check for {label!r} has malformed occurrences"
            )
        if (
            len(occurrences) < 2
            or declared_occurrences != occurrences
            or anchor.get("request") != first.request
            or anchor.get("did") != f"0x{first.did}"
            or anchor.get("consistent") is not True
            or any(
                normalized[position].request != first.request
                for position in occurrences
            )
        ):
            raise InferenceError(f"anchor check for {label!r} is inconsistent")
        anchors[label] = occurrences

    return normalized, list(schedule), anchors


def load_report(path: Path, *, limits: Limits) -> tuple[dict, str, int]:
    try:
        with path.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise InferenceError("input report must be a regular file")
            raw = handle.read(limits.max_report_bytes + 1)
    except OSError as exc:
        raise InferenceError(f"cannot read input report: {path}") from exc
    size = len(raw)
    if size <= 0 or size > limits.max_report_bytes:
        raise InferenceError(
            "input report size is empty or exceeds the fixed read bound: "
            f"{size} > {limits.max_report_bytes}"
        )
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InferenceError(f"cannot read input report: {exc}") from exc
    if not isinstance(report, dict):
        raise InferenceError("input report root must be an object")
    return report, hashlib.sha256(raw).hexdigest(), size


def _parse_numeric(values: Sequence[str]) -> tuple[NumericSeries | None, str | None]:
    parsed: list[float] = []
    units: list[str] = []
    exponents: list[int] = []
    matched = 0
    for value in values:
        match = NUMERIC_RENDER_RE.fullmatch(value)
        if match is None:
            continue
        matched += 1
        token = match.group(1)
        unit = " ".join((match.group(2) or "").split())
        try:
            number = Decimal(token)
        except InvalidOperation:
            return None, "invalid_numeric_rendering"
        if not number.is_finite():
            return None, "nonfinite_numeric_rendering"
        try:
            rendered_number = float(number)
        except (OverflowError, ValueError):
            return None, "numeric_rendering_out_of_float_range"
        if not math.isfinite(rendered_number):
            return None, "numeric_rendering_out_of_float_range"
        parsed.append(rendered_number)
        units.append(unit)
        exponents.append(number.as_tuple().exponent)
    if matched == 0:
        return None, "not_numeric"
    if matched != len(values):
        return None, "mixed_numeric_and_enum_rendering"
    if len(set(units)) != 1 or not units[0]:
        return None, "mixed_or_missing_units"
    if len(set(exponents)) != 1:
        return None, "inconsistent_display_precision"
    quantum = float(Decimal(1).scaleb(exponents[0]))
    if not math.isfinite(quantum) or quantum <= 0:
        return None, "invalid_display_quantum"
    return NumericSeries(tuple(parsed), units[0], quantum), None


def _raw_interpretations(
    raw_hex: Sequence[str],
) -> list[dict[str, object]]:
    payloads = [bytes.fromhex(value) for value in raw_hex]
    width = len(payloads[0])
    descriptors: list[tuple[str, bool]]
    if width == 1:
        descriptors = [("big", False), ("big", True)]
    else:
        descriptors = [
            ("big", False),
            ("big", True),
            ("little", False),
            ("little", True),
        ]
    grouped: dict[tuple[int, ...], list[dict[str, object]]] = {}
    for byte_order, signed in descriptors:
        vector = tuple(
            int.from_bytes(payload, byteorder=byte_order, signed=signed)
            for payload in payloads
        )
        grouped.setdefault(vector, []).append(
            {
                "slice_start": 0,
                "slice_length": width,
                "byte_order": byte_order,
                "signed": signed,
            }
        )
    return [
        {"raw_values": vector, "interpretations": interpretations}
        for vector, interpretations in grouped.items()
    ]


def _clean_float(value: float) -> float:
    return float(f"{value:.12g}")


def _affine_fit(
    raw: Sequence[int],
    displayed: Sequence[float],
    *,
    quantum: float,
    min_samples: int,
    min_distinct: int,
) -> dict[str, object] | None:
    if len(raw) != len(displayed) or len(raw) < min_samples:
        return None
    if len(set(raw)) < min_distinct or len(set(displayed)) < min_distinct:
        return None
    x_mean = sum(raw) / len(raw)
    y_mean = sum(displayed) / len(displayed)
    denominator = sum((value - x_mean) ** 2 for value in raw)
    if denominator == 0:
        return None
    slope = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(raw, displayed)
    ) / denominator
    if not math.isfinite(slope) or slope == 0:
        return None
    intercept = y_mean - slope * x_mean
    errors = [
        y - (slope * x + intercept)
        for x, y in zip(raw, displayed)
    ]
    tolerance = quantum * 0.500001 + 1e-12
    max_error = max(abs(error) for error in errors)
    if max_error > tolerance:
        return None
    rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    y_range = max(displayed) - min(displayed)
    total = sum((value - y_mean) ** 2 for value in displayed)
    residual = sum(error * error for error in errors)
    return {
        "slope": _clean_float(slope),
        "intercept": _clean_float(intercept),
        "samples": len(raw),
        "distinct_raw_values": len(set(raw)),
        "distinct_rendered_values": len(set(displayed)),
        "raw_min": min(raw),
        "raw_max": max(raw),
        "rendered_min": _clean_float(min(displayed)),
        "rendered_max": _clean_float(max(displayed)),
        "rmse": _clean_float(rmse),
        "normalized_rmse": _clean_float(rmse / y_range),
        "max_abs_error": _clean_float(max_error),
        "display_half_quantum_tolerance": _clean_float(tolerance),
        "r_squared": _clean_float(
            1.0 - residual / total if total else 0.0
        ),
    }


def _same_formula(
    first: dict[str, object],
    second: dict[str, object],
    *,
    quantum: float,
) -> bool:
    slope_a = float(first["slope"])
    slope_b = float(second["slope"])
    intercept_a = float(first["intercept"])
    intercept_b = float(second["intercept"])
    raw_span = max(
        1.0,
        float(first["raw_max"]) - float(first["raw_min"]),
        float(second["raw_max"]) - float(second["raw_min"]),
    )
    return (
        abs(slope_a - slope_b) <= quantum / raw_span + 1e-10
        and abs(intercept_a - intercept_b) <= quantum + 1e-10
    )


def _descriptor_set(candidate: dict[str, object]) -> set[str]:
    return {
        json.dumps(item, sort_keys=True, separators=(",", ":"))
        for item in candidate["interpretations"]
    }


def _lag_pairs(
    raw: Sequence[int],
    rendered: Sequence[float] | Sequence[str],
    lag: int,
) -> tuple[list[int], list]:
    if abs(lag) >= len(raw):
        return [], []
    if lag >= 0:
        return list(raw[: len(raw) - lag]), list(rendered[lag:])
    shift = -lag
    return list(raw[shift:]), list(rendered[: len(rendered) - shift])


def _numeric_segment(
    segment: SegmentInput,
    series: NumericSeries,
    *,
    settings: Settings,
    limits: Limits,
) -> dict[str, object]:
    reasons: list[str] = []
    result: dict[str, object] = {
        "kind": "numeric",
        "unit": series.unit,
        "display_quantum": _clean_float(series.quantum),
        "physical_scale_verified": False,
        "promotion_allowed": False,
        "histogram": {"eligible": False, "candidates": []},
        "sequence": {"eligible": False, "candidates": []},
        "selected": None,
        "failure_reasons": reasons,
    }
    if not segment.count_match:
        reasons.append("count_mismatch")
        return result
    if len(segment.raw_hex) < settings.min_samples:
        reasons.append("too_few_samples")
        return result
    if len(set(series.values)) < settings.min_distinct:
        reasons.append("too_few_distinct_rendered")
        return result

    interpretations = _raw_interpretations(segment.raw_hex)
    hypotheses = 0
    histogram_candidates: list[dict[str, object]] = []
    for interpretation in interpretations:
        raw = interpretation["raw_values"]
        if len(set(raw)) < settings.min_distinct:
            continue
        for direction in ("increasing", "decreasing"):
            hypotheses += 1
            if hypotheses > limits.max_hypotheses:
                raise InferenceError("numeric hypotheses exceed configured cap")
            ordered_raw = sorted(raw)
            ordered_display = sorted(
                series.values, reverse=direction == "decreasing"
            )
            fit = _affine_fit(
                ordered_raw,
                ordered_display,
                quantum=series.quantum,
                min_samples=settings.min_samples,
                min_distinct=settings.min_distinct,
            )
            if fit is None:
                continue
            histogram_candidates.append(
                {
                    "method": "equal_count_monotone_histogram",
                    "direction": direction,
                    "interpretations": interpretation["interpretations"],
                    **fit,
                }
            )
    histogram_candidates.sort(
        key=lambda item: (
            item["max_abs_error"],
            item["normalized_rmse"],
            len(item["interpretations"]),
            json.dumps(item["interpretations"], sort_keys=True),
        )
    )
    result["histogram"] = {
        "eligible": True,
        "method": (
            "equal-count sorted multiset fit; no sample timestamps or "
            "row pairing asserted"
        ),
        "candidates": histogram_candidates[: settings.top],
    }
    if not histogram_candidates:
        reasons.append("no_affine_fit_within_display_quantum")
        return result

    best = histogram_candidates[0]
    alternatives = [
        candidate
        for candidate in histogram_candidates[1:]
        if not (
            candidate["direction"] == best["direction"]
            and _descriptor_set(candidate) == _descriptor_set(best)
        )
    ]
    if alternatives:
        reasons.append("interpretation_ambiguous")
        return result

    selected = dict(best)
    selected["evidence_grade"] = "single_segment_histogram_candidate"
    selected["observed_scope"] = "AlfaOBD_rendering_candidate_only"
    result["selected"] = selected

    if segment.boundary:
        result["sequence"] = {
            "eligible": False,
            "reason": "outer_boundary_sequence_disallowed",
            "candidates": [],
        }
        return result
    if not segment.debug_exact:
        result["sequence"] = {
            "eligible": False,
            "reason": "debug_not_exact_full_run",
            "candidates": [],
        }
        return result

    selected_descriptors = _descriptor_set(best)
    matching_interpretation = next(
        item
        for item in interpretations
        if {
            json.dumps(value, sort_keys=True, separators=(",", ":"))
            for value in item["interpretations"]
        }
        == selected_descriptors
    )
    raw = matching_interpretation["raw_values"]
    sequence_candidates: list[dict[str, object]] = []
    for lag in range(-settings.max_lag, settings.max_lag + 1):
        paired_raw, paired_display = _lag_pairs(raw, series.values, lag)
        fit = _affine_fit(
            paired_raw,
            paired_display,
            quantum=series.quantum,
            min_samples=settings.min_samples,
            min_distinct=settings.min_distinct,
        )
        if fit is None or not _same_formula(
            best, fit, quantum=series.quantum
        ):
            continue
        sequence_candidates.append(
            {
                "method": "ordinal_lag_hypothesis",
                "lag": lag,
                "coverage": _clean_float(len(paired_raw) / len(raw)),
                **fit,
            }
        )
    result["sequence"] = {
        "eligible": True,
        "lag_semantics": "rendered_index = raw_index + lag",
        "candidates": sequence_candidates,
    }
    if len(sequence_candidates) == 1:
        selected["evidence_grade"] = "interior_ordinal_corroborated_candidate"
        selected["ordinal_lag"] = sequence_candidates[0]["lag"]
    elif len(sequence_candidates) > 1:
        result["sequence"]["reason"] = "lag_ambiguous"
    else:
        result["sequence"]["reason"] = "no_ordinal_lag_corroboration"
    return result


def _enum_segment(
    segment: SegmentInput,
    *,
    settings: Settings,
    limits: Limits,
) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": "enum",
        "physical_scale_verified": False,
        "promotion_allowed": False,
        "complete_enum": False,
        "unobserved_raw_values": "unknown",
        "selected": None,
        "lag_candidates": [],
        "failure_reasons": [],
    }
    reasons = result["failure_reasons"]
    assert isinstance(reasons, list)
    if not segment.count_match:
        reasons.append("count_mismatch")
        return result
    if segment.boundary:
        reasons.append("outer_boundary_sequence_disallowed")
        return result
    if not segment.debug_exact:
        reasons.append("debug_not_exact_full_run")
        return result
    if len(segment.raw_hex) < settings.min_samples:
        reasons.append("too_few_samples")
        return result

    distinct_raw = sorted(set(segment.raw_hex))
    distinct_labels = sorted(set(segment.rendered))
    if (
        len(distinct_raw) > DEFAULT_MAX_ENUM_STATES
        or len(distinct_labels) > DEFAULT_MAX_ENUM_STATES
    ):
        reasons.append("enum_state_cap_exceeded")
        return result
    lag_state_hypotheses = (
        2 * settings.max_lag + 1
    ) * max(len(distinct_raw), 1)
    if lag_state_hypotheses > limits.max_hypotheses:
        raise InferenceError("enum hypotheses exceed configured cap")
    if len(distinct_raw) == 1 and len(distinct_labels) == 1:
        if len(segment.raw_hex) < settings.min_enum_observations:
            reasons.append("single_enum_state_under_sampled")
            return result
        result["selected"] = {
            "evidence_grade": "single_state_partial_observation",
            "observed_mapping": [
                {"raw_hex": distinct_raw[0], "rendered": distinct_labels[0]}
            ],
            "reverse_mapping_unique": True,
            "complete_enum": False,
            "observed_scope": "AlfaOBD_rendering_candidate_only",
        }
        reasons.append("single_enum_state")
        return result
    if len(distinct_raw) < 2 or len(distinct_labels) < 2:
        reasons.append("enum_state_cardinality_mismatch")
        return result

    valid: list[dict[str, object]] = []
    for lag in range(-settings.max_lag, settings.max_lag + 1):
        raw, labels = _lag_pairs(segment.raw_hex, segment.rendered, lag)
        if len(raw) < settings.min_samples:
            continue
        mappings: dict[str, set[str]] = {}
        counts: Counter[tuple[str, str]] = Counter()
        for raw_value, label in zip(raw, labels):
            mappings.setdefault(raw_value, set()).add(label)
            counts[(raw_value, label)] += 1
        if any(len(labels_for_raw) != 1 for labels_for_raw in mappings.values()):
            continue
        mapping = {
            raw_value: next(iter(labels_for_raw))
            for raw_value, labels_for_raw in mappings.items()
        }
        if any(
            counts[(raw_value, label)] < settings.min_enum_observations
            for raw_value, label in mapping.items()
        ):
            continue
        transitions = sum(
            raw[index] != raw[index - 1]
            for index in range(1, len(raw))
        )
        if transitions < settings.min_enum_transitions:
            continue
        valid.append(
            {
                "lag": lag,
                "coverage": _clean_float(len(raw) / len(segment.raw_hex)),
                "transitions": transitions,
                "mapping": [
                    {
                        "raw_hex": raw_value,
                        "rendered": mapping[raw_value],
                        "observations": counts[
                            (raw_value, mapping[raw_value])
                        ],
                    }
                    for raw_value in sorted(mapping)
                ],
            }
        )
    result["lag_candidates"] = valid
    if not valid:
        reasons.extend(
            [
                "enum_raw_maps_to_multiple_labels_or_is_under_sampled",
                "insufficient_transitions",
            ]
        )
        return result

    canonical = {
        tuple(
            (row["raw_hex"], row["rendered"])
            for row in candidate["mapping"]
        )
        for candidate in valid
    }
    if len(canonical) != 1:
        reasons.append("enum_alignment_ambiguous")
        return result
    mapping_rows = valid[0]["mapping"]
    rendered_counts = Counter(row["rendered"] for row in mapping_rows)
    selected = {
        "evidence_grade": "enum_partial_ordinal_candidate",
        "observed_mapping": mapping_rows,
        "reverse_mapping_unique": all(
            count == 1 for count in rendered_counts.values()
        ),
        "complete_enum": False,
        "valid_lags": [candidate["lag"] for candidate in valid],
        "lag_ambiguous": len(valid) > 1,
        "observed_scope": "AlfaOBD_rendering_candidate_only",
    }
    result["selected"] = selected
    if len(valid) > 1:
        reasons.append("lag_ambiguous_but_mapping_consistent")
    return result


def _compatible_numeric_anchor(
    segment: SegmentInput,
    series: NumericSeries,
    candidate: dict[str, object],
    descriptors: set[str],
) -> bool:
    if not segment.count_match:
        return False
    interpretation = next(
        (
            item
            for item in _raw_interpretations(segment.raw_hex)
            if {
                json.dumps(value, sort_keys=True, separators=(",", ":"))
                for value in item["interpretations"]
            }
            >= descriptors
        ),
        None,
    )
    if interpretation is None:
        return False
    raw = sorted(interpretation["raw_values"])
    displayed = sorted(
        series.values,
        reverse=float(candidate["slope"]) < 0,
    )
    predicted = [
        float(candidate["slope"]) * value + float(candidate["intercept"])
        for value in raw
    ]
    tolerance = series.quantum * 0.500001 + 1e-12
    return len(predicted) == len(displayed) and all(
        abs(actual - expected) <= tolerance
        for actual, expected in zip(displayed, predicted)
    )


def infer_report(
    report: dict,
    *,
    settings: Settings = Settings(),
    limits: Limits = Limits(),
    selected_labels: set[str] | None = None,
    kind_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    _validate_settings(settings, limits)
    segments, schedule, anchors = _validate_report(report, limits=limits)
    kind_overrides = kind_overrides or {}
    unknown_overrides = set(kind_overrides) - set(schedule)
    if unknown_overrides:
        raise InferenceError(
            f"kind override labels are absent: {sorted(unknown_overrides)!r}"
        )
    if selected_labels is not None:
        missing = selected_labels - set(schedule)
        if missing:
            raise InferenceError(
                f"selected labels are absent: {sorted(missing)!r}"
            )

    segment_results: list[dict[str, object]] = []
    numeric_series: dict[int, NumericSeries] = {}
    for segment in segments:
        if selected_labels is not None and segment.label not in selected_labels:
            continue
        override = kind_overrides.get(segment.label)
        if override == "enum":
            # An explicit enum override is the operator's disambiguation that
            # rendered strings are categorical labels. This intentionally
            # includes numeric-looking labels ("1", "2") and mixed labels
            # ("P", "1", "D"); neither is a physical numeric scale.
            parsed, parse_reason = None, None
        else:
            parsed, parse_reason = _parse_numeric(segment.rendered)
        kind = override or ("numeric" if parsed is not None else "enum")
        if kind == "numeric":
            if parsed is None:
                analysis: dict[str, object] = {
                    "kind": "numeric",
                    "selected": None,
                    "physical_scale_verified": False,
                    "promotion_allowed": False,
                    "failure_reasons": [parse_reason or "nonnumeric_rendering"],
                }
            else:
                numeric_series[segment.sequence] = parsed
                analysis = _numeric_segment(
                    segment,
                    parsed,
                    settings=settings,
                    limits=limits,
                )
        elif kind == "enum":
            if parse_reason not in ("not_numeric", None):
                analysis = {
                    "kind": "enum",
                    "selected": None,
                    "physical_scale_verified": False,
                    "promotion_allowed": False,
                    "failure_reasons": [parse_reason],
                }
            else:
                analysis = _enum_segment(
                    segment,
                    settings=settings,
                    limits=limits,
                )
        else:
            raise InferenceError(
                f"unsupported kind override for {segment.label!r}: {kind!r}"
            )
        segment_results.append(
            {
                "sequence": segment.sequence,
                "label": segment.label,
                "did": f"0x{segment.did}",
                "boundary_segment": segment.boundary,
                "info_wire_count_match": segment.count_match,
                "debug_transport_exact_full_run": segment.debug_exact,
                "analysis": analysis,
            }
        )

    result_by_sequence = {
        row["sequence"]: row for row in segment_results
    }
    signals: list[dict[str, object]] = []
    seen_labels: set[str] = set()
    for label in schedule:
        if label in seen_labels:
            continue
        seen_labels.add(label)
        if selected_labels is not None and label not in selected_labels:
            continue
        occurrences = [
            segment.sequence for segment in segments if segment.label == label
        ]
        rows = [result_by_sequence[sequence] for sequence in occurrences]
        analyses = [row["analysis"] for row in rows]
        selected = [
            analysis.get("selected")
            for analysis in analyses
            if isinstance(analysis.get("selected"), dict)
        ]
        kinds = {analysis.get("kind") for analysis in analyses}
        signal: dict[str, object] = {
            "module_key": "cluster",
            "label": label,
            "did": rows[0]["did"],
            "occurrences": occurrences,
            "verification_status": "candidate_only",
            "physical_scale_verified": False,
            "promotion_allowed": False,
            "selected": None,
            "evidence_grade": "unidentifiable",
            "failure_reasons": [],
        }
        if len(kinds) != 1:
            signal["failure_reasons"] = ["occurrence_kind_conflict"]
            signals.append(signal)
            continue
        kind = next(iter(kinds))
        signal["kind"] = kind
        if label in anchors and kind == "numeric":
            informative = [
                (sequence, analysis["selected"])
                for sequence, analysis in zip(occurrences, analyses)
                if isinstance(analysis.get("selected"), dict)
            ]
            if informative:
                base_sequence, base = informative[0]
                assert isinstance(base, dict)
                base_series = numeric_series.get(base_sequence)
                common_descriptors = _descriptor_set(base)
                consistent = True
                for _sequence, candidate in informative[1:]:
                    assert isinstance(candidate, dict)
                    series = numeric_series.get(_sequence)
                    if (
                        series is None
                        or base_series is None
                        or series.unit != base_series.unit
                        or series.quantum != base_series.quantum
                        or not _same_formula(
                            base, candidate, quantum=base_series.quantum
                        )
                    ):
                        consistent = False
                        break
                    common_descriptors &= _descriptor_set(candidate)
                if not common_descriptors:
                    consistent = False
                compatible_occurrences: list[int] = []
                unresolved_occurrences: list[int] = []
                conflicting_occurrences: list[int] = []
                if consistent and base_series is not None:
                    informative_sequences = {
                        sequence for sequence, _candidate in informative
                    }
                    for sequence in occurrences:
                        if sequence in informative_sequences:
                            continue
                        series = numeric_series.get(sequence)
                        segment = segments[sequence]
                        if series is None or not segment.count_match:
                            unresolved_occurrences.append(sequence)
                        elif (
                            series.unit != base_series.unit
                            or series.quantum != base_series.quantum
                            or not _compatible_numeric_anchor(
                                segment,
                                series,
                                base,
                                common_descriptors,
                            )
                        ):
                            conflicting_occurrences.append(sequence)
                        else:
                            compatible_occurrences.append(sequence)
                if not consistent or conflicting_occurrences:
                    signal["evidence_grade"] = "conflicting_evidence"
                    signal["failure_reasons"] = ["anchor_formula_conflict"]
                    signal["conflicting_occurrences"] = conflicting_occurrences
                elif unresolved_occurrences:
                    signal["failure_reasons"] = [
                        "anchor_occurrence_not_comparable"
                    ]
                    signal["unresolved_occurrences"] = unresolved_occurrences
                else:
                    consolidated = dict(base)
                    consolidated["interpretations"] = [
                        json.loads(value)
                        for value in sorted(common_descriptors)
                    ]
                    consolidated["supporting_occurrences"] = [
                        sequence for sequence, _candidate in informative
                    ]
                    consolidated["compatible_uninformative_occurrences"] = (
                        compatible_occurrences
                    )
                    if len(informative) >= 2:
                        grade = "repeated_anchor_corroborated_candidate"
                    elif compatible_occurrences:
                        grade = "anchor_compatibility_only"
                    else:
                        grade = base["evidence_grade"]
                    consolidated["evidence_grade"] = grade
                    signal["selected"] = consolidated
                    signal["evidence_grade"] = grade
            else:
                signal["failure_reasons"] = sorted(
                    {
                        reason
                        for analysis in analyses
                        for reason in analysis.get("failure_reasons", [])
                    }
                )
        elif label in anchors:
            signal["failure_reasons"] = [
                "repeated_nonnumeric_anchor_consolidation_not_supported"
            ]
        elif selected:
            candidate = dict(selected[0])
            signal["selected"] = candidate
            signal["evidence_grade"] = candidate["evidence_grade"]
        else:
            signal["failure_reasons"] = sorted(
                {
                    reason
                    for analysis in analyses
                    for reason in analysis.get("failure_reasons", [])
                }
            )
        signals.append(signal)

    return {
        "schema_version": 1,
        "method": "offline_singleton_schema2_rendering_inference",
        "verification_status": "candidate_only",
        "physical_verification": False,
        "promotion_allowed": False,
        "interpretation_warning": (
            "Selected results are candidates for AlfaOBD's observed rendering "
            "only. Android Info rows have no timestamps; no physical scale, "
            "complete enum, or promotion-ready telemetry is established."
        ),
        "input_campaign": {
            "campaign_id": _require_dict(report["campaign"], "campaign").get(
                "campaign_id"
            ),
            "module_key": "cluster",
            "join_schema_version": 2,
        },
        "limits": {
            "max_report_bytes": limits.max_report_bytes,
            "max_segments": limits.max_segments,
            "max_samples_per_segment": limits.max_samples_per_segment,
            "max_hypotheses": limits.max_hypotheses,
            "max_enum_states": DEFAULT_MAX_ENUM_STATES,
        },
        "settings": {
            "min_samples": settings.min_samples,
            "min_distinct": settings.min_distinct,
            "max_lag": settings.max_lag,
            "min_enum_observations": settings.min_enum_observations,
            "min_enum_transitions": settings.min_enum_transitions,
            "top": settings.top,
        },
        "segments": segment_results,
        "signals": signals,
    }


def _parse_kind_overrides(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        label, separator, kind = value.rpartition("=")
        if not separator or not label or kind not in {"numeric", "enum"}:
            raise InferenceError(
                "--kind must be EXACT_LABEL=numeric or EXACT_LABEL=enum"
            )
        if label in result:
            raise InferenceError(f"duplicate --kind override for {label!r}")
        result[label] = kind
    return result


def atomic_write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise InferenceError(
            f"refusing to overwrite existing output: {path}"
        ) from exc
    created = True
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        created = False
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if created:
            try:
                path.unlink()
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument(
        "--kind",
        action="append",
        default=[],
        help="override one exact label as EXACT_LABEL=numeric|enum",
    )
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--min-distinct", type=int, default=DEFAULT_MIN_DISTINCT)
    parser.add_argument("--max-lag", type=int, default=DEFAULT_MAX_LAG)
    parser.add_argument(
        "--min-enum-observations",
        type=int,
        default=DEFAULT_MIN_ENUM_OBSERVATIONS,
    )
    parser.add_argument(
        "--min-enum-transitions",
        type=int,
        default=DEFAULT_MIN_ENUM_TRANSITIONS,
    )
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument(
        "--max-report-bytes",
        type=int,
        default=DEFAULT_MAX_REPORT_BYTES,
        help="lower the fixed input byte cap",
    )
    parser.add_argument(
        "--max-segments",
        type=int,
        default=DEFAULT_MAX_SEGMENTS,
        help="lower the fixed segment cap",
    )
    parser.add_argument(
        "--max-samples-per-segment",
        type=int,
        default=DEFAULT_MAX_SAMPLES_PER_SEGMENT,
        help="lower the fixed per-segment sample cap",
    )
    parser.add_argument(
        "--max-hypotheses",
        type=int,
        default=DEFAULT_MAX_HYPOTHESES,
        help="lower the fixed inference hypothesis cap",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limits = Limits(
        max_report_bytes=args.max_report_bytes,
        max_segments=args.max_segments,
        max_samples_per_segment=args.max_samples_per_segment,
        max_hypotheses=args.max_hypotheses,
    )
    settings = Settings(
        min_samples=args.min_samples,
        min_distinct=args.min_distinct,
        max_lag=args.max_lag,
        min_enum_observations=args.min_enum_observations,
        min_enum_transitions=args.min_enum_transitions,
        top=args.top,
    )
    try:
        _validate_settings(settings, limits)
        kind_overrides = _parse_kind_overrides(args.kind)
        report, digest, input_size = load_report(args.report, limits=limits)
        result = infer_report(
            report,
            settings=settings,
            limits=limits,
            selected_labels=set(args.label) if args.label else None,
            kind_overrides=kind_overrides,
        )
        result["input_report"] = {
            "path": str(args.report),
            "sha256": digest,
            "size_bytes": input_size,
        }
        output = args.output or DEFAULT_OUTPUT_ROOT / (
            f"{args.report.stem}-singleton-inference.json"
        )
        atomic_write_report(output, result)
    except (InferenceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"OFFLINE candidate-only inference: {output}")
    for signal in result["signals"]:
        print(
            f"  {signal['label']} ({signal['did']}): "
            f"{signal['evidence_grade']}"
        )
    print("Physical verification: NO; promotion allowed: NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
