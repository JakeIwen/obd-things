# PCM Plots loaded-drive mapping and passive C-CAN correlation — 2026-07-27

## Outcome

A 35-minute current-vehicle drive expanded the preceding idle campaign across real load,
deceleration, and temperature variation. AlfaOBD's labeled Gauge rows, its exact PCM diagnostic
trace, and a simultaneous independent listen-only PCAN capture establish the following useful
facts:

- PCM DID `01D5` is an exact unsigned big-endian engine-speed value in rpm.
- PCM DID `06DA` is a signed big-endian current-engine-torque value with a `0.04 Nm/bit` scale
  across both positive load and negative overrun.
- PCM DID `069F` is the Alfa-labeled **VVT Oil Temperature** value, `raw - 64 °C`. It must not be
  relabeled as general engine-sump oil temperature.
- The prior oil-pressure and coolant-temperature mappings remain valid under load.
- The selected PCM profile's Transmission Oil Temperature and Turbine speed rows do not work on
  this PCM: their legacy requests return `requestOutOfRange`. Those values should be mapped from
  the installed ZF 948TE TCM.

The diagnostic results are exact current-vehicle label/raw associations. Their physical units and
names remain `observed_alfa_scale`, not ODX-verified definitions. Passive broadcast correlations
below add independent field identity evidence; a high time-series score alone is never treated as
permission to publish a candidate.

## Conditions and capture completeness

- AlfaOBD 2.4.4.0 was connected through OBDLink MX+ to
  `Chrysler Pentastar/Hemi engine Model Year 2021`.
- PCAN independently monitored C-CAN pins 6/14 at 500 kbit/s, listen-only.
- Campaign ID: `pcm-plots-drive-20260727T161307Z`.
- Host capture interval: 2026-07-27 10:13:08 through 10:48:50 MDT, approximately 35 minutes
  42 seconds.
- The selected drive reached 47.846 km/h in AlfaOBD, 4,236 rpm, 240.76 Nm positive torque, and
  -62.92 Nm overrun torque.
- The engine was shut off at the end. The recorder finalized automatically after verified
  `0x2EF` absence rather than running its deliberately oversized 22-hour ceiling.

The full passive stream contains 5,817,430 frames across four finalized zstd chunks. The recorder
reported a complete full stream, zero detected socket drops, and zero preflight RX dropped/missed
counts. Its `duration_complete: false` is expected: the ignition-off presence gate ended the
campaign successfully with reason `tracked_id_absent`.

The Gauge section contains 1,323 valid rows with no short or long rows, from tablet clock
09:18:23.035 through 09:47:51.904. The tablet clock was about one hour behind the host, but passive
correlation does not use that wall-clock comparison: every PCM response was independently linked
to its original PCAN frame and kernel timestamp.

## Exact PCM polling cycle and labeled mappings

The drive repeated this eleven-request cycle:

```text
22 022A, 22 011D, 22 01A1, 22 03D6, 22 01D5, 22 06DA,
22 069F, 22 0413, 22 019E, 21 18, 21 62
```

All 1,323 Gauge rows aligned to a cycle boundary with a 1 ms median and 13 ms maximum absolute
offset. The exact label/raw associations are:

| AlfaOBD label | PCM request / field | Alfa-observed rendering | drive range / result |
|---|---|---|---|
| Engine oil pressure | `22 022A`, u8 byte 0 | raw × 4 kPa | 196–560 kPa; exact zero-lag fit |
| Coolant temperature | `22 011D`, u8 byte 0 | raw - 64 °C | 69–100 °C; exact zero-lag fit |
| Generator Duty Cycle | `22 01A1`, u16be | approximately raw × 100/32768 % | 7.447–100.008%; near-exact rounded fit |
| Vehicle speed | `22 03D6`, u8 byte 0 | approximately raw × 0.31068596 km/h | 0–47.846 km/h; near-exact rounded fit |
| Engine speed | `22 01D5`, u16be | raw rpm | 716–4,236 rpm; exact fit |
| Current engine torque | `22 06DA`, i16be | raw × 0.04 Nm | -62.92–240.76 Nm; exact spot-validated scale |
| VVT Oil Temperature | `22 069F`, u8 byte 0 | raw - 64 °C | 55–96 °C; exact fit |
| Throttle Blade Position | `22 0413`, u16be | approximately raw × 100/81920 % | 0.083–3.591%; near-exact rounded fit |
| Battery voltage | `22 019E`, u16be | approximately raw × 0.01544043 V | 11.719–14.483 V; near-exact rounded fit |
| Transmission Oil Temperature | `21 18` | none | `7F 21 31`; blank all 1,323 rows |
| Turbine speed | `21 62` | none | `7F 21 31`; blank all 1,323 rows |

The generic fitter ranked the torque association as ambiguous because rapid loaded-drive changes
and cycle timing make a single affine fit sensitive to adjacent samples. Direct same-cycle raw
checks remove the scale ambiguity:

| rendered value | raw response | signed raw | check |
|---:|---|---:|---:|
| 1.60 Nm | `62 06DA 0028` | 40 | `40 × 0.04 = 1.60` |
| 240.76 Nm | `62 06DA 1783` | 6,019 | `6,019 × 0.04 = 240.76` |
| -62.92 Nm | `62 06DA F9DB` | -1,573 | `-1,573 × 0.04 = -62.92` |

This establishes the diagnostic DID scale across the sign boundary. It does not by itself solve
the passive torque broadcast's encoding.

Four statically decoded 2011 FCA PCM engineering profiles independently use
the same field widths and conversions for `022A`, `011D`, `01A1`, `01D5`,
`06DA`, `069F`, and `019E`. In particular, they corroborate signed i16
`06DA * 0.04 N*m` and direct u16 `01D5` rpm. They also assign incompatible
legacy conversions to `0413` (`raw * 0.0245%`) and `03D6`
(`raw * 0.5 mph`). The live current-vehicle Alfa/wire mappings above win both
conflicts. The same profiles define old `21 18`/`21 62` data, while the
current PCM's repeated `7F 21 31` responses prove that those legacy services
are not transferable. Full comparison:
[`2026-07-30_legacy_pcm_cda_overlap.md`](2026-07-30_legacy_pcm_cda_overlap.md).

## AlfaOBD recording traps found in this run

The Plots selector showed 12 selected gauges, including Output Speed, but the final Gauge section
persisted only the 11 columns above. Output Speed was neither a CSV column nor part of the repeated
request cycle. A selected row is therefore not evidence that AlfaOBD polled or recorded it.

The current Debug recording was not cleanly closed, so these drive exchanges inherited the prior
complete recording date, 2026-07-26, while the Gauge section explicitly says 2026-07-27. The
association was recovered with an explicit Debug-date override only after the clock-ordered trace,
exact cycle, and every Gauge boundary aligned. The join report preserves both dates and records
`date_overrides_gauge_section: true`; silently rewriting one source date would have hidden the
artifact defect.

## Passive C-CAN correlations

Every correlation exact-linked its selected PCM response to the original global candump frame,
excluded standard diagnostic IDs and `18DAxxxx` traffic, and processed all four chronological
capture chunks. The reports remain mechanically classified `candidate_only`; promotion decisions
also require the exact Alfa/raw relationship and independent field evidence.

| diagnostic reference | leading passive result | interpretation |
|---|---|---|
| `022A` oil pressure | `0x41D` byte 2; R² 0.9995118, 1,351/1,356 samples, 0.294 raw-count RMSE | loaded-drive confirmation of the existing receive-only oil-pressure source |
| `011D` coolant | `0x2ED` byte 0; R² 0.9999295, 1,351/1,356 samples | loaded-drive confirmation of the existing receive-only coolant source |
| `01D5` engine speed | `0x0FC` u16be bytes 0–1; `DID_raw = 0.250008 × unmasked_broadcast_raw - 0.052`, R² 0.9999794, 1,351 samples | independently qualifies `(0x0FC u16be & 0xFFFC) / 4 rpm` as a receive-only telemetry source |
| `06DA` current torque | byte-aligned search initially ranked `0x1F4` u16be bytes 0–1 at R² 0.9831147; packed-field follow-up below identifies that field as a related-platform ADAS torque request and finds a much more plausible physical torque quantity in `0x100` | torque-related fields are now better separated, but `0x100`'s exact torque stage remains unresolved; no torque/power telemetry promotion |
| `03D6` vehicle speed | `0x101` speed region; R² 0.9999638, full coverage | strong identity confirmation; Alfa's absolute scale does not yet reconcile cleanly enough with the packed broadcast field for promotion |
| `0413` throttle blade | `0x41B` u16be bytes 4–5; R² 0.9983770, 1,350 samples | strong loaded-drive throttle-related field; exact broadcast scale remains unresolved |
| `019E` battery voltage | `0x41A` byte 0; R² 0.9055454, full coverage | loaded-drive corroboration of the already independently verified system-voltage source |
| `01A1` generator duty | best R² 0.6080 | no defensible passive generator-duty mapping |
| `069F` VVT oil temperature | best R² 0.7935 on a broad `0x412` field | warm-up covariance, not a defensible passive temperature mapping |

The loaded drive therefore resolves the diagnostic torque sign/scale but not
the passive broadcast encoding. A one-field affine fit is insufficient here;
the related frames may contain normalized torque, an offset/multiplexed value,
or another control-stage quantity. Derived power remains blocked rather than
quietly combining unlike quantities.

### Packed-field torque follow-up

The original generic correlation deliberately tested only byte-aligned integer
views. A follow-up examined Stellantis-style 13-bit Motorola torque fields,
using the frame's low five bits of byte 0 followed by byte 1.

`0x100` contains a strong physical-scale candidate:

```text
raw13 = ((byte0 & 0x1F) << 8) | byte1
candidate_Nm = raw13 * 0.125 - 512
```

That decode spans -62.875 to 246.25 Nm over the full capture, nearly the same
signed domain as DID `06DA` (-62.92 to 240.76 Nm). Searching -1,000 through
+500 ms found the best affine comparison at a -106 ms broadcast offset:

```text
DID_06DA_Nm = 1.002011 * candidate_Nm + 7.118
R² = 0.982574
affine RMSE = 10.890 Nm
direct-formula RMSE = 13.137 Nm
samples = 1,351
```

The near-unit slope strongly supports the packed field and `0.125 Nm/bit`
scale. It does **not** establish semantic equivalence: the direct candidate
sat about 7 Nm below the DID on average, its error changed across positive and
negative torque regimes, and isolated steady-frame discrepancies reached
roughly 80 Nm. Other `0x100` state bits explain part of the residual, consistent
with a different torque stage or operating-mode quantity. Publishing it as
`Current engine torque`, or deriving horsepower from it, would overstate what
the evidence proves.

The initially higher-ranked `0x1F4` result is now less ambiguous semantically.
The current comma.ai
[`_stellantis_common.dbc`](https://github.com/commaai/opendbc/blob/c3e6dca2e43d0620c7c31be1872823ed9d0c2c92/opendbc/dbc/generator/chrysler/_stellantis_common.dbc#L78-L80)
defines decimal frame 500 (`0x1F4`) as `DAS_3` and its corresponding 13-bit
field as `ENGINE_TORQUE_REQUEST`, `raw × 0.25 - 500 Nm`. This is
related-platform rather than ProMaster ODX authority, but it explains why the
field covaries strongly with current torque and also why it must not be
promoted as a measured current-torque source.

## Artifact integrity

| evidence | SHA-256 |
|---|---|
| final `AlfaOBD_Debug.bin` | `f6147f6f72d67a09335376940b78ef99f11205570e0664680714f03f850cb4a3` |
| final `Gauges_Data.csv` | `ff5192906c5244de0a6537659d51bb3cfa246cca1ccc46a95218ad4546235ce9` |
| decoded Debug text | `d714a5242a493dc5b2b52828e4263d192e92eeac920621ed1c2c0f3e44d3540f` |
| gauge-join archive | `3aec3b1b8f45bbd13d20679a6789d43cb8dd397a43c8003db394cc92cabb1ec1` |
| unpacked gauge-join report | `83b6f3f05f94dbec736208c5f2f276c9c0f188b5c1c74841dc03c043e762f9cf` |
| exact PCM wire JSONL | `2db3edbec3e1e1c8bfa89c0e2ac8381a262452a04f364959d8c4886d32b13d3a` |
| exact PCM wire summary | `46e7484ee93db40812d5fc3c4391e10e1649a26b1239253e44ee4384df946dc6` |
| full chunk 0 | `5757d6b424faca04bdee9c9dc7ac06112a3c77c9ab645e0d11b0b3953f67346f` |
| full chunk 1 | `fcb87c7a5747df255cb6cb5c232d4bf5a19ea57de0963a8fa402e9f4fcc706ca` |
| full chunk 2 | `d2e1a2a8ff53ecd59be762028c2ddfc651acc4ceed5c187850ffd5730d41b494` |
| full chunk 3 | `10a1e98cfb5503e5dcc24dd59130258aac14248e72ae07976bea8ef406a1b26a` |

The exact PCM wire extraction contains 12,200 paired reads, zero unpaired positive responses, and
three incomplete requests at capture boundaries. Those boundary requests were excluded rather than
guessed.

The nine four-chunk correlation reports were produced by these van-compute jobs:

| DID | job | report SHA-256 |
|---:|---|---|
| `022A` | `20260727T171148Z-81c0b53d` | `f6d3299f3e10d9c9654c727f67a20c153b7d05257b5a86e79dd310d6e5a70c8d` |
| `011D` | `20260727T171148Z-0edebf44` | `3ecce4a4b120769c5c2c1291c1b927d1780e81375f0535008cec46990bba0df6` |
| `01A1` | `20260727T171148Z-26dd2248` | `dd1e74e52f72af47094a14a6dc47466625f5568a48f79292cf7c7d12b78c8fda` |
| `03D6` | `20260727T171147Z-38622f25` | `ab8dd562e2e553efcee673b119575378fdfd9c0f7afdfdfc23e16981331c88f9` |
| `01D5` | `20260727T171148Z-5321e51e` | `29d54892c1fd941b228b4bde8ff95a3f2e8d5537d35f0e0ac873da2ca14d8c00` |
| `06DA` | `20260727T171148Z-b40f2882` | `8a056fc87de1e269cdcfc91af61e9d07e981b328faae798e87b14672be06107a` |
| `069F` | `20260727T171148Z-4b794f6c` | `fc206602d4ee5822effcd75f627091bd544f17d42299923bb92bd8c41ea48eab` |
| `0413` | `20260727T171148Z-0571d5b2` | `ff3b537a48fbfe3ea3f199faacdb165b1e3360406717559ba6b0a787c3c29742` |
| `019E` | `20260727T171147Z-9b0c2272` | `fcdada0e34359fac300d65c6f0ba4042764f23627b73db87245a20a7cbd9a77d` |

## Utility and next work

The drive turns engine speed and loaded signed torque into exact PCM diagnostic measurements and
confirms that the existing passive oil-pressure/coolant dashboard foundation survives real driving
conditions. Once a passive torque encoding is independently solved, fresh time-aligned torque and
RPM can support an explicitly labeled **ECU-estimated crankshaft horsepower** value; it must never
be labeled wheel horsepower.

VVT oil temperature is now available diagnostically but is not a substitute for sump oil
temperature. Transmission temperature remains a high-priority TCM mapping task. The quickest next
AlfaOBD session should connect the installed ZF 948TE profile and record only transmission oil
temperature, turbine/input speed, output speed, current gear, and converter slip while the
listen-only PCAN capture continues.
