# 2026-07-19 C-CAN drive signal analysis (`0x101` / `0x0EE`)

## Outcome

The field spanning bytes 0–2 of C-CAN frame `0x101` is instantaneous vehicle speed, not the
odometer/distance accumulator claimed in older radar notes:

```python
speed_raw = ((byte0 & 0x01) << 11) | (byte1 << 3) | (byte2 >> 5)
```

The field meaning and packing are high confidence. Its absolute scale is not yet proven: the two
remaining candidates are `speed_raw / 16` and `speed_raw / 32` km/h. One simultaneous known road
speed from GPS, cluster, or the radar's verified DID `0x1002` will choose between them.

## Evidence

This was offline analysis of passive captures; the analysis sent no CAN traffic:

- `tmp/captures/ccan/pair6-14_500k_ignition-on-through-drive_20260719_155436.log`
  (539,982 `0x101` frames, 99.998 Hz, raw range 0–2153)
- `tmp/captures/ccan/pair6-14_500k_drive-continuation_20260719_172415.log`
  (224,137 `0x101` frames, raw range 0–2074)

Observed behavior:

- The value rises and falls smoothly with motion and returns to zero at stops. About 88% of
  consecutive deltas are zero; nearly every nonzero delta is `+1` or `-1`.
- The capture contains the continuous packed transition `00 FF E0` = 2047 to `01 00 00` = 2048,
  confirming the high bit and ruling out a byte-boundary artifact.
- Every value from 7 through the observed maximum occurs. Stop transitions are `0 ↔ 7/8`; there is
  no rollover or saturation in either capture.
- C-CAN `0x0EE` bytes 0–1, interpreted as big-endian unsigned raw, independently track the field at
  approximately `8 × speed_raw`. Moving-sample Pearson correlation is `0.9999919`; regression is
  `0x0EE_raw ≈ 8.0099 × speed_raw - 11.6`.
- Using the paired candidate scales `0x101 / 32` and `0x0EE / 256` km/h gives integrated continuation
  distances of 21.463 and 21.456 km, a difference of about 0.007 km. This proves their relationship,
  not the physical unit: `/16` and `/128` form the equally consistent doubled pair.

The observed primary-capture maximum is either 67.281 km/h (41.81 mph) at `/32`, or
134.563 km/h (83.61 mph) at `/16`. The long steady plateau is correspondingly about 38–41 mph or
77–82 mph. Route/driver ground truth is intentionally required before promoting either scale.

## Other `0x101` fields

- `((byte2 & 0x03) << 6) | (byte3 >> 2)` behaves like braking/deceleration rather than distance:
  it is near zero during steady high speed and has moving correlation around `0.76` with braking
  magnitude. This remains a candidate until a controlled pedal/coast experiment.
- Byte 4 bit 7 appears primarily during stronger braking; candidate only.
- Byte 5 bit 3 behaves like a speed-valid/ignition flag: every moving frame has it set, and speed is
  always zero when it is clear.
- Byte 6 low nibble is a rolling `0..15` frame counter.
- Byte 7 is CRC-8/SAE-J1850 over bytes 0–6. It matched all 224,137 continuation frames.

## Documentation correction

Older statements that `0x101` bytes 2–3 contain a monotonic odometer accumulator are retracted. The
packed speed field demonstrably consumes byte2 bits 7–5, and the adjacent field is reversible and
braking-like. No odometer accumulator has been established in `0x101`.
