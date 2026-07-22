# AlfaOBD live status and B-CAN correlation — 2026-07-21

## Outcome

A fresh AlfaOBD Debug Data recording, labeled status snapshots, one Gauges Data section, and a
simultaneous listen-only PCAN capture established useful DID correlations for the current van.
The session produced valid status reads from the C-CAN BCM and the B-CAN ICS, Uconnect, and EMCM2;
the EMCM2 connection survived a brief ignition sleep. It also showed that the selected AlfaOBD
Climate profile is not compatible enough with the installed `0x98` variant to use its gauge labels
or scaling. A later controlled direct-read follow-up resolved the two EMCM2 rotary bytes plus the
Mute and Screen-button states that were at rest in the AlfaOBD snapshot.

The useful result is not a globally interchangeable DID list. Every mapping below remains in its
ECU namespace. Confidence labels mean:

- **exact** — the label-to-DID association is supported by request order plus matching values,
  repeated observations, cross-module convention, or an exact packed-text response;
- **group-exact** — one DID is demonstrably the source of a displayed group, but its internal
  bits/fields are not all decoded;
- **order candidate** — request and rendered-output order support the association, but a controlled
  state change is still needed; and
- **invalid for this ECU** — the selected AlfaOBD profile failed variant verification or rendered
  values contradicted known vehicle state.

## Conditions and topology

- Vehicle parked, ignition on, engine off. The ignition briefly slept once during the recorded work;
  no completed status burst was interrupted, and each status request received either a positive
  response or an explicit negative response. A later sleep occurred only after traffic and capture
  had stopped and is not part of this evidence window.
- AlfaOBD 2.4.4.0 ran on the owner's Android tablet. The Pi controlled the UI through USB ADB;
  vehicle diagnostics used the OBDLink MX+ connected to the OBD branch.
- No external yellow, blue, or grey adapter was installed. AlfaOBD used its documented no-physical-
  adapter path for the OBDLink MX+ when profiles were marked `MS-CAN BLUE`.
- PCAN was connected separately to the pigtail B-CAN DB9 (DLC pins 3/11) at 125 kbit/s. This
  campaign configured it listen-only and ran only `candump`; spot checks and the final state were
  listen-only. The mixed-traffic caveat below prevents claiming that no other process briefly armed
  the interface during the entire capture.
- Debug Data and Gauges Data recording were enabled before the new module work.

## Module coverage

Counts below refer to distinct `22` DIDs in the aligned status burst, not repeated TesterPresent
traffic or the initial `10 03` session request.

| ECU | diagnostic route | status-burst result | AlfaOBD Plots |
|---|---|---:|---|
| BCM `0x40` | C-CAN, `18DA40F1 -> 18DAF140` | 62 positive, 27 explicit negative | unavailable |
| ICS `0x85` | B-CAN, `18DA85F1 -> 18DAF185` | 18 positive, 0 negative | unavailable |
| Uconnect `0x87` | B-CAN, `18DA87F1 -> 18DAF187` | 44 positive, 31 explicit negative | unavailable |
| Climate `0x98` | B-CAN, `18DA98F1 -> 18DAF198` | repeated 8-DID gauge loop; 6 positive, 2 NRC `31` | 8 gauges, all unusable for this variant |
| EMCM2 `0xD9` | B-CAN, `18DAD9F1 -> 18DAF1D9` | 16 positive, 0 negative | unavailable |

Across the complete current Debug bin, excluding TesterPresent, AlfaOBD recorded 1,069 `22`
requests and 88 `10 03` requests at nine physical addresses. The derived map contains 243 distinct
address/request keys. The larger BCM count includes its identity block and two status snapshots;
the B-CAN status counts above are the cleaner description of the new work.

## Diagnostic routing observation

The tablet and Pi clocks differed by a stable approximately `59 min 58.5 sec`. Four exact B-CAN
request payloads align across the Debug and PCAN records with that offset. Applying the same offset
places the second BCM status burst inside the PCAN capture. The B-CAN trace contains the complete
AlfaOBD exchanges for `0x85`, `0x87`, `0x98`, and `0xD9`, but contains no `18DA40F1` or
`18DAF140` frame anywhere.

That is direct setup-specific corroboration that the standard BCM profile used the C-CAN branch
while the adapter-6/`MS-CAN BLUE` profiles used B-CAN. It is consistent with the OBDLink's automatic
pin-routing path, but this observation alone does not prove its internal implementation. It also
explains why a PCAN attached only to B-CAN cannot monitor an AlfaOBD BCM request even though the
same OBDLink can reach both branches. It does not imply that the BCM is a B-CAN diagnostic endpoint.

## Recurring environmental and housekeeping DIDs

These associations recur across the observed status sequences. BCM, ICS, Uconnect, and EMCM2 all
read every row except `2002`, `2003`, and `2008`; Alfa's Uconnect status profile omitted those three,
which recur in BCM, ICS, and EMCM2. They are still per-ECU observations: recurrence is evidence for
an FCA convention, not permission to apply the DID blindly to another module.

| DID | observed interpretation | decode / evidence |
|---|---|---|
| `0103` | VIN-odometer counter/lock group | `FFFF` rendered counter 255 + locked; `0000` rendered counter 0 + unlocked |
| `2013` | stand-alone VIN-lock state | label association exact; Alfa's BCM enum rendering was inconsistent, as noted below |
| `2001` | odometer | unsigned 24-bit big-endian x `0.1 km`; `0C920C -> 82382.0 km` |
| `2002` | odometer at last rewrite | unsigned 24-bit big-endian x `0.1 km`; EMCM2's `.63` display is float rounding at a very large sentinel-like value |
| `2003` | rewrite count | direct unsigned integer |
| `1008` | ECU lifetime / RAM timestamp | direct unsigned minutes |
| `2008` | EEPROM functioning time | direct unsigned minutes |
| `1009` | RAM time since key-on | raw x `15 sec` |
| `2009` | EEPROM time since key-on | raw x `15 sec` |
| `200A` | key-on counter | direct unsigned integer |
| `200B` | ECU lifetime at first DTC | direct unsigned minutes |
| `200C` | key-on time at first DTC | raw x `15 sec` |

## BCM `0x40`

The fresh 20:57 status burst repeats the current-van BCM namespace already bounded in the candidate
DID inventory. It adds label and scaling evidence, not a new endpoint.

### Exact and group-exact correlations

| DID | status meaning | evidence / remaining boundary |
|---|---|---|
| `013B` | fuel level | `64 -> 100%`; association exact, physical scale has only one point here |
| `013C` | ambient temperature | two snapshots establish `raw x 0.5 - 40 deg C`: `74 -> 18`, `76 -> 19` |
| `1000` | engine speed | exact label association; zero-only capture does not establish scale |
| `1002` | vehicle speed | exact label association; zero-only capture does not establish scale |
| `1004` | battery voltage | raw x `0.1 V`; observed across BCM snapshots and Uconnect |
| `1204` | battery ADC voltage | unsigned 16-bit big-endian `/1000 V`; `2FCD -> 12.24 V` |
| `2949` | Logistic Mode | exact label association |
| `0133` | External Lights Inputs | group-exact; Alfa requested it twice and emitted the same four-label block twice |
| `0135` | 14 steering-wheel-switch states | group-exact; the APK definition also contains 14 fields |
| `0136` | four wiper/climate inputs | group-exact; both request and rendered block were duplicated |
| `0150` | external-light outputs | group-exact; individual output bits remain unresolved |
| `292D` | configuration-check-fail counter | direct integer |
| `292E` | PROXI write counter | direct integer |
| `2023` | PROXI/system-configuration readback | the 250-byte positive read matches the later of two previously captured current-van `2E 2023` payloads at all unredacted bytes |

Strong order candidates are `0130` for the door-input group; `0131/0132` for the vehicle-status
and brake groups; `0137/0138` for Generic Inputs; `0151` for windows/doors actuators; and
`0152/0153/0154` for internal-light, wiper/climate-output, and FPS material. `2010` and `1921`
jointly feed the BCM ignition/power-mode section, but their individual split is unresolved.
The `40A1/40A2/40A3/40A6/40AA` records feed the configuration-fail snapshot, without a defensible
per-module bit assignment yet.

### AlfaOBD rendering defects

The fresh BCM snapshot proves that the text renderer is not authoritative for every value:

- `1008=000389A0` is 231,840 minutes, but Alfa displayed `905`, exactly the truncated first three
  response bytes interpreted as an integer;
- `1009=0023` should render 525 seconds under the repeatedly verified x15 convention, but Alfa
  displayed zero; and
- `2013=02` displayed `Not defined` in this snapshot even though the same raw value renders a locked
  state in ICS, Uconnect, and the earlier BCM snapshot.

Raw responses therefore take precedence over an isolated Alfa text value when they conflict.

## ICS `0x85`

The common odometer/lifetime DIDs above align exactly. The remaining useful joins are strong ordered
candidates pending one controlled button or dimmer change:

| DID | candidate status group | current evidence |
|---|---|---|
| `0263` | CAN battery voltage | `4B -> 12.00 V`; candidate `0.16 V/LSB` scale |
| `0200` | switchbank local voltage | `78 -> 12.00 V`; candidate `0.1 V/LSB` scale |
| `027E` | HVAC/button states | all buttons were off |
| `027F` | indicator LED states | all reported LEDs were off |
| `0300` | dimming controls | bytes 0, 1, 2, 6, and 7 match five displayed values at `0.5%/LSB`; remaining field positions need decoding |

The profile offered no Plots page, but a status refresh already reads the button, LED, and dimming
groups in one short transaction. That is a better controlled-correlation surface than manually
cycling hundreds of gauges.

## Uconnect `0x87`

| DID | status meaning | confidence / decode |
|---|---|---|
| `180C` | volume plus bass/treble/balance/fade/midrange | exact packed group: `11 0A 0A 0A 0A 0A` rendered volume 17 and five neutral settings |
| `1823` | ECU temperature | exact association; `5B -> 51 deg C` supports a one-point candidate `raw - 40 deg C` scale |
| `283F` | camera brightness | exact association; `1E -> 30%` supports a one-point candidate direct-percent scale |
| `2921` | available-language table | exact packed ASCII match to the rendered tags/availability flags |
| `1002` | vehicle speed | exact label association; captured at zero |
| `1004` | battery voltage | raw x `0.1 V`; `78 -> 12.0 V` |
| `280B` | software level, part level, and sales code | exact packed ASCII (`145`, `3`, `UBC`) |
| `2A00` | Bluetooth MAC | exact six-byte match |
| `F191-F195` | hardware/software identity fields | exact rendered identity association; retain each DID separately |

`280C/280E/280F` are strong ordered candidates for the Internal Settings, Brand Splash, and
country-code-override fields. In particular, `280E=04` rendered `Nissan` and `280F=00` rendered
`Disabled`, but a controlled change is still preferable before promotion. `1806` likely supplies
the aggregate current-frequency group, while `1820/1821` likely contain panel and steering-wheel
button states. Displayed zero frequencies must not be assigned to `1803`, `1804`, or `1808`:
those reads ended in explicit negative responses, so the UI may merely have shown defaults.

## EMCM2 `0xD9`

The common odometer/lifetime DIDs align exactly. `1921` is group-exact for the 16-item operational
mode/ignition block. The initial AlfaOBD snapshot read both `2A00=0000` and `2A01=0000`, so its
rendered rotary/power/mute group could not establish the internal split.

A controlled follow-up disconnected AlfaOBD and alternated only physical `22 2A00` and `22 2A01`
reads through the PCAN after an exact `10 03` response. The vehicle was parked, ignition on, engine
off, and the owner changed one physical control at a time:

| DID response data | controlled input | result |
|---|---|---|
| `2A00=0000` | all controls at rest | repeated baseline |
| `2A00=4100` | left Volume/Power knob clockwise | exact; two captured samples |
| `2A00=8100` | left Volume/Power knob counterclockwise | exact; one captured sample |
| `2A00=0041` | right Tune/Enter/Scroll knob clockwise | exact; three captured samples across two bursts |
| `2A01=0100` | discrete Mute button held briefly | exact; eight consecutive samples over about 1.4 seconds |
| `2A01=0010` | discrete Screen Off button held briefly | exact; ten consecutive samples over about 1.9 seconds |

This establishes that `2A00` uses separate direction bytes for the left and right knobs. `41` means
clockwise and `81` means counterclockwise for the left knob; `41` also means clockwise for the right
knob. The corresponding right-knob counterclockwise value `0081` is a symmetry hypothesis only: a
later attempt produced no nonzero sample and does not verify it. That retry followed the Screen Off
test and the display state was not recorded, so screen state remains a plausible confounder. `2A01`
independently contains the Mute and Screen-button states above. Do not relabel the observed Screen bit
as AlfaOBD's generic `Power Button` without the still-missing decoder association.

The exact-vehicle OEM `ENTERTAINMENT MULTIMEDIA CONTROL MODULE (EMCM) - OPERATION` page says both
knobs can be twisted, shifted in four directions, or pressed; it describes a long left-knob press as
Radio On/Off and a brief right-knob press as confirm/OK. The source is the local OEM mirror file
`~/dev/ram_2022_GAS/vehicle/accessories_and_optional_equipment/relays_and_modules_-_accessories_and_optional_equipment/entertainment_system_control_module/description_and_operation/components/entertainment_multimedia_control_module_(emcm)_-_operation.html`.
The owner reported no perceptible axial travel on either installed knob, and ordinary attempted
presses produced no `2A00/2A01` change. No extra force was applied. Preserve this as an
OEM-versus-installed-control discrepancy: the no-event attempt is not evidence that the input cannot
exist, and the knobs should not be forced.

`1004=7A` is consistent with 12.2 V under the convention above, but this EMCM2 renderer emitted no
corresponding label. Keep it unresolved in the EMCM2 namespace until independently observed.

## Climate `0x98`: reject the selected gauge definitions

AlfaOBD warned that model/ISO verification failed because the installed ECU's
`F1A5=000A702520` does not match the selected `COND_MARELLI_EP` profile. Continuing only for a
non-actuating diagnostic observation produced 92 complete samples from this loop:

| request | response in the loop |
|---|---|
| `22 0260` | `7F 22 31` |
| `22 0261` | `7F 22 31` |
| `22 0262` | positive, one byte |
| `22 0263` | positive, one byte |
| `22 0264` | positive, two bytes |
| `22 0273` | positive, two bytes |
| `22 0274` | positive, two bytes |
| `22 0276` | positive, two bytes |

The rendered results were engine speed `NA`, vehicle speed `NA`, water temperature `-39 deg C`,
battery voltage `0.160 V`, outside temperature `63.5 deg C`, fan control `0%`, mixer `0.6%`, and
distribution motor `0.1%`. Every rendered value was constant or `NA`; raw `0263` and `0276` each
showed one alternate response. The rendered values contradict known vehicle state. None of those
label-to-DID assignments or scales is valid current-van evidence.

This is an important limitation of treating AlfaOBD as an oracle: a selected profile can poll
unsupported DIDs and can apply definitions from the wrong variant even when some requests receive
positive responses. Exact subtype verification and raw/log correlation remain mandatory.

## Optional-profile retries

The Display `0x6A`, Trailer Tow `0x4A`, Left Blind Spot `0x62`, and Right Blind Spot `0x65`
profiles again ended in `NO DATA`. The Alfa Debug file retained 82 no-response `10 03` attempts;
its `0x65` tail is incomplete. The independent PCAN trace establishes the complete wire result:
exactly 24 session attempts to each of the four addresses and zero response frames at all four
matching response IDs. Combined with the earlier direct F1A5/F187 timeouts, this makes
absent/unfitted hardware more likely, but still does not prove absence under every power/session
condition. Do not repeat the same probes without new exact-vehicle or installed-equipment evidence.

## Safety and capture provenance

The decoded Debug log contains only these UDS request services:

| service | count | purpose |
|---|---:|---|
| `10` | 88 | all were `10 03`; six positive, 82 no-response attempts retained in the Debug file |
| `22` | 1,069 | read data by identifier |
| `3E` | 2,593 | TesterPresent while Alfa screens remained connected |

There was no write, IO control, SecurityAccess, routine control, DTC clear, reset, communication
control, coding, or PROXI operation in this fresh session. Session control and DID reads are active
diagnostic traffic: successful `10 03` requests transiently changed ECU diagnostic-session state,
but no mutating or actuation service was observed.

The controlled EMCM2 follow-up likewise used only extended-session control, TesterPresent, and
physical reads of `2A00/2A01`. Across its three bounded files, all 5,733 request attempts received
positive responses and produced 2,729 sample rows: 2,728 complete `2A00/2A01` pairs plus one final
`2A00`-only row at the duration boundary. The first file ended at its duration limit; the two
follow-ups were intentionally interrupted after their action windows. All three persisted reports
record `restored_passive=true`. The third file's all-zero right-counterclockwise window is preserved
as an unresolved attempt, not promoted as a negative control result.

The complete B-CAN wire capture independently contains 101 single-frame service-`10` requests,
930 service-`22` requests, 509 service-`3E` requests, and 33 ISO-TP flow-control frames across
physical tester IDs. It contains no other request service. After separating the 68 unattributed
identity reads below, the Alfa-attributed B-CAN portion is 101 session requests, 862 DID reads, and
509 TesterPresent requests. This wire-level audit covers the optional-profile tail missing from the
Debug file and reaches the same service-boundary conclusion.

The PCAN capture is a mixed-bus observation, not an Alfa-exclusive request log. It also contains
68 unattributed, non-mutating `22 F132`, `22 F100`, and `22 F1A0` identity reads across 14 physical
addresses. The source process was no longer running when inspected and cannot be assigned with
confidence. Use Alfa's Debug bin for exact app request counts and the PCAN trace for physical
branch, CAN-ID, and timing corroboration. The extra traffic does not alter the safety conclusion;
its observed payloads were read-only identity requests.

## Raw provenance

All raw/machine output remains gitignored:

- `tmp/captures/bcan/events/bcan_alfaobd_gauges_ignition_on_20260721_2150.log` — 26,391,047
  bytes, 435,955 parsed frames, zero parse failures, and 88 CAN IDs; SHA-256
  `61b5522c2931c9b97b2fd55359b6937bbdf3be9cb29661fe057a1bcf9edf4997`.
- `tmp/ecu_mapping/android_tablet/live_20260721_2150/final/logs/AlfaOBD_Debug.bin` — 717,536
  bytes; SHA-256 `4e64988d3f33b7068faf9bf92909b63f8030c8bf4dc1489d4880dae07c0f0578`.
- `tmp/ecu_mapping/android_tablet/live_20260721_2150/alfaobd_campaign_suffix.bin` — the exact
  post-baseline suffix, 430,816 bytes; SHA-256
  `12fd512ea3a74c64c34f86453dd8c915d5d20376dc53e087877a9cf0a0ff4a45`.
- `tmp/ecu_mapping/android_tablet/live_20260721_2150/final/logs/Gauges_Data.csv` — 5,830 bytes;
  SHA-256 `a9710f326142e7d2c105ca17f24104930e79cacc063d1d9ab460382f013357ab`.
- `tmp/compute/done/20260722T044043Z-8b68bb4c/result/summary.json` — bounded worker summary;
  SHA-256 `b41f9a42d40a7b59ebc0fd90d59ee3d2c286f00ea9e5acbc96a7761a5aa5db23`.
- `tmp/sweeps/emcm2_controls_20260721_2340.json` — 1,426 samples / 2,994 positive requests;
  SHA-256 `86e0e2b76679031dd6f9a4e877f0c774ec000c08f1e9291b1ae567afa2671282`.
- `tmp/sweeps/emcm2_controls_part2_20260721_2345.json` — 639 samples / 1,343 positive requests;
  SHA-256 `c522de0ffe35031e58d51d4cc50f0ddb3a21ca321852feb8cb1432db2c675e3b`.
- `tmp/sweeps/emcm2_right_ccw_20260721_2350.json` — 664 samples / 1,396 positive requests;
  SHA-256 `e3d24057fd2e7246299b0575d45d8b6b00b0d51240238321c7246680bbf0858e`.

The pulled BCM text log contains the full VIN and must remain under `tmp/`. This finding deliberately
contains no unique VIN serial.

## Next evidence-producing work

Broad connection and status coverage is complete. The next high-yield work is a short controlled
change against the existing candidate groups:

1. ICS: press one HVAC button and change one dimmer state while refreshing status; compare `027E`,
   `027F`, and `0300`.
2. Uconnect: change volume by one step and press one panel/steering-wheel button; compare `180C`,
   `1820`, and `1821`.
3. EMCM2: the left-knob directions, right-knob clockwise direction, Mute, and Screen inputs are now
   controlled mappings in `2A00/2A01`; do not repeat them. A future short capture may isolate the
   unresolved right-knob counterclockwise value by polling only `2A00` with the screen known on and
   using baseline -> counterclockwise -> rest -> repeat. Revisit OEM-described knob press/shift
   inputs only if the owner first confirms their normal physical operation; never force the controls.
4. BCM work remains on C-CAN. Refresh one bounded status group around a deliberate door/lock/input
   change rather than repeating the complete status sweep.
5. Do not reuse the current Climate gauge profile unless an exact installed-subtype definition is
   recovered. Do not retry optional addresses without a new power-state or equipment lead.
