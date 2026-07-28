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

All four development known-field cases and all three available blind
known-field cases passed their tracked gates:

| reference | expected passive field | rank | coverage | R² | result |
|---|---|---:|---:|---:|---|
| PCM `01D5` engine speed | `0x0FC`, DLC 8, `u14be@7` | 2 | 0.996313 | 0.9999794279 | pass |
| TCM `F40D` vehicle speed | `0x101`, DLC 8, `u12be@0` | 5 | 1.000 | 0.9993328750 | pass |
| TCM `2102` turbine speed | `0x1F7`, DLC 8, `u16be@39` | 5 | 1.000 | 0.9999805746 | pass |
| TCM `101B` target crankshaft torque | `0x100`, DLC 8, `u11be@31` | 2 | 1.000 | 0.9995522578 | pass |
| blind-72 TCM `F40D` vehicle speed | `0x101`, DLC 8, `u12be@0` | 3 | 1.000 | 0.9999292408 | pass |
| blind-72 TCM `2102` turbine speed | `0x1F7`, DLC 8, `u16be@39` | 4 | 1.000 | 0.9999670457 | pass |
| blind-72 TCM `101B` target crankshaft torque | `0x100`, DLC 8, `u11be@31` | 2 | 0.998510 | 0.9995062643 | pass |

The provenance-bound evaluator re-read all seven reports with their matching
successful compute manifests and passed all seven. Its gitignored aggregate is
`tmp/ecu_mapping/signal_field_benchmark_known_evaluation_7case.json`, SHA-256
`24e90703e33456246385befc69177f6920bf75701cf189f1d46d033dd369ab7c`.
The aggregate remains `benchmark_complete: false` because unresolved and
holdout cases are deliberately still present.

The vehicle-speed result also illustrates why rank alone is not geometry
proof: shifted 12-bit views ranked slightly higher during this limited range,
while the established `u12be@0` field retained the expected approximately
`1/16` raw slope. The exact target-torque geometry had an approximately
unit-scale slope (`1.010612`) to the TCM DID raw value.

The benchmark remains intentionally incomplete. The cluster two-chunk task
fixes its older coarse DID-1000 invocation and cannot express the packed RPM
case. Owner-approved bounded variadic compute tasks now cover one exact wire
stream plus 1–16 chronological capture chunks. Exact extraction found no TCM
requests/responses in the nominal continuation leg, so that leg is unusable
for TCM validation rather than silently treated as an empty holdout. The
72- and 45-minute blind legs contain valid exact TCM wire streams; the first
has completed its paired thermal comparison and the second is pending compute.

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

### Independent 72-minute blind leg

Exact wire extraction recovered 24,166 complete positive TCM responses
(48,332 request/response rows), including 2,013 samples each for `04FE` and
`0301`. No Alfa CSV interpolation or sample-held timing was used.

The independent result resolves the development ambiguity in one direction:

| diagnostic reference | raw range | `0x417` bytes2–3 result | R² |
|---|---:|---|---:|
| `04FE` gearbox oil | 112–122 | established `u16be@23` view absent from the reported top 100; best eligible `0x417` 16-bit family only | 0.63891 |
| `0301` TCU chip | 97–118 | `u16be@23`, rank 6, 0.99901 coverage | 0.89384 |

The oil report's overall maximum eligible R² was 0.88619 on unrelated
`0x1F7` bytes3–4, a warm-up/time covariance that is not an oil-temperature
identity. For the chip comparison, the bytes2–3 affine model was
`DID_raw = 0.0141676 × candidate_raw + 68.6539` with 2.071 raw-count RMSE.
That is materially stronger than the oil association, but it does not preserve
the former `raw / 64 - 2 °C` physical decode across legs. Therefore:

- the `0x417` gearbox-oil identity and scale are rejected by current
  independent evidence;
- `0x417` remains only an unresolved thermal/state field;
- no transmission-temperature telemetry field is promoted;
- the next search should look beyond `0x417` for a true `04FE` carrier.

The second blind leg independently contains 17,870 complete TCM exchanges
(1,489–1,490 samples per selected DID). Its completed `04FE` half strengthens
the rejection: the exact bytes2–3 view ranked 32 with R² 0.44824, a negative
affine slope, and full coverage across DID raw 109–118. Its overall maximum
again came from unrelated `0x1F7` covariance (R² 0.86800). The paired `0301`
comparator also fails to reproduce the first blind association: `u16be@23`
ranked 27 with R² 0.55372 and full coverage across DID raw 93–106. Its affine
model, `DID_raw = 0.00596332 × candidate_raw + 84.3614` with 2.585 raw-count
RMSE, differs materially from the first blind leg's slope and intercept. The
overall maximum was only R² 0.64944 on unrelated `0x0EE` byte 5. Thus the
provisional chip-temperature association is rejected alongside the oil
identity; this field is not a defensible temperature telemetry source.

Both second-blind thermal cases are now `proxy_challenge`, not `pending`.
That classification records completed non-asserting evidence; it does not
retroactively invent a pass/fail threshold after seeing the result. The
benchmark evaluator deliberately counts only evaluable positive, negative, or
carrier expectations, so these thermal conclusions remain in the
provenance-backed comparison above rather than appearing as artificial
benchmark passes.

Intersecting the reported top 100 candidates across the development leg and
both blind oil reports leaves only overlapping `0x1F7` shaft-speed families.
Although their R² stayed roughly 0.83–0.89, their affine intercept shifted by
about 88 DID raw counts between development and blind data. No other exact
stream/field survived all three lists. Thus the already-completed coarse
search contains no defensible replacement gearbox-oil carrier; future
refinement needs a new evidence-led shortlist rather than a wider search around
`0x417`.

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

### Independent 72-minute blind regime leg

The frozen classifier was applied unchanged to 2,014 exact TCM `1018` samples.
Unlike the development leg, every intended driving regime had enough samples:

| regime | samples |
|---|---:|
| idle | 290 |
| positive pull | 206 |
| steady cruise | 235 |
| lift transition | 418 |
| negative overrun | 157 |
| other | 705 |
| missing classifier input | 2 |
| insufficient history | 1 |

The global ranking again reflects covariance, not identity: `0x0FC` led at R²
0.98420, request-related `0x1F4` followed at R² 0.98411, and the plausible
`0x100` packed torque field reached R² 0.95574.

If `0x100` were the same actual crankshaft torque as `1018`, their documented
physical scales would imply a stable raw relationship close to
`DID_raw = 0.125 × candidate_raw - 12`. Instead, the exact packed field changed
substantially by regime:

| regime | R² | slope | intercept | raw RMSE |
|---|---:|---:|---:|---:|
| idle | 0.94530 | 0.12190 | 10.03 | 3.96 |
| positive pull | 0.90391 | 0.11362 | 55.67 | 14.66 |
| steady cruise | 0.98806 | 0.12419 | -0.87 | 8.55 |
| lift transition | 0.88992 | 0.12220 | 10.73 | 23.60 |
| negative overrun | 0.63031 | 0.21954 | -387.18 | 13.53 |

The strong cruise slice is therefore a mode-specific near-match, not a
universal identity. The leading field family itself also changes: `0x0FC`
leads idle/lift, a different `0x100` byte view leads positive pull, the expected
`0x100` packed field leads cruise, and another `0x100` view leads overrun. This
independent counterexample rejects all three shortlisted families as a safe
actual-torque telemetry source. Actual measured torque and derived horsepower
remain unavailable.

The evidence-led `101A` comparator then tested the specific alternative that
`0x100` might be Alfa's “Crankshaft Torque, without TCU Torque Requests.”
Across 2,014 exact samples, the packed field reached global R² 0.96251 but
again failed the stable physical relationship: idle
`0.12830 / -16.72`, pull `0.11361 / +56.16`, cruise
`0.12477 / -4.74`, lift `0.12610 / -4.90`, and overrun
`0.19772 / -301.55` for slope/intercept, with overrun R² only 0.58459.
`0x1F4` ranked higher globally (R² 0.98514), but its independently sourced
request semantics and incompatible physical slope prevent relabeling it as
`101A`. Thus `0x100` is neither TCM `1018` actual torque nor `101A` torque
without TCU requests. The already-established `101B` target torque and `101F`
maximum request live in different passive fields, so repeating their known
identities as regime searches would not resolve `0x100`.

## Concrete utility gained

Applying the implementation to existing evidence produced no speculative new
dashboard metric. It produced four concrete project gains instead:

1. Three established TCM mappings—vehicle speed, turbine speed, and target
   torque—now pass independent blind whole-leg recovery, and all seven current
   provenance-bound known-field cases pass.
2. `0x417` is removed from consideration as both gearbox-oil temperature and
   the provisional chip-temperature alternative, preventing a wrong
   transmission-temperature gauge.
3. `0x100` is now explicitly ruled out as both `1018` actual torque and `101A`
   torque without TCU requests across every operating regime, preventing
   misleading torque/horsepower telemetry.
4. Every accepted benchmark result is byte-bound to its compute manifest and
   exact input hashes; older reports lacking the new staleness/provenance
   fields are excluded until rerun rather than silently grandfathered.

The immediate utility is therefore higher confidence in three existing
transmission signals and two avoided false mappings. True gearbox-oil
temperature and measured engine torque remain the next discovery targets.

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

A later local-fallback failure was independently reduced to the broker's child
process limit: the same bubblewrap command failed at `RLIMIT_NPROC=256` and
succeeded at 512, while the systemd unit's `TasksMax=128` remained in force.
A narrow systemd drop-in now starts the broker with `--max-processes 512`.
This fixes namespace creation without raising the service's actual task-count
ceiling. Job `20260728T101953Z-3b2127f1` then completed the formerly failing
five-chunk workload locally in 558 seconds.

Two later jobs were simultaneously claimed by `m4mac.05` and `.06`. Both slot
heartbeats disappeared before either uploaded a result, and the broker safely
returned both manifests to the queue at attempt 1. This is useful recovery
behavior, but it also reconfirms that several logical slots on one Mac are not
independent capacity for these searches. Placement should enforce a
per-physical-host concurrency limit rather than relying on callers to avoid
back-to-back submissions.

After the compute-service lease/heartbeat update, four serial blind jobs
(turbine, target torque, actual-torque regimes, and the `101A` comparator)
completed remotely on their first attempts in roughly seven minutes each, all
beyond the former five-minute failure boundary. No lease expiry or retry
occurred. This verifies the updated service path for the current workload
while retaining serial submission as the conservative physical-host memory
policy.

The current full suite reports 701 passed, 3 skipped, and 181 subtests passed.
Its only two failures are the pre-existing dashboard/broker vehicle-state
freshness expectations, unrelated to these changes. The four directly affected
signal-field/benchmark/correlator suites pass 70 tests, 7 subtests, with 3
optional cantools tests skipped. A final compute-service spot check against the
current tree passed 61 selected tests and 7 subtests, with 3 optional cantools
tests skipped.

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
| blind-72 exact TCM wire extraction | `20260728T093549Z-6d42dac8` | wire `8c6013236851962ae972d66012fc10fac5ed51d47021d9d6d9721f2121cbe7be` |
| blind-72 gearbox-oil challenge | `20260728T094130Z-e90f3aa1` | `6d6440558b6572f91c9cd2657facc673b60c93c6ceca1c73484ffc3f6f37a9c2` |
| blind-72 TCU-chip comparator | `20260728T095033Z-dc5e6751` | `5b85dcad83908e56cf3ebdcc223722cf2780cd94ace1fdfb5400abfcecccab1e` |
| blind-45 exact TCM wire extraction | `20260728T095920Z-cc006418` | wire `bbccf2407c416d84a80016461f1f0c9412d0cbae0af0fdba91c78f82b1081890` |
| blind-45 gearbox-oil challenge | `20260728T101953Z-3b2127f1` | `e1f85928124640f134dc9b16794e5d74172da225be353545db4f75e8ec67939f` |
| blind-45 TCU-chip comparator | `20260728T103322Z-56eda131` | `93e52a7391cf62ad2fb576d1bf273fef9d2b8d1862532a36e4d935d557790360` |
| blind-72 vehicle-speed known field | `20260728T103639Z-5d558ba1` | `3658963188088f05d0d10cdb1f8d89c8fe9db7b5c517e4ee4d4fbc5aafd3e8a8` |
| blind-72 turbine-speed known field | `20260728T183723Z-c8ab6a1a` | `6b20ea9a9c092f3200bbd96cb99cff347cc728844e0bf8bea03e84e9e0589d7b` |
| blind-72 target-torque known field | `20260728T184509Z-f04d4426` | `0f9ed1871feb6c3470d8e8842d66c63b10ae0065b18b49ac4ff6ca78b14d7e82` |
| blind-72 actual-torque regime slice | `20260728T185251Z-2bfd58d2` | `8a0e27e793f009c3c777aaa5e4a0edea3cabd18f68998f7c45629f24a4e506ac` |
| blind-72 torque-without-requests comparator | `20260728T190125Z-9224ce5b` | `cca57b8c5b291b3d2f137d9a8e3297c5f35070e4ba8b5ce6741341ca0d33b835` |
