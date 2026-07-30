# TPMS / RF Hub diagnosis — 2022 Ram Promaster (VIN 3C6LRVDG4NE######)

Handoff doc. State as of **2026-07-30**. Any agent/human should be able to resume from here.
Link path & general UDS tooling: see repo `docs/`, `lib/modules.py`, and the radar project docs
(same PCAN → SGW-bypass → C-CAN tap). Everything below was verified on the vehicle.

**Data locations (repo convention, 2026-07-08 — see root `AGENTS.md` / `README.md`):** all
tool output for this project goes to **`tmp/tpms/`** (gitignored: drive-log CSV, AlfaOBD
sniffs); raw bus-state reference captures live in `tmp/captures/`; committed evidence
(DID sweep + state markers) is promoted into **`findings/` (this dir)**. The old
`dumps/` and `tmp/raw_dumps/` locations no longer exist.

## The complaint

Intermittent **C1504** ("Tire Pressure Sensor 4 — Rear Right") for the vehicle's whole life.
The RR sensor was replaced **twice** to no effect. Wheels rotated once (~Jan 2026). The logger
finally caught the current failure on 2026-07-16 through 2026-07-18 as **C1503-31** (rear left,
no signal), paired one-for-one with loss of sensor ID `7004C287`, physically rear left. See the
campaign status and
[`findings/2026-07-16_c1503_slot4_dropout.md`](findings/2026-07-16_c1503_slot4_dropout.md).

## Key findings (each independently verified)

1. **RF Hub (RFH)**: Continental, P/N 68516285AC, SW 10438241AA v0400. UDS at addr **0xC7**:
   TX `18DAC7F1` / RX `18DAF1C7`, 29-bit normal-fixed, 500k C-CAN. Registered as `rf_hub` in
   `lib/modules.py`. **Answers with ignition OFF** (battery-powered, it's the RKE receiver).
   Wiring (AllData pinout, connector D2665A): CAN-C on pins 11/12, fused B+, ignition feed,
   security K-line. `~/dev/ram_2022_GAS` holds the full factory docs (DTC charts, pinouts).

2. **Current physical wheel-to-sensor-ID map** (service-critical). The map was verified on
   2026-07-07 by deflating each physical tire about 5 psi while watching the four pressure DIDs,
   then re-inflating in reverse order. Both directions gave the same mapping. It remains valid until
   the wheels are moved or rotated.

   **Fault/service target: physical REAR LEFT, sensor ID `7004C287`.** Identify it by both wheel
   and ID; do not select a tire solely from the historical C1504/RFH position label.

   | physical wheel | sensor ID | RFH slot | pressure DID | ID DID | current status |
   |---|---|---:|---|---|---|
   | Front left (FL) | `11825BA9` | 1 | `31D0` | `31CB` | healthy in the captured logs; ID-family outlier |
   | Front right (FR) | `7004E049` | 2 | `31D1` | `31CC` | healthy in the captured logs |
   | Rear right (RR) | `700497DF` | 3 | `31D2` | `31CD` | healthy in the captured logs |
   | **Rear left (RL)** | **`7004C287`** | **4** | **`31D3`** | **`31CE`** | **FAULTY DROPOUT CHANNEL — C1503-31/no signal; evidence-backed sensor replacement target** |

   Shop-facing evidence and requested verification steps:
   [`findings/discount_tire_tpms_evidence_2026-07-19.md`](findings/discount_tire_tpms_evidence_2026-07-19.md).

   Pressure scale **0.1 kPa** (confirmed: deflations tracked exactly). Placard-ish targets:
   fronts 55 / rears 75 psi. `31C6`/`31C7` (2207/5517) likely front/rear thresholds.

   A 2011 CDA engineering bundle independently labels `31D0-31D3` as left-front, right-front,
   right-rear, and left-rear `Altitude Compensated Pressure`, matching the current physical
   pressure-channel order. Its decoded rows use a 16-bit `0.0145 psi/raw` conversion, which
   independently agrees with the live-verified `0.1 kPa/raw` scale to the old database's
   four-place precision. Treat the extra `Altitude Compensated` wording as a historical semantic
   candidate only.
   The same old profile labels sensor-ID DIDs `31CD/31CE` as rear-left/rear-right, the reverse of
   the current van's evidence-backed rear ID order. This contradiction is an explicit warning
   not to replace the live map with a legacy profile. Evidence and source provenance:
   [`legacy FCA Windows/CDA archive triage`](../ecu_mapping/findings/promaster_2022/2026-07-29_legacy_fca_windows_archive.md).

   Exact-model 2022 OEM TPMS operation material independently says the RF Hub
   stores four unique sensor IDs by wheel position. It does not expose the
   `31CB-31D3` DID numbers, scale, or rear ordering, so the controlled
   deflation and current fault-record evidence remains canonical.

   The ID-DID↔pressure-DID slot pairing (31CB↔31D0 … 31CE↔31D3) is no longer just a DID-adjacency
   assumption: the `40A6-40A9` records carry a position byte — pos01=`11825BA9`, pos02=`7004E049`,
   pos03=`700497DF`, pos04=`7004C287` — matching both the ID and pressure DID orders, and this same
   ordering appears in an **independent AlfaOBD capture** (`projects/ecu_mapping/findings/
   promaster_2022/module_did_map.txt`). `7004C287` is absent from `40A6-40A9` in **both** captures.

   ⚠️ **CORRECTION (2026-07-14):** the moment C1512-88 self-erased, `40A6-40A9` began returning
   NRC 0x22 (conditionsNotCorrect) — they are **fault-linked records, NOT a permanent position
   table**. So "`7004C287` has no `40Ax` record" does **not** establish that it is unlocalized, and
   the earlier pre-dropout prime-suspect claim that rested on it was unproven. The current faulty-
   channel designation comes instead from the directly observed Jul 16-18 dropouts. What survives:
   the `40Ax` position bytes did align with the ID/pressure slot order while readable, so the
   ID↔slot pairing stands; and the deflate-derived wheel map never depended on `40Ax` at all.

3. **The dropout source is now caught (2026-07-16 through 2026-07-18):** pressure DID `31D3`
   returned raw `FFFF` (rendered by the current CSV decoder as the impossible **950.5 psi**) on
   708 polls, while FL/FR/physical-RR had zero invalid samples. Every invalid sample coincided
   exactly with raw DTC `550331=8F` = **C1503-31, rear-left sensor no signal, active/lamp
   requested**. `31D3`
   belongs to ID `7004C287`, physically rear left. It recovered and relapsed twice after 20m26s
   and 20m36s, matching the factory 20-minutes-above-15-mph no-message threshold. This proves
   the RF Hub intermittently loses usable data from that sensor; it does not alone split "sensor
   stopped transmitting" from "hub stopped receiving that one sensor." Full timeline and
   inference limits: [`findings/2026-07-16_c1503_slot4_dropout.md`](findings/2026-07-16_c1503_slot4_dropout.md).

4. **Reinterpretation of the history, now bounded by the live dropout:** the July 7 fault-linked
   records showed crossed rear positions, so historical "C1504 = RR" could have sent both
   replacements to the wrong corner. The twice-"replaced" corner's sensor (`11825BA9`) rode the
   forward-cross rotation to FL and is fine; `7004C287` is now physically rear left and is the
   channel that actually drops out. **Physical sensor provenance is unknown:** the owner reports
   two missing sensors were installed by Discount Tire in 2024. Although three current IDs share a
   plausible factory `7004xxxx` family and `11825BA9` is an outlier, programmable aftermarket
   sensors can clone an old OEM ID from the RF Hub. ID shape cannot prove OEM vs aftermarket.
   However, the new C1503 nominal rear-left label agrees with its physical location. The earlier
   crossed rear-position records therefore **must not** be treated as a permanent current mapping;
   localization may have corrected when C1512 self-erased on July 13. Factory guidance still says
   to identify the sensor by ID because RF-Hub positions may be wrong, and not to replace the hub
   for a single sensor mechanical/no-signal fault.

5. **DTCs decoded** (`19 02 0D`): `904064`=B1040-64; `551288`=C1512-88; and
   `550331`=C1503-31. Before July 13, B1040 and C1512 were status 0x08 (confirmed, not currently
   failing) and aging down (−1 error-count per clean cycle). C1512 then self-erased. C1503 first
   appeared active as status 0x8F at 2026-07-16 22:31:47 and drops to 0x0E as soon as `31D3`
   becomes valid again. Two more dropout/recovery episodes were captured Jul 18; its last logged
   state at 2026-07-19 00:06 was recovered/confirmed-only 0x08 with all four pressures valid.
   B1040-64 = "Operational Mode Status info 1 – signal plausibility" (AlfaOBD label): hub
   intermittently missing the BCM ignition-mode CAN message; detections cluster 60–90 s after
   key-on; recurring since startup ~#1014. C1512-88 = localization failed; first at #1243
   ≈ right after the tire rotation. If B1040 keeps recurring, inspect RFH connector/grounds
   (module is behind right B-pillar area per body harness family; see AllData).

6. **Snapshot/environment records** (`19 04 <dtc> 00|01`, rec 00 = first, 01 = latest):
   DID `1008` u32 operating-time minutes; `1009` u16 × 15 s since key-on; `200A` u16 startups
   counter; `6082` failure-type byte; `1921` unknown 9 B. Extended data (`19 06 <dtc> FF`)
   record 01 last byte = error counter. All verified against AlfaOBD screenshots.

7. **Other DIDs of interest**: `301E-3021` per-slot `[04][3-byte timestamp][age]` last-RX
   records — update trigger NOT yet characterized (did not reset on parked pressure events;
   one reset seen at ignition-on). `40A6-40A9` localization/event records `02 01 <pos> <id> ...`.
   `40C0` = 2022-date-stamped factory event log. `2024` = the one LOCKED DID (security access).
   Full sweep: `findings/rf_hub_did_sweep.txt` (118 readable / 1 locked / 0 unresolved),
   ignition-state bands in `findings/rf_hub_sweep_state_markers.txt`.

## TPMS logger and telemetry ownership

- **`tpms_logger.py` (this dir)** remains the dropout tripwire. Its fixed RF Hub profile polls
  `31D0-31D3`, `301E-3021`, and `19 02 0D` every 10 seconds, writes
  `tmp/tpms/tpms_drive_log.csv`, and publishes valid pressures as
  `tire.pressure.fl/fr/rr/rl`. It drains stale ISO-TP frames before every request and accepts
  pressure/DTC data only when the positive response echoes the requested DID or `19` subfunction.
  Known DTCs retain both raw ECU value and label, for example `550331(C1503-31)=8F`. Raw pressure
  `FFFF` is invalid/no data: the logger writes an empty pressure cell and withholds telemetry. Its
  diagnostic request allowlist is fixed to physical RF Hub `22 31D0-31D3`,
  `22 301E-3021`, and `19 02 0D` reads (plus ISO-TP flow control required to receive a segmented
  response); it sends no session-control, tester-present, write, clear-DTC, routine, or security
  request.
- **Broker delegation is checked first in auto mode.** Any response from the broker Unix socket
  makes the standalone logger yield completely, regardless of broker version, active-drive setting,
  HTTP status, or response fields: it takes no CAN lock, does not change `can0`, and opens no UDS
  socket. Timeouts, malformed responses, permission failures, and other ambiguous I/O failures also
  yield. This fail-closed rule prevents a temporarily unhealthy or older broker's passive collector
  from being starved by a competing TPMS session. With the current broker, its coordinated
  active-drive collector owns the single armed interval, continues publishing the qualified C-CAN
  powertrain metrics, and polls the four allowlisted RF Hub pressure DIDs alongside PCM electrical
  telemetry. The standalone extended last-RX/DTC CSV campaign is not collected while delegated.
- **The standalone path is a fail-closed fallback, not a competing collector.** It is used only
  when the broker endpoint is narrowly proven absent because the Unix socket is missing or refuses
  the connection. A live broker that explicitly disables active-drive still owns passive telemetry,
  so that setting does **not** authorize this logger to take over. Before taking the exclusive
  diagnostic lock the fallback requires a verified UP/500 kbit/s/listen-only/ERROR-ACTIVE interface,
  a usable same-boot `c-can` topology record with exact DLC pair `6/14`, no active operation inhibit,
  passive C-CAN identity, and three fresh standard `0x0FC` samples above 400 rpm. The passive
  identity/RPM gate holds the shared observer lock so an interface-changing participant cannot race
  that socket. It repeats every check under the exclusive lock, including broker absence before and
  after the RPM sample, captures the exact safe interface state, arms once, and repeats the RPM,
  topology, inhibit, broker-absence, and adapter-health gates throughout the session. A broker that
  starts later therefore ends the fallback and triggers restoration. Ignition presence, charging
  voltage, or the old `0x2EF`-only gate cannot authorize transmission.
- The fallback closes its ISO-TP socket before exactly restoring and reading back the captured
  listen-only configuration, all while still holding the exclusive lock. This happens after normal
  completion, engine-RPM loss, exceptions, and termination. A failed restore is a critical fault:
  the process latches against further polls and writes the same-boot operation inhibit
  `tpms-restoration-failed`. Inspect and explicitly prove `can0` safe/passive before considering
  `python3 tools/can_operation_state.py inhibit-end tpms-restoration-failed`.
- Manual fallback has the same broker-absence gate and cleanup contract. It refuses to start while
  the broker is live or its status is uncertain; an explicitly authorized standalone campaign must
  stop the broker first. Start from passive mode:
  `./bringup.sh && python3 projects/tpms/tpms_logger.py`. Do not pre-arm with `--tx`.
- The earlier campaign used passive `0x2EF` presence as its ignition gate. Its observer-effect
  finding remains valid—diagnostic polling can keep FCA network management awake, and stopping it
  allowed sleep after about 60 seconds—but `0x2EF` alone is no longer an authorization gate.
  Engine-running `0x0FC` evidence prevents the unattended logger from waking or polling an
  ignition-on/engine-off vehicle.
- The tracked unit is `projects/tpms/tpms-logger.service`; the previously installed live unit is
  `/etc/systemd/system/tpms-logger.service`. The 2026-07-30 integration change is offline-only:
  no service was installed, enabled, restarted, or live-tested. Any deployment/restart remains an
  explicit owner-authorized operation after reviewing current broker and adapter state.
- **`isotp_decode_rfh.py` (this dir)** — offline ISO-TP transcript decoder for candump logs
  (hardcoded to the RFH ID pair); used to decode the AlfaOBD session sniffs in
  `tmp/tpms/rfh_alfaobd_sniff_ccan_resilient.log`.
- **voltage_mon cron remains COMMENTED OUT** (dated tag in `crontab -l`), so there are no
  scheduled low-battery ntfy alerts. The 2026-07-25 hardening keeps awake-bus reads passive and
  permits a sleeping-bus wake only behind the exclusive SocketCAN lock, absence of same-boot
  external-tool inhibits, an explicit same-boot C-CAN/B-CAN topology record, and an immediate
  under-lock interface/silence recheck. Armed/down/unhealthy/unknown/CAN-CH states never wake.
  AlfaOBD controller actions automatically inhibit unattended wake. It no longer needs to wait for
  PCAN to return to B-CAN for contention safety, but re-enabling cron remains a separate owner
  decision.
- **Before any manual bus work**: `sudo systemctl stop tpms-logger` (restart after).
  Gotcha: `pkill -f`/`pgrep -f` with a pattern that appears in your own command line kills
  your own shell — use `pkill -x candump` or exact PIDs.
- **Current integration state (code updated 2026-07-30; deployment not live-verified):** one
  PCAN adapter still means there can be only one armed owner. The coordinated broker owner is the
  coexistence path for live passive powertrain, PCM electrical, and four TPMS pressures. The auto
  standalone logger runs only when the broker Unix endpoint itself is proven absent, then only after
  acquiring the exclusive lock. Any live or ambiguously unreachable broker makes it defer so a
  short observer-lock gap cannot turn into a drive-long dashboard outage. Check the live service and
  broker status before changing either.

## Campaign status (2026-07-19) — dropout repeatedly captured

- **Physical-RL sensor `7004C287` is the isolated dropout channel.** First caught Jul 16 at
  22:31:47; it remained invalid through that drive, began the Jul 17 first drive invalid, recovered,
  and then relapsed twice at the factory's ~20-minute no-message threshold. Two further dropout/
  recovery episodes were captured Jul 18. Across 6,891 logged samples through Jul 19 00:06, all
  708 invalid readings were physical RL and every one exactly matched active `C1503-31=8F`; the
  other three slots had zero invalid samples. The warning light's observed flashing at the next
  drive start agrees with DTC status `0x8F` (active plus warning-indicator-requested).
- **C1512-88 SELF-ERASED at 2026-07-13 20:37** (caught live by the logger; confirmed by UDS —
  `19 02 0D` stopped returning C1512, and it remains absent after the C1503 recurrence). It aged
  out on clean cycles, as predicted.
  Simultaneously `40A6-40A9` went to NRC 0x22 → see the CORRECTION above.
- **B1040-64 persists**, mostly as confirmed/non-active status 0x08. Its extended-data error
  counter was down to **10** on Jul 14 (from 37 at first sight); no post-recurrence extended-data
  read has been made.
- **Pressure decode VALIDATED against the vehicle**: owner cycled the IPC to the TPMS screen on
  the 2026-07-13 drive and saw rears peak at **90 psi**; the logger's max for that session is
  **90.0 psi (RL)**. The 0.1 kPa scale is now confirmed against the cluster's own display.
- Before the recurrence, the cluster showed four real numbers (not dashes). During the captured
  failure, the RF Hub's `FFFF` sentinel implies the cluster should show `--` for physical RL.
- The fault **did recur while polling every 10 s and after the deflate/re-inflate exercise**. The
  logger observer effect did not suppress it; the tire exercise at most delayed recurrence.
- The last sample, Jul 19 00:06:53, had all four pressures valid and C1503 stored/non-active
  (`08`), but the repeated receive droughts make another recurrence likely until the cause is
  corrected.
- A parked ignition-on inventory on Jul 21 again found exactly `C1503-31=08` and `B1040-64=08`:
  both remain confirmed history, with testFailed clear at that instant. No DTC was cleared.

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
(e.g. 60 s — a dropout persists a full 20-min driving period before a C150x fault sets, so 60 s still
catches it) to cut bus intervention ~6×, or (b) go truly off-bus with an RTL-SDR on 433 MHz.

## Next steps

1. Keep the logger running to measure recurrence. The tripwire objective is achieved: physical RL,
   sensor ID `7004C287`, is the isolated dropout channel.
2. Find the 2024 Discount Tire invoice/work order and later replacement invoices. Brand/SKU and
   recorded wheel positions may identify which current sensor bodies are aftermarket; IDs alone
   cannot, because a programmable replacement may clone an OEM ID stored in the RF Hub. Confirm
   `11825BA9` (likely the second replacement) and get the first replacement's ID if recorded.
3. For a mechanism-level confirmation before parts: RTL-SDR (~$30) + `rtl_433`, or a compatible
   TPMS trigger/analyzer, splits "sensor stopped transmitting" from "hub stopped hearing." Without
   that extra split, replacing physical-RL sensor `7004C287` is now the evidence-backed repair target.
4. If B1040-64 recurs (watch error counter / new snapshot startups#): RFH connector/ground
   inspection; also consider PROXI alignment check via AlfaOBD.
5. On sensor replacement, write/relearn the **new** sensor ID and verify over multiple drives longer
   than 20 minutes. Do not rewrite unchanged IDs as a no-signal remedy. Treat the earlier crossed
   rear-position records as historical, then verify current localization rather than forcing a
   position change. DTC clearing still requires owner authorization; clearing needs no security
   access beyond session 03 per factory behavior, but verify the `14 FF FF FF` response before assuming.
6. Do **not** automate DTC clearing as a lamp workaround without a controlled test and explicit
   owner authorization. A recovered sensor already clears the active/lamp-request bit without
   erasing the stored DTC. During `31D3=FFFF`, clearing cannot supply the missing pressure and the
   monitor is expected to reassert the fault (exact timing after a clear is untested). The verified
   OEM workflow says to erase **all** RF-Hub DTCs after repair; selective C1503 clearing is not yet
   verified, and repeated all-DTC clears would destroy C1503/B1040 history and hide new RF-Hub faults.
7. Uncharacterized: `301E-3021` update trigger, `1921` snapshot blob, `40C0` event log,
   locked DID `2024`.
