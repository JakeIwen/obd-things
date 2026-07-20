# Live C-CAN ECU discovery — 2026-07-19

This campaign independently checked physical diagnostic endpoints on the current 2022
ProMaster. It used the PCAN on the SGW-bypass C-CAN tap at DLC pair 6/14, 500 kbit/s,
29-bit normal-fixed ISO-TP with tester address `0xF1`.

## Conditions and safety boundary

- The vehicle was parked, ignition ON, engine OFF for all successful discovery and identity
  reads. An earlier ignition-OFF six-target pass received no responses.
- Discovery used one physical identity read per target (`22 F187` or legacy `1A 87`), never a
  functional broadcast, at no more than five requests per second.
- Identity inventories used physical `22` reads only. No session change, SecurityAccess,
  write, IO control, routine start/stop, DTC clear, or other actuation was sent.
- These were active diagnostic reads, not passive observations. Every run restored `can0` to
  listen-only mode before the normal logger was restarted.
- Raw reports remain gitignored under `tmp/`. Some composite `F1A0` identity responses in the
  original reports contain the full VIN; do not promote them without masking.

## Address discovery

The initial modern six-candidate pass found five responders (`0x18`, `0x1F`, `0x2A`, `0x40`,
and `0xC7`) and a timeout at the AlfaOBD-observed PCM address `0x10`.

The first expanded `22 F187` run attempted targets `0x00` through `0xF0` before stopping at
`0xF1`: normal addressing cannot use identical tester-to-ECU and ECU-to-tester CAN IDs at the
tester address. Its preserved partial report contains 241 attempts: seven positive responses
and 234 timeouts. The tool was then fixed to exclude `0xF1`.

The subsequent legacy `1A 87` run covered all 255 usable target addresses. The same seven
modern responders returned `7F 1A 11` (`serviceNotSupported`); the other 248 targets timed out.
That result does not prove an ECU is absent. In particular, the PCM at `0x10` remains unresolved:
the current-van AlfaOBD trace suggests it may require diagnostic session `10 92` before legacy
identity service `1A 87`.

| phys | physical TX → RX | verified or inferred role | `F187` | corroborating identity |
|---|---|---|---|---|
| `0x18` | `18DA18F1` → `18DAF118` | ZF 948TE TCM | `46342086` | `F194/F132=68532161AF`; `F192=ES11-1065 D` |
| `0x1F` | `18DA1FF1` → `18DAF11F` | electronic shifter | `P7FK46LXHAD` | `F188/F194=AGSM637FCA`; `F191=52209130`; `F192=073250002B0` |
| `0x2A` | `18DA2AF1` → `18DAF12A` | ACC radar | `68516215AE` | already mapped in the radar project |
| `0x40` | `18DA40F1` → `18DAF140` | BCM | `68524831AF` | `F188/F194=04446561007`; `F192=BC637M.0001` |
| `0x60` | `18DA60F1` → `18DAF160` | Marelli Instrument Panel Cluster (IPC) | `68517084AD` | `F192=50019990002`; `F194=04009460924` |
| `0xC6` | `18DAC6F1` → `18DAF1C6` | Global Telematics Box Module (TBM2), high confidence | spaces | `F132=68510377AC`; `F188=52225318`; `F191=52182163`; `F192=TBM200A11P` |
| `0xC7` | `18DAC7F1` → `18DAF1C7` | RF Hub | `68516285AC` | already mapped in the TPMS project |

## Raw provenance

- ignition-OFF six-target pass: `tmp/discovery/ecu_discovery_20260719_151730-0600.json`
- ignition-ON six-target pass: `tmp/discovery/ecu_discovery_20260719_154710-0600.json`
- partial modern expanded pass: `tmp/discovery/ecu_discovery_20260719_154841-0600.json`
- complete legacy expanded pass: `tmp/discovery/ecu_discovery_20260719_155100-0600.json`
- identity reports: `tmp/inventories/{tcm,shifter,bcm_ccan,ecu_60,ecu_c6}/identity_20260719_*.json`

## Next bounded steps

1. While parked, complete modern `22 F187` coverage for `0xF2` through `0xFF` (the unattempted
   tail of the interrupted modern run).
2. Confirm the TBM2 `0xC6` assignment with an official FCA part-number source if one becomes
   available; the live `TBM200A11P` identity, Mopar part supersession, and exact-vehicle OEM TBM2
   documentation already make it high-confidence.
3. Review and, while parked, test the AlfaOBD-observed PCM `10 92` → `1A 87` sequence. Treat
   the session change as active diagnostic state even though it is not an actuator.
4. Run per-module non-clearing DTC inventories and result-only (`31 03`) routine inventories
   only while parked; keep the potentially large `19 0A` supported-DTC catalog opt-in.

## Identity-source cross-checks

- FCA's [NHTSA Part 573 filing](https://downloads.regulations.gov/NHTSA-2023-0046-0001/attachment_1.pdf)
  lists `68517084AD` as an Instrument Panel Cluster supplied by Marelli for 2022–2023 ProMaster.
- The Mopar replacement catalog identifies telematics module `68647858AA` as superseding
  `68510377AC`; the local exact-2022-VF OEM procedure names the Global Telematics Box Module
  `TBM2` and places it on CAN-C:
  `/home/pi/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/b_code_charts/b22a9/b22a9-96/global_telematics_box_module_-_ecu_internal_performance_-_component_internal_failure.html`.
- FCA's official [wiTECH J2534 report](https://kb.fcawitech.com/assets/J2534_FedWorldReport.pdf)
  places `68532161AF` in the 2022 VF 3.6L 948TE TCM software lineage, corroborating address `0x18`.
