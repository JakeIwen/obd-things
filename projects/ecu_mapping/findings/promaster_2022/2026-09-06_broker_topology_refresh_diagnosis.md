# Broker ownership loss after a voltage status refresh — 2026-09-06

## Outcome

An offline reproduction confirms a status-classification defect that can stop
the drive recorder while the broker's active helper still owns healthy C-CAN
and running-RPM evidence remains fresh. No CAN access, API listener, live
acquisition, service change, or production-code mutation was used.

This gives a concrete pre-drive repair target. The earlier recorder fix
correctly recovers from an ownership-loss exception, but does not prevent this
false ownership-loss decision.

## Exact path

1. `projects/battery/voltage_mon.py:acquire()` sends a broker acquisition
   request for `battery.voltage`. The installed crontab schedules this at
   10:00, 12:00, 14:00, 16:00, 18:00, 20:00, and 22:00 local time.
2. A running helper holds the authoritative role/channel lock, so an ordinary
   competing passive voltage sample can return `can_busy` without sending.
3. `TelemetryBroker.acquire()` nevertheless calls `_refresh_interface_status()`
   after validating the acquisition result, including this legitimate refusal.
4. `PassiveInterfaceManager` reports the resolved, armed role as
   `reason=interface_armed`, `passive_ready=false`. That is correct for its
   passive-observer admission contract.
5. `RoleAwareVoltageAcquirer.status_snapshot()` incorrectly uses that
   `passive_ready` bit as the value of physical `topology.usable`.
6. `TelemetryBroker.status_response()` overlays the helper's armed mode and
   owner, but leaves the newly false topology flag in place.
7. `drive_recorder.broker_armed_ready()` requires `topology.usable=true`, so it
   rejects the otherwise legitimate armed interval.

The refreshed interface snapshot is retained in broker state. This is therefore
not merely a short status-request timeout; recorder re-admission can remain
blocked until a later valid interface refresh. The stopped/parked admission
rules must remain intact when repairing the distinction between physical route
validity and permission for a passive observer to attach.

## Reproduction evidence

Compute job `20260906T201603Z-42d5c8de` ran one diagnostic test against the real
`RoleAwareVoltageAcquirer`, `TelemetryBroker.acquire/status_response`, and
`broker_armed_ready` implementations. The manager supplied a healthy resolved
armed interface; the acquisition itself was replaced with a no-I/O `can_busy`
result. Helper state and current running evidence were held unchanged.

Observed output:

```json
{
  "reproduced": true,
  "before_recorder_ready": true,
  "after_recorder_ready": false,
  "owner": "broker_active_drive",
  "running": true,
  "topology": {
    "bus": "c-can",
    "pair": "6/14",
    "source": "usb_serial_and_dev_id",
    "usable": false,
    "reason": "can7 is not listen-only"
  }
}
```

The test passed by asserting this currently defective behavior; it is a
diagnostic reproduction, not a passing regression for the desired repair.
Its exact original source is preserved in the compute job's source snapshot;
a convenience copy is at
`tmp/vehicle_data/test_topology_refresh_diagnostic.py`. It was removed from
the normal tests tree after the diagnostic run.

The schedule aligns with the August 31/September 4 ownership-loss intervals
documented in the
[drive review](2026-09-05_drive_inventory_oil_candidates.md). Those original
rejected status payloads were not retained, so this does not prove that every
historical interruption had this single cause. Host load and status-I/O
failures remain separate mechanisms.

## Next repair and verification

- Separate valid physical role/topology from passive-mode admission while
  preserving exact USB identity, pair, bitrate, controller, FD/ONE-SHOT,
  ownership, and inhibit checks. Do not globally turn an armed interface into
  a passive-ready role or grant arbitrary owners permission to record.
- Convert the diagnostic into a regression: legitimate active ownership must
  remain admissible after a scheduled voltage acquisition refresh.
- Add negative cases for identity changes, external armed owners, real
  topology/health faults, and failed restoration.
- Verify the correction offline before deployment. A later drive crossing a
  scheduled voltage-check boundary is the operational follow-up, not a
  prerequisite for developing the fix.

Other work available before driving: implement the replicated passive `0x760`
starred odometer path; plan a parked below-full `0227` fuel-level comparison.
The full VVT thermal trajectory and new passive CAN-CH correlations benefit
from the next ordinary drive.
