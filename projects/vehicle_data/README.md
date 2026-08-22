# Vehicle telemetry broker

This project exposes a small, approved metric vocabulary without exposing raw
CAN, arbitrary DIDs, or configuration functions. It accepts a few deliberately
named trip-logger observations, including four raw cluster values whose exact
DIDs, byte widths, sources, and candidate quality are fixed in the registry.

The implementation has two trust zones:

- `broker.py` owns CAN access and serves a Unix-domain HTTP API. Its GET
  endpoints only read cache. An allowlisted acquisition POST can invoke only
  the serial-role-aware battery reader, whose installed runtime is receive-only;
  a separate strict observation POST may only populate
  an exact metric/source tuple already approved for a local logger. While the
  engine is proven running, the broker may supervise `active_drive.py`, a
  termination-safe exclusive C-CAN owner described below.
- `web.py` has no CAN imports and proxies cache/status over HTTP. It defaults
  to loopback and requires `--allow-remote-bind` for any other address. It
  rejects all acquisition requests unless deliberately started with
  `--allow-acquisitions`; neither the tracked nor live systemd service enables
  that flag.
- `drive_recorder.py` is a receive-only companion to the broker-owned active
  interval. It opens no diagnostic transport, takes no channel lock, never
  configures the resolved C-CAN interface, and never transmits. It records only
  after the broker's status proves the reviewed active-drive helper owns the
  armed interface, then binds `candump` to that reported channel and rechecks
  the immutable USB identity when dual-adapter identity metadata is present.

The code does not install or enable itself. The units under `systemd/` retain
safe loopback defaults and must be reviewed against the target host. The
current vanpi deployment is recorded below.

## Dual-USBCANFD transition and roadmap status

The 2026-08-20 implementation completed roadmap items 2–7 for the installed two
dual-channel adapters, and the role-aware broker was deployed and passively
live-validated on 2026-08-21. Item 1 (USB hub/power-path changes) remains
deliberately tabled; this work neither diagnosed nor modified the hub.

1. **Hub/power work — tabled.** No USB reset, power-cycle, topology change, or
   service workaround is included here.
2. **Stable interface roles — implemented.** `lib/vehicle_can_roles.py` defines
   the exact USB serial plus `dev_id` map for C-CAN, B-CAN, CAN CH, and the
   unused spare. `lib/can_role_resolver.py` resolves it, while
   `can_interfaces.py` grants broker-side passive leases. Missing or duplicate
   identities fail closed; no code treats a saved `canN` as a physical identity.
3. **Three-bus passive foundation — implemented.** The broker's
   `dual-usbcanfd` mode reconciles the three vehicle roles to classical CAN,
   their fixed rates, FD off, listen-only, ERROR-ACTIVE, and restart-ms zero.
   The spare stays down. Reconciliation changes SocketCAN link state only and
   sends no CAN frame. Existing decoded telemetry is still primarily C-CAN;
   this foundation does not invent B-CAN or CAN-CH signal decodes.
4. **Historian — implemented.** A separate five-second recorder stores curated
   broker snapshots in SQLite even while the main collector is waiting for a
   long broker-owned active-drive interval. It preserves exact source,
   quality, provenance, freshness, trip/regime, and explicit metric/interface
   gaps; missing observations are never stored as zero. Raw numeric and
   interface samples default to seven-day retention. Bounded daily maintenance
   first advances minute rollups and refuses to prune if that rollup cursor is
   behind; trips, gap records, catalogs, and compact rollups remain available.
   The five-second writer checks this daily-gated maintenance no more than once
   per hour, and reports a maintenance failure without losing an already
   committed snapshot.
5. **History dashboard — implemented.** Trips, current-versus-prior summaries,
   bounded 7/30-day aggregates, and at most 96 downsampled 24-hour points per
   selected metric are fetched separately from the one-hertz SSE stream.
6. **Early warning — implemented, training required.** Explainable rules cover
   regime-matched oil-pressure decline, coolant/transmission-temperature rise,
   voltage decline (with generator-duty corroboration when available), and
   per-wheel TPMS decline. They require persistence plus 30 comparable minute
   buckets from at least three prior trips. Output includes median/MAD,
   threshold, regime, provenance, persistence, and corroborators; there is no
   opaque score or claim of diagnosis.
7. **DTC history/dashboard — implemented as an offline, parked workflow.**
   Strict UDS `19` parsing, SQLite recurrence/status history, saved-report
   import, and a compact cache cover all 16 registry modules without treating
   timeout/unavailable as “no codes.” The dashboard has a cache-only GET and
   deliberately has no scan or clear POST. `tools/dtc_scan.py` defaults to an
   offline sequential `19 02 FF` plan and report import; it never opens a CAN
   socket. Guarded one-module live reads remain in `tools/dtc_inventory.py`,
   which now supports `--resolve-runtime` and holds both the logical-role and
   resolved-channel locks around its existing parked execution gates.

The tracked `dual-usbcanfd` systemd unit and
`/var/lib/van-telemetry` SQLite state directory are deployed. Passive
commissioning and the current enabled service state are recorded below;
changing a tracked unit still does not by itself alter the installed copy.

## Metric and quality

`battery.voltage` chooses an approved broadcast reader after passive bus
classification:

| Source | Bus | Quality | Notes |
|---|---|---|---|
| `bcan.broadcast.0x46c` | B-CAN, 125 kbit/s | `verified` | low 13-bit word / 400 |
| `ccan.broadcast.0x41a` | C-CAN, 500 kbit/s | `verified` | byte0 x 0.05 V + 4.0 V; readable while the parked branch is awake |
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
| `generator.field_duty` | `pcm.did.01a1` | number, `%` | `observed_alfa_scale` |
| `engine.crankshaft_torque` | `pcm.did.06da` | number, `lb-ft` | `observed_alfa_scale` |
| `engine.oil_pressure` | `ccan.broadcast.0x41d` | number, `psi` | `observed_alfa_scale` |
| `engine.coolant_temperature` | `ccan.broadcast.0x2ed` | number, `°F` | `observed_alfa_scale` |
| `engine.rpm` | `ccan.broadcast.0x0fc` | number, `rpm` | `observed_alfa_scale` |
| `engine.target_crankshaft_torque` | `ccan.broadcast.0x100` | number, `lb-ft` | `observed_alfa_scale` |
| `transmission.output_speed` | `ccan.broadcast.0x1f7` | number, `rpm` | `observed_alfa_scale` |
| `transmission.oil_temperature` | `ccan.broadcast.0x1f7` | number, `°F` | `observed_alfa_scale` |
| `transmission.turbine_speed` | `ccan.broadcast.0x1f7` | number, `rpm` | `observed_alfa_scale` |
| `vehicle.ignition_on` | `ccan.broadcast.0x2ef` | boolean, `boolean` | `verified` |
| `vehicle.speed` | `ccan.broadcast.0x101` | number, `mph` | `observed_alfa_scale` |
| `tire.pressure.fl` | `rf_hub.did.31d0` | number, `psi` | `verified` |
| `tire.pressure.fr` | `rf_hub.did.31d1` | number, `psi` | `verified` |
| `tire.pressure.rr` | `rf_hub.did.31d2` | number, `psi` | `verified` |
| `tire.pressure.rl` | `rf_hub.did.31d3` | number, `psi` | `verified` |
| `diagnostics.cluster.did.1000.raw` | `cluster.did.1000` | integer, `raw_u16_be` | `candidate` |
| `diagnostics.cluster.did.1002.raw` | `cluster.did.1002` | integer, `raw_u8` | `candidate` |
| `diagnostics.cluster.did.0107.raw` | `cluster.did.0107` | integer, `raw_u8` | `candidate` |
| `diagnostics.cluster.did.1005.raw` | `cluster.did.1005` | integer, `raw_u8` | `candidate` |

`vehicle.ignition_on` is a positive-presence witness: a received `0x2EF`
frame may publish only `true`. A publisher-supplied `false` is rejected
because silence is not a decoded negative value; the observation instead
expires to stale/unknown when the frame disappears.

The four TPMS metrics use the wheel map and `raw x 0.1 kPa` pressure scale
verified by the TPMS project's 2026-07-07 deflate/reinflate test. RF Hub slots
1–4 remain FL, FR, RR, RL; in particular, slots 3/4 must not be swapped. The
TPMS logger converts valid values to psi and publishes them over the local Unix
API. Raw `FFFF` means invalid/no sensor data and is never published; an earlier
cached value instead expires after the 30-second freshness window. These are
active physical UDS reads, not passive broadcast metrics. See
[`projects/tpms/README.md`](../tpms/README.md) for sensor IDs, evidence, and the
service/recorder contention boundary.

`generator.field_duty` is the PCM's generator field-command duty, decoded as
`u16be x 100 / 32768`. It is not alternator current, alternator load, or
alternator temperature. The observed scale is not clamped: exact-vehicle data
reached approximately 100.008%, and the public range deliberately permits that
small overshoot. The metric expires after four seconds. Sustained high duty can
mean high commanded charging effort, but duty alone does not establish a
thermal-danger threshold. The routinely used house-battery DC-DC charger is a
normal substantial alternator load and may drive this metric high. No default
warning uses generator duty as its primary signal; it is only optional,
explanatory corroboration for a separately persistent low-voltage deviation.

`engine.crankshaft_torque` is the PCM's diagnostic current-engine-torque
value from DID `06DA`, decoded as signed `i16be x 0.04 Nm` and converted to
lb-ft for presentation. The exact sign and scale were observed across
positive load and negative overrun in the synchronized 2026-07-27 drive. It
is an ECU-reported crankshaft estimate, not wheel torque or a dynamometer
measurement. It is polled only inside the same qualified engine-running
interval as generator field duty and expires after four seconds.
If the optional torque request fails, the helper reports that metric-specific
reason and disables only torque for the remainder of that engine-running
epoch. Generator duty, TPMS, passive powertrain telemetry, and broker-owned
raw recording continue. Safety-gate, lock, topology, adapter, and restoration
failures still stop the complete armed interval.

The raw rows preserve the original cluster evidence without presenting
unverified speed, gear, or temperature conversions as facts. The separately
qualified passive `engine.rpm` metric supersedes the need to interpret raw
cluster `1000` in the dashboard. These remain diagnostics metrics, not a
general-purpose DID publication namespace.

## Owner-priority telemetry roadmap

The first engine-health additions are oil pressure, coolant temperature,
engine-oil temperature, transmission-oil temperature, RPM, actual crankshaft
torque, and derived power.
Oil pressure is a particularly strong target: the exact-vehicle OEM material
confirms a scan-tool-readable EOP sensor and dual-stage pump, while the
current-vehicle Alfa configuration literally reports `Oil Pressure ABS: Yes`
and `Oil Pressure Sensor: Enabled`. Those vendor labels are useful navigation
evidence, not an independent decode of `ABS` or a DID/scale. Expected pressure
bands differ by RPM, operating temperature, and pump mode, so the display
cannot use one static good/bad threshold. Power must be labeled as
ECU-estimated crankshaft power, not wheel horsepower.

The 2026-07-26 simultaneous PCM Plots/wire campaign qualified the first two
receive-only sources. The 2026-07-27 PCM loaded drive qualified passive engine
speed, and the later TCM loaded drive qualified road speed, both transmission
shaft speeds, and explicitly labeled TCM target crankshaft torque:

| Metric | Passive C-CAN source | Decode | Quality |
|---|---|---|---|
| `engine.oil_pressure` | `0x41D` byte 2 | native raw x 4 kPa, published as psi | `observed_alfa_scale` |
| `engine.coolant_temperature` | `0x2ED` byte 0 | native raw - 40 °C, published as °F | `observed_alfa_scale` |
| `engine.rpm` | `0x0FC` bytes 0–1 u16be | low 2 bits masked, raw / 4 rpm | `observed_alfa_scale` |
| `vehicle.speed` | `0x101` packed 12-bit field | raw / 16 km/h, published as mph | `observed_alfa_scale` |
| `transmission.output_speed` | `0x1F7` byte0 bit0 then bytes 1–2 | packed 17-bit raw / 32 rpm | `observed_alfa_scale` |
| `transmission.oil_temperature` | `0x1F7` byte 3 signed i8 | native raw × 0.375 + 57 °C, published as °F | `observed_alfa_scale` |
| `transmission.turbine_speed` | `0x1F7` bytes 4–5 u16be | raw / 2 rpm | `observed_alfa_scale` |
| `engine.target_crankshaft_torque` | `0x100` bytes 3–4 upper 11 bits | raw - 500 Nm, published as lb-ft | `observed_alfa_scale` |

All are in the public registry and the passive collector reads them only
after its normal C-CAN interface and identity gates pass. They require no
per-reading approval and send no CAN traffic. The exact current-vehicle
correlation is recorded in the
[`PCM Plots idle finding`](../ecu_mapping/findings/promaster_2022/2026-07-26_pcm_plots_idle_mapping.md)
and
[`PCM loaded-drive finding`](../ecu_mapping/findings/promaster_2022/2026-07-27_pcm_plots_loaded_drive_mapping.md),
plus the
[`TCM loaded-drive finding`](../ecu_mapping/findings/promaster_2022/2026-07-27_tcm_plots_loaded_drive_mapping.md).
Transmission-oil temperature is additionally qualified by the independent
cold-start and predeclared hot-soak discrimination sequence in the
[`TCM oil-temperature mapping`](../ecu_mapping/findings/promaster_2022/2026-07-29_tcm_oil_temperature_candidate.md).

The collector defaults to a one-second pause between passive cycles. The
powertrain scalars and ignition presence witness expire after five seconds,
which covers the bounded bus-classification plus snapshot cycle without
dashboard flicker while still failing stale promptly after traffic stops.
Generator field duty and current crankshaft torque are each polled at
approximately one hertz during a qualified running interval and expire after
four seconds.

### Presentation units

User-facing telemetry defaults to US customary units: pressure in psi,
temperature in °F, road speed in mph, and torque in lb-ft. Native CAN/ECU
decodes remain documented in their original kPa, °C, km/h, and Nm units so
the evidence and conversions stay reproducible. When torque is promoted, its
qualified native Nm value must be multiplied by `0.737562149` before
publication as lb-ft. Raw diagnostic metrics remain raw and are never
unit-converted.

Engine-oil temperature, passive **actual** loaded torque, and derived power
are not yet qualified. Diagnostic current crankshaft torque is now available
from guarded PCM DID `06DA`; transmission-oil temperature is available from
the receive-only source above. The available
`engine.target_crankshaft_torque` metric is a TCM command target and is
deliberately excluded from the dashboard's actual-torque and power roles.
Passive RPM is available; `0x100 u13be@9` is now a strong replicated passive
current-torque candidate, but it remains unpromoted pending a frozen proxy or
identity gate and therefore cannot yet feed a receive-only power calculation.
The evidence, exact OEM
pressure/thermostat context, alert-design constraints, PCM/TCM acquisition
sequence, and later mechanical and electrical targets are maintained in the
[`priority telemetry finding`](../ecu_mapping/findings/promaster_2022/2026-07-25_priority_telemetry_targets.md).
The dashboard keeps roadmap cards visible for oil pressure, coolant
temperature, engine-oil temperature, crankshaft torque, and crankshaft power.
Oil pressure, coolant, RPM, and guarded diagnostic crankshaft torque now
receive fresh observations. The remaining roadmap labels do not create
metrics or imply that a source is available. Context-aware oil-pressure bands
and fresh time-aligned torque/RPM power derivation still require specialized
evaluation and presentation logic. Passive RPM sends no diagnostic traffic.

The oil-pressure card does provide **advisory OEM context**, not an alert. When
fresh coolant and RPM are available it selects a published warm-engine
reference: 15–34 psi at approximately 650 rpm, 28–35 psi from 1,000–3,000 rpm,
or 65–80 psi above 3,500 rpm, and only at 192–212 °F coolant. Because the OEM
idle row names a point rather than a range, the UI uses 550–850 rpm only as a
clearly labeled nearest-reference context window; it is not presented as an
OEM test band. The card explicitly reports that the 3,000–3,500 rpm transition
has no published band. It does not color or classify the live pressure as
safe/unsafe and does not implement the approximately-12-psi critical rule,
because that rule still needs a verified running/startup-grace evaluator.

## Dashboard profiles and vehicle state

Dashboard values are registry-driven even where the layout keeps a future
metric role visible. `GET /v1/snapshot` returns the public metric catalog,
every metric's cache-only response, broker/interface status, and an
evidence-qualified `vehicle_state` object in one request. A future registered
metric therefore becomes available to the generic metric and catalog panels
without adding another web proxy route or SSE request.

Built-in dashboard profiles are **Overview**, **Parked**, **Driving**, and
**Diagnostics**. **Overview is the stable default.** The user can select one
manually, opt into **Automatic**, or choose exactly which panels appear in a
**Custom** profile. The selection and custom panel list use browser
`localStorage`; they are per-device preferences and never write broker
configuration or touch CAN. Browsers carrying the former default Automatic
selection are migrated to Overview; explicit manual and Custom selections are
preserved.

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
activity can be ignition-on or a key-fob/module wake. Current
passive acquisition can report `awake`, inferred `asleep`, or `unknown`, with
`running: null` whenever the evidence cannot distinguish those cases. This
keeps the automatic layout engine ready for a separately verified
ignition/motion metric without silently promoting a voltage heuristic.

## Dashboard freshness timing

A synchronized 120-second passive trace on 2026-07-30 separated a recurring
whole-dashboard blank from CAN and adapter failures. The raw, gitignored
capture is under
`tmp/vehicle_data/dropout_timing_20260730T003727/`.

- C-CAN `0x2EF` delivered 2,400 frames at a median 50.003 ms interval; the
  maximum gap was 51.499 ms and there were no gaps over 100 ms.
- All 25 interface samples remained 500 kbit/s, listen-only, ERROR-ACTIVE,
  with zero TX/RX bus-error counters and zero RX errors. No matching PCAN,
  USB, undervoltage, reset, disconnect, or EXT4 event appeared in the capture
  window.
- The passive collector refreshed the powertrain set every 3.491–3.601
  seconds. The former default SSE interval was 2.005–2.019 seconds. Metric age
  at SSE generation reached 3.524 seconds.
- Ten of 61 consecutive SSE intervals therefore carried the last powertrain
  observation beyond its five-second registry freshness limit before the next
  event. The calculated overrun reached 531 ms. The browser's one-second
  freshness tick can make that short overrun visible as a whole-panel blank.
- The broker Unix-socket snapshot stayed responsive (14.8 ms maximum). LAN
  snapshot requests had no failures and reached 1.148 seconds maximum, below
  the browser's separate two-second HTTP-response bound. The one-minute load
  average was 1.05–1.90 during this trace, although swap remained full.

The default SSE interval is now one second. This keeps the five-second metric
expiry unchanged while putting the measured 3.524-second worst-phase delivery
below the expiry boundary before the following stream event. A regression
test preserves that measured phase relationship.

An earlier Chromium reproduction under heavy Pi contention exceeded the
two-second HTTP baseline bound and displayed `Broker unavailable`. That is a
separate fail-closed path, not evidence of a CAN gap. It did not recur in the
normal-load synchronized trace, so the HTTP bound has not been relaxed.

## Safety contract

Passive reads:

1. resolve the requested logical role from exact USB
   serial plus `dev_id`, then take both its shared role lock and the shared lock
   for that currently resolved `canN`;
2. re-resolve after taking the locks and require the same physical identity;
3. require the interface to already be UP, classical CAN with FD off,
   listen-only, ERROR-ACTIVE, restart-ms zero, and at the fixed rate for that
   role;
4. read only the allowlisted broadcast frame. No broker path treats a saved
   `canN` as physical identity.

The C-CAN powertrain socket's kernel filters constrain standard identifiers
and reject extended/RTR forms, but deliberately leave `CAN_ERR_FLAG` out of
the normal-frame mask. Linux gives that bit special receive-filter semantics:
including it suppresses ordinary data frames. Received frames are still
explicitly rejected in userspace if any extended, RTR, or error flag is set.

An ordinary passive read never brings up or reconfigures an interface. The
serial-aware reconciler is the broker's only passive link configurator: it
applies each role's fixed classical-CAN bitrate and never guesses or switches a
bus based on traffic. The former single-channel auto-retune helper and its
status/UI contract are retired. CAN-CH/grey is resolved and reported as its own
simultaneous role but rejected as a battery source.

### Coordinated engine-running diagnostic interval

The PCM, RF Hub, and existing powertrain broadcasts share the physical C-CAN
leg, so independent long-running pollers still cannot arm that one resolved
channel without excluding its listen-only collector. The broker uses one
coordinated active-drive helper for the complete engine-running epoch; B-CAN,
CAN CH, and the spare remain separate roles:

1. the normal listen-only collector first observes RPM from qualified C-CAN
   `0x0FC`;
2. the helper takes the exclusive logical C-CAN and currently resolved-channel
   locks, requires the expected Board A USB serial and `dev_id=0`, same-boot
   C-CAN topology on DLC pins 6/14, no operation inhibit, a healthy classical
   CAN/FD-off, restart-ms-zero, listen-only 500-kbit/s interface, passive C-CAN
   identity, and at least three fresh samples at or above 400 rpm;
3. it arms the adapter once, repeats the RPM gate, and remains the sole
   SocketCAN owner until RPM becomes zero/sub-threshold/stale or another stop
   condition appears;
4. while armed it continues receiving and publishing the existing allowlisted
   C-CAN powertrain/battery broadcasts, polls generator field duty and PCM
   current crankshaft torque at about one hertz each, and round-robins the
   four existing RF Hub pressure reads. Every individual send consumes a new
   purpose-bound permit issued from the held exclusive lock and that cycle's
   qualified RPM snapshot;
5. it closes every socket, restores and verifies the exact prior listen-only
   configuration, and only then releases the lock.

This is an intentionally honest armed interval. Observations retain their
source acquisition class and also report
`interface_mode=armed_diagnostic`; status reports
`current_owner.kind=broker_active_drive`. The interface is not described as
passive while it is armed. The design trades one down/up transition at the
start and end of a running epoch for continuous powertrain, generator, battery,
and TPMS telemetry; it does not cycle SocketCAN once per poll.

The closed PCM engine-running registry contains exactly two immutable
profiles. Its only possible PCM transmissions are these physical, 29-bit,
fixed-DLC-8 frames:

```text
18DA10F1#032201A100000000
18DA10F1#032206DA00000000
```

Each response must be a single frame from `18DAF110` with the corresponding
exact `62 01 A1` or `62 06 DA` echo and exactly two data bytes. A raw CAN
socket is used so a malformed multi-frame reply cannot cause an ISO-TP
FlowControl transmission.
There is no caller-selectable DID, service, CAN ID, payload, functional
address, session, or tester-present option. In particular, this path contains
no `10 92` or `3E` traffic. Future PCM metrics require a reviewed code change
adding another fixed profile and exact decoder; candidate names do not create
a diagnostic proxy.

The coordinated helper also preserves the already reviewed TPMS pressure path.
Those are the only other CAN frames it can construct:

```text
18DAC7F1#032231D000000000
18DAC7F1#032231D100000000
18DAC7F1#032231D200000000
18DAC7F1#032231D300000000
```

All six are physical `ReadDataByIdentifier` requests to endpoints registered
in `lib/modules.py`; no functional broadcast request exists. The helper stops
on loss of RPM or C-CAN traffic, topology/inhibit changes, adapter health
failure, timeout, malformed response, rejection, termination, or exception.
`session_required` is reported distinctly and does not enable a session
change. A failed or unverified restoration sets a persistent operation inhibit,
is latched in the broker, and prevents another active interval.

The newly added torque read is optional within an otherwise healthy interval:
its first response failure is published as a metric-specific acquisition
failure and suppresses further `06DA` requests until the next engine-running
epoch. It does not stop generator duty, TPMS, passive broadcast publication,
or the receive-only drive recorder. This narrower behavior does not relax any
interface, topology, RPM, permit, or restoration gate.

The transport methods cannot send on control-flow convention alone. Their
opaque permit is fixed to the currently serial-resolved C-CAN `canN` and its
live exclusive diagnostic-lock handle, one of the three reviewed transport
purposes, the issuing process, and
the latest snapshot's positive frame count plus three finite RPM samples at or
above 400. It expires after 250 ms and is consumed by its first attempted use,
including a wrong-purpose, stale, released-lock, or failed-send attempt. There
is no zero-argument generator-duty sender.

The direct-read evidence includes positive padded `22 01A1` reads without an
explicit session change, and the synchronized Alfa/PCAN drive repeatedly
observed padded `22 06DA`, so the implementation starts with no session
traffic. The standalone `01A1` capture did not positively identify the
inherited session. A clean post-ignition experiment is still useful if the
research goal is to prove the minimal/default session label; it is not
required to add `10 92`, and an NRC requiring another session must remain a
reported blocker until a separate design and owner authorization exist.

### Broker-coordinated raw drive recording

`drive_recorder.py` preserves full-bus evidence while the broker owns the
serial-resolved C-CAN channel. The normal observer lock and the standalone
`passive_drive_capture.py` entry point intentionally reject an armed interface;
this narrower companion instead requires one exact broker status:

- active drive enabled and `armed_diagnostic`, with an integer helper PID and
  `current_owner.kind=broker_active_drive`;
- qualified `0x0FC` engine-running state, usable same-boot C-CAN pins 6/14
  topology, no operation inhibit, and the broker-reported healthy 500-kbit/s
  ERROR-ACTIVE `canN`, rechecked against the expected USB serial and `dev_id`;
- an initial `0x2EF` frame within five seconds of opening the receive socket.

It then reuses the loss-accounted recorder with `candump -D`, a 16 MiB socket
receive buffer, ten-minute zstd chunks, full and priority streams, and the
existing 30/25 GiB free-space floors. `-D` keeps the receive process attached
across the broker's expected end-of-interval interface down/up restoration.
The recorder accepts an armed interface only while the exact broker owner is
present, but may continue after verified listen-only restoration so the raw
session includes the key-off tail. Twenty seconds without `0x2EF` cleanly ends
and verifies the campaign.

The daemon loops after a successful finalization: while parked it opens no CAN
socket and waits for the next broker-owned running interval, then creates a new
timestamped campaign automatically. Consequently the installed service does
not need manual re-arming between ordinary drive legs when enabled. Its
installed unit currently remains disabled/inactive. Output is under
`/mnt/EXFAT512/obd-things/tmp/captures/ccan/broker-drive/`, and the small
operational state is `tmp/vehicle_data/drive-recorder-state.json`.

The deployed role-aware battery acquirer supports passive mode only. The former
wake-acquisition mode and its single-adapter implementation have been removed;
there is no broker bitrate-switch path. The reconciler records each resolved
role's topology after verified passive setup, while active C-CAN work still
requires the same-boot role, physical pair, USB identity, and current interface
health gates.

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
GET  /v1/history
GET  /v1/health
GET  /v1/diagnostics/dtcs
POST /v1/acquisitions/battery.voltage
     {"mode":"passive"}
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

There is no raw-frame, arbitrary-DID, diagnostic-session, live-DTC-scan,
DTC-clear, reset, calibration, configuration, or PROXI endpoint. The DTC GET
reads only the atomic JSON cache written by an offline/local import workflow.
It cannot open SocketCAN. History and health likewise read only SQLite.

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

History, early-warning, and saved DTC payloads are intentionally absent from
the one-hertz snapshot/SSE response. The browser fetches their three dedicated
GET endpoints on page synchronization and then no more often than once per
minute. This prevents the compact but substantially larger diagnostic cache
from being duplicated into every live telemetry event.

The current offline DTC cache was seeded from the 19 existing inventory
reports. It contains dated evidence, not a current scan: 11 modules have a
successful saved result, PCM is explicitly unavailable after its generic
request timeout, and the four CAN-CH modules are explicitly never scanned by
the repository reader. Status `0x40` means test-not-completed-only and is shown
separately rather than counted as a fault. Generate or inspect an offline plan
without CAN access using:

```bash
python3 tools/dtc_scan.py --resolve-runtime
```

`--resolve-runtime` reads sysfs identities only. Completed
`tools/dtc_inventory.py` JSON reports can be previewed with repeated
`--import-report`; adding `--commit` updates the SQLite history and atomic
dashboard cache without any CAN I/O. There is intentionally no `--execute` in
this multi-module planning/import tool. Runtime route annotation is per bus: a
missing role is marked unresolved without suppressing plan rows for independently
resolved roles. The one-module reader is also dry-run by default:

```bash
python3 tools/dtc_inventory.py <module-key>
```

Its gated live form is a tool-owned scoped operation, not a separate arming
step:

```bash
python3 tools/dtc_inventory.py <module-key> \
  --execute --confirm-parked --pair <documented-pair> \
  --conditions "parked; ignition ON; engine OFF"
```

After the confirmations, it resolves the module's exact role, acquires the
role/channel locks, checks exact-role contention, host privilege, and same-boot
inhibits, requires the exact passive classical-CAN baseline, arms for only its
fixed service-`19` set, and
restores before returning. It never sends `14` clear or a session change; a
failed restore latches a wildcard same-boot inhibit. Do not pre-arm a netdev or
substitute a remembered `canN`. This live path is intentionally not callable
from the web UI.

Examples:

```bash
python3 projects/vehicle_data/client.py status
python3 projects/vehicle_data/client.py get battery.voltage
python3 projects/vehicle_data/client.py acquire battery.voltage --mode passive
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

Deployment update, 2026-08-21: the tracked role-aware
`van-telemetry.service` is installed, enabled from `multi-user.target`, and
active. Passive commissioning resolved C-CAN as `can0` at 500 kbit/s, B-CAN as
`can1` at 125 kbit/s, and CAN CH as `can2` at 500 kbit/s. All three read back as
classical CAN, FD off, listen-only, `restart-ms 0`, and ERROR-ACTIVE; the
unconnected spare was `can3` and remained down. TX and error counters were
zero. Those netdev names are the observed commissioning snapshot only and must
still be re-resolved from serial plus `dev_id` after any USB/hub change.

The cache-only telemetry web path is active over the configured LAN/Tailscale
access, `/api/telemetry-summary` reports the service running, and the SQLite
historian is writing. The temporary commissioning override was removed, so the
normal service has active-drive enabled; because the vehicle remained asleep,
the helper stayed idle and TX remained zero. This run validates the passive
reconciler, web path, and historian, not an active diagnostic/polling interval.

Stationary active-drive commissioning completed later on 2026-08-21. The
engine-running interval remained armed for 146.66 seconds and added 438 C-CAN
TX packets (2.99/s), matching the fixed two-PCM-plus-one-rotating-TPMS
scheduler. Twenty-nine consecutive historian snapshots each retained fresh
generator duty, PCM current torque, all four TPMS pressures, RPM, oil pressure,
coolant temperature, transmission-oil temperature, and zero vehicle speed.
Generator duty ranged 26.782–91.168%; the high initial value coincided with the
owner's routine house-battery DC-DC charger being enabled, and duty fell after
the owner switched that load off a few seconds into the run. This was an
intentional normal-load transition, not warning evidence. RPM ranged
748–1,506 and oil pressure 30.168–34.809 psi; the four pressure reads were
58.1/56.1/76.8/77.2 psi (FL/FR/RR/RL). B-CAN and CAN CH remained independently
listen-only and healthy with zero TX throughout. After engine-off, the helper
and exclusive owner cleared, C-CAN returned to classical 500 kbit/s, FD off,
listen-only, `restart-ms 0`, ERROR-ACTIVE, and its TX counter stopped at 438.
No CAN error, drop, bus-off, USB event, service warning, restoration inhibit,
or restart was observed. This validates the deployed dual-USBCANFD active
arm/read/restore path under stationary idle; loaded driving behavior remains a
separate evidence question.

A 2026-08-21 historian repair made nested logical-role status authoritative
during broker startup and stopped treating a pre-reconciliation kernel
`canN` name as a durable interface role. On the first post-restart ingest it
closed the existing false `can0/interface_role_absent` interval without
deleting the two original startup samples or earlier gap history. After two
history cycles, coverage reported `current`, active interface gaps were empty,
SQLite `quick_check` returned `ok`, all three logical roles remained healthy,
and CAN TX/error counters remained zero.

The installed role-aware `van-drive-recorder.service` and
`tpms-logger.service` copies match their tracked units but remain
disabled/inactive and were not live-validated. Replacements for
`promaster-bcan-recorder.service`, `promaster-mapping-drive.service`,
`tpms-drivesniff.service`, and the external `can-three-bus-capture.service` are
also installed and disabled/inactive. The old B-CAN recorder unit/enablement is
retired; none of these campaign services should be inferred to have run during
broker commissioning.

The broker CLI now requires the explicit safety gate
`--can-interface-mode dual-usbcanfd`; there is no legacy choice or `--channel`
fallback. The installed unit now supplies that gate. Any future stale or custom
unit that starts this code without it will fail argument parsing before
interface resolution or link configuration.

Before a future reinstall or manually initiated restart, perform a read-only
role/status check and confirm that all four exact identities resolve once, with the three vehicle
roles on their documented rates and the spare unconnected. Also verify the
`pi` service account's noninteractive sudo policy for the literal resolved
channels: the reconciler needs `ip link set dev <canN> down`, the fixed
classical-CAN `type can bitrate {125000|500000} fd off listen-only on
restart-ms 0` form, and `ip link set dev <canN> up`; the active C-CAN owner also
needs its reviewed listen-only-off arm and exact-state restoration forms.
Use `sudo -n -l -- <literal command>` to inspect authorization without running
the link command. If any role is missing/ambiguous, the physical pair differs,
or a required literal command is not pre-authorized, do not restart: the
service will fail closed/degraded rather than guessing a channel.

The retired `10-can0-passive-baseline.conf` drop-in and
`obd-things-ensure-passive-can0` helper were removed from the live host into a
recoverable timestamped `tmp/` backup during the 2026-08-21 migration. The
effective revised service is enabled from `multi-user.target`, not a
`sys-subsystem-net-devices-can0.device.wants/` path. Preserve that arrangement:
after any future unit change, reload systemd and inspect both the effective unit
and enablement rather than assuming that copying a file changed the running
service.

The separate machine-local `van-dashboard.service` was cleaned on 2026-08-20:
its external `van_dashboard.py` no longer contains the fixed-`can0` raw monitor
or COP ALERT RF-Hub wake. COP ALERT remains a non-CAN exterior-light and
notification feature, with its lights paused by the existing ignition-monitor
marker, but it no longer keeps the dashcam or vehicle network awake. The
dashboard's remaining vehicle-data integration is the cache-only telemetry
HTTP endpoint; it does not open SocketCAN or own a bus role.

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
[`broker-drive poll validation`](../ecu_mapping/findings/promaster_2022/2026-08-04_broker_drive_poll_validation.md).

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
- The live web service omits `--allow-acquisitions`. Dashboard GETs and stream
  updates are cache-only, and acquisition POSTs fail closed with HTTP 403.

### Current read-only host inspection

Inspect the effective unit rather than assuming the tracked example matches the
host:

```bash
systemctl is-enabled van-telemetry.service van-telemetry-web.service \
  van-telemetry-web-tailscale.service van-drive-recorder.service
systemctl is-active van-telemetry.service van-telemetry-web.service \
  van-telemetry-web-tailscale.service van-drive-recorder.service
systemctl cat van-telemetry.service
systemctl cat van-telemetry-web.service
systemctl cat van-telemetry-web-tailscale.service
find /etc/systemd/system -maxdepth 3 -type l \
  -lname '*van-telemetry.service' -print
cat tmp/vehicle_data/drive-recorder-state.json
ss -lntp | /usr/bin/grep ':8765'
curl --fail http://vanpi.lan:8765/v1/status
curl --fail http://<tailscale-ip>:8765/v1/status
```

If the owner authorizes a selected LAN-address change, update the machine-local
drop-in, run `systemctl daemon-reload`, and restart only
`van-telemetry-web.service`. Do not
replace the explicit address with a wildcard merely to avoid maintaining the
override.

## Existing voltage monitor

`projects/battery/voltage_mon.py` uses the broker when its Unix socket exists.
If the socket exists but is invalid or unreachable, the monitor fails closed
instead of bypassing the intended owner. Read-only `crontab -l` on 2026-08-20
showed `projects/battery/voltage_mon.sh` still scheduled every two hours from
10:00 through 22:00; this migration did not alter it. With the broker absent,
the in-process fallback uses the same serial-resolved passive C-CAN role lease.
Both paths hold shared role/channel locks, require the exact passive classical
state, and never reconfigure or transmit. A sleeping bus is an expected
unavailable sample; wake-assisted acquisition is removed from the scheduled
multi-role path.

## Validation

Offline tests use fake interfaces, locks, sources, and clocks. They cover
cache-only GETs, strict local publication, typed values, source-metadata
allowlists, broker-stamped age, passive acquisition, silent-bus handling,
coalescing and rate limits,
Unix API behavior, the registry-driven snapshot, evidence-qualified vehicle
state, dashboard profile assets, cache-only web defaults, exact broker-owned
recorder admission, initial-ignition timeout, and persistent-candump command
construction.

The role-aware broker is deployed and its passive three-bus reconciliation was
live-CAN validated on 2026-08-21. The vehicle was asleep, so active-drive and
the disabled companion services were not exercised. Service health and passive
validation are not evidence of a successful active acquisition; inspect metric
provenance and quality on every available observation.
