#!/usr/bin/env python3
"""Extract exact physical ``22`` exchanges from a finalized candump stream.

This is strictly offline.  It converts single-frame, physically addressed
request/positive-response pairs for one registered module into the wire schema
consumed by :mod:`tools.can_timeseries_correlate`.  Every output response keeps
the original kernel timestamp, CAN payload, and global candump line sequence so
the correlator can prove that its reference samples came from the supplied raw
capture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Sequence


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.modules import MODULES, Module  # noqa: E402
from tools.can_timeseries_correlate import (  # noqa: E402
    CliZstdDecompressor,
    CorrelateError,
    MAX_CAPTURE_FILES,
    StreamStats,
    iter_candump_frames,
)


TMP_ROOT = (REPO / "tmp").resolve()
MAX_EXCHANGES = 1_500_000
_VAN_COMPUTE_JOB_ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")


def _single_frame_payload(can_data: bytes) -> bytes | None:
    if not can_data or can_data[0] & 0xF0:
        return None
    declared = can_data[0] & 0x0F
    if declared == 0 or declared > len(can_data) - 1:
        return None
    return can_data[1 : 1 + declared]


def _timestamp_text(timestamp_us: int) -> str:
    seconds, micros = divmod(timestamp_us, 1_000_000)
    return f"{seconds}.{micros:06d}"


def _wire_row(
    *,
    frame,
    payload: bytes,
    module: Module,
    did: int,
    classification: str,
    direction: str,
    sequence: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "type": "wire_frame",
        "sequence": sequence,
        "module_key": module.key,
        "direction": direction,
        "classification": classification,
        "did": f"{did:04X}",
        "timestamp_source": "candump_kernel",
        "timestamp_epoch_us": frame.timestamp_us,
        "timestamp_text": _timestamp_text(frame.timestamp_us),
        "raw_line_sequence": frame.raw_line_sequence,
        "can_id": f"{frame.can_id:08X}",
        "can_data_hex": frame.payload.hex(" ").upper(),
        "isotp_payload_hex": payload.hex(" ").upper(),
    }


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


def _checked_output(
    path: Path,
    *,
    module: Module,
    allow_van_compute_result: bool = False,
) -> Path:
    expected = f"{module.key}_wire.jsonl"
    if path.name != expected:
        raise CorrelateError(f"output filename must be exactly {expected}")

    roots = [TMP_ROOT.resolve(strict=False)]
    if allow_van_compute_result:
        job_root = _van_compute_job_root()
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
    inside_root = False
    for root in roots:
        try:
            inside = os.path.commonpath((str(root), str(resolved))) == str(root)
        except ValueError:
            inside = False
        if inside and resolved != root:
            inside_root = True
            break
    if not inside_root:
        permitted = str(TMP_ROOT)
        if allow_van_compute_result:
            permitted += " or the validated van-compute result directory"
        raise CorrelateError(f"output must be an explicit file below {permitted}")
    if path.exists() or path.is_symlink():
        raise CorrelateError(f"refusing to overwrite existing output: {path}")
    summary = path.with_suffix(".summary.json")
    if summary.exists() or summary.is_symlink():
        raise CorrelateError(f"refusing to overwrite existing output: {summary}")
    return resolved


def _write_summary(path: Path, payload: dict[str, object]) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def extract(
    *,
    module: Module,
    captures: Sequence[Path],
    output: Path,
    allow_van_compute_result: bool = False,
) -> dict[str, object]:
    if not captures or len(captures) > MAX_CAPTURE_FILES:
        raise CorrelateError(
            f"capture count must be between 1 and {MAX_CAPTURE_FILES}"
        )
    for path in captures:
        if path.name.endswith(".partial"):
            raise CorrelateError(f"partial evidence is not accepted: {path}")
        try:
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise CorrelateError(f"input is unavailable: {path}") from exc
        if not canonical.is_file():
            raise CorrelateError(f"input is not a regular file: {path}")
        if not (
            path.name.endswith(".candump")
            or path.name.endswith(".log")
            or path.name.endswith(".txt")
            or path.name.endswith(".zst")
        ):
            raise CorrelateError(f"unsupported candump input: {path}")

    checked = _checked_output(
        output,
        module=module,
        allow_van_compute_result=allow_van_compute_result,
    )
    checked.parent.mkdir(parents=True, exist_ok=True)
    checked = _checked_output(
        checked,
        module=module,
        allow_van_compute_result=allow_van_compute_result,
    )
    stats = [
        StreamStats(
            str(path),
            "zstd" if path.name.endswith(".zst") else "none",
        )
        for path in captures
    ]
    pending = None
    exchange_count = 0
    incomplete_requests = 0
    ignored_responses = 0
    output_sequence = 0
    digest = hashlib.sha256()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(checked, flags, 0o600)
    created = True
    try:
        with os.fdopen(fd, "wb") as handle:
            for frame in iter_candump_frames(
                captures,
                stats=stats,
                decompressor=CliZstdDecompressor(),
                expected_channel=module.channel,
            ):
                payload = _single_frame_payload(frame.payload)
                if payload is None:
                    continue
                if (
                    frame.can_id == module.txid
                    and len(payload) == 3
                    and payload[0] == 0x22
                ):
                    if pending is not None:
                        incomplete_requests += 1
                    pending = (
                        int.from_bytes(payload[1:3], "big"),
                        frame,
                        payload,
                    )
                    continue
                if (
                    frame.can_id != module.rxid
                    or len(payload) < 4
                    or payload[0] != 0x62
                ):
                    continue
                did = int.from_bytes(payload[1:3], "big")
                if pending is None or pending[0] != did:
                    ignored_responses += 1
                    continue
                request_did, request_frame, request_payload = pending
                pending = None
                if exchange_count >= MAX_EXCHANGES:
                    raise CorrelateError(
                        f"exchange count exceeds safety cap {MAX_EXCHANGES}"
                    )
                rows = (
                    _wire_row(
                        frame=request_frame,
                        payload=request_payload,
                        module=module,
                        did=request_did,
                        classification="exact_request",
                        direction="tester_to_ecu",
                        sequence=output_sequence,
                    ),
                    _wire_row(
                        frame=frame,
                        payload=payload,
                        module=module,
                        did=did,
                        classification="exact_positive_response",
                        direction="ecu_to_tester",
                        sequence=output_sequence + 1,
                    ),
                )
                output_sequence += 2
                exchange_count += 1
                for row in rows:
                    encoded = (
                        json.dumps(
                            row,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                    handle.write(encoded)
                    digest.update(encoded)
            if pending is not None:
                incomplete_requests += 1
            if exchange_count == 0:
                raise CorrelateError(
                    f"no exact physical 22/62 exchanges found for {module.key}"
                )
            handle.flush()
            os.fsync(handle.fileno())
        created = False
    finally:
        if created:
            try:
                checked.unlink()
            except OSError:
                pass

    summary_path = checked.with_suffix(".summary.json")
    summary = {
        "schema_version": 1,
        "classification": "offline_exact_wire_extraction",
        "module": {
            "key": module.key,
            "name": module.name,
            "bus": module.bus,
            "channel": module.channel,
            "txid_hex": f"{module.txid:08X}",
            "rxid_hex": f"{module.rxid:08X}",
            "addressing_mode": module.addressing_mode,
        },
        "captures": [item.as_dict() for item in stats],
        "exchange_count": exchange_count,
        "wire_row_count": output_sequence,
        "incomplete_requests": incomplete_requests,
        "ignored_unpaired_positive_responses": ignored_responses,
        "wire_path": str(checked),
        "wire_sha256": digest.hexdigest(),
        "provenance": {
            "kernel_timestamps_preserved": True,
            "global_raw_line_sequence_preserved": True,
            "manifest_validated": False,
            "loss_accounting_validated": False,
        },
    }
    try:
        _write_summary(summary_path, summary)
    except BaseException:
        checked.unlink(missing_ok=True)
        raise
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline-only: extract exact physical ReadDataByIdentifier "
            "wire references from finalized candump inputs"
        )
    )
    parser.add_argument("module", choices=tuple(MODULES))
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="exact tmp/.../<module>_wire.jsonl output; never overwritten",
    )
    parser.add_argument(
        "--allow-van-compute-result",
        action="store_true",
        help=(
            "allow <module>_wire.jsonl in the validated van-compute job "
            "result directory"
        ),
    )
    parser.add_argument("captures", nargs="+", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = extract(
            module=MODULES[args.module],
            captures=args.captures,
            output=args.output,
            allow_van_compute_result=args.allow_van_compute_result,
        )
    except CorrelateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        f"Wrote {summary['exchange_count']} exact {args.module} exchanges "
        f"to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
