# Promaster bus map — master reference

2022 Ram Promaster (VIN 3C6LRVDG4NE######). Verified facts, explicit candidates, and remaining
unknowns are labeled with their confidence/provenance below. This is the single place to learn
what is already mapped on each bus before starting new reverse-engineering.

> **Maintenance rule:** when you verify a new broadcast frame, decode, or wake behavior, add it
> here **in the same change** — with its provenance (which capture/finding proved it) and a
> confidence note. A fact that lives only in a session's memory or a code constant is a fact the
> next agent can't find. Module *addressing* is the exception: its source of truth is
> `lib/modules.py` (tools execute it, so it can't silently drift) — this doc only summarizes it.

Provenance shorthand: capture logs live under `tmp/captures/` (bus-state reference captures
`ccan/`, `bcan/`, each with a `wake_from_*` set in `events/`). Committed evidence lives in
`projects/<x>/findings/`.

---

## Physical buses

| bus | rate | where | access | notes |
|---|---|---|---|---|
| **C-CAN / HS-CAN** | 500 kbit/s | OBD pins **6/14** | PCAN via **SGW bypass** (ECRI tap on internal C-CAN) | powertrain + diagnostics. `bringup.sh` default. The bypass is why our UDS reaches gated modules at all; legislated OBD-II Mode 01 PIDs do NOT route through it. |
| **B-CAN / CAN IHS** | **125 kbit/s, live-verified** | DLC pins **3/11** (OEM: CAN IHS +/−) | PCAN through the **B-CAN DB9** of the owner's labeled dual-pair OBD pigtail; `bringup.sh --bcan` | Comfort/body effects (locks, lights, interior) and the established B-CAN signature set were captured with listen-only explicitly on and zero RX errors on 2026-07-20. Owner confirmed the pigtail directly selects the documented 2022 ProMaster B-CAN pair; the DIY yellow adapter has never been used on this van. The installed AlfaOBD selector independently renders catalog adapter `6` as `MS-CAN BLUE`; a 2026-07-21 live pass verified four of its eight 29-bit diagnostic targets and left four unresolved. [Evidence](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_bcan_live_ecu_discovery.md). |
| **CAN CH / second high-speed CAN** | **500 kbit/s, live-verified** | DLC pins **12/13** (OEM: CAN CH +/−) | PCAN through the owner's grey adapter; vehicle 12/13 remapped to interface 6/14 | A 2026-07-25 passive PCAN capture at 500 kbit/s had zero TX/RX errors while AlfaOBD independently completed identification/DTC reads from installed ABS (`0x28`), EPS (`0x30`), HALF (`0x31`), and ORC (`0xC0`). Addresses `0x26` PAM and `0xA0` park assist remain configured absent/unverified. [Evidence](../projects/ecu_mapping/findings/promaster_2022/2026-07-25_canch_live_verification.md). |

The currently configured C-CAN/B-CAN modes come up **passive (listen-only)** by default;
`bringup.sh --tx` arms transmission (UDS needs it).

The DLC pin names above come from local OEM diagram `2022_VF_EN_18-000-000`, revision 2064:
`/home/pi/dev/ram_2022_GAS/diagrams/systems/data_link_connector.html`. That source establishes
physical topology, not bitrate. Its companion CAN C/CH/IHS topology diagrams identify attached
modules. The labeled pigtail plus the 2026-07-20 passive captures tie CAN IHS/B-CAN to pins
3/11 at 125 kbit/s; the 2026-07-25 grey-adapter capture independently ties CAN CH to pins
12/13 at 500 kbit/s.

[AlfaOBD's current hardware guide](https://alfaobd.com/) independently identifies pins 12/13 as
the second high-speed CAN bus for 2022+ ProMaster and describes the supported PowerNet/CUSW layout
as high-speed CAN on 6/14 plus middle-speed CAN on 3/11. Its
[vehicle table](https://www.alfaobd.com/supported_cars.html) specifically assigns the grey
second-high-speed adapter to `RAM PRO MASTER (VF) 2022+`, rather than the yellow adapter used by
pre-2022 VF. This is strong diagnostic-tool-vendor provenance for bus class and adapter routing.
The 3/11 rate is independently live-verified at 125 kbit/s, and the later 12/13 capture independently
established 500 kbit/s plus four installed diagnostic endpoints.

The exact-vehicle OEM `COMMUNICATION / CAN BUS DESCRIPTION` at
`/home/pi/dev/ram_2022_GAS/data_pages/article/63088/guid/na-cr22vf-GUID-4C3C4E91-36D8-4B2A-A666-DF07A5921AF8_html.html`
explicitly calls CAN-C **500K** and CAN-B **50K**. Its layout uses additional branch labels
(`C-1` through `C-8` and `BH`) while the DLC diagram says CAN C/CH/IHS. The document's 50K statement
therefore describes a differently named internal branch or conflicts with the exposed DLC branch; it
does **not** override the live 125-kbit/s measurement on pins 3/11. The related-platform 50-kbit/s lead
is retained as historical research context, not as the current survey plan, in
[`2026-07-19_related_platform_bus_leads.md`](../projects/ecu_mapping/findings/promaster_2022/2026-07-19_related_platform_bus_leads.md).

---

## UDS modules (addressing → `lib/modules.py` is source of truth)

29-bit ISO-TP normal-fixed unless noted. This table adds the bus + operational quirks the
registry can't hold; keep the addresses in sync with `lib/modules.py`.

| key | module | bus | TX → RX | quirks |
|---|---|---|---|---|
| `radar_acc` | Bosch ACC radar (DASM / MRR1evo) | C-CAN | `18DA2AF1` → `18DAF12A` | ACKs our frames even with ignition cut mid-sweep. Speed only via DID `0x1002` (no OBD PIDs behind SGW). |
| `pcm` | Powertrain Control Module (3.6L Pentastar) | C-CAN | `18DA10F1` → `18DAF110` | Fixed-DLC-8 padded `10 92 → 50 92`, then `1A 87 → 5A 87` containing `68532157AI`, was independently verified while idling on 2026-07-21. AlfaOBD repeated the positive `50 92` ignition-on/engine-off on 2026-07-22; the identical Alfa profile/setup timed out while ignition was asleep and succeeded after rearm. The 2026-07-26 simultaneous Plots/wire campaign mapped eleven runtime DIDs, including oil pressure `022A`, coolant `011D`, and signed current torque `06DA`; see the per-module summary below. |
| `rf_hub` | RF Hub (Continental) — TPMS/RKE | C-CAN | `18DAC7F1` → `18DAF1C7` | **Answers with ignition OFF** (battery-powered RKE receiver). |
| `tcm` | ZF 948TE transmission controller | C-CAN | `18DA18F1` → `18DAF118` | Live identity on 2026-07-19: `F187=46342086`, `F194/F132=68532161AF`, `F192=ES11-1065 D`. |
| `shifter` | SILATECH electronic shifter | C-CAN | `18DA1FF1` → `18DAF11F` | Live identity on 2026-07-19: `F187=P7FK46LXHAD`, `F188/F194=AGSM637FCA`. |
| `bcm_ccan` | Body Control Module, C-CAN endpoint | C-CAN | `18DA40F1` → `18DAF140` | Live identity on 2026-07-19: `F187=68524831AF`, `F192=BC637M.0001`; actuation remains power-mode gated. |
| `cluster` | Marelli Instrument Panel Cluster (IPC) | C-CAN | `18DA60F1` → `18DAF160` | Live identity on 2026-07-19: `F187=68517084AD`, `F192=50019990002`. FCA's [NHTSA Part 573 filing](https://downloads.regulations.gov/NHTSA-2023-0046-0001/attachment_1.pdf) identifies `68517084AD` as the Marelli IPC. |
| `telematics` | Global Telematics Box Module (TBM2) | C-CAN | `18DAC6F1` → `18DAF1C6` | Live identity on 2026-07-19: `F132=68510377AC`, `F192=TBM200A11P`. The TBM string, exact-part [Mopar catalog supersession](https://www.moparpartsgiant.com/parts/mopar-module-telematics~68647858aa.html), and exact-vehicle local OEM TBM2 procedure make the role high-confidence. |
| `ics_bcan` | Integrated Center Stack customer-interface panel | B-CAN | `18DA85F1` → `18DAF185` | Live identity on 2026-07-21: `F1A5=0032701720`, `F187=7DN08LXFAB`; exact APK subtype match plus exact-vehicle OEM CAN-IHS role. |
| `uconnect_bcan` | Uconnect radio/display module | B-CAN | `18DA87F1` → `18DAF187` | Live identity on 2026-07-21: `F1A5=0024701A19`, `F187=60986318`; exact global APK UCONNECT subtype/address match plus Radio/DSM DTC families. |
| `climate_bcan` | Electronic Climate Control / HVAC module | B-CAN | `18DA98F1` → `18DAF198` | Live identity on 2026-07-21: `F1A5=000A702520`, `F187=68516124AE`; AlfaOBD routing plus exact-vehicle HVAC DTC evidence. |
| `emcm2_bcan` | EMCM2 center-stack menu/volume controls | B-CAN | `18DAD9F1` → `18DAF1D9` | Live identity on 2026-07-21: `F1A5=0066708320`, `F187=7DN14LXHAF`; exact APK subtype match plus exact-vehicle OEM CAN-IHS role. |
| `abs_canch` | ABS/ESC | CAN CH | `18DA28F1` → `18DAF128` | Live grey-adapter identity on 2026-07-25: `F1A5=0006501520`, `F187=68516283AD`; C1200 was history/intermittent and last test passed. |
| `eps_canch` | Electric Power Steering (ZF/TRW) | CAN CH | `18DA30F1` → `18DAF130` | Live grey-adapter identity on 2026-07-25: `F1A5=0002507919`, `F187=68509191AD`; no faults reported. |
| `half_canch` | HALF driver-assistance / forward-camera module | CAN CH | `18DA31F1` → `18DAF131` | Live grey-adapter identity on 2026-07-25: `F1A5=001E502920`, `F187=68567254AA`. |
| `orc_canch` | Occupant Restraint Controller / airbag | CAN CH | `18DAC0F1` → `18DAF1C0` | Live grey-adapter identity on 2026-07-25: `F1A5=001A507720`, `F187=68518674AC`; no faults reported. |

Four B-CAN diagnostic endpoints are now registered from exact physical request/response captures.
This does **not** validate the previously listed `0x75C`, `0x760`, `0x762`, `0x764`, `0x768`, and
`0x7C0` guesses: those are fixed-payload 1–2 Hz application broadcasts, not an ISO-TP family, and
the one observed `0x7B8` frame contained ASCII `3231`. The earlier 111-second AlfaOBD RF Hub
capture had no B-CAN diagnostic exchange because that session addressed RFH and BCM through their
29-bit C-CAN endpoints. Do not infer an 11-bit `+8` pair without an actual exchange. See the
[`B-CAN pair verification`](../projects/ecu_mapping/findings/promaster_2022/2026-07-20_bcan_pair_verification.md)
and the subsequent [`live B-CAN discovery`](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_bcan_live_ecu_discovery.md).

The maintained AlfaOBD-derived B-CAN profile remains
`18DA{4A,62,65,6A,85,87,98,D9}F1`, with corresponding `18DAF1xx` responses. Addresses `85`,
`87`, `98`, and `D9` answered both F1A5 and F187 on 2026-07-21. Addresses `4A`, `62`, `65`, and
`6A` timed out to both reads and remain unresolved/possibly optional, not proven absent. Dry-run
the bounded set with `python3 tools/ecu_discover.py --profile promaster88-bcan`; a live rerun is
active diagnostic traffic and still requires the profile-specific confirmation and normal safety
gates. [Catalog/UI evidence](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_alfaobd_apk_catalog.md#adapter-routing-recovered-from-the-live-application-selector).

A later simultaneous AlfaOBD/PCAN observation corroborated the branch routing: time-aligned
`0x85/87/98/D9` exchanges appeared on the pins-3/11 B-CAN capture, while a BCM `0x40` status sweep
inside the same capture window produced no `18DA40F1/18DAF140` B-CAN frame. The standard BCM
profile therefore used C-CAN in this setup while adapter-6 profiles used B-CAN. The same observation
yielded status-DID candidates but rejected the selected Climate profile's gauge labels/scales for
the installed unmatched subtype. [Evidence](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_alfaobd_live_status_correlation.md).

> **PCM legacy-session requirement:** `0x10` was independently verified from the PCAN tap on
> 2026-07-21. While parked with the engine idling, fixed-DLC-8 zero-padded
> `18DA10F1 -> 18DAF110` traffic produced exact `10 92 -> 50 92`, followed by a positive
> multi-frame `1A 87 -> 5A 87` identity containing `68532157AI`; FCA's official J2534 report maps
> that part to a 2022 VF 3.6L PCM calibration. A 2026-07-22 AlfaOBD follow-up repeated `50 92` with
> ignition on and the engine off, then collected positive legacy status/live-data reads. The same
> app/profile timed out while ignition was asleep and succeeded after rearm, so engine running is
> not required. Fixed-DLC-8 padding remains part of the known-good recipe; do not infer ordinary
> default-session `22` support. See
> [`2026-07-22 C-CAN live correlation`](../projects/ecu_mapping/findings/promaster_2022/2026-07-22_ccan_alfaobd_live_correlation.md#pcm-engine-off-legacy-session-result).

---

## C-CAN broadcast frames (passive-readable)

| id | field | decode | meaning | when present | confidence |
|---|---|---|---|---|---|
| `0x2EF` | payload | unresolved / mode-dependent | **not an approved voltage source.** Historical `FF 11` / `0F 15` payloads happened to track ignition/engine state, but live `FF 21` disproved the former low-13-bit `/400` interpretation. Retain only the presence meaning below until multiplexing is mapped. | **ignition ON / running only** | voltage interpretation withdrawn 2026-07-25 |
| `0x2EF` | presence | — | **ignition-on gate** — its presence = key-on; tpms-logger uses it as the drive/park gate | ignition ON | verified (frame-count gates failed; presence gate works) |
| `0x41A` | byte0 | `4.0 + raw × 0.05 V` | **system voltage** — C-CAN analogue of 0x46C, readable in a parked wake | any awake C-CAN incl. parked wake | **verified** against BCM +30/ADC status and a controlled charger transition |
| `0x41D` | byte2 | `raw × 4 kPa` | **engine oil pressure**. Byte-for-byte raw mirror of Alfa-associated PCM DID `022A`; engine-off zero, start rise, and hot-idle-like 204–208 kPa behavior agree with the exact-vehicle mechanical context. | ignition ON / running | **observed Alfa scale; receive-only telemetry allowlisted**, 733/733 simultaneous samples, R² 0.9998047; [2026-07-26 evidence](../projects/ecu_mapping/findings/promaster_2022/2026-07-26_pcm_plots_idle_mapping.md) |
| `0x2ED` | byte0 | `raw - 40 °C` | **engine coolant temperature**. Exactly related to Alfa-associated PCM DID `011D`, whose own raw form is byte - 64 °C. | ignition ON / running | **observed Alfa scale; receive-only telemetry allowlisted**, 733/733 simultaneous samples, effectively exact R² 1.0; same evidence |
| `0x100` | byte2 | wrap/mode unresolved; idle-range fit looks like `byte - 244 Nm` | strong **current-engine-torque-related candidate** tracking signed PCM DID `06DA` (DID raw x 0.04 Nm). The apparent idle formula cannot represent loaded positive torque without a wrap or mode rule, so it is not a telemetry metric yet. | ignition ON / running | strong idle-range candidate, 732/732 samples, R² 0.9995295; loaded drive required, same evidence |
| `0x4B1` | byte0 bit0 | `0=closed`, `1=open` in one driver-door trial | **driver-door-correlated candidate** — exact open/close edges; another door must be tested before calling it driver-exclusive | ignition ON | controlled candidate, [2026-07-22 evidence](../projects/ecu_mapping/findings/promaster_2022/2026-07-22_ccan_alfaobd_live_correlation.md#passive-driver-door-candidates) |
| `0x419` | byte2 | `0x77=closed`, `0x97=open` in the same trial | second exact **door-correlated candidate**; the `0xE0` XOR may combine several states | ignition ON | controlled candidate, same evidence |
| `0x1FA` | byte3 bit1 | `0=released`, `1=held` | strongest high-rate binary **service-brake-correlated candidate** | ignition ON | one controlled hold/release; repeat and parking-brake discriminator pending |
| `0x0FA` | byte0 | `0x40` released; `0x48` transition; `0x4C` held | high-rate brake state/pressure candidate; exact two-bit split unresolved | ignition ON | one controlled hold/release, [2026-07-22 evidence](../projects/ecu_mapping/findings/promaster_2022/2026-07-22_ccan_alfaobd_live_correlation.md#passive-service-brake-candidates) |
| `0x10F` | bytes1-3 | zero released; nonzero/varying held | high-rate analogue pedal/pressure candidate; scaling unresolved | ignition ON | one controlled hold/release, same evidence |
| `0x1F1` | byte0 bit1 + byte2 | binary bit and varying analogue field | corroborating service-brake candidate; unrelated byte0 bits also drift | ignition ON | one controlled hold/release, same evidence |
| `0x417` | byte4 | `0x60` released; mostly `0x40`, with `0x20` excursions, held | propagated brake state/load candidate, not a stable enum yet | ignition ON | one controlled hold/release, same evidence |
| `0x5A8` | byte3 | `0x56 -> 0x76 -> 0x56` | low-rate propagated service-brake candidate | ignition ON | one controlled hold/release, same evidence |
| `0x5BE` | byte2 | `0x00 -> 0x18 -> 0x00` | low-rate propagated service-brake candidate | ignition ON | one controlled hold/release, same evidence |
| `0x101` | `((b0 & 1) << 11) \| (b1 << 3) \| (b2 >> 5)` | **scale unresolved:** leading candidates `/16` or `/32` km/h | **instantaneous vehicle speed**, not an odometer accumulator. It ramps reversibly, is flat at zero when stopped, crosses 2047→2048 continuously, and tracks `0x0EE` at ≈8:1. A known-speed reference is still required before choosing the scale. | ignition ON; moving value while driving | **field/meaning high confidence; scale unverified**, 2026-07-19 drive captures; [analysis](../projects/ecu_mapping/findings/promaster_2022/2026-07-19_ccan_drive_signal_analysis.md) |
| `0x101` | `((b2 & 3) << 6) \| (b3 >> 2)` | raw | braking/deceleration-like field; correlated with braking magnitude and near zero at steady speed. Not yet ground-truthed. | driving | candidate, same 2026-07-19 analysis |
| `0x101` | byte6 low nibble; byte7 | counter `0..15`; CRC-8/SAE-J1850 over bytes0–6 | rolling frame counter and checksum. CRC matched every one of 224,137 continuation frames. | ignition ON | verified in the 2026-07-19 continuation capture |
| `0x0EE` | bytes[0:2] BE u16 | ≈`8 × 0x101_speed_raw`; paired scale candidates `/128` or `/256` km/h | independent higher-resolution vehicle-speed field corroborating `0x101`; Pearson `r=0.9999919` while moving. Absolute scale remains tied to the same ground-truth question. | ignition ON / driving | field relationship high confidence; scale unverified, [analysis](../projects/ecu_mapping/findings/promaster_2022/2026-07-19_ccan_drive_signal_analysis.md) |
| signature set | — | — | C-CAN identity guard: `0x100 101 103 104 10F 110 116 0EA 0EE 0FA 0FE` (+ `2EF 41A`) | high-rate, ignition-on & in parked wakes | used by `classify_bus()` |

During the controlled brake hold, the already-known `0x41A` voltage byte fell from `0xA0` to
`0x9E/0x9C` and recovered after release. That is a secondary voltage/load effect consistent with
the brake lamps, not a second meaning for the voltage byte. Cadence-driven changes in `0x412` and
`0x73A` were likewise rejected rather than promoted as door fields.

The exact `0x41A` scale was established on 2026-07-25 while the starter-battery charger moved from
charge to maintenance. Raw byte0 stepped `BE -> BC -> BA -> ... -> B0`, which the affine decode
maps to `13.50 -> 13.40 -> 13.30 -> ... -> 12.80 V`. AlfaOBD BCM Status independently showed
13.50 V at the charged endpoint, then a fresh snapshot showed 12.70 V (+30) / 12.80 V (ADC) while
the broadcast was at `AE`/`B0`. The former `/14.2` divisor omitted the 4 V offset and therefore
under-reported progressively as voltage fell.

---

## CAN-CH broadcast frames (passive-readable)

The 2026-07-25 grey-adapter capture contained 2,740,037 frames over about 24 minutes at 500 kbit/s
with the PCAN continuously listen-only and its error counters at zero. CAN-CH carries some identifiers
also seen on ordinary C-CAN, so bitrate or one shared identifier cannot identify the branch.

| id set | meaning | confidence |
|---|---|---|
| `0x0DA 0x0DC 0x0F1 0x106 0x10E 0x117 0x1F6` | high-rate CAN-CH identity guard. All seven occurred about 100 Hz in the grey capture and were absent from the same campaign's pins-6/14 reference capture. `identify_bus()` requires at least three together. | verified for awake CAN-CH; [evidence](../projects/ecu_mapping/findings/promaster_2022/2026-07-25_canch_live_verification.md) |
| `18DA28F1/F128`, `18DA30F1/F130`, `18DA31F1/F131`, `18DAC0F1/F1C0` | physical AlfaOBD exchanges with installed ABS, EPS, HALF, and ORC. Any captured member is decisive CAN-CH evidence. | verified |

A silent 500-kbit/s interface cannot be distinguished passively between C-CAN and CAN-CH. Do not
probe, wake, or switch bitrate merely to identify grey. The unattended voltage monitor can use only
an explicit same-boot physical-topology record when the bus is silent. Recorded CAN-CH reports grey
and exits; missing, stale, malformed, or unknown topology fails closed.

---

## B-CAN broadcast frames (passive-readable)

| id | field | decode | meaning | confidence |
|---|---|---|---|---|
| `0x46C` | bytes[4:5] BE, **low 13 bits** (`& 0x1FFF`) | `/ 400` (≈ 0.0025 V/LSB) | **system voltage** — byte[4] HIGH bits are STATUS FLAGS (bit6=0x4000 seen → phantom ~53 V if unmasked). Verified across engine ON→OFF (14.24 V charging → 12.48–12.80 V resting). ~2 Hz. Parked-battery source. | **verified** (mask + sane-range-filtered in `bcan_voltage.py`) |
| `0x46C` | byte5 bit0 + byte6 bits6-7 | `..33 53 00` ↔ `..33 52 C0` | **lock-state feedback** — toggles each lock/unlock; monitor to confirm a future UDS unlock worked | verified |
| `0x5B2` | byte3 | `0x10` ↔ `0x14` | lock-state latch (corroborates 0x46C) | verified |
| `0x082` | ASCII multiplex | — | user's OWN gear ("running_van_no_internet"), **not factory** — ignore | verified (owner-added) |
| signature set | — | — | B-CAN identity guard: `0x46C 0A0 0E0 2EA 3DC 3DE 3E0 3E2 3E4 3E6 354 356` | used by `classify_bus()` |

---

## Wake / sleep semantics (load-bearing — read before parked work)

- **CAN-CH wake is intentionally unmapped.** Never reuse the C-CAN RFH poke or B-CAN `0x7FF`
  burst on pins 12/13. `voltage_mon.py` treats passively identified or explicitly recorded
  same-boot CAN-CH as a notify-and-exit topology.
- **B-CAN wake:** a **key-fob UNLOCK wakes it (~95 s window)**; **a door-open does NOT** (capture = 0 frames). Ignition/engine wakes it too. `bcan_voltage.py --wake` TX-wakes a silent B-CAN with a `0x7FF` burst. Verified 2026-06-26; captures in `tmp/captures/bcan/events/wake_from_*`.
- **C-CAN wake — the Pi CAN wake it, but only with an *addressed* poke (verified 2026-07-08, twice):**
  - A raw `0x7FF` broadcast burst @500k does **NOT** wake a parked C-CAN (verified 2026-07-07 — ~490 frames drew only a lone 0x200). Selective wake: junk broadcast frames aren't a wake reason.
  - But **a single addressed UDS read to `rf_hub`** (KL30-powered / always-awake RKE receiver) **wakes the full C-CAN broadcast schedule**: confirmed-asleep bus (0 frames/3 s) → one `22` read → ~17.5k frames/15 s incl. **`0x41A` @10 Hz (~12.8 V)** → re-sleeps **~30 s** after traffic stops (shorter than B-CAN's ~95 s). A diag exchange with an awake KL30 module is what triggers the gateway's network-management wake.
  - **Consequence:** autonomous parked voltage polling works from the C-CAN tap (wake-poke rf_hub → passive `0x41A` read) — no need to sit on B-CAN. This dissolved the old one-adapter B-CAN-vs-C-CAN conflict. (Earlier "C-CAN readable only when something else wakes it" was too narrow — it predated the rf_hub-poke test.)
  - **Implemented for manual and guarded unattended use:** `ccan_voltage.py --wake` was live-tested 2026-07-08 (one `22 F190` read to rf_hub → raw `0x41A=B0`, now correctly decoded as **12.80 V**; the former divisor rendered 12.39 V). `voltage_mon.py` may call the same primitive only after it holds the exclusive SocketCAN lock, finds no same-boot external-operation inhibit, verifies a same-boot C-CAN topology record, and rechecks passive silence under the lock. AlfaOBD controller actions create an external inhibit before ADB opens.
  - **Reusable API (2026-07-09):** the detect + wake logic is factored into **`lib/canbus.py`** — `identify_bus()` / `detect_bus()` (which bus, from the signature sets above), `tx_wake_burst()` (B-CAN), `poke_wake()` (C-CAN rf_hub), and `wake()` (detect + wake, the "keep a parked bus awake" primitive). The signature id sets `CCAN_SIG`/`BCAN_SIG` live there too (sourced from this doc). Both voltage readers now call these; a new project needing to find+rouse whichever bus is connected should import them.
- **TX side effect (GOTCHA):** the rf_hub wake-poke also wakes the BCM → **switched accessory rails power up** (dash USB / dashcam boots), following the ~30–60 s awake window. Verified 2026-07-08. Owner has OK'd unprompted parked TX; just account for the side effect when reading evidence (an unexplained parked dashcam boot may be our own diag traffic — or a free bus-wake detector). tpms-logger is zero-TX in idle by design.
- **Remote-unlock status:** BCM diagnostic actuation is power-mode gated (`7F..22` key-off even with bus awake); recommended path is a spare-fob relay, not CAN. Full detail in memory `bcan-bringup` / the B-CAN section above.

---

## Per-module DID maps (per-ECU — do NOT merge into one list)

DID namespaces are **per-ECU**: the radar's `0x0845` and any other module's `0x0845` are
unrelated. Each module keeps its own canonical map next to its analysis:

- **radar_acc** → [`projects/radar/findings/did_map.md`](../projects/radar/findings/did_map.md) — canonical 56-DID map (sessions, security, routines, DTCs, angle scaling). Full sweep: `projects/radar/findings/radar_acc_did_sweep.txt`.
- **rf_hub** → [`projects/tpms/README.md`](../projects/tpms/README.md) — TPMS/RKE DID map inline (pressure `31D0-31D3`, sensor-ID `31CB-31CE`, snapshot/extended-data DIDs, the verified wheel↔slot table). Full sweep: `projects/tpms/findings/rf_hub_did_sweep.txt`.
- **tcm / shifter / bcm_ccan / cluster / telematics / pcm** → [`2026-07-21 candidate DID inventory`](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_candidate_did_inventory.md) for complete inherited-session `F100-F1FF` results and BCM candidate/page inventories; [`2026-07-22 C-CAN AlfaOBD correlation`](../projects/ecu_mapping/findings/promaster_2022/2026-07-22_ccan_alfaobd_live_correlation.md) for installed runtime-profile aliases, bounded cluster/TCM/PCM polling sets, controlled BCM `0130/0152` door and `0132/0150` brake groups, and the engine-off PCM legacy-session result. The complete BCM session-03 `4000-40FF` page found only default-visible `40A1`, `40A2`, `40AA` and session-gated `40A3`, `40A6`; no other session-only positive appeared. Keep these namespaces separate; Alfa-rendered labels/scaling remain candidates unless controlled state or an exact decoder verifies them.
- **cluster live data** → [`2026-07-24 singleton correlation`](../projects/ecu_mapping/findings/promaster_2022/2026-07-24_cluster_singleton_correlation.md) independently discriminates AlfaOBD labels `Engine speed`, `Vehicle speed`, `Actual Gear`, `Battery Voltage (+30)`, and `Outside temperature` to cluster DIDs `1000`, `1002`, `0107`, `1004`, and `1005`. Battery `1004` supports AlfaOBD's `raw x 0.1 V` rendering over observed raw values `0x76-0x79`; RPM/speed scales, the non-P gear enum, the temperature formula, and independent physical voltage remain unverified. A direct Alfa-closed comparison produced identical responses after positively acknowledged `10 01` and `10 03`, proving default session suffices and extended session adds no access for this set. A separate no-session pass also succeeded, but its inherited session was not positively identified. The [`2026-07-25 idling logger shakedown`](../projects/ecu_mapping/findings/promaster_2022/2026-07-25_cluster_idle_logger_shakedown.md) added 623 nonzero `1000` samples: raw `2936..6136` fell from an initial high, warm-up-like value to a stable band near `3000` while `1002=00` and `0107=00`. The subsequent [`DID 1000 broadcast correlation`](../projects/ecu_mapping/findings/promaster_2022/2026-07-26_cluster_did1000_broadcast_correlation.md) exact-linked those 623 samples and ranked `0x0FC` bytes 0–1 (`u16be`) first with full coverage, R² 0.9998896, a unit-slope fit, and 6.42 raw-count RMSE. This is a strong passive raw Engine-speed candidate and remains consistent with `raw / 4` (roughly 734–1,534 rpm), but no simultaneous Alfa/tachometer reference was present, so the physical scale and telemetry promotion remain unverified.
- **PCM live data** → [`2026-07-26 simultaneous Plots/wire mapping`](../projects/ecu_mapping/findings/promaster_2022/2026-07-26_pcm_plots_idle_mapping.md) maps the current-vehicle Alfa profile's eleven-item polling cycle to PCM DIDs `022A`, `011D`, `0413`, `0188`, `0227`, `01A1`, `019B`, `019E`, `019C`, `06DA`, and `069E`. Nine changing rows have direct time-aligned label/field/scale evidence; constant fuel-level and target-voltage rows have fixed-order associations only. Passive `0x41D` byte2 x4 kPa and `0x2ED` byte0 -40 °C are now allowlisted engine-oil-pressure and coolant-temperature sources. Torque DID `06DA` is signed i16be x0.04 Nm; passive `0x100` byte2 is a strong idle-range lead whose loaded wrap/mode remains unresolved.
- **ics_bcan / uconnect_bcan / climate_bcan / emcm2_bcan** → [`2026-07-21 B-CAN live ECU discovery`](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_bcan_live_ecu_discovery.md) for exact addressing/identity, DTC semantics, result-only routines, and inherited-session `F100-F1FF`; [`live AlfaOBD status correlation`](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_alfaobd_live_status_correlation.md) for exact common scalars, candidate ICS/Uconnect status groups, and controlled EMCM2 `2A00/2A01` rotary/Mute/Screen mappings. The selected Climate profile failed variant verification, so its observed gauge labels/scales are explicitly invalid for the installed ECU.

To plan a new module inventory without touching CAN, run
`python3 tools/did_sweep.py <key> START END` (dry-run is the default). A parked live run requires
the explicit `--execute --confirm-parked --pair ... --conditions ...` gates described in the root
README. Checkpointed JSONL plus an atomic summary land under `tmp/inventories/<key>/`; a clean,
complete run also produces a compatibility text view under `tmp/sweeps/`. Promote selected evidence
and its per-ECU analysis into that project's `findings/`, then add a pointer row here.
