# 2026-09-24 LED headlamp/DRL DTC and IPC lamp-warning options

*Revised 2026-09-25: the owner confirmed that the low beams, high beams, and
DRLs are **all** LED, so six BCM circuits are affected.*

Research-only analysis. No CAN traffic, module write, DTC clear, or service change
was performed. Inputs were the local OEM service scrape, the owner's preserved
AlfaOBD logs and PROXI records (ignored under `tmp/`), an offline disassembly of
the installed AlfaOBD 2.4.4.0 APK, and web sources. The VIN and raw
configuration payloads are deliberately omitted. This builds on
[`LED_HEADLIGHTS_HANDOFF.md`](../LED_HEADLIGHTS_HANDOFF.md) and the
[2026-07-25 attempt/recovery record](2026-07-25_led_option_recovery.md). It does
not repeat them.

## Bottom line

1. **Recommended now: parallel load resistors on the LED circuits.** This
   requires no module write and no PROXI alignment, and removing the resistors
   reverses it. FCA's own ProMaster upfitter guidance uses parallel 50 W power
   resistors to satisfy the BCM's current monitoring. With all six circuits
   converted, the full kit is:
   - **four 6 Ω (5.6–6.8 Ω) resistors for the H7 circuits**, in the 100 W class;
   - **two 12 Ω resistors for the 7440 DRLs**, in the 50 W class;
   - all bolted to real heat sinks.

   The kit adds up to about 35 W of heat in daytime, about 69 W with low beams,
   and about 138 W with low and high beams together. Install and verify one
   circuit at a time.
   See [Option A](#option-a--parallel-load-resistors-six-circuits).

   A lighter hybrid is to keep LEDs only in the low beams and return the high
   beams and DRLs to halogen. That leaves two resistors
   ([Option A2](#option-a2--hybrid-led-low-beams-only)).
2. **Configuration route, now relatively more attractive but still gated:
   `Front Lights Diagnosis: Enabled -> Disabled`.** This is one PROXI bit and
   probably covers all six circuits, replacing roughly 138 W of worst-case
   under-hood heat and 12 splices. It remains a PROXI write plus alignment with
   AlfaOBD 2.4.4.0, the path that misrouted the DASM, flipped an unintended
   option, and left `B10AA` in July. It would also likely remove bulb-out
   detection for every front lamp, including the turn signals. Make it the
   preferred long-term route only after the alignment tooling problem is
   resolved; see [Option B](#option-b--disable-front-lights-diagnosis-deferred).
3. **Do not retry `Headlamp LED Management`.** AlfaOBD's own definitions describe
   it as LED DRL/parking/sidelight management. The July write proved that the
   BCM still set `B162A/B162E/B1632/B1636` with it Present.
4. **Clearing DTCs is not a fix.** The OEM monitor runs continuously whenever
   the ignition is on and the lamps are on, so the BCM re-detects the fault
   within seconds. The July log shows exactly this: the codes returned
   immediately after each clear. Clear DTCs only after a hardware or
   configuration fix, to confirm that fix.

## What the codes are (exact-vehicle OEM)

Source: local AllData scrape `~/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/b_code_charts/`.

| Circuit | DTC (`-15` = short to battery or open) | Driver circuit / lamp-connector pin | OEM bulb | Nominal halogen current |
|---|---|---|---|---|
| Left low beam | `B162A` | `L43` GN/BK 0.5 mm², left lamp D6605A pin 4 | H7 55 W | 4.2 A at 13.2 V |
| Right low beam | `B162E` | `L44` GN/WH 0.5 mm², right lamp pin 4 | H7 55 W | 4.2 A |
| Left high beam | `B1632` | `L33` GY/BK 0.5 mm², left lamp pin 1 | H7 55 W | 4.2 A |
| Right high beam | `B1636` | `L34` 0.5 mm², right lamp pin 1 | H7 55 W | 4.2 A |
| Left DRL | `B104D` | `L58` GN/YE 0.35 mm², left lamp pin 8 | 7440 / W21W 21 W | 1.56 A at 13.5 V |
| Right DRL | `B104E` | `L59` GN/YE 0.35 mm², right lamp pin 8 | 7440 / W21W 21 W | 1.56 A |

Verification of the mapping:
- **Low and high beams:** the OEM DTC chart titles confirm `B162A`, `B162E`,
  `B1632`, and `B1636`.
- **DRL:** the OEM chart confirms `B104E-15` as "DRL Light Right". AlfaOBD's
  July log renders `B104D` as "DRL Light Left". The local scrape has no
  `B104D` chart page.
- **Beam bulbs:** the H7 55 W figures come from the OEM bulb table.
- **DRL bulb:** the OEM bulb table omits the DRL. The OEM parts listing for
  the headlamp includes a "7440/w21w Bulb" (`L0007440`). Aftermarket fitment
  data lists the 2014–2024 ProMaster DRL bulb as 7440
  ([PartsGeek/Hella 7440](https://www.partsgeek.com/nqs5gy7-ram-promaster-2500-daytime-running-light-bulb.html)).
  The OEM DRL removal procedure removes a separate DRL bulb socket from the
  front lamp.
- **Still to confirm on the MacBook OEM set:** the DRL bulb specification.

**The DRL is a dedicated lamp, not a dimmed beam.** Three facts support this:
it has its own driver (`L58`/`L59`), it has its own bulb and socket, and the
BCM configuration reads `DRL Lamp Location: None/Position DRL`. The
alternatives in that field are `High Beam DRL`, `Low Beam DRL`, and
`Turn Signal DRL`.

**PWM.** `Front Lights PWM Enable: Enabled` shows that the BCM may PWM-drive
front-lamp outputs. Which outputs it applies to, and at what duty or frequency,
is **unverified**. It is not in the local scrape. Confirm this on the MacBook
OEM set: 08 Electrical › Lamps/Lighting – Exterior, the Description/Operation
pages for the headlamps and DRL, including the DRL enable conditions and
whether the DRL intensity is reduced.

The lamp ground is pin 5 (`Z909` left, `Z299` right, 1.5 mm²). All of these are
high-side driver outputs of the BCM (connector C3/C). No relay is involved. The
OEM "When Monitored" condition for each code is *ignition on and that lamp on*,
and the set condition is *open or short to voltage on the driver circuit*.

`B1636-15` is therefore the **right high beam**, not a low-beam code. The
2026-09-24 snapshot, the July baseline, and the July experiment all show the
full set: both low beams, both high beams, and both DRLs. The high-beam and DRL
entries have status `0x4D`: failed, pending, and confirmed, but not yet tested
this cycle because those lamps were not on. See the
[2026-09-24 F1 scan snapshot](../../ecu_mapping/findings/promaster_2022/2026-09-24_in_vehicle_f1_scan.md#dtc-snapshot--19-02-0d-2026-09-24).

OEM bulb specification (`lighting_and_horns/specifications/.../exterior_lamps_-_specifications.html`):
low beam H7 55 W halogen, high beam H7 55 W halogen, and fog H1 55 W. The OEM
service part is `L0000H7SLL`. Source for the DRL bulb:
`parts_and_labor/headlamp.html`.

The OEM BCM operation page says the BCM monitors the circuits it controls and
"will also send electronic message requests to the Instrument Panel Cluster (IPC)
for the display of certain textual warning messages". The IPC lamp warning is
therefore BCM-driven. Once the BCM stops detecting the fault, the IPC warning
has no reason to remain. No IPC-side setting that hides this warning was found,
and masking it at the IPC would also hide genuine failures.

## Why `Headlamp LED Management: Present` did not help

**The field is now verified.** AlfaOBD's 2022 BCM PROXI definition table
(class `m8` in the disassembled APK) defines `Headlamp LED Management` as byte
143 (`0x8F`), bit 6 (`0x40`), with `0: Absent / 1: Present`. The July 25
17:15:09 write was made immediately after the labeled change. It differs from
the verified baseline at exactly that bit, plus 4 leading metadata bytes. That
upgrades the earlier "strong candidate" in the
[candidate DID inventory](../../ecu_mapping/findings/promaster_2022/2026-07-21_candidate_did_inventory.md)
to verified.

**It governs different lamps.** The same field sits at byte 133, bit 5 in
AlfaOBD's older table variants (`n8`, and `q8` for Ducato X290). There it is
labeled `LED DRL/Parking/Sidelights (Headlamp LED Management)` and
`LED DRL/Parking/Sidelights Ducato X290 (Headlamp LED Management)`, with values
`Light Bulb / LEDs`. It selects the factory LED DRL/position-lamp strategy of
LED-equipped headlamp units. It does not disable open-load monitoring of
halogen low- and high-beam outputs.

**The July log proves the outcome.** With the Present configuration aligned,
the BCM reported `B1632 B162E B162A B104E B104D` at 17:20:23, and
`B1636 B1632 B162E B162A B104E B104D` again at 18:06:50 and 18:15:02
(`tmp/vehicle_configuration/20260726_proxi_failure_logs/device_logs/BCDELPHI_Info.log`).
Forum reports on other FCA BCMs match. Changing a lamp "type" to LED did not stop
bulb-out messages, while disabling the corresponding *diagnostics* option did.
One example is the 2019 ProMaster: `DRL Type -> LED` plus alignment failed, and
`DRL Diagnostics -> Not enable` worked.

### Correction to the July record: the aligned state also enabled Stop&Start

The existing handoff describes the July attempt as one labeled change. The raw
command trace shows otherwise. The cumulative Info log records
`Car configuration change: Stop&Start: ' ABSENT ' -> ' PRESENT '` in the same
dialog sequence. Every aligned write from 17:16:14 through 17:55:14 (all 15
endpoints, several passes) carried **both** `Headlamp LED Management = 1` and
byte 57, bit 0 = 1. AlfaOBD's table `l8` labels byte 57, bit 0 as `Stop&Start`
(`0: Absent / 1: Present`). The 17:37–17:41 pass carried Stop&Start alone. From
18:26:28 onward, every write equals the baseline byte-for-byte, which is the
recovery.

The June-22 historical experiment changed only `0x8F`. The Stop&Start change is
a July 25 artifact. Its presence was not reflected in the recovery note or the
handoff.

Consequences:

- `B10AA-00` (PROXI configuration control) during the July "LED" state cannot
  be attributed to the LED field. Two explanations remain. The first is an
  inconsistent Stop&Start presence, since the configured type is ECM-based
  "CG type" and this van has no stop/start. The second is the known DASM
  misroute, which leaves the radar's EOL state not OK. The earlier June LED-only
  change also left `B10AA` in history, which weakly favors the DASM or
  "any change" explanation. This remains unresolved.
- The headlamp-DTC conclusion is unaffected: Stop&Start does not plausibly
  suppress lamp monitoring, and the lamp codes persisted anyway.
- Any future configuration run must verify the full before/after byte diff
  **before** alignment and stop if more than the intended bit changed.

## BCM configuration fields that do govern lamp diagnosis and PWM

These fields come from AlfaOBD's own `m8` table. Byte offsets index the 250-byte
DID `0x2023` payload, and bit 0 is the LSB. The convention was validated for
this van: all 16 lighting fields decoded from the preserved baseline
(`tmp/proxi_safety/20260725_procedure2/current_proxi_250.bin`) exactly match
AlfaOBD's rendered `PROXI System configuration` values from 2026-07-22.

| Field | Byte.bit | Values | This van |
|---|---:|---|---|
| **Front Lights Diagnosis** | 190.1 | 0 Disabled / 1 Enabled | **Enabled** |
| Rear Lights Diagnosis | 190.2 | 0 Disabled / 1 Enabled | Enabled |
| **Front Lights PWM Enable** | 190.3 | 0 Disabled / 1 Enabled | **Enabled** |
| Rear Lights PWM Enable | 190.4 | 0 Disabled / 1 Enabled | Enabled |
| Parking Turn Light Strategy | 190.0 | 0 Disabled / 1 Enabled | Disabled |
| Inadvertent Light PWM Enable | 188.5 | 0 Disabled / 1 Enabled | Enabled |
| Headlamp LED Management | 143.6 | 0 Absent / 1 Present | Absent |
| Headlamp Type | 193.5 (2 bits) | Type 1–4 | Type 1 |
| DRL Lamp Location | 149.0 (3 bits) | None/Position, High Beam, Low Beam, Turn Signal DRL | None/Position |
| DRL Type//Front Bench Seat (Dart) | 123.1 | LED (500X MY19+)/Light Bulb … | 0 |
| Light Fade PWM Enable | 202.0 | Absent / Present | Absent |

This table is decode evidence only. Never write these bits directly; see
Option B for the supported path.

`Rear Lights Diagnosis` is probably the PROXI form of FCA's rear
bulb-out-detection disable (sales codes LB6/5QP). That is an inference, not a
verified mapping. No front-lamp equivalent sales code was found.

## Probable mechanism (hypothesis, unverified)

The DTC is the combined "short to battery **or open**" form, and the BCM
PWM-drives the front lamps (`Front Lights PWM Enable: Enabled`). The likely
failure has two parts. Many LED bulbs have an internal driver with input
capacitance. During PWM off-intervals or off-state open-load checks, that
capacitance holds the output high instead of letting it fall to ground the way
a halogen filament does. The LED's lower DC current adds to the problem. Both
parts are cured by a resistive load to ground at the lamp. This also explains
the reports that disabling PWM helps LEDs on other FCA BCMs. Neither mechanism
has been measured on this van.

## Option A — parallel load resistors (six circuits)

**Evidence.** FCA's *ProMaster Lighting* upfitter guide
([ramtrucks.com BBG vfelm.pdf](https://www.ramtrucks.com/assets/bbg/pdf/2016/van/docs/vf/vfelm.pdf))
makes two relevant statements:

- The BCM "has a preset allowable current (amperage) operating range for each of
  these outputs", including the headlamps and DRL.
- For LED conversions without the BOD configuration option, "Power resistors
  must be used". It specifies 10 Ω for a 30 W bulb and 8.2 Ω for a 35 W bulb,
  "50 Watt", "wired in parallel with the LED lamp", mounted "in an area with
  adequate ventilation and heat dissipation".

Those guide values set the resistor alone to about **61–64 % of the halogen
current** (1.35 A vs 2.2 A, and 1.65 A vs 2.6 A, at 13.5 V). The guide
addresses rear circuits, but it is the same BCM family's documented remedy.
The sizing below applies the same ratio. The LED bulb's own current then adds
margin.

### Per-circuit sizing

These are DC figures at 13.5 V (engine off) and 14.4 V (charging). PWM lowers
the real dissipation; see below.

| Circuits | Original load | FCA-ratio target | Recommended resistor | Resistor current | Resistor heat | Rating / heat sink |
|---|---|---|---|---|---|---|
| Low beam L/R (`B162A`/`B162E`) | H7 55 W, 4.2 A | 5.2–5.4 Ω | **6 Ω** (E12: 5.6 or 6.8 Ω) | 2.25 / 2.4 A | 30 / 35 W | 100 W class on its datasheet heat sink |
| High beam L/R (`B1632`/`B1636`) | H7 55 W, 4.2 A | 5.2–5.4 Ω | **6 Ω** (5.6 or 6.8 Ω) | 2.25 / 2.4 A | 30 / 35 W while on | 100 W class; 50 W class only if high beams stay brief |
| DRL L/R (`B104D`/`B104E`) | 7440/W21W 21 W, 1.56 A | 13.6–14.4 Ω | **12 Ω** (15 Ω for less heat) | 1.1 / 1.2 A | 15 / 17 W | 50 W class on its datasheet heat sink |

- **5.6 Ω vs 6.8 Ω:** choose 5.6 Ω if the LED bulb draws under about 1 A, and
  6.8 Ω if it draws 1.5 A or more.
- **Upper limit:** keep the LED current plus the resistor current at or below
  the halogen current, about 4.2 A for H7 and 1.6 A for the DRL.
- **Lower limit:** never go below about 5 Ω on an H7 circuit or 10 Ω on a DRL
  circuit. The BCM also has an over-current threshold and shuts a circuit off
  on too-high current.

### Heat sinking

The Arcol/Ohmite HS datasheet
([RS PDF](https://docs.rs-online.com/8b3e/0900766b815a22a3.pdf)) gives these
ratings:

- **HS50:** 50 W only on a 535 cm² × 1 mm aluminum heat sink, about 3 °C/W
  surface rise, and just 14 W with no heat sink.
- **HS100:** 100 W on a 995 cm² × 3 mm aluminum heat sink, about 1 °C/W, and
  30 W with no heat sink.
- **Maximum hot-spot temperature:** 200 °C.

Applied to these circuits:

| Resistor and mounting | Dissipation | Surface rise | Verdict |
|---|---|---|---|
| 6 Ω HS50 on its standard sink | ~35 W | ~105 °C, reaching ~165–185 °C with under-hood ambient | **Too hot** for continuous low-beam use |
| 6 Ω HS100 on a ~1000 cm², 3 mm aluminum plate (about 30 × 33 cm) | ~35 W | ~35 °C | Acceptable |
| 12 Ω HS50 on its standard sink (about 23 × 23 cm, 1 mm) | ~17 W | ~50 °C | Acceptable for the DRL |

Practical mounting:
- **Plate area:** each resistor needs its own sink, or a shared plate at least
  the sum of the listed areas.
- **Steel body metal:** thick steel structure can substitute for aluminum, but
  steel spreads heat about four times worse. Use more area, and verify with an
  IR thermometer.
- **Clearances:** keep resistors at least 5 cm from plastic, the headlamp
  housings, wiring, painted trim, and the radar area.
- **Loose kits:** plug-and-play kits whose resistor dangles free are
  over-stressed at 30–35 W. They are rated 14 W (HS50 class) free-air.

### PWM-driven outputs

- **Resistors are safe on PWM outputs.** A resistor is a pure resistive load,
  like the filament it stands in for. Its heat scales with the mean-square
  voltage, P = D·V²/R, so any duty cycle below 100 % lowers the dissipation
  proportionally.
- **Size for 100 % duty anyway.** The duty is unverified. If the BCM regulates
  lamps to a halogen-safe effective voltage of about 13.2 V, the 6 Ω heat at
  14.4 V charging falls from about 35 W to about 29 W.
- **Frequency is not a problem.** The inductance of a wire-wound resistor is
  negligible at lamp-PWM frequencies.
- **A resistor does not cure LED flicker.** The LED driver still sees the same
  chopped voltage.
  - If there is **no visible flicker**, add nothing else.
  - If there **is flicker**, use a combined resistor + capacitor
    ("anti-flicker decoder") module, or LED bulbs rated for PWM. Do **not** use
    a capacitor-only module. A capacitor with no bleed resistor holds the output
    voltage during the BCM's off-state check and can itself set a
    "short to battery" `-15` code.
  - Disabling `Front Lights PWM Enable` is the configuration-side flicker cure,
    with the Option B hazards.
- **Measurement is imprecise.** A DC multimeter at a PWM output reads the
  average voltage (D·V), not the RMS. Use resistor temperature as the practical
  check.

### Totals (resistors only, 14.4 V, 100 % duty)

| Scenario | Added current | Added heat |
|---|---|---|
| Daytime, DRLs on, headlamps off | 2.4 A | ~35 W |
| Night, low beams on (DRL assumed off with headlamps; verify) | 4.8 A | ~69 W |
| Low + high beams together (if low stays on with high; verify) | 9.6 A | ~138 W |
| All six at once (not a normal state) | 12 A | ~173 W |

For comparison, the original halogen load was 110 W for the low beams, 110 W
for the high beams, and 42 W for the DRLs. The BCM outputs and the alternator
therefore see no more than the factory load. The concern is *where* the heat
goes.

### Parts list (one set)

| Qty | Item | Example | Notes |
|---:|---|---|---|
| 4 | 6 Ω (5.6/6.8 Ω) 100 W aluminum-housed wire-wound resistor | Arcol/Ohmite **HS100 5R6 J** or **HS100 6R8 J** (also sold as generic "100W 6R aluminum resistor") | low and high beams |
| 2 | 12 Ω 50 W aluminum-housed wire-wound resistor | Arcol/Ohmite **HS50 12R J** | DRLs; 15 Ω (`HS50 15R J`) for less heat |
| 4 | aluminum plate ~30 × 33 cm × 3 mm, or equivalent mounting | — | H7 resistors; shared plates must sum the areas |
| 2 | aluminum plate ~23 × 23 cm × 1.5–3 mm | — | DRL resistors |
| 4 | H7 pass-through/pigtail harness, male–female | generic "H7 extension/adapter harness" | lets each H7 resistor tap without cutting factory wire |
| 2 | 7440/W21W pass-through harness, or solder/crimp taps | generic "7440 load resistor harness" (discard its bundled resistor) | DRL |
| — | 16–18 AWG silicone or high-temperature wire, adhesive-lined heat shrink, non-insulated butt splices or solder, fiberglass sleeving, ring terminals | — | no insulation-displacement "splice taps" on 0.35–0.5 mm² factory wire |
| — | M4 stainless screws, nyloc nuts, silicone thermal compound | — | two per resistor |
| — | IR thermometer and DC clamp meter (optional) | — | measure LED current, verify temperatures |

Do not add a fuse in series with a resistor. The BCM output is already
short-protected, and a blown fuse would recreate the open-circuit code.

### Install and verify order: one circuit at a time

Before starting, record a non-clearing BCM DTC read in AlfaOBD as the
reference.

Use the untreated opposite side as a control: its code should keep returning
while the treated circuit's code stays gone. The IPC lamp probably clears only
when **all** monitored front circuits pass. AlfaOBD's catalog has a single
`External lights failure warning lamp` item, so expect the warning to remain
until step 6.

1. **Left low beam (`B162A`).**
   - Install and mount the resistor.
   - With the engine off, have low beams on for 5 minutes and check the
     resistor temperature.
   - Clear the BCM DTCs (owner action in AlfaOBD).
   - Cycle the ignition off, wait 30 s or more, then turn it on.
   - Run the low beams for at least 5 minutes, then do a non-clearing read.
   - **Pass:** `B162A` absent while `B162E` returns.
2. **Right low beam (`B162E`).** Same procedure. **Pass:** neither low-beam
   code returns.
3. **Left DRL (`B104D`).**
   - Install the resistor.
   - Clear the DTCs and cycle the ignition.
   - Operate the DRLs: engine running, headlamp switch off. The exact DRL
     enable conditions, such as parking brake or gear, are unverified; confirm
     the lamp is actually lit.
   - Run for at least 5 minutes, then read.
   - **Pass:** `B104D` absent while `B104E` returns.
4. **Right DRL (`B104E`).** Same procedure.
5. **Left high beam (`B1632`).**
   - Install, clear, and cycle the ignition.
   - Use steady high beams, not flash-to-pass, for at least 2 minutes, then
     read. Note whether the low beams stay lit with the high beams; that fixes
     the heat scenario above.
   - **Pass:** `B1632` absent while `B1636` returns.
6. **Right high beam (`B1636`).**
   - Same procedure.
   - **Final check:** clear once more, then do a full ignition cycle.
   - Exercise DRL, low, and high beams, then do a non-clearing read. Expect
     none of the six codes.
   - Confirm the IPC lamp warning is off.
7. **Soak test.**
   - Take a long drive at night, then IR-check every resistor and splice.
   - Re-read the BCM DTCs the next day and again after about 1–2 weeks,
     including a cold start.
   - Re-inspect the mounts and splices after a few hundred miles.

**Optional lower-heat trial (unverified).** Once all six pass, a lighter load
on a single circuit may still pass, for example 10 Ω on an H7 circuit or 15 Ω
on a DRL. Test one circuit at a time using the same control method. Keep a
change only if the circuit stays code-free across several drives, including
cold starts.

**Alternatives in the same class.**
- **"CANbus/error-free" LED bulbs with built-in resistors:** these dump the
  same heat inside the sealed headlamp housing. Avoid them for H7; they are
  marginal for a 7440 DRL.
- **Resistor-containing decoders:** acceptable if their resistor can be bolted
  down. Capacitor-only decoders are not.
- **Relay-driven LED feed:** does not help, because the BCM would see only the
  relay coil, which still reads as "open".

**Reversal.** Unplug the pass-through harnesses. No module state changes.

## Option A2 — hybrid: LED low beams only

Return the high beams (H7 55 W) and DRLs (7440/W21W 21 W) to halogen, and keep
LEDs only in the low beams, where they matter most for night driving. This
reduces the job to two 6 Ω/100 W resistors. The added heat is about 69 W, and
only while the low beams are on. There is no daytime heat, and no resistors
are needed on circuits that run all day (DRL) or at full current for long
periods (high beams). High beams are used intermittently, and halogen DRLs are
what the BCM expects. This keeps full outage monitoring on four of six
circuits and involves no module writes.

## Option B — disable `Front Lights Diagnosis` (deferred)

**Exact path (AlfaOBD 2.4.4.0):**

1. Select `RAM PRO MASTER (VF) 2022+`, then the Body computer (exact Delphi/
   Marelli/Aptiv 2022+ profile).
2. Open the procedures/tools menu and choose `Car configuration change`. The APK
   menu row 1622 describes it: "This procedure allows to change car
   configuration options… Select an option you want to change from the list
   box".
3. Select `Front Lights Diagnosis`, then `0: Disabled`.

The July log shows AlfaOBD proceeding to `Proxy alignment` after a
configuration change. The option list spans both the `l8` table (Stop&Start
came from it) and the `m8` table (Headlamp LED Management), so
`Front Lights Diagnosis` is very likely listed. That is **unverified**. The
owner can confirm by opening the list box without executing anything.

**Why deferred:**

- It is a PROXI change requiring alignment. AlfaOBD 2.4.4.0 misroutes the DASM
  participant (see the
  [misroute finding](2026-07-26_alfaobd_proxi_dasm_misroute.md)). The July
  campaign needed about five alignment attempts and left `B10AA`.
- The July dialog also flipped an unintended option (Stop&Start). Any run must
  prove the diff before alignment.
- Scope is unknown. "Front Lights" almost certainly covers the front turn
  signals, park lamps, DRLs, and fogs as well as the headlamps. Disabling it
  would likely also remove the front turn-signal failure indication
  (hyperflash) and warnings for genuinely failed bulbs. US lighting rules
  expect a turn-signal failure indication, so verify this after any change.
- Warranty and legal considerations are unchanged; see Risks.

**Reconsidered with all six circuits LED (2026-09-25).** The balance has
shifted toward this route, but it has not flipped.

*For Option B:*
- **Coverage:** one bit probably covers all six circuits. AlfaOBD's 2022 `m8`
  table has no separate DRL- or headlamp-diagnosis field; the older X290 table
  had a separate `DRL Diagnostics`.
- **No hardware:** it replaces 6 resistors, 12 splices, about 6,000 cm² of heat
  sink, and 35–138 W of continuous under-hood heat.
- **Cost:** nothing to buy.

*Against it:*
- **July hazards unchanged:** the same AlfaOBD 2.4.4.0 alignment misrouted the
  DASM, needed about five passes, silently carried an extra `Stop&Start`
  change, and left `B10AA`.
- **Shift-interlock history:** the owner's earlier no-shift episode followed
  lamp/configuration work.
- **Scope:** loss of bulb-out detection on all front lamps, including the turn
  signals.
- **Rollback:** requires another alignment.

**Verdict:** use Option A (or A2) now. Promote Option B to the preferred
long-term route only when both of these are true:
1. A newer AlfaOBD release is shown offline to address the DASM at `0x2A`
   during 2022 alignment, or wiTECH access is available.
2. The owner accepts losing front-lamp outage detection.

A cheap, zero-risk step toward that is to inspect the next AlfaOBD APK offline
with the same method used here.

**Minimum conditions before attempting.** Everything in the handoff's
[Safe next-run procedure](../LED_HEADLIGHTS_HANDOFF.md#safe-next-run-procedure)
applies, with this field substituted for `Headlamp LED Management`. In addition:

1. Resolve the DASM participant problem first. Check a newer AlfaOBD release
   offline, using the same APK/database extraction plus disassembly, to confirm
   that 2022 alignment addresses `0x2A`. Alternatively, use wiTECH. Do not
   hand-patch a Pi-side alignment.
2. Save a fresh AlfaOBD proxy backup and a fresh `22 2023` read. Confirm both
   are byte-identical to the retained baseline before any change.
3. After the BCM write and **before alignment**, compare the new record with the
   baseline. The only permitted differences are byte 190, bit 1 (1 → 0) and
   leading metadata. Anything else, for example byte 57 bit 0: stop and restore.
4. Afterward, verify all front lamps, turn-signal behavior with one bulb
   unplugged, a non-clearing DTC read in the BCM and IPC, PROXI OK, and a zero
   fail counter.
5. Rollback: `Proxy tools -> Write custom configuration -> Read From File`
   using the retained `ProxyBackup_2026_07_24_19_48_12.txt`, then
   `Verify Custom Proxy`, write, and align. This is the path proven on
   2026-07-25 in the
   [recovery record](2026-07-25_led_option_recovery.md).

`Front Lights PWM Enable -> Disabled` (byte 190, bit 3) is a narrower-sounding
alternative. It might cure the code by the mechanism above, and it is reported
to stop LED flicker on other FCA BCMs. However, it carries the same PROXI
hazard, its effect on the DTC is unverified, and it would run any remaining
halogen bulbs at full charging voltage, which shortens their life. Consider it
only in the same deferred campaign, and never together with the diagnosis
change.

## Option C — return all six to halogen (H7 55 W, 7440/W21W 21 W)

This has zero electrical or configuration risk and is legally clean. The
low-beam connector-charring history reported by other ProMaster owners (one of
the reasons people convert) argues for inspecting the connector if halogens go
back in.

## Not recommended / ruled out

- Retrying `Headlamp LED Management`: wrong function, and already disproven for
  these codes.
- Clearing DTCs alone: re-detected immediately, per the OEM monitor conditions
  and the July log.
- The ProMaster "Chicken Dance" (key on → brake → Reverse → pull high-beam
  stalk >10 s…): FCA documents it only for **rear** BOD on 2015+ chassis, and
  forum reports say it does not work on 2019+/2022 vans.
- An IPC setting: no configuration found. The indicator is a BCM message
  request.
- A raw Pi-side `2E 2023` write or a home-built alignment: blocked by the
  repository's actuation safety gates. The misroute finding also shows that the
  standalone path is not yet validated.

## Risks

- **Heat (Option A):** the principal risk. Worst case with all six LED
  circuits is about 138 W under the hood. Use 100 W-class resistors on the H7
  circuits and real heat sinks; see the datasheet figures above.
- **BCM re-flagging:** with a correctly sized resistor the circuit current sits
  inside the OEM window. A value that is too low (for example 2–3 Ω) could
  approach the over-current or short threshold. The OEM guide says the BCM shuts
  off a circuit on too-high current.
- **DRL and high beams:** all six codes come from the same BCM monitoring.
  Option A leaves BCM behavior untouched. Configuration changes to DRL or
  front-lamp PWM could change DRL behavior.
- **DRL duty cycle:** the DRL runs all day while driving, so its resistors see
  the longest duty. That is the reason for 12 Ω/50 W on proper heat sinks, or
  for the halogen DRL in Option A2.
- **Legal/inspection:** NHTSA's position is that LED replacement light sources
  in headlamps designed for halogen bulbs are not FMVSS 108 compliant, and
  enforcement is left to the states
  ([NHTSA interpretation "LEDlamp.1"](https://www.nhtsa.gov/interpretations/ledlamp1);
  the page blocks automated fetching from vanpi, so this is summarized from its
  search-index text). Neither the resistors nor the configuration change alters
  that status. Aim the headlamps after any bulb change (OEM "Lamp Alignment"
  standard procedure).
- **Warranty:** a disconnectable resistor leaves no module trace. A PROXI change
  is recorded in the BCM (write counter, which is currently 15). The 2022
  basic 3-year/36,000-mile coverage has likely lapsed by mileage (BCM odometer
  about 82,000 km in July). General knowledge, not verified for this VIN.

## Unverified and follow-ups

1. ~~Which bulbs are LED?~~ **Answered 2026-09-25:** low, high, and DRL are
   all LED. Still unknown per bulb: the LED current draw (measure it to choose
   between 5.6 and 6.8 Ω), whether the low beams stay lit with high beams,
   whether the DRLs turn off with the headlamps, the DRL enable conditions, the
   actual PWM duty, and whether there is any visible flicker.
2. The minimum load that clears `-15` on this BCM (the optional graded trial).
3. Whether `Front Lights Diagnosis` appears in AlfaOBD's `Car configuration
   change` list for this BCM, and its exact scope.
4. The root cause of July's `B10AA`: Stop&Start inconsistency versus the DASM
   EOL/misroute.
5. MacBook OEM documents
   (`/Users/jacobr/Jake/J2534/service_docs/ram_2022_GAS_3`) worth checking:
   - 08 Electrical › Lamps/Lighting – Exterior › Description and Operation,
     for lamp outage monitoring, PWM, and any LED/upfitter note;
   - the DRL bulb specification (7440/W21W per parts data), plus DRL
     description/operation: enable conditions, intensity/PWM, and the
     DRL-off-with-headlamps rule;
   - the headlamp description/operation, covering low/high simultaneous
     operation and BCM lamp PWM/voltage regulation;
   - the BCM standard procedures for PROXI Configuration Alignment and Restore
     Vehicle Configuration;
   - any 2019+ ProMaster Body Builder lighting/BOD update.

## Sources

- Local OEM scrape: `~/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/…/b162a-15`, `b162e-15`, `b1632-15`, `b1636-15`, `b104e-15`; `parts_and_labor/headlamp.html` (7440/W21W, `L0007440`); `vehicle/lighting_and_horns/headlamp/diagrams/connector_views/{left,right}_front_lamp_assembly.html`; `vehicle/lighting_and_horns/specifications/mechanical_(including_torque)/exterior_lamps_-_specifications.html`; `…/body_control_module/description_and_operation/components/body_control_module_(bcm)_-_operation.html`.
- AlfaOBD 2.4.4.0 APK (owner copy, `tmp/ecu_mapping/android_tablet/apk/base.apk`), offline `baksmali` disassembly: classes `m8` (2022 BCM PROXI fields ≥ byte 110), `l8` (bytes < 110, including `Stop&Start` 57.0), `n8`/`q8` (older variants, including Ducato X290 `DRL Diagnostics` 245.5); database `Devices_diagnostics` for `BCDELPHI55851`.
- Owner AlfaOBD evidence: `tmp/vehicle_configuration/20260726_proxi_failure_logs/{device_logs/BCDELPHI_Info.log,reassembled_all.txt}`; baseline `tmp/proxi_safety/20260725_procedure2/current_proxi_250.bin`; decoded status `tmp/ecu_mapping/android_tablet/ccan_live_20260722_001010/bcm_status_baseline_text.txt`.
- FCA/Ram, *ProMaster Lighting — General Information* (Body Builder Guide): https://www.ramtrucks.com/assets/bbg/pdf/2016/van/docs/vf/vfelm.pdf
- PartsGeek 2014–2024 ProMaster DRL bulb (Hella 7440): https://www.partsgeek.com/nqs5gy7-ram-promaster-2500-daytime-running-light-bulb.html
- Arcol HS aluminium-housed resistor datasheet: https://docs.rs-online.com/8b3e/0900766b815a22a3.pdf
- Super Bright LEDs H7 load resistor kit: https://www.superbrightleds.com/headlight-load-resistor-kit-h7-led-headlight-bulbs-h7-connection-kit
- ProMaster Forum threads (pages block automated fetching; content per search-index summaries, so treat as anecdotal):
  - 2019 ProMaster DRL LED, `DRL Diagnostics -> Not enable`: https://www.promasterforum.com/threads/2019-promaster-led-drl-conversion-bulb-error-alphaobd-route.94796/
  - Chicken Dance not working on 2022: https://www.promasterforum.com/threads/chicken-dance-for-disabling-bod-bulb-out-detection-for-the-rear-not-working-on-2022-promaster-after-led-conversion.103050/
  - H7 LED conversions: https://www.promasterforum.com/threads/led-conversion-for-promaster-headlights.101382/
  - connector charring: https://www.promasterforum.com/threads/headlight-bulbs-dont-do-what-i-did.98673/
- Other FCA platforms (anecdotal):
  - Ram "changed the setting to LED, still bulb out": https://5thgenrams.com/community/threads/getting-bulb-out-error-message-already-changed-the-setting-in-alfaobd-to-say-the-lights-were-led.30874/
  - Jeep Cherokee front-light PWM disable via AlfaOBD: https://www.jeepcherokeeclub.com/threads/pwm-front-lights-disable-alfaobd.246186/
  - FCA `B162A` undercurrent fixed with a parallel load: https://diag.net/msg/m2xlvdzwigjd4txkx51f9nbhhq
- NHTSA interpretation on LED replacement light sources: https://www.nhtsa.gov/interpretations/ledlamp1
