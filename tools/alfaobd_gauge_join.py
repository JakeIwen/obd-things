#!/usr/bin/env python3
"""Join one AlfaOBD Gauges_Data section to its decoded debug polling loop.

This is offline correlation, not a DID oracle.  It preserves the selected gauge
section and every sample row, accepts exactly one decoded debug source per run, and
only treats ``22 XXXX -> 62 XXXX ...`` with an exact DID echo as usable data.  Fits
remain candidates; historical/ambiguous results are never described as verified on
the current vehicle.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
import csv
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Iterable, Iterator

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.ecu_mapping.alfalog import iter_exchanges_detailed


DEFAULT_OUTPUT_ROOT = REPO / "tmp" / "inventories" / "alfaobd_gauge_join"
DEFAULT_MAX_EXCHANGES = 100_000
DEFAULT_MAX_HYPOTHESES_PER_METRIC = 20_000
PROFILE_PREFIX = "Recording data for"
DATE_PREFIX = "Date (YY/MM/DD):"
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}(?:\.\d+)?$")
REQUEST_RE = re.compile(r"^22([0-9A-F]{4})$")


def clock_ms(value: str) -> int:
    hours, minutes, tail = value.split(":")
    seconds_text, dot, fraction = tail.partition(".")
    milliseconds = int((fraction + "000")[:3]) if dot else 0
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds_text)) * 1000 + milliseconds


def normalize_profile(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def normalize_date(value: str) -> str:
    for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            pass
    return value.strip()


def normalize_address(value: str | None) -> str:
    cleaned = re.sub(r"\s+", "", value or "").upper()
    if cleaned.startswith("ATSH"):
        cleaned = cleaned[4:]
    if cleaned.startswith("0X"):
        cleaned = cleaned[2:]
    return cleaned


def csv_fields(line: str) -> list[str]:
    fields = next(csv.reader([line]))
    if line.rstrip("\r\n").endswith(",") and fields and fields[-1] == "":
        fields.pop()
    return [field.strip() for field in fields]


@dataclass
class GaugeRow:
    line_number: int
    timestamp: str
    fields: list[str]

    @property
    def time_ms(self) -> int:
        return clock_ms(self.timestamp)


@dataclass
class GaugeSection:
    index: int
    profile: str
    profile_source: str
    date_raw: str
    date: str
    start_line: int
    header_line: int | None = None
    end_line: int | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[GaugeRow] = field(default_factory=list)
    unparsed_lines: int = 0


def iter_gauge_sections(path: Path) -> Iterator[GaugeSection]:
    """Yield sections one at a time; only the current section's rows are buffered."""
    current: GaugeSection | None = None
    current_profile = "<unknown: no preceding profile marker>"
    pending_profile_source = "inherited"
    section_index = 0
    last_line = 0

    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for line_number, raw in enumerate(handle, 1):
            last_line = line_number
            line = raw.strip("\r\n")
            stripped = line.strip()
            if stripped.startswith(PROFILE_PREFIX):
                named = stripped[len(PROFILE_PREFIX):].strip()
                if named:
                    current_profile = named
                    pending_profile_source = "explicit"
                else:
                    pending_profile_source = "inherited"
                continue
            if stripped.startswith(DATE_PREFIX):
                if current is not None:
                    current.end_line = line_number - 1
                    yield current
                section_index += 1
                raw_date = stripped[len(DATE_PREFIX):].strip()
                current = GaugeSection(
                    index=section_index,
                    profile=current_profile,
                    profile_source=pending_profile_source,
                    date_raw=raw_date,
                    date=normalize_date(raw_date),
                    start_line=line_number,
                )
                pending_profile_source = "inherited"
                continue
            if current is None or not stripped or set(stripped) == {"_"}:
                continue
            if current.header_line is None and stripped.startswith("Time,"):
                fields = csv_fields(line)
                if len(fields) >= 2 and fields[0] == "Time":
                    current.header_line = line_number
                    current.columns = fields
                else:
                    current.unparsed_lines += 1
                continue
            if current.header_line is not None:
                fields = csv_fields(line)
                if fields and TIME_RE.fullmatch(fields[0]):
                    current.rows.append(GaugeRow(line_number, fields[0], fields))
                    continue
            current.unparsed_lines += 1

    if current is not None:
        current.end_line = last_line
        yield current


@dataclass
class DidExchange:
    request_ts: str
    response_end_ts: str | None
    request_line: int
    response_end_line: int | None
    completion_reason: str
    prompt_seen: bool
    date: str
    profile: str
    address: str
    request: str
    response: str
    did: str
    payload: bytes | None
    pending_count: int

    @property
    def request_ms(self) -> int:
        return clock_ms(self.request_ts)

    @property
    def response_end_ms(self) -> int:
        return clock_ms(self.response_end_ts or self.request_ts)


def iter_did_exchanges(
    path: Path,
    *,
    date: str,
    profile: str,
    address: str | None = None,
) -> Iterator[DidExchange]:
    expected_date = normalize_date(date)
    expected_profile = normalize_profile(profile)
    expected_address = normalize_address(address)
    for exchange in iter_exchanges_detailed(path):
        match = REQUEST_RE.fullmatch(exchange["req"])
        if not match:
            continue
        if normalize_date(exchange["date"]) != expected_date:
            continue
        if normalize_profile(exchange["module"]) != expected_profile:
            continue
        actual_address = normalize_address(exchange["addr"])
        if expected_address and actual_address != expected_address:
            continue
        did = match.group(1)
        response = exchange["resp"].upper()
        # AlfaOBD can concatenate response-pending frames before the eventual answer.
        # Accept only zero or more exact 7F 22 78 frames followed by one exact echoed
        # positive response; reject every other prefix/trailer concatenation.
        positive = re.fullmatch(rf"(?:7F2278)*62{did}([0-9A-F]+)", response)
        payload_hex = positive.group(1) if positive else ""
        exact_positive = len(payload_hex) >= 2 and len(payload_hex) % 2 == 0
        yield DidExchange(
            request_ts=exchange["request_ts"],
            response_end_ts=exchange["response_end_ts"],
            request_line=exchange["request_line"],
            response_end_line=exchange["response_end_line"],
            completion_reason=exchange["completion_reason"],
            prompt_seen=exchange["prompt_seen"],
            date=expected_date,
            profile=exchange["module"],
            address=actual_address,
            request=exchange["req"],
            response=response,
            did=did,
            payload=bytes.fromhex(payload_hex) if exact_positive else None,
            pending_count=(len(response) - 6 - len(payload_hex)) // 6 if exact_positive else 0,
        )


@dataclass
class PollCycle:
    id: int
    run_id: int
    run_index: int
    exchanges: list[DidExchange]

    @property
    def anchor_ms(self) -> int:
        # Gauge CSV rows are emitted at the prompt completing the repeated first DID.
        return self.exchanges[0].response_end_ms

    @property
    def anchor_ts(self) -> str:
        return self.exchanges[0].response_end_ts or self.exchanges[0].request_ts


def infer_boundary_did(
    exchanges: Iterable[DidExchange],
    rows: list[GaugeRow],
    *,
    tolerance_ms: int = 50,
) -> dict[str, object]:
    """Infer the loop's first DID from response completions nearest Gauge rows."""
    row_times = sorted(row.time_ms for row in rows)
    scores: dict[str, list[int]] = {}
    totals: dict[str, int] = {}
    if row_times:
        for exchange in exchanges:
            totals[exchange.did] = totals.get(exchange.did, 0) + 1
            if not exchange.prompt_seen or exchange.response_end_ts is None:
                continue
            position = bisect_left(row_times, exchange.response_end_ms)
            nearby = [
                row_times[index]
                for index in (position - 1, position)
                if 0 <= index < len(row_times)
            ]
            if not nearby:
                continue
            gap = min(abs(value - exchange.response_end_ms) for value in nearby)
            if gap <= tolerance_ms:
                scores.setdefault(exchange.did, []).append(gap)
    rows_by_did = []
    for did in sorted(totals):
        gaps = sorted(scores.get(did, []))
        median = None
        if gaps:
            middle = len(gaps) // 2
            median = gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2
        rows_by_did.append(
            {
                "did": did,
                "near_gauge_rows": len(gaps),
                "total_exchanges": totals[did],
                "median_abs_gap_ms": median,
            }
        )
    rows_by_did.sort(
        key=lambda item: (
            -item["near_gauge_rows"],
            item["median_abs_gap_ms"] if item["median_abs_gap_ms"] is not None else math.inf,
            item["did"],
        )
    )
    chosen = rows_by_did[0]["did"] if rows_by_did and rows_by_did[0]["near_gauge_rows"] >= 3 else None
    ambiguous = False
    if chosen and len(rows_by_did) > 1:
        first, second = rows_by_did[:2]
        ambiguous = (
            second["near_gauge_rows"] == first["near_gauge_rows"]
            and abs(second["median_abs_gap_ms"] - first["median_abs_gap_ms"]) <= 5
        )
    return {
        "did": None if ambiguous else chosen,
        "best_did": chosen,
        "ambiguous": ambiguous,
        "tolerance_ms": tolerance_ms,
        "scores": rows_by_did,
    }


def build_cycles(
    exchanges: Iterable[DidExchange],
    max_run_gap_ms: int = 5000,
    *,
    boundary_did: str | None = None,
) -> list[PollCycle]:
    cycles: list[PollCycle] = []
    current: list[DidExchange] = []
    run_id = 0
    run_index = 0
    last: DidExchange | None = None
    learned_boundary = boundary_did
    active = False

    def finish() -> None:
        nonlocal current, run_index
        if current:
            cycles.append(PollCycle(len(cycles), run_id, run_index, current))
            run_index += 1
        current = []

    for exchange in exchanges:
        discontinuity = (
            last is not None
            and (
                exchange.address != last.address
                or exchange.profile != last.profile
                or exchange.date != last.date
                or exchange.request_ms < last.request_ms
                or exchange.request_ms - last.response_end_ms > max_run_gap_ms
            )
        )
        if discontinuity:
            if active:
                finish()
            else:
                current = []
            run_id += 1
            run_index = 0
            learned_boundary = boundary_did
            active = False

        if not active and learned_boundary is not None:
            if exchange.did == learned_boundary:
                current = [exchange]
                active = True
            last = exchange
            continue

        if not active:
            # Fallback for sparse sections that cannot infer a timestamp boundary:
            # learn the first repeated DID and discard any one-off startup prelude.
            prior = next(
                (index for index, item in enumerate(current) if item.did == exchange.did),
                None,
            )
            if prior is None:
                current.append(exchange)
                last = exchange
                continue
            learned_boundary = exchange.did
            current = current[prior:]
            active = True
            finish()
            current = [exchange]
            last = exchange
            continue

        # A stable polling loop begins again when its learned first DID repeats.
        # Other duplicated DIDs remain in the cycle; the fitter refuses to choose
        # between duplicate observations of the same DID.
        if exchange.did == learned_boundary:
            finish()
            current = [exchange]
        else:
            current.append(exchange)
        last = exchange
    if active:
        finish()
    return cycles


@dataclass
class Alignment:
    row_index: int
    cycle_id: int | None
    offset_ms: int | None
    second_best_gap_ms: int | None
    ambiguous_time: bool


def align_rows(
    rows: list[GaugeRow],
    cycles: list[PollCycle],
    *,
    max_time_error_ms: int = 750,
    ambiguity_margin_ms: int = 100,
) -> list[Alignment]:
    ordered = sorted(enumerate(cycles), key=lambda item: (item[1].anchor_ms, item[0]))
    anchors = [cycle.anchor_ms for _, cycle in ordered]
    alignments: list[Alignment] = []
    for row_index, row in enumerate(rows):
        lower = bisect_left(anchors, row.time_ms - max_time_error_ms)
        upper = bisect_right(anchors, row.time_ms + max_time_error_ms)
        ranked = sorted(
            range(lower, upper),
            key=lambda item: (abs(anchors[item] - row.time_ms), ordered[item][0]),
        )
        if not ranked:
            alignments.append(Alignment(row_index, None, None, None, False))
            continue
        best = ranked[0]
        best_cycle_id = ordered[best][0]
        best_gap = abs(anchors[best] - row.time_ms)
        second_gap = abs(anchors[ranked[1]] - row.time_ms) if len(ranked) > 1 else None
        alignments.append(
            Alignment(
                row_index,
                best_cycle_id,
                row.time_ms - anchors[best],
                second_gap,
                second_gap is not None and second_gap - best_gap <= ambiguity_margin_ms,
            )
        )
    return alignments


def numeric_value(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def affine_fit(points: list[tuple[float, int]]) -> dict[str, float] | None:
    if len(points) < 3:
        return None
    ys = [point[0] for point in points]
    xs = [point[1] for point in points]
    if len(set(xs)) < 3 or len(set(ys)) < 3:
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((value - x_mean) ** 2 for value in xs)
    if denominator == 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    intercept = y_mean - slope * x_mean
    errors = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    y_range = max(ys) - min(ys)
    total = sum((value - y_mean) ** 2 for value in ys)
    return {
        "slope": slope,
        "intercept": intercept,
        "rmse": rmse,
        "normalized_rmse": rmse / y_range,
        "r_squared": 1.0 - sum(error * error for error in errors) / total if total else 0.0,
        "max_abs_error": max(abs(error) for error in errors),
    }


def fit_metric(
    section: GaugeSection,
    column_index: int,
    alignments: list[Alignment],
    cycles: list[PollCycle],
    *,
    allowed_dids: set[str] | None = None,
    min_samples: int = 6,
    lags: range = range(-2, 3),
    top_n: int = 5,
    max_hypotheses: int = DEFAULT_MAX_HYPOTHESES_PER_METRIC,
    source_scope: str = "unknown",
) -> dict[str, object]:
    label = section.columns[column_index]
    values: dict[int, float] = {}
    for row_index, row in enumerate(section.rows):
        if len(row.fields) != len(section.columns):
            continue
        number = numeric_value(row.fields[column_index])
        if number is not None:
            values[row_index] = number
    result: dict[str, object] = {
        "column_index": column_index,
        "label": label,
        "numeric_rows": len(values),
        "distinct_display_values": len(set(values.values())),
        "status": "unidentifiable",
        "reason": None,
        "candidates": [],
    }
    if len(values) < min_samples or len(set(values.values())) < 3:
        result["reason"] = "requires at least three varying displayed values and the minimum sample count"
        return result

    by_run_position = {(cycle.run_id, cycle.run_index): cycle for cycle in cycles}
    candidates: list[dict[str, object]] = []
    hypotheses_evaluated = 0
    for lag in lags:
        observations: list[tuple[int, float, PollCycle]] = []
        for alignment in alignments:
            if (
                alignment.cycle_id is None
                or alignment.ambiguous_time
                or alignment.row_index not in values
            ):
                continue
            base = cycles[alignment.cycle_id]
            shifted = by_run_position.get((base.run_id, base.run_index + lag))
            if shifted is not None:
                observations.append((alignment.row_index, values[alignment.row_index], shifted))
        denominator = len(observations)
        if denominator < min_samples:
            continue
        dids = sorted(
            {
                exchange.did
                for _, _, cycle in observations
                for exchange in cycle.exchanges
                if exchange.payload is not None
                and (allowed_dids is None or exchange.did in allowed_dids)
            }
        )
        for did in dids:
            payload_rows: list[tuple[int, float, bytes]] = []
            for row_index, displayed, cycle in observations:
                matches = [
                    exchange.payload for exchange in cycle.exchanges
                    if exchange.did == did and exchange.payload is not None
                ]
                if len(matches) == 1:
                    payload_rows.append((row_index, displayed, matches[0]))
            if len(payload_rows) < min_samples:
                continue
            minimum_length = min(len(payload) for _, _, payload in payload_rows)
            for start in range(minimum_length):
                for length in range(1, min(4, minimum_length - start) + 1):
                    interpretations = ["big"] if length == 1 else ["big", "little"]
                    equivalent: dict[tuple[tuple[int, int], ...], dict[str, object]] = {}
                    for byte_order in interpretations:
                        for signed in (False, True):
                            points_with_rows = [
                                (
                                    row_index,
                                    displayed,
                                    int.from_bytes(
                                        payload[start:start + length],
                                        byteorder=byte_order,
                                        signed=signed,
                                    ),
                                )
                                for row_index, displayed, payload in payload_rows
                            ]
                            vector = tuple((row_index, raw) for row_index, _, raw in points_with_rows)
                            group = equivalent.setdefault(
                                vector,
                                {
                                    "points": [(displayed, raw) for _, displayed, raw in points_with_rows],
                                    "interpretations": [],
                                },
                            )
                            group["interpretations"].append(
                                {"byte_order": byte_order, "signed": signed}
                            )
                    for group in equivalent.values():
                        hypotheses_evaluated += 1
                        if hypotheses_evaluated > max_hypotheses:
                            raise ValueError(
                                f"candidate hypotheses exceed {max_hypotheses} for metric "
                                f"{label!r}; restrict --did or raise "
                                "--max-hypotheses-per-metric deliberately"
                            )
                        points = group["points"]
                        if len(points) < min_samples:
                            continue
                        fitted = affine_fit(points)
                        if fitted is None:
                            continue
                        coverage = len(points) / denominator
                        quality = coverage / (1.0 + 10.0 * fitted["normalized_rmse"])
                        candidates.append(
                            {
                                "did": did,
                                "slice_start": start,
                                "slice_length": length,
                                "cycle_lag": lag,
                                "interpretations": group["interpretations"],
                                "samples": len(points),
                                "coverage": coverage,
                                "distinct_raw_values": len({raw for _, raw in points}),
                                **fitted,
                                "quality_score": quality,
                                "exact_to_0_001": fitted["max_abs_error"] <= 0.00051,
                            }
                        )
    candidates.sort(
        key=lambda item: (
            -item["quality_score"],
            item["normalized_rmse"],
            -item["samples"],
            item["slice_length"],
            item["did"],
        )
    )
    if not candidates:
        result["reason"] = "no exact-echo DID slice had enough varying aligned samples"
        return result

    top = candidates[0]
    close_runner = any(
        candidate["quality_score"] >= top["quality_score"] - 0.01
        and candidate["normalized_rmse"] <= top["normalized_rmse"] + 0.01
        and (
            candidate["did"], candidate["slice_start"], candidate["slice_length"], candidate["cycle_lag"]
        ) != (top["did"], top["slice_start"], top["slice_length"], top["cycle_lag"])
        for candidate in candidates[1:]
    )
    interpretation_ambiguity = len(top["interpretations"]) > 1
    top["mapping_ambiguity"] = close_runner
    top["interpretation_ambiguity"] = interpretation_ambiguity
    provenance_status = (
        "historical_reference_candidate"
        if source_scope == "historical-other-vehicle"
        else "correlation_candidate"
    )
    result["status"] = (
        "ambiguous_" + provenance_status
        if close_runner or interpretation_ambiguity
        else provenance_status
    )
    result["reason"] = (
        "correlation and affine fit only; independent/current-vehicle validation is required"
    )
    result["candidates"] = candidates[:top_n]
    result["hypotheses_evaluated"] = hypotheses_evaluated
    result["lag_scores"] = [
        {
            "cycle_lag": lag,
            "best_quality_score": max(
                (candidate["quality_score"] for candidate in candidates if candidate["cycle_lag"] == lag),
                default=None,
            ),
            "best_normalized_rmse": min(
                (candidate["normalized_rmse"] for candidate in candidates if candidate["cycle_lag"] == lag),
                default=None,
            ),
        }
        for lag in lags
    ]
    return result


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            for row in rows:
                json.dump(row, handle, ensure_ascii=False)
                handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Offline AlfaOBD gauge/debug DID correlation")
    result.add_argument("gauges", type=Path)
    result.add_argument("debug", type=Path, help="one decoded AlfaOBD debug log (never pool snapshots)")
    result.add_argument("--section", type=int, required=True, help="1-based Gauges_Data section index")
    result.add_argument("--address", help="optional exact ATSH address, e.g. DA10F1")
    result.add_argument("--boundary-did", help="override inferred polling-loop first DID")
    result.add_argument("--metric", action="append", help="exact metric label; repeatable")
    result.add_argument("--did", action="append", help="restrict candidate DID (four hex digits)")
    result.add_argument("--min-samples", type=int, default=6)
    result.add_argument(
        "--max-exchanges",
        type=int,
        default=DEFAULT_MAX_EXCHANGES,
        help=(
            "maximum matching debug exchanges retained for this one section/profile "
            f"(default: {DEFAULT_MAX_EXCHANGES}; raise deliberately for a larger archive)"
        ),
    )
    result.add_argument(
        "--max-hypotheses-per-metric",
        type=int,
        default=DEFAULT_MAX_HYPOTHESES_PER_METRIC,
        help=(
            "maximum byte-slice/endian/signed/lag hypotheses evaluated per metric "
            f"(default: {DEFAULT_MAX_HYPOTHESES_PER_METRIC})"
        ),
    )
    result.add_argument("--max-time-error-ms", type=int, default=750)
    result.add_argument("--top", type=int, default=5)
    result.add_argument(
        "--source-scope",
        choices=("unknown", "historical-other-vehicle", "current-van"),
        default="unknown",
    )
    result.add_argument("--out-dir", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if (
        args.section < 1
        or args.min_samples < 3
        or args.top < 1
        or args.max_exchanges < 1
        or args.max_hypotheses_per_metric < 1
        or args.max_time_error_ms < 0
    ):
        print(
            "error: section/top/input limits must be positive, max-time-error-ms must be "
            "nonnegative, and min-samples must be at least 3",
            file=sys.stderr,
        )
        return 2
    allowed_dids = None
    if args.did:
        allowed_dids = {value.upper().removeprefix("0X") for value in args.did}
        if any(re.fullmatch(r"[0-9A-F]{4}", value) is None for value in allowed_dids):
            print("error: --did values must be exactly four hexadecimal digits", file=sys.stderr)
            return 2
    boundary_override = None
    if args.boundary_did:
        boundary_override = args.boundary_did.upper().removeprefix("0X")
        if re.fullmatch(r"[0-9A-F]{4}", boundary_override) is None:
            print("error: --boundary-did must be exactly four hexadecimal digits", file=sys.stderr)
            return 2
    try:
        section = next(
            (item for item in iter_gauge_sections(args.gauges) if item.index == args.section),
            None,
        )
        if section is None:
            raise ValueError(f"gauge section {args.section} not found")
        if not section.rows:
            raise ValueError(f"gauge section {args.section} has no sample rows")
        exchanges: list[DidExchange] = []
        for exchange in iter_did_exchanges(
            args.debug,
            date=section.date,
            profile=section.profile,
            address=args.address,
        ):
            if len(exchanges) >= args.max_exchanges:
                raise ValueError(
                    f"matching debug exchanges exceed --max-exchanges {args.max_exchanges}; "
                    "narrow the section/profile/address or raise the limit deliberately"
                )
            exchanges.append(exchange)
        if not exchanges:
            raise ValueError(
                "no matching single-DID 22 exchanges for the selected date/profile/address"
            )
        addresses = sorted({exchange.address for exchange in exchanges})
        if not args.address and len(addresses) > 1:
            raise ValueError(f"multiple debug addresses matched ({', '.join(addresses)}); pass --address")
        boundary_inference = infer_boundary_did(exchanges, section.rows)
        if boundary_override is None and boundary_inference["did"] is None:
            score_preview = ", ".join(
                f"{item['did']}:{item['near_gauge_rows']} rows/"
                f"{item['median_abs_gap_ms']} ms"
                for item in boundary_inference["scores"][:5]
            ) or "none"
            raise ValueError(
                "could not infer one unambiguous polling-boundary DID from prompt-completion "
                f"timestamps (top scores: {score_preview}); inspect the trace and rerun with "
                "--boundary-did"
            )
        selected_boundary = boundary_override or boundary_inference["did"]
        cycles = build_cycles(exchanges, boundary_did=selected_boundary)
        observed_boundaries = sorted({cycle.exchanges[0].did for cycle in cycles})
        alignments = align_rows(
            section.rows,
            cycles,
            max_time_error_ms=args.max_time_error_ms,
        )
        selected_columns = [
            index for index, label in enumerate(section.columns) if index > 0
            and (not args.metric or label in args.metric)
        ]
        if args.metric and len(selected_columns) == 0:
            raise ValueError("none of the requested --metric labels occur in the selected section")
        metrics = [
            fit_metric(
                section,
                index,
                alignments,
                cycles,
                allowed_dids=allowed_dids,
                min_samples=args.min_samples,
                top_n=args.top,
                max_hypotheses=args.max_hypotheses_per_metric,
                source_scope=args.source_scope,
            )
            for index in selected_columns
        ]
        gauge_hash = file_sha256(args.gauges)
        debug_hash = file_sha256(args.debug)
        output_dir = args.out_dir or DEFAULT_OUTPUT_ROOT / (
            f"section_{section.index:04d}_{gauge_hash[:8]}_{debug_hash[:8]}"
        )
        use_counts: dict[int, int] = {}
        for alignment in alignments:
            if alignment.cycle_id is not None:
                use_counts[alignment.cycle_id] = use_counts.get(alignment.cycle_id, 0) + 1
        absolute_offsets = sorted(
            abs(alignment.offset_ms)
            for alignment in alignments
            if alignment.offset_ms is not None
        )
        median_offset = None
        if absolute_offsets:
            middle = len(absolute_offsets) // 2
            median_offset = (
                absolute_offsets[middle]
                if len(absolute_offsets) % 2
                else (absolute_offsets[middle - 1] + absolute_offsets[middle]) / 2
            )
        report = {
            "schema_version": 1,
            "method": "offline_gauge_debug_affine_candidates",
            "verification_status": "candidate_only",
            "interpretation_warning": (
                "Fits are time-correlated candidates, not verified DID names/scalings. Historical-other-vehicle "
                "inputs are reference-only and must never be promoted as current-van truth."
            ),
            "source_scope": args.source_scope,
            "gauges": {"path": str(args.gauges), "sha256": gauge_hash, "size_bytes": args.gauges.stat().st_size},
            "debug": {"path": str(args.debug), "sha256": debug_hash, "size_bytes": args.debug.stat().st_size},
            "section": {
                "index": section.index,
                "profile": section.profile,
                "profile_source": section.profile_source,
                "date": section.date,
                "date_raw": section.date_raw,
                "start_line": section.start_line,
                "header_line": section.header_line,
                "end_line": section.end_line,
                "columns": section.columns,
                "sample_rows": len(section.rows),
                "unparsed_lines": section.unparsed_lines,
            },
            "debug_filter": {"address": args.address, "matched_addresses": addresses},
            "input_limits": {
                "max_matching_debug_exchanges": args.max_exchanges,
                "max_hypotheses_per_metric": args.max_hypotheses_per_metric,
            },
            "polling": {
                "did_exchanges": len(exchanges),
                "cycles": len(cycles),
                "cycle_boundary": (
                    selected_boundary
                    or (observed_boundaries[0] if len(observed_boundaries) == 1 else None)
                ),
                "observed_cycle_boundary_dids": observed_boundaries,
                "cycle_boundary_source": (
                    "user_override"
                    if boundary_override
                    else "gauge_response_timestamp_inference"
                    if boundary_inference["did"]
                    else "first_repeated_DID_fallback"
                ),
                "cycle_boundary_inference": boundary_inference,
                "assigned_exchanges": sum(len(cycle.exchanges) for cycle in cycles),
                "unassigned_startup_or_partial_exchanges": (
                    len(exchanges) - sum(len(cycle.exchanges) for cycle in cycles)
                ),
            },
            "scoring": {
                "cycle_lags_tested": [-2, -1, 0, 1, 2],
                "minimum_samples": args.min_samples,
                "minimum_distinct_raw_and_display_values": 3,
                "quality_score": "coverage / (1 + 10 * normalized_rmse)",
                "time_ambiguity_margin_ms": 100,
                "mapping_close_runner_tolerance": 0.01,
                "ambiguous_time_rows_used_in_fit": False,
            },
            "alignment": {
                "anchor": "response_end_of_selected polling-boundary DID",
                "matched_rows": sum(item.cycle_id is not None for item in alignments),
                "unmatched_rows": sum(item.cycle_id is None for item in alignments),
                "ambiguous_time_rows": sum(item.ambiguous_time for item in alignments),
                "reused_cycle_rows": sum(max(0, count - 1) for count in use_counts.values()),
                "median_abs_offset_ms": median_offset,
                "max_abs_offset_ms": max(absolute_offsets) if absolute_offsets else None,
                "max_time_error_ms": args.max_time_error_ms,
            },
            "metrics": metrics,
            "outputs": {"gauge_rows": "gauge_rows.jsonl", "cycles": "cycles.jsonl"},
        }
        atomic_jsonl(
            output_dir / "gauge_rows.jsonl",
            (
                {
                    "section_index": section.index,
                    "line_number": row.line_number,
                    "timestamp": row.timestamp,
                    "fields": row.fields,
                    "width_valid": len(row.fields) == len(section.columns),
                    "base_cycle_id": alignment.cycle_id,
                    "base_cycle_offset_ms": alignment.offset_ms,
                    "second_best_gap_ms": alignment.second_best_gap_ms,
                    "ambiguous_time": alignment.ambiguous_time,
                    "base_cycle_use_count": use_counts.get(alignment.cycle_id, 0),
                }
                for row, alignment in zip(section.rows, alignments)
            ),
        )
        atomic_jsonl(
            output_dir / "cycles.jsonl",
            (
                {
                    "cycle_id": cycle.id,
                    "run_id": cycle.run_id,
                    "run_index": cycle.run_index,
                    "anchor_ts": cycle.anchor_ts,
                    "exchanges": [
                        {
                            "did": exchange.did,
                            "request_ts": exchange.request_ts,
                            "response_end_ts": exchange.response_end_ts,
                            "request_line": exchange.request_line,
                            "response_end_line": exchange.response_end_line,
                            "completion_reason": exchange.completion_reason,
                            "prompt_seen": exchange.prompt_seen,
                            "request": exchange.request,
                            "response": exchange.response,
                            "exact_positive_echo": exchange.payload is not None,
                            "response_pending_count": exchange.pending_count,
                        }
                        for exchange in cycle.exchanges
                    ],
                }
                for cycle in cycles
            ),
        )
        # Publish the summary last so it never points at half-written evidence files.
        atomic_json(output_dir / "report.json", report)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Section {section.index}: {len(section.rows)} gauge rows; {len(cycles)} debug cycles")
    print(f"Aligned {report['alignment']['matched_rows']} rows; reports: {output_dir}")
    for metric in metrics:
        if metric["candidates"]:
            candidate = metric["candidates"][0]
            print(
                f"  [{metric['column_index']}] {metric['label']}: {metric['status']}; "
                f"DID {candidate['did']} bytes {candidate['slice_start']}+{candidate['slice_length']} "
                f"lag {candidate['cycle_lag']:+d}; displayed={candidate['slope']:.9g}*raw"
                f"{candidate['intercept']:+.9g}"
            )
        else:
            print(f"  [{metric['column_index']}] {metric['label']}: {metric['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
