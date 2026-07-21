# obd-things — CAN/UDS toolkit for a 2022 Ram Promaster

A small, reusable platform for talking to the modules on one specific van (a 2022 Ram Promaster)
over **PEAK PCAN-USB + SocketCAN** on a Raspberry Pi. It started as ACC-radar alignment work and is
structured so the **generic CAN/UDS plumbing is reusable for any module** (PCM, BCM, ABS, …), with
each investigation living under `projects/<name>/`.

> **New here (human or agent)? Read this whole file first** (universal facts + gotchas below), then
> the README of whatever you're working on, e.g. [`projects/radar/README.md`](projects/radar/README.md).

## Working method — RESEARCH-FIRST (for agents)
At every diagnostic fork, **before** going heads-down on bus-level reverse engineering:
1. **Web-search the open question** — OEM procedures/TSBs, how the relevant tools behave, community DIY reports.
2. **Ask what tools/resources/access the user has** — AlfaOBD, wiTECH, scan tools, service-doc subscriptions,
   local scrapes (e.g. the AllData scrape at `~/dev/ram_2022_GAS`).
3. **Mine resources already on disk / in-repo** — docs, scrapes, prior findings.

Hard-won lesson on this project: the decisive clues lived in the **tool/OEM/community ecosystem**, not on
the wire. Low-level CAN/UDS RE is the *fallback*, not the opener. (See memory `research-first-diagnostics`.)

---

## Layout

```
bringup.sh                 GENERIC: bring up the PCAN, PASSIVE by default (--tx to arm; --bcan
                             for the 125k body bus; --probe to find an unknown rate) + liveness
docs/                      cross-project vehicle reference
  bus-map.md                 MASTER map: verified broadcast frames + decodes per bus, wake/sleep
                             semantics, module summary — READ before new reverse-engineering
lib/                       GENERIC, module-agnostic plumbing
  uds.py                     ISO-TP socket, UDS request, NRC table, byte decoders, USB-drop recovery
  modules.py                 module registry — SOURCE OF TRUTH for addressing; ADD A MODULE HERE
  diagnostic_safety.py       per-SocketCAN-channel lock for guarded active diagnostic tools
live_data/                 GENERIC top-style live-view library (not a standalone CLI)
  live_data.py               BASE: a thin module wrapper passes Module + Metric rows to run()
tools/                     GENERIC, module-agnostic CLI tools (take a module key)
  uds_send.py                ad-hoc raw UDS request (payload determines safety class)
  ecu_discover.py            bounded active ECU presence scan -> tmp/discovery/
  identity_inventory.py      bounded per-ECU identity reads -> tmp/inventories/<key>/
  dtc_inventory.py           non-clearing per-ECU DTC inventory -> tmp/inventories/<key>/
  can_capture_summary.py     streaming offline candump summary (`--snapshot` bounds growing logs)
  did_sweep.py               dry-run-first, checkpointed ReadDataByIdentifier inventory (22)
  routine_scan.py            dry-run-first, checkpointed result-only RoutineControl inventory (31 03)
  ccan_inventory_campaign.sh one-command parked baseline, DID-page, or session-compare campaigns
  signal_correlate.py        DID byte-slice <-> signal correlator (lstsq), capture + analyze
projects/                  per-target investigations and durable findings
  radar/                     2022 Promaster ACC radar (Bosch DASM / MRR1evo14F) — see its README
    *.py                       radar-specific scripts (baseline, live, drive log, 0x0251 actuation, …)
    docs/ findings/            radar narrative docs, decoded data + promoted (tracked) captures
tmp/                       gitignored — ALL machine-written data lands here, never in git:
  captures/                  raw candump logs (tools/dump.sh default)
  discovery/                 bounded ECU-address discovery reports
  inventories/               per-module identity, DTC, DID, and routine reports
  sweeps/                    completed DID compatibility text + signal_correlate.py output
  locks/                     advisory per-channel active-diagnostic lock files
  <project>/                 per-project logger output (tmp/radar/, tmp/battery/, tmp/tpms/)
```

**Data convention:** tool defaults write under `tmp/` (gitignored); keep any explicit output override
there too. When a capture/sweep proves
worth keeping, PROMOTE it: move it into `projects/<x>/findings/` and commit it next to the analysis
that cites it. "Is it tracked?" is answered by location alone — nothing under `tmp/` ever is.

**Generic vs project-specific:** anything in `lib/`, `tools/`, `live_data/`, and `bringup.sh` is
module-agnostic and reusable — it knows nothing about any particular ECU (addressing is passed in via
the module key). Anything under `projects/<name>/` is specific to that target.

Scripts under `projects/` locate the repo root by walking up to the dir containing `lib/`, so they run
from any working directory and survive being moved deeper. New generic tools in `tools/` can use the
simpler `REPO = dirname(__file__)/..`.

---

## Universal facts about THIS van's bus (verified — trust these)

- **Live-verified buses plus OEM DLC branches:**
  - **C-CAN / HS-CAN, 500 kbit/s** — OBD pins **6/14**; powertrain + diagnostics. `bringup.sh` default.
  - **B-CAN / CAN-IHS, 125 kbit/s** — OBD pins **3/11**, reached through the B-CAN leg of the
    labeled dual-DB9 pigtail; comfort/body traffic. Live-verified 2026-07-20; `bringup.sh --bcan`.
  - OEM wiring identifies **CAN CH on pins 12/13**. That branch remains unverified live and is not
    required for the current C-CAN/B-CAN campaign.
  - One PCAN channel = one physical pair = **one bus at a time**; the OBD splitter parallels a single bus
    (lets PCAN + a scan tool share it), it does **not** merge C-CAN and B-CAN.
- **Diagnostic addressing:** verified C-CAN modules use UDS over ISO-TP with **29-bit** IDs. Tester =
  `0xF1`; each ECU has a physical address (e.g. radar `0x2A` → TX `0x18DA2AF1`, RX `0x18DAF12A`).
  The shared transport also supports explicit 11-bit module entries; each registry entry records its
  addressing mode and bitrate. Add only independently verified TX/RX pairs in `lib/modules.py`.
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
  USB hub**. Long-running tools that explicitly call `lib.uds.recover_socket` can auto-recover;
  bounded discovery/identity/DTC tools fail, preserve a partial report, and restore passive mode.
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

Both inventory commands below are safe planning runs: dry-run is the default, so they do not
inspect the live interface, open a CAN socket, or write a report.

```bash
python3 tools/did_sweep.py radar_acc 0800 08FF # plan 256 physical DID reads; NO CAN traffic
python3 tools/routine_scan.py radar_acc 0200 020F # plan 31 03 reads (+ FF00-FF03); NO CAN traffic
./tools/ccan_inventory_campaign.sh --candidate-dids # plan per-ECU F1xx + AlfaOBD candidates; NO CAN traffic
./tools/ccan_inventory_campaign.sh --bcm-pages # plan four evidence-selected BCM pages; NO CAN traffic
./tools/ccan_inventory_campaign.sh --bcm-session-compare # plan four-DID default/session-03 comparison
./tools/ccan_inventory_campaign.sh --pcm-probe # plan exact PCM 10 92 -> 1A 87 presence sequence
./tools/ccan_inventory_campaign.sh --session-probes # plan both bounded session checks together
./tools/ccan_inventory_campaign.sh --bcm-extended-page # plan session-03 BCM 4000-40FF
./tools/ccan_inventory_campaign.sh --session-followup # plan padded PCM retry + BCM extended page
```

Generic diagnostic tools take a **module key** from `lib/modules.py`; inspect that registry for
the current verified set and each module's bus/addressing metadata.

### Diagnostic CLI matrix

Every command below is a no-I/O plan unless its live gates are supplied. Live commands also run
interface/service preflight, take the per-channel transmitter lock, and return the adapter to verified
listen-only mode. Consequently, explicitly re-run `./bringup.sh --tx` before each subsequent live tool.

| Tool | Default plan | Additional live requirements / scope |
|---|---|---|
| `ecu_discover.py` | seven modern/default-session C-CAN endpoints | `--execute --confirm-parked --pair --conditions`; all 255 usable 29-bit targets add `--all-29bit-targets --confirm-expanded-scan`; custom pairs add `--confirm-custom-physical`; the verified PCM legacy-session probe remains restricted to one custom target and adds `--confirm-session-change` |
| `identity_inventory.py` | bounded standardized/OEM identity set, excluding VIN | common live gates above; `--did` replaces defaults; VIN is opt-in and masked in reports |
| `dtc_inventory.py` | non-clearing `19 01`, `19 02`, and `19 03` | common live gates; the larger supported-DTC `19 0A` catalog is opt-in |
| `did_sweep.py` | bounded `22` range | common live gates; expanded ranges and explicit sessions have separate confirmations described below |
| `routine_scan.py` | result-only `31 03` | common live gates; cannot start/stop a routine; expanded ranges and explicit sessions have separate confirmations |
| `signal_correlate.py capture` | bounded capture plan | common live gates plus `--confirm-session-change --confirm-no-active-routine`; fixed extended session |
| `uds_send.py` | classify and print one exact physical request | reads use common live gates; session or mutation payloads add the exact confirmations printed by the plan |
| module wrapper around `live_data.run()` | bounded direct-view plan | common live gates plus engine-off/session/no-active-routine confirmations; parked use only |

`live_data/live_data.py` is a library, not a standalone command. Create a thin project wrapper that
defines only its module key and `Metric` table and calls `run()`; do not copy radar-specific `--follow`
imports into an unrelated module.

### Parked live inventories

Never execute an inventory while the vehicle is moving. Finish/stop any drive capture first, park
the vehicle, record the ignition/engine state, and stop the background TPMS poller. A bounded live
DID inventory then looks like this:

```bash
sudo systemctl stop tpms-logger
./bringup.sh --tx
python3 tools/did_sweep.py radar_acc 0800 08FF \
  --execute --confirm-parked --pair 6/14 \
  --conditions "parked, ignition ON, engine OFF"
sudo systemctl start tpms-logger
```

The tool refuses live execution unless the module's interface is up, armed (not listen-only), at
the registry bitrate, not BUS-OFF, and noninteractive `sudo` is available for cleanup. It also
refuses to compete with `tpms-logger` or `promaster-drive-capture`. Once preflight passes and the
tool acquires its channel lock, its cleanup path restores the adapter to listen-only mode, including
on interruption/error. Re-run `./bringup.sh --tx` before each additional active tool, then restart
`tpms-logger` when the manual campaign is finished.

For the current ProMaster mapping campaign, the wrapper automates that re-arm/restore cycle. Its
`--candidate-dids` mode scans `F100-F1FF` on TCM, shifter, BCM, cluster, and telematics, then reads
61 additional BCM-only DIDs that were positive in the current-van AlfaOBD trace. It sends 1,341
physical `22` reads in the inherited session, performs no session change, and is dry-run by default:

```bash
./tools/ccan_inventory_campaign.sh --candidate-dids

# Later, while parked with ignition ON, engine OFF, and PCAN physically on C-CAN:
./tools/ccan_inventory_campaign.sh --candidate-dids --execute --confirm-parked \
  --conditions "ignition ON, engine OFF, PCAN on pigtail C-CAN DB9"
```

After that candidate campaign, `--bcm-pages` covers the four 256-DID neighborhoods selected by
the confirmed positives (`0100`, `2000`, `2900`, and `4000`). It uses the same inherited-session,
physical-read-only boundary and live gates:

```bash
./tools/ccan_inventory_campaign.sh --bcm-pages
./tools/ccan_inventory_campaign.sh --bcm-pages --execute --confirm-parked \
  --conditions "ignition ON, engine OFF, PCAN on pigtail C-CAN DB9"
```

Once those pages are complete, the separately gated `--bcm-session-compare` mode reads only
`40A3-40A6` before and after physical `10 03`. The current-van AlfaOBD trace returned `40A3` and
`40A6` positively while it held session `03`, whereas the default-session campaign returned
`7F 22 31`. Live use therefore requires explicit session-change confirmation:

```bash
./tools/ccan_inventory_campaign.sh --bcm-session-compare
./tools/ccan_inventory_campaign.sh --bcm-session-compare --execute \
  --confirm-parked --confirm-session-change \
  --conditions "ignition ON, engine OFF, PCAN on pigtail C-CAN DB9"
```

`did_sweep.py` writes each result immediately to
`tmp/inventories/<module>/dids_<timestamp>.results.jsonl` and writes an atomic run summary beside it.
Only a clean, complete run whose passive restore succeeded also creates the historical text view in
`tmp/sweeps/`. A live range above 512 DIDs additionally requires `--confirm-expanded-scan`; selecting
all 65,536 DIDs requires both `--full-range` and `--confirm-expanded-scan` and takes at least about
9.1 hours at the default 2 requests/s.

`routine_scan.py` has the same dry-run and parked-live gates. Its default plan covers `0200-03FF`
plus `FF00-FF03`; choose tighter hexadecimal bounds while mapping a new ECU. It can only send
requestRoutineResults (`31 03`) and cannot construct routine start/stop (`31 01`/`31 02`). Each
completed result is fsync-checkpointed to
`tmp/inventories/<module>/routines_<timestamp>.results.jsonl`; the companion atomic JSON report makes
partial/error and passive-restoration state explicit. Live plans above 512 unique RIDs require
`--confirm-expanded-scan`. The 512-ID default range plus four extra RIDs totals 516 requests, so a live
default-plan run requires that confirmation; the tighter example below does not.

```bash
# Alternative bounded routine campaign:
sudo systemctl stop tpms-logger
./bringup.sh --tx
python3 tools/routine_scan.py radar_acc 0200 020F \
  --execute --confirm-parked --pair 6/14 \
  --conditions "parked, ignition ON, engine OFF"
sudo systemctl start tpms-logger
```

Both tools inherit the ECU's current session by default and send neither DiagnosticSessionControl
nor TesterPresent. An explicit session is a separately gated state change: DID scans require
`--session HEX --confirm-session-change`; routine scans additionally require
`--confirm-no-active-routine`, because changing/re-entering a session can discard routine state.
Session bytes are restricted to `01-7F` so the response-suppression bit cannot defeat positive-echo
validation. Explicit-session scans require at least `--rate 0.5`; slower request spacing can exceed the
two-second bounded TesterPresent cadence used to hold the selected session.

The PCM is a verified legacy-session exception backed by both a current-van AlfaOBD trace and an
independent PCAN exchange. Its dry-run plan sends nothing and records the physical pair, `10 92`
preamble, fixed-DLC-8 zero padding, and `1A 87` identity request:

```bash
python3 tools/ecu_discover.py \
  --target pcm=18DA10F1:18DAF110 \
  --probe legacy-1a87 --session 92 --tx-padding 00
```

Live execution is restricted to that one explicit physical pair and requires both
`--confirm-custom-physical` and `--confirm-session-change`. The tool requires exact `50 92` before
it will send `1A 87`; it never uses functional broadcast. The explicit zero padding reproduces
AlfaOBD's ELM `PP 2C=01` fixed-eight-byte CAN framing. The first independent attempt omitted this
padding and timed out before the identity request. A parked engine-idling retry on 2026-07-21
received `50 92` and a positive multi-frame `5A 87` containing `68532157AI`, verifying the
registered `pcm` endpoint. Because both padding and engine state changed, that run did not isolate
which condition caused the earlier timeout.

The campaign wrapper supplies the service/interface lifecycle and those fixed target arguments so
the owner can run the same probe with one command after separately authorizing the session change:

```bash
./tools/ccan_inventory_campaign.sh --pcm-probe
./tools/ccan_inventory_campaign.sh --pcm-probe --execute \
  --confirm-parked --confirm-session-change \
  --conditions "ignition ON, engine OFF, PCAN on pigtail C-CAN DB9"
```

The initial combined mode runs the PCM probe first, then the four-DID BCM comparison:

```bash
./tools/ccan_inventory_campaign.sh --session-probes
./tools/ccan_inventory_campaign.sh --session-probes --execute \
  --confirm-parked --confirm-session-change \
  --conditions "ignition ON, engine OFF, PCAN on pigtail C-CAN DB9"
```

After that comparison proved `40A3` and `40A6` session-gated, the combined follow-up mode was
narrowed to the fixed-DLC PCM retry and one BCM `4000-40FF` page in session `03`. It also saves a
raw capture filtered to the PCM request/response IDs:

```bash
./tools/ccan_inventory_campaign.sh --session-followup
./tools/ccan_inventory_campaign.sh --session-followup --execute \
  --confirm-parked --confirm-session-change \
  --conditions "ignition ON, engine OFF, PCAN on pigtail C-CAN DB9"
```

The PCM half is now complete. If the BCM page has not yet run, use `--bcm-extended-page` rather
than repeating the combined mode.

Participating active diagnostic tools also take a nonblocking per-channel advisory lock under
`tmp/locks/`, so two of them cannot transmit through the same SocketCAN channel concurrently. The
participants are the guarded inventory/discovery tools, `uds_send.py`, `signal_correlate.py capture`,
direct-bus `live_data` viewers, and `tpms_logger.py` while it is polling. Offline analysis, passive
capture, and the TPMS logger's ignition-watch idle state do not take the lock. The lock is cooperative:
stop any older/project-specific transmitter that has not adopted it before starting manual bus work.

## Adding another module / project

1. Add a `Module(...)` entry to `lib/modules.py` (key, name, txid, rxid, plus explicit bitrate and
   `addressing_mode="normal_11bits"` when the module is not on the default 500k/29-bit transport).
2. The generic tools work immediately: `did_sweep.py <key>`, `routine_scan.py <key>`, `uds_send.py <key> …`.
3. For a live view, make a thin wrapper that defines the module key + `METRICS` table and calls
   `live_data.live_data.run()`; keep radar-specific follow/CSV logic out of generic wrappers.
4. Put target-specific scripts/docs/findings under `projects/<name>/`.

---

## Safety & liability

**Read this before running anything that transmits to the vehicle.**

- Most tools send **non-mutating diagnostic reads** (`22`, `19`, `31 03`), but they are active
  transmissions: they can wake modules, change diagnostic-session state, and briefly power accessory
  rails. They are not passive captures. Stop `tpms-logger`, confirm the physical bus/rate, and restore
  listen-only mode after a manual campaign.
- `tools/uds_send.py` accepts an arbitrary payload and therefore is only as safe as the supplied service.
  It is dry-run by default and gates mutation/unknown services, but those gates do not make an arbitrary
  request intrinsically safe.
- **Dedicated radar actuation tools:** `projects/radar/radar_acc_sda_drive.py` and the older
  `radar_acc_align_0251.py` issue `31 01` (startRoutine) to calibrate the ACC radar. The generic gated
  `uds_send.py` can also transmit an explicitly authorized mutation/actuation payload. Radar calibration
  is actuation on a **safety-critical forward-collision / ADAS sensor.** A
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
