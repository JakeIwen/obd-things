# PCM Plots idle mapping and passive C-CAN correlation — 2026-07-26

## Outcome

A simultaneous AlfaOBD Plots recording and independent listen-only PCAN
capture mapped eleven owner-selected PCM gauges to an exact physical
`22 DID -> 62 DID` polling order. Nine changing gauges received a direct
time-aligned label, DID, field, and Alfa-rendered scale. The two constant
gauges could be associated by their fixed position in the repeated eleven-DID
cycle, but their scales remain candidate-only.

More importantly, the same capture found strong passive broadcast forms for
the two highest-priority engine-health values:

| metric | Alfa-associated PCM DID | passive field | relationship | evidence |
|---|---:|---|---|---|
| Engine oil pressure | `022A`, byte 0 x 4 kPa | `0x41D` byte 2 | broadcast raw mirrors DID raw | 733/733 nearest samples, R² 0.9998047, raw RMSE 0.233 |
| Coolant temperature | `011D`, byte 0 - 64 °C | `0x2ED` byte 0 | DID raw = broadcast raw + 24; broadcast is byte - 40 °C | 733/733, effectively exact R² 1.0 and zero raw RMSE |

These two fields are now allowlisted receive-only telemetry sources. They do
not require AlfaOBD, a diagnostic session, or CAN transmission during a drive.
Their dashboard quality is `observed_alfa_scale`: the identity and rendering
are grounded in this current-vehicle Alfa/wire experiment, not in an ODX
definition.

The current-engine-torque result is promising but is not promoted. PCM DID
`06DA` is an exact signed big-endian value rendered as raw x 0.04 Nm. Passive
`0x100` byte 2 tracks it over the idle range with R² 0.9995295 and about
0.383 Nm display-domain RMSE. In the observed high-byte range the affine form
looks like `torque_Nm ~= byte2 - 244`, but that cannot describe positive
driving torque without a wrap or mode rule. A loaded drive must cross that
boundary before the passive field can be decoded or used for derived power.

## Conditions

- Vehicle parked in Park.
- Plots recording began with ignition on and engine off; the engine was then
  started and left idling through warm-up.
- AlfaOBD 2.4.4.0 was connected through OBDLink MX+ to
  `Chrysler Pentastar/Hemi engine Model Year 2021`.
- PCAN independently monitored C-CAN pins 6/14 at 500 kbit/s, listen-only.
- The synchronized diagnostic interval ran from approximately
  17:03:49 to 17:18:48 MDT.
- Campaign ID: `pcm-plots-idle-20260726T230230Z`.

Alfa recorded 710 complete current-section gauge rows. The decoded Debug trace
contained 730 polling cycles and 8,025 prompt-complete DID exchanges. All 710
gauge rows aligned; the inferred cycle boundary was unambiguous at `022A`,
with a 1 ms median absolute boundary gap.

The full PCAN stream contains 2,797,960 frames and 126,568,738 decompressed
bytes across two finalized zstd chunks. It has no detected SocketCAN drops and
both compressed streams pass integrity checks. The recorder was deliberately
stopped after the warm-up evidence was complete, before its originally
requested 2,700 seconds elapsed. Its checkpoint therefore honestly records a
signal/incomplete-duration result even though every partial was finalized and
the stream is complete through the stop time.

## Exact Plots polling order and mappings

The repeated request order was:

```text
022A, 011D, 0413, 0188, 0227, 01A1, 019B, 019E, 019C, 06DA, 069E
```

| AlfaOBD label | PCM DID / field | Alfa-observed rendering | result |
|---|---|---|---|
| Engine oil pressure | `022A`, u8 byte 0 | raw x 4 kPa | exact fit; 709 samples, 9 raw values |
| Coolant temperature | `011D`, u8 byte 0 | raw - 64 °C | exact fit; 709 samples, 59 raw values |
| Throttle Blade Position | `0413`, u16be bytes 0–1 | approximately raw x 100/81920 % | rounded near-exact fit; 81 raw values |
| Throttle Position Sensor Percent | `0188`, u8 byte 0 | raw x 0.655 % | exact fit; 13 raw values |
| Fuel Level Percent | `0227`, u8 byte 0 | candidate raw x 0.5 % | constant `C8 -> 100%`; order association only |
| Generator Duty Cycle | `01A1`, u16be bytes 0–1 | approximately raw x 100/32768 % | rounded near-exact fit; 220 raw values |
| Target Charging Voltage | `019B`, u16be bytes 0–1 | unresolved candidate near raw x 0.01544 V | constant `038C -> 14.020 V`; order association only |
| Battery voltage | `019E`, u16be bytes 0–1 | approximately raw x 0.01544 V | near-exact rendered fit; 18 raw values |
| Voltage Sense | `019C`, u16be bytes 0–1 | approximately raw x 0.01544 V | near-exact rendered fit; 19 raw values |
| Current engine torque | `06DA`, i16be bytes 0–1 | raw x 0.04 Nm | exact fit; 169 raw values |
| VVT Oil Pressure | `069E`, u8 byte 0 | raw x 4 kPa | exact fit; 9 raw values |

The two pressure rows were similar during this idle trial but remained
independently discriminated by their positions and zero-lag fits. A lagged
cross-fit was materially worse. The labels must therefore remain separate;
`069E` is not an alias for engine oil pressure.

The passive correlation cannot make the same clean distinction because both
pressure DIDs track `0x41D` byte 2 closely during idle. DID `022A` is the
better match (raw RMSE 0.233 versus 0.414 for `069E`; R² 0.9998047 versus
0.9993812), and it is the row explicitly labeled Engine oil pressure. Retain
that source assignment, but use a loaded pump-mode transition to characterize
whether `069E` is a filtered, VVT-specific, or otherwise related pressure.

The selector dialog itself reported only seven checked rows even though the
stopped Plots page displayed and recorded all eleven rows above. The catalog
walker deliberately did not press OK, preserving the visible owner-priority
set. This is another AlfaOBD UI-state trap: dialog checkmarks are not a
reliable reconstruction of the active Plots list on this APK/tablet.

## Mechanical observations

Coolant warmed from about 32 °C engine-off to 86 °C. Engine oil pressure rose
from zero at engine-off/start to about 216 kPa, then stabilized around
204–208 kPa once coolant exceeded 82 °C. That is approximately 29.6–30.2 psi.

The exact-vehicle OEM warm table applies only at 89–100 °C coolant and lists
103.4–234.4 kPa (15–34 psi) near 650 rpm. This trial ended slightly below the
table's temperature precondition, so the comparison is reassuring operating
context rather than a formal warm-pressure pass. The dashboard must not use
one fixed green/red band: RPM, temperature, pump mode, engine-running state,
and startup grace remain required for alerts.

## Passive-correlation results

Every remote report exact-linked its selected PCM response to the original
global candump frame and excluded diagnostic and extended IDs from the
candidate set.

| DID | leading passive result | interpretation |
|---:|---|---|
| `022A` oil pressure | `0x41D` byte 2, R² 0.9998047 | promoted receive-only source |
| `011D` coolant | `0x2ED` byte 0, R² effectively 1.0 | promoted receive-only source |
| `06DA` current torque | `0x100` byte 2, R² 0.9995295 | strong idle-range lead; wrap/mode unresolved |
| `0413` throttle blade | `0x41B` byte 5, R² 0.9953101 | strong lead, scale/offset unresolved |
| `0188` throttle sensor % | `0x736` byte 3, R² 0.9957565 | strong lead, exact identity unresolved |
| `019E` battery voltage | `0x41A` byte 0, R² 0.9829325 | corroborates the already-verified voltage frame |
| `019C` voltage sense | `0x41A` byte 0, R² 0.9457148 | related electrical measurement, not a new source |
| `069E` VVT oil pressure | `0x41D` byte 2, R² 0.9993812 | closely related to oil pressure; worse fit than `022A`, not a second source |
| `01A1` generator duty | best R² 0.7862291 | no defensible passive mapping from idle data |

The throttle results need a loaded drive to distinguish actual, commanded, and
related pedal/throttle fields. Generator duty shared the warm-up trend with
many electrical and engine signals and produced no useful passive identity.
Fuel level and target charging voltage were constant, so they were not
submitted as varying time-series references.

## Artifact integrity

| evidence | SHA-256 |
|---|---|
| final `AlfaOBD_Debug.bin` | `2d4a06e787e6f3c310773d7f701d27118763d143534af41072b5de8417779a95` |
| final `Gauges_Data.csv` | `48713ca3fdda52b7cbcafc9e796466ecc42c02c532544feb6566beed11e5447b` |
| gauge-join `report.json` | `b3bf1c293a3edb20abf457217f12bd577d137689b600243f9a279caf95f8911a` |
| exact PCM wire JSONL | `b378b73a770e17e710480ba3f05267fc76f7abe7b01c8dcb22f11aa22fcf44b7` |
| PCM wire summary | `159eab503cf6819131b4fe7e11fbf0f637df2d37613267d7f71a6f9508382c79` |
| full chunk 0 | `0a0478f7a9d41ce3eb0dbf285a9a1bfd7df3a8c3347cf304646736d94ed46132` |
| full chunk 1 | `3e886cd244492c9d410a21e2d7c4598621d934e732721e04aee62c16dd166fe5` |

Catalog campaign `pcm-plots-catalog-20260726T224830Z` completed a
forward/reverse 193-row inventory without manual reconciliation. Its catalog
hash is
`e1d5e74db311b13a6156cdaa30b0126a0789a502aaaffeaa36be33bca10ef3de`;
the catalog report and completion-state SHA-256 values are
`51021170c8d970c940e2f23abca54956ad7cc57adbe38b42030e7168ad247f86`
and
`de7ce3e45168dca46e0e503cfffc9d2a4aab7e8418daa72c0ff80ee4f50bd460`.

The remote oil, coolant, and torque reports were van-compute jobs
`20260726T232519Z-61f950ee`, `20260726T232519Z-9267fb32`, and
`20260726T233136Z-b3c9684a`; their report SHA-256 values are respectively
`d6bad30c69aa45d707795a987586954f24266dc3a43006a85da16084a160fe46`,
`a515efdae34bd831fff52932eb87261fa55e75d23a510ca0f53b5784a1dbc228`,
and
`e168751dd94139f8a59cd8bcd746ab03fb77b10d0310ca1090ea613a13b5843f`.
The successful VVT-pressure retry was job
`20260726T233445Z-af4265ba`, report SHA-256
`5c81c896985224f2e7746dc6cc17975f943a1894eef57de6123a456fb3841790`.
Its first attempt (`20260726T232522Z-4ff27360`) exited 137 on one remote
worker and produced no report; retrying after the parallel queue drained
completed normally.

## Utility and next step

Oil pressure and coolant temperature can now be displayed continuously from
passive C-CAN while the vehicle is awake. Torque remains available as an exact
Alfa-associated PCM DID and has a strong passive lead, but the next loaded
drive must establish the byte-wrap/mode rule. Engine RPM should be recorded in
the same synchronized run so contemporaneous, qualified torque and RPM can
eventually produce explicitly labeled ECU-estimated crankshaft power.

Engine-oil temperature was not among this active eleven-row Plots set and
remains unmapped. The complete 193-row catalog makes it straightforward to
schedule a later targeted recording without manually actuating hundreds of
controls.
