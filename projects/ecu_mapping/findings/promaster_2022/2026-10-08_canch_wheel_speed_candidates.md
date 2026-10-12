# CAN-CH wheel-speed candidate screen — 2026-10-08

## Result and boundary

CAN-CH standard-ID `0x0EE`, DLC 8, contains four strongly speed-correlated
13-bit fields at the exact positions described as wheel speeds in the related
FCA Giorgio DBC. Two discovery recordings and three further complete drives
support pursuing this hypothesis. The frozen `raw / 16 km/h` formula now has
independent whole-drive error and standstill checks; no per-drive fit is used
in that follow-up. Steering also has repeatable internal consistency evidence.
The fields remain **exploratory candidates**: wheel-corner assignments, exact
physical scaling, invalid values, standstill/reverse behavior, and a production
decoder are not established. No dashboard metric or alert was added.

The initial work used saved recordings; the later owner-assisted parked pass
below recorded live traffic in listen-only mode. No CAN request, link change,
service restart, or storage reconfiguration was performed.

## Candidate authority

Source: [opendbc FCA Giorgio definition, pinned revision](https://github.com/commaai/opendbc/blob/a894cc9e29f36c115874959db196ec902d159b8f/opendbc/dbc/fca_giorgio.dbc).
Downloaded October 8; SHA-256:
`d327a7a07670016f4ecf56ae200e0239705017888134274c34fc3151d7c0e96d`.
The source and declaration made before the analysis are retained under
`tmp/ecu_mapping/can-ch-telemetry-20261008/`.

The four DBC/cantools Motorola fields are unsigned, length 13, with start bits
`7`, `10`, `29`, and `32`. Giorgio labels them FL, FR, RL, and RR, respectively,
and specifies `raw × 0.017 m/s` (`raw × 0.0612 km/h`). Those labels and that
scale are related-platform hypotheses, not current-vehicle facts.

The September summary previously established matching frame lengths for other
Giorgio leads: `0x0DE` steering, `0x0FA` brake switch, `0x0FE` dynamics, and
`0x106` EPS status. This screen evaluates only `0x0EE`; it does not validate
those other fields. The Giorgio source itself marks acceleration scaling TBD
and yaw scaling estimated.

## Method and evidence

The new named offline task `can-cross-bus-speed-canch-0ee-bits` reuses
`tools/can_cross_bus_speed.py` and `lib/signal_fields.py`. It screens 13-bit
geometry only on `0x0EE`; the four vendor fields were selected before examining
either result. The output retains all eligible fields, including equivalent
shifted views; ranking alone cannot choose a physical bit boundary.

Reference: established C-CAN `0x101` speed, nearest timestamp within 20 ms,
minimum 5 km/h. `can0` and `can2` are the channel names inside these historical
files, not live routing instructions. Reported errors below are **separately
fitted affine errors** against vehicle speed, not errors of the vendor formula
or a frozen production decoder. Vehicle speed is another vehicle-network
measurement, not independent wheel instrumentation.

All inputs are finalized `chunk_000000_full.candump.zst` files from paired
`c-can/` and `can-ch/` directories below the archive's
`tmp/captures/three_bus_drive/broker-drive/`:

| Recording | Scope | Matched candidate frames | Reference range |
|---|---|---:|---:|
| `broker-drive-20260922T042152731314` | first 600 seconds of a longer drive | 56,569 | 5–105.5 km/h |
| `broker-drive-20261008T002931395579` | complete approximately 297-second recording | 12,885 | 5–24.9375 km/h |

Both roles' end checkpoints report successful complete full streams and zero
detected socket drops. The October analysis excludes below-5-km/h periods even
though its input covers the whole recording. Matching may reuse a reference
sample; matched counts are not independent physical observations or a general
coverage score.

| Motorola start bit | September R² | September RMSE, km/h | October R² | October RMSE, km/h |
|---:|---:|---:|---:|---:|
| 7 | 0.999961 | 0.101 | 0.996708 | 0.316 |
| 10 | 0.999964 | 0.097 | 0.996691 | 0.317 |
| 29 | 0.999946 | 0.119 | 0.990250 | 0.544 |
| 32 | 0.999835 | 0.207 | 0.995643 | 0.363 |

September fitted slopes span 0.06239–0.06280 km/h per count, close to 1/16;
October slopes span 0.06063–0.06244. Offsets and possible turning differences
prevent promoting either a fitted scale or the vendor's 0.0612 factor from
these fits. Neither recording supplies independently labeled wheel corners.

Successful jobs and report SHA-256 values:

- September: `20261008T070655Z-32acb006`,
  `0497a930b49873ae936911132b3a7cfa6646ad986bbba277ab2c3738ec36e1ce`.
- October: `20261008T070330Z-c3d88bcc`,
  `515d1fbf10b293ccbc8cdf85e0e534c0a303174b298b8163fcab094839d220a8`.

Each report and queue manifest retains both input hashes. The earlier
September job `20261008T070329Z-3b7ff7a5` failed because the worker resource
watchdog's `ps` call timed out; it produced no correlation result and is not
vehicle evidence. The successful retry used unchanged input hashes and task.

## Frozen-formula checks on three other complete drives

`projects/ecu_mapping/canch_validation.py` is a separate offline entry point,
run through the named `canch-fixed-candidate-validation` task. It reuses the
existing strict timestamp/channel parser, shared bit geometry, bounded fixed
residual statistics, input hashing, and atomic report writer. It imports no
live CAN owner. The companion named test task is
`canch-fixed-validation-tests`; its current eight tests and two subtests pass,
including full-rate pulse preservation and held-angle gaps.

The declaration saved in `fixed-validation-plan.json` preceded these runs:
`raw / 16 km/h` and the vendor's `raw × 0.0612 km/h`, with no fitting. It scores
the first reference observation in each 100 ms epoch bucket against the
nearest full-rate candidate within 20 ms. Full-rate stream summaries separately
count message lengths, exact zeros, all-ones words, values above 4160, and gaps
over 100 ms. A suspect word is retained in counts and excluded from error
scores. That exclusion is a stated plausibility policy, not a proven ECU
invalid-value definition.

All three complete C-CAN/CAN-CH capture sets report successful full streams
and zero detected socket drops. They total approximately 49.6 minutes:

| Complete recording | Scored references | Moving references, ≥5 km/h | Maximum reference speed |
|---|---:|---:|---:|
| `broker-drive-20261007T225646470538` | 12,389 | 9,131 | 47.8125 km/h |
| `broker-drive-20261007T234445564677` | 6,427 | 4,441 | 50.1875 km/h |
| `broker-drive-20261008T032157578587` | 10,914 | 8,806 | 98.5 km/h |

Frozen `/16` moving errors below are ordered by Motorola starts `7/10/29/32`:

| Recording start, UTC | RMSE for the four fields, km/h | p95 absolute error, km/h |
|---|---|---|
| Oct 7 22:56 | 0.237 / 0.241 / 0.379 / 0.307 | 0.688 / 0.688 / 0.438 / 0.438 |
| Oct 7 23:44 | 0.317 / 0.337 / 0.628 / 0.314 | 0.812 / 0.875 / 1.438 / 0.750 |
| Oct 8 03:21 | 0.237 / 0.239 / 0.379 / 0.370 | 0.625 / 0.625 / 0.688 / 0.688 |

Every scored reference, including standstill and creeping, matched each wheel
field: 100% temporal coverage across 29,730 timestamps. No tested wheel stream
had wrong DLC, an all-ones raw value, raw above 4160, or a gap over 100 ms.
With aggregate reference speed ≤0.1 km/h, the p95 wheel discrepancy was zero
on all three drives; brief nonzero wheel values still occurred at that
aggregate standstill threshold and are preserved in the reports.

The vendor factor produced larger RMSE for **every field on every drive**:
0.587–1.359 km/h versus 0.237–0.628 for `/16`. This favors `/16` against our
established aggregate-speed reference. Wheel differences during turns are not
sensor-error proof, and neither this comparison nor a high fit determines
physical corner labels or absolute wheel instrumentation calibration.

### Steering, braking, and freshness

- Giorgio `0x0DE` angle (`u14be@5 × 0.1 −716.8`) differentiated over 50–150 ms
  tracks its separate rate (`u12be@19 × 0.5 −1000`): Pearson r is
  0.98964, 0.98821, and 0.98541 across 3,338 / 2,422 / 4,266 changing-angle
  comparisons. This supports the pair's relative encoding, not the physical
  sign, absolute angle, or zero offset.
- At 5–40 km/h and candidate absolute angle ≥30, angle correlates with the
  vendor front/right-minus-left difference at r 0.905–0.930, with its rear
  difference at 0.917–0.947, and with candidate yaw at −0.948 to −0.904.
  These are mutually consistent **candidate** signals, not independent
  physical references or proof of left/right naming.
- `0x0FA` byte 0 bit 3 is a changing binary field on all three drives, with
  65/40/69 sampled rising transitions. It is often set during stops and also
  changes while moving. This extends the existing brake-associated lead;
  independent pedal/parking-brake discrimination remains pending. The 10 Hz
  analysis can miss short pulses.
- `0x0DE` stops approximately 19 seconds before the aggregate-speed stream at
  each recording's shutdown tail. Future acquisition must age steering
  independently; ongoing wheel/speed traffic cannot refresh a steering value.

Successful validation jobs and report SHA-256:

- `20261008T090252Z-a0ad6bce` (22:56),
  `52c80b1a219f7ebd3e0eb0c03ace2b01a14a10d28f1a06741aaf3baaa5b7cbe3`.
- `20261008T090253Z-864c194d` (23:44),
  `af5bec0625f4c811a15fa71a0a12a8feaf8a82c59b70de2ac2bebb3ffb903d14`.
- `20261008T090254Z-cdcb246c` (03:21),
  `c277da9b0614cb0f4032fd110e76fc7887b0626d79a845fd80c3d4e7aff73e5e`.

## Owner-assisted parked reference, October 8 09:17–09:24 UTC

The owner confirmed stationary in Park, ignition on, engine off, with limited
movement without steering/brake boost. The maintained role-aware
`tools/three_bus_capture.py` recorded a bounded six-minute session, with
one-minute chunks, using shared passive role/channel ownership. Broker and
automatic recorder remained active; their active-drive helpers were idle.

Evidence directory:
`tmp/captures/canch_validation/session_20261008T091756.763123Z_2/`.
It contains raw role-separated candump files, per-chunk routing/loss metadata,
`owner-references.json`, `preflight.json`, and `postflight.json`. All three
vehicle roles stayed classical listen-only/ERROR-ACTIVE; every adapter's TX
counter was unchanged (C-CAN 10,735, B-CAN 871, CAN-CH 7, spare 0). The capture
reported zero dropped frames and no retryable errors. There are five explicit
approximately 0.23–0.39-second chunk-rotation gaps; neither the pedal sequence
nor the directional steering holds straddles a gap.

### Service-brake press/release feedback

The owner reported three gentle pedal presses, each followed by release, then
foot off; the parking brake was held unchanged. CAN-CH standard `0x0FA`, DLC 8,
**byte 0 bit 3** produced exactly three rising and three falling edges:

| Press, UTC | Release, UTC | Observed hold |
|---|---|---:|
| 09:21:03.819393 | 09:21:06.289477 | 2.470 s |
| 09:21:07.669395 | 09:21:10.239456 | 2.570 s |
| 09:21:11.999380 | 09:21:14.329335 | 2.330 s |

Each edge is bracketed by adjacent raw frames approximately 10 ms apart. The
bit was zero before and after the sequence. Together with the related-platform
pedal-switch definition and the earlier C-CAN controlled brake association,
this **verifies CAN-CH service-brake press/release feedback under the tested
parked conditions**. It is not a pressure measurement or an assertion about
automated braking/ABS intervention. Parking-brake-only behavior was not tested.
Byte 0 bit 2 also followed all three presses, usually rising 40–80 ms later;
its proposed pressure-threshold meaning remains unverified.

### Steering direction and rough angular scale

The owner reported the wheel turned left, then performed the requested small
counterclockwise/return/clockwise/return sequence, estimating about 80 degrees
each way. The owner reply arrived after capture end, but the recorded holds
and final return all lie inside the captured window:

| Recorded phase | UTC held interval | Candidate angle |
|---|---|---:|
| Before directional movements | 09:22:58.528–09:23:23.356 | about +75.6° |
| Counterclockwise / left hold | 09:23:27.246–09:23:31.206 | about +166.2° |
| Clockwise / right hold | 09:23:39.696–09:23:41.876 | about +14.3° |
| Final return | 09:23:45.415–09:23:56.915 | about +80.7° |

This verifies that the `0x0DE` field responds to physical steering, with
counterclockwise increasing and clockwise decreasing. Its approximately 152°
total excursion is consistent with the owner's rough 160° estimate across
both directions. The movements and return were approximate, not instrumented;
they do **not** prove the exact 0.1-degree scale or the −716.8-degree offset.
No centered-wheel zero reference was supplied. The labeled directions describe
movement from the starting position; absolute position relative to physical
center was not independently observed. Preserve that distinction when
considering steering-center calibration.

### Stationary wheels and reproducibility

All four wheel fields were exactly raw zero for 35,840 received frames each;
the C-CAN speed reference was zero throughout its 35,842 frames. This adds an
independently confirmed parked zero reference, not corner identity or reverse
direction evidence.

Full-rate edge brackets and stable-angle intervals are now emitted by the
offline checker; holds split at capture gaps. Final reference-events analysis:
`20261008T093014Z-2de049ac`, report SHA-256
`76852bba5bb681cf2659b48d01e8aa5929528222b8fbcc7a1fdcd3b5d3ff57b6`.
Focused regression job `20261008T093013Z-31b46dca` passed eight tests and two
subtests. Earlier aggregate analysis `20261008T092533Z-633c53b7` independently
counted the same three pedal cycles. No production metric was enabled.

## Remaining validation and integration

1. Establish corner assignments and steering sign/scale with independent,
   labeled references. A normal maneuver recorded with known turn direction
   can discriminate hypotheses; it does not by itself prove every corner or
   absolute steering angle. Avoid requiring driver interaction with logging.
2. Steering movement direction and parked pedal feedback are established by
   the owner pass above. An independently centered wheel and a more precise
   angular reference remain useful before treating the proposed angle as
   calibrated. Physical pressure and dynamics need their own scaling evidence.
3. Check reverse and independently confirmed standstill behavior; the saved
   speed-only regimes do not identify transmission direction. No observed
   sentinel is not proof that one can never occur.
4. Only qualified sources proceed to fixed receive-only decoders, source and
   freshness definitions, cooperative CAN-CH acquisition, history and UI.
   A useful approximate display would instead need the explicitly labeled
   operational-proxy policy and independent frozen-formula tolerances.

The concrete acquisition design is a filtered receive-only CAN-CH feed under
the existing shared serial/role/channel ownership, yielding to a scoped parked
diagnostic owner. Keep kernel reception timestamps, exact SFF/DLC checks and
per-signal expiry; coalesce updates for the dashboard without replacing receipt
times. Hardware setup, an ABS diagnostic session, wake traffic, and a new
always-armed helper are not prerequisites for this passive path. Physical
labels remain the admission boundary before ordinary dashboard registration.

The earlier `0x0DC` rejection remains valid for its tested wheel-speed
hypothesis. That investigation covered the seven CAN-CH-unique high-rate IDs;
`0x0EE` is a different, shared-ID lead and was outside that shortlist.
