# Cluster DID `1000` to C-CAN broadcast correlation — 2026-07-26

## Outcome

Offline time-series correlation found a near-byte-for-byte passive C-CAN
candidate for the cluster's Engine-speed-associated DID `1000`:

| rank | broadcast field | coverage | R² | affine fit to DID raw | RMSE |
|---:|---|---:|---:|---|---:|
| 1 | `0x0FC`, `u16be` at bytes 0–1 | 623/623 | 0.9998896 | `DID1000 = 1.00002248 * field - 0.2513` | 6.42 raw counts |
| 4 | `0x1F7`, `u16be` at bytes 4–5 | 623/623 | 0.9988247 | `DID1000 = 1.95976453 * field + 144.9844` | 20.95 raw counts |
| 5 | `0x0F4`, `u16be` at bytes 0–1 | 623/623 | 0.9980228 | `DID1000 = 0.49812921 * field + 12.0446` | 27.17 raw counts |

The `0x0FC` field is the useful lead. Its observed range was `2928..6136`,
versus `2936..6136` for the asynchronously sampled DID. The nearest-frame
matches were only 2.81 ms apart on average and 5.43 ms apart at worst. The
small residual is consistent with normal engine-speed change between two
independently scheduled messages.

This establishes a strong candidate raw relationship, not a physical RPM
decode. The prior AlfaOBD singleton work independently associates cluster DID
`1000` with the label Engine speed, and the idling shape remains consistent
with `raw / 4` rpm. This experiment did not contain a simultaneous AlfaOBD
rendering, tachometer reference, commanded RPM hold, or driving variation, so
the `/4` scale is still unverified and `0x0FC` must not yet be promoted to the
public `engine.rpm` metric or used for limits.

`0x0F4` and `0x1F7` also tracked warm-up/idle behavior, but their non-unit
affine relationships and larger residuals make them secondary related-signal
leads rather than independent identity mappings. The idling trace has one
dominant time trend, so correlated temperature, load, charging, and control
signals can rank highly without representing engine speed.

Ranks 1–10 do not represent ten independent confirmations. They are
overlapping byte, endian, 16-bit, and 32-bit views of the same three frame
regions. In particular, the rank-2 `0x0FC` 32-bit field contains the rank-1
16-bit field, and the rank-3 `0x1F7` 32-bit field contains the simpler rank-4
16-bit field. The shortest plausible 16-bit fields above are the canonical
leads. Matching used absolute nearest-frame time deltas and therefore does not
establish message direction, causal order, or true signal lag.

## Evidence and method

- Source campaign:
  `cluster-drive-shakedown-20260726T050955Z`
- Conditions: parked in Park, engine idling, C-CAN at 500 kbit/s
- Reference: 623 exact positive physical `22 1000` responses from the cluster,
  decoded as unsigned big-endian bytes 0–1
- Reference cadence: approximately 0.865 Hz over 719.108 seconds; this sparse
  sampling strengthens the time-trend/confounding caveat
- Passive input: both finalized zstd candump chunks, 1,960,920 frames total
- Candidate filter: 11-bit non-diagnostic frames only
- Matching: nearest frame within 100 ms
- Minimums: 20 samples, 50 percent coverage, four distinct values
- Report: van-compute job `20260726T205913Z-319936a6`,
  `report.json` SHA-256
  `b2dd59175a49828b48f69a1cd6fe6e829354ff5e59ddd6c1e4da65855c35a8f5`
- Reference and chunk submission hashes match the artifact table in the
  [idling shakedown finding](2026-07-25_cluster_idle_logger_shakedown.md).

The report exact-linked all 623 selected high-level reference rows to their
raw diagnostic frames. It deliberately did not revalidate the campaign
manifest, socket-drop accounting, or final summary. Those separate checks are
already recorded in the idling shakedown finding: clean finalization, complete
frame accounting, and zero reported socket drops.

All 100 retained rows are explicitly `candidate_only`, with physical identity,
scale verification, and telemetry promotion set false. Extended frames were
excluded, so the cluster's own `18DAF160` diagnostic responses could not become
the trivial top match.

## Related FCA-family corroboration

Public FCA-family artifacts independently make `raw / 4` the leading scale
hypothesis:

- comma.ai's pinned
  [FCA Giorgio DBC](https://raw.githubusercontent.com/commaai/opendbc/d7c9aff771a847f066e49573eaf69a458e7f2e14/opendbc/dbc/fca_giorgio.dbc)
  defines decimal frame `252` (`0x0FC`) as `ENGINE_1` and places
  `ENGINE_RPM` in Motorola bits `7|14`. That is the upper 14 bits of bytes
  0–1, equivalent to `(u16be >> 2)` rpm.
- A working public
  [Alfa Romeo Giulia dashboard implementation](https://raw.githubusercontent.com/ClaudeMarais/AlfaRomeoGiulia_DashboardInfo_ESP32-S3/7a6cbd42da79d50e8dd0d1cf567eaac281d2eda0/OBD2Calculations.h)
  maps broadcast `0x0FC` bytes 0–1 to
  `(byte0 * 256 + (byte1 & 0xFC)) / 4`, and applies `/4` to its diagnostic
  Engine-RPM value as well.
- An older public
  [ProMaster cluster capture](https://raw.githubusercontent.com/maxpfeif/canb/master/canb_engine_speed_ic.csv)
  repeatedly contains `18DAF160 056210000B80`; DID `1000` raw `0x0B80`
  becomes a plausible idle value of 736 rpm under the same `/4` scale.

These are useful related-platform and older-vehicle priors, not an OEM
definition for this exact 2022 ProMaster configuration. The Giorgio-derived
implementations also should not be treated as fully independent evidence from
one another. They substantially strengthen the `/4` hypothesis, but the exact
van's simultaneous displayed-RPM comparison remains the clean promotion gate.

## Utility and next evidence step

If confirmed, `0x0FC` provides engine speed passively and at broadcast rate:
no diagnostic transmit, session control, or one-hertz DID polling would be
needed. It would also give the time base needed to interpret oil pressure,
torque, and derived power.

The next drive should record `0x0FC` while AlfaOBD renders Engine speed and,
where practical, compare both with the instrument tachometer at several held
RPM plateaus. Include engine-off zero, the start transient, and both
acceleration and deceleration so an actual value can be distinguished from
filtered or commanded copies. A scale test should evaluate
`u16be(bytes 0–1) / 4` directly. Until that succeeds, retain `0x0FC` as a
diagnostics-only raw candidate and do not attach safe/unsafe engine thresholds
to it.
