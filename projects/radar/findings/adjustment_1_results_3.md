# Adjustment #1 — Results Drive #3: SDA SUCCESS — fault RESOLVED (2026-06-27)

**Headline: the DIY Service Drive Alignment worked. C1418-78 went from active (0x8F) to a stored-history
record (0x0E), ACC/FCW is functional again, and the fix held on the next drive.** This is the resolution
of the whole investigation. Source logs (gitignored `tmp/` + committed `findings/`):
`projects/radar/findings/sda_20260627_225708.csv` (the SDA run) and
`tmp/radar/radar_acc_drive_20260627_232202.csv` (post-SDA confirmation drive).

## What we ran
`radar_acc_sda_drive.py --arm` — start `0x0251` once, hold the session with `3E`, drive ~20 min. Two
prerequisites that finally made it work:
1. **Re-enter extended session immediately before `31 01`** — the confirm prompt had let the S3 timeout
   (~5 s) drop us to the default session, where RoutineControl returns `7F 31 7F`
   (serviceNotSupportedInActiveSession). Fixed in the script.
2. **A real, sustained drive** — mean 40 mph, max 52, **87% of samples ≥30 km/h**. The recommended
   straight/steady profile is what let the routine accumulate to completion.

## ★ DISCOVERY: `0x0251` exposes a live SDA progress counter
The **3rd byte of the `31 03 0251` routine-status response is a 0x00→0x64 (0–100%) progress counter**,
monotonic. (Status layout observed: `B0 B1 B2 B3`, e.g. `00 01 5B 00` → B2=0x5B=91%. B1 is a state byte:
`01`=running, `03`=completed at the end. B0 fluctuates (live quality/flags); B3 mostly 00.)

| progress (B2) | time | note |
|---|---|---|
| 0 → 50% | 2.6 min | fast early accumulation |
| 50 → 75% | 9.3 min | the long middle — needs sustained driving |
| 75 → 90% | 14.3 min | |
| 90 → **100% (0x64)** | **16.9 min (t+1013 s)** | **commit** |

## ★ The commit: DTC cleared exactly at 100%
In the **same sample** that B2 reached 0x64, the DTC flipped and the routine state went `01`→`03`:
```
C1418-78:  0x8F  ->  0x0E     at t+1013.4 s, progress = 100%
0x8F = testFailed + tfThisOpCycle + pending + confirmed + warningInd
0x0E =             tfThisOpCycle + pending + confirmed
       └─ testFailed (bit0) -> 0   AND   warningIndicatorRequested (bit7) -> 0   => ACC restored
```
`testFailed` and `warningInd` dropping are why ACC came back: the radar stopped actively faulting and
withdrew the "ACC unavailable" request.

## Why this worked when "just driving" (drives #1/#2) did not
Drives #1/#2 had `elev_0845` parked at +0.28° but **the routine was never started**, so online auto-align
alone never cleared an entrenched fault. With `0x0251` **active**, the radar ran its formal alignment to
100% and committed. Notably `elev_0845` **drifted +0.278° → +0.67° during the routine** — so the absolute
angle was NOT the clearing criterion; **running the routine to completion was.** The elevation DIDs read
*relative to the committed calibration*, which the SDA re-established (our old ±0.30° "spec" was never the
real gate).

## Post-SDA confirmation drive (radar_acc_drive_20260627_232202.csv, 6 min)
- **DTC stable at `0x0E` for all 360 rows** — no regression to 0x8F. The clear is durable, not momentary.
- **New boresight ≈ +0.66°, rock-steady** (range +0.6575…+0.6626 over 6 min) — the SDA's committed
  reference, holding flat. Clean log (0 garbage samples), 14.1 V, ACC functional in real use.

## Status now / optional cleanup
- **Active fault RESOLVED.** `0x0E` is a stored-history record (`confirmed`+`pending`+`tfThisOpCycle`);
  it ages out over clean ignition cycles. A one-time `14` ClearDiagnosticInformation **would stick now**
  (testFailed=0) if you want it gone immediately — purely cosmetic; ACC already works.

## Lessons banked
- **The SDA is a pure-UDS, local, DIY fix — no wiTECH, no shop, no security unlock needed.** `0x0251`
  started in session `0x03`, held with `3E`, ran to 100% over a ~17 min steady drive, committed itself.
- **Read SDA progress live** via routine-status byte[2] (0–100%). Wired into `radar_acc_sda_drive.py`
  (shows `SDA NN%`, chimes at 100%) so a future run/repro gets the signal — drive #3 succeeded but the
  driver didn't notice the DTC change mid-drive because there was no live progress/cue at the time.

(See `adjustment_1_results_1.md`/`_2.md` for the lead-up, `did_map.md` for the DID/routine reference,
`radar_acc_did_findings.md` for the full narrative.)
