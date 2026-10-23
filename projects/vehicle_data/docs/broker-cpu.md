# Broker CPU investigation — October 23, 2026

## Confirmed cause and implementation

Repeated history scans caused both the supplemental-refresh bursts and part of
the steady historian cost. This is distinct from the bounded, recurring USB
identity/interface checks; those safety checks are unchanged.

The saved-database profile found:

- Each history refresh compares 11 metrics across 10 prior trips. Its 110
  `_trip_metric_aggregate` calls used `metric_rollups_baseline(metric, regime,
  bucket_us)`, scanning each metric's entire retained rollup history to select
  one trip. They consumed 0.70–0.85 seconds of the 0.90–0.96-second in-memory
  history profile. The fuel trip totals use the same access pattern, but were
  a much smaller cost than the existing trip comparison.
- Each ingest's `SELECT DISTINCT role FROM interface_samples` walked about
  910,000 SQLite VM instructions to rediscover a few role names.
- The previous-topology query scanned older snapshot IDs, then sorted by
  capture time: about 1,488,000 instructions per ingest. Capture times and
  snapshot IDs are ordered together by the public ingestion contract.
- Health-response SQL and saved DTC loading were small. The August partial
  indexes were installed and used; another copy of that repair was not needed.

The implementation plan is deliberately confined to these three measured
paths: add an index beginning with trip identity, seek successive distinct
interface-role keys, and seek the previous known topology by snapshot ID.
Compare all saved-data products and ingestion results, test growth with real
SQLite tables, run the whole suite, then activate only in a fresh asleep window
and repeat the same live measurements.

`history/schema.py::_create_query_indexes` adds two idempotent indexes at
historian construction:

- `metric_rollups_trip(trip_key, metric, bucket_seconds, regime, bucket_us)`
  bounds trip comparisons, trip fuel totals and trip-filtered series. Its
  suffix preserves the old regime/time/rowid traversal for floating-point sums.
- `system_health_samples_known_topology(snapshot_id, topology_generation,
  captured_us) WHERE topology_generation IS NOT NULL` covers the previous-known
  lookup even through long periods without known topology.

`history/interface_queries.py` owns the two bounded interface-history lookups.
The recursive minimum-key query retains every recorded role, including legacy
channel placeholders; the existing gap-retirement code still handles them.
Previous topology uses descending snapshot IDs, whose order follows the
historian's rejection of non-increasing capture timestamps. Locks and caller
transactions are unchanged.

No raw observations, retention policy, metric selection, warning rules,
notification behavior, freshness limits, sampling/refresh intervals, or CAN
acquisition/restoration checks change. The schema version remains unchanged.
The extra indexes remain compatible with the previous code during rollback.

## Evidence and measurement

The five-minute live baseline used service-cgroup CPU counters every second,
cache-only Unix status every two seconds, and snapshot/history/health every
20 seconds. CPU percentages are fractions of **one core**, including broker
children; thread figures are not added to the process total. Probe CPU was
1.90 seconds over 301 seconds. No heavy offline job overlapped this sample.

| Measurement | Before deployment | After deployment |
|---|---:|---:|
| Mean broker CPU, one core | 26.86% | 14.67% |
| User / system CPU | 15.17% / 11.69% | 9.69% / 4.98% |
| Median / peak one-second CPU | 13.94% / 124.66% | 12.15% / 84.40% |
| Mean outside observed refresh intervals | 16.99% | 13.41% |
| Five completed supplemental refreshes | 5.607–8.998 s | 0.623–0.905 s |
| Mean supplemental refresh | 6.694 s | 0.725 s |
| Historian interval, median / maximum | 5.275 / 11.856 s | 5.128 / 6.119 s |
| Status latency, median / maximum | 5.58 / 43.96 ms | 5.09 / 26.64 ms |
| Snapshot latency, median / maximum | 9.46 / 111.36 ms | 12.47 / 26.95 ms |
| History latency, median / maximum | 9.14 / 85.30 ms | 10.89 / 19.73 ms |
| Health latency, median / maximum | 52.39 / 171.71 ms | 56.40 / 103.56 ms |

The after sample used the identical probe and cadence for 300 seconds after
startup, with five completed refreshes and 1.76 seconds of probe CPU. Mean
broker CPU fell **45.4%** and mean refresh duration fell **89.2%**. Peak
one-second CPU is not eliminated; residual work outside refreshes averages
13.41% of one core. The samples occurred several hours apart under ordinary
host load, so this is a matched operational comparison, not an isolated lab
benchmark. Snapshot/history/health median request times did not improve; their
worst sampled latencies and the historian's long refresh-related gaps did.

All sampled requests returned 200; the van stayed asleep and CAN TX counters
did not move. Refresh scheduling remains 60 seconds after completion, plus
the historian loop's work/wait, rather than a wall-clock minute boundary.

A separate bounded probe of the unchanged four-interface status operation
cost roughly 30–44 ms of parent-plus-child CPU per call without cProfile.
Source review also found full cached-status construction in the idle compass
loop every 250 ms. Neither establishes an exact live thread attribution;
those paths and their safety timing were left intact.

The source database was copied using the SQLite backup API from a read-only,
pinned transaction, including committed WAL contents. The successful backup
took 227.95 seconds and contains 1,611,997,184 bytes. An initial 180-second
idle-I/O attempt timed out and rolled its output back to zero bytes. No live
database experiment or WAL deletion occurred. This follows the
[SQLite online backup contract](https://www.sqlite.org/backup.html).

`tools/telemetry_history_profile.py`, named task `telemetry-history-profile`,
loads that finalized backup into a disposable in-memory database within the
compute worker's existing limits. It runs the real historian constructor,
history/fuel/health/DTC methods and optional saved-snapshot ingestion. Time is
fixed, only scratch-directory strings are normalized, and every response
field is retained. External delivery and first-start hourly maintenance are
excluded explicitly. Reports include SQL timings, VM counts, query plans,
cProfile output, input indexes and response hashes. Memory timings on the
worker are not live Pi benchmarks. The source remains read-only.

Before profile job `20261023T175446Z-d7f52e82` used SQLite 3.54.0. Read-only
EXPLAIN on the Pi's SQLite 3.40.1 independently confirmed the same three old
access paths. The earlier disk-clone job failed at the worker's 128 MiB file
limit; the in-memory approach preserves that limit rather than changing it.

After job `20261023T180921Z-bfd67c83` produced **13 identical response hashes**:
three history/fuel/health/DTC rounds plus the ingestion result. The new trip
index and covering known-topology index were selected; distinct roles used
successive indexed seeks. Mean profiled CPU per operation was:

| Operation | Before | After |
|---|---:|---:|
| History response | 861.4 ms | 179.9 ms |
| Fuel attachment | 50.0 ms | 7.3 ms |
| Health response | 53.6 ms | 72.3 ms |
| DTC response | 5.5 ms | 7.6 ms |
| Combined supplemental loaders | 970.5 ms | 267.1 ms |
| Saved-snapshot ingest | 119.9 ms | 72.0 ms |

History CPU fell 79.1%; the combined loaders fell 72.5%. Ingest SQL work fell
from approximately 2,433,000 VM instructions to 35,000 (98.6%). Health/DTC
code and outputs are unchanged; their small timing variation is retained in
the table rather than described as an improvement. Index initialization on
the memory clone took 0.497 seconds. The separate live Pi measurements above
establish the actual deployed improvement.

Raw evidence stays under `tmp/vehicle_data/broker-cpu-20261023/`, with the live
sample at `tmp/vehicle_data/cpu-before-20261023.jsonl` and the finalized backup
at `tmp/vehicle_data/cpu-history-20261023.sqlite3`.

## Process rows, validation and activation

The owner-shell `ps -T` capture **confirms thread 3264376 belonged to broker
process 3264272**, alongside its other 15 threads. The htop rows were a process
total and one included thread; the reported 105.2% and 94.2% must not be added.
The separate 75-second `pidstat -t` sample measured the process at 19.82% and
that thread at 11.32% on average. This shorter, process-only sample is not mixed
with the five-minute cgroup baseline, which includes short-lived children.
The host capture was necessary because the agent sandbox hides host `/proc`.

Full-suite job `20261023T181554Z-43619077` passed **1,968 tests, 12 skips and
2,909 subtests** in 184.94 seconds through the current `repo-tests` entry point
with fail-fast and no test selection. This task added no skips. Targeted trip regression
`20261023T180558Z-3693a878` passed; interface regression job
`20261023T180850Z-92b04b71` passed six cases. They exercise the public historian
with real SQLite, 20,000 unrelated trip rollups, 30,000 repeated interface rows,
10,000 topology rows, unknown-topology tails, legacy gaps and retention holes.
Revised profiler regression job `20261023T175420Z-8357db10` passed four cases.
Dashboard task
`20261023T174721Z-3ca79979` passed 499 tests and the production build; no frontend
assets need deployment for this fix. The initial code-shape check reports
unchanged, unrelated baseline growth in `broker.py`, `metrics.py`, and
`dashboard/src/styles/app.css`; this task does not restructure those files.

The first full-suite attempt exposed a timing bug in the API concurrency test:
its normal recovery request inherited the stalled peer's 50 ms deadline.
The narrow correction already present on the remote branch restores the normal
I/O budget only after the stalled response has acquired its own fixed deadline;
the stalled-peer expiry assertion and production code remain unchanged.

**Deployed and independently verified October 23 at 20:44–20:53 UTC.** The
owner-shell handoff pinned source `9372f26` and clean runtime paths before the
guarded asleep restart. Systemd launched broker PID 2376347 at 20:44:53 UTC;
the new API instance is `2026-10-23T20:44:58.301929+00:00`. Both new indexes
exist and are selected on the live Pi's SQLite 3.40.1. The application schema
version remains 1. Ten indexed snapshot rows spanning the restart confirm
history continuity, with an 8.438-second maximum restart interval.

The agent's no-new-privileges boundary required the normal SSH shell. The first
handoff's unnecessary `sudo -v` prompted for a password; the corrected handoff
uses noninteractive, command-specific checks, with no credential keepalive or
sudo-policy change. The owner's normal shell has passwordless sudo.

The pre-task clean source/rollback target is `43e8c1b`; the broker does not
export an embedded runtime Git revision. The previous instance began
`2026-10-21T19:08:57.262387+00:00`. The four replaced old runtime files remain
in the raw rollback tar; older code can retain the new indexes and all history.

The work is isolated on `codex/broker-cpu-20261023`. Remote `master` advanced
with unrelated architecture/benchmark work while this checkout retained two
storage-policy commits. Neither history is overwritten or implicitly deployed;
the dedicated branch keeps this task's tested source explicit.

Installed unit/drop-in hashes are unchanged. Only the broker was explicitly
restarted; `PartOf=` also restarted both web listeners and the DTC path watcher.
The broker and both web listeners are active with zero crash restarts. Recorder
PID 743 and advisor PID 1255 stayed unchanged; the DTC worker remains inactive.
No units, enablement, dashboard assets, adapters, networking or cron changed.
The new broker has no journal entries/errors. The old process printed
`KeyboardInterrupt` during the requested stop; systemd recorded successful
deactivation and startup, not an automatic crash restart.

Owner checks before/after/final and an independent final check verified fresh
asleep state, idle/restored helpers, no active/foreign owner or inhibit, exact
serial/dev_id identities, all vehicle roles passive/healthy and the spare down.
All four channels had zero TX, RX, error and drop deltas across the deployment
window. Existing open episodes 666 and 667 remained open; delivered notification
count stayed 137 with zero pending/failed entries. Metric catalog names are
unchanged. Historian snapshots advanced in each post-restart check, and status,
snapshot, history and health returned 200 through Unix, LAN and Tailscale.

Deployment evidence, including the full after sample and independent checks,
is in `tmp/vehicle_data/broker-cpu-20261023/deploy-20261023T204329Z/`.
`live-comparison.json` records the calculated reductions. The residual baseline
includes the measured safety/interface work described above; it has not been
fully attributed thread by thread. Remaining history work includes the 7/30-day
aggregates. No polling cadence or ownership check was weakened to reduce it.

Use the existing `restart-broker-parked.sh` and supplement its checks with
fresh serial/dev_id identity, state/freshness, ownership, inhibit, passive
readback, zero TX/error deltas, advancing historian, and API/journal/event
verification. Recheck the literal sudo policy in the owner shell. The raw
one-off `cpu_deploy_check.py` performs these read-only observations; it never
restarts a service or configures/opens CAN. Saved asleep checks and offline
tests are not new driving validation.
