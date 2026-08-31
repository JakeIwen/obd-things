#!/usr/bin/env python3
"""Offline bounded C-CAN speed to CAN-CH field correlator.

This tool accepts one finalized C-CAN candump chunk and its synchronized
CAN-CH chunk.  It decodes the established C-CAN 0x101 vehicle-speed field and
ranks coarse scalar fields only on an explicit candidate-ID allowlist.  It is
an exploratory shortlist tool: high correlation does not establish wheel
identity, sign, absolute scaling, or safety-critical suitability.

The tool has no SocketCAN imports or live mode.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterator, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.can_capture_summary import Frame, parse_frame
from lib.signal_fields import SignalField, SignalFieldError


REFERENCE_ID = 0x101
MAX_INPUT_BYTES = 2 * 1024 * 1024 * 1024
MAX_INPUT_LINES = 20_000_000
MAX_DISTINCT_VALUES = 4096
MAX_TOP = 200
_CHANNEL_RE = re.compile(r"\S+\Z")


class CrossBusError(RuntimeError):
    pass


@dataclass
class RunningFit:
    samples: int = 0
    sum_x: float = 0.0
    sum_y: float = 0.0
    sum_xx: float = 0.0
    sum_yy: float = 0.0
    sum_xy: float = 0.0
    minimum_x: float | None = None
    maximum_x: float | None = None
    minimum_y: float | None = None
    maximum_y: float | None = None
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    distinct_x: set[int] = field(default_factory=set)

    def add(self, x: int, y: float, timestamp: float) -> None:
        self.samples += 1
        self.sum_x += x
        self.sum_y += y
        self.sum_xx += x * x
        self.sum_yy += y * y
        self.sum_xy += x * y
        self.minimum_x = x if self.minimum_x is None else min(self.minimum_x, x)
        self.maximum_x = x if self.maximum_x is None else max(self.maximum_x, x)
        self.minimum_y = y if self.minimum_y is None else min(self.minimum_y, y)
        self.maximum_y = y if self.maximum_y is None else max(self.maximum_y, y)
        self.first_timestamp = (
            timestamp if self.first_timestamp is None else min(self.first_timestamp, timestamp)
        )
        self.last_timestamp = (
            timestamp if self.last_timestamp is None else max(self.last_timestamp, timestamp)
        )
        if len(self.distinct_x) < MAX_DISTINCT_VALUES:
            self.distinct_x.add(x)

    def result(self) -> dict[str, object] | None:
        n = self.samples
        if n < 2:
            return None
        var_x = self.sum_xx - self.sum_x * self.sum_x / n
        var_y = self.sum_yy - self.sum_y * self.sum_y / n
        if var_x <= 0 or var_y <= 0:
            return None
        covariance = self.sum_xy - self.sum_x * self.sum_y / n
        r_squared = covariance * covariance / (var_x * var_y)
        slope = covariance / var_x
        intercept = (self.sum_y - slope * self.sum_x) / n
        residual_sum_squares = max(0.0, var_y - covariance * covariance / var_x)
        return {
            "samples": n,
            "distinct_candidate_values": len(self.distinct_x),
            "candidate_minimum": self.minimum_x,
            "candidate_maximum": self.maximum_x,
            "reference_minimum_kph": self.minimum_y,
            "reference_maximum_kph": self.maximum_y,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "r_squared": r_squared,
            "reference_kph_per_candidate_count": slope,
            "reference_kph_intercept": intercept,
            "affine_rmse_kph": math.sqrt(residual_sum_squares / n),
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_lines(path: Path) -> Iterator[str]:
    if not path.is_file():
        raise CrossBusError(f"capture does not exist: {path}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise CrossBusError(f"capture exceeds byte limit: {path}")
    if path.suffix.lower() != ".zst":
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if line_number > MAX_INPUT_LINES:
                    raise CrossBusError("capture exceeds line-count limit")
                yield line
        return
    try:
        process = subprocess.Popen(
            ["zstd", "-dc", "--", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise CrossBusError(f"cannot start zstd for {path}: {exc}") from exc
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise CrossBusError("zstd did not provide output pipes")
    try:
        for line_number, line in enumerate(process.stdout, 1):
            if line_number > MAX_INPUT_LINES:
                process.kill()
                raise CrossBusError("capture exceeds line-count limit")
            yield line
        stderr = process.stderr.read()
        return_code = process.wait()
    finally:
        process.stdout.close()
        process.stderr.close()
    if return_code != 0:
        raise CrossBusError(stderr.strip() or f"zstd exited {return_code}")


def _iter_frames(path: Path, channel: str) -> Iterator[Frame]:
    for line in _iter_lines(path):
        frame = parse_frame(line)
        if frame is not None and frame.interface == channel:
            yield frame


def _decode_speed_kph(payload: bytes) -> float | None:
    if len(payload) < 3:
        return None
    raw = ((payload[0] & 0x01) << 11) | (payload[1] << 3) | (payload[2] >> 5)
    value = raw / 16.0
    return value if math.isfinite(value) and 0.0 <= value <= 260.0 else None


def _candidate_fields(payload: bytes) -> Iterator[tuple[str, int]]:
    for offset, value in enumerate(payload):
        yield f"byte:{offset}", value
    for offset in range(max(0, len(payload) - 1)):
        word = (payload[offset] << 8) | payload[offset + 1]
        yield f"u16be:{offset}", word
        yield f"u16le:{offset}", (payload[offset + 1] << 8) | payload[offset]
        yield f"u12be-hi:{offset}", word >> 4
        yield f"u12be-lo:{offset}", word & 0x0FFF
        yield f"u13be-hi:{offset}", word >> 3
        yield f"u13be-lo:{offset}", word & 0x1FFF


@lru_cache(maxsize=None)
def _bit_field_specs(dlc: int, lengths: tuple[int, ...]) -> tuple[tuple[str, SignalField], ...]:
    specs = []
    for byte_order in ("little", "big"):
        for length in lengths:
            for start in range(dlc * 8):
                try:
                    geometry = SignalField(start, length, byte_order, signed=False)
                except SignalFieldError:
                    continue
                if geometry.required_payload_bytes > dlc:
                    continue
                specs.append(
                    (
                        f"bits:{byte_order}:{start}:{length}:unsigned",
                        geometry,
                    )
                )
    return tuple(specs)


def _bit_candidate_fields(
    payload: bytes,
    lengths: tuple[int, ...],
) -> Iterator[tuple[str, int]]:
    for label, geometry in _bit_field_specs(len(payload), lengths):
        try:
            yield label, geometry.extract(payload)
        except SignalFieldError:
            continue


def _nearest_reference(
    timestamps: list[float],
    values: list[float],
    timestamp: float,
    radius_seconds: float,
) -> float | None:
    position = bisect_left(timestamps, timestamp)
    choices = []
    if position < len(timestamps):
        choices.append(position)
    if position > 0:
        choices.append(position - 1)
    if not choices:
        return None
    best = min(choices, key=lambda index: abs(timestamps[index] - timestamp))
    return values[best] if abs(timestamps[best] - timestamp) <= radius_seconds else None


def correlate(
    reference_capture: Path,
    candidate_capture: Path,
    *,
    reference_channel: str,
    candidate_channel: str,
    candidate_ids: frozenset[int],
    radius_ms: float,
    minimum_speed_kph: float,
    minimum_samples: int,
    minimum_distinct: int,
    top: int,
    bit_search_ids: frozenset[int] = frozenset(),
    bit_search_lengths: tuple[int, ...] = (),
) -> dict[str, object]:
    reference_timestamps = []
    reference_values = []
    for frame in _iter_frames(reference_capture, reference_channel):
        if frame.id_bits != 11 or frame.can_id != REFERENCE_ID:
            continue
        speed = _decode_speed_kph(frame.payload)
        if speed is None or speed < minimum_speed_kph:
            continue
        reference_timestamps.append(frame.timestamp)
        reference_values.append(speed)
    if len(reference_timestamps) < minimum_samples:
        raise CrossBusError("insufficient moving C-CAN 0x101 reference samples")
    if any(
        later < earlier
        for earlier, later in zip(reference_timestamps, reference_timestamps[1:])
    ):
        raise CrossBusError("reference capture timestamps are not chronological")

    fits: dict[tuple[int, int, str], RunningFit] = {}
    selected_candidate_frames = 0
    matched_candidate_frames = 0
    radius_seconds = radius_ms / 1000.0
    for frame in _iter_frames(candidate_capture, candidate_channel):
        if frame.id_bits != 11 or frame.can_id not in candidate_ids:
            continue
        selected_candidate_frames += 1
        reference = _nearest_reference(
            reference_timestamps,
            reference_values,
            frame.timestamp,
            radius_seconds,
        )
        if reference is None:
            continue
        matched_candidate_frames += 1
        fields = (
            _bit_candidate_fields(frame.payload, bit_search_lengths)
            if frame.can_id in bit_search_ids
            else _candidate_fields(frame.payload)
        )
        for label, value in fields:
            key = (frame.can_id, frame.dlc, label)
            fits.setdefault(key, RunningFit()).add(value, reference, frame.timestamp)

    rows = []
    for (can_id, dlc, label), fit in fits.items():
        result = fit.result()
        if (
            result is None
            or result["samples"] < minimum_samples
            or result["distinct_candidate_values"] < minimum_distinct
        ):
            continue
        rows.append(
            {
                "can_id": f"0x{can_id:03X}",
                "id_bits": 11,
                "dlc": dlc,
                "field": label,
                **result,
            }
        )
    rows.sort(
        key=lambda row: (
            -float(row["r_squared"]),
            float(row["affine_rmse_kph"]),
            str(row["can_id"]),
            str(row["field"]),
        )
    )
    return {
        "schema_version": 1,
        "classification": "exploratory_cross_bus_speed_shortlist",
        "warning": (
            "Correlation to vehicle speed does not establish wheel identity, "
            "absolute scale, or a safety-critical decode."
        ),
        "reference": {
            "capture": str(reference_capture),
            "sha256": _sha256(reference_capture),
            "channel": reference_channel,
            "can_id": "0x101",
            "decode": "((b0 & 1) << 11 | b1 << 3 | b2 >> 5) / 16 km/h",
            "moving_samples": len(reference_timestamps),
            "minimum_speed_kph": minimum_speed_kph,
        },
        "candidate": {
            "capture": str(candidate_capture),
            "sha256": _sha256(candidate_capture),
            "channel": candidate_channel,
            "ids": [f"0x{value:03X}" for value in sorted(candidate_ids)],
            "selected_frames": selected_candidate_frames,
            "matched_frames": matched_candidate_frames,
            "radius_ms": radius_ms,
            "bit_search_ids": [
                f"0x{value:03X}" for value in sorted(bit_search_ids)
            ],
            "bit_search_lengths": list(bit_search_lengths),
        },
        "selection": {
            "minimum_samples": minimum_samples,
            "minimum_distinct": minimum_distinct,
            "top": top,
        },
        "ranked_fields": rows[:top],
        "eligible_field_count": len(rows),
    }


def _parse_id(value: str) -> int:
    try:
        parsed = int(value, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("candidate ID must be hexadecimal") from exc
    if not 0 <= parsed <= 0x7FF:
        raise argparse.ArgumentTypeError("candidate ID must be an 11-bit identifier")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference_capture", type=Path)
    parser.add_argument("candidate_capture", type=Path)
    parser.add_argument("--reference-channel", required=True)
    parser.add_argument("--candidate-channel", required=True)
    parser.add_argument("--candidate-id", action="append", type=_parse_id, required=True)
    parser.add_argument("--bit-search-id", action="append", type=_parse_id, default=[])
    parser.add_argument("--bit-search-length", action="append", type=int, default=[])
    parser.add_argument("--radius-ms", type=float, default=20.0)
    parser.add_argument("--minimum-speed-kph", type=float, default=5.0)
    parser.add_argument("--minimum-samples", type=int, default=100)
    parser.add_argument("--minimum-distinct", type=int, default=8)
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not _CHANNEL_RE.fullmatch(args.reference_channel) or not _CHANNEL_RE.fullmatch(
        args.candidate_channel
    ):
        raise SystemExit("capture channels must be nonempty and contain no whitespace")
    if (
        not math.isfinite(args.radius_ms)
        or not 0 < args.radius_ms <= 1000
        or not math.isfinite(args.minimum_speed_kph)
        or not 0 <= args.minimum_speed_kph <= 260
        or not 2 <= args.minimum_samples <= 5_000_000
        or not 2 <= args.minimum_distinct <= MAX_DISTINCT_VALUES
        or not 1 <= args.top <= MAX_TOP
        or any(not 1 <= length <= 32 for length in args.bit_search_length)
        or any(value not in args.candidate_id for value in args.bit_search_id)
        or bool(args.bit_search_id) != bool(args.bit_search_length)
    ):
        raise SystemExit("invalid bounded correlation parameters")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite output: {args.output}")
    try:
        report = correlate(
            args.reference_capture,
            args.candidate_capture,
            reference_channel=args.reference_channel,
            candidate_channel=args.candidate_channel,
            candidate_ids=frozenset(args.candidate_id),
            radius_ms=args.radius_ms,
            minimum_speed_kph=args.minimum_speed_kph,
            minimum_samples=args.minimum_samples,
            minimum_distinct=args.minimum_distinct,
            top=args.top,
            bit_search_ids=frozenset(args.bit_search_id),
            bit_search_lengths=tuple(sorted(set(args.bit_search_length))),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except (CrossBusError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {len(report['ranked_fields'])} ranked fields to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
