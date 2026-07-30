# Legacy PCM CDA overlap against current-vehicle captures — 2026-07-30

## Outcome

Four 2011 FCA PCM engineering profiles contain all 14 service-`22` requests
already observed in the 2022 van's AlfaOBD PCM recordings. The historical
field widths and conversions independently agree with the current captures
for ten changing values and are consistent with one constant voltage value.
They also expose two direct profile conflicts and one still-unresolved
fuel-level ambiguity:

- legacy DID `0413` uses `raw * 0.0245%`, about 20 times the current
  Alfa/wire rendering;
- legacy DID `03D6` uses `raw * 0.5 mph`, which does not describe the current
  Alfa/wire rendering; and
- legacy DID `0227` uses `raw - 100%`, but every current wire response was
  `C8`, for which both that formula and the earlier `raw * 0.5%` candidate
  produce 100%.

The current captures remain authoritative wherever the profiles disagree.
These bundles contain no exact current part/software identity or populated
year/body applicability row. They are historical vendor corroboration, not a
2022 PCM definition.

No live CAN traffic, vehicle command, Windows program, or security material
was used. The six source archives were read statically, selected members were
extracted under `tmp/fca_pcm_survey/`, and `/mnt/EXFAT512/FCA/` was left
unchanged.

## Profiles and identity boundary

The six selected archives are the latest small `PCM-PCM-6x` profiles in the
collection. Their core bundles declare CDA bundle version `11.2.0.0`; five
are dated 2011-08-31 and `63-00-003` is dated 2011-09-01.

| archive | variant / version / minimum | protocol ID | decoded field rows | current-target result |
|---|---|---:|---:|---|
| `PCM-PCM-60-00-005.eng` | `96 / 0 / 5` | 1 | 27,732 | all 16 requested services present |
| `PCM-PCM-61-00-005.eng` | `97 / 0 / 5` | 1 | 27,732 | all 16 requested services present |
| `PCM-PCM-60-B1-001.eng` | `96 / 177 / 1` | 1 | 27,729 | all 16 requested services present |
| `PCM-PCM-61-B0-002.eng` | `97 / 176 / 2` | 1 | 30,122 | all 16 requested services present |
| `PCM-PCM-62-00-003.eng` | `98 / 0 / 3` | 11 | 407 | none of the 16 services present |
| `PCM-PCM-63-00-003.eng` | `99 / 0 / 3` | 11 | 1,113 | none of the 16 services present |

All six use request `18DA10F1` and response `18DAF110`, the same physical
diagnostic endpoint as the current PCM. Profiles 60 through 62 use broadcast
address `18DB33F1`; profile 63 uses `18DBFEF1`. The two sparse profiles'
zero-overlap result despite matching request/response addresses is a useful
counterexample: endpoint equality does not select a compatible variant.

Each decoded database passed 28 declared integrity checks. The four
overlapping profiles have identical normalized definitions and conversions
for the 14 service-`22` targets. Their service-`21` definitions are also
historical matches, but the current PCM returned
`7F 21 31` (`requestOutOfRange`) for both during the loaded drive.

This source set is separate from the September 2022 wiTECH report for the
owner's former model-year 2015 diesel ProMaster. Nothing here establishes that
these general 2011 engineering profiles came from that vehicle. Conversely,
no powertrain mapping from the former diesel should be assigned to the current
gasoline van or its 948TE.

## Exact current-request comparison

All fields begin at bit 24, immediately after the three-byte positive-response
echo. `u8`, `u16be`, and `i16be` below describe the resulting data field.

| DID | legacy CDA definition | comparison with 2022 evidence |
|---|---|---|
| `022A` | Oil Pressure Sensor PSI; u8; `raw * 4 kPa` | exact layout and scale agreement with the live Engine oil pressure mapping |
| `011D` | Engine Coolant Temp; u8; `raw - 64 °C` | exact agreement |
| `0413` | Throttle Blade Position; u16be; `raw * 0.0245%` | **conflicts** with current `raw * 100/81920%`; reject the legacy scale |
| `0188` | Throttle Position Sensor Percent; u8; `raw * 0.65499%` | agrees with current rounded `raw * 0.655%` |
| `0227` | Fuel Level Percent; u8; `raw - 100%`, valid raw 100–200 | unresolved on current van; all 733 wire responses were `C8 -> 100%`, also compatible with `raw * 0.5%` |
| `01A1` | Generator Duty Cycle; u16be; `raw * 0.003052%` | agrees with current rounded `raw * 100/32768%` |
| `019B` | Target Charging Voltage; u16be; `raw * 0.01544 V` | consistent with the one current constant value; needs variation for a current-scale proof |
| `019E` | Battery Volt; u16be; `raw * 0.01544 V` | agrees with the changing current response/rendering |
| `019C` | Voltage Sense; u16be; `raw * 0.01544 V` | agrees with the changing current response/rendering |
| `06DA` | Actual Torque; i16be; `raw * 0.04 N*m` | exact layout, signedness, and scale agreement across positive load and negative overrun |
| `069E` | VVT Oil Pressure; u8; `raw * 4 kPa` | exact agreement |
| `01D5` | Engine Speed; u16be; `raw rpm` | exact agreement |
| `03D6` | Vehicle Speed; u8; `raw * 0.5 mph` | **conflicts** with current Alfa/wire `raw * 0.31068596 km/h`; do not transfer |
| `069F` | VVT Oil Temp; u8; `raw - 64 °C` | exact agreement with the current VVT-specific temperature mapping |

The label `Oil Pressure Sensor PSI` is internally inconsistent with the
legacy conversion's kPa output; the numerical conversion matches the current
mapping. This is another reason to preserve raw definitions and units rather
than relying on display names alone.

For fuel level, one non-`C8` current response will distinguish the candidates.
For example, raw 150 would render as 50% under the legacy formula but 75%
under the earlier half-scale candidate. Until such a value is captured,
neither formula is promoted as an exact current decode.

## Current-positive PCM inventory overlap

A separate join against the union of already captured, directly established
current PCM positives found 157 of 187 requests in each of
`PCM-PCM-60-00-005` and `PCM-PCM-61-00-005`. The current set consists of 167
service-`22` and six service-`1A`
positives from the 2026-07-25 current PCM map, plus the 14 later exact
service-`22` recording DIDs absent from that map: 181 service-`22` and six
service-`1A` requests total.

This is stronger than catalog membership: those 157 requests have positive
current-vehicle evidence. It makes the old profiles relevant gasoline-PCM
siblings, but response support still does not prove that an old field layout,
bit label, or conversion survived.

The 30 current-positive DIDs absent from profiles 60/61 are:

```text
0647 09A0 09A1 09A2 09A3 0B58 0B59 0B94 0CC3 0CC8
0F1C 1160 1311 1318 14F9 14FA 14FD 14FF 20C3 20CE
212F 2130 2138 215A 2185 2189 218A 218F 2196 2197
```

Several non-gauge overlaps are useful next-step candidates:

| DID / service | current response data | legacy definition | candidate result |
|---|---|---|---|
| `1A 87` | `02 40 7F 34 0D 20 47 15 08 00 36 38 35 33 32 31 35 37 41 49` | `DCX / MMC ECU Identification`; origin, supplier, variant, diagnostic, reserved, hardware, software, and ten-byte ECU part-number fields | exact current payload-length and field-boundary agreement; final field is ASCII `68532157AI` |
| `055C` | `00B8` | Fuel Tank Size; u16be, `raw * 0.125 gallons` | 23.000 gallons; plausible historical candidate |
| `9E8E` | `184C` | Odometer Location 0; u16be, `raw * 8.192 miles` | 50,954.240 miles |
| `9E8F` | `184D` | Odometer Location 1; same conversion | 50,962.432 miles |
| `9E90` | `184E` | Odometer Location 2; same conversion | 50,970.624 miles |
| `9E91` | `184B` | Odometer Location 3; same conversion | 50,946.048 miles |

The four odometer-like values cluster within 24.576 miles, which is
structurally persuasive for redundant storage locations. Their exact roles
and update policy remain unverified; they are not substitutes for the
current cluster/BCM odometer.

An older derived DID map displayed `1A87` with an apparent trailing `32`.
That was ISO-TP padding leakage, not response data. The first-frame length is
`0x16`; the tracked raw capture and the current `alfalog.iter_exchanges`
decoder both end the 22-byte response at `...68532157AI`. The exact legacy
length match above is based on those sources, not the stale rendering.

Under the old tables, the first field's `02` means DCA. Supplier `40` and
variant `7F` are absent from the 2011 enums, while the remaining byte
boundaries yield diagnostic version `34`, reserved `0D`, hardware version
`2047`, software version `150800`, and the exact ten-byte part number. The
structural match is strong; the obsolete supplier/variant tables remain a
warning against importing the old enum vocabulary.

Other exact-width historical candidates are `0645` Crank Status, `03C2`
filtered switch inputs, `0232` Fuel Control Status, `035A` ESIM Status, and
the `10AC`/`10AD`/`1102` filtered digital-input groups. Their individual old
bit labels include options and powertrain features that may not exist on this
van, so only the group-level leads are retained.

Direct width conflicts provide the counterweight: current `01B6`, `0231`,
and `031F` responses are respectively one, four, and two bytes, while the
legacy definitions occupy two, two, and one byte. Current positive responses
also exist at old cylinder-7/8-related DIDs even though the installed engine
is a V6. A positive DID is therefore not enough to infer unchanged semantics
or installed hardware.

## AlfaOBD catalog overlap

The current AlfaOBD `TIGERSHARK_CUSW` Plots catalog has 193 presentation rows
covering 190 unique DIDs. A read-only join against
`PCM-PCM-60-00-005.eng` found 167 of the 190 unique requests. The decoder
returned 168 historical command rows for those 167 DIDs, with no unresolved
labels, conversions, or references.

The 23 absent requests are:

```text
0ABD 0ABE 0AF0 0B79 0B87 0B9C 0B9D 0BA0 0BA7
0D2A 0D33 0D47 0D50 0DA5
1737 1738 1739 173A 173F 1740
FE11 FE18 FE62
```

This 87.9% request overlap is strong evidence of long-lived FCA PCM
diagnostic namespaces. Catalog membership alone is not installed support; the
separate current-positive comparison above is the appropriate support
boundary.

The two denominators describe mostly different request universes. Only 16
DIDs occur in both the 187-request current-positive set and the 190-DID
AlfaOBD catalog: the 14 recorded targets above plus `0949` and `094A`. All 16
join the legacy profile. Of 171 current-only requests, 141 join; of 174
catalog-only requests, 151 join. The visually similar totals—157/187
current-positive and 167/190 catalog—are therefore not alternative counts of
one list.

Of the 167 overlapping unique rows, 110 current and legacy labels are
character-for-character equal and 118 become equal after case and
punctuation normalization. Some differences are harmless abbreviations, but
the catalog also contains substantive conflicts:

| DID | current AlfaOBD catalog | 2011 CDA profile |
|---|---|---|
| `06A0` | Exhaust Cam 1 Duty Cycle | Intake Cam 1 Duty Cycle |
| `06A1` | Exhaust Cam 2 Duty Cycle | Intake Cam 2 Duty Cycle |
| `06A2` | Intake Cam 1 Duty Cycle | Exhaust Cam 1 Duty Cycle |
| `06A3` | Intake Cam 2 Duty Cycle | Exhaust Cam 2 Duty Cycle |
| `063F` | Cam State | CAM STATE EXH 1 STORED |

Those conflicts block bulk promotion even where a current positive response
exists. Controlled physical, state-transition, or cross-signal evidence
remains the upgrade path for an interpretation.

## Practical result

The archive materially strengthens confidence in the already observed
current mappings for oil pressure, coolant temperature, throttle-sensor
percent, generator duty, battery/charging voltages, signed torque, VVT
pressure, engine RPM, and VVT oil temperature. It also narrows fuel-level
work to one discriminating sample.

It does not justify changing the current throttle-blade or vehicle-speed
scales, scanning 167 catalog DIDs again without a targeted question, or
reusing the legacy transmission rows. The two scale conflicts, the cam-label
swap, the sparse same-address variants, and the current `21` negative
responses are all positive evidence that variant selection matters.

## Current-evidence provenance

- The 173-request base positive set comes from the current PCM section of
  `tmp/ecu_mapping/canch_20260725/all_module_did_map.txt`; its six
  service-`1A` responses were rechecked with the current parser because the
  old derived renderer leaked padding on `1A87`.
- The 14 later exact service-`22` requests and their rendered values come from
  the linked 2026-07-26 idle and 2026-07-27 loaded-drive findings.
- The independent exact `1A87` transport is preserved in
  [`2026-07-21_pcm_fixed_dlc_engine_idling.candump`](2026-07-21_pcm_fixed_dlc_engine_idling.candump).
- The single-frame `055C` and `9E8E-9E91` current responses are in
  `tmp/ecu_mapping/android_tablet/ccan_live_20260722_001010/`.
- The 190-DID catalog join is retained under
  `tmp/fca_pcm_survey/PCM-PCM-60-00-005/tigershark-catalog-joins.json`.

All `tmp/` paths are ignored working evidence; the source-archive hashes below
and the tracked current findings provide the durable provenance boundary.

## Archive provenance

| artifact | SHA-256 |
|---|---|
| `PCM-PCM-60-00-005.eng` | `07a9625e69dfcb1a28278f25659b91dcdfde246a9d0dfa61f0dd5ec7672f530b` |
| `PCM-PCM-61-00-005.eng` | `7c25f3743a4f77b46d4cce647a0337a9edf261cccd5b7212450cc5c58c42b0e2` |
| `PCM-PCM-60-B1-001.eng` | `8b8c3894232b7c701003103f9346afb94265b376483e2153c0e4f69c8f6a640a` |
| `PCM-PCM-61-B0-002.eng` | `c70a423c55a46140a37d1c5d72be95288432fe3e902c51d12c0e90430cdaef05` |
| `PCM-PCM-62-00-003.eng` | `07472ef924becc5f4ffe11069dc7024b00250f0d876722a4a4fa290ccf69dc0a` |
| `PCM-PCM-63-00-003.eng` | `0dc1e831d83bd35360efad09f081640c9ce03e7d769a269847b3f6ebf8c11c2a` |

These hashes identify files under the private archive root. They do not
confer current-vehicle applicability.
