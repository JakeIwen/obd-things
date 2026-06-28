# Adjustment #1 — Results Drive #2 (2026-06-27)

Second post-adjustment drive (the stability-confirmation drive). **Headline: alignment is locked in —
`elev_0845` held rock-steady at +0.278° across a fresh ignition cycle, confirming the adjustment-#1
result is stable. But DTC C1418-78 still did NOT clear, and a comms glitch exposed a false-fire bug in
the SUCCESS chime.** Source log (gitignored `tmp/`): `tmp/dumps/radar_acc_drive_20260627_221602.csv`.

## The drive
- 1132 rows, **18.9 min** (22:16:03 → 22:35:00), `tmp/CHIME` present → two-tier chime armed.
- Speed: mean ~31 mph, **56% of samples ≥40 km/h** (moving). Real city+ drive.
- Caveat: a ~73-second comms-glitch burst (see below) injected 22 garbage speed samples (254 km/h) and
  bogus voltages (0 V / 25.4 V) — discount those.

## ★ Result: `elev_0845` HELD at +0.278° (stable, no longer converging)
| metric | drive #2 | drive #1 (for contrast) |
|---|---|---|
| first / last | +0.278° / +0.278° | −0.62° / +0.28° |
| min / max / mean | +0.247° / +0.278° / **+0.2777°** | −0.62 / +0.28 / −0.135 |
| 5-min window means | +0.2765 → +0.2780 → +0.2780 → +0.2780 | monotonic climb through 0 |
| distinct values | **2** (1065× +0.278, 12× +0.247) | 1413 (live converging) |

Two things this proves:
1. **The +0.28° from drive #1 persisted across a new ignition cycle** — the stability confirmation the
   drive-#1 next-steps asked for. Two drives now agree the aim is good.
2. The signal collapsed from 1413 distinct values (actively converging) to **a near-constant +0.278°**.
   The radar has **latched +0.278° as its stored boresight** and stopped re-converging. Alignment is
   *done*: in spec (within ±0.30°, well within ±1°). Parked/idle no longer matters — it's a stored value now.

## DTC C1418-78: did NOT clear — still active (0x8F)
- Active (status `0x8F`, `testFailed` set) for **1058 / 1132 rows**, including at the end of the drive.
- 74 rows read the DTC as **absent ('')**, but that is a **comms glitch, NOT a clear**: the absent block
  [115 s – 188 s] coincides with garbage speed (254 km/h ×22) and bogus voltage (0 V / 25.4 V) — the
  `19 02 FF` response was garbled, so `c1418()` couldn't find `54 18 78`. The code re-asserted `0x8F` at
  189 s and stayed latched for the remaining ~16 min. (The single '' at the final row 1132.1 s is the
  session-teardown read.)
- **`testFailed` is still set even at a perfect, stable +0.278°** → the radar's misalignment monitor is
  not self-clearing the fault on aim alone. Two drives in, the latch has not released by driving.

## ⚠ Bug exposed: SUCCESS chime false-fired on the glitch
The transient "absent" DTC read tripped the SUCCESS tier: simulation confirms **SUCCESS would have fired
at t = 115 s** (~2 min in) on a single garbled sample, then the DTC re-asserted at 189 s. So any
`success.mp3` heard early in this drive was **spurious** — the DTC did not clear.
- **Root cause:** the SUCCESS tier has no debounce and can't distinguish "DTC genuinely absent from a
  valid `0x59` response" from "couldn't read it." A comms glitch reads as a clear.
- **FIXED (2026-06-27):** `c1418()` in `radar_acc_drive_log.py` now returns `(status, valid)` — a
  no/garbled `19 02 FF` response is `valid=False` (UNKNOWN, not a clear). SUCCESS requires a *valid*
  absent read sustained for `DTC_CLEAR_DEBOUNCE = 8` consecutive samples. **The same fix was applied to
  `radar_acc_sda_drive.py`'s clear-detect** (which previously `break`-ended the drive on one garbled
  read). Replayed against this CSV: the validity gate (not the debounce — the glitch was 74 *contiguous*
  absent reads) is what rejects it → no false fire; a genuine 8 s+ clear still fires.
- The **SETTLED tier was correct**: flat + in-spec + ~630 s moving → it would have fired
  `SETTLED:IN-SPEC` as intended.

## Interpretation
- **Alignment is mechanically complete and proven stable** (drives #1 and #2). Nothing more to gain from
  the nudge or from continued driving — `0845` is a stored constant now.
- **The fault latch is the only thing left**, and it will not self-release (testFailed still asserts at
  good aim). A blind `14` ClearDiagnosticInformation would likely just re-assert (testFailed = 1), so the
  correct finisher is the **dynamic SDA routine `0x0251`** (`radar_acc_sda_drive.py`), which formally
  re-writes the calibration and resets the fault.

## Next steps
1. ~~Fix the SUCCESS-chime debounce~~ **DONE (2026-06-27)** — valid-response gate + 8-read debounce in
   both `radar_acc_drive_log.py` and `radar_acc_sda_drive.py` (see bug section).
2. **Run the DIY SDA** (`radar_acc_sda_drive.py --arm`): start `0x0251` once, hold session with `3E`
   (never `10 03`), drive straight/steady ~30–45 mph ~15–20 min. Converges/clears → DIY fix, no wiTECH.
   Stalls on security (`7F..33`) → sniff AlfaOBD for the `27` seed/key (Fix path #2b).
3. If the SDA also won't drop the latch, a **one-time scan-tool DTC clear** is the last finisher — but
   only after alignment is confirmed (it is) and treating ACC/FCW as untrusted until proven on a
   controlled test.

(See `adjustment_1_results_1.md` for drive #1, `radar_acc_did_findings.md` for the full narrative, and
`did_map.md` for the DID/DTC reference.)
