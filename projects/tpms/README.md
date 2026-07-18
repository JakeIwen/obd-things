# TPMS / RF Hub diagnosis — 2022 Ram Promaster (VIN 3C6LRVDG4NE######)

Handoff doc. State as of **2026-07-07**. Any agent/human should be able to resume from here.
Link path & general UDS tooling: see repo `docs/`, `lib/modules.py`, and the radar project docs
(same PCAN → SGW-bypass → C-CAN tap). Everything below was verified on the vehicle.

**Data locations (repo convention, 2026-07-08 — see root `AGENTS.md` / `README.md`):** all
tool output for this project goes to **`tmp/tpms/`** (gitignored: drive-log CSV, AlfaOBD
sniffs); raw bus-state reference captures live in `tmp/captures/`; committed evidence
(DID sweep + state markers) is promoted into **`findings/` (this dir)**. The old
`dumps/` and `tmp/raw_dumps/` locations no longer exist.

## The complaint

Intermittent **C1504** ("Tire Pressure Sensor 4 — Rear Right") for the vehicle's whole life.
The RR sensor was replaced **twice** to no effect. Wheels rotated once (~Jan 2026).
Current codes in the RF Hub: **B1040-64** and **C1512-88** (details below). No C1504 stored today.

## Key findings (each independently verified)

1. **RF Hub (RFH)**: Continental, P/N 68516285AC, SW 10438241AA v0400. UDS at addr **0xC7**:
   TX `18DAC7F1` / RX `18DAF1C7`, 29-bit normal-fixed, 500k C-CAN. Registered as `rf_hub` in
   `lib/modules.py`. **Answers with ignition OFF** (battery-powered, it's the RKE receiver).
   Wiring (AllData pinout, connector D2665A): CAN-C on pins 11/12, fused B+, ignition feed,
   security K-line. `~/dev/ram_2022_GAS` holds the full factory docs (DTC charts, pinouts).

2. **Sensor IDs and the verified wheel map** (deflate ~5 psi per corner + watch DIDs, then
   re-inflate in reverse order — both directions matched):

   | slot | pressure DID | ID DID | sensor ID | physically at | hub believes | note |
   |------|-----|-----|------------|----|----|------|
   | 1 | 31D0 | 31CB | `11825BA9` | FL | FL | the replacement sensor (non-factory family) |
   | 2 | 31D1 | 31CC | `7004E049` | FR | FR | has TWO records in 40A6-40A9 |
   | 3 | 31D2 | 31CD | `700497DF` | **RR** | RL | **rears mirrored in hub** |
   | 4 | 31D3 | 31CE | `7004C287` | **RL** | RR | **NO 40Ax record — prime suspect** |

   Pressure scale **0.1 kPa** (confirmed: deflations tracked exactly). Placard-ish targets:
   fronts 55 / rears 75 psi. `31C6`/`31C7` (2207/5517) likely front/rear thresholds.

   The ID-DID↔pressure-DID slot pairing (31CB↔31D0 … 31CE↔31D3) is no longer just a DID-adjacency
   assumption: the `40A6-40A9` records carry a position byte — pos01=`11825BA9`, pos02=`7004E049`,
   pos03=`700497DF`, pos04=`7004C287` — matching both the ID and pressure DID orders, and this same
   ordering appears in an **independent AlfaOBD capture** (`projects/ecu_mapping/findings/
   promaster_2022/module_did_map.txt`). `7004C287` is absent from `40A6-40A9` in **both** captures.

   ⚠️ **CORRECTION (2026-07-14):** the moment C1512-88 self-erased, `40A6-40A9` began returning
   NRC 0x22 (conditionsNotCorrect) — they are **fault-linked records, NOT a permanent position
   table**. So "`7004C287` has no `40Ax` record" does **not** establish that it is unlocalized, and
   the prime-suspect status that rested on it is **downgraded to unproven**. What survives: the
   `40Ax` position bytes did align with the ID/pressure slot order while readable, so the ID↔slot
   pairing stands; and the deflate-derived wheel map (below) never depended on `40Ax` at all.

3. **Reinterpretation of the history**: the hub's rear positions are crossed (classic failed
   rear-axle localization). "C1504 = RR" pointed at the wrong corner, so both sensor
   replacements chased the hub's mislabel. The twice-"replaced" corner's sensor (`11825BA9`)
   rode the forward-cross rotation to FL and is fine. The only sensor the hub never built a
   localization record for is `7004C287` — an **original** sensor now physically at REAR LEFT.
   An aging original transmitter that no one ever replaced fits the entire multi-year pattern.
   Factory chart notes agreeing: positions in the hub may be wrong; do NOT replace the hub for
   C1504-07; C1512 localization needs ABS speed data; RF interference is a listed cause.

4. **Current DTCs decoded** (`19 02 0D`): `904064`=B1040-64, `551288`=C1512-88, both status
   0x08 (confirmed, not currently failing), both aging down (−1 error-count per clean cycle).
   B1040-64 = "Operational Mode Status info 1 – signal plausibility" (AlfaOBD label): hub
   intermittently missing the BCM ignition-mode CAN message; detections cluster 60–90 s after
   key-on; recurring since startup ~#1014. C1512-88 = localization failed; first at #1243
   ≈ right after the tire rotation. If B1040 keeps recurring, inspect RFH connector/grounds
   (module is behind right B-pillar area per body harness family; see AllData).

5. **Snapshot/environment records** (`19 04 <dtc> 00|01`, rec 00 = first, 01 = latest):
   DID `1008` u32 operating-time minutes; `1009` u16 × 15 s since key-on; `200A` u16 startups
   counter; `6082` failure-type byte; `1921` unknown 9 B. Extended data (`19 06 <dtc> FF`)
   record 01 last byte = error counter. All verified against AlfaOBD screenshots.

6. **Other DIDs of interest**: `301E-3021` per-slot `[04][3-byte timestamp][age]` last-RX
   records — update trigger NOT yet characterized (did not reset on parked pressure events;
   one reset seen at ignition-on). `40A6-40A9` localization/event records `02 01 <pos> <id> ...`.
   `40C0` = 2022-date-stamped factory event log. `2024` = the one LOCKED DID (security access).
   Full sweep: `findings/rf_hub_did_sweep.txt` (118 readable / 1 locked / 0 unresolved),
   ignition-state bands in `findings/rf_hub_sweep_state_markers.txt`.

## Infrastructure now running

- **`tpms_logger.py` (this dir)** — the dropout tripwire. Manual mode: `./bringup.sh --tx &&
  python3 projects/tpms/tpms_logger.py`. Auto mode (`--auto`) runs as **systemd
  `tpms-logger.service`** (enabled, User=pi): IDLE = pure-RX 2 s listen for **`0x2EF`** every
  30 s (zero TX, parked bus sleeps); 0x2EF present (ignition on) → poll `31D0-31D3`,
  `301E-3021`, `19 02 0D` every 10 s → `tmp/tpms/tpms_drive_log.csv`; 0x2EF gone → session
  ends ≤12 s, bus asleep ~60 s later (measured).
  **The gate MUST stay 0x2EF, not frame count: our own polling holds FCA network management
  awake** (verified: with a frame-count gate the bus never slept; polling stopped → asleep in
  60 s). A dropout in the CSV names its slot → physical wheel via the table above; a DTC
  status flip (e.g. `C1504-xx=2F`) timestamps fault onset.
  The live unit is `/etc/systemd/system/tpms-logger.service`; a tracked copy sits in this dir
  (`tpms-logger.service`). (Re)install after changing the unit (NOT needed for logger edits —
  those just need a restart):
  `sudo cp projects/tpms/tpms-logger.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart tpms-logger`
- **`isotp_decode_rfh.py` (this dir)** — offline ISO-TP transcript decoder for candump logs
  (hardcoded to the RFH ID pair); used to decode the AlfaOBD session sniffs in
  `tmp/tpms/rfh_alfaobd_sniff_ccan_resilient.log`.
- **voltage_mon cron is COMMENTED OUT** (dated tag in `crontab -l`): it was crashing
  (`iface_bitrate()` TypeError, mid-refactor), its B-CAN path is physically moot while the
  PCAN sits on the C-CAN tap, and its iface flips would blind this logger. Re-enable by
  uncommenting when the PCAN returns to the B-CAN tap. **No low-battery ntfy alerts while
  disabled.** A separate Claude session owns the voltage_mon refactor — coordinate.
- **Before any manual bus work**: `sudo systemctl stop tpms-logger` (restart after).
  Gotcha: `pkill -f`/`pgrep -f` with a pattern that appears in your own command line kills
  your own shell — use `pkill -x candump` or exact PIDs.

## Campaign status (2026-07-14) — 18 sessions, ~11 h of driving logged

- **ZERO dropouts.** All four sensors, incl. `7004C287`, reported every 10 s in every session.
- **C1512-88 SELF-ERASED at 2026-07-13 20:37** (caught live by the logger; confirmed by UDS —
  `19 02 0D` now returns B1040-64 only). It aged out on clean cycles, as predicted.
  Simultaneously `40A6-40A9` went to NRC 0x22 → see the CORRECTION above.
- **B1040-64 persists**, error counter down to **10** (from 37 at first sight) → on track to
  self-erase in ~10 more clean cycles unless it recurs.
- **Pressure decode VALIDATED against the vehicle**: owner cycled the IPC to the TPMS screen on
  the 2026-07-13 drive and saw rears peak at **90 psi**; the logger's max for that session is
  **90.0 psi (RL)**. The 0.1 kPa scale is now confirmed against the cluster's own display.
- Cluster shows four real numbers (not dashes) → the RFH **is** currently broadcasting valid
  per-wheel pressures, so a zero-TX passive logging channel exists (see Open questions).
- **Caveat on all of the above:** the fault has not recurred *while we have been polling the hub
  every 10 s and after we deflated/re-inflated all four tires*. Neither confound is ruled out.

## Passive pressure-broadcast hunt — NEGATIVE (2026-07-14)

Goal was to find a C-CAN frame carrying per-wheel pressure so the logger could go zero-TX
(removing any observer effect). Captured two real warm-up drives (49 + 54 min, thermal rise
F 55→68 / R 75→90 psi) via the RX-only `drive_sniff.py`, correlated every broadcast slice
against the logger's ground-truth curves (`find_pressure_frame.py`). **No pressure broadcast
found**, by four independent methods:
- plain correlation — confounded (everything rises together on warm-up; ~0.9 vs all wheels);
- 4-fields-in-one-frame structural fit (shared affine map to the 4 pressures) — nothing;
- multiplexed rotating-wheel frame — only counter bytes, no 2-low/2-high pressure structure;
- **common-mode-removed residual correlation + cross-drive consistency** (the decisive test) —
  the only survivor, `0x5A0`, is a per-drive trip/minute counter (b1 rises 0→53 identically both
  drives); its apparent RR match was a sparse-zero interpolation artifact.

**Likely reason:** the RFH→IPC pressure data is gateway-routed to the interior/IHS CAN (where the
cluster lives) and does not traverse the internal diagnostic C-CAN our SGW-bypass taps. The
`31D0-31D3` DID poll works only because UDS diagnostic addressing IS routed to us. **Consequence:
passive-only TPMS logging is not achievable on this tap; the logger must keep polling.** Captures
kept under `tmp/tpms/captures/`; `drive_sniff.py` / `tpms-drivesniff.service` can be stopped
(`sudo systemctl disable --now tpms-drivesniff`) unless re-run for a different signal.

To resolve the observer-effect question without a passive bus channel: either (a) slow the poll
(e.g. 60 s — a dropout persists a full 20-min driving period before C1504 sets, so 60 s still
catches it) to cut bus intervention ~6×, or (b) go truly off-bus with an RTL-SDR on 433 MHz.

## Next steps

1. Let the logger accumulate drives. On a dropout: which physical wheel, key-on time,
   DTC flips. (Prime-suspect status for `7004C287` is now UNPROVEN — treat all four equally.)
2. User to dig up sensor invoices → confirm `11825BA9` (2nd replacement) and get the 1st
   replacement's ID (if it matches nothing on the van, it was binned during replacement #2).
3. If dropouts occur: RTL-SDR (~$30) + `rtl_433` on the Pi decodes 433 MHz FCA TPMS bursts →
   splits "sensor stopped transmitting" from "hub stopped hearing". Not yet purchased.
4. If B1040-64 recurs (watch error counter / new snapshot startups#): RFH connector/ground
   inspection; also consider PROXI alignment check via AlfaOBD.
5. Endgame options: replace `7004C287` if it's the dropout source (cheap, DIY per AllData
   procedure in the scrape) and/or fix the rear position mirror via relearn (needs TPMS
   trigger tool or AlfaOBD sensor-ID write — AlfaOBD CAN write sensor IDs on this platform),
   then clear DTCs (owner OK required; clearing needs no security access beyond session 03
   per factory behavior — verify with `14 FF FF FF` response before assuming).
6. Uncharacterized: `301E-3021` update trigger, `1921` snapshot blob, `40C0` event log,
   locked DID `2024`.
