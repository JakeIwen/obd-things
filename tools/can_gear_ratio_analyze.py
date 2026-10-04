#!/usr/bin/env python3
"""Offline 0x1F7 shaft-ratio evidence against shortlisted 0x1F4 nibbles.

No CAN interface access or guessed PRND/gear labels. Source decodes come from
the installed-TCM 2102/2103 correlations in docs/bus-map.md. Results remain
exploratory until physical gear ratios and independent legs are compared.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.can_capture_summary import parse_frame
from tools.can_event_window import capture_lines
from lib import broadcast_signals

SHAFT_ID = broadcast_signals.TRANSMISSION_OUTPUT_SPEED.can_id
CANDIDATE_ID = 0x1F4
MAX_AGE_SECONDS = 0.1
MIN_SHAFT_RPM = 200
RATIO_BOUNDS = (0.25, 6.0)


def shaft_ratio(payload: bytes) -> float | None:
    if len(payload) != 8:
        raise ValueError("0x1F7 must have the established DLC 8")
    output = (((payload[0] & 1) << 16) | int.from_bytes(payload[1:3], "big")) / 32
    turbine = int.from_bytes(payload[4:6], "big") / 2
    if output < MIN_SHAFT_RPM or turbine < MIN_SHAFT_RPM:
        return None
    ratio = turbine / output
    return ratio if RATIO_BOUNDS[0] <= ratio <= RATIO_BOUNDS[1] else None


def distribution(counter: Counter) -> dict:
    count = sum(counter.values())
    if not count:
        return {"samples": 0}
    mean = sum(key * n for key, n in counter.items()) / count / 1000
    total = 0
    median = None
    for key, n in sorted(counter.items()):
        total += n
        if total * 2 >= count:
            median = key / 1000
            break
    return {
        "samples": count, "mean_ratio": mean, "median_ratio": median,
        "minimum_ratio": min(counter) / 1000, "maximum_ratio": max(counter) / 1000,
        "modes": [{"ratio": key / 1000, "samples": n}
                  for key, n in counter.most_common(8)],
    }


def analyze(path: Path, channel: str) -> dict:
    latest = None
    histogram = Counter()
    counts = Counter()
    groups = [defaultdict(Counter) for _ in range(16)]
    first = last = None
    with capture_lines(path) as stream:
        for line in stream:
            if "DROPCOUNT" in line.upper():
                raise ValueError("capture reports socket loss; refusing a silent qualification")
            frame = parse_frame(line)
            if frame is None:
                counts["non_frame_lines"] += bool(line.strip())
                continue
            counts["frames"] += 1
            if frame.interface != channel or frame.id_bits != 11:
                continue
            if frame.can_id == CANDIDATE_ID:
                if len(frame.payload) != 8:
                    raise ValueError("shortlisted 0x1F4 stream must have DLC 8")
                latest = frame
                counts["candidate_frames"] += 1
            elif frame.can_id == SHAFT_ID:
                counts["shaft_frames"] += 1
                ratio = shaft_ratio(frame.payload)
                if ratio is None:
                    counts["excluded_low_speed_or_ratio"] += 1
                    continue
                first = frame.timestamp if first is None else first
                last = frame.timestamp
                quantized = round(ratio * 1000)
                histogram[quantized] += 1
                if latest is None or not 0 <= frame.timestamp - latest.timestamp <= MAX_AGE_SECONDS:
                    counts["without_fresh_candidate"] += 1
                    continue
                counts["matched"] += 1
                for byte_index, value in enumerate(latest.payload):
                    groups[2 * byte_index][value & 15][quantized] += 1
                    groups[2 * byte_index + 1][value >> 4][quantized] += 1
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    fields = []
    for index, assignments in enumerate(groups):
        fields.append({
            "byte": index // 2, "nibble": "high" if index % 2 else "low",
            "dbc_start_bit": index // 2 * 8 + (7 if index % 2 else 3),
            "length_bits": 4, "byte_order": "big",
            "values": {str(value): distribution(counter)
                       for value, counter in sorted(assignments.items())},
        })
    return {"source": str(path), "sha256": digest.hexdigest(), "channel": channel,
            "counts": dict(counts), "first_ratio_at": first, "last_ratio_at": last,
            "ratios": distribution(histogram), "candidate_fields": fields}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", type=Path, nargs="+")
    parser.add_argument("--capture-channel", required=True)
    parser.add_argument("--output", type=Path, default=REPO / "tmp/ecu_mapping/gear-ratios.json")
    args = parser.parse_args(argv)
    if not 1 <= len(args.captures) <= 4:
        parser.error("provide one to four finalized captures")
    try:
        report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                  "offline_only": True, "evidence_tier": "exploratory_candidate",
                  "telemetry_promotion_allowed": False,
                  "reference": "0x1F7 turbine rpm / output-shaft rpm; established TCM 2102/2103 scales",
                  "limits": {"minimum_each_shaft_rpm": MIN_SHAFT_RPM,
                             "ratio_bounds": RATIO_BOUNDS, "max_candidate_age_seconds": MAX_AGE_SECONDS},
                  "warning": "No PRND/direction or gear-number meaning is asserted. Low speed and clutch transitions need separate treatment.",
                  "captures": [analyze(path, args.capture_channel) for path in args.captures]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as destination:
            json.dump(report, destination, indent=2, sort_keys=True)
            destination.write("\n")
        print(f"Wrote {len(report['captures'])} separate candidate reports to {args.output}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
