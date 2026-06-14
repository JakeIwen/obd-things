# Agent Handoff — current state (2026-06-13)

**Read this first.** Single source of truth for picking up this repo. Background detail is in
`radar_acc_handoff.md` (original investigation), data in `../findings/` + `../dumps/`, the AlfaOBD
write-up in `radar_acc_alfaobd_bugreport.md`. Don't re-derive what's in the "VERIFIED" section.

---

## TL;DR
2022 Ram Promaster ACC radar (Bosch DASM, identifies as **MRR1evo14F**) has an **active vertical
misalignment fault (DTC C1418-78)** that disables ACC/FCW. We have a **fully working read-only
UDS link** to the radar over PCAN-USB + SocketCAN and have reverse-engineered its live data. Two
confirmed conclusions: (1) the radar-alignment routine is **`0x0251`**, not the `0x0250` AlfaOBD
calls; (2) the real misalignment angle (~**−1.2° vertical**) is readable at DIDs we found, which
AlfaOBD reports as "not supported." The repo is read-only **except for one gated actuation tool**
(`tools/radar_acc_align_0251.py`, the only `31 01` in the repo). **0x0251 mechanics are now fully
reverse-engineered** (session 0x03, no option byte, single-start lifecycle — see
`../findings/radar_acc_did_findings.md`), but running it with a **static mirror on a parked van does
nothing** (routine stays RUNNING, stored angle unchanged, DTC stays active). Current best conclusion:
−1.26° is a **physical** misalignment beyond the self-align window → a **mechanical** fix, not a UDS
routine. See Open work.

## Vehicle & goal
- 2022 Ram Promaster, VIN `3C6LRVDG4NE######`. SGW bypass installed (diagnostic writes reach modules).
- Goal: clear C1418-78 by getting the radar **vertical** alignment back in spec so ACC works.

## VERIFIED — trust these, do not re-test
- **Bus:** HS-CAN, **500 kbit/s**, OBD pins **6/14**. Only 500k yields traffic; all other rates silent.
- **Radar addressing:** UDS/ISO-TP, **29-bit normal-fixed**. TX `0x18DA2AF1`, RX `0x18DAF12A` (phys addr 0x2A, tester 0xF1).
- **Baseline reproduces exactly:** `10 03`→`50 03 00 32 01 F4`; `22 F1A5`→`62 F1A5 00 39 50 16 20`;
  `22 F18C` serial→`TD5730292062400`; `22 F191` family→`MRR1evo14F`; SW `F195`=`0400`, HW `F193`=`01`.
- **DTCs (`19 02 FF`):** 8 total; **only C1418-78 is active (status 0x8F = currently failing)**, the
  other 7 are dormant (0x40). FCA 3-byte DTC encoding (e.g. C1418-78 = `54 18 78`).
- **Routine scan CLEAN:** `31 03` over 0x0200–0x03FF+0xFF0x found exactly **one** routine: `0x0251`
  (`7F3124` = exists/not-started). `0x0250` → `7F3131` (not implemented). **0x0251 is the alignment routine.**
- **DID sweep CLEAN:** full `22` sweep 0x0000–0xFFFF = 56 readable, 0 locked, 0 unresolved (`../dumps/radar_acc_did_sweep.txt`).
- **Deviation-angle DIDs** (static stored, consistent with the active fault):
  | DID | raw | decoded (inferred scale) | meaning |
  |---|---|---|---|
  | `0x0841` | int16 | ≈ **−1.30°** (÷1000) | vertical misalignment (drifts a few millideg between sessions → likely a live online estimate) |
  | `0x0845` | 2×int32 | ≈ (**−1.26°**, −0.01°) (÷1e6) | (elevation, azimuth) |
  | `0x0850` | 2×int32 | ≈ (−1.23°, −0.03°) (÷1e6) | (elevation, azimuth), 2nd source |
  | `0x0861` | 2×int16 | (−0.285, +0.128) | aux, uncertain |
- **VERIFIED non-angle DIDs** (matched AlfaOBD exactly → proves the reads are sound):
  `0x1006` = control-module voltage ×0.1 V; `0x0835` = ECU internal temp ≈ raw−40 °C (only DID that drifts).

## Run it (commands)
```bash
./bringup.sh                              # can0 up @500k (listen-only OFF); ignition must be ON
python3 tools/radar_acc_baseline.py       # reproduce baseline + DTCs
python3 live_data/radar_acc.py            # live top-style alignment gauge @5 Hz
python3 tools/routine_scan.py radar_acc   # reconfirm 0x0251 (read-only, 31 03)
python3 tools/did_sweep.py radar_acc      # full DID sweep -> dumps/ (~15 min; partial range -> own file)
python3 tools/uds_send.py radar_acc 22 F1 A5   # ad-hoc read-only request
```
Architecture: `lib/uds.py` (generic UDS) + `lib/modules.py` (addressing registry) → `live_data/`
(base viewer + per-module sub-script) + `tools/` (generic scanners). Add a module = entry in
`modules.py` + copy `live_data/radar_acc.py`.

## Key conclusion: AlfaOBD is mis-mapped for this MY2022 variant
Same wrong-variant pattern in two places: it calls routine `0x0250` (unsupported here) instead of
`0x0251`; and its misalignment live-data gauges ("Slow/Fast misalignment angle", "Mirror sensor
vertical deviation") return **"Request not supported"** while the one vertical gauge it can read
shows ~0 — so it hides the −1.2° fault entirely. Full evidence + a ready-to-send report in
`radar_acc_alfaobd_bugreport.md` (attach the AlfaOBD screenshot when sending).

## Gotchas (these already bit us)
- **`listen-only` is sticky** across `ip link set up` — always bring up with explicit `listen-only off`
  (bringup.sh does). Symptom if wrong: RX fine, all TX silently dropped.
- **`berr-reporting` unsupported** by this PCAN adapter (`listen-only` works).
- **USB stability:** the PCAN browns out / drops on a shared root hub (undervoltage, `Rx urb aborted -32`).
  Keep it on the **powered USB hub**. The scanners auto-recover from drops; the live viewer degrades to NO DATA.
- **Ignition auto-powers-down** on its own. When asleep the powertrain broadcast stops, but the radar
  still ACKs/answers direct UDS reads — so reads work, just no bus flood. Engine running = stable ~14 V.

## Open work (priority order)
1. **Inspect the radar mount physically** — the leading conclusion is that −1.26° vertical is a real
   *physical* misalignment (stable across drive cycles, beyond the self-align window). Look behind the
   fascia for a bent/knocked bracket or a mount sitting tilted ~1.26° down. No field-adjustable aim
   screws on this unit; aim is set at the bracket-to-body mount.
2. **Get the FCA/wiTECH Promaster (RU body) radar alignment procedure** — disambiguates static-mirror
   vs dynamic-drive, and gives the documented mechanical adjustment. Do this before more actuation.
3. **Perturbation test (read-only):** `python3 tools/perturb_monitor.py`, then gently load the bracket
   up/down — flags any DID that twitches. Confirms whether there's a live orientation signal or it's
   purely driving-derived (expected: only voltage/temp move).
4. **Dynamic-drive hypothesis (lower priority):** start `0251`, keep the session alive, drive straight
   >50 km/h, watch `0845`/`0850` converge. Rated low given the cross-drive-cycle stability of −1.26°.
5. **Send the AlfaOBD bug report** (`radar_acc_alfaobd_bugreport.md`) — strong as-is.

### 0x0251 — what's now VERIFIED (was "param/scale inferred")
`tools/radar_acc_align_0251.py` (the only `31 01` in the repo; `--arm` + typed confirm to fire) now
drives the routine correctly: **session 0x03, `31 01 0251` with NO option byte, single-start** (2nd
start → `7F3124`; `10 03` re-entry RESETS it; `31 02` stops it; status `01 01 00 02`=running /
`00 04 00 02`=idle). **Negative result:** static mirror on a parked van does nothing — routine stays
RUNNING, angle unchanged, DTC stays 0x8F; it validates before committing so the radar is left
unchanged. Full detail in `../findings/radar_acc_did_findings.md`. Legal/liability terms: README
"Safety & liability".

## Caveats / uncertainty
- Angle **scale** (millideg vs microdeg) and exact DID→name labels are **inferred**, not from a Bosch
  ODX. Cross-check against AlfaOBD's displayed numbers (or the perturbation test) to certify.
- Bend-the-bracket vs run-the-routine: clearing the code ≠ correct boresight; the mirror routine
  calibrates the true beam center, which has a per-unit offset from the housing. See discussion in git history.

## Safety
Forward-collision radar. Everything in this repo is **read-only** (`22`/`19`/`31 03`) **except**
`tools/radar_acc_align_0251.py`, the one gated actuation tool (`31 01`). A mis-aimed radar causes
phantom braking / missed detection. Actuation is owner-consent-only and on your own vehicle — the
legal/liability conditions in the README "Safety & liability" apply and are not optional.

## Environment state
Raspberry Pi, `/home/pi/dev/obd-things`. Installed: `can-utils` (apt); `python-can`, `can-isotp`,
`udsoncan` (pip `--break-system-packages`). PCAN-USB on a powered hub. `can0` via in-kernel `peak_usb`.
