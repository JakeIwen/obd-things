# Legacy FCA Windows/CDA archive triage — 2026-07-29

## Result

The local archive at `/mnt/EXFAT512/FCA/` is a useful historical candidate
source, but it is not a 2022 ProMaster diagnostic authority.

Its best material is a set of FCA CDA engineering bundles whose internal
databases contain diagnostic requests, labels, bit layouts, conversions,
DTCs, routines, module addressing, and a schema capable of expressing
year/body applicability. The archive also contains an independent September
2022 wiTECH PROXI report for the owner's prior model-year 2015 diesel VF and
guided-diagnostic workflows. Together they provide historical labels for
several exact request overlaps already observed on the 2022 van.

The strongest results are:

- legacy BCM bundles label DID `2023` as a PROXI/system-configuration record
  and `40A2` as EOL data;
- a separate 2022 wiTECH report for the prior 2015 diesel VF places `2023` and
  `40A2` in the same EOL/PROXI configuration domain;
- a legacy RF Hub bundle contains the exact eight TPMS requests used on the
  current van and supplies per-wheel labels; and
- four legacy PCM profiles contain all 14 service-`22` requests already
  observed on the current PCM, corroborating ten changing conversions while
  exposing incompatible throttle-blade and vehicle-speed scales;
- two legacy shifter/GSM profiles supply recurring lifecycle-record candidates
  and an old-enumeration counterexample; and
- legacy cluster and DASM bundles supply candidate layouts and conversions for
  exact current-DID overlaps.

The same evidence also demonstrates why these labels must remain
variant-scoped. One legacy RF Hub profile reverses the two rear sensor-ID
labels relative to the 2022 van's evidence-backed ID-to-pressure-slot map,
and two legacy cluster profiles assign different meanings to DID `0107`.

No live CAN traffic or vehicle actuation was used for this research.

## Scope and handling

The source corpus was inspected statically. No Windows executable, installer,
DLL, guided diagnostic, security file, or vehicle command was executed.
Archives were opened as data and selected files were extracted under the
gitignored `tmp/fca_research/` tree. The source corpus was left unchanged.

Inventory:

| directory | files | apparent bytes | useful content |
|---|---:|---:|---|
| `AllEngs (2)` | 4,231 | 788,592,888 | CDA `.eng` engineering bundles |
| `witech_1` | 31 | 890,245,310 | wiTECH 17.04.27 installer and VCI firmware/update bundles |
| `DRB III` | 105 | 332,980,649 | old DRB emulator, generic scan tool, and one VF PROXI report |
| `guidedDiagnostics` | 421 | 734,814 | workflow properties/JavaScript and opaque `.exml` payloads |
| `micropod_setup` | 3 | 102,928,288 | microPOD setup packages |
| **total** | **4,792** | **2,115,488,097** | about 1.97 GiB apparent size |

The 4,231 `.eng` files have internal ZIP timestamps from 2007-07-29 through
2011-09-02. The FGA subset spans 2010-03-12 through 2011-09-01 and the CUSW
subset spans 2010-09-07 through 2011-08-31. They therefore predate the North
American ProMaster and cannot establish a 2022 layout by themselves. The
top-level filesystem timestamps are copy history and were not used to date
the contents.

The owner previously had a 2015 diesel ProMaster. Treat the 2015-VF report
and any other model-year-specific owner captures as provenance from that
former vehicle unless separately proved otherwise. This is especially
important for PCM and TCM material: their diesel powertrain mappings are
substantially different from the current gasoline van and 948TE. The
2007-2011 CDA bundle collection is a broader engineering archive rather than
evidence that every included profile came from either van.

## PCM overlap

Four 2011 PCM profiles at variants 60/61 use the current PCM endpoint
`18DA10F1`/`18DAF110` and contain all 14 service-`22` requests observed in the
current idle and loaded-drive recordings. They independently agree with the
current layouts and conversions for oil pressure, coolant temperature,
throttle-sensor percent, generator duty, three voltage values, signed torque,
VVT pressure, engine speed, and VVT oil temperature.

This is high-value corroboration, not an exact identity match. The same
profiles use a throttle-blade scale about 20 times the live current value and
a vehicle-speed conversion that is incompatible with the current
Alfa/wire result. Their fuel-level formula is `raw - 100%`, but every current
response was `C8`, which also fits the earlier `raw * 0.5%` candidate. Two
sparse variants at the same request/response endpoint overlap none of the
current targets. These positive counterexamples prevent profile-wide
promotion.

A broader comparison found 167 of 190 unique requests in AlfaOBD's current
PCM Plots catalog, but substantive intake/exhaust cam-label swaps appear among
the overlaps. A separate comparison against existing current-positive
captures found 157 of 187 requests and useful part-number, tank-size, and
redundant-odometer candidates, alongside direct width conflicts. The full
field-by-field comparison, fuel-level ambiguity, negative `21 18`/`21 62`
current responses, profile metadata, and source hashes are recorded in
[`2026-07-30_legacy_pcm_cda_overlap.md`](2026-07-30_legacy_pcm_cda_overlap.md).

## BCM PROXI/EOL corroboration

Two independent source types converge on the broad meaning of the current BCM
DIDs:

| source | historical request/record | source label | current-van relationship |
|---|---|---|---|
| `BCM-BCM_CUSW-60-01-002.eng` | `22 2023`, `2E 2023` | `System Configuration (PROXI)` | current BCM returns a 250-byte `2023` readback matching the latest captured `2E 2023` payload |
| `BCM-BCM_FGA-4083EC8A-016.eng` | `22 2023`, `2E 2023` | `Detailed System Configuration (PROXI)` | same current-vehicle support; field layout remains unresolved |
| both legacy BCM profiles | `22 40A2` | `EoL data` | current BCM returns an 80-byte structured `40A2` record |
| legacy FGA BCM profile | `22 40A3` | `Snapshot Data IMMO` | current BCM supports a dynamic 14-byte value in session `03` |
| legacy FGA BCM profile | `22 40A6` | `Snapshot Date Logistic Mode` | current BCM supports a 16-byte value in session `03` |
| September 2022 wiTECH report for the prior 2015 diesel VF | RDI `2023` | `EOL Data` | same 250-byte record length as the current BCM |
| same report for the 2015 diesel VF | RDI `40A2` | `EOL configuration Table` | the report renders five eight-byte rows, while the current BCM returns 80 bytes |

Both decoded legacy BCM profiles use the current BCM diagnostic address tuple:
request `18DA40F1`, response `18DAF140`, and functional address
`18DBFEF1`. Their variant identities and layouts are nevertheless old and
different:

| record | decoded legacy payload | current/live payload | implication |
|---|---:|---:|---|
| CUSW `2023` | 255 bytes, 526 named fields | 250 bytes | do not apply the old field offsets |
| FGA `2023` | 85 bytes, 338 named fields | 250 bytes | different generation/layout |
| CUSW `40A2` | 80 bytes, 640 one-bit fields | 80 bytes | exact length and strong structural candidate |
| FGA `40A2` | 40 bytes, 200 one-bit plus 15 byte fields | 2015 report renders 40 bytes | historically consistent with the prior-van report |
| FGA `40A3` | 12 bytes | 14 bytes | label is useful; layout is not transferable |
| FGA `40A6` | 8 bytes | 16 bytes | label is useful; layout is not transferable |

For both profiles, `2E 2023` is an active write definition whose request-side
fields mirror the corresponding `22 2023` response layout. Its decoded
presence is research evidence, not a command to replay.

The old 80-byte CUSW `40A2` definition divides exactly into five 128-bit node
maps:

| current payload offset, if the old structure carries forward | legacy map meaning |
|---|---|
| `0x00-0x0F` | node absent/present |
| `0x10-0x1F` | node has-not/has EOL |
| `0x20-0x2F` | node inactive/active |
| `0x30-0x3F` | EOL not-required/required |
| `0x40-0x4F` | EOL not-OK/OK |

The current `40A2` value is five byte-identical 16-byte blocks, and the prior
VF report independently renders five shorter node maps. That convergence
makes the five-block interpretation a strong historical structural candidate
for the current record. Individual current node assignments remain unresolved:
several current set bits land on plausible old labels such as BCM, ECM, EPS,
IPC, TCM, GSM, ORC, and RFHM, while other set bits were reserved or unnamed
in 2011. The node-name table must not be promoted wholesale.

The report for the prior 2015 diesel vehicle says all control units were correctly
configured. Its filename
contains the prior vehicle's full VIN, so tracked material identifies it only
as `/mnt/EXFAT512/FCA/DRB III/<timestamp>_PROXI_<VIN>.html`. Neither its VIN
nor its raw vehicle-specific configuration payload belongs in tracked output.

This is strong historical vendor corroboration for the broad roles of `2023`
and `40A2`. It does not decode a 2022 field or prove that the 2011/2015 and
2022 record layouts are interchangeable. The current-vehicle evidence and
session behavior remain canonical in
[`2026-07-21_candidate_did_inventory.md`](2026-07-21_candidate_did_inventory.md).

## RF Hub TPMS overlap and built-in counterexample

`RFH-RFHM_CUSW-60-02-001.eng` contains each current TPMS request exactly once.
The diagnostic request rows mechanically join to the shared RFH English label
dictionary. Standard identity anchors in the same rows provide a useful
control: `22 F190` joins to `VIN`, `22 F187` to
`VehicleManufacturerSparePartNumber`, and `22 F1A5` to `ISOCode`.

The decoded historical profile uses the same diagnostic CAN address tuple as
the current RFH: request `18DAC7F1`, response `18DAF1C7`, and functional
address `18DBFEF1`. Its manifest is dated 2011-08-12 and identifies variant
`60-02` under the `RFHM_CUSW` family; address equality is not an exact
hardware/software-identity match.

The legacy and current mappings compare as follows:

| DID | legacy CDA label | 2022 live map | assessment |
|---|---|---|---|
| `31CB` | Front Left Wheel Sensor ID | front left ID | agrees |
| `31CC` | Front Right Wheel Sensor ID | front right ID | agrees |
| `31CD` | Rear Left Wheel Sensor ID | **rear right ID** | legacy rear label conflicts |
| `31CE` | Rear Right Wheel Sensor ID | **rear left ID** | legacy rear label conflicts |
| `31D0` | Tire 1 (Left Front) Altitude Compensated Pressure | front left pressure | agrees |
| `31D1` | Tire 2 (Right Front) Altitude Compensated Pressure | front right pressure | agrees |
| `31D2` | Tire 3 (Right Rear) Altitude Compensated Pressure | rear right pressure | agrees |
| `31D3` | Tire 4 (Left Rear) Altitude Compensated Pressure | rear left pressure | agrees |

The fully parsed message rows define the four IDs as direct 32-bit fields and
the four pressures as unsigned 16-bit fields starting immediately after the
three-byte positive-response echo. All four pressure rows use the same linear
conversion: mask `0xFFFF`, slope `0.0145`, offset zero, and unit `psi`. The
current van's independently established `0.1 kPa/raw` scale is
`0.0145038 psi/raw`, so the historical conversion agrees to the database's
four-place precision.

The 2022 map wins. Lowering and raising each physical tire established the
pressure-DID-to-wheel positions. The separate ID-to-pressure-slot association
is supported by current fault records, `40Ax` records while they were
available, and an independent AlfaOBD capture. The old rear-ID disagreement
may be a profile difference, a later layout change, or a historical
source-label defect. It must not be used to change the current service map.

The phrase `Altitude Compensated Pressure` is a useful semantic candidate for
the current pressure values, not yet a current-vehicle fact. The archive
independently corroborates the current scale, but the current scale remains a
live-vehicle fact because it was verified against physical pressure changes
and the cluster display.

## Cluster overlap and DID-namespace warning

`IPC-IPC_FGA-3283CE8A-006.eng` uses the current cluster address tuple and
defines several requests that are positive on the current 2022 cluster:

| DID | legacy FGA definition | current assessment |
|---|---|---|
| `1000` | Engine Speed, unsigned 16-bit `raw * 0.25 rpm` | independently matches current `/4 rpm` |
| `1002` | Vehicle Speed, unsigned 16-bit `raw / 128 km/h` | useful scale candidate |
| `0107` | Gear in use indication: `0=P`, `1=R`, `2=N`, `3=D`, `17-22=1st-6th` | PRND candidate only; old six-speed gears do not describe the current nine-speed |
| `1004` | Power supply, unsigned 16-bit `raw * 0.001 V` | conflicts with the current observed layout; reject |
| `1005` | External Temperature, unsigned 8-bit `raw * 0.5 - 40 °C` | useful current-scale candidate |

This is useful candidate evidence because the current live work already
anchors several of these meanings. It is not safe to promote the whole
profile: `IPC-IPC_CUSW-61-00-001.eng` assigns `0107` to
`Telltale - Park Lamp`. The collision is direct evidence that a DID number is
not a global signal name, even between historical profiles of the same broad
module class.

## DASM/radar overlap

`ACC-DASM_CUSW-60-00-000.eng` is dated 2011-07-21 and uses the current DASM
address tuple `18DA2AF1`/`18DAF12A`/`18DBFEF1`. It shares 29 current-positive
DIDs; 18 are standard identity/maintenance material. The non-identity
candidate definitions are:

| DID | legacy definition |
|---|---|
| `1008` | ECU timestamp in RAM, unsigned 32-bit minutes |
| `1009` | timestamp since key-on in RAM, unsigned 16-bit `raw * 15 sec` |
| `2001` | odometer, unsigned 24-bit `raw * 0.1 km` |
| `2002` | odometer at last flash update, unsigned 24-bit `raw * 0.1 km` |
| `2003` | flash rewrite count, unsigned 8-bit |
| `2008` | ECU timestamp in EEPROM, unsigned 32-bit minutes |
| `2009` | timestamp since key-on in EEPROM, unsigned 16-bit `raw * 15 sec` |
| `200A` | key-on counter, unsigned 16-bit |
| `200B` | ECU time at first DTC, unsigned 32-bit minutes |
| `200C` | key-on time at first DTC, unsigned 16-bit `raw * 15 sec` |
| `2010` | programming status, 32 bitfields |

These layouts follow recurring FCA environmental/maintenance conventions and
are useful low-confidence historical candidates for already captured DASM
data, not a current radar decoder. The current radar's
`084x`/`085x`/`086x` calibration family and targeted runtime DIDs are absent
from this old bundle.

## Shifter/GSM and identity-only module results

Two legacy GSM profiles share 32 of 48 current-positive shifter requests.
Ten lifecycle records have matching current widths and recurring FCA
direct-minute, 15-second, odometer, and counter conversions. Current DID
`F158` demonstrates the limit: the old byte layout produces model year 2022
and country USA, but its old body-style table decodes the ProMaster as a
Convertible.

Selected ABS, EPS, and ORC profiles use the current physical endpoints but
overlap almost entirely in identity records. Direct width and variant-layout
conflicts prevent any runtime-signal promotion. The exact comparison and
source hashes are in
[`2026-07-30_legacy_module_cda_overlap.md`](2026-07-30_legacy_module_cda_overlap.md).

## 2022 OEM service-corpus boundary

A separate read-only search of the exact-model 2022 OEM service corpus
corroborates the broad domains but not the recovered wire layouts:

- BCM operation and PROXI DTC material says the BCM stores and compares the
  vehicle configuration against expected/present/active ECU membership;
- RF Hub DTC `B1051-55` and ACC DTC `C140A-55` place those modules in the
  PROXI/configuration population; and
- TPMS operation material says the RF Hub stores four unique sensor IDs by
  left-front, right-front, left-rear, and right-rear position.

The corpus searches found no `2023`/`40A2` payload definition, current node-bit
assignment, `31CB-31D3` DID table, `Altitude Compensated Pressure` label, rear
ID ordering, or DASM lifecycle decode. The OEM material therefore upgrades
the broad system behavior only; it does not make a 2011 CDA field layout
authoritative for 2022.

The bounded corpus-search jobs were
`20260730T042331Z-2a1782a2` (BCM operation),
`20260730T042332Z-5c8bb2ee` (PROXI DTC semantics),
`20260730T042508Z-58f1629a` (RF Hub),
`20260730T042509Z-d09cb429` (ACC), and
`20260730T042403Z-b7edcc0f` (TPMS).

## Negative applicability and identity bounds

- Five selected `TCM_CUSW` variants contain none of the current
  ZF9HP-targeted requests. A selected FGA profile contains only `F40C` but
  assigns an incompatible emissions-PID meaning. This is a namespace
  collision, not a 948TE mapping. Absence in these old profiles says nothing
  about support on the current TCM. Given the owner's former 2015 diesel, old
  PCM/TCM material must stay in that vehicle/powertrain scope.
- All 20 selected databases declare `ROUTINE_TO_YB`, but the table is empty in
  every one; no decoded VF body applicability row was found.
- Exact searches across 221 extracted members for 24 current ECU identity
  strings returned zero hits. Address and DID continuity do not establish
  exact current ECU identity.

## What the `.eng` files contain

Every sampled `.eng` file is an outer ZIP containing:

- a module/variant `.bndl` ZIP;
- a shared `Module-Label.properties.bndl`; and
- sometimes security and routine bundles.

The core bundle contains an HSQLDB-style `db.script`, `db.data`,
`db.properties`, and manifest. The exposed schema includes:

- ECU and request/response/broadcast bus addressing;
- variant/version and security-level association;
- diagnostic request strings;
- signal bit positions, lengths, names, and recordability;
- linear conversion slope, offset, unit, precision, and bounds;
- table and string encodings;
- DTC sets and environmental records; and
- routines plus tables capable of linking them to variants and year/body
  applicability. Those applicability tables are empty in the 20 selected
  databases discussed below.

The database properties identify HSQLDB `1.8.0` and a nonstandard
`hsqldb.cache_version=1.8.0.x`. This is a real FCA storage variant, not a
property typo. Comparison with the
[official HSQLDB 1.8.0.10 source release](https://sourceforge.net/projects/hsqldb/files/hsqldb/hsqldb_1_8_0/hsqldb_1_8_0_10.zip/download)
shows that stock HSQLDB 1.8.0.7 and 1.8.0.10 expect a 16-byte `DiskNode` for
each row index. FCA's field records omit those nodes and place separate index
records ahead of contiguous node-less field rows. Merely changing the version
property makes stock HSQLDB start at the wrong bytes and return garbage.

The reusable read-only
[`tools/fca_hsql_decode.py`](../../../../tools/fca_hsql_decode.py)
derives schemas and key constraints from each matching `db.script`, accepts
only the unmodified FCA `1.8.0.x` cache marker, and emits JSON to standard
output. It established the structure on `TCM-TCM-08-72-003`:

- 32-byte file header;
- 99 aligned FCA index records;
- every `SET TABLE` root multiplied by the declared scale lands on an index
  record whose next big-endian integer is the exact row count;
- 8,421 contiguous field rows follow in table order; and
- all 8,421 rows decode with zero-only alignment padding.

The decoded database passed 15 primary-key uniqueness checks and seven
explicit foreign-key checks with no violation. As a semantic control, one
decoded command joined `21 62` to `Speed Data 62TE` and four 16-bit
engine/turbine/output/transfer-speed fields with a linear `1 rpm/bit`
conversion. That 2008 62TE profile is not applicable to the current 948TE,
but it validates the extraction method.

The schema-derived parser then decoded the RFH candidate independently:
83 index records plus 6,025 table rows, with all rows consumed and 13
primary-key and seven foreign-key checks passing. The TPMS widths, labels,
conversion, and bus addresses above come from those validated joins rather
than string proximity.

Its
[`synthetic unit tests`](../../../../tests/test_fca_hsql_decode.py)
contain no FCA data and cover schema/key parsing, row types, modified UTF-8,
read-only behavior, validation failures, non-finite numeric rejection,
label/conversion joins, unresolved-label reporting, the CLI, and rejection of
stock or spoofed cache versions.

The core and label bundles are not cryptographically bound to one another.
The tool therefore reports hashes for the database members and label file
and explicitly lists unresolved command, field, unit, and encoding label IDs.
Keep the outer `.eng` hash and package path with any extracted result; zero
unresolved labels catches ordinary dictionary mismatches but is not proof of
an exact module-version match.

This format resolution makes targeted signal-layout and conversion recovery
possible without running wiTECH or CDA. Security/seed material is outside the
mapping objective and was not decoded or exercised.

## Guided diagnostics

The guided-diagnostic tree contains 20 `diagnostic.exml` files, 15 JavaScript
files, eight `.ebsh` files, localized properties, and one plaintext AspectJ
configuration. Every `.exml` size is a multiple of 16 bytes. Their high
entropy, repeated identical 16-byte blocks at multiple offsets, and shared
first block are strongly consistent with uncompressed XML under a
deterministic 128-bit ECB-style cipher, most plausibly AES-ECB. The exact
algorithm, key, and padding remain unproved.

`guidedDiagnostics/feedback/META-INF/aop.xml` explicitly excludes
`com.dcx.NGST.crypto..*`, making that runtime package the best decryptor-loader
lead. No matching NGST class or JAR was exposed elsewhere in the archive or
in the inspected wiTECH sidecar ZIP listings. The likely client code remains
inside the 830 MB installer, which was not executed or decompiled.

Readable files still recover several useful facts:

- `ResetECU` explicitly associates request `22 01D5` with engine RPM,
  independently corroborating the current PCM result.
- `RestoreProxiCfg` JavaScript processes a separate offset array for each
  XMIT, compares old and new byte/bit values with XOR, and builds DDE
  name/value tables before replacing each changed XMIT payload. This
  establishes the hidden XML's broad data model without revealing its
  commands.
- `BPCM_FeedBack/diagnostic.js` contains 34 literal read requests:

  ```text
  F190 F18C F194 F132 F183 F100 200A
  A002 A005 A008 A017 A018 A019 A025 A029 A037 A046 A057
  A062 A063 A064 A301 A302 A303 A304 A305 A306 A307 A308
  A309 A310 A311 A312 A313
  ```

  The literals after `A309` are `A310-A313`, not inferred hexadecimal
  `A30A-A30F` requests. They are unlabeled BPCM leads with no current-vehicle
  support claim.
- The TCM62 workflow is titled `ZF Nine Speed Service Quality Initiative`,
  but its readable code contains only a 30-DTC gate. One literal is `PD199`,
  while the related TCM40 list uses valid-looking `P1D99`; preserve that
  source defect rather than silently correcting it.

`guidedDiagnostics/RestoreProxiCfg/` describes this high-level workflow:

1. retrieve the vehicle configuration using VIN and sales codes;
2. compare it with current BCM values;
3. write changed configuration data;
4. request an ignition cycle; and
5. perform PROXI alignment.

Its JavaScript includes helpers for comparing byte/bit offsets and building a
table of changed diagnostic data elements, but it does not expose the exact
service payloads.

`guidedDiagnostics/BFWrenchLightVF/diagnostic.exml` is explicitly a VF
`Disable wrench light` procedure, but its actionable content is likewise
opaque. It is a possible later lead, not a recovered procedure and not
authorization to run one.

## Lower-value or ruled-out branches

- `wiTECH_Install_17.04.27.exe` is an 829,910,936-byte signed InstallShield
  setup executable with product version 17.04.27. Static 7-Zip inspection
  exposes PE resources but not the compressed client payload. It was not run.
- The adjacent `17.04.27_bundles/` tree contains microPOD, wiPOD,
  StarMOBILE, VAS, and flash/update firmware. It is useful for hardware
  enablement research, not the leading diagnostic-map source.
- The Enhanced DRB III emulator reports version 11.54. Its `flpart.csv` has
  423 rows covering model years 1998-2006 and no VF body. It is not a
  ProMaster map.
- `U-Scan Chrysler/database.xml` is a generic May 2007 SAE J1979 PID/scaling
  database. Its `dtc.xml` is generic J2012 material.
- The `Data Bus Diagnostic Tool` MSI identifies General Motors and is outside
  this vehicle scope.
- No exposed ODX, PDX, MDX, A2L, DBC, SQLite diagnostic database, CAN log,
  PCAP, or BLF was found.
- No `.eng` filename explicitly names VF, ProMaster, Ducato, 948TE, or 9HP.

## Recommended follow-up order

1. Use the decoded FCA row format only on exact current-DID overlaps, beginning
   with captured current-positive values. The RFH, BCM, and PCM comparisons
   above, plus the bounded shifter comparison, are complete first passes.
2. Recover bit layouts and conversions together with the exact module variant,
   diagnostic request, and label ID. Never export a flat/global DID list.
3. Compare every recovered definition with current live responses or existing
   controlled captures. Preserve contradictions such as the rear sensor-ID
   swap instead of forcing a match.
4. Apply the same offline comparison to other captured current-positive
   module values; do not spend further time on the legacy TCM branch without
   an exact identity lead.
5. Search for an exact current part/software/variant tuple before assigning
   higher confidence. Address or module-name overlap alone is insufficient.
6. If the client branch is resumed, target the code that loads
   `com.dcx.NGST.crypto` rather than guessing a key from ciphertext. Installer
   decompilation is a larger Windows-static-analysis branch and remains
   secondary to bounded `.eng` comparisons.

No additional live scanning is justified by this archive alone.

## Selected archive provenance

| artifact | SHA-256 |
|---|---|
| `ACC-DASM_CUSW-60-00-000.eng` | `92d381347cc99504b0ba422fe08512de377a109783ddbb3f7cf96e28b979db80` |
| `BCM-BCM_CUSW-60-01-002.eng` | `e7633c402bcd0c2f8bf386eb86b41b7450c597922e412057feeeb2022b4e67bc` |
| `BCM-BCM_FGA-4083EC8A-016.eng` | `e78bcd01cfbb5e4db0a423cea61affdc95676c560fd00c4fa49e204a9799b0b3` |
| `IPC-IPC_FGA-3283CE8A-006.eng` | `e99ecdf4d6c0f294c601114027d70b056c35cbbdf605fe6182cc5aac602dbd6c` |
| `IPC-IPC_CUSW-61-00-001.eng` | `bf20aeaef2a2479b4ca11d6c51587368dac183e4d8b8a901a8aa342b3eb4dc5f` |
| `PCM-PCM-60-00-005.eng` | `07a9625e69dfcb1a28278f25659b91dcdfde246a9d0dfa61f0dd5ec7672f530b` |
| `PCM-PCM-61-00-005.eng` | `7c25f3743a4f77b46d4cce647a0337a9edf261cccd5b7212450cc5c58c42b0e2` |
| `RFH-RFHM_CUSW-60-02-001.eng` | `e56cda813892d585cdb6721698b79274481ded7d0869176eadbabc61262888b0` |

The archive root is a private owner resource. These hashes identify the local
historical packages; they do not confer current-vehicle applicability.
