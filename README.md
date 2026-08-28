# obd-things — CAN/UDS toolkit for a 2022 Ram Promaster

A small, reusable platform for talking to the modules on one specific van (a
2022 Ram Promaster) over **SocketCAN**, using the serial-resolved dual-USBCANFD
telemetry installation on a Raspberry Pi. Historical campaigns used a PEAK
PCAN-USB; those records remain evidence, not a supported current workflow. It
started as ACC-radar alignment work and is structured so the
**generic CAN/UDS plumbing is reusable for any module** (PCM, BCM, ABS, …),
with each investigation living under `projects/<name>/`.

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

Signal work uses the tiered policy in
[`docs/can-evidence-tiers.md`](docs/can-evidence-tiers.md): exploratory
candidates may move quickly, operational proxies may pass explicit
non-critical error tolerances, and only independently established physical
decodes enter the verified bus map or ordinary telemetry allowlist. Safety and
provenance gates do not loosen with the statistical tier.

---

## Layout

```
docs/                      cross-project vehicle reference
  bus-map.md                 MASTER map: verified broadcast frames + decodes per bus, wake/sleep
                             semantics, module summary — READ before new reverse-engineering
  alfaobd-evidence-history.md
                             canonical chronology of observed AlfaOBD mis-mappings, artifact traps,
                             confidence boundaries, and validation rules
lib/                       GENERIC, module-agnostic plumbing
  uds.py                     ISO-TP socket, UDS request, NRC table, byte decoders, USB-drop recovery
  modules.py                 module registry — SOURCE OF TRUTH for addressing; ADD A MODULE HERE
  can_role_resolver.py       exact serial/dev_id -> ephemeral SocketCAN role resolution
  vehicle_can_roles.py       installed three-bus role/serial/dev_id specifications
  can_runtime_route.py       scoped live arm/restore owner for supported diagnostic CLIs
  can_wake.py                fixed logical B/C wake profiles with exact passive restoration
  can_handoff.py             cooperative per-role fairness ahead of authoritative CAN locks
  signal_fields.py           dependency-free DBC/cantools Intel/Motorola raw bit geometry
  diagnostic_safety.py       shared/exclusive logical-role and resolved-channel locks
  can_operation_state.py     same-boot physical-topology + external-campaign wake inhibits
live_data/                 GENERIC top-style live-view library (not a standalone CLI)
  live_data.py               BASE: a thin module wrapper passes Module + Metric rows to run()
tools/                     GENERIC, module-agnostic CLI tools (take a module key)
  uds_send.py                ad-hoc raw UDS request (payload determines safety class)
  ecu_discover.py            bounded active ECU presence scan -> tmp/discovery/
  identity_inventory.py      bounded per-ECU identity reads -> tmp/inventories/<key>/
  dtc_inventory.py           non-clearing per-ECU DTC inventory -> tmp/inventories/<key>/
  dtc_scan.py                offline multi-module DTC plan + saved-report import; no CAN access
  dtc_batch.py               guarded fixed 19 02 FF batch for 15 reviewed modules
  dtc_web_arm.py             one-use local authorization for the Tailscale DTC UI
  can_capture_summary.py     interface-preserving offline candump/.zst summary (`--snapshot` bounds growing logs)
  can_event_window.py        bounded exact saved-log frame windows + optional CRC-8/SAE-J1850 audit
  three_bus_capture.py       cooperative receive-only three-role chunks -> tmp/captures/three_bus_drive/
  alfaobd_dat.py             offline AlfaOBD .dat cache inventory/baseline comparison
  can_operation_state.py     explicit topology/inhibit status CLI; performs no CAN I/O
  did_sweep.py               dry-run-first, checkpointed ReadDataByIdentifier inventory (22)
  routine_scan.py            dry-run-first, checkpointed result-only RoutineControl inventory (31 03)
  signal_correlate.py        DID byte-slice <-> signal correlator (lstsq), capture + analyze
  can_timeseries_correlate.py offline cluster-DID <-> passive broadcast candidate correlator
  can_signal_benchmark.py    whole-drive report benchmark; never runs heavy searches on vanpi
projects/                  per-target investigations and durable findings
  vehicle_configuration/    BCM/PROXI configuration campaigns and recovery handoffs
  vehicle_data/             allowlisted cached telemetry broker, CLI, and gated dashboard
  radar/                     2022 Promaster ACC radar (Bosch DASM / MRR1evo14F) — see its README
    radar_acc_live.py          maintained dry-run-first scoped viewer + offline historical CSV follow
    docs/ findings/            radar narrative docs, decoded data + promoted (tracked) captures
  ecu_mapping/vonstar_service.py private fixed-action Unix API for deployed Vonstar controls
tmp/                       gitignored — ALL machine-written data lands here, never in git:
  captures/                  role-aware passive/drive-recorder raw CAN logs
  discovery/                 bounded ECU-address discovery reports
  inventories/               per-module identity, DTC, DID, and routine reports
  sweeps/                    completed DID compatibility text + signal_correlate.py output
  locks/                     advisory logical-role and resolved-channel CAN lock files
  <project>/                 per-project logger output (tmp/radar/, tmp/battery/, tmp/tpms/)
```

**Data convention:** tool defaults write under the Pi's local `tmp/` (gitignored). Long-running
recorders may instead use the mount-guarded cold-data mirror at
`/mnt/EXFAT512/obd-things/tmp/`. Large completed captures and offline-compute history may be moved
there after verification; local symlinks can preserve familiar paths, but they do not make the data
available when the flash drive is absent. See [`docs/data-archive.md`](docs/data-archive.md) for the
current layout, restore procedure, and van-compute symlink restriction.

When a capture/sweep proves worth keeping, PROMOTE a reviewed, suitably redacted evidence subset
into `projects/<x>/findings/` and commit it next to the analysis that cites it. "Is it tracked?" is
answered by location alone — neither local nor EXFAT `tmp/` data is tracked.

**Generic vs project-specific:** anything in `lib/`, `tools/`, and `live_data/` is
module-agnostic and reusable — it knows nothing about any particular ECU (addressing is passed in via
the module key). Anything under `projects/<name>/` is specific to that target.

Scripts under `projects/` locate the repo root by walking up to the dir containing `lib/`, so they run
from any working directory and survive being moved deeper. New generic tools in `tools/` can use the
simpler `REPO = dirname(__file__)/..`.

---

## Universal facts about THIS van's bus (verified — trust these)

- **Live-verified buses plus OEM DLC branches:**
  - **C-CAN / HS-CAN, 500 kbit/s** — OBD pins **6/14**; powertrain + diagnostics; permanent
    dual-USBCANFD role Board A CAN1.
  - **B-CAN / CAN-IHS, 125 kbit/s** — OBD pins **3/11**; comfort/body traffic; permanent role
    Board A CAN2. The former single-PCAN workflow is historical evidence only.
  - **CAN CH / second HS-CAN, 500 kbit/s** — OBD pins **12/13** through the grey adapter;
    permanent role Board B CAN1. Live verification on 2026-07-25 used passive PCAN observation
    plus AlfaOBD exchanges for ABS, EPS, HALF, and ORC; that adapter procedure is provenance,
    not the current routing method. See `docs/bus-map.md`.
  - Each dedicated adapter channel maps to exactly one physical pair. C-CAN,
    B-CAN, and CAN CH can all remain UP and passively observed concurrently;
    the fourth channel is unused. A splitter parallels one bus and never merges
    these networks. See `docs/bus-map.md` for the exact serial/`dev_id` role map.
- **Diagnostic addressing:** verified C-CAN modules and the four verified B-CAN modules use UDS over
  ISO-TP with **29-bit** IDs. Tester = `0xF1`; each ECU has a physical address (e.g. radar `0x2A` →
  TX `0x18DA2AF1`, RX `0x18DAF12A`). The shared transport also supports explicit 11-bit module
  entries, but no ProMaster B-CAN 11-bit diagnostic pair is currently verified. Each registry entry
  records its addressing mode and bitrate; add only independently verified TX/RX pairs.
- **SGW bypass is installed**, so diagnostic UDS (`22`/`19`/`31`/…) reaches the internal modules. **BUT
  legislated OBD-II is NOT reachable this way** — Mode 01 PIDs via functional `0x7DF` / physical `0x7E0`
  (11- and 29-bit) all return NO RESPONSE, because the bypass taps the *internal* bus, not the gateway's
  OBD path. **Consequence: to read vehicle signals (speed, RPM, …) you must decode the broadcast frames on
  the bus, not query OBD PIDs.**
- **Most modules sleep** when ignition is off → bus goes silent. A sleeping ECU may still ACK direct UDS
  reads (slowly); engine running = stable ~14 V and full broadcast traffic. Diagnostic sessions time out
  (~5 s, S3) when idle — and re-entering a session can RESET in-progress routine state.

## Gotchas (these already bit us)

- **Controller modes are sticky** across `ip link set up`. Every current role configuration must
  explicitly select classical CAN with FD off, the fixed role bitrate, intended listen-only state,
  intended ONE-SHOT state, and `restart-ms 0`, then verify a fresh readback. Ordinary passive and
  diagnostic paths require ONE-SHOT off; only the fixed C-CAN wake profile enables it. RX working
  does not prove TX is armed.
- **Down before re-up:** changing CAN link settings on an already-up interface fails with
  `Device or resource busy`. On the dual installation, only the serial-aware reconciler or a
  reviewed active owner may perform the down/configure/up sequence while holding both role and
  resolved-channel locks.
- **Netdev names are not identities.** A hub reset may renumber all four `canN` devices. Resolve
  USB serial plus `dev_id` again at every ownership boundary; never save or infer `can0` as C-CAN.
- **Legacy PEAK limitations remain historical context:** its driver did not support
  `berr-reporting on`, and a shared-hub brownout produced `Rx urb aborted -32`. Those captures
  remain valid evidence, but they do not define the current dual-USBCANFD runtime or channel names.
- **Passive activity is the safe wake-state check**, but run it only through a role-aware observer
  path that owns and rechecks the intended channel. Never poll UDS merely to detect "awake"; that
  can keep modules and accessory rails awake and drain the 12 V battery.

---

## Current dual-USBCANFD operation

The role-aware `van-telemetry` service was installed, enabled, and brought
active on 2026-08-21. Passive commissioning resolved C-CAN as `can0` at
500 kbit/s, B-CAN as `can1` at 125 kbit/s, and CAN CH as `can2` at 500 kbit/s;
all three read back as classical CAN, FD off, listen-only, `restart-ms 0`, and
ERROR-ACTIVE. The unconnected spare was `can3` and remained down. TX and error
counters were zero. These `canN` names record that one commissioning snapshot,
not durable identities.

The normal service is running with active-drive enabled. On 2026-08-24 the
broker was restarted onto the role-aware wake implementation while the vehicle
was asleep; the active-drive helper remained idle, the historian and both web
listeners recovered, and all three vehicle roles remained exact passive with
zero TX/error counters. The broker-owned `van-drive-recorder.service` is
enabled and active. As of 2026-08-27 it waits receive-only for a reviewed
active-drive interval, then records separate synchronized C-CAN, B-CAN, and
CAN-CH compressed streams; its asleep deployment restart opened no CAN socket
and caused no link or TX change. The next real drive is still required to
validate the new three-role capture path end to end.
The new `van-cop-can-wake.service` is also installed, enabled, and active; with
COP off and both markers absent its initial state was `idle` and caused no TX.
`vonstar.service` is installed, enabled, and active as the private serialized
vehicle-access API at `/run/vonstar/api.sock`; the port-8788 dashboard is its
only web-facing client. Its 2026-08-27 deployment/restart added zero CAN TX and
left every vehicle role exact passive/error-free with no inhibit. Starting the
service is idle; only an explicit action or aggregate state request can touch
CAN.
The B-CAN recorder, mapping recorder, TPMS logger/sniffer, and manual three-bus
capture are installed but disabled/inactive. The retired fixed-`can0` systemd
drop-in and helper remain removed from the live host.

`Module.bus` is the physical routing source of truth. A current live
path must resolve that logical bus through the exact serial/`dev_id`, acquire
the logical-role lock and its presently resolved-channel lock, and recheck the
identity before CAN I/O. Missing, duplicate, changed, FD-enabled, wrong-rate,
or otherwise unprovable roles fail closed.

The vehicle-data broker's `dual-usbcanfd` mode is the reviewed passive
reconciler and its engine-running C-CAN helper is its automatic active owner.
The generic inventory/discovery CLIs now have a separate scoped active route:
after all command-specific confirmations, each tool resolves the module's
role, holds the role and channel exclusively, checks exact-role contention,
host privilege, and same-boot inhibits, requires an exact passive classical-CAN baseline, arms only for its
bounded operation, revalidates identity, and restores the captured passive
state. An unverified restore creates a wildcard same-boot inhibit.

The shared wake path is similarly role-scoped but deliberately narrower:
`lib/can_wake.py` accepts only logical `b-can` or `c-can` and fixes every
physical/timing/traffic detail internally. Scheduled parked-voltage monitoring
uses B-CAN's bounded wake only through the broker's local Unix API, after
passive-first and fresh parked-state gates. COP ALERT's separate supervisor
uses the C-CAN RF Hub profile because its accessory-rail/dashcam side effect is
the intended behavior. Both restore and verify the captured passive baseline
before returning, and CAN-CH has no wake profile. The telemetry acquisition web
proxy cannot request a wake. COP's separate dashboard can only publish its
existing intent marker; the guarded supervisor remains the CAN authority.
New button markers receive a 250 ms debounce and fast pre-transmit retry;
only a marker surviving a supervisor restart receives the three-second grace.
The supervisor journals transitions and retains its last blocked reason in its
private runtime status for the dashboard to display read-only.
Broker receive-side C-CAN work and fixed wake traffic also coordinate through
`lib/can_handoff.py`: passive samples pass through a reader gate and take a
shared scheduling turn; a wake closes that gate, waits a bounded 1.25 seconds
for in-flight readers to drain, and reserves the turn exclusively before taking
the unchanged exclusive role/channel locks. The handoff grants no CAN authority
and cannot bypass identity, parked, inhibit, health, or restoration gates.
Successful wake cadence is measured from attempt start, so transaction duration
is no longer added to the 15-second refresh interval. The corroborating broker
state may be at most five seconds old, matching the registered ignition/RPM
freshness window and covering one measured multi-role collector cycle; live
ignition/RPM evidence is still rechecked by the wake core at every send boundary.
Both installed profiles were live-validated asleep on 2026-08-24: B-CAN
returned verified 12.32 V after exactly 75 wake frames; C-CAN used ten
ONE-SHOT `22 FEFF` wake frames plus one normally acknowledged validation read.
Each restored classical listen-only, ONE-SHOT off, `restart-ms 0`, and
ERROR-ACTIVE with zero final error counters and no active inhibit.

The cooperative `tools/three_bus_capture.py` recorder is the all-bus passive
capture path. Three concurrent workers each hold only their own shared role and
resolved-channel observer lease and run a one-interface receive-only `candump`.
Rotation, adapter loss, or a child exit releases and freshly resolves only that
role; healthy bus files and children continue uninterrupted. Each role writes
separate chunks and route metadata, so a file can never mix bus identities. The
recorder never configures or downs a link and can coexist with other shared
observers; an active tool is excluded only on the role/channel it needs. The
role-aware reconciler must already have established each exact passive baseline.
`--check` reports every role independently and creates no capture.

There is deliberately no standalone command that leaves a role armed for some
other process. Do not work around tool ownership with a remembered `canN` or an
ad-hoc `ip link` command. The former single-PCAN bring-up script has been
removed. Dry run remains the default. A future or recovered project-specific
tool has no live authority until it is explicitly wired to scoped role
ownership; otherwise keep it offline-only.

## Quick start

The inventory commands below are safe planning runs: dry-run is the default, so they do not
inspect the live interface, open a CAN socket, or write a report.

```bash
python3 tools/did_sweep.py radar_acc 0800 08FF # plan 256 physical DID reads; NO CAN traffic
python3 tools/routine_scan.py radar_acc 0200 020F # plan 31 03 reads (+ FF00-FF03); NO CAN traffic
python3 tools/ecu_discover.py --profile promaster88-bcan # plan 4 verified + 4 unresolved B-CAN reads; NO traffic
```

Generic diagnostic tools take a **module key** from `lib/modules.py`; inspect that registry for
the current verified set and each module's bus/addressing metadata.

### Diagnostic CLI matrix

Every command below is a no-I/O plan unless its live gates are supplied. On the
dual installation, a supported live path derives the current interface from
the module's logical bus and exact USB identity, then owns both the role and
resolved channel through restoration. Passing a static/default `can0` or
recreating the removed single-PCAN bring-up is not a substitute for that
routing. Plan/dry-run output remains the default; each live CLI owns its own
bounded arm/restore interval after the additional gates below.

| Tool | Default plan | Additional live requirements / scope |
|---|---|---|
| `ecu_discover.py` | seven modern/default-session C-CAN endpoints | `--execute --confirm-parked --pair --conditions`; the bounded four-verified/four-unresolved B-CAN set uses `--profile promaster88-bcan` and adds `--confirm-catalog-candidates`; all 255 usable 29-bit targets add `--all-29bit-targets --confirm-expanded-scan`; custom pairs add `--confirm-custom-physical`; the verified PCM legacy-session probe remains restricted to one custom target and adds `--confirm-session-change` |
| `identity_inventory.py` | bounded standardized/OEM identity set, excluding VIN | common live gates above; `--did` replaces defaults; VIN is opt-in and masked in reports |
| `dtc_inventory.py` | non-clearing `19 01`, `19 02`, and `19 03` | common live gates; the larger supported-DTC `19 0A` catalog is opt-in |
| `dtc_scan.py` | offline sequential multi-module `19 02 FF` plan and saved-report import | never opens CAN; preview/import existing reports only |
| `dtc_batch.py` | fixed physical `19 02 FF` plan for 15 explicitly reviewed modules; PCM unsupported | `--execute` plus Park/gear/ignition-on-engine-off confirmations; fresh speed/RPM/identity/state gates before every request, <=1 Hz, grouped role windows, exact passive restoration before import; the Tailscale UI additionally requires a one-use local `dtc_web_arm.py` token |
| `did_sweep.py` | bounded `22` range | common live gates; expanded ranges and explicit sessions have separate confirmations described below |
| `routine_scan.py` | result-only `31 03` | common live gates; cannot start/stop a routine; expanded ranges and explicit sessions have separate confirmations |
| `signal_correlate.py capture` | bounded capture plan | common live gates plus `--confirm-session-change --confirm-no-active-routine`; fixed extended session |
| `uds_send.py` | classify and print one exact physical request | reads use common live gates; session or mutation payloads add the exact confirmations printed by the plan |
| module wrapper around `live_data.run()` | bounded direct-view plan | common live gates plus engine-off; explicit-session wrappers also require session/no-active-routine confirmations. `cluster_live.py` defaults to its separately verified session-unchanged, `22`-only policy; parked use only |

`live_data/live_data.py` is a library, not a standalone command. Create a thin project wrapper that
defines only its module key and `Metric` table and calls `run()`; do not copy radar-specific `--follow`
imports into an unrelated module. Its historical default remains explicit session `03` with
bounded TesterPresent. A wrapper may opt out only after direct evidence proves its DIDs are
compatible with the session-unchanged policy.

### Historical campaign provenance

The completed 2026-06 through 2026-08 evidence was collected with a former
single-PCAN setup. Its fixed-`can0`, cable-switching, and bring-up command
records are retained only in dated project handoffs and findings so captures
remain interpretable. The supporting bring-up script and campaign wrapper are
retired; none of those records is a current execution path.

Use the role-aware dry-run tools above and the target project's current README.
A live workflow is supported only after it resolves `Module.bus` from exact USB
identity, acquires the logical-role and resolved-channel locks, revalidates the
identity and classical-CAN state, and has a reviewed arming/restoration owner.
Do not reconstruct a historical recipe on an ephemeral `canN`.

## Adding another module / project

1. Add a `Module(...)` entry to `lib/modules.py` (key, name, txid, rxid, logical `bus`, plus explicit
   bitrate and `addressing_mode="normal_11bits"` when the module is not on the default 500k/29-bit
   transport). Do not save a current `canN` as module identity.
2. The generic tools work immediately: `did_sweep.py <key>`, `routine_scan.py <key>`, `uds_send.py <key> …`.
3. For a live view, make a thin wrapper that defines the module key + `METRICS` table and calls
   `live_data.live_data.run()`; keep radar-specific follow/CSV logic out of generic wrappers.
4. Put target-specific scripts/docs/findings under `projects/<name>/`.

---

## Safety & liability

**Read this before running anything that transmits to the vehicle.**

- Most tools send **non-mutating diagnostic reads** (`22`, `19`, `31 03`), but they are active
  transmissions: they can wake modules, change diagnostic-session state, and briefly power accessory
  rails. They are not passive captures. Coordinate the telemetry broker, drive recorder,
  `tpms-logger`, and any external tool; prove the logical bus/USB identity, physical pair, classical
  CAN state, and rate; then restore and verify listen-only mode after a manual campaign.
- `tools/uds_send.py` accepts an arbitrary payload and therefore is only as safe as the supplied service.
  It is dry-run by default and gates mutation/unknown services, but those gates do not make an arbitrary
  request intrinsically safe.
- **Historical radar actuation tools were removed after the successful repair.** Their dated findings
  preserve the exact `31 01`/routine evidence, but they are not executable recovery shortcuts. The
  generic gated `uds_send.py` can still transmit an explicitly authorized mutation/actuation payload.
  Radar calibration
  is actuation on a **safety-critical forward-collision / ADAS sensor.** A
  mis-aimed or mis-calibrated radar can cause phantom braking or fail to detect an obstacle, at speed.

**Conditions for any future actuation or `31 01` invocation:**
1. **Only on a vehicle you own**, or with the **documented, informed consent of the owner.** Not on
   another person's vehicle, a rental, a fleet vehicle, or anything you are not authorized in writing to modify.
2. **You are solely responsible** for confirming it is legal where you are to diagnose, calibrate, or modify
   an ADAS/ESC/safety system, and for any inspection/recertification a calibration may require. Tampering with
   safety equipment may carry regulatory, insurance, and liability consequences. Not legal advice.
3. **Verify alignment before driving.** After any calibration, confirm the result (DTC cleared, deviation
   angles in spec) and treat ACC/FCW as untrusted until proven on a controlled test.
4. Any routine parameter format or angle scale reconstructed from the historical evidence remains
   **reverse-engineered, not from a Bosch ODX**, and may be wrong.

## License & disclaimer
MIT — see [LICENSE](LICENSE). **Provided "AS IS", WITHOUT WARRANTY OF ANY KIND**; the authors and
contributors accept **no liability** for any damage, injury, loss, or legal consequence arising from its use
(expressly including actuation). You use it **entirely at your own risk and on your own
responsibility.** If you are not prepared to accept that risk for a safety-critical system, run only the
read-only tools.
