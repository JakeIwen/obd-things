# Machine-data retention and EXFAT archive

This document records where untracked evidence lives and how to distinguish cold archived data
from disposable `tmp/` output. It complements the root README's data convention; it does not make
anything under either `tmp/` tree tracked or backed up.

## Retention rules

- Preserve raw data that required this van, a drive, an engine start, an ignition cycle, a
  controlled physical action, AlfaOBD, or a live diagnostic exchange. Promote only reviewed,
  redacted evidence subsets into git.
- Keep active locks/service state, small inventories, current benchmark inputs, executable ECU
  databases, and near-term working results in the Pi's local `tmp/`.
- Put large completed captures, cold raw evidence, and compute history in
  `/mnt/EXFAT512/obd-things/tmp/` after checking the mount and verifying the copy.
- Delete caches, virtual environments, decompiler output, failed staging copies, and other
  mechanical derivatives only when their source remains available.
- A symlink under local `tmp/` is a convenience pointer, not redundancy. If it appears broken,
  inspect `findmnt -T /mnt/EXFAT512` before deciding the artifact is gone.

## Recurring compute retention

Use `python3 -m tools.compute_retention` from the repository root to preview
the owner's **2 GiB cap for completed compute history**, with a maximum age
of **30 days for both successful and failed jobs**. It prints the backend's JSON plan
and changes no data. `--apply` explicitly deletes eligible completed jobs.
This is the storage-administration exception to the usual `pi_compute.py`
frontend rule: analysis and tests still go through the named compute tasks.
The wrapper calls the installed administrative entry point in an isolated
child process; it does not duplicate queue locking, citation scanning or deletion.

The policy selected by the owner on October 21 applies to local `done/` and
`failed/` compute history, including source snapshots, results and metadata:

- Delete uncited jobs older than 30 days, measured from manifest completion.
- If retained history is still above 2,147,483,648 allocated bytes, delete the
  oldest remaining uncited jobs until it fits. The size cap takes precedence
  over keeping a full 30 days; recent disposable jobs can be evicted sooner.
- Preserve jobs cited outside `tmp/` in tracked or nonignored working files,
  their transitive dependencies, and dependencies of queued/running jobs.
- Active jobs and archive symlink targets are outside the completed-history
  budget. Raw vehicle inputs outside the queue are never selected.
- If protected or uninspectable data prevents meeting the budget, report it
  and return failure. Never claim the 2 GiB target was met when it was not.

Before the first manual apply or enabling the timer, review the plan's
eligible IDs. Subsequent weekly runs apply automatically. **Ignored handoffs under
`.agent/` and references inside `tmp/` do not pin jobs.** Promote a needed job
ID into the tracked finding or project documentation that uses it, then rerun
the plan. Existing handoff-only references are preserved in
[`compute-retention-pins.md`](compute-retention-pins.md); keep that list current
as workstreams finish or add evidence. Keep the canonical raw input separately: the pruner does not verify
that a job's original inputs remain available. Do not use compute history as
the only copy of vehicle evidence.

The adapter requires van-compute's supported `--keep-days` and
`--max-history-bytes` options. It passes those flags to the installed
administrative CLI and performs no runtime policy overrides. An older backend
rejects the flags before deletion. Deploy the reviewed van-compute update
through its coupled Mac/Pi installer before activating this policy; do not
patch its immutable Pi release. The source owner is
`/Users/jacobr/dev/van-compute`, with deployment instructions in that repo's
`pi/docs/compute/VAN_COMPUTE.md`.

`tools/systemd/obd-compute-retention.service`, `.path` and `.timer` apply the
policy when completed-job directories change and hourly (up to five minutes
random delay), with persistent missed-run catch-up. A new completed job can
briefly take history over budget until the triggered maintenance finishes;
this is a retention limit, not a filesystem quota on active work. They are opt-in: committing these files does
not install or enable them. The service runs as `pi`, has no CAN or network
access, and writes only the queue and van-compute's existing submission-fence
directory. That fence temporarily defers new compute submissions while the
backend rescans citations and holds its queue lock. It does not restart the
compute broker or telemetry services.
Deletion is per file, not atomic per job. An interruption can leave a partial
job that a later plan reports as skipped; investigate that entry rather than
forcing removal. The unit sends SIGINT so the backend can restore its
submission fence during normal cancellation.

Install all three regular unit files in `/etc/systemd/system/`, run
`systemctl daemon-reload`, run the first cleanup, and enable both
`obd-compute-retention.path` and `obd-compute-retention.timer`. Check the timer's
next scheduled run with `systemctl list-timers obd-compute-retention.timer`;
inspect completed runs with `journalctl -u obd-compute-retention.service`.
Disable both triggers with `sudo systemctl disable --now
obd-compute-retention.timer obd-compute-retention.path` to stop future cleanup.
This preserves all remaining data.

The wrapper's public CLI and unit contracts are covered by
`tests/test_compute_retention.py`. Run the whole repository suite through
the `repo-tests` compute task, plus `dashboard-test-build` when its Node wrapper
cannot execute on the worker's platform. Validate the units with
`systemd-analyze verify` before installation. The backend remains responsible
for its own deletion and concurrency tests.

## Storage review policy

Treat **6 GiB local `tmp/` usage as a review trigger**, not a deletion quota.
The machine-managed backup watchdog's existing alert is 8 GiB; do not change
its deployed `~/scripts` copy from this repository. Review monthly and whenever
that alert fires, using `du -xhd1 tmp` (local allocated bytes, without following
archive symlinks) and the compute-retention preview. Check the remaining
EXFAT space separately. Never use a whole-tree age deletion command.

| Data class | Policy |
| --- | --- |
| Completed compute history | 2 GiB cap; at most 30 days for either outcome, with citation/dependency protection above. Check on completion and hourly. |
| Raw CAN, AlfaOBD inputs, inventories, owner references | No automatic deletion. Preserve canonical inputs; archive completed large files only after mount/space checks and hash verification. |
| Replay databases, extracted arrays, build copies | Review after the workstream ends or after 30 days idle. Remove only after confirming retained source and reproducible command; preserve named oracle fixtures and SQLite WAL/SHM sets. |
| Browser/Python environments | Review after 30 days unused. Reinstallable, but keep any runtime used by an active workstream; do not repeatedly remove a shared runtime between jobs. |
| Locks, active captures, live service state/history | Excluded. Do not unlink a live lock or change historian retention to meet the scratch-data budget. |

If compute cleanup cannot bring total `tmp/` usage under the review trigger, investigate
the largest remaining category. Prefer a reviewed, task-specific shorter
window for disposable test/build jobs in van-compute over reducing retention
for all research jobs. Archive large evidence files rather than copying
compute source snapshots file by file onto EXFAT's large allocation units.

## Current archive layout

The 2026-07-30 storage audit moved seven large CAN logs into the same relative paths below the
EXFAT tree:

```text
/mnt/EXFAT512/obd-things/tmp/captures/bcan/events/
/mnt/EXFAT512/obd-things/tmp/captures/ccan/events/
/mnt/EXFAT512/obd-things/tmp/captures/ccan/pair6-14_500k_*.log
```

These are 2,132,080,699 bytes of B-CAN/C-CAN drive, ignition, AlfaOBD, and controlled-event
evidence. Each source/destination pair was SHA-256 checked before the local regular file was
replaced with a symlink. Existing finding paths therefore still resolve while EXFAT512 is mounted.
For van-compute inputs, pass the real `/mnt/EXFAT512/...` path because the queue deliberately
rejects symlink inputs.

Three large PROXI/CAN-CH safety captures (298,970,794 bytes) were handled the same way below:

```text
/mnt/EXFAT512/obd-things/tmp/proxi_safety/
```

Their local `tmp/proxi_safety/...` names remain symlinks so the configuration-recovery findings
retain working paths.

## Compute history

The complete compute history at the audit checkpoint is preserved as real directories under:

```text
/mnt/EXFAT512/obd-things/tmp/compute/done/    # 420 jobs
/mnt/EXFAT512/obd-things/tmp/compute/failed/  # 117 jobs
```

Local `tmp/compute/archive` points to that root. The 61 completed jobs whose IDs are cited by
tracked findings/configuration also remain as real local directories under `tmp/compute/done/`,
so their normal `pi_compute.py status` and `result` commands continue to work.

Van-compute intentionally ignores symlinked job directories. Consequently, an archived-only job
is read directly through `tmp/compute/archive/{done,failed}/<job-id>/`; it is not visible to
`pi_compute.py status`, `result`, or `list`. To restore normal queue access, enter van-compute
maintenance, copy the complete archived job directory back as a real
`tmp/compute/{done,failed}/<job-id>/` directory, verify its manifest/results, and then leave
maintenance. Do not replace the queue's `done/` or `failed/` state directory with a symlink:
completion depends on same-filesystem atomic moves.

EXFAT's allocation unit makes the many small compute source snapshots consume substantially more
allocated space than their logical byte count. That is expected; the archive prioritizes direct
per-file access and provenance over compact packing.

## 2026-07-30 cleanup ledger

The audit reduced physical local `tmp/` use from 5,606,412,288 bytes to 1,431,855,104 bytes and
increased root free space from about 2.3 GiB to 5.9 GiB.

The following 697,573,376 bytes were deleted rather than archived because they are mechanical
derivatives:

- two unused APK/androguard Python environments;
- two JADX partial-output trees, one baksmali tree, and one extracted DEX tree, all reproducible
  from the retained owner-supplied APK;
- `tmp/ecu_mapping/compute-inputs/`, whose eight staged compressed captures were byte-identical to
  the canonical EXFAT drive-correlation captures.

The active `tmp/venvs/obd-research/` search environment and `tmp/playwright/` browser runtime were
kept local. ECU catalog databases, benchmark inputs, inventories, sweeps, TPMS/radar data, and all
other smaller or near-term project artifacts were also retained locally.

## 2026-10-12 cleanup ledger

A root-filesystem growth audit moved 4.4 GiB of completed evidence to the same relative paths below
`/mnt/EXFAT512/obd-things/tmp/`. Each file was SHA-256 checked before the local item was replaced
with a symlink, so cited paths still resolve while EXFAT512 is mounted:

- `captures/three_bus_drive/20260819_183854_-0600_can2-ccan_can3-bcan_can0-canch.candump` and the
  `session_20260820_011258_-0600` and `session_20260823T042427.681227Z_1189408` directories. The
  `current_session`/`current_capture_path` pointer files and the metadata/summary files stay local.
- `radar/acc-passive-20260924/`, `radar/acc-passive-20260927/`, `radar/acc-passive-20260928/`.
- `vehicle_data/warning_replay/fix-20260925/` and the seventeen 2026-09-24 `replay-*.sqlite3`
  outputs. Exports, reports, the 2026-10-08 replays, and `system-replay-*` databases (which have
  WAL sidecars) stay local.

Compute history (`tmp/compute/done` and `failed`, about 3.9 GiB in 98k files) was left local.
Copied per file to EXFAT512 it would occupy about 14 GiB because of the 128 KiB allocation unit.
The Pi backup watchdog now alerts when local `tmp/` exceeds its configured budget.

## 2026-10-21 audit (before cleanup)

The reported 7.2 GB is 7.18 GiB: local `tmp/` occupied 7,712,530,432 allocated
bytes, with 7,454,678,071 apparent bytes. Archive symlink targets are excluded.

| Local category | Allocated size, rounded | Interpretation |
| --- | ---: | --- |
| `compute/` | 4.42 GiB | 560 successful and 74 failed jobs; 3.29 GB decimal in repeated source snapshots. |
| `vehicle_data/` | 876 MiB | Includes 455 MiB warning replays and 291 MiB current fuel-economy work. |
| `ecu_mapping/` | 713 MiB | Owner inputs, extracted catalogs and derived analysis; mixed retention needs. |
| `captures/` | 358 MiB | Unique vehicle evidence. |
| `playwright/` | 312 MiB | Mostly reinstallable browser/runtime files. |

Compute history grew by about 1.94 GB decimal across 214 October jobs. The
2026-10-21 19:31 UTC backend preview marked 52 jobs eligible, totalling
219,639,222 apparent bytes (about 209 MiB), while protecting 233 jobs and
keeping 349 inside their age windows. That is a modest immediate saving;
weekly retention prevents indefinite accumulation but is not a hard size cap.
No data was deleted for this audit. The gitignored plan is
`tmp/storage-retention/compute-plan.json`; regenerate it before applying.

The owner then selected 30 days for both outcomes. The adapter's 19:55 UTC
preview identified 195 eligible jobs (184 successful, 11 failed), totalling
789,466,864 apparent bytes (753 MiB). It retained 233 protected jobs and 214
young jobs. That preview is `tmp/storage-retention/compute-plan-30-days.json`;
figures change as current test jobs finish. This remains a preparation-only
audit: no apply or timer installation has run.

After promoting 45 handoff-only references to the tracked pins list, the
20:00 UTC final preview retained 274 protected jobs and 213 young jobs. It
identified **159 eligible jobs, 615,059,366 apparent bytes (587 MiB)**, with
no skipped entries. No pinned ID was eligible. The final review file is
`tmp/storage-retention/compute-plan-30-days-pinned.json`.

Verification on the isolated publish branch: 1,943 Python tests passed with
12 reported skips, 491 dashboard tests and the build passed, and systemd unit
validation and the code-shape ratchet passed. The test-fixture portability
repairs are separate from the retention change. No live cleanup or timer
activation was included in verification.

Normal drive recordings already write zstd chunks directly to EXFAT, with
30/25 GiB free-space degradation/stop floors. EXFAT had about 31 GiB free
(94% used), so moving every scratch directory there would trade one space
problem for another. Agent sandbox mount views can be read-only even when
the underlying filesystem is writable; they do not alone prove a host mount
failure.

A separate small growth source is shared-observer metadata appended by
`lib/diagnostic_safety.py::_acquire_channel_lock`: the C-CAN handoff lock was
about 15 MB and grew around 1.9 MB/day. Fixing that requires preserving the
live shared/exclusive lock contract and checking metadata consumers. It is
excluded from cleanup; never unlink it while owners may hold its inode.

## Owner revision: 2 GiB completed-compute cap

The owner rejected the age-only saving and set a 2 GiB maximum specifically
for compute history. This supersedes the weekly age-only activation script.
The revised backend preview measured 5,216,096,256 allocated bytes and selected
324 uncited jobs totalling 3,082,387,456 bytes. Projected remaining history is
2,133,708,800 bytes: **4.86 GiB → 1.99 GiB**, reclaiming **2.87 GiB** while
preserving cited jobs and active dependencies. The live read-only preview took
35 seconds; its report is `tmp/storage-retention/compute-plan-2g-final.json`.
No data has been deleted.

The updated van-compute source has 301 passing tests. The revised consumer has
1,944 passing Python tests (12 reported skips) and 491 passing dashboard tests
plus the build. Systemd validation passed. The published backend baseline
matches the installed Pi source cohort; deployment must still use the Mac's
normal coupled installer. Existing unrelated `queue.py` code-shape growth in
the backend is unchanged; the budget change introduces no size exceptions.

Activation order: deploy the reviewed van-compute backend on Mac and Pi,
install all three obd retention units, run the first cleanup successfully,
then enable the `.path` and `.timer` triggers. The old age-only handoff is
superseded. Actual reclaimed space and installed service behavior remain to
be verified after the owner runs the replacement activation handoff.
