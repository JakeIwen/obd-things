# Parked C-CAN read-only module inventory — 2026-07-21

## Conditions and boundary

The owner ran `tools/ccan_inventory_campaign.sh` while parked with ignition ON, engine OFF,
and the PCAN on the pigtail's C-CAN DB9 (DLC 6/14). The campaign used only physical diagnostic
requests:

- `22 F187` against the remaining 29-bit normal-fixed target bytes `F2-FF`;
- `19 01 FF`, `19 02 FF`, and `19 03` against each verified module; and
- `31 03` requestRoutineResults for `0200-020F` plus `FF00-FF03`.

It did not use functional broadcast, change session, start/stop a routine, clear DTCs, request
SecurityAccess, write data, or perform IO control. All 15 live child runs completed without a
fatal error or partial report and verified a passive restore. Final interface state was C-CAN at
500 kbit/s, listen-only, ERROR-ACTIVE, with zero TX/RX CAN error counters.

## Address discovery completion

All 14 remaining target bytes `F2-FF` timed out on `22 F187`. Combined with the earlier `00-F0`
pass (excluding tester address `F1`), the physical `18DAxxF1` address-byte space has now been
covered in the inherited/default session. This did not add a responder beyond the seven already
registered modules. A timeout does not prove an ECU is absent or unreachable in every session;
the AlfaOBD-observed PCM at address `0x10` remains the known example of that limitation.

Raw report:
`tmp/discovery/ecu_discovery_20260721_004834_342418-0600.json`.

## DTC inventory

Every module answered `19 02 FF`. Every module rejected `19 01 FF` and `19 03` with
`7F 19 12` (subFunctionNotSupported). The `FF` status mask intentionally returns dormant and
not-yet-tested records too; record count is therefore **not** an active-fault count.

| module | records | status-bearing records worth preserving |
|---|---:|---|
| TCM | 55 | `P1500-00=08` confirmed/history; the other 54 are `40` (test not completed this operation cycle). Exact-vehicle OEM title: TCM ECU configuration mismatch. |
| shifter | 4 | `P081C-64=08`, `P1C73-24=08` confirmed/history; two records are `40`. Labels remain unresolved. |
| BCM | 107 | Five records are actively failing/pending/confirmed (`4D`): `B1632-15`, `B162E-15`, `B162A-15`, `B104E-15`, `B104D-15`; `B10AA-00=08` is confirmed/history. The exact-vehicle OEM corpus identifies these as left high-beam, right/left low-beam, right/left DRL open/short-to-battery circuits, plus BCM PROXI configuration mismatch. The other 101 records are `40`. |
| cluster | 9 | `U1741-87=0C` pending+confirmed but testFailed clear; eight records are `40`. Exact subtype label remains unresolved. |
| telematics | 15 | `U0100-00`, `U0129-00`, `U0140-00`, and `U0151-00` are `08` confirmed/history communication records; eleven are `40`. |
| RF Hub | 2 | `C1503-31=08` and `B1040-64=08`, both confirmed/history and not failing at the instant of this inventory. This agrees with the TPMS project's rear-left intermittent-sensor history and persistent B1040 history. |
| radar | 9 | `C1408-86=08` and `C1418-78=08`, both confirmed/history and not currently failing; seven are `40`. Exact-vehicle OEM titles are ESC fail status present / signal invalid and vertical misalignment / alignment incorrect. |

Status interpretation follows ISO 14229 bits: `08` is confirmed only; `0C` is pending+confirmed;
`4D` is testFailed+pending+confirmed+testNotCompletedThisOperationCycle; `40` alone is not a
current failure. No DTC was cleared.

Raw reports:
`tmp/inventories/{tcm,shifter,bcm_ccan,cluster,telematics,rf_hub,radar_acc}/dtcs_20260721_*.json`.

## Result-only routine inventory

No positive `71 03` result was returned. That is expected when no corresponding routine has been
started and does not establish that a module lacks routines.

| module | inherited-session result |
|---|---|
| TCM | `0200 -> 7F 31 24` requestSequenceError candidate; `FF02 -> 7F 31 12`; the other 18 RIDs returned `31` outOfRange. |
| BCM | `0204 -> 7F 31 24` requestSequenceError candidate; the other 19 returned `31` outOfRange. |
| cluster | all 20 returned `31` outOfRange. |
| RF Hub | all 20 returned `31` outOfRange. |
| shifter | all 20 returned `7F` serviceNotSupportedInActiveSession. |
| telematics | all 20 returned `7F` serviceNotSupportedInActiveSession. |
| radar | all 20 returned `7F` serviceNotSupportedInActiveSession. |

The TCM `0200` and BCM `0204` responses are only ordering-sensitive leads. NRC `24` by itself does
not prove a RID is implemented, and it does not authorize a `31 01` start request. Any future
session change or routine start needs separate review and authorization.

Raw reports:
`tmp/inventories/{tcm,shifter,bcm_ccan,cluster,telematics,rf_hub,radar_acc}/routines_20260721_*.json`.

## Next read-only campaign

The next high-yield step is a checkpointed per-module DID inventory, starting with bounded identity
and known-populated pages rather than a global `0000-FFFF` sweep. Prioritize the TCM, shifter, BCM,
cluster, and telematics modules; radar and RF Hub already have mature project-specific DID maps.
Use the current-van AlfaOBD requests as explicit candidate DIDs, keep namespaces per ECU, and leave
session changes out of the first pass.

Prepared offline after this campaign: `tools/ccan_inventory_campaign.sh --candidate-dids`. Its
dry-run-verified plan contains 1,341 physical `22` reads: the `F100-F1FF` page on TCM, shifter, BCM,
cluster, and telematics, followed by 61 BCM-only non-F1xx DIDs whose exact `62 <DID>` positive
responses are preserved in the current-van AlfaOBD map. The list intentionally excludes malformed,
negative, radar, and RF-Hub entries; those two modules already have mature project-specific maps.
The mode inherits the current session and sends no `10` or `3E` request.
