# Broker API concurrency

## Deployment

- **Deployed SHA:** `21523ec8ac755bb714a32f7e5c0f13899389f9b9` (merged to master).
- **Previous master / rollback SHA:** `0c93cd2d2232792a905f8817f11e9358a324fd98`.
- Restart requested `2026-10-05T12:10:01.783068377Z`; systemd started the new
  broker at `12:10:02.910819Z`; broker instance `started_at` is
  **`2026-10-05T12:10:03.822520Z`**. Use that instance timestamp for first-drive checks.
- The prescribed parked/asleep script completed its 90-second verification at
  `2026-10-05T12:11:35.089218047Z`. No refusal/retry or rollback was needed.
- Only `van-telemetry.service` was explicitly restarted. Systemd's existing
  dependencies restarted both web units and reactivated the DTC path; no other
  unit was manually restarted. Recorder/COP PIDs stayed unchanged.
- O1 (`0c93cd2`) was already installed: both unit files matched byte-for-byte and
  `NeedDaemonReload=no`; no install or daemon reload was performed.

## Verification

- Fresh parked gates before each action: asleep, running=false, age <=5 s,
  advancing collector/role observations, idle helpers, no inhibits/foreign owner,
  exact USB serial/dev_id identities, healthy classical/listen-only roles, and
  unchanged RX/TX counters across a three-second quiet sample.
- Restart TX counters were unchanged: C `137242`, B `12866`, CH/spare `0`.
- Final quiet samples at `12:23:49.900610Z` and `12:23:52.936308Z` proved asleep,
  healthy/passive C/B/CH, spare down, and static TX: C `137242`, B `12941`, CH/spare `0`.
- Broker, LAN web, Tailscale web, recorder and COP units were all active with
  `NRestarts=0`. TPMS remained inactive. Status and snapshot both returned HTTP 200.
- No warning-priority entries or traceback/error matches after the new broker
  instance timestamp. The old PID's planned-stop KeyboardInterrupt traceback is
  expected; the recorder logged one informational missing-socket wait during
  startup, before the new instance timestamp, without opening CAN.
- Merge gates rerun: **1,761 Python tests, 1 skip, 2,787 subtests; 441 Node tests**;
  production build, HTTP contracts, pinned status hashes, and complete byte-identical
  broker/active-route/gate oracle captures passed (16 tools / 882 gate runs).

## Before / after latency

One identical Pi-local runner, direct HTTP (no proxy), one polling request at a
time, full response consumption, three-second warmup and three seconds after
child exit. It invoked exactly once per revision:

```bash
env -i HOME=/home/pi LOGNAME=pi USER=pi SHELL=/bin/sh PATH=/usr/bin:/bin /home/pi/dev/obd-things/projects/battery/voltage_mon.sh --no-notify
```

Nearest-rank percentiles; milliseconds. Primary table includes successful requests
whose **start** falls inside the wrapper command interval:

| Runtime | Samples | p50 | p95 | Max |
|---|---:|---:|---:|---:|
| Before (`0c93cd2`) | 58 | 8.948 | 22.709 | **7821.107** |
| After (`21523ec`) | 1007 | 7.527 | 11.466 | **20.590** |

- Execution-overlap window: before n=59, p50=9.092, p95=22.709, max=7821.107;
  after n=1008, p50=7.527, p95=11.466, max=20.590.
- Whole window: before n=781, p50=7.698, p95=12.853, max=7821.107;
  after n=1749, p50=7.528, p95=11.323, max=24.336.
- Longest successful-response gap: **7.821 s -> 0.0245 s**. All responses were 200.
  The maximum/gap reveals the legacy stall that p95 alone largely hides.
- Before command: `12:04:14.810924Z`–`12:04:23.267258Z` (8.456 s).
  After: `12:19:39.486834Z`–`12:19:47.813935Z` (8.327 s).
- Both commands exited 0, reported verified **wake-assisted B-CAN / 12.48 V**,
  and added exactly **75 B-CAN TX frames**, zero C/CH/spare TX. No battery retry,
  direct CAN socket, notification, or alert-state update was performed.
- Preserve the baseline runner's diagnostic: it returned its own code 3 because
  the immediate post-read collector timestamp was 10.45 s old. The wrapper itself
  succeeded; raw link evidence was passive/healthy, with the correct TX delta.
  Fresh, advancing, naturally-asleep quiet checks passed at `12:07:07`/`12:07:10`
  before any pull/restart. No gate was loosened and the baseline was not repeated.
  The post-deploy runner passed all its immediate checks (code 0), followed by
  the final naturally-asleep/quiet acceptance above.

## Review notes and remaining scope

- Fable cross-family review: **DEPLOY**, no must-fix findings (coordinator report).
- **Eight queued/running POSTs can starve GETs during a wake** (one active plus
  seven waiting occupies all eight slots). This is bounded and unlikely,
  not a priority guarantee. Retain the recorder's 20-second idle timeout and
  voltage-monitor running skip; neither is globally redundant.
- **GIL contention during CAN has not been isolated/measured directly on the Pi.**
  This one parked-wake workload proves end-to-end responsiveness, not long-drive
  CPU scheduling behavior or all saturation cases.
- First natural post-deploy drive remains to be checked. Latest trip at handoff
  was 76, ended `2026-10-05T00:34:39.516643+00:00`; there were no post-deploy trips.
  Do not start the engine or drive just to manufacture validation.
- 2026-10-07: three drives (ignition 13:00-14:15 MDT) produced no trips because the Pi's
  VL805 USB controller had died at 05:59 MDT (UAS write timeouts on the Seagate `mbp2tbkup`
  disk escalated to "HC died"), removing both gs_usb CAN adapters. The broker reported
  `asleep` (basis `passive_bus_silence`) with no adapters present. A guarded
  `safe_reboot.sh` at 14:24 restored USB/CAN; this was not a broker-code failure. The first
  natural post-deploy drive is still unchecked; use the same `--since` checker command.

## First-drive check (read-only)

From the Mac, run this single command after the next natural drive:

```bash
ssh pi@vanpi.lan 'python3 /home/pi/dev/obd-things/tmp/vehicle_data/o2-concurrency/first-drive-check.py --since 2026-10-05T12:10:03.822520Z'
```

The staged checker opens
`file:/var/lib/van-telemetry/history.sqlite3?mode=ro`, sets `PRAGMA query_only=ON`,
lists the latest ten `trips` rows and rows with `last_active_us >=` the deployment
checkpoint, and prints `no-post-deploy-trip-yet` if none qualify. Columns include
id, start/last-active/end times, start/end reasons and snapshot count.
It queries `van-*` journals since **`2026-10-05 12:10:03.822520 UTC`**, once at
warning priority and once grepping `Traceback|[A-Za-z]+Error:|[A-Za-z]+Exception:`.
Pi journalctl does not accept the ISO `T...Z` form directly; the checker converts it.
Inspect every nonzero command error; no matches is not a proof that a drive happened.
Any warning or non-known-LAN-ConnectionResetError traceback needs investigation.
Compare trip evidence and recorder continuity; do not infer a successful drive
from HTTP responsiveness alone. After first-drive acceptance, promote durable
findings to AGENTS/README and remove this handoff.

## Rollback (parked/asleep only)

In the Pi checkout, with a clean working tree, the following preserves history and
uses the same restart procedure; it does not reset/overwrite unrelated changes:

```bash
cd /home/pi/dev/obd-things && test -z "$(git status --porcelain)" && python3 tmp/vehicle_data/o2-concurrency/measure-broker-latency.py --check-only --expected-sha "$(git rev-parse HEAD)" && git switch --detach 0c93cd2d2232792a905f8817f11e9358a324fd98 && bash tmp/vehicle_data/restart-broker-parked.sh
```

A refused gate blocks the restart; never clear inhibits or weaken checks to force
rollback. Recheck units, endpoints, journal and static passive CAN after rollback.
The checkout is intentionally detached afterwards; return to master through a
separately reviewed deployment, not a force reset.

## Evidence locations

- Pi: `/home/pi/dev/obd-things/tmp/vehicle_data/o2-concurrency/` contains the unchanged
  measurement runner, before/after JSON with raw timings, readiness snapshots,
  deploy log/times/cursor, unit records, journal JSON, and first-drive checker/output.
- Runner SHA-256: `d520a244b89ef6b3286bedfb93cd27b4164c033c6c91e28846793281cf7c506e`.
- First-drive checker SHA-256: `b6ed0e4c7f5036cab828801abac91e01c502c30a39de822872a82d4357c9dc2f`.
- The operator kept an additional local evidence archive outside the disposable
  worktree. No source gate/oracle or pinned fixture was modified for deployment.
