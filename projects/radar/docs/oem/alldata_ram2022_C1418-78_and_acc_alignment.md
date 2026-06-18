# AllData (Ram 2022, GAS) — C1418-78 chart + ACC alignment procedure

**Vehicle-specific** service data from a local AllData scrape (`~/dev/ram_2022_GAS`, not in this
repo). This is **authoritative for our actual van** (2022 Ram), unlike the FCA-generic STAR TSB.
Source paths (in the scrape):
- DTC chart: `vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/c_code_charts/c1418/c1418-78/…vertical_misalignment…html`
- Alignment proc: `data_pages/article/63088/guid/na-cr22vf-GUID-FDD5E9EA-…_html.html` ("ACC MODULE ALIGNMENT")
- Removal: `…GUID-2D798380-…` · Description/Operation: `…GUID-9045FD01-…` / `…GUID-90676207-…`

## ★★ The alignment method is SERVICE DRIVE ALIGNMENT (SDA) — a DYNAMIC drive calibration
From "ACC MODULE ALIGNMENT": perform when removing/installing the radar, after a proximity-fault
warning, after rear-axle adjustments, or after front-end R&R. Procedure:
1. Confirm tire pressures correct.
2. Connect the diagnostic scan tool; navigate to ACC module **"ECU view"** → **"Misc Functions"**.
3. Run **"Service Drive Alignment (SDA): radar calibration"** and follow the scan-tool instructions.
4. **NOTE: a Wi-Fi mobile hotspot (internet) is required for this procedure.**

**This overturns our earlier static-mirror premise** (which came from a Giulia/AllData doc): the
Promaster uses a **drive-based** alignment, not a 120 cm static mirror. It explains everything we saw:
- static mirror on a parked van did nothing (wrong method);
- normal sustained driving didn't converge (SDA wasn't running — it's a deliberate, scan-tool-initiated,
  guided drive, likely with a server/internet component given the Wi-Fi-hotspot requirement);
- our `0x0251` start that stays "RUNNING" is consistent with **SDA armed and awaiting the guided drive**.

## C1418-78 diagnostic chart (Ram 2022) — fix flow
Set when "the ACC Module detects that the radar sensor calibration is **invalid or missing**" (engine
running, vehicle speed present). Steps:
1. Check for **Service Bulletins / flash updates** (→ STAR S2123000064 in this folder), apply if any.
2. Erase DTCs, re-run conditions, see if it returns.
3. **CALIBRATE THE ACC MODULE** — *"Prior to running the Radar Sensor Calibration Procedure, verify
   that the ACC Module is properly mounted. An improper mounting can cause the calibration to fail."*
   Then run the calibration (= the SDA procedure above).
4. Check ACC harness/connectors (terminal drag test, corrosion, seating).
5. Still failing → replace + program the ACC module.

## Implications for our work (reconciled)
- **Leading fix is now: run SDA (dynamic drive via the scan tool), after confirming mounting.** Not a
  static mirror, not a parked routine.
- **Soften the "physical misalignment" conclusion:** the −1.26° persisting through normal driving is
  *expected* because SDA was never run — it does **not** prove a physical fault. Mounting is still worth
  verifying (TSB + chart note), but "physical" is no longer strongly supported; **"never successfully
  SDA-calibrated" is at least as likely.**
- **DIY/UDS replication caveat:** SDA is wiTECH-guided and needs internet (Wi-Fi hotspot) — there may be
  a server handshake we can't reproduce with raw `0x0251`. Replication attempt = start `0x0251`, keep the
  session alive, perform the guided drive (Open work #4) — but it may require the wiTECH/server side.

### Why the internet? — REVISED 2026-06-18: most likely wiTECH session continuity, NOT a radar requirement
AllData has **zero** "secure gateway"/"AutoAuth" docs; the only connectivity note is the bare "Wi-Fi
Mobile Hot Spot router is required" (no rationale). Better-supported reading now:
- **AlfaOBD (a ~$50 app, no cloud/websocket, plain UDS over an ELM/OBDLink adapter) runs the FCA radar
  alignment routine on supported models** (people report success). So the routine is a **self-contained
  local UDS sequence at the radar — NOT cloud-gated.** (AlfaOBD mis-maps our MY2022 variant: it calls
  `0250` + hides the angles; the right routine for our radar is `0251`. That's an AlfaOBD DB gap, not a
  cloud dependency.)
- **wiTECH 2.0 is a browser/web-portal app** (VCI bridges the vehicle; UI runs in-browser vs Stellantis).
  A persistent **websocket** would drop the moment connectivity is lost — and SDA happens while *driving*
  away from shop wifi. So the hotspot is most plausibly **session continuity for the cloud tool**, not a
  radar-side compute/auth step.
- **Therefore the "needs a Stellantis server to commit" worry is probably wrong.** Our raw-`0x0251` Pi
  path has real odds. The realistic remaining blocker is **local**: does `0251` need a `27` security
  unlock to commit? (`27 05` returns a seed; we lack the key algorithm.) If so, **sniff AlfaOBD** (PCAN
  listen-only) doing its routine on the radar to capture the `27` seed→key exchange (per-ECU-family;
  almost certainly the same unlock `0251` needs), then replicate. Cloud-free, no wiTECH, no dealer.
