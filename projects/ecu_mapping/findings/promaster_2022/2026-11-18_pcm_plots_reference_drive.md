# PCM gauges tied to DIDs and broadcast carriers (AlfaOBD reference drive, 2026-11-18)

## Result

During a two-leg night drive on 2026-11-18 (about 19:56–23:20 MST) AlfaOBD 2.4.4.0 polled the
PCM on its Plots page while the Pi recorded C-CAN, B-CAN and CAN-CH listen-only. Joining the
gauge CSV to the wire gives an exact decode for 19 labeled PCM gauges, and a search of every
broadcast field on the three buses finds passive carriers for five of them that the bus map did
not have. All decodes below are candidates from one drive unless the grade says otherwise.

### Gauge label → PCM DID

Every decode reproduces AlfaOBD's displayed value to its rounding on held-out rows (fit on one
half of the joined rows, checked on the other, both directions). Pairing is by value agreement
over the whole range, not by request order.

| AlfaOBD label | DID | Decode | Rows | Grade |
|---|---|---|---|---|
| Engine speed, rpm | `01D5` | u16 | 4657 | high |
| Estimated gear, N | `06E3` | u8 | 4657 | high |
| Current engine torque, Nm | `06DA` | s16 × 0.04 | 252 | high |
| Intake air temperature, °C | `0123` | u8 − 64 | 252 | high |
| Intake manifold air pressure, kPa | `0174` | u16 × 0.2 | 252 | high |
| Injector Pulse Width Cylinder 1 | `0234` | u32 × 0.000977, **ms** (the label says usec) | 252 | high |
| Accelerator pedal sensor 1 voltage, V | `040E` | u16 × 0.004888 | 252 | high |
| Air flow measured, g/s | `01CA` | u32 × 0.000977 | 252 | high |
| Engine oil pressure, kPa | `022A` | u8 × 4 | 4405 | high |
| Coolant temperature, °C | `011D` | u8 − 64 | 4405 | high |
| AC Hi-Side Pressure, kPa | `0329` | u8 × 14 | 4405 | high |
| 1/1 Short Term ADAP, % | `02BB` | u16 × 0.1953 − 100 | 4405 | high |
| 1/1 Long Term ADAP, % | `02BD` | s16 × 0.1953 (sign untested: raw 0–55 only) | 4405 | high |
| Spark advance, ° | `067C` | s8 × 0.5 | 4405 | high |
| Battery Temperature, °C | `0125` | u8 − 64 | 4405 | high |
| Front left wheel speed, km/h | `1737` | u16 / 64 | 4405 | high |
| Outside temperature, °C | `0127` | u8 − 64 | 252 | medium (3 values) |
| Fuel Level Percent, % | `0227` | u8 − 100 | 252 | medium (4 values) |
| Barometric pressure, kPa | `0109` | u16 × 0.199773 | 252 | medium (raw 404–407 only; `0.2 × raw − 0.09` fits equally) |

AlfaOBD's constants are the literals 0.000977, 0.004888 and 0.1953, not 1/1024, 5/1023 and
1/5.12. The difference is visible at the top of the range (0.07 g/s at 150 g/s air flow), so a
decoder that must match AlfaOBD's display uses the literals. These confirm and refine the
candidate fits in the [fuel-level finding](2026-11-15_fuel_level_0x356.md).

Not settled:

- **CAT Modeled Temperature (`012B`)**: the reply is a u16 that varied 547–1017 over the drive
  (r 0.62 with speed). AlfaOBD decodes only the high byte minus 64, so its gauge sat at −61/−62 °C.
  The DID pairing is certain (both display values match every row); the true scale is unknown.
- **Constant gauges**: Target cruise speed (`0891`, `0000` on all 628 replies; cruise was not
  set), Variable Speed Fuel Pump Actual Rail Pressure, ST Knock Retard and Desired PWM Radiator Fan
  all read 0 throughout. The three set-B labels map to `01AE`, `01B9` and `0B9C` (constant `00`,
  `00`, `0000` on 4455 replies) in an order the data cannot decide. They may be unsupported on
  this engine or simply inactive on a cold night.

### Passive broadcast carriers

Each candidate was fitted on one fold and checked on the other (across legs for rpm and gear,
halves of a leg otherwise), and had to beat the best rpm/speed/time proxy by a wide margin.
Bit positions are MSB-first (byte0 bit7 = position 0).

| Signal (DID) | Carrier | Decode | Held-out RMSE | Grade |
|---|---|---|---|---|
| Intake air temp (`0123`) | C-CAN `0x417` 10 Hz, byte2 bits 2–0 + byte3 bits 7–5 | `field − 40 °C` (slope 1.000) | 0.08 °C | high, **new** |
| A/C high-side pressure (`0329`) | C-CAN `0x41B` 10 Hz, 6 bits from position 3 (byte0 bits 4–0 + byte1 bit 7) | ≈ `9.8 × field + 10 kPa` | 4.5 kPa (= the DID's own 14 kPa step) | high, **new** |
| same | B-CAN `0x3E2` 10 Hz, 6 bits from position 13 | same raw, same fit | 4.1 kPa | high, **new** |
| Estimated gear (`06E3`) | C-CAN `0x5A8` 1.1 Hz, 4 bits from position 29 (byte3 bits 2–0 + byte4 bit 7) | field 1–7 = gear 1–7; 0 or 13 = 0; 14 = 1 (8 rows, probably reverse) | exact on 99.4 % / 99.8 % across legs (chance 28 % / 51 %) | high, **new** |
| Coolant temp (`011D`) | B-CAN `0x3E2` byte0 | `byte − 40 °C` | 0.08 °C | high, **new** (C-CAN `0x2ED` byte0 confirmed) |
| Wheel/vehicle speed (`1737`) | B-CAN `0x3E2`, 14 bits from position 41 | `field / 64 km/h` | 0.2 km/h | high, **new** (C-CAN `0x0EE` and `0x101` confirmed) |
| Engine speed (`01D5`) | B-CAN `0x3E6` 10 Hz, 14 bits from position 26 | `8 × field + 3 rpm` | 15–20 rpm (timing-limited) | high, **new** (C-CAN `0x0FC` confirmed) |
| same | CAN-CH `0x0DC` 100 Hz, 13 bits from position 3 | `field rpm` (slope 0.9997) | 13–34 rpm | high, **new** |
| Accelerator pedal 1 voltage (`040E`) | C-CAN `0x412` 10 Hz, byte3 bits 7–2 | ≈ `0.076 × field + 0.09 V` | 0.055 V (proxy 0.79 V) | medium, **new**; `0x736`, `0x1F0`, `0x0FB` also track pedal, so "sensor 1 voltage" versus pedal position is not separable |
| Engine torque (`06DA`) | C-CAN `0x0FB` 100 Hz, 10 bits from position 18 | ≈ `field − 496 Nm` | 6–9 Nm (timing-limited) | medium, **new**; the bus map's `0x100` `u13be@9` candidate fits as well (0.1235 × raw − 493, 10–12 Nm) |

Field widths are proven only over the raw range the drive exercised. Intake air temperature saw
6–16 °C (field 46–56), so the field's upper bound is untested: a 6-bit field would cap at 23 °C,
and the common FCA layout would be 8 bits (`((bytes2–3 BE) >> 5) & 0xFF`). A/C pressure saw
238–420 kPa (field 23–42); a 6-bit field would cap near 630 kPa, below normal summer high-side
pressure. Both need a warm-weather drive before any telemetry use.

The `0x417` intake-air field sits inside the bytes2–3 word the bus map records as an unresolved
thermal field whose earlier oil and TCM-chip-temperature fits failed replication. Intake air
temperature tracks under-hood heat, which would explain those partial associations.

Fuel level: B-CAN `0x356` byte0 (the dashboard's tank level) tracks PCM `0227` monotonically
but not one-to-one (PCM ≈ 0.856 × `0x356` + 10.2 over 37 → 18 %). They are differently calibrated
gauges; don't substitute one for the other.

No carrier: outside temperature, barometric pressure, air flow, battery temperature, CAT modeled
temperature, both fuel trims, spark advance. Manifold pressure and injector pulse width correlate
only with coarse load fields (`0x412`, `0x0FB`, `0x100`) and are not identified as their own
signals. Slow few-valued references (outside temperature, baro, battery temperature) match
several unrelated drifting fields equally, so none was promoted.

## Method

- AlfaOBD drove on the owner's tablet (OBDLink MX+ Bluetooth on the OBD port), operated by the
  agent over ADB with verified single taps. The Pi's own PCM drive helpers were paused for the
  whole session with the `alfaobd` external-operation inhibit, because they use the same tester
  address (`18DA10F1`). The Pi transmitted nothing.
- Set A (12 gauges, graphs off, about 1.4 s per cycle) ran in leg one; its CSV covers 20:28–20:38
  MST. Set B (12 gauges plus AlfaOBD's own oil pressure and coolant temperature, 14 DIDs per 1.32 s
  cycle, every request answered) ran 21:22–23:18 MST. The engine-off at 20:40 MST and the end of
  the drive each left the Plots page stuck after the connection dropped, so the last 1–2 minutes of
  each set exist on the wire only.
- Join: every CSV row repeats the `01D5` reply exactly (252/252 rows in set A, 4405/4405 in set B),
  so rows were keyed to poll cycles and each other DID joined at a fixed cycle offset. The tablet
  clock is PST; its row times run about 1–1.5 s ahead of the wire reply.
- Carrier search: every broadcast ID on the three buses, unsigned and signed fields of every width
  and start bit in both byte orders, decimated to at most one frame per 100 ms per ID, against the
  DID series sampled at the wire reply times.

## Evidence (gitignored on vanpi)

- Captures, all finalized `success: true`, `full_stream_complete: true`, zero socket and interface
  drops, under `/mnt/EXFAT512/obd-things/tmp/captures/{ccan,canch,bcan}/drive-correlation/`:
  `pcm-plots-drive-20261119T0256Z` (leg one, all three buses), `…-leg2` (C-CAN and CAN-CH leg two),
  B-CAN `…-leg4` (20:57–23:20 MST).
- Tablet logs: `tmp/ecu_mapping/pcm-plots-drive-20261119T0256Z/input/pull1/` (`Gauges_Data.csv`
  sha256 `afdf867d…`, `AlfaOBD_Debug.bin` sha256 `10e64cb4…`).
- Join: `tmp/ecu_mapping/pcm-plots-drive-20261119T0256Z/analysis/` (`did_table.md`/`.json`,
  `series/<DID>.csv` for both legs, `README.md` with the commands).
- Carriers: `tmp/ecu_mapping/pcm-plots-drive-20261119T0256Z/carriers/` (`carriers.md`/`.json`,
  `README.md`).

## Tooling notes

- `tools.candump_diagnostic_wire` takes chunk files, not a capture directory; leg two (2.6 h)
  took about 20 minutes on the Pi.
- `tools.can_timeseries_correlate` accepts only a `<module>_wire.jsonl` stream, so the carrier
  search used an adapter script in the evidence folder.
- `tools.candump_arrays` holds whole captures in Python lists; an 18 M-frame C-CAN leg ran the Pi
  out of memory. The search used a streaming decimator instead.
- AlfaOBD caps the number of ticked gauges; untick before ticking. Graph drawing slowed polling to
  about 4 s per cycle; GRAPHS OFF restored 1.3–1.5 s.

## Next

- A warm-weather drive to test the `0x417` intake-air and `0x41B` A/C field widths above the
  ranges seen here, and a second reference for gear on `0x5A8`.
- A drive with cruise set to exercise `0891`, and one with the radiator fan running for the
  constant set-B DIDs.
- `012B` scale: needs a reference other than AlfaOBD's truncated display.

## Correction 2026-11-19: field widths are at least 7 and 8 bits (8 and 9 proposed)

The warm-weather check listed under Next is done, using the July 27 pcm-plots captures and the
August 19 three-bus candump (working scripts and filtered frames:
`tmp/ecu_mapping/field-width-screen-20261119/`, gitignored). It overturns the 6-bit widths above. What the data demonstrates is a minimum width; the exact geometry below is the proposal:

- Intake air (`0x417`) is proposed as 8 bits, byte2 bits 4–0 + byte3 bits 7–5 (`((b2 << 8 | b3) >> 5) & 0xFF`,
  `SignalField(20, 8, "big")`), decoded `field − 40 °C`. August field 62–100 (22–60 °C), with a
  31–36 °C heat soak at 90 °C coolant and 61 smooth 63↔64 crossings; a 6-bit field would have
  wrapped at 64. That shows at least 7 bits; the maximum, 100, never exercises an eighth. byte3 bits 4–0 and byte4 are other fields.
- A/C high side (`0x41B`) is proposed as 9 bits, byte0 + byte1 bit 7 (`(b0 << 1) | (b1 >> 7)`,
  `SignalField(7, 9, "big")`), decoded ≈ `9.9 × field + 6 kPa`. Field 47–155 (about 470–1540 kPa),
  with smooth crossings of 64 and 128, which shows at least 8 bits; the maximum, 155, never
  exercises a ninth. The fit was made only to about 420 kPa, so the scale above it is extrapolated
  and its error there is unmeasured until a second reference covers the higher range. byte1 bits 6–0 and
  bytes 4–5 are other fields.
- B-CAN `0x3E2` carries the same pressure as a proposed 9-bit field from MSB-first position 10, but it holds
  non-pressure values (128, 133, 511) with the engine off, so telemetry uses `0x41B` only.

Both are now candidate telemetry metrics (`engine.intake_air_temperature`,
`hvac.ac_high_side_pressure`); see `docs/bus-map.md`.
