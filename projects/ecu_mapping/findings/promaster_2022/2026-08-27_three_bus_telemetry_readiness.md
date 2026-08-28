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
limited to reviewed C-CAN targets until a B-CAN candidate passes the same
label, scaling, session, variation, and restoration gates; CAN-CH is
passive-only under the revised boundary below.

On 2026-08-28 the owner reported that connecting AlfaOBD to the ABS module
causes the IPC to illuminate multiple warning indicators. The exact cause is
unresolved: it could be session behavior, altered/paused normal messaging,
communication DTCs, or another ABS-specific diagnostic response, and is not by
itself proof of a security mechanism. The observable vehicle effect is enough
to change policy. ABS, EPS, HALF, and ORC are now excluded from moving-vehicle
active telemetry development; ordinary CAN-CH telemetry is passive-only.

The passive/static analysis sent no CAN frame. Saved logs, prior read-only
inventories, the reconstructed AlfaOBD catalog, the local OEM corpus, and
read-only live service/interface status were used. The separately documented
bounded B-CAN odometer support check later sent the fixed wake plus exactly one
physical read and restored passive state.

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
| ICS or EMCM2 `2001` | ECU-local odometer/lifetime candidate (`u24be × 0.1 km`) | label/scale recur across current status sequences and ICS returned `62 20 01 0D 0F E8` while fully powered, but its 53,191.860 mi decode was 11.140 mi below the simultaneous 53,203 mi dash display; direct `vehicle.odometer` identity is rejected pending an offset/update relationship |
| Uconnect `1823` | infotainment ECU temperature | label association exact; Uconnect returned `62 18 23 49` in the ignition-on/default-session support check below and the owner observed no visible side effect; `raw − 40 °C` now spans two observed values but still lacks independent temperature ground truth and remains candidate-only |
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
static pass. Following the owner's ABS warning-light observation, the prepared
active support pass is withdrawn. These DIDs remain offline correlation
references only; CAN-CH fields should be mapped from passive captures and
controlled physical references without connecting a diagnostic session.

The former exact no-session support pass was dry-run validated but is retained
below only as withdrawn provenance; do not execute it:

```bash
python3 tools/did_sweep.py abs_canch \
  --did 1002 --did 1004 \
  --did 0880 --did 0881 --did 0882 --did 0883 \
  --did 0884 --did 0885 --did 0886 --did 0887 --did 0888 \
  --pair 12/13 \
  --conditions "parked; ignition ON; engine OFF; exact installed ABS candidate support pass"
```

Dry run selected exactly eleven physical `22` reads and sent no `10` or `3E`.
The new passive-only CAN-CH policy supersedes the planned live follow-up.

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

### B-CAN ignition-on no-session support check

At 2026-08-28T19:27Z, with the van parked, ignition on, and engine off, broker
status independently reported fresh qualified C-CAN `0x0FC` at 0 rpm. The
serial-resolved B-CAN role was Board A CAN2 on DLC pins 3/11, 125 kbit/s
classical CAN, listen-only, `restart-ms 0`, ONE-SHOT off, ERROR-ACTIVE, with no
operation inhibit. The broker and automatic three-bus recorder remained active;
the standalone TPMS and legacy B-CAN recorders were disabled/inactive.

Two separately armed one-request `did_sweep.py` runs then produced exact
positive responses:

| module | physical request | exact response | candidate decode |
|---|---|---|---|
| Uconnect `0x87` | `22 18 23` | `62 18 23 49` in 8 ms | 33 °C / 91.4 °F using `raw − 40 °C` |
| ICS `0x85` | `22 20 01` | `62 20 01 0D 0F E8` in 7 ms | 85,604.0 km / 53,191.860 mi using `u24be × 0.1 km` |

Neither run sent DiagnosticSessionControl, TesterPresent, a retry, a wake
burst, or CAN-CH traffic. Each closed its transport and independently restored
the exact passive B-CAN state before the next run. The B-CAN TX counter rose by
exactly two packets across the campaign, all three vehicle roles ended
listen-only and ERROR-ACTIVE with zero error/drop counters, no inhibit was set,
and the telemetry/recorder services retained zero restarts. At 2026-08-28T19:30Z
the owner reported no warning light, radio interruption, center-stack anomaly,
or other visible effect. This establishes no-session-change support and the
parked owner-observation gate for both fixed reads while ignition is on. It does
not establish their suitability while the network is asleep, the Uconnect
temperature's absolute scale, or odometer variation.

The owner's simultaneous instrument-cluster reference was 53,203 mi. ICS
`2001` decoded to 53,191.859540 mi, a 11.140460 mi / 17.928832 km deficit—about
179.3 raw tenths-of-a-kilometre counts. That is far beyond display rounding and
rejects direct publication as `vehicle.odometer`. Plausible unresolved models
include a delayed/key-cycle-synchronized ICS copy, a fixed or changing module
offset, or a differently defined lifetime counter. A repeat after a key cycle
and another after known distance accumulation can distinguish those models;
the formula remains a candidate decode of the ICS-local value, not the van's
authoritative mileage.

The owner elected to omit Uconnect `1823` from development because its
infotainment-board temperature has low vehicle-health value and unresolved
absolute scaling. It remains historical evidence only.

### Fixed B-CAN helper implementation

The promoted implementation exposes the ICS value as candidate-quality
`vehicle.odometer`, displayed as `ODOMETER*` with the 11.140-mile discrepancy
disclosed. `projects/vehicle_data/bcan_auxiliary.py` has one raw-CAN transmit
body, `18DA85F1#03222001`, and accepts only the exact single-frame
`62 20 01` echo with three data bytes (plus an optional zero CAN-padding byte).
It cannot select another ECU, DID, payload, cadence, session, TesterPresent, or
functional address and cannot send FlowControl.

The broker starts it only beside a qualified engine-running C-CAN epoch, but
supervises and restores B-CAN independently. Its five-second cadence is
response-before-next-request serialized. Non-restoration failures suppress the
candidate for the rest of that engine epoch without stopping C-CAN metrics;
unverified restoration sets a persistent wildcard inhibit. Broker status
attributes the armed B-CAN role to `auxiliary_drive`, and the synchronized raw
recorder consumes that exact ownership evidence instead of taking a conflicting
passive B-CAN lease. This preserves three-bus capture of the request/response
traffic needed to resolve the counter relationship.

Commit `d14d90a` was pushed and deployed while the vehicle was asleep on
2026-08-28. Broker status exposed the enabled-but-idle auxiliary owner and the
candidate metric catalog; both broker and recorder remained healthy, every
role stayed exact passive/error-free, and the LAN dashboard served the starred
card. No deployment-time CAN frame was sent, so the production raw transport
and armed-B-CAN recorder companion remain pending live engine-running evidence.

## Polling admission plan

A B-CAN poll target enters the broker only after all of the following:

1. exact installed ECU/subtype and physical endpoint;
2. one fixed physical `22 DID`, with no session-control or TesterPresent
   requirement;
3. independently supported label and units;
4. variation across a controlled parked action or synchronized drive;
5. fixed decode tested on an independent event/drive leg;
6. per-role exclusive scheduling, response-before-next-request serialization,
   independent failure containment, and exact passive restoration; and
7. raw three-bus coverage proving request/response provenance and no drops.

CAN-CH has no ordinary active-poll admission path. Its ABS/EPS/HALF/ORC
traffic remains available to the synchronized raw recorder for passive field
mapping.

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
