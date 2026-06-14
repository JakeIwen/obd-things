#!/usr/bin/env python3
"""Live ACC-radar (Bosch MRR1evo / DASM) alignment + health view.

    python3 projects/radar/radar_acc_live.py          # 5 Hz (default)
    python3 projects/radar/radar_acc_live.py 0.5      # override refresh interval (seconds)

~20 UDS reads/s, <2% extra bus load - usable as a live aiming gauge while adjusting the mount.
DID provenance, scaling caveats, and the AlfaOBD cross-check are in findings/ and docs/.
"""
import os
import sys

# locate repo root (dir containing lib/) regardless of how deep this script lives
_root = os.path.dirname(os.path.abspath(__file__))
while _root != os.path.dirname(_root) and not os.path.isdir(os.path.join(_root, "lib")):
    _root = os.path.dirname(_root)
sys.path.insert(0, _root)
from lib.modules import MODULES
from live_data.live_data import run, Metric, s16, s32, u8

MILLIDEG = 1.0 / 1000.0           # raw int / 1000 -> degrees   (inferred)
MICRODEG = 1.0 / 1_000_000        # raw int / 1e6  -> degrees   (inferred)
SPEC_DEG = 1.0                    # ~ Bosch class static-alignment window

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

if __name__ == "__main__":
    run(MODULES["radar_acc"], METRICS, title="radar_acc", spec_deg=SPEC_DEG, refresh_hz=5.0)
