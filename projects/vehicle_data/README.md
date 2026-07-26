# Vehicle telemetry broker

This project exposes a small, approved metric vocabulary without exposing raw
CAN, arbitrary DIDs, or configuration functions. It accepts a few deliberately
named trip-logger observations, including four raw cluster values whose exact
DIDs, byte widths, sources, and candidate quality are fixed in the registry.

The implementation has two trust zones:

- `broker.py` owns CAN access and serves a Unix-domain HTTP API. Its GET
  endpoints only read cache. An allowlisted acquisition POST may touch the
  built-in battery reader; a separate strict observation POST may only populate
  an exact metric/source tuple already approved for a local logger.
- `web.py` has no CAN imports and proxies cache/status over HTTP. It defaults
  to loopback and requires `--allow-remote-bind` for any other address. It
  rejects all acquisition requests unless deliberately started with
  `--allow-acquisitions`; neither the tracked nor live systemd service enables
  that flag.

The code does not install or enable itself. The units under `systemd/` retain
safe loopback defaults and must be reviewed against the target host. The
current vanpi deployment is recorded below.

## Metric and quality

`battery.voltage` chooses an approved broadcast reader after passive bus
classification:

| Source | Bus | Quality | Notes |
|---|---|---|---|
| `bcan.broadcast.0x46c` | B-CAN, 125 kbit/s | `verified` | low 13-bit word / 400 |
| `ccan.broadcast.0x41a` | C-CAN, 500 kbit/s | `verified` | byte0 x 0.05 V + 4.0 V; readable in a parked wake |
| `cluster.did.1004` | C-CAN, 500 kbit/s | `observed_alfa_scale` | physical `22 1004`; Alfa-observed raw u8 x 0.1 V |

C-CAN `0x2EF` remains an ignition-on presence gate, not an approved voltage
source. Its payload is mode-dependent and the former low-13-bit `/400` decode
has been withdrawn.

Canonical decoding and bus evidence remains in
[`docs/bus-map.md`](../../docs/bus-map.md) and the battery readers; the broker
does not create a second source of truth.

Every available observation includes value, unit, source, bus, acquisition
class, quality, wall-clock timestamp, age, and staleness. Failures use a stable
reason such as `adapter_absent`, `wrong_bus`, `bus_asleep`, `can_busy`,
`rate_limited`, or `restoration_failed`.

The initial drive-publisher vocabulary is intentionally narrow:

| Metric | Source | Type/unit | Quality |
|---|---|---|---|
| `battery.voltage` | `cluster.did.1004` | number, `V` | `observed_alfa_scale` |
| `vehicle.ignition_on` | `ccan.broadcast.0x2ef` | boolean, `boolean` | `verified` |
| `diagnostics.cluster.did.1000.raw` | `cluster.did.1000` | integer, `raw_u16_be` | `candidate` |
| `diagnostics.cluster.did.1002.raw` | `cluster.did.1002` | integer, `raw_u8` | `candidate` |
| `diagnostics.cluster.did.0107.raw` | `cluster.did.0107` | integer, `raw_u8` | `candidate` |
| `diagnostics.cluster.did.1005.raw` | `cluster.did.1005` | integer, `raw_u8` | `candidate` |

`vehicle.ignition_on` is a positive-presence witness: a received `0x2EF`
frame may publish only `true`. A publisher-supplied `false` is rejected
because silence is not a decoded negative value; the observation instead
expires to stale/unknown when the frame disappears.

The raw rows preserve evidence without presenting unverified RPM, speed, gear,
or temperature conversions as facts. They are diagnostics metrics, not a
general-purpose DID publication namespace.

## Owner-priority telemetry roadmap

The first engine-health additions should be oil pressure, coolant temperature,
engine-oil temperature, RPM, actual crankshaft torque, and derived power.
Oil pressure is a particularly strong target: the exact-vehicle OEM material
confirms a scan-tool-readable EOP sensor and dual-stage pump, while the
current-vehicle Alfa configuration literally reports `Oil Pressure ABS: Yes`
and `Oil Pressure Sensor: Enabled`. Those vendor labels are useful navigation
evidence, not an independent decode of `ABS` or a DID/scale. Expected pressure
bands differ by RPM, operating temperature, and pump mode, so the display
cannot use one static good/bad threshold. Power must be labeled as
ECU-estimated crankshaft power, not wheel horsepower.

The DIDs and scales are not yet qualified, so none of those names belongs in
the public registry today. The evidence, exact OEM pressure/thermostat context,
alert-design constraints, PCM/TCM acquisition sequence, and later mechanical
and electrical targets are maintained in the
[`priority telemetry finding`](../ecu_mapping/findings/promaster_2022/2026-07-25_priority_telemetry_targets.md).
The dashboard keeps inert roadmap cards visible for oil pressure, coolant
temperature, engine-oil temperature, crankshaft torque, and crankshaft power.
Those labels do not create metrics or imply that a source is available. A card
receives a value only after a matching registry definition and qualified fresh
observation exist. Context-aware oil-pressure bands, engine-running/startup
gates, and fresh time-aligned torque/RPM power derivation still require
specialized evaluation and presentation logic.

## Dashboard profiles and vehicle state

Dashboard values are registry-driven even where the layout keeps a future
metric role visible. `GET /v1/snapshot` returns the public metric catalog,
every metric's cache-only response, broker/interface status, and an
evidence-qualified `vehicle_state` object in one request. A future registered
metric therefore becomes available to the generic metric and catalog panels
without adding another web proxy route or SSE request.

Built-in dashboard profiles are **Overview**, **Parked**, **Driving**, and
**Diagnostics**. The user can select one manually, choose **Automatic**, or
choose exactly which panels appear in a **Custom** profile. The selection and
custom panel list use browser `localStorage`; they are per-device preferences
and never write broker configuration or touch CAN.

Future drive, engine-health, and tire roles remain visible with `MAPPING
PENDING` instead of disappearing. This makes the intended dashboard and current
mapping gaps explicit without inventing a value. Registry membership is a
metric-schema and evidence boundary, not a request for human approval before
each read. Candidate metrics may appear in Diagnostics, but they remain
withheld from driver-qualified hero values until their identity and scaling
meet the recorded evidence policy.

Automatic mode currently makes only these evidence-backed choices, and every
state used for a layout must carry a finite nonnegative age no older than three
seconds:

- fresh `asleep`/`parked` selects the Parked electrical layout;
- the verified `vehicle.ignition_on` observation selects Driving while true;
- a future verified `moving` or `running` state also selects Driving;
- `awake` or `unknown` selects Overview.

Automatic Driving selection is only a layout choice. It is not an
engine-running safety gate for oil-pressure alerts or any other mechanical
limit evaluator.

The broker deliberately does **not** infer engine-running state from charging
voltage. An external charger can overlap alternator voltage, and ordinary bus
activity can be ignition-on, a key-fob wake, or a broker-assisted wake. Current
passive acquisition can report `awake`, inferred `asleep`, or `unknown`, with
`running: null` whenever the evidence cannot distinguish those cases. This
keeps the automatic layout engine ready for a separately verified
ignition/motion metric without silently promoting a voltage heuristic.

## Safety contract

Passive reads:

1. take the shared `can0` observer lock;
2. require the interface to already be UP, listen-only, ERROR-ACTIVE, and at
   125 or 500 kbit/s;
3. classify the bus from observed traffic;
4. read only the allowlisted broadcast frame.

An ordinary passive read never brings up or reconfigures an interface. The
collector has one separate, guarded auto-retune path for a physical C-CAN ↔
B-CAN leg change:

1. the evidence streak must begin with an RX-error-backed `wrong-rate`
   result; three consecutive qualifying observations are required, with a
   resulting listen-only `ERROR-WARNING`/`ERROR-PASSIVE` state allowed to
   continue—but never initiate—the streak;
2. a separate helper takes the exclusive channel lock, requires a listen-only
   controller that is either healthy or plausibly degraded by the preceding
   wrong-rate sample, checks external-operation inhibits, and independently
   rechecks the evidence;
3. it invalidates the old topology record, configures only the other approved
   bitrate with listen-only explicitly on and `restart-ms 0`, then accepts the
   change only if known bus signatures identify C-CAN, B-CAN, or CAN-CH;
4. an unrecognized alternate rate is restored to the starting passive
   configuration. A controller degraded by wrong-rate sampling may recover to
   `ERROR-ACTIVE` during that restoration. Silence alone is never enough to
   switch or guess a physical leg.

The helper uses noninteractive `sudo` for only the existing SocketCAN
down/up commands. If that privilege is unavailable, it fails closed and
reports the reason rather than leaving the failure invisible. Retune attempts
have a 30-second cooldown. Participating observers and active tools exclude the
helper through the shared/exclusive channel lock; an armed interface and
AlfaOBD/external inhibit also block it. The helper explicitly refuses to run
while `tpms-logger.service` or `tpms-drivesniff.service` is active because their
interface expectations could otherwise fight the selected bitrate. Older
processes that bypass both the shared lock and these known service gates cannot
be detected reliably.

CAN-CH/grey is identified and reported but rejected as a battery source.
Auto-retuning never transmits a CAN frame.

`wake_if_asleep` first performs the same passive path. Only a silent bus can
proceed. It then takes the exclusive diagnostics lock and rechecks the interface
and silence. Wake additionally requires a usable same-boot C-CAN or B-CAN
topology record, matching bitrate, and no active operation inhibit. CAN-CH is
always forbidden. The existing wake helpers restore listen-only mode; the
acquirer also compares the complete pre/post interface snapshots and reports
`restoration_failed` if exact restoration cannot be proven.

Because a silent C-CAN branch cannot be passively distinguished from a silent
CAN-CH branch at the same bitrate, every physical adapter or routing change
must invalidate/update the topology record. The guarded auto-retune helper does
that itself only after strong wrong-rate evidence; a silent or otherwise
unrecognized cable change still requires
`tools/can_operation_state.py`. A same-boot record is a necessary gate, not
permission to ignore a cable change.

Wake requests are coalesced, serialized by the channel lock, and limited to one
attempt per metric every 15 minutes by default. The passive collector never
requests wake.

`GET /v1/status` includes an `auto_retune` object with its current state,
wrong-rate evidence count, cooldown, explanatory detail, and the complete last
attempt outcome. The dashboard renders the same state under **Auto bus
switch**, including why a switch was blocked or failed.

The Unix HTTP transport itself is serialized so active CAN cleanup remains on
the process main thread, where the existing termination-signal guard is valid.
Consequently a cache GET can wait behind one bounded active acquisition, but it
cannot interrupt it or create concurrent CAN work. The unprivileged web process
is threaded independently.

## Local API

The default Unix socket is `/run/van-telemetry/api.sock`.

```text
GET  /v1/status
GET  /v1/snapshot
GET  /v1/metrics
GET  /v1/metrics/battery.voltage
POST /v1/acquisitions/battery.voltage
     {"mode":"passive"}
     {"mode":"wake_if_asleep"}
POST /v1/observations/<allowlisted-metric>
     {"value":...,"unit":"...","source":"...","bus":"...","quality":"..."}
```

GETs are cache-only. Observation POSTs exist only on the Unix API and are not
proxied by `web.py`, even when web acquisition is deliberately enabled. The
body must contain exactly the five shown fields. Metric, source, unit, bus,
quality, scalar type, and numeric bounds are validated together; publisher
timestamps and acquisition labels are rejected. The broker stamps wall-clock
and monotonic receipt time itself. `TelemetryClient.publish()` also supplies a
local monotonic queue deadline of at most one second in an HTTP header. The
serialized broker rejects an expired request instead of accepting a value as
fresh after the publisher has already timed out behind another acquisition.
The deadline is only an admission bound; it is never used as the observation
timestamp. Existing broadcast battery sources are not publisher-enabled,
preventing a local logger from masquerading as the in-process voltage reader.

There is no raw frame, arbitrary DID, diagnostic session, DTC, reset,
calibration, configuration, or PROXI endpoint.

`/v1/snapshot` is the preferred dashboard endpoint. Its shape is:

```text
{
  "status": { ..., "vehicle_state": {...} },
  "catalog": [ ...public metric definitions... ],
  "metrics": { "battery.voltage": {...} }
}
```

The web SSE stream uses that same cache-only snapshot. It does not acquire or
poll CAN when a browser connects. The web tier adds a `web_delivery` envelope
to every HTTP and SSE snapshot with a process-instance ID, increasing
sequence, wall-clock generation time, and process-monotonic generation time.
The browser establishes an instance with a cache-bypassing HTTP snapshot,
accepts only newer events from that instance, and bounds HTTP round-trip time
before using its midpoint to map the web process's monotonic clock onto the
browser's monotonic clock. Stream age never depends on wall time, so an NTP
clock step cannot make queued data younger. The full bounded HTTP trip and the
monotonic-offset uncertainty are conservatively added to embedded observation
ages. A queued stream event is rejected if it is over ten seconds old **or**
if its delivery delay would carry any available metric or verified vehicle
state past its registered freshness window. Missing, nonnumeric, or negative
ages are never driver-qualified.

A visible-page watchdog advances cached ages using the browser's monotonic
clock even when no event arrives; expired verified vehicle state becomes
unknown and can no longer select the automatic Driving layout. A stalled or
errored stream triggers a new cache-bypassing HTTP baseline. On page
hide, restoration, or visibility change the browser immediately invalidates
the displayed cache, closes the old stream, obtains a fresh no-store HTTP
snapshot when visible, and only then opens a new stream. It does not assume
that `performance.now()` advanced across Android deep sleep. Obsolete HTTP
callbacks cannot render. Together these guards prevent Chrome/Android tab
suspension, buffered SSE, wall-clock adjustment, or a dead stream from
replaying old relative-age fields as apparently live telemetry.

Examples:

```bash
python3 projects/vehicle_data/client.py status
python3 projects/vehicle_data/client.py get battery.voltage
python3 projects/vehicle_data/client.py acquire battery.voltage --mode passive
python3 projects/vehicle_data/client.py acquire battery.voltage \
  --mode wake_if_asleep --confirm-wake
python3 projects/vehicle_data/client.py publish battery.voltage \
  --value 12.4 --unit V --source cluster.did.1004 --bus c-can \
  --quality observed_alfa_scale
```

Logger code can use
`TelemetryClient.publish(metric, value=..., unit=..., source=..., bus=...,
quality=...)`; it returns the same `(HTTP status, response object)` tuple as
`TelemetryClient.request`.

For a manual cache-only dashboard:

```bash
python3 projects/vehicle_data/web.py --bind 127.0.0.1 --port 8765
```

The dashboard uses server-sent events, but every stream update is still made
from broker GET endpoints and cannot trigger CAN traffic. Bind defaults to
loopback; remote access normally belongs behind an authenticated proxy. A
deliberately trusted interface can instead be selected explicitly:

```bash
python3 projects/vehicle_data/web.py \
  --bind <interface-address> --port 8765 --allow-remote-bind
```

This opt-in does not add authentication. Bind to one intended interface address
and keep the service cache-only; avoid a wildcard bind unless another layer
restricts clients.

## Current vanpi deployment

Last verified 2026-07-26:

- `van-telemetry.service`, `van-telemetry-web.service`, and a separate
  machine-local Tailscale web service are installed, enabled at boot, and
  running.
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
- The live web service omits `--allow-acquisitions`. Dashboard GETs and stream
  updates are cache-only, and acquisition POSTs fail closed with HTTP 403.

Inspect the effective unit rather than assuming the tracked example matches the
host:

```bash
systemctl is-enabled van-telemetry.service van-telemetry-web.service \
  van-telemetry-web-tailscale.service
systemctl is-active van-telemetry.service van-telemetry-web.service \
  van-telemetry-web-tailscale.service
systemctl cat van-telemetry-web.service
systemctl cat van-telemetry-web-tailscale.service
ss -lntp | grep ':8765'
curl --fail http://vanpi.lan:8765/v1/status
curl --fail http://<tailscale-ip>:8765/v1/status
```

If the selected LAN address changes, update the machine-local drop-in, run
`systemctl daemon-reload`, and restart only `van-telemetry-web.service`. Do not
replace the explicit address with a wildcard merely to avoid maintaining the
override.

## Existing voltage monitor

`projects/battery/voltage_mon.py` uses the broker when its Unix socket exists.
If the socket exists but is invalid or unreachable, the monitor fails closed
instead of bypassing the intended owner. Before the service is deployed it uses
the same in-process `VoltageAcquirer`, so its scheduler and notification path
continue to work while acquisition safety still has one implementation.

## Validation

Offline tests use fake interfaces, locks, sources, and clocks. They cover
cache-only GETs, strict local publication, typed values, source-metadata
allowlists, broker-stamped age, passive acquisition,
silent-bus wake gates, coalescing/rate limits, post-wake restoration failure,
Unix API behavior, the registry-driven snapshot, evidence-qualified vehicle
state, dashboard profile assets, and cache-only web defaults.

The services are deployed on vanpi as described above. Service health and
offline validation are not evidence of a successful live CAN acquisition;
inspect metric provenance and quality on every available observation.
