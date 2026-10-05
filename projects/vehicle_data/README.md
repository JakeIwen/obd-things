# Vehicle telemetry broker

This project exposes a small, approved metric vocabulary without exposing raw
CAN, arbitrary DIDs, or configuration functions. It accepts a few deliberately
named trip-logger observations, including four raw cluster values whose exact
DIDs, byte widths, sources, and candidate quality are fixed in the registry.

The implementation has two trust zones:

- `broker.py` owns CAN access and serves a Unix-domain HTTP API. Its GET
  endpoints only read cache. An allowlisted acquisition POST can invoke only
  the serial-role-aware battery reader: normal collection is receive-only,
  while the local-only `wake_if_asleep` mode may use the fixed B-CAN wake
  profile under the parked-state and cooldown gates described below;
  a separate strict observation POST may only populate
  an exact metric/source tuple already approved for a local logger. While the
  engine is proven running, the broker may supervise `active_drive.py`, a
  termination-safe exclusive C-CAN owner described below.
- `web.py` has no CAN imports and proxies cache/status over HTTP as the `/v1`
  JSON API; it serves no page. `web_v2.py` subclasses it, adds the `/v2`
  routes and serves the one telemetry dashboard (`dashboard/dist`) on port
  8765; both web units run `web_v2.py` (see "Telemetry dashboard" below). The
  listener defaults to loopback and requires `--allow-remote-bind` for any
  other address. It permits the fixed passive voltage-read POST by default;
  `--cache-only` disables that button/POST, and `--allow-acquisitions` remains
  a compatible explicit enable flag. GETs/streams never acquire data. Wake
  requests remain rejected by the web proxy.
- `drive_recorder.py` is a synchronized three-bus receive-only companion to the
  broker-owned active interval. It opens no diagnostic transport, never
  configures an interface, and never transmits. It records only after the
  broker proves the reviewed active-drive helper owns armed C-CAN, then binds a
  separate `candump` to that route while holding shared passive role/channel
  leases for freshly resolved B-CAN and CAN-CH. Each bus has its own compressed
  files, immutable USB identity, loss accounting, and required awake witness.
- `cop_can_wake.py` is the separately managed replacement for the dashboard's
  retired direct CAN path. The dashboard only maintains
  `/run/van-dashboard/cop-alert.active`; the supervisor requires that marker
  to remain stable, independently pauses on `/home/pi/hooks/ignition_is_on`,
  and requires fresh broker evidence that active-drive is idle and the engine
  is stopped, or the narrowly defined awake/running-unknown state produced by
  a prior parked poke. It then asks the fixed logical `c-can` wake profile for
  one physical RF Hub `22 FEFF` transaction. Exact serial/`dev_id` role ownership,
  pair/rate/classical-CAN checks, fresh `0x2EF`/`0x0FC` rejection gates,
  operation inhibits, and passive restoration remain inside the shared wake
  core. A successful poke is fully restored before the fixed 15-second wait;
  the dashboard never imports SocketCAN or learns a `canN` name.
  A marker newly created by an explicit button action has only a 250 ms
  debounce. A marker already present when the supervisor process starts keeps
  a separate three-second restart grace. Pre-transmit safety contention retries
  after 500 ms without touching CAN; a wake/validation failure retains the
  conservative five-second retry. Every meaningful state transition is emitted
  as one structured `cop-can-wake-state` journal record.
  Broker passive voltage and powertrain readers pass through a reader gate and
  hold a shared C-CAN scheduling turn outside their normal shared role/channel
  lease. The wake closes that gate, waits at most 1.25 seconds for an in-flight
  reader to finish, and reserves the turn exclusively before taking the
  unchanged exclusive role/channel locks. A new broker observer therefore
  cannot overtake the waiting wake. This handoff is fairness only: it conveys
  no identity, interface-mutation, or transmission capability.

The code does not install or enable itself. The units under `systemd/` retain
safe loopback defaults and must be reviewed against the target host. The
current vanpi deployment is recorded below.

### Owner-configured warning triggers and Codex chat

September 22 live repair: the Early Warning overview now omits repeated baseline
input/sample arrays while retaining decision facts, with explicit omissions and
a 512 KiB response budget. Full evidence remains in event details/exports. This
fixes the observed 1.33 MB response exceeding the unchanged 1 MiB transport limit.
Failed loads no longer incorrectly claim notification delivery is disabled.
Broker/web/advisor are deployed and live-verified (HTTP 200, ~301 KB overview,
full event detail intact, no CAN TX delta). This activation includes the earlier
interface-health and monitoring changes described below. See
[event-history.md](docs/event-history.md) for regression/deployment evidence.

September 21 interface-health follow-up: first-probe initialization no longer
opens three unhealthy-role watches. Failed/delayed discovery is one status
advisory; real role/controller faults remain actionable. Verified active B-CAN
ownership now reports healthy topology while remaining non-passive, guarded by
exact route/identity/configuration checks. Legacy generic titles are clarified
for display without rewriting saved evidence. The event dialog exposes its four
actions in one row and restores the event-count paragraph's side inset. Source
and validation are complete; activated September 22 with the overview-size fix.
See the [implementation and validation record](docs/event-history.md).

September 21 lifecycle follow-up: routine freshness expiry is a collapsed
monitoring note. Interrupted vehicle-health watches that never qualified as a
warning archive as **Unconfirmed · Monitoring ended** after their quiet window
(60 seconds for coolant), retaining evidence and leaving TO REVIEW. Confirmed
warnings remain unresolved during missing data. Sustained missing readings with
independent fresh running RPM are separate, non-notifying telemetry-quality
incidents. See [the event-history contract](docs/event-history.md) for scope,
validation and the September 22 activation of this follow-up.

[Event history and evidence](docs/event-history.md) now implements searchable
saved events, opening/first-warning/checkpoint evidence, preserved baseline
inputs and sample windows, lifecycle recovery gates, exports, owner annotations,
and bounded counterfactual comparisons. The in-app agent receives the same
dated event packet and a versioned system guide. The original
[event evidence review](docs/event-evidence-review.md) records the motivating
coolant case. Source, offline validation and production activation are complete;
the event-history handoff records the September 22 live verification.

September 20 update: the advisor's fixed job timeout is now 300 seconds
(previously 180, producing “Codex took too long”). The dialog defaults to
Default / High (currently Astra), exposes validated model/effort controls,
records actual turn settings, and closes with X or Escape. Add Early Warning
uses a dedicated structured-proposal chat and explicit approval to save
bounded numeric threshold rules. Codex gains no shell, app-source editing,
CAN or service-control tools. Approved configuration is the supported app
modification path; unsupported trigger logic needs a separate implementation.

`custom_warnings.py` validates and evaluates owner rules using fresh historian
data. The advisor writes `/var/lib/van-telemetry-advisor/early-warnings.json`;
the production evaluator reloads it in existing background evaluations. Tests
opt in with a temporary path. Built-ins are unaffected by configuration errors.
The frontend distinguishes saved from broker-loaded rules and supports removal.
See [warning chat](docs/warning-chat.md) for boundaries and activation.
Source is updated; advisor/web restarts and one safe parked broker restart are
needed. No live warning was installed during tests.

Follow-up timeout fix: the advisor now reuses the existing Codex SQLite index
instead of backfilling an empty private index against the same 2 GB session
archive. Ephemeral runs, restricted tools, private app chats and the existing
ChatGPT login remain unchanged. Safe per-turn timing records distinguish local
startup, turn start, response activity and shutdown, with CPU/throttling totals;
Timing Details is available in the chat. This specific update requires only an
advisor restart and page reload, not a broker/web/CAN restart. See the warning
chat document for diagnostic semantics, verification and the prior live evidence.
The owner subsequently reported a successful live response after applying this
fix; the earlier timeout is no longer reproduced in that reported retry.

## Auxiliary bus development boundary

Ordinary CAN-CH telemetry is passive-only. The owner observed that an AlfaOBD
ABS connection illuminates multiple IPC warning indicators; the root cause is
not established, but the visible effect is sufficient to exclude ABS from
moving-vehicle polling. EPS, HALF, and ORC are likewise outside the auxiliary
active allowlist because they are steering, ADAS, and restraint controllers.
Their broadcast signals may still be decoded from synchronized receive-only
captures.

[`auxiliary_polling.py`](auxiliary_polling.py) is the offline executable policy
for prospective B-CAN work. It contains only the ICS `2001` odometer candidate,
fixes a minimum five-second
per-target interval, requires response-before-next-request sequencing, and
rejects every CAN-CH module. It has no live CAN import or execution mode. A
2026-08-28 parked ignition-on/engine-off check returned exact no-session-change
positive responses from both endpoints, and the owner observed no IPC, radio,
or center-stack side effect. The simultaneous dash odometer was 53,203 mi,
however, while ICS `2001` decoded to 53,191.860 mi—a material 11.140 mi deficit.
ICS is exposed as candidate-quality `vehicle.odometer` with an explicit
dashboard asterisk and unresolved offset/update relationship. The low-value
Uconnect temperature candidate is omitted. [`bcan_auxiliary.py`](bcan_auxiliary.py)
is the independently owned implementation: during a broker-qualified
engine-running epoch it owns only the serial-resolved B-CAN role, arms once,
and sends the immutable raw single-frame `03 22 20 01` request at five-second
cadence. It has no module/DID/payload/session/TesterPresent/cadence option and
cannot emit ISO-TP FlowControl. A timeout, malformed echo, NRC, topology or USB
change, inhibit, termination, or exception ends only the B-CAN helper and
requires exact passive restoration. Restoration failure sets a wildcard
inhibit; other B-CAN failures leave the independent C-CAN telemetry interval
running but block another auxiliary attempt until engine stop.

Broker status reports this owner separately as `auxiliary_drive`. While it is
armed, the synchronized recorder treats B-CAN as a broker-owned receive-only
companion—matching its existing C-CAN behavior—so raw B-CAN request/response
and broadcast evidence remain captured without a conflicting shared lease.
CAN-CH remains an ordinary shared passive recorder role.

## Dual-USBCANFD transition and roadmap status

The 2026-08-20 implementation completed roadmap items 2–7 for the installed two
dual-channel adapters, and the role-aware broker was deployed and passively
live-validated on 2026-08-21. Item 1 (USB hub/power-path changes) remains
deliberately tabled; this work neither diagnosed nor modified the hub.

1. **Hub/power replacement — tabled; transient observation implemented.** No
   USB reset, power-cycle, topology change, or service workaround is included.
   A receive-only kernel kobject-uevent observer retains sub-snapshot CAN
   adapter/ancestor-hub removal edges in a bounded queue. Events are deduped by
   boot/uevent identity, committed to SQLite before acknowledgement, and held
   behind an event-level advisory-consumption checkpoint until their episode
   persistence commits. Only explicitly consumed removal IDs are acknowledged;
   timestamp races and failed advisory passes replay on the next snapshot. One
   branch incident resolves only after fresh exact serial-role health is
   re-established. Prior-process incidents survive a broker restart until
   authoritative healthy exact-role evidence retires them. It never resets,
   rebinds, or configures hardware. See
   [`docs/usb-can-incident-monitor.md`](../../docs/usb-can-incident-monitor.md).
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
   opaque score or claim of diagnosis. Advisory episodes and transitions are
   persisted on ingest, including an independently gated absolute oil-pressure
   rule pinned to passive `0x41D` pressure plus `0x0FC` RPM, and CAN-infrastructure
   health. The warning panel exposes persisted outbox failures rather than only
   configured enablement. The tracked service explicitly enables
   delivery through the host's queue-aware ntfy helper; generator duty alone
   is never a warning.
7. **DTC history/dashboard — guarded parked batch implemented.**
   Strict UDS `19` parsing, SQLite recurrence/status history, saved-report
   import, and a compact cache cover all 16 registry modules without treating
   timeout/unavailable as “no codes.” `tools/dtc_batch.py` groups 15 supported
   registered modules into three role-owned arm/restore windows and sends only
   fixed physical `19 02 FF`, at no more than one request per second, after
   fresh ignition-on/engine-off/speed-zero and exact identity/state gates. PCM
   remains explicitly unsupported. The LAN dashboard stays cache-only; the
   Tailscale listener can queue this fixed job after explicit parked/gear/
   ignition-on-engine-off confirmations, without a separate local token. There is no
   DTC-clear, arbitrary-payload, module-address, or session-control path.

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

Raw `0x1F7` transmission-oil temperature has an additional stateful admission
gate before snapshot medians, broker cache, and historian samples. A change
greater than 10 °C within less than 1.0 second is rejected using raw-frame
monotonic timestamps. Gate state survives passive collector snapshots and the
active-drive helper's bounded snapshots. A rejected level stays quarantined
while uninterrupted frames remain more than 10 °C from the last good value;
returning to the last-good neighborhood clears it, while an observation gap of
at least one second starts a new evidence window. Rejection keeps the last good
temperature and does not discard `transmission.output_speed` or
`transmission.turbine_speed` decoded from that same `0x1F7` frame.

The broker latches repeated rejections into one bounded incident and exposes
active plus recent incidents in status. The historian upserts those stable
incident IDs, including rejection count and raw delta/window evidence, in a
separate data-quality table. A later admitted temperature resolves the incident.
If the broker restarts while an incident is active, the historian keeps that
evidence open only until the replacement process publishes its first
authoritative admitted sample for the same metric/source; process identity
prevents an empty startup snapshot from claiming recovery. Gate state updates
are atomic even though each normal passive/active reader has a single owner.
These records are explicitly ineligible for the advisory outbox: they never
send a notification, create an inhibit, reset hardware, reconfigure SocketCAN,
or change CAN traffic.

The initial drive-publisher vocabulary is intentionally narrow:

| Metric | Source | Type/unit | Quality |
|---|---|---|---|
| `battery.voltage` | `cluster.did.1004` | number, `V` | `observed_alfa_scale` |
| `generator.field_duty` | `pcm.did.01a1` | number, `%` | `observed_alfa_scale` |
| `engine.crankshaft_torque` | `pcm.did.06da` | number, `lb-ft` | `observed_alfa_scale` |
| `engine.vvt_oil_temperature` | `pcm.did.069f` | number, `°F` | `observed_alfa_scale` |
| `engine.oil_pressure` | `ccan.broadcast.0x41d` | number, `psi` | `observed_alfa_scale` |
| `engine.coolant_temperature` | `ccan.broadcast.0x2ed` | number, `°F` | `observed_alfa_scale` |
| `engine.rpm` | `ccan.broadcast.0x0fc` | number, `rpm` | `observed_alfa_scale` |
| `engine.target_crankshaft_torque` | `ccan.broadcast.0x100` | number, `lb-ft` | `observed_alfa_scale` |
| `transmission.output_speed` | `ccan.broadcast.0x1f7` | number, `rpm` | `observed_alfa_scale` |
| `transmission.oil_temperature` | `ccan.broadcast.0x1f7` | number, `°F` | `observed_alfa_scale` |
| `transmission.turbine_speed` | `ccan.broadcast.0x1f7` | number, `rpm` | `observed_alfa_scale` |
| `vehicle.ignition_on` | `ccan.broadcast.0x2ef` | boolean, `boolean` | `verified` |
| `vehicle.speed` | `ccan.broadcast.0x101` | number, `mph` | `observed_alfa_scale` |
| `vehicle.speed_limit` | `ccan.broadcast.0x0e0` | integer, `mph` | `verified` |
| `acc.set_speed` | `ccan.broadcast.0x5a0` | integer, `mph` | `verified` |
| `acc.state` | `ccan.broadcast.0x5a0` | string, `state` | `verified` |
| `acc.mode` | `ccan.broadcast.0x5a0` | string, `mode` | `verified` |
| `acc.follow_distance` | `ccan.broadcast.0x5a0` | integer, `bars` | `verified` |
| `acc.lead_vehicle` | `ccan.broadcast.0x5a0` | boolean, `boolean` | `verified` |
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

`vehicle.speed_limit` and `acc.set_speed` are the instrument cluster's
display values, referenced by the owner on the 2026-09-27 drive (every
captured speed-limit change and the ACC set speed 61 → 66 matched exactly; see
the [owner-referenced drive](../radar/findings/2026-09-27_acc_speed_limit_owner_reference.md)
and the `docs/bus-map.md` rows for `0x0E0` and `0x5A0`):

- `0x0E0` byte 0 is the speed limit in raw mph. Its `0` (key-off / no limit
  shown) is never published, so "none" arrives as the metric going stale
  after five seconds.
- `0x5A0` byte 3 is the set speed in raw mph, published only while the ACC
  state is ready, engaged, override or standby, byte 3 is nonzero and byte 2
  (km/h) agrees within 1 km/h. Off and ready-before-SET publish no set speed;
  the broker cache keeps an earlier value until its 10-second staleness, so
  consumers gate on `acc.state`.
- `acc.state` (`off`, `ready`, `engaged`, `override`, `standby`) is
  `((B6 & 1) << 2) | (B7 >> 6)` of the same frame, for adaptive and fixed-speed
  cruise alike. The owner's callouts on the 2026-09-28 drive matched every
  state (see the
  [owner-referenced drive](../radar/findings/2026-09-28_acc_owner_reference_drive.md)).
  Other raw values are not published.
- The graphic index `B6 >> 2` of the same frame gives three more display
  fields, checked against the same callouts:
  - `acc.mode` is `adaptive` or `fixed` (regular cruise);
  - `acc.follow_distance` is the distance setting in bars, 1 to 4, in adaptive
    mode;
  - `acc.lead_vehicle` says whether the cluster shows the vehicle-ahead icon,
    while engaged or in accelerator override in adaptive mode.

  Each is published only in the states that show it and only for a state and
  index pair listed in `ccan_powertrain.ACC_HUD_FAMILIES`. As with the set
  speed, the cache keeps an earlier value for up to 10 seconds, so consumers
  gate on `acc.state`. The lead vehicle's range and relative speed are not in
  this frame and have not been found.

They are display information only: no warning, notification or safety logic
uses them. `0x5A0` is sent at 1 Hz plus on change, and `0x0E0` trails the
required powertrain frames within each 100 ms cycle. The old
`ccan_powertrain.LowRateFrameWait` could not bridge the active helper's 0.35 s
snapshot window against the 1 s display period. Trip 67 had fresh `acc.state`
in only 998/2,239 running snapshots (44.573%). During one missed interval the
raw recorder still received 263 ACC frames with a maximum 1.004 s gap.

`display_receiver.py` now owns the production broker's display observations.
One continuous socket has exact standard-data kernel filters for only `0x0E0`
and `0x5A0`, a 64 KiB requested receive buffer, and kernel receipt timestamps.
It publishes each received frame immediately, atomically for all ACC fields.
Queued frames older than one second, missing timestamps and malformed frames
are discarded; silence never refreshes a value. Older bounded snapshots cannot
overwrite its cache. Existing freshness limits and state gates remain intact.

The receiver starts after initial reconciliation. While listen-only it holds
shared C-CAN role/channel leases and the normal passive scheduling turn. It
checks the wake admission gate every 250 ms without writing lock metadata and
releases before the broker starts its helper. While armed, it uses the drive
recorder's exact broker-owner proof, independently checks the USB role and
healthy classical-CAN state every second, and takes no competing lease.
Link loss, identity change, inhibit or ownership loss closes the socket; each
retry resolves and validates afresh. It never configures a link, sends a frame,
or changes helper heartbeat/diagnostic behavior. Its state, count and most
recent frame time are in `/v1/status` → `display_receiver`.
The kernel mechanisms follow the Linux documentation for
[CAN receive filters](https://docs.kernel.org/networking/can.html#raw-socket-option-can-raw-filter)
and [receipt timestamp control messages](https://docs.kernel.org/networking/timestamping.html#control-interfaces).

Both sources remain in `broker.ACTIVE_DRIVE_SOURCES`.
`tests/test_display_frames.py` covers snapshot source admission, and
`tests/test_display_receiver.py` exercises all 100 centisecond phases across
180 seconds, on-change delivery, kernel timestamps, stale queues, route loss,
passive/armed ownership and cooperative wake handoff. A post-deployment drive
is still required to measure the >=95% freshness acceptance target. Run the
bounded read-only audit with the new trip ID:

```bash
python3 tools/acc_freshness_audit.py --trip TRIP_ID
```

Deployment on 2026-09-29 at about 23:07Z used the guarded asleep broker restart.
The receiver entered listen-only reception with zero frames while asleep; all
three vehicle roles remained healthy/listen-only, the spare stayed down, no
inhibit was present, and every CAN TX counter was unchanged. Final offline
validation: 1,523 Python tests passed, 7 skipped, 1,354 subtests; 440 dashboard
tests passed including bundle budgets. Measured on the first drives after the change (2026-10-01 and 2026-10-02, historian trips 70, 71 and 72, `tools/acc_freshness_audit.py`): `acc.state` was fresh in 100 % of running snapshots on all three (203, 226 and 57 snapshots), against 44.6 % on trip 67. During the armed helper interval every sample had age 0 s. `acc.set_speed` reads lower (56 %, 67 %, 0 %) only because it is not published while ACC is off.

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

`engine.crankshaft_power` is an ECU-estimated crankshaft calculation from the
fresh PCM `06DA` torque and qualified passive `0x0FC` RPM pair:
`hp = lb-ft × rpm / 5252.113`. It is recomputed only when the matching torque
sample arrives, requires no more than 1.5 seconds of input skew, and inherits
the older input's timestamp so it cannot appear fresh after either dependency
expires. Negative overrun is preserved. This is not measured wheel horsepower
or a dynamometer result.

`engine.vvt_oil_temperature` preserves AlfaOBD's exact **VVT Oil Temperature**
label for PCM `069F`. It decodes one data byte as `raw - 64 °C`, converts to
°F, and expires after 15 seconds. The coordinated owner polls it no more often
than once every five seconds, after the normal generator/torque/TPMS cycle.
Each read has its own fresh transmit permit; a response failure disables only
this optional metric until the next running epoch. A restored/stopped owner
invalidates it immediately. The six-card telemetry dashboard uses the explicit
VVT label and discloses that the sensor/model relationship to sump oil remains
unresolved. No temperature alert is inferred. History includes the new metric.

`pcm_temperature_support.py` is a separate parked support check, never a
dashboard endpoint or a running-helper profile. Its default is an inert plan:

```bash
python3 projects/vehicle_data/pcm_temperature_support.py
```

Its fixed live sequence sends at most one padded physical `22 F45C` and one
`22 069F`, at least one second apart, with a 0.75-second response timeout and
no retry, wake, session control, TesterPresent, or FlowControl. Live use
requires `--execute --confirm-parked-ignition-on-engine-off`; fresh ignition,
three zero-RPM samples, and zero road speed are checked under exact role
ownership before arming and again before each request. Results and exact
restoration status go under `tmp/inventories/pcm/`. F45C is a standardized
candidate only and is not added to production polling by this check.

The same guarded tool now offers a separate **`--profile oil-life`** check
for the historically mapped PCM `2185`. It sends only one fixed padded
`03 22 21 85 00 00 00 00` frame, never the temperature requests. Its 0.75 s
timeout, no retries/session/TesterPresent/FlowControl, fresh ignition-on,
three zero-RPM samples, zero-speed gate, role ownership, and passive cleanup
are unchanged. It accepts only the exact one-byte positive echo with raw
percent in `0..100`; other values are rejected, not clamped. Reports use
`tmp/inventories/pcm/oil-life-support-*.json`; nothing is published to telemetry.

```bash
python3 projects/vehicle_data/pcm_temperature_support.py --profile oil-life
# Only while parked, ignition ON and engine OFF, in a shell with working sudo:
python3 projects/vehicle_data/pcm_temperature_support.py --profile oil-life --execute --confirm-parked-ignition-on-engine-off
```

October 2 preparation passed 6 tests plus 6 subtests in job
`20261002T232716Z-96511136`. The agent's live attempt stopped at the host
privilege check (`no new privileges`, sudo unavailable) before arming or
transmission; the successful owner-shell check below supersedes that blocker
for live validation. This tool does not enable production polling.

The first owner-shell oil-life attempt (`20261002T233039010556Z`) was blocked
before arming by the broker display receiver's shared C-CAN lease. The support
tool now reserves `can_handoff.active_turn("c-can")` before the authoritative
role/channel locks and holds that scheduling turn through passive restoration
and lock release. Admission waits at most 1.25 seconds; this is not a request
retry or a lock bypass. Cooperating broker receivers yield without a service
restart. Regression job `20261003T002824Z-20e79287` passed 23 tests and 124
subtests across the support tool, handoff locks, and display receiver.

Owner-shell validation at `2026-10-03T00:30:15Z` succeeded: `62 21 85 52`
decoded to **82% oil life remaining**, with `restored_passive: true` and no
error. C-CAN TX rose by exactly one, all vehicle interfaces read back healthy
and passive, and the continuous display receiver resumed with unchanged
broker/recorder PIDs and zero restarts. No session-control, TesterPresent,
retry or reset was sent; the inherited session is not identified. Evidence:
`tmp/inventories/pcm/oil-life-support-20261003T003014137255Z.json` and the
[oil-life finding](../ecu_mapping/findings/promaster_2022/2026-09-22_gear_and_oil_life_offline.md).
The successful standalone check itself does not publish a dashboard value.

`engine.oil_life_remaining` is now implemented as the fourth closed PCM profile,
`pcm.did.2185`, integer percent `0..100`, quality `observed_alfa_scale`. It is
an ECU maintenance estimate, not measured oil quality or service history. The
coordinated running helper reads it immediately when first due and no more
often than every **60 seconds**, after the preceding PCM/TPMS replies. It
requires its own fresh one-use permit and the unchanged running/identity/
inhibit/lock/interface gates. It never sends session control, TesterPresent,
FlowControl, a retry, or reset. An oil-life response failure disables only
this optional metric for the running epoch; existing telemetry and recording
continue. Loss of the owner immediately invalidates the live value despite
its 180-second freshness limit. The generic last-reading store preserves a
dated value independently of the live cache and recovers it across restarts.

The existing Service card in Parked and Health now has a registered source:
`/v1/maintenance.oil_life.available` describes **source availability**, not a
fresh measurement. The card reads the live/dated metric separately, showing
`—` before the first automatic sample. The public catalog supplies provenance,
units and limits. Oil-change records still cannot reset the PCM; there are no
new warning thresholds or public acquisition/publisher permissions.

Rolling-deploy compatibility is explicit: the helper defaults oil life off
unless its supervising broker passes `--enable-oil-life`. The updated C-CAN
supervisor passes that fixed flag; B-CAN does not. Thus the old running broker
may launch updated helper source without receiving an unknown metric before
its restart.

Implementation verification on October 3: **1,631 Python tests, 2,059 subtests
passed; 6 skipped** (`20261003T013845Z-1e242336`), including cadence, permit
isolation, invalid percentages/echoes, optional failures, stop/TTL retention,
restart persistence and the actual Service-card view-model. The separate
dashboard build passed **443 Node tests** (`20261003T013727Z-8f1f455d`). Build
`2c99f40ec884` was published atomically without deleting old hashed assets;
its index and updated oil-life caveat were read back over LAN HTTP. The prior
build is recoverable from `tmp/vehicle_data/dashboard-before-oil-life-20261003T0142.tar.gz`.

**Enabled, owner-verified 2026-10-03 at 01:47:39Z:** the owner ran the guarded
restart from a normal shell because the agent cannot elevate. The helper
checked fresh stopped/asleep state and idle healthy owners, restarted the
broker, then confirmed the new catalog/source and enabled active-drive policy.
Independent post-deployment API checks confirm the new metric, 60-second
engine-running policy, Service-card source availability and persistent
last-reading store with no storage error. All four broker/recorder/web services
are active with zero crash restarts. The recorder PID remained unchanged.

The vehicle was asleep, so the helper stayed idle and **no automatic oil-life
sample has been observed yet**. `engine_not_running`/no cached reading is
expected, not a deployment failure. All three CAN roles remained classical,
listen-only, ERROR-ACTIVE, `restart-ms 0`, with zero RX/TX errors. TX counters
stayed C/B/CH **127234/10846/0**, so activation added no CAN traffic. The first
automatic sample waits for the next qualified engine-running interval; no
special engine start or further command is needed for activation.

Recorded owner-shell activation command (already completed):

```bash
python3 /home/pi/dev/obd-things/tmp/vehicle_data/enable-oil-life.py --execute --confirm-parked-engine-off
```

No earlier 17% or 82% report was injected as a new live reading. Edits remain
unstaged; no commit/push was requested.

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
Oil pressure, coolant, RPM, and guarded diagnostic crankshaft torque receive
fresh observations; engine-oil temperature has only the separately labeled
VVT `069F` reading. Context-aware oil-pressure evaluation and fresh
time-aligned torque/RPM power derivation still require specialized evaluation
logic. Passive RPM sends no diagnostic traffic.

The OEM warm-engine oil-pressure references (15–34 psi at approximately
650 rpm, 28–35 psi from 1,000–3,000 rpm, 65–80 psi above 3,500 rpm, only at
192–212 °F coolant; no published band for 3,000–3,500 rpm) are advisory context,
not alerts. The dashboard's oil-pressure bands, their slightly lower floors and
their running/coolant gates are specified in
[`dashboard/docs/caveats.md`](dashboard/docs/caveats.md) and `dashboard/src/bands.js`.
The broker's approximately-12-psi critical rule is an early-warning concern,
documented in [`warnings-redesign.md`](dashboard/docs/warnings-redesign.md).

## Telemetry dashboard

There is one telemetry dashboard: the app in [`dashboard/`](dashboard/), served
on port 8765. It was built in September 2026 as "v2", a parallel app on port
8766, and on 2026-09-27 it replaced the former static frontend
(`static/*.js`, removed; it survives in git history up to commit `fa966ba`).
Port 8766 and the `van-telemetry-web-v2{,-tailscale}.service` units are
retired. "v2" survives only in names: the `/v2/*` routes, `web_v2.py`, the
`van-telemetry.v2.settings` storage key and the node-suite wrapper
`tests/test_dashboard_v2_node.py`.

Documents, in reading order:

- [`dashboard/docs/design.md`](dashboard/docs/design.md) is the build
  contract (the 09-22 baseline with the 09-23 review amendments A1–A21) and
  the deployment recipe (section 11). Read it before changing the app.
- [`dashboard/docs/caveats.md`](dashboard/docs/caveats.md) and
  [`dashboard/docs/warnings.md`](dashboard/docs/warnings.md) are the
  owner-facing pages, rendered to `/docs/caveats.html` and
  `/docs/warnings.html`; no other docs are published.
- [`dashboard/docs/freshness-contract.md`](dashboard/docs/freshness-contract.md)
  lists the acceptance rules `src/link.js` implements, and
  [`dashboard/docs/warnings-redesign.md`](dashboard/docs/warnings-redesign.md)
  the tiered early-warning backend.

### Serving and deployment

- `web_v2.py` subclasses `web.py` for every `/v1` route and adds: the built
  frontend from `dashboard/dist` (`index.html` no-cache with ETag, hashed
  `/assets/*` gzip'd and immutable, `/docs/*` allowlist); `GET /v2/stream`
  (status-lite plus a catalog hash instead of the catalog); `GET /v2/summary`
  (one trimmed bundle; warning lists are never capped and other capped lists
  report `<key>_omitted_count`); `bind` and the `build` id in every `web` flags
  object; and one User-Agent log line per client IP. `web.py` alone serves no
  page. Keep the `web_v2.py` filename: the installed units execute it by path.
- `van-telemetry-web.service` binds loopback in the tracked unit; the
  machine-local drop-in `/etc/systemd/system/van-telemetry-web.service.d/10-lan.conf`
  adds `--allow-remote-bind` and the LAN address (`http://vanpi.lan:8765/`).
  The passive voltage read is allowed there and DTC jobs are not.
  `van-telemetry-web-tailscale.service` binds the Tailscale address from
  `/etc/van-telemetry/tailscale-web.env` on port 8765 with guarded DTC jobs
  (`VAN_TELEMETRY_DTC_ORIGIN=http://<tailscale-ip>:8765`). Both run with
  `PrivateDevices`, `ProtectHome=read-only` and `PartOf=van-telemetry.service`.
- The MacBook-managed Van Dashboard (`van-dashboard.service`, from read-only
  `/home/pi/scripts`) reads `http://192.168.6.103:8765/v1/snapshot`. Every
  `/v1` route must keep working.
- `dist/` is gitignored build output served live. `npm ci && npm run build` in
  `dashboard/` is the frontend deploy step; the build publishes atomically and
  writes its id to `dist/build.json`. A frontend-only change needs no restart.
  A `web.py`/`web_v2.py` change needs a restart of the two web units. New
  broker metrics need a parked `van-telemetry` restart (the broker registry,
  passive reader and active helper snapshot all change).
- Only the Tailscale listener has DTC jobs, so only one process writes the
  unlocked on-disk job pointer. The request file is exclusive-create. Start
  one scan at a time.

### Frontend decisions

- Preact 10, @preact/signals 2 and esbuild, targeting `chrome80`/`firefox78`
  for the owner's portrait-mounted 800×1280 Samsung tablet. The core bundle
  budget is 92,160 B (91,749 B used on 2026-09-27), so new code belongs in lazy
  chunks.
- Views read summary slices and stable signals. Only `derive.minuteClock`
  touches the clock in a render. There are no inline styles, and the only
  timers are the DTC poll and the shared 10 s sparkline redraw. Status-lite is
  published only when its stable projection changes.
- Views: Drive (page 1 fits one portrait screen; page 2 "Sensors" is a lazy
  chunk with speed limit, ACC set speed, gear estimate, radar aim, shaft
  speeds, power and torque), Parked, Health, History and System. Dialogs:
  event history, Ask Codex / Add Early Warning (needs
  `van-telemetry-advisor.service`, see [warning chat](docs/warning-chat.md)),
  the per-device customiser and the oil-change sheet. The guarded DTC scan
  appears only where `web.dtc_jobs_enabled`.
- Owner rules: no feature removed; no provenance jargon on cards; "stale" at
  most once per card; nothing under 13 px; 44 px tap targets. The alert lives
  in the 48 px top bar in place of the state text (warning/critical only,
  `metric value +n`).
- Tires are coloured by absolute floors and warning rules only. Voltage band
  gating is `derive.voltageMode`: running after 10 s of RPM ≥ 400, parked for
  samples ≥ 30 s after running, otherwise neutral. Coolant normal is
  160–220 °F. Numerals change colour only for a confirmed warning or a live red
  band; the normal band is drawn neutral. Voltage shows 1 decimal on Drive and
  2 on Parked.
- A manual view choice lasts until the engine next starts or stops. Dim is per
  device. Tapping the connection dot runs a resync plus a forced summary
  fetch. Settings live in `localStorage` (`van-telemetry.v2.settings`); on the
  shared 8765 origin the former app's `van-telemetry.dashboard.v3` view
  selection migrates once and the Codex chat access code
  (`van-warning-chat.*`) carries over.
- `RETAIN_LAST_READING` in `dashboard/src/store.js` is the single presentation
  policy for dated last values (see "Last-recorded display policy" below).
- Held-open tier-2 warning notices can carry `None` in
  `current.observed_at`/`source` and in baseline/deviation values; the
  frontend must tolerate this.

Tests: `npm test` in `dashboard/` (node suite, also run by
`tests/test_dashboard_v2_node.py`), `tests/test_web_v2.py`, and
`tools/shoot.mjs` / `tools/measure.mjs` for browser screenshots and
measurements at 800×1280 and 1280×800.

### Open items (2026-09-27)

- Still to build: the tablet probe page (design section 10, step 0), 192/512
  PNG icons, the `window.__perf` counters with a live-drive replay in
  `tools/measure.mjs`, and a formal parity-checklist run against the former
  app (git history).
- `vehicle.speed_limit`, `acc.set_speed` and `acc.state` (deployed
  2026-09-27) were first recorded live on 2026-09-28 (historian trip 67). The
  owner annotated that drive, and its raw frames verified the ACC state,
  following distance, lead-vehicle icon, cruise mode and every cruise button
  ([September 28 drive](../radar/findings/2026-09-28_acc_owner_reference_drive.md)).
  `acc.mode`, `acc.follow_distance` and `acc.lead_vehicle` were added on
  2026-09-29 (catalog 32 metrics) and have not been seen live yet.
- The September 29 audit confirmed the broker missed the 32 s `off` interval
  at 2026-09-29 00:04:23Z while raw frames continued at about 1 Hz. The new
  continuous display receiver replaces snapshot timing for display publication;
  trips 70-72 (2026-10-01/02) measured 100 % fresh `acc.state` running snapshots,
  so the >=95 % acceptance target is met.
- The passive collector cycles every ~4.3 s instead of 1 s, giving 2–3.5 s old
  values and `unknown` vehicle-state blips (drive audit 2026-09-24).
- Parked battery-low (tier 0, S1) needs 3 readings in ~24 s, but parked wakes
  are single bursts, so a sagging battery over a multi-day stay may not
  confirm. Old rules never pushed parked cases either. Replay reports are
  under `tmp/vehicle_data/warning_replay/`.
- From the 2026-09-24 drive audit: output speed carries a constant +1/32 rpm
  LSB; the API returns unrounded floats; `active_drive.interface_mode` stays
  `armed_diagnostic` after restoration.
- Warning backend decisions still open: a robust slope for tire notices; a
  minimum number of settled buckets per stop for resting voltage; whether to
  keep the coolant 222 °F slow-motion floor. Tier 3 (adapter/bus/data quality)
  never notifies, and a single cranking dip never alerts.
- Server work for later: the per-wheel TPMS cold baseline, `trip_summaries`
  per-trip statistics, and a `recent_series` seed for the Drive sparklines
  (design section 8). The RPM gear footer waits for a driver-qualified
  `transmission.gear_estimate` (candidate today; design section 9).
- ACC radar DID reads while driving are blocked by design (all DID tools
  require `--confirm-parked`, and the helper has no running-engine handoff).
  Candidates: radar 102A, 1921, 0103, 0851, 0858, 0863, 0872, 2013, 292E,
  0857, 0861, 0862; PCM 0891. Next: a parked default-session check, then a
  reviewed addition to the helper's fixed read list.
- Board A receive stall (2026-09-22 → 09-24): C-CAN and B-CAN stopped
  receiving while their links stayed UP/listen-only/ERROR-ACTIVE; a reboot
  recovered it and the cause is unknown. A low-speed device at USB
  1-1.2.4.4.2 failing enumeration every few seconds on the same hub tree is a
  suspect. `receive_watch.py` now flags a role whose `rx_packets` stays flat
  ≥ 120 s while CAN-CH moves (broker basis
  `passive_can_ch_activity_c_can_silent`, historian reason `receive_silent`,
  a tier-3 System note that never notifies).

[History: Fixes deployed during the dashboard build (2026-09-24 → 09-27)](../../docs/history/vehicle-data-changelog.md#fixes-deployed-during-the-dashboard-build-2026-09-24--09-27)

### Vehicle state for the dashboard

Dashboard values are registry-driven even where the layout keeps a future
metric role visible. `GET /v1/snapshot` returns the public metric catalog,
every metric's cache-only response, broker/interface status, and an
evidence-qualified `vehicle_state` object in one request. A future registered
metric therefore becomes available to the generic metric and catalog panels
without adding another web proxy route or SSE request.

Candidate metrics stay out of driver-qualified values until their identity
and scaling meet the recorded evidence policy; the System view's catalog shows
them. The dashboard's automatic view (design section 5.2) uses only fresh
evidence and is a layout choice, never an engine-running safety gate for
oil-pressure alerts or any other mechanical limit evaluator.

The broker deliberately does **not** infer engine-running state from charging
voltage. An external charger can overlap alternator voltage, and ordinary bus
activity can be ignition-on or a key-fob/module wake. Current
passive acquisition can report `awake`, inferred `asleep`, or `unknown`, with
`running: null` whenever the evidence cannot distinguish those cases. This
keeps the dashboard's automatic view selection ready for a separately
verified ignition/motion metric without silently promoting a voltage
heuristic.

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

The closed PCM engine-running registry contains exactly three immutable
profiles. Its only possible PCM transmissions are these physical, 29-bit,
fixed-DLC-8 frames:

```text
18DA10F1#032201A100000000
18DA10F1#032206DA00000000
18DA10F1#0322069F00000000
```

Each response must be a single frame from `18DAF110` with the corresponding
exact echo: `62 01 A1` and `62 06 DA` each require two data bytes;
`62 06 9F` requires one. A raw CAN
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

All seven are physical `ReadDataByIdentifier` requests to endpoints registered
in `lib/modules.py`; no functional broadcast request exists. The helper stops
on loss of RPM or C-CAN traffic, topology/inhibit changes, adapter health
failure, timeout, malformed response, rejection, termination, or exception.
`session_required` is reported distinctly and does not enable a session
change. A failed or unverified restoration sets a persistent operation inhibit,
is latched in the broker, and prevents another active interval.

The broker's supervisor (`ActiveDriveSupervisor`, also used for the B-CAN
auxiliary helper) ends a helper that emits nothing for 10 s, and kills one that
has not finished cleanup 10 s after termination. Either case is an unverified
restoration. Both limits measure the helper, not the host. When the
supervisor's own loop did not run for 1 s or more, that time is credited to
both limits, up to 30 s per silence window, and one line is written to the
service journal. A helper that goes quiet while the supervisor keeps running
gets no credit, and a stall longer than 30 s still fails closed.

- Reason (2026-09-28, trip 67): at 01:38:30Z the whole Pi stalled for about
  10 s. The journal has a gap and out-of-order entries from 19:38:19 to
  19:38:29 MDT, the drive recorder logged three timed-out broker status
  requests (6.2 s), and an agent CLI process started at 19:38:29 after an SSH
  login. Both helpers were ended for silence and both inhibits latched
  (`vehicle-data-restoration-failed`, `vehicle-data-bcan-restoration-failed`).
  Polled metrics and the drive capture stopped for the rest of the drive. The
  stall's cause is likely the process start under memory pressure; that is not
  proven.
- Read back the next day: all three vehicle roles UP, listen-only and
  ERROR-ACTIVE with zero bus errors, restarts and bus-off, and static TX
  counters. The adapters had been restored; only the confirmation was lost.
- Recovery used, owner-directed, van asleep: inspect all four roles, then
  `python3 tools/can_operation_state.py inhibit-end NAME` for each inhibit,
  then restart `van-telemetry` (the broker also holds the latch in memory).
  The broker serves a cached interface status, so an ended inhibit can still be
  listed for a few seconds.
- Tests: `tests/test_active_drive.py`, the `test_supervisor_*stall*` cases and
  `test_supervisor_still_bounds_a_helper_that_hangs_without_a_stall`.

The newly added torque read is optional within an otherwise healthy interval:
its first response failure is published as a metric-specific acquisition
failure and suppresses further `06DA` requests until the next engine-running
epoch. It does not stop generator duty, TPMS, passive broadcast publication,
or the receive-only drive recorder. This narrower behavior does not relax any
interface, topology, RPM, permit, or restoration gate.

The transport methods cannot send on control-flow convention alone. Their
opaque permit is fixed to the currently serial-resolved C-CAN `canN` and its
live exclusive diagnostic-lock handle, one of the four reviewed transport
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

`drive_recorder.py` preserves synchronized C-CAN, B-CAN, and CAN-CH evidence
while the broker owns the serial-resolved C-CAN channel. The normal C-CAN
observer lock and the standalone
`passive_drive_capture.py` entry point intentionally reject an armed interface;
this narrower companion instead requires one exact broker status:

When `auxiliary_drive` is armed, the same status must prove the exact
serial-resolved B-CAN helper PID, fixed pins-3/11 topology, 125-kbit/s
classical-CAN state, and no restoration fault. The recorder then binds B-CAN
without taking the helper's exclusive role/channel lease. If the helper is
disabled or has restored cleanly, the recorder retains its original shared
passive B-CAN lease path.

- active drive enabled and `armed_diagnostic`, with an integer helper PID and
  `current_owner.kind=broker_active_drive`;
- qualified `0x0FC` engine-running state, usable same-boot C-CAN pins 6/14
  topology, no operation inhibit, and the broker-reported healthy 500-kbit/s
  ERROR-ACTIVE `canN`, rechecked against the expected USB serial and `dev_id`;
- an initial `0x2EF` frame within five seconds of opening the receive socket.
- for each secondary, an exact passive B-CAN/CAN-CH broker route followed by a
  freshly acquired shared role/channel lease (or the exact auxiliary B-CAN
  owner above); B-CAN must produce `0x46C` and CAN-CH its unique `0x0DA`
  signature within five seconds of each segment start. Since 2026-09-27 an
  unproven secondary no longer blocks the C-CAN start; it joins when proven
  (see below).

It then runs three loss-accounted recorders with `candump -D`, 16 MiB socket
receive buffers, ten-minute zstd chunks, a C-CAN priority stream, and full
streams for every role above the existing 30/25 GiB free-space floors. `-D`
keeps each receive process attached
across the broker's expected end-of-interval interface down/up restoration.
The recorder accepts an armed interface only while the exact broker owner is
present, but may continue after verified listen-only restoration so the raw
session includes the key-off tail. Twenty seconds without C-CAN `0x2EF`
cleanly ends all three recorders. `capture-set.json` is written after every
role finalizes; its `complete` flag is true only when all three roles recorded
one uninterrupted segment, and `primary_complete` reports C-CAN. A drop,
compression, storage, or cleanup failure on any role still fails the campaign.

#### Scheduled status refresh and secondary-route resilience (2026-09-27)

Campaign `broker-drive-20260927T212949433889` stopped all three buses at
22:00:10Z, 34 minutes before engine stop, when the owner's cron
`projects/battery/voltage_mon.py` requested `battery.voltage`. As in the
[2026-09-06 diagnosis](../ecu_mapping/findings/promaster_2022/2026-09-06_broker_topology_refresh_diagnosis.md),
the refused (`can_busy`) acquisition refreshed the broker's interface snapshot
mid-interval. The auxiliary-armed B-CAN role was re-probed as
`interface_armed` with `safe=false`; `safe` is the passive-observer bit and
only a verified listen-only role gets it. The broker's verified-owner overlay
still marked the route correctly (`armed_owner=broker_auxiliary_drive`,
`topology_usable=true`; the historian kept `topology_usable=1` throughout),
but `drive_recorder.broker_secondary_route()` required `safe=true` for the
armed branch too, so every B-CAN route check failed once a second until the
engine stopped. The C-CAN side of the same refresh was repaired on 09-26.

Repairs:

- `broker_secondary_route()` requires `safe=true` only for the passive branch.
  The armed branch now requires the broker's verified-owner markers
  (`armed_owner=broker_auxiliary_drive`, `topology_usable=true`) in addition to
  the unchanged auxiliary helper, identity, bitrate, FD, one-shot, restart-ms,
  and controller checks. The broker sets those markers only after re-checking
  the helper's owner route, restoration latch, and inhibits. The broker is
  unchanged.
- The recorder records C-CAN as the only required role. B-CAN and CAN-CH are
  best-effort segments. A secondary route loss ends only that role's segment,
  after the generic recorder finalizes it. Route loss here means lost broker
  proof, failed lease/identity/listen-only/controller revalidation, or no
  `0x46C`/`0x0DA` within five seconds. C-CAN and the other secondary keep
  recording. The lost role is re-admitted no sooner than 10 s later. That
  requires a fresh broker status that still proves the primary interval and
  the exact route, then a fresh lease or armed-owner check. The new segment
  appends to the same role directory and continues its chunk numbering
  (`Recorder(sequence_start=...)`), so no earlier chunk or partial is reopened.
  `run.json` lists `secondary_admission`. `route-events.jsonl` logs every
  segment start/end and await. `capture-set.json` lists each secondary's
  `segments` and `continuous`. A secondary directory appears only after its
  first admitted segment. `van_scan_harvest.bus_capture_ended()` now uses the
  latest `capture_start`/`capture_end` marker.
- The recorder remains receive-only: it never configures CAN, never takes an
  exclusive lease, and never records a route it cannot currently prove.
- Rejected alternative: skipping `_refresh_interface_status()` in
  `TelemetryBroker.acquire()` after a `can_busy` refusal. While the helpers
  own the channels, the collector is blocked, so this refresh is the only
  fresh kernel evidence during a drive. Skipping it would keep a pre-arm
  snapshot that still claims listen-only and would hide a real
  controller/USB/identity fault. It would also freeze status whenever the
  TPMS logger or a manual tool holds the lock, and it would starve the
  USB-CAN monitor and receive-watch evidence. The defect was in how
  consumers classified the refresh.
- Regressions (`tests/test_vehicle_drive_recorder.py`): the B-CAN refresh
  replay for both the armed auxiliary helper and a blocked helper with passive
  B-CAN. Negative cases cover controller, bitrate, FD, one-shot, restart-ms,
  link, foreign owner route, restoration latch, inhibit, and missing owner
  markers. Segment tests cover loss/re-admission, a missing start signature,
  fatal drop/storage errors, start while a secondary is unproven, and type
  preservation through the real `Recorder`.

Deployment requires only restarting `van-drive-recorder` while parked.
Operational follow-up: the next drive that crosses a scheduled voltage check
(10:00–22:00 local, even hours) should keep one uninterrupted B-CAN segment.

An armed recorder status check retries only the observed transient Unix-socket
conditions (`EAGAIN`/`EWOULDBLOCK` and timeout), for at most five attempts and a
nominal five-second deadline. Every failed attempt revalidates the exact CAN
interface. Verified listen-only restoration ends the ownership check cleanly;
a non-transient error, failed revalidation, or bounded exhaustion ends the
current capture as broker-ownership loss and returns the daemon to its wait
loop. It does not open or configure CAN, weaken the initial broker-owned armed
gate, or exit into a systemd restart loop.

The September 5 follow-up preserves `BrokerOwnershipLost` through the actual
capture finalization boundary using `RecoverableCaptureError`. The original
fix lost its type when the generic recorder wrapped it in `CaptureError`.
Now cleanup completes before the typed failure reaches the daemon's wait loop;
failed compressor validation, child cleanup, final interface checks, and
storage loss take precedence and remain fatal. Secondary ownership loss may
also recover, but concurrent secondary storage/cleanup failure wins. The
regression exercises the real recorder and daemon boundary, including an
ownership loss followed by failed compression.

The daemon loops after a successful finalization: while parked it opens no CAN
socket and waits for the next broker-owned running interval, then creates a new
timestamped campaign automatically. Consequently the installed service does
not need manual re-arming between ordinary drive legs when enabled. Its
installed unit is enabled and active. New output is under
`/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/`, and the small
operational state is `tmp/vehicle_data/drive-recorder-state.json`.

The role-aware battery acquirer supports ordinary passive mode plus one bounded
local `wake_if_asleep` transaction. It first tries the permanent C-CAN role and
then B-CAN passively. Only a silent exact B-CAN role may use the fixed 75-frame
`0x7FF` wake; publication requires B-CAN signatures, sane verified `0x46C`, and
completed exact passive restoration. The broker establishes a nonextendable
12-second authorization from fresh collector/parked state and enforces a
15-minute wake-attempt cooldown. Active-drive, observers, inhibits, unhealthy
or stale broker state, and restoration faults win. There is no bitrate hunt,
cross-role fallback, or caller-selected channel/payload. The reconciler records
each resolved role's topology after verified passive setup, and every wake
rechecks that same-boot role/pair record under exclusive ownership.

The Unix HTTP transport executes every POST on its serving thread (the process
main thread in production), where the existing termination-signal guard remains
valid. Eight bounded daemon transport workers can answer cache GETs during an
acquisition; POSTs execute one at a time in ready-request FIFO order. This does
not change CAN ownership, acquisition order within the source, or cleanup.
Excess connections remain in the kernel backlog (64); eight occupied connection
slots can still delay reads. A five-second absolute timeout bounds each HTTP
input/output phase, not queueing or CAN work. Expired input cannot dispatch a
mutation. The unprivileged web processes remain independently threaded.
This O2 implementation has not been activated on the Pi by this change.
Each `UnixHTTPServer` instance serves one lifetime; construct a fresh instance
rather than restarting `serve_forever()` on a stopped object. `serve_unix()`
already creates a fresh server on every invocation.

### Passive harvest of the van's own health checks (2026-09-24)

The van has its own diagnostic client, probably the TBM2. It is never queried itself, and it is
the only registered module missing from the sweep table. It uses tester source `F1`, the same as
the Pi. Now and then during a drive it runs a read-only sweep of about 40 addresses: identity DIDs
`F132`/`F100`/`F1A0`, `19 02 0D` DTC passes, TCM `22 21xx` reads, and PCM KWP `18 00 FF 00` after
`7F 19 11`. It runs only while the Pi's F1 traffic is quiet and yields within one to two seconds.
CAN-CH targets are requested on C-CAN. The gateway forwards them, and their replies come back onto
C-CAN as `18DAF2xx`. B-CAN targets are requested on B-CAN directly. Sweeps were seen on
2026-08-31 02:38–02:56Z and 2026-09-24 21:12–21:27Z. The 08-31 capture also has two partial,
identity-only sweeps at 02:26Z and 02:30Z. The analysis is in
`tmp/ecu_mapping/f1-scan-20260924/`.

`van_scan_harvest.py` turns those sweeps into app data with **no CAN access**:

- **Input.** It reads only the chunks that a campaign's `manifest.jsonl` lists as complete, and
  only when the file size matches the manifest. It never reads `.partial` files, never touches
  `capture.lock` and never touches the recorder process. It streams `zstd -dcq | grep -F ' 18DA'`,
  about 1.5 s per 14 MB C-CAN chunk.
- **Reassembly and exclusion.** It reassembles ISO-TP messages and pairs each request with its
  reply. The Pi's own requests are counted only as activity intervals:
  - PCM `22 01A1/06DA/069F`
  - RF Hub `22 31D0–31D3`
  - radar `22 0845`
  - ICS `22 2001`
  - any `19 02 FF` / `19 01 FF` / `19 03` / `19 0A`

  `tests/test_van_scan_harvest.py` checks this list against the helper modules.
- **Windows.** Non-Pi exchanges are grouped into windows split by a gap of more than 60 s. A
  window closes only after every recording bus has been read past it.
- **Classification.** A window with a `19 02 0D` request is an in-vehicle health check. Any other
  window is kept as `other_f1_traffic` and is not imported. One such window was the Pi's own PCM
  experiments on 08-31.
- **Idempotence.** Per-campaign checkpoints under `tmp/vehicle_data/van_scans/state/` carry the
  ISO-TP and pairing state across chunk boundaries.
- **Output.** Each scan is written to `tmp/vehicle_data/van_scans/scans/<campaign>--<start>.json`
  with its windows, Pi-overlap evidence, and per-module `59 02`/`58` results, identity DIDs and
  other reads. Every VIN is masked. The scan is imported into the separate
  `in_vehicle_scans` / `in_vehicle_module_results` / `in_vehicle_dtc_observations` tables of
  `tmp/vehicle_data/dtc-history.sqlite3`, versioned by the metadata key
  `in_vehicle_scan_schema_version`. The Pi's `module_scans` table and its derived state are never
  written, because a mask-`0D` read cannot resolve codes that a `19 02 FF` read found.
- **Broker cache.** It rewrites `tmp/vehicle_data/van-scan-cache.json` atomically. The broker's
  `/v1/diagnostics/dtcs` adds that file as `in_vehicle_scan`. The Pi fields are unchanged; the new
  part carries the latest successful result per module with `observed_at`, and `last_scan` with
  `pi_quiet`. `web_v2.dtc_lite` passes it through. The dashboard Health card shows whichever read is newer
  for each module, and System → Broker shows "Van health check". The owner-facing wording is in
  `dashboard/docs/caveats.md`.

It runs from `systemd/van-scan-harvest.{service,timer}`: a oneshot every 20 min at Nice 19 with
idle I/O and `--max-chunks 30`. The mount is read-only in the unit, and `PrivateNetwork` and
`AF_UNIX` only are set. A manual run looks like this:

```bash
nice -n 19 ionice -c3 python3 projects/vehicle_data/van_scan_harvest.py \
  --campaign 'broker-drive-20260924T*'   # omit --campaign to process every incomplete campaign
```

The drive recorder's `run.json` conditions no longer claim "no external diagnostic client". They
now say that the van's own F1 client may sweep while the Pi is not polling, and that this harvester
notes it.

### File-only cruise summaries (September 29; running since October 2)

`cruise_summary.py` reduces completed C-CAN **full** chunks for one completed
historian trip. It reuses the verified cluster decode and validates each input's
manifest size/hash, historical channel and identifier namespace. It integrates
engaged, override and lead-icon durations, CANC press/standby transitions, and
time-weighted set speed versus displayed speed limit. State gaps over two seconds
and speed-limit gaps over half a second become unknown; zero limit is unpaired.
Unknown raw state 3 contributes unknown time but can bridge a <=0.5 s cancel
transition. Button frames require correct DLC, trailing byte, counter continuity
and CRC-8/SAE-J1850. A brake association uses the candidate `0x1FA` byte3 bit1,
stays explicitly unverified, and renders as `possible brake`; non-button cancels
are never automatically called brake cancels. Simultaneous evidence is separate.

`cruise_harvest.py` is a small local planner/controller, defaulting to plan-only.
It reads at most 20 completed trip rows, recorder metadata and compute results;
it never opens CAN or decompresses a capture. It uses only ended C-CAN campaigns
and manifest-complete full chunks whose sizes match. Each trip gets immutable
input metadata under `tmp/vehicle_data/cruise_summaries/manifests/`. Heavy work
is submitted solely to the proposed named task `cruise-summary-reduce`; state
in `jobs.json` prevents duplicate jobs, and changed input metadata requires
review. Completed results must match the exact trip and manifest hash before
atomic import to `trips/<id>.json`. No historian samples are rewritten.

The broker attaches at most five bounded result files during its history cache
refresh; ordinary cached GETs and the dashboard remain CAN-free. History shows
coverage, uncertain cancel attribution and absent evidence explicitly. Units
`systemd/van-cruise-harvest.{service,timer}` stage a file-only controller every
20 minutes, with network isolation and low I/O priority. The compute task
`cruise-summary-reduce` was registered in `.van-compute.json` on 2026-10-02 at the
owner's request, and the timer was installed and enabled the same day. Its first
run queued the backfill of older completed trips; results import on later runs.

Validation: 1,535 Python tests passed, 7 skipped, 1,374 subtests (named
`repo-tests` job `20260929T233425Z-5215d5af`); 443 dashboard tests passed, including
bundle budgets. The dashboard is built/deployed. Chromium at 800×1096 and
800×1280 verified the page-1 ACC footer with all ten tiles and no scroll; a
synthetic History summary rendered its coverage and uncertain attribution.
Core size is 84,628/92,160 B and the shared warning chunk 46,069/46,080 B.
Unit syntax passed `systemd-analyze verify`.

The backend history attachment has been live since the broker restart of
2026-10-02 02:25 MDT. The earlier blocker was an agent sandbox that refused sudo;
the guarded command, for future broker changes, is:

```bash
cd /home/pi/dev/obd-things && bash tmp/vehicle_data/restart-broker-parked.sh
```

It refuses while awake. No inhibit was ended, no helper request was changed,
and no CAN traffic was sent for this work. Trips 67, 70, 71 and 72 were reduced
on 2026-10-02 (trip 67 from 15 full chunks, 213,123,565 compressed bytes) and
appear under History with their coverage and cancel attribution.

```bash
# Metadata-only plan for one trip; does not scan/decompress a saved capture:
python3 projects/vehicle_data/cruise_harvest.py --trip 67
# After the named task is approved/registered, submit or import its existing job:
python3 projects/vehicle_data/cruise_harvest.py --trip 67 --execute
```

After task approval/registration, the proposed timer can be installed from the
ordinary owner shell with:

```bash
sudo install -m 0644 projects/vehicle_data/systemd/van-cruise-harvest.service projects/vehicle_data/systemd/van-cruise-harvest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now van-cruise-harvest.timer
```

The next-drive [braking callout sheet](../radar/findings/2026-09-29_acc_braking_callouts.md)
keeps `0x4AF` B3 bit2 at Tier 1. The separate
[lead-range read proposal](../radar/findings/2026-09-29_lead_vehicle_read_plan.md)
contains exact candidates, rates, parked prerequisites and requested approval;
it authorizes no transmission and changes no existing live gate.

## Local API

The HTTP implementation is split by responsibility: `api_server.py` owns the
bounded concurrent-read Unix server and main-thread POST executor, `api_client.py`
owns `TelemetryClient`, and `api.py` re-exports the existing imports (including
`_prepare_socket_path`). Shared wire
constants, listener flags, broker-unavailable envelopes, route matching and SSE
transport live in `http_common.py`. `DtcWebController` lives in `lib/dtc_web.py`
and remains importable from `web.py`; the optional legacy arm path is retained.
`api_schema.py` documents the existing `/v1` payloads with TypedDicts only, with
no runtime validation or changes to serialization. Contract regressions live in
`tests/test_vehicle_data_http_contract.py` alongside the existing HTTP tests.
`web_v2.py` remains the service entrypoint; neither unit arguments nor origin
checks change. The full `/v1/stream` still omits `bind` from its flags, whereas
the dashboard snapshot and lite stream keep their sorted flags and build id.

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
     {"mode":"wake_if_asleep"}  # local Unix API only; fixed B-CAN profile
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
The Unix acquisition handler accepts `wake_if_asleep` only for
`battery.voltage`. `web.py` continues to accept/proxy only `mode=passive`, even
with its passive acquisition proxy enabled by default. The
dashboard/manual voltage-check wrapper passes `--passive-only`, so it cannot
indirectly select the wake mode through `voltage_mon.py`.

The broker Unix API has no raw-frame, arbitrary-DID, diagnostic-session,
live-DTC-scan, DTC-clear, reset, calibration, configuration, or PROXI endpoint.
Its DTC GET reads only the atomic JSON cache and cannot open SocketCAN. History
and health likewise read only SQLite. The optional Tailscale web job boundary
is separate: it can place only one explicitly confirmed, closed-schema request for the
non-networked fixed batch worker and expose bounded status/cancel operations.
The ordinary LAN listener does not expose those actions.

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
The dashboard client (`dashboard/src/link.js`) establishes an instance with a
cache-bypassing HTTP snapshot, accepts only newer events from that instance,
maps the web process's monotonic clock onto its own with a bounded HTTP round
trip, rejects queued stream events that are over ten seconds old or would carry
a metric or verified vehicle state past its freshness window, resyncs on a
stalled or errored stream, and invalidates its cache on page hide, restore or
visibility change. Stream age never depends on wall time, so an NTP clock step
cannot make queued data younger. The numbered rules and their tests are in
[`dashboard/docs/freshness-contract.md`](dashboard/docs/freshness-contract.md).
Missing, nonnumeric, or negative ages are never driver-qualified.

History, early-warning, and saved DTC payloads are intentionally absent from
the one-hertz snapshot/SSE response. The dashboard fetches them as one trimmed
`/v2/summary` bundle after every resync and then every 60 seconds while the
page is visible; `/v1/history`, `/v1/health` and `/v1/diagnostics/dtcs` remain
available to other clients. This prevents the compact but substantially larger diagnostic cache
from being duplicated into every live telemetry event.

Those dedicated GETs are also memory-only at the Unix API. The broker primes
their cache before opening its listener, refreshes it once per minute from the
existing history thread, and exposes refresh state in
`status.supplemental_cache`. SQLite history/health queries and DTC-file reads
therefore stay off HTTP workers. This boundary was added after
two simultaneous dashboard clients caused repeat 5–10 second snapshot 503
bursts whenever their history/health/DTC refreshes overlapped; the browser's
five-second freshness fail-safe correctly rendered `Unknown`, but the source
telemetry had remained fresh throughout.

Vehicle-state evidence is ordered by authority as well as recency. Fresh
verified `0x0FC` RPM or `0x2EF` ignition evidence cannot be downgraded by a
later generic battery/bus-activity observation. Generic activity may establish
`Awake` only after the stronger metric's registered freshness window expires.
This prevents an otherwise healthy running state from oscillating through the
weaker activity state between collector operations; it does not extend any
freshness limit.

The saved DTC cache was originally seeded from the 19 existing inventory
reports. It contains dated evidence, not a current scan: 11 modules have a
successful saved result, PCM is explicitly unavailable after its generic
request timeout, and the four CAN-CH modules are explicitly never scanned by
the repository reader. Status `0x40` means test-not-completed-only and is shown
separately rather than counted as a fault. Generate or inspect an offline plan
without CAN access using:

The cache-only response enriches each returned record with a reporting-module-
scoped description from `dtc_descriptions.py`. Most current entries are short,
reviewed titles from the local exact-vehicle 2022 OEM service corpus; RF Hub
entries use the stronger vehicle-specific TPMS findings where appropriate.
When an exact component meaning has not been reviewed, the UI says so and
shows only the standardized failure subtype (for example, “signal stuck
high”). It never guesses from a code used by a different ECU. These titles
explain code definitions only: they are not diagnoses, live scans, or proof
that a saved condition is still present.

```bash
python3 tools/dtc_scan.py --resolve-runtime
```

`--resolve-runtime` reads sysfs identities only. Completed
`tools/dtc_inventory.py` JSON reports can be previewed with repeated
`--import-report`; adding `--commit` updates the SQLite history and atomic
dashboard cache without any CAN I/O. There is intentionally no `--execute` in
this multi-module planning/import tool. Runtime route annotation is per bus: a
missing role is marked unresolved without suppressing plan rows for independently
resolved roles.

The reviewed multi-module live worker is also dry-run by default:

```bash
python3 tools/dtc_batch.py --json
```

Its exact execution, restoration, report-import, cancellation, and guarded web
authorization contracts are documented in
[`docs/dtc-batch-worker.md`](../../docs/dtc-batch-worker.md). The Tailscale UI
does not run CAN inside an HTTP thread: it writes a mode-0600 fixed request
under `/run`, and `van-dtc-batch.path` starts the separate non-networked
oneshot worker. As requested on September 20, the browser no longer needs a
locally generated token. The exact configured Origin, explicit confirmations,
single-job exclusion, sticky restoration-failure block and every worker-side
live state/identity/transport check remain. Legacy arm-token support and the
`--dtc-arm-file` web option have been removed. A start body carrying `token`
is now rejected with HTTP 409 (`dtc_job_rejected`, exact-schema error), without
queuing a job; clients must send only the three true confirmations. Rebuild
`dashboard/dist` and restart both web listeners when deploying this removal.
No DTC scan was run while implementing this change.

The one-module reader remains useful for an explicitly scoped investigation
and is dry-run by default:

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

For a manual cache-only dashboard trial on a spare loopback port (build
`dashboard/dist` first; see "Telemetry dashboard" above):

```bash
python3 projects/vehicle_data/web_v2.py --bind 127.0.0.1 --port 8799 --cache-only
```

The dashboard uses server-sent events, but every stream update is still made
from broker GET endpoints and cannot trigger CAN traffic. Bind defaults to
loopback; remote access normally belongs behind an authenticated proxy. A
deliberately trusted interface can instead be selected explicitly with
`--bind <interface-address> --allow-remote-bind`. This opt-in does not add
authentication. Bind to one intended interface address and use `--cache-only`
if browser-requested passive voltage reads are not wanted; avoid a wildcard
bind unless another layer restricts clients.

Active-drive host-latency handling keeps the 250 ms RPM-evidence permit
boundary strict without turning ordinary scheduler delay into an epoch-wide
failure. If that exact evidence snapshot ages out before permit issuance, or
the issued permit expires when the fixed transport consumes it immediately
before `send()`, the helper sends nothing, preserves its exclusive owner,
waits for the normal one-hertz boundary, and collects wholly new running
evidence. The latter has its own exception/result type so it cannot conceal a
wrong purpose, channel, process, lock, or clock failure. Lost locks,
invalid/foreign clocks, bad snapshots, post-consume transport/response faults,
and restoration faults remain fatal. The B-CAN companion separately retries
only missing startup signature evidence for six bounded attempts; identity,
topology, controller, inhibit, and later in-session signature failures still
fail immediately. Broker blocked-state status retains the helper's original
terminal detail instead of replacing it with only the recovery condition.

## Current vanpi deployment

### Mileage and oil-change records (2026-09-16)

The Service card (Parked and Health views) exposes the existing candidate
`vehicle.odometer` ICS feed and its roughly 11-mile recorded discrepancy from
the cluster. Last-known mileage retains its original observation timestamp
while parked and across broker restarts. Startup may recover one dated,
previously fresh ICS sample through the historian's indexed latest-sample
lookup. It is never injected into the live metric cache as a new observation.

`/v1/maintenance` reads an in-memory service journal.
`POST /v1/maintenance/oil-changes` accepts only date, optional miles, mileage
source, notes, and an idempotency request ID. The web boundary requires
same-origin JSON; the broker validates the closed schema and commits the
record atomically before acknowledging it. Service history is append-only and
persists in `maintenance.json` beside the configured history database
(`/var/lib/van-telemetry/maintenance.json` on vanpi), shared by all browsers.
The history thread flushes latest mileage at its supplemental-refresh cadence.
Malformed existing storage is preserved and blocks journal writes. This
endpoint does not grant CAN access or reset any vehicle indicator.

Oil-life percentage remains explicitly unavailable: an AlfaOBD status label
was observed, but its exact current-vehicle DID/scale has not been established.
See the [service-dashboard evidence review](../ecu_mapping/findings/promaster_2022/2026-09-16_service_dashboard_evidence.md).
Last oil-change data must be entered by the owner; old status logs are not
treated as service records. Date is required, mileage is optional, and the
form distinguishes cluster/receipt mileage from ICS estimates. Distance since
service is calculated only for the same ICS mileage basis.

Deployment verified 2026-09-16 MDT: memory API and shared form are live on LAN
and Tailscale. Startup recovered 53,725.617 mi from the dated September 15 ICS
historian observation. Browser verification shows 53,725.6 mi with its date
and "not live", an enabled save form, and no fabricated oil-change entry.
Portrait 800x1280 and landscape 1280x800 have no horizontal overflow or JS
errors. Remote job `20260917T011211Z-857b3e15` passed 144 tests and 53 subtests;
final maintenance job `20260917T011318Z-105ecf05` passed 16 tests and 7 subtests.
Asleep deployment added no CAN TX; all roles stayed passive/error-free.

### Radar alignment dashboard (2026-09-16)

The dashboard shows radar alignment on a Drive page 2 tile and the System
view's radar card. The broker provides latest elevation/azimuth, sample means
over the trailing 60 and 300 seconds, sample count/time coverage, absolute five-minute peak, and
distance to the owner's +/-1 degree monitoring reference. At 0.8 degree it
shows an approaching-reference indication; at 1 degree it shows outside the
reference. These are display bands, not verified OEM fault thresholds or proof
that ACC/FCW is correctly calibrated. Source/scaling remain **candidate**:
the radar project's `0845` signed i32 pair /1e6 interpretation is inferred.
`0841` instantaneous pitch is deliberately not used for alignment monitoring.

`radar_alignment.py` implements only physical `22 0845` plus one fixed ISO-TP
FlowControl for an exact 11-byte `62 08 45` reply. The optional helper path uses
new running evidence and independent 250 ms permits for each send, at most one
read per ten seconds. Session changes, TesterPresent and calibration actions
are absent. A radar response failure disables radar reads for that epoch while
PCM/TPMS continue; owner/permit safety failures retain fatal cleanup behavior.

**Parked read support verified 2026-09-16 MDT:** `22 08 45` returned exact
`62 08 45 00 01 65 91 00 01 6E C3`, elevation +0.091537° and azimuth
+0.093891° under the existing inferred scale, without a session change.
The scoped owner verified passive restoration; evidence is recorded in the
radar DID map and `tmp/radar/dashboard-support-20260917.txt`. The helper's
commissioning flag is now enabled. Its full running cadence still requires
validation during an owner-controlled engine-running interval.

Deployment verified 2026-09-16 MDT: broker and LAN/Tailscale listeners restarted
with idle helpers and verified passive roles. Both angle metrics and bounded
summaries are exposed by the live snapshot. The support read added exactly two
C-CAN TX packets (request plus FlowControl); the subsequent restart added none.
All vehicle roles remained listen-only/ERROR-ACTIVE with zero RX/TX errors.
Focused final remote job `20260917T004133Z-1d77e2d8` passed 79 tests and 55
subtests. The one-off parked reading was not injected as live dashboard data.

Rolling summaries count new observations in the broker. As of the September 20
retention update, each window ends at its latest radar observation and freezes
there while parked. The last reading, means, peaks, sample counts and original
timestamp remain visible with a "LAST RECORDED · NOT LIVE" badge. New samples
after a gap over 30 seconds start new averaging windows. Live freshness gates
remain unchanged, and retained values never enter the live metric cache.
The small `radar-alignment.json` file beside the history database retains these
summaries across restarts; the history thread flushes changed summaries. A
bounded indexed startup query can recover the final five-minute window from
existing historian observations, deduplicating repeated cached samples.
Raw averaging history is bounded to five minutes/512 observations per axis.
Dashboard GETs perform no SQL, disk write, or diagnostic request.

Activation verified September 20 via the live snapshot: the broker restarted
outside this managed session at 20:22:54 UTC. Retained radar summaries and
last readings are now exposed with no reported storage errors. This verifies
historical recovery, not a new active radar polling interval.

The former static frontend's layout registry, customiser, SocketCAN/TPMS/
battery tiles and DTC-history disclosure were ported into `dashboard/src`
(customiser, System buses card, tire grid, Parked battery card, Health codes
card) and are specified in `dashboard/docs/design.md`. One backend fact from
that era still matters: `status.current_owner` becomes `broker` during an
in-flight operation and null between reads; it is not a service-ownership
indicator. Confirmed DTC history older than one calendar month (UTC, month-end
clamped) is collapsed by default; this is presentation only.

### Last-recorded display policy (September 20 audit)

Open early-warning episodes stay visible as unresolved advisories even when
the latest assessment lacks fresh evidence; this does not promote that
unavailable assessment to a live warning. Recovered sample-filter incidents are
kept apart from open items. Warning cards offer the read-only local Codex
explanation dialog when the web process advertises it; see
[warning chat deployment and boundaries](docs/warning-chat.md).

`RETAIN_LAST_READING` in `dashboard/src/store.js` is the single presentation
policy for dated last values. Everything not explicitly included is live-only,
including new metrics until reviewed; `RETAIN_CANDIDATE_ESTIMATES` names the
only candidate-quality metrics that may be retained. Whole-mile odometer
display truncates, never rounds, without changing stored precision. These are
presentation rules, not freshness or admission changes; the owner-facing
wording is in `dashboard/docs/caveats.md`.

| Data | Parked/stale display |
| --- | --- |
| Voltage, odometer, four tire pressures | Last accepted reading, original date, not live |
| Coolant, VVT oil and transmission temperatures | Last accepted temperature, original date, not current temperature |
| Radar elevation/azimuth | Last estimate; existing separately retained averages/coverage where available |
| Oil life remaining | Retain a valid registered percent reading when a source exists; currently unmapped, so no invented value |
| Speed, RPM, gear, ignition/running state | Fresh only; no historical value promoted as current state |
| Oil pressure, torque/power/target torque, generator duty, shaft speeds | Fresh only; standalone historical values lack current operating context |
| Diagnostic raw bytes | Fresh only in diagnostic cards; historical evidence remains in the historian |
| Oil-change journal, DTC history, trip summaries | Already dated records; their existing historical presentation is unchanged |

Historical values require a finite, in-range number, matching unit, registered
source and quality, and valid non-future observation timestamp. Normal
driver-facing retained values require verified or observed-Alfa-scale quality.
Only the existing odometer and radar estimates permit candidate quality, with
their original provenance unchanged. Retention never upgrades quality, live
counts, engine-state evidence, alarms, acquisition permissions or sample age.
The oil-life display now supports live/retained `engine.oil_life_remaining`
**only if that metric is actually registered** with a qualified percent source;
it is not added to the acquisition registry by this UI change. Historical
AlfaOBD 17% is not imported or inferred from the owner service journal.

`last_readings.py` maintains a separate bounded last-accepted observation per
registry metric, updated only after successful source/value/plausibility
admission. Current unavailable responses retain their failure reason and
`available=false`; an optional `last_recorded` object carries historical data
without live availability/age flags. GETs only copy memory. The normal history
worker flushes dirty `last-readings.json` beside the history database every
history cycle; clean shutdown also flushes. With history disabled, periodic
flushing is unavailable and persistence occurs only on clean shutdown.
Startup uses one indexed fresh-only `latest_sample` lookup per metric to
recover existing evidence without new CAN reads. The historian's
`metric_samples_fresh_latest` index bounds these lookups. Failures report via
`status.last_readings.storage_error` and Collector Health; they do not stop
live collection. Recovered values never enter the live cache.

Deployment verified September 20: an externally performed broker restart at
20:22:54 UTC activated durable retention. The live snapshot reports persistent
last-readings storage with no error, recovered VVT oil temperature and radar
history are displayed, and the LAN web listener advertises passive voltage
acquisition enabled. This agent did not perform the restart or any CAN work.

Pre-drive investigation, 2026-09-06: an
[offline reproduction](../ecu_mapping/findings/promaster_2022/2026-09-06_broker_topology_refresh_diagnosis.md)
identified a separate source of false recorder ownership loss. A scheduled
voltage acquisition refresh sets `topology.usable` from the role's
`passive_ready` flag; legitimate broker-owned armed mode therefore makes that
topology false even while the helper and running evidence remain valid. The
recorder then rejects the refreshed status. This classification defect is
reproduced but not yet repaired; it is distinct from the already-deployed
exception-recovery fix below. No service or CAN state changed during diagnosis.

Live validation, 2026-09-06 13:47 MDT: the parked, ignition-on/engine-off check
returned `7F 22 12` for `F45C` and `62 06 9F 60` (89.6 °F) for `069F`, with
no session change. Exactly two C-CAN requests were sent; all roles returned
exact passive/ERROR-ACTIVE with zero errors, no inhibit, and unchanged
B-CAN/CAN-CH TX counts. The preceding stationary engine-running capture
independently contains 13 matching `069F` pairs, 75.2–80.6 °F, at 5.046–6.077
second intervals. The VVT metric became unavailable immediately after its
owner stopped. The manual ownership handoff also live-validated recorder
recovery: all raw role streams finalized with zero detected drops, the
capture-set wrapper remained incomplete, and the same daemon returned to
waiting with zero restarts. No service change or new deployment was needed.
Full [support and recovery evidence](../ecu_mapping/findings/promaster_2022/2026-09-05_drive_inventory_oil_candidates.md#september-6-live-support-and-recorder-recovery)
completes the pending validation below; `F45C` remains excluded from polling.

Deployment update, 2026-09-05 02:42 MDT: `3cc8c4f` repaired capture-error
propagation and deployed the fixed optional `069F` profile, broker metric,
history entry, and explicitly labeled VVT temperature card. Broker and recorder
are active/enabled with zero restarts; recorder is waiting with no `candump`
child, collector/historian are advancing, and both web listeners are active.
All three roles remained exact passive/ERROR-ACTIVE without an inhibit, and
TX stayed at 38,567/1,246/0. The van was asleep, so this deployment sent no
CAN frame and does not establish direct no-session `069F` support. The bounded
`F45C`/`069F` parked checker is prepared but awaits ignition-on/engine-off.
Focused compute job `20260905T083650Z-daae21e3` passed 132 tests and 76
subtests, including real capture cleanup and optional temperature cadence /
failure isolation. A broader pass had 211 successes and seven known
Pi-sandbox read-only-lock-path failures; it is not a full-suite pass. A real
Firefox render check showed 204.8 °F for a synthetic fresh VVT sample and a
dash at 16-second staleness, with no 800-pixel portrait overflow. Those
synthetic values were confined to the isolated browser and never published to
the broker or historian.

Deployment update, 2026-08-31 01:22 MDT: commit `bc8c583` bounded the drive
recorder's broker-status recovery and is pushed to `origin/master`. The failed
unit's 46-restart start-limit was cleared after the broker reported the van
asleep, no current active owner, both helpers idle/listen-only, and all three
roles exact passive. The recorder is active with zero restarts and reports
`waiting for reviewed broker active-drive ownership`; it has no `candump`
child while parked. C-CAN/B-CAN/CAN-CH stayed listen-only, ERROR-ACTIVE, and
error-free with unchanged TX counts 36,802/2,485/0, so recovery sent no CAN
frame. Focused remote validation passed 23 tests; broader recorder validation
passed 39 tests and seven subtests. The bounded branch still awaits natural
exercise during a future broker-owned drive interval.

Deployment update, 2026-08-30 23:18 MDT: the vehicle-state precedence and
supplemental-cache repair are live. Both restart gates observed the van asleep,
helpers idle/listen-only with no PID or restoration fault, and all three roles
exact passive. The broker primed history/health/DTC products in 1.09 seconds
before serving, then completed its first scheduled minute refresh while every
five-second probe snapshot returned HTTP 200. A deliberately excessive
15-way burst returned 125/125 HTTP 200 responses: 50 snapshots and 25 each for
history, health, and DTCs. Snapshot maximum latency was 2.32 seconds under that
burst and there were no 503s after increasing the bounded Unix accept backlog;
ordinary dashboard concurrency is substantially lower. Broker, LAN, and
Tailscale services are active with zero restarts, collector/historian are
advancing without error, and the supplemental cache is `ready` without error.
C-CAN/B-CAN/CAN-CH remain listen-only, ERROR-ACTIVE, and error-free. TX counts
were unchanged at 36,802/2,485/0, so deployment transmitted nothing. Remote
focused validation passed 210 tests and 77 subtests with only the already-known
stale dashboard-copy assertion excluded.

Deployment update, 2026-08-30 20:58 MDT: the consume-time permit-expiry
repair and module-scoped DTC descriptions are live. The restart gate observed
zero RPM, cleared/idle C-CAN and B-CAN helpers, exact passive interfaces, and
no restoration fault; residual post-key-off traffic still classified the van
as awake. After restart, broker plus LAN/Tailscale listeners are active with
zero restarts, both helpers remain idle/listen-only without a PID or fault,
and the collector and five-second historian are advancing without error. All
three vehicle roles remain classical CAN, listen-only, `restart-ms 0`, and
ERROR-ACTIVE with zero error/drop counters. TX packet counts were unchanged at
23,779/2,480/0 for C-CAN/B-CAN/CAN-CH, so deployment transmitted nothing. Both
web endpoints return the new cache-only DTC catalog: 53 of 55 displayed
module/code pairs have reviewed meanings; the two unresolved pairs disclose
only their standardized failure subtype. Remote focused validation passed 94
tests and 56 subtests. The no-TX consume-expiry retry still requires its first
engine-running live validation.

Deployment update, 2026-08-30: the host-latency/helper-detail repair and tablet
layout are live. Deployment occurred with the vehicle passively confirmed
asleep; the broker, LAN/Tailscale listeners, and three-role recorder are active
with zero restarts. C-CAN, B-CAN, and CAN-CH remain exact classical CAN,
listen-only, `restart-ms 0`, and ERROR-ACTIVE; restart TX deltas were exactly
`0/0/0` and current RX/TX error counters are zero. Both active helpers are idle
with no PID or restoration fault, the recorder is waiting, and SQLite
maintenance completed before both web endpoints returned HTTP 200. The focused
helper suite passed 76 tests and 52 subtests; the broad portable suite passed
1,093 tests and 710 subtests with four skips and only the known stale dashboard
copy assertion deliberately deselected. A later engine-running/top-of-hour
interval is still required to live-validate the new no-TX skipped-cycle path.

Deployment update, 2026-08-28: commit `d14d90a` installed the fixed B-CAN ICS
helper, starred candidate odometer card, broker auxiliary supervision, and
armed-B-CAN recorder companion. `van-telemetry` and `van-drive-recorder` were
restarted while the vehicle was asleep; both are active/enabled with zero
restarts. Broker status reports `auxiliary_drive.enabled=true`, idle,
listen-only, no helper PID or restoration fault, and the catalog exposes only
candidate-quality `vehicle.odometer` from `ics.did.2001`. All three vehicle
roles read back classical, listen-only, ERROR-ACTIVE, `restart-ms 0`, with zero
errors/drops and no operation inhibit. The LAN listener served the new
`ODOMETER*` card and discrepancy disclosure. No new CAN frame was transmitted
during deployment; the raw-CAN production helper and recorder-companion path
still require their first qualified engine-running validation.

Deployment update, 2026-08-21: the tracked role-aware
`van-telemetry.service` is installed, enabled from `multi-user.target`, and
active. Passive commissioning resolved C-CAN as `can0` at 500 kbit/s, B-CAN as
`can1` at 125 kbit/s, and CAN CH as `can2` at 500 kbit/s. All three read back as
classical CAN, FD off, listen-only, `restart-ms 0`, and ERROR-ACTIVE; the
unconnected spare was `can3` and remained down. TX and error counters were
zero. Those netdev names are the observed commissioning snapshot only and must
still be re-resolved from serial plus `dev_id` after any USB/hub change.

The telemetry web path is active over the configured LAN/Tailscale access,
`/api/telemetry-summary` reports the service running, and the SQLite historian
is writing. LAN remains cache-only. The Tailscale listener advertises the
locally armed fixed DTC job boundary; `van-dtc-batch.path` is active and its
oneshot worker is inactive until an exact request is queued. The temporary
commissioning override was removed, so the normal service has active-drive
enabled; because the vehicle remained asleep, the helper stayed idle and TX
remained zero during this passive deployment check. This run validates the
passive reconciler, web path, and historian, not a DTC batch.

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

The 2026-08-21 advisory/diagnostic deployment added the receive-only USB
kobject monitor, persisted warning episodes and data-quality events, queued
ntfy delivery, ECU-estimated crankshaft power, the raw `0x1F7` plausibility
gate, and the fixed DTC worker boundary. Post-restart validation found and
fixed one empty-aggregate history query exposed by the newly registered power
metric. The final `/v1/history` response is current with ten bounded trends and
one prior stationary trip; `/v1/health` reports no active episode, no USB or
data-quality incident, zero pending/failed notification rows, and the ntfy sink
enabled without error. The USB monitor is running with both expected serials,
five learned ancestor hubs, and no observed event. All three vehicle roles
remain listen-only/ERROR-ACTIVE with zero errors; TX counters remain
438/0/0, unchanged from stationary commissioning. No DTC request or test
notification was sent during deployment.

A 2026-08-24 historian performance repair retained the five-second raw
snapshot, rollup, advisory, persistence, and notification cadence while adding
two partial SQLite indexes for the actual hot queries. One serves newest-fresh
metric lookup without walking every newer stale cache copy; the other covers
the complete observation identity used to deduplicate rollups. Before the
repair, a parked 25-second sample measured the broker at 42.48% average CPU
with repeated 80–99% one-core bursts and 63.82 KiB/s writes. After additive
index migration and restart, the same sample measured 9.59% average CPU, a
30% maximum one-second sample, 20.62 KiB/s writes, and zero I/O delay. Both web
listeners returned HTTP 200, historian state remained running without error,
SQLite `quick_check` returned `ok`, and query plans selected the new indexes.
No CAN acquisition behavior, evidence rule, sampling interval, or notification
latency was relaxed.

The installed role-aware `van-drive-recorder.service` is enabled and active,
waiting receive-only for broker-owned active-drive intervals. `tpms-logger`,
`promaster-bcan-recorder`, `promaster-mapping-drive`, `tpms-drivesniff`, and the
manual `can-three-bus-capture` service remain disabled/inactive. The old B-CAN
recorder unit/enablement is retired; none of those disabled campaign services
should be inferred to have run during broker commissioning.

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
classical-CAN `type can bitrate {125000|500000} fd off listen-only on one-shot
off restart-ms 0` form, and `ip link set dev <canN> up`; the active C-CAN owner also
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
or direct COP ALERT RF-Hub wake. That boundary remains in force. COP ALERT's
exterior-light/notification manager owns only its runtime marker and continues
to pause lights from the ignition-monitor marker; the dashboard's remaining
vehicle-data integration is the cache-only telemetry HTTP endpoint.

The role-aware replacement is the tracked
`van-cop-can-wake.service`. It observes the marker from a separate process and
can perform only the fixed C-CAN RF Hub wake described above. The CLI is
plan-only without both `--execute` and `--confirm-fixed-c-can-wake`. On
2026-08-24 the unit was installed, enabled, and started with COP off and both
markers absent. It reported `idle`, its wake count remained zero, all three
vehicle roles stayed passive, and every CAN TX/error counter stayed zero. The
shared C-CAN transaction was then live-validated with a separate temporary
marker, leaving the dashboard's COP state, lights, and notifications untouched:
ten ONE-SHOT `22 FEFF` frames woke a silent C-CAN, one normally acknowledged
validation read returned the exact positive DID echo, and the owner restored
listen-only/ONE-SHOT-off/`restart-ms 0`/ERROR-ACTIVE with zero errors or
inhibits. The installed supervisor uses that same transaction when its real COP
marker is active; its channel-free state is
`/run/van-cop-can-wake/status.json`.

That status file is the read-only dashboard integration contract. It is a
private, atomic, bounded JSON object and exposes no channel or transmission
parameters. Current state is one of `starting`, `idle`, `arming_delay`,
`paused_ignition`, `blocked`, `waking`, `active_waiting`, `stopping`, or
`stopped`. `marker_active` remains requested intent; `transaction_in_progress`,
`wake_count`, `last_attempt_at`, and `last_success_at` report execution.
`next_attempt_at` reports a scheduled retry/refresh. The persistent
`last_blocked_at`, `last_blocked_reason`, and `last_blocked_detail` survive
marker removal, so turning COP off no longer destroys the reason an attempted
activation did not transmit. The dashboard must sanitize and proxy only these
fields; it must never write this file or invoke CAN directly.
`last_transaction_seconds` reports the most recent bounded attempt duration;
`success_cadence_basis=attempt_start` means the next successful refresh is due
15 seconds from the prior attempt start, not 15 seconds after its restoration.
The outer broker-state gate uses the registered five-second ignition/RPM
freshness window. This covers the measured roughly 4.1-second gap between
vehicle-state updates in a full multi-role collector cycle without weakening
the wake core's fresh 0x2EF/0x0FC checks at every send boundary.

This behavior was corrected after the first real button trial on 2026-08-25.
The dashboard posted ON/OFF at `00:47:29/32` and again at `00:47:33/39`.
The first interval ended at the old three-second marker threshold; the second
could perform only one preflight check before disarm. No CAN attempt occurred
(`wake_count=0`, `last_attempt_at=null`, C-CAN TX zero), and the old idle branch
then erased the transient block reason. The explicit-activation debounce, fast
pre-TX retry, persistent block fields, and transition journal above are the
direct remediation. A service restart with COP off was subsequently verified
to remain idle and leave C-CAN TX at zero.

A longer live run on 2026-08-25 then exposed unfair scheduling: the broker's
two receive-side C-CAN leases repeatedly coincided with each 15-second COP due
time. Successful transactions themselves took about seven seconds, and the old
completion-based schedule added another 15; shared-lock collisions and
five-second generic retries extended some success intervals to 33 seconds. The
per-role shared/exclusive scheduling handoff and attempt-start cadence described
above directly address both causes while preserving every authoritative lock
and fail-closed gate.

The first deployment replaced role-lock `can_busy`/five-second retries with
roughly 500 ms scheduling retries and brought successive success intervals down
to 15.522, 15.998, 16.561, 15.472, and 15.501 seconds. The final
writer-preference gate removed even that probabilistic retry: four consecutive
live attempts started 15.004, 15.003, and 15.005 seconds apart, completed
successfully 14.805, 14.774, and 14.903 seconds apart, and recorded no blocked
transition. No post-deployment authoritative role-lock collision occurred.
C-CAN returned after every attempt as classical listen-only, ONE-SHOT off,
`restart-ms 0`, ERROR-ACTIVE, with zero errors and no inhibit. The broker
collector remained running.

[History: Historical pre-migration deployment record](../../docs/history/vehicle-data-changelog.md#historical-pre-migration-deployment-record)

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

`projects/battery/voltage_mon.py` uses only the authoritative broker. If its
Unix socket is absent, invalid, or unreachable, the monitor fails closed and
never creates an independent active owner. Read-only `crontab -l` on 2026-08-20
showed `projects/battery/voltage_mon.sh` scheduled every two hours from 10:00
through 22:00; that coarse cadence remains outside the broker's 15-minute
minimum. Scheduled/default runs request `wake_if_asleep`; already-awake C-CAN
or B-CAN is still read passively first. `--passive-only` is the web/manual-safe
path and never wakes. Sampling and CSV history continue when ntfy is absent or
unreachable, but notification state is left unchanged so an undelivered low or
recovery edge is not consumed. `--no-notify` likewise samples/logs without
mutating alert state.

Since 2026-10-04 the monitor first reads `/v1/status` and skips the tick (exit 0,
no CSV row, alert state untouched) when `vehicle_state.running` is true: the
broker already records running voltage passively. Unknown or unreachable status
keeps the old behaviour. The drive recorder's idle wait retains its 20 s status
timeout (in-interval checks keep 2 s). It originally removed the two-hourly
"broker status unavailable … TimeoutError" lines caused by the serialized Unix
API. O2's concurrent cache reads remove that ordinary acquisition-induced delay,
but not overload, host stalls or component-lock waits. Both the idle timeout and
the running-voltage skip deliberately remain; neither is globally redundant.

The broker also retains one explicit engine-off baseline without transmitting.
After a qualified running epoch ends with exact helper restoration, it observes
the ordinary passive voltage tail for 30 seconds and atomically saves the newest
fresh verified sample as `engine-off-voltage.json` beside the configured history
database. A restarted engine cancels the pending capture. If the bus sleeps
before a passive sample appears, the event records no replacement and never
falls back to a wake. `status.engine_off_voltage` exposes the bounded capture
state; the van dashboard compares the saved sample timestamp with scheduled
`voltage_mon` history and displays whichever is newer.

## Validation

Full portable regression on 2026-09-18 passed **1,168 tests, 4 skips, and
773 subtests** on m4mac (`van_compute` job `20260918T171501Z-05fe09a6`),
with no test-selection exclusions. On 2026-09-27 the former static
frontend's asset tests were removed with it; `test_base_listener_serves_no_dashboard_page`
now checks that `web.py` alone serves only JSON (404 with CSP for `/`), and
the dashboard is covered by `tests/test_web_v2.py` and the node suite
(`npm test`, wrapped by `tests/test_dashboard_v2_node.py`).

Offline tests use fake interfaces, locks, sources, and clocks. They cover
cache-only GETs, strict local publication, typed values, source-metadata
allowlists, broker-stamped age, passive acquisition, silent-bus handling,
coalescing and rate limits,
Unix API behavior, the registry-driven snapshot, evidence-qualified vehicle
state, the dashboard listener and client, cache-only web defaults, exact broker-owned
recorder admission, initial-ignition timeout, and persistent-candump command
construction.

The role-aware broker is deployed and its passive three-bus reconciliation was
live-CAN validated on 2026-08-21. The B-CAN auxiliary implementation was
deployed asleep on 2026-08-28 and therefore remained idle. Service health and
passive validation are not evidence of a successful active acquisition;
inspect metric provenance and quality on every available observation.

The 2026-08-24 wake deployment added hardware-specific active validation. The
scheduled path first exposed that these `gs_usb` controllers reject automatic
bus-off restart; that failed arm sent zero frames and restored B-CAN exactly.
With explicit `restart-ms 0`, the same broker/cron path sent the fixed 75-frame
B-CAN burst, returned verified `0x46C=12.32 V`, and restored passive with zero
errors. A first C-CAN F190 experiment proved why PEAK-era automatic retry could
not be copied: it returned a response but left this controller ERROR-WARNING,
causing the restoration latch to block all active work. Exact Board A USB reset
recovered C-CAN/B-CAN, all roles were revalidated, and only then was the latch
retired. The final maintained C profile uses ONE-SHOT `22 FEFF` wake frames,
switches to normal retry only after C-CAN signatures appear, validates one exact
positive response, and restores ONE-SHOT off. A fully sleeping test completed
with eleven TX packets and zero errors/inhibits. These failures and recoveries
are part of the canonical hardware evidence, not successful-run noise to omit.
The latest portable regression run, including the scheduling handoff,
attempt-start cadence, and the drive-recorder constructor regression test,
passed 1,043 tests with 4 skips and 692 subtests (`van_compute` job
`20260825T211110Z-3a6a1c3c`); focused local wake/handoff/route/COP tests also
passed. The enabled `van-drive-recorder` service was reset and restarted after
that constructor fix and reached its expected
`waiting for reviewed broker active-drive ownership` state without opening a
CAN capture.
