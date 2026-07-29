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
