#!/usr/bin/env python3
"""Join one completed AlfaOBD singleton campaign to a passive CAN capture.

This is a strictly offline evidence joiner.  It validates the singleton campaign
checkpoint, event log, pulled-artifact hashes, and passive-recorder manifest before
using the recorded Android byte offsets as buffered-file witnesses.  Host-timed
passive-wire intervals are authoritative for each segment.  TesterPresent
exchanges are discarded; every remaining wire message is required to form a
strictly alternating physical request/response pair on the registered CAN IDs.

Results are deliberately ``candidate_only``.  Info labels are aligned as
contiguous runs across the whole buffered outer interval; their rendered sample
counts are not assumed to match the wire poll counts.  A label resolves only when
its segment repeatedly polls one exact ``22 <DID>`` request and every response is
positive ``62 <DID>``.  Repeated anchors must resolve to the same exact request in
every occurrence or the join fails.

No ADB, CAN socket, service, network, or vehicle interface is opened.  The only
child process this tool may run is ``zstd -dc`` for an already-recorded chunk.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import BinaryIO, Callable, Iterable, Iterator, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.modules import MODULES
from lib.vehicle_can_roles import CAN_ROLE_SPECS, normalize_can_role
from tools.alfaobd_singleton_campaign import CampaignError, CampaignPlan, load_plan


DEFAULT_OUT_DIR = REPO / "tmp" / "ecu_mapping" / "alfaobd_singleton_join"
DEFAULT_MAX_ARTIFACT_BYTES = 1024**3
DEFAULT_MAX_SEGMENT_BYTES = 64 * 1024**2
DEFAULT_MAX_SEGMENTS = 512
DEFAULT_MAX_EVENTS = 100_000
DEFAULT_MAX_EXCHANGES_PER_SEGMENT = 20_000
DEFAULT_MAX_MANIFEST_ROWS = 100_000
DEFAULT_MAX_WIRE_BYTES = 2 * 1024**3
DEFAULT_MAX_WIRE_MESSAGES = 100_000
DEFAULT_MAX_WIRE_PAYLOAD_BYTES = 128 * 1024**2
DEFAULT_MAX_CAPTURE_RUNS = 32
HARD_MAX_OUTER_ARTIFACT_BYTES = 64 * 1024**2
HARD_MAX_OUTER_MESSAGES = 100_000
HARD_MAX_WIRE_MESSAGES = 100_000
HARD_MAX_WIRE_FRAMES = 250_000
HARD_MAX_WIRE_PAYLOAD_BYTES = 128 * 1024**2
DEBUG_ARTIFACT = "AlfaOBD_Debug.bin"
INFO_SUFFIX = "_Info.log"
HEX_BYTES = frozenset(b"0123456789abcdefABCDEF")
TRANSPORT_LINE_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2}\.\d{3}) ([SR]): ([0-9A-Fa-f]*)$"
)
ELM_SEGMENT_RE = re.compile(r"^([0-9A-Fa-f]):([0-9A-Fa-f]+)$")
INFO_PARAMETER_ROW_RE = re.compile(r"^\s*([^:\r\n]+?)\s*:\s*(\S.*?)\s*$")
CANDUMP_RE = re.compile(
    rb"^\((?P<timestamp>[^)]+)\)\s+(?P<channel>\S+)\s+"
    rb"(?P<can_id>[0-9A-Fa-f]{3,8})#(?P<data>[0-9A-Fa-f]*)\s*$"
)
CHANNEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TOPOLOGY_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
ROLE_SPECS = {spec.role: spec for spec in CAN_ROLE_SPECS}


class JoinError(RuntimeError):
    """A malformed, incomplete, contradictory, or unbounded evidence input."""


@dataclass(frozen=True)
class ArtifactBoundary:
    before: int
    after: int


@dataclass(frozen=True)
class DebugExchange:
    request: bytes
    response: bytes


@dataclass(frozen=True)
class WireMessage:
    can_id: int
    payload: bytes
    first_timestamp: float
    last_timestamp: float
    channel: str


@dataclass(frozen=True)
class WireFrame:
    timestamp: float
    can_id: int
    data: bytes
    source_index: int
    channel: str


@dataclass
class _IsoTpPending:
    total_length: int
    payload: bytearray
    next_sequence: int
    first_timestamp: float


@dataclass(frozen=True)
class SegmentEvidence:
    sequence: int
    gauge: str
    before_time: float
    after_time: float
    boundaries: dict[str, ArtifactBoundary]


@dataclass(frozen=True)
class InfoRun:
    label: str
    rendered_values: tuple[str, ...]


@dataclass(frozen=True)
class DebugTransportStreams:
    requests: tuple[bytes, ...]
    responses: tuple[bytes, ...]
    tester_present_requests: int
    tester_present_responses: int
    leading_empty_prompts: int
    trailing_empty_prompts: int
    trailing_response_fragment_bytes: int


@dataclass(frozen=True)
class CaptureSpec:
    directory: Path
    run_id: str
    run_sha256: str | None = None
    checkpoint_sha256: str | None = None
    manifest_sha256: str | None = None


@dataclass(frozen=True)
class ValidatedCapture:
    spec: CaptureSpec
    frames: tuple[WireFrame, ...]
    coverage_intervals: tuple[tuple[float, float, int], ...]
    provenance: dict[str, object]


def _require_dict(value: object, context: str) -> dict:
    if not isinstance(value, dict):
        raise JoinError(f"{context} must be a JSON object")
    return value


def _require_int(value: object, context: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise JoinError(f"{context} must be an integer >= {minimum}")
    return value


def _require_finite_number(
    value: object,
    context: str,
    *,
    minimum: float | None = None,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or (minimum is not None and float(value) < minimum)
    ):
        qualifier = (
            "a finite number"
            if minimum is None
            else f"a finite number >= {minimum}"
        )
        raise JoinError(f"{context} must be {qualifier}")
    return float(value)


def _parse_utc(value: object, context: str) -> float:
    if not isinstance(value, str) or not value:
        raise JoinError(f"{context} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JoinError(f"{context} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise JoinError(f"{context} must include a timezone")
    return parsed.timestamp()


def sha256_file(path: Path, *, maximum_bytes: int) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise JoinError(f"cannot stat evidence file {path}: {exc}") from exc
    if not path.is_file():
        raise JoinError(f"evidence path is not a regular file: {path}")
    if size > maximum_bytes:
        raise JoinError(
            f"evidence file exceeds resource cap: {path} ({size} > {maximum_bytes})"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, *, maximum_bytes: int = 16 * 1024**2) -> dict:
    try:
        size = path.stat().st_size
        if size > maximum_bytes:
            raise JoinError(f"JSON evidence exceeds resource cap: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except JoinError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise JoinError(f"cannot read JSON evidence {path}: {exc}") from exc
    return _require_dict(value, str(path))


def _read_jsonl(path: Path, *, maximum_rows: int, maximum_bytes: int) -> list[dict]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise JoinError(f"cannot stat JSONL evidence {path}: {exc}") from exc
    if size > maximum_bytes:
        raise JoinError(f"JSONL evidence exceeds resource cap: {path}")
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise JoinError(f"blank JSONL row at {path}:{line_number}")
                if len(rows) >= maximum_rows:
                    raise JoinError(
                        f"JSONL row cap exceeded at {path}: {maximum_rows}"
                    )
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise JoinError(
                        f"invalid JSONL at {path}:{line_number}: {exc}"
                    ) from exc
                rows.append(_require_dict(row, f"{path}:{line_number}"))
    except OSError as exc:
        raise JoinError(f"cannot read JSONL evidence {path}: {exc}") from exc
    if not rows:
        raise JoinError(f"JSONL evidence is empty: {path}")
    return rows


def _only_event(rows: list[dict], event: str) -> tuple[int, dict]:
    matches = [
        (index, row) for index, row in enumerate(rows) if row.get("event") == event
    ]
    if len(matches) != 1:
        raise JoinError(f"expected exactly one {event!r} event, found {len(matches)}")
    return matches[0]


def _segment_event(
    rows: list[dict], event: str, sequence: int, gauge: str
) -> tuple[int, dict]:
    matches = [
        (index, row)
        for index, row in enumerate(rows)
        if row.get("event") == event and row.get("sequence") == sequence
    ]
    if len(matches) != 1:
        raise JoinError(
            f"expected exactly one {event!r} event for sequence {sequence}, "
            f"found {len(matches)}"
        )
    index, row = matches[0]
    if row.get("gauge") != gauge:
        raise JoinError(
            f"{event} sequence {sequence} label mismatch: "
            f"{row.get('gauge')!r} != {gauge!r}"
        )
    return index, row


def _artifact_offset(row: dict, filename: str, context: str) -> int:
    artifacts = _require_dict(row.get("artifacts"), f"{context}.artifacts")
    entry = _require_dict(artifacts.get(filename), f"{context}.{filename}")
    path = entry.get("path")
    if not isinstance(path, str) or Path(path).name != filename:
        raise JoinError(f"{context}.{filename} has inconsistent Android path")
    return _require_int(entry.get("size"), f"{context}.{filename}.size")


def validate_campaign(
    campaign_dir: Path,
    *,
    maximum_artifact_bytes: int,
    maximum_segments: int,
    maximum_events: int,
) -> tuple[
    CampaignPlan,
    list[SegmentEvidence],
    dict[str, Path],
    dict[str, object],
]:
    """Validate campaign completion, boundaries, and final artifact provenance."""
    if not campaign_dir.is_dir():
        raise JoinError(f"campaign directory does not exist: {campaign_dir}")
    try:
        plan = load_plan(campaign_dir / "plan.json")
    except (CampaignError, OSError, ValueError) as exc:
        raise JoinError(f"invalid singleton plan: {exc}") from exc
    if len(plan.schedule) > maximum_segments:
        raise JoinError(
            f"singleton schedule exceeds segment cap: "
            f"{len(plan.schedule)} > {maximum_segments}"
        )

    state = _read_json(campaign_dir / "state.json")
    if state.get("schema_version") != 1:
        raise JoinError("singleton state schema_version must be 1")
    if state.get("campaign_id") != plan.campaign_id:
        raise JoinError("singleton state campaign_id does not match plan")
    if state.get("phase") != "complete":
        raise JoinError(f"singleton campaign is not complete: {state.get('phase')!r}")
    if state.get("manual_reconcile") is not False:
        raise JoinError("singleton campaign requires manual reconciliation")
    if state.get("next_sequence") != len(plan.schedule):
        raise JoinError("singleton completion checkpoint has wrong next_sequence")

    events_path = campaign_dir / "events.jsonl"
    rows = _read_jsonl(
        events_path,
        maximum_rows=maximum_events,
        maximum_bytes=64 * 1024**2,
    )
    started_index, started = _only_event(rows, "campaign_started")
    complete_index, complete = _only_event(rows, "campaign_complete")
    if started_index >= complete_index or complete_index != len(rows) - 1:
        raise JoinError("campaign start/complete event ordering is invalid")
    if (
        started.get("campaign_id") != plan.campaign_id
        or started.get("module_key") != plan.module_key
    ):
        raise JoinError("campaign_started provenance does not match plan")
    if complete.get("segments") != len(plan.schedule):
        raise JoinError("campaign_complete segment count does not match plan")
    if any(row.get("event") in {"campaign_error", "artifact_finalization_error"} for row in rows):
        raise JoinError("completed campaign event log also contains a failure event")

    artifacts: dict[str, Path] = {}
    pull_events: dict[str, tuple[int, dict]] = {}
    for index, row in enumerate(rows):
        if row.get("event") != "artifact_pull":
            continue
        filename = row.get("filename")
        if not isinstance(filename, str) or filename in pull_events:
            raise JoinError("artifact_pull filenames must be unique strings")
        pull_events[filename] = (index, row)
    if set(pull_events) != set(plan.artifacts):
        missing = sorted(set(plan.artifacts) - set(pull_events))
        extra = sorted(set(pull_events) - set(plan.artifacts))
        raise JoinError(
            "artifact_pull set does not match plan artifacts: "
            f"missing={missing}, extra={extra}"
        )
    for filename in plan.artifacts:
        _pull_index, pull = pull_events[filename]
        if pull.get("source_present") is not True or pull.get("pulled") is not True:
            if filename in plan.required_segment_growth:
                raise JoinError(f"required artifact was not pulled: {filename}")
            continue
        expected_size = _require_int(pull.get("size"), f"{filename} pull size")
        expected_hash = pull.get("sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_hash
        ):
            raise JoinError(f"invalid artifact hash provenance for {filename}")
        path = campaign_dir / "android_logs" / "final" / filename
        if path.stat().st_size != expected_size:
            raise JoinError(
                f"pulled artifact size mismatch for {filename}: "
                f"{path.stat().st_size} != {expected_size}"
            )
        actual_hash = sha256_file(path, maximum_bytes=maximum_artifact_bytes)
        if actual_hash != expected_hash:
            raise JoinError(f"pulled artifact hash mismatch for {filename}")
        artifacts[filename] = path

    info_names = [name for name in plan.artifacts if name.endswith(INFO_SUFFIX)]
    if DEBUG_ARTIFACT not in artifacts:
        raise JoinError(f"campaign lacks pulled {DEBUG_ARTIFACT}")
    if len(info_names) != 1 or info_names[0] not in artifacts:
        raise JoinError(
            "campaign must contain exactly one pulled profile *_Info.log artifact"
        )
    required_names = (DEBUG_ARTIFACT, info_names[0])

    segments: list[SegmentEvidence] = []
    last_after = {name: -1 for name in required_names}
    previous_done_index = started_index
    previous_after_time: float | None = None
    first_before_time: float | None = None
    for sequence, gauge in enumerate(plan.schedule):
        selected_index, _ = _segment_event(
            rows, "singleton_selected", sequence, gauge
        )
        before_index, before = _segment_event(
            rows, "segment_offsets_before", sequence, gauge
        )
        started_segment_index, _ = _segment_event(
            rows, "segment_started", sequence, gauge
        )
        stopped_index, _ = _segment_event(
            rows, "segment_stopped_verified", sequence, gauge
        )
        after_index, after = _segment_event(
            rows, "segment_offsets_after", sequence, gauge
        )
        done_index, _ = _segment_event(rows, "segment_complete", sequence, gauge)
        if not (
            previous_done_index
            < selected_index
            < before_index
            < started_segment_index
            < stopped_index
            < after_index
            < done_index
            < complete_index
        ):
            raise JoinError(f"segment {sequence} event ordering is invalid")
        before_time = _parse_utc(
            before.get("wall_time_utc"),
            f"segment {sequence} before wall_time_utc",
        )
        after_time = _parse_utc(
            after.get("wall_time_utc"),
            f"segment {sequence} after wall_time_utc",
        )
        if after_time <= before_time:
            raise JoinError(f"segment {sequence} wall-clock interval is empty")
        if previous_after_time is not None and before_time <= previous_after_time:
            raise JoinError(
                f"segment {sequence} wall-clock interval overlaps/regresses"
            )
        if first_before_time is None:
            first_before_time = before_time
        boundaries: dict[str, ArtifactBoundary] = {}
        for filename in required_names:
            start = _artifact_offset(
                before, filename, f"segment {sequence} before"
            )
            end = _artifact_offset(after, filename, f"segment {sequence} after")
            if end <= start:
                raise JoinError(
                    f"segment {sequence} {filename} did not grow within interval"
                )
            if start < last_after[filename]:
                raise JoinError(
                    f"segment {sequence} {filename} offsets overlap/regress"
                )
            if end > artifacts[filename].stat().st_size:
                raise JoinError(
                    f"segment {sequence} {filename} offset exceeds pulled artifact"
                )
            boundaries[filename] = ArtifactBoundary(start, end)
            last_after[filename] = end
        segments.append(
            SegmentEvidence(
                sequence=sequence,
                gauge=gauge,
                before_time=before_time,
                after_time=after_time,
                boundaries=boundaries,
            )
        )
        previous_done_index = done_index
        previous_after_time = after_time

    for filename in required_names:
        outer_start = segments[0].boundaries[filename].before
        outer_end = segments[-1].boundaries[filename].after
        if outer_end <= outer_start:
            raise JoinError(
                f"buffered outer interval for {filename} did not grow"
            )

    segment_event_names = {
        "singleton_selected",
        "segment_offsets_before",
        "segment_started",
        "segment_stopped_verified",
        "segment_offsets_after",
        "segment_complete",
    }
    for row in rows:
        if row.get("event") not in segment_event_names:
            continue
        sequence = row.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 0 <= sequence < len(plan.schedule)
        ):
            raise JoinError("segment event has an out-of-range sequence")

    if any(
        not (previous_done_index < pull_index < complete_index)
        for pull_index, _pull in pull_events.values()
    ):
        raise JoinError("artifact_pull events must follow all completed segments")
    started_time = _parse_utc(
        started.get("wall_time_utc"), "campaign_started.wall_time_utc"
    )
    complete_time = _parse_utc(
        complete.get("wall_time_utc"), "campaign_complete.wall_time_utc"
    )
    assert first_before_time is not None and previous_after_time is not None
    if not (
        started_time < first_before_time
        and previous_after_time < complete_time
    ):
        raise JoinError("campaign wall-clock bounds do not enclose all segments")

    provenance = {
        "plan_sha256": sha256_file(
            campaign_dir / "plan.json", maximum_bytes=16 * 1024**2
        ),
        "state_sha256": sha256_file(
            campaign_dir / "state.json", maximum_bytes=16 * 1024**2
        ),
        "events_sha256": sha256_file(
            events_path, maximum_bytes=64 * 1024**2
        ),
        "artifact_sha256": {
            name: pull_events[name][1]["sha256"] for name in sorted(artifacts)
        },
        "info_artifact": info_names[0],
    }
    return plan, segments, artifacts, provenance


def _read_interval(
    path: Path, boundary: ArtifactBoundary, *, maximum_bytes: int
) -> bytes:
    length = boundary.after - boundary.before
    if length > maximum_bytes:
        raise JoinError(
            f"artifact interval exceeds per-segment resource cap: "
            f"{path.name} {length} > {maximum_bytes}"
        )
    with path.open("rb") as handle:
        handle.seek(boundary.before)
        payload = handle.read(length)
    if len(payload) != length:
        raise JoinError(f"short interval read from {path}")
    return payload


def _ascii_command(hex_payload: str) -> str:
    try:
        return bytes.fromhex(hex_payload).decode("latin-1")
    except ValueError:
        return ""


def _decoded_complete_lines(
    path: Path, boundary: ArtifactBoundary, *, maximum_bytes: int
) -> tuple[list[str], bytes]:
    if boundary.before % 2 or boundary.after % 2:
        raise JoinError(
            f"{DEBUG_ARTIFACT} boundaries must align to encoded hex-byte pairs"
        )
    raw = _read_interval(path, boundary, maximum_bytes=maximum_bytes)
    if any(byte not in HEX_BYTES for byte in raw):
        raise JoinError(f"{DEBUG_ARTIFACT} interval contains non-hex bytes")
    try:
        decoded = bytes(byte ^ 0xFF for byte in bytes.fromhex(raw.decode("ascii")))
    except (UnicodeDecodeError, ValueError) as exc:
        raise JoinError(f"cannot decode {DEBUG_ARTIFACT} interval") from exc

    previous_is_boundary = boundary.before == 0
    if boundary.before:
        with path.open("rb") as handle:
            handle.seek(boundary.before - 2)
            previous = handle.read(2)
        if len(previous) != 2 or any(byte not in HEX_BYTES for byte in previous):
            raise JoinError("cannot establish Debug interval's preceding byte")
        previous_decoded = bytes.fromhex(previous.decode("ascii"))[0] ^ 0xFF
        previous_is_boundary = previous_decoded in (10, 13)
    text = decoded.decode("latin-1")
    if not previous_is_boundary:
        first_breaks = [
            index
            for index in (text.find("\r"), text.find("\n"))
            if index >= 0
        ]
        text = text[min(first_breaks) + 1 :] if first_breaks else ""
    if text and text[-1] not in "\r\n":
        last_break = max(text.rfind("\r"), text.rfind("\n"))
        text = text[: last_break + 1] if last_break >= 0 else ""
    lines = re.split(r"\r\n|\r|\n", text)
    return [line for line in lines if line], raw


def _parse_debug_response_block(block: str) -> bytes | None:
    segments: list[tuple[int, str]] = []
    plain: list[str] = []
    length_header: int | None = None
    saw_segment = False
    for raw_part in re.split(r"\r\n|\r|\n", block):
        part = raw_part.strip().upper().replace(" ", "")
        if not part:
            continue
        segment = ELM_SEGMENT_RE.fullmatch(part)
        if segment:
            saw_segment = True
            index = int(segment.group(1), 16)
            encoded_part = segment.group(2)
            if len(encoded_part) % 2:
                raise JoinError(
                    "Debug response prompt has an odd-length ELM segment"
                )
            expected_index = len(segments) & 0x0F
            if index != expected_index:
                raise JoinError(
                    "Debug response prompt has an out-of-order ELM segment: "
                    f"{index:X} != {expected_index:X}"
                )
            segments.append((index, encoded_part))
            continue
        if re.fullmatch(r"[0-9A-F]{3}", part):
            if length_header is not None or saw_segment or plain:
                raise JoinError(
                    "Debug response prompt has a misplaced or duplicate "
                    "ELM length header"
                )
            length_header = int(part, 16)
            if length_header <= 0:
                raise JoinError("Debug response prompt has an empty ELM length")
            continue
        if re.fullmatch(r"[0-9A-F]{2,}", part) and len(part) % 2 == 0:
            plain.append(part)
            continue
        raise JoinError(
            "nonempty Debug response prompt is not a hexadecimal transport response"
        )
    if not segments and not plain and length_header is None:
        return None
    if length_header is not None and not segments:
        raise JoinError(
            "Debug response prompt has an ELM length header without indexed segments"
        )
    if segments and length_header is None:
        raise JoinError(
            "Debug response prompt has indexed ELM segments without a length header"
        )
    if segments and plain:
        raise JoinError("Debug response prompt mixes segmented and plain payloads")
    if segments:
        encoded = "".join(encoded_part for _index, encoded_part in segments)
    else:
        encoded = "".join(plain)
    try:
        payload = bytes.fromhex(encoded)
    except ValueError as exc:
        raise JoinError("Debug response prompt has malformed hexadecimal data") from exc
    if length_header is not None:
        if length_header < 8:
            raise JoinError(
                "Debug indexed ELM response length must be at least 8 bytes"
            )
        segment_payloads = [
            bytes.fromhex(encoded_part)
            for _index, encoded_part in segments
        ]
        if (
            len(segment_payloads) < 2
            or len(segment_payloads[0]) != 6
            or any(len(part) != 7 for part in segment_payloads[1:-1])
            or not 1 <= len(segment_payloads[-1]) <= 7
        ):
            raise JoinError(
                "Debug indexed ELM response has invalid ISO-TP row widths"
            )
        bytes_before_final = sum(
            len(part) for part in segment_payloads[:-1]
        )
        if not bytes_before_final < length_header <= len(payload):
            raise JoinError(
                "Debug indexed ELM response length does not terminate "
                "within its final row"
            )
        payload = payload[:length_header]
    return payload


def parse_debug_transport_streams(
    path: Path,
    boundary: ArtifactBoundary,
    *,
    maximum_bytes: int,
    maximum_messages: int,
) -> tuple[DebugTransportStreams, str]:
    """Parse independent S commands and prompt-delimited R responses.

    AlfaOBD writes its artifact in coarse buffers, and its transport callbacks
    can arrive in an order that makes request-owned response parsing lose valid
    records.  Keeping the two directions independent preserves the observable
    campaign prefixes without inventing per-sample pairing.
    """
    lines, raw = _decoded_complete_lines(
        path,
        boundary,
        maximum_bytes=maximum_bytes,
    )
    requests: list[bytes] = []
    responses: list[bytes] = []
    response_buffer = ""
    tester_requests = 0
    tester_responses = 0
    leading_empty_prompts = 0
    pending_trailing_empty_prompts = 0
    seen_nonempty_prompt = False

    def retain(target: list[bytes], payload: bytes, context: str) -> None:
        if len(target) >= maximum_messages:
            raise JoinError(
                f"buffered Debug {context} cap exceeded: {maximum_messages}"
            )
        target.append(payload)

    for line in lines:
        match = TRANSPORT_LINE_RE.fullmatch(line)
        if not match:
            continue
        direction, encoded = match.group(2), match.group(3)
        if len(encoded) % 2:
            raise JoinError("Debug transport callback contains odd-length hex")
        decoded = _ascii_command(encoded)
        if direction == "S":
            for raw_command in re.split(r"\r\n|\r|\n", decoded):
                command = raw_command.strip().upper().replace(" ", "")
                if not command or command.startswith(("AT", "ST")):
                    continue
                if (
                    not re.fullmatch(r"[0-9A-F]+", command)
                    or len(command) % 2
                ):
                    raise JoinError(
                        "Debug S callback contains a malformed diagnostic request"
                    )
                payload = bytes.fromhex(command)
                if payload[:1] == b"\x3e":
                    tester_requests += 1
                else:
                    retain(requests, payload, "request")
            continue

        response_buffer += decoded
        while ">" in response_buffer:
            block, response_buffer = response_buffer.split(">", 1)
            response = _parse_debug_response_block(block)
            if response is None:
                if seen_nonempty_prompt:
                    pending_trailing_empty_prompts += 1
                else:
                    leading_empty_prompts += 1
                continue
            if pending_trailing_empty_prompts:
                raise JoinError(
                    "buffered Debug stream contains an interior empty response prompt"
                )
            seen_nonempty_prompt = True
            if response[:1] == b"\x7e" or response[:2] == b"\x7f\x3e":
                tester_responses += 1
            else:
                retain(responses, response, "response")

    streams = DebugTransportStreams(
        requests=tuple(requests),
        responses=tuple(responses),
        tester_present_requests=tester_requests,
        tester_present_responses=tester_responses,
        leading_empty_prompts=leading_empty_prompts,
        trailing_empty_prompts=pending_trailing_empty_prompts,
        trailing_response_fragment_bytes=len(response_buffer.encode("latin-1")),
    )
    return streams, hashlib.sha256(raw).hexdigest()


def _plain_complete_lines(
    path: Path,
    boundary: ArtifactBoundary,
    *,
    maximum_bytes: int,
) -> tuple[list[str], bytes]:
    """Read only complete text lines from an arbitrary buffered-file interval."""
    raw = _read_interval(path, boundary, maximum_bytes=maximum_bytes)
    complete = raw
    starts_at_boundary = boundary.before == 0
    if boundary.before:
        with path.open("rb") as handle:
            handle.seek(boundary.before - 1)
            previous = handle.read(1)
        if len(previous) != 1:
            raise JoinError(f"cannot establish {path.name} interval's preceding byte")
        starts_at_boundary = previous in (b"\r", b"\n")
    if not starts_at_boundary:
        first_breaks = [
            index
            for index in (complete.find(b"\r"), complete.find(b"\n"))
            if index >= 0
        ]
        complete = complete[min(first_breaks) + 1 :] if first_breaks else b""
    if complete and complete[-1:] not in (b"\r", b"\n"):
        last_break = max(complete.rfind(b"\r"), complete.rfind(b"\n"))
        complete = complete[: last_break + 1] if last_break >= 0 else b""
    try:
        text = complete.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JoinError(f"{path.name} complete lines are not valid UTF-8") from exc
    return re.split(r"\r\n|\r|\n", text), raw


def parse_info_runs(
    path: Path,
    boundary: ArtifactBoundary,
    labels: Sequence[str],
    *,
    maximum_bytes: int,
    maximum_samples: int,
) -> tuple[list[InfoRun], str]:
    """Parse contiguous exact-label runs from one buffered outer Info interval."""
    lines, raw = _plain_complete_lines(
        path,
        boundary,
        maximum_bytes=maximum_bytes,
    )
    patterns = [
        (label, re.compile(rf"^\s*{re.escape(label)}\s*:\s*(.*?)\s*$"))
        for label in dict.fromkeys(labels)
    ]
    runs: list[InfoRun] = []
    active_label: str | None = None
    active_values: list[str] = []
    sample_count = 0

    def finish_active() -> None:
        nonlocal active_label, active_values
        if active_label is not None:
            runs.append(InfoRun(active_label, tuple(active_values)))
        active_label = None
        active_values = []

    for line in lines:
        matches = [
            (label, match)
            for label, pattern in patterns
            if (match := pattern.fullmatch(line)) is not None
        ]
        if not matches:
            parameter_row = INFO_PARAMETER_ROW_RE.fullmatch(line)
            if parameter_row is not None:
                raise JoinError(
                    "unexpected parameter-shaped Info row for label "
                    f"{parameter_row.group(1)!r}"
                )
            finish_active()
            continue
        if len(matches) != 1:
            raise JoinError(f"ambiguous exact Info label line: {line!r}")
        label, match = matches[0]
        value = match.group(1)
        if not value:
            raise JoinError(f"empty rendered value for singleton label {label!r}")
        if sample_count >= maximum_samples:
            raise JoinError(
                f"buffered Info sample cap exceeded: {maximum_samples}"
            )
        sample_count += 1
        if active_label != label:
            finish_active()
            active_label = label
        active_values.append(value)
    finish_active()
    return runs, hashlib.sha256(raw).hexdigest()


def parse_candump_frame(line: bytes) -> tuple[float, int, bytes, str] | None:
    match = CANDUMP_RE.fullmatch(line.rstrip(b"\r\n"))
    if not match:
        return None
    try:
        timestamp = float(match.group("timestamp"))
        can_id = int(match.group("can_id"), 16)
        data = bytes.fromhex(match.group("data").decode("ascii"))
        channel = match.group("channel").decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    if (
        not math.isfinite(timestamp)
        or not 0 <= can_id <= 0x1FFFFFFF
        or len(data) > 8
        or CHANNEL_RE.fullmatch(channel) is None
    ):
        return None
    # Preserve the established tuple prefix for callers that only consume
    # timestamp/identifier/payload while adding the load-bearing wire channel.
    return timestamp, can_id, data, channel


def reassemble_wire_messages(
    lines: Iterable[bytes],
    *,
    selected_ids: frozenset[int],
    maximum_messages: int,
    maximum_payload_bytes: int = DEFAULT_MAX_WIRE_PAYLOAD_BYTES,
    minimum_timestamp: float | None = None,
    maximum_timestamp: float | None = None,
) -> list[WireMessage]:
    """Reassemble classic-CAN normal-addressed ISO-TP for 11- or 29-bit IDs."""
    frames: list[WireFrame] = []
    for line in lines:
        parsed = parse_candump_frame(line)
        if parsed is None:
            continue
        timestamp, can_id, data, channel = parsed
        if minimum_timestamp is not None and timestamp < minimum_timestamp:
            continue
        if maximum_timestamp is not None and timestamp > maximum_timestamp:
            continue
        if can_id in selected_ids and data:
            frames.append(
                WireFrame(
                    timestamp=timestamp,
                    can_id=can_id,
                    data=data,
                    source_index=0,
                    channel=channel,
                )
            )
    return reassemble_wire_frames(
        frames,
        maximum_messages=maximum_messages,
        maximum_payload_bytes=maximum_payload_bytes,
    )


def reassemble_wire_frames(
    frames: Iterable[WireFrame],
    *,
    maximum_messages: int,
    maximum_payload_bytes: int = DEFAULT_MAX_WIRE_PAYLOAD_BYTES,
) -> list[WireMessage]:
    """Reassemble an already validated, chronological, de-duplicated frame stream."""
    pending: dict[tuple[str, int], _IsoTpPending] = {}
    messages: list[WireMessage] = []
    retained_payload_bytes = 0
    previous_timestamp: float | None = None

    wire_channel: str | None = None

    def emit(
        channel: str,
        can_id: int,
        payload: bytes,
        first: float,
        last: float,
    ) -> None:
        nonlocal retained_payload_bytes
        if len(messages) >= maximum_messages:
            raise JoinError(f"wire-message cap exceeded: {maximum_messages}")
        retained_payload_bytes += len(payload)
        if retained_payload_bytes > maximum_payload_bytes:
            raise JoinError(
                f"wire-payload retention cap exceeded: {maximum_payload_bytes}"
            )
        messages.append(WireMessage(can_id, payload, first, last, channel))

    for frame in frames:
        timestamp, can_id, data = frame.timestamp, frame.can_id, frame.data
        channel = frame.channel
        if CHANNEL_RE.fullmatch(channel) is None:
            raise JoinError(f"invalid or missing wire channel {channel!r}")
        if wire_channel is None:
            wire_channel = channel
        elif channel != wire_channel:
            raise JoinError(
                "mixed wire channels cannot be reassembled or merged: "
                f"{wire_channel!r} and {channel!r}"
            )
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise JoinError("merged wire frames are not chronological")
        previous_timestamp = timestamp
        if not data:
            raise JoinError(f"empty selected CAN frame on 0x{can_id:X}")
        stream_key = (channel, can_id)
        frame_type = data[0] >> 4
        if frame_type == 0:
            if stream_key in pending:
                raise JoinError(
                    f"wire ISO-TP single frame interrupted pending message on 0x{can_id:X}"
                )
            length = data[0] & 0x0F
            if length == 0 or length > len(data) - 1:
                raise JoinError(f"invalid wire ISO-TP single frame on 0x{can_id:X}")
            emit(
                channel,
                can_id,
                data[1 : 1 + length],
                timestamp,
                timestamp,
            )
        elif frame_type == 1:
            if len(data) < 3:
                raise JoinError(f"short wire ISO-TP first frame on 0x{can_id:X}")
            if stream_key in pending:
                raise JoinError(
                    f"wire ISO-TP first frame interrupted pending message on 0x{can_id:X}"
                )
            total = ((data[0] & 0x0F) << 8) | data[1]
            if total <= len(data) - 2:
                raise JoinError(f"invalid wire ISO-TP first-frame length on 0x{can_id:X}")
            pending[stream_key] = _IsoTpPending(
                total_length=total,
                payload=bytearray(data[2:]),
                next_sequence=1,
                first_timestamp=timestamp,
            )
        elif frame_type == 2:
            state = pending.get(stream_key)
            if state is None:
                raise JoinError(
                    f"wire ISO-TP consecutive frame without first frame on 0x{can_id:X}"
                )
            sequence = data[0] & 0x0F
            if sequence != state.next_sequence:
                raise JoinError(
                    f"wire ISO-TP sequence mismatch on 0x{can_id:X}: "
                    f"{sequence} != {state.next_sequence}"
                )
            state.payload.extend(data[1:])
            state.next_sequence = (state.next_sequence + 1) & 0x0F
            if len(state.payload) >= state.total_length:
                emit(
                    channel,
                    can_id,
                    bytes(state.payload[: state.total_length]),
                    state.first_timestamp,
                    timestamp,
                )
                del pending[stream_key]
        elif frame_type == 3:
            # FlowControl is transport metadata, not a UDS payload.
            continue
        else:
            raise JoinError(f"unsupported wire ISO-TP PCI on 0x{can_id:X}")
    if pending:
        identifiers = ", ".join(
            f"{channel}:0x{can_id:X}" for channel, can_id in sorted(pending)
        )
        raise JoinError(f"incomplete wire ISO-TP message(s): {identifiers}")
    return messages


def _manifest_stream_path(capture_dir: Path, stream: dict) -> Path:
    raw_path = stream.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise JoinError("completed capture stream lacks a path")
    path = Path(raw_path)
    if not path.is_absolute():
        path = capture_dir / path
    try:
        resolved_root = capture_dir.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise JoinError(f"capture stream is unavailable: {path}") from exc
    try:
        inside = os.path.commonpath((str(resolved_root), str(resolved))) == str(
            resolved_root
        )
    except ValueError:
        inside = False
    if not inside:
        raise JoinError(f"manifest stream escapes capture directory: {path}")
    return resolved


def iter_capture_lines(
    path: Path,
    *,
    byte_budget: list[int],
    maximum_bytes: int,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> Iterator[bytes]:
    """Yield plain candump lines from recorder .zst chunks or plain test/import files."""
    process: subprocess.Popen | None = None
    completed = False
    handle: BinaryIO
    if path.name.endswith(".zst"):
        executable = shutil.which("zstd")
        if executable is None:
            raise JoinError("zstd is required to read passive recorder chunks")
        try:
            process = popen(
                [executable, "-dc", "--", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise JoinError(f"cannot start zstd for {path}: {exc}") from exc
        if process.stdout is None:
            process.kill()
            process.wait()
            raise JoinError("zstd stdout pipe was not created")
        handle = process.stdout
    else:
        try:
            handle = path.open("rb")
        except OSError as exc:
            raise JoinError(f"cannot read capture stream {path}: {exc}") from exc
    try:
        for line in handle:
            byte_budget[0] += len(line)
            if byte_budget[0] > maximum_bytes:
                if process is not None and process.poll() is None:
                    process.kill()
                raise JoinError(
                    f"decompressed wire-data cap exceeded: {maximum_bytes}"
                )
            if len(line) > 4096:
                raise JoinError(f"unreasonably long candump line in {path}")
            yield line
        completed = True
    finally:
        handle.close()
        if process is not None:
            if not completed and process.poll() is None:
                process.terminate()
            try:
                returncode = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                returncode = -9
            if completed and returncode != 0:
                raise JoinError(
                    f"zstd failed while reading {path} with status {returncode}"
                )


def _validate_expected_hash(
    actual: str, expected: str | None, context: str
) -> None:
    if expected is not None and actual != expected:
        raise JoinError(f"{context} hash does not match capture-set declaration")


def _route_metadata_records(
    run: dict,
    checkpoint: dict,
    manifest: Sequence[dict],
) -> Iterator[tuple[str, dict]]:
    """Yield every recorder metadata object that may repeat route identity."""

    yield "run", run
    interface = run.get("interface")
    if isinstance(interface, dict):
        yield "run.interface", interface
    yield "checkpoint", checkpoint
    for index, row in enumerate(manifest):
        yield f"manifest[{index}]", row
        streams = row.get("streams")
        if not isinstance(streams, dict):
            continue
        for name, stream in streams.items():
            if isinstance(stream, dict):
                yield f"manifest[{index}].streams[{name!r}]", stream


def validate_capture_route_metadata(
    *,
    run: dict,
    checkpoint: dict,
    manifest: Sequence[dict],
    module_key: str,
) -> dict[str, object]:
    """Validate every available route claim against the registered module.

    Older, hash-pinned recorder evidence may predate explicit logical-role
    fields, so absence remains representable.  Any field that *is* present is
    authoritative and must agree across the capture and with the module's
    canonical physical role.
    """

    module = MODULES[module_key]
    try:
        expected_role = normalize_can_role(module.bus)
    except ValueError as exc:
        raise JoinError(
            f"module {module_key!r} has an invalid logical bus {module.bus!r}"
        ) from exc
    role_spec = ROLE_SPECS.get(expected_role)
    if role_spec is None or role_spec.pair is None:
        raise JoinError(
            f"module {module_key!r} has no canonical connected-bus route"
        )
    if role_spec.bitrate != module.bitrate:
        raise JoinError(
            f"module {module_key!r} bitrate disagrees with canonical role"
        )

    channels: set[str] = set()
    topology_fingerprints: set[str] = set()
    present_fields: set[str] = set()
    for context, record in _route_metadata_records(run, checkpoint, manifest):
        if "logical_bus" in record:
            present_fields.add("logical_bus")
            value = record.get("logical_bus")
            try:
                logical_bus = normalize_can_role(value)  # type: ignore[arg-type]
            except ValueError as exc:
                raise JoinError(
                    f"{context}.logical_bus is not a valid installed bus role"
                ) from exc
            if logical_bus != expected_role:
                raise JoinError(
                    f"{context}.logical_bus {logical_bus!r} does not match "
                    f"module {module_key!r} bus {expected_role!r}"
                )
        if "physical_pair" in record:
            present_fields.add("physical_pair")
            pair = record.get("physical_pair")
            if pair != role_spec.pair:
                raise JoinError(
                    f"{context}.physical_pair {pair!r} does not match "
                    f"module {module_key!r} pair {role_spec.pair!r}"
                )
        if "bitrate" in record:
            present_fields.add("bitrate")
            if record.get("bitrate") != module.bitrate:
                raise JoinError(
                    f"{context}.bitrate does not match module {module_key!r}"
                )
        if "channel" in record:
            present_fields.add("channel")
            channel = record.get("channel")
            if not isinstance(channel, str) or CHANNEL_RE.fullmatch(channel) is None:
                raise JoinError(f"{context}.channel is not a valid interface name")
            channels.add(channel)
        if "topology_fingerprint" in record:
            present_fields.add("topology_fingerprint")
            fingerprint = record.get("topology_fingerprint")
            if (
                not isinstance(fingerprint, str)
                or TOPOLOGY_FINGERPRINT_RE.fullmatch(fingerprint) is None
            ):
                raise JoinError(
                    f"{context}.topology_fingerprint is not a canonical fingerprint"
                )
            topology_fingerprints.add(fingerprint)

    if len(channels) > 1:
        raise JoinError(
            "capture metadata declares mixed wire channels: "
            + ", ".join(sorted(channels))
        )
    if len(topology_fingerprints) > 1:
        raise JoinError("capture metadata contains conflicting topology fingerprints")
    return {
        "expected_logical_bus": expected_role,
        "expected_physical_pair": role_spec.pair,
        "expected_bitrate": module.bitrate,
        "declared_channel": next(iter(channels), None),
        "topology_fingerprint": next(iter(topology_fingerprints), None),
        "metadata_fields_present": sorted(present_fields),
    }


def validate_capture_evidence(
    spec: CaptureSpec,
    *,
    source_index: int,
    plan: CampaignPlan,
    segments: list[SegmentEvidence],
    maximum_manifest_rows: int,
    maximum_artifact_bytes: int,
    maximum_wire_bytes: int,
    maximum_wire_messages: int,
    byte_budget: list[int],
    frame_budget: list[int],
) -> ValidatedCapture:
    capture_dir = spec.directory
    if not capture_dir.is_dir():
        raise JoinError(f"passive capture directory does not exist: {capture_dir}")
    run_path = capture_dir / "run.json"
    manifest_path = capture_dir / "manifest.jsonl"
    checkpoint_path = capture_dir / "checkpoint.json"
    run_hash = sha256_file(run_path, maximum_bytes=16 * 1024**2)
    checkpoint_hash = sha256_file(
        checkpoint_path, maximum_bytes=16 * 1024**2
    )
    manifest_hash = sha256_file(
        manifest_path, maximum_bytes=64 * 1024**2
    )
    _validate_expected_hash(run_hash, spec.run_sha256, f"{spec.run_id} run.json")
    _validate_expected_hash(
        checkpoint_hash,
        spec.checkpoint_sha256,
        f"{spec.run_id} checkpoint.json",
    )
    _validate_expected_hash(
        manifest_hash,
        spec.manifest_sha256,
        f"{spec.run_id} manifest.jsonl",
    )
    run = _read_json(run_path)
    checkpoint = _read_json(checkpoint_path)
    manifest = _read_jsonl(
        manifest_path,
        maximum_rows=maximum_manifest_rows,
        maximum_bytes=64 * 1024**2,
    )
    if run.get("type") != "run_metadata":
        raise JoinError("passive run.json is not run_metadata")
    if run.get("campaign") != spec.run_id:
        raise JoinError(
            f"passive capture run id does not match declared id {spec.run_id!r}"
        )
    if run.get("interaction") != "passive_receive_only":
        raise JoinError("capture provenance is not passive_receive_only")
    interface = _require_dict(run.get("interface"), "run.interface")
    module = MODULES[plan.module_key]
    route_validation = validate_capture_route_metadata(
        run=run,
        checkpoint=checkpoint,
        manifest=manifest,
        module_key=plan.module_key,
    )
    if (
        interface.get("up") is not True
        or interface.get("listen_only") is not True
        or interface.get("controller_state") != "ERROR-ACTIVE"
        or interface.get("bitrate") != module.bitrate
    ):
        raise JoinError("capture interface provenance does not match module/passive state")
    if checkpoint.get("status") != "complete":
        raise JoinError("passive capture checkpoint is not complete")

    starts = [row for row in manifest if row.get("type") == "capture_start"]
    ends = [row for row in manifest if row.get("type") == "capture_end"]
    chunks = [row for row in manifest if row.get("type") == "chunk"]
    if len(starts) != 1 or len(ends) != 1 or not chunks:
        raise JoinError("passive manifest must have one start/end and at least one chunk")
    if (
        starts[0] is not manifest[0]
        or manifest.index(starts[0]) >= manifest.index(ends[0])
        or ends[0] is not manifest[-1]
    ):
        raise JoinError("passive manifest start/end ordering is invalid")
    end = ends[0]
    if (
        end.get("success") is not True
        or end.get("reason") != "duration_complete"
        or end.get("duration_complete") is not True
        or end.get("detected_socket_drops") != 0
        or end.get("signal_number") is not None
        or end.get("error") is not None
    ):
        raise JoinError(
            "passive capture was not a successful duration-complete zero-drop run"
        )
    checkpoint_fields = (
        "type",
        "time_utc",
        "reason",
        "success",
        "duration_complete",
        "signal_number",
        "full_stream_complete",
        "requested_duration_seconds",
        "elapsed_seconds",
        "error",
        "free_bytes",
        "detected_socket_drops",
    )
    if any(checkpoint.get(field) != end.get(field) for field in checkpoint_fields):
        raise JoinError("passive checkpoint does not agree with capture_end")
    requested_duration = _require_int(
        end.get("requested_duration_seconds"),
        "capture_end.requested_duration_seconds",
        minimum=1,
    )
    elapsed_seconds = _require_finite_number(
        end.get("elapsed_seconds"),
        "capture_end.elapsed_seconds",
        minimum=0.0,
    )
    if elapsed_seconds < requested_duration:
        raise JoinError(
            "passive capture elapsed_seconds is shorter than its requested duration"
        )
    if run.get("duration_seconds") != requested_duration:
        raise JoinError("run duration does not agree with capture_end")
    start_time = _parse_utc(
        starts[0].get("time_utc"), "capture_start.time_utc"
    )
    end_time = _parse_utc(end.get("time_utc"), "capture_end.time_utc")
    if end_time <= start_time:
        raise JoinError("passive capture wall-clock interval is empty")
    if not math.isclose(
        elapsed_seconds,
        end_time - start_time,
        rel_tol=0.0,
        abs_tol=1.0,
    ):
        raise JoinError(
            "passive capture elapsed_seconds disagrees with its wall-clock interval"
        )
    if any(row.get("type") == "socket_drop" for row in manifest):
        raise JoinError("passive capture manifest contains a socket-drop record")

    by_sequence: dict[int, dict] = {}
    coverage_by_sequence: dict[int, tuple[float, float, int]] = {}
    for row in chunks:
        sequence = _require_int(row.get("sequence"), "chunk.sequence")
        if sequence in by_sequence:
            raise JoinError(f"duplicate passive chunk sequence {sequence}")
        if row.get("complete") is not True:
            raise JoinError(f"passive chunk {sequence} is incomplete")
        first = _require_finite_number(
            row.get("first_frame_timestamp"),
            f"chunk {sequence}.first_frame_timestamp",
        )
        last = _require_finite_number(
            row.get("last_frame_timestamp"),
            f"chunk {sequence}.last_frame_timestamp",
        )
        if last < first:
            raise JoinError(f"passive chunk {sequence} has invalid frame timestamps")
        chunk_started = _parse_utc(
            row.get("started_utc"),
            f"chunk {sequence}.started_utc",
        )
        chunk_ended = _parse_utc(
            row.get("ended_utc"),
            f"chunk {sequence}.ended_utc",
        )
        chunk_elapsed = _require_finite_number(
            row.get("elapsed_seconds"),
            f"chunk {sequence}.elapsed_seconds",
            minimum=0.0,
        )
        wall_elapsed = chunk_ended - chunk_started
        if wall_elapsed <= 0 or chunk_elapsed <= 0:
            raise JoinError(f"passive chunk {sequence} has an empty time interval")
        if not math.isclose(
            chunk_elapsed,
            wall_elapsed,
            rel_tol=0.0,
            abs_tol=1.0,
        ):
            raise JoinError(
                f"passive chunk {sequence} elapsed_seconds disagrees with "
                "started_utc/ended_utc"
            )
        if first < chunk_started - 1.0 or last > chunk_ended + 1.0:
            raise JoinError(
                f"passive chunk {sequence} frame timestamps fall outside "
                "its recorded time interval"
            )
        if (
            chunk_started < start_time - 1.0
            or chunk_ended > end_time + 1.0
        ):
            raise JoinError(
                f"passive chunk {sequence} falls outside the capture interval"
            )
        by_sequence[sequence] = row
        coverage_by_sequence[sequence] = (
            chunk_started,
            chunk_ended,
            sequence,
        )
    ordered_sequences = sorted(by_sequence)
    if ordered_sequences != list(range(len(ordered_sequences))):
        raise JoinError("passive chunk sequences are not contiguous from zero")
    for previous, current in zip(ordered_sequences, ordered_sequences[1:]):
        previous_start, previous_end, _ = coverage_by_sequence[previous]
        current_start, current_end, _ = coverage_by_sequence[current]
        if current_start <= previous_start or current_end <= previous_end:
            raise JoinError(
                f"passive chunk {current} time bounds do not advance by sequence"
            )
    run_coverage_start, run_coverage_end, _ = coverage_by_sequence[
        ordered_sequences[0]
    ]
    for sequence in ordered_sequences[1:]:
        chunk_start, chunk_end, _ = coverage_by_sequence[sequence]
        if chunk_start > run_coverage_end:
            raise JoinError(
                f"passive capture has an internal chunk coverage gap before "
                f"chunk {sequence}"
            )
        run_coverage_end = max(run_coverage_end, chunk_end)
    if (
        run_coverage_start > start_time + 1.0
        or run_coverage_end < end_time - 1.0
    ):
        raise JoinError(
            "passive chunk coverage does not span the completed capture interval"
        )

    evidence_start = min(segment.before_time for segment in segments)
    evidence_end = max(segment.after_time for segment in segments)
    coverage_intervals = tuple(
        coverage_by_sequence[sequence] for sequence in ordered_sequences
    )
    first_frame_timestamp = min(
        float(by_sequence[sequence]["first_frame_timestamp"])
        for sequence in ordered_sequences
    )
    last_frame_timestamp = max(
        float(by_sequence[sequence]["last_frame_timestamp"])
        for sequence in ordered_sequences
    )
    overlapping_positions = [
        position
        for position, sequence in enumerate(ordered_sequences)
        if coverage_by_sequence[sequence][1] >= evidence_start
        and coverage_by_sequence[sequence][0] <= evidence_end
    ]
    if not overlapping_positions:
        raise JoinError("no passive chunk overlaps the singleton event interval")
    # Read one complete surrounding chunk on each side.  ISO-TP is reassembled
    # only after all capture runs are validated and merged, so an FF/CF pair at
    # an evidence or recorder-rotation boundary is not clipped prematurely.
    selected_positions = range(
        max(0, min(overlapping_positions) - 1),
        min(len(ordered_sequences), max(overlapping_positions) + 2),
    )
    selected_chunks = [
        by_sequence[ordered_sequences[position]] for position in selected_positions
    ]

    raw_priority_ids = run.get("priority_ids")
    if not isinstance(raw_priority_ids, list):
        raise JoinError("run.priority_ids must be a list")
    priority_ids: set[int] = set()
    for value in raw_priority_ids:
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"0[xX][0-9A-Fa-f]{1,8}", value)
        ):
            raise JoinError("run.priority_ids contains an invalid CAN ID")
        parsed_id = int(value, 16)
        if parsed_id > 0x1FFFFFFF:
            raise JoinError("run.priority_ids contains an out-of-range CAN ID")
        priority_ids.add(parsed_id)
    expected_ids = {module.txid, module.rxid}
    can_use_priority = expected_ids <= priority_ids and all(
        _require_dict(row.get("streams"), "chunk.streams").get("priority")
        for row in selected_chunks
    )
    stream_kind = "priority" if can_use_priority else "full"
    selected_paths: list[Path] = []
    selected_records: list[dict] = []
    seen_paths: set[Path] = set()
    for row in selected_chunks:
        streams = _require_dict(row.get("streams"), "chunk.streams")
        stream = _require_dict(
            streams.get(stream_kind),
            f"chunk {row['sequence']} {stream_kind} stream",
        )
        if stream.get("complete") is not True or stream.get("zstd_exit") not in (None, 0):
            raise JoinError(
                f"chunk {row['sequence']} {stream_kind} stream is incomplete"
            )
        path = _manifest_stream_path(capture_dir, stream)
        if path in seen_paths:
            raise JoinError(f"capture stream path is reused by multiple chunks: {path}")
        seen_paths.add(path)
        compressed_bytes = _require_int(
            stream.get("compressed_bytes"),
            f"chunk {row['sequence']} compressed_bytes",
        )
        if path.stat().st_size != compressed_bytes:
            raise JoinError(f"capture stream size mismatch: {path}")
        expected_hash = stream.get("sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_hash
        ):
            raise JoinError(f"capture stream lacks a valid hash: {path}")
        if sha256_file(path, maximum_bytes=maximum_artifact_bytes) != expected_hash:
            raise JoinError(f"capture stream hash mismatch: {path}")
        selected_paths.append(path)
        selected_records.append(
            {
                "sequence": row["sequence"],
                "path": path.name,
                "sha256": expected_hash,
                "compressed_bytes": compressed_bytes,
                "first_frame_timestamp": row["first_frame_timestamp"],
                "last_frame_timestamp": row["last_frame_timestamp"],
                "started_utc": row["started_utc"],
                "ended_utc": row["ended_utc"],
                "elapsed_seconds": row["elapsed_seconds"],
            }
        )

    frames: list[WireFrame] = []
    budget_before = byte_budget[0]
    previous_timestamp: float | None = None
    observed_channel: str | None = None
    declared_channel = route_validation["declared_channel"]
    assert declared_channel is None or isinstance(declared_channel, str)
    maximum_frames = min(
        maximum_wire_messages * 64,
        HARD_MAX_WIRE_FRAMES,
    )
    for path, chunk_row in zip(selected_paths, selected_chunks):
        chunk_first = float(chunk_row["first_frame_timestamp"])
        chunk_last = float(chunk_row["last_frame_timestamp"])
        for line in iter_capture_lines(
            path,
            byte_budget=byte_budget,
            maximum_bytes=maximum_wire_bytes,
        ):
            parsed = parse_candump_frame(line)
            if parsed is None:
                raise JoinError(
                    f"hash-verified recorder stream contains a malformed "
                    f"candump line: {path.name}"
                )
            timestamp, can_id, data, channel = parsed
            if observed_channel is None:
                observed_channel = channel
            elif channel != observed_channel:
                raise JoinError(
                    f"capture {spec.run_id!r} contains mixed candump channels "
                    f"{observed_channel!r} and {channel!r}"
                )
            if declared_channel is not None and channel != declared_channel:
                raise JoinError(
                    f"capture {spec.run_id!r} wire channel {channel!r} does not "
                    f"match declared channel {declared_channel!r}"
                )
            if timestamp < chunk_first - 1e-6 or timestamp > chunk_last + 1e-6:
                raise JoinError(
                    f"hash-verified recorder stream frame falls outside "
                    f"chunk {chunk_row['sequence']} manifest bounds"
                )
            if previous_timestamp is not None and timestamp < previous_timestamp:
                raise JoinError(
                    f"capture {spec.run_id!r} selected stream frames are "
                    "not chronological"
                )
            previous_timestamp = timestamp
            if stream_kind == "priority" and can_id not in priority_ids:
                raise JoinError(
                    "hash-verified priority stream contains an undeclared CAN ID"
                )
            if can_id not in (module.txid, module.rxid):
                continue
            if not data:
                raise JoinError(
                    "hash-verified recorder stream contains an empty module "
                    f"diagnostic frame on 0x{can_id:X}"
                )
            if frame_budget[0] >= maximum_frames:
                raise JoinError(
                    f"whole-evidence wire-frame cap exceeded while reading "
                    f"capture {spec.run_id!r}: "
                    f"{maximum_frames}"
                )
            frames.append(
                WireFrame(
                    timestamp=timestamp,
                    can_id=can_id,
                    data=data,
                    source_index=source_index,
                    channel=channel,
                )
            )
            frame_budget[0] += 1
    if not frames:
        raise JoinError("selected passive stream contains no module diagnostic frames")
    assert observed_channel is not None
    provenance = {
        "run_id": spec.run_id,
        "run_sha256": run_hash,
        "checkpoint_sha256": checkpoint_hash,
        "manifest_sha256": manifest_hash,
        "stream_kind": stream_kind,
        "decompressed_bytes_read": byte_budget[0] - budget_before,
        "selected_frame_count": len(frames),
        "wire_channel": observed_channel,
        "route_validation": {
            **route_validation,
            "wire_channel_matches_declared": (
                declared_channel is None or observed_channel == declared_channel
            ),
            "all_hash_verified_rows_single_channel": True,
        },
        "coverage": {
            "first_frame_timestamp": first_frame_timestamp,
            "last_frame_timestamp": last_frame_timestamp,
            "capture_start_epoch": start_time,
            "capture_end_epoch": end_time,
            "chunk_intervals": [
                {
                    "sequence": sequence,
                    "start": interval_start,
                    "end": interval_end,
                }
                for interval_start, interval_end, sequence in coverage_intervals
            ],
        },
        "chunks": selected_records,
        "capture_end": {
            "reason": "duration_complete",
            "success": True,
            "duration_complete": True,
            "full_stream_complete": end.get("full_stream_complete"),
            "detected_socket_drops": 0,
        },
    }
    return ValidatedCapture(
        spec=spec,
        frames=tuple(frames),
        coverage_intervals=coverage_intervals,
        provenance=provenance,
    )


def validate_capture(
    capture_dir: Path,
    *,
    plan: CampaignPlan,
    segments: list[SegmentEvidence],
    maximum_manifest_rows: int,
    maximum_artifact_bytes: int,
    maximum_wire_bytes: int,
    maximum_wire_messages: int,
    maximum_wire_payload_bytes: int,
) -> tuple[list[WireMessage], dict[str, object]]:
    """Backward-compatible validator for one exactly campaign-named capture."""
    evidence = validate_capture_evidence(
        CaptureSpec(capture_dir, plan.campaign_id),
        source_index=0,
        plan=plan,
        segments=segments,
        maximum_manifest_rows=maximum_manifest_rows,
        maximum_artifact_bytes=maximum_artifact_bytes,
        maximum_wire_bytes=maximum_wire_bytes,
        maximum_wire_messages=maximum_wire_messages,
        byte_budget=[0],
        frame_budget=[0],
    )
    wire_messages = reassemble_wire_frames(
        evidence.frames,
        maximum_messages=min(maximum_wire_messages, HARD_MAX_WIRE_MESSAGES),
        maximum_payload_bytes=min(
            maximum_wire_payload_bytes,
            HARD_MAX_WIRE_PAYLOAD_BYTES,
        ),
    )
    return wire_messages, evidence.provenance


def load_capture_set(
    path: Path,
    *,
    singleton_campaign_id: str,
    maximum_runs: int = DEFAULT_MAX_CAPTURE_RUNS,
) -> tuple[list[CaptureSpec], dict[str, object]]:
    """Load an explicit, hash-pinned binding between one singleton and recorder runs."""
    payload = _read_json(path, maximum_bytes=4 * 1024**2)
    if payload.get("schema_version") != 1:
        raise JoinError("capture-set schema_version must be 1")
    if payload.get("singleton_campaign_id") != singleton_campaign_id:
        raise JoinError("capture-set singleton_campaign_id does not match campaign")
    entries = payload.get("captures")
    if not isinstance(entries, list) or not entries:
        raise JoinError("capture-set captures must be a non-empty list")
    if len(entries) > maximum_runs:
        raise JoinError(
            f"capture-set run cap exceeded: {len(entries)} > {maximum_runs}"
        )
    try:
        base = path.resolve(strict=True).parent
    except OSError as exc:
        raise JoinError(f"capture-set file is unavailable: {path}") from exc

    specs: list[CaptureSpec] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    declared: list[dict[str, object]] = []
    for index, raw_entry in enumerate(entries):
        entry = _require_dict(raw_entry, f"capture-set captures[{index}]")
        run_id = entry.get("run_id")
        raw_path = entry.get("path")
        if (
            not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", run_id)
        ):
            raise JoinError(
                f"capture-set captures[{index}].run_id is not a safe exact run id"
            )
        if not isinstance(raw_path, str) or not raw_path:
            raise JoinError(
                f"capture-set captures[{index}].path must be a non-empty string"
            )
        directory = Path(raw_path)
        if not directory.is_absolute():
            directory = base / directory
        try:
            directory = directory.resolve(strict=True)
        except OSError as exc:
            raise JoinError(
                f"capture-set run directory is unavailable: {raw_path}"
            ) from exc
        if not directory.is_dir():
            raise JoinError(f"capture-set path is not a directory: {directory}")
        if run_id in seen_ids:
            raise JoinError(f"capture-set repeats run_id {run_id!r}")
        if directory in seen_paths:
            raise JoinError(f"capture-set repeats path {directory}")
        seen_ids.add(run_id)
        seen_paths.add(directory)

        hashes: dict[str, str] = {}
        for field in ("run_sha256", "checkpoint_sha256", "manifest_sha256"):
            value = entry.get(field)
            if not isinstance(value, str) or not re.fullmatch(
                r"[0-9a-f]{64}", value
            ):
                raise JoinError(
                    f"capture-set captures[{index}].{field} must be a lowercase SHA-256"
                )
            hashes[field] = value
        specs.append(
            CaptureSpec(
                directory=directory,
                run_id=run_id,
                run_sha256=hashes["run_sha256"],
                checkpoint_sha256=hashes["checkpoint_sha256"],
                manifest_sha256=hashes["manifest_sha256"],
            )
        )
        declared.append(
            {
                "run_id": run_id,
                "path": str(directory),
                **hashes,
            }
        )
    return specs, {
        "mode": "capture_set",
        "capture_set_sha256": sha256_file(path, maximum_bytes=4 * 1024**2),
        "singleton_campaign_id": singleton_campaign_id,
        "declared_captures": declared,
    }


def merge_capture_frames(
    captures: Sequence[ValidatedCapture],
) -> tuple[list[WireFrame], dict[str, object]]:
    """Merge recorder views, de-duplicating only identical cross-run observations."""
    channels = {frame.channel for capture in captures for frame in capture.frames}
    if len(channels) != 1:
        raise JoinError(
            "capture set contains mixed wire channels and cannot be merged: "
            + ", ".join(sorted(channels))
        )
    wire_channel = next(iter(channels))
    topology_fingerprints = {
        fingerprint
        for capture in captures
        if isinstance(capture.provenance.get("route_validation"), dict)
        for fingerprint in (
            capture.provenance["route_validation"].get("topology_fingerprint"),
        )
        if fingerprint is not None
    }
    if len(topology_fingerprints) > 1:
        raise JoinError(
            "capture set route metadata contains conflicting topology fingerprints"
        )
    records = sorted(
        (frame for capture in captures for frame in capture.frames),
        key=lambda frame: (
            frame.timestamp,
            frame.channel,
            frame.can_id,
            frame.data,
            frame.source_index,
        ),
    )
    merged: list[WireFrame] = []
    duplicate_observations = 0
    index = 0
    while index < len(records):
        first = records[index]
        end = index + 1
        while (
            end < len(records)
            and records[end].timestamp == first.timestamp
            and records[end].channel == first.channel
            and records[end].can_id == first.can_id
        ):
            end += 1
        group = records[index:end]
        payloads = {frame.data for frame in group}
        if len(payloads) != 1:
            raise JoinError(
                "overlapping captures disagree at "
                f"{first.timestamp:.6f} on 0x{first.can_id:X}"
            )
        sources = [frame.source_index for frame in group]
        if len(set(sources)) != len(sources):
            raise JoinError(
                "one capture contains a duplicate frame timestamp/id observation at "
                f"{first.timestamp:.6f} on 0x{first.can_id:X}"
            )
        merged.append(
            WireFrame(
                timestamp=first.timestamp,
                can_id=first.can_id,
                data=first.data,
                source_index=min(sources),
                channel=first.channel,
            )
        )
        duplicate_observations += len(group) - 1
        index = end
    return merged, {
        "source_frame_count": len(records),
        "merged_frame_count": len(merged),
        "exact_overlap_observations_deduplicated": duplicate_observations,
        "wire_channel": wire_channel,
        "topology_fingerprint": next(iter(topology_fingerprints), None),
    }


def validate_continuous_coverage(
    captures: Sequence[ValidatedCapture],
    segments: Sequence[SegmentEvidence],
) -> list[dict[str, object]]:
    """Require one uninterrupted union of validated recorder chunk intervals."""
    intervals = sorted(
        (start, end, capture.spec.run_id, sequence)
        for capture in captures
        for start, end, sequence in capture.coverage_intervals
    )
    merged: list[list[object]] = []
    for start, end, run_id, sequence in intervals:
        member = {
            "run_id": run_id,
            "chunk_sequence": sequence,
        }
        if not merged or start > float(merged[-1][1]):
            merged.append([start, end, [member]])
            continue
        merged[-1][1] = max(float(merged[-1][1]), end)
        cast_members = merged[-1][2]
        assert isinstance(cast_members, list)
        cast_members.append(member)

    evidence_start = min(segment.before_time for segment in segments)
    evidence_end = max(segment.after_time for segment in segments)
    containing = [
        interval
        for interval in merged
        if float(interval[0]) <= evidence_start
        and evidence_end <= float(interval[1])
    ]
    if len(containing) != 1:
        raise JoinError(
            "successful capture coverage is not continuous across every "
            "singleton segment"
        )
    for segment in segments:
        if not any(
            float(start) <= segment.before_time
            and segment.after_time <= float(end)
            for start, end, _members in merged
        ):
            raise JoinError(
                f"segment {segment.sequence} is not continuously covered by capture evidence"
            )
    return [
        {
            "start": float(start),
            "end": float(end),
            "run_ids": sorted(
                {
                    str(member["run_id"])
                    for member in members
                    if isinstance(member, dict)
                }
            ),
            "members": list(members),
        }
        for start, end, members in merged
    ]


def _value_distribution(values: Iterable[str]) -> list[dict[str, object]]:
    counts = Counter(values)
    return [
        {"value": value, "count": counts[value]}
        for value in sorted(counts)
    ]


def parse_wire_segment(
    wire_messages: list[WireMessage],
    segment: SegmentEvidence,
    *,
    txid: int,
    rxid: int,
) -> tuple[list[DebugExchange], dict[str, object]]:
    """Validate authoritative host-timed request/response pairs for one segment."""
    overlapping = [
        message
        for message in wire_messages
        if message.can_id in (txid, rxid)
        and message.last_timestamp >= segment.before_time
        and message.first_timestamp <= segment.after_time
    ]
    straddling = [
        message
        for message in overlapping
        if not (
            segment.before_time <= message.first_timestamp
            and message.last_timestamp <= segment.after_time
        )
    ]
    if straddling:
        raise JoinError(
            f"segment {segment.sequence} contains an ISO-TP message that "
            "straddles its authoritative host-timed boundary"
        )
    channels = {message.channel for message in overlapping}
    if len(channels) > 1:
        raise JoinError(
            f"segment {segment.sequence} contains mixed wire channels: "
            + ", ".join(sorted(channels))
        )

    diagnostic: list[WireMessage] = []
    tester_present = 0
    for message in overlapping:
        is_tester_request = (
            message.can_id == txid and message.payload[:1] == b"\x3e"
        )
        is_tester_response = message.can_id == rxid and (
            message.payload[:1] == b"\x7e"
            or message.payload[:2] == b"\x7f\x3e"
        )
        if is_tester_request or is_tester_response:
            tester_present += 1
            continue
        diagnostic.append(message)

    if not diagnostic:
        raise JoinError(
            f"segment {segment.sequence} has no complete non-TesterPresent "
            "wire request/response pair"
        )
    if len(diagnostic) % 2:
        raise JoinError(
            f"segment {segment.sequence} has an unpaired non-TesterPresent "
            f"wire message ({len(diagnostic)} messages)"
        )

    exchanges: list[DebugExchange] = []
    for index in range(0, len(diagnostic), 2):
        request_message = diagnostic[index]
        response_message = diagnostic[index + 1]
        if (
            request_message.can_id != txid
            or response_message.can_id != rxid
        ):
            raise JoinError(
                f"segment {segment.sequence} non-TesterPresent wire traffic "
                "does not strictly alternate tester request then ECU response"
            )
        exchanges.append(
            DebugExchange(request_message.payload, response_message.payload)
        )

    requests = {exchange.request for exchange in exchanges}
    if len(requests) != 1:
        raise JoinError(
            f"segment {segment.sequence} does not contain exactly one distinct "
            f"wire request ({len(requests)} observed)"
        )
    request = next(iter(requests))
    if len(request) != 3 or request[0] != 0x22:
        raise JoinError(
            f"segment {segment.sequence} sole wire request is not exactly "
            "22 <DID>"
        )
    positive_prefix = b"\x62" + request[1:]
    for exchange in exchanges:
        if not exchange.response.startswith(positive_prefix):
            raise JoinError(
                f"segment {segment.sequence} contains a response that is not "
                "positive 62 <same DID>"
            )

    response_hex = [exchange.response.hex().upper() for exchange in exchanges]
    raw_data_hex = [
        exchange.response[len(positive_prefix) :].hex().upper()
        for exchange in exchanges
    ]
    return exchanges, {
        "matched": True,
        "status": "authoritative_host_timed_pairs",
        "channel": diagnostic[0].channel,
        "request": request.hex().upper(),
        "did": f"0x{int.from_bytes(request[1:], 'big'):04X}",
        "pair_count": len(exchanges),
        "message_count": len(diagnostic),
        "tester_present_messages_discarded": tester_present,
        "first_timestamp": diagnostic[0].first_timestamp,
        "last_timestamp": diagnostic[-1].last_timestamp,
        "responses": response_hex,
        "response_distribution": _value_distribution(response_hex),
        "raw_data_distribution": _value_distribution(raw_data_hex),
    }


def _group_debug_runs(
    values: Sequence[bytes],
    *,
    key: Callable[[bytes], bytes],
) -> list[tuple[bytes, list[bytes]]]:
    runs: list[tuple[bytes, list[bytes]]] = []
    for value in values:
        value_key = key(value)
        if runs and runs[-1][0] == value_key:
            runs[-1][1].append(value)
        else:
            runs.append((value_key, [value]))
    return runs


def _request_key_for_debug_response(response: bytes) -> bytes:
    if len(response) < 3 or response[0] != 0x62:
        raise JoinError(
            "buffered Debug stream contains a non-positive DID response"
        )
    return b"\x22" + response[1:3]


def _debug_boundary_alignment(
    debug_values: Sequence[bytes],
    wire_values: Sequence[bytes],
    *,
    sequence: int,
    direction: str,
    edge: str,
) -> dict[str, object]:
    minimum_overlap = min(3, len(wire_values))
    if len(debug_values) < minimum_overlap:
        raise JoinError(
            f"boundary segment {sequence} Debug {direction} overlap is too "
            f"short ({len(debug_values)} < {minimum_overlap})"
        )
    if len(debug_values) > len(wire_values):
        raise JoinError(
            f"boundary segment {sequence} Debug {direction} run is longer "
            "than its authoritative wire run"
        )
    width = len(debug_values)
    offsets = [
        offset
        for offset in range(len(wire_values) - width + 1)
        if list(wire_values[offset : offset + width]) == list(debug_values)
    ]
    if not offsets:
        raise JoinError(
            f"boundary segment {sequence} Debug {direction} run is not a "
            "contiguous subset of its authoritative wire run"
        )
    if width < len(wire_values):
        if edge == "start" and list(debug_values) != list(wire_values[-width:]):
            raise JoinError(
                f"first segment Debug {direction} clipping is not a suffix "
                "of authoritative wire"
            )
        if edge == "end" and list(debug_values) != list(wire_values[:width]):
            raise JoinError(
                f"final segment Debug {direction} clipping is not a prefix "
                "of authoritative wire"
            )
    return {
        "status": (
            "exact_full_run"
            if len(debug_values) == len(wire_values)
            else "clipped_contiguous_subset"
        ),
        "minimum_required_overlap": minimum_overlap,
        "wire_match_offsets": offsets,
        "wire_match_offset_unique": len(offsets) == 1,
        "required_edge": {
            "start": "debug_suffix_after_start_clipping",
            "end": "debug_prefix_before_end_clipping",
            "both": "contiguous_subset_with_both_outer_edges",
        }[edge],
    }


def corroborate_debug_transport(
    streams: DebugTransportStreams,
    wire_segments: Sequence[Sequence[DebugExchange]],
) -> dict[str, object]:
    """Corroborate whole-envelope Debug streams without sample pairing."""
    if not wire_segments:
        raise JoinError("cannot corroborate Debug without wire segments")
    expected_requests = [segment[0].request for segment in wire_segments]
    request_runs = _group_debug_runs(streams.requests, key=lambda value: value)
    response_runs = _group_debug_runs(
        streams.responses,
        key=_request_key_for_debug_response,
    )
    request_run_keys = [request for request, _values in request_runs]
    response_run_keys = [request for request, _values in response_runs]
    if request_run_keys != expected_requests:
        raise JoinError(
            "buffered Debug request run order does not match authoritative "
            "wire segment DID order"
        )
    if response_run_keys != expected_requests:
        raise JoinError(
            "buffered Debug response run order does not match authoritative "
            "wire segment DID order"
        )

    run_reports: list[dict[str, object]] = []
    final_index = len(wire_segments) - 1
    for sequence, wire_exchanges in enumerate(wire_segments):
        wire_requests = [exchange.request for exchange in wire_exchanges]
        wire_responses = [exchange.response for exchange in wire_exchanges]
        debug_requests = request_runs[sequence][1]
        debug_responses = response_runs[sequence][1]
        request_count = len(debug_requests)
        response_count = len(debug_responses)
        if sequence == 0 and sequence == final_index:
            retention_compatible = abs(request_count - response_count) <= 1
        elif sequence == 0:
            retention_compatible = response_count - request_count in (0, 1)
        elif sequence == final_index:
            retention_compatible = request_count - response_count in (0, 1)
        else:
            retention_compatible = request_count == response_count
        if not retention_compatible:
            raise JoinError(
                f"segment {sequence} Debug request/response retention counts "
                f"are incompatible ({request_count} requests, "
                f"{response_count} responses)"
            )
        if sequence not in (0, final_index):
            if debug_requests != wire_requests:
                raise JoinError(
                    f"interior segment {sequence} Debug request run does not "
                    "exactly match authoritative wire"
                )
            if debug_responses != wire_responses:
                raise JoinError(
                    f"interior segment {sequence} Debug response run does not "
                    "exactly match authoritative wire"
                )
            request_alignment = {
                "status": "exact_full_run",
                "minimum_required_overlap": len(wire_requests),
                "wire_match_offsets": [0],
            }
            response_alignment = {
                "status": "exact_full_run",
                "minimum_required_overlap": len(wire_responses),
                "wire_match_offsets": [0],
            }
        else:
            edge = (
                "both"
                if sequence == 0 and sequence == final_index
                else "start"
                if sequence == 0
                else "end"
            )
            request_alignment = _debug_boundary_alignment(
                debug_requests,
                wire_requests,
                sequence=sequence,
                direction="request",
                edge=edge,
            )
            response_alignment = _debug_boundary_alignment(
                debug_responses,
                wire_responses,
                sequence=sequence,
                direction="response",
                edge=edge,
            )
        run_reports.append(
            {
                "sequence": sequence,
                "request": expected_requests[sequence].hex().upper(),
                "did": (
                    f"0x{int.from_bytes(expected_requests[sequence][1:], 'big'):04X}"
                ),
                "wire_pair_count": len(wire_exchanges),
                "debug_request_count": request_count,
                "debug_response_count": response_count,
                "retention_counts_compatible": True,
                "request_alignment": request_alignment,
                "response_alignment": response_alignment,
            }
        )
    return {
        "matched": True,
        "status": "whole_campaign_independent_streams_corroborated",
        "planned_did_run_order_exact": True,
        "sample_pairing": "not_attempted_buffered_artifacts",
        "runs": run_reports,
    }


def _candidate_for_segment(
    gauge: str,
    info_samples: Sequence[str],
    exchanges: list[DebugExchange],
    wire: dict[str, object],
) -> dict[str, object]:
    if not info_samples:
        raise JoinError(f"singleton label {gauge!r} has no complete Info samples")
    if not exchanges or wire.get("matched") is not True:
        raise JoinError(f"singleton label {gauge!r} lacks authoritative wire pairs")
    exact_request = exchanges[0].request
    did = int.from_bytes(exact_request[1:], "big")
    rendered_values = list(info_samples)
    response_values = [
        exchange.response.hex().upper() for exchange in exchanges
    ]
    raw_data_values = [
        exchange.response[3:].hex().upper() for exchange in exchanges
    ]
    count_match = len(info_samples) == len(exchanges)
    return {
        "classification": "candidate",
        "label": gauge,
        "request": exact_request.hex().upper(),
        "did": f"0x{did:04X}",
        "info_sample_count": len(info_samples),
        "wire_pair_count": len(exchanges),
        "count_match": count_match,
        "rendered_values": rendered_values,
        "rendered_distribution": _value_distribution(rendered_values),
        "wire_responses": response_values,
        "wire_response_distribution": _value_distribution(response_values),
        "raw_data_distribution": _value_distribution(raw_data_values),
        "sample_pairing": "not_attempted_buffered_artifacts",
        "reasons": [],
    }


def build_report(
    campaign_dir: Path,
    capture_dir: Path | None = None,
    *,
    capture_set: Path | None = None,
    maximum_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    maximum_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES,
    maximum_segments: int = DEFAULT_MAX_SEGMENTS,
    maximum_events: int = DEFAULT_MAX_EVENTS,
    maximum_exchanges_per_segment: int = DEFAULT_MAX_EXCHANGES_PER_SEGMENT,
    maximum_manifest_rows: int = DEFAULT_MAX_MANIFEST_ROWS,
    maximum_wire_bytes: int = DEFAULT_MAX_WIRE_BYTES,
    maximum_wire_messages: int = DEFAULT_MAX_WIRE_MESSAGES,
    maximum_wire_payload_bytes: int = DEFAULT_MAX_WIRE_PAYLOAD_BYTES,
) -> dict[str, object]:
    plan, segments, artifacts, campaign_provenance = validate_campaign(
        campaign_dir,
        maximum_artifact_bytes=maximum_artifact_bytes,
        maximum_segments=maximum_segments,
        maximum_events=maximum_events,
    )
    if (capture_dir is None) == (capture_set is None):
        raise JoinError(
            "provide exactly one single capture directory or --capture-set"
        )
    capture_set_provenance: dict[str, object] | None = None
    if capture_set is not None:
        specs, capture_set_provenance = load_capture_set(
            capture_set,
            singleton_campaign_id=plan.campaign_id,
        )
    else:
        assert capture_dir is not None
        specs = [CaptureSpec(capture_dir, plan.campaign_id)]

    byte_budget = [0]
    frame_budget = [0]
    validated_captures = [
        validate_capture_evidence(
            spec,
            source_index=index,
            plan=plan,
            segments=segments,
            maximum_manifest_rows=maximum_manifest_rows,
            maximum_artifact_bytes=maximum_artifact_bytes,
            maximum_wire_bytes=maximum_wire_bytes,
            maximum_wire_messages=maximum_wire_messages,
            byte_budget=byte_budget,
            frame_budget=frame_budget,
        )
        for index, spec in enumerate(specs)
    ]
    coverage = validate_continuous_coverage(validated_captures, segments)
    merged_frames, merge_provenance = merge_capture_frames(validated_captures)
    wire_messages = reassemble_wire_frames(
        merged_frames,
        maximum_messages=min(maximum_wire_messages, HARD_MAX_WIRE_MESSAGES),
        maximum_payload_bytes=min(
            maximum_wire_payload_bytes,
            HARD_MAX_WIRE_PAYLOAD_BYTES,
        ),
    )
    if capture_set_provenance is None:
        capture_provenance = dict(validated_captures[0].provenance)
        capture_provenance.update(
            {
                "mode": "single",
                "coverage_union": coverage,
                "merge": merge_provenance,
            }
        )
    else:
        capture_provenance = {
            **capture_set_provenance,
            "runs": [
                capture.provenance for capture in validated_captures
            ],
            "coverage_union": coverage,
            "merge": merge_provenance,
            "decompressed_bytes_read": byte_budget[0],
        }
    module = MODULES[plan.module_key]
    info_name = str(campaign_provenance["info_artifact"])
    outer_byte_limit = min(
        maximum_artifact_bytes,
        maximum_segment_bytes * len(segments),
        HARD_MAX_OUTER_ARTIFACT_BYTES,
    )
    outer_exchange_limit = min(
        maximum_exchanges_per_segment * len(segments),
        HARD_MAX_OUTER_MESSAGES,
    )
    info_outer_boundary = ArtifactBoundary(
        segments[0].boundaries[info_name].before,
        segments[-1].boundaries[info_name].after,
    )
    info_runs, info_outer_hash = parse_info_runs(
        artifacts[info_name],
        info_outer_boundary,
        plan.dialog_labels,
        maximum_bytes=outer_byte_limit,
        maximum_samples=outer_exchange_limit,
    )
    observed_run_labels = [run.label for run in info_runs]
    if observed_run_labels != list(plan.schedule):
        raise JoinError(
            "buffered Info label-run sequence does not match campaign schedule: "
            f"observed={observed_run_labels!r}, expected={list(plan.schedule)!r}"
        )

    debug_outer_boundary = ArtifactBoundary(
        segments[0].boundaries[DEBUG_ARTIFACT].before,
        segments[-1].boundaries[DEBUG_ARTIFACT].after,
    )
    debug_streams, debug_outer_hash = parse_debug_transport_streams(
        artifacts[DEBUG_ARTIFACT],
        debug_outer_boundary,
        maximum_bytes=outer_byte_limit,
        maximum_messages=outer_exchange_limit,
    )
    debug_requests = [
        request.hex().upper() for request in debug_streams.requests
    ]
    debug_responses = [
        response.hex().upper() for response in debug_streams.responses
    ]
    debug_provenance = {
        "role": "whole_campaign_transport_corroboration_only",
        "sample_alignment": "not_attempted_buffered_artifacts",
        "offsets_are": "buffer_flush_witnesses_not_segment_boundaries",
        "outer_interval": {
            "before": debug_outer_boundary.before,
            "after": debug_outer_boundary.after,
            "bytes": debug_outer_boundary.after - debug_outer_boundary.before,
            "slice_sha256": debug_outer_hash,
        },
        "request_count": len(debug_streams.requests),
        "response_count": len(debug_streams.responses),
        "tester_present_requests_discarded": (
            debug_streams.tester_present_requests
        ),
        "tester_present_responses_discarded": (
            debug_streams.tester_present_responses
        ),
        "leading_empty_response_prompts_discarded": (
            debug_streams.leading_empty_prompts
        ),
        "trailing_empty_response_prompts_discarded": (
            debug_streams.trailing_empty_prompts
        ),
        "trailing_response_fragment_bytes": (
            debug_streams.trailing_response_fragment_bytes
        ),
        "distinct_requests": sorted(set(debug_requests)),
        "request_distribution": _value_distribution(debug_requests),
        "response_distribution": _value_distribution(debug_responses),
    }

    segment_reports: list[dict[str, object]] = []
    wire_segment_exchanges: list[list[DebugExchange]] = []
    for segment, info_run in zip(segments, info_runs):
        debug_boundary = segment.boundaries[DEBUG_ARTIFACT]
        info_boundary = segment.boundaries[info_name]
        exchanges, wire = parse_wire_segment(
            wire_messages,
            segment,
            txid=module.txid,
            rxid=module.rxid,
        )
        wire_segment_exchanges.append(exchanges)
        candidate = _candidate_for_segment(
            segment.gauge,
            info_run.rendered_values,
            exchanges,
            wire,
        )
        count_match = len(info_run.rendered_values) == len(exchanges)
        segment_reports.append(
            {
                "sequence": segment.sequence,
                "label": segment.gauge,
                "event_interval": {
                    "before_epoch": segment.before_time,
                    "after_epoch": segment.after_time,
                },
                "artifact_offset_witnesses": {
                    DEBUG_ARTIFACT: {
                        "before": debug_boundary.before,
                        "after": debug_boundary.after,
                        "bytes": debug_boundary.after - debug_boundary.before,
                    },
                    info_name: {
                        "before": info_boundary.before,
                        "after": info_boundary.after,
                        "bytes": info_boundary.after - info_boundary.before,
                    },
                },
                "info": {
                    "alignment": "contiguous_exact_label_run_by_schedule",
                    "run_ordinal": segment.sequence,
                    "sample_count": len(info_run.rendered_values),
                    "rendered_values": list(info_run.rendered_values),
                    "rendered_distribution": _value_distribution(
                        info_run.rendered_values
                    ),
                },
                "wire": wire,
                "info_wire_count_match": count_match,
                "candidate": candidate,
            }
        )
    debug_provenance["corroboration"] = corroborate_debug_transport(
        debug_streams,
        wire_segment_exchanges,
    )

    anchor_checks: list[dict[str, object]] = []
    for anchor in plan.repeat_anchors:
        occurrences = [
            report for report in segment_reports if report["label"] == anchor
        ]
        if len(occurrences) < 2:
            raise JoinError(
                f"repeat anchor {anchor!r} does not occur at least twice in schedule"
            )
        requests = [
            _require_dict(report["candidate"], "candidate").get("request")
            for report in occurrences
        ]
        if any(request is None for request in requests) or len(set(requests)) != 1:
            raise JoinError(
                f"repeat anchor {anchor!r} did not resolve to one identical exact request"
            )
        anchor_checks.append(
            {
                "label": anchor,
                "occurrences": [report["sequence"] for report in occurrences],
                "request": requests[0],
                "did": _require_dict(
                    occurrences[0]["candidate"], "candidate"
                ).get("did"),
                "consistent": True,
            }
        )

    candidate_count = sum(
        _require_dict(report["candidate"], "candidate").get("classification")
        == "candidate"
        for report in segment_reports
    )
    corroborated_count = sum(
        _require_dict(report["wire"], "wire").get("matched") is True
        for report in segment_reports
    )
    return {
        "schema_version": 2,
        "classification": "candidate_only",
        "buffered_artifact_mode": {
            "enabled": True,
            "segment_timing_authority": "host_event_intervals_plus_passive_wire",
            "info_alignment": (
                "whole_outer_interval_contiguous_exact_label_runs_by_schedule"
            ),
            "debug_alignment": (
                "whole_outer_interval_independent_stream_corroboration"
            ),
            "info_wire_sample_count_equality_required": False,
        },
        "campaign": {
            "campaign_id": plan.campaign_id,
            "module_key": plan.module_key,
            "expected_runtime": plan.expected_runtime,
            "schedule": list(plan.schedule),
            "repeat_anchors": list(plan.repeat_anchors),
            "provenance": campaign_provenance,
        },
        "module_addressing": {
            "addressing_mode": module.addressing_mode,
            "request_can_id": f"0x{module.txid:X}",
            "response_can_id": f"0x{module.rxid:X}",
            "bus": module.bus,
            "bitrate": module.bitrate,
        },
        "passive_capture": capture_provenance,
        "info_artifact": {
            "role": "whole_campaign_rendered_label_runs",
            "outer_interval": {
                "before": info_outer_boundary.before,
                "after": info_outer_boundary.after,
                "bytes": info_outer_boundary.after - info_outer_boundary.before,
                "slice_sha256": info_outer_hash,
            },
            "run_labels": observed_run_labels,
            "run_sample_counts": [
                len(run.rendered_values) for run in info_runs
            ],
        },
        "debug_artifact": debug_provenance,
        "segments": segment_reports,
        "anchor_checks": anchor_checks,
        "summary": {
            "segments": len(segment_reports),
            "candidate_segments": candidate_count,
            "unresolved_segments": len(segment_reports) - candidate_count,
            "anchors_consistent": True,
            "wire_sequences_corroborated": corroborated_count,
        },
        "interpretation": (
            "Candidate associations only: exact scheduled AlfaOBD Info label runs "
            "and authoritative passive-wire 22/62 exchanges do not independently "
            "prove semantic identity or scaling. Buffered artifact offsets do not "
            "pair individual rendered and raw samples."
        ),
    }


def atomic_write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise JoinError(f"refusing to overwrite existing report path: {path}") from exc
    created = True
    try:
        with os.fdopen(
            fd,
            mode="w",
            encoding="utf-8",
        ) as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
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
                os.unlink(path)
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("capture_dir", nargs="?", type=Path)
    parser.add_argument(
        "--capture-set",
        type=Path,
        help=(
            "explicit JSON binding one singleton campaign to hash-pinned "
            "overlapping passive recorder runs"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--max-artifact-bytes",
        type=int,
        default=DEFAULT_MAX_ARTIFACT_BYTES,
    )
    parser.add_argument(
        "--max-segment-bytes",
        type=int,
        default=DEFAULT_MAX_SEGMENT_BYTES,
    )
    parser.add_argument(
        "--max-segments", type=int, default=DEFAULT_MAX_SEGMENTS
    )
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument(
        "--max-exchanges-per-segment",
        type=int,
        default=DEFAULT_MAX_EXCHANGES_PER_SEGMENT,
    )
    parser.add_argument(
        "--max-manifest-rows",
        type=int,
        default=DEFAULT_MAX_MANIFEST_ROWS,
    )
    parser.add_argument(
        "--max-wire-bytes", type=int, default=DEFAULT_MAX_WIRE_BYTES
    )
    parser.add_argument(
        "--max-wire-messages", type=int, default=DEFAULT_MAX_WIRE_MESSAGES
    )
    parser.add_argument(
        "--max-wire-payload-bytes",
        type=int,
        default=DEFAULT_MAX_WIRE_PAYLOAD_BYTES,
    )
    return parser


def _positive_limit(value: int, name: str) -> int:
    if value <= 0:
        raise JoinError(f"{name} must be positive")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        limits = {
            "maximum_artifact_bytes": _positive_limit(
                args.max_artifact_bytes, "--max-artifact-bytes"
            ),
            "maximum_segment_bytes": _positive_limit(
                args.max_segment_bytes, "--max-segment-bytes"
            ),
            "maximum_segments": _positive_limit(
                args.max_segments, "--max-segments"
            ),
            "maximum_events": _positive_limit(args.max_events, "--max-events"),
            "maximum_exchanges_per_segment": _positive_limit(
                args.max_exchanges_per_segment,
                "--max-exchanges-per-segment",
            ),
            "maximum_manifest_rows": _positive_limit(
                args.max_manifest_rows, "--max-manifest-rows"
            ),
            "maximum_wire_bytes": _positive_limit(
                args.max_wire_bytes, "--max-wire-bytes"
            ),
            "maximum_wire_messages": _positive_limit(
                args.max_wire_messages, "--max-wire-messages"
            ),
            "maximum_wire_payload_bytes": _positive_limit(
                args.max_wire_payload_bytes,
                "--max-wire-payload-bytes",
            ),
        }
        if (args.capture_dir is None) == (args.capture_set is None):
            raise JoinError(
                "provide exactly one capture_dir positional argument or --capture-set"
            )
        report = build_report(
            args.campaign_dir,
            args.capture_dir,
            capture_set=args.capture_set,
            **limits,
        )
        output = args.output or (
            DEFAULT_OUT_DIR / f"{report['campaign']['campaign_id']}.json"
        )
        atomic_write_report(output, report)
        print(f"Candidate-only singleton join written to {output}")
        return 0
    except (JoinError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
