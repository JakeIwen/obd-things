# Three-bus telemetry readiness — 2026-08-27

## Outcome

The permanent C-CAN, B-CAN, and CAN-CH taps provide enough synchronized drive
variation to support targeted mapping, but the evidence does **not** yet justify
generic active polling on all three buses. C-CAN already has reviewed
dashboard DIDs. B-CAN has a small number of useful ECU-scoped active-read
candidates. CAN-CH has a strong passive mapping surface, while its saved active
reads are still almost entirely identity data.

The broker-owned drive recorder was therefore extended and deployed to retain
all three raw buses on every qualified future drive. Active polling remains
limited to reviewed C-CAN targets until one B-CAN or CAN-CH candidate passes
the same label, scaling, session, variation, and restoration gates.

No CAN frame was transmitted for this analysis. Saved logs, prior read-only
inventories, the reconstructed AlfaOBD catalog, the local OEM corpus, and
read-only live service/interface status were used.

## Synchronized passive evidence

Source session:

`tmp/captures/three_bus_drive/session_20260823T042427.681227Z_1189408/`

The six-hour session completed with zero reported drops on all three roles. Its
high-activity first chunk covered the same 1,508-second awake interval:

| role | frames | mean rate | identifiers | source SHA-256 |
|---|---:|---:|---:|---|
| C-CAN | 4,040,291 | 2,678.9 fps | 103 | `0bd19a979b0acc061e024fd4d352c14759d65f930e534251600aa67a1f9fa7cd` |
| B-CAN | 266,956 | 177.0 fps | 70 | `80e9a661e96560e05ae7f52a46fafa083edfbe2e95a777c27149e4e09efd47be` |
| CAN-CH | 2,810,125 | 1,863.3 fps | 61 | `f00baae4a2e614176465d0335f10a3e8d53dfde0731a9620445d4bd6a6ba7e08` |

Offline `can-capture-summary` jobs were
`20260828T011408Z-6b0d2f0a` (C-CAN),
`20260828T011401Z-0caef3ff` (B-CAN), and
`20260828T011407Z-78c16015` (CAN-CH).

Thirty-three stream identities were shared between C-CAN and CAN-CH. In
particular, established RPM `0x0FC` and speed `0x101` occurred at matching
high rates on both branches, so active CAN-CH reads for those values would add
traffic without adding dashboard information. CAN-CH also supplied 26 stream
identities absent from both other captures, including unique high-rate
`0x0DA`, `0x0DC`, `0x0F1`, `0x106`, `0x10E`, `0x117`, and `0x1F6`.
Exact-vehicle OEM documentation independently says the ABS/EPS network
broadcasts wheel-speed, steering-angle/position, steering-torque, yaw, and
lateral-acceleration inputs. It does not bind those names to CAN IDs or fields.
The unique CAN-CH streams are therefore a bounded controlled-correlation
surface, not decoded metrics.

B-CAN supplied 63 stream identities absent from both other captures. The
separately established `0x46C` voltage and the access/door candidates from the
2026-08-26 RKE campaign remain the most mature passive body-network signals.

## Active-read evidence boundary

Four B-CAN diagnostic endpoints are live verified. Prior current-vehicle
AlfaOBD status/wire evidence provides these potentially useful reads:

| module/DID | candidate dashboard use | evidence boundary |
|---|---|---|
| ICS or EMCM2 `2001` | odometer (`u24be × 0.1 km`) | label/scale recur exactly across current status sequences; one parked no-session ICS read after a validated B-CAN wake timed out, so support during a fully powered broker-owned running interval remains unproven |
| Uconnect `1823` | infotainment ECU temperature | label association exact; `raw − 40 °C` has only one observed point and remains candidate-only |
| ICS `0300` | dimmer percentage/group | five displayed values match `0.5%/LSB`; remaining positions unresolved |
| Uconnect `180C` | volume/EQ group | exact association, but low vehicle-health value |
| EMCM2 `2A00/2A01` | knob/button events | controlled and exact, but event controls belong in passive/event UI rather than a one-hertz telemetry poller |

The installed Climate ECU positively answers six profile DIDs, but the selected
AlfaOBD subtype failed variant verification and rendered physically impossible
labels/scales. None is eligible for polling or telemetry promotion.

The saved CAN-CH diagnostic sessions verified ABS, EPS, HALF, and ORC identity
and addressing but contain no defensible runtime DID label. The reconstructed
AlfaOBD database has exact `isocode` rows for the installed subtypes but does
not directly join those subtype IDs to the request arrays. Legacy FCA
ABS/EPS/ORC definitions overlap mainly in identity records and already failed
the transferability gate.

### Installed ABS candidate Plots map recovered statically

After owner authorization, bounded DEX/JADX work recovered the missing join
without returning the full proprietary decompile. Exact current ABS
`F1A5=0006501520` selects AlfaOBD Device 13883 (`ABS9_CAN`). The connection
dispatcher explicitly assigns Plots request array `aa.b` to that profile and
selects its Device-13883 display ordering. The array has exactly eleven DIDs;
the reconstructed database has exactly eleven ordered `ABS9_CAN` parameters:

| order | DID | AlfaOBD parameter | native candidate formula |
|---:|---:|---|---|
| 1 | `1002` | vehicle speed | `u16be × 0.0078125 km/h` |
| 2 | `1004` | battery voltage | `u16be / 1000 V` |
| 3 | `0880` | front-left wheel speed | `u16be × 0.0078125 km/h` |
| 4 | `0881` | front-right wheel speed | `u16be × 0.0078125 km/h` |
| 5 | `0882` | rear-left wheel speed | `u16be × 0.0078125 km/h` |
| 6 | `0883` | rear-right wheel speed | `u16be × 0.0078125 km/h` |
| 7 | `0884` | oil/brake pressure | signed `i16be × 0.015625 bar` candidate |
| 8 | `0885` | steering angle | signed `i16be × 0.0625 degree` candidate |
| 9 | `0886` | yaw rate | signed `i16be × 0.0625 degree/s` candidate; response path can expose more than one field |
| 10 | `0887` | lateral acceleration | `u16be × 0.02 - 40.96 m/s²` candidate; response path can expose more than one field |
| 11 | `0888` | longitudinal acceleration | `u16be × 0.02 - 40.96 m/s²` candidate; response path can expose more than one field |

The formulas come from the profile's `n0.z1.r()` response dispatcher. The
pressure label is retained exactly as AlfaOBD's `Oil pressure`; OEM context
strongly suggests a brake-hydraulic pressure source, but static evidence does
not authorize silently renaming it. `0886-0888` need raw current responses to
select the intended field when the decoder emits multiple values.

This is strong installed-variant vendor-derived candidate evidence, not a live
support or physical-scale proof. No `0880-0888` request was sent during this
static pass. A bounded current-van support read with ignition on, engine off is
the next gate; a later synchronized drive must establish variation, wheel
ordering, sign, zero points, and plausibility before dashboard promotion.

Static provenance:

- APK SHA-256 `97b0f100280453b134ceffc09025f2c443adb383ad4382afcd0b0fd7a9a853b9`;
- `aa.java` SHA-256 `7d4ea16e66225c83c2edfe6a9f334cf5f7fb22c12140aeb8c5e5b6194a9de880`;
- bounded dispatcher `z2.java` SHA-256 `95028732e2c583a3b079df6234f49926006de474ad143ab4c5cb71606eb78ba3`;
- bounded response decoder `z1.java` SHA-256 `bd86690acfad39498dd16ac6e62839dbea05bbda62e8ef3d3c68c02ea3a2abd5`;
- reconstructed database SHA-256 `073fd4c46c438d4591e590d9fc2556bc5da3c1aff2e8008c504a9ef1f0398be5`;
- bounded jobs `20260828T014732Z-bd147279`,
  `20260828T015125Z-b11928c5`, `20260828T015531Z-45ee7f49`,
  `20260828T015531Z-f739868b`, and `20260828T015610Z-af8f7379`.

### B-CAN no-session odometer check

At 2026-08-28T01:37Z, the broker's reviewed parked B-CAN wake returned a
verified `0x46C` value of 12.64 V. One subsequent physical request to the
verified ICS endpoint sent exactly `22 20 01` without DiagnosticSessionControl
or TesterPresent. It received no response and was not retried. The active route
restored B-CAN exactly to 125 kbit/s, classical listen-only, ONE-SHOT off,
`restart-ms 0`, ERROR-ACTIVE, with zero error/drop counters. The interface TX
delta across wake plus read was exactly 76 frames: 75 fixed wake frames and one
diagnostic request.

This result rejects `2001` as an unattended parked poll. It does not establish
whether the fully powered ICS answers the same no-session read with ignition or
engine running. That one-request drive-state check remains warranted; adding a
session change to make the value respond is not warranted for routine telemetry.

## Polling admission plan

A B-CAN or CAN-CH poll target enters the broker only after all of the following:

1. exact installed ECU/subtype and physical endpoint;
2. one fixed physical `22 DID`, with no session-control or TesterPresent
   requirement;
3. independently supported label and units;
4. variation across a controlled parked action or synchronized drive;
5. fixed decode tested on an independent event/drive leg;
6. per-role exclusive scheduling, response-before-next-request serialization,
   independent failure containment, and exact passive restoration; and
7. raw three-bus coverage proving request/response provenance and no drops.

The next high-value evidence campaign is a read-only AlfaOBD Plots catalog for
the exact installed EPS and ABS profiles, followed by a synchronized drive with
only steering angle, four wheel speeds, brake pressure, yaw, and lateral
acceleration selected. The Android tablet was not connected during this
analysis, so live catalog inventory was not attempted. Static APK/catalog and
OEM research alone cannot safely invent the missing DID associations.

A repository `apk-decompile --no-res` worker job
(`20260828T012242Z-d446fbb3`) was also attempted against the retained
owner-supplied AlfaOBD APK. It failed closed after JADX output exceeded the
worker's total-result limit; no partial archive was returned and no decompile
was run on vanpi. Continuing that path requires owner approval for a new
bounded compute task that decompiles privately and returns only selected
classes. Owner authorization was subsequently received; fixed single-class
tasks were added and produced the ABS map above. The full-tree task remains a
known inappropriate path for this APK.

## Three-bus recorder deployment

`projects/vehicle_data/drive_recorder.py` now creates separate `c-can/`,
`b-can/`, and `can-ch/` compressed streams under one campaign. C-CAN remains a
receive-only companion to the broker's exclusive active-drive owner. B-CAN and
CAN-CH hold independent shared passive role/channel leases. Admission requires
`0x46C` on B-CAN and unique CAN-CH signature `0x0DA` within five seconds.
Identity change, loss accounting, storage-floor failure, or failure of either
secondary recorder fails the complete set. C-CAN `0x2EF` disappearance remains
the common end boundary.

The updated enabled service was restarted asleep on 2026-08-27 MDT and reached
`waiting for reviewed broker active-drive ownership` without opening CAN
sockets, changing a link, or transmitting. The three-role behavior still
requires validation on the next real drive. Offline verification passed 1,067
tests, 4 skips, and 698 subtests (`20260828T013115Z-705fa5e0`).
