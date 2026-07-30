# Vehicle configuration

This project is the durable home for configuration work that can change the
2022 ProMaster's BCM PROXI record or require configuration alignment across
multiple modules. Begin with
[`LED_HEADLIGHTS_HANDOFF.md`](LED_HEADLIGHTS_HANDOFF.md) for the active LED
low-beam investigation.

## Current status

- The pre-change 250-byte BCM configuration was independently read twice and
  both reads are byte-identical. AlfaOBD's native backup decodes to the same
  bytes and records `Headlamp LED Management: Absent`.
- Historical FCA sources now place the records in the same broad PROXI/EOL
  configuration domain: 2011 CDA BCM bundles label DID `2023` as system
  configuration/PROXI and `40A2` as EOL data, while a September 2022 wiTECH
  report for the owner's prior model-year 2015 diesel VF labels them EOL data
  and an EOL configuration table. These are candidate labels, not a 2022
  field layout; however, the legacy 80-byte CUSW `40A2` layout and the current
  value both divide into five 128-bit node maps, providing a strong candidate
  for the record's broad block structure. Individual current node bits remain
  unresolved. See the
  [`legacy FCA Windows/CDA archive triage`](../ecu_mapping/findings/promaster_2022/2026-07-29_legacy_fca_windows_archive.md).
- On 2026-07-25 the owner tried AlfaOBD's labeled
  `Headlamp LED Management: Absent -> Present` change and PROXI alignment.
  AlfaOBD repeatedly reported `Failure connecting to module` for DASM even
  though direct DASM status loaded, ACC remained functional, no ACC fault was
  reported, and the odometer did not flash.
- The recovered raw trace now proves that AlfaOBD never addressed the installed
  DASM at `0x2A` during those alignment passes. It repeatedly sent `10 03` to
  nonresponding `0x26`, which its own model-88 catalog assigns to optional
  PAM2/Parking Assist, then rendered that result as a DASM failure. Treat this
  as a very-high-confidence AlfaOBD participant-address/label defect, not a
  radar failure.
- The original 250-byte backup was subsequently loaded through AlfaOBD's
  `Write custom configuration` editor, decoded by `Verify Custom Proxy`, and
  written as the recovery record. The alignment had to be retried about five
  times before no module other than DASM reported failure; DASM failed on
  every attempt.
- After the recovery alignment and an engine start, BCM DTC `B10AA-00` did not
  return. This is strong operational evidence that BCM configuration
  consistency was restored. A fresh post-recovery DID `0x2023` readback and
  status snapshot have not yet been archived, so the restored byte identity
  remains to be confirmed without another write.
- Do not retry the LED option or chase AlfaOBD's DASM alignment result merely
  to make its report green. Preserve the recovered state and use read-only
  checks if further confirmation is needed.

The sanitized baseline and provenance are in
[`findings/2026-07-25_proxi_baseline.md`](findings/2026-07-25_proxi_baseline.md).
The owner-observed write/alignment and recovery outcome is in
[`findings/2026-07-25_led_option_recovery.md`](findings/2026-07-25_led_option_recovery.md).
The recovered tablet-log analysis and standalone-alignment feasibility are in
[`findings/2026-07-26_alfaobd_proxi_dasm_misroute.md`](findings/2026-07-26_alfaobd_proxi_dasm_misroute.md).
Raw configuration bytes, full module logs, tablet evidence, and campaign
manifests remain ignored under `tmp/`.

## Safety boundary

Configuration changes and PROXI alignment are vehicle writes. Alignment is not
a harmless validation command: it can write several participating modules and
can leave the vehicle inconsistent if power, communications, or routing fails.

For this project:

- Never run alignment merely to test whether it works.
- Make only one supported, human-labeled configuration change per campaign.
- Preserve a fresh pre-write BCM record and compare it byte-for-byte with the
  known-good baseline before changing anything.
- Use regulated battery support continuously at 13.2–13.5 V and never above
  13.5 V during a write/alignment campaign.
- Stop if a required module cannot be reached or rejects the new
  configuration. Do not drive an unresolved partially aligned vehicle.
- Do not clear DTCs before collecting the post-operation evidence.
- Treat restore and alignment as separate operations: restoring the BCM record
  does not by itself prove every downstream module has been resynchronized.
- Keep the AlfaOBD external-operation inhibit active for the whole campaign.

The owner's earlier no-shift event means recovery planning is a prerequisite,
not an afterthought. A communications or interlock fault can leave the
electronic shifter locked in Park or Neutral; do not assume Neutral will be
available for trailer loading.

## Canonical references

Link to these sources instead of copying their evolving details here:

- Physical C-CAN, B-CAN, and CAN-CH routing and wake behavior:
  [`docs/bus-map.md`](../../docs/bus-map.md)
- Reusable AlfaOBD UI controller, adaptive waits, topology records, and
  campaign inhibit:
  [`docs/alfaobd-adb-controller.md`](../../docs/alfaobd-adb-controller.md)
- AlfaOBD profile, rendering, and artifact limitations:
  [`docs/alfaobd-evidence-history.md`](../../docs/alfaobd-evidence-history.md)
- BCM DID `0x2023`, historical writes, and the candidate LED flag:
  [`2026-07-21 candidate DID inventory`](../ecu_mapping/findings/promaster_2022/2026-07-21_candidate_did_inventory.md)
- Exact model/profile catalog and adapter evidence:
  [`2026-07-21 AlfaOBD APK catalog`](../ecu_mapping/findings/promaster_2022/2026-07-21_alfaobd_apk_catalog.md)
- Verified grey-adapter/CAN-CH routing:
  [`2026-07-25 CAN-CH live verification`](../ecu_mapping/findings/promaster_2022/2026-07-25_canch_live_verification.md)
- Guarded autonomous voltage-monitor behavior:
  [`projects/tpms/README.md`](../tpms/README.md#infrastructure-now-running)

## Data layout

Future machine-written output should default to
`tmp/vehicle_configuration/<campaign>/`. The completed July baseline remains
at `tmp/proxi_safety/` so its manifests and internal paths are not broken.
Nothing under either directory should be committed in place.

Promote only reviewed conclusions into this project. In particular, do not
track:

- full VINs or VIN-bearing configuration payloads;
- native backup files or raw 250-byte PROXI records;
- tablet screenshots, UI XML, debug bins, or cumulative info logs;
- unredacted module identification dumps; or
- raw DTC/capture logs.

Tracked findings may name ignored evidence paths and report byte counts or
equality results. Exact local digests remain in the ignored manifests so the
files can be authenticated without publishing a vehicle-specific fingerprint.
