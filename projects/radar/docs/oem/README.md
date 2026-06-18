# OEM / authoritative sources

First-party manufacturer service material and peer-reviewed references for the ACC radar — the
**trustworthy** layer, as opposed to our own reverse-engineered/inferred findings (which live in
`../../findings/`). Cite these over our inferences when they conflict.

---

## ★ AllData (Ram 2022, GAS) — C1418-78 chart + ACC alignment → [`alldata_ram2022_C1418-78_and_acc_alignment.md`](alldata_ram2022_C1418-78_and_acc_alignment.md)
**Most authoritative — vehicle-specific to our actual van.** Key result: the Ram-2022 alignment is the
**"Service Drive Alignment (SDA): radar calibration"** — a **dynamic drive** procedure run from the scan
tool (ACC ECU view → Misc Functions), needing tire pressure + a **Wi-Fi hotspot**. **NOT a static
mirror** (that premise came from a Giulia doc and is wrong for the Promaster). Read this first.

---

## FCA STAR S2123000064 Rev.A — "Service FCW Message On Cluster And C1418-78 Vertical Misalignment DTC"
- **File:** [`FCA_STAR_S2123000064_C1418-78_vertical_misalignment_2021-11.pdf`](FCA_STAR_S2123000064_C1418-78_vertical_misalignment_2021-11.pdf)
- **Publisher:** FCA US LLC (Mopar STAR Online), release Nov 2021. **Directly addresses our exact DTC C1418-78.**
- **Source:** NHTSA TSB mirror `https://static.nhtsa.gov/odi/tsbs/2021/MC-10204352-9999.pdf` (retrieved 2026-06-18).

### What it says (this is the authoritative fix path for C1418-78)
- **Root cause is mechanical, not a dialed-in angle:** "the [ACC] module is extremely sensitive to
  being **level** with the bumper and being **fully seated** in the bracket." Calibration fails
  repeatedly when it isn't.
- A common specific cause: the **aluminum bumper bar sits too high and physically contacts the ACC
  module**, displacing it. Pull the module and look for **witness/rub marks** on the aluminum bar.
- **Repair sequence:**
  1. Remove front fascia.
  2. **Re-seat the module** (remove + reinstall) — confirm fully seated and level; this alone may fix it.
  3. If bumper-bar contact: loosen the aluminum bumper bolts, **slide the bumper DOWN** as far as the
     locating studs allow, retighten **bottom bolts first, then top**.
  4. Reinstall the module; confirm **no contact** with the bar.
  5. **Then run the calibration routine** (FCA's WiTECH calibration — the `0x0251`-class routine) and clear DTCs.

### How this maps to our work
- Explains why the −1.26° **won't self-align** and why the static-mirror routine / parked nudge did
  nothing: it's a **seating / physical-interference** fault. Fix the mechanics first, *then* calibrate.
- The literal sign (up vs down) of our `0x0845`/`0x0850` elevation is **not** given here; the TSB makes
  it moot — the corrective action is "seat it level + relieve bumper contact," not a signed tilt.
- **Caveat:** this TSB is FCA-generic (photos look like a car platform, e.g. Giulia — not the Promaster
  RU van). Our van's exact bumper-bar/bracket geometry may differ, but the principle (seating +
  interference, then calibrate) almost certainly applies. Verify against the Promaster service info:
  "08 - Electrical / 8E - Electronic Control Modules / MODULE, Adaptive Cruise Control (ACC)".

## Web research — tooling / SDA / security / costs → [`research_2026-06-18_tooling_and_alignment.md`](research_2026-06-18_tooling_and_alignment.md)
Closes ecosystem-layer unknowns: wiTECH 2.0 is cloud/browser (hotspot = its session+security, not a
radar step); **0250 = static-mirror (cars, AlfaOBD-supported) vs 0251 = dynamic SDA (our Promaster)**;
AlfaOBD likely can't run our SDA but proves the radar routine is local UDS; **FCA `27` seed/key is locally
solvable** (AlfaOBD/DiagCode, offline); aftermarket tools (Autel/Launch + AutoAuth ~$50/yr) do FCA ADAS
calibrations for ~$600-1500, and a shop recalibration is ~$250-500 (not $2k).

## Supporting reference (not OEM, but peer-reviewed)
- Burza et al., *Overview of Radar Alignment Methods and Analysis of Radar Misalignment's Impact* —
  `https://pmc.ncbi.nlm.nih.gov/articles/PMC11314900/`. Physics of vertical misalignment (sky vs ground)
  but no vendor DID sign convention.
