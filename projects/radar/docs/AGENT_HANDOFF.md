# Agent Handoff — 2022 Ram Promaster ACC radar, DTC C1418-78 (current state 2026-06-18)

**Read this first**, then the repo-root [`README.md`](../../../README.md) (universal bus facts/gotchas +
the **RESEARCH-FIRST** working method) and the **authoritative OEM docs** in [`docs/oem/`](oem/) (trust
those over our inference). Background: `radar_acc_handoff.md`; raw data in `../findings/` + `../dumps/`;
AlfaOBD write-up `radar_acc_alfaobd_bugreport.md`.

---

## TL;DR (reconciled — this supersedes any older framing)
2022 Ram Promaster forward **ACC radar** (Bosch MRR1evo14F, identifies as "DASM", bumper-mounted) has an
**active vertical-misalignment fault, DTC C1418-78 (status 0x8F)**, which disables ACC/FCW. We have a
**fully working UDS link** (PCAN-USB + SocketCAN) and have reverse-engineered the live data: the radar
reports a **stored vertical boresight error of ≈ −1.26°** (DIDs `0x0845`/`0x0850`, elevation).

**The fix is to re-align the radar — and the OEM method is a dynamic "Service Drive Alignment (SDA)",
NOT a static mirror.** The radar also self-aligns small deviations during normal driving, but only within
a **limited capture window**; −1.26° is **beyond** that window (proven: a 2-hour, 60%-highway drive did
not move it). So the **gate is physical**: re-seat/level the mount to bring the deviation back inside the
window, then let normal driving re-converge it **or** run the SDA. **Van is the owner's home/office →
no shop visits**; everything below is in-place DIY.

The repo is read-only **except one gated actuation tool** (`radar_acc_align_0251.py`, the only `31 01`).

---

## ⛔ RULED OUT — do NOT retry these (each cost real time; the reason is settled)
1. **Static-mirror alignment** (the 3-position +2°/0°/−2° mirror flow). **WRONG method for this van** — it
   came from a Giulia doc. The Promaster uses the **dynamic SDA**. Running `0x0251` with a parked mirror
   does **nothing** (stays "RUNNING", `0845` unchanged, DTC stays). The mirror prompts still in
   `radar_acc_align_0251.py` are **history — ignore them**; use `radar_acc_sda_drive.py` instead.
2. **OBD-II PIDs (Mode 01) for vehicle data.** Functional `0x7DF` + physical `0x7E0`, 11- and 29-bit, **all
   NO RESPONSE** behind the SGW bypass (we're on the internal bus, not the gateway's OBD path). Vehicle
   **speed = radar DID `0x1002`** (km/h, 1 byte) — already wired into the logger. Don't re-probe OBD.
3. **Decoding speed from the CAN broadcast.** Abandoned — `0x1002` solved it. (A distance/odometer
   accumulator lives at CAN ID `0x101` if ever needed, but you won't need it.)
4. **"Just keep driving" to auto-fix at the current −1.26°.** Ruled out by a **2-hour, 60%-highway drive**
   (`tmp/dumps/radar_acc_drive_20260618_143202.csv`): `0845` stayed flat at −1.26°, DTC never cleared.
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
   bumper bar contacts it (bar too high → loosen, slide bumper **DOWN**, retighten bottom-first). Use a phone
   **inclinometer** to get the radar face physically nominal — this sidesteps the unknown DID sign (aim for
   "level/square," not a signed angle). Goal: deviation back **inside the auto-align window**. Nothing
   downstream works until this is done. *(AllData front-fascia + ACC-module removal steps can be pulled from
   `~/dev/ram_2022_GAS` on request.)*
   **No live CAN feedback during the wrench work** — `0845`/`0850` are stored (update only via driving) and
   `0841` is target-derived; the perturbation test proved a physical tilt doesn't register while parked. So
   the loop is **inclinometer → drive → re-read `0845`/`0850`** (iterate). And note: if still outside the
   auto-align window, the stored readings stay frozen and give NO feedback — rely on the inclinometer to
   confirm you're physically nominal; a drive-read only starts moving once you're back in range.
1b. **Then drive normally + monitor (no tool).** The cron logger captures it passively; watch `0845`/`0850`
   trend toward 0 / DTC clear over miles. Cheapest shot, best fit for "van is home." Don't *rely* on it.
2. **DIY Service Drive Alignment — if 1b doesn't converge.** `radar_acc_sda_drive.py --arm`: starts `0x0251`
   once, holds the session with `3E` (never `10 03`), logs `0845`/`0850`/DTC/speed while you drive
   (straight/steady ~30-45 mph, ~15-20 min). Converges/clears → **DIY fix, no wiTECH**. Stays "RUNNING" →
   likely needs a `27` unlock (→ #2b).
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
- **DID sweep CLEAN:** `22` over 0x0000–0xFFFF = 56 readable, 0 locked (`../dumps/radar_acc_did_sweep.txt`).
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
./bringup.sh --tx                              # can0 @500k ARMED (UDS needs --tx; passive is the default now); ignition ON
python3 projects/radar/radar_acc_baseline.py   # reproduce baseline + DTCs (read-only)
python3 projects/radar/radar_acc_live.py       # live alignment/health gauge @5 Hz (read-only)
python3 projects/radar/radar_acc_drive_log.py  # log angles+speed+DTC to dumps/ while driving (read-only)
python3 projects/radar/radar_acc_sda_drive.py --arm   # ** ACTUATION ** DIY SDA: start 0x0251 + hold session + log; then DRIVE
python3 tools/uds_send.py radar_acc 22 F1 91   # ad-hoc read (generic)
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
   `tmp/dumps/radar_acc_drive_*.csv`. **Remove via `crontab -e` once the radar is fixed/abandoned.**
2. `tmp/CAPTURE_RAW` and `tmp/HUNT_DIDS` markers — **retired**; `did_hunt_log.py` + raw-burst code remain dormant
   (only fire if a marker is re-created). Speed (`0x1002`) is wired into the normal logger.

## Safety
Forward-collision radar. Everything is read-only (`22`/`19`/`31 03`) **except** `radar_acc_align_0251.py` and
`radar_acc_sda_drive.py` (`31 01`). A mis-aimed radar causes phantom braking / missed detection. Actuation is
owner-consent-only, on your own vehicle; ACC/FCW is already disabled by the active DTC so the radar is inert
during a test. Legal/liability terms: repo-root README "Safety & liability".

## Environment
Raspberry Pi, `/home/pi/dev/obd-things`. `can-utils` (apt); `python-can`, `can-isotp` (pip --break-system-packages).
PCAN-USB on a powered hub → **SGW-bypass cable** → vehicle internal C-CAN; `can0` via in-kernel `peak_usb`.
Van has **Starlink** (internet available in-vehicle). The whole link depends on the SGW bypass (see VERIFIED).
