# Vehicle configuration

This project is the durable home for configuration work that can change the
2022 ProMaster's BCM PROXI record or require configuration alignment across
multiple modules. Begin with
[`LED_HEADLIGHTS_HANDOFF.md`](LED_HEADLIGHTS_HANDOFF.md) for the active LED
low-beam investigation.

## Current status

- The current 250-byte BCM configuration has been independently read twice and
  both reads are byte-identical.
- AlfaOBD reports `PROXI Status: OK`, configuration-check-fail counter `0`, and
  PROXI write counter `15`.
- `Headlamp LED Management` is currently `Absent`.
- Procedure 1 completed as a read-only inventory and backup campaign.
- Procedure 2 stopped before its first write because no factory/VIN
  configuration was available and voltage support did not meet the documented
  programming range.
- No alignment was run during either campaign.
- No restore has been tested on this vehicle. The retained current
  configuration is a verified backup, not a demonstrated end-to-end rollback.

The sanitized baseline and provenance are in
[`findings/2026-07-25_proxi_baseline.md`](findings/2026-07-25_proxi_baseline.md).
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
