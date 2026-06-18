# Web research — radar alignment tooling, SDA, security, costs (2026-06-18)

Max-effort web sweep to close ecosystem-layer unknowns. Findings + sources below.

## 1. wiTECH 2.0 is a cloud/browser tool — confirms the "hotspot" reasoning
FCA's own KB: wiTECH 2.0 is "web browser based," an "Internet based diagnostic tool"; the **VCI device
communicates directly to back-end cloud servers, driven in part by diagnostic application SECURITY
requirements**; "online model, so you will need to connect … to a mobile hotspot." → the SDA hotspot is
**wiTECH session + cloud-security**, NOT a radar-side compute step. (kb.fcawitech.com FAQ.)

## 2. The 0250-vs-0251 split is a METHOD split (this resolves our long-standing puzzle)
- **0250 = STATIC-MIRROR radar alignment** — older FCA cars (Giulia, Dart, Chrysler 200, Renegade,
  Compass). **AlfaOBD supports it** (radar-cal lives under "cruise control", gives **mirror-position**
  instructions per step) and people DIY it **cloud-free**. (giuliaforums "ADAS Radar Calibration DIY".)
- **0251 = SERVICE DRIVE ALIGNMENT (dynamic drive)** — our Promaster (AllData, `alldata_ram2022_…md`).
- So the methods genuinely differ by platform/radar generation. Our radar rejects 0250 and the static
  mirror did nothing because **the Promaster uses the dynamic SDA, not the mirror.** Consistent with all
  our observations.

## 3. Can AlfaOBD fix OUR radar? Probably NOT the SDA — but it proves two enabling facts
- AlfaOBD's radar calibration is the **static-mirror (0250)** flow → made for cars, **not** the Promaster
  SDA (0251). It mis-maps our MY2022 variant (calls 0250, hides angle gauges — see bug report). So AlfaOBD
  likely **can't** run our radar's SDA. (It *can* do PROXI config-alignment + ACC retrofit on the van.)
- **BUT** AlfaOBD running radar-cal **cloud-free on cars proves the radar-alignment routine class is local
  UDS, not cloud-gated** → our DIY `0x0251` attempt is plausible *at the radar level*.
- **NOTE:** "PROXI / proxy alignment" (config sync across modules) ≠ radar boresight calibration. Forums
  confirm DASM radar DTCs persist despite a good proxy alignment. Don't conflate them.

## 4. FCA security (27 seed/key) is LOCALLY solvable — not a cloud wall
AlfaOBD does FCA SecurityAccess **offline** (built-in seed→key; reads the security PIN from the BCM for
some functions). Third-party **seed-key calculators exist** (e.g. DiagCode "FCA SKGT"). → if `0x0251`
needs a `27` unlock to commit, the key is **computable locally** (sniff AlfaOBD or use a calculator),
NOT dependent on Stellantis servers. (alfaobd.com; diagcode.com; adamengineering seed-key analysis.)

## 5. Tool/cost reality — it is NOT "DIY hex vs $2k dealer"
- **Aftermarket tools do FCA ADAS calibrations via AutoAuth** (the aftermarket OE-auth service, ~$50/yr
  per brand): **Autel MaxiSys** (Elite/MS909/919/Ultra) and **Launch X431** (PRO Elite etc.) — tablets in
  the **~$600–1,500** range, reusable for everything else on the van. (autel.us; Launch listings.)
- **A shop ADAS radar recalibration is typically ~$250–500** (not $2k — that figure was the wiTECH
  tool/subscription, irrelevant to a one-off service).
- Your **Starlink** satisfies the internet requirement for any of these (AutoAuth/cloud handshake).

## 6. DASM disambiguation (don't conflate)
"DASM" on some RAM trucks = the **windshield CAMERA** (static, inclinometer, *no driving* — per i-CAR).
**Ours is the bumper ACC RADAR** (Bosch MRR, `0x18DA2AF1`), whose method is the **dynamic SDA**. The
i-CAR "no driving / inclinometer" notes are the camera, not our radar.

## Sources
- wiTECH 2.0 FAQ — https://kb.fcawitech.com/article/witech-2-0-frequently-asked-questions-87.html
- giuliaforums ADAS Radar Calibration DIY — https://www.giuliaforums.com/threads/adas-radar-calibration-diy.66675/
- i-CAR RAM DASM calibration — https://rts.i-car.com/crn-1443.html
- AlfaOBD — https://www.alfaobd.com/ ; DiagCode FCA SKGT — https://www.diagcode.com/products/fcaskgt/
- AutoAuth / aftermarket ADAS (Autel) — https://autel.us/autel-pushing-the-boundaries-of-aftermarket-diagnostics/
- ProMaster P2583 (front distance sensor, related) — https://www.go-parts.com/garage/obd-p2583-ram-promaster-2021-2024
