#!/usr/bin/env python3
"""Strictly offline, bounded-memory CAN time-series correlator.

The reference stream is a completed ``cluster_wire.jsonl`` produced by
``projects/ecu_mapping/cluster_drive_log.py``.  Only rows classified as
``exact_positive_response`` for the explicitly selected DID become reference
samples.  Saved candump text (plain or ``.zst``) supplies candidate broadcast
fields.

Every classic-CAN payload contributes byte fields, overlapping unsigned 16-bit
big- and little-endian fields, and aligned unsigned 32-bit fields in both byte
orders.  A streaming chronological merge matches those fields to each reference
sample either by the nearest frame or by a mean/min/max statistic in a symmetric
time window.  Online covariance keeps memory independent of capture duration;
explicit caps bound the remaining time-window, identifier, match-state, field,
reference, frame, and decompressed-byte state.

This module never opens CAN, imports ADB, inspects services, or accesses the
network.  Its only subprocess boundary is :class:`CliZstdDecompressor`, whose
fixed argv invokes ``zstd -dc -- <path>`` for a ``.zst`` input.  Plain inputs
use no subprocess.

Correlation establishes association and a candidate affine relationship, not
signal identity or physical scaling.  Reports therefore always carry the
``candidate_only`` classification.
"""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import BinaryIO, ContextManager, Iterable, Iterator, Protocol, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.modules import MODULES


TMP_ROOT = REPO / "tmp"
CLUSTER_MODULE = MODULES["cluster"]

SCHEMA_VERSION = 1
MAX_WIRE_LINE_BYTES = 1 << 20
MAX_WIRE_STREAM_LINES = 1_500_000
MAX_WIRE_STREAM_BYTES = 1 << 30
MAX_CANDUMP_LINE_BYTES = 4096
MAX_CAPTURE_FILES = 256
MAX_ACTIVE_REFERENCES = 1_000
MAX_PENDING_WIRE_LINKS = 1_000
MAX_CANDIDATE_IDS = 1_024
MAX_HISTORY_FRAMES = 250_000
MAX_ACTIVE_MATCH_STATES = 100_000
MAX_ACTIVE_WINDOW_FIELDS = 100_000
MAX_CANDIDATE_FIELDS = 30_000
MAX_RADIUS_MS = 10_000
MAX_TOP_COUNT = 1_000
# One selected DID is sampled at roughly 1 Hz by cluster_drive_log.py, so this
# still covers a full 20-hour leg with ample margin while bounding cumulative
# candidate/reference work.
MAX_REFERENCE_SAMPLES = 100_000
MAX_CAPTURE_FRAMES = 300_000_000
MAX_CAPTURE_DECOMPRESSED_BYTES = 64 * 1024**3
MAX_TRACKED_DISTINCT_VALUES = 16

_TIMESTAMP = rb"(?P<timestamp>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
_LONG_FRAME = re.compile(
    rb"^\s*\(" + _TIMESTAMP + rb"\)\s+"
    rb"(?P<interface>\S+)\s+"
    rb"(?P<can_id>[0-9A-Fa-f]{1,8})\s+"
    rb"\[(?P<dlc>\d{1,2})\]"
    rb"(?:\s+(?P<data>[0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})*))?\s*$"
)
_COMPACT_FRAME = re.compile(
    rb"^\s*\(" + _TIMESTAMP + rb"\)\s+"
    rb"(?P<interface>\S+)\s+"
    rb"(?P<can_id>[0-9A-Fa-f]{1,8})#"
    rb"(?P<data>(?:[0-9A-Fa-f]{2})*)\s*$"
)
_REFERENCE_FIELD_RE = re.compile(
    r"^(byte|u16be|u16le|u32be|u32le):([0-9]{1,4})$"
)


class CorrelateError(RuntimeError):
    """Raised for malformed evidence or a violated offline-analysis gate."""


@dataclass(frozen=True)
class ReferenceSample:
    timestamp_us: int
    value: float
    raw_line_sequence: int | None = None
    expected_can_id: int | None = None
    expected_can_data: bytes | None = None

    @property
    def has_wire_link(self) -> bool:
        values = (
            self.raw_line_sequence,
            self.expected_can_id,
            self.expected_can_data,
        )
        if all(value is None for value in values):
            return False
        if any(value is None for value in values):
            raise CorrelateError("reference has an incomplete raw-frame link")
        return True


@dataclass(frozen=True)
class CanFrame:
    timestamp_us: int
    can_id: int
    id_bits: int
    payload: bytes
    raw_line_sequence: int | None = None


@dataclass(frozen=True, order=True)
class FieldSpec:
    kind: str
    offset: int

    @property
    def width_bytes(self) -> int:
        if self.kind == "byte":
            return 1
        if self.kind.startswith("u16"):
            return 2
        if self.kind.startswith("u32"):
            return 4
        raise CorrelateError(f"unsupported field kind {self.kind!r}")

    @property
    def byte_order(self) -> str | None:
        if self.kind == "u16be":
            return "big"
        if self.kind == "u16le":
            return "little"
        if self.kind == "u32be":
            return "big"
        if self.kind == "u32le":
            return "little"
        return None

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.offset}"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "offset": self.offset,
            "width_bytes": self.width_bytes,
            "byte_order": self.byte_order,
            "signed": False,
        }


@dataclass(frozen=True, order=True)
class CandidateKey:
    can_id: int
    id_bits: int
    field: FieldSpec


@dataclass
class StreamStats:
    path: str
    compression: str
    lines: int = 0
    bytes_read: int = 0
    frames: int = 0
    blank_lines: int = 0
    first_timestamp_us: int | None = None
    last_timestamp_us: int | None = None
    _sha256: object = field(default_factory=hashlib.sha256, repr=False)

    def add_raw_line(self, line: bytes) -> None:
        self.lines += 1
        self.bytes_read += len(line)
        self._sha256.update(line)

    def add_timestamp(self, timestamp_us: int) -> None:
        self.frames += 1
        if self.first_timestamp_us is None:
            self.first_timestamp_us = timestamp_us
        self.last_timestamp_us = timestamp_us

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "compression": self.compression,
            "decompressed_stream_sha256": self._sha256.hexdigest(),
            "decompressed_bytes_read": self.bytes_read,
            "line_count": self.lines,
            "frame_count": self.frames,
            "blank_line_count": self.blank_lines,
            "first_timestamp_epoch_us": self.first_timestamp_us,
            "last_timestamp_epoch_us": self.last_timestamp_us,
        }


class Decompressor(Protocol):
    """Typed boundary for opening one zstd-compressed evidence stream."""

    def open(self, path: Path) -> ContextManager[BinaryIO]:
        """Return a context manager yielding decompressed bytes."""


class CliZstdDecompressor:
    """The sole allowed subprocess boundary: fixed-form zstd decompression."""

    def __init__(self, executable: str = "zstd") -> None:
        self.executable = executable

    @contextmanager
    def open(self, path: Path) -> Iterator[BinaryIO]:
        try:
            process = subprocess.Popen(
                [self.executable, "-dc", "--", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise CorrelateError(f"cannot start zstd for {path}: {exc}") from exc
        if process.stdout is None:
            process.kill()
            process.wait()
            raise CorrelateError("zstd stdout pipe was not created")

        completed = False
        try:
            yield process.stdout
            completed = True
        finally:
            process.stdout.close()
            if not completed and process.poll() is None:
                process.terminate()
            try:
                returncode = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                returncode = -9
            if completed and returncode != 0:
                raise CorrelateError(
                    f"zstd failed while reading {path} with status {returncode}"
                )


def _bounded_lines(handle: BinaryIO, *, maximum_line_bytes: int) -> Iterator[bytes]:
    while True:
        line = handle.readline(maximum_line_bytes + 1)
        if not line:
            return
        if len(line) > maximum_line_bytes:
            raise CorrelateError(
                f"input line exceeds the {maximum_line_bytes}-byte safety cap"
            )
        if not line.endswith(b"\n"):
            next_byte = handle.read(1)
            if next_byte:
                raise CorrelateError(
                    f"input line exceeds the {maximum_line_bytes}-byte safety cap"
                )
        yield line


def _timestamp_to_us(value: bytes | str, *, context: str) -> int:
    try:
        decimal_value = Decimal(value.decode("ascii") if isinstance(value, bytes) else value)
    except (InvalidOperation, UnicodeDecodeError) as exc:
        raise CorrelateError(f"{context} has an invalid timestamp") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise CorrelateError(f"{context} has a non-finite or negative timestamp")
    micros = decimal_value * Decimal(1_000_000)
    rounded = micros.to_integral_value(rounding=ROUND_HALF_EVEN)
    if micros != rounded:
        raise CorrelateError(f"{context} timestamp has sub-microsecond precision")
    return int(rounded)


def parse_candump_frame(line: bytes) -> CanFrame | None:
    """Parse one classic-CAN candump line; return ``None`` for a blank line."""
    if not line.strip():
        return None
    stripped = line.rstrip(b"\r\n")
    match = _LONG_FRAME.fullmatch(stripped)
    if match:
        dlc = int(match.group("dlc"), 10)
        data_text = match.group("data") or b""
        try:
            payload = bytes.fromhex(data_text.decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CorrelateError("candump line has invalid payload hex") from exc
        if len(payload) != dlc:
            raise CorrelateError("candump line DLC does not match its payload")
    else:
        match = _COMPACT_FRAME.fullmatch(stripped)
        if match is None:
            raise CorrelateError("malformed nonempty candump line")
        try:
            payload = bytes.fromhex(match.group("data").decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CorrelateError("candump line has invalid payload hex") from exc

    if len(payload) > 8:
        raise CorrelateError("CAN FD payloads are outside this classic-CAN analyzer")
    try:
        interface = match.group("interface").decode("ascii")
    except UnicodeDecodeError as exc:
        raise CorrelateError("candump interface is not ASCII") from exc
    if interface != CLUSTER_MODULE.channel:
        raise CorrelateError(
            f"candump interface must be the pinned cluster channel "
            f"{CLUSTER_MODULE.channel!r}"
        )
    can_id_text = match.group("can_id")
    can_id = int(can_id_text, 16)
    if len(can_id_text) == 3 and can_id <= 0x7FF:
        id_bits = 11
    elif len(can_id_text) == 8 and can_id <= 0x1FFFFFFF:
        id_bits = 29
    else:
        raise CorrelateError(
            "candump identifier must be exactly three SFF or eight EFF "
            "hexadecimal digits"
        )
    return CanFrame(
        timestamp_us=_timestamp_to_us(
            match.group("timestamp"), context="candump frame"
        ),
        can_id=can_id,
        id_bits=id_bits,
        payload=payload,
    )


def _parse_hex_did(value: str) -> int:
    text = value[2:] if value.lower().startswith("0x") else value
    if not re.fullmatch(r"[0-9A-Fa-f]{1,4}", text):
        raise argparse.ArgumentTypeError("DID must be one to four hexadecimal digits")
    return int(text, 16)


def _parse_reference_field(value: str) -> FieldSpec | None:
    if value == "auto":
        return None
    match = _REFERENCE_FIELD_RE.fullmatch(value)
    if match is None:
        raise CorrelateError(
            "reference field must be auto, byte:N, u16be:N, u16le:N, "
            "u32be:N, or u32le:N"
        )
    return FieldSpec(match.group(1), int(match.group(2), 10))


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r} is not accepted")


def _decode_field(payload: bytes, spec: FieldSpec) -> int:
    end = spec.offset + spec.width_bytes
    if spec.offset < 0 or end > len(payload):
        raise CorrelateError(
            f"field {spec.label} exceeds a {len(payload)}-byte payload"
        )
    if spec.kind == "byte":
        return payload[spec.offset]
    if spec.kind == "u16be":
        return int.from_bytes(payload[spec.offset:end], "big")
    if spec.kind == "u16le":
        return int.from_bytes(payload[spec.offset:end], "little")
    if spec.kind == "u32be":
        return int.from_bytes(payload[spec.offset:end], "big")
    if spec.kind == "u32le":
        return int.from_bytes(payload[spec.offset:end], "little")
    raise CorrelateError(f"unsupported field kind {spec.kind!r}")


class ReferenceDecoder:
    def __init__(self, requested: str) -> None:
        self.requested = requested
        self.resolved = _parse_reference_field(requested)

    def decode(self, data: bytes) -> int:
        if self.resolved is None:
            if len(data) == 1:
                self.resolved = FieldSpec("byte", 0)
            elif len(data) == 2:
                self.resolved = FieldSpec("u16be", 0)
            elif len(data) == 4:
                self.resolved = FieldSpec("u32be", 0)
            else:
                raise CorrelateError(
                    "auto reference decoding requires exactly one, two, or four "
                    "DID data bytes; select --reference-field explicitly"
                )
        return _decode_field(data, self.resolved)


def iter_reference_samples(
    path: Path,
    *,
    did: int,
    decoder: ReferenceDecoder,
    stats: StreamStats,
) -> Iterator[ReferenceSample]:
    previous_timestamp: int | None = None
    previous_raw_sequence: int | None = None
    selected_count = 0
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise CorrelateError(f"cannot read reference stream {path}: {exc}") from exc
    with handle:
        for line_number, line in enumerate(
            _bounded_lines(handle, maximum_line_bytes=MAX_WIRE_LINE_BYTES), 1
        ):
            if line_number > MAX_WIRE_STREAM_LINES:
                raise CorrelateError("reference stream line-count safety cap exceeded")
            if stats.bytes_read + len(line) > MAX_WIRE_STREAM_BYTES:
                raise CorrelateError("reference stream byte-count safety cap exceeded")
            stats.add_raw_line(line)
            if not line.strip():
                stats.blank_lines += 1
                continue
            try:
                row = json.loads(line, parse_constant=_reject_json_constant)
            except (
                json.JSONDecodeError,
                UnicodeDecodeError,
                ValueError,
                RecursionError,
            ) as exc:
                raise CorrelateError(
                    f"{path}:{line_number}: malformed JSON"
                ) from exc
            if not isinstance(row, dict):
                raise CorrelateError(f"{path}:{line_number}: JSON row is not an object")
            if row.get("classification") != "exact_positive_response":
                continue

            row_did = row.get("did")
            if not isinstance(row_did, str) or not re.fullmatch(
                r"[0-9A-Fa-f]{4}", row_did
            ):
                raise CorrelateError(
                    f"{path}:{line_number}: exact positive row has invalid DID"
                )
            row_did_value = int(row_did, 16)
            payload_text = row.get("isotp_payload_hex")
            if not isinstance(payload_text, str):
                raise CorrelateError(
                    f"{path}:{line_number}: exact positive row lacks payload hex"
                )
            try:
                payload = bytes.fromhex(payload_text)
            except ValueError as exc:
                raise CorrelateError(
                    f"{path}:{line_number}: invalid ISO-TP payload hex"
                ) from exc
            if (
                len(payload) < 4
                or payload[0] != 0x62
                or int.from_bytes(payload[1:3], "big") != row_did_value
            ):
                raise CorrelateError(
                    f"{path}:{line_number}: exact positive payload/DID mismatch"
                )
            timestamp_value = row.get("timestamp_epoch_us")
            if not isinstance(timestamp_value, int) or isinstance(timestamp_value, bool):
                raise CorrelateError(
                    f"{path}:{line_number}: invalid timestamp_epoch_us"
                )
            if timestamp_value < 0:
                raise CorrelateError(
                    f"{path}:{line_number}: negative timestamp_epoch_us"
                )
            if row_did_value != did:
                continue
            if (
                type(row.get("schema_version")) is not int
                or row.get("schema_version") != SCHEMA_VERSION
            ):
                raise CorrelateError(
                    f"{path}:{line_number}: selected row has invalid schema_version"
                )
            if row.get("type") != "wire_frame":
                raise CorrelateError(
                    f"{path}:{line_number}: selected row has invalid type"
                )
            if row.get("direction") != "cluster_to_tester":
                raise CorrelateError(
                    f"{path}:{line_number}: selected row has invalid direction"
                )
            if row.get("timestamp_source") != "candump_kernel":
                raise CorrelateError(
                    f"{path}:{line_number}: selected row has invalid timestamp_source"
                )
            if previous_timestamp is not None and timestamp_value < previous_timestamp:
                raise CorrelateError(
                    "selected reference rows are not chronological"
                )
            raw_sequence = row.get("raw_line_sequence")
            if (
                not isinstance(raw_sequence, int)
                or isinstance(raw_sequence, bool)
                or raw_sequence < 0
            ):
                raise CorrelateError(
                    f"{path}:{line_number}: selected row has invalid "
                    "raw_line_sequence"
                )
            if (
                previous_raw_sequence is not None
                and raw_sequence <= previous_raw_sequence
            ):
                raise CorrelateError(
                    "selected reference raw_line_sequence values are not "
                    "strictly increasing"
                )
            can_id_text = row.get("can_id")
            if not isinstance(can_id_text, str) or not re.fullmatch(
                r"[0-9A-Fa-f]{1,8}", can_id_text
            ):
                raise CorrelateError(
                    f"{path}:{line_number}: selected row has invalid can_id"
                )
            expected_can_id = int(can_id_text, 16)
            if expected_can_id > 0x1FFFFFFF:
                raise CorrelateError(
                    f"{path}:{line_number}: selected row can_id exceeds 29 bits"
                )
            if expected_can_id != CLUSTER_MODULE.rxid:
                raise CorrelateError(
                    f"{path}:{line_number}: selected DID row is not from the "
                    f"registered cluster RX endpoint "
                    f"0x{CLUSTER_MODULE.rxid:08X}"
                )
            can_data_text = row.get("can_data_hex")
            if not isinstance(can_data_text, str):
                raise CorrelateError(
                    f"{path}:{line_number}: selected row lacks can_data_hex"
                )
            try:
                expected_can_data = bytes.fromhex(can_data_text)
            except ValueError as exc:
                raise CorrelateError(
                    f"{path}:{line_number}: selected row has invalid can_data_hex"
                ) from exc
            if not expected_can_data or len(expected_can_data) > 8:
                raise CorrelateError(
                    f"{path}:{line_number}: selected row has invalid classic-CAN "
                    "data length"
                )
            declared = expected_can_data[0] & 0x0F
            if (
                expected_can_data[0] & 0xF0
                or declared != len(payload)
                or declared > len(expected_can_data) - 1
                or expected_can_data[1 : 1 + declared] != payload
            ):
                raise CorrelateError(
                    f"{path}:{line_number}: can_data_hex does not contain the "
                    "exact positive ISO-TP payload"
                )
            if selected_count >= MAX_REFERENCE_SAMPLES:
                raise CorrelateError("reference sample safety cap exceeded")
            selected_count += 1
            previous_timestamp = timestamp_value
            previous_raw_sequence = raw_sequence
            value = float(decoder.decode(payload[3:]))
            stats.add_timestamp(timestamp_value)
            yield ReferenceSample(
                timestamp_value,
                value,
                raw_line_sequence=raw_sequence,
                expected_can_id=expected_can_id,
                expected_can_data=expected_can_data,
            )


def _open_capture(
    path: Path, decompressor: Decompressor
) -> ContextManager[BinaryIO]:
    if path.name.endswith(".zst"):
        return decompressor.open(path)
    return path.open("rb")


def iter_candump_frames(
    paths: Sequence[Path],
    *,
    stats: list[StreamStats],
    decompressor: Decompressor,
) -> Iterator[CanFrame]:
    previous_timestamp: int | None = None
    raw_line_sequence = 0
    total_decompressed_bytes = 0
    for path, source_stats in zip(paths, stats):
        try:
            context = _open_capture(path, decompressor)
            with context as handle:
                for line_number, line in enumerate(
                    _bounded_lines(
                        handle, maximum_line_bytes=MAX_CANDUMP_LINE_BYTES
                    ),
                    1,
                ):
                    source_stats.add_raw_line(line)
                    total_decompressed_bytes += len(line)
                    if (
                        total_decompressed_bytes
                        > MAX_CAPTURE_DECOMPRESSED_BYTES
                    ):
                        raise CorrelateError(
                            "capture decompressed-byte safety cap exceeded"
                        )
                    if not line.strip():
                        source_stats.blank_lines += 1
                        continue
                    try:
                        frame = parse_candump_frame(line)
                    except CorrelateError as exc:
                        raise CorrelateError(
                            f"{path}:{line_number}: {exc}"
                        ) from exc
                    assert frame is not None
                    if (
                        previous_timestamp is not None
                        and frame.timestamp_us < previous_timestamp
                    ):
                        raise CorrelateError(
                            "candump inputs are not chronological in the supplied order"
                        )
                    previous_timestamp = frame.timestamp_us
                    source_stats.add_timestamp(frame.timestamp_us)
                    if raw_line_sequence >= MAX_CAPTURE_FRAMES:
                        raise CorrelateError("capture frame safety cap exceeded")
                    yield CanFrame(
                        frame.timestamp_us,
                        frame.can_id,
                        frame.id_bits,
                        frame.payload,
                        raw_line_sequence=raw_line_sequence,
                    )
                    raw_line_sequence += 1
        except CorrelateError:
            raise
        except OSError as exc:
            raise CorrelateError(f"cannot read candump stream {path}: {exc}") from exc


def iter_payload_fields(payload: bytes) -> Iterator[tuple[FieldSpec, int]]:
    for offset, value in enumerate(payload):
        yield FieldSpec("byte", offset), value
    for offset in range(max(0, len(payload) - 1)):
        pair = payload[offset : offset + 2]
        yield FieldSpec("u16be", offset), int.from_bytes(pair, "big")
        yield FieldSpec("u16le", offset), int.from_bytes(pair, "little")
    # Keep 32-bit candidates word-aligned to avoid five overlapping
    # interpretations of every eight-byte classic-CAN payload.
    for offset in range(0, max(0, len(payload) - 3), 4):
        word = payload[offset : offset + 4]
        yield FieldSpec("u32be", offset), int.from_bytes(word, "big")
        yield FieldSpec("u32le", offset), int.from_bytes(word, "little")


def is_diagnostic_id(can_id: int, id_bits: int) -> bool:
    """Conservative default diagnostic ranges for candidate exclusion."""
    if id_bits == 11:
        return can_id == 0x7DF or 0x7E0 <= can_id <= 0x7EF
    prefix = can_id & 0x1FFF0000
    return prefix in (0x18DA0000, 0x18DB0000)


@dataclass
class OnlineRegression:
    count: int = 0
    mean_x: float = 0.0
    mean_y: float = 0.0
    m2_x: float = 0.0
    m2_y: float = 0.0
    covariance: float = 0.0
    first_reference_timestamp_us: int | None = None
    last_reference_timestamp_us: int | None = None
    contributing_frames: int = 0
    contributing_abs_delta_us: int = 0
    maximum_abs_delta_us: int = 0
    minimum_frames_per_sample: int | None = None
    maximum_frames_per_sample: int = 0
    minimum_x: float = math.inf
    maximum_x: float = -math.inf
    minimum_y: float = math.inf
    maximum_y: float = -math.inf
    distinct_x_values: list[float] = field(default_factory=list, repr=False)
    distinct_y_values: list[float] = field(default_factory=list, repr=False)
    distinct_x_saturated: bool = False
    distinct_y_saturated: bool = False

    @staticmethod
    def _track_distinct(
        value: float, values: list[float], saturated: bool
    ) -> bool:
        if saturated or value in values:
            return saturated
        if len(values) < MAX_TRACKED_DISTINCT_VALUES:
            values.append(value)
            return False
        return True

    def add(
        self,
        x: float,
        y: float,
        *,
        reference_timestamp_us: int,
        contributing_frames: int,
        contributing_abs_delta_us: int,
        maximum_abs_delta_us: int,
    ) -> None:
        if not math.isfinite(x) or not math.isfinite(y):
            raise CorrelateError("non-finite value reached regression")
        self.count += 1
        delta_x = x - self.mean_x
        delta_y = y - self.mean_y
        self.mean_x += delta_x / self.count
        self.mean_y += delta_y / self.count
        self.m2_x += delta_x * (x - self.mean_x)
        self.m2_y += delta_y * (y - self.mean_y)
        self.covariance += delta_x * (y - self.mean_y)
        self.minimum_x = min(self.minimum_x, x)
        self.maximum_x = max(self.maximum_x, x)
        self.minimum_y = min(self.minimum_y, y)
        self.maximum_y = max(self.maximum_y, y)
        self.distinct_x_saturated = self._track_distinct(
            x, self.distinct_x_values, self.distinct_x_saturated
        )
        self.distinct_y_saturated = self._track_distinct(
            y, self.distinct_y_values, self.distinct_y_saturated
        )
        if self.first_reference_timestamp_us is None:
            self.first_reference_timestamp_us = reference_timestamp_us
        self.last_reference_timestamp_us = reference_timestamp_us
        self.contributing_frames += contributing_frames
        self.contributing_abs_delta_us += contributing_abs_delta_us
        self.maximum_abs_delta_us = max(
            self.maximum_abs_delta_us, maximum_abs_delta_us
        )
        if self.minimum_frames_per_sample is None:
            self.minimum_frames_per_sample = contributing_frames
        else:
            self.minimum_frames_per_sample = min(
                self.minimum_frames_per_sample, contributing_frames
            )
        self.maximum_frames_per_sample = max(
            self.maximum_frames_per_sample, contributing_frames
        )

    def result(
        self, *, reference_count: int, match_mode: str
    ) -> dict[str, object] | None:
        if self.count < 2 or self.m2_x <= 0.0 or self.m2_y <= 0.0:
            return None
        slope = self.covariance / self.m2_x
        intercept = self.mean_y - slope * self.mean_x
        denominator = self.m2_x * self.m2_y
        pearson = self.covariance / math.sqrt(denominator)
        pearson = max(-1.0, min(1.0, pearson))
        r_squared = max(0.0, min(1.0, pearson * pearson))
        residual_sum_squares = max(
            0.0,
            self.m2_y - (self.covariance * self.covariance / self.m2_x),
        )
        rmse = math.sqrt(residual_sum_squares / self.count)
        coverage_ratio = self.count / reference_count
        return {
            "sample_count": self.count,
            "coverage_ratio": coverage_ratio,
            "fit_coverage_score": r_squared * coverage_ratio,
            "variation": {
                "candidate": {
                    "minimum": self.minimum_x,
                    "maximum": self.maximum_x,
                    "tracked_distinct_count": len(self.distinct_x_values),
                    "distinct_count_is_lower_bound": self.distinct_x_saturated,
                },
                "matched_reference": {
                    "minimum": self.minimum_y,
                    "maximum": self.maximum_y,
                    "tracked_distinct_count": len(self.distinct_y_values),
                    "distinct_count_is_lower_bound": self.distinct_y_saturated,
                },
                "distinct_tracking_cap": MAX_TRACKED_DISTINCT_VALUES,
            },
            "reference_timestamp_coverage": {
                "first_epoch_us": self.first_reference_timestamp_us,
                "last_epoch_us": self.last_reference_timestamp_us,
            },
            "affine_model": {
                "equation": "reference_raw = scale * candidate_raw + intercept",
                "scale": slope,
                "intercept": intercept,
                "rmse_reference_raw": rmse,
            },
            "correlation": {
                "pearson_r": pearson,
                "r_squared": r_squared,
            },
            "timing": {
                "match_mode": match_mode,
                "contributing_frame_count": self.contributing_frames,
                "mean_contributing_frames_per_sample": (
                    self.contributing_frames / self.count
                ),
                "minimum_contributing_frames_per_sample": (
                    self.minimum_frames_per_sample
                ),
                "maximum_contributing_frames_per_sample": (
                    self.maximum_frames_per_sample
                ),
                "mean_contributing_abs_delta_ms": (
                    self.contributing_abs_delta_us
                    / self.contributing_frames
                    / 1000.0
                ),
                "maximum_contributing_abs_delta_ms": (
                    self.maximum_abs_delta_us / 1000.0
                ),
            },
        }


@dataclass
class NearestMatch:
    reference_timestamp_us: int
    timestamp_us: int
    payload: bytes

    def add(self, frame: CanFrame) -> None:
        old_key = (
            abs(self.timestamp_us - self.reference_timestamp_us),
            self.timestamp_us,
        )
        new_key = (
            abs(frame.timestamp_us - self.reference_timestamp_us),
            frame.timestamp_us,
        )
        if new_key < old_key:
            self.timestamp_us = frame.timestamp_us
            self.payload = frame.payload


@dataclass
class WindowField:
    count: int = 0
    total: float = 0.0
    minimum: float = math.inf
    maximum: float = -math.inf
    abs_delta_us: int = 0
    maximum_abs_delta_us: int = 0

    def add(self, value: int, abs_delta_us: int) -> None:
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        self.abs_delta_us += abs_delta_us
        self.maximum_abs_delta_us = max(self.maximum_abs_delta_us, abs_delta_us)

    def value(self, statistic: str) -> float:
        if statistic == "mean":
            return self.total / self.count
        if statistic == "min":
            return self.minimum
        if statistic == "max":
            return self.maximum
        raise CorrelateError(f"unsupported window statistic {statistic!r}")


@dataclass
class WindowMatch:
    reference_timestamp_us: int
    fields: dict[FieldSpec, WindowField] = field(default_factory=dict)

    def add(self, frame: CanFrame) -> int:
        created = 0
        delta = abs(frame.timestamp_us - self.reference_timestamp_us)
        for field_spec, value in iter_payload_fields(frame.payload):
            accumulator = self.fields.get(field_spec)
            if accumulator is None:
                accumulator = WindowField()
                self.fields[field_spec] = accumulator
                created += 1
            accumulator.add(value, delta)
        return created


MatchState = NearestMatch | WindowMatch


@dataclass
class ActiveReference:
    sample: ReferenceSample
    matches: dict[tuple[int, int], MatchState] = field(default_factory=dict)


@dataclass
class AnalysisConfig:
    match_mode: str = "nearest"
    radius_us: int = 100_000
    include_extended: bool = False
    include_diagnostic_ids: bool = False
    minimum_samples: int = 20
    minimum_coverage_ratio: float = 0.5
    minimum_distinct_values: int = 4
    top_count: int = 100

    @property
    def window_statistic(self) -> str | None:
        if self.match_mode.startswith("window-"):
            return self.match_mode.split("-", 1)[1]
        return None

    def validate(self) -> None:
        if self.match_mode not in {
            "nearest",
            "window-mean",
            "window-min",
            "window-max",
        }:
            raise CorrelateError(f"unsupported match mode {self.match_mode!r}")
        if not 1 <= self.radius_us <= MAX_RADIUS_MS * 1000:
            raise CorrelateError(
                f"radius must be between 0.001 and {MAX_RADIUS_MS} ms"
            )
        if self.minimum_samples < 2:
            raise CorrelateError("minimum samples must be at least 2")
        if (
            not math.isfinite(self.minimum_coverage_ratio)
            or not 0.0 < self.minimum_coverage_ratio <= 1.0
        ):
            raise CorrelateError(
                "minimum coverage ratio must be finite and in (0, 1]"
            )
        if not 2 <= self.minimum_distinct_values <= MAX_TRACKED_DISTINCT_VALUES:
            raise CorrelateError(
                "minimum distinct values must be between 2 and "
                f"{MAX_TRACKED_DISTINCT_VALUES}"
            )
        if self.top_count <= 0:
            raise CorrelateError("top count must be positive")
        if self.top_count > MAX_TOP_COUNT:
            raise CorrelateError(
                f"top count exceeds the {MAX_TOP_COUNT}-candidate safety cap"
            )


class StreamingCorrelator:
    def __init__(self, config: AnalysisConfig) -> None:
        config.validate()
        self.config = config
        self.active: deque[ActiveReference] = deque()
        self.histories: dict[tuple[int, int], deque[CanFrame]] = {}
        self.regressions: dict[CandidateKey, OnlineRegression] = {}
        self.reference_count = 0
        self.reference_first_us: int | None = None
        self.reference_last_us: int | None = None
        self.total_capture_frames = 0
        self.eligible_capture_frames = 0
        self.excluded_extended_frames = 0
        self.excluded_diagnostic_frames = 0
        self.empty_payload_frames = 0
        self.active_match_states = 0
        self.active_window_fields = 0
        self.history_frames = 0
        self.pending_wire_links: dict[int, ReferenceSample] = {}
        self.linked_reference_count = 0
        self.verified_reference_links = 0

    def _eligible(self, frame: CanFrame) -> bool:
        if frame.id_bits == 29 and not self.config.include_extended:
            self.excluded_extended_frames += 1
            return False
        if (
            is_diagnostic_id(frame.can_id, frame.id_bits)
            and not self.config.include_diagnostic_ids
        ):
            self.excluded_diagnostic_frames += 1
            return False
        if not frame.payload:
            self.empty_payload_frames += 1
            return False
        return True

    def _trim_history(
        self, history: deque[CanFrame], current_timestamp_us: int
    ) -> None:
        cutoff = current_timestamp_us - self.config.radius_us
        while history and history[0].timestamp_us < cutoff:
            history.popleft()
            self.history_frames -= 1

    def _advance(self, timestamp_us: int) -> None:
        while (
            self.active
            and self.active[0].sample.timestamp_us + self.config.radius_us
            < timestamp_us
        ):
            self._finalize_reference(self.active.popleft())

    def _new_match(
        self, active: ActiveReference, frame: CanFrame
    ) -> MatchState:
        key = (frame.can_id, frame.id_bits)
        match = active.matches.get(key)
        if match is None:
            if self.config.match_mode == "nearest":
                match = NearestMatch(
                    active.sample.timestamp_us, frame.timestamp_us, frame.payload
                )
            else:
                match = WindowMatch(active.sample.timestamp_us)
            active.matches[key] = match
            self.active_match_states += 1
            if self.active_match_states > MAX_ACTIVE_MATCH_STATES:
                raise CorrelateError(
                    "active candidate/reference match-state safety cap exceeded"
                )
        return match

    def _add_frame_to_active(
        self, active: ActiveReference, frame: CanFrame
    ) -> None:
        if (
            abs(frame.timestamp_us - active.sample.timestamp_us)
            > self.config.radius_us
        ):
            return
        match = self._new_match(active, frame)
        if isinstance(match, WindowMatch):
            self.active_window_fields += match.add(frame)
            if self.active_window_fields > MAX_ACTIVE_WINDOW_FIELDS:
                raise CorrelateError(
                    "active window-field accumulator safety cap exceeded"
                )
        else:
            match.add(frame)

    def add_frame(self, frame: CanFrame) -> None:
        expected_sequence = self.total_capture_frames
        if (
            frame.raw_line_sequence is not None
            and frame.raw_line_sequence != expected_sequence
        ):
            raise CorrelateError(
                "capture raw-frame sequence is not contiguous from zero"
            )
        if self.pending_wire_links:
            first_link_sequence = min(self.pending_wire_links)
            if first_link_sequence < expected_sequence:
                raise CorrelateError(
                    "selected cluster-wire response is missing from the supplied "
                    "candump sequence"
                )
            if first_link_sequence == expected_sequence:
                if frame.raw_line_sequence is None:
                    raise CorrelateError(
                        "linked references require globally sequenced candump frames"
                    )
                reference = self.pending_wire_links.pop(expected_sequence)
                assert reference.expected_can_id is not None
                assert reference.expected_can_data is not None
                if (
                    frame.timestamp_us != reference.timestamp_us
                    or frame.can_id != reference.expected_can_id
                    or frame.payload != reference.expected_can_data
                ):
                    raise CorrelateError(
                        "selected cluster-wire response does not match the exact "
                        "global candump frame sequence/timestamp/ID/payload"
                    )
                self.verified_reference_links += 1
        if self.total_capture_frames >= MAX_CAPTURE_FRAMES:
            raise CorrelateError("capture frame safety cap exceeded")
        self.total_capture_frames += 1
        self._advance(frame.timestamp_us)
        if not self._eligible(frame):
            return
        self.eligible_capture_frames += 1
        key = (frame.can_id, frame.id_bits)
        history = self.histories.get(key)
        if history is None:
            if len(self.histories) >= MAX_CANDIDATE_IDS:
                raise CorrelateError("candidate identifier safety cap exceeded")
            history = deque()
            self.histories[key] = history
        self._trim_history(history, frame.timestamp_us)
        history.append(frame)
        self.history_frames += 1
        if self.history_frames > MAX_HISTORY_FRAMES:
            raise CorrelateError("time-window frame-history safety cap exceeded")
        for active in self.active:
            self._add_frame_to_active(active, frame)

    def add_reference(self, sample: ReferenceSample) -> None:
        self._advance(sample.timestamp_us)
        if self.reference_count >= MAX_REFERENCE_SAMPLES:
            raise CorrelateError("reference sample safety cap exceeded")
        if sample.has_wire_link:
            assert sample.raw_line_sequence is not None
            if sample.raw_line_sequence < self.total_capture_frames:
                raise CorrelateError(
                    "selected cluster-wire response sequence was already passed "
                    "in the supplied candump"
                )
            if sample.raw_line_sequence in self.pending_wire_links:
                raise CorrelateError(
                    "duplicate selected cluster-wire raw frame sequence"
                )
            if len(self.pending_wire_links) >= MAX_PENDING_WIRE_LINKS:
                raise CorrelateError(
                    "pending cluster-wire linkage safety cap exceeded"
                )
            self.pending_wire_links[sample.raw_line_sequence] = sample
            self.linked_reference_count += 1
        self.reference_count += 1
        if self.reference_first_us is None:
            self.reference_first_us = sample.timestamp_us
        self.reference_last_us = sample.timestamp_us
        active = ActiveReference(sample)
        for history in self.histories.values():
            self._trim_history(history, sample.timestamp_us)
            for frame in history:
                self._add_frame_to_active(active, frame)
        self.active.append(active)
        if len(self.active) > MAX_ACTIVE_REFERENCES:
            raise CorrelateError("active reference-window safety cap exceeded")

    def _regression(self, key: CandidateKey) -> OnlineRegression:
        regression = self.regressions.get(key)
        if regression is None:
            if len(self.regressions) >= MAX_CANDIDATE_FIELDS:
                raise CorrelateError("candidate field safety cap exceeded")
            regression = OnlineRegression()
            self.regressions[key] = regression
        return regression

    def _finalize_reference(self, active: ActiveReference) -> None:
        self.active_match_states -= len(active.matches)
        for (can_id, id_bits), match in active.matches.items():
            if isinstance(match, NearestMatch):
                delta = abs(
                    match.timestamp_us - active.sample.timestamp_us
                )
                for field_spec, value in iter_payload_fields(match.payload):
                    self._regression(
                        CandidateKey(can_id, id_bits, field_spec)
                    ).add(
                        float(value),
                        active.sample.value,
                        reference_timestamp_us=active.sample.timestamp_us,
                        contributing_frames=1,
                        contributing_abs_delta_us=delta,
                        maximum_abs_delta_us=delta,
                    )
            else:
                self.active_window_fields -= len(match.fields)
                statistic = self.config.window_statistic
                assert statistic is not None
                for field_spec, accumulator in match.fields.items():
                    self._regression(
                        CandidateKey(can_id, id_bits, field_spec)
                    ).add(
                        accumulator.value(statistic),
                        active.sample.value,
                        reference_timestamp_us=active.sample.timestamp_us,
                        contributing_frames=accumulator.count,
                        contributing_abs_delta_us=accumulator.abs_delta_us,
                        maximum_abs_delta_us=accumulator.maximum_abs_delta_us,
                    )

    def finish(self) -> None:
        if self.pending_wire_links:
            raise CorrelateError(
                "selected cluster-wire response is missing from the supplied "
                "candump sequence"
            )
        while self.active:
            self._finalize_reference(self.active.popleft())
        if self.active_match_states != 0:
            raise CorrelateError("internal active match-state accounting mismatch")
        if self.active_window_fields != 0:
            raise CorrelateError("internal window-field accounting mismatch")

    def candidate_rows(self) -> tuple[list[dict[str, object]], dict[str, int]]:
        rows: list[dict[str, object]] = []
        rejection_counts = {
            "below_minimum_samples": 0,
            "below_minimum_coverage": 0,
            "below_minimum_distinct_values": 0,
            "constant_candidate_or_reference": 0,
        }
        for key, regression in self.regressions.items():
            if regression.count < self.config.minimum_samples:
                rejection_counts["below_minimum_samples"] += 1
                continue
            if (
                regression.count / self.reference_count
                < self.config.minimum_coverage_ratio
            ):
                rejection_counts["below_minimum_coverage"] += 1
                continue
            if (
                len(regression.distinct_x_values)
                < self.config.minimum_distinct_values
            ):
                rejection_counts["below_minimum_distinct_values"] += 1
                continue
            result = regression.result(
                reference_count=self.reference_count,
                match_mode=self.config.match_mode,
            )
            if result is None:
                rejection_counts["constant_candidate_or_reference"] += 1
                continue
            row = {
                "classification": "candidate_only",
                "candidate_only": True,
                "physical_identity_verified": False,
                "scale_verified": False,
                "telemetry_promotion_allowed": False,
                "can_id": key.can_id,
                "can_id_hex": (
                    f"{key.can_id:08X}"
                    if key.id_bits == 29
                    else f"{key.can_id:03X}"
                ),
                "id_bits": key.id_bits,
                "field": key.field.as_dict(),
                **result,
            }
            rows.append(row)
        rows.sort(
            key=lambda row: (
                -float(row["fit_coverage_score"]),
                -float(row["correlation"]["r_squared"]),
                -float(row["coverage_ratio"]),
                -int(row["sample_count"]),
                int(row["can_id"]),
                str(row["field"]["kind"]),
                int(row["field"]["offset"]),
            )
        )
        rejection_counts["eligible_but_omitted_by_top"] = max(
            0, len(rows) - self.config.top_count
        )
        rows = rows[: self.config.top_count]
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
        return rows, rejection_counts


def analyze_streams(
    reference_samples: Iterable[ReferenceSample],
    frames: Iterable[CanFrame],
    *,
    config: AnalysisConfig,
) -> StreamingCorrelator:
    correlator = StreamingCorrelator(config)
    reference_iterator = iter(reference_samples)
    frame_iterator = iter(frames)
    reference = next(reference_iterator, None)
    if reference is None:
        raise CorrelateError("reference stream has no exact positive rows for the DID")
    frame = next(frame_iterator, None)
    while reference is not None or frame is not None:
        if reference is not None and (
            frame is None or reference.timestamp_us <= frame.timestamp_us
        ):
            assert reference is not None
            correlator.add_reference(reference)
            reference = next(reference_iterator, None)
        else:
            assert frame is not None
            correlator.add_frame(frame)
            frame = next(frame_iterator, None)
    correlator.finish()
    if correlator.total_capture_frames == 0:
        raise CorrelateError("candump inputs contain no frames")
    if correlator.eligible_capture_frames == 0:
        raise CorrelateError("candump inputs contain no eligible candidate frames")
    return correlator


def _validate_inputs(wire: Path, captures: Sequence[Path]) -> None:
    if not captures:
        raise CorrelateError("at least one candump input is required")
    if len(captures) > MAX_CAPTURE_FILES:
        raise CorrelateError(
            f"capture file count exceeds the {MAX_CAPTURE_FILES}-file safety cap"
        )
    paths = [wire, *captures]
    resolved: set[Path] = set()
    for path in paths:
        if path.name.endswith(".partial"):
            raise CorrelateError(f"partial evidence is not accepted: {path}")
        try:
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise CorrelateError(f"input is unavailable: {path}") from exc
        if not canonical.is_file():
            raise CorrelateError(f"input is not a regular file: {path}")
        if canonical in resolved:
            raise CorrelateError(f"duplicate input path: {path}")
        resolved.add(canonical)
    if wire.name != "cluster_wire.jsonl":
        raise CorrelateError(
            "reference input must have the finalized recorder basename "
            "cluster_wire.jsonl"
        )
    for capture in captures:
        if not (
            capture.name.endswith(".candump")
            or capture.name.endswith(".log")
            or capture.name.endswith(".txt")
            or capture.name.endswith(".zst")
        ):
            raise CorrelateError(
                f"unsupported candump filename (expected text or .zst): {capture}"
            )


def _validated_output_path(path: Path) -> Path:
    root = TMP_ROOT.resolve(strict=False)
    resolved = path.resolve(strict=False)
    try:
        inside = os.path.commonpath((str(root), str(resolved))) == str(root)
    except ValueError:
        inside = False
    if not inside or resolved == root:
        raise CorrelateError(f"output must be an explicit file below {TMP_ROOT}")
    if path.suffix.lower() != ".json":
        raise CorrelateError("output filename must end in .json")
    if path.exists() or path.is_symlink():
        raise CorrelateError(f"refusing to overwrite existing output: {path}")
    return resolved


def _exclusive_write_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after mkdir in case an existing parent component was a symlink.
    checked = _validated_output_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    created = False
    try:
        fd = os.open(checked, flags, 0o600)
        created = True
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise CorrelateError(f"refusing to overwrite existing output: {path}") from exc
    except BaseException:
        if fd is not None:
            os.close(fd)
        if created:
            try:
                checked.unlink()
            except OSError:
                pass
        raise


def run_analysis(
    *,
    wire: Path,
    captures: Sequence[Path],
    did: int,
    reference_field: str,
    config: AnalysisConfig,
    decompressor: Decompressor | None = None,
) -> dict[str, object]:
    _validate_inputs(wire, captures)
    decoder = ReferenceDecoder(reference_field)
    reference_stats = StreamStats(str(wire), "none")
    capture_stats = [
        StreamStats(
            str(path),
            "zstd" if path.name.endswith(".zst") else "none",
        )
        for path in captures
    ]
    if decompressor is None:
        decompressor = CliZstdDecompressor()
    correlator = analyze_streams(
        iter_reference_samples(
            wire, did=did, decoder=decoder, stats=reference_stats
        ),
        iter_candump_frames(
            captures, stats=capture_stats, decompressor=decompressor
        ),
        config=config,
    )
    if (
        correlator.linked_reference_count != correlator.reference_count
        or correlator.verified_reference_links != correlator.reference_count
    ):
        raise CorrelateError(
            "not every selected reference was verified against the global "
            "candump frame sequence"
        )
    candidate_rows, rejected = correlator.candidate_rows()
    assert decoder.resolved is not None
    return {
        "schema_version": SCHEMA_VERSION,
        "classification": "candidate_only",
        "candidate_only": True,
        "physical_identity_verified": False,
        "scale_verified": False,
        "telemetry_promotion_allowed": False,
        "candidate_only_reason": (
            "time correlation and affine fit do not prove signal identity, units, "
            "byte semantics, or physical scaling"
        ),
        "offline_only": True,
        "affine_orientation": (
            "reference DID raw value = scale * candidate broadcast raw value "
            "+ intercept"
        ),
        "analysis": {
            "match_mode": config.match_mode,
            "window_statistic": config.window_statistic,
            "radius_ms": config.radius_us / 1000.0,
            "minimum_samples": config.minimum_samples,
            "minimum_coverage_ratio": config.minimum_coverage_ratio,
            "minimum_distinct_values": config.minimum_distinct_values,
            "top_count": config.top_count,
            "candidate_filter": {
                "include_extended": config.include_extended,
                "include_diagnostic_ids": config.include_diagnostic_ids,
                "default_standard_diagnostic_ids": [
                    "0x7DF",
                    "0x7E0-0x7EF",
                ],
                "default_extended_diagnostic_prefixes": [
                    "0x18DAxxxx",
                    "0x18DBxxxx",
                ],
            },
            "hard_memory_state_caps": {
                "active_references": MAX_ACTIVE_REFERENCES,
                "pending_wire_links": MAX_PENDING_WIRE_LINKS,
                "candidate_ids": MAX_CANDIDATE_IDS,
                "history_frames": MAX_HISTORY_FRAMES,
                "active_match_states": MAX_ACTIVE_MATCH_STATES,
                "active_window_fields": MAX_ACTIVE_WINDOW_FIELDS,
                "candidate_fields": MAX_CANDIDATE_FIELDS,
                "reported_candidates": MAX_TOP_COUNT,
                "total_reference_samples": MAX_REFERENCE_SAMPLES,
                "wire_stream_lines": MAX_WIRE_STREAM_LINES,
                "wire_stream_bytes": MAX_WIRE_STREAM_BYTES,
                "capture_files": MAX_CAPTURE_FILES,
                "total_capture_frames": MAX_CAPTURE_FRAMES,
                "capture_decompressed_bytes": (
                    MAX_CAPTURE_DECOMPRESSED_BYTES
                ),
            },
        },
        "reference": {
            "module": {
                "key": CLUSTER_MODULE.key,
                "name": CLUSTER_MODULE.name,
                "bus": CLUSTER_MODULE.bus,
                "channel": CLUSTER_MODULE.channel,
                "txid_hex": f"{CLUSTER_MODULE.txid:08X}",
                "rxid_hex": f"{CLUSTER_MODULE.rxid:08X}",
                "addressing_mode": CLUSTER_MODULE.addressing_mode,
            },
            "did": f"{did:04X}",
            "requested_field": reference_field,
            "resolved_field": decoder.resolved.as_dict(),
            "sample_count": correlator.reference_count,
            "global_candump_linkage": {
                "required": True,
                "linked_sample_count": correlator.linked_reference_count,
                "verified_sample_count": correlator.verified_reference_links,
                "verification_fields": [
                    "raw_line_sequence",
                    "timestamp_epoch_us",
                    "can_id",
                    "can_data_hex",
                ],
            },
            "timestamp_coverage": {
                "first_epoch_us": correlator.reference_first_us,
                "last_epoch_us": correlator.reference_last_us,
            },
            "source": reference_stats.as_dict(),
        },
        "capture": {
            "provenance_limits": {
                "manifest_validated": False,
                "loss_accounting_validated": False,
                "campaign_summary_validated": False,
                "warning": (
                    "This report exact-links selected DID rows to raw frames but "
                    "does not independently validate manifest chunk accounting, "
                    "socket drops, or clean campaign finalization; review the "
                    "completed campaign summary separately."
                ),
            },
            "sources": [item.as_dict() for item in capture_stats],
            "decompressed_bytes_read": sum(
                item.bytes_read for item in capture_stats
            ),
            "total_frames": correlator.total_capture_frames,
            "eligible_candidate_frames": correlator.eligible_capture_frames,
            "excluded_extended_frames": correlator.excluded_extended_frames,
            "excluded_diagnostic_frames": correlator.excluded_diagnostic_frames,
            "empty_payload_frames": correlator.empty_payload_frames,
            "candidate_identifier_count": len(correlator.histories),
            "candidate_field_state_count": len(correlator.regressions),
        },
        "ranking": {
            "reported_candidate_count": len(candidate_rows),
            "unreported_or_rejected_field_counts": rejected,
            "order": [
                "r_squared multiplied by coverage_ratio descending",
                "r_squared descending",
                "coverage_ratio descending",
                "sample_count descending",
                "CAN ID and field deterministic tie-break",
            ],
            "candidates": candidate_rows,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline-only: correlate exact cluster DID responses with saved "
            "candump broadcast fields."
        )
    )
    parser.add_argument(
        "--wire",
        required=True,
        type=Path,
        help="completed cluster_wire.jsonl reference stream",
    )
    parser.add_argument(
        "--did",
        required=True,
        type=_parse_hex_did,
        help="reference DID in hexadecimal (for example 1000)",
    )
    parser.add_argument(
        "--reference-field",
        default="auto",
        help=(
            "auto, byte:N, u16be:N, u16le:N, u32be:N, or u32le:N "
            "within DID data bytes"
        ),
    )
    parser.add_argument(
        "--match",
        choices=("nearest", "window-mean", "window-min", "window-max"),
        default="nearest",
        help="candidate sampling rule around each DID response",
    )
    parser.add_argument(
        "--radius-ms",
        type=float,
        default=100.0,
        help=f"symmetric match radius, >0 and <= {MAX_RADIUS_MS} (default 100)",
    )
    parser.add_argument(
        "--minimum-samples",
        type=int,
        default=20,
        help="minimum matched reference samples for a ranked field",
    )
    parser.add_argument(
        "--minimum-coverage",
        type=float,
        default=0.5,
        help="minimum matched fraction of all references in (0, 1] (default 0.5)",
    )
    parser.add_argument(
        "--minimum-distinct",
        type=int,
        default=4,
        help=(
            "minimum distinct candidate values, 2 through "
            f"{MAX_TRACKED_DISTINCT_VALUES} (default 4)"
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=100,
        help=(
            f"maximum ranked candidates to retain (hard cap {MAX_TOP_COUNT})"
        ),
    )
    parser.add_argument(
        "--include-extended",
        action="store_true",
        help="include 29-bit candidate frames (off by default)",
    )
    parser.add_argument(
        "--include-diagnostic-ids",
        action="store_true",
        help="include conservatively classified diagnostic identifiers",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="new JSON report path below repository tmp/; never overwritten",
    )
    parser.add_argument(
        "captures",
        nargs="+",
        type=Path,
        help="chronologically ordered candump text and/or .zst files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.radius_ms):
            raise CorrelateError("radius must be finite")
        radius_us_decimal = Decimal(str(args.radius_ms)) * Decimal(1000)
        if radius_us_decimal != radius_us_decimal.to_integral_value():
            raise CorrelateError("radius must resolve to whole microseconds")
        config = AnalysisConfig(
            match_mode=args.match,
            radius_us=int(radius_us_decimal),
            include_extended=args.include_extended,
            include_diagnostic_ids=args.include_diagnostic_ids,
            minimum_samples=args.minimum_samples,
            minimum_coverage_ratio=args.minimum_coverage,
            minimum_distinct_values=args.minimum_distinct,
            top_count=args.top,
        )
        config.validate()
        output = _validated_output_path(args.output)
        _validate_inputs(args.wire, args.captures)
        input_paths = {
            args.wire.resolve(strict=True),
            *(path.resolve(strict=True) for path in args.captures),
        }
        if output in input_paths:
            raise CorrelateError("output must not replace an input")
        report = run_analysis(
            wire=args.wire,
            captures=args.captures,
            did=args.did,
            reference_field=args.reference_field,
            config=config,
        )
        _exclusive_write_json(output, report)
    except CorrelateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        f"Wrote {report['ranking']['reported_candidate_count']} "
        f"candidate-only correlations to {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
