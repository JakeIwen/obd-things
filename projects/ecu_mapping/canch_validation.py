"""Offline, fixed-formula CAN-CH candidate checks on complete saved drives.

Run as ``python3 -m projects.ecu_mapping.canch_validation``. The four wheel
fields retain neutral names until physical corner references exist. Source
definitions here are investigation-local and never imported by live telemetry.
"""
from __future__ import annotations

import argparse
from array import array
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import correlation
from typing import TypedDict

from lib.broadcast_signals import KMH_TO_MPH, VEHICLE_SPEED
from lib.reports import atomic_json
from lib.signal_fields import SignalField
from tools.can_timeseries_correlate import (
    CliZstdDecompressor, CorrelateError, EXPLORATORY_EVIDENCE_TIER, FieldSpec, FixedFormulaConfig,
    FixedFormulaResiduals, StreamFieldSelector, StreamStats, iter_candump_frames,
)
from tools.can_workbench.adapter import identity


MATCH_US = 20_000
SCORE_INTERVAL_US = 100_000
GAP_US = 100_000
MAX_SAMPLES = 600_000
WHEEL_SCALES = {"sixteenth": 1 / 16, "giorgio": 0.0612}
REGIMES = ("stopped", "creeping", "moving")
GIO_SOURCE = (
    "https://github.com/commaai/opendbc/blob/"
    "a894cc9e29f36c115874959db196ec902d159b8f/opendbc/dbc/fca_giorgio.dbc"
)


class ValidationReport(TypedDict):
    """Versioned result contract; this report grants no live telemetry authority."""

    schema_version: int
    classification: str
    evidence_tier: str
    physical_identity_verified: bool
    telemetry_promotion_allowed: bool
    candidate_source: str
    inputs: list[dict]
    reference_streams: list[dict]
    candidate_streams: list[dict]
    reference_summary: dict
    scoring: dict
    wheels: dict
    context: dict
    steering_consistency: dict
    brake_switch: dict
    brake_threshold: dict


@dataclass(frozen=True)
class Candidate:
    name: str
    can_id: int
    dlc: int
    geometry: SignalField
    scale: float = 1
    offset: float = 0
    unit: str = "raw"

    def formula(self, channel, scale):
        selector = StreamFieldSelector(
            self.can_id, 11, self.dlc,
            FieldSpec.from_signal_field(self.geometry), channel,
        )
        return FixedFormulaConfig(selector, scale, 0)


WHEELS = tuple(Candidate(f"wheel_{start}", 0x0EE, 8,
                         SignalField(start, 13, "big")) for start in (7, 10, 29, 32))
CONTEXT = (
    Candidate("steering_angle", 0x0DE, 6, SignalField(5, 14, "big"), 0.1, -716.8, "degree_candidate"),
    Candidate("steering_rate", 0x0DE, 6, SignalField(19, 12, "big"), 0.5, -1000, "degree_per_second_candidate"),
    Candidate("brake_switch", 0x0FA, 8, SignalField(3, 1, "big")),
    Candidate("brake_threshold", 0x0FA, 8, SignalField(2, 1, "big")),
    Candidate("yaw", 0x0FE, 8, SignalField(31, 12, "big"), -0.0014, 2.86, "radian_per_second_candidate"),
)
SPECS = WHEELS + CONTEXT


@dataclass
class Samples:
    timestamps: array = field(default_factory=lambda: array("q"))
    values: array = field(default_factory=lambda: array("d"))
    wrong_dlc: int = 0
    gap_count: int = 0
    maximum_gap_us: int = 0

    def add(self, stamp, value):
        if len(self.timestamps) >= MAX_SAMPLES:
            raise ValueError("selected stream exceeds 600,000-sample bound")
        if self.timestamps:
            gap = stamp - self.timestamps[-1]
            self.maximum_gap_us = max(self.maximum_gap_us, gap)
            self.gap_count += gap > GAP_US
        self.timestamps.append(stamp)
        self.values.append(value)

    def nearest(self, stamp, radius=MATCH_US):
        pos = bisect_left(self.timestamps, stamp)
        choices = [p for p in (pos - 1, pos) if 0 <= p < len(self.timestamps)]
        if not choices:
            return None
        index = min(choices, key=lambda p: abs(self.timestamps[p] - stamp))
        if abs(self.timestamps[index] - stamp) > radius:
            return None
        return self.values[index]

    def summary(self):
        return {
            "count": len(self.values), "wrong_dlc": self.wrong_dlc,
            "minimum": min(self.values, default=None),
            "maximum": max(self.values, default=None),
            "zero_count": self.values.count(0),
            "gap_count": self.gap_count, "gap_threshold_ms": GAP_US / 1000,
            "maximum_gap_ms": self.maximum_gap_us / 1000,
            "first_timestamp_us": self.timestamps[0] if self.timestamps else None,
            "last_timestamp_us": self.timestamps[-1] if self.timestamps else None,
            "first_value": self.values[0] if self.values else None,
            "last_value": self.values[-1] if self.values else None,
        }


def load_streams(paths, channel, *, reference=False):
    specs = () if reference else SPECS
    streams = {"speed": Samples()} if reference else {s.name: Samples() for s in specs}
    stats = [StreamStats(str(p), "zstd" if p.suffix == ".zst" else "plain") for p in paths]
    selected = {can_id: tuple(s for s in specs if s.can_id == can_id)
                for can_id in {s.can_id for s in specs}}
    for frame in iter_candump_frames(paths, stats=stats,
                                     decompressor=CliZstdDecompressor(),
                                     expected_channel=channel):
        if frame.id_bits != 11:
            continue
        if reference and frame.can_id == VEHICLE_SPEED.can_id:
            if frame.dlc != 8:
                streams["speed"].wrong_dlc += 1
            else:
                value = VEHICLE_SPEED.decode(frame.payload) / KMH_TO_MPH
                streams["speed"].add(frame.timestamp_us, value)
        for spec in selected.get(frame.can_id, ()):
            stream = streams[spec.name]
            if frame.dlc != spec.dlc:
                stream.wrong_dlc += 1
                continue
            raw = spec.geometry.extract(frame.payload)
            stream.add(frame.timestamp_us, raw * spec.scale + spec.offset)
    return streams, [s.as_dict() for s in stats]


def score_points(reference):
    previous_bucket = None
    for stamp, value in zip(reference.timestamps, reference.values):
        bucket = stamp // SCORE_INTERVAL_US
        if bucket != previous_bucket:
            previous_bucket = bucket
            yield stamp, value


def regime(speed):
    return "stopped" if speed <= 0.1 else "creeping" if speed < 5 else "moving"


def wheel_check(spec, samples, references, channel):
    residuals = {(scale, group): FixedFormulaResiduals()
                 for scale in WHEEL_SCALES for group in REGIMES}
    eligible, rejected, matched = Counter(), Counter(), Counter()
    for stamp, speed in references:
        if not 0 <= speed <= 260:
            rejected["implausible_reference"] += 1
            continue
        group = regime(speed)
        eligible[group] += 1
        raw = samples.nearest(stamp)
        if raw is None:
            rejected["unmatched"] += 1
            continue
        if raw == 8191 or raw / 16 > 260:
            rejected["suspect_wheel_value"] += 1
            continue
        matched[group] += 1
        for name, scale in WHEEL_SCALES.items():
            residuals[name, group].add(raw, speed, scale=scale, intercept=0)
    reports = {}
    for name, scale in WHEEL_SCALES.items():
        reports[name] = {}
        for group in REGIMES:
            result = residuals[name, group]
            reports[name][group] = result.result(
                reference_count=eligible[group], config=spec.formula(channel, scale)
            ) if result.count else None
    return {
        **samples.summary(), "geometry": spec.geometry.as_dict(),
        "raw_all_ones_count": samples.values.count(8191),
        "raw_over_4160_count": sum(v > 4160 for v in samples.values),
        "eligible_reference_counts": dict(eligible), "matched_counts": dict(matched),
        "rejected": dict(rejected), "fixed_scale_errors_kph": reports,
    }


def association(xs, ys):
    if len(xs) < 3 or min(xs) == max(xs) or min(ys) == max(ys):
        return {"count": len(xs), "pearson_r": None}
    return {"count": len(xs), "pearson_r": correlation(xs, ys)}


def binary_transitions(samples):
    """Bound each observed edge by consecutive full-rate samples, excluding gaps."""
    edges = []
    count = 0
    previous = None
    for stamp, value in zip(samples.timestamps, samples.values):
        if previous is not None and stamp - previous[0] <= GAP_US and value != previous[1]:
            count += 1
            if len(edges) < 100:
                edges.append({"from": int(previous[1]), "to": int(value),
                              "previous_timestamp_us": previous[0], "timestamp_us": stamp})
        previous = (stamp, value)
    return {"count": count, "edges": edges, "omitted_count": count - len(edges),
            "maximum_contiguous_gap_ms": GAP_US / 1000}


def steering_holds(samples):
    """Summarize held candidate angles for joining explicit operator references."""
    runs = []
    count = 0
    start = last = None
    low = high = total = 0.0
    observations = 0

    def finish():
        nonlocal count
        if observations and last - start >= 2_000_000:
            count += 1
            if len(runs) < 100:
                runs.append({"start_timestamp_us": start, "end_timestamp_us": last,
                             "duration_seconds": (last - start) / 1_000_000,
                             "mean_candidate_degrees": total / observations,
                             "range_candidate_degrees": [low, high]})

    for stamp, value in zip(samples.timestamps, samples.values):
        if last is None or stamp - last > GAP_US or max(high, value) - min(low, value) > 0.5:
            finish()
            start = stamp
            low = high = value
            total = 0.0
            observations = 0
        low, high = min(low, value), max(high, value)
        total += value
        observations += 1
        last = stamp
    finish()
    return {"runs": runs, "omitted_count": count - len(runs),
            "minimum_hold_seconds": 2, "maximum_range_candidate_degrees": 0.5,
            "limitation": "candidate units; gaps split holds; no owner labels inferred"}


def steering_check(streams, references):
    angle, rate = streams["steering_angle"], streams["steering_rate"]
    changes, rates, angles, front, rear, yaws = [], [], [], [], [], []
    previous = None
    for stamp, speed in references:
        value, velocity = angle.nearest(stamp), rate.nearest(stamp)
        if value is None or velocity is None:
            previous = None
            continue
        if previous is not None and 50_000 <= stamp - previous[0] <= 150_000:
            derivative = (value - previous[1]) * 1_000_000 / (stamp - previous[0])
            if abs(derivative) > 5:
                changes.append(derivative)
                rates.append(velocity)
        previous = (stamp, value)
        wheels = [streams[s.name].nearest(stamp) for s in WHEELS]
        yaw = streams["yaw"].nearest(stamp)
        if 5 <= speed <= 40 and abs(value) >= 30 and yaw is not None and all(
            w is not None and 0 <= w <= 4160 for w in wheels
        ):
            angles.append(value)
            front.append((wheels[1] - wheels[0]) / 16)
            rear.append((wheels[3] - wheels[2]) / 16)
            yaws.append(yaw)
    return {
        "angle_derivative_vs_rate": association(changes, rates),
        "angle_vs_vendor_front_difference": association(angles, front),
        "angle_vs_vendor_rear_difference": association(angles, rear),
        "angle_vs_yaw": association(angles, yaws),
        "held_angles": steering_holds(angle),
        "turn_selection": "reference 5..40 km/h, abs(candidate angle) >=30; no sign refit",
        "limitation": "all comparisons except vehicle speed use unverified candidate fields",
    }


def brake_check(samples, references):
    states, phases, transitions = Counter(), Counter(), Counter()
    previous = None
    for stamp, speed in references:
        value = samples.nearest(stamp)
        if value is None:
            previous = None
            continue
        states[int(value)] += 1
        phases[f"{regime(speed)}:{int(value)}"] += 1
        if previous is not None and stamp - previous[0] <= 150_000:
            if previous[1] != value:
                transitions[f"{int(previous[1])}->{int(value)}"] += 1
        previous = (stamp, value)
    return {"sampled_states": dict(states), "by_speed_regime": dict(phases),
            "sampled_transitions": dict(transitions),
            "full_rate_transitions": binary_transitions(samples),
            "limitation": "10 Hz transitions may miss pulses; full-rate intervals are separate; no owner labels inferred"}


def run_validation(reference_paths, candidate_paths, reference_channel, candidate_channel) -> ValidationReport:
    paths = reference_paths + candidate_paths
    if not reference_paths or not candidate_paths or len(paths) > 16:
        raise ValueError("require 1..15 reference/candidate files, at most 16 total")
    if any(not p.is_file() or p.stat().st_size > 256 * 1024**2 for p in paths):
        raise ValueError("each input must be a regular finalized file <=256 MiB")
    before = [identity(p) for p in paths]
    if len({p["path"] for p in before}) != len(before):
        raise ValueError("duplicate input capture")
    reference, reference_stats = load_streams(reference_paths, reference_channel, reference=True)
    streams, candidate_stats = load_streams(candidate_paths, candidate_channel)
    references = list(score_points(reference["speed"]))
    if not references or not any(streams[s.name].timestamps for s in WHEELS):
        raise ValueError("missing speed reference or candidate wheel stream")
    report: ValidationReport = {
        "schema_version": 1, "classification": "fixed_can_ch_candidate_validation",
        "evidence_tier": EXPLORATORY_EVIDENCE_TIER, "physical_identity_verified": False,
        "telemetry_promotion_allowed": False, "candidate_source": GIO_SOURCE,
        "inputs": before, "reference_streams": reference_stats,
        "candidate_streams": candidate_stats, "reference_summary": reference["speed"].summary(),
        "scoring": {"interval_ms": SCORE_INTERVAL_US / 1000, "maximum_match_delta_ms": MATCH_US / 1000,
                    "reference_samples": len(references), "formula_refitted": False,
                    "suspect_value_policy": "report all-ones/over-260 km/h; exclude only from wheel error scores"},
        "wheels": {s.name: wheel_check(s, streams[s.name], references, candidate_channel) for s in WHEELS},
        "context": {s.name: {**streams[s.name].summary(), "unit": s.unit,
                             "geometry": s.geometry.as_dict(), "scale": s.scale, "offset": s.offset}
                    for s in CONTEXT},
        "steering_consistency": steering_check(streams, references),
        "brake_switch": brake_check(streams["brake_switch"], references),
        "brake_threshold": brake_check(streams["brake_threshold"], references),
    }
    if before != [identity(p) for p in paths]:
        raise ValueError("input changed during analysis")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--reference-count", type=int, required=True)
    parser.add_argument("--reference-channel", required=True)
    parser.add_argument("--candidate-channel", required=True)
    default = Path(__file__).resolve().parents[2] / "tmp/ecu_mapping/canch_validation"
    parser.add_argument("--output", type=Path, default=default / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + ".json"))
    args = parser.parse_args(argv)
    if not 0 < args.reference_count < len(args.captures):
        parser.error("reference-count must leave at least one candidate input")
    if args.output.exists():
        parser.error("refusing to overwrite output")
    try:
        report = run_validation(
            args.captures[:args.reference_count], args.captures[args.reference_count:],
            args.reference_channel, args.candidate_channel,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(args.output, report)
    except (ValueError, OSError, CorrelateError) as exc:
        parser.exit(2, f"ERROR: {exc}\n")
    print(f"Wrote fixed candidate checks to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
