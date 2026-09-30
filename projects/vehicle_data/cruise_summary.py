#!/usr/bin/env python3
"""File-only cruise summary reducer; submit real captures through van-compute.

Intervals describe the recorded cluster display, not physical control output.
Missing frames are unknown. The service-brake bit remains a candidate, so its
cancel association is explicitly provisional rather than an exact cause.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import hashlib
import json
import math
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.vehicle_data import ccan_powertrain as cp
from tools.can_capture_summary import parse_frame
from tools.can_event_window import capture_lines, crc8_sae_j1850

VERSION = 1
IDS = frozenset((0x5A0, 0x2FA, 0x0E0, 0x1FA))
STATE_MAX_AGE = 2.0
LIMIT_MAX_AGE = 0.5


def reduce_frames(frames, *, start, end):
    """Integrate ordered (epoch, SFF identifier, payload) rows within one trip."""
    if not (math.isfinite(start) and math.isfinite(end) and start < end):
        raise ValueError("invalid completed-trip time window")
    totals = Counter()
    counts = Counter()
    faults = Counter()
    last_t = start
    state = {}
    state_at = float("-inf")
    limit = None
    limit_at = float("-inf")
    previous_named = None
    previous_named_at = float("-inf")
    button = None
    button_at = float("-inf")
    brake = None
    brake_at = float("-inf")
    events = deque()
    pending = deque()
    cancels = Counter(button=0, brake_associated=0, unknown=0, ambiguous=0)
    last_input = float("-inf")
    raw3_at = None

    def integrate(until):
        nonlocal last_t
        until = min(end, max(start, until))
        if until <= last_t:
            return
        # Split at actual signal expiry, never carry a state through a gap.
        points = sorted({last_t, until, *[x for x in (state_at + STATE_MAX_AGE,
                         limit_at + LIMIT_MAX_AGE) if last_t < x < until]})
        for a, b in zip(points, points[1:]):
            seconds = b - a
            known = a < state_at + STATE_MAX_AGE and "acc.state" in state
            if not known:
                totals["unknown_seconds"] += seconds
                continue
            totals["coverage_seconds"] += seconds
            st = state["acc.state"]
            if st == "engaged":
                totals["engaged_seconds"] += seconds
            if st == "override":
                totals["override_seconds"] += seconds
            if st in ("engaged", "override") and state.get("acc.lead_vehicle") is True:
                totals["following_seconds"] += seconds
            speed = state.get("acc.set_speed")
            if st == "engaged" and speed is not None:
                if limit is not None and a < limit_at + LIMIT_MAX_AGE:
                    delta = speed - limit
                    totals["paired_seconds"] += seconds
                    totals["delta_mph_seconds"] += delta * seconds
                    totals["set_mph_seconds"] += speed * seconds
                    totals["limit_mph_seconds"] += limit * seconds
                    totals["above_seconds" if delta > 0 else "below_seconds" if delta < 0 else "equal_seconds"] += seconds
                else:
                    totals["unpaired_seconds"] += seconds
        last_t = until

    def finish_cancels(now):
        while pending and pending[0] + 0.15 < now:
            at = pending.popleft()
            near = {kind for t, kind in events if at - 1.0 <= t <= at + 0.15}
            kind = "ambiguous" if len(near) > 1 else next(iter(near), "unknown")
            cancels[kind] += 1
        while events and events[0][0] < now - 3:
            events.popleft()

    for at, can_id, data in frames:
        if not math.isfinite(at) or at < last_input:
            raise ValueError("capture frames are not chronological")
        last_input = at
        if at < start or at > end or can_id not in IDS:
            continue
        integrate(at)
        counts[f"0x{can_id:03X}"] += 1
        if can_id == 0x5A0:
            if len(data) != 8:
                faults["invalid_display_dlc"] += 1
                state = {}
                previous_named = None
                continue
            if at - state_at > STATE_MAX_AGE and state_at != float("-inf"):
                faults["display_gaps"] += 1
                previous_named = None
            state = {o.metric: o.value for o in cp.decode_frame_observations(can_id, data)}
            state_at = at
            st = state.get("acc.state")
            if st is None:
                faults["unknown_display_state"] += 1
                if cp.acc_state_raw(data) == 3:
                    raw3_at = at if raw3_at is None else raw3_at
                else:
                    previous_named = None
                continue
            bridge = raw3_at is None or at - raw3_at <= 0.5
            if (st == "standby" and previous_named in ("engaged", "override")
                    and at - previous_named_at <= STATE_MAX_AGE and bridge):
                pending.append(at)
                # A continuously held, recently observed brake bit also counts
                # as association, without pretending it is a proven cause.
                if brake is True and at - brake_at <= 0.1:
                    events.append((at, "brake_associated"))
            raw3_at = None
            previous_named, previous_named_at = st, at
        elif can_id == 0x0E0:
            limit_at = at
            limit = data[0] if len(data) == 4 and 0 < data[0] <= 120 else None
        elif can_id == 0x2FA:
            if len(data) != 4 or data[3] != 0x0E or (data[1] & 0x70) != 0x30 or crc8_sae_j1850(data[:2]) != data[2]:
                faults["invalid_button_frame"] += 1
                button = None
                continue
            sequence = data[1] & 15
            contiguous = button is not None and at - button_at <= 0.1 and sequence == ((button[1] + 1) & 15)
            if button is not None and not contiguous:
                faults["button_discontinuities"] += 1
            pressed = bool(data[0] & 0x80)
            if contiguous and pressed and not button[0]:
                events.append((at, "button"))
            button, button_at = (pressed, sequence), at
        elif can_id == 0x1FA:
            if len(data) == 8:
                pressed = bool(data[3] & 2)
                if pressed and brake is False and at - brake_at <= 0.1:
                    events.append((at, "brake_associated"))
                brake, brake_at = pressed, at
            else:
                brake = None
                faults["invalid_brake_dlc"] += 1
        finish_cancels(at)
    integrate(end)
    finish_cancels(float("inf"))
    paired = totals["paired_seconds"]
    return {
        "duration_seconds": end - start,
        **{k: round(totals[k], 6) for k in ("coverage_seconds", "unknown_seconds", "engaged_seconds", "override_seconds", "following_seconds")},
        "coverage_percent": round(100 * totals["coverage_seconds"] / (end - start), 3),
        "cancels": dict(cancels),
        "brake_association_verified": False,
        "comparison": {
            **{k: round(totals[k], 6) for k in ("paired_seconds", "unpaired_seconds", "above_seconds", "equal_seconds", "below_seconds")},
            "mean_delta_mph": round(totals["delta_mph_seconds"] / paired, 3) if paired else None,
            "mean_set_mph": round(totals["set_mph_seconds"] / paired, 3) if paired else None,
            "mean_limit_mph": round(totals["limit_mph_seconds"] / paired, 3) if paired else None,
        },
        "frames": dict(counts), "faults": dict(faults),
    }


def iter_frames(paths, chunks):
    for path, chunk in zip(paths, chunks, strict=True):
        if path.name.endswith(".partial") or path.stat().st_size != chunk["size"]:
            raise ValueError("capture size or completion changed")
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != chunk["sha256"]:
            raise ValueError("capture hash differs from the completed manifest")
        with capture_lines(path) as lines:
            for line in lines:
                if not any(token in line for token in (" 5A0#", " 2FA#", " 0E0#", " 1FA#")):
                    continue
                frame = parse_frame(line)
                if frame and frame.id_bits == 11 and frame.interface == chunk["channel"] and frame.can_id in IDS:
                    yield frame.timestamp, frame.can_id, frame.payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=REPO / "tmp/vehicle_data/cruise-summary.json")
    parser.add_argument("captures", type=Path, nargs="+")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("schema_version") != VERSION or manifest.get("trip", {}).get("ended_us") is None:
        raise ValueError("a versioned completed-trip manifest is required")
    chunks = manifest["chunks"]
    if any(c.get("role") != "c-can" or c.get("stream") != "full" for c in chunks):
        raise ValueError("only completed C-CAN full chunks are accepted")
    trip = manifest["trip"]
    result = reduce_frames(iter_frames(args.captures, chunks), start=trip["started_us"] / 1e6, end=trip["ended_us"] / 1e6)
    result.update(schema_version=VERSION, trip=trip, chunks=chunks,
                  recording_complete=all(c.get("recording_complete") is True for c in chunks),
                  manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Trip {trip['id']}: {result['coverage_percent']}% recorded display coverage; {args.output}")


if __name__ == "__main__":
    main()
