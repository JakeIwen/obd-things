# B-CAN live ECU discovery and read-only inventory — 2026-07-21

## Outcome

Four physical 29-bit UDS endpoints were independently verified on the current van's DLC
pins 3/11 B-CAN branch at 125 kbit/s:

| key | role | tester → ECU | ECU → tester | `F1A5` data | `F187` spare-part data |
|---|---|---|---|---|---|
| `ics_bcan` | Integrated Center Stack customer-interface panel | `18DA85F1` | `18DAF185` | `0032701720` | `7DN08LXFAB` |
| `uconnect_bcan` | Uconnect radio/display module | `18DA87F1` | `18DAF187` | `0024701A19` | `60986318` |
| `climate_bcan` | Electronic Climate Control / HVAC module | `18DA98F1` | `18DAF198` | `000A702520` | `68516124AE` |
| `emcm2_bcan` | EMCM2 center-stack multimedia/menu controls | `18DAD9F1` | `18DAF1D9` | `0066708320` | `7DN14LXHAF` |

The same four targets answered two independent physical reads, `22 F1A5` and `22 F187`.
Catalog-routed addresses `4A`, `62`, `65`, and `6A` timed out to both reads. Those four remain
unresolved/possibly optional; a timeout in this ignition/session state does not prove absence.

This replaces the earlier statement that direct B-CAN diagnostics were not established. It does
not revive the rejected high 11-bit `+8` hypotheses: the verified endpoints use FCA's 29-bit
`18DAxxF1` / `18DAF1xx` normal-fixed family.

## Conditions and safety boundary

- Vehicle parked, ignition on, engine off; oil change in progress, so engine start was prohibited.
- PCAN physically connected to the pigtail B-CAN DB9 on DLC pins 3/11.
- SocketCAN reported 125 kbit/s, zero RX/TX errors, and zero bus-off events.
- Discovery used one physical request per target at no more than 1 request/s. The per-module inventories
  used only `22` reads, non-clearing `19` reads, and result-only `31 03` reads.
- No functional broadcast, session control, TesterPresent, SecurityAccess, write, IO control,
  routine start/stop, DTC clear, reset, or configuration request was sent.
- Every tool completed without a fatal/interruption state and restored `can0` to listen-only mode.

## Role and identity evidence

The live addresses came from AlfaOBD model-code-88 adapter-6 routing, but the role assignments do
not rely on menu labels alone:

- `85`: live `F1A5=0032701720` exactly matches APK subtype `ICS_FGA` Device 55930. Exact-vehicle
  OEM documentation defines the ICS as the CAN-IHS customer interface for HVAC, audio, hazard,
  ACC, and other button/knob functions.
- `87`: live `F1A5=0024701A19` is not in model-88 UCONNECT Device 6052's subtype list, but it is an
  exact global APK match to UCONNECT Device 8931 at the same `0x87` address. Confirmed Radio/ETM
  and Display Screen Module DTC families independently support the radio/display role.
- `98`: model-88 routes `COND_MARELLI_EP` to `0x98`; its `F187=68516124AE` is the spare-part
  identifier. Exact-vehicle `U1427-87` documentation identifies the reporting ECU as HVAC. The
  live F1A5 value shares the catalog `000A70` family prefix but has no exact APK match.
- `D9`: live `F1A5=0066708320` exactly matches APK subtype `EMCM2` Device 54749. Exact-vehicle OEM
  documentation identifies the EMCM as the CAN-IHS menu/volume control panel below the radio or
  display, not the main radio itself.

Selected standardized identity results, kept separate by ECU namespace:

| module | `F188` manufacturer SW | `F192` supplier HW | `F194` supplier SW | `F195` version |
|---|---|---|---|---|
| ICS | blank | `440000196` | `440000412` | `06 00` |
| Uconnect | `52224999` | `20.40.01` | `145.00.15` | `91 00` |
| Climate | `213601` | `440000242` | `00213601` | `18 04` |
| EMCM2 | `A214900` | `7DN14LXHAF` | `A214900` | `21 49` |

`F187` is the vehicle-manufacturer spare-part-number DID; it must not be conflated with the
software identifiers above. Uconnect's composite `F1A0` contains another leading identifier,
`60981626`; its meaning remains unresolved and it does not replace direct `F187=60986318`.

## DTC inventory

All four accepted `19 02 FF`; all four returned NRC `12` to `19 01 FF` and `19 03`. Status-byte
semantics matter:

- `08` means confirmed/stored only; the current-test-failed bits are clear.
- `40` means only that the test was not completed this operation cycle; it is not by itself a fault.
- `48` combines confirmed with test-not-completed; it still does not mean currently failing.

| module | result |
|---|---|
| ICS | zero records; status-availability mask `4F` |
| Uconnect | 41 records: 31 status-`40` only, nine status-`08`, one status-`48`; all 41 had `testFailed=0` |
| Climate | `U1427-87=08`, confirmed history and not currently failing |
| EMCM2 | `U0140-00=08`, `U0155-00=08`, `U1930-00=08`; confirmed history and not currently failing |

The ten Uconnect records with the confirmed bit are `B143A-11`, `B1570-19`, `B157E-00`,
`B210A-16`, `B280B-02`, `U0100-00`, `U0101-87` (status `48`), `U0155-86`, `U0155-8F`, and
`U0401-86`. This is a stored-history inventory, not an active-fault count.

## Result-only routine and DID-page inventories

The bounded result-only campaign requested `31 03` for RIDs `0200-020F` and `FF00-FF03`:

- ICS, Uconnect, and EMCM2 returned NRC `7F` for all 20 RIDs: service not supported in the active
  session.
- Climate recognized RID `0201` and returned `71 03 02 01 00 02`; the other 19 RIDs returned NRC
  `31`. The vendor-specific `00 02` result bytes are unresolved. This does not name the routine,
  prove it ran successfully, or authorize a future `31 01`/`31 02` start/stop request.

Offline APK inspection found a `31 01 02 01` **routine-start** payload on the default code path for
the model-88-selected Climate Device 6082 profile. That makes the profile's RID `0201`
actuation-sensitive, but the live ECU's unmatched F1A5 means exact variant compatibility remains
unproved. The APK did not provide a climate-reachable `31 03 02 01` decoder or an explicit join from
that payload to a diagnostic-menu label. Nearby menu rows such as “Flap actuators learning test”
are therefore only candidates and must not be assigned to RID `0201` by order. No routine start was
sent in this campaign.

Each module then completed all 256 inherited-session `F100-F1FF` reads with no session change:

| module | positive | NRC `31` | positive DIDs |
|---|---:|---:|---|
| ICS | 24 | 232 | common set below |
| Uconnect | 26 | 230 | common set, plus `F10B`, `F158`, `F1B6`; lacks `F196` |
| Climate | 24 | 232 | common set below |
| EMCM2 | 24 | 232 | common set below |

The 24-DID common set is `F180 F181 F182 F183 F184 F185 F186 F187 F188 F18A F18B F18C F190
F191 F192 F193 F194 F195 F196 F1A0 F1A1 F1A4 F1A5 F1B0`. The reports mask VIN-bearing data;
the large Uconnect `F1B6` certificate remains only in gitignored raw output and is not reproduced.

## Raw provenance

Discovery reports:

- `tmp/discovery/ecu_discovery_20260721_202828_905980-0600.json` — F1A5, SHA-256
  `4a14b5169cb6a14b1988013efb7be618af9e1c7b84edac054d61d8a55676642d`.
- `tmp/discovery/ecu_discovery_20260721_203040_691921-0600.json` — F187, SHA-256
  `be714ce0c78f907837a878a90c6a235e38708a04c1499c1b080784abbb05e952`.

Independent raw CAN evidence:

- `tmp/captures/bcan/events/bcan_f1a5_discovery_ignition_on_engine_off_20260721_202752.log`,
  SHA-256 `fab4c334c6720fd8558b542797a5b3e3087ef9ecc43399b23c5569f4e827e3da`.
- `tmp/captures/bcan/events/bcan_f187_fallback_ignition_on_engine_off_20260721_202851.log`,
  SHA-256 `d4f8a7789b0de56381e95eb36ca354c1c68f0bcecbacf8b7d41966fc306f7482`.

Offline capture summaries were run on the bounded M4 worker snapshots. F1A5 job
`20260722T024356Z-5c5589f4` parsed 5,182/5,182 frames with zero unparsed; F187 job
`20260722T024356Z-221ed653` parsed 11,473/11,473 with zero unparsed. Their reports are
`tmp/ecu_mapping/bcan_live_discovery/{f1a5,f187}/summary.json`, with SHA-256 values
`44bb99124eaed5fc5e3e7565e8f40c58c194131ac8342ccb820b99cbc726e200` and
`b8bc94dc02f18c60e932b21c9a003db836a00935822257cbc927553d408a90d4`. Both independently
show all eight requests and responses only from `85`, `87`, `98`, and `D9`.

Per-module identity, DTC, routine, and checkpointed DID reports are under
`tmp/inventories/{ics_bcan,uconnect_bcan,climate_bcan,emcm2_bcan}/`. They are machine output and
remain gitignored; this finding is the deliberately promoted, VIN-safe interpretation.

## What remains unresolved

1. Do not classify the four timeouts as absent without a new reason to test another power/session
   condition; trailer, blind-spot, and separate display hardware may simply not be fitted.
2. Most positive F1xx DIDs are identity/certificate data, not live sensor values. Names and scaling
   for ordinary live-data DIDs still require catalog/log correlation plus controlled ground truth.
3. Climate RID `0201` remains a result-only lead. Offline APK inspection found a profile start
   payload but no defensible name or result decode. Do not start it without new exact-variant evidence,
   a separate actuation review, and owner authorization.
4. The next high-yield mapping step is a fresh AlfaOBD Gauges + Debug Data recording in small,
   labeled batches while PCAN records the same B-CAN traffic. That can join human labels/rendered
   values to exact per-module DIDs without manually changing hundreds of individual values.
