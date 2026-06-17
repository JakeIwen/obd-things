# obd-things — CAN/UDS toolkit for a 2022 Ram Promaster

A small, reusable platform for talking to the modules on one specific van (a 2022 Ram Promaster)
over **PEAK PCAN-USB + SocketCAN** on a Raspberry Pi. It started as ACC-radar alignment work and is
structured so the **generic CAN/UDS plumbing is reusable for any module** (PCM, BCM, ABS, …), with
each investigation living under `projects/<name>/`.

> **New here (human or agent)? Read this whole file first** (universal facts + gotchas below), then
> the README of whatever you're working on, e.g. [`projects/radar/README.md`](projects/radar/README.md).

---

## Layout

```
bringup.sh                 GENERIC: bring up the PCAN, PASSIVE by default (--tx to arm; --bcan
                             for the 125k body bus; --probe to find an unknown rate) + liveness
lib/                       GENERIC, module-agnostic plumbing
  uds.py                     ISO-TP socket, UDS request, NRC table, byte decoders, USB-drop recovery
  modules.py                 module registry (addressing) — ADD A MODULE HERE to reach it
live_data/                 GENERIC top-style live viewer
  live_data.py               BASE viewer: pass a module + a Metric table -> live display
tools/                     GENERIC, module-agnostic CLI tools (take a module key)
  uds_send.py                ad-hoc read-only UDS request
  did_sweep.py               ReadDataByIdentifier sweep 0000-FFFF       -> dumps/<key>_did_sweep.txt
  routine_scan.py            RoutineControl discovery scan (31 03, read-only)
  signal_correlate.py        DID byte-slice <-> signal correlator (lstsq), capture + analyze
dumps/                     GENERIC scratch output from the tools above
projects/                  per-target investigations (radar today; add more here)
  radar/                     2022 Promaster ACC radar (Bosch DASM / MRR1evo14F) — see its README
    *.py                       radar-specific scripts (baseline, live, drive log, 0x0251 actuation, …)
    docs/ findings/ dumps/     radar narrative docs, decoded data, kept captures
tmp/                       gitignored runtime scratch (auto-logger output, raw captures)
```

**Generic vs project-specific:** anything in `lib/`, `tools/`, `live_data/`, and `bringup.sh` is
module-agnostic and reusable — it knows nothing about any particular ECU (addressing is passed in via
the module key). Anything under `projects/<name>/` is specific to that target.

Scripts under `projects/` locate the repo root by walking up to the dir containing `lib/`, so they run
from any working directory and survive being moved deeper. New generic tools in `tools/` can use the
simpler `REPO = dirname(__file__)/..`.

---

## Universal facts about THIS van's bus (verified — trust these)

- **Two buses (multi-speed):**
  - **C-CAN / HS-CAN, 500 kbit/s** — OBD pins **6/14**; powertrain + diagnostics. `bringup.sh` default.
  - **B-CAN / body bus, 125 kbit/s** — comfort/body (locks, lights, windows, VIN broadcast). Reached on
    the low-speed adapter pinout; `bringup.sh --bcan`. (Use `--probe` to rediscover an unknown rate.)
  - One PCAN channel = one physical pair = **one bus at a time**; the OBD splitter parallels a single bus
    (lets PCAN + a scan tool share it), it does **not** merge C-CAN and B-CAN.
- **Diagnostic addressing:** UDS over ISO-TP, **29-bit normal-fixed**. Tester = `0xF1`; each ECU has a
  physical address (e.g. radar `0x2A` → TX `0x18DA2AF1`, RX `0x18DAF12A`). Add modules in `lib/modules.py`.
- **SGW bypass is installed**, so diagnostic UDS (`22`/`19`/`31`/…) reaches the internal modules. **BUT
  legislated OBD-II is NOT reachable this way** — Mode 01 PIDs via functional `0x7DF` / physical `0x7E0`
  (11- and 29-bit) all return NO RESPONSE, because the bypass taps the *internal* bus, not the gateway's
  OBD path. **Consequence: to read vehicle signals (speed, RPM, …) you must decode the broadcast frames on
  the bus, not query OBD PIDs.**
- **Most modules sleep** when ignition is off → bus goes silent. A sleeping ECU may still ACK direct UDS
  reads (slowly); engine running = stable ~14 V and full broadcast traffic. Diagnostic sessions time out
  (~5 s, S3) when idle — and re-entering a session can RESET in-progress routine state.

## Gotchas (these already bit us)

- **`listen-only` is sticky** across `ip link set up` — always set it explicitly (`bringup.sh` does:
  passive by default, `--tx` to arm). Symptom if armed-but-stuck-passive: RX fine, **all TX silently dropped**.
- **Down before re-up:** changing bitrate/adapter on an already-up iface fails (`Device or resource busy`);
  `bringup.sh` always `ip link set <if> down` first, so switching 500k↔125k is safe.
- **`berr-reporting` unsupported** by this PCAN adapter (use `listen-only` instead).
- **USB brownout:** the PCAN drops on a shared root hub (`Rx urb aborted -32`). Keep it on the **powered
  USB hub**. The scanners auto-recover (`lib.uds.recover_socket`); the live viewer shows NO DATA.
- **Passive bus-activity check** (`timeout 3 candump -n 1 can0`) is the safe way to tell whether the
  vehicle is running without transmitting — exit 0 = traffic present, 124 = silent. Never poll UDS just to
  detect "awake," or you risk keeping modules awake / draining the 12 V battery.

---

## Bus bring-up (`bringup.sh`)

One script for both buses. **Passive (listen-only ON) by default** — only sniffs, never transmits/ACKs;
pass `--tx` to arm (UDS tools require it). Always brings the iface down first, so switching speed/adapter
is safe. Auto-picks the sole `can*` iface (or set `IFACE=canN`).

```bash
./bringup.sh                 # C-CAN 500k, passive sniff            (DEFAULT)
./bringup.sh --tx            # C-CAN 500k, ARMED — can send UDS
./bringup.sh --bcan          # B-CAN 125k, passive sniff
./bringup.sh --bcan --tx     # B-CAN 125k, ARMED
./bringup.sh --probe         # cycle common low-speed rates, report which is live
./bringup.sh --bitrate N     # override bitrate (e.g. 250000)
```

## Quick start

```bash
./bringup.sh --tx                              # C-CAN 500k, ARMED (UDS needs TX); ignition ON (engine running ideal)
python3 tools/uds_send.py radar_acc 22 F1 91   # ad-hoc read (radar family id) — sanity-check the link
python3 tools/did_sweep.py radar_acc           # full DID sweep -> dumps/radar_acc_did_sweep.txt
python3 tools/routine_scan.py radar_acc        # RoutineControl discovery (read-only)
```

All generic tools take a **module key** from `lib/modules.py` (`radar_acc` today).

## Adding another module / project

1. Add a `Module(...)` entry to `lib/modules.py` (key, name, txid, rxid).
2. The generic tools work immediately: `did_sweep.py <key>`, `routine_scan.py <key>`, `uds_send.py <key> …`.
3. For a live view, copy `projects/radar/radar_acc_live.py`, swap the module key + `METRICS` table (it
   imports the base viewer from `live_data/live_data.py`).
4. Put target-specific scripts/docs/findings under `projects/<name>/`.

---

## Safety & liability

**Read this before running anything that writes to the vehicle.**

- **Almost everything here is read-only** (`22`, `19`, `31 03`) and safe to run.
- **One tool performs actuation:** `projects/radar/radar_acc_align_0251.py` issues `31 01` (startRoutine)
  to calibrate the ACC radar. It is gated (read-only dry run by default; `--arm` + a typed confirmation
  required to fire) but it is still actuation on a **safety-critical forward-collision / ADAS sensor.** A
  mis-aimed or mis-calibrated radar can cause phantom braking or fail to detect an obstacle, at speed.

**Conditions of use for the actuation tool (and any `31 01` you derive from it):**
1. **Only on a vehicle you own**, or with the **documented, informed consent of the owner.** Not on
   another person's vehicle, a rental, a fleet vehicle, or anything you are not authorized in writing to modify.
2. **You are solely responsible** for confirming it is legal where you are to diagnose, calibrate, or modify
   an ADAS/ESC/safety system, and for any inspection/recertification a calibration may require. Tampering with
   safety equipment may carry regulatory, insurance, and liability consequences. Not legal advice.
3. **Verify alignment before driving.** After any calibration, confirm the result (DTC cleared, deviation
   angles in spec) and treat ACC/FCW as untrusted until proven on a controlled test.
4. The routine's parameter format and angle scaling are **reverse-engineered, not from a Bosch ODX** — they
   may be wrong. See the "living script" banner in the tool.

## License & disclaimer
MIT — see [LICENSE](LICENSE). **Provided "AS IS", WITHOUT WARRANTY OF ANY KIND**; the authors and
contributors accept **no liability** for any damage, injury, loss, or legal consequence arising from its use
(expressly including the actuation tool). You use it **entirely at your own risk and on your own
responsibility.** If you are not prepared to accept that risk for a safety-critical system, run only the
read-only tools.
