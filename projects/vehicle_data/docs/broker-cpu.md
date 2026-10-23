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
| Mean broker CPU, one core | 26.86% | Pending activation |
| User / system CPU | 15.17% / 11.69% | Pending activation |
| Median / peak one-second CPU | 13.94% / 124.66% | Pending activation |
| Mean outside observed refresh intervals | 16.99% | Pending activation |
| Five completed supplemental refreshes | 5.607–8.998 s | Pending activation |
| Historian interval, median / maximum | 5.275 / 11.856 s | Pending activation |
| Status latency, median / maximum | 5.58 / 43.96 ms | Pending activation |
| Snapshot latency, median / maximum | 9.46 / 111.36 ms | Pending activation |
| History latency, median / maximum | 9.14 / 85.30 ms | Pending activation |
| Health latency, median / maximum | 52.39 / 171.71 ms | Pending activation |

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
the memory clone took 0.497 seconds. None of these numbers replaces the pending
live Pi after measurement.

Raw evidence stays under `tmp/vehicle_data/broker-cpu-20261023/`, with the live
sample at `tmp/vehicle_data/cpu-before-20261023.jsonl` and the finalized backup
at `tmp/vehicle_data/cpu-history-20261023.sqlite3`.

## Process rows, validation and activation

Systemd reports one broker process, historical/current-at-investigation PID
3264272, with 16 tasks and no crash restarts. The owner's htop capture shows
that PID at 105.2% and likely thread 3264376 at 94.2%, with identical memory
figures and other smaller matching rows. That is consistent with process-total
plus thread display, not evidence of two cores of independent broker work.
Host thread membership is pending confirmation because the agent sandbox
cannot see host `/proc` entries. The deployment handoff will save host thread
IDs before replacing the process.

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

Deployment is explicitly authorized but **not yet activated**. The agent's
literal sudo permission check fails under the sandbox's no-new-privileges
restriction. Independent code review, saved-data equivalence and the full suite
are complete; activation and a live after benchmark require the owner-shell
handoff. The pre-task clean source/rollback target is
`43e8c1b`; the broker does not export a runtime Git revision. Its current
instance began `2026-10-21T19:08:57.262387+00:00`.

The work is isolated on `codex/broker-cpu-20261023`. Remote `master` advanced
with unrelated architecture/benchmark work while this checkout retained two
storage-policy commits. Neither history is overwritten or implicitly deployed;
the dedicated branch keeps this task's tested source explicit.

Installed units/drop-ins are recorded in the raw evidence. Only a broker
restart is required; `PartOf=` also restarts both existing web listeners and
the DTC path watcher. No unit installation, daemon reload, enablement change,
dashboard build installation, or recorder restart is needed. Index creation
is the only database migration: additive, with startup work and WAL/storage
growth to verify. Rollback restores the prior code while retaining the indexes
and all history.

Use the existing `restart-broker-parked.sh` and supplement its checks with
fresh serial/dev_id identity, state/freshness, ownership, inhibit, passive
readback, zero TX/error deltas, advancing historian, and API/journal/event
verification. Recheck the literal sudo policy in the owner shell. The raw
one-off `cpu_deploy_check.py` performs these read-only observations; it never
restarts a service or configures/opens CAN. Saved asleep checks and offline
tests are not new driving validation.
