# ACC radar (DASM / MRR1evo14F) — complete UDS map (DIDs, sessions, security, routines, DTCs)

Canonical, persistent reference for **everything we know or suspect** about the radar's UDS surface.
Source: full `22` sweep `../dumps/radar_acc_did_sweep.txt` (56 readable DIDs, 0 locked) + runtime work.
Raw values shown are **idle/parked** snapshots. Addressing: 29-bit normal-fixed, TX `0x18DA2AF1` / RX
`0x18DAF12A`. **All comms reach the radar via a physical SGW (Security Gateway) bypass** tapping the
internal C-CAN — this is why UDS works here and why OBD-II PIDs don't route. Confidence: **[V]** verified · **[I]** inferred (internally consistent, no ODX) ·
**[S]** suspected · **[?]** unknown.

## ★ Alignment / dynamic signals (the ones that matter for C1418-78)
| DID | idle raw | decode | meaning | conf |
|---|---|---|---|---|
| `0x0845` | `FFECC849 FFFFE55F` | 2×i32 ÷1e6 → (**−1.259°**, −0.007°) | **(elevation, azimuth) — AUTHORITATIVE stored misalignment.** Rock-flat across a 2-hr drive ⇒ stored calibration value. This is the C1418-78 driver. | I |
| `0x0850` | `FFED44D4 FFFF7E86` | 2×i32 ÷1e6 → (−1.228°, −0.033°) | (elevation, azimuth), **2nd / noisier live-ish** source; wanders −1.2…−1.36° while driving, no trend to 0 | I |
| `0x0841` | `FAE8` | i16 ÷1000 → −1.304° | **LIVE instantaneous** vertical estimate — swings **±10°** while driving (vehicle pitch), ~0 parked. **NOT** the stored fault; do not treat as the misalignment. | I |
| `0x0861` | `FEE3 0080` | 2×i16 → (−285, +128) | aux angle pair, scale/meaning uncertain (azimuth-ish, ~in-spec) | S |
| `0x0862` | `00000000` | 2×i16 → (0, 0) | aux angle pair / spare, reads 0 | ? |
| `0x1002` | `00` | u8 → km/h | **VEHICLE SPEED (km/h).** VERIFIED via DID hunt (0 at stops, plateau 68–88 = 42–55 mph matched a real drive). What AlfaOBD shows. Wired into the drive logger. | V |
| `0x0840` | `00` | u8 | flag adjacent to angle DIDs — **suspected alignment-status / valid flag** (worth watching during SDA) | S |
| `0x0842` | `00` | u8 | flag adjacent to angle DIDs — suspected alignment/calibration status | S |
| `0x0857` | `AA` | u8 | toggles ~170↔0 while driving — status/quality flag | S |

## Health (verified against AlfaOBD)
| DID | idle raw | decode | meaning | conf |
|---|---|---|---|---|
| `0x1006` | `85` | u8 ×0.1 → 13.3 V | control-module voltage | V |
| `0x0835` | `4C` | u8 −40 → 36 °C | ECU internal temp (only DID that drifts at idle) | V |

## Counters / timers (rise over time/distance — NOT angles)
| DID | idle raw | decode | meaning | conf |
|---|---|---|---|---|
| `0x1008` | `00029A48` | u32 → 170,568 | rising counter (ignition cycles / operation count) | S |
| `0x2008` | `00029A49` | u32 → 170,569 | paired counter (≈ `1008`+1) | S |
| `0x200B` | `0002996E` | u32 → 170,350 | related counter | S |
| `0x1009` | `0018` | u16 → 24 | monotonic counter during a drive (steps +2) — sample/odometer-ish | S |
| `0x2009` | `001E` | u16 → 30 | coarse stepping counter (steps of ~12 while driving) | S |
| `0x0F1A1`/`F1A1` | `00000025 9810` | — | build/usage counter or timer | ? |

## Status / config / unknown (small or sentinel)
| DID | idle raw | meaning | conf |
|---|---|---|---|
| `0x0103` | `FFFF` | status word? | ? |
| `0x0851` | `01` | flag/enable | ? |
| `0x0858` | `00` | flag | ? |
| `0x0863` | `01` | flag/enable | ? |
| `0x0872` | `00` | flag | ? |
| `0x2001` | `0C3FD2` | 3-byte status/measurement | ? |
| `0x2002` | `000000` | unknown (zeros) | ? |
| `0x2003` | `01` | flag/enable | ? |
| `0x200A` | `04E8` | u16 → 1256 (static; ~1270 elsewhere) | fixed parameter? | ? |
| `0x200C` | `004A` | u16 → 74 (static) | fixed parameter? | ? |
| `0x2010` | `FFFFFFFF` | invalid / N-A sentinel | I |
| `0x2013` | `02` | enum/flag | ? |
| `0x292E` | `07` | enum/flag | ? |
| `0x102A` | `00×9` | 9-byte data block, empty at idle | ? |
| `0x1921` | `08 20 00×7` | 9-byte status/config block | ? |

## Identification block (0xF1xx + build) — VERIFIED (standard UDS identification)
| DID | value | meaning |
|---|---|---|
| `0xF190` / `0xF1B0` | `3C6LRVDG4NE134328` | **VIN** |
| `0xF191` / `0xF192` | `MRR1evo14F` | radar HW family / model |
| `0xF18C` | `TD5730292062400` | ECU serial number |
| `0xF187` | `68516215AE` | Mopar spare-part number |
| `0xF188` / `0xF194` | `1037609794` | ECU software number |
| `0xF193` | `01` | HW version |
| `0xF195` | `04 00` | SW version (4.0) |
| `0xF186` | `03` | active diagnostic session (03 = extended) |
| `0xF184`/`0xF185` | `2534…` | cal/serial id block |
| `0xF180`–`F183` | date/fingerprint blocks | boot/app SW fingerprints + mfg dates |
| `0xF18A`/`F18B`/`F196`/`F1A4` | blank/spaces | unused supplier/date fields |
| `0xF1A0` | composite | PN+VIN+family+SW+`00 39 50 16 20` rolled up |
| `0xF1A5` | `00 39 50 16 20` | config/version code |
| `0xFD08` | `…SYSB_PLUS_FIAT_637MY22_R4.0_I0… Compiled by GDS1KOR Mon Apr 11 15:54:43 2022` | build banner |

## Sessions (DiagnosticSessionControl `10 xx`)
- `0x01` default · `0x03` **extended** (reads + `0x0251` runs here; `27 05` seed available here) ·
  `0x40` and `0x60` also grant (`50 40/60 …`) but are **NOT** where `0x0251` runs (red herrings).
- S3 timeout ~5 s when idle; **re-sending `10 03` RESETS a running routine** — hold with `3E`.

## Security (SecurityAccess `27`)
- `27 05` in session `0x03` → **seed** `67 05 F0 A7 5F 5A` (4-byte). Key algorithm not held by us
  (FCA seed/key is **locally solvable** — AlfaOBD/DiagCode — if a routine needs it; do NOT brute force).
- `27 01/03/07/11` → `7F2712` (not supported); `27 05` in `0x60` → `7F277E` (wrong session).

## Routines (RoutineControl `31`)
- **`0x0251` = the alignment routine.** Start `31 01 0251` (NO option) in session `0x03` → `71 01 0251`;
  **single-start** (2nd start → `7F3124`; `31 02` stops; status `31 03 0251` → `71 03 0251 01 01 00 02`
  running / `00 04 00 02` idle). It is the **dynamic Service Drive Alignment** (needs a drive — see
  `../docs/oem/`), NOT a static-mirror routine.
- `0x0250` → `7F3131` (the static-mirror routine used on FCA cars; **not implemented on this radar**).
- Full `31 03` scan over 0x0200–0x03FF + 0xFF0x found only `0x0251`.

## DTCs (`19 02 FF`)
- 8 stored; **only C1418-78 ACTIVE (status `0x8F`)**, other 7 dormant (`0x40`). FCA 3-byte encoding,
  C1418-78 = `54 18 78`. (C1417-78 = horizontal-misalignment counterpart, per AllData.)
