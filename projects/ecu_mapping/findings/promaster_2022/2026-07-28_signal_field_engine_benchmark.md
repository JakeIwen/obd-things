# 2026-07-28 packed-field engine and whole-leg benchmark

## Scope and safety

This was offline-only work against saved C-CAN captures and exact extracted
diagnostic wire rows. No CAN interface, vehicle service, ADB session, or live
diagnostic request was opened. Every correlation result remains
`candidate_only`; none of this work promotes a new telemetry metric or scaling.

The implementation deliberately stops at the first three phases of the current
mapping doctrine:

1. a dependency-free DBC/cantools Intel/Motorola field engine;
2. bounded arbitrary-bit refinement plus provenance-bound whole-leg
   benchmarking;
3. an opt-in operating-regime slice for the unresolved actual-torque question.

OCR/reference ingestion and automated DBC/template export remain out of scope.
The affine scorer is unchanged, and no sentinel, Spearman, calibration, or
time-series null framework was added.

## Field engine and correlator changes

`lib/signal_fields.py` now implements 1–32-bit unsigned/signed extraction and
insertion using explicit DBC/cantools start-bit conventions. It covers Intel
little endian and Motorola sawtooth big endian. The established Stellantis
packed layouts reduce to ordinary geometry:

- low-five-bits-plus-next-byte 13-bit field: Motorola start bit `8N + 4`,
  length 13;
- low-bit-plus-next-word 17-bit field: Motorola start bit `8N`, length 17.

The full DLC-8, both-order, both-signedness profile has 5,632 unique value
geometries after equivalent definitions are de-duplicated. Fixed
cantools-generated boundary fixtures always run; the optional research
environment additionally passed 5,000 deterministic randomized two-way
cantools cases and every 1–32-bit order/signed boundary combination.

The existing correlator keeps its 39-field DLC-8 coarse profile by default.
Arbitrary geometry is opt-in for at most two exact
`channel / SFF-EFF / CAN ID / DLC` streams and is capped at 6,000 fields per
stream. Exact PCAN response timestamps remain the primary reference. Reports
now expose maximum staleness, observed DID polling cadence, complete
candidate-only flags, and the maximum eligible R² before top-N truncation.

`tools/can_signal_benchmark.py` does not run searches. It validates tracked
whole-drive cases against a matching successful `pi_compute` manifest,
including ordered compressed-input hashes, staged report source paths, module,
DID, reference field, exact wire linkage, search profile, staleness, and
candidate-only gates. The supplied report's exact byte SHA-256 must equal the
compute manifest's `report.json` result digest. Development, validation, and
blind sets may not reuse artifacts across splits, including aliases with a
different artifact ID but the same normalized path hint or pinned digest.
Empty eligible sets satisfy a `no_defensible_match` negative control, while a
null maximum paired with candidate rows is rejected. Regime configuration
validation also projects all candidate fields across all five regimes and
rejects combinations that could exceed the runtime state cap.

## Known-field benchmark results

All four completed known-field cases passed their tracked gates:

| reference | expected passive field | rank | coverage | R² | result |
|---|---|---:|---:|---:|---|
| PCM `01D5` engine speed | `0x0FC`, DLC 8, `u14be@7` | 2 | 0.996313 | 0.9999794279 | pass |
| TCM `F40D` vehicle speed | `0x101`, DLC 8, `u12be@0` | 5 | 1.000 | 0.9993328750 | pass |
| TCM `2102` turbine speed | `0x1F7`, DLC 8, `u16be@39` | 5 | 1.000 | 0.9999805746 | pass |
| TCM `101B` target crankshaft torque | `0x100`, DLC 8, `u11be@31` | 2 | 1.000 | 0.9995522578 | pass |

The vehicle-speed result also illustrates why rank alone is not geometry
proof: shifted 12-bit views ranked slightly higher during this limited range,
while the established `u12be@0` field retained the expected approximately
`1/16` raw slope. The exact target-torque geometry had an approximately
unit-scale slope (`1.010612`) to the TCM DID raw value.

The benchmark is intentionally incomplete. The cluster two-chunk task fixes
its older coarse DID-1000 invocation and cannot express the packed RPM case.
The validation continuation and 72-/45-minute blind legs also lack extracted
exact TCM wire artifacts and approved five-/eight-chunk compute tasks.

## Transmission-temperature challenge

The development leg contains 169 exact positive samples for each TCM
temperature DID. Testing the same `0x417` bytes2–3 field (`u16be@23`) produced:

| diagnostic reference | reference raw range | field rank | R² | affine RMSE |
|---|---:|---:|---:|---:|
| `04FE` gearbox oil | 75–83 | 7 | 0.9466050403 | 0.4221 raw count |
| `0301` TCU chip | 72–81 | 1 | 0.9637763021 | 0.4308 raw count |

The chip-temperature reference is the stronger zero-lag affine match on this
leg, while earlier lag-aware work favored a plausible oil-temperature
formula. Both temperatures warm together. The correct conclusion is not that
`0x417` is chip temperature; it is that the development evidence cannot
identify `0x417` as gearbox oil. The tracked benchmark now pairs `04FE` and
`0301` on every planned leg, and `0x417` remains unpromoted.

## Actual-torque Phase-3 gate and regime result

The coarse DID `1018` search supplied the concrete reason to add the regime
slice. Across all 169 exact samples, its leading passive families were:

| passive family | representative field | R² |
|---|---|---:|
| `0x0FC` | bytes5–6 / overlapping views | 0.9931519575 |
| `0x1F4` | packed/word torque-request region | 0.9927203968 |
| `0x100` | packed/word torque-related region | 0.9851173244 |

Those frames represent semantically different torque stages in current
evidence, so the high global fit is covariance rather than actual-torque
identity.

The opt-in Phase-3 pass used the explicitly documented development thresholds
and shortlisted only `0x0FC`, `0x1F4`, and `0x100`. Classification counts were:

| regime | samples |
|---|---:|
| idle | 143 |
| positive pull | 6 |
| lift transition | 4 |
| negative overrun | 2 |
| steady cruise | 0 |
| other | 14 |
| missing classifier input | 0 |

Only idle and positive pull met the five-sample fit floor. The ranking changed
materially by regime: idle favored a `0x100` packed field at R² `0.95047`,
while the six positive-pull samples favored `0x0FC` at R² `0.99922`, closely
followed by known request-related `0x1F4`. This is useful counterevidence
against treating the best global field as a universal actual-torque identity.
It is not enough to select a replacement: the exact-wire portion of this leg
is overwhelmingly idle and has no steady-cruise slice.

The documented thresholds should remain fixed for the next comparison rather
than being tuned to manufacture development-leg coverage. The next useful
Phase-3 evidence is the continuation and blind driving legs, followed by the
same regime view of `101A`, `101B`, and `101F` if their exact wire samples are
present.

## Compute execution note

An initial burst of six concurrent remote jobs caused four correlations to
exit 137 with empty stdout/stderr on separate `m4mac` worker slots
(`m4mac.09`, `.00`, `.02`, and `.03`). The jobs began within two seconds of
one another and died after 49–195 seconds. Their IDs were
`20260728T073319Z-e6b7d187`, `20260728T073320Z-0a1669a1`,
`20260728T073321Z-f88c721d`, and `20260728T073322Z-346c3e19`. The same
workloads completed normally when retried sequentially, indicating shared
host/container memory pressure rather than malformed evidence or arguments.
Full saved-log work remained on `pi_compute`; none was moved to vanpi. Until
the service update is verified, one concurrent `can-log-batch` job per
physical host is the conservative scheduling limit.

The repository test job reported 695 passed and 4 skipped. Its only two
failures were the pre-existing dashboard/broker vehicle-state freshness
expectations, unrelated to these changes.

## Result artifact integrity

Raw reports remain gitignored under `tmp/compute/done/`. The job IDs and report
SHA-256 values needed to locate and verify this analysis are:

| purpose | `pi_compute` job | report SHA-256 |
|---|---|---|
| engine-speed known field | `20260728T080522Z-f235bde1` | `45ccdfd95c7dd4a213b1703416d55c57d6441c875fb61188a2042baa6e1923ad` |
| vehicle-speed known field | `20260728T073320Z-46fae568` | `41092df418647b2c61097a7c2e45b4f1375ebb0a43df4910f8378a62f4a33a47` |
| actual-torque coarse gate | `20260728T073452Z-1bd35853` | `2a7af8c781ac0b19034c5dc0ab6a343190f5555415810c95895d718638de4273` |
| gearbox-oil challenge | `20260728T074659Z-2e2795fe` | `02bcfc5e6c4571558de75dca902fea8c135370c3981d061e09345b92087341b2` |
| TCU-chip comparator | `20260728T075039Z-6fb8b3c1` | `83c7b0cf42a84546fc9d59477cd4e367b3f9f4820d7c8d943c5b825da19d0e34` |
| target-torque known field | `20260728T075414Z-ef5f0b76` | `3ae4f55e4159d884fca51bfcc41e6744a750474dd262f1bd616211cd411ba241` |
| actual-torque regime slice | `20260728T075737Z-316a6aa2` | `2470429d563ed166d1b01677b9f542725c277cc03c788d8a21c813af893df6a8` |
| turbine-speed known field | `20260728T080135Z-80e5198b` | `0f4416d26cf1404168c962e17a944ad37ae02fd0ce14065b6ba139113075ac03` |
