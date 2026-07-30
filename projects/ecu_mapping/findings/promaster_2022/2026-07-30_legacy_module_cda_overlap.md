# Legacy shifter, ABS, EPS, and ORC CDA overlap — 2026-07-30

## Outcome

A bounded static comparison of eight 2010–2011 FCA engineering profiles
against already captured 2022 responses recovered useful shifter/GSM
lifecycle candidates and explicit non-transferability evidence for ABS, EPS,
and ORC.

The two legacy GSM profiles contain 32 of 48 known current-positive shifter
requests. Ten recurring FCA lifecycle records have matching current response
widths, providing good historical candidates for their field layouts and
conversions. A current `F158` response confirms that some field positions
survived while showing that old enumeration tables did not.

The ABS, EPS, and ORC profiles overlap almost exclusively in standardized
identity material and include direct layout conflicts. They do not resolve a
current runtime signal.

No live CAN request, vehicle actuation, Windows execution, or security work
was performed. Source members were read statically, working extracts remained
under ignored `tmp/fca_eng_survey/`, and the FCA archive was unchanged.

## Shifter/GSM lifecycle candidates

Both `ESM-GSM_CUSW-60-01-000.eng` and
`ESM-GSM_FGA-01-01-000.eng` use request `18DA1FF1` and response
`18DAF11F`, the current shifter endpoint. They contain 32 of the 48 unique
service-`22` requests already positive in current captures.

The following legacy definitions have the same data widths as the current
positive responses:

| DID | legacy definition | layout / conversion | current status |
|---|---|---|---|
| `1008` | ECU timestamp in RAM | u32, direct minutes | matching width; historical candidate |
| `1009` | time since key-on in RAM | u16, `raw * 15 sec` | matching width; historical candidate |
| `2001` | odometer | u24, `raw * 0.1 km` | matching width; historical candidate |
| `2002` | odometer at last flash update | u24, `raw * 0.1 km` | matching width; historical candidate |
| `2003` | flash rewrite count | u8 count | matching width; historical candidate |
| `2008` | ECU timestamp in EEPROM | u32, direct minutes | matching width; historical candidate |
| `2009` | time since key-on in EEPROM | u16, `raw * 15 sec` | matching width; historical candidate |
| `200A` | key-on counter | u16 count | matching width; historical candidate |
| `200B` | ECU time at first DTC | u32, direct minutes | matching width; historical candidate |
| `200C` | key-on time at first DTC | u16, `raw * 15 sec` | matching width; historical candidate |

The same record family and conversions recur in legacy BCM and DASM profiles,
and current BCM behavior independently validates several of those meanings.
That cross-module recurrence strengthens the shifter candidates, but matching
width alone does not prove the current GSM labels. A repeated ignition-cycle
or time/odometer comparison would be needed before promotion.

The 16 current-positive shifter requests absent from both legacy profiles are:

```text
0103 1002 1004 1006 1921 192D 193D 194D
1C1C 2013 F188 F18A F18B F191 F1A1 F1B0
```

### `F158` structure survives; enumerations do not

The current shifter returns:

```text
62 F1 58 16 72 03 02
```

The old four-field layout interprets the data as:

| byte | old field | old-table result | current assessment |
|---:|---|---|---|
| `16` | model year offset from 2000 | 2022 | exact |
| `72` | vehicle line | reserved | field position plausible; old enum obsolete |
| `03` | body style | Convertible | incompatible with the current ProMaster |
| `02` | country | USA | agrees |

This is direct evidence that a stable field boundary does not make an old
enumeration table current. The body-style conflict prevents profile-wide
promotion.

## ABS, EPS, and ORC limits

All six selected profiles use their modules' exact current physical
request/response endpoints. Their request overlap is nevertheless dominated
by identity records:

| module/profile | overlap with known current positives | useful conclusion |
|---|---:|---|
| ABS `BSM_CUSW` | 19/21 | identity continuity; no runtime map |
| ABS `ABS_FGA` | 18/21 | identity continuity; no runtime map |
| EPS `EPS_CUSW` | 17/21 | identity continuity; no runtime map |
| EPS `EPS_FGA` | 17/21 | identity continuity; no runtime map |
| ORC `ORC_CUSW` | 18/22 | identity continuity; no runtime map |
| ORC `ORC_FGA` | 18/22 | identity continuity; no runtime map |

The conflicts are informative:

- current ABS DID `F100` has 21 data bytes, while the old BSM definition has
  only five;
- the old EPS CUSW profile treats `F18C` as a flat 15-byte serial, matching
  the current ASCII shape, while the FGA profile divides the same DID into
  structured production subfields; and
- the available current ABS/EPS/ORC captures contain identity and DTC traffic,
  not controlled changing values that could qualify a runtime definition.

The flat EPS serial interpretation is the better structural candidate for
this capture, but neither legacy profile is an exact installed identity.
No runtime mapping or old structured subfield is promoted.

## Applicability boundary

The profiles predate the North American ProMaster, contain no exact current
part/software tuple, and have no populated year/body applicability result.
Their exact endpoints and large identity overlap demonstrate long-lived FCA
diagnostic conventions, not installed-module equivalence.

This source set is also distinct from the old wiTECH report for the owner's
former 2015 diesel ProMaster. No PCM or TCM definition from that former
powertrain is used here.

## Current-evidence provenance

The current-positive shifter union comes from the tracked
[`2026-07-21 candidate inventory`](2026-07-21_candidate_did_inventory.md)
and the current-vehicle AlfaOBD map under
`tmp/ecu_mapping/android_tablet/ccan_live_20260722_001010/`. The exact
`F158` response is independently preserved in
`tmp/inventories/shifter/dids_20260721_012903_536622-0600.results.jsonl`.
The ABS, EPS, and ORC comparison uses the already captured module maps under
`tmp/ecu_mapping/canch_20260725/`; no new diagnostic request was made for this
archive work.

## Archive provenance

| artifact | manifest date | SHA-256 |
|---|---|---|
| `ABS-BSM_CUSW-60-02-000.eng` | 2011-07-27 | `d1d220df5729d0cad06272bc2ed150eea7635f89c8c9a4480f406a18efacef18` |
| `ABS-ABS_FGA-EC07168A-009.eng` | 2010-10-15 | `f41f871de31d7840a8a285475879fb54970c88974ee7014227f8432ea16c514c` |
| `EPS-EPS_CUSW-60-01-004.eng` | 2011-07-19 | `e2d3a37cd50e101a30b3d62debed5a7da5ba7bd0d58e7030dddc8d13b90e4a70` |
| `EPS-EPS_FGA-31839B89-010.eng` | 2011-05-20 | `084defd46859c2dcb4eb45fe1c48920adec1667f19d1bc374a22697e45b6ad8c` |
| `ESM-GSM_CUSW-60-01-000.eng` | 2011-03-11 | `42ea225f7eb4d7fa8e1fb8acb8a31043f6e8c87eb09edd75c22fbf1fbebd2ca4` |
| `ESM-GSM_FGA-01-01-000.eng` | 2011-07-21 | `a158045165394b4231bfc5a9a241ce578ddbe351ee6953f1bfd69ace86fe018a` |
| `ORC-ORC_CUSW-60-01-001.eng` | 2011-08-24 | `da8668e0a7bf49e093a7e50cc2d03281da4cbf181aa1ce040a3bf7c45f4c06c0` |
| `ORC-ORC_FGA-EA07B38A-009.eng` | 2011-01-27 | `456bbf91f9168fcf60b6590a21269d1ee8f6fb07760789d527145e5499cbd9e4` |

These hashes identify private local archive files. They do not confer
current-vehicle applicability.
