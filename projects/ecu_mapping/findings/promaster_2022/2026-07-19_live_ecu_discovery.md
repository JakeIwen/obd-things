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
That result did not prove an ECU absent. In particular, the PCM at `0x10` required the exact
legacy session/framing recipe shown by the current-van AlfaOBD trace on physical
`18DA10F1 -> 18DAF110`: `10 92 -> 50 92`, immediately followed by `1A 87 -> 5A 87 ...`.
The 27-byte identity response contains ASCII `68532157AI32157`. FCA's official J2534 report maps
`68532157AI` to the **2022 VF 3.6L PCM, 948TE, box on, 50-state** calibration lineage. This makes
the role and expected identity high-confidence. The independent verification below establishes the
endpoint from the PCAN tap.

That exhaustive result applies only to the pins-6/14 C-CAN branch under the recorded inherited
session and framing. Later offline AlfaOBD work proved that model-88 adapter `6` is MS-CAN BLUE
(pins 3/11) and adapter `7` is C-CAN2 GREY (pins 12/13). Reusing the same numeric target byte on
another physical branch is a separate experiment; the C-CAN timeouts do not rule out those modules.
See the [APK catalog/selector finding](2026-07-21_alfaobd_apk_catalog.md#adapter-routing-recovered-from-the-live-application-selector).

| phys | physical TX → RX | verified or inferred role | `F187` | corroborating identity |
|---|---|---|---|---|
| `0x10` | `18DA10F1` → `18DAF110` | 3.6L Pentastar PCM | not supported in the tested default state | fixed-DLC-8 `10 92`, then `1A 87`: `68532157AI` |
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
- successful padded PCM report: `tmp/discovery/ecu_discovery_20260721_130053_398245-0600.json`
- successful filtered PCM capture: `tmp/captures/ccan/events/pcm_probe_20260721_130052_-0600.candump`;
  promoted excerpt: [`2026-07-21_pcm_fixed_dlc_engine_idling.candump`](2026-07-21_pcm_fixed_dlc_engine_idling.candump)

## PCM independent verification — 2026-07-21

The bounded retry ran parked with the engine idling, transmission in Park, parking brake set, and
the PCAN on C-CAN. It used 29-bit normal-fixed ISO-TP and zero-padded every CAN frame to DLC 8.
The PCM answered both requests immediately:

- `10 92 -> 50 92` (exact session echo)
- `1A 87 -> 5A 87 02 40 7F 34 0D 20 47 15 08 00 36 38 35 33 32 31 35 37 41 49`

The second response contains ASCII `68532157AI`, matching the expected 2022 VF 3.6L PCM identity.
The two calls completed in about 15 ms, so the campaign's short runtime was expected rather than a
sign of early termination. The JSON report records 2/2 responses, `partial=false`, no fatal error,
and `restored_passive=true`; the wrapper then restarted `tpms-logger` as designed.

This independently verifies the address pair, session preamble, fixed-DLC-compatible framing, and
identity. It does **not** distinguish whether padding or the engine-running power state resolved the
earlier timeout because both changed between the failed and successful attempts. An engine-off padded
repeat would isolate that variable, but it is no longer required to register or use this endpoint.

## Next bounded steps

1. **Completed 2026-07-21:** modern `22 F187` coverage for `0xF2` through `0xFF`; all 14 targets
   timed out, adding no responder. See
   [`2026-07-21_readonly_module_inventory.md`](2026-07-21_readonly_module_inventory.md).
2. Confirm the TBM2 `0xC6` assignment with an official FCA part-number source if one becomes
   available; the live `TBM200A11P` identity, Mopar part supersession, and exact-vehicle OEM TBM2
   documentation already make it high-confidence.
3. **Completed 2026-07-21, parked/engine-idling:** the fixed-DLC-8 padded PCM probe received exact
   positive responses to both `10 92` and `1A 87`, independently verifying the `0x10` endpoint and
   expected identity. It is now registered as `pcm`; keep using the specialized legacy probe until
   its default-session and DID behavior are mapped.
4. **Initial bounded pass completed 2026-07-21:** per-module non-clearing DTC inventories and
   result-only (`31 03`) samples. Continue to keep the potentially large `19 0A` supported-DTC
   catalog opt-in and do not treat requestSequenceError as proof a routine exists.
5. The next new-address experiment is the eight-target, vendor-routed B-CAN profile rather than
   another full C-CAN address sweep: `python3 tools/ecu_discover.py --profile promaster88-bcan`
   dry-runs the exact plan. These remain candidates until an exact response is captured.

## Identity-source cross-checks

- FCA's [NHTSA Part 573 filing](https://downloads.regulations.gov/NHTSA-2023-0046-0001/attachment_1.pdf)
  lists `68517084AD` as an Instrument Panel Cluster supplied by Marelli for 2022–2023 ProMaster.
- The Mopar replacement catalog identifies telematics module `68647858AA` as superseding
  `68510377AC`; the local exact-2022-VF OEM procedure names the Global Telematics Box Module
  `TBM2` and places it on CAN-C:
  `/home/pi/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/b_code_charts/b22a9/b22a9-96/global_telematics_box_module_-_ecu_internal_performance_-_component_internal_failure.html`.
- FCA's official [wiTECH J2534 report](https://kb.fcawitech.com/assets/J2534_FedWorldReport.pdf)
  places `68532161AF` in the 2022 VF 3.6L 948TE TCM software lineage, corroborating address `0x18`,
  and maps the AlfaOBD PCM response string `68532157AI` specifically to a 2022 VF 3.6L PCM
  948TE box-on 50-state calibration.
