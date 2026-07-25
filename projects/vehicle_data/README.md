# Vehicle telemetry broker

This project exposes a small, approved metric vocabulary without exposing raw
CAN, arbitrary DIDs, or configuration functions. The MVP metric is
`battery.voltage`.

The implementation has two trust zones:

- `broker.py` owns CAN access and serves a Unix-domain HTTP API. Its GET
  endpoints only read cache. Only an allowlisted acquisition POST can touch a
  source.
- `web.py` has no CAN imports and proxies cache/status over loopback HTTP. It
  rejects all acquisition requests unless deliberately started with
  `--allow-acquisitions`; the tracked systemd unit does not enable that flag.

Nothing here installs or enables a service. The units under `systemd/` are
deployment examples and must be reviewed against the live Pi before use.

## Metric and quality

`battery.voltage` chooses an approved broadcast reader after passive bus
classification:

| Source | Bus | Quality | Notes |
|---|---|---|---|
| `bcan.broadcast.0x46c` | B-CAN, 125 kbit/s | `verified` | low 13-bit word / 400 |
| `ccan.broadcast.0x2ef` | C-CAN, 500 kbit/s | `approximate` | fine field; exact divisor remains unpinned |
| `ccan.broadcast.0x41a` | C-CAN, 500 kbit/s | `approximate` | coarse parked-wake field |

Canonical decoding and bus evidence remains in
[`docs/bus-map.md`](../../docs/bus-map.md) and the battery readers; the broker
does not create a second source of truth.

Every available observation includes value, unit, source, bus, acquisition
class, quality, wall-clock timestamp, age, and staleness. Failures use a stable
reason such as `adapter_absent`, `wrong_bus`, `bus_asleep`, `can_busy`,
`rate_limited`, or `restoration_failed`.

## Safety contract

Passive reads:

1. take the shared `can0` observer lock;
2. require the interface to already be UP, listen-only, ERROR-ACTIVE, and at
   125 or 500 kbit/s;
3. classify the bus from observed traffic;
4. read only the allowlisted broadcast frame.

The broker never brings up or reconfigures an interface for a passive read.
CAN-CH/grey is detected and rejected as a battery source.

`wake_if_asleep` first performs the same passive path. Only a silent bus can
proceed. It then takes the exclusive diagnostics lock and rechecks the interface
and silence. Wake additionally requires a usable same-boot C-CAN or B-CAN
topology record, matching bitrate, and no active operation inhibit. CAN-CH is
always forbidden. The existing wake helpers restore listen-only mode; the
acquirer also compares the complete pre/post interface snapshots and reports
`restoration_failed` if exact restoration cannot be proven.

Because a silent C-CAN branch cannot be passively distinguished from a silent
CAN-CH branch at the same bitrate, every physical adapter or routing change
must invalidate/update the topology record through
`tools/can_operation_state.py`. A same-boot record is a necessary gate, not
permission to ignore a cable change.

Wake requests are coalesced, serialized by the channel lock, and limited to one
attempt per metric every 15 minutes by default. The passive collector never
requests wake.

The Unix HTTP transport itself is serialized so active CAN cleanup remains on
the process main thread, where the existing termination-signal guard is valid.
Consequently a cache GET can wait behind one bounded active acquisition, but it
cannot interrupt it or create concurrent CAN work. The unprivileged web process
is threaded independently.

## Local API

The default Unix socket is `/run/van-telemetry/api.sock`.

```text
GET  /v1/status
GET  /v1/metrics
GET  /v1/metrics/battery.voltage
POST /v1/acquisitions/battery.voltage
     {"mode":"passive"}
     {"mode":"wake_if_asleep"}
```

GETs are cache-only. There is no raw frame, arbitrary DID, diagnostic session,
DTC, reset, calibration, configuration, or PROXI endpoint.

Examples:

```bash
python3 projects/vehicle_data/client.py status
python3 projects/vehicle_data/client.py get battery.voltage
python3 projects/vehicle_data/client.py acquire battery.voltage --mode passive
python3 projects/vehicle_data/client.py acquire battery.voltage \
  --mode wake_if_asleep --confirm-wake
```

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

## Existing voltage monitor

`projects/battery/voltage_mon.py` uses the broker when its Unix socket exists.
If the socket exists but is invalid or unreachable, the monitor fails closed
instead of bypassing the intended owner. Before the service is deployed it uses
the same in-process `VoltageAcquirer`, so its scheduler and notification path
continue to work while acquisition safety still has one implementation.

## Validation

Offline tests use fake interfaces, locks, sources, and clocks. They cover
cache-only GETs, allowlist enforcement, source metadata, passive acquisition,
silent-bus wake gates, coalescing/rate limits, post-wake restoration failure,
Unix API behavior, and cache-only web defaults.

No tracked service is installed or enabled by this project, and offline
validation is not evidence of a successful live CAN acquisition.
