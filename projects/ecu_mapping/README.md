# ecu_mapping — mining AlfaOBD debug logs into ECU DID / routine / actuation maps

Goal: turn AlfaOBD's own diagnostic sessions into per-module maps for our 2022 Ram Promaster
(**VIN `3C6LRVDG4NE######`**) — which DIDs each ECU exposes, what routines/actuations
AlfaOBD runs, and their addressing — to feed the radar, TPMS, and BCM/remote-unlock work
without re-deriving from scratch. AlfaOBD is a *DID oracle*: it only touches DIDs that exist
and it labels them, so its logs are a shortcut.

## ⚠️ Provenance — two vans in the data (read before trusting anything)

AlfaOBD debug files accumulate across every vehicle a tablet has touched. We have two dumps:

| source (in `~/claude/shared-files/`) | vehicle | use |
|---|---|---|
| `old.AlfaOBD_Debug.bin` (~396 MB, 2022–2024) | multi-profile **aggregate**; only F190-identified vehicle is the prior **2015** diesel `3C6TRVDD2FE######` | reference only — NOT 2022 ProMaster |
| `AlfaOBD logs and data July 8 2026/` | **2022 ProMaster** `3C6LRVDG4NE######` (fresh, 2026-07-07) | ground truth |

**"Recording data for X" is the AlfaOBD *profile the operator selected*, not confirmed
hardware.** Many entries are near-empty probes — check `reads=` in the map. The 396 MB `old.`
bin is a multi-year, multi-profile **aggregate**: its only F190-identified vehicle is the 2015
diesel, but it also carries unrelated profiles (e.g. 2024 "Chrysler Pentastar 2021" sessions —
`3E01` keepalive only, no F190 — which are NOT the diesel, and are a *gas* Pentastar profile,
possibly an early poke at 2022 ProMaster or another vehicle). So the promaster_2015_diesel map header reads
"F190-identified VIN", not "the vehicle", and a profile name may not match that VIN.

Within the fresh folder, `AlfaOBD_Debug.bin` (2.9 MB) is **100 % 2022 ProMaster**, and
`RFH_FGA_Info.log` / `ADAPTIVE_CRUISE_Info.log` / `TIGERSHARK_CUSW_Info.log` are 2022 ProMaster.
**Gotcha:** `BCDELPHI_Info.log` is a cumulative, mixed-vehicle Body Computer text log. Its early
status/DTC snapshots include the old 2015 van and cannot be applied wholesale to this vehicle, but
its 2026-06-22 tail aligns within seconds with the current-van debug trace's BCM configuration write
and DTC clear. Use only timestamp-correlated entries, and capture a fresh single-module log when a
label matters. Always run `vin_scan.py` on any new debug bin first. See memory
`[[alfaobd-debug-bin-other-van]]`.

## The AlfaOBD debug format

`*_Debug.bin` (Preferences → "Debug Data recording") is an ASCII **hex** string whose bytes
are the **ones-complement (XOR 0xFF)** of the log text. Decoded, it's a timestamped ELM/STN
adapter trace: `HH:MM:SS.mmm S:/R: <hex>`, where each payload is hex-encoded ASCII (a UDS
message like `22F190`, or an `AT`/`ST` command). Multi-frame responses come back as a length
line + indexed `0:`/`1:`/`2:` segments. `ATSH <hdr>` lines set the target module address.
Because AlfaOBD may write the full date only at `Recording closed`, the parsers pre-index
clock-ordered header/close pairs before streaming exchanges; long-open or unclosed recordings keep
the prior best-known date instead of being blindly backdated from a later close marker.

To capture a fresh one on the tablet: enable **Debug Data recording** (raw) and ideally
**Gauges data recording** (labeled CSV, `Gauges_Data.log`) in Preferences, drive the
modules/live-data, then pull from
`/sdcard/Android/data/com.android.AlfaOBD/files/logs/`.

## Pipeline

```
tools/alfaobd_decode.py  <in.bin> [out.txt]      # generic: .bin -> decoded text (reusable)
projects/ecu_mapping/vin_scan.py        <decoded.txt> [vin]   # which van? (run FIRST)
projects/ecu_mapping/extract_did_map.py <decoded.txt> <out>   # per-module DID/service map
projects/ecu_mapping/alfalog.py                  # shared log parser + ELM reassembly
```

`reassemble_commands.py <decoded.txt> <out.txt> [atsh]` — rebuilds multi-frame COMMANDS.
AlfaOBD sends long requests as MANUAL ISO-TP frames: First Frame `1L LL <6 data>` + a trailing
ELM responses-hint digit (17 hex chars), ECU Flow Control `30 00 00`, Consecutive Frames
`2N <7 data>` + hint. This tool drops the hint digit, strips the PCI byte(s), concatenates,
truncates to the FF length, and pairs with the response — then interprets each command
(`2E`/`2F`/`31`/`27`/`10`/`14`). `extract_did_map` handles single-frame `22` *reads* and now
skips the manual-frame scraps; use this tool for the commands.

**VIN handling (publish-safe):** the real VIN is never hardcoded — scripts read it from the
`OBD_VIN` env var (`export OBD_VIN=3C6…`), and tracked outputs mask the unique serial
(`…######`) in every form (ASCII + hex), so committed files carry only the non-identifying
model descriptor. Raw logs under `tmp/` (gitignored) keep the full VIN.

## Data layout (per repo convention)

- **`tmp/ecu_mapping/`** (gitignored): `raw/` = copied `.bin`/`.log`; decoded `*.decoded.txt`.
  Raw CAN/log data is never git-tracked.
- **`findings/`** (tracked): *extrapolations* only — the derived maps.
  - `promaster_2022/module_did_map.txt` — 2022 ProMaster, per-module DID/service inventory (ground truth)
  - `promaster_2015_diesel/module_did_map.txt` — 2015 reference van (same family; candidate cross-ref)
  - `promaster_2022/command_log.txt` — reassembled + interpreted command sequences (2022 ProMaster)

## Findings so far (fresh 2022 ProMaster bin, 2026-07-07)

Modules seen (ATSH → phys addr): radar `DA2AF1`/0x2A, **BCM `DA40F1`/0x40**, RFH `DAC7F1`/0xC7,
trans `DA18F1`/0x18, engine `DA10F1`/0x10 + `7E0`, shifter `DA1FF1`/0x1F.

Direct live discovery on 2026-07-19 independently verified C-CAN endpoints `0x18`, `0x1F`,
`0x2A`, `0x40`, `0x60`, `0xC6`, and `0xC7`; PCM `0x10` remains unresolved in the default
session. See [`2026-07-19_live_ecu_discovery.md`](findings/promaster_2022/2026-07-19_live_ecu_discovery.md).
The companion [`ODX/PDX source research`](findings/promaster_2022/2026-07-19_odx_pdx_source_research.md)
records the free local toolchain, searched sources, and remaining acquisition paths.
The [`2026-07-21 read-only module inventory`](findings/promaster_2022/2026-07-21_readonly_module_inventory.md)
completes inherited-session `18DAxxF1` address coverage and records bounded DTC/result-only routine
responses for all seven verified C-CAN modules. It found no additional address responder; DTC state
and routine-response leads are kept per module there.
The follow-on [`candidate DID inventory`](findings/promaster_2022/2026-07-21_candidate_did_inventory.md)
records complete `F100-F1FF` pages for TCM, shifter, BCM, cluster, and telematics plus a direct
recheck of 61 current-van AlfaOBD BCM candidates. It established 135 positive identity-page
responses and reverified 59 BCM candidates. A controlled follow-up proved BCM `40A3`/`40A6` are
session-gated: both returned `7F 22 31` in the inherited state and positive data after validated
`10 03 -> 50 03 00 32 01 F4` under otherwise unchanged conditions.
The subsequent four-page BCM pass completed 1,024/1,024 reads and found one additional positive,
`2023`, whose complete 250-byte readback matches the later captured AlfaOBD PROXI/configuration write
payload at every unredacted byte. It also preserved four condition-gated DIDs
and the first controlled key-cycle evidence for dynamic BCM values.
The [`related-platform passive bus leads`](findings/promaster_2022/2026-07-19_related_platform_bus_leads.md)
record a 50-kbit/s/29-bit 2020 Citroën Jumper cabin-bus hypothesis. It is now explicitly superseded
for this van's DLC 3/11 branch: the labeled B-CAN pigtail and passive captures live-verified that pair
at 125 kbit/s on 2026-07-20. See the
[`B-CAN pair verification`](findings/promaster_2022/2026-07-20_bcan_pair_verification.md).
That analysis also rejects the old high 11-bit candidates as fixed-rate application broadcasts;
no direct B-CAN diagnostic endpoint is currently established, so active inventories stay on the
verified C-CAN endpoints while B-CAN remains a passive signal/event source.
The [`2026-07-19 passive drive analysis`](findings/promaster_2022/2026-07-19_ccan_drive_signal_analysis.md)
corrects CAN ID `0x101` from the old odometer hypothesis to a packed instantaneous-speed field,
corroborated by `0x0EE`; the exact `/16`-versus-`/32` km/h scale still needs one known-speed reference.

- **Radar (0x2A)** confirms the radar project's story: `31 01 0250` → `7F3131` (wrong RID),
  alignment-gauge DIDs (`083E/083F/0846/0830/0860`) → `7F2231` "not supported". DID `0850`
  returns real bytes (`FF ED 44 D4 FF FF 7E 86`) — decode target. See `../radar/`.
- **BCM (0x40)** — real commands (from the reassembled log): `2F` IO-control actuations that
  **succeeded** (`2F5115/5118/5120/5040/5041/5050` → `6F..03`/`6F..00` return-control), each run
  as `ctrl=03` (shortTermAdjustment) `opt=01`/`02` then `ctrl=00` (release); routine `31 01 0200`
  → `7F..22` conditionsNotCorrect (power-mode gated); two large, positively acknowledged
  **PROXI config writes** (`2E 2023`, 250-byte payloads, each `7F 2E 78` then `6E 20 23`);
  `10 03` session, `14` ClearDTC. **Correction:** the
  `27`/`2A`/`2B` "commands" an earlier pass reported were **not** SecurityAccess — they were
  Consecutive Frames of the `2E 2023` write (nibble-2 PCI). **No `27` in this session.** With the
  SGW bypassed (`[[sgw-bypass-always]]`) the successful `2F` actuations are the remote-unlock
  lead; next is identifying *which* `2F` DID drives the door lock (correlate with what was
  actuated in AlfaOBD) and verifying on 2022 ProMaster via the tap before replaying.
  Offline action-label recovery is currently exhausted: the current-van debug stream records the
  raw commands but not the UI action names, `BT_Debug.log` contains connection transport rather than
  those diagnostic payloads, and the mixed `BCDELPHI_Info.log` has no action annotation at the
  June-12 command times. No AlfaOBD APK/database is present locally, and exact public-web/GitHub
  searches on 2026-07-21 found no useful matches. A fresh, one-action-at-a-time AlfaOBD session with
  the PCAN listening in parallel is the next evidence-producing step for labels; do not guess them
  from command timing alone.
- **RFH (0xC7)** full ID block + TPMS; pair with labeled `RFH_FGA_Info.log` (current faults
  `U0001/B1040/C1502-FR/C1501-FL`) for the TPMS project. See `../tpms/`.

## Next steps

1. Repeat the AlfaOBD-only PCM `18DA10F1 -> 18DAF110` exact probe parked with ignition ON and engine
   OFF, this time with fixed-DLC-8 zero padding and a filtered raw capture. The first unpadded attempt
   timed out at `10 92`, while AlfaOBD's successful ELM setup explicitly selects fixed eight-byte
   CAN frames. If the padded retry still fails, repeat with the engine idling. The expected identity
   contains `68532157AI`, which FCA's official J2534 report maps to the exact 2022 VF 3.6L lineage.
2. Run one bounded BCM `4000-40FF` page in session `03`. The controlled comparison has now proven
   that extended session exposes `40A3` and `40A6`, justifying one session-specific page to bound
   nearby hidden DIDs. It still requires explicit DiagnosticSessionControl authorization.
   `tools/ccan_inventory_campaign.sh --session-followup` performs steps 1 and 2 together.
3. **Unlock:** identify which BCM `2F` IO-control DID drives the door lock/unlock (correlate the
   command log's timestamps with the actuations run in AlfaOBD, or its labels), then verify on
   2022 ProMaster via the tap before replaying — `2F 51xx ctrl=03 opt=xx`. See `promaster_2022/command_log.txt`.
4. Correlate `*_Info.log` labels ↔ debug-bin DIDs → labeled maps (start RFH/TPMS + radar).
5. Once a DID/address/routine is *verified on 2022 ProMaster*, promote it into the canonical maps
   (`../../docs/bus-map.md`, `../../lib/modules.py`, project DID maps) per the maintenance rule.
