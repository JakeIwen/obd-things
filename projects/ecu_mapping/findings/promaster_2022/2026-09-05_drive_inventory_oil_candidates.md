# New drive evidence and engine-oil candidates — 2026-09-05

Implementation follow-up: the owner authorized all three next items. Commit
`3cc8c4f` now repairs typed recorder recovery, provides the bounded two-request
parked support checker, and integrates `engine.vvt_oil_temperature` into the
guarded running poller, history, and telemetry gauge. It was deployed asleep
at 02:42 MDT with zero CAN TX change. Live `F45C` and direct no-session `069F`
validation remain pending an ignition-on/engine-off vehicle window; the
research evidence below does not claim those checks occurred.

## Recording inventory

The archive contains 14 new broker-drive intervals since the August 31 recorder
recovery, dated August 31, September 1, and September 4 in US/Mountain time.
Their UTC directory names start with `broker-drive-202609`. Inventory was based
on the small role-local checkpoints and chunk manifests, not a full decode of
every raw stream.

| Recorded role | Summed coverage | Full-stream frames | Compressed full-stream bytes |
|---|---:|---:|---:|
| C-CAN | 3.753924 hours | 36,487,593 | 329,524,862 |
| B-CAN | 3.755406 hours | 2,387,561 | 17,228,763 |
| CAN-CH | 3.755926 hours | 25,508,420 | 237,117,194 |

Ten synchronized capture sets have `complete=true`, totaling about 1.078 hours
of C-CAN coverage. Four intervals lack a completed capture-set wrapper because
the C-CAN safety check lost broker attribution. All 42 role-local checkpoints
report `full_stream_complete=true` and zero detected socket drops. The failed
wrappers must remain failed; their finalized chunks are still useful bounded
evidence and do not establish uninterrupted coverage across recording gaps.

All paths below are relative to
`/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/`:

| Campaign suffix after `broker-drive-` | C-CAN minutes | Complete set |
|---|---:|---|
| `20260901T003917318835` | 5.78 | yes |
| `20260901T014728249587` | 12.85 | no |
| `20260901T171128913051` | 0.56 | yes |
| `20260904T213535161749` | 12.27 | yes |
| `20260904T215625644220` | 3.84 | no |
| `20260904T224042405771` | 11.66 | yes |
| `20260904T235052350074` | 8.92 | yes |
| `20260905T000925403295` | 10.86 | yes |
| `20260905T011325152755` | 6.49 | yes |
| `20260905T012307765665` | 37.19 | no |
| `20260905T021334472723` | 106.69 | no |
| `20260905T040352934949` | 5.17 | yes |
| `20260905T041004365974` | 0.62 | yes |
| `20260905T042012633188` | 2.35 | yes |

At inspection the mount was writable, with about 42 GiB free. The recorder was
active/waiting and had three restarts during the current boot. The broker was
active, both helpers idle, the vehicle classified asleep, and all three roles
were classical CAN, listen-only, ERROR-ACTIVE, with `restart-ms 0`.
This investigation performed no vehicle transmission or service change.

## Independent passive odometer replication

The 106.69-minute interval `20260905T021334472723` is independent of the
[August 30 development evidence](2026-08-30_three_bus_drive_odometer_validation.md).
Its 11 finalized B-CAN full chunks supplied 482 exact ICS `22 2001` exchanges,
with no incomplete request or unpaired positive. The responses cover
September 4 20:13:36–20:53:41 MDT at a mean interval of 5.000129 seconds.
The counter rose monotonically from 860,230 to 860,980 tenths-km (75 km /
46.602839 miles), with 76 distinct values and no backward step.

The field and formula were frozen before this test:

`ICS_DID_2001_raw = 10 * B-CAN_sff_760_DLC6_u17be@8`

The result was 446 time-near matches out of 482 references (92.531% coverage,
100 ms maximum match radius). Of those, 435 matched exactly and 11 differed by
one kilometre. In native DID raw units: MAE 0.246637, RMSE 1.570467, p95
absolute error 0, signed error range -10..0. No refitting was performed.
This strengthens the passive ICS-local-distance candidate considerably; it
does not resolve the previously observed approximately 11-mile cluster offset
or promote the value to authoritative odometer mileage.

ICS polling stopped after about 40 minutes, while raw B-CAN recording continued
for the remaining interval. The broker journal records B-CAN restoration at
20:53:46 MDT. Its precise termination reason was not recovered in this pass.
C-CAN PCM polling continued in the later sampled windows. The evidence supports
independent B-CAN failure containment, not continuous ICS polling for 107 minutes.

Compute jobs and reproducibility:

- `20260905T081904Z-a3f0a3d4`: exact ICS extraction from all 11 full chunks;
  `ics_bcan_wire.jsonl` SHA-256
  `875a73fe2425467746e05c8cc5726ada6f3429fdc10fa0574a7cd7dc0c1aae6a`.
- `20260905T082029Z-66f56c8f`: frozen `sff:760:6=bits:big:8:17:unsigned`,
  multiplier 10, intercept 0; all 482 references linked back to exact raw
  frame identity, timestamp, sequence, and payload. `report.json` SHA-256
  `2fe1e698e1631d980b7c057a2f1368d145e833532c8bebf412f73beaa5d6a0a2`.

## PCM evidence and oil-temperature direction

An identifier-filtered extraction of C-CAN priority chunks 0, 4, and 10 from
that same interval covered about 26.7 recorded minutes spread across its beginning,
middle, and end. It recovered 1,601 complete `01A1` pairs and 1,601 complete
`06DA` pairs, with no overlapping/unpaired exchange and 8.477 ms maximum
observed response latency. Generator duty ranged 7.996–86.523%; current torque
ranged -59.72 to 281.88 Nm. No other PCM request or response appeared in those
sampled chunks. This is useful fresh excitation for the existing quantities,
but supplies no new oil-temperature reference or DID discovery by itself.

The successful event-window job was `20260905T082047Z-8107828d`, selecting only
29-bit `18DA10F1` and `18DAF110`; `window.json` SHA-256
`b8897894a310ca80ecf84309a5abdfd6b43ab97c5e2716492f91181d184497ef`.
The preceding fixed four-chunk PCM task `20260905T081905Z-561f89ba` failed
before analysis because its recipe lacks the now-required `--capture-channel`.
That is a compute-recipe defect, not negative vehicle evidence; its failed
output was not used.

Two oil-temperature avenues remain distinct:

1. **Supported PCM `069F`: VVT Oil Temperature**, `u8 - 64 degrees C`.
   The [loaded Alfa/wire mapping](2026-07-27_pcm_plots_loaded_drive_mapping.md)
   established 55–96 degrees C, and the
   [legacy PCM overlap](2026-07-30_legacy_pcm_cda_overlap.md) independently
   corroborates the label and conversion. A useful next feature is to expose
   this under its actual VVT label after checking the direct no-session-change
   recipe. Its exact sensor/model relationship to bulk engine oil is still
   unresolved; it must not silently become a measured sump-temperature gauge.
2. **Untested standardized candidate `F45C`: Engine Oil Temperature**.
   Au Group Electronics' own
   [OBD simulator manual, revision C, table 2-2, page 12](https://auelectronics.com/downloads/usermanual_simobd2can.pdf)
   pairs classic PID `5C` with service-22 identifier `F45C`. This establishes
   an OBD-over-UDS candidate namespace, not support in this 2022 legacy PCM.
   No prior `F45C` test was found in the maintained current-vehicle maps.
   A future bounded physical support check is reasonable; the already-known
   failure of Mode 01 behind the bypass does not establish either success or
   failure of physical `22 F45C`. No such request was sent in this pass.

Do not recycle `3159`, `315A`, `B010`, `B011`, or `B012`: their actual negative
support results remain in the
[priority roadmap](2026-07-25_priority_telemetry_targets.md).
An additional OEM-corpus search was queued as `20260905T081914Z-a71b0b74` but
had no result at this checkpoint; no conclusion here depends on it.

## Recorder and shutdown-voltage follow-ups

The four interrupted intervals ended with
`armed interface is not owned by the reviewed broker active-drive interval`.
September 4 restarts occurred at 16:00, 20:00, and 22:00 MDT; the earlier
August 31 interruption also occurred around 20:00. Their alignment with the
hour is evidence for a future scheduling investigation, not a proven cause.

The August 31 recovery fix remains incomplete: the safety callback raises
`BrokerOwnershipLost`, but `tools/passive_drive_capture.py` ends a failed run
with `raise CaptureError(str(fatal)) from fatal`. The broker recorder propagates
that wrapper, and its daemon catches only a directly raised
`BrokerOwnershipLost` for in-process recovery. Thus the typed recovery signal
is lost at the capture boundary. The next repair should preserve or explicitly
recognize the typed cause after successful cleanup, with a regression through
the real recorder boundary. Other storage/interface failures must remain fatal.

The engine-off-voltage feature did produce its first checked live artifact:
`/var/lib/van-telemetry/engine-off-voltage.json` records a verified passive C-CAN
`0x41A` sample of 13.0 V at `2026-09-05T04:22:43.509905Z`, following the engine
stop at `04:22:15.045490Z`. It was saved at `04:22:45.647974Z`; broker capture
status was `complete` without error. This validates the passive shutdown-tail
capture, not a fully rested battery measurement.
