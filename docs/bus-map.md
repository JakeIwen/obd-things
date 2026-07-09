# Promaster bus map — master reference

2022 Ram Promaster (VIN 3C6LRVDG4NE######). Everything below was **verified on the vehicle**;
each row cites how. This is the single place to learn what's already mapped on each bus before
starting new reverse-engineering.

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
| **B-CAN (body)** | 125 kbit/s | separate physical bus (low-speed adapter wiring), **not** pins 6/14 | second PCAN; `bringup.sh --bcan` | comfort/body (locks, lights, interior). AlfaOBD's UDS rides C-CAN, not this bus — body bus shows *effects* only. |

Both come up **passive (listen-only)** by default; `bringup.sh --tx` arms transmission (UDS needs it).

---

## UDS modules (addressing → `lib/modules.py` is source of truth)

29-bit ISO-TP normal-fixed unless noted. This table adds the bus + operational quirks the
registry can't hold; keep the addresses in sync with `lib/modules.py`.

| key | module | bus | TX → RX | quirks |
|---|---|---|---|---|
| `radar_acc` | Bosch ACC radar (DASM / MRR1evo) | C-CAN | `18DA2AF1` → `18DAF12A` | ACKs our frames even with ignition cut mid-sweep. Speed only via DID `0x1002` (no OBD PIDs behind SGW). |
| `rf_hub` | RF Hub (Continental) — TPMS/RKE | C-CAN | `18DAC7F1` → `18DAF1C7` | **Answers with ignition OFF** (battery-powered RKE receiver). |
| *(not yet added)* | Body Control Module (BCM) | — | C-CAN `18DA40F1` → `18DAF140` for ignition-by-diag routine; **also 11-bit UDS on B-CAN** (diag IDs `7C0 7B8 760 762 764 768 75C` seen) | actuation is **power-mode gated** (LOCK/ignition routines return `7F..22 conditionsNotCorrect` key-off). See [bcan wake notes](#b-can-broadcast-frames). Adding needs an 11-bit Module variant. **AlfaOBD 2022 ProMaster log** (`projects/ecu_mapping`) confirms 0x40 answers UDS and ran **`2F` IO-control actuations that succeeded** (`2F 5115/5118/5120/5040/5041/5050`, `ctrl=03`→`6F..03`) + `2E 2023` PROXI writes — leads for remote-unlock; verify on our tap before replaying. |

> **AlfaOBD-observed modules (provenance: `projects/ecu_mapping` 2022 ProMaster debug log, VIN
> `…######` — strong evidence, but NOT yet independently driven from our tap, so not in
> `lib/modules.py`).** Physical UDS addresses `18DAxxF1`/`18DAF1xx` seen answering on 2022 ProMaster:
> **0x10** engine PCM (profile "Tigershark/Pentastar MY21"), **0x18** transmission
> (reports "ZF 948TE 9-speed"), **0x1F** electronic shifter, **0x40** BCM (above),
> **0x2A** radar, **0xC7** RF Hub. Per-module DID inventories + reassembled command sequences:
> `projects/ecu_mapping/findings/`. Promote any row into this table + `lib/modules.py` only
> after our own tap confirms it.

---

## C-CAN broadcast frames (passive-readable)

| id | field | decode | meaning | when present | confidence |
|---|---|---|---|---|---|
| `0x2EF` | bytes[0:1] LE u16 | `/ ~400` | **system voltage (fine)** — same ÷~400 family as B-CAN 0x46C; engine/ignition ratio 1.17 (alternator) | **ignition ON / running only** | field confirmed; **divisor not pinned** (needs one ground-truth cal via `ccan_voltage.py --calibrate`) |
| `0x2EF` | presence | — | **ignition-on gate** — its presence = key-on; tpms-logger uses it as the drive/park gate | ignition ON | verified (frame-count gates failed; presence gate works) |
| `0x41A` | byte0 | `/ ~14.2` | **system voltage (coarse)** — C-CAN analogue of 0x46C, readable in a parked *wake* (~12.5 V resting) | any awake C-CAN incl. parked wake | field confirmed; divisor coarse/approx |
| `0x101` | — | — | odometer/speed broadcast (cross-ref for the radar's 0x1002 speed DID) | driving | referenced in radar hunt work |
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
- **TX side effect (GOTCHA):** the rf_hub wake-poke also wakes the BCM → **switched accessory rails power up** (dash USB / dashcam boots), following the ~30–60 s awake window. Verified 2026-07-08. Owner has OK'd unprompted parked TX; just account for the side effect when reading evidence (an unexplained parked dashcam boot may be our own diag traffic — or a free bus-wake detector). tpms-logger is zero-TX in idle by design.
- **Remote-unlock status:** BCM diagnostic actuation is power-mode gated (`7F..22` key-off even with bus awake); recommended path is a spare-fob relay, not CAN. Full detail in memory `bcan-bringup` / the B-CAN section above.

---

## Per-module DID maps (per-ECU — do NOT merge into one list)

DID namespaces are **per-ECU**: the radar's `0x0845` and any other module's `0x0845` are
unrelated. Each module keeps its own canonical map next to its analysis:

- **radar_acc** → [`projects/radar/findings/did_map.md`](../projects/radar/findings/did_map.md) — canonical 56-DID map (sessions, security, routines, DTCs, angle scaling). Full sweep: `projects/radar/findings/radar_acc_did_sweep.txt`.
- **rf_hub** → [`projects/tpms/README.md`](../projects/tpms/README.md) — TPMS/RKE DID map inline (pressure `31D0-31D3`, sensor-ID `31CB-31CE`, snapshot/extended-data DIDs, the verified wheel↔slot table). Full sweep: `projects/tpms/findings/rf_hub_did_sweep.txt`.

To sweep a new/unmapped module: `python3 tools/did_sweep.py <key>` → `tmp/sweeps/`, then promote
the analysis into that project's `findings/` and add a pointer row here.
