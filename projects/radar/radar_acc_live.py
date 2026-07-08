#!/usr/bin/env python3
"""Live ACC-radar (Bosch MRR1evo / DASM) alignment + health view.

    python3 projects/radar/radar_acc_live.py          # 5 Hz, reads the bus directly (a TESTER)
    python3 projects/radar/radar_acc_live.py 0.5      # override refresh interval (seconds)
    python3 projects/radar/radar_acc_live.py --follow # NO bus access -- tails the newest cron
                                                      #   drive CSV (tmp/radar/radar_acc_drive_*.csv)
    python3 projects/radar/radar_acc_live.py --follow <path.csv>

Direct mode is ~20 UDS reads/s -- do NOT run it while the cron auto-logger is active (two testers on
one ISO-TP socket cross-talk). Use **--follow** during a logged drive: it only reads the CSV the cron
logger writes, so there is zero bus contention. DID provenance/scaling: findings/ + docs/.
"""
import os
import sys
import glob
import time

# locate repo root (dir containing lib/) regardless of how deep this script lives
_root = os.path.dirname(os.path.abspath(__file__))
while _root != os.path.dirname(_root) and not os.path.isdir(os.path.join(_root, "lib")):
    _root = os.path.dirname(_root)
sys.path.insert(0, _root)
from lib.modules import MODULES
from live_data.live_data import run, Metric, s16, s32, u8
# share the SETTLED detector with the drive logger so the live view tracks exactly what the chime fires on
from radar_acc_drive_log import (slope_dpm, SETTLE_MOVE_KMH, SETTLE_WINDOW_S, SETTLE_MIN_MOVE_S,
                                 SETTLE_RANGE_DEG, SETTLE_SLOPE_DPM, SPEC_DEG as SETTLE_SPEC)

MILLIDEG = 1.0 / 1000.0           # raw int / 1000 -> degrees   (inferred)
MICRODEG = 1.0 / 1_000_000        # raw int / 1e6  -> degrees   (inferred)
SPEC_DEG = 1.0                    # ~ Bosch class static-alignment window (direct-mode gauge)


def dtc_seg(dtc, GRN, RED, YEL, RST):
    """Decode the C1418-78 status byte (CSV cell) into key ISO-14229 flags. '' = absent (cleared).
    F=testFailed(0x01) C=confirmed(0x08) W=warningIndicator(0x80). testFailed set -> red (active)."""
    if dtc in ("", None):
        return f"{GRN}CLEARED?{RST}"
    try:
        b = int(dtc)
    except ValueError:
        return dtc
    flags = "".join(c for c, bit in (("F", 0x01), ("C", 0x08), ("W", 0x80)) if b & bit)
    col = RED if (b & 0x01) else YEL          # testFailed set = active (red); else dormant/maturing (amber)
    return f"{col}0x{b:02X}[{flags or '-'}]{RST}"

METRICS = [
    # --- deviation angles (inferred names/scale; see findings/radar_acc_did_findings.md) ---
    Metric(0x0841, "Vertical deviation",       lambda d: s16(d, 0), MILLIDEG, "deg"),
    Metric(0x0845, "Elevation (vertical)",     lambda d: s32(d, 0), MICRODEG, "deg"),
    Metric(0x0845, "Azimuth (horizontal)",     lambda d: s32(d, 4), MICRODEG, "deg"),
    Metric(0x0850, "Elevation (alt source)",   lambda d: s32(d, 0), MICRODEG, "deg"),
    Metric(0x0850, "Azimuth (alt source)",     lambda d: s32(d, 4), MICRODEG, "deg"),
    Metric(0x0861, "Aux angle A (uncertain)",  lambda d: s16(d, 0), MILLIDEG, "deg?"),
    Metric(0x0861, "Aux angle B (uncertain)",  lambda d: s16(d, 2), MILLIDEG, "deg?"),
    # --- VERIFIED sanity rows (matched AlfaOBD live data exactly) ---
    Metric(0x1006, "Control module voltage",   lambda d: u8(d, 0),      0.1, "V"),
    Metric(0x0835, "ECU internal temp",        lambda d: u8(d, 0) - 40, 1.0, "C"),
]

def follow_csv(path=None):
    """Tail the cron logger's drive CSV (no bus access -> no contention). Shows 0845 elevation with
    delta/direction from the start-of-drive baseline, the decoded DTC status byte (F/C/W flags), and a
    speed-gated SETTLED indicator + trailing-window slope -- the same plateau gate the logger chimes on."""
    GRN, RED, CYA, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[36m", "\033[33m", "\033[2m", "\033[0m"
    dumps_dir = os.path.join(_root, "tmp", "radar")
    newest = lambda: (sorted(glob.glob(os.path.join(dumps_dir, "radar_acc_drive_*.csv")),
                             key=os.path.getmtime) or [None])[-1]
    cur = os.path.abspath(path) if path else newest()
    if not cur or not os.path.exists(cur):
        print("No radar_acc_drive_*.csv in tmp/radar yet — start driving (the cron logger creates it), "
              "then re-run with --follow."); return
    print(f"following {cur}\n(no bus access; Ctrl-C to stop)\n")

    def fnum(v):
        try: return float(v)
        except (TypeError, ValueError): return None

    header, baseline, pos, last_growth = None, None, 0, time.time()
    move_samples, move_secs, last_move_el = [], 0.0, None   # SETTLED detector (mirrors the logger)
    sys.stdout.write("\033[?25l")
    try:
        while True:
            if time.time() - last_growth > 12:                  # current file stale -> a new drive?
                nb = newest()
                if nb and os.path.abspath(nb) != cur:
                    cur, header, baseline, pos, last_growth = os.path.abspath(nb), None, None, 0, time.time()
                    move_samples, move_secs, last_move_el = [], 0.0, None   # new drive -> reset detector
            try:
                with open(cur) as fh:
                    fh.seek(pos); lines = fh.readlines(); pos = fh.tell()
            except FileNotFoundError:
                time.sleep(0.5); continue
            if lines:
                last_growth = time.time()
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                if header is None and ln.startswith("iso_time"):
                    header = ln.split(","); continue
                if header is None:
                    continue
                r = dict(zip(header, ln.split(",")))
                e845 = fnum(r.get("elev_0845"))
                if e845 is not None and baseline is None:
                    baseline = e845
                # build the live line
                el = fnum(r.get("elapsed_s")); sp = fnum(r.get("speed_mph")); vt = fnum(r.get("volt"))
                skmh = fnum(r.get("speed_kmh"))
                e850 = fnum(r.get("elev_0850")); v41 = fnum(r.get("vert_0841")); az = fnum(r.get("azim_0845"))
                dtc = r.get("c1418", "") or ""
                seg845 = "0845 elev   ...  "
                if e845 is not None:
                    if baseline:
                        d = e845 - baseline
                        if abs(d) < 0.005:
                            col, dirlbl = CYA, "flat"
                        else:
                            conv = abs(e845) < abs(baseline)
                            col, dirlbl = (GRN, "→0") if conv else (RED, "WORSE")
                        seg845 = (f"0845 elev {col}{e845:+.4f}{RST}  Δ{d:+.4f} {col}({dirlbl}){RST}")
                    else:
                        seg845 = f"0845 elev {CYA}{e845:+.4f}{RST}"

                # SETTLED detector -- identical gate to the logger's settled-chime (speed-gated plateau)
                if e845 is not None and el is not None and (skmh or 0) >= SETTLE_MOVE_KMH:
                    if last_move_el is not None and 0 < (el - last_move_el) <= 3.0:
                        move_secs += el - last_move_el
                    last_move_el = el
                    move_samples.append((el, e845))
                    cut = el - SETTLE_WINDOW_S
                    while move_samples and move_samples[0][0] < cut:
                        move_samples.pop(0)
                elif e845 is not None:
                    last_move_el = None                          # a stop breaks moving contiguity
                settle_seg = ""
                if (len(move_samples) >= 20
                        and (move_samples[-1][0] - move_samples[0][0]) >= 0.9 * SETTLE_WINDOW_S):
                    es = [v for _, v in move_samples]; rng = max(es) - min(es); slp = abs(slope_dpm(move_samples))
                    if rng <= SETTLE_RANGE_DEG and slp <= SETTLE_SLOPE_DPM and move_secs >= SETTLE_MIN_MOVE_S:
                        tag = "IN-SPEC" if (e845 is not None and abs(e845) <= SETTLE_SPEC) else "STALLED"
                        settle_seg = f" {GRN}⟂SETTLED:{tag}{RST}"
                    else:
                        why = "" if move_secs >= SETTLE_MIN_MOVE_S else f" drv{move_secs:.0f}/{SETTLE_MIN_MOVE_S:.0f}s"
                        settle_seg = f" {DIM}slope{slp:.3f}/min{why}{RST}"
                elif (skmh or 0) >= SETTLE_MOVE_KMH:
                    settle_seg = f" {DIM}settle…{move_secs:.0f}/{SETTLE_MIN_MOVE_S:.0f}s{RST}"
                dtc_s = dtc_seg(dtc, GRN, RED, YEL, RST)
                line = (f"\r\033[K{DIM}t+{(el or 0):5.0f}s{RST} "
                        f"{(('%4.0f' % sp) if sp is not None else '  ? ')}mph "
                        f"{(('%4.1f' % vt) if vt is not None else ' ? ')}V │ {seg845} "
                        f"az{(('%+.3f' % az) if az is not None else ' n/a')} │ "
                        f"0850 {(('%+.4f' % e850) if e850 is not None else 'n/a')} │ "
                        f"0841 {(('%+.2f' % v41) if v41 is not None else 'n/a')} │ DTC {dtc_s}{settle_seg}")
                sys.stdout.write(line); sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")


if __name__ == "__main__":
    if "--follow" in sys.argv:
        i = sys.argv.index("--follow")
        p = sys.argv[i + 1] if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("-") else None
        follow_csv(p)
    else:
        run(MODULES["radar_acc"], METRICS, title="radar_acc", spec_deg=SPEC_DEG, refresh_hz=5.0)
