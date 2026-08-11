#!/usr/bin/env python3
"""Strictly offline, bounded-memory CAN time-series correlator.

The reference stream is a completed per-module ``*_wire.jsonl`` stream.  The
original producer is ``projects/ecu_mapping/cluster_drive_log.py``; generic
streams may also be extracted from a saved parallel diagnostic capture.  Only
rows classified as
``exact_positive_response`` for the explicitly selected DID become reference
samples.  Saved candump text (plain or ``.zst``) supplies candidate broadcast
fields.

By default, every classic-CAN payload contributes the established 39-field
DLC-8 coarse profile: bytes, overlapping unsigned 16-bit words, two
Stellantis packed fields, and aligned unsigned 32-bit words.  An explicit
``--bit-search-id`` replaces that profile for no more than two shortlisted
identifiers with bounded 1..32-bit DBC/cantools Intel and Motorola geometries.

A streaming chronological merge matches those fields to each exact
diagnostic-wire reference either by the nearest frame or by a mean/min/max
statistic in a symmetric time window. Candidate identity includes channel,
SFF/EFF namespace, CAN ID, and DLC. Online covariance keeps memory independent
of capture duration; explicit caps bound the remaining time-window,
identifier, match-state, field, reference, frame, and decompressed-byte state.

This module never opens CAN, imports ADB, inspects services, or accesses the
network.  Its only subprocess boundary is :class:`CliZstdDecompressor`, whose
fixed argv invokes ``zstd -dc -- <path>`` for a ``.zst`` input.  Plain inputs
use no subprocess.

Correlation establishes association and a candidate affine relationship, not
signal identity or physical scaling. Reports therefore remain mechanically
``candidate_only`` for promotion safety, while the orthogonal
``evidence_tier`` distinguishes exploratory discovery from a frozen-formula
operational-proxy evaluation.
"""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from functools import lru_cache
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

from lib.modules import MODULES, NORMAL_11BITS, Module
from lib.signal_fields import (
    BYTE_ORDERS,
    MAX_SIGNAL_BITS,
    SignalField,
    SignalFieldError,
    iter_signal_fields,
)


TMP_ROOT = REPO / "tmp"
DEFAULT_MODULE = MODULES["cluster"]
# Backward-compatible public alias used by existing callers and evidence tests.
CLUSTER_MODULE = DEFAULT_MODULE

SCHEMA_VERSION = 1
EXPLORATORY_EVIDENCE_TIER = "exploratory_candidate"
PROXY_EVALUATION_EVIDENCE_TIER = "operational_proxy_evaluation"
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
MAX_BIT_SEARCH_IDENTIFIERS = 2
MAX_BIT_SEARCH_FIELDS_PER_IDENTIFIER = 6_000
MAX_REGIME_CANDIDATE_STREAMS = 4
MAX_REGIME_REGRESSIONS = 50_000
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
    r"^(byte|[ui]16be|[ui]16le|[ui]32be|[ui]32le):([0-9]{1,4})$"
)
_BIT_REFERENCE_FIELD_RE = re.compile(
    r"^bits:(little|big):([0-9]{1,3}):([0-9]{1,2}):"
    r"(unsigned|signed)$"
)
_BIT_SEARCH_ID_RE = re.compile(
    r"^(sff|eff):([0-9A-Fa-f]{1,8}):([1-8])$"
)
_VAN_COMPUTE_JOB_ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")
_VAN_COMPUTE_WIRE_NAME_RE = re.compile(
    r"\d{3}-[a-z][a-z0-9_]{0,63}_wire\.jsonl"
)


class CorrelateError(RuntimeError):
    """Raised for malformed evidence or a violated offline-analysis gate."""


def _van_compute_job_root() -> Path:
    job_id = os.environ.get("VAN_COMPUTE_JOB_ID", "")
    if not _VAN_COMPUTE_JOB_ID_RE.fullmatch(job_id):
        raise CorrelateError(
            "van-compute operation requires a valid VAN_COMPUTE_JOB_ID"
        )
    try:
        source_root = REPO.resolve(strict=True)
    except OSError as exc:
        raise CorrelateError(
            "van-compute source directory is unavailable"
        ) from exc
    if not source_root.is_dir() or source_root.name != "source":
        raise CorrelateError(
            "van-compute operation requires a real staged source directory"
        )
    return source_root.parent


@dataclass(frozen=True)
class ReferenceSample:
    timestamp_us: int
    value: float
    raw_line_sequence: int | None = None
    expected_can_id: int | None = None
    expected_id_bits: int | None = None
    expected_channel: str | None = None
    expected_can_data: bytes | None = None

    @property
    def has_wire_link(self) -> bool:
        values = (
            self.raw_line_sequence,
            self.expected_can_id,
            self.expected_id_bits,
            self.expected_channel,
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
    channel: str = DEFAULT_MODULE.channel

    @property
    def dlc(self) -> int:
        return len(self.payload)


@dataclass(frozen=True, order=True)
class FieldSpec:
    kind: str
    offset: int
    geometry: SignalField | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.offset, int)
            or isinstance(self.offset, bool)
            or self.offset < 0
        ):
            raise CorrelateError("field offset must be a non-negative integer")
        if self.kind == "bitfield":
            if self.geometry is None:
                raise CorrelateError("bitfield specification requires geometry")
            if self.offset != self.geometry.first_payload_byte:
                raise CorrelateError(
                    "bitfield offset must equal its first payload byte"
                )
        elif self.geometry is not None:
            raise CorrelateError(
                "legacy field specification cannot carry bit geometry"
            )

    @classmethod
    def from_signal_field(cls, geometry: SignalField) -> "FieldSpec":
        return cls("bitfield", geometry.first_payload_byte, geometry)

    @property
    def width_bytes(self) -> int:
        if self.geometry is not None:
            return self.geometry.span_bytes
        if self.kind == "byte":
            return 1
        if self.kind == "u13be-low5":
            return 2
        if self.kind == "u17be-low1":
            return 3
        if self.kind.startswith(("u16", "i16")):
            return 2
        if self.kind.startswith(("u32", "i32")):
            return 4
        raise CorrelateError(f"unsupported field kind {self.kind!r}")

    @property
    def byte_order(self) -> str | None:
        if self.geometry is not None:
            return self.geometry.byte_order
        if self.kind in ("u13be-low5", "u17be-low1"):
            return "big"
        if self.kind in ("u16be", "i16be"):
            return "big"
        if self.kind in ("u16le", "i16le"):
            return "little"
        if self.kind in ("u32be", "i32be"):
            return "big"
        if self.kind in ("u32le", "i32le"):
            return "little"
        return None

    @property
    def signed(self) -> bool:
        if self.geometry is not None:
            return self.geometry.signed
        return self.kind.startswith("i")

    @property
    def label(self) -> str:
        if self.geometry is not None:
            return self.geometry.label
        return f"{self.kind}:{self.offset}"

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "offset": self.offset,
            "width_bytes": self.width_bytes,
            "byte_order": self.byte_order,
            "signed": self.signed,
        }
        if self.geometry is not None:
            result.update(
                {
                    "dbc_start_bit": self.geometry.dbc_start_bit,
                    "length_bits": self.geometry.length_bits,
                    "label": self.geometry.label,
                    "bit_numbering": "dbc_cantools_sawtooth",
                }
            )
        return result

    def sort_key(self) -> tuple[object, ...]:
        if self.geometry is None:
            return (0, self.kind, self.offset)
        return (
            1,
            self.kind,
            self.offset,
            self.geometry.dbc_start_bit,
            self.geometry.length_bits,
            self.geometry.byte_order,
            self.geometry.signed,
        )


@dataclass(frozen=True, order=True)
class CandidateKey:
    channel: str
    can_id: int
    id_bits: int
    dlc: int
    field: FieldSpec


@dataclass(frozen=True)
class StreamFieldSelector:
    """One exact passive stream plus one raw field."""

    can_id: int
    id_bits: int
    dlc: int
    field: FieldSpec
    channel: str = DEFAULT_MODULE.channel

    def __post_init__(self) -> None:
        if self.id_bits not in (11, 29):
            raise CorrelateError("selector id_bits must be 11 or 29")
        maximum = 0x7FF if self.id_bits == 11 else 0x1FFFFFFF
        if (
            type(self.can_id) is not int
            or not 0 <= self.can_id <= maximum
        ):
            raise CorrelateError("selector CAN ID is out of range")
        if type(self.dlc) is not int or not 1 <= self.dlc <= 8:
            raise CorrelateError("selector DLC must be between 1 and 8")
        if not isinstance(self.field, FieldSpec):
            raise CorrelateError("selector field must be a FieldSpec")
        if (
            _legacy_signal_field(self.field).required_payload_bytes
            > self.dlc
        ):
            raise CorrelateError(
                f"selector field {self.field.label} exceeds DLC {self.dlc}"
            )
        if not isinstance(self.channel, str) or not self.channel:
            raise CorrelateError("selector channel must be nonempty")

    @property
    def stream_key(self) -> tuple[str, int, int, int]:
        return (self.channel, self.can_id, self.id_bits, self.dlc)

    def as_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "can_id_hex": (
                f"{self.can_id:08X}"
                if self.id_bits == 29
                else f"{self.can_id:03X}"
            ),
            "id_bits": self.id_bits,
            "dlc": self.dlc,
            "field": self.field.as_dict(),
        }


@dataclass(frozen=True)
class FixedFormulaConfig:
    """One predeclared candidate-to-reference raw affine formula."""

    candidate: StreamFieldSelector
    scale: float
    intercept: float

    def validate(self) -> None:
        if not isinstance(self.candidate, StreamFieldSelector):
            raise CorrelateError(
                "fixed formula candidate must be a stream field selector"
            )
        if not math.isfinite(self.scale) or not math.isfinite(self.intercept):
            raise CorrelateError(
                "fixed formula scale and intercept must be finite"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.as_dict(),
            "equation": (
                "predicted_reference_raw = scale * candidate_raw + intercept"
            ),
            "scale": self.scale,
            "intercept": self.intercept,
        }


REGIME_NAMES = (
    "idle",
    "positive_pull",
    "steady_cruise",
    "lift_transition",
    "negative_overrun",
)


@dataclass(frozen=True)
class RegimeAnalysisConfig:
    """Explicit, scale-aware rules for opt-in torque-regime slicing."""

    speed: StreamFieldSelector
    rpm: StreamFieldSelector
    throttle: StreamFieldSelector
    candidate_streams: frozenset[tuple[int, int, int]]
    stopped_speed_max: float
    moving_speed_min: float
    idle_rpm_min: float
    pull_speed_rate_min: float
    pull_throttle_min: float
    steady_speed_rate_max: float
    steady_throttle_rate_max: float
    lift_throttle_rate_max: float
    overrun_speed_rate_max: float
    overrun_throttle_max: float
    minimum_samples: int = 5

    def validate(self) -> None:
        if (
            not isinstance(self.candidate_streams, frozenset)
            or not 1
            <= len(self.candidate_streams)
            <= MAX_REGIME_CANDIDATE_STREAMS
        ):
            raise CorrelateError(
                "regime candidate streams must be a frozenset containing "
                f"1..{MAX_REGIME_CANDIDATE_STREAMS} exact streams"
            )
        for item in self.candidate_streams:
            if (
                not isinstance(item, tuple)
                or len(item) != 3
                or type(item[0]) is not int
                or item[1] not in (11, 29)
                or type(item[2]) is not int
                or not 1 <= item[2] <= 8
            ):
                raise CorrelateError(
                    "regime candidate streams must be "
                    "(CAN ID, 11|29, DLC) tuples"
                )
            maximum = 0x7FF if item[1] == 11 else 0x1FFFFFFF
            if not 0 <= item[0] <= maximum:
                raise CorrelateError(
                    "regime candidate CAN ID is out of range"
                )
        numeric = {
            "stopped_speed_max": self.stopped_speed_max,
            "moving_speed_min": self.moving_speed_min,
            "idle_rpm_min": self.idle_rpm_min,
            "pull_speed_rate_min": self.pull_speed_rate_min,
            "pull_throttle_min": self.pull_throttle_min,
            "steady_speed_rate_max": self.steady_speed_rate_max,
            "steady_throttle_rate_max": self.steady_throttle_rate_max,
            "lift_throttle_rate_max": self.lift_throttle_rate_max,
            "overrun_speed_rate_max": self.overrun_speed_rate_max,
            "overrun_throttle_max": self.overrun_throttle_max,
        }
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in numeric.values()
        ):
            raise CorrelateError("regime thresholds must be finite")
        if self.stopped_speed_max >= self.moving_speed_min:
            raise CorrelateError(
                "regime stopped speed must be below moving speed"
            )
        if self.idle_rpm_min < 0.0:
            raise CorrelateError("regime idle RPM minimum must be non-negative")
        if self.pull_speed_rate_min <= 0.0:
            raise CorrelateError(
                "regime pull speed-rate minimum must be positive"
            )
        if (
            self.steady_speed_rate_max < 0.0
            or self.steady_throttle_rate_max < 0.0
        ):
            raise CorrelateError(
                "regime steady-rate maxima must be non-negative"
            )
        if self.lift_throttle_rate_max >= 0.0:
            raise CorrelateError(
                "regime lift throttle-rate maximum must be negative"
            )
        if self.overrun_speed_rate_max > 0.0:
            raise CorrelateError(
                "regime overrun speed-rate maximum must be non-positive"
            )
        if (
            type(self.minimum_samples) is not int
            or self.minimum_samples < 2
        ):
            raise CorrelateError(
                "regime minimum samples must be an integer of at least 2"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "inputs": {
                "speed": self.speed.as_dict(),
                "rpm": self.rpm.as_dict(),
                "throttle": self.throttle.as_dict(),
            },
            "candidate_streams": [
                (
                    f"eff:{can_id:08X}:{dlc}"
                    if id_bits == 29
                    else f"sff:{can_id:03X}:{dlc}"
                )
                for can_id, id_bits, dlc in sorted(
                    self.candidate_streams,
                    key=lambda item: (item[1], item[0], item[2]),
                )
            ],
            "thresholds_raw_units": {
                "stopped_speed_max": self.stopped_speed_max,
                "moving_speed_min": self.moving_speed_min,
                "idle_rpm_min": self.idle_rpm_min,
                "pull_speed_rate_min_per_second": (
                    self.pull_speed_rate_min
                ),
                "pull_throttle_min": self.pull_throttle_min,
                "steady_speed_rate_max_per_second": (
                    self.steady_speed_rate_max
                ),
                "steady_throttle_rate_max_per_second": (
                    self.steady_throttle_rate_max
                ),
                "lift_throttle_rate_max_per_second": (
                    self.lift_throttle_rate_max
                ),
                "overrun_speed_rate_max_per_second": (
                    self.overrun_speed_rate_max
                ),
                "overrun_throttle_max": self.overrun_throttle_max,
            },
            "minimum_samples_per_regime": self.minimum_samples,
        }


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


def parse_candump_frame(
    line: bytes,
    *,
    expected_channel: str = DEFAULT_MODULE.channel,
) -> CanFrame | None:
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
    if interface != expected_channel:
        raise CorrelateError(
            f"candump interface must be the pinned module channel "
            f"{expected_channel!r}"
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
        channel=interface,
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
    if match is not None:
        return FieldSpec(match.group(1), int(match.group(2), 10))
    bit_match = _BIT_REFERENCE_FIELD_RE.fullmatch(value)
    if bit_match is not None:
        try:
            geometry = SignalField(
                int(bit_match.group(2), 10),
                int(bit_match.group(3), 10),
                bit_match.group(1),
                bit_match.group(4) == "signed",
            )
        except SignalFieldError as exc:
            raise CorrelateError(f"invalid reference bit field: {exc}") from exc
        return FieldSpec.from_signal_field(geometry)
    raise CorrelateError(
        "reference field must be auto, byte:N, u16be:N, u16le:N, "
        "u32be:N, u32le:N, i16be:N, i16le:N, i32be:N, i32le:N, "
        "or bits:<little|big>:<DBC-start>:<length>:<unsigned|signed>"
    )


def _parse_bit_search_id(value: str) -> tuple[int, int, int]:
    match = _BIT_SEARCH_ID_RE.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError(
            "bit-search stream must be sff:HHH:DLC or eff:HHHHHHHH:DLC"
        )
    can_id = int(match.group(2), 16)
    id_bits = 11 if match.group(1) == "sff" else 29
    maximum = 0x7FF if id_bits == 11 else 0x1FFFFFFF
    if can_id > maximum:
        raise argparse.ArgumentTypeError(
            f"{match.group(1)} identifier exceeds 0x{maximum:X}"
        )
    return can_id, id_bits, int(match.group(3), 10)


def _parse_stream_field_selector(value: str) -> StreamFieldSelector:
    stream_text, separator, field_text = value.partition("=")
    if not separator or not field_text:
        raise argparse.ArgumentTypeError(
            "stream field must be "
            "sff:HHH:DLC=FIELD or eff:HHHHHHHH:DLC=FIELD"
        )
    try:
        can_id, id_bits, dlc = _parse_bit_search_id(stream_text)
        field = _parse_reference_field(field_text)
    except (argparse.ArgumentTypeError, CorrelateError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if field is None:
        raise argparse.ArgumentTypeError(
            "stream field cannot use automatic decoding"
        )
    try:
        if _legacy_signal_field(field).required_payload_bytes > dlc:
            raise argparse.ArgumentTypeError(
                f"field {field.label} exceeds the selected DLC {dlc}"
            )
    except (CorrelateError, SignalFieldError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return StreamFieldSelector(can_id, id_bits, dlc, field)


def _parse_signal_length(value: str) -> int:
    try:
        length = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "signal length must be a decimal integer"
        ) from exc
    if not 1 <= length <= MAX_SIGNAL_BITS:
        raise argparse.ArgumentTypeError(
            f"signal length must be between 1 and {MAX_SIGNAL_BITS}"
        )
    return length


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r} is not accepted")


@lru_cache(maxsize=None)
def _legacy_signal_field(spec: FieldSpec) -> SignalField:
    if spec.geometry is not None:
        return spec.geometry
    if spec.kind == "byte":
        return SignalField(spec.offset * 8, 8, "little")
    if spec.kind == "u13be-low5":
        return SignalField(spec.offset * 8 + 4, 13, "big")
    if spec.kind == "u17be-low1":
        return SignalField(spec.offset * 8, 17, "big")
    match = re.fullmatch(r"([ui])(16|32)(be|le)", spec.kind)
    if match is None:
        raise CorrelateError(f"unsupported field kind {spec.kind!r}")
    byte_order = "big" if match.group(3) == "be" else "little"
    start_bit = spec.offset * 8 + (7 if byte_order == "big" else 0)
    return SignalField(
        start_bit,
        int(match.group(2), 10),
        byte_order,
        signed=match.group(1) == "i",
    )


def _decode_field(payload: bytes, spec: FieldSpec) -> int:
    end = spec.offset + spec.width_bytes
    if spec.offset < 0 or end > len(payload):
        raise CorrelateError(
            f"field {spec.label} exceeds a {len(payload)}-byte payload"
        )
    try:
        return _legacy_signal_field(spec).extract(payload)
    except SignalFieldError as exc:
        raise CorrelateError(str(exc)) from exc


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
    module: Module = DEFAULT_MODULE,
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
            valid_directions = {"ecu_to_tester"}
            if module.key == "cluster":
                valid_directions.add("cluster_to_tester")
            profile_direction = f"{module.key}_to_tester"
            if (
                profile_direction not in valid_directions
                and row.get("direction") == profile_direction
            ):
                if row.get("module") != module.key:
                    raise CorrelateError(
                        f"{path}:{line_number}: selected row has invalid module"
                    )
                valid_directions.add(profile_direction)
            if row.get("direction") not in valid_directions:
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
            expected_id_bits = (
                11 if module.addressing_mode == NORMAL_11BITS else 29
            )
            if expected_can_id > 0x1FFFFFFF:
                raise CorrelateError(
                    f"{path}:{line_number}: selected row can_id exceeds 29 bits"
                )
            if expected_can_id != module.rxid:
                raise CorrelateError(
                    f"{path}:{line_number}: selected DID row is not from the "
                    f"registered {module.key} RX endpoint "
                    f"0x{module.rxid:08X}"
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
                expected_id_bits=expected_id_bits,
                expected_channel=module.channel,
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
    expected_channel: str = DEFAULT_MODULE.channel,
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
                        frame = parse_candump_frame(
                            line, expected_channel=expected_channel
                        )
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
                        channel=frame.channel,
                    )
                    raw_line_sequence += 1
        except CorrelateError:
            raise
        except OSError as exc:
            raise CorrelateError(f"cannot read candump stream {path}: {exc}") from exc


@lru_cache(maxsize=8)
def _legacy_payload_specs(payload_length: int) -> tuple[FieldSpec, ...]:
    specs: list[FieldSpec] = []
    specs.extend(FieldSpec("byte", offset) for offset in range(payload_length))
    for offset in range(max(0, payload_length - 1)):
        # Preserve the historical order exactly for report compatibility.
        specs.append(FieldSpec("u13be-low5", offset))
        specs.append(FieldSpec("u16be", offset))
        specs.append(FieldSpec("u16le", offset))
    specs.extend(
        FieldSpec("u17be-low1", offset)
        for offset in range(max(0, payload_length - 2))
    )
    # Keep 32-bit candidates word-aligned in the default coarse pass.
    for offset in range(0, max(0, payload_length - 3), 4):
        specs.append(FieldSpec("u32be", offset))
        specs.append(FieldSpec("u32le", offset))
    return tuple(specs)


@lru_cache(maxsize=256)
def _bit_search_specs(
    payload_length: int,
    minimum_bits: int,
    maximum_bits: int,
    lengths: tuple[int, ...],
    byte_orders: tuple[str, ...],
    signedness: tuple[bool, ...],
) -> tuple[FieldSpec, ...]:
    try:
        geometries = tuple(
            iter_signal_fields(
                payload_length,
                minimum_bits=minimum_bits,
                maximum_bits=maximum_bits,
                lengths=lengths or None,
                byte_orders=byte_orders,
                signedness=signedness,
            )
        )
    except SignalFieldError as exc:
        raise CorrelateError(f"invalid bit-search geometry: {exc}") from exc
    if len(geometries) > MAX_BIT_SEARCH_FIELDS_PER_IDENTIFIER:
        raise CorrelateError(
            "bit-search field count exceeds the "
            f"{MAX_BIT_SEARCH_FIELDS_PER_IDENTIFIER}-field per-identifier cap; "
            "select fewer lengths or byte orders"
        )
    return tuple(FieldSpec.from_signal_field(item) for item in geometries)


def iter_payload_fields(payload: bytes) -> Iterator[tuple[FieldSpec, int]]:
    """Yield the backward-compatible coarse candidate profile."""

    for spec in _legacy_payload_specs(len(payload)):
        yield spec, _decode_field(payload, spec)


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
class FixedFormulaResiduals:
    """Bounded exact residuals for one predeclared affine hypothesis."""

    count: int = 0
    sum_error: float = 0.0
    sum_squared_error: float = 0.0
    sum_absolute_error: float = 0.0
    minimum_error: float = math.inf
    maximum_error: float = -math.inf
    absolute_errors: list[float] = field(default_factory=list, repr=False)

    def add(
        self,
        candidate_raw: float,
        reference_raw: float,
        *,
        scale: float,
        intercept: float,
    ) -> None:
        if self.count >= MAX_REFERENCE_SAMPLES:
            raise CorrelateError(
                "fixed formula residual-count safety cap exceeded"
            )
        predicted = scale * candidate_raw + intercept
        error = reference_raw - predicted
        if not math.isfinite(error):
            raise CorrelateError("non-finite fixed formula residual")
        absolute_error = abs(error)
        self.count += 1
        self.sum_error += error
        self.sum_squared_error += error * error
        self.sum_absolute_error += absolute_error
        self.minimum_error = min(self.minimum_error, error)
        self.maximum_error = max(self.maximum_error, error)
        self.absolute_errors.append(absolute_error)

    def result(
        self,
        *,
        reference_count: int,
        config: FixedFormulaConfig,
    ) -> dict[str, object]:
        if self.count == 0:
            raise CorrelateError(
                "fixed formula candidate had no matched observations"
            )
        ordered = sorted(self.absolute_errors)
        nearest_rank_index = max(0, math.ceil(0.95 * self.count) - 1)
        mean_error = self.sum_error / self.count
        return {
            "sample_count": self.count,
            "coverage_ratio": self.count / reference_count,
            "error_orientation": (
                "reference_raw - predicted_reference_raw"
            ),
            "signed_mean_error": mean_error,
            "absolute_mean_bias": abs(mean_error),
            "mean_absolute_error": self.sum_absolute_error / self.count,
            "rmse": math.sqrt(self.sum_squared_error / self.count),
            "p95_absolute_error": ordered[nearest_rank_index],
            "p95_method": (
                "conservative nearest-rank: sorted ceil(0.95 * n)"
            ),
            "minimum_signed_error": self.minimum_error,
            "maximum_signed_error": self.maximum_error,
            "formula": config.as_dict(),
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

    def add(
        self,
        frame: CanFrame,
        decoded_fields: Iterable[tuple[FieldSpec, int]],
    ) -> int:
        created = 0
        delta = abs(frame.timestamp_us - self.reference_timestamp_us)
        for field_spec, value in decoded_fields:
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
    matches: dict[tuple[str, int, int, int], MatchState] = field(
        default_factory=dict
    )


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
    bit_search_ids: frozenset[tuple[int, int, int]] = field(
        default_factory=frozenset
    )
    bit_search_minimum_bits: int = 1
    bit_search_maximum_bits: int = MAX_SIGNAL_BITS
    bit_search_lengths: tuple[int, ...] = ()
    bit_search_byte_orders: tuple[str, ...] = ("little", "big")
    bit_search_signedness: tuple[bool, ...] = (False, True)
    fixed_formula: FixedFormulaConfig | None = None
    regime_analysis: RegimeAnalysisConfig | None = None

    @property
    def window_statistic(self) -> str | None:
        if self.match_mode.startswith("window-"):
            return self.match_mode.split("-", 1)[1]
        return None

    def uses_bit_search(self, can_id: int, id_bits: int, dlc: int) -> bool:
        return (can_id, id_bits, dlc) in self.bit_search_ids

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
        if not isinstance(self.bit_search_ids, frozenset):
            raise CorrelateError("bit-search IDs must be a frozenset")
        if len(self.bit_search_ids) > MAX_BIT_SEARCH_IDENTIFIERS:
            raise CorrelateError(
                "bit-search identifier count exceeds the "
                f"{MAX_BIT_SEARCH_IDENTIFIERS}-identifier safety cap"
            )
        for item in self.bit_search_ids:
            if (
                not isinstance(item, tuple)
                or len(item) != 3
                or type(item[0]) is not int
                or item[1] not in (11, 29)
                or type(item[2]) is not int
                or not 1 <= item[2] <= 8
            ):
                raise CorrelateError(
                    "bit-search streams must be (CAN ID, 11|29, DLC) tuples"
                )
            maximum = 0x7FF if item[1] == 11 else 0x1FFFFFFF
            if not 0 <= item[0] <= maximum:
                raise CorrelateError("bit-search CAN ID is out of range")
        if (
            type(self.bit_search_minimum_bits) is not int
            or type(self.bit_search_maximum_bits) is not int
            or not 1
            <= self.bit_search_minimum_bits
            <= self.bit_search_maximum_bits
            <= MAX_SIGNAL_BITS
        ):
            raise CorrelateError(
                "bit-search bounds must satisfy 1 <= minimum <= maximum <= "
                f"{MAX_SIGNAL_BITS}"
            )
        if not isinstance(self.bit_search_lengths, tuple) or any(
            type(length) is not int
            or not self.bit_search_minimum_bits
            <= length
            <= self.bit_search_maximum_bits
            for length in self.bit_search_lengths
        ):
            raise CorrelateError(
                "bit-search lengths must be integers within the configured bounds"
            )
        if len(set(self.bit_search_lengths)) != len(self.bit_search_lengths):
            raise CorrelateError("bit-search lengths must not contain duplicates")
        if (
            not isinstance(self.bit_search_byte_orders, tuple)
            or not self.bit_search_byte_orders
            or len(set(self.bit_search_byte_orders))
            != len(self.bit_search_byte_orders)
            or any(
                order not in BYTE_ORDERS
                for order in self.bit_search_byte_orders
            )
        ):
            raise CorrelateError(
                "bit-search byte orders must contain unique little and/or big"
            )
        if (
            not isinstance(self.bit_search_signedness, tuple)
            or not self.bit_search_signedness
            or len(set(self.bit_search_signedness))
            != len(self.bit_search_signedness)
            or any(type(value) is not bool for value in self.bit_search_signedness)
        ):
            raise CorrelateError(
                "bit-search signedness must contain unique bool values"
            )
        total_bit_search_fields = 0
        for _, _, dlc in self.bit_search_ids:
            total_bit_search_fields += len(
                _bit_search_specs(
                    dlc,
                    self.bit_search_minimum_bits,
                    self.bit_search_maximum_bits,
                    self.bit_search_lengths,
                    self.bit_search_byte_orders,
                    self.bit_search_signedness,
                )
            )
        if total_bit_search_fields > MAX_CANDIDATE_FIELDS:
            raise CorrelateError(
                "configured bit-search streams exceed the global "
                f"{MAX_CANDIDATE_FIELDS}-field candidate cap"
            )
        if self.fixed_formula is not None:
            self.fixed_formula.validate()
            selector = self.fixed_formula.candidate
            if selector.id_bits == 29 and not self.include_extended:
                raise CorrelateError(
                    "extended fixed formula stream requires "
                    "--include-extended"
                )
            if (
                is_diagnostic_id(selector.can_id, selector.id_bits)
                and not self.include_diagnostic_ids
            ):
                raise CorrelateError(
                    "diagnostic fixed formula stream requires "
                    "--include-diagnostic-ids"
                )
            if self.uses_bit_search(
                selector.can_id, selector.id_bits, selector.dlc
            ):
                eligible_specs = _bit_search_specs(
                    selector.dlc,
                    self.bit_search_minimum_bits,
                    self.bit_search_maximum_bits,
                    self.bit_search_lengths,
                    self.bit_search_byte_orders,
                    self.bit_search_signedness,
                )
            else:
                eligible_specs = _legacy_payload_specs(selector.dlc)
            if selector.field not in eligible_specs:
                raise CorrelateError(
                    "fixed formula field is not present in the configured "
                    "candidate field profile"
                )
        if self.regime_analysis is not None:
            if self.match_mode != "nearest":
                raise CorrelateError(
                    "regime analysis currently requires nearest matching"
                )
            self.regime_analysis.validate()
            projected_regime_fields = 0
            for can_id, id_bits, dlc in (
                self.regime_analysis.candidate_streams
            ):
                if self.uses_bit_search(can_id, id_bits, dlc):
                    projected_regime_fields += len(
                        _bit_search_specs(
                            dlc,
                            self.bit_search_minimum_bits,
                            self.bit_search_maximum_bits,
                            self.bit_search_lengths,
                            self.bit_search_byte_orders,
                            self.bit_search_signedness,
                        )
                    )
                else:
                    projected_regime_fields += len(
                        _legacy_payload_specs(dlc)
                    )
            projected_regressions = (
                projected_regime_fields * len(REGIME_NAMES)
            )
            if projected_regressions > MAX_REGIME_REGRESSIONS:
                raise CorrelateError(
                    "configured regime candidate fields can create "
                    f"{projected_regressions} regressions across "
                    f"{len(REGIME_NAMES)} regimes, exceeding the "
                    f"{MAX_REGIME_REGRESSIONS}-regression safety cap; "
                    "select fewer streams, lengths, byte orders, or "
                    "signedness variants"
                )
            regime_streams = set(self.regime_analysis.candidate_streams)
            regime_streams.update(
                (selector.can_id, selector.id_bits, selector.dlc)
                for selector in (
                    self.regime_analysis.speed,
                    self.regime_analysis.rpm,
                    self.regime_analysis.throttle,
                )
            )
            if any(
                id_bits == 29 and not self.include_extended
                for _, id_bits, _ in regime_streams
            ):
                raise CorrelateError(
                    "extended regime streams require "
                    "--include-extended"
                )
            if any(
                is_diagnostic_id(can_id, id_bits)
                and not self.include_diagnostic_ids
                for can_id, id_bits, _ in regime_streams
            ):
                raise CorrelateError(
                    "diagnostic regime streams require "
                    "--include-diagnostic-ids"
                )


class StreamingCorrelator:
    def __init__(self, config: AnalysisConfig) -> None:
        config.validate()
        self.config = config
        self.active: deque[ActiveReference] = deque()
        self.histories: dict[
            tuple[str, int, int, int], deque[CanFrame]
        ] = {}
        self.regressions: dict[CandidateKey, OnlineRegression] = {}
        self.reference_count = 0
        self.reference_first_us: int | None = None
        self.reference_last_us: int | None = None
        self.reference_interval_count = 0
        self.reference_interval_total_us = 0
        self.reference_interval_minimum_us: int | None = None
        self.reference_interval_maximum_us = 0
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
        self.eligible_candidate_maximum_r_squared: float | None = None
        self.fixed_formula_residuals = (
            None
            if config.fixed_formula is None
            else FixedFormulaResiduals()
        )
        self.regime_regressions: dict[
            tuple[str, CandidateKey], OnlineRegression
        ] = {}
        self.regime_classification_counts = {
            **{name: 0 for name in REGIME_NAMES},
            "other": 0,
            "missing_classifier_input": 0,
            "insufficient_history": 0,
        }
        self._previous_regime_features: (
            tuple[int, float, float, float] | None
        ) = None

    def _iter_payload_fields(
        self, can_id: int, id_bits: int, payload: bytes
    ) -> Iterator[tuple[FieldSpec, int]]:
        if not self.config.uses_bit_search(can_id, id_bits, len(payload)):
            yield from iter_payload_fields(payload)
            return
        specs = _bit_search_specs(
            len(payload),
            self.config.bit_search_minimum_bits,
            self.config.bit_search_maximum_bits,
            self.config.bit_search_lengths,
            self.config.bit_search_byte_orders,
            self.config.bit_search_signedness,
        )
        for spec in specs:
            yield spec, _decode_field(payload, spec)

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
        key = (frame.channel, frame.can_id, frame.id_bits, frame.dlc)
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
            self.active_window_fields += match.add(
                frame,
                self._iter_payload_fields(
                    frame.can_id, frame.id_bits, frame.payload
                ),
            )
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
                assert reference.expected_id_bits is not None
                assert reference.expected_channel is not None
                assert reference.expected_can_data is not None
                if (
                    frame.timestamp_us != reference.timestamp_us
                    or frame.can_id != reference.expected_can_id
                    or frame.id_bits != reference.expected_id_bits
                    or frame.channel != reference.expected_channel
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
        key = (frame.channel, frame.can_id, frame.id_bits, frame.dlc)
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
        if self.reference_last_us is not None:
            interval = sample.timestamp_us - self.reference_last_us
            self.reference_interval_count += 1
            self.reference_interval_total_us += interval
            if self.reference_interval_minimum_us is None:
                self.reference_interval_minimum_us = interval
            else:
                self.reference_interval_minimum_us = min(
                    self.reference_interval_minimum_us, interval
                )
            self.reference_interval_maximum_us = max(
                self.reference_interval_maximum_us, interval
            )
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

    def _regime_for_reference(self, active: ActiveReference) -> str | None:
        regime = self.config.regime_analysis
        if regime is None:
            return None

        values = []
        for selector in (regime.speed, regime.rpm, regime.throttle):
            match = active.matches.get(selector.stream_key)
            if not isinstance(match, NearestMatch):
                self.regime_classification_counts[
                    "missing_classifier_input"
                ] += 1
                return None
            try:
                values.append(float(_decode_field(match.payload, selector.field)))
            except CorrelateError:
                self.regime_classification_counts[
                    "missing_classifier_input"
                ] += 1
                return None

        speed, rpm, throttle = values
        current = (
            active.sample.timestamp_us,
            speed,
            rpm,
            throttle,
        )
        previous = self._previous_regime_features
        self._previous_regime_features = current

        if speed <= regime.stopped_speed_max and rpm >= regime.idle_rpm_min:
            label = "idle"
        elif previous is None:
            self.regime_classification_counts["insufficient_history"] += 1
            return None
        else:
            elapsed_seconds = (
                active.sample.timestamp_us - previous[0]
            ) / 1_000_000.0
            if elapsed_seconds <= 0.0:
                raise CorrelateError(
                    "regime classifier reference time did not advance"
                )
            speed_rate = (speed - previous[1]) / elapsed_seconds
            throttle_rate = (throttle - previous[3]) / elapsed_seconds
            moving = speed >= regime.moving_speed_min
            drivetrain_active = moving and rpm >= regime.idle_rpm_min
            if (
                drivetrain_active
                and throttle_rate <= regime.lift_throttle_rate_max
            ):
                label = "lift_transition"
            elif (
                drivetrain_active
                and speed_rate >= regime.pull_speed_rate_min
                and throttle >= regime.pull_throttle_min
            ):
                label = "positive_pull"
            elif (
                drivetrain_active
                and throttle <= regime.overrun_throttle_max
                and speed_rate <= regime.overrun_speed_rate_max
            ):
                label = "negative_overrun"
            elif (
                drivetrain_active
                and abs(speed_rate) <= regime.steady_speed_rate_max
                and abs(throttle_rate)
                <= regime.steady_throttle_rate_max
            ):
                label = "steady_cruise"
            else:
                label = "other"
        self.regime_classification_counts[label] += 1
        return label

    def _add_regime_observation(
        self,
        regime_name: str | None,
        key: CandidateKey,
        value: float,
        reference_value: float,
        *,
        reference_timestamp_us: int,
        contributing_frames: int,
        contributing_abs_delta_us: int,
        maximum_abs_delta_us: int,
    ) -> None:
        regime = self.config.regime_analysis
        if regime is None or regime_name not in REGIME_NAMES:
            return
        if (key.can_id, key.id_bits, key.dlc) not in regime.candidate_streams:
            return
        regime_key = (regime_name, key)
        regression = self.regime_regressions.get(regime_key)
        if regression is None:
            if len(self.regime_regressions) >= MAX_REGIME_REGRESSIONS:
                raise CorrelateError(
                    "regime regression safety cap exceeded"
                )
            regression = OnlineRegression()
            self.regime_regressions[regime_key] = regression
        regression.add(
            value,
            reference_value,
            reference_timestamp_us=reference_timestamp_us,
            contributing_frames=contributing_frames,
            contributing_abs_delta_us=contributing_abs_delta_us,
            maximum_abs_delta_us=maximum_abs_delta_us,
        )

    def _add_fixed_formula_observation(
        self,
        key: CandidateKey,
        candidate_raw: float,
        reference_raw: float,
    ) -> None:
        fixed = self.config.fixed_formula
        residuals = self.fixed_formula_residuals
        if fixed is None or residuals is None:
            return
        selector = fixed.candidate
        expected = CandidateKey(
            selector.channel,
            selector.can_id,
            selector.id_bits,
            selector.dlc,
            selector.field,
        )
        if key != expected:
            return
        residuals.add(
            candidate_raw,
            reference_raw,
            scale=fixed.scale,
            intercept=fixed.intercept,
        )

    def _finalize_reference(self, active: ActiveReference) -> None:
        regime_name = self._regime_for_reference(active)
        self.active_match_states -= len(active.matches)
        for (channel, can_id, id_bits, dlc), match in active.matches.items():
            if isinstance(match, NearestMatch):
                delta = abs(
                    match.timestamp_us - active.sample.timestamp_us
                )
                for field_spec, value in self._iter_payload_fields(
                    can_id, id_bits, match.payload
                ):
                    key = CandidateKey(
                        channel, can_id, id_bits, dlc, field_spec
                    )
                    self._regression(key).add(
                        float(value),
                        active.sample.value,
                        reference_timestamp_us=active.sample.timestamp_us,
                        contributing_frames=1,
                        contributing_abs_delta_us=delta,
                        maximum_abs_delta_us=delta,
                    )
                    self._add_fixed_formula_observation(
                        key,
                        float(value),
                        active.sample.value,
                    )
                    self._add_regime_observation(
                        regime_name,
                        key,
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
                    key = CandidateKey(
                        channel, can_id, id_bits, dlc, field_spec
                    )
                    value = accumulator.value(statistic)
                    self._regression(key).add(
                        value,
                        active.sample.value,
                        reference_timestamp_us=active.sample.timestamp_us,
                        contributing_frames=accumulator.count,
                        contributing_abs_delta_us=accumulator.abs_delta_us,
                        maximum_abs_delta_us=accumulator.maximum_abs_delta_us,
                    )
                    self._add_fixed_formula_observation(
                        key,
                        value,
                        active.sample.value,
                    )
                    self._add_regime_observation(
                        regime_name,
                        key,
                        value,
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

    @staticmethod
    def _candidate_row(
        key: CandidateKey, result: dict[str, object]
    ) -> dict[str, object]:
        return {
            "classification": "candidate_only",
            "evidence_tier": EXPLORATORY_EVIDENCE_TIER,
            "candidate_only": True,
            "physical_identity_verified": False,
            "scale_verified": False,
            "telemetry_promotion_allowed": False,
            "channel": key.channel,
            "can_id": key.can_id,
            "can_id_hex": (
                f"{key.can_id:08X}"
                if key.id_bits == 29
                else f"{key.can_id:03X}"
            ),
            "id_bits": key.id_bits,
            "dlc": key.dlc,
            "field": key.field.as_dict(),
            **result,
        }

    @staticmethod
    def _sort_candidate_rows(rows: list[dict[str, object]]) -> None:
        rows.sort(
            key=lambda row: (
                -float(row["fit_coverage_score"]),
                -float(row["correlation"]["r_squared"]),
                -float(row["coverage_ratio"]),
                -int(row["sample_count"]),
                str(row["channel"]),
                int(row["can_id"]),
                int(row["id_bits"]),
                int(row["dlc"]),
                (
                    0,
                    str(row["field"]["kind"]),
                    int(row["field"]["offset"]),
                )
                if "dbc_start_bit" not in row["field"]
                else (
                    1,
                    str(row["field"]["kind"]),
                    int(row["field"]["offset"]),
                    int(row["field"]["dbc_start_bit"]),
                    int(row["field"]["length_bits"]),
                    str(row["field"]["byte_order"]),
                    bool(row["field"]["signed"]),
                ),
            )
        )

    def candidate_rows(self) -> tuple[list[dict[str, object]], dict[str, int]]:
        rows: list[dict[str, object]] = []
        self.eligible_candidate_maximum_r_squared = None
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
            r_squared = float(result["correlation"]["r_squared"])
            if self.eligible_candidate_maximum_r_squared is None:
                self.eligible_candidate_maximum_r_squared = r_squared
            else:
                self.eligible_candidate_maximum_r_squared = max(
                    self.eligible_candidate_maximum_r_squared,
                    r_squared,
                )
            rows.append(self._candidate_row(key, result))
        self._sort_candidate_rows(rows)
        rejection_counts["eligible_but_omitted_by_top"] = max(
            0, len(rows) - self.config.top_count
        )
        rows = rows[: self.config.top_count]
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
        return rows, rejection_counts

    def regime_rows(self) -> dict[str, dict[str, object]] | None:
        regime = self.config.regime_analysis
        if regime is None:
            return None
        results: dict[str, dict[str, object]] = {}
        for regime_name in REGIME_NAMES:
            reference_count = self.regime_classification_counts[regime_name]
            rows: list[dict[str, object]] = []
            rejected = {
                "below_minimum_samples": 0,
                "below_two_distinct_values": 0,
                "constant_candidate_or_reference": 0,
            }
            for (candidate_regime, key), regression in (
                self.regime_regressions.items()
            ):
                if candidate_regime != regime_name:
                    continue
                if regression.count < regime.minimum_samples:
                    rejected["below_minimum_samples"] += 1
                    continue
                if (
                    len(regression.distinct_x_values) < 2
                    or len(regression.distinct_y_values) < 2
                ):
                    rejected["below_two_distinct_values"] += 1
                    continue
                result = regression.result(
                    reference_count=reference_count,
                    match_mode=self.config.match_mode,
                )
                if result is None:
                    rejected["constant_candidate_or_reference"] += 1
                    continue
                rows.append(self._candidate_row(key, result))
            self._sort_candidate_rows(rows)
            eligible_maximum = (
                None
                if not rows
                else max(
                    float(row["correlation"]["r_squared"])
                    for row in rows
                )
            )
            rejected["eligible_but_omitted_by_top"] = max(
                0, len(rows) - self.config.top_count
            )
            rows = rows[: self.config.top_count]
            for rank, row in enumerate(rows, 1):
                row["rank"] = rank
            results[regime_name] = {
                "classified_reference_count": reference_count,
                "reported_candidate_count": len(rows),
                "eligible_candidate_maximum_r_squared": eligible_maximum,
                "rejected_field_counts": rejected,
                "candidates": rows,
            }
        return results


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


def _validate_inputs(
    wire: Path,
    captures: Sequence[Path],
    *,
    module: Module = DEFAULT_MODULE,
    allow_van_compute_staging: bool = False,
) -> None:
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
    expected_wire_name = f"{module.key}_wire.jsonl"
    valid_wire_name = wire.name == expected_wire_name
    if allow_van_compute_staging:
        job_root = _van_compute_job_root()
        input_parent = wire.parent
        try:
            input_root = input_parent.resolve(strict=True)
        except OSError as exc:
            raise CorrelateError(
                "van-compute input directory is unavailable"
            ) from exc
        if (
            input_parent.is_symlink()
            or not input_root.is_dir()
            or input_root.name != "inputs"
            or input_root.parent != job_root
            or any(path.resolve(strict=True).parent != input_root for path in paths)
        ):
            raise CorrelateError(
                "van-compute inputs must be real files directly below the "
                "staged sibling inputs directory"
            )
        valid_wire_name = valid_wire_name or bool(
            _VAN_COMPUTE_WIRE_NAME_RE.fullmatch(wire.name)
        )
    if not valid_wire_name:
        raise CorrelateError(
            "reference input must have the finalized recorder basename "
            f"{expected_wire_name} (or its validated van-compute staged name)"
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


def _validated_output_path(
    path: Path, *, allow_van_compute_result: bool = False
) -> Path:
    roots = [TMP_ROOT.resolve(strict=False)]
    if allow_van_compute_result:
        job_root = _van_compute_job_root()
        if path.name != "report.json":
            raise CorrelateError(
                "van-compute result output must be named report.json"
            )
        result_parent = path.parent
        try:
            result_root = result_parent.resolve(strict=True)
        except OSError as exc:
            raise CorrelateError(
                "van-compute result directory is unavailable"
            ) from exc
        if (
            result_parent.is_symlink()
            or not result_root.is_dir()
            or result_root.name != "result"
            or result_root.parent != job_root
        ):
            raise CorrelateError(
                "van-compute result output requires the staged sibling result "
                "directory"
            )
        roots.append(result_root)

    resolved = path.resolve(strict=False)
    inside_root: Path | None = None
    for root in roots:
        try:
            inside = os.path.commonpath((str(root), str(resolved))) == str(root)
        except ValueError:
            inside = False
        if inside and resolved != root:
            inside_root = root
            break
    if inside_root is None:
        permitted = str(TMP_ROOT)
        if allow_van_compute_result:
            permitted += " or the validated van-compute result directory"
        raise CorrelateError(f"output must be an explicit file below {permitted}")
    if path.suffix.lower() != ".json":
        raise CorrelateError("output filename must end in .json")
    if path.exists() or path.is_symlink():
        raise CorrelateError(f"refusing to overwrite existing output: {path}")
    return resolved


def _exclusive_write_json(
    path: Path,
    payload: dict[str, object],
    *,
    allow_van_compute_result: bool = False,
) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after mkdir in case an existing parent component was a symlink.
    checked = _validated_output_path(
        path, allow_van_compute_result=allow_van_compute_result
    )
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
    module: Module = DEFAULT_MODULE,
    decompressor: Decompressor | None = None,
    allow_van_compute_staging: bool = False,
) -> dict[str, object]:
    _validate_inputs(
        wire,
        captures,
        module=module,
        allow_van_compute_staging=allow_van_compute_staging,
    )
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
            wire,
            did=did,
            decoder=decoder,
            stats=reference_stats,
            module=module,
        ),
        iter_candump_frames(
            captures,
            stats=capture_stats,
            decompressor=decompressor,
            expected_channel=module.channel,
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
    regime_rows = correlator.regime_rows()
    fixed_formula_result = None
    if config.fixed_formula is not None:
        if correlator.fixed_formula_residuals is None:
            raise CorrelateError(
                "fixed formula residual state was not initialized"
            )
        fixed_formula_result = correlator.fixed_formula_residuals.result(
            reference_count=correlator.reference_count,
            config=config.fixed_formula,
        )
    assert decoder.resolved is not None
    return {
        "schema_version": SCHEMA_VERSION,
        "classification": "candidate_only",
        "evidence_tier": EXPLORATORY_EVIDENCE_TIER,
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
            "maximum_candidate_staleness_ms": config.radius_us / 1000.0,
            "staleness_definition": (
                "absolute candidate-frame delta from the exact "
                "PCAN-observed diagnostic response timestamp"
            ),
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
            "candidate_field_profile": {
                "default": (
                    "legacy_coarse_bytes_words_and_stellantis_packed_fields"
                ),
                "default_dlc8_field_count": len(_legacy_payload_specs(8)),
                "targeted_bit_search_streams": [
                    (
                        f"eff:{can_id:08X}:{dlc}"
                        if id_bits == 29
                        else f"sff:{can_id:03X}:{dlc}"
                    )
                    for can_id, id_bits, dlc in sorted(
                        config.bit_search_ids,
                        key=lambda item: (item[1], item[0], item[2]),
                    )
                ],
                "targeted_ids_replace_default_profile": True,
                "bit_numbering": "DBC/cantools sawtooth",
                "minimum_bits": config.bit_search_minimum_bits,
                "maximum_bits": config.bit_search_maximum_bits,
                "selected_lengths": list(config.bit_search_lengths),
                "selected_lengths_empty_means_full_configured_range": True,
                "byte_orders": list(config.bit_search_byte_orders),
                "signedness": [
                    "signed" if signed else "unsigned"
                    for signed in config.bit_search_signedness
                ],
                "equivalent_value_geometries_deduplicated": True,
            },
            "candidate_stream_identity": [
                "channel",
                "SFF/EFF namespace",
                "CAN ID",
                "DLC",
                "capture source path and decompressed SHA-256",
            ],
            "hard_memory_state_caps": {
                "active_references": MAX_ACTIVE_REFERENCES,
                "pending_wire_links": MAX_PENDING_WIRE_LINKS,
                "candidate_ids": MAX_CANDIDATE_IDS,
                "history_frames": MAX_HISTORY_FRAMES,
                "active_match_states": MAX_ACTIVE_MATCH_STATES,
                "active_window_fields": MAX_ACTIVE_WINDOW_FIELDS,
                "candidate_fields": MAX_CANDIDATE_FIELDS,
                "bit_search_streams": MAX_BIT_SEARCH_IDENTIFIERS,
                "bit_search_fields_per_identifier": (
                    MAX_BIT_SEARCH_FIELDS_PER_IDENTIFIER
                ),
                "reported_candidates": MAX_TOP_COUNT,
                "total_reference_samples": MAX_REFERENCE_SAMPLES,
                "fixed_formula_residuals": MAX_REFERENCE_SAMPLES,
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
                "key": module.key,
                "name": module.name,
                "bus": module.bus,
                "channel": module.channel,
                "txid_hex": f"{module.txid:08X}",
                "rxid_hex": f"{module.rxid:08X}",
                "addressing_mode": module.addressing_mode,
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
                    "channel",
                    "SFF/EFF namespace",
                    "can_id",
                    "can_data_hex",
                ],
            },
            "timestamp_coverage": {
                "first_epoch_us": correlator.reference_first_us,
                "last_epoch_us": correlator.reference_last_us,
            },
            "observed_polling_cadence": {
                "interval_count": correlator.reference_interval_count,
                "minimum_interval_ms": (
                    None
                    if correlator.reference_interval_minimum_us is None
                    else correlator.reference_interval_minimum_us / 1000.0
                ),
                "mean_interval_ms": (
                    None
                    if correlator.reference_interval_count == 0
                    else correlator.reference_interval_total_us
                    / correlator.reference_interval_count
                    / 1000.0
                ),
                "maximum_interval_ms": (
                    None
                    if correlator.reference_interval_count == 0
                    else correlator.reference_interval_maximum_us / 1000.0
                ),
                "reference_timestamp_source": (
                    "exact PCAN-observed diagnostic wire response"
                ),
                "csv_sample_holding_used": False,
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
            "candidate_identifier_count": len(
                {
                    (channel, can_id, id_bits)
                    for channel, can_id, id_bits, _ in correlator.histories
                }
            ),
            "candidate_stream_count": len(correlator.histories),
            "candidate_field_state_count": len(correlator.regressions),
        },
        "ranking": {
            "reported_candidate_count": len(candidate_rows),
            "eligible_candidate_maximum_r_squared": (
                correlator.eligible_candidate_maximum_r_squared
            ),
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
        **(
            {}
            if fixed_formula_result is None
            else {
                "fixed_formula_evaluation": {
                    "classification": "candidate_only",
                    "evidence_tier": PROXY_EVALUATION_EVIDENCE_TIER,
                    "candidate_only": True,
                    "physical_identity_verified": False,
                    "scale_verified": False,
                    "telemetry_promotion_allowed": False,
                    "purpose": (
                        "score one predeclared affine formula without "
                        "refitting it to this capture"
                    ),
                    "semantic_identity_warning": (
                        "low residual error can reject a poor formula but "
                        "cannot by itself prove physical signal identity"
                    ),
                    "reference_timestamp_source": (
                        "exact PCAN-observed diagnostic wire response"
                    ),
                    "candidate_timestamp_source": (
                        "nearest eligible passive CAN frame within the "
                        "configured radius"
                        if config.match_mode == "nearest"
                        else (
                            f"{config.window_statistic} of eligible passive "
                            "CAN frames in the configured symmetric window"
                        )
                    ),
                    "result": fixed_formula_result,
                }
            }
        ),
        **(
            {}
            if config.regime_analysis is None
            else {
                "regime_analysis": {
                    "classification": "candidate_only",
                    "evidence_tier": EXPLORATORY_EVIDENCE_TIER,
                    "candidate_only": True,
                    "physical_identity_verified": False,
                    "scale_verified": False,
                    "telemetry_promotion_allowed": False,
                    "purpose": (
                        "compare shortlisted torque-related passive fields "
                        "across explicit operating regimes"
                    ),
                    "semantic_identity_warning": (
                        "regime-dependent covariance can reject a proposed "
                        "identity but cannot prove actual torque semantics"
                    ),
                    "reference_timestamp_source": (
                        "exact PCAN-observed diagnostic wire response"
                    ),
                    "classifier_delta_basis": (
                        "rate between consecutive exact reference timestamps; "
                        "no CSV sample holding"
                    ),
                    "config": config.regime_analysis.as_dict(),
                    "classification_counts": (
                        correlator.regime_classification_counts
                    ),
                    "rankings": regime_rows,
                }
            }
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline-only: correlate exact module DID responses with saved "
            "candump broadcast fields."
        )
    )
    parser.add_argument(
        "--module",
        choices=tuple(MODULES),
        default=DEFAULT_MODULE.key,
        help=(
            "registered diagnostic module that owns the wire stream "
            f"(default: {DEFAULT_MODULE.key})"
        ),
    )
    parser.add_argument(
        "--wire",
        required=True,
        type=Path,
        help="completed <module>_wire.jsonl reference stream",
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
            "auto, byte:N, unsigned u16/u32, signed i16/i32, or "
            "bits:<little|big>:<DBC-start>:<length>:<unsigned|signed> "
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
        "--bit-search-id",
        action="append",
        type=_parse_bit_search_id,
        default=[],
        metavar="{sff:HEX:DLC,eff:HEX:DLC}",
        help=(
            "replace the coarse candidate profile with exhaustive DBC bit "
            "geometry for this exact CAN ID namespace and DLC; repeat at most "
            f"{MAX_BIT_SEARCH_IDENTIFIERS} times"
        ),
    )
    parser.add_argument(
        "--bit-search-min-bits",
        type=_parse_signal_length,
        default=1,
        help="minimum targeted field length (default 1)",
    )
    parser.add_argument(
        "--bit-search-max-bits",
        type=_parse_signal_length,
        default=MAX_SIGNAL_BITS,
        help=f"maximum targeted field length (default {MAX_SIGNAL_BITS})",
    )
    parser.add_argument(
        "--bit-search-length",
        action="append",
        type=_parse_signal_length,
        default=[],
        metavar="BITS",
        help=(
            "search only this targeted bit length; repeat for additional "
            "lengths (otherwise use the configured min..max range)"
        ),
    )
    parser.add_argument(
        "--bit-search-byte-order",
        choices=("little", "big", "both"),
        default="both",
        help="targeted DBC byte-order profile (default both)",
    )
    parser.add_argument(
        "--bit-search-signedness",
        choices=("unsigned", "signed", "both"),
        default="both",
        help="targeted raw signedness profile (default both)",
    )
    parser.add_argument(
        "--fixed-formula-field",
        type=_parse_stream_field_selector,
        metavar="STREAM=FIELD",
        help=(
            "evaluate this exact candidate field against the reference "
            "using a predeclared affine formula"
        ),
    )
    parser.add_argument(
        "--fixed-formula-scale",
        type=float,
        help=(
            "predeclared multiplier in predicted_reference_raw = "
            "scale * candidate_raw + intercept"
        ),
    )
    parser.add_argument(
        "--fixed-formula-intercept",
        type=float,
        help=(
            "predeclared intercept in predicted_reference_raw = "
            "scale * candidate_raw + intercept"
        ),
    )
    parser.add_argument(
        "--regime-analysis",
        action="store_true",
        help=(
            "opt in to explicit idle/pull/cruise/lift/overrun slices for "
            "shortlisted passive torque candidates"
        ),
    )
    parser.add_argument(
        "--regime-speed-field",
        type=_parse_stream_field_selector,
        metavar="STREAM=FIELD",
    )
    parser.add_argument(
        "--regime-rpm-field",
        type=_parse_stream_field_selector,
        metavar="STREAM=FIELD",
    )
    parser.add_argument(
        "--regime-throttle-field",
        type=_parse_stream_field_selector,
        metavar="STREAM=FIELD",
    )
    parser.add_argument(
        "--regime-candidate-id",
        action="append",
        type=_parse_bit_search_id,
        default=[],
        metavar="{sff:HEX:DLC,eff:HEX:DLC}",
        help=(
            "exact passive stream to rank inside each regime; repeat up to "
            f"{MAX_REGIME_CANDIDATE_STREAMS} times"
        ),
    )
    parser.add_argument("--regime-stopped-speed-max", type=float)
    parser.add_argument("--regime-moving-speed-min", type=float)
    parser.add_argument("--regime-idle-rpm-min", type=float)
    parser.add_argument("--regime-pull-speed-rate-min", type=float)
    parser.add_argument("--regime-pull-throttle-min", type=float)
    parser.add_argument("--regime-steady-speed-rate-max", type=float)
    parser.add_argument("--regime-steady-throttle-rate-max", type=float)
    parser.add_argument("--regime-lift-throttle-rate-max", type=float)
    parser.add_argument("--regime-overrun-speed-rate-max", type=float)
    parser.add_argument("--regime-overrun-throttle-max", type=float)
    parser.add_argument(
        "--regime-minimum-samples",
        type=int,
        default=5,
        help="minimum samples needed to fit one candidate within a regime",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="new JSON report path below repository tmp/; never overwritten",
    )
    parser.add_argument(
        "--allow-van-compute-result",
        action="store_true",
        help=(
            "allow report.json in the validated van-compute job result "
            "directory"
        ),
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
        bit_search_byte_orders = (
            ("little", "big")
            if args.bit_search_byte_order == "both"
            else (args.bit_search_byte_order,)
        )
        bit_search_signedness = {
            "unsigned": (False,),
            "signed": (True,),
            "both": (False, True),
        }[args.bit_search_signedness]
        if len(set(args.bit_search_id)) != len(args.bit_search_id):
            raise CorrelateError("bit-search IDs must not contain duplicates")
        module = MODULES[args.module]

        def module_selector(
            selector: StreamFieldSelector,
        ) -> StreamFieldSelector:
            return StreamFieldSelector(
                selector.can_id,
                selector.id_bits,
                selector.dlc,
                selector.field,
                channel=module.channel,
            )

        fixed_formula_values = (
            args.fixed_formula_field,
            args.fixed_formula_scale,
            args.fixed_formula_intercept,
        )
        if any(value is not None for value in fixed_formula_values) and any(
            value is None for value in fixed_formula_values
        ):
            raise CorrelateError(
                "fixed formula evaluation requires field, scale, and intercept"
            )
        fixed_formula = None
        if args.fixed_formula_field is not None:
            fixed_formula = FixedFormulaConfig(
                candidate=module_selector(args.fixed_formula_field),
                scale=args.fixed_formula_scale,
                intercept=args.fixed_formula_intercept,
            )
        regime_option_values = (
            args.regime_speed_field,
            args.regime_rpm_field,
            args.regime_throttle_field,
            *args.regime_candidate_id,
            args.regime_stopped_speed_max,
            args.regime_moving_speed_min,
            args.regime_idle_rpm_min,
            args.regime_pull_speed_rate_min,
            args.regime_pull_throttle_min,
            args.regime_steady_speed_rate_max,
            args.regime_steady_throttle_rate_max,
            args.regime_lift_throttle_rate_max,
            args.regime_overrun_speed_rate_max,
            args.regime_overrun_throttle_max,
        )
        if not args.regime_analysis and any(
            value is not None for value in regime_option_values
        ):
            raise CorrelateError(
                "regime options require explicit --regime-analysis"
            )
        regime_analysis = None
        if args.regime_analysis:
            required_regime_values = (
                args.regime_speed_field,
                args.regime_rpm_field,
                args.regime_throttle_field,
                args.regime_stopped_speed_max,
                args.regime_moving_speed_min,
                args.regime_idle_rpm_min,
                args.regime_pull_speed_rate_min,
                args.regime_pull_throttle_min,
                args.regime_steady_speed_rate_max,
                args.regime_steady_throttle_rate_max,
                args.regime_lift_throttle_rate_max,
                args.regime_overrun_speed_rate_max,
                args.regime_overrun_throttle_max,
            )
            if any(value is None for value in required_regime_values):
                raise CorrelateError(
                    "regime analysis requires all classifier fields and "
                    "thresholds"
                )
            if not args.regime_candidate_id:
                raise CorrelateError(
                    "regime analysis requires at least one candidate stream"
                )
            if (
                len(set(args.regime_candidate_id))
                != len(args.regime_candidate_id)
            ):
                raise CorrelateError(
                    "regime candidate streams must not contain duplicates"
                )
            regime_analysis = RegimeAnalysisConfig(
                speed=module_selector(args.regime_speed_field),
                rpm=module_selector(args.regime_rpm_field),
                throttle=module_selector(args.regime_throttle_field),
                candidate_streams=frozenset(args.regime_candidate_id),
                stopped_speed_max=args.regime_stopped_speed_max,
                moving_speed_min=args.regime_moving_speed_min,
                idle_rpm_min=args.regime_idle_rpm_min,
                pull_speed_rate_min=args.regime_pull_speed_rate_min,
                pull_throttle_min=args.regime_pull_throttle_min,
                steady_speed_rate_max=args.regime_steady_speed_rate_max,
                steady_throttle_rate_max=(
                    args.regime_steady_throttle_rate_max
                ),
                lift_throttle_rate_max=args.regime_lift_throttle_rate_max,
                overrun_speed_rate_max=args.regime_overrun_speed_rate_max,
                overrun_throttle_max=args.regime_overrun_throttle_max,
                minimum_samples=args.regime_minimum_samples,
            )
        config = AnalysisConfig(
            match_mode=args.match,
            radius_us=int(radius_us_decimal),
            include_extended=args.include_extended,
            include_diagnostic_ids=args.include_diagnostic_ids,
            minimum_samples=args.minimum_samples,
            minimum_coverage_ratio=args.minimum_coverage,
            minimum_distinct_values=args.minimum_distinct,
            top_count=args.top,
            bit_search_ids=frozenset(args.bit_search_id),
            bit_search_minimum_bits=args.bit_search_min_bits,
            bit_search_maximum_bits=args.bit_search_max_bits,
            bit_search_lengths=tuple(args.bit_search_length),
            bit_search_byte_orders=bit_search_byte_orders,
            bit_search_signedness=bit_search_signedness,
            fixed_formula=fixed_formula,
            regime_analysis=regime_analysis,
        )
        config.validate()
        output = _validated_output_path(
            args.output,
            allow_van_compute_result=args.allow_van_compute_result,
        )
        _validate_inputs(
            args.wire,
            args.captures,
            module=MODULES[args.module],
            allow_van_compute_staging=args.allow_van_compute_result,
        )
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
            module=MODULES[args.module],
            allow_van_compute_staging=args.allow_van_compute_result,
        )
        _exclusive_write_json(
            output,
            report,
            allow_van_compute_result=args.allow_van_compute_result,
        )
    except CorrelateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        f"Wrote {report['ranking']['reported_candidate_count']} "
        f"exploratory candidate-only correlations to {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
