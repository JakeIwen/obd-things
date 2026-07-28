# 2026-07-27 ZF 948TE Plots loaded-drive mapping

## Outcome

A current-vehicle AlfaOBD ZF 948TE Plots session and simultaneous listen-only
C-CAN capture established the selected gauge-to-DID associations and several
passive broadcast decodes. No Pi diagnostic traffic was sent while moving.

The strongest passive results are:

- vehicle speed: packed `0x101` field `/ 16 km/h`;
- corroborating vehicle speed: `0x0EE` bytes 0–1 `/ 128 km/h`;
- turbine speed: `0x1F7` bytes 4–5 `/ 2 rpm`;
- transmission output speed: packed 17-bit `0x1F7` field consisting of byte0
  bit0 followed by bytes 1–2, `/ 32 rpm`;
- TCM target crankshaft torque: `(0x100 bytes 3–4 >> 5) - 500 Nm`;
- maximum engine torque requested by the transmission:
  `(0x0F0 bytes 3–4 >> 5) - 500 Nm`.

The `0x101` speed field, both `0x1F7` shaft-speed fields, and target torque are
receive-only telemetry sources. `0x0EE` remains an independent corroborating
decode rather than a registered telemetry source. Target torque is a commanded
TCM quantity, not measured engine output, and must not be labeled as actual
torque or used to claim measured horsepower.

This development leg originally made `0x417` bytes 2–3 the leading
gearbox-oil-temperature candidate. Subsequent independent whole-drive evidence
has superseded that interpretation: two blind legs failed to reproduce the oil
association or scaling, and the second also failed to reproduce the first
blind leg's stronger chip-temperature covariance. `0x417` is not a
temperature telemetry source. The development observations below are retained
as provenance for that rejection; the current conclusion is in the
[`2026-07-28 packed-field benchmark`](2026-07-28_signal_field_engine_benchmark.md).

## Conditions and safety

- vehicle: installed 2022 Ram ProMaster 2500;
- PCAN: C-CAN DLC pins 6/14, 500 kbit/s, SocketCAN listen-only;
- diagnostic client: OBDLink MX+ through the parallel SGW-bypass splitter;
- AlfaOBD profile: `ZF 948TE 9 speed  Automatic Transmission`;
- selected Plots: 12 owner-priority speed, torque, and temperature gauges;
- moving work: passive PCAN recording only; no Pi UDS transmission, session
  change, routine, IO control, write, clear, or security request.

The first drive leg ran for 4,673.875 seconds and finalized on passive loss of
ignition frame `0x2EF`. Its checkpoint reports `success: true`,
`full_stream_complete: true`, and zero recorder-detected socket drops. The
kernel receive-drop counter stayed at its pre-existing value of two.

The continuation leg ran for 2,845.930 seconds and finalized by the same
20-second tracked-ID-absent rule. Its five full-stream chunks contain
7,701,316 frames; all ten full/priority zstd streams pass integrity checks,
every chunk hash matches the manifest, no partial file remains, and the
recorder again detected zero socket drops.

## Artifact provenance

Machine output remains gitignored:

| artifact | SHA-256 |
|---|---|
| EXFAT capture `run.json` | `fd57139c84d91f77bf7117df88f0ecf35da857f2572759c580a29fbfdd8a2fa5` |
| EXFAT capture `checkpoint.json` | `1ef9e69e952b58be0b87d615ca43e19eaded3a417694a36802b05ebedf02b2c6` |
| EXFAT capture `manifest.jsonl` | `474c4ab005a984f2922dd17a5332eb1016337324d8a116fb37601940d71ff4e3` |
| exact TCM wire stream, four initial chunks | `5dad6ee78aadd3dde6aae9e440ad58130d7a89111e0990b716c97e128b340b39` |
| pulled `Gauges_Data.csv` | `3d72c44934a64f9fc926f9fb334bc089326c8e39f91e3e81190b0e59d1630ec3` |
| pulled `AlfaOBD_Debug.bin` | `55b6b39ede323e8d752fb30a8056e12495403d7832e6b5a1b8d86bdde025946f` |
| decoded Alfa debug | `382b676658ef90d1378aa8ce42734be4571287ebf9e93e60f050b5bb1519458f` |
| section-4 gauge join report | `bda63344b5df0e92772b02ff4ed1adc907eaf6175d9275b773a9ef94d252cb1d` |
| continuation capture `run.json` | `c0f611c28eac0cbffa588889828f619be94f36bcc9eb14ae00b5d18479bc6fe4` |
| continuation capture `checkpoint.json` | `3e8fde1b91d92b9f08c586212064ac03a38c969a7878150e4a0d0202bdb81164` |
| continuation capture `manifest.jsonl` | `b482c54dcf3a51238bf3f94545ced4a28dcd175cb436ab0e8cf51064bdc53d1b` |

Relevant local paths:

- capture:
  `/mnt/EXFAT512/obd-things/tmp/captures/ccan/drive-correlation/pcm-plots-drive-20260728T002525Z/`;
- continuation capture:
  `/mnt/EXFAT512/obd-things/tmp/captures/ccan/drive-correlation/pcm-plots-drive-20260728T020124Z/`;
- exact wire and correlation reports:
  `tmp/ecu_mapping/tcm-drive-analysis/`;
- pulled and joined Alfa artifacts:
  `tmp/ecu_mapping/android_tablet/tcm-fuel-stop-20260727-1950/`.

`candump_diagnostic_wire.py` exact-linked 2,253 paired TCM exchanges in the
first four chronological chunks. The extracted stream contains 4,506 exact
request/positive-response wire rows and preserves the global raw-line
sequence, kernel timestamp, CAN ID, and payload linkage.

## Alfa gauge-to-DID proof

The cumulative Gauges file contains one explicit current ZF section. It has
168 complete 12-gauge rows plus short rows written after AlfaOBD stopped
cycling the full selection. The debug/gauge join inferred `22:2102` as the
unambiguous cycle boundary: all 387 gauge row timestamps were within 50 ms,
with a 1 ms median absolute gap.

Every varying selected gauge matched its named DID and native scaling with
zero fitted error across the 168 complete cycles:

| AlfaOBD gauge | DID | response decode |
|---|---:|---|
| Turbine speed | `2102` | `u16be × 0.25 rpm` |
| Engine speed | `F40C` | `u16be × 0.25 rpm` |
| Maximum Engine Torque Requested By Transmission | `101F` | `u16be - 500 Nm` |
| Actual Crankshaft Torque | `1018` | `u16be - 500 Nm` |
| Torque Converter Slip Speed | `0500` | signed `i16be rpm` |
| Gearbox output revs | `2103` | `u16be × 0.25 rpm` |
| Crankshaft Torque without TCU Torque Requests | `101A` | `u16be - 500 Nm` |
| Target Crankshaft Torque | `101B` | `u16be - 500 Nm` |
| Vehicle speed | `F40D` | `u8 km/h` |
| TCU chip temperature | `0301` | `u8 - 40 °C` |
| Gearbox oil temperature | `04FE` | `u8 - 40 °C` |

Transmission Torque Intervention `101D` stayed at the rendered sentinel
`1546 Nm`; a constant trace cannot map a broadcast field.

This proves the label/DID/scaling association for the installed TCM profile.
It does not by itself prove that every highly correlated broadcast field has
the same physical meaning; each passive promotion below also requires the
field layout, range, and near-exact loaded-drive relationship.

## Passive broadcast results

The correlation input comprises 169 samples for the slower 12-gauge cycle and
248 samples for the three gauges AlfaOBD continued polling. Timing offsets
below compensate only for asynchronous broadcast and diagnostic sampling.

| diagnostic reference | passive field | relationship in native terms | loaded result |
|---|---|---|---|
| `F40D` Vehicle speed | `0x101` packed `((b0 & 1) << 11) \| (b1 << 3) \| (b2 >> 5)` | `raw / 16 km/h` | 0–48 km/h; R² `0.9993655`; 0.161 km/h affine RMSE |
| `F40D` Vehicle speed | `0x0EE` bytes 0–1 BE | `raw / 128 km/h` | R² `0.9990360`; 0.198 km/h affine RMSE |
| `F40C` Engine speed | `0x0FC` bytes 0–1 BE | broadcast raw equals DID raw; both `/4 rpm` | R² `0.99999989`; 1.16 DID-raw-count RMSE |
| `2102` Turbine speed | `0x1F7` bytes 4–5 BE | broadcast `raw / 2 rpm` | R² `0.99999024`; about 2.79 rpm physical RMSE |
| `2103` Gearbox output revs | `0x1F7` byte0 bit0 then bytes 1–2 BE | packed 17-bit broadcast `raw / 32 rpm` | R² `0.99999578` and about 0.40 rpm physical RMSE below the high-bit transition; continuation-leg rollover validated the high bit |
| `101B` Target Crankshaft Torque | `0x100` bytes 3–4 BE | `(raw >> 5) - 500 Nm` | R² `0.99998794`; 0.104 DID-raw-count RMSE |
| `101F` Maximum Engine Torque Requested By Transmission | `0x0F0` bytes 3–4 BE | `(raw >> 5) - 500 Nm` | R² `0.99999986`; 0.061 DID-raw-count RMSE |

For `0x100` bytes 3–4, the low five bits were zero in the inspected matched
samples. For `0x0F0` bytes 3–4, the low five bits were `0x1F`; shifting right
five produced the exact integer DID domain. This is why the canonical decode
uses `>> 5` rather than a floating `/32` approximation.

The prior `0x100` bytes0–1 packed-13-bit torque lead is a separate field. It
continues to track torque, but this campaign does not make it interchangeable
with the newly resolved bytes3–4 TCM target-torque field.

The initial 169-sample `2103` diagnostic window reached only 1,442 rpm, so
byte0 bit0 remained zero and the generic byte-aligned correlator saw only
bytes 1–2. The continuation leg crossed the 16-bit boundary. One representative
frame was:

```text
0x1F7  01 6F A1 38 12 90 03 D0
```

Using only `0x6FA1 / 32` would incorrectly report 893 rpm. Including byte0
bit0 gives `0x16FA1 / 32 = 2,941.03 rpm`, mechanically consistent with the
simultaneous roughly 64 mph road speed. The earlier low-range frames used
byte0 bit0 as zero, while this high-range continuation used it as one; adding
that bit restores the mechanically plausible output-speed progression. The
telemetry decoder and its regression test therefore implement the full
17-bit field.

The separately finalized continuation capture provides another direct
boundary case. Its first sampled high-bit frame was:

```text
0x1F7  01 00 61 30 1E 9E 0A 5D
```

At the last sampled `0x101` speed of 67.4375 km/h, the full field reports
2,051.03 rpm. Ignoring byte0 bit0 reports only 3.03 rpm. Of 29,997 sampled
`0x1F7` frames in that chunk, 26,707 had the high bit set and the full-field
maximum was 3,544.03 rpm.

## Transmission-temperature lead

Gearbox-oil DID `04FE` varied from 35 to 43 °C. `0x417` bytes 2–3 BE ranked
first with R² `0.9466050`. Comparing native temperatures over a wider lag
search gives the compact candidate:

```text
gearbox_oil_temperature_C ≈ (u16be(0x417[2:4]) / 64) - 2
```

At a -2.91 second alignment its remaining centered RMSE is about 0.525 °C and
mean error is about 0.093 °C. The same field also covaries with TCU chip
temperature because both warm together, but requires about a 4.88 °C offset
to follow that gauge. This favors gearbox oil, but the narrow thermal range
and multi-second filtering make it a candidate, not a verified decode.

Next evidence should restart the same 12-gauge TCM Plots scan while fully
parked, then record a substantially broader cold-to-warm range. Do not expose
`0x417` as transmission-fluid temperature until that trace discriminates it
from TCU chip temperature and confirms the offset.

## Rejected or unresolved from this pass

- `0500` converter slip had no useful passive fit (best R² about `0.18`).
- `101D` was constant and therefore unidentifiable.
- `1018` actual torque and `101A` torque without TCM requests strongly
  covaried with `0x1F4`, `0x0FC`, and `0x100`, but none produced the exact,
  semantically safe passive identity needed for measured torque. In
  particular, related-platform evidence labels the `0x1F4` field as an engine
  torque request. Do not publish actual torque or derive horsepower from it.
- A pinned comma.ai Stellantis DBC supplies a useful but non-transferable
  structural lead: its
  [`ECM_1`](https://github.com/commaai/opendbc/blob/c3e6dca2e43d0620c7c31be1872823ed9d0c2c92/opendbc/dbc/generator/chrysler/_stellantis_common.dbc#L7-L11)
  frame defines packed effective and expected engine-torque fields beside
  engine RPM. Applying that layout to current-van `0x0FC` bytes 2–3 and
  optimizing lag against TCM `1018` reached only R² about `0.898`, with a
  non-unit slope and large offset. The related DBC uses a different CAN ID and
  does not override exact-vehicle evidence; `0x0FC` torque remains unresolved.
- `0301` TCU chip temperature and `04FE` gearbox oil temperature rose
  together; this leg therefore retained a bounded `0x417` oil-temperature
  candidate at the time. Independent blind evidence later rejected it, as
  noted above.
