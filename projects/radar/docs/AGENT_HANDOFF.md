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
(`projects/radar/radar_acc_align_0251.py`, the only `31 01` in the repo). **0x0251 mechanics are now fully
reverse-engineered** (session 0x03, no option byte, single-start lifecycle — see
`../findings/radar_acc_did_findings.md`), but running it with a **static mirror on a parked van does
nothing** (routine stays RUNNING, stored angle unchanged, DTC stays active). Current best conclusion:
−1.26° is a **physical** misalignment beyond the self-align window → a **mechanical** fix, not a UDS
routine. See Open work.

## ▶ ACTIVE TASKS — drive-data auto-collected; current results
A cron logger records every drive **with no user action**, to `tmp/dumps/radar_acc_drive_*.csv` (on the
Pi, gitignored — not in a fresh clone). Now logs **angles + DTC + voltage + speed**.

**Task A — capture vehicle speed: ✅ DONE (2026-06-17).** Speed = radar DID **`0x1002`, 1 byte, km/h**
(found via the DID hunt; 0 at stops, plateau 68-88 = 42-55 mph matched the reported speed; this is what
AlfaOBD shows). Wired into `radar_acc_drive_log.py`; the dead OBD path is retired and the
`HUNT_DIDS`/`CAPTURE_RAW` markers + raw bursts are removed.

**Task B — RESOLVED by OEM docs (2026-06-18): alignment is a scan-tool "Service Drive Alignment".**
The AllData Ram-2022 procedure (`docs/oem/alldata_ram2022_C1418-78_and_acc_alignment.md`) says alignment
= **"Service Drive Alignment (SDA): radar calibration"** — a wiTECH-initiated **dynamic drive** (ACC ECU
view → Misc Functions; needs tire pressure + a **Wi-Fi hotspot**). **Not a static mirror, not passive
self-align.** This reframes the drive data:
- `elev_0845` stayed flat at −1.254° at all speeds (`tmp/dumps/hunt_20260617_222502.csv`); `0841` is a
  live ±10° instantaneous estimate. But the radar **does not** self-align during normal driving — SDA
  must be explicitly run — **so this does NOT prove a physical fault** (earlier "strongly physical" was
  wrong; it just means SDA was never run).
- **Leading fix:** run **SDA** (ideally wiTECH; or replicate via `0x0251` + the guided drive — our
  `0x0251` start that stays "RUNNING" is consistent with SDA armed-and-awaiting-the-drive), **after**
  confirming proper mounting (TSB + chart note). DIY caveat: SDA needs internet (Wi-Fi hotspot) → may
  have a server handshake raw `0x0251` can't reproduce.

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
./bringup.sh --tx                                  # can0 up @500k ARMED (UDS needs --tx now); ignition ON
python3 projects/radar/radar_acc_baseline.py       # reproduce baseline + DTCs
python3 projects/radar/radar_acc_live.py           # live top-style alignment gauge @5 Hz
python3 tools/routine_scan.py radar_acc            # reconfirm 0x0251 (generic, read-only, 31 03)
python3 tools/did_sweep.py radar_acc               # full DID sweep -> dumps/ (~15 min; partial range -> own file)
python3 tools/uds_send.py radar_acc 22 F1 A5       # ad-hoc read-only request (generic)
```
Architecture: generic platform at the repo root — `lib/uds.py` (UDS) + `lib/modules.py` (addressing
registry) + `live_data/live_data.py` (base viewer) + `tools/` (generic scanners). Radar-specific work
lives here under `projects/radar/`. Add a module = entry in `lib/modules.py` + copy
`projects/radar/radar_acc_live.py`. Universal bus facts/gotchas: repo-root `README.md`.

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
0. **DIY Service Drive Alignment attempt — the decisive untried experiment.** OEM alignment is a
   scan-tool-initiated **dynamic drive** (AllData, `docs/oem/`). Every prior `0x0251` run was PARKED, so
   it just sat "RUNNING". **`projects/radar/radar_acc_sda_drive.py --arm`** does the missing test: starts
   `0x0251` once, holds the session alive with TesterPresent (never re-sends `10 03` → would reset it),
   and logs `0845`/`0850`/DTC/speed while you drive (straight/steady ~30-45 mph, ~15-20 min). If the radar
   does the SDA itself → elevation converges / DTC clears = **DIY fix, no wiTECH**. If it stays pinned/
   RUNNING → the commit needs the wiTECH cloud/server side and pure-UDS is blocked. **Either outcome
   answers it, for free.** Do **after** #1 (mounting). The "Wi-Fi hotspot" is likely just wiTECH's cloud
   UI, not necessarily a radar-side requirement — this test tells us.
1. **Verify the module mounting first** (precondition for SDA; FCA STAR S2123000064 in `docs/oem/`):
   re-seat it fully + level in the bracket; pull it and check for **witness/rub marks** where the aluminum
   bumper bar contacts it — if the bar's too high, **slide the bumper DOWN** off the module. "Improper
   mounting can cause the calibration to fail" (Ram chart). NOTE: the static-mirror flow in
   `radar_acc_align_0251.py` is the WRONG method for this van (it's SDA/dynamic) — ignore those steps.
2. **Promaster-specific geometry** — the TSB is FCA-generic (car-platform photos); confirm the RU-van's
   bracket/bumper against service info "08 - Electrical / 8E - ECMs / MODULE, ACC / Removal and Installation".
3. ~~Perturbation test~~ **DONE (2026-06-13):** bounced the suspension ~1–2 in (≈0.7° body pitch);
   `0845`/`0850` unchanged, `0841` moved only ~7 millideg (drift, not tracking). **No live orientation
   signal — angle is driving-derived.** Confirms physical movement does not register while parked.
4. **Dynamic-drive hypothesis (lower priority):** start `0251`, keep the session alive, drive straight
   >50 km/h, watch `0845`/`0850` converge. Rated low given the cross-drive-cycle stability of −1.26°.
5. **Send the AlfaOBD bug report** (`radar_acc_alfaobd_bugreport.md`) — strong as-is.

### 0x0251 — what's now VERIFIED (was "param/scale inferred")
`projects/radar/radar_acc_align_0251.py` (the only `31 01` in the repo; `--arm` + typed confirm to fire) now
drives the routine correctly: **session 0x03, `31 01 0251` with NO option byte, single-start** (2nd
start → `7F3124`; `10 03` re-entry RESETS it; `31 02` stops it; status `01 01 00 02`=running /
`00 04 00 02`=idle). **Negative result:** static mirror on a parked van does nothing — routine stays
RUNNING, angle unchanged, DTC stays 0x8F; it validates before committing so the radar is left
unchanged. Full detail in `../findings/radar_acc_did_findings.md`. Legal/liability terms: README
"Safety & liability".

## ⚠ TEARDOWN — temporary data-collection setup installed on the Pi (REMOVE WHEN DONE)
These are **not in git** (they live on the Pi / in crontab) and must be torn down once we have
enough data, so the rig isn't left logging the vehicle indefinitely.

1. **Cron auto drive-logger** — installed in the user's crontab (`crontab -l`):
   `* * * * * ... python3 projects/radar/auto_drive_logger.py >> tmp/auto_drive_logger.log 2>&1`
   Passively logs each drive to `tmp/dumps/*.csv` (read-only). **Bus-aware** (bringup.sh is now
   passive-by-default + multi-bus): it operates only on the radar's **C-CAN 500k** and **auto-arms**
   it (listen-only off) to transmit the UDS reads; it **skips entirely when `can0` is at 125k B-CAN**
   so it never disrupts body-bus work. **Remove once we've collected enough driving traces to settle
   physical-vs-dynamic alignment:** edit it out via `crontab -e` (or `crontab -r`). Output under `tmp/`.
2. ~~Raw-CAN burst marker `tmp/CAPTURE_RAW`~~ — **RETIRED (2026-06-17):** speed found via DID, not
   broadcast; marker removed and `tmp/canraw/` bursts cleared (~1.5 GB reclaimed). The burst-launch
   code remains dormant in `auto_drive_logger.py` (only fires if the marker is re-created).
3. ~~Speed-DID hunt marker `tmp/HUNT_DIDS`~~ — **RETIRED (2026-06-17):** speed DID `0x1002` identified;
   marker removed. `did_hunt_log.py` is kept (dormant, reusable for hunting other signals — runs only
   if the marker is re-created).

## Caveats / uncertainty
- Angle **scale** (millideg vs microdeg) and exact DID→name labels are **inferred**, not from a Bosch
  ODX. Cross-check against AlfaOBD's displayed numbers (or the perturbation test) to certify.
- Bend-the-bracket vs run-the-routine: clearing the code ≠ correct boresight; the mirror routine
  calibrates the true beam center, which has a per-unit offset from the housing. See discussion in git history.

## Safety
Forward-collision radar. Everything in this repo is **read-only** (`22`/`19`/`31 03`) **except**
`projects/radar/radar_acc_align_0251.py`, the one gated actuation tool (`31 01`). A mis-aimed radar causes
phantom braking / missed detection. Actuation is owner-consent-only and on your own vehicle — the
legal/liability conditions in the README "Safety & liability" apply and are not optional.

## Environment state
Raspberry Pi, `/home/pi/dev/obd-things`. Installed: `can-utils` (apt); `python-can`, `can-isotp`,
`udsoncan` (pip `--break-system-packages`). PCAN-USB on a powered hub. `can0` via in-kernel `peak_usb`.
