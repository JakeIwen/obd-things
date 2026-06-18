# DASM (Bosch MRR1evo14F) — DID sweep findings

Radar identifies as **MRR1evo14F** (Bosch Mid-Range Radar gen-1 evo). VIN `3C6LRVDG4NE134328`.
Full read-only `22 <did>` sweep of 0x0000–0xFFFF: **56 readable DIDs, 0 locked, 0 unresolved (CLEAN)**.
Raw log: `projects/radar/dumps/radar_acc_did_sweep.txt`. Tool: `python3 tools/did_sweep.py radar_acc` (generic).

## ★ Alignment / deviation-angle candidates (stored, static across repeated reads)

| DID | raw | decoded | likely meaning |
|-----|-----|---------|----------------|
| `0841` | `FA E8` | int16 = −1304 → **−1.30°** (millideg) | **vertical (elevation) deviation** |
| `0845` | `FFECC849 FFFFE55F` | int32 pair = −1,259,959 / −6,817 → **(−1.26°, −0.007°)** (microdeg) | **(elevation, azimuth) deviation** |
| `0850` | `FFED44D4 FFFF7E86` | int32 pair = −1,227,564 / −33,146 → **(−1.23°, −0.033°)** (microdeg) | (elevation, azimuth) — 2nd copy/filtered |
| `0861` | `FE E3 00 80` | int16 = −285 / +128 | small angle pair (azimuth-ish, in spec) |

**Interpretation:** a consistent **vertical/elevation misalignment of ≈ −1.2° to −1.3°**, with
horizontal/azimuth ≈ 0°. A vertical error beyond the ~±1° class spec is exactly the active fault
**DTC C1418-78 (vertical radar misalignment)**. The values are static across repeated reads → these
are *stored* measured-deviation values, not noisy live signal.

**Scale/label NOT yet certified.** millideg vs microdeg is inferred from internal consistency + the
DTC, not from a Bosch/FCA data dictionary. **To lock it down:** open AlfaOBD's radar-alignment screen
(or its live-data list) and read its displayed "vertical/horizontal deviation" numbers, then match them
to these raw DID values. That correlation pins both the exact DID and the unit — same rigor approach we
used for routine 0x0251.

## Counters / timers (increment over time — NOT angles)
`1008`, `2008` (~0x00029A68, rising) · `200B` (0x0002996E) · `F1A1` (`00 00 00 25 98 10`).

## Status / misc live-ish
`0835` (`50`↔`51`, only DID that varied — temp/voltage/status) · `2001` (`0C 3F D2`) ·
`2010` (`FF FF FF FF` = invalid/N-A sentinel) · small flags `0851 0857 0863 2013 292E`.

## Identification block (metadata, 0xF1xx)
`F190`=VIN · `F191/F192`=`MRR1evo14F` · `F187`=`68516215AE` (Mopar PN) · `F18C`=`TD5730292062400` (serial) ·
`F194/F188`=`1037609794` · `F195`=`04 00` (SW) · `F193`=`01` (HW) · `F1A5`=`00 39 50 16 20` ·
`FD08`= build banner `SYSB_PLUS_FIAT_637MY22_R4.0_I0 … Compiled … Mon Apr 11 15:54:43 2022`.

## Next step to confirm angles
1. In AlfaOBD, open the radar live-data / alignment screen; note vertical & horizontal deviation values.
2. Re-read `0841 / 0845 / 0850 / 0861` here (`python3 tools/uds_send.py radar_acc 22 08 41`, etc.) and match.
3. Once matched, we have a verified live readout of the misalignment — useful to watch DURING a
   future `31 01 0251` alignment run (with the 120 cm mirror) to confirm it converges toward 0°.

---

# Routine 0x0251 — alignment routine mechanics (runtime-verified 2026-06-13)

Tool: `projects/radar/radar_acc_align_0251.py` (the only actuation in the repo). The earlier docs
guessed at session/param; below is what the radar actually does on the wire.

## How to drive it (VERIFIED)
- **Start:** `31 01 0251` with **NO option byte**, in **extended session 0x03** → `71 01 0251`.
  Appending an option byte (we tried `01`, copied from AlfaOBD's *different* routine `0x0250`)
  → `7F 31 31` requestOutOfRange / `7F 31 13` length. **0x0251 takes zero option bytes.**
- **Single-start lifecycle, NOT call-per-position:**
  - 2nd `31 01` while running → `7F 31 24` requestSequenceError ("already running").
  - `31 02 0251` (stopRoutine) → `71 02 0251`, returns to idle.
  - **Re-sending `10 03` (session re-entry) also RESETS the routine.** This was a real bug in
    the first runner: it re-entered the session before each "capture", and because the 0x03
    session times out (S3 ≈ 5 s) while the operator works the prompts, every capture silently
    *restarted* the routine — the status counter never advanced.
- **Status via `31 03 0251`** → `71 03 0251 <rec>`:
  - running = `01 01 00 02`, idle/stopped = `00 04 00 02`.
- **Session/security red herrings:** sessions `0x40` and `0x60` are also grantable but are NOT
  where this routine starts (we chased them after the timeout-induced `7F317F`). Security
  level 5 seed is offered in 0x03 (`27 05` → `67 05 F0A75F5A`, constant) but **0x0251 needs no
  `27` unlock** to start.

## ★ Key NEGATIVE result: static-mirror method does not drive this radar
Ran the routine correctly — start once, hold session alive with `3E`, present a flat mirror at
+2° / 0° / −2° on a **stationary** van. Result:
- status record stayed `RUNNING` (`01 01 00 02`) the entire time, indifferent to mirror moves;
- stored elevation `0845`/`0850` (≈ **−1.26°**) never changed;
- DTC **C1418-78 stayed 0x8F (active)**.

The routine **validates before committing**, so nothing bad was stored — the radar was left
byte-for-byte unchanged. But presenting a static mirror to a parked vehicle accomplishes nothing.

## Working conclusion
The misalignment angle is **radar-target-derived** (Doppler-classified stationary returns), not
read from a tilt sensor — so it needs **driving** to recompute, and a parked static mirror gives
it nothing to measure. Crucially, **−1.26° has been stable across drive cycles** (identical to the
fault-time freeze-frame at 13.9 V engine-running). If the radar could dynamically self-align this
out, normal driving would already have done so. Therefore **−1.26° vertical is most likely a real
*physical* misalignment beyond the self-align capture window** → the bracket/mount needs mechanical
correction (owner reports no field-adjustable aim screws; aim is set at the bracket-to-body mount
behind the fascia). A UDS routine alone will not zero it.

## Perturbation test — DONE (2026-06-13): no live orientation signal
Ran `projects/radar/perturb_monitor.py` while bouncing the front suspension ~1–2 in by body weight
(≈0.7° / ~700 millideg of body+radar pitch). Result: `0845`/`0850` (authoritative −1.26°) did
**not** change at all; `0841` wiggled only ~7 millideg (continuing its slow session drift, ~100×
too small to be tracking the bounce); everything else moving was counters/temp. **Conclusion: no
exposed accelerometer/inclinometer — the misalignment is target-derived and needs DRIVING to update;
a static physical nudge does not register.** Reinforces the physical-misalignment conclusion.

## First real drive — city only (2026-06-17): angle behavior
Two auto-logged city drives (~10 min + ~6 min, `tmp/dumps/radar_acc_drive_20260617_19*.csv`):
- **`elev_0845` stayed ≈ −1.2585° (moved ~2 millideg total); `elev_0850` ≈ −1.2°; DTC `0x8F` throughout.**
  The authoritative stored elevation did **not** converge toward 0 → consistent with a physical
  misalignment, **but city-only driving can't rule out dynamic** (dynamic alignment needs sustained,
  straight, higher speed — it wouldn't engage in stop-and-go anyway). **A highway run is still the
  discriminator.**
- **`0841` is a LIVE instantaneous estimate while driving** — swings ±10° around ~0 (240 distinct
  values), tracking vehicle pitch/road, *not* converging to the stored −1.26°. So `0845` = frozen
  authoritative value, `0841` = live/noisy, `0850` = intermediate.
- Broadcast (raw burst) recon: a **distance/odometer accumulator sits at CAN ID `0x101` (bytes ~2-3,
  monotonic, flat at stops)**; useful as a speed-rate ground truth. No clean direct *speed* field was
  trivially isolated in the broadcast → pursued speed via a radar DID instead (below).

## ★ Vehicle speed DID — VERIFIED (2026-06-17): `0x1002` = km/h (1 byte)
Found via the DID hunt (`did_hunt_log.py`) on a drive with sustained 40-50 mph. `0x1002` byte0 is
0 at every stop, ramps smoothly, and plateaus at 68-88 (= 42-55 mph) during the sustained stretch —
textbook speed profile, matching the reported speed. This is what AlfaOBD shows in ACC live data (the
radar consumes vehicle speed for ACC and re-exposes it). Now wired into `radar_acc_drive_log.py` as
the speed source (one read on the radar socket; OBD-II path retired). `0x1009`/`0x2009` are monotonic
counters (not speed); `0x0857` is a toggling flag.

## ★ Sustained-speed drive (2026-06-17) + OEM method (2026-06-18): alignment is SDA, not self-align
Drive with **sustained 60-89 km/h (37-55 mph) for ~10 min** (`tmp/dumps/hunt_20260617_222502.csv`):
- **`elev_0845` dead flat at −1.254°** — mean −1.2540 FAST (≥60 km/h, n=606) vs −1.2558 STOPPED (n=176);
  no speed dependence, no convergence. `elev_0850` wandered −1.16→−1.37 (not toward 0).
- OEM docs (`docs/oem/alldata_ram2022_C1418-78_and_acc_alignment.md`) show the shop method is a
  scan-tool **Service Drive Alignment (SDA)**. But SDA being the *service* method does NOT mean it's the
  only way C1418 clears — it's the deterministic, time-effective shop path (owner's point, 2026-06-18).

## ★ Owner history: ACC self-cleared once → there IS limited-range online auto-alignment (2026-06-18)
Owner reports "ACC NOT AVAILABLE" appeared on the IPC a couple times **before** the fault went permanent;
the **first occurrence cleared on its own within a couple hundred miles of normal driving, no action**
(not confirmed to be C1418, but no obvious cause). Strong implication: the radar runs a **continuous
online misalignment estimate + auto-correction with a LIMITED capture range**:
- Early, **small** deviation → driving pulled it back in range → warning cleared (self-healed).
- Now at **−1.26°** → **beyond the auto-align capture window** → can't self-correct → latched C1418-78.
  This is exactly why the sustained-50 mph drive showed `0845` frozen (auto-align won't chase a deviation
  outside its range), AND it reconciles the "is it physical?" question: **the deviation is real and the
  radar normally self-heals small ones; −1.26° is just too big.**

**Leading DIY hypothesis (no shop, no tool):** physically correct/re-seat the mount to bring the deviation
back **within the online auto-align window**, then **normal driving may auto-clear it** like it did the
first time — no SDA, no scan tool, no security unlock. Watch `0845`/`0850`/DTC via the cron logger over
normal use. Don't *rely* on it yet, but it's the path that best fits "van is home/office — no shop visits."

## ★ 2-hour drive (2026-06-18, `radar_acc_drive_20260618_143202.csv`, 7013 rows): driving alone does NOT fix it
117 min, mean 76 km/h, **60% of samples ≥80 km/h (50 mph)**, with stops — ideal auto-align conditions.
- **`elev_0845` dead flat at ≈ −1.262°** across the whole drive (10-min-window means −1.266 → −1.262, ~4
  millideg of noise, NOT convergence). `elev_0850` (noisier live estimate) wandered −1.20…−1.36, centered
  ~−1.26, **no trend toward 0**. **C1418-78 never cleared** (0x8F throughout).
- **Conclusion:** at −1.26°, **normal driving does NOT re-converge** — the deviation is beyond the online
  auto-align capture window. The earlier self-clear was a *smaller* deviation. → **the physical mount
  correction is REQUIRED, not optional; there is no "just keep driving" shortcut from here.** Once the
  deviation is physically reduced into the window, re-run a comparable drive and watch `0845` move (clean
  before/after baseline now exists). Speed via `0x1002` logged correctly (0-113 km/h) — logger fully working.

## Open / untested
- **Run SDA (the real fix):** scan tool → ACC ECU view → Misc Functions → "Service Drive Alignment
  (SDA): radar calibration" (tire pressure OK, Wi-Fi hotspot). DIY replication: start `0x0251`, keep
  session alive, do the guided drive — but SDA's internet/server side may not be reproducible with raw
  `0x0251`. Verify mounting first (`docs/oem/` TSB).
- **Get the FCA/wiTECH Promaster (RU body) radar procedure** to disambiguate static-vs-dynamic and
  the documented mechanical adjustment — before any more actuation.
