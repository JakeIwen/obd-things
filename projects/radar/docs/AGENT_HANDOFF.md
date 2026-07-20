# Agent Handoff — 2022 Ram Promaster ACC radar, DTC C1418-78 (RESOLVED 2026-06-27)

**Read this first**, then the repo-root [`README.md`](../../../README.md) (universal bus facts/gotchas +
the **RESEARCH-FIRST** working method) and the **authoritative OEM docs** in [`docs/oem/`](oem/) (trust
those over our inference). Background: `radar_acc_handoff.md`; raw data in `../findings/`;
AlfaOBD write-up `radar_acc_alfaobd_bugreport.md`.

---

## ✅ RESOLVED (2026-06-27) — DIY Service Drive Alignment fixed it
C1418-78 is **cleared and ACC/FCW is functional again.** Full write-up: [`../findings/adjustment_1_results_3.md`].
What worked, in order: (1) a **physical nudge** of the radar ~1.3° brought it back inside the auto-align
window (drive #1 converged −1.26°→+0.28°); (2) the **DIY SDA** — `radar_acc_sda_drive.py --arm` started
routine `0x0251`, held the session with `3E`, and we drove a steady ~40 mph for ~17 min. The radar's SDA
**progress counter** (routine-status byte[2], 0x00→0x64 = 0–100%) climbed to 100% and **committed**: the
DTC flipped `0x8F → 0x0E` (testFailed bit0→0, warningIndicator bit7→0) at that instant. ACC works; it held
`0x0E` on the next drive. The residual `0x0E` is a stored-history record (ages out, or one-time `14` clear
sticks now that testFailed=0). **Pure-UDS, local, no wiTECH / no shop / no `27` unlock needed.** Below is
the original investigation (kept for context / if it ever regresses).

## TL;DR (original framing — kept for context; superseded by RESOLVED above)
2022 Ram Promaster forward **ACC radar** (Bosch MRR1evo14F, identifies as "DASM", bumper-mounted) had an
**active vertical-misalignment fault, DTC C1418-78 (status 0x8F)**, which disabled ACC/FCW. We have a
**fully working UDS link** (PCAN-USB + SocketCAN) and reverse-engineered the live data: the radar reported
a **stored vertical boresight error of ≈ −1.26°** (DIDs `0x0845`/`0x0850`, elevation).

**The fix was to re-align the radar — the OEM method is a dynamic "Service Drive Alignment (SDA)",
NOT a static mirror.** The radar also self-aligns small deviations during normal driving, but only within
a **limited capture window**; −1.26° was **beyond** it (proven: a 2-hour drive did not move it). So the
**gate was physical**: re-seat/level the mount to bring the deviation back inside the window, then run the
SDA (which we did). **Van is the owner's home/office → no shop visits**; everything was in-place DIY.

Most diagnostic tools are non-mutating but still transmit and may change session state. The two
dedicated radar actuators are `radar_acc_sda_drive.py` and the older
`radar_acc_align_0251.py`; generic gated `tools/uds_send.py` can also send an explicitly authorized
mutation or actuation payload.

---

## ⛔ RULED OUT — do NOT retry these (each cost real time; the reason is settled)
1. **Static-mirror alignment** (the 3-position +2°/0°/−2° mirror flow). **WRONG method for this van** — it
   came from a Giulia doc. The Promaster uses the **dynamic SDA**. Running `0x0251` with a parked mirror
   does **nothing** (stays "RUNNING", `0845` unchanged, DTC stays). The mirror prompts still in
   `radar_acc_align_0251.py` are **history — ignore them**; use `radar_acc_sda_drive.py` instead.
2. **OBD-II PIDs (Mode 01) for vehicle data.** Functional `0x7DF` + physical `0x7E0`, 11- and 29-bit, **all
   NO RESPONSE** behind the SGW bypass (we're on the internal bus, not the gateway's OBD path). Vehicle
   **speed = radar DID `0x1002`** (km/h, 1 byte) — already wired into the logger. Don't re-probe OBD.
3. **Assuming CAN ID `0x101` is an odometer accumulator.** Disproved by the 2026-07-19 passive drive:
   its packed 12-bit field is reversible instantaneous speed and tracks `0x0EE` almost perfectly.
   Its exact `/16`-versus-`/32` km/h scale still needs one known-speed reference. Radar DID `0x1002`
   remains the verified km/h source for this project.
4. **"Just keep driving" to auto-fix at the current −1.26°.** Ruled out by a **2-hour, 60%-highway drive**
   (`tmp/radar/radar_acc_drive_20260618_143202.csv`): `0845` stayed flat at −1.26°, DTC never cleared.
   Online auto-align won't chase a deviation this far out. Driving alone fixes it **only after** the mount
   is physically corrected back into the window (see Fix path #1).
5. **Wrong sessions for the routine (`0x40`, `0x60`).** Red herrings from the `0x03` session timing out.
   `0x0251` runs in **extended session `0x03`**.
6. **Option bytes on `0x0251`.** It takes **NO option byte**. We swept `0x00`–`0xFF` and all lengths → all
   `7F3131`. Don't sweep again.
7. **Re-sending `10 03` / restarting mid-routine.** That **RESETS** the running routine (this is why early
   "captures" never accumulated). During SDA, hold the session with **`3E` TesterPresent only**.
8. **Brute-forcing SecurityAccess (`27`).** Don't. If `0x0251` needs an unlock, the FCA seed/key is
   **locally solvable** (sniff AlfaOBD / DiagCode FCA SKGT) — Fix path #2b. `27 05` returns a seed in `0x03`.
9. **Parked nudging/perturbation to read misalignment direction.** Angle is driving-derived; a parked nudge
   does **not** register (tested — only voltage/temp moved). Get direction by **physically measuring the
   mount** (inclinometer), not from a DID.
10. **Shop visit / buying wiTECH.** Off the table (van = home). wiTECH's internet/"Wi-Fi hotspot" is its own
    **browser/websocket + cloud-security** architecture, **not** a radar requirement — the alignment routine
    is **local UDS** (AlfaOBD runs the FCA radar routine cloud-free on supported cars). Do **not** conclude
    "impossible without a dealer."
11. **Treating AlfaOBD as a direct fix for OUR radar.** Its radar-cal is the **static-mirror (car)** method;
    it mis-maps this MY2022 (calls `0250`, returns "not supported" for the misalignment gauges). Useful for
    PROXI config + ACC retrofit, and as a **seed/key source** (#2b) — but it likely **can't** run our SDA.
12. **"DASM = windshield camera."** Some RAM "DASM" service docs describe the **windshield camera** (static,
    inclinometer, *no driving*). **OURS is the bumper RADAR** (`0x18DA2AF1`) → **dynamic SDA**. Don't apply
    camera procedures to it.

---

## AlfaOBD — capabilities & limits for THIS radar (new agents: it is NOT infallible)
New agents reflexively treat AlfaOBD as the answer. **For this MY2022 Promaster radar it is mis-mapped
and CANNOT align it.** Evidence in `radar_acc_alfaobd_bugreport.md`:
- ❌ **Wrong routine ID** — AlfaOBD's "radar calibration" calls `0x0250`; this firmware rejects it
  (`7F3131`). The routine it actually implements is `0x0251`. AlfaOBD's radar-align is unusable here.
- ❌ **Wrong method** — AlfaOBD's radar-cal is the **static-mirror** flow (for FCA *cars*: Giulia / Dart /
  200 / Renegade / Compass). Our Promaster uses the **dynamic SDA** — a different procedure it doesn't map.
- ❌ **Hides the fault** — its misalignment gauges return "not supported" / read ~0, so a faulted radar
  *looks aligned*. The real misalignment is at `0x0845`/`0x0850`, which AlfaOBD doesn't read.
- ⚠️ **"PROXI / proxy alignment" ≠ radar calibration** — PROXI is vehicle-config sync (module swaps, ACC
  retrofit enable); it does **not** clear the radar boresight DTC. Do not conflate the two.
- ✅ **What it IS good for here:** PROXI config + ACC retrofit; showing vehicle **speed** (how we ID'd
  `0x1002`); and a **local FCA seed/key source** (it does SecurityAccess offline) for Fix-path #2b.

**Bottom line: do not assume AlfaOBD can align this radar — it can't.** It's a config/retrofit tool and a
seed/key oracle, not the C1418-78 fix.

## The model (best current understanding)
- The radar stores a vertical boresight error ≈ **−1.26°** (`0x0845`/`0x0850` elevation) → latches **C1418-78**
  → ACC/FCW disabled.
- It runs **continuous online misalignment correction with a LIMITED capture window**. Evidence: the owner's
  "ACC NOT AVAILABLE" **self-cleared once within ~200 mi** of driving (small, in-window deviation) before
  going permanent; `0x0841` is a live ±10° instantaneous estimate. At **−1.26° it's beyond the window** →
  cannot self-heal (the 2-hour drive confirms `0845` frozen).
- **Why it grew past the window:** a physical shift — improper **mounting/seating**, the **aluminum bumper
  bar contacting/pushing** the module (the documented FCA cause), or a knock. So "physical vs just-needs-
  calibration" resolves to: **bring it physically back into the window, then calibrate/drive.**
- **OEM fix = Service Drive Alignment (SDA)** (scan-tool-initiated dynamic drive), **after** confirming
  proper mounting. SDA is the shop's *reliable, deterministic* method — not necessarily the *only* way the
  code clears (if physically re-centered, normal driving may re-converge).

---

## Fix path (priority order — van = home, NO shop; all in-place DIY)
1. **Physically re-seat / correct the mount — THE GATE.** (FCA STAR S2123000064, `docs/oem/`.) Seat the
   module fully + **level** in its bracket; pull it and check for **witness/rub marks** where the aluminum
   bumper bar contacts it (bar too high → loosen, slide bumper **DOWN**, retighten bottom-first). Use a
   **phone "level"/inclinometer app** (the built-in iPhone Measure→Level, or any free Android bubble-level
   app) held against the radar face / bracket to read and set the tilt with a delta approximately the 
   magnitude of your 0x845 reported deviation. Goal: deviation back **inside the auto-align window**. Nothing
   downstream works until this is done.
   **No live CAN feedback during the wrench work** — the angle DIDs are driving-derived (the perturbation
   test proved a physical tilt doesn't register while parked). Loop: **inclinometer → drive → re-read
   `0845`/`0850`** (iterate). Distinguish **measure vs auto-correct**: the radar *measures* its current
   misalignment over a wide range (that's how it read −1.26° and set the DTC), so after a drive the reading
   should **track a physical change in BOTH directions** — bend it the wrong way and expect ~**−2.5°**, not a
   frozen −1.26°. The "limited window" only governs auto-*correction to zero*, not measurement. CAVEAT: we've
   never actually observed `0845`/`0850` respond to a physical change (no controlled before/after), so treat
   "it tracks" as strong inference. **De-risk: make a small (¼–½°) reversible test adjustment first, drive,
   and read** — toward 0 = right way (and confirms it responds); more negative = wrong way; no movement =
   reading is more stored-like than expected (lean on inclinometer + SDA).
1b. **Then drive normally + monitor.** The cron trigger is passive, but once launched the logger auto-arms
   C-CAN and sends active, non-mutating UDS reads; watch `0845`/`0850`
   trend toward 0 / DTC clear over miles. Cheapest shot, best fit for "van is home." Don't *rely* on it.
   **Audible cue (two-tier chime):** `touch tmp/CHIME` before a verification drive → the cron logger arms two
   distinct Sonos chimes so you know mid-drive when to stop: **SUCCESS** (`success.mp3`) the moment **C1418-78
   clears** (testFailed bit drops → ACC should return — the real win); and **SETTLED** (`settled.mp3`) when
   `elev_0845` has **plateaued while genuinely driving** (≥10 min cumulative moving time, then a flat 5-min
   trailing window: range ≤0.05° & |slope| ≤0.02°/min) → more driving won't move it (console says IN-SPEC vs
   OUT-OF-SPEC/stalled). The old "≥20% delta from baseline" trigger was replaced — it fired on mere movement,
   not completion, and was fooled by parked stretches (drive #1 sat flat ~9 min while parked). The SETTLED
   gate is **speed-gated** (DID `0x1002`) precisely to ignore those. `rm tmp/CHIME` after. (Marker, not a
   manual `--chime` run, so only one logger touches the bus.)
   **Watch it live:** `python3 projects/radar/radar_acc_live.py --follow` — tails the cron logger's CSV
   (NO bus access, no contention) and shows `0845` with Δ / %-change / →0-vs-WORSE from the start-of-drive
   baseline. NEVER run `radar_acc_live.py` in its direct (bus-reading) mode while the cron logger is active
   — two testers on one ISO-TP socket cross-talk (observed: desynced reads, a false "DTC cleared").
2. **DIY Service Drive Alignment — THE FIX THAT WORKED (2026-06-27).** `radar_acc_sda_drive.py --arm`:
   starts `0x0251` once, holds the session with `3E` (never `10 03`), shows live **SDA progress %**
   (routine-status byte[2]) while you drive straight/steady ~30-45 mph. **No fixed time limit** — it runs
   until it COMMITS (progress→100% / DTC `0x8F`→`0x0E`, plays SUCCESS chime) or progress stalls 10 min /
   the routine resets (TIMEOUT chime). Took ~17 min of steady driving. **Pause the cron auto-logger first**
   (its per-minute `10 03` resets the routine). If START returns `7F..33` (security) → #2b.
2b. **If it stalls on security (`7F..33`): sniff AlfaOBD (no dealer).** PCAN **listen-only** (`./bringup.sh`)
   while AlfaOBD talks to the radar (even its wrong `0250` attempt); capture the `27` seed→key (per-ECU-family;
   almost certainly the same unlock `0251` needs), replicate before `31 01 0251`. Also offline-computable (DiagCode).
3. **Confirm Promaster geometry** — the TSB is FCA-generic (car-platform photos); verify the RU-van's
   bracket/bumper against service info "08 - Electrical / 8E - ECMs / MODULE, ACC / Removal and Installation".
4. **Tooling fallback (last resort, still IN-PLACE):** aftermarket **Autel/Launch + AutoAuth** (~$50/yr/brand)
   does FCA ADAS calibrations on-site over Starlink (~$600-1500 tool, reusable). Detail in
   `docs/oem/research_2026-06-18_tooling_and_alignment.md`. **NOT** a shop drop-off.

---

## VERIFIED — trust these, do not re-test
- **Bus:** HS-CAN / C-CAN, **500 kbit/s**, OBD pins 6/14. (Body B-CAN = 125k via `bringup.sh --bcan`.)
- **Radar addressing:** UDS/ISO-TP, **29-bit normal-fixed**. TX `0x18DA2AF1`, RX `0x18DAF12A` (phys 0x2A, tester 0xF1).
- **LINK PATH — everything goes through a physical SGW BYPASS.** A 2018+ FCA Security Gateway sits between
  the OBD port and the internal buses; this van has an **ECRI-style SGW-bypass cable** that taps the
  **internal** C-CAN directly. That bypass is **why our diagnostic UDS (`22`/`19`/`31`) reaches the radar
  at all**, and **why legislated OBD-II PIDs do NOT route** (we're past the gateway's OBD path — ruled-out
  #2). If the bypass is removed/disturbed, **nothing here works.** It is a *gateway* bypass only — it does
  not touch the wiTECH cloud/AutoAuth layer or any in-module `27` security.
- **Baseline reproduces exactly:** `10 03`→`50 03 00 32 01 F4`; `22 F191` family→`MRR1evo14F`; serial `22 F18C`→`TD5730292062400`; SW `F195`=`0400`, HW `F193`=`01`.
- **DTCs (`19 02 FF`):** 8 total; **only C1418-78 active (0x8F)**, 7 dormant (0x40). FCA 3-byte encoding (C1418-78 = `54 18 78`).
- **Routine scan CLEAN:** only `0x0251` exists; **runs in session `0x03`, NO option byte, single-start** (2nd
  `31 01` while running → `7F3124`; `31 02` stops; `10 03` re-entry RESETS; status `01 01 00 02`=running / `00 04 00 02`=idle).
- **SDA progress is readable live:** `31 03 0251` → `71 03 0251 B0 B1 B2 B3`. **B2 = progress `0x00`→`0x64`
  (0–100%), monotonic**; B1 state (`01`=running, `03`=completed); B0 fluctuates (live flags); B3 mostly 00.
  At B2=100% the routine commits and C1418-78 flips `0x8F`→`0x0E`. (Proven on the 2026-06-27 SDA run.)
- **DID sweep CLEAN:** `22` over 0x0000–0xFFFF = 56 readable, 0 locked (`../findings/radar_acc_did_sweep.txt`).
  **Full decoded map of all 56 DIDs + sessions/security/routines/DTCs → [`../findings/did_map.md`](../findings/did_map.md).**
- **Key DIDs (scales inferred, internally consistent):**
  | DID | meaning | notes |
  |---|---|---|
  | `0x0845` (2×i32 ÷1e6) | **(elevation, azimuth) — AUTHORITATIVE stored** | elevation ≈ **−1.26°**; rock-flat across a 2-hr drive = stored calibration value |
  | `0x0850` (2×i32 ÷1e6) | (elevation, azimuth) — 2nd, noisier live-ish | hovers −1.2…−1.36°, no trend to 0 |
  | `0x0841` (i16 ÷1000) | **live instantaneous** vertical estimate | swings ±10° while driving (vehicle pitch), ~0 parked — **NOT** the stored fault |
  | `0x1002` (u8) | **vehicle speed, km/h** | VERIFIED via DID hunt; what AlfaOBD shows |
  | `0x1006` (u8 ×0.1) | control-module voltage | matched AlfaOBD |
  | `0x0835` (u8 −40) | ECU temp °C | only DID that drifts at idle |

---

## Run it (commands)
```bash
./bringup.sh --tx                              # can0 @500k ARMED; ignition ON
python3 projects/radar/radar_acc_baseline.py   # active non-mutating baseline + DTC reads
python3 projects/radar/radar_acc_live.py       # dry-run plan only; prints every live gate
python3 projects/radar/radar_acc_live.py --follow [CSV]  # bus-free view of an existing CSV
python3 projects/radar/radar_acc_drive_log.py  # active non-mutating UDS drive logger
python3 projects/radar/radar_acc_sda_drive.py --arm      # ** ACTUATION ** start 0x0251
python3 tools/uds_send.py radar_acc 22 F1 91   # dry-run plan; prints exact live gates
```
Architecture: generic platform at repo root (`lib/`, `tools/`, `live_data/live_data.py`, `bringup.sh`);
radar-specific work here under `projects/radar/`.

## Gotchas
- **`listen-only` is sticky** — `bringup.sh` is **passive by default**; UDS tools need `--tx`. Symptom if wrong: RX fine, all TX dropped.
- **USB brownout** on a shared hub (`Rx urb aborted -32`) — keep PCAN on the **powered hub**; scanners auto-recover.
- **Ignition auto-powers-down**; asleep = bus silent but the radar still ACKs direct reads. Engine running = ~14 V.
- `0x0251` session times out (~5 s) when idle, and **`10 03` re-entry resets the routine** — hold with `3E`.

## ⚠ TEARDOWN — temporary rig on the Pi (NOT in git; remove when done)
1. **Cron auto drive-logger** (`crontab -l` → `projects/radar/auto_drive_logger.py` every minute). Bus-aware:
   operates only on C-CAN 500k, **auto-arms** to log, **skips when can0 is at 125k B-CAN**. Logs each drive to
   `tmp/radar/radar_acc_drive_*.csv`. **Remove via `crontab -e` once the radar is fixed/abandoned.**
2. `tmp/CAPTURE_RAW` and `tmp/HUNT_DIDS` markers — **retired**; `did_hunt_log.py` + raw-burst code remain dormant
   (only fire if a marker is re-created). Speed (`0x1002`) is wired into the normal logger.
3. `tmp/CHIME` marker — while present, the cron logger arms the **two-tier chime** (SUCCESS = C1418-78
   clears; SETTLED = `elev_0845` plateaus while driving — see Fix path #1b) for mount-verification drives.
   **`rm tmp/CHIME`** when done so normal commutes don't chime.

## Safety
Forward-collision radar. `22`/`19`/`31 03` are non-mutating diagnostic requests, not passive capture.
`radar_acc_align_0251.py` and `radar_acc_sda_drive.py` send `31 01`; generic gated `tools/uds_send.py`
can send other authorized payloads. A mis-aimed radar can cause phantom braking or missed detection.
Actuation is owner-consent-only, on your own vehicle. The former active DTC is resolved and ACC/FCW is
functional, so never assume the radar is inert. Legal/liability terms: repo-root README "Safety & liability".

## Environment
Raspberry Pi, `/home/pi/dev/obd-things`. `can-utils` (apt); `python-can`, `can-isotp` (pip --break-system-packages).
PCAN-USB on a powered hub → **SGW-bypass cable** → vehicle internal C-CAN; `can0` via in-kernel `peak_usb`.
Van has **Starlink** (internet available in-vehicle). The whole link depends on the SGW bypass (see VERIFIED).
