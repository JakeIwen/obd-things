#!/usr/bin/env python3
"""Offline gear-ratio clustering from extracted C-CAN 0x1F7 / 0x0FC / 0x101 frames.

Pure Python 3 stdlib. No CAN I/O, no capture decompression: inputs are the compact
JSONL frame lists written by collect_extract.py (one file per chunk, time-ordered,
``{"t": epoch_s, "id": "HEX", "d": "hex bytes"}``).

Decodes (docs/bus-map.md, C-CAN broadcast table):
  0x1F7  output-shaft speed = ((b0 & 1) << 16 | b1 << 8 | b2) / 32 rpm   (TCM DID 2103)
         turbine speed      = (b4 << 8 | b5) / 2 rpm                        (TCM DID 2102)
  0x0FC  engine speed       = ((b0 << 8 | b1) & 0xFFFC) / 4 rpm            (PCM DID 01D5)
  0x101  vehicle speed      = (((b0 & 1) << 11) | (b1 << 3) | (b2 >> 5)) / 16 km/h

Method:
  * turbine and output come from the same 0x1F7 frame; engine rpm and speed are paired to
    each 0x1F7 frame by nearest timestamp (previous-or-next 0x0FC / 0x101 frame);
  * qualifying frame: paired within --pair-max-dt s, output > 100 rpm, rpm > 500, speed > 3 mph;
  * ratio = turbine_rpm / output_rpm, histogrammed in 0.01 bins;
  * development leg: one cluster per ZF 9HP48 nominal ratio (R 3.83; 1..9 = 4.70 2.84 1.91 1.38
    1.00 0.81 0.70 0.58 0.48) is located as the histogram peak within +-10 % of nominal; the
    cluster center is the median of qualifying ratios within +-3 % of that peak; the band
    tolerance is +-3 % (about 8 sigma of the in-gear spread), widened to +-4 % / +-5 % only when
    that step captures >= 1 % more frames than the +-2 % core (coverage-vs-tolerance table is
    reported); bands are checked for overlap and split at the geometric midpoint if they touch;
  * the lookup is FROZEN (written to lookup.json) and the independent leg is scored with it
    without refitting (fraction of qualifying frames inside a band, unknown runs, unmatched
    peaks, per-gear speed/rpm/slip statistics, reverse and low-ratio sanity checks);
  * torque-converter slip = engine_rpm - turbine_rpm per 0x1F7 frame.

Outputs (under --out-dir): analysis.json, lookup.json, results.md (auto-generated numbers and
ASCII histograms), histogram_<leg>.txt, segments_<leg>.jsonl. Prints a JSON summary to stdout.

Compute-task form (one positional-free argv, single result file):
  fit   : analyze_gear_ratio.py --dev-files <dev chunk JSONL...> --result-json <result>/analysis.json
  score : analyze_gear_ratio.py --score-inputs <analysis.json or lookup.json> <indep chunk JSONL...>
                                --result-json <result>/analysis.json
--lookup / --score-inputs[0] accept either a lookup.json or a previous analysis.json (its "lookup"
member is used). --out-dir defaults to the --result-json directory.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
import time
from array import array
from collections import Counter, defaultdict
from pathlib import Path

KMH_TO_MPH = 0.621371
NOMINAL = [
    ("R", 3.83), ("1", 4.70), ("2", 2.84), ("3", 1.91), ("4", 1.38),
    ("5", 1.00), ("6", 0.81), ("7", 0.70), ("8", 0.58), ("9", 0.48),
]
GEAR_INDEX = {label: i for i, (label, _) in enumerate(NOMINAL)}  # R=0, 1..9
BIN = 0.01
HIST_MAX = 8.0
NBINS = int(round(HIST_MAX / BIN))
TOL_STEPS = (0.01, 0.02, 0.03, 0.04, 0.05, 0.07)
CANDIDATE_TOLS = (0.02, 0.03, 0.04, 0.05)


# ----------------------------------------------------------------------------- decode helpers

def decode_1f7(b: bytes):
    output_rpm = (((b[0] & 1) << 16) | (b[1] << 8) | b[2]) / 32.0
    turbine_rpm = ((b[4] << 8) | b[5]) / 2.0
    return output_rpm, turbine_rpm


def decode_0fc(b: bytes) -> float:
    return (((b[0] << 8) | b[1]) & 0xFFFC) / 4.0


def decode_101_mph(b: bytes) -> float:
    raw = ((b[0] & 1) << 11) | (b[1] << 3) | (b[2] >> 5)
    return raw / 16.0 * KMH_TO_MPH


def parse_line(line: str):
    """Fast parse of the fixed collect_extract.py JSONL layout with a json fallback."""
    parts = line.split('"')
    if len(parts) == 11 and parts[1] == "t" and parts[3] == "id" and parts[7] == "d":
        return float(parts[2][1:-1]), parts[5], parts[9]
    obj = json.loads(line)
    return float(obj["t"]), obj["id"], obj["d"]


# ----------------------------------------------------------------------------- leg loading

class Leg:
    """Compact per-0x1F7-frame arrays for one drive leg."""

    def __init__(self, name: str):
        self.name = name
        self.files: list[str] = []
        self.t = array("d")
        self.turbine = array("f")
        self.output = array("f")
        self.rpm = array("f")
        self.speed = array("f")       # mph
        self.rpm_dt = array("f")
        self.spd_dt = array("f")
        self.b0hi = array("B")        # 0x1F7 byte0 with bit0 masked off (status bits candidate)
        self.b6hi = array("B")        # 0x1F7 byte6 high nibble (low nibble is a counter)
        self.counts: Counter = Counter()
        self.first_t = None
        self.last_t = None
        self.qual = None              # array('b') 1 = qualifying
        self.ratio = None             # array('f') ratio for all frames (nan when output == 0)
        self.gear = None              # array('b') -1 unknown else GEAR index (0 = R)

    def n(self) -> int:
        return len(self.t)


def load_leg(name: str, directory: Path | None, max_files: int | None, pair_max_dt: float,
             pending_timeout: float = 1.0, explicit_files: list[Path] | None = None) -> Leg:
    leg = Leg(name)
    if explicit_files:
        files = sorted(Path(p) for p in explicit_files)
    else:
        files = sorted(p for p in directory.iterdir() if p.suffix == ".jsonl")
    if max_files is not None:
        files = files[:max_files]
    leg.files = [str(p) for p in files]
    prev_rpm = None   # (t, v)
    prev_spd = None
    pending: list[list] = []   # [t, out, turb, b0hi, b6hi, rpm, rpm_dt, spd, spd_dt]

    def resolve_field(rec, idx_val, idx_dt, prev, cur):
        cands = [c for c in (prev, cur) if c is not None]
        if not cands:
            return
        best = min(cands, key=lambda c: abs(c[0] - rec[0]))
        rec[idx_val] = best[1]
        rec[idx_dt] = abs(best[0] - rec[0])

    def flush(force_t=None):
        # emit fully resolved records from the front, preserving time order
        while pending:
            rec = pending[0]
            if rec[5] is None or rec[7] is None:
                if force_t is None or (force_t - rec[0]) < pending_timeout:
                    break
                # timed out: resolve with whatever previous values exist
                if rec[5] is None:
                    resolve_field(rec, 5, 6, prev_rpm, None)
                if rec[7] is None:
                    resolve_field(rec, 7, 8, prev_spd, None)
            pending.pop(0)
            leg.t.append(rec[0]); leg.output.append(rec[1]); leg.turbine.append(rec[2])
            leg.b0hi.append(rec[3]); leg.b6hi.append(rec[4])
            leg.rpm.append(rec[5] if rec[5] is not None else -1.0)
            leg.rpm_dt.append(rec[6] if rec[6] is not None else 9.0)
            leg.speed.append(rec[7] if rec[7] is not None else -1.0)
            leg.spd_dt.append(rec[8] if rec[8] is not None else 9.0)

    for path in files:
        print(f"[{name}] reading {path.name}", file=sys.stderr, flush=True)
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    t, cid, dhex = parse_line(line)
                    b = bytes.fromhex(dhex.replace(" ", ""))
                except (ValueError, KeyError):
                    leg.counts["unparsed_lines"] += 1
                    continue
                leg.counts["frames_" + cid] += 1
                if leg.first_t is None:
                    leg.first_t = t
                leg.last_t = t
                if cid == "1F7":
                    if len(b) != 8:
                        leg.counts["1F7_bad_dlc"] += 1
                        continue
                    out, turb = decode_1f7(b)
                    pending.append([t, out, turb, b[0] & 0xFE, b[6] >> 4, None, None, None, None])
                elif cid == "0FC":
                    if len(b) < 2:
                        leg.counts["0FC_bad_dlc"] += 1
                        continue
                    cur = (t, decode_0fc(b))
                    for rec in pending:
                        if rec[5] is None:
                            resolve_field(rec, 5, 6, prev_rpm, cur)
                    prev_rpm = cur
                elif cid == "101":
                    if len(b) < 3:
                        leg.counts["101_bad_dlc"] += 1
                        continue
                    cur = (t, decode_101_mph(b))
                    for rec in pending:
                        if rec[7] is None:
                            resolve_field(rec, 7, 8, prev_spd, cur)
                    prev_spd = cur
                else:
                    leg.counts["frames_other"] += 1
                    continue
                flush(force_t=t)
    flush(force_t=float("inf"))

    # qualification + ratio
    n = leg.n()
    leg.qual = array("b", bytes(n))
    leg.ratio = array("f", [0.0]) * n
    c = leg.counts
    for i in range(n):
        out = leg.output[i]
        turb = leg.turbine[i]
        if out > 0:
            r = turb / out
            leg.ratio[i] = r
        else:
            leg.ratio[i] = float("nan")
            c["1F7_output_zero"] += 1
        paired = leg.rpm_dt[i] <= pair_max_dt and leg.spd_dt[i] <= pair_max_dt
        if not paired:
            c["1F7_unpaired"] += 1
            continue
        if out <= 100.0:
            c["1F7_output_le_100"] += 1
            continue
        if leg.rpm[i] <= 500.0:
            c["1F7_rpm_le_500"] += 1
            continue
        if leg.speed[i] <= 3.0:
            c["1F7_speed_le_3mph"] += 1
            continue
        if turb <= 0:
            c["1F7_turbine_zero"] += 1
            continue
        leg.qual[i] = 1
        c["1F7_qualifying"] += 1
    c["1F7_total"] = n
    return leg


# ----------------------------------------------------------------------------- histogram

def histogram(leg: Leg, only_qual: bool = True):
    H = [0] * NBINS
    over = 0
    for i in range(leg.n()):
        if only_qual and not leg.qual[i]:
            continue
        r = leg.ratio[i]
        if r != r:
            continue
        k = int(r / BIN)
        if k < NBINS:
            H[k] += 1
        else:
            over += 1
    return H, over


def smooth3(H):
    n = len(H)
    return [(H[i - 1] if i > 0 else 0) + H[i] + (H[i + 1] if i + 1 < n else 0) for i in range(n)]


def percentiles(values, ps=(0, 5, 50, 95, 100)):
    if not values:
        return {f"p{p}": None for p in ps}
    s = sorted(values)
    n = len(s)
    out = {}
    for p in ps:
        idx = min(n - 1, max(0, int(round(p / 100.0 * (n - 1)))))
        out[f"p{p}"] = s[idx]
    return out


def mean_std(values):
    n = len(values)
    if n == 0:
        return None, None
    m = sum(values) / n
    v = sum((x - m) ** 2 for x in values) / n
    return m, math.sqrt(v)


# ----------------------------------------------------------------------------- lookup fit

def fit_lookup(leg: Leg, H, nominal=NOMINAL, min_peak_frac=0.0005, min_peak_abs=30):
    """Locate one cluster per nominal ratio on the development leg and freeze bands."""
    nq = sum(H)
    S = smooth3(H)
    min_peak = max(min_peak_abs, int(min_peak_frac * nq))
    clusters = {}
    # pass 1: peak bins
    for label, rn in nominal:
        lo = int((rn * 0.90) / BIN)
        hi = min(NBINS - 1, int((rn * 1.10) / BIN))
        k = max(range(lo, hi + 1), key=lambda j: S[j])
        # refine to the raw maximum next to the smoothed peak (asymmetric tails shift S by one bin)
        k = max(range(max(lo, k - 1), min(hi, k + 1) + 1), key=lambda j: H[j])
        peak = H[k]
        found = S[k] >= min_peak
        clusters[label] = {
            "label": label, "nominal": rn, "found": found,
            "peak_bin_center": round((k + 0.5) * BIN, 4), "peak_bin_count": peak,
            "peak_smoothed_count": S[k], "min_peak_required": min_peak,
        }
    # pass 2: precise center = median of qualifying ratios within +-3 % of the peak bin center
    windows = {label: [] for label in clusters}
    pk = {label: clusters[label]["peak_bin_center"] for label in clusters if clusters[label]["found"]}
    for i in range(leg.n()):
        if not leg.qual[i]:
            continue
        r = leg.ratio[i]
        for label, c in pk.items():
            if abs(r - c) <= 0.03 * c:
                windows[label].append(r)
    for label, vals in windows.items():
        cl = clusters[label]
        if not cl["found"] or not vals:
            cl["found"] = False
            continue
        vals.sort()
        cl["center"] = vals[len(vals) // 2]
        m, sd = mean_std(vals)
        cl["mean_within_3pct"] = m
        cl["std_within_3pct"] = sd
        cl["n_within_3pct_of_peak"] = len(vals)
    # pass 3: coverage vs tolerance relative to the refined center
    cov = {label: {tol: 0 for tol in TOL_STEPS} for label in clusters}
    centers = {label: clusters[label]["center"] for label in clusters if clusters[label]["found"]}
    for i in range(leg.n()):
        if not leg.qual[i]:
            continue
        r = leg.ratio[i]
        for label, c in centers.items():
            d = abs(r - c) / c
            if d <= TOL_STEPS[-1]:
                for tol in TOL_STEPS:
                    if d <= tol:
                        cov[label][tol] += 1
    for label, cl in clusters.items():
        if not cl["found"]:
            continue
        cl["coverage_vs_tolerance"] = {f"{tol:.2f}": cov[label][tol] for tol in TOL_STEPS}
        # tolerance rule: +-3 % floor (about 8 sigma of the in-gear spread), widened to 4 % / 5 % only
        # when that step captures at least 1 % more frames than the +-2 % core (a genuinely wide cluster)
        core = cov[label][0.02]
        chosen = 0.03
        for tol in (0.03, 0.04):
            nxt = TOL_STEPS[TOL_STEPS.index(tol) + 1]
            gain = cov[label][nxt] - cov[label][tol]
            if core > 0 and gain >= 0.01 * core:
                chosen = nxt
            else:
                break
        cl["tolerance_rel"] = chosen
        cl["low"] = cl["center"] * (1 - chosen)
        cl["high"] = cl["center"] * (1 + chosen)
        cl["deviation_from_nominal_pct"] = (cl["center"] / cl["nominal"] - 1) * 100
    # overlap check on sorted bands
    found = sorted((cl for cl in clusters.values() if cl["found"]), key=lambda c: c["center"])
    overlaps = []
    for a, b in zip(found, found[1:]):
        if a["high"] >= b["low"]:
            mid = math.sqrt(a["center"] * b["center"])
            overlaps.append({"lower": a["label"], "upper": b["label"], "resolved_at": mid})
            a["high"] = min(a["high"], mid - 1e-9)
            b["low"] = max(b["low"], mid + 1e-9)
    lookup = {
        "schema": "gear-ratio-lookup/1",
        "source_leg": leg.name,
        "source_files": leg.files,
        "ratio_definition": "turbine_rpm / output_rpm from one 0x1F7 frame (bytes4-5/2 over 17-bit(b0.bit0,b1,b2)/32)",
        "qualifying_frame": "paired 0x0FC/0x101 within pair_max_dt, output>100 rpm, rpm>500, speed>3 mph",
        "nominal_set": "ZF 9HP48: R 3.83; 1..9 = 4.70 2.84 1.91 1.38 1.00 0.81 0.70 0.58 0.48",
        "bands": [
            {"gear": cl["label"], "nominal": cl["nominal"], "center": cl["center"],
             "tolerance_rel": cl["tolerance_rel"], "low": cl["low"], "high": cl["high"]}
            for cl in found
        ],
        "missing_gears": [cl["label"] for cl in clusters.values() if not cl["found"]],
        "band_overlaps_resolved": overlaps,
    }
    return lookup, clusters


def band_edges(lookup):
    bands = sorted(lookup["bands"], key=lambda b: b["low"])
    edges = []
    labels = []
    for b in bands:
        edges.append(b["low"]); edges.append(b["high"])
        labels.append(b["gear"])
    return edges, labels


def assign_gears(leg: Leg, lookup):
    edges, labels = band_edges(lookup)
    idx_of = [GEAR_INDEX[l] for l in labels]
    n = leg.n()
    gear = array("b", [-1]) * n
    for i in range(n):
        if not leg.qual[i]:
            continue
        r = leg.ratio[i]
        j = bisect.bisect_right(edges, r)
        if j % 2 == 1:
            gear[i] = idx_of[j // 2]
    leg.gear = gear
    return gear


# ----------------------------------------------------------------------------- sequence analysis

def sequence_analysis(leg: Leg, debounce_frames: int = 5, gap_s: float = 2.0):
    """Debounced gear state machine over qualifying frames -> segments, transitions, unknown runs."""
    segments = []
    transitions = []
    raw_changes = 0
    unknown_runs = []
    confirmed = None
    seg_start = seg_last = None
    seg_frames = 0
    seg_unknown = 0
    cand = None
    cand_n = 0
    cand_t0 = None
    last_qual_t = None
    last_raw_gear = None
    unk_start = None
    unk_n = 0
    unk_ratios = []
    seg_end_t = None

    def close_segment(end_t):
        nonlocal confirmed, seg_start, seg_last, seg_frames, seg_unknown, seg_end_t
        if confirmed is not None and seg_start is not None:
            segments.append({
                "gear": NOMINAL[confirmed][0], "start": seg_start, "end": seg_last,
                "duration_s": seg_last - seg_start, "frames": seg_frames, "unknown_frames": seg_unknown,
            })
            seg_end_t = seg_last
        confirmed = None
        seg_start = seg_last = None
        seg_frames = 0
        seg_unknown = 0

    def close_unknown(end_t):
        nonlocal unk_start, unk_n, unk_ratios
        if unk_start is not None and unk_n > 0:
            unknown_runs.append({
                "start": unk_start, "end": end_t, "duration_s": end_t - unk_start, "frames": unk_n,
                "ratio_median": sorted(unk_ratios)[len(unk_ratios) // 2],
                "ratio_min": min(unk_ratios), "ratio_max": max(unk_ratios),
            })
        unk_start = None
        unk_n = 0
        unk_ratios = []

    for i in range(leg.n()):
        if not leg.qual[i]:
            continue
        t = leg.t[i]
        g = leg.gear[i]
        if last_qual_t is not None and t - last_qual_t > gap_s:
            close_segment(last_qual_t)
            close_unknown(last_qual_t)
            cand = None; cand_n = 0
            seg_end_t = None
            last_raw_gear = None
        last_qual_t = t
        if g != last_raw_gear and last_raw_gear is not None and g != -1 and last_raw_gear != -1:
            raw_changes += 1
        if g != -1:
            last_raw_gear = g
        if g == -1:
            if unk_start is None:
                unk_start = t
            unk_n += 1
            unk_ratios.append(leg.ratio[i])
            if confirmed is not None:
                seg_unknown += 1
            cand = None; cand_n = 0
            continue
        close_unknown(t)
        if g == confirmed:
            seg_last = t
            seg_frames += 1
            cand = None; cand_n = 0
            continue
        if g == cand:
            cand_n += 1
        else:
            cand = g; cand_n = 1; cand_t0 = t
        if cand_n >= debounce_frames:
            prev = confirmed
            prev_end = seg_last
            close_segment(prev_end)
            if prev is not None and prev_end is not None:
                transitions.append({
                    "from": NOMINAL[prev][0], "to": NOMINAL[cand][0], "t": cand_t0,
                    "transit_s": cand_t0 - prev_end,
                    "from_idx": prev, "to_idx": cand,
                })
            confirmed = cand
            seg_start = cand_t0
            seg_last = t
            seg_frames = cand_n
            seg_unknown = 0
            cand = None; cand_n = 0
    close_segment(last_qual_t)
    close_unknown(last_qual_t)
    return segments, transitions, raw_changes, unknown_runs


def summarize_sequence(segments, transitions, raw_changes, unknown_runs):
    per_gear = defaultdict(list)
    for s in segments:
        per_gear[s["gear"]].append(s["duration_s"])
    dwell = {}
    for g, durs in per_gear.items():
        durs_sorted = sorted(durs)
        dwell[g] = {
            "segments": len(durs), "total_s": sum(durs), "min_s": durs_sorted[0],
            "median_s": durs_sorted[len(durs) // 2], "max_s": durs_sorted[-1],
            "segments_lt_0.5s": sum(1 for d in durs if d < 0.5),
            "segments_lt_1s": sum(1 for d in durs if d < 1.0),
        }
    matrix = Counter((tr["from"], tr["to"]) for tr in transitions)
    up = [tr for tr in transitions if tr["from_idx"] >= 1 and tr["to_idx"] > tr["from_idx"]]
    down = [tr for tr in transitions if tr["from_idx"] >= 1 and tr["to_idx"] >= 1 and tr["to_idx"] < tr["from_idx"]]
    rd = [tr for tr in transitions if tr["from_idx"] == 0 or tr["to_idx"] == 0]
    up_step = Counter(tr["to_idx"] - tr["from_idx"] for tr in up)
    down_step = Counter(tr["from_idx"] - tr["to_idx"] for tr in down)
    transit_up = percentiles([tr["transit_s"] for tr in up], (5, 50, 95))
    transit_down = percentiles([tr["transit_s"] for tr in down], (5, 50, 95))
    # monotone-upshift check: launches = runs of consecutive transitions that start from gear 1
    launches = 0
    monotone_launches = 0
    i = 0
    while i < len(transitions):
        if transitions[i]["from"] == "1" and transitions[i]["to_idx"] > 1:
            launches += 1
            mono = True
            j = i
            while j < len(transitions) and transitions[j]["to_idx"] > transitions[j]["from_idx"]:
                if transitions[j]["to_idx"] - transitions[j]["from_idx"] != 1:
                    mono = False
                j += 1
            monotone_launches += mono
            i = max(j, i + 1)
        else:
            i += 1
    long_unknown = [u for u in unknown_runs if u["duration_s"] >= 1.0]
    return {
        "segments": len(segments), "transitions": len(transitions), "raw_gear_changes": raw_changes,
        "dwell_per_gear": dwell,
        "transition_matrix": {f"{a}->{b}": n for (a, b), n in sorted(matrix.items())},
        "upshifts": len(up), "upshift_step_counts": {str(k): v for k, v in sorted(up_step.items())},
        "upshift_sequential_fraction": (up_step.get(1, 0) / len(up)) if up else None,
        "downshifts": len(down), "downshift_step_counts": {str(k): v for k, v in sorted(down_step.items())},
        "reverse_transitions": [(tr["from"], tr["to"]) for tr in rd],
        "transit_s_upshift": transit_up, "transit_s_downshift": transit_down,
        "launches_from_gear_1": launches, "launches_with_strictly_sequential_upshifts": monotone_launches,
        "unknown_runs": len(unknown_runs),
        "unknown_runs_ge_1s": len(long_unknown),
        "unknown_frames_in_runs_ge_1s": sum(u["frames"] for u in long_unknown),
        "longest_unknown_run_s": max((u["duration_s"] for u in unknown_runs), default=0.0),
        "longest_unknown_runs": sorted(unknown_runs, key=lambda u: -u["duration_s"])[:10],
    }


# ----------------------------------------------------------------------------- per-gear stats

def per_gear_stats(leg: Leg, lookup):
    by = defaultdict(lambda: {"ratio": array("f"), "speed": array("f"), "rpm": array("f"), "slip": array("f"),
                              "b0hi": Counter(), "b6hi": Counter()})
    for i in range(leg.n()):
        if not leg.qual[i]:
            continue
        g = leg.gear[i]
        key = NOMINAL[g][0] if g >= 0 else "unknown"
        d = by[key]
        d["ratio"].append(leg.ratio[i]); d["speed"].append(leg.speed[i]); d["rpm"].append(leg.rpm[i])
        d["slip"].append(leg.rpm[i] - leg.turbine[i])
        d["b0hi"][leg.b0hi[i]] += 1
        d["b6hi"][leg.b6hi[i]] += 1
    out = {}
    for key, d in by.items():
        m, sd = mean_std(d["ratio"])
        slip = d["slip"]
        out[key] = {
            "frames": len(d["ratio"]),
            "ratio_mean": m, "ratio_std": sd,
            "ratio": percentiles(d["ratio"], (0, 5, 50, 95, 100)),
            "speed_mph": percentiles(d["speed"], (0, 5, 50, 95, 100)),
            "rpm": percentiles(d["rpm"], (0, 5, 50, 95, 100)),
            "slip_rpm": percentiles(slip, (5, 50, 95)),
            "locked_frac_abs_slip_le_20": sum(1 for s in slip if abs(s) <= 20) / len(slip),
            "locked_frac_abs_slip_le_50": sum(1 for s in slip if abs(s) <= 50) / len(slip),
            "byte0_masked_values": {f"0x{k:02X}": v for k, v in d["b0hi"].most_common(6)},
            "byte6_high_nibble_values": {f"0x{k:X}": v for k, v in d["b6hi"].most_common(6)},
        }
    return out


def unmatched_peaks(H, lookup, nq, min_frac=0.002, min_abs=20, radius=5):
    edges, labels = band_edges(lookup)
    S = smooth3(H)
    thr = max(min_abs, int(min_frac * nq))
    peaks = []
    seen = set()
    for k in range(NBINS):
        if S[k] < thr:
            continue
        lo = max(0, k - radius); hi = min(NBINS - 1, k + radius)
        if S[k] < max(S[lo:hi + 1]):
            continue
        if any(S[j] == S[k] and j < k for j in range(lo, k)):
            continue
        kk = max(range(max(0, k - 1), min(NBINS - 1, k + 1) + 1), key=lambda j: H[j])
        if kk in seen or H[kk] < thr:
            continue
        seen.add(kk)
        center = (kk + 0.5) * BIN
        j = bisect.bisect_right(edges, center)
        if j % 2 == 1:
            continue
        peaks.append({"ratio": round(center, 3), "count": H[kk], "smoothed": S[kk],
                      "nearest_nominal": min(NOMINAL, key=lambda nr: abs(nr[1] - center))[0]})
    return peaks


def standstill_analysis(leg: Leg, gap_s: float = 2.0, window_s: float = 3.0, stationary_output_rpm: float = 10.0):
    """Turbine/engine-rpm coupling k at standstill with the engine running.

    Hypothesis for the N/P publication rule: in P/N the turbine idles with the engine (k ~ 0.9-1.0);
    in D/R with the vehicle held stationary the torque converter is stalled (k ~ 0). Reports the k
    distribution over all standstill frames, k just before each launch, k just after each stop and
    k during the first seconds after engine start (known P).
    """
    n = leg.n()
    k_arr = array("f", [float("nan")]) * n
    Hk = [0] * 41  # 0.05 bins to 2.0 (+overflow)
    cats = Counter()
    first_run_t = None
    start_k = []
    for i in range(n):
        if leg.rpm[i] > 500 and leg.rpm_dt[i] <= 0.05 and leg.output[i] < stationary_output_rpm:
            k = leg.turbine[i] / leg.rpm[i]
            k_arr[i] = k
            Hk[min(40, int(k / 0.05))] += 1
            cats["stalled_lt_0.30" if k < 0.30 else ("coupled_ge_0.85" if k >= 0.85 else "intermediate")] += 1
            if first_run_t is None:
                first_run_t = leg.t[i]
            if leg.t[i] - first_run_t <= 10.0:
                start_k.append(k)

    def window_median(i, direction):
        vals = []
        j = i
        t0 = leg.t[i]
        while 0 <= j < n and abs(leg.t[j] - t0) <= window_s:
            k = k_arr[j]
            if k == k:
                vals.append(k)
            j += direction
        if not vals:
            return None
        vals.sort()
        return vals[len(vals) // 2]

    launches = []
    stops = []
    prev_q = None
    for i in range(n):
        if not leg.qual[i]:
            continue
        if prev_q is None or leg.t[i] - leg.t[prev_q] >= gap_s:
            k = window_median(i - 1, -1) if i > 0 else None
            if k is not None:
                launches.append(k)
            if prev_q is not None:
                k2 = window_median(prev_q + 1, +1) if prev_q + 1 < n else None
                if k2 is not None:
                    stops.append(k2)
        prev_q = i

    def cat_counts(vals):
        return {"n": len(vals), "stalled_lt_0.30": sum(1 for v in vals if v < 0.30),
                "intermediate": sum(1 for v in vals if 0.30 <= v < 0.85),
                "coupled_ge_0.85": sum(1 for v in vals if v >= 0.85)}

    total = sum(cats.values())
    return {
        "definition": f"engine rpm>500, output<{stationary_output_rpm:g} rpm (stationary); k = turbine_rpm/engine_rpm; "
                      f"launch/stop windows = median k of stationary frames within {window_s:g} s",
        "standstill_frames": total,
        "k_categories": dict(cats),
        "k_fractions": {c: (v / total) for c, v in cats.items()} if total else {},
        "k_histogram_0.05": {f"{i * 0.05:.2f}": Hk[i] for i in range(41) if Hk[i]},
        "engine_start_first_10s_k": percentiles(start_k, (5, 50, 95)),
        "pre_launch_k_median_1s": cat_counts(launches),
        "post_stop_k_median_1s": cat_counts(stops),
    }


def score_leg(leg: Leg, lookup, H):
    nq = leg.counts["1F7_qualifying"]
    inside = sum(1 for i in range(leg.n()) if leg.qual[i] and leg.gear[i] != -1)
    per_gear = per_gear_stats(leg, lookup)
    segments, transitions, raw_changes, unknown_runs = sequence_analysis(leg)
    seq = summarize_sequence(segments, transitions, raw_changes, unknown_runs)
    peaks = unmatched_peaks(H, lookup, nq)
    # sanity: lowest ratios only at high speed; reverse only at low speed
    sanity = {}
    for g in ("8", "9"):
        if g in per_gear:
            sp = per_gear[g]["speed_mph"]
            sanity[f"gear_{g}_speed_min_mph"] = sp["p0"]
            sanity[f"gear_{g}_speed_p5_mph"] = sp["p5"]
    for g in ("1", "2"):
        if g in per_gear:
            sanity[f"gear_{g}_speed_p95_mph"] = per_gear[g]["speed_mph"]["p95"]
            sanity[f"gear_{g}_speed_max_mph"] = per_gear[g]["speed_mph"]["p100"]
    if "R" in per_gear:
        sp = per_gear["R"]["speed_mph"]
        sanity["reverse_frames"] = per_gear["R"]["frames"]
        sanity["reverse_speed_max_mph"] = sp["p100"]
        sanity["reverse_speed_p95_mph"] = sp["p95"]
        sanity["reverse_segments"] = seq["dwell_per_gear"].get("R", {}).get("segments", 0)
        sanity["reverse_byte0_masked_values"] = per_gear["R"]["byte0_masked_values"]
        others = Counter()
        for k, d in per_gear.items():
            if k not in ("R", "unknown"):
                for hexk, v in d["byte0_masked_values"].items():
                    others[hexk] += v
        sanity["forward_byte0_masked_values"] = dict(others.most_common(6))
    else:
        sanity["reverse_frames"] = 0
    # speed ordering monotone across gears (median speed should increase with gear number)
    med = [(g, per_gear[g]["speed_mph"]["p50"]) for g, _ in NOMINAL[1:] if g in per_gear]
    sanity["median_speed_by_gear"] = {g: v for g, v in med}
    sanity["median_speed_monotone_with_gear"] = all(a[1] < b[1] for a, b in zip(med, med[1:]))
    return {
        "leg": leg.name, "files": leg.files, "first_t": leg.first_t, "last_t": leg.last_t,
        "span_s": (leg.last_t - leg.first_t) if leg.first_t is not None else None,
        "counts": dict(leg.counts),
        "qualifying_frames": nq, "frames_inside_band": inside,
        "fraction_inside_band": (inside / nq) if nq else None,
        "frames_in_no_band": nq - inside,
        "histogram_overflow_ge_8": None,
        "per_gear": per_gear, "sequence": seq, "unmatched_peaks": peaks, "sanity": sanity,
        "standstill": standstill_analysis(leg),
        "_segments": segments,
    }


# ----------------------------------------------------------------------------- ASCII plots

def ascii_overview(H, lo=0.40, hi=5.00, step=0.05, width=60):
    rows = []
    agg = int(round(step / BIN))
    k0 = int(lo / BIN); k1 = int(hi / BIN)
    vals = []
    for k in range(k0, k1, agg):
        vals.append((k * BIN, sum(H[k:k + agg])))
    mx = max((c for _, c in vals), default=0)
    lmx = math.log10(mx + 1) if mx else 1.0
    for r, c in vals:
        n = int(round(width * math.log10(c + 1) / lmx)) if mx else 0
        tags = [lab for lab, nr in NOMINAL if r <= nr < r + step]
        tag = ("  <- " + "/".join(tags)) if tags else ""
        rows.append(f"{r:5.2f} |{'#' * n:<{width}}| {c:8d}{tag}")
    return "\n".join(rows)


def ascii_zoom(H, band, half=0.06, width=50):
    c = band["center"]
    k0 = max(0, int((c - half) / BIN)); k1 = min(NBINS, int((c + half) / BIN) + 1)
    mx = max(H[k0:k1]) if k1 > k0 else 0
    rows = [f"gear {band['gear']}  center {c:.4f}  band [{band['low']:.4f}, {band['high']:.4f}]  nominal {band['nominal']}"]
    for k in range(k0, k1):
        r = k * BIN
        n = int(round(width * H[k] / mx)) if mx else 0
        mark = "*" if band["low"] <= r + BIN / 2 <= band["high"] else " "
        rows.append(f"{r:5.2f} {mark}|{'#' * n:<{width}}| {H[k]:7d}")
    return "\n".join(rows)


# ----------------------------------------------------------------------------- report

def fmt(v, nd=3):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def leg_markdown(res, H, lookup, is_dev: bool):
    L = []
    L.append(f"### Leg `{res['leg']}`\n")
    L.append(f"- files: {len(res['files'])}; span {fmt(res['span_s'], 1)} s; frames 0x1F7 {res['counts'].get('frames_1F7', 0):,}, "
             f"0x0FC {res['counts'].get('frames_0FC', 0):,}, 0x101 {res['counts'].get('frames_101', 0):,}; unparsed {res['counts'].get('unparsed_lines', 0)}")
    c = res["counts"]
    L.append(f"- 0x1F7 exclusions: unpaired {c.get('1F7_unpaired', 0):,}; output<=100 rpm {c.get('1F7_output_le_100', 0):,}; "
             f"rpm<=500 {c.get('1F7_rpm_le_500', 0):,}; speed<=3 mph {c.get('1F7_speed_le_3mph', 0):,}; turbine 0 {c.get('1F7_turbine_zero', 0):,}")
    L.append(f"- **qualifying frames {res['qualifying_frames']:,}; inside a band {res['frames_inside_band']:,} "
             f"({fmt((res['fraction_inside_band'] or 0) * 100, 2)} %); in no band {res['frames_in_no_band']:,}**")
    L.append("")
    L.append("Ratio histogram (0.05-wide rows, log bars, 0.01-bin data):\n")
    L.append("```\n" + ascii_overview(H) + "\n```\n")
    L.append("| gear | frames | ratio mean | ratio sd | ratio p5/p50/p95 | speed mph p0/p5/p50/p95/p100 | rpm p5/p50/p95 | slip rpm p5/p50/p95 | locked(|slip|<=20) |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for g, _ in NOMINAL + [("unknown", None)]:
        if g not in res["per_gear"]:
            continue
        d = res["per_gear"][g]
        r = d["ratio"]; s = d["speed_mph"]; e = d["rpm"]; sl = d["slip_rpm"]
        L.append(f"| {g} | {d['frames']:,} | {fmt(d['ratio_mean'], 4)} | {fmt(d['ratio_std'], 4)} | "
                 f"{fmt(r['p5'])}/{fmt(r['p50'])}/{fmt(r['p95'])} | {fmt(s['p0'], 1)}/{fmt(s['p5'], 1)}/{fmt(s['p50'], 1)}/{fmt(s['p95'], 1)}/{fmt(s['p100'], 1)} | "
                 f"{fmt(e['p5'], 0)}/{fmt(e['p50'], 0)}/{fmt(e['p95'], 0)} | {fmt(sl['p5'], 0)}/{fmt(sl['p50'], 0)}/{fmt(sl['p95'], 0)} | {fmt(d['locked_frac_abs_slip_le_20'] * 100, 1)} % |")
    L.append("")
    seq = res["sequence"]
    L.append(f"Sequence (debounce 5 frames = 0.1 s, gap 2 s): segments {seq['segments']}, transitions {seq['transitions']}, "
             f"raw undebounced gear changes {seq['raw_gear_changes']}; upshifts {seq['upshifts']} "
             f"(step counts {seq['upshift_step_counts']}, sequential fraction {fmt(seq['upshift_sequential_fraction'])}); "
             f"downshifts {seq['downshifts']} (step counts {seq['downshift_step_counts']}); reverse transitions {seq['reverse_transitions']}; "
             f"launches from 1st {seq['launches_from_gear_1']}, strictly sequential {seq['launches_with_strictly_sequential_upshifts']}; "
             f"transit s upshift p5/p50/p95 {fmt(seq['transit_s_upshift']['p5'])}/{fmt(seq['transit_s_upshift']['p50'])}/{fmt(seq['transit_s_upshift']['p95'])}, "
             f"downshift {fmt(seq['transit_s_downshift']['p5'])}/{fmt(seq['transit_s_downshift']['p50'])}/{fmt(seq['transit_s_downshift']['p95'])}.")
    L.append("")
    L.append("| gear | segments | total s | min s | median s | max s | segments <0.5 s | <1 s |")
    L.append("|---|---|---|---|---|---|---|---|")
    for g, _ in NOMINAL:
        d = seq["dwell_per_gear"].get(g)
        if d:
            L.append(f"| {g} | {d['segments']} | {fmt(d['total_s'], 1)} | {fmt(d['min_s'], 2)} | {fmt(d['median_s'], 2)} | {fmt(d['max_s'], 1)} | {d['segments_lt_0.5s']} | {d['segments_lt_1s']} |")
    L.append("")
    L.append(f"Transition matrix: `{seq['transition_matrix']}`\n")
    L.append(f"Unknown (no-band) runs: {seq['unknown_runs']} total, {seq['unknown_runs_ge_1s']} lasting >= 1 s "
             f"({seq['unknown_frames_in_runs_ge_1s']:,} frames), longest {fmt(seq['longest_unknown_run_s'], 2)} s.")
    if seq["longest_unknown_runs"]:
        L.append("Longest unknown runs (start, s, frames, ratio min/median/max):")
        for u in seq["longest_unknown_runs"][:6]:
            L.append(f"- t={u['start']:.3f} {fmt(u['duration_s'], 2)} s {u['frames']} fr ratio {fmt(u['ratio_min'])}/{fmt(u['ratio_median'])}/{fmt(u['ratio_max'])}")
    L.append("")
    L.append(f"Histogram peaks outside every band (>=0.2 % of qualifying frames): {res['unmatched_peaks'] if res['unmatched_peaks'] else 'none'}\n")
    L.append(f"Sanity: `{json.dumps(res['sanity'], default=str)}`\n")
    st = res["standstill"]
    L.append(f"Standstill ({st['definition']}): {st['standstill_frames']:,} frames; "
             f"turbine/rpm categories {st['k_categories']}; engine-start first 10 s k p5/p50/p95 "
             f"{fmt(st['engine_start_first_10s_k']['p5'])}/{fmt(st['engine_start_first_10s_k']['p50'])}/{fmt(st['engine_start_first_10s_k']['p95'])}; "
             f"pre-launch 1 s median k {st['pre_launch_k_median_1s']}; post-stop 1 s median k {st['post_stop_k_median_1s']}.\n")
    return "\n".join(L)


def write_results_md(out_dir: Path, args, lookup, clusters, dev_res, dev_H, ind_res, ind_H, gate, verdict):
    L = ["# analyze_gear_ratio.py results (auto-generated)\n",
         f"generated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}; args `{vars(args)}`\n"]
    if clusters is not None:
        L.append("## Development-leg cluster fit\n")
        L.append("| gear | nominal | found | peak bin | peak count | center (median +-3 %) | dev from nominal | sd within 3 % | tol | band | coverage 1/2/3/4/5/7 % |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for g, _ in NOMINAL:
            cl = clusters[g]
            if cl["found"]:
                cov = cl["coverage_vs_tolerance"]
                L.append(f"| {g} | {cl['nominal']} | yes | {cl['peak_bin_center']} | {cl['peak_bin_count']:,} | {fmt(cl['center'], 4)} | "
                         f"{fmt(cl['deviation_from_nominal_pct'], 2)} % | {fmt(cl['std_within_3pct'], 4)} | +-{cl['tolerance_rel'] * 100:.0f} % | "
                         f"[{fmt(cl['low'], 4)}, {fmt(cl['high'], 4)}] | {'/'.join(str(cov[k]) for k in ('0.01', '0.02', '0.03', '0.04', '0.05', '0.07'))} |")
            else:
                L.append(f"| {g} | {cl['nominal']} | **no** | {cl['peak_bin_center']} | {cl['peak_bin_count']} | - | - | - | - | - | - |")
        L.append("")
        if lookup["band_overlaps_resolved"]:
            L.append(f"Band overlaps resolved at geometric midpoints: {lookup['band_overlaps_resolved']}\n")
        L.append("Per-band zoom (0.01 bins, `*` marks bins inside the band):\n")
        for b in lookup["bands"]:
            L.append("```\n" + ascii_zoom(dev_H, b) + "\n```")
        L.append("")
    L.append("## Frozen lookup\n")
    L.append("| gear | center | tolerance | low | high |")
    L.append("|---|---|---|---|---|")
    for b in lookup["bands"]:
        L.append(f"| {b['gear']} | {fmt(b['center'], 4)} | +-{b['tolerance_rel'] * 100:.0f} % | {fmt(b['low'], 4)} | {fmt(b['high'], 4)} |")
    L.append("")
    if dev_res is not None:
        L.append("## Development leg\n")
        L.append(leg_markdown(dev_res, dev_H, lookup, True))
    if ind_res is not None:
        L.append("## Independent leg (frozen lookup, no refit)\n")
        L.append(leg_markdown(ind_res, ind_H, lookup, False))
        L.append("## Gate\n")
        L.append("```\n" + json.dumps(gate, indent=1, default=str) + "\n```\n")
    L.append(f"## Verdict\n\n{verdict}\n")
    (out_dir / "results.md").write_text("\n".join(L), encoding="utf-8")


# ----------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dev-dir", type=Path, help="development-leg JSONL directory (fits the lookup)")
    ap.add_argument("--indep-dir", type=Path, help="independent-leg JSONL directory (scored with the frozen lookup)")
    ap.add_argument("--dev-files", type=Path, nargs="+", help="explicit development-leg JSONL files (compute-task form)")
    ap.add_argument("--indep-files", type=Path, nargs="+", help="explicit independent-leg JSONL files (compute-task form)")
    ap.add_argument("--lookup", type=Path, help="existing frozen lookup.json or analysis.json; skips fitting (dev leg then only scored)")
    ap.add_argument("--score-inputs", type=Path, nargs="+",
                    help="compute-task form: first path = frozen lookup.json/analysis.json, remaining = independent-leg JSONL files")
    ap.add_argument("--out-dir", type=Path, help="output directory (default: directory of --result-json)")
    ap.add_argument("--result-json", type=Path, help="also write analysis.json to this exact path (compute-task {result:...})")
    ap.add_argument("--max-files", type=int, default=None, help="use only the first N chunk files per leg (smoke tests)")
    ap.add_argument("--pair-max-dt", type=float, default=0.05, help="max |dt| s for the nearest 0x0FC/0x101 pairing")
    ap.add_argument("--gate-min-inside", type=float, default=0.95)
    ap.add_argument("--tag", default="", help="free-text label stored in analysis.json")
    args = ap.parse_args(argv)
    if args.score_inputs:
        if len(args.score_inputs) < 2:
            ap.error("--score-inputs needs a lookup/analysis JSON followed by at least one JSONL file")
        if args.lookup is not None or args.indep_files or args.indep_dir is not None:
            ap.error("--score-inputs replaces --lookup/--indep-files/--indep-dir")
        args.lookup = args.score_inputs[0]
        args.indep_files = args.score_inputs[1:]
    if args.out_dir is None:
        if args.result_json is None:
            ap.error("need --out-dir or --result-json")
        args.out_dir = args.result_json.parent
    have_dev = args.dev_dir is not None or bool(args.dev_files)
    have_indep = args.indep_dir is not None or bool(args.indep_files)
    if not have_dev and args.lookup is None:
        ap.error("need --dev-dir/--dev-files (to fit) or --lookup (frozen)")
    if not have_dev and not have_indep:
        ap.error("nothing to analyze: give --dev-dir/--dev-files and/or --indep-dir/--indep-files")
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    timing = {}

    dev = ind = None
    dev_H = ind_H = None
    clusters = None
    lookup = None
    if args.lookup is not None:
        lookup = json.loads(args.lookup.read_text())
        if isinstance(lookup, dict) and lookup.get("schema") == "gear-ratio-analysis/1":
            lookup = lookup["lookup"]
        if not isinstance(lookup, dict) or lookup.get("schema") != "gear-ratio-lookup/1" or "bands" not in lookup:
            ap.error(f"{args.lookup}: not a gear-ratio-lookup/1 or gear-ratio-analysis/1 JSON")
        lookup["frozen_from"] = str(args.lookup)
    if have_dev:
        t1 = time.time()
        dev = load_leg("dev", args.dev_dir, args.max_files, args.pair_max_dt, explicit_files=args.dev_files)
        timing["load_dev_s"] = time.time() - t1
        dev_H, dev_over = histogram(dev)
        if lookup is None:
            lookup, clusters = fit_lookup(dev, dev_H)
            lookup["fit_args"] = {"pair_max_dt": args.pair_max_dt, "max_files": args.max_files, "tag": args.tag}
            (out / "lookup.json").write_text(json.dumps(lookup, indent=1), encoding="utf-8")
        assign_gears(dev, lookup)
        (out / "histogram_dev.txt").write_text(ascii_overview(dev_H, lo=0.0, hi=8.0, step=0.01, width=80) + "\n", encoding="utf-8")
    if have_indep:
        t1 = time.time()
        ind = load_leg("indep", args.indep_dir, args.max_files, args.pair_max_dt, explicit_files=args.indep_files)
        timing["load_indep_s"] = time.time() - t1
        ind_H, ind_over = histogram(ind)
        assign_gears(ind, lookup)
        (out / "histogram_indep.txt").write_text(ascii_overview(ind_H, lo=0.0, hi=8.0, step=0.01, width=80) + "\n", encoding="utf-8")

    dev_res = score_leg(dev, lookup, dev_H) if dev is not None else None
    ind_res = score_leg(ind, lookup, ind_H) if ind is not None else None
    if dev_res is not None:
        dev_res["histogram_overflow_ge_8"] = dev_over
    if ind_res is not None:
        ind_res["histogram_overflow_ge_8"] = ind_over
    for res in (dev_res, ind_res):
        if res is None:
            continue
        with open(out / f"segments_{res['leg']}.jsonl", "w", encoding="utf-8") as fh:
            for s in res.pop("_segments"):
                fh.write(json.dumps(s) + "\n")

    gate = None
    verdict = "no independent leg scored: development-only run (exploratory)"
    if ind_res is not None:
        frac = ind_res["fraction_inside_band"] or 0.0
        seq = ind_res["sequence"]
        missing = lookup.get("missing_gears", [])
        gate = {
            "gate_min_inside": args.gate_min_inside,
            "indep_fraction_inside_band": frac,
            "indep_qualifying_frames": ind_res["qualifying_frames"],
            "indep_frames_in_no_band": ind_res["frames_in_no_band"],
            "indep_unknown_runs_ge_1s": seq["unknown_runs_ge_1s"],
            "indep_longest_unknown_run_s": seq["longest_unknown_run_s"],
            "indep_unmatched_peaks": ind_res["unmatched_peaks"],
            "indep_upshift_sequential_fraction": seq["upshift_sequential_fraction"],
            "indep_median_speed_monotone_with_gear": ind_res["sanity"]["median_speed_monotone_with_gear"],
            "lookup_missing_gears": missing,
            "band_overlaps_resolved": lookup.get("band_overlaps_resolved", []),
            "pass_fraction": frac >= args.gate_min_inside,
            "pass_no_unmatched_peaks": not ind_res["unmatched_peaks"],
            "pass_no_sustained_unknown_ge_5s": seq["longest_unknown_run_s"] < 5.0,
        }
        gate["pass"] = gate["pass_fraction"] and gate["pass_no_unmatched_peaks"] and gate["pass_no_sustained_unknown_ge_5s"]
        if gate["pass"]:
            verdict = (f"PASS: {frac * 100:.2f} % of {ind_res['qualifying_frames']:,} qualifying independent-leg frames fall inside a "
                       f"frozen band (gate >= {args.gate_min_inside * 100:.0f} %), no histogram peak outside the nominal set, no sustained "
                       f"no-band run >= 5 s -> eligible as a Tier-2 operational proxy (state_detection) per docs/can-evidence-tiers.md; "
                       f"physical_identity_verified stays false, telemetry_promotion_allowed stays false.")
        else:
            verdict = (f"FAIL/INCOMPLETE: inside-band fraction {frac * 100:.2f} % (gate {args.gate_min_inside * 100:.0f} %), "
                       f"unmatched peaks {len(ind_res['unmatched_peaks'])}, longest no-band run {seq['longest_unknown_run_s']:.2f} s, "
                       f"missing gears in lookup {missing} -> not supportable as an operational proxy on this evidence; exploratory only.")

    timing["total_s"] = time.time() - t0
    analysis = {
        "schema": "gear-ratio-analysis/1", "tag": args.tag, "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "nominal": NOMINAL, "bin_width": BIN, "lookup": lookup, "clusters": clusters,
        "dev": dev_res, "indep": ind_res, "gate": gate, "verdict": verdict, "timing": timing,
        "peak_rss_kb": _peak_rss_kb(),
    }
    analysis_text = json.dumps(analysis, indent=1, default=str)
    (out / "analysis.json").write_text(analysis_text, encoding="utf-8")
    if args.result_json is not None:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        args.result_json.write_text(analysis_text, encoding="utf-8")
    write_results_md(out, args, lookup, clusters, dev_res, dev_H, ind_res, ind_H, gate, verdict)
    # The van-compute task declares every output; a fit-only or score-only run leaves the other
    # leg's files empty rather than absent so the worker accepts the result set.
    for name in ("lookup.json", "results.md", "histogram_dev.txt", "histogram_indep.txt",
                 "segments_dev.jsonl", "segments_indep.jsonl"):
        (out / name).touch(exist_ok=True)
    summary = {
        "verdict": verdict, "gate": gate,
        "lookup_bands": [{"gear": b["gear"], "center": round(b["center"], 4), "tol": b["tolerance_rel"]} for b in lookup["bands"]],
        "missing_gears": lookup.get("missing_gears", []),
        "dev": None if dev_res is None else {"qualifying": dev_res["qualifying_frames"], "inside_frac": dev_res["fraction_inside_band"],
                                              "transitions": dev_res["sequence"]["transitions"], "segments": dev_res["sequence"]["segments"]},
        "indep": None if ind_res is None else {"qualifying": ind_res["qualifying_frames"], "inside_frac": ind_res["fraction_inside_band"],
                                               "transitions": ind_res["sequence"]["transitions"], "segments": ind_res["sequence"]["segments"]},
        "timing": timing, "peak_rss_kb": analysis["peak_rss_kb"], "out_dir": str(out),
    }
    print(json.dumps(summary, indent=1, default=str))
    return 0


def _peak_rss_kb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:  # pragma: no cover
        return None


if __name__ == "__main__":
    sys.exit(main())
