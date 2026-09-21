# Service dashboard evidence — September 16, 2026

## Odometer

Use the already-supported ICS `2001` reading with its existing candidate label.
The [August 30 comparison](2026-08-30_three_bus_drive_odometer_validation.md)
confirms its relative distance accumulation while retaining a roughly 11-mile
cluster discrepancy. Neither a silent offset nor a claim of authoritative IPC
mileage is justified. The dashboard now retains the last dated reading across
parked periods/restarts; a saved reading is never republished as fresh CAN data.

## Oil life is observed but its telemetry mapping is not established

The current-van July 22 AlfaOBD status export contains `Engine Oil Life
Remaining: 17 %`, alongside runtime-since-reset and other oil-change fields.
Source: `tmp/ecu_mapping/android_tablet/ccan_live_20260722_001010/pcm_system_status_text.txt`.
The same export includes `Automatic Oil Change Indicator Odometer When Reset:
429496729.60 km`, an unusable sentinel-like value. It cannot establish the last
oil-change mileage or date.

The [current-vehicle correlation finding](2026-07-22_ccan_alfaobd_live_correlation.md)
already calls oil life plausible, but does not establish its DID, field offset,
or scale. The tracked module map and subsequent PCM overlap/Plots findings do
not supply a reviewed current-vehicle oil-life decoder. The installed-profile
catalog uses unresolved numeric label references; direct zero-/one-based label
index substitution is explicitly not authoritative under
`docs/alfaobd-evidence-history.md`. Bounded read-only catalog queries
`20260917T010533Z-6ced5eba`, `20260917T010647Z-6448f3ce`,
`20260917T010754Z-7e92f9f5`, and `20260917T010827Z-3c95f4ee` confirm this
lookup limitation; no speculative DID or session request was sent.

Consequently the service panel says oil-life telemetry is not yet mapped.
The July percentage is historical evidence only. Temperature and pressure
are separate signals and are not converted into an invented oil-health score.
Next mapping work should join the exact PCM status request to this label and
validate its decoder/variant before a bounded no-session support read.

## Owner service records

The service journal is independent owner-entered history, not a vehicle oil-life
reset. Date and optional mileage, mileage source, notes, and recording timestamp
are retained. No oil-change date or mileage was inferred from the July logs.
Distance since service is computed only when both mileages use the ICS estimate;
cluster/receipt mileage must not silently be subtracted from the discrepant ICS
counter. The service journal never sends a diagnostic request.
