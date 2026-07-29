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
  `bf15c5a765646d72fcc3b83917ee83e9996262f6a537774329a19547cfe158f2`.

The provenance-bound two-case evaluator reports
`passed: false`, `telemetry_promotion_allowed: false`, and no missing case for
this hypothesis. Its gitignored aggregate SHA-256 is
`428f71e1d24405c26e4ee897d7f19ff261c1fe7ccae57b2dd1b7fd78fea3fcb4`.

## Verdict and next evidence

`0x1F7` byte 3 signed i8 is the strongest gearbox-oil carrier candidate found
so far, and the old `0x417` candidate remains rejected. It is not allowlisted,
must not drive a temperature warning, and must not be presented as verified
telemetry.

The next decisive run is one independent cold-start drive that covers at
least 30 °C of `04FE` change while retaining exact TCM polling and a zero-drop
passive stream. Its acceptance criteria must be frozen before inspection and
should test both:

1. broad-range carrier recovery (where R² is meaningful); and
2. direct error against the provisional fixed formula
   `°C = 0.375 × signed_i8 + 57`.

No additional broad whole-bus search is justified before that controlled
challenge.

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
