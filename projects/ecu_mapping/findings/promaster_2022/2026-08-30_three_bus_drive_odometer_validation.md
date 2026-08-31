# Three-bus drive and ICS odometer validation — 2026-08-30

## Outcome

The 2026-08-30 drive produced about 3.81 hours of finalized compressed raw
coverage on each permanent vehicle bus. Two synchronized capture sets
finalized successfully as complete three-role campaigns; later broker-status
attribution failures fragmented the recorder output, but every per-role chunk
still finalized as a complete compressed full stream with zero detected socket
drops. Treat the two capture sets as campaign-grade evidence and the 45 later
sets as individually usable fragments rather than lost data.

The fixed B-CAN ICS `22 2001` helper also passed its first production drive
test. Across the initial 25-minute interval it sent 302 exact physical
requests at 0.200 Hz and received 302 exact DLC-7 positive responses. The
decoded counter rose monotonically by 23.0 km / 14.291537 mi, while trapezoidal
integration of the independently decoded C-CAN vehicle-speed history over the
same timestamps produced 14.365140 mi. The 0.073603 mi difference is 0.512% of
the speed-integrated distance. This supports the documented `u24be × 0.1 km`
relative scale and shows that `2001` is a distance accumulator, while the prior
11.140 mi disagreement with the cluster display still blocks authoritative
absolute-odometer promotion.

A later post-key-cycle helper episode returned 53,308.677 mi. Relative to the
first 53,191.860 mi sample, ICS gained 116.817 mi; C-CAN speed integration over
the same multi-trip timestamp interval gained 116.511358 mi after excluding
eight greater-than-15-second observation/key-off gaps. The 0.305642 mi
difference is 0.262% of the ICS delta. This cross-key-cycle replication rejects
a merely stale startup copy and strongly supports a continuously accumulated
distance counter with a different absolute baseline or module-specific offset.

The owner reported 53,404 mi on the dash after the drive. The last recorded
passive `0x760` value was 85,913 whole km / 53,383.863239 mi at
`1788149091.268172`; C-CAN speed integration from that timestamp through the
last trip sample added 8.553372 mi. The resulting ICS/passive endpoint estimate
is 53,392.416611 mi, leaving the dash 11.583389 mi higher. The original parked
dash-minus-ICS difference was 11.140460 mi. Their 0.442929 mi difference is
consistent with accumulated speed-integration error over about 200.7 mi and
does not show material baseline drift. It remains inappropriate to silently
calibrate away the offset without identifying why the modules disagree.

## Capture inventory

Two complete synchronized sets:

| campaign | UTC interval | C-CAN frames | B-CAN frames | CAN-CH frames | detected socket drops |
|---|---|---:|---:|---:|---:|
| `broker-drive-20260830T233453172463` | `23:34:53–01:45:56` | 21,322,355 | 1,382,821 | 14,874,223 | 0 on every role |
| `broker-drive-20260831T023646810679` | `02:36:47–02:57:14` | 3,300,239 | 215,921 | 2,321,092 | 0 on every role |

The complete-set capture time is 9,089.281 seconds, about 2.525 hours. Their
`capture-set.json` SHA-256 values are respectively
`0295a9d65b7fc1819ad89fdfc6494067c3f45e04c161ab5685a383edf07c7796`
and
`a7cb464c7032a81d0713e2c7f6d135c5f4833e49a1e4df5c85dc0568cca17344`.

Including later fragments, manifest metadata totals:

| role | finalized chunks | capture hours | frames | compressed full-stream bytes |
|---|---:|---:|---:|---:|
| C-CAN | 63 | 3.805 | 36,868,602 | 318,065,873 |
| B-CAN | 63 | 3.808 | 2,407,997 | 16,612,065 |
| CAN-CH | 63 | 3.814 | 25,977,894 | 229,839,675 |

Forty-seven C-CAN, B-CAN, and CAN-CH recorder intervals reached a
`capture_end`. All 47 role-local streams reported `full_stream_complete=true`
and `detected_socket_drops=0`. C-CAN marked two intervals successful after the
tracked ignition ID disappeared; 45 ended with broker attribution errors (36
`EAGAIN`, nine timeouts). Their paired B-CAN and CAN-CH recorders all ended
successfully on the coordinated external stop. Thus only two directories have
a complete capture-set wrapper, but the compressed raw fragments are intact.

The recorder service exhausted its systemd restart limit after 46 restarts and
was failed/inactive when inspected after the drive. The broker and all three
interfaces were healthy, passive, and uninhibited. This is a recorder control-
plane availability fault, not missing CAN data; the service must be repaired
or explicitly recovered before relying on automatic capture for another drive.

## ICS wire and scale evidence

The initial three finalized B-CAN chunks contain:

- `18DA85F1`, DLC 4, payload `03 22 20 01`: 302 frames total;
- `18DAF185`, DLC 7, payload prefix `06 62 20 01`: 302 frames total;
- exact cadence: 0.200 Hz in each complete ten-minute chunk;
- first response: `06 62 20 01 0D 0F E8` at
  `1788132894.387561`;
- last response: `06 62 20 01 0D 10 CE` at
  `1788134399.383754`;
- first/last decoded values: 53,191.859540 and 53,206.151078 mi;
- 23 positive steps, 278 zero steps, zero negative steps, and maximum one-step
  increment 10 raw counts (1.0 km).

The one-kilometre update granularity explains the staircase without changing
the native tenths-of-a-kilometre representation. The close independent speed
integral establishes relative distance scaling; it does not establish which
module owns the legally/displayed absolute odometer or justify removing the
dashboard asterisk.

## Passive B-CAN carrier

Exact wire extraction independently recovered all 302 request/positive pairs
with no incomplete request or unpaired response. A coarse DID-to-broadcast
correlation then shortlisted B-CAN standard ID `0x760`, DLC 6. Its Motorola
`u17be@8` field ranged from 85,604 to 85,626—exactly the whole-kilometre form
of the simultaneous ICS counter.

The frozen, no-refit formula
`ICS_2001_tenths_km = 10 × 0x760_u17be@8` produced:

- 174 time-near matches and 57.616% coverage across the three-chunk discovery
  envelope;
- p95 absolute error 0 raw tenths-km counts;
- mean absolute error 0.1724 counts (0.01724 km);
- RMSE 1.3131 counts;
- signed error range `-10..0` counts, meaning the only mismatch was one
  one-kilometre update step attributable to independent message timing.

A chronological third-chunk-only extraction supplied a sparse five-match
holdout. Every error again remained either zero or one kilometre; the small
sample prevents calling that leg a high-coverage validation, but it does not
reject the frozen formula. Combined with the end-of-drive endpoint above,
`0x760` is a strong replicated passive ICS-local distance candidate. It is not
an authoritative vehicle odometer and is not yet permission to remove active
polling or the dashboard asterisk.

## CAN-CH speed/wheel shortlist

A new bounded cross-bus task used established C-CAN `0x101` speed as the
reference and examined only the seven verified CAN-CH-unique high-rate IDs.
The initial low-motion chunk put `0x0DC` first at R² 0.879 and 1.334 km/h
affine RMSE, so only `0x0DC` advanced to a 12/13/14/16-bit geometry search.
No geometry improved the result.

The independent continuously moving chunk rejected that lead: 59,994 matched
`0x0DC` frames peaked at R² 0.626 in the coarse pass and R² 0.620 in the
targeted pass, with about 22.0–22.1 km/h affine RMSE. Every other reviewed
unique high-rate ID was weaker (best next result R² 0.397). Therefore none of
the seven unique high-rate identities is promoted as a wheel-speed carrier.
Wheel speeds may use a shared/multiplexed identifier, a different field family,
or require controlled wheel-specific references; broadening the bit search is
not warranted from this result alone.

Historian speed integration over the three detected drive trips was 76.334,
40.177, and 84.162 mi (200.673 mi total), excluding four greater-than-15-second
gaps in the first two trips and none in the third. These are reconstructed
distance references, not replacements for the instrument-cluster odometer.

## Active-helper boundary

The first active C/B epoch ended at 00:00:03Z. Raw C-CAN evidence shows exact
positive PCM generator-duty, PCM torque, and RF-Hub pressure responses through
the final completed cycle; the final torque response arrived at
`00:00:02.321083Z`, after which no next RF-Hub request was emitted. C-CAN and
B-CAN both restored exactly and no operation inhibit was set. The B helper did
not fail its request/response protocol; it stopped because the parent C-CAN
epoch ended.

The original helper exception was overwritten in broker status, so the exact
first terminal cause remains unavailable. Host top-of-hour process load is
time-aligned, and subsequent development recovered transmit-permit latency
races, but those facts are supporting context rather than proof of this first
failure's exact exception.

## Provenance

Compute jobs:

- B-CAN chunk summaries: `20260831T013045Z-b2700eca`,
  `20260831T013045Z-de2bc796`, `20260831T013045Z-3dbce7a5`;
- exact 302-response ICS window: `20260831T013236Z-646e2438`, output SHA-256
  `eb4ec19b99074b4970f2d22bcdc7c93f1dff69648786384ba7fadf3d391b373b`;
- final C-CAN diagnostic window: `20260831T013236Z-b461eee7`, output SHA-256
  `1544b49ddff31960dbc0945a402c661efc05c09351a7c4a15c2ffe049fcbbc9e`.
- exact ICS wire: `20260831T055003Z-f7ed50dd`, wire SHA-256
  `99a00dc326950e42dee1f9517bdc958f17c955bfc4b8a97134cf76b36b6f77dc`;
- ICS coarse/passive correlation: `20260831T060235Z-590f8001`;
- `0x760` frozen-formula discovery evaluation:
  `20260831T060449Z-a652dc3f`;
- third-chunk wire and frozen-formula holdout:
  `20260831T060622Z-967be8ce` and `20260831T060650Z-f9a4b073`;
- CAN-CH unique-ID first-pass coarse/targeted jobs:
  `20260831T055004Z-2138d0e5` and `20260831T055226Z-5a5c5b0c`;
- independent moving-chunk coarse/targeted jobs:
  `20260831T055917Z-1c9f1037` and `20260831T055404Z-9101c9ff`;
- final passive `0x760` endpoint: `20260831T060729Z-8ff6802a`.

Raw capture output remains under the gitignored EXFAT archive tree. No CAN
frame was transmitted for this offline analysis.
