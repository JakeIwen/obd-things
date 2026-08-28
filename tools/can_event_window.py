#!/usr/bin/env python3
"""Extract a bounded, exact frame window from saved candump logs.

This is an offline-only evidence tool. It reads finalized plain-text or ``.zst``
candump logs, never imports SocketCAN helpers, and cannot inspect or change a CAN
interface or service. Exact payloads can contain private vehicle data; keep the
JSON result under ``tmp/`` unless a reviewed subset is deliberately promoted.

Unfiltered extraction requires both ``--start`` and ``--end`` and is limited to
30 seconds. Identifier-filtered extraction may scan a longer capture, but the
stored result is always capped by ``--maximum-frames``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Iterator, TextIO


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import can_capture_summary


MAX_UNFILTERED_WINDOW_SECONDS = 30.0
DEFAULT_MAXIMUM_FRAMES = 50_000
HARD_MAXIMUM_FRAMES = 200_000
CRC_MISMATCH_SAMPLE_LIMIT = 20


class EventWindowError(RuntimeError):
    pass


def parse_id_selector(value: str) -> tuple[int, int]:
    """Parse an explicit ``11:HEX`` or ``29:HEX`` identifier selector."""

    try:
        bits_text, can_id_text = value.split(":", 1)
        bits = int(bits_text, 10)
        can_id = int(can_id_text, 16)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(
            "identifier must be explicit 11:HEX or 29:HEX"
        ) from exc
    maximum = 0x7FF if bits == 11 else 0x1FFFFFFF if bits == 29 else None
    if maximum is None or not 0 <= can_id <= maximum:
        raise argparse.ArgumentTypeError(
            "identifier must be explicit 11:000-7FF or 29:00000000-1FFFFFFF"
        )
    return bits, can_id


def _finite_timestamp(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be numeric") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("timestamp must be finite")
    return parsed


def crc8_sae_j1850(data: bytes) -> int:
    """CRC-8/SAE-J1850: poly 0x1D, init 0xFF, xorout 0xFF, non-reflected."""

    crc = 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


@contextmanager
def capture_lines(path: Path) -> Iterator[TextIO]:
    """Yield a streaming text reader for a plain or finalized zstd capture."""

    if path.suffix.lower() != ".zst":
        with path.open("r", encoding="utf-8", errors="replace") as capture:
            yield capture
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
        raise EventWindowError(f"cannot start zstd for {path}: {exc}") from exc
    if process.stdout is None or process.stderr is None:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise EventWindowError("zstd did not provide stdout/stderr pipes")
    try:
        yield process.stdout
        stderr = process.stderr.read()
        returncode = process.wait()
    finally:
        process.stdout.close()
        process.stderr.close()
    if returncode != 0:
        detail = stderr.strip() or f"exit status {returncode}"
        raise EventWindowError(f"zstd decompression failed for {path}: {detail}")


def extract_window(
    captures: list[Path],
    *,
    start: float | None,
    end: float | None,
    selectors: set[tuple[int, int]],
    maximum_frames: int,
    audit_crc8_j1850: bool = False,
) -> dict[str, object]:
    if not captures:
        raise EventWindowError("at least one capture is required")
    if start is not None and end is not None and end < start:
        raise EventWindowError("--end must be greater than or equal to --start")
    if not selectors:
        if start is None or end is None:
            raise EventWindowError(
                "unfiltered extraction requires both --start and --end"
            )
        if end - start > MAX_UNFILTERED_WINDOW_SECONDS:
            raise EventWindowError(
                f"unfiltered extraction is limited to {MAX_UNFILTERED_WINDOW_SECONDS:g} seconds"
            )
    if not 1 <= maximum_frames <= HARD_MAXIMUM_FRAMES:
        raise EventWindowError(
            f"--maximum-frames must be between 1 and {HARD_MAXIMUM_FRAMES}"
        )

    frames: list[dict[str, object]] = []
    source_rows: list[dict[str, object]] = []
    stream_counts: Counter[tuple[int, str, int, int]] = Counter()
    crc_checked = 0
    crc_mismatches = 0
    crc_mismatch_samples: list[dict[str, object]] = []

    for source_index, path in enumerate(captures):
        total_lines = 0
        parsed_frames = 0
        selected_frames = 0
        unparsed_lines = 0
        with capture_lines(path) as lines:
            for line_number, line in enumerate(lines, 1):
                total_lines += 1
                if not line.strip():
                    continue
                frame = can_capture_summary.parse_frame(line)
                if frame is None:
                    unparsed_lines += 1
                    continue
                parsed_frames += 1
                if start is not None and frame.timestamp < start:
                    continue
                if end is not None and frame.timestamp > end:
                    continue
                if selectors and (frame.id_bits, frame.can_id) not in selectors:
                    continue

                selected_frames += 1
                stream_counts[
                    (source_index, frame.interface, frame.id_bits, frame.can_id)
                ] += 1
                if audit_crc8_j1850:
                    if not frame.payload:
                        raise EventWindowError(
                            "CRC-8 audit cannot evaluate a zero-length selected frame"
                        )
                    crc_checked += 1
                    computed_crc = crc8_sae_j1850(frame.payload[:-1])
                    observed_crc = frame.payload[-1]
                    if computed_crc != observed_crc:
                        crc_mismatches += 1
                        if len(crc_mismatch_samples) < CRC_MISMATCH_SAMPLE_LIMIT:
                            crc_mismatch_samples.append(
                                {
                                    "timestamp": frame.timestamp,
                                    "source_index": source_index,
                                    "line_number": line_number,
                                    "interface": frame.interface,
                                    "id_bits": frame.id_bits,
                                    "can_id_hex": can_capture_summary._format_can_id(
                                        frame.can_id, frame.id_bits
                                    ),
                                    "computed_crc_hex": f"{computed_crc:02X}",
                                    "observed_crc_hex": f"{observed_crc:02X}",
                                }
                            )
                frames.append(
                    {
                        "timestamp": frame.timestamp,
                        "source_index": source_index,
                        "line_number": line_number,
                        "interface": frame.interface,
                        "id_bits": frame.id_bits,
                        "can_id": frame.can_id,
                        "can_id_hex": can_capture_summary._format_can_id(
                            frame.can_id, frame.id_bits
                        ),
                        "dlc": frame.dlc,
                        "data_hex": frame.payload.hex(" ").upper(),
                    }
                )
                if len(frames) > maximum_frames:
                    raise EventWindowError(
                        f"selection exceeded --maximum-frames={maximum_frames}; "
                        "narrow the time window or identifiers"
                    )
        source_rows.append(
            {
                "source_index": source_index,
                "path": str(path),
                "total_lines": total_lines,
                "parsed_frames": parsed_frames,
                "selected_frames": selected_frames,
                "unparsed_lines": unparsed_lines,
            }
        )

    frames.sort(
        key=lambda row: (
            float(row["timestamp"]),
            int(row["source_index"]),
            int(row["line_number"]),
        )
    )
    streams = [
        {
            "source_index": source_index,
            "interface": interface,
            "id_bits": id_bits,
            "can_id": can_id,
            "can_id_hex": can_capture_summary._format_can_id(can_id, id_bits),
            "count": count,
        }
        for (source_index, interface, id_bits, can_id), count in sorted(
            stream_counts.items()
        )
    ]
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": "offline_saved_candump_event_window",
        "payload_warning": (
            "Exact CAN payloads may contain private vehicle data; keep this result under tmp/ "
            "and review/redact before promotion."
        ),
        "selection": {
            "start": start,
            "end": end,
            "identifiers": [
                {
                    "id_bits": bits,
                    "can_id": can_id,
                    "can_id_hex": can_capture_summary._format_can_id(can_id, bits),
                }
                for bits, can_id in sorted(selectors)
            ],
            "maximum_frames": maximum_frames,
        },
        "sources": source_rows,
        "streams": streams,
        "selected_frames": len(frames),
        "first_timestamp": frames[0]["timestamp"] if frames else None,
        "last_timestamp": frames[-1]["timestamp"] if frames else None,
        "frames": frames,
    }
    if audit_crc8_j1850:
        payload["crc8_sae_j1850_audit"] = {
            "parameters": {
                "polynomial_hex": "1D",
                "initial_hex": "FF",
                "xorout_hex": "FF",
                "reflected": False,
                "coverage": "all payload bytes except final observed CRC byte",
            },
            "checked_frames": crc_checked,
            "mismatch_count": crc_mismatches,
            "mismatch_samples": crc_mismatch_samples,
            "mismatch_samples_truncated": crc_mismatches > len(crc_mismatch_samples),
        }
    return payload


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2)
        output.write("\n")


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("captures", type=Path, nargs="+")
    argument_parser.add_argument("--start", type=_finite_timestamp)
    argument_parser.add_argument("--end", type=_finite_timestamp)
    argument_parser.add_argument(
        "--id",
        dest="identifiers",
        type=parse_id_selector,
        action="append",
        default=[],
        help="explicit identifier selector; repeat 11:HEX or 29:HEX",
    )
    argument_parser.add_argument(
        "--maximum-frames", type=int, default=DEFAULT_MAXIMUM_FRAMES
    )
    argument_parser.add_argument(
        "--audit-crc8-j1850",
        action="store_true",
        help="audit the final byte of every selected frame as CRC-8/SAE-J1850",
    )
    argument_parser.add_argument("--output", type=Path, required=True)
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        resolved_output = args.output.resolve()
        if any(path.resolve() == resolved_output for path in args.captures):
            raise EventWindowError("--output must not overwrite an input capture")
        payload = extract_window(
            args.captures,
            start=args.start,
            end=args.end,
            selectors=set(args.identifiers),
            maximum_frames=args.maximum_frames,
            audit_crc8_j1850=args.audit_crc8_j1850,
        )
        write_json(args.output, payload)
    except (OSError, EventWindowError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"selected {payload['selected_frames']} exact frame(s) from "
        f"{len(payload['sources'])} saved capture(s); JSON: {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
