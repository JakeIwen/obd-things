# 2026-07-26 AlfaOBD PROXI DASM misroute

This is the sanitized analysis of the AlfaOBD 2.4.4.0 logs recovered from the
Android tablet after the 2026-07-25 LED-option and recovery campaign. The raw
files contain the full VIN and vehicle-specific 250-byte PROXI records, so they
remain ignored under:

`tmp/vehicle_configuration/20260726_proxi_failure_logs/`

The acquisition was read-only. ADB copied all 23 files in AlfaOBD's external
`files/logs` directory, preserving device timestamps. The useful sources are:

- `device_logs/BCDELPHI_Info.log` — rendered alignment results and BCM status;
- `device_logs/AlfaOBD_Debug.bin` — cumulative raw adapter traffic;
- `AlfaOBD_Debug.decoded.txt` — locally decoded adapter traffic;
- `device_logs/ADAPTIVE_CRUISE_Info.log` — direct DASM identity and DTC result;
- `device_logs/BT_Debug.log` — adapter disconnect/reconnect workflow; and
- `reassembled_all.txt` — offline manual-ISO-TP command reconstruction.

## The reported DASM failure did not address the DASM

The BCM Info log contains five complete alignment result blocks in which the
other visited modules completed successfully and AlfaOBD rendered:

`Driver Assist System Module (DASM)... Failure connecting to the module`

The raw transport contradicts that label:

- During the Body-computer alignment recordings, AlfaOBD sent 84
  `DiagnosticSessionControl 10 03` attempts to physical address `0x26`
  (`18DA26F1`). None received a response.
- AlfaOBD did not address physical target `0x2A` during those Body-computer
  alignment recordings.
- The same installed APK's model-88 catalog maps `0x26` to optional `PAM2`
  (Parking Assist Module) on adapter 7 / CAN-CH. The current BCM configuration
  says the parking-assist function is absent.
- The installed Bosch DASM/radar is independently verified at `0x2A`
  (`18DA2AF1`) on ordinary C-CAN.

Minutes after the alignment failures, AlfaOBD's direct Adaptive Cruise profile
addressed `0x2A`. It immediately received a positive `10 03` response, a
matching `F1A5` identity response, positive tester-present responses, a valid
DTC response, and the complete identity set. The rendered result reported no
DASM faults. The BCM's own status snapshots also continued to describe DASM as
present, active, EOL-required, and `Response OK`.

This is therefore a **very-high-confidence AlfaOBD participant-address/label
binding defect**. The application either used the PAM2 address for a step it
called DASM or mislabeled a PAM2 reachability failure as DASM. It is not
evidence that the physical radar failed or was unreachable.

Do not "fix" the trace by blindly replacing `0x26` with `0x2A` in a write
sequence. The radar's complete DID inventory did not establish DID `0x2023`,
and the captured AlfaOBD alignment never demonstrated the DASM's configuration
write contract. The correct behavior may be to omit an absent PAM2 participant,
use a DASM-specific operation, or use a different payload/session rather than
send the generic 250-byte write to `0x2A`.

## Standalone alignment feasibility

The same capture materially improves the feasibility of a future
AlfaOBD-independent tool. Across the campaign, full 250-byte
`WriteDataByIdentifier 2E 2023` requests received positive acknowledgements
from 15 installed endpoints:

`40, 10, 60, 18, 1F, C6, C7, 98, 87, 85, D9, 30, 28, C0, 31`

Those endpoints cover ordinary C-CAN, B-CAN, and CAN-CH. The trace also
preserves the module order, adapter-change boundaries, session differences
(notably the PCM's legacy `10 92` path), response-pending behavior, retry
behavior, and multiple complete passes using the restored configuration.
`BT_Debug.log` explicitly records the adapter reconnections as
`ProxyAlignment continued`.

This is enough to build and test an **offline alignment transcript model** and
a dry-run campaign planner. It is not yet enough to authorize a live replay:

1. Harden the command reassembler against interrupted/manual ISO-TP records and
   prove each final response is assigned to the correct request.
2. Partition the transcript into exact attempts and adapter routes, then
   establish the accepted session, framing, payload, and terminal response for
   every installed ECU variant.
3. Determine the correct disposition of the false `0x26` participant without
   transmitting: omit absent PAM2, add a DASM-specific sequence, or classify
   DASM as already EOL-aligned only when independent status supports it.
4. Make the implementation recovery-first: fresh BCM readback, exact
   known-good equality, continuous 13.2–13.5 V support, per-module checkpoints,
   no automatic retry after an ambiguous write, and a verified rollback path.
5. Validate only during a future owner-authorized configuration/recovery
   campaign. Never run a no-op alignment merely as a test.

PROXI alignment is more operationally dangerous than the earlier single-ECU
radar calibration because it writes many safety- and drivability-relevant
modules across three physical buses. The captured wire protocol is,
nevertheless, sufficiently concrete that a guarded standalone implementation
is technically plausible.

## External/OEM context

The exact-vehicle OEM corpus describes PROXI/PROCSI as the BCM's stored vehicle
configuration, compared with other ECUs, and directs the scan tool's BCM
miscellaneous routine for alignment. It does not expose the wire protocol.
FCA bulletin
[08-150-23](https://static.nhtsa.gov/odi/tsbs/2023/MC-10241105-9999.pdf)
independently requires 13.2–13.5 V support, Restore Vehicle Configuration, and
then PROXI Configuration Alignment. That remains the safety baseline even for
an independent implementation.
