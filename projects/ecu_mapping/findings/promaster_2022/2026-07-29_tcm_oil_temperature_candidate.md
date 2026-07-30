# TCM gearbox-oil temperature candidate — 2026-07-29

## Scope and classification

This is offline-only analysis of saved, listen-only C-CAN captures and exact
PCAN-observed TCM diagnostic exchanges. No CAN interface, vehicle service, or
ADB session was opened during the analysis.

The result below is `candidate_only`. The discovery leg produced a strong new
carrier and a plausible scale, but neither is eligible for telemetry until the
predeclared independent-leg gates below pass. The TCM DID namespace remains
ECU-scoped: `04FE` is the installed ZF 948TE profile's gearbox-oil-temperature
reference, rendered by AlfaOBD as `raw - 40 °C`.

## Discovery leg

Campaign `pcm-plots-drive-20260728T230109Z` ran for 4,005 seconds and finalized
after ignition loss with:

- seven complete ten-minute-or-shorter chunks;
- 10,905,813 raw C-CAN frames;
- zero recorder-detected socket drops;
- a complete full stream; and
- 28,056 exact positive TCM request/response pairs recovered from the raw
  PCAN capture.

The temperature comparisons each used 2,338 exact response timestamps. Oil DID
`04FE` spanned raw `76..118`, or `36..78 °C`; chip-temperature DID `0301`
spanned raw `77..117`, or `37..77 °C`.

The coarse whole-bus search again rejected `0x417`: its best reported family
reached only R² `0.67987` against oil and `0.74259` against chip temperature.
The oil search's leading result was initially an invalid overlapping `0x1F7`
view that joined pieces of already-known shaft-speed fields. A bounded
8–16-bit refinement of only `sff:1F7:8` resolved the useful component:

| reference | passive field | coverage | R² | affine model | raw RMSE |
|---|---|---:|---:|---|---:|
| TCM `04FE` byte 0 | `0x1F7` byte 3, signed i8 (`i8le@24`) | 1.000 | 0.99583874 | `DID_raw = 0.36988898 × i8 + 96.98292` | 0.7909 |

Byte 3 is a complete standalone byte between the already-verified output-shaft
field (byte 0 bit 0 plus bytes 1–2) and turbine-speed field (bytes 4–5). Thus
the refined result is not the coarse cross-field splice. The fitted
relationship is close to this simple candidate physical rendering:

```text
gearbox oil temperature °C ≈ 0.375 × signed_i8(0x1F7 byte 3) + 57
```

That formula is not yet promoted. A high fit during one thermal trajectory can
still be warm-up covariance.

Retrospective direct scoring of this development leg, compute job
`20260729T235136Z-ec7fdbb4`, gives full 2,338-sample coverage, signed bias
`-0.15697 °C`, MAE `0.66595 °C`, RMSE `0.82388 °C`, and `1.0 °C` p95 absolute
error. Because the simple formula was derived from this leg, those direct
development metrics describe it but are not independent validation. The
result-report SHA-256 is
`8651d42d9eeb4eef0c6b213d3b73a82f8debf74b0a66fb9c0b9ed168d570ec65`.

## Frozen independent-leg gates

Before retrieving either independent-leg result, the benchmark manifest added
carrier-only cases for this exact signed byte:

- 72-minute leg: rank at most 20, coverage at least 0.99, R² at least 0.98;
- 45-minute leg: rank at most 20, coverage at least 0.99, R² at least 0.95.

In addition to those executable carrier gates, scale acceptance is frozen at:

- affine `DID_raw / candidate_i8` slope `0.35..0.40`;
- affine intercept `95..99` DID raw counts; and
- affine RMSE no greater than 1.5 DID raw counts.

The paired `0301` chip-temperature reports are controls, not alternate labels.
Promotion also requires that the signed-byte relationship remain materially
more stable for `04FE` than for `0301`.

At gate freeze, the remote jobs were:

| purpose | `pi_compute` job |
|---|---|
| discovery-leg coarse oil | `20260729T023225Z-1ab8de18` |
| discovery-leg coarse chip control | `20260729T024044Z-2fc04d3f` |
| discovery-leg `0x1F7` 8–16-bit oil refinement | `20260729T024849Z-6df39836` |
| discovery-leg signed-byte chip control | `20260729T025906Z-2a17390b` |
| independent 72-minute signed-byte oil | `20260729T025911Z-b7dea7e9` |
| independent 72-minute signed-byte chip control | `20260729T025912Z-19e64030` |

The discovery reports are byte-bound to these result hashes:

- coarse oil: `cf4c842b8dab2f8e55b30e3e8bbaafc28f4b7f1363ec593d7757eef90b9b7d45`;
- coarse chip: `cf1f55a1e5197e5507b6af02523c35b4ddafb7da1923c974b7d27ad87fc9ff2e`;
- oil bit refinement: `5484e463e4cd2d043de78b20feb482d900688890205cc5093aed6e8f110a17b0`.

## Independent results

Both executable carrier gates failed. They are recorded as failures rather
than being weakened after inspection:

The 45-minute jobs were submitted after the gate commit:
`20260729T030318Z-5594e071` for oil and
`20260729T030320Z-c8c1e2dc` for the chip control.

| leg | exact samples | DID raw range | signed-byte range | rank | coverage | R² | slope | intercept | raw RMSE | frozen result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| discovery 66-minute | 2,338 | 76–118 | -56–56 | 28 in the 8–16-bit search; 1 in the exact 8-bit search | 1.000 | 0.99584 | 0.36989 | 96.9829 | 0.791 | development only |
| independent 72-minute | 2,013 | 112–122 | 40–64 | 9 | 1.000 | 0.88605 | 0.35229 | 98.0387 | 0.811 | **fail**: R² below 0.98 |
| independent 45-minute | 1,489 | 109–118 | 32–56 | 8 | 1.000 | 0.86623 | 0.33805 | 98.5899 | 0.848 | **fail**: R² below 0.95 and slope below 0.35 |

The blind legs' narrow 9–10-count oil ranges make R² especially sensitive to
quantization and small thermal lag. That explains why their sub-one-count RMSE
and intercepts look much better than their R², but it does not turn a
predeclared failure into a pass.

After the fixed-formula evaluator was implemented, the already-declared
`predicted_04FE_raw = 0.375 × signed_i8 + 97` formula was applied directly to
both blind legs. These are exact-error measurements on independent captures,
but they were computed retrospectively and do not rewrite the original
predeclared R² outcomes:

| leg | compute job | exact samples | coverage | signed bias | MAE | RMSE | p95 absolute error |
|---|---|---:|---:|---:|---:|---:|---:|
| independent 72-minute | `20260729T234814Z-5b44e0a9` | 2,013 | 1.000 | -0.15996 °C | 0.69449 °C | 0.83930 °C | 1.0 °C |
| independent 45-minute | `20260729T234813Z-9eb881fe` | 1,489 | 1.000 | -0.07052 °C | 0.76897 °C | 0.88302 °C | 1.0 °C |

The direct fixed scale is therefore much more stable than the narrow-range
refitted slopes suggested. This strengthens the scaling evidence, while
leaving physical identity subject to the separate thermal-discrimination
gate.

The chip-temperature controls support the oil-specific interpretation without
rescuing the carrier gate:

- discovery `0301` versus the exact signed byte: R² `0.88367`, slope
  `0.29380`, intercept `92.466`, and RMSE `3.526`;
- on the 72-minute and 45-minute legs, the exact signed byte did not reach the
  reported top 100; the best reported `0x1F7` 8-bit families reached only
  about R² `0.6475` and `0.6520`.

Thus byte 3 behaves materially more like gearbox oil than TCU chip
temperature on all three legs. The remaining problem is stable absolute
scaling, not label discrimination.

The result-report SHA-256 values are:

- discovery signed-byte chip control:
  `6f95631a8406487cf8a7962ecc50bbe090a19039a05d96891e5c43e2e86b3e85`;
- 72-minute oil:
  `6020ab7a25a36871a59f4c45601de1f849f282fb38c8a21b371fb7cc1e5788e9`;
- 72-minute chip:
  `901582d6a9e4c7ae6631544b1d8a42287424f3e5099c20353232d1ea0b7261d5`;
- 45-minute oil:
  `1047e42d825d8e9489a0bbcc4cfcb34e08d102bdf37ce8fa8e3d3ae3fdf273fe`;
- 45-minute chip:
  `bf15c5a765646d72fcc3b83917ee83e9996262f6a537774329a19547cfe158f2`;
- 72-minute fixed-formula oil:
  `8eb424ed363726f5dcccce54e93d373de1fa231cbc57b57c560cf25a27eb2ef5`;
- 45-minute fixed-formula oil:
  `6d97d28b863b450955914d80531029fadf94f2ea9bb8282e9b40e4e138a0a06d`.

The provenance-bound two-case evaluator reports
`passed: false`, `telemetry_promotion_allowed: false`, and no missing case for
this hypothesis. Its gitignored aggregate SHA-256 is
`428f71e1d24405c26e4ee897d7f19ff261c1fe7ccae57b2dd1b7fd78fea3fcb4`.

## Verdict and next evidence

`0x1F7` byte 3 signed i8 is the strongest gearbox-oil carrier candidate found
so far, and the old `0x417` candidate remains rejected. It is not allowlisted,
must not drive a temperature warning, and must not be presented as verified
telemetry.

The requested independent cold-start challenge is complete below. It strongly
validated the predeclared carrier geometry and fixed scale, but the frozen
oil-versus-chip discriminator failed. No additional broad whole-bus search or
identical cold-start trajectory is justified. The next useful evidence must
make gearbox oil and TCU-chip temperature follow materially different thermal
trajectories, or supply exact installed-calibration/ODX semantics.

## Frozen broad-range challenge gates

The following gates were frozen before the next cold-start capture. They do
not replace or reinterpret the failed narrow-range independent gates above.
The capture must:

- cover at least 30 °C of exact TCM `04FE` change;
- contain at least 1,000 exact positive `04FE` samples;
- retain a complete integrated C-CAN stream with zero recorder-detected
  socket drops and no unexplained TCM-endpoint traffic;
- recover exact passive field `0x1F7` signed byte 3 (`i8le@24`) with coverage
  at least 0.99, rank at most 10 in the bounded 8–16-bit search, and R² at
  least 0.98;
- retain affine slope `0.35..0.40`, intercept `95..99` DID raw counts, and
  RMSE no greater than 1.5 DID raw counts;
- make the fixed formula `°C = 0.375 × signed_i8 + 57` achieve RMSE no greater
  than 1.5 °C, absolute mean bias no greater than 1.0 °C, and 95th-percentile
  absolute error no greater than 2.5 °C; and
- beat the paired `0301` chip-temperature control by at least 0.10 R² for the
  exact signed byte.

Failure of any gate leaves the carrier candidate-only. Passing all gates
qualifies the fixed decode for a final telemetry-review step; it does not
silently enable a warning threshold.

The reviewed autonomous profile in
[`cluster_drive_log.py`](../../cluster_drive_log.py) polls only physical
`22 04FE` and `22 0301` at two total requests per second, sends no session
change or TesterPresent, records the full bus plus exact TCM wire evidence,
and restores the interface passive on exit. Despite the legacy filename,
`--profile tcm-thermal` is a separate fixed request profile and does not poll
the cluster.

## Additional same-day capture

The later `pcm-plots-drive-20260729T005006Z` leg retained four clean
ten-minute chunks and 3,334 exact TCM exchanges before AlfaOBD stopped
responding. Its labeled 242-row portion covered only `71..73 °C` oil and
`68..70 °C` chip temperature, too little thermal range to be a useful
independent scaling gate.

The full recorder stopped after reporting 3,728 socket drops in its fifth
chunk. That leg is not complete zero-drop evidence. The loss gate behaved
correctly, and the guarded recorder reserve was subsequently raised from
4 MiB to 16 MiB to better absorb transient EXFAT/compression stalls; zero-drop
acceptance remains mandatory.

## Broad-range cold-start result

Campaign `tcm-thermal-coldstart-20260729T222745Z` supplied the frozen
broad-range challenge. It ran for about 37 minutes 57 seconds before vehicle
end-of-leg conditions produced five terminal diagnostic timeouts. The logger
labeled the end as a DID-health failure because its three-timeout threshold
expired before the ten-second ignition-loss timer; this is consistent with,
but does not by itself prove, ignition shutdown during that short interval.
The terminal classification does not weaken the retained evidence:

- `04FE` and `0301` each have 2,027 exact positive responses;
- `04FE` covered raw `73..125`, or **33..85 °C**, a 52 °C span;
- all 4 raw chunks finalized complete;
- the raw capture retained 6,179,375 frames with zero detected socket drops;
- request/response count cross-validation reported no mismatch, no negative
  response, no unexplained TCM-endpoint frame, and no pending response; and
- the adapter was restored passive and the channel lock was released.

The exact signed-byte oil report, compute job
`20260729T232002Z-b1324a4a`, returned:

| frozen criterion | required | observed | result |
|---|---:|---:|---|
| `04FE` span | at least 30 °C | 52 °C | pass |
| exact samples | at least 1,000 | 2,027 | pass |
| coverage | at least 0.99 | 1.000 | pass |
| rank | at most 10 | 6 | pass |
| R² | at least 0.98 | 0.99734126 | pass |
| affine slope | 0.35..0.40 | 0.37607873 | pass |
| affine intercept | 95..99 raw | 97.10152 | pass |
| affine RMSE | at most 1.5 raw | 0.85056 | pass |

The tool then scored the predeclared physical formula directly, without
refitting it to this capture. Compute job
`20260729T233958Z-7c42c188` exact-linked every reference to the global raw
stream and evaluated
`predicted_04FE_raw = 0.375 × signed_i8 + 97`:

| fixed-formula criterion | required | observed | result |
|---|---:|---:|---|
| coverage | at least 0.99 | 1.000 (2,027/2,027) | pass |
| RMSE | at most 1.5 °C | 0.86253 °C | pass |
| absolute mean bias | at most 1.0 °C | 0.13518 °C | pass |
| p95 absolute error | at most 2.5 °C | 1.0 °C | pass |

The mean signed error orientation is
`04FE reference - predicted reference = +0.13518 °C`; mean absolute error is
`0.71929 °C`, and the observed signed errors span `-1..+2 °C`. This is a
direct fixed-scale result rather than a post-hoc affine-fit statistic.

The paired chip control, compute job `20260729T232002Z-c937bcb4`, found the
same exact byte at full coverage and rank 6 with R² `0.98339398`, slope
`0.31418336`, intercept `90.16842`, and RMSE `1.78735` raw counts. Its scale
and residual are materially less oil-like, but the frozen discriminator
required oil R² to exceed chip R² by at least `0.10`; the observed margin was
only `0.01394728`. That criterion **failed** and is not weakened after
inspection.

The provisional oil carrier therefore remains `candidate_only`. The
broad-range drive strongly reproduces its oil scale but does not independently
separate two temperatures that followed nearly the same warm-up trajectory.
Do not repeat an identical cold-start drive. The next useful evidence needs a
condition that makes gearbox oil and TCU-chip temperature diverge, or exact
installed-calibration/ODX semantics for this transmitted field.

Evidence hashes:

- finalized campaign summary:
  `02c06683072cb7f750ef506dcbcbee415999d3262a6efbd87777cedbc5c046d4`;
- high-level sample JSONL:
  `cf62aa2952ab23fef2afb561bab65fb867429584bbe5200d9c0097a1d243a69a`;
- raw manifest:
  `9eee3601f37e5e6c749aaebbb56e09676ee5526ce62c90df3063c87a3a385efc`;
- exact TCM wire JSONL:
  `ca6b2c3d8d8d0e7030ced9afeb6d2f3c0bfe72dcfe751e6a23158cd607c072f9`;
- oil result report:
  `4ffdd73eb5710fd90ff1b0ca0a9496e6913b1bf095c7d9b66cb14bae61e3a385`;
- chip-control result report:
  `fbd585cf903612e7b4f8a9d131aa924f8a9c1062a92cae1438d4c1a403b65d15`;
- fixed-formula result report:
  `d764efed18518d13e2dcec6e2eb011d1a2aebb1a1ab0a9ff04e510d403ff45ae`.

The logger's shutdown classifier was subsequently hardened around the terminal
pattern observed here. It still fails after three consecutive timeouts when
fresh `0x2EF` ignition-presence traffic continues. When the third timeout is
instead corroborated by at least two seconds without `0x2EF`, it now stops
cleanly as `diagnostic_timeout_after_ignition_frame_absent`; the independent
ten-second ignition-loss gate remains in place. This changes future terminal
classification only, not the retained evidence or the frozen result above.

## Frozen hot-soak divergence challenge

Before the next capture, the owner reported that the van had been parked with
ignition off for about 30 minutes after driving. This creates a distinct
hot-soak/restart experiment rather than repeating the rejected common
cold-start trajectory. The capture protocol is:

1. leave AlfaOBD closed and PCAN on C-CAN pins 6/14;
2. turn ignition on while still parked and start the paired `04FE/0301`
   logger before starting the engine;
3. start the engine and begin ordinary driving promptly; and
4. let ignition loss end the autonomous capture after the drive.

The evidence must retain at least 600 exact positive samples from each DID, a
complete zero-drop full-bus stream, exact request/response count agreement,
verified passive restoration, and no competing TCM diagnostic client.

The already frozen oil formula remains
`predicted_04FE_raw = 0.375 × signed_i8(0x1F7 byte 3) + 97`. It must again
achieve at least 0.99 coverage, RMSE no greater than 1.5 °C, absolute mean
bias no greater than 1.0 °C, and p95 absolute error no greater than 2.5 °C.

The semantic discriminator is frozen before inspection:

- at least 60 adjacent complete `04FE/0301` polling cycles must show an
  absolute oil/chip difference of at least 3 °C;
- the observed signed oil-minus-chip difference must span at least 2 °C;
- applying the unchanged oil formula against `0301` must produce RMSE at
  least 2.0 °C worse than against `04FE`; and
- its mean absolute error against `0301` must be at least 1.5 °C worse than
  against `04FE`.

If `04FE` spans at least 10 °C, the former R² discriminator is also reported
unchanged: oil R² must exceed chip R² by at least 0.10. A narrower leg does
not reinterpret that earlier frozen failure; it simply leaves the R² check
unscored and relies on the predeclared direct-error and paired-divergence
gates above. Passing this challenge permits a final telemetry-review decision
but does not automatically allowlist the field or create a warning threshold.
