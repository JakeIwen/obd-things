# AlfaOBD ADB state controller

`tools/alfaobd_controller.py` is the reusable, fail-closed UI observer and
read-only action controller for AlfaOBD on the attached Android tablet. It runs
on vanpi, so polling occurs beside ADB rather than through repeated SSH
round-trips.

The implementation is in `lib/alfaobd_adb.py`. The guarded singleton monitor
campaign also uses its adaptive polling primitive.

## Safety boundary

The controller has no generic tap command. Its action allowlist contains only:

- connect
- disconnect
- acknowledge the adapter-routing prompt
- continue past an ISO-verification warning
- read System ID
- read all faults

It exposes no DTC clear, Active Diagnostics, reset, calibration,
configuration, write, or PROXI action. Diagnostic actions require both
`--execute` and `--confirm-read-only-diagnostics`. A tap is logged once and is
never retried because an ADB error after submission leaves delivery ambiguous.

The controller takes a fresh UI dump immediately before every allowlisted tap.
It refuses a missing, duplicate, stale, disabled, non-clickable, or
multiply-matched target.

## Unattended CAN-wake coordination

The SocketCAN channel lock cannot see AlfaOBD because Alfa uses the separate OBDLink interface.
Every executed controller action therefore creates a same-boot `alfaobd` inhibit before ADB is
opened. The inhibit remains after disconnect and must be removed explicitly only after the complete
vehicle campaign is over:

```bash
python3 tools/alfaobd_controller.py campaign-begin
python3 tools/alfaobd_controller.py campaign-status
python3 tools/alfaobd_controller.py campaign-end
```

An Alfa adapter prompt invalidates the recorded physical topology. Before physically moving an
adapter, mark it unknown; after the new routing is confirmed, record the exact branch:

```bash
python3 tools/can_operation_state.py topology-set unknown \
  --source manual_adapter_change

python3 tools/can_operation_state.py topology-set can-ch \
  --pair 12/13 --source manual_grey_confirmation
```

Use `c-can --pair 6/14` or `b-can --pair 3/11` after those routes are physically confirmed.
Topology records are valid only for the current Pi boot. Missing, stale, malformed, unknown, or
CAN-CH state cannot authorize an unattended wake. A forgotten inhibit also fails safely by blocking
wake; do not use `campaign-end` merely to silence that status.

## States and waits

The classifier recognizes:

- connected and disconnected
- connection in progress
- adapter prompt
- ISO-verification warning
- populated and empty System ID pages
- populated faults, explicit no-faults, and empty faults pages
- failure
- main screen
- unknown/intermediate UI
- timeout

Polling uses compressed `uiautomator dump` calls with a default 0.75-second
delay between completed dumps. Calls never overlap. On the current tablet a
dump itself takes about 2.8 seconds, so the effective observation period is
roughly 3.5 seconds; the interval is not a claim that this tablet can produce
two complete hierarchies per second.

The wait returns immediately after an expected state appears. Adapter and ISO
dialogs are terminal intervention states: callers must explicitly include
them as expected or the wait fails closed. Unexpected terminal failures and
timeouts save the latest XML, a screenshot, and JSON metadata below
`tmp/alfaobd_controller/failures/`.

Read-only UI-dump failures may be retried at most twice, with every retry
logged. Taps are never retried.

## Examples

Observe without tapping:

```bash
python3 tools/alfaobd_controller.py observe
```

Wait up to 60 seconds for connection, a routing prompt, or an ISO warning:

```bash
python3 tools/alfaobd_controller.py wait \
  --expect connected,adapter_prompt,iso_warning \
  --timeout 60
```

Inspect a connection action without touching the tablet:

```bash
python3 tools/alfaobd_controller.py action connect
```

Perform one confirmed read-only connection action:

```bash
python3 tools/alfaobd_controller.py action connect \
  --execute --confirm-read-only-diagnostics
```

The controller logs state changes, bounded dump retries, tap intent/return, and
the terminal result to stderr as JSON. `--event-log PATH` also appends those
records to a file.

## Delays intentionally retained

`tools/alfaobd_singleton_campaign.py` no longer uses fixed sleeps for dialog
opening, checkbox transitions, dialog confirmation, monitor start, monitor
stop, or early log growth. Three timed waits remain because elapsed time is
the evidence being collected rather than a proxy for UI completion:

- the requested monitor-segment dwell
- the post-stop artifact-stability observation window
- the equivalent cleanup artifact-stability observation window

Those intervals cannot be replaced by immediate UI state detection without
weakening the campaign's provenance checks.
