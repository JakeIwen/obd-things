# AlfaOBD bug report — MY2022 Ram Promaster, Bosch ACC radar (DASM / MRR1evo)

## Summary
On the 2022 Ram Promaster's Bosch Adaptive Cruise Control radar, two AlfaOBD definitions
appear to be mapped for a different model-year/variant:

1. **The radar-alignment routine uses the wrong RoutineControl ID** — AlfaOBD calls `0x0250`,
   which this ECU rejects as unsupported; the routine this firmware actually implements is
   **`0x0251`**. This makes "Active alignment: radar calibration" impossible, so the active
   misalignment DTC can never be cleared.
2. **Several misalignment live-data gauges request DIDs this firmware does not implement**
   (they return "Request not supported by Control Unit"), while the DIDs that *do* expose the
   misalignment are not read. The result: a radar with an **active** vertical-misalignment fault
   displays misalignment values of ~0 / "not supported", hiding the very data needed to diagnose it.

The routine finding is definitive (unambiguous UDS negative-response codes). The live-data DID
identities below are reverse-engineered and offered as strong leads for correction.

---

## Vehicle / ECU identification
- **Vehicle:** 2022 Ram Promaster, VIN `3C6LRVDG4NE134328` (MY2022).
- **Module:** Bosch DASM / ACC radar ("Adaptive Cruise Control Unit Bosch").
  - Family (DID F191/F192): `MRR1evo14F`
  - Spare part no. (F187): `68516215AE`
  - Software (F195): `04 00`   ·   Hardware (F193): `01`
  - Serial (F18C): `TD5730292062400`
  - Build banner (FD08): `SYSB_PLUS_FIAT_637MY22_R4.0_I0 … Compiled by GDS1KOR on Mon Apr 11 15:54:43 2022`
- **Active fault:** `C1418-78` (vertical radar misalignment), status `0x8F`
  (testFailed + testFailedThisOperationCycle + pending + confirmed + warningIndicatorRequested) —
  i.e. *currently* failing, not historical.

## Bus / addressing used to verify
- HS-CAN diagnostic bus, **500 kbit/s**, OBD pins 6/14.
- UDS over ISO-TP, **29-bit normal-fixed** addressing. Module physical address `0x2A`, tester `0xF1`:
  - Tester → DASM: CAN ID `0x18DA2AF1`
  - DASM → tester: CAN ID `0x18DAF12A`
- Verified independently with a PEAK PCAN-USB + SocketCAN (raw `isotp`), so the responses below are
  the ECU's actual replies, not a tool artifact.

---

## Bug 1 — Radar-alignment routine ID is off by one (0x0250 → 0x0251)

**Observed (AlfaOBD):** "Active alignment: radar calibration" fails immediately with
*"The ECU has detected that the request contains parameter(s) with value(s) outside allowable range"*
(UDS NRC `0x31`, requestOutOfRange).

**Evidence (raw UDS):**
| Request | Meaning | ECU response | Interpretation |
|---|---|---|---|
| `31 01 0250 01` | startRoutine 0x0250, opt 01 | `7F 31 31` | requestOutOfRange |
| `31 03 0250` | requestRoutineResults 0x0250 (no params) | `7F 31 31` | 0x0250 **not implemented** (no parameter could be "out of range") |
| `31 03 0251` | requestRoutineResults 0x0251 | `7F 31 24` | requestSequenceError = **routine exists, not yet started** |

A hardened **read-only** scan of `31 03 <rid>` across `0x0200–0x03FF` plus `0xFF00–0xFF03`
(516 routine IDs, every one answered — no dropped frames) found **exactly one** recognized
routine: **`0x0251`** (`7F3124`). Every other ID returned `7F3131`.

**Conclusion / fix:** for this MY2022 Promaster Bosch DASM, the radar-calibration routine ID is
**`0x0251`**, not `0x0250`. Please correct the routine ID for this ECU/variant. (The on-screen
mirror geometry instructions appear correct; only the routine ID is wrong.)

---

## Bug 2 — Misalignment live-data gauges request unimplemented DIDs

**Observed (AlfaOBD "Plotted Data", this ECU):**
| Gauge | Value shown |
|---|---|
| Slow misalignment angle, ° | **Request not supported by Control Unit** |
| Fast misalignment angle, ° | **Request not supported by Control Unit** |
| Mirror sensor vertical deviation angle, ° | **Request not supported by Control Unit** |
| Vertical deviation angle in active alignment, ° | −0.070 |
| Horizontal deviation angle in active alignment, ° | 0.000 |
| Mirror sensor horizontal deviation angle, ° | 0.000 |
| ECU internal temperature, °C | 39.000 |
| Control module voltage (+15), V | 12.600 |

**Problem:** the ECU has an active **vertical** misalignment fault, yet every gauge that would
reveal it is either "not supported" or reads ~0. "Request not supported" indicates AlfaOBD is
issuing `22 <did>` for DIDs this firmware rejects (DID not implemented in this variant).

**The data IS present on the ECU.** A full read-only `ReadDataByIdentifier` sweep (all 0x0000–0xFFFF,
56 readable DIDs) found stored misalignment values consistent with the active DTC:

| DID | Raw | Decoded (inferred scale) |
|---|---|---|
| `0x0841` | `FA E8` | int16 ≈ **−1.30°** (vertical) |
| `0x0845` | `FF EC C8 49 FF FF E5 5F` | int32 pair ≈ **(−1.26°, −0.01°)** (elev, azim) |
| `0x0850` | `FF ED 44 D4 FF FF 7E 86` | int32 pair ≈ **(−1.23°, −0.03°)** |

**Cross-check that the sweep reads are valid** (two of AlfaOBD's *working* gauges matched exactly):
- DID `0x1006` = `0x7E` (126) → ×0.1 = **12.6 V** = AlfaOBD "Control module voltage (+15)".
- DID `0x0835` ≈ raw−40 → **~39 °C** = AlfaOBD "ECU internal temperature" (also the only DID observed
  to drift, as a temperature should).

So where AlfaOBD and the ECU point at the same parameter they agree — which makes the "not
supported" misalignment gauges look like a DID-map mismatch for this variant rather than a missing
feature in the ECU.

**Suggested fix:** for this firmware (`MRR1evo14F`, SW `0400`, part `68516215AE`, build
`SYSB_PLUS_FIAT_637MY22_R4.0_I0`), re-point the misalignment live-data parameters to the DIDs this
ECU implements (candidates `0x0841` / `0x0845` / `0x0850`). The exact DID→name mapping and the
angle scaling (millidegree vs microdegree) should be confirmed against the Bosch ODX/data
definition; the values above are reverse-engineered.

---

## Reproduction (read-only, safe)
With a tester at `0x18DA2AF1` / `0x18DAF12A`, 500k HS-CAN:
```
31 03 0250   -> 7F 31 31   (0x0250 not implemented)
31 03 0251   -> 7F 31 24   (0x0251 exists, not started)  <-- correct routine
22 08 41     -> 62 08 41 FA E8                            (vertical misalignment ~ -1.3 deg)
22 10 06     -> 62 10 06 7E                               (= 12.6 V, validates the read)
```

## What's certain vs inferred
- **Certain:** the routine NRCs (`0250`→`7F3131`, `0251`→`7F3124`); the "not supported" responses to
  AlfaOBD's misalignment gauges; the exact voltage/temperature DID matches.
- **Inferred (good leads, please confirm vs Bosch data):** that `0x0841/0x0845/0x0850` are the
  misalignment angles, and their exact unit/scale.
