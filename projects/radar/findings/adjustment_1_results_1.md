# Adjustment #1 — Results Drive #1 (2026-06-26)

First post-adjustment drive after physically nudging the ACC radar **~1.3°**. **Headline: it worked —
the radar auto-aligned during the drive**, converging the chronic −1.26° elevation through zero.
Source log: not committed (gitignored `tmp/`); analyzed from
`tmp/radar/radar_acc_drive_20260626_232502.csv`.

## Setup / baselines
| state | `elev_0845` |
|---|---|
| Chronic fault (pre-adjustment, months) | ≈ **−1.26°** |
| Post-nudge, **parked** read (pre-drive) | ≈ −1.28° (stale — `0845` only re-measures while driving) |
| **This drive, start** | −0.62° (first sample) / −0.45° (first-5-min mean) |
| **This drive, end** | **+0.28°** (leveled) |

Note the jump from the parked −1.28° to the drive-start −0.45°: the stored value only re-measured on
the fresh ignition cycle + driving, then it kept converging. Parked reads are stale; trust driving data.

## The drive
- File `radar_acc_drive_20260626_232502.csv` — **1736 rows, 29.3 min, 0% garbage** (clean: only the cron
  logger on the bus, no contention — used `radar_acc_live.py --follow`, not the direct viewer).
- Speed: max 79 km/h (~49 mph), mean 40 km/h, 21% of samples ≥40 mph. Real city+ drive.

## ★ Result: `elev_0845` converged monotonically through 0
5-minute window means:

| window | mean `0845` | Δ |
|---|---|---|
| 0–5 min | −0.446 | — |
| 5–10 min | −0.341 | +0.105 |
| 10–15 min | −0.231 | +0.110 |
| 15–20 min | −0.135 | +0.096 |
| 20–25 min | **+0.153** | +0.288 |
| 25–30 min | **+0.275** | +0.122 |

`elev_0850` tracked it: −0.36 → +0.25 (range [−0.362, +0.302]). Full-drive `0845`: first −0.623,
last +0.278, min −0.623, max +0.282, mean −0.135, 1413 distinct values (clean live signal, not noise).

### angle vs time (ASCII)
```
elev_0845 (deg) vs time
+0.30 │                                                             ●●●●●●●●●●●●●
+0.24 │                                                         ●●●●
+0.13 │                                                    ●●●
+0.02 │··················································●·······················  (0°)
-0.09 │                                          ●●●●●●●
-0.20 │                             ●●●●●●
-0.31 │                  ●●●●
-0.36 │      ●●●●●●●●●●●●
-0.47 │    ●●
-0.58 │●
      └──────────────────────────────────────────────────────────────────────────
       0min                                                              29min
```

## Leveling-out check (the question asked)
**Yes — it leveled, but overshot zero.** It rose from −0.58°, crossed 0 at ~min 17, and **flattened at
~+0.28° for the final ~5 min**:
- last 5-min mean **+0.270°**, last 2-min mean **+0.278°** (only +0.007° over the final 2 min → flat).
- (A "slope = +0.036°/min over last 10 min" figure is the mid-drive climb at min 19–24; the very end is flat.)
- Stair-step shape: brief plateau ~−0.36° (min 3–12), climb, final plateau **+0.28°** (min 24–29).

## Interpretation
- **The nudge was the correct direction and ~right magnitude.** It brought the deviation back **inside the
  radar's online auto-align capture window**; normal driving then corrected the stored boresight from
  −1.26° to ~0. Confirms the limited-range-auto-align + "physically correct, then drive" model.
- **Slight positive overshoot** (settled +0.28°, was −1.26°). **+0.28° is well within the ±1° spec** —
  functionally aligned. The nudge was a hair too far; centering would mean easing it back ~0.3° (not
  necessary — re-touching risks knocking it out).

## Caveats / not-done
- **DTC C1418-78 still ACTIVE (`0x8F`) for all 1736 rows — did NOT clear.** Alignment is good now but the
  fault latch hasn't dropped; likely needs another ignition cycle / drive (and/or stable confirmation), or
  a one-time scan-tool clear, before ACC/FCW return.
- Single drive — convergence is clear, but stability isn't confirmed yet.

## Next steps
1. **Another clean drive** (`--follow` only, no direct viewer): does `0845` **hold ~+0.28°** (stable = good)
   and does **C1418-78 clear / ACC return**? That's the "FIXED" confirmation.
2. If it keeps drifting positive → back the nudge off slightly. If stable but DTC won't clear over a couple
   cycles → run the DIY SDA (`radar_acc_sda_drive.py`) or a one-time scan-tool DTC clear as the finisher.
3. Log results as `adjustment_1_results_2.md`, etc.

(See `radar_acc_did_findings.md` for the full narrative and `did_map.md` for the DID reference.)
