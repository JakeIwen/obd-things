# C-CAN AlfaOBD live correlation — 2026-07-22

## Outcome

A parked AlfaOBD/PCAN campaign exercised the six under-mapped C-CAN profiles that were useful
without starting the engine: cluster `0x60`, TBM2 `0xC6`, electronic shifter `0x1F`, TCM `0x18`,
BCM `0x40`, and PCM `0x10`. AlfaOBD used the OBDLink MX+ while the PCAN independently listened
on the pigtail's C-CAN DB9 (DLC 6/14) at 500 kbit/s.

The campaign produced four durable results:

1. It tied the installed ECU identities to the AlfaOBD runtime profiles actually used and recovered the
   raw DID polling loops behind the cluster, TCM, and PCM monitor selections.
2. Controlled driver-door and service-brake changes resolved four BCM status-group DIDs and
   several passive broadcast candidates. These are immediately useful for zero-transmit state
   monitoring, while exact source/bit semantics still need the discriminating tests listed below.
3. The same PCM profile that timed out while the ignition had slept connected after ignition was
   rearmed, still with the engine off. Engine running is therefore **not** required for the PCM's
   padded legacy `10 92` session.
4. It demonstrated more AlfaOBD rendering errors on otherwise correctly addressed ECUs. Raw
   request/response bytes and controlled physical state take precedence over an isolated label.

No AlfaOBD output test or diagnostic actuation was used. The only physical changes were the owner
opening/closing the driver door and pressing/releasing the normal brake pedal.

## Conditions and topology

- Vehicle parked, transmission in Park, ignition on, engine off. The ignition slept before the
  first PCM attempt and was then rearmed; that sleep/wake transition became useful evidence.
- AlfaOBD 2.4.4.0 ran on the owner's Android tablet, controlled through USB ADB. The OBDLink MX+
  remained on the OBD branch of the SGW-bypass Y splitter.
- PCAN was connected to the parallel pigtail C-CAN DB9, configured 500 kbit/s and listen-only. It
  remained error-active with zero TX/RX error counters at the end of the campaign.
- No external AlfaOBD yellow, blue, or grey adapter was installed.
- Debug Data and Gauges Data recording were enabled. The Debug bin grew as expected, but
  `Gauges_Data.csv` did not change, so no label-to-DID claim below relies on that CSV.

## Source scope and integrity

The campaign workspace is gitignored at
`tmp/ecu_mapping/android_tablet/ccan_live_20260722_001010/`. Its extracted Debug suffix contains
three `F190` VIN reads and only the expected current van, masked here as `3C6LRVDG4NE######`.

| artifact | bytes / frames | span or SHA-256 |
|---|---:|---|
| `alfaobd_campaign_suffix.bin` | 2,232,618 bytes | `f0042e9e40428572958d2900eab0af4550bc8b642d61bdb30587dbb749c67912` |
| `alfaobd_campaign.decoded.txt` | 1,116,309 bytes | `6fe10e77ba22cd22355d8aa18603351bb46f686fe4a8606f73bdef8371593eef` |
| final `AlfaOBD_Debug.bin` | 2,962,534 bytes | `3d34fb2101fc064660f994b2139aba8c8ac54feb84b109c7bffcadda67447334` |
| C-CAN `..._0010.log` | 4,693,618 frames; 0 unparsed | 1,728.167456 s; `b51187de3dbacb819ab0268c5728eda5ba4c0e31132188f2546f1a5ad84c0086` |
| C-CAN `..._0041.log` | 4,886,380 frames; 0 unparsed | 1,799.997402 s; `a0029e1c5d880a1cca8c1153adbe6ccfa29b7b8d15a77c4752159fa91be562e5` |
| C-CAN `..._0113.log` | 1,481,255 frames; 0 unparsed | 874.866003 s; `cc94273166401fca1de43eeb204ad119a8033dca3e38c7679d71ae45e6cb8468` |

Complete machine summaries are under `tmp/inventories/ccan_alfaobd_20260722/`. Raw screenshots
and TBM2 logs contain private telematics identifiers and must remain under `tmp/`.

### Diagnostic-service audit

The decoded suffix contains 11,323 completed/request-flushed exchanges. The 8,979 non-TesterPresent
requests form 574 distinct address/request keys across the six profiles:

| service | count | purpose in this campaign |
|---|---:|---|
| `10` | 30 | five `10 03` profile handshakes; 24 unanswered and one positive PCM `10 92` attempt |
| `1A` | 7 | legacy PCM identification reads |
| `21` | 17 | legacy PCM local-identifier reads |
| `22` | 8,925 | identity, status, and monitor reads |
| `3E` | 2,344 | TesterPresent while AlfaOBD held a connection |

The reassembled command audit found no `11`, `14`, `27`, `2E`, `2F`, or `31`: no ECU reset,
DTC clear, security access, write, IO control, or routine control occurred. PCAN itself transmitted
nothing.

## Installed profiles and raw coverage

Counts exclude `3E`; "distinct" means a distinct complete request in that ECU namespace, not a
global DID count.

| ECU | selected menu -> connected runtime profile | installed identity evidence | requests / distinct |
|---|---|---|---:|
| cluster `0x60` | `Instrument Panel Marelli/Siemens EP` -> `Instrument panel Continental` | `F1A5=0003507420`; part `68517084AD`; HW `50019990002`; Magneti Marelli supplier fields | 1,392 / 63 |
| TBM2 `0xC6` | `Telematic Box Module 2` -> `Telematics box module 2 Marelli` | `F1A5=0023506920`; HW `TBM200A11P`; part `68510377AC`; vehicle SW `52225318` | 94 / 93 |
| shifter `0x1F` | `Electronic Shift Module` -> `Electronic Shifter Module SILATECH` | `F1A5=0016507A19`; drawing `P7FK46LXHAD`; HW `073250002B0`; SW `AGSM637FCA.` | 47 / 46 |
| TCM `0x18` | generic `Marelli AUTO SHIFT` -> `ZF 948TE 9 speed Automatic Transmission` | `F1A5=5285040D3D`; ZF; drawing `46342086`; part `68532161AF` | 5,482 / 72 |
| BCM `0x40` | `Body Computer Delphi/Marelli/Aptiv` -> `Body computer Marelli` | `F1A5=0000607719`; exact APK subtype Device 55851; prior live part `68524831AF` | 275 / 91 |
| PCM `0x10` | `Chrysler Tigershark/Fire/GSE/SGE/Pentastar` -> `Chrysler Pentastar/Hemi engine Model Year 2021` | legacy identity contains part `68532157AI`; rendered installed model year 2022 and 3.6 V6 | 1,689 / 209 |

The menu/runtime naming conflicts are aliases, not evidence for new endpoints. The physical pairs
remain those in `lib/modules.py`.

### Monitor loops recovered without Gauges CSV

AlfaOBD's raw trace still exposes the repeated DID set behind each selected watcher group:

| ECU | selected watcher surface | repeated `22` DIDs |
|---|---|---|
| cluster | Dimming, Buzzer volume, Actual Gear, Engine speed, Fuel level, Vehicle speed, Battery Voltage (+30), Outside temperature | `0104 0105 0107 1000 1001 1002 1004 1005` |
| TCM | 11 groups covering ignition, cable/bus shifter state, PRND, current/target gear, clutch, paddle/sport, brake, torque intervention, park sensors, and kickdown | `0288 0516 0518 0519 051A 051C 0540 0805` |
| PCM | 12 selected groups covering crank/start-stop, fuel control, ESIM, filtered/instant digital inputs, and desired outputs | `0232 035A 03C2 0645 09A0 10AC 10AD 1102` |

This establishes bounded raw polling sets, not a one-to-one label assignment for every field. The
CSV did not record new rows, and several labels failed controlled-state checks. Known conventions
such as cluster `1000` engine speed, `1002` vehicle speed, and `1004` battery voltage remain
supported, but the other internal joins need controlled variation or the exact decoder.

The profile `.dat` files are also unsafe as campaign evidence. `ZF9HP.dat` was rewritten from
57,483 to 114,875 bytes, but every value row in the final file is exactly its baseline sequence
concatenated with itself. `TIGERSHARK_CUSW.dat` is byte-for-byte unchanged. These files are cached
plot series with no usable timestamp join here, not fresh labeled measurements. This is reproduced
by `tools/alfaobd_dat.py`; the comparison reports are
`tmp/inventories/ccan_alfaobd_20260722/{zf9hp_dat,tigershark_dat}.json`.

## Controlled BCM correlations

### Diagnostic DIDs

The status refreshes bracketed a closed baseline, an open driver door, and a held brake pedal.
The raw bytes changed even when AlfaOBD's rendered text did not:

| BCM DID | baseline -> controlled state -> restored evidence | bounded interpretation |
|---|---|---|
| `0130` | `8C` closed -> `88` driver door open -> `8C` in the later closed/brake snapshot | door-input group; mask `0x04` is driver-door-correlated in this trial |
| `0152` | `0000` closed -> `0002` driver door open -> `0000` later | internal-light group consequence correlated with the open door; do not call it a door-input bit yet |
| `0132` | `0020` brake released -> `0023` held | brake/status group; low-byte mask `0x03` changed |
| `0150` | `000000` brake released -> `006000` held | external-light-output group; middle-byte mask `0x60` is consistent with stop-light outputs |

During the open-door refresh AlfaOBD still rendered `Driver door open/close switch: Closed`.
During the held-brake refresh it still rendered both the brake pedal and stop lights as inactive.
This disproves those particular field decodes while retaining the DID-level group associations.

### Passive driver-door candidates

The full C-CAN trace records the physical door edges, independent of the later status-refresh taps:

| CAN ID / field | closed -> open -> closed | exact edge timestamps | confidence boundary |
|---|---|---|---|
| `0x4B1` byte0 bit0 | `0 -> 1 -> 0` | `1784703344.200652`; `1784703453.430247` | strongest binary driver-door-correlated candidate; another door must be tested before calling it driver-exclusive |
| `0x419` byte2 | `0x77 -> 0x97 -> 0x77` | `1784703344.260641`; `1784703453.460278` | second exact door-correlated field; the `0xE0` XOR may contain several states |

`0x412` byte2 advanced `E3/E4/E5/E6/E7` on its own cadence across the same windows. It is a
counter/time artifact, not a door bit. `0x73A` behaved similarly and is not promoted.

### Passive service-brake candidates

One ordinary pedal press began near epoch `1784704083` and release propagated near
`1784704155`. The following fields changed with the controlled hold and returned afterward:

| CAN ID / field | released -> held -> released | interpretation boundary |
|---|---|---|
| `0x1FA` byte3 bit1 | `0 -> 1 -> 0` (`00 -> 02 -> 00`) | cleanest high-rate binary service-brake candidate |
| `0x0FA` byte0 | `0x40 -> 0x48` during transition, then `0x4C` held -> `0x40` | high-rate brake state/pressure candidate; exact two-bit split unresolved |
| `0x10F` bytes1-3 | all zero -> nonzero and continuously varying -> all zero | high-rate analogue pedal/pressure candidate; no scale yet |
| `0x1F1` byte0 bit1; byte2 | bit1 cleared -> set -> cleared; byte2 zero -> varying -> zero | corroborating binary plus analogue candidate; unrelated byte0 bits also changed over time |
| `0x417` byte4 | `0x60 ->` mostly `0x40`, with `0x20` excursions -> `0x60` | correlated state/load candidate, not yet a single stable enum |
| `0x5A8` byte3 | `0x56 -> 0x76 -> 0x56` | low-rate propagated brake-state candidate |
| `0x5BE` byte2 | `0x00 -> 0x18 -> 0x00` | low-rate propagated brake-state candidate |

The known coarse-voltage field `0x41A` byte0 fell from `0xA0` to `0x9E/0x9C` and recovered only
after release. That is a secondary voltage/load effect consistent with the brake lamps, not a new
brake encoding. The same caution applies to any frame that merely tracks system voltage.

These candidates are useful for passive detection now, but a second press plus separate parking-
brake and brake-light-load experiments are still needed before assigning transmitter, physical
units, redundancy role, or exact bit names.

## Profile-specific failures that matter

- **Shifter:** AlfaOBD rendered `Lever position: Drive` while the van and the TCM's hardwired
  shifter decode were in Park. Do not use that enum as a selector-state oracle.
- **TCM:** all full-length responses in the eight-DID watcher loop remained byte-identical during
  the controlled brake press. Shorter apparent variants were truncated Debug fragments, not state
  changes. The selected `Brake switch` watcher therefore did not expose this van's live pedal state.
- **TBM2:** the exact-profile status page contained plausible registration/antenna state but also
  rendered backup-battery voltage `1.30 V` and charge `235%`. The impossible percentage invalidates
  that charge scale and makes the battery pair untrustworthy; the voltage needs an independent check.
- **PCM:** odometer, ignition state, oil life, and core engine identity were plausible, but several
  qualification/configuration labels described impossible vehicle variants. Treat the profile as a
  transport/DID candidate source, then validate each semantic field.

## PCM engine-off legacy-session result

While ignition was asleep, AlfaOBD sent 24 fixed-eight-byte `10 92` attempts to `18DA10F1`; none
received a response and the app reported `NO DATA`. The owner rearmed ignition without starting the
engine. The same selected profile and OBDLink setup then received exact `50 92`, followed by
positive legacy identity/status reads and the eight-DID monitor loop above.

This resolves the earlier engine-state ambiguity: the PCM answers its legacy diagnostic session
with ignition on and engine off. It does not prove that an unpadded SocketCAN request will work;
the known-good recipe remains 29-bit normal-fixed addressing with fixed-DLC-8 zero padding. It also
shows that silence during a timed-out ignition state is not evidence for a wrong PCM profile.

## What this buys and the next discriminating work

- A direct read-only monitor can now poll the bounded cluster, TCM, and PCM raw DID sets without
  manually paging through hundreds of AlfaOBD watchers.
- Passive C-CAN can detect a driver-door-correlated state and several independent service-brake
  correlates without opening a diagnostic session.
- PCM live reads no longer require engine idle merely to connect; idle is needed only when the
  experimental question needs RPM/temperature/load variation.

The highest-yield next experiments are:

1. Passenger-door open/close under the same capture to distinguish driver-exclusive `0x4B1`,
   `0x419`, BCM `0130`, and the internal-light consequence `0152`.
2. One repeated service-brake hold, then a separate parking-brake change, to reject coincidental or
   shared-brake fields. A brake-light electrical-load discriminator can isolate the `0x41A` effect.
3. One engine-idle comparison using the already recovered PCM/TCM loops—no new broad inventory—and
   optionally a stable 1,500-rpm point if the owner is present and conditions are safe.
4. Later controlled gear/speed observations for the cluster/TCM fields during an ordinary drive.

Any AlfaOBD output test, routine, coding, DTC clear, or proposed replay remains a separate actuation
step requiring its own exact review and authorization.
