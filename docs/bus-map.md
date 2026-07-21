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
| **B-CAN / CAN IHS** | **125 kbit/s, live-verified** | DLC pins **3/11** (OEM: CAN IHS +/−) | PCAN through the **B-CAN DB9** of the owner's labeled dual-pair OBD pigtail; `bringup.sh --bcan` | Comfort/body effects (locks, lights, interior) and the established B-CAN signature set were captured with listen-only explicitly on and zero RX errors on 2026-07-20. Owner confirmed the pigtail directly selects the documented 2022 ProMaster B-CAN pair; the DIY yellow adapter has never been used on this van. [Evidence](../projects/ecu_mapping/findings/promaster_2022/2026-07-20_bcan_pair_verification.md). |
| **CAN CH / second high-speed CAN** | **unverified live**; **500 kbit/s is the leading candidate** | DLC pins **12/13** (OEM: CAN CH +/−) | PCAN requires repinning; passive survey pending | OEM topology includes BCM, SGW, ORC, park assist, EPS, ABS, and forward camera. AlfaOBD's current hardware guide explicitly calls pins 12/13 the second high-speed CAN bus on 2022+ ProMaster. That establishes the bus class, not the exact bitrate or addressing on this van. |

The currently configured C-CAN/B-CAN modes come up **passive (listen-only)** by default;
`bringup.sh --tx` arms transmission (UDS needs it).

The DLC pin names above come from local OEM diagram `2022_VF_EN_18-000-000`, revision 2064:
`/home/pi/dev/ram_2022_GAS/diagrams/systems/data_link_connector.html`. That source establishes
physical topology, not bitrate. Its companion CAN C/CH/IHS topology diagrams identify attached
modules. The labeled pigtail plus the 2026-07-20 passive captures now tie CAN IHS/B-CAN to pins
3/11 at 125 kbit/s; CAN CH still requires a live survey if it becomes relevant.

[AlfaOBD's current hardware guide](https://alfaobd.com/) independently identifies pins 12/13 as
the second high-speed CAN bus for 2022+ ProMaster and describes the supported PowerNet/CUSW layout
as high-speed CAN on 6/14 plus middle-speed CAN on 3/11. Its
[vehicle table](https://www.alfaobd.com/supported_cars.html) specifically assigns the grey
second-high-speed adapter to `RAM PRO MASTER (VF) 2022+`, rather than the yellow adapter used by
pre-2022 VF. This is strong diagnostic-tool-vendor provenance for bus class and adapter routing,
but it is not a live bitrate measurement or an OEM address map. The 3/11 live rate is now established
independently at 125 kbit/s. Survey 12/13 at 500 kbit/s first if CAN CH work is later prioritized.

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
| `pcm` | Powertrain Control Module (3.6L Pentastar) | C-CAN | `18DA10F1` → `18DAF110` | Live-verified 2026-07-21 while parked/engine-idling: fixed-DLC-8 padded `10 92 → 50 92`, then `1A 87 → 5A 87` containing `68532157AI`. Default-session and unpadded probes had timed out. |
| `rf_hub` | RF Hub (Continental) — TPMS/RKE | C-CAN | `18DAC7F1` → `18DAF1C7` | **Answers with ignition OFF** (battery-powered RKE receiver). |
| `tcm` | ZF 948TE transmission controller | C-CAN | `18DA18F1` → `18DAF118` | Live identity on 2026-07-19: `F187=46342086`, `F194/F132=68532161AF`, `F192=ES11-1065 D`. |
| `shifter` | SILATECH electronic shifter | C-CAN | `18DA1FF1` → `18DAF11F` | Live identity on 2026-07-19: `F187=P7FK46LXHAD`, `F188/F194=AGSM637FCA`. |
| `bcm_ccan` | Body Control Module, C-CAN endpoint | C-CAN | `18DA40F1` → `18DAF140` | Live identity on 2026-07-19: `F187=68524831AF`, `F192=BC637M.0001`; actuation remains power-mode gated. |
| `cluster` | Marelli Instrument Panel Cluster (IPC) | C-CAN | `18DA60F1` → `18DAF160` | Live identity on 2026-07-19: `F187=68517084AD`, `F192=50019990002`. FCA's [NHTSA Part 573 filing](https://downloads.regulations.gov/NHTSA-2023-0046-0001/attachment_1.pdf) identifies `68517084AD` as the Marelli IPC. |
| `telematics` | Global Telematics Box Module (TBM2) | C-CAN | `18DAC6F1` → `18DAF1C6` | Live identity on 2026-07-19: `F132=68510377AC`, `F192=TBM200A11P`. The TBM string, exact-part [Mopar catalog supersession](https://www.moparpartsgiant.com/parts/mopar-module-telematics~68647858aa.html), and exact-vehicle local OEM TBM2 procedure make the role high-confidence. |

No B-CAN diagnostic endpoint is registered. The previously listed `0x75C`, `0x760`, `0x762`,
`0x764`, `0x768`, and `0x7C0` candidates are fixed-payload 1–2 Hz broadcasts, not an ISO-TP
family; the one observed `0x7B8` frame contained ASCII `3231`, not a diagnostic response. A
111-second B-CAN capture made during an AlfaOBD RF Hub session likewise contains no ISO-TP
exchange, while the adapter trace addresses the RF Hub and BCM over their verified 29-bit C-CAN
endpoints. Direct diagnostics on B-CAN are therefore **not established**. Do not infer `+8`
pairs or add an 11-bit module without an actual request/response capture. See the
[`B-CAN pair verification`](../projects/ecu_mapping/findings/promaster_2022/2026-07-20_bcan_pair_verification.md).

> **PCM legacy-session requirement:** `0x10` was independently verified from the PCAN tap on
> 2026-07-21. While parked with the engine idling, fixed-DLC-8 zero-padded
> `18DA10F1 -> 18DAF110` traffic produced exact `10 92 -> 50 92`, followed by a positive
> multi-frame `1A 87 -> 5A 87` identity containing `68532157AI`; FCA's official J2534 report maps
> that part to a 2022 VF 3.6L PCM calibration. Earlier default-session and unpadded probes timed
> out. Because padding and engine power state changed between the failed and successful attempts,
> this run verifies the endpoint and recipe but does not isolate which condition was decisive.
> Keep using the bounded legacy probe rather than assuming ordinary default-session `22` support. See
> [`2026-07-19_live_ecu_discovery.md`](../projects/ecu_mapping/findings/promaster_2022/2026-07-19_live_ecu_discovery.md).

---

## C-CAN broadcast frames (passive-readable)

| id | field | decode | meaning | when present | confidence |
|---|---|---|---|---|---|
| `0x2EF` | bytes[0:1] LE u16 | `/ ~400` | **system voltage (fine)** — same ÷~400 family as B-CAN 0x46C; engine/ignition ratio 1.17 (alternator) | **ignition ON / running only** | field confirmed; **divisor not pinned** (needs one ground-truth cal via `ccan_voltage.py --calibrate`) |
| `0x2EF` | presence | — | **ignition-on gate** — its presence = key-on; tpms-logger uses it as the drive/park gate | ignition ON | verified (frame-count gates failed; presence gate works) |
| `0x41A` | byte0 | `/ ~14.2` | **system voltage (coarse)** — C-CAN analogue of 0x46C, readable in a parked *wake* (~12.5 V resting) | any awake C-CAN incl. parked wake | field confirmed; divisor coarse/approx |
| `0x101` | `((b0 & 1) << 11) \| (b1 << 3) \| (b2 >> 5)` | **scale unresolved:** leading candidates `/16` or `/32` km/h | **instantaneous vehicle speed**, not an odometer accumulator. It ramps reversibly, is flat at zero when stopped, crosses 2047→2048 continuously, and tracks `0x0EE` at ≈8:1. A known-speed reference is still required before choosing the scale. | ignition ON; moving value while driving | **field/meaning high confidence; scale unverified**, 2026-07-19 drive captures; [analysis](../projects/ecu_mapping/findings/promaster_2022/2026-07-19_ccan_drive_signal_analysis.md) |
| `0x101` | `((b2 & 3) << 6) \| (b3 >> 2)` | raw | braking/deceleration-like field; correlated with braking magnitude and near zero at steady speed. Not yet ground-truthed. | driving | candidate, same 2026-07-19 analysis |
| `0x101` | byte6 low nibble; byte7 | counter `0..15`; CRC-8/SAE-J1850 over bytes0–6 | rolling frame counter and checksum. CRC matched every one of 224,137 continuation frames. | ignition ON | verified in the 2026-07-19 continuation capture |
| `0x0EE` | bytes[0:2] BE u16 | ≈`8 × 0x101_speed_raw`; paired scale candidates `/128` or `/256` km/h | independent higher-resolution vehicle-speed field corroborating `0x101`; Pearson `r=0.9999919` while moving. Absolute scale remains tied to the same ground-truth question. | ignition ON / driving | field relationship high confidence; scale unverified, [analysis](../projects/ecu_mapping/findings/promaster_2022/2026-07-19_ccan_drive_signal_analysis.md) |
| signature set | — | — | C-CAN identity guard: `0x100 101 103 104 10F 110 116 0EA 0EE 0FA 0FE` (+ `2EF 41A`) | high-rate, ignition-on & in parked wakes | used by `classify_bus()` |

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

- **B-CAN wake:** a **key-fob UNLOCK wakes it (~95 s window)**; **a door-open does NOT** (capture = 0 frames). Ignition/engine wakes it too. `bcan_voltage.py --wake` TX-wakes a silent B-CAN with a `0x7FF` burst. Verified 2026-06-26; captures in `tmp/captures/bcan/events/wake_from_*`.
- **C-CAN wake — the Pi CAN wake it, but only with an *addressed* poke (verified 2026-07-08, twice):**
  - A raw `0x7FF` broadcast burst @500k does **NOT** wake a parked C-CAN (verified 2026-07-07 — ~490 frames drew only a lone 0x200). Selective wake: junk broadcast frames aren't a wake reason.
  - But **a single addressed UDS read to `rf_hub`** (KL30-powered / always-awake RKE receiver) **wakes the full C-CAN broadcast schedule**: confirmed-asleep bus (0 frames/3 s) → one `22` read → ~17.5k frames/15 s incl. **`0x41A` @10 Hz (~12.4 V)** → re-sleeps **~30 s** after traffic stops (shorter than B-CAN's ~95 s). A diag exchange with an awake KL30 module is what triggers the gateway's network-management wake.
  - **Consequence:** autonomous parked voltage polling works from the C-CAN tap (wake-poke rf_hub → passive `0x41A` read) — no need to sit on B-CAN. This dissolved the old one-adapter B-CAN-vs-C-CAN conflict. (Earlier "C-CAN readable only when something else wakes it" was too narrow — it predated the rf_hub-poke test.)
  - **Implemented:** `ccan_voltage.py --wake` (built + live-tested 2026-07-08: one `22 F190` read to rf_hub → **12.39 V** via `0x41A`), and wired into `voltage_mon.acquire()` (the @500k branch RFH-wakes a silent bus, symmetric to the B-CAN 0x7FF path). Only fires on a `classify_bus`-confirmed silent C-CAN; the poke is self-validating (no rf_hub answer = not on C-CAN).
  - **Reusable API (2026-07-09):** the detect + wake logic is factored into **`lib/canbus.py`** — `identify_bus()` / `detect_bus()` (which bus, from the signature sets above), `tx_wake_burst()` (B-CAN), `poke_wake()` (C-CAN rf_hub), and `wake()` (detect + wake, the "keep a parked bus awake" primitive). The signature id sets `CCAN_SIG`/`BCAN_SIG` live there too (sourced from this doc). Both voltage readers now call these; a new project needing to find+rouse whichever bus is connected should import them.
- **TX side effect (GOTCHA):** the rf_hub wake-poke also wakes the BCM → **switched accessory rails power up** (dash USB / dashcam boots), following the ~30–60 s awake window. Verified 2026-07-08. Owner has OK'd unprompted parked TX; just account for the side effect when reading evidence (an unexplained parked dashcam boot may be our own diag traffic — or a free bus-wake detector). tpms-logger is zero-TX in idle by design.
- **Remote-unlock status:** BCM diagnostic actuation is power-mode gated (`7F..22` key-off even with bus awake); recommended path is a spare-fob relay, not CAN. Full detail in memory `bcan-bringup` / the B-CAN section above.

---

## Per-module DID maps (per-ECU — do NOT merge into one list)

DID namespaces are **per-ECU**: the radar's `0x0845` and any other module's `0x0845` are
unrelated. Each module keeps its own canonical map next to its analysis:

- **radar_acc** → [`projects/radar/findings/did_map.md`](../projects/radar/findings/did_map.md) — canonical 56-DID map (sessions, security, routines, DTCs, angle scaling). Full sweep: `projects/radar/findings/radar_acc_did_sweep.txt`.
- **rf_hub** → [`projects/tpms/README.md`](../projects/tpms/README.md) — TPMS/RKE DID map inline (pressure `31D0-31D3`, sensor-ID `31CB-31CE`, snapshot/extended-data DIDs, the verified wheel↔slot table). Full sweep: `projects/tpms/findings/rf_hub_did_sweep.txt`.
- **tcm / shifter / bcm_ccan / cluster / telematics** → [`2026-07-21 candidate DID inventory`](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_candidate_did_inventory.md) — complete inherited-session `F100-F1FF` results per ECU plus BCM candidate/page inventories. The complete BCM session-03 `4000-40FF` page found only default-visible `40A1`, `40A2`, `40AA` and session-gated `40A3`, `40A6`; no other session-only positive appeared. Keep these namespaces separate; labels/scaling outside established identity strings remain unresolved.

To plan a new module inventory without touching CAN, run
`python3 tools/did_sweep.py <key> START END` (dry-run is the default). A parked live run requires
the explicit `--execute --confirm-parked --pair ... --conditions ...` gates described in the root
README. Checkpointed JSONL plus an atomic summary land under `tmp/inventories/<key>/`; a clean,
complete run also produces a compatibility text view under `tmp/sweeps/`. Promote selected evidence
and its per-ECU analysis into that project's `findings/`, then add a pointer row here.
