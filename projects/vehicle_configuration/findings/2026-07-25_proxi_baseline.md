# 2026-07-25 BCM PROXI baseline

This is the sanitized durable record of two read-only AlfaOBD campaigns on the
current 2022 ProMaster. It intentionally excludes the VIN, raw configuration
bytes, vehicle-specific digests, and unredacted identification dumps.

## Provenance and outcome

Procedure 1 ran from 2026-07-24 through 2026-07-25 and collected a complete
module/DTC inventory, BCM System ID, configuration-check state, AlfaOBD's
native backup, and an independently extracted DID `0x2023` record. Procedure 2
began on 2026-07-25 but stopped at the pre-write checkpoint.

Both campaigns were read-only:

- no configuration value was changed;
- no PROXI alignment was started;
- no DTC was cleared; and
- no reset, calibration, or Active Diagnostic was run.

The Procedure 2 read returned the same 250 configuration bytes as Procedure 1,
with zero differing bytes. The native AlfaOBD text backup also decodes to those
same 250 bytes. This makes the retained record a strong current-state backup.
It does not prove the configuration is the factory-as-built record, and no
restore has yet demonstrated that it is sufficient for end-to-end recovery.

## Verified BCM state

| Item | Verified value |
|---|---|
| AlfaOBD version | 2.4.4.0 |
| Vehicle menu | `RAM PRO MASTER (VF) 2022+` |
| BCM profile | exact 2022+ Delphi/Marelli/Aptiv Body Computer profile |
| PROXI status | OK |
| Configuration-check-fail counter | 0 |
| PROXI write counter | 15 |
| Headlamp LED Management | Absent |
| DID `0x2023` response | 253 bytes total |
| Configuration payload | 250 bytes |
| Procedure 1 vs Procedure 2 | byte-identical |
| VIN consistency | original/current fields matched; value intentionally omitted |

The pre-write BCM-reported voltage was 12.81 V. That was adequate for
read-only work but did not meet the 13.2–13.5 V programming-support range
found in the local OEM corpus. The same OEM guidance says not to allow the
charger to time out or exceed 13.5 V during programming.

## Configuration-check snapshot

The snapshot treated BCM, ECM, EPS, IPC, ABS, climate, TCM, shifter/AGSM, ORC,
HALF, TBM, entertainment/telematics, DASM, and RFH as present. EMCM was present
but not active/EOL-required. Relevant configured-absent options included PAM,
AFLM, blind-spot modules, trailer-tow, VTM, RMN, door module, steering lock,
and amplifier.

BCM, ECM, EPS, IPC, ABS, TCM, shifter, ORC, HALF, DASM, and RFH reported
`Response OK`. Climate and entertainment used AlfaOBD's
`Don't care or Not OK` semantics despite being configured present; that status
must not be reinterpreted as a newly failed alignment without profile-specific
evidence.

## Relevant DTC baseline

The full multi-module inventory remains in the ignored Procedure 1 manifest.
The configuration/headlamp-relevant subset was:

- BCM active/current: `B1632-15`, `B162E-15`, `B162A-15`, `B104E-15`, and
  `B104D-15`. The exact-vehicle OEM corpus associates this set with high-beam,
  low-beam, and DRL open/short-to-battery circuit monitoring.
- BCM confirmed/history: `B10AA-00`, the PROXI/configuration mismatch family,
  plus several communications/history records.
- IPC history: `U1700-86`, `U1741-87`, `U0010-00`, and `U0011-00`.
- TCM history: `P1500-00`.
- Shifter history: `P1C73-24`.
- ABS history: `C1200`, voltage above threshold, with last test passed. Its
  freeze-frame belonged to an earlier driving event; AlfaOBD's displayed
  `0.00 V` was default/invalid data, not the charger voltage during the
  campaign.

These codes are a comparison baseline, not a direction to clear them. A
post-change inventory should be collected before any clearing decision.

## Preserved ignored evidence

Procedure 1 root:

`tmp/proxi_safety/20260724_2033_baseline/`

- `procedure1_manifest.md` — full reviewed manifest and local digests
- `ProxyBackup_2026_07_24_19_48_12.txt` — native AlfaOBD backup
- `did_2023_response.bin` — complete positive response
- `proxi_250.bin` — extracted current configuration
- `alfaobd_ccan_passive.log` — passive C-CAN observation
- `alfaobd_post_bcan/` — B-CAN tablet evidence
- `canch_20260725/` — passive CAN-CH and tablet evidence

Procedure 2 root:

`tmp/proxi_safety/20260725_procedure2/`

- `procedure2_prewrite_manifest.md` — reviewed stop-point manifest
- `current_did_2023_response.bin` — fresh positive response
- `current_proxi_250.bin` — fresh 250-byte current configuration
- `ccan_live.log` — passive observation
- `controller_events.jsonl` — UI-controller event trail

The manifests retain exact SHA-256 values locally. They are deliberately not
repeated here because the underlying payload contains vehicle-identifying
material.

## What the baseline does not prove

- It is not an authoritative factory/VIN configuration. AlfaOBD 2.4.4.0 did
  not retrieve one, and none exists in the retained evidence.
- It does not prove the candidate LED field location. The historical write
  correlation needs one fresh labeled before/after comparison.
- It does not prove AlfaOBD can restore this vehicle after a partial alignment.
- It does not prove a no-op alignment will succeed or be harmless.
- It does not establish that the owner's earlier no-shift event was caused by
  the LED bulbs, the labeled configuration change, PROXI state, voltage, or an
  unrelated interlock fault.
