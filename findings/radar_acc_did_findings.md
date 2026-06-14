# DASM (Bosch MRR1evo14F) — DID sweep findings

Radar identifies as **MRR1evo14F** (Bosch Mid-Range Radar gen-1 evo). VIN `3C6LRVDG4NE134328`.
Full read-only `22 <did>` sweep of 0x0000–0xFFFF: **56 readable DIDs, 0 locked, 0 unresolved (CLEAN)**.
Raw log: `dumps/radar_acc_did_sweep.txt`. Tool: `python3 tools/did_sweep.py radar_acc`.

## ★ Alignment / deviation-angle candidates (stored, static across repeated reads)

| DID | raw | decoded | likely meaning |
|-----|-----|---------|----------------|
| `0841` | `FA E8` | int16 = −1304 → **−1.30°** (millideg) | **vertical (elevation) deviation** |
| `0845` | `FFECC849 FFFFE55F` | int32 pair = −1,259,959 / −6,817 → **(−1.26°, −0.007°)** (microdeg) | **(elevation, azimuth) deviation** |
| `0850` | `FFED44D4 FFFF7E86` | int32 pair = −1,227,564 / −33,146 → **(−1.23°, −0.033°)** (microdeg) | (elevation, azimuth) — 2nd copy/filtered |
| `0861` | `FE E3 00 80` | int16 = −285 / +128 | small angle pair (azimuth-ish, in spec) |

**Interpretation:** a consistent **vertical/elevation misalignment of ≈ −1.2° to −1.3°**, with
horizontal/azimuth ≈ 0°. A vertical error beyond the ~±1° class spec is exactly the active fault
**DTC C1418-78 (vertical radar misalignment)**. The values are static across repeated reads → these
are *stored* measured-deviation values, not noisy live signal.

**Scale/label NOT yet certified.** millideg vs microdeg is inferred from internal consistency + the
DTC, not from a Bosch/FCA data dictionary. **To lock it down:** open AlfaOBD's radar-alignment screen
(or its live-data list) and read its displayed "vertical/horizontal deviation" numbers, then match them
to these raw DID values. That correlation pins both the exact DID and the unit — same rigor approach we
used for routine 0x0251.

## Counters / timers (increment over time — NOT angles)
`1008`, `2008` (~0x00029A68, rising) · `200B` (0x0002996E) · `F1A1` (`00 00 00 25 98 10`).

## Status / misc live-ish
`0835` (`50`↔`51`, only DID that varied — temp/voltage/status) · `2001` (`0C 3F D2`) ·
`2010` (`FF FF FF FF` = invalid/N-A sentinel) · small flags `0851 0857 0863 2013 292E`.

## Identification block (metadata, 0xF1xx)
`F190`=VIN · `F191/F192`=`MRR1evo14F` · `F187`=`68516215AE` (Mopar PN) · `F18C`=`TD5730292062400` (serial) ·
`F194/F188`=`1037609794` · `F195`=`04 00` (SW) · `F193`=`01` (HW) · `F1A5`=`00 39 50 16 20` ·
`FD08`= build banner `SYSB_PLUS_FIAT_637MY22_R4.0_I0 … Compiled … Mon Apr 11 15:54:43 2022`.

## Next step to confirm angles
1. In AlfaOBD, open the radar live-data / alignment screen; note vertical & horizontal deviation values.
2. Re-read `0841 / 0845 / 0850 / 0861` here (`python3 tools/uds_send.py radar_acc 22 08 41`, etc.) and match.
3. Once matched, we have a verified live readout of the misalignment — useful to watch DURING a
   future `31 01 0251` alignment run (with the 120 cm mirror) to confirm it converges toward 0°.
