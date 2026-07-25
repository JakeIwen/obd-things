# AlfaOBD 2.4.4.0 APK catalog extraction — 2026-07-21

## Outcome

The owner-authorized copy of AlfaOBD installed on the Android tablet contains a split SQLite
catalog. Reassembling its 51 `assets/alfaobd.db.NNN` members recovered a valid 52,099,072-byte
database. This is not an ODX/PDX package, but it supplies a useful vendor-derived search prior:
selectable ECU profiles and addresses, raw per-request field layouts/scaling metadata, enumeration
references, and diagnostic-menu labels.

The recovered catalog is **not vehicle ground truth**. A model menu can include mutually exclusive
engine/module profiles, and the database does not prove that an ECU is installed, awake, routed on
the connected bus, or compatible with a particular session. Candidates stay out of
`lib/modules.py` until verified on this van.

## Local provenance

| Artifact | Evidence |
|---|---|
| installed package | `com.AlfaOBD.AlfaOBD`, version code 134 / version `2.4.4.0` |
| `base.apk` | 29,125,704 bytes; SHA-256 `97b0f100280453b134ceffc09025f2c443adb383ad4382afcd0b0fd7a9a853b9` |
| reconstructed database | 52,099,072 bytes; SHA-256 `073fd4c46c438d4591e590d9fc2556bc5da3c1aff2e8008c504a9ef1f0398be5` |
| database self-version | `ver.version_code=134`, `ver.version_name=2.4.4.0` |
| validation | SQLite header present and `PRAGMA integrity_check` returned `ok` |

Raw APK/database copies remain gitignored under `tmp/ecu_mapping/android_tablet/` and must not be
redistributed. `tools/alfaobd_apk_db.py` makes the reconstruction reproducible from an
owner-supplied APK without embedding proprietary data in the repository.

The currently selected AlfaOBD UI model was `RAM PRO MASTER (VF) 2022+`. Its database association
is model code `88`: the 26 `ECUList` rows whose comma-delimited `Dodge_RAM` field contains `88`
match the module choices displayed for that model. Treat that linkage as strong local application
evidence, not an OEM definition.

## 2022+ ProMaster profile/address candidates

The table below summarizes model-code-88 choices. A 29-bit target means the `ECUUnits.ecuaddress`
byte used by AlfaOBD's `18DAxxF1` family; the expected physical response family is
`18DAF1xx`. `PENTASTAR` instead has explicit 11-bit `7E0`/`7E8` IDs. A blank address means this
database join did not expose one for that profile variant.

| target | selectable profile(s) | status on this van |
|---:|---|---|
| `7E0` / `7E8` | `PENTASTAR` | catalog alternative; the current AlfaOBD trace and live legacy probe instead identify PCM at 0x10 |
| `0x10` | two `EDC17CF5_CAN` diesel variants; `TIGERSHARK_CUSW` | 0x10 verified live; current trace selected `TIGERSHARK_CUSW` |
| `0x18` | `AUTO_SHIFT` | verified live TCM address; profile label is generic/mismatched to the installed 948TE |
| `0x1F` | `ESM` | verified live shifter address |
| `0x26` | `PAM2` | unverified candidate |
| `0x28` | `ABS9_CAN` | verified live on CAN-CH/grey 2026-07-25; `F1A5=0006501520`, `F187=68516283AD` |
| `0x2A` | `ADAPTIVE_CRUISE` | verified live |
| `0x30` | `ESTEER_DELPHI_CAN` | verified live EPS on CAN-CH/grey 2026-07-25; `F1A5=0002507919`, `F187=68509191AD` |
| `0x31` | `HALF_DUCATO` | verified live CAN-CH/grey 2026-07-25; `F1A5=001E502920`, `F187=68567254AA` |
| `0x40` | `BCDELPHI` | verified live |
| `0x4A` | `TRAILER_TOW` | F1A5 and F187 timed out on B-CAN 2026-07-21; unresolved/possibly absent |
| `0x60` | `MARELLI_DASH_EP` | verified live |
| `0x62`, `0x65` | `LBSS_FGA`, `RBSS_FGA` | F1A5 and F187 timed out on B-CAN 2026-07-21; unresolved/possibly absent |
| `0x6A` | `DCSD` | F1A5 and F187 timed out on B-CAN 2026-07-21; unresolved/possibly absent |
| `0x85` | `ICS_FGA` | verified live B-CAN ICS endpoint; `F1A5=0032701720`, `F187=7DN08LXFAB` |
| `0x87` | `UCONNECT` | verified live B-CAN Uconnect endpoint; `F1A5=0024701A19`, `F187=60986318` |
| `0x98` | `COND_MARELLI_EP` | verified live B-CAN HVAC endpoint; `F1A5=000A702520`, `F187=68516124AE` |
| `0xA0` | `PARK_BOSCH_EP` | unverified candidate |
| `0xC0` | `BOSCH_EP` airbag | verified live ORC on CAN-CH/grey 2026-07-25; `F1A5=001A507720`, `F187=68518674AC` |
| `0xC6` | `TBM2` | verified live |
| `0xC7` | `RFH_CUSW` | verified live RF Hub address |
| `0xCB` | `SGW_FGA` | unverified candidate; the physical SGW bypass changes reachability assumptions |
| `0xD9` | `EMCM2` | verified live B-CAN EMCM2 endpoint; `F1A5=0066708320`, `F187=7DN14LXHAF` |

The exact overlap with all eight previously live-verified C-CAN addresses (`0x10`, `0x18`, `0x1F`,
`0x2A`, `0x40`, `0x60`, `0xC6`, and `0xC7`) made the catalog valuable for prioritization. The
subsequent bounded B-CAN pass verified four more catalog-routed addresses (`0x85`, `0x87`, `0x98`,
and `0xD9`) without justifying the remaining optional profiles as present.

## Adapter routing recovered from the live application selector

The database's numeric `ECUList.adapter` values were initially only opaque catalog fields. The
installed application now resolves two of them directly without connecting its OBD interface or
opening a vehicle session:

| selected model-88 profile | catalog adapter | exact selector text | routing consequence |
|---|---:|---|---|
| Climate Control Marelli EP (`0x98`) | `6` | `Use MS-CAN BLUE adapter` | pins 3/11, the van's live-verified 125-kbit/s B-CAN branch |
| Electric Steering DELPHI EP (`0x30`) | `7` | `Use C CAN 2 GREY adapter` | pins 12/13, the second high-speed/CAN-CH branch |

The gitignored UI evidence is retained as XML and screenshots under
`tmp/ecu_mapping/android_tablet/alfaobd_adapter{6_blue,7_grey}_ui.*`. The XML SHA-256 values are
`ad871a6210fd7efb9eb1f9a8173b1a26da19a25b8d34ad2f94f563decb54b8af` (blue) and
`05d5aa1a7d2a9207910bc3d1e844eb52c0c83d46c6cd2488b069bc98929fcb32` (grey).
This matches [AlfaOBD's current public hardware guide](https://www.alfaobd.com/) (accessed
2026-07-21): blue remaps pins 3/11 to an interface's CAN pins, while grey remaps pins 12/13. Its
[supported-vehicle table](https://www.alfaobd.com/supported_cars.html) separately specifies the
grey-adapter setup for `RAM PRO MASTER (VF) 2022+`.

That resolves the model-code-88 candidates into useful physical-bus groups:

| adapter | branch | model-88 29-bit targets | present status |
|---:|---|---|---|
| `0` | ordinary C-CAN/profile connection | `10,18,1F,2A,40,60,C6,C7,CB` | first eight verified; SGW `CB` unresolved behind the bypass |
| `6` | B-CAN / MS-CAN BLUE | `4A,62,65,6A,85,87,98,D9` | `85,87,98,D9` verified; `4A,62,65,6A` timed out twice and remain unresolved |
| `7` | C-CAN2 / GREY / CAN-CH | `26,28,30,31,A0,C0` | `28,30,31,C0` verified at 500 kbit/s on pins 12/13; optional/configured-absent `26,A0` remain unverified |

This explains why adapter-6/7 candidates timed out during exhaustive pins-6/14 address scans: the
scan covered their address bytes but not their catalog-selected physical branches. The bounded
`tools/ecu_discover.py --profile promaster88-bcan` plan subsequently captured exact responses from
four adapter-6 endpoints on pins 3/11; only those four entered `lib/modules.py`. The other four
timeouts remain unresolved because they may be optional or state-dependent. Live mode retains the
separate `--confirm-catalog-candidates` gate plus the normal parked/pair/conditions checks and
passive restore.

### Why the candidate profile starts with F1A5

All eight adapter-6 model rows use catalog initialization type `5`, and each row's Device ID has one
or more entries in the database's `isocodes` table. The table does not itself label those values as
F1A5. Before the live pass, the fact that AlfaOBD read `F1A5` and selected exact `isocodes` subtypes
on all seven default-session C-CAN endpoints made the values below evidence-backed B-CAN candidates.
The last column now records the subsequent independent 2026-07-21 B-CAN result:

| target | model-88 profile | Device ID / unit ID | catalog isocode / inferred F1A5 candidate(s) | live result |
|---:|---|---:|---|---|
| `0x4A` | `TRAILER_TOW` | 7193 / 408 | `E607040DD3`, `E607830D52` | F1A5 and F187 timeout; unresolved |
| `0x62` | `LBSS_FGA` | 8670 / 433 | `0033071819`, `0033409317`, `003350B214`, `0033701819`, `0033706220`, `0033706B20` | F1A5 and F187 timeout; unresolved |
| `0x65` | `RBSS_FGA` | 8671 / 434 | `0034409417`, `003450B114`, `0034701919`, `0034706320`, `0034706C20` | F1A5 and F187 timeout; unresolved |
| `0x6A` | `DCSD` | 55885 / 452 | `0083701223`, `0083701C19`, `0083709E20`, `008370B720`, `008370C020` | F1A5 and F187 timeout; unresolved |
| `0x85` | `ICS_FGA` | 55930 / 272 | `0032701720`, `0032707D19` | exact `0032701720`; verified |
| `0x87` | `UCONNECT` | 6052 / 410 | `0024401614`, `0024402814`, `0024406014`, `0024506D17`, `0024702F18`, `0024704E17`, `0024705515`, `B5831A0B32`, `B583980BB0` | `0024701A19`; not Device 6052, but exact APK UCONNECT Device 8931 match at `0x87` |
| `0x98` | `COND_MARELLI_EP_6079` | 6082 / 131 | `3483290BXX` (catalog wildcard pattern) | `000A702520`; no exact match, same `000A70` family as other climate variants |
| `0xD9` | `EMCM2` | 54749 / 451 | `0066405820`, `0066505919`, `0066700319`, `0066708320` | exact `0066708320`; verified |

The cumulative historical tablet trace also contains positive F1A5 responses at `0x87` and `0x98`,
but that mixed old-vehicle source remains reference only. Current-van endpoint proof instead comes
from the independent physical F1A5 and F187 responses recorded in
[`2026-07-21_bcan_live_ecu_discovery.md`](2026-07-21_bcan_live_ecu_discovery.md).

## Current subtype identification from live F1A5 values

Unlike a model-menu row, this join starts with each ECU's `F1A5` value read from the current van.
Exact matches in the database's `isocodes` table select the installed AlfaOBD subtype, and the
resulting addresses agree with independent live discovery:

| live ECU | current `F1A5` data | catalog Device ID | catalog/address consequence |
|---|---|---:|---|
| radar | `00 39 50 16 20` | 8905 | `ADAPTIVE_CRUISE`, 0x2A |
| RF Hub | `00 41 50 26 20` | 8887 | alias fallback `RFH_CUSW`, 0xC7 |
| BCM | `00 00 60 77 19` | 55851 | `BCDELPHI`, 0x40 |
| TCM | `52 85 04 0D 3D` | 8962 | ZF9HP variant at 0x18; supersedes generic model-menu `AUTO_SHIFT` Device ID 6253 for data lookup |
| shifter | `00 16 50 7A 19` | 55982 | alias fallback `ESM`, 0x1F |
| cluster | `00 03 50 74 20` | 8801 | installed subtype at 0x60; supersedes generic model-menu Device ID 6812 for data lookup |
| telematics | `00 23 50 69 20` | 55732 | `TBM2`, 0xC6 |
| ICS | `00 32 70 17 20` | 55930 | exact `ICS_FGA`, 0x85; B-CAN verified |
| Uconnect | `00 24 70 1A 19` | 8931 | exact global `UCONNECT`, 0x87; supersedes model-88 Device 6052 for subtype lookup |
| Climate | `00 0A 70 25 20` | no exact match | model-88 `COND_MARELLI_EP` routing and family prefix still support 0x98; B-CAN verified independently |
| EMCM2 | `00 66 70 83 20` | 54749 | exact `EMCM2`, 0xD9; B-CAN verified |

Among the seven original C-CAN rows above, only exact subtype 55851 has direct membership in this
APK's request/routine tables:
`FGA_BCM_DATA`. The other six exact subtypes have identity/isocode rows but no direct request or
routine definitions in the inspected tables; their data is stored elsewhere, encoded, or implemented
in application code. Model-menu PCM Device ID 6829 exposes only generic 11-bit `7E0`/`7E8` metadata
and has neither an isocode nor a request-table row. It does not describe the current trace's verified
legacy internal PCM endpoint at 0x10.

## Climate RID 0201: profile start payload found, label still unresolved

The live B-CAN result-only inventory sent `31 03 02 01` to the verified climate endpoint and
received `71 03 02 01 00 02`. Offline inspection then found a `31 01 02 01` routine-start payload
in the DEX default handler for the model-88-selected `COND_MARELLI_EP_6079` Device 6082 profile.
The SQLite routine table itself contains no Device 6082 row, no `31030201` row, and no `31010201`
row; control-flow inspection also found no `31 03 02 01` decoder reachable from that DEX handler.

This does not establish an exact routine name. The live ECU's `F1A5=000A702520` does not match
Device 6082's `3483290BXX` wildcard, so the start payload describes AlfaOBD's selected model-menu
profile, not proven exact-variant compatibility. The nearby diagnostic-menu entries “Sensor fan
inside vehicle” and “Flap actuators learning test” have no explicit join to RID `0201`; menu order
is not a safe mapping. Treat `31 01 02 01` as an actuation lead only. It was not sent to the vehicle,
and it requires a separate payload review, safe-state plan, and owner authorization before any
future test. The returned result bytes `00 02` remain unresolved.

A later live non-actuating diagnostic observation confirmed the compatibility warning in practice.
AlfaOBD failed model/ISO verification against live `F1A5=000A702520`; continuing only to observe Plots produced an
eight-DID loop with two NRC-`31` reads and six positive responses, but its eight rendered values were
constant, nonsensical, or `NA` for known vehicle state. Those Climate gauge labels and scales are not
valid for the installed ECU. A positive DID response does not repair the subtype mismatch. See the
[`live AlfaOBD status correlation`](2026-07-21_alfaobd_live_status_correlation.md).

## BCM read-data catalog

`FGA_BCM_DATA` associates current BCM `Device_ID=55851` with 75 distinct `22xxxx` requests and
1,569 response-field rows. Rows include bit position/length, scale and offset, units, and enum-table
references. This is a much richer structural source than the historical Gauges CSV because it links
field layouts and raw string references directly to request bytes.

All 75 catalog requests already occur in the current-van AlfaOBD debug trace. Their recorded final
responses comprise 55 positives and 20 negatives (`NRC 22` or `NRC 31`); there is no missing catalog
request that warrants another live scan. Three high-interest positive examples are:

| DID | raw request-name reference | field rows | live status |
|---|---|---:|---|
| `40A3` | `(5189)` | 66 | positive only after validated session `10 03` |
| `40A6` | `(18776)` | 14 | positive only after validated session `10 03` |
| `40AA` | `(18637)` | 232 | positive in the inherited/default state |

The numeric placeholders are **not decoded labels yet**. Treating them as direct one-based lines in
`alfaobd5_en.txt` produces plausible-looking but unproven names; zero-based expansion produces
nonsensical names for known BCM requests. This proves another runtime indirection exists. Raw
placeholder IDs, bit layouts, and numeric scale fields are preserved as evidence, while any expanded
text remains explicitly heuristic until that indirection is reversed. No PROXI, car-configuration,
coding, write, or alignment operation was run during this extraction.

The 20 catalog requests that were negative in the trace are `0140`, `0155`, `0157`, `2940`,
`2947`, `3505`, `A023-A02F`, and `A054`. Those negatives are still condition/session evidence, not
proof the definitions are wrong. Re-test one only when its catalog label supplies a concrete
experimental reason. The existing trace is sufficient for offline work across the `01xx`, `10xx`,
`12xx`, `19xx`, `20xx`, `29xx`, `30xx`, `35xx`, `3xxx`, `40xx`, and `A0xx` groups.

### Offline structural decode outcome

`tools/alfaobd_bcm_decode.py` now applies those definitions to the existing current-van trace and
checkpointed BCM inventories without opening CAN or ADB. It validates each inventory against its
paired summary (`bcm_ccan`, `18DA40F1 -> 18DAF140`, 29-bit normal-fixed) before accepting any row,
and attaches requested session, confirmed session state, conditions, results path, and summary path
to every inventory observation. This prevents an overlapping DID from another ECU or diagnostic
state from silently becoming BCM evidence. For example, `40A3` remains visibly split between
inherited-state `7F 22 31` and positive data from confirmed session `03` campaigns.

The current report contains 75 requests and 540 unique field definitions: 362 enum, 124 numeric,
and 54 raw. Across 67 distinct complete positive response variants it decoded 493 field instances
with zero out-of-bounds fields. The report deliberately surfaces rather than repairs vendor-data
anomalies: `1004` has malformed slope text `0.10.0`, while `1008`, `2008`, and `200B` use ambiguous
32-bit bounds `0..-1`. Human names, unit IDs, and physical scaling remain unverified even when the
catalog arithmetic is mechanically valid. A controlled ground-truth comparison is still required
before promoting any of these fields into a per-ECU DID map.

## Diagnostic-menu labels and the unlock limitation

`Diag_devices` identifies `BCDELPHI55851`; joining its diagnostic menu yields 67 labels. They
include horn, lamps, wipers, front/rear door-lock relay outputs, battery/ignition commands, ECU
reset, and three explicitly configuration-changing entries (`PROXY alignment`, `Car configuration
change`, and `Proxy tools`). This confirms that the correct BCM profile exposes door-lock actions.

However, these menu tables do not directly associate each label with the six captured `2F` IO-control
DIDs (`5040`, `5041`, `5050`, `5115`, `5118`, `5120`). It would be unsafe to infer that mapping
from menu order. A fresh one-action-at-a-time AlfaOBD debug/PCAN capture is still required to label
the lock and unlock payloads. Configuration-changing menu entries remain out of scope unless the
owner explicitly authorizes them later.

## Historical tablet data

The tablet's cumulative `Gauges_Data.csv` contains 254 sections and 89,793 structurally valid
samples, but every section is dated 2022–2024 and belongs to old diesel, six-speed transmission,
or historical TPMS profiles. It has labels/rendered values but no wire DIDs and no 2026/current-van
session. It is a parser fixture and old-vehicle vocabulary source, not current-van evidence.

Two recovered 2022 debug snapshots likewise identify only the prior 2015 diesel VIN. They recover
partial raw provenance for the existing old-van map but add no current-van mappings. These
provenance boundaries prevent old labels or scaling from leaking into the 2022 module namespaces.

## Next evidence-producing work

1. The read-only `tools/alfaobd_catalog.py` export now preserves the model-code-88 ECU rows, exact
   subtype isocodes, BCM definitions, raw placeholders, and source hashes in JSON. Reverse the
   application's extra string-table indirection before treating any placeholder expansion as a label.
2. The structural decode is complete. Spot-check names/units/scaling against controlled vehicle
   state before promoting fields; do not repeat the already complete 75-request set without a new
   question.
3. The guarded adapter-6 B-CAN survey and broad AlfaOBD status observation are complete. Four
   endpoints are verified; four optional profiles timed out again. Do not repeat them without a new
   state/session or installed-equipment question. The selected Climate profile failed live subtype
   verification and its gauge interpretations are invalid for this ECU.
4. With ignition on and PCAN listen-only, run one front-door lock/unlock output action at a time in
   AlfaOBD while recording Debug Data. Do not enter Tools/PROXI or car-configuration menus.
5. For ICS, Uconnect, and EMCM2, use one controlled button/knob/value change against the status-DID
   groups already bounded in the linked live observation; another broad gauge/status pass is not
   needed.
