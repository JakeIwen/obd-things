# Guarded multi-module DTC batch worker

`tools/dtc_batch.py` is the live-capable companion to the deliberately
offline-only `tools/dtc_scan.py`. The batch worker was implemented and tested
offline on 2026-08-21; it has not yet been exercised against the vehicle.

## Fixed scope

The worker has exactly one diagnostic request:

```text
physical UDS 19 02 FF — report DTCs matching all supported status bits
```

Only immutable entries from `lib/modules.py` that also appear in the explicit
reviewed `DTC_BATCH_SUPPORTED_KEYS` allowlist can be selected. A future module
registry addition remains unsupported until its DTC framing/session behavior
is deliberately reviewed and promoted. There is no
payload, CAN-ID, channel, session, padding, or functional-address override.
The implementation has no path for `10` DiagnosticSessionControl, `3E`
TesterPresent, `14` ClearDiagnosticInformation, arbitrary `19` subfunctions,
or DTC clearing. ISO-TP flow control required to receive a segmented response
is transport behavior, not an additional diagnostic-service request.

PCM is listed as unsupported and receives no request. Its known identity path
used special DLC-8 padding/session framing, while its generic DTC request has
not been reviewed on the permanent dual-USBCANFD installation. Review the
exact default-session `19 02 FF` framing separately before changing that
policy.

The ordinary dry run opens no CAN socket and creates no job:

```bash
python3 tools/dtc_batch.py
python3 tools/dtc_batch.py --bus can-ch --json
```

The default plan contains 15 requestable modules: seven C-CAN, four B-CAN,
and four CAN-CH. PCM is the sixteenth registry row and remains unsupported.

## Live safety gates

Live execution requires all three explicit operator assertions:

```bash
python3 tools/dtc_batch.py --execute \
  --confirm-parked \
  --confirm-park-gear \
  --confirm-ignition-on-engine-off
```

The Park confirmation is an operator assertion because no verified passive
gear-position decode is approved as a safety gate. The other state assertions
do not substitute for machine evidence. Before arming and immediately before
every request, the worker also requires:

- a responding `van-telemetry` broker whose reviewed active-drive owner is
  enabled, idle, listen-only, and free of a restoration fault;
- fresh C-CAN `0x2EF` ignition-on presence;
- at least three new `0x0FC` samples at or below 50 rpm;
- a new `0x101` sample at no more than 0.1 mph;
- exact serial/`dev_id` resolution for C-CAN and the target logical role;
- exact same-boot physical-pair records (`6/14`, `3/11`, or `12/13`);
- no applicable same-boot operation inhibit;
- exact classical-CAN, FD-off, role bitrate, ERROR-ACTIVE, restart-ms-zero
  interface state; and
- a global service-request rate no greater than one request per second.

The fresh C-CAN snapshot may consume up to 750 ms. After it completes, the
worker checks the cooperative-cancel flag and revalidates the target USB
identity, armed interface state, physical-pair record, and inhibits one final
time. Those are the last operations before the fixed request is handed to the
transport.

For a non-C-CAN target, the worker first holds shared C-CAN role/channel
observer leases for the fresh vehicle-state gate, then takes the target's
logical-role lock followed by its freshly resolved channel lock exclusively.
For a C-CAN target the same exclusive ownership is used for state observation
and the fixed physical request. Modules are grouped by logical bus, so each
bus normally has one arm/restore interval rather than one link-state cycle per
ECU. USB identity, topology, inhibits, interface state, broker state, and fresh
vehicle state are rechecked before every transmission.

Every role is restored to its exact captured passive state before its reports
enter history. An unverified restoration stops the whole job, immediately
attempts to write a same-boot wildcard inhibit, retains the per-module evidence with
`restored_passive=false`, and refuses history/cache import for that role. Do
not clear the inhibit merely to retry; first inspect all four permanent
adapter roles and repair the restoration problem.

Restoration failure has terminal priority. The batch-level inhibit is
attempted immediately after cleanup and before report persistence, and a
secondary report-write failure cannot turn the job into an ordinary failure.
If the inhibit itself cannot be persisted, that separate failure is retained
in the terminal restoration detail instead of falsely claiming it was
latched. A failed-cleanup report preserves the raw response and its
`observed_category`, but its importer-facing category is `inventory_error`
and `authoritative_for_history=false`. Even a valid-looking `59 02` response
from such a report therefore imports only as unavailable and cannot establish
DTC absence or resolve prior state.

Timeouts, negative responses, and other unavailable results are imported as
unavailable attempts. They never become a zero-DTC observation and therefore
cannot resolve or erase the last successful module state. Only a strictly
parsed positive `59 02` response from a cleanup-verified report can establish
DTC presence or absence.

## Reports, history, progress, and cancellation

Each completed module gets an atomically replaced inventory-compatible report
under:

```text
tmp/inventories/<module>/dtcs_batch_<job-id>_<module>.json
```

After verified role restoration, those reports are imported through the
existing DTC historian and the compact dashboard cache is atomically
refreshed. Defaults remain:

```text
tmp/vehicle_data/dtc-history.sqlite3
tmp/vehicle_data/dtc-cache.json
```

One atomic job ledger lives at:

```text
tmp/inventories/dtc-batch/<job-id>/job.json
```

It records the current bus/module, per-module state and report path, queried,
unavailable and imported counts, terminal outcome, and any restoration fault.
A future UI runner can choose a safe job ID in advance with `--job-id` and
poll without CAN access:

```bash
python3 tools/dtc_batch.py --status <job-id> --json
```

Cooperative cancellation is a separate atomic flag checked before every arm
and request:

```bash
python3 tools/dtc_batch.py --cancel <job-id> --json
```

`--status` derives `cancel_requested` directly from that atomic flag, so a
supervisor sees the request immediately without racing the worker's progress
ledger writes. Cancellation is cooperative: it cannot retract a request
already handed to the transport and may wait for that bounded receive to
finish. A service-`0x78` response-pending sequence can extend the transport's
wait beyond the ordinary per-response timeout.

SIGINT, SIGTERM, and SIGHUP use fresh two-phase guards for each logical-role
window. Signals are recorded without raising while ownership is being armed
or released, are interruptible during active request work, and cannot cut
through socket/link cleanup. A signal received during setup or cleanup becomes
a cancellation only after the exact role has been restored. New guards are
created for the next role, so cleanup on one bus cannot suppress termination
on another.

## Locally armed Tailscale web trigger

The LAN listener remains cache-only. The separately configured Tailscale
listener exposes a fixed start/status/cancel UI, but a start also requires a
short-lived, one-use authorization created locally on vanpi:

```bash
python3 tools/dtc_web_arm.py
```

Its bind address is machine-local, not committed. Before installing/restarting
the Tailscale unit, verify the current Tailscale IPv4 address and create
`/etc/van-telemetry/tailscale-web.env` from the tracked example with both the
exact bind and matching `http://<address>:8765` origin.

Only the token digest is stored, in a mode-0600 file under
`/run/van-telemetry`; the plaintext token exists only in the terminal and the
browser password field. It expires after five minutes and is deleted before a
request is queued, including when later queueing fails. The POST must come
from the exact configured Tailscale origin and repeats the three operator
confirmations. It accepts no module list, CAN IDs, payload, session, clear
option, channel, command, or filesystem path.

The network-facing web process retains `NoNewPrivileges=true` and cannot arm
SocketCAN. It atomically creates one closed-schema request under `/run`.
`van-dtc-batch.path` activates a non-networked oneshot worker which consumes
that request and calls `tools/dtc_batch.py` with a fixed argument vector. The
worker, not the web request thread, owns the CAN operation and its termination
guards. Status is a bounded projection of the atomic job ledger. Cancellation
only creates the existing cooperative cancel flag; it cannot retract an
in-flight transport request. A `restoration_failed` job is sticky at the web
boundary: a new token cannot start another job. Inspect the exact roles and
same-boot inhibit locally; only after deliberate repair may the operator
manually retire the current-job pointer. No DTC clear endpoint exists.
