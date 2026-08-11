# PCM generator-duty direct-read recipe and passive-field follow-up

Date: 2026-07-30  
Vehicle scope: installed 2022 ProMaster 3.6 L PCM only  
Classification: exact physical diagnostic-read recipe verified; passive carrier unresolved

## Result

PCM DID `01A1` is directly readable without sending DiagnosticSessionControl when the
request uses fixed-DLC-8 zero padding:

```text
normal-fixed 29-bit physical addressing
tester -> PCM: 18DA10F1
PCM -> tester: 18DAF110
request:        22 01 A1, ISO-TP TX padding 00
response:       62 01 A1 80 00
decode:         raw_u16be * 100 / 32768 = 100.000000 %
```

Two independent one-request probes, each starting from and restoring a verified
500 kbit/s listen-only C-CAN interface, returned the exact response above. Neither
probe sent `10`, `3E`, a functional request, or any service other than physical
`22`. A preceding unpadded `22 01A1` attempt timed out and also restored verified
listen-only mode.

This establishes the minimum recipe observed in the current parked, engine-running
state as **padded `22 01A1`, with no explicit session change**. It does not positively
identify the inherited ECU session. No external PCM request or session-maintenance
traffic appeared during two subsequent five-second passive watches, but a future
post-ignition-cycle test is still the cleanest way to label the behavior
`default_session` rather than `inherited/unknown`.

Passive `0x0FC` samples immediately before the probes decoded to approximately
847–848 rpm. The interface had a usable same-boot C-CAN pins-6/14 topology record,
was `ERROR-ACTIVE`, and had zero TX/RX bus-error counters. `tpms-logger` and
`tpms-drivesniff` were inactive. The ordinary telemetry collector's observer lock
caused several pre-transmission deferrals; the probe waited for the exclusive lock
instead of racing it.

Gitignored evidence:

| artifact | SHA-256 |
|---|---|
| unpadded attempt summary | `1d5224e3d9ab29b9f7779a27826156ad85df3a38bad5687a8337875ffec7027f` |
| unpadded result | `9d40854bdab01139195cd05c79b9527560a3c498bc308ccb8ea502293be4256a` |
| first padded positive report | `c8c9ccaac8130d91d30e3868e4d2e7871cb4c5017e2dd53141cc6e17a0752e81` |
| second padded positive report | `60439989485a7004917d361cea8fb62f269115716249df3995fba248b84d8fce` |

## Coordinated live deployment validation

The merged broker-owned implementation first ran against the live vehicle on
2026-07-30. Qualified `0x0FC` evidence reported approximately 1,495 rpm before
the helper armed `can0`. The LAN dashboard then received a fresh
`generator.field_duty` observation from `pcm.did.01a1` at approximately one
hertz: the first checked value was 72.08251953125%, followed by sustained
100.000% samples during the observed high-idle interval.

The same exclusive interval continued all allowlisted powertrain broadcasts
and round-robin RF Hub reads. All four wheel pressures refreshed successfully.
PCAN remained 500 kbit/s and ERROR-ACTIVE with zero TX/RX bus-error counters
and zero interface drops during the checked interval. The standalone TPMS
service had already detected the broker and yielded without taking a CAN lock
or opening a diagnostic socket. Live post-engine-stop restoration remained
pending when this running-interval result was recorded.

### Concurrent same-adapter raw recording

The receive-only vehicle-data drive recorder was deployed during a later
running interval on 2026-07-30 without restarting the telemetry broker or
reconfiguring `can0`. Temporary raw `candump` coverage overlapped the service
restart. The hardened managed capture began at
`2026-07-31T00:17:04.935569Z` in the gitignored campaign
`/mnt/EXFAT512/obd-things/tmp/captures/ccan/broker-drive/broker-drive-20260731T001704814083/`.
Its manifest records the persistent receive command
`candump -L -D -d -r 16777216 can0`.

The managed compressed stream continued growing after the temporary recorder
stopped. It contains repeated local
`18DA10F1#032201A100000000` requests and matching `18DAF110` positive
responses, plus the broker's RF Hub `18DAC7F1` requests and `18DAF1C7`
positives. The first ten-minute rotation finalized 1,632,049 full-stream
frames and 814,831 priority-stream frames; both zstd streams passed an
independent integrity check.

| first finalized artifact | SHA-256 |
|---|---|
| `chunk_000000_full.candump.zst` | `c66b64229dd7fed4513fc60b780320ffa803c69b3a01d2228c19ac0a0a16308a` |
| `chunk_000000_priority.candump.zst` | `3508cab6f9e6dc4e64cf1672b7aede00d80247e090235d19ae818f7d8bd8a481` |

This proves that concurrent receive-side raw logging on the PCAN does not
consume the broker's bus diagnostic duty cycle or suppress local
transmit-loopback evidence. The dashboard simultaneously returned HTTP 200;
three checks showed fresh generator-duty values of approximately 47.516%,
49.243%, and 59.494%, while all four tire pressures remained fresh.

No candump drop marker appeared, interface RX/TX error and drop counters were
zero, and the cumulative arbitration-lost count remained unchanged at one
across deployment. The raw command does not subscribe to CAN error frames, so
those interface counters and drop markers—not an error-frame stream—are the
available loss/health evidence.

The campaign finalized successfully at `2026-07-31T01:09:26.666381Z` after
52 minutes 22 seconds. Its manifest contains six complete chunk records,
8,514,259 full-stream frames, 4,256,864 priority-stream frames, zero detected
socket drops, no error, `full_stream_complete=true`, and no remaining partial
file. The last chunk independently retained exact 121/121 PCM and 121/121 RF
Hub request/positive pairs. The normal stop reason was
`tracked_id_absent`: the last `0x2EF` was followed by the configured 20-second
key-off tail.

The lone runtime stderr line, `can0: interface down`, records the broker's
expected restoration transition. `candump -D` survived it and reached normal
finalization. Broker status subsequently reported
`restoration_failed=false`, no active operation inhibit, and a usable same-boot
C-CAN pins-6/14 topology; the adapter read back UP, 500 kbit/s, listen-only,
ERROR-ACTIVE, with zero interface RX/TX errors or drops. The enabled recorder
daemon returned to its broker-owned-drive wait without a child CAN process,
proving automatic rearming-to-wait. A new campaign will be created when the
next engine-running interval satisfies the same gates.

## Targeted passive-field search

The earlier coarse searches found no defensible passive carrier: the idle dataset
ranked `0x100` first at R² 0.7862291, while the loaded drive ranked `0x412` first
at R² 0.6079955. Because different identifiers led in different operating regimes,
the 2026-07-30 follow-up searched arbitrary unsigned DBC/cantools geometry only in
the exact streams `sff:100:8` and `sff:412:5`, using 8-, 10-, 12-, and 16-bit
lengths in both byte orders.

The targeted search did not rescue either candidate:

| dataset | best targeted result | R² | interpretation |
|---|---|---:|---|
| parked idle | `0x100`, `u16le@22` | 0.7899693 | only a negligible improvement over the coarse warm-up covariance |
| loaded drive | `0x412`, `u12le@28` | 0.6080081 | only a negligible improvement over the coarse result |

The leading identifier and field still fail to transfer across the independent
operating regimes. Both reports remain mechanically `candidate_only`, with scale
and physical identity unverified and telemetry promotion forbidden.

Freezing each best-fit formula and applying it without refitting to the other
independent dataset supplied the decisive counterexamples:

| frozen development formula | independent evaluation | coverage | RMSE | p95 absolute error |
|---|---|---:|---:|---:|
| loaded-drive `0x412 u12le@28` | parked idle | 100% / 732 samples | 8.004 percentage points | 14.806 percentage points |
| parked-idle `0x100 u16le@22` | loaded drive | 99.63% / 1,351 samples | 50.793 percentage points | 76.664 percentage points |

The conversions to percentage points use the established DID full-scale
relationship `percent = raw * 100 / 32768`. These are frozen-formula errors, not
refitted correlations. They reject both fields as transferable passive
Generator Duty Cycle representations.

Targeted-search reports:

| dataset | compute job | report SHA-256 |
|---|---|---|
| parked idle | `20260730T065837Z-a57b7270` | `fc47fff5e57f4b1f085ec43f2c3f2a9635595889e4bc512d5ba2006154d9eaff` |
| loaded drive | `20260730T065838Z-85c9f319` | `c3cd75f1569a83118af26f86f5ec3b07202035b9fbfebfa0a3e87b8f17adda13` |
| loaded `0x412` formula on idle | `20260730T070409Z-8b3b0b8e` | `01922f0df6d1a14add52297f87a5b929915ee9fd6eb958764279df878cedfc51` |
| idle `0x100` formula on loaded | `20260730T070409Z-197e1946` | `021f9e920836ef71a445f703224d06239b3cda7ef38d3ae7ca3ad6d065ad80a6` |

The practical telemetry path is therefore the guarded physical DID reader, not a
promoted passive frame. Generator duty remains a command/field-effort quantity,
not generator current or alternator temperature; thermal or output-current alerts
require separate evidence.

## Exact-wire follow-up across three automatic drive legs

The automatically rearming recorder completed three independent broker-owned
drive campaigns on 2026-07-31:

| campaign | elapsed | full chunks | full frames | priority frames | drops |
|---|---:|---:|---:|---:|---:|
| `broker-drive-20260731T001704814083` | 3,141.736 s | 6 | 8,514,259 | 4,256,864 | 0 |
| `broker-drive-20260731T012751675165` | 4,862.603 s | 9 | 13,193,470 | 6,594,053 | 0 |
| `broker-drive-20260731T030218233898` | 4,000.368 s | 7 | 10,848,178 | 5,422,418 | 0 |

Every chunk was complete, each capture ended successfully after the configured
20-second absence of `0x2EF`, and no partial file remained. This supplies
32,555,907 loss-accounted full-stream frames over approximately 3 hours
20 minutes while the broker polled PCM `01A1` at one hertz.

The existing four-chunk compute recipes constrained the first comparison to the
first 40 minutes of each leg. Exact PCAN-wire extraction recovered 2,400
request/positive-response pairs per leg, with no incomplete request or ignored
unpaired positive response. All 2,400 response timestamps in each leg linked
back to the exact global candump frame. The raw `01A1` ranges were sufficiently
varied for comparison: 9,235–29,311, 10,039–32,768, and 8,359–32,768
respectively (approximately 28.18–89.45%, 30.64–100%, and 25.51–100%).

The default broadcast-field pass again failed the independent-leg transfer
test. Different identifiers led each leg:

| leg | leading identifier and coarse field | R² |
|---|---|---:|
| first | `0x1F4 u32be:4` | 0.327594 |
| second | `0x0EE byte:0` | 0.616180 |
| third | `0x0F4 u13be-low5:3` | 0.763203 |

The best identifiers represented in all three top-100 lists were `0x545` and
`0x417`, but their minimum per-leg R² values were only 0.252667 and 0.238806.
Their fitted fields and/or affine scales were also not stable across the three
legs. A bounded 8-, 10-, 12-, and 16-bit unsigned DBC-geometry search was
therefore limited to those two identifiers; its results are recorded below.

Exact-wire extraction and coarse-correlation provenance:

| campaign | extraction job | wire SHA-256 | coarse job | report SHA-256 |
|---|---|---|---|---|
| `T001704814083` | `20260731T042852Z-f5c3465c` | `eecb197449620f407e6d3841fdf58e8c3624ad1f026a895305b0e306c6f0aaf7` | `20260731T043329Z-a41b3df2` | `6ded119677de9b49d9eb7cab5a56b6f3cc91e805d700480b6618ca6a300adda2` |
| `T012751675165` | `20260731T042852Z-eb46adf0` | `7a1fae99a0d782455f6c4f620ad6b37be2c38c5dbd355b5f0160e727adfe758d` | `20260731T043329Z-b4fa05ca` | `aebb243e07d2fe393750fbaecc226dd28d74dd05e890e5a60bdb0f9eb7d030f3` |
| `T030218233898` | `20260731T042853Z-2482604c` | `dbf97a2b417887a36259240f68b73ab7741e3b5db5f635bf70be4992b2ab8fdb` | `20260731T043330Z-f9f969a9` | `dbdf9a7101747d0ecc4489ab8ee3becc44dbefe82e97cecb7dda10df80066104` |

The targeted pass found no transferable `0x545` geometry. Its first-leg best
was `u16be@21` at R² 0.252667; no searched `0x545` field survived into the
second or third leg's top 100. For `0x417`, the same `u8be@18` geometry did
appear on all three legs, but its fitted relationship changed substantially:

| leg | R² | fitted raw scale | fitted raw intercept |
|---|---:|---:|---:|
| first | 0.238920 | 337.8370 | -9,751.397 |
| second | 0.508617 | 178.2601 | 6,189.624 |
| third | 0.712446 | 119.8043 | 11,348.684 |

That is covariance with changing operating regime, not a stable raw encoding.
The middle-leg `0x417 u8be@18` affine formula was frozen for no-refit scoring
against the first and third legs.

| campaign | targeted job | report SHA-256 |
|---|---|---|
| `T001704814083` | `20260731T043951Z-1ecc2d87` | `f44c6f3cd7efdd20c8fab24fd2fdf9c410d8ca3700f9592cb635a827323a9575` |
| `T012751675165` | `20260731T043951Z-55856e49` | `708421ad2910f32aca5732463e1cb5166fa8db32ce545c6565f26b3485f045a3` |
| `T030218233898` | `20260731T043951Z-050c0f5b` | `998bf12c84dc67246a73eed6655de1f41a0e48e0c64799640e126af3ef46674c` |

The no-refit result decisively rejected that apparent repeat:

| frozen development formula | independent leg | coverage | RMSE | p95 absolute error |
|---|---|---:|---:|---:|
| middle-leg `0x417 u8be@18` | first | 100% / 2,400 | 13.924 percentage points | 22.703 percentage points |
| middle-leg `0x417 u8be@18` | third | 100% / 2,400 | 12.832 percentage points | 22.459 percentage points |

The middle-leg raw formula was
`predicted_01A1_raw = 178.2600643012664 * candidate_raw + 6189.624278056941`.
Percentage-point errors use the established `01A1` conversion
`percent = raw * 100 / 32768`.

| independent leg | fixed-formula job | report SHA-256 |
|---|---|---|
| first | `20260731T044708Z-e612cf58` | `ac6306c1618d73f8642d1c10bba8a39fb2a0580734a6cd3258c38f4e438f56d6` |
| third | `20260731T044708Z-bd5b553d` | `d0c2e7d89305b13cb1909be6a87560ba5f5f04dfc6d8a3414551400184c45687` |

Therefore neither shortlisted identifier is a transferable passive
Generator Duty Cycle carrier. No passive frame is promoted. The operational
source remains the guarded, broker-coordinated physical PCM `01A1` reader.
This rejection is intentionally narrow: `0x417` and `0x545` remain valid
exploratory candidates for other related quantities or state transitions, and
the evidence does not classify either identifier as meaningless.
