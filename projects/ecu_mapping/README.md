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
**Gotcha:** `BCDELPHI_Info.log` (Body Computer text log) is **stale — still the 2015 van**;
re-capture BCM text on 2022 ProMaster if you need it (the fresh `.bin` did catch 2022 ProMaster BCM).
Always run `vin_scan.py` on any new log first. See memory `[[alfaobd-debug-bin-other-van]]`.

## The AlfaOBD debug format

`*_Debug.bin` (Preferences → "Debug Data recording") is an ASCII **hex** string whose bytes
are the **ones-complement (XOR 0xFF)** of the log text. Decoded, it's a timestamped ELM/STN
adapter trace: `HH:MM:SS.mmm S:/R: <hex>`, where each payload is hex-encoded ASCII (a UDS
message like `22F190`, or an `AT`/`ST` command). Multi-frame responses come back as a length
line + indexed `0:`/`1:`/`2:` segments. `ATSH <hdr>` lines set the target module address.

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

- **Radar (0x2A)** confirms the radar project's story: `31 01 0250` → `7F3131` (wrong RID),
  alignment-gauge DIDs (`083E/083F/0846/0830/0860`) → `7F2231` "not supported". DID `0850`
  returns real bytes (`FF ED 44 D4 FF FF 7E 86`) — decode target. See `../radar/`.
- **BCM (0x40)** — real commands (from the reassembled log): `2F` IO-control actuations that
  **succeeded** (`2F5115/5118/5120/5040/5041/5050` → `6F..03`/`6F..00` return-control), each run
  as `ctrl=03` (shortTermAdjustment) `opt=01`/`02` then `ctrl=00` (release); routine `31 01 0200`
  → `7F..22` conditionsNotCorrect (power-mode gated); two large **PROXI config writes**
  (`2E 2023`, ~200-byte ASCII blocks); `10 03` session, `14` ClearDTC. **Correction:** the
  `27`/`2A`/`2B` "commands" an earlier pass reported were **not** SecurityAccess — they were
  Consecutive Frames of the `2E 2023` write (nibble-2 PCI). **No `27` in this session.** With the
  SGW bypassed (`[[sgw-bypass-always]]`) the successful `2F` actuations are the remote-unlock
  lead; next is identifying *which* `2F` DID drives the door lock (correlate with what was
  actuated in AlfaOBD) and verifying on 2022 ProMaster via the tap before replaying.
- **RFH (0xC7)** full ID block + TPMS; pair with labeled `RFH_FGA_Info.log` (current faults
  `U0001/B1040/C1502-FR/C1501-FL`) for the TPMS project. See `../tpms/`.

## Next steps

1. **Unlock:** identify which BCM `2F` IO-control DID drives the door lock/unlock (correlate the
   command log's timestamps with the actuations run in AlfaOBD, or its labels), then verify on
   2022 ProMaster via the tap before replaying — `2F 51xx ctrl=03 opt=xx`. See `promaster_2022/command_log.txt`.
2. Correlate `*_Info.log` labels ↔ debug-bin DIDs → labeled maps (start RFH/TPMS + radar).
3. Improve `reassemble_commands.py` response capture for the long `2E` writes (currently the
   request reassembles fully but the post-write response is only partly captured).
4. Once a DID/address/routine is *verified on 2022 ProMaster*, promote it into the canonical maps
   (`../../docs/bus-map.md`, `../../lib/modules.py`, project DID maps) per the maintenance rule.
