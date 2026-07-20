#!/usr/bin/env python3
"""Stream a candump log and summarize its passive CAN traffic.

Accepted input is the common absolute-timestamp output produced by ``candump -ta``::

    (1782677301.162015) can0 100 [8] 50 19 A7 40 60 00 04 6C
    (1782677301.162015) can0 100#5019A7406000046C

The input is processed one line at a time.  Memory use is therefore proportional to the
number of distinct CAN identifiers, not the number of frames in the capture.  In each
changed-byte mask, bit 0 represents payload byte 0, bit 1 represents byte 1, and so on.
A byte is marked when its value or presence differs from that identifier's first frame.

JSON reports also retain per-byte minimums, maximums, and presence counts.  This makes a
constant value that changes between two captures detectable, but those statistics can reveal
payload content (including fragments of diagnostic identity data).  Keep reports under ``tmp/``
and review/redact selected evidence before promoting it into tracked findings.

This tool only reads a saved file.  It does not import SocketCAN helpers, open a CAN socket,
or inspect/change an interface or service.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import BinaryIO, Iterable, Iterator, TextIO


_TIMESTAMP = r"(?P<timestamp>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
_LONG_FRAME = re.compile(
    rf"^\s*\({_TIMESTAMP}\)\s+"
    r"(?P<interface>\S+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]{1,8})\s+"
    r"\[(?P<dlc>\d{1,2})\]"
    r"(?:\s+(?P<data>[0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})*))?\s*$"
)
_COMPACT_FRAME = re.compile(
    rf"^\s*\({_TIMESTAMP}\)\s+"
    r"(?P<interface>\S+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]{1,8})#(?P<data>(?:[0-9A-Fa-f]{2})*)\s*$"
)


@dataclass(frozen=True)
class Frame:
    timestamp: float
    interface: str
    can_id: int
    id_bits: int
    payload: bytes

    @property
    def dlc(self) -> int:
        return len(self.payload)


@dataclass
class IdStats:
    can_id: int
    id_bits: int
    count: int = 0
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    dlc_counts: Counter[int] = field(default_factory=Counter)
    baseline_payload: bytes | None = None
    changed_byte_mask: int = 0
    maximum_dlc: int = 0
    byte_minimums: list[int | None] = field(default_factory=list)
    byte_maximums: list[int | None] = field(default_factory=list)
    byte_presence_counts: list[int] = field(default_factory=list)

    def add(self, frame: Frame) -> None:
        if self.baseline_payload is None:
            self.baseline_payload = frame.payload
        else:
            comparison_length = max(len(self.baseline_payload), len(frame.payload))
            for index in range(comparison_length):
                baseline_byte = (
                    self.baseline_payload[index]
                    if index < len(self.baseline_payload)
                    else None
                )
                current_byte = frame.payload[index] if index < len(frame.payload) else None
                if baseline_byte != current_byte:
                    self.changed_byte_mask |= 1 << index

        while len(self.byte_minimums) < len(frame.payload):
            self.byte_minimums.append(None)
            self.byte_maximums.append(None)
            self.byte_presence_counts.append(0)
        for index, value in enumerate(frame.payload):
            minimum = self.byte_minimums[index]
            maximum = self.byte_maximums[index]
            self.byte_minimums[index] = value if minimum is None else min(minimum, value)
            self.byte_maximums[index] = value if maximum is None else max(maximum, value)
            self.byte_presence_counts[index] += 1

        self.count += 1
        self.first_timestamp = (
            frame.timestamp
            if self.first_timestamp is None
            else min(self.first_timestamp, frame.timestamp)
        )
        self.last_timestamp = (
            frame.timestamp
            if self.last_timestamp is None
            else max(self.last_timestamp, frame.timestamp)
        )
        self.dlc_counts[frame.dlc] += 1
        self.maximum_dlc = max(self.maximum_dlc, frame.dlc)

    def as_dict(self) -> dict[str, object]:
        duration = _duration(self.first_timestamp, self.last_timestamp)
        constant_byte_mask = 0
        for index, (minimum, maximum, presence_count) in enumerate(
            zip(self.byte_minimums, self.byte_maximums, self.byte_presence_counts)
        ):
            if presence_count == self.count and minimum is not None and minimum == maximum:
                constant_byte_mask |= 1 << index
        return {
            "can_id": self.can_id,
            "can_id_hex": _format_can_id(self.can_id, self.id_bits),
            "id_bits": self.id_bits,
            "count": self.count,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "duration_s": duration,
            "average_rate_fps": _event_rate(self.count, duration),
            "dlcs": sorted(self.dlc_counts),
            "dlc_counts": {
                str(dlc): self.dlc_counts[dlc] for dlc in sorted(self.dlc_counts)
            },
            "changed_byte_mask": self.changed_byte_mask,
            "changed_byte_mask_hex": _format_mask(
                self.changed_byte_mask, self.maximum_dlc
            ),
            "constant_byte_mask": constant_byte_mask,
            "constant_byte_mask_hex": _format_mask(constant_byte_mask, self.maximum_dlc),
            "byte_minimums": self.byte_minimums,
            "byte_maximums": self.byte_maximums,
            "byte_presence_counts": self.byte_presence_counts,
        }


def parse_frame(line: str) -> Frame | None:
    """Parse one common ``candump -ta`` line, or return ``None`` when unsupported."""
    match = _LONG_FRAME.fullmatch(line)
    if match:
        dlc = int(match.group("dlc"), 10)
        data_text = match.group("data") or ""
        payload = bytes.fromhex(data_text)
        if len(payload) != dlc:
            return None
    else:
        match = _COMPACT_FRAME.fullmatch(line)
        if not match:
            return None
        payload = bytes.fromhex(match.group("data"))

    timestamp = float(match.group("timestamp"))
    if not math.isfinite(timestamp):
        return None

    can_id_text = match.group("can_id")
    can_id = int(can_id_text, 16)
    if can_id > 0x1FFFFFFF:
        return None

    # candump renders an extended frame with eight hexadecimal ID digits.  Numeric IDs above
    # 0x7FF are necessarily extended as well.  Keeping the width signal distinguishes the rare
    # extended frame whose numeric identifier also fits in the standard 11-bit range.
    id_bits = 29 if len(can_id_text) > 3 or can_id > 0x7FF else 11
    return Frame(
        timestamp=timestamp,
        interface=match.group("interface"),
        can_id=can_id,
        id_bits=id_bits,
        payload=payload,
    )


def summarize_lines(lines: Iterable[str], source: str = "<stream>") -> dict[str, object]:
    """Summarize an iterable without retaining its frame lines or payload history."""
    total_lines = 0
    blank_lines = 0
    unparsed_lines = 0
    total_frames = 0
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    interface_counts: Counter[str] = Counter()
    format_counts: Counter[int] = Counter()
    identifiers: dict[tuple[int, int], IdStats] = {}

    for line in lines:
        total_lines += 1
        if not line.strip():
            blank_lines += 1
            continue
        frame = parse_frame(line)
        if frame is None:
            unparsed_lines += 1
            continue

        total_frames += 1
        first_timestamp = (
            frame.timestamp
            if first_timestamp is None
            else min(first_timestamp, frame.timestamp)
        )
        last_timestamp = (
            frame.timestamp
            if last_timestamp is None
            else max(last_timestamp, frame.timestamp)
        )
        interface_counts[frame.interface] += 1
        format_counts[frame.id_bits] += 1

        key = (frame.can_id, frame.id_bits)
        stats = identifiers.get(key)
        if stats is None:
            stats = IdStats(can_id=frame.can_id, id_bits=frame.id_bits)
            identifiers[key] = stats
        stats.add(frame)

    duration = _duration(first_timestamp, last_timestamp)
    id_rows = [
        identifiers[key].as_dict()
        for key in sorted(identifiers, key=lambda item: (item[0], item[1]))
    ]
    return {
        "schema_version": 2,
        "payload_statistics_warning": (
            "Per-byte minimum/maximum values can reveal raw payload content. Keep this report "
            "under tmp/ and redact reviewed evidence before promotion."
        ),
        "source": source,
        "total_lines": total_lines,
        "blank_lines": blank_lines,
        "unparsed_lines": unparsed_lines,
        "total_frames": total_frames,
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "duration_s": duration,
        "average_rate_fps": _event_rate(total_frames, duration),
        "interfaces": {
            interface: interface_counts[interface]
            for interface in sorted(interface_counts)
        },
        "frame_formats": {
            "11bit": {
                "frames": format_counts[11],
                "unique_ids": sum(row["id_bits"] == 11 for row in id_rows),
            },
            "29bit": {
                "frames": format_counts[29],
                "unique_ids": sum(row["id_bits"] == 29 for row in id_rows),
            },
        },
        "ids": id_rows,
    }


def summarize_file(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8", errors="replace") as capture:
        return summarize_lines(capture, source=str(path))


def _bounded_text_lines(
    capture: BinaryIO, byte_limit: int, state: dict[str, bool]
) -> Iterator[str]:
    """Yield complete decoded lines from at most ``byte_limit`` bytes of a binary file.

    A writer may have only partially emitted its current candump line when the byte limit is
    sampled.  Dropping that one unterminated tail makes a growing-file snapshot deterministic and
    avoids reporting an ordinary race at the boundary as an unparsed frame.
    """
    remaining = byte_limit
    while remaining > 0:
        raw_line = capture.readline(remaining)
        if not raw_line:
            break
        remaining -= len(raw_line)
        if not raw_line.endswith(b"\n"):
            state["trailing_partial_line_ignored"] = True
            break
        yield raw_line.decode("utf-8", errors="replace")


def summarize_file_snapshot(path: Path) -> dict[str, object]:
    """Summarize only bytes present when the file is opened, even if it keeps growing."""
    with path.open("rb") as capture:
        byte_limit = os.fstat(capture.fileno()).st_size
        state = {"trailing_partial_line_ignored": False}
        summary = summarize_lines(
            _bounded_text_lines(capture, byte_limit, state), source=str(path)
        )
    summary["snapshot"] = {
        "byte_limit": byte_limit,
        "trailing_partial_line_ignored": state["trailing_partial_line_ignored"],
    }
    return summary


def print_human(summary: dict[str, object], output: TextIO | None = None) -> None:
    if output is None:
        output = sys.stdout
    total_frames = int(summary["total_frames"])
    unparsed_lines = int(summary["unparsed_lines"])
    blank_lines = int(summary["blank_lines"])
    duration = summary["duration_s"]
    rate = summary["average_rate_fps"]
    formats = summary["frame_formats"]

    print(f"Capture: {summary['source']}", file=output)
    print(
        f"Frames: {total_frames} parsed; {unparsed_lines} unparsed; "
        f"{blank_lines} blank ({summary['total_lines']} input lines)",
        file=output,
    )
    if total_frames:
        print(
            f"Time: {float(summary['first_timestamp']):.6f} to "
            f"{float(summary['last_timestamp']):.6f}; "
            f"span={float(duration):.6f}s; mean-rate={_display_rate(rate)} fps",
            file=output,
        )
    else:
        print("Time: no parsed frames", file=output)
    print(
        "Formats: "
        f"11-bit={formats['11bit']['frames']} frames/{formats['11bit']['unique_ids']} IDs; "
        f"29-bit={formats['29bit']['frames']} frames/{formats['29bit']['unique_ids']} IDs",
        file=output,
    )
    interfaces = summary["interfaces"]
    interface_text = ", ".join(
        f"{interface}={count}" for interface, count in interfaces.items()
    ) or "none"
    print(f"Interfaces: {interface_text}", file=output)
    print(
        "\nID          bits       count  first timestamp      last timestamp  "
        "DLCs       changed-byte mask",
        file=output,
    )
    for row in summary["ids"]:
        dlcs = ",".join(str(dlc) for dlc in row["dlcs"])
        print(
            f"{row['can_id_hex']:<11} {row['id_bits']:>4} {row['count']:>11}  "
            f"{row['first_timestamp']:>16.6f}  {row['last_timestamp']:>16.6f}  "
            f"{dlcs:<10} {row['changed_byte_mask_hex']}",
            file=output,
        )


def _duration(first_timestamp: float | None, last_timestamp: float | None) -> float | None:
    if first_timestamp is None or last_timestamp is None:
        return None
    return max(0.0, last_timestamp - first_timestamp)


def _event_rate(count: int, duration: float | None) -> float | None:
    # N timestamped events contain N-1 observed intervals.  This reports 1 Hz for two frames
    # exactly one second apart and avoids claiming an infinite rate for a one-frame capture.
    if count < 2 or duration is None or duration <= 0:
        return None
    return (count - 1) / duration


def _display_rate(rate: object) -> str:
    return "n/a" if rate is None else f"{float(rate):.3f}"


def _format_can_id(can_id: int, id_bits: int) -> str:
    width = 3 if id_bits == 11 else 8
    return f"{can_id:0{width}X}"


def _format_mask(mask: int, maximum_dlc: int) -> str:
    width = max(2, (maximum_dlc + 3) // 4)
    return f"0x{mask:0{width}X}"


def write_json(path: Path, summary: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump(summary, output, indent=2, sort_keys=False)
        output.write("\n")


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description=(
            "Stream and summarize a saved candump -ta log. This is an offline-only tool; "
            "it never touches SocketCAN or system services."
        ),
        epilog=(
            "Changed-byte-mask bit N corresponds to payload byte N. JSON byte minima/maxima "
            "can reveal payload content, so keep reports under tmp/ and redact before promotion. "
            "JSON is written only when an explicit --json PATH is supplied."
        ),
    )
    argument_parser.add_argument("capture", type=Path, help="saved candump log to read")
    argument_parser.add_argument(
        "--json",
        type=Path,
        metavar="PATH",
        help="also write the complete summary to this explicit path",
    )
    argument_parser.add_argument(
        "--snapshot",
        action="store_true",
        help=(
            "bound input to the file size observed at open time; use this for a capture that "
            "is still growing"
        ),
    )
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.json is not None:
        try:
            if args.capture.resolve() == args.json.resolve():
                print("error: --json path must not overwrite the input capture", file=sys.stderr)
                return 2
        except OSError:
            # Opening either path below will provide the actionable error.
            pass

    try:
        summary = (
            summarize_file_snapshot(args.capture)
            if args.snapshot
            else summarize_file(args.capture)
        )
        if args.json is not None:
            write_json(args.json, summary)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_human(summary)
    if args.snapshot:
        snapshot = summary["snapshot"]
        suffix = "; trailing partial line ignored" if snapshot["trailing_partial_line_ignored"] else ""
        print(f"\nSnapshot boundary: {snapshot['byte_limit']} bytes{suffix}")
    if args.json is not None:
        print(f"\nJSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
