"""Offline: save frames of finalized candump logs as per-identifier numpy arrays.

The output ``.npz`` holds, for each identifier text as written in the log
(``0FC``, ``18DAF110``), ``t_<id>`` float64 epoch timestamps, ``d_<id>``
payloads zero-padded to eight bytes (uint8, N x 8) and ``n_<id>`` the original
payload lengths. Analysis notebooks and one-off reference syncs (for example a
dash video against a drive capture) load it instead of re-parsing text.

This never opens a CAN interface or service. ``.zst`` inputs go through the
shared ``lib.candump_io.zstd_stream`` boundary; malformed lines are counted and
skipped. An optional epoch window bounds the stored frames.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np

from lib.candump_io import CandumpFormat, CandumpSyntaxError, parse_candump_line, zstd_stream


FORMAT = CandumpFormat(timestamp_syntax="decimal")


def _lines(path: Path):
    if path.suffix == ".zst":
        with zstd_stream(path, error_type=SystemExit) as process:
            yield from process.stdout
        return
    with path.open("rb") as handle:
        yield from handle


def collect(paths, start=None, end=None):
    """Return ({id: (times, payloads, lengths)}, malformed_line_count)."""
    times, payloads, lengths = defaultdict(list), defaultdict(list), defaultdict(list)
    malformed = 0
    for path in paths:
        for line in _lines(Path(path)):
            if not line.strip():
                continue
            try:
                fields = parse_candump_line(line.rstrip(b"\r\n"), format=FORMAT)
            except (CandumpSyntaxError, ValueError):
                malformed += 1
                continue
            stamp = float(fields.timestamp)
            if (start is not None and stamp < start) or (end is not None and stamp > end):
                continue
            key = fields.can_id_text.decode("ascii").upper()
            times[key].append(stamp)
            payloads[key].append(fields.payload[:8].ljust(8, b"\0"))
            lengths[key].append(len(fields.payload))
    frames = {
        key: (np.array(times[key]),
              np.frombuffer(b"".join(payloads[key]), dtype=np.uint8).reshape(-1, 8),
              np.array(lengths[key], dtype=np.uint8))
        for key in times
    }
    return frames, malformed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("captures", nargs="+", help="chronological candump -L text or .zst logs")
    parser.add_argument("--start", type=float, help="first epoch second to keep")
    parser.add_argument("--end", type=float, help="last epoch second to keep")
    parser.add_argument("--output", required=True, help="destination .npz")
    args = parser.parse_args(argv)
    frames, malformed = collect(args.captures, args.start, args.end)
    arrays = {}
    for key, (stamps, data, lengths) in frames.items():
        arrays[f"t_{key}"], arrays[f"d_{key}"], arrays[f"n_{key}"] = stamps, data, lengths
    np.savez(args.output, **arrays)
    total = sum(len(stamps) for stamps, _, _ in frames.values())
    print(f"{len(frames)} identifiers, {total} frames, {malformed} malformed lines -> {args.output}")


if __name__ == "__main__":
    main()
