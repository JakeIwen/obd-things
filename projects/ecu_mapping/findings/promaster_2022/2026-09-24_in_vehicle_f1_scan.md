# In-vehicle F1 diagnostic client — September 24, 2026

## Result

An **in-vehicle diagnostic client that uses tester source address `F1`** periodically runs a
read-only identity and DTC sweep of the whole vehicle. It uses the same physical 29-bit request
identifiers as the Pi and AlfaOBD (`18DA<target>F1`), so its traffic can look like ours. It is **not**
the Pi: the Pi's C-CAN role was listen-only for the whole tester window, and the owner confirmed that
no external scan tool was connected on 2026-09-24. The client was observed on 2026-08-31 UTC (three
windows) and on 2026-09-24 from 21:12:17.587 to 21:27:08.649Z (15:12–15:27 MDT).

Its identity is unresolved. The leading guess is the **TBM2 telematics module (`0xC6`)**, with
moderate confidence. Its target table contains every other registered address plus 25
optional/absent ones, so the single omission of `0xC6` is the strongest clue. The Security Gateway
(`0xCB`, which the table did include and which stayed silent) is the alternative. Neither is proven.

> **Operational warning:** during a period with no Pi polling, `18DAxxF1`/`18DAF1xx` traffic that the
> broker did not originate is this client, not a Pi tool, AlfaOBD, or a bus fault. Attribute
> diagnostic traffic by its originator's logs and timing, never by the `F1` source byte alone.

This analysis was offline and read-only against saved captures. No CAN transmission was involved.

## Provenance

- 2026-09-24 raw capture (archived, not committed):
  `/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/broker-drive-20260924T210710082916`
- 2026-08-31 comparison run (same client, 5 DTC passes plus the PCM/TCM job):
  `/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/broker-drive-20260831T023646810679`
  (02:36Z, 20:36 MDT on 2026-08-30), plus two further 2026-08-31 windows in the same archive.
- Analysis working outputs: `tmp/ecu_mapping/f1-scan-20260924/{exchanges.csv,modules.md,dids.md,tcm_21xx.md,dtcs.md}`.
- Promoted evidence: [`2026-09-24_in_vehicle_f1_scan_exchanges.csv`](2026-09-24_in_vehicle_f1_scan_exchanges.csv)
  holds the 391 de-duplicated 2026-09-24 request/response exchanges, with the VIN serial masked
  in both ASCII and hex (`3C6LRVDG4NE######`, hex `…4E45232323232323`).

## Client behavior

### Job structure (2026-09-24)

| phase | UTC | content |
|---|---|---|
| A — identity | 21:12:17.587–21:18:44.987 | per target: `22 F132`, `22 F100`, `22 F1A0`; PCM also `1A 87` |
| B — DTC pass 1 | 21:18:46.247–21:20:33.945 | `19 02 0D` to every responder; PCM fallback `18 00 FF 00` |
| C — DTC pass 2 + data | 21:20:35.236–21:25:16.860 | round-robin with the PCM job (`22 0137`, `22 0320`, `22 1FCC–1FDB`, `21 01–07`) and the TCM job (`22 026B`, `22 211B–213E`) |
| D — DTC pass 3 | 21:25:18.404–21:27:03.495 | `19 02 0D` repeat |
| E — DTC pass 4 | 21:27:05.096–21:27:08.649 | first two targets only; aborted when Pi polling resumed |

- The client moves to a new target about every 10 s. PCM first-response latency was
  1.2–5.3 ms.
- A request answered with `7F xx 31` or `7F xx 12` was sent four times in total, about 1 s
  apart, with identical results. A timeout (about 3.1 s) was not retried, so the client made one attempt
  per DID to each silent target.
- Its FlowControl is **unpadded `30 00 00` with DLC 3**. The consequence for the PCM is described
  below.
- The job is entirely read-only. The observed services were `22`, `19`, `1A`, and KWP `21`/`18`
  only. It sent no session change, SecurityAccess, write, routine,
  IO control, or DTC clear.

### Coexistence with Pi polling

Across 87 saved captures, the client never overlapped Pi `F1` polling:

- It starts only after `F1` diagnostic traffic has been quiet for a while. The quiet interval
  was 92 s on 2026-09-24 (Pi `F1`-quiet 21:10:45.8–21:27:10.7Z, first client request at
  21:12:17.587Z). On 2026-08-31 it was 309 s before one window and 5–6 s before another.
- It aborts within 1–2 s when the Pi resumes. On 2026-09-24 its last request was at
  21:27:08.649Z, the Pi resumed at 21:27:10.7Z, and no client request followed.
- Quiet alone does not trigger it. On 2026-08-30 there was about 98 minutes of engine-running
  quiet without a scan. The trigger is therefore scheduled or conditional, and it is not yet
  identified.

### Physical attachment and gateway routing

- **C-CAN and B-CAN are direct.** On both buses the client's FlowControl follows each ECU first
  frame within about one frame time, which rules out a gateway hop. B-CAN targets (`0x32 … 0xD9`)
  appear only on B-CAN, and none of those requests or responses is copied to C-CAN.
- **CAN-CH is gateway-forwarded from C-CAN.** Requests for `0x26 0x28 0x30 0x31 0xA0 0xC0`
  appear on C-CAN first and on CAN-CH a median 0.31 ms later (range 0.23–2.78 ms, 51 exchanges),
  forwarded frame by frame. A gateway emits its own FlowControl **`30 00 0A`** (STmin 10 ms) on
  CAN-CH.
- **CAN-CH replies are mirrored back to C-CAN as `18DAF2xx`**, with target byte `F2` rather than
  `F1`. The client's own FlowControl on C-CAN is `30 00 00`. An `18DAF2xx` frame on C-CAN is
  therefore a gateway copy of a CAN-CH diagnostic reply, not a second tester.

## Module inventory

Every responder was already registered in `lib/modules.py`. **`telematics` (`0xC6`) was the only
registered module the client never queried.**

| addr | key | bus | result |
|---|---|---|---|
| `0x10` | `pcm` | C-CAN | answered (see PCM) |
| `0x18` | `tcm` | C-CAN | answered, 39/39 positive |
| `0x1F` | `shifter` | C-CAN | answered |
| `0x2A` | `radar_acc` | C-CAN | answered |
| `0x40` | `bcm_ccan` | C-CAN | answered |
| `0x60` | `cluster` | C-CAN | answered |
| `0xC7` | `rf_hub` | C-CAN | answered |
| `0x28 0x30 0x31 0xC0` | `abs_canch` `eps_canch` `half_canch` `orc_canch` | CAN-CH (via C-CAN) | answered |
| `0x85 0x87 0x98 0xD9` | `ics_bcan` `uconnect_bcan` `climate_bcan` `emcm2_bcan` | B-CAN | answered |
| `0xC6` | `telematics` | C-CAN | **never queried** |

Twenty-five unregistered addresses were queried once per DID and stayed silent. Catalog names are
AlfaOBD candidates only. Silence after a single attempt does not prove a module is absent.

| addr | bus | AlfaOBD catalog candidate |
|---|---|---|
| `0x01` | C-CAN | DCU_FGA/airbag-family 7DA entries (weak) |
| `0x12` | C-CAN | LC02 (KWP legacy; weak) |
| `0x1A` | C-CAN | DTCM_CUSW (driveline/transfer-case) |
| `0x20` | C-CAN | CDCM (adapter 7) / legacy ABS |
| `0x42` | C-CAN | EVCU |
| `0x44` | C-CAN | BPCM (battery pack) |
| `0x4B` | C-CAN | no catalog entry |
| `0x50` | C-CAN | no catalog entry |
| `0x58` | C-CAN | OCS_CUSW (occupant classification) |
| `0xA1` | C-CAN | TIRE_CONTROL_TRW |
| `0xC4` | C-CAN | STEER_LOCK_TVI |
| `0xCB` | C-CAN | SGW_FGA (security gateway) |
| `0x26` | CAN-CH | PAM2 park assist (model 88) |
| `0xA0` | CAN-CH | PARK_BOSCH_EP (model 88) |
| `0x32` | B-CAN | DSCM driver seat (adapter 6) |
| `0x41` | B-CAN | PLGM_CUSW/FGA power liftgate (adapter 6) |
| `0x4A` | B-CAN | TRAILER_TOW (model 88) |
| `0x54` | B-CAN | no catalog entry |
| `0x55` | B-CAN | no catalog entry |
| `0x62` | B-CAN | LBSS_FGA left blind-spot (model 88) |
| `0x65` | B-CAN | RBSS_FGA right blind-spot (model 88) |
| `0x66` | B-CAN | no catalog entry (EVCU is only listed as `0x42`) |
| `0x6A` | B-CAN | DCSD (model 88) |
| `0x83` | B-CAN | AMP_CUSW/AMP_FGA amplifier (adapter 3) |
| `0xC2` | B-CAN | CSWM_CUSW heated seat/wheel (adapter 3) |

These results agree with the earlier Pi timeouts for `0x4A/62/65/6A` on B-CAN and with the
absence of `0x26/0xA0` from the CAN-CH catalog.

## CAN-CH identities (`22 F1A0` composite record)

These are the first Pi-side captures of CAN-CH identity data. Each F1A0 composite contains the
`F187` part number and the `F1A5` subtype, and both exactly match the 2026-07-25 AlfaOBD
identities in the [CAN-CH verification](2026-07-25_canch_live_verification.md). Field names other
than F187/F1A5 are positional guesses (`…-like`). The embedded VIN matches this van and is masked.

| module | part (F187) | sw / F192-like | F194-like | F195-like | F1A5 | `22 F132` | `22 F100` |
|---|---|---|---|---|---|---|---|
| ABS `0x28` | `68516283AD` | `0265957013` | `1267985695` | `0202` | `0006501520` | positive, 10 spaces | positive, 29 spaces |
| EPS `0x30` | `68509191AD` | `F4C1` | `FI06EB00-00` | `0030` | `0002507919` | `7F 22 31` | `7F 22 31` |
| HALF `0x31` | `68567254AA` | `A012N333` | `006.003.000` | `0203` | `001E502920` | `7F 22 31` | `7F 22 31` |
| ORC `0xC0` | `68518674AC` | `0285015767` | `BB101464` | `1020` | `001A507720` | positive, 10 spaces | `7F 22 31` |

On the C-CAN and B-CAN responders, the F1A0 composites matched the Pi's prior per-ECU
`F100-F1FF` sweeps apart from the masked VIN. The TCM also repeated `F132=68532161AF`.

## PCM (`0x10`) — per-ECU results

| request | result |
|---|---|
| `22 F132`, `22 F100` | `7F 22 12` |
| `22 F1A0` | first frame only, declared 67 B (`62 F1A0 20 20 20 …`) |
| `1A 87` | first frame only, declared 22 B (`5A 87 02 40 7F 34 …`) |
| `19 02 0D` | `7F 19 11` (serviceNotSupported) |
| `18 00 FF 00` (KWP ReadDTCByStatus) | `58 00` — **zero stored DTCs** (AlfaOBD's July trace returned `58 03 …`) |
| `22 0137` | positive, 4 B, `00 01 3A F5` on 2026-09-24. The value changed between runs, so the content is dynamic. |
| `22 0320` | positive, 1 B, `00` |
| `22 1FCC`–`1FD8` (13 DIDs) | positive first frames, each declaring **243 B**. The body was not received. |
| `22 1FD9` | positive first frame declaring **515 B**. The client sent no FlowControl and abandoned it. |
| `22 1FDA`, `22 1FDB` | `7F 22 12` |
| KWP `21 01`–`21 07` | positive first frames declaring 149, 177, 177, 177, 177, 135, and 37 B. The body was not received. |

**FlowControl finding:** after the client's unpadded 3-byte `30 00 00`, the PCM never sent a
consecutive frame. On 2026-08-30 the Pi's 8-byte zero-padded FlowControl received the complete
`1A 87` response. **PCM multi-frame reads therefore require an 8-byte padded FlowControl**, in
addition to the known fixed-DLC-8 request framing. The TCM, CAN-CH modules, and other responders
completed multi-frame replies after the same unpadded FlowControl.

These PCM reads succeeded in the client's default/inherited session, with no `10 92`. The DIDs
`1FCC`–`1FD9`, `0137`, `0320`, and the KWP `21 01`–`07` local IDs are first observations and have no
labels. The first-frame bytes after the service/DID echo are zero except for `21 07`
(`00 00 FF 00`). No body content is known.

## TCM (`0x18`) — per-ECU results

All 39 TCM requests were positive. These were UDS `22 21xx` reads with 2-byte DIDs, not KWP
`21 xx`. Each DID was read once per run, and the table compares the 2026-09-24 run with the
2026-08-31 02:36Z run. Decodes come from the AlfaOBD ZF9HP catalog in `projects/ecu_mapping/zf9hp.py`.
These are the first live reads of those catalog rows on this van. The labels are catalog
candidates, not controlled verification.

| DID | 2026-09-24 raw | catalog decode | 2026-08-31 |
|---|---|---|---|
| `026B` | 100 B static record (below) | not in catalog | identical |
| `211B`–`211E` | `00` each | not in catalog (1 B) | identical |
| `211F` / `2120` / `2121` / `2122` | `13` / `3F` / `08` / `3B` | clutch B filling pressure 190 mbar / filling counter 63 / filling time 16 ms / fast-filling counter 59 | identical |
| `2123`–`2126` | `E5` / `41` / `01` / `3E` | clutch C −270 mbar / 65 / 2 ms / 62 | identical |
| `2127`–`212A` | `01` / `36` / `01` / `31` | clutch D 10 mbar / 54 / 2 ms / 49 | identical |
| `212B`–`212E` | `08` / `38` / `03` / `32` | clutch E 80 mbar / 56 / 6 ms / 50 | identical |
| `2130` | `02` | not in catalog (1 B) | identical |
| `2131`, `2132`, `2136`–`213A` | `0000` each | not in catalog (2 B) | identical |
| `213B` | `0000` | TCC boost time offset 0 ms | identical |
| `213C` | `0003` | TCC base-point adapt 3 mA | identical |
| `213D` | `00000000FF34` | gear engagement 0/1/2 = 0 / 0 / −204 mbar | **changed** (`…FF33` on 08-31) |
| `213E` | `FF2F00000000` | gear disengagement 0/1/2 = −209 / 0 / 0 mbar | identical |

`211F`–`212E` and `213B`–`213E` are **live-positive learned clutch-fill and adaptation values**.
They were stable across the 24 days between runs except for a one-count change in `213D`, which fits
slowly updating adaptations rather than live signals. `211B`–`211E`, `2130`–`2132`, `2136`–`213A`,
and `026B` are new positives with no catalog label. The reads were made while driving
(35–102 km/h, oil 72–75 °C), and the values were unchanged within that range.

Full `026B` payload (100 data bytes, identical on 08-31 and 09-24):
`15000BAA4A490002E139000099A13D2B9B6A3C2B7D0018580000187401E301E301F71FF80002CA020020000000002B6604DD0301F401F40B3401F401F416FC00000001004001000008081B1A0C0C470C00008002000000000000000000007E0004000000`

## DTC snapshot — `19 02 0D`, 2026-09-24

There were three complete passes, and every module returned identical data in each. The mask
`0x0D` selects testFailed, pending, and confirmed DTCs, so history-only records do not appear.
Descriptions are module-scoped and come from the repo's reviewed tables where one exists.

| module | DTC (status) |
|---|---|
| shifter `0x1F` | `P081C-64` (08), `P1C73-24` (0F) |
| ABS `0x28` | `C1200-17` (4C) |
| radar `0x2A` | `C1418-78` (08) |
| EPS `0x30` | none |
| HALF `0x31` | `B1006-49` (0C), `C14A5-92` (08), `C14A5-97` (08, not present on 08-31) |
| BCM `0x40` | `B1636-15` (4D), `B1632-15` left high beam (4D), `B162E-15` right low beam (0F), `B162A-15` left low beam (0F), `B104E-15` right DRL (4D), `B104D-15` left DRL (4D) |
| cluster `0x60` | `U1741-87` (08); `U1701-87`, present on 08-31, is no longer reported |
| ICS `0x85` | none |
| Uconnect `0x87` | `B143A-11` (08), `B1570-19` (08), `B1577-13` (0F), `B210A-16` (0C), `B280B-02` (08), `U0155-86` (08) |
| climate `0x98` | `U0155-87` (08), `U0423-87` (08) |
| ORC `0xC0` | none |
| RF Hub `0xC7` | `B1040-64` (08; 0E on 08-31) |
| EMCM2 `0xD9` | none |
| TCM `0x18` | none |
| PCM `0x10` | UDS `19` unsupported; KWP `18 00 FF 00` → zero DTCs |

This is the first recorded DTC read of the CAN-CH modules from the Pi side. AlfaOBD read them
on 2026-07-25.

**Owner-confirmed context (2026-09-24):** the owner has confirmed that BCM `B162A-15` and
`B162E-15` (left/right low-beam circuit open) are entirely caused by the owner's LED headlight
conversion. `B1636-15` is likely from the same lamp family and the same cause. These codes are not
a wiring or BCM fault to chase. See the [LED headlight handoff](../../../vehicle_configuration/LED_HEADLIGHTS_HANDOFF.md).

## Follow-ups (parked; none run)

1. **PCM padded-FlowControl read:** a bounded, parked read of `22 0137`, `22 0320`, and
   `22 1FCC`–`1FD9` should use fixed-DLC-8 requests and an 8-byte zero-padded FlowControl without
   a session change. It could add the KWP `21 01`–`07` local IDs if they are justified. This is
   active diagnostic traffic and requires the root README's safety gates plus coordination with
   the broker. Its first question is simply what the 243/515-byte records contain.
2. **Tester identity:** compare against a Uconnect/Mopar **Vehicle Health Report** dated
   2026-08-30 about 20:26–20:56 MDT or 2026-09-24 about 15:12–15:27 MDT. A report at either time
   would confirm TBM2 as the originator. Otherwise, find the trigger condition that differs from
   the 98-minute quiet interval on 2026-08-30 without a scan.
3. **Bus-identity classifier caveat, which affects code and is not fixed here:**
   `lib/canbus.py` `CANCH_DIAG_SIG` treats the request IDs `18DA28F1`, `18DA30F1`, `18DA31F1`,
   and `18DAC0F1` as decisive CAN-CH evidence. During a client scan, those requests appear on
   **C-CAN**. A passive `identify_bus()` probe of C-CAN at that moment could therefore return
   `can-ch`. Only the reply IDs `18DAF128/F130/F131/F1C0` are CAN-CH-exclusive, so the set should
   probably be restricted to the reply side. The owner of that code should review the change and
   its tests.
4. **Broker/tooling hygiene:** any passive analysis that attributes `F1` traffic must exclude
   these windows. Any Pi active-diagnostic start should expect up to about 2 s of trailing
   client traffic.
