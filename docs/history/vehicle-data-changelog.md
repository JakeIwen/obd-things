# Vehicle-data changelog

Verbatim sections from `projects/vehicle_data/README.md`; relative links and
above/below references retain their original source-file context.

### Fixes deployed during the dashboard build (2026-09-24 → 09-27)

- `lib/canbus.py`: `CCAN_SIG` no longer contains `0x41A` (B-CAN carries it at
  about 1 Hz, so B-CAN was identified as C-CAN and the auxiliary odometer
  failed `wrong_bus`), and `CANCH_DIAG_SIG` holds CAN-CH *responses* only
  (`18DAF1xx` for ABS/EPS/HALF/ORC), because the van's own F1 sweep puts
  forwarded `18DAxxF1` requests on C-CAN. Tests: `tests/test_canbus_identify.py`.
- `derived.ccan_0x1f7_shaft_ratio` is in the broker's `ACTIVE_DRIVE_SOURCES`;
  its absence latched `restoration_failed` on the first moving gear sample on
  2026-09-24. Every snapshot source is now covered by a regression test in
  `tests/test_vehicle_data.py`.
- Tire pair asymmetry counts only moving, freshly sent readings, opens on the
  latest pair and clears fast (false alarm of trip 59; see
  [`warnings-redesign.md`](../../projects/vehicle_data/dashboard/docs/warnings-redesign.md)). The card
  sentence comes from `early_warning._pair_summary`.
- The broker's `armed_owner` and route-derived `topology_usable` survive the
  scheduled `voltage_mon` status refresh; see "Scheduled status refresh and
  secondary-route resilience" below and the
  [2026-09-06 diagnosis](../../projects/ecu_mapping/findings/promaster_2022/2026-09-06_broker_topology_refresh_diagnosis.md).

### Historical pre-migration deployment record

Everything below this heading describes the last verified single-PCAN
deployment and its dated capture evidence. It is intentionally preserved for
provenance and rollback comparison; it is not an installation recipe for the
permanent roles. The current deployment record above is authoritative; the
material below must not be restored as an operational recipe.

Last verified 2026-08-04:

Guarded PCM current crankshaft torque was deployed at 03:14 MDT. The broker
catalog and LAN dashboard expose `engine.crankshaft_torque` from fixed physical
DID `06DA` in lb-ft while preserving the approximately-one-hertz
`generator.field_duty` path. Two later complete drive captures contain 8,514
changing `06DA` positives from -67.28 through 269.88 Nm, paired one-for-one
with their physical requests. A torque-only failure remains isolated for that
epoch and cannot terminate generator/TPMS collection or the broker-owned raw
drive recorder.

The merged coordinated active-drive and generator-duty code is installed and
the broker has been restarted on it. Active-drive collection is enabled and
the LAN dashboard's `generator.field_duty` route is verified end-to-end. The
first live engine-running interval on 2026-07-30 sustained fresh approximately
one-hertz `01A1` observations, initially 72.083% and then 100.000%, while all
four TPMS positions and the allowlisted broadcasts continued to refresh.
PCAN remained ERROR-ACTIVE with zero TX/RX errors or drops throughout the
checked interval.

The tracked `van-drive-recorder.service` is also installed, enabled at boot,
and active. It was deployed receive-only during an ongoing drive with
overlapping temporary `candump` coverage, without restarting the broker or
reconfiguring `can0`. Its hardened campaign
`broker-drive-20260731T001704814083` began at
`2026-07-31T00:17:04.935569Z`; the manifest records
`candump -L -D -d -r 16777216 can0`. The compressed full stream continued to
grow after the overlap recorder stopped; its first ten-minute full and
priority rotations finalized and passed zstd verification. The stream
contained local PCM
`18DA10F1` requests, `18DAF110` positives, RF Hub `18DAC7F1` requests, and
`18DAF1C7` positives. No `DROPCOUNT` marker appeared. During the same check,
the LAN dashboard returned HTTP 200, generator duty remained fresh at
approximately one hertz (47.516%, 49.243%, then 59.494%), and every TPMS value
remained fresh. Interface RX/TX error and drop counters were zero; the
cumulative arbitration-lost counter was one before and after deployment and
did not increase.

The campaign finalized successfully at `2026-07-31T01:09:26.666381Z` after
52 minutes 22 seconds. Six full and priority rotations were complete, with
8,514,259 full-stream frames, 4,256,864 priority-stream frames, zero detected
socket drops, no leftover partials, and `full_stream_complete=true`. The final
chunk still contained exactly 121 PCM requests/positives and 121 RF Hub
requests/positives. The terminal reason was the intended
`tracked_id_absent`: the last `0x2EF` was followed by the configured 20-second
key-off tail.

The recorder's `can0: interface down` stderr line coincided with the broker's
expected restoration transition; `candump -D` remained alive and the campaign
completed normally. Broker status then reported `restoration_failed=false`,
no active inhibit, and a usable C-CAN pins-6/14 topology. `can0` read back UP,
500 kbit/s, listen-only, ERROR-ACTIVE, with zero RX/TX error and drop counters.
The recorder daemon returned automatically to its broker-owned-drive wait with
no child `candump` process. This proves clean finalization and automatic
rearming-to-wait; creation of the next timestamped campaign necessarily awaits
the next qualified engine-running interval.

Two later engine-running intervals then exercised that rearm path end to end.
Campaigns `broker-drive-20260731T012751675165` and
`broker-drive-20260731T030218233898` started without manual recorder action and
successfully finalized after 4,862.603 and 4,000.368 seconds. Together with the
first hardened campaign, all 22 full/priority rotations are complete:
32,555,907 full-stream frames, 16,273,335 priority-stream frames, zero detected
socket drops, and no leftover partial. This is direct multi-leg evidence that
the installed service automatically primes each subsequent drive.

The 2026-08-01/02 campaigns `broker-drive-20260801T225441745239` and
`broker-drive-20260802T014258086240` add 23,227,794 full-stream frames across
142.6 minutes, 16 verified chunks, and zero socket drops. Offline physical-wire
accounting found 8,514 complete scheduler cycles: every `01A1`, `06DA`, and RF
Hub request received its expected positive response, with 3.605 ms median and
9.203 ms maximum PCM response latency. The three one-hertz diagnostic reads
add six extended frames per second, conservatively below 0.2% of the 500-kbit/s
link. Full provenance and the failed zero-frame B-CAN attempt are recorded in
the
[`broker-drive poll validation`](../../projects/ecu_mapping/findings/promaster_2022/2026-08-04_broker_drive_poll_validation.md).

A machine-local `10-can0-passive-baseline.conf` drop-in performs a guarded
passive interface preflight before broker startup; it leaves an already-correct
interface untouched and otherwise uses the locked passive bring-up path.

- As of 2026-08-11, `van-telemetry.service` is enabled from
  `sys-subsystem-net-devices-can0.device`, not `multi-user.target`. It binds to
  that device unit, so an absent PCAN leaves the telemetry stack inactive
  without blocking boot or retrying, appearance of `can0` starts the broker,
  and removal of `can0` stops it.
- `van-telemetry-web.service` and the separate machine-local Tailscale web
  service are installed but are not enabled independently at boot. Starting
  the device-activated broker pulls both listeners in through its wanted-unit
  relationships.
- `van-drive-recorder.service` is installed, enabled, and running. It is
  independent of the broker's service lifetime, waits safely when the broker is
  unavailable or not armed, and restarts on recorder failure with bounded
  systemd backoff.
- Starting the broker also pulls in the LAN listener. The LAN listener is
  `PartOf=van-telemetry.service`, so a broker restart restarts the listener
  rather than leaving it detached. The machine-local deployment applies the
  same lifecycle relationship to the Tailscale listener and adds it to the
  broker's wanted units. A deliberate broker stop therefore stops both web
  listeners, while a later broker start restores the complete installed web
  stack.
- The live broker registry exposes all four verified TPMS wheel metrics.
  `tpms-logger.service` is enabled and running, but the merged process detects
  the live broker and yields without taking a CAN lock or opening a diagnostic
  socket. The broker's coordinated active-drive owner polls TPMS alongside
  `01A1` while the engine-running gate holds.
- The broker is available only through
  `/run/van-telemetry/api.sock`, owned by the unprivileged `pi` user/group.
- The tracked web unit remains loopback-only. A machine-local systemd drop-in
  at
  `/etc/systemd/system/van-telemetry-web.service.d/10-lan.conf`
  deliberately adds `--allow-remote-bind` and binds one selected Ethernet
  address. The address is operational host configuration and is not tracked.
- Trusted devices on the van LAN use `http://vanpi.lan:8765/`. The service is
  unauthenticated, so it must not be port-forwarded or exposed beyond a trusted
  network.
- Trusted tailnet devices use a second cache-only listener bound to vanpi's
  specific Tailscale address on port `8765`. It is a separate machine-local
  unit so the LAN endpoint remains available and neither listener needs a
  wildcard bind. Tailnet access remains subject to Tailscale policy; the
  dashboard itself does not add authentication.
- The live web services omit `--allow-acquisitions`. Historically this blocked
  voltage acquisition POSTs. On September 20 the owner requested passive reads
  enabled by default, implemented in the parser with `--cache-only` as opt-out.
  An externally performed restart at 20:22:54 UTC subsequently activated the
  default; the LAN snapshot now advertises acquisition enabled. GETs/streams
  remain cache-only and wake requests remain blocked.
