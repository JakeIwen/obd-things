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
| `0x28` | `ABS9_CAN` | unverified candidate |
| `0x2A` | `ADAPTIVE_CRUISE` | verified live |
| `0x30` | `ESTEER_DELPHI_CAN` | unverified EPS candidate |
| `0x31` | `HALF_DUCATO` | unverified candidate |
| `0x40` | `BCDELPHI` | verified live |
| `0x4A` | `TRAILER_TOW` | unverified candidate |
| `0x60` | `MARELLI_DASH_EP` | verified live |
| `0x62`, `0x65` | `LBSS_FGA`, `RBSS_FGA` | unverified blind-spot candidates |
| `0x6A` | `DCSD` | unverified display candidate |
| `0x85`, `0x87` | `ICS_FGA`, `UCONNECT` | unverified infotainment candidates |
| `0x98` | `COND_MARELLI_EP` | unverified HVAC candidate |
| `0xA0` | `PARK_BOSCH_EP` | unverified candidate |
| `0xC0` | `BOSCH_EP` airbag | unverified candidate |
| `0xC6` | `TBM2` | verified live |
| `0xC7` | `RFH_CUSW` | verified live RF Hub address |
| `0xCB` | `SGW_FGA` | unverified candidate; the physical SGW bypass changes reachability assumptions |
| `0xD9` | `EMCM2` | unverified candidate |

The exact overlap with all eight live-verified addresses (`0x10`, `0x18`, `0x1F`, `0x2A`,
`0x40`, `0x60`, `0xC6`, and `0xC7`) makes the catalog valuable for prioritization. It does not
justify declaring the remaining profiles present. The next ignition-on discovery pass should use
this bounded list with the correct per-profile initialization/session behavior instead of another
full address-space scan.

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
| `6` | B-CAN / MS-CAN BLUE | `4A,62,65,6A,85,87,98,D9` | not yet actively surveyed on B-CAN |
| `7` | C-CAN2 / GREY / CAN-CH | `26,28,30,31,A0,C0` | outside the current C-CAN/B-CAN scope |

This explains why adapter-6/7 candidates timed out during exhaustive pins-6/14 address scans: the
scan covered their address bytes but not their catalog-selected physical branches. It does not prove
the optional modules are installed. `tools/ecu_discover.py --profile promaster88-bcan` now provides
an eight-target, dry-run-by-default B-CAN `22 F187` plan. Live mode requires the separate
`--confirm-catalog-candidates` gate plus the normal parked/pair/conditions checks and passive restore.
No candidate enters `lib/modules.py` before an exact response is captured.

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

Only exact subtype 55851 has direct membership in this APK's request/routine tables:
`FGA_BCM_DATA`. The other six exact subtypes have identity/isocode rows but no direct request or
routine definitions in the inspected tables; their data is stored elsewhere, encoded, or implemented
in application code. Model-menu PCM Device ID 6829 exposes only generic 11-bit `7E0`/`7E8` metadata
and has neither an isocode nor a request-table row. It does not describe the current trace's verified
legacy internal PCM endpoint at 0x10.

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
3. Survey the eight adapter-6 candidates on the verified B-CAN branch with the guarded named profile.
   A negative response proves an endpoint exists; a timeout proves only that this request/session/
   state did not answer. Keep all candidates out of the registry until an exact response is captured.
4. With ignition on and PCAN listen-only, run one front-door lock/unlock output action at a time in
   AlfaOBD while recording Debug Data. Do not enter Tools/PROXI or car-configuration menus.
5. For other modules, record fresh simultaneous Gauges Data and Debug Data in small labeled batches.
   That supplies the timestamp bridge between human labels/scaled values and raw DID responses.
