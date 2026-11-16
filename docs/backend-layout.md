# Backend organization and offline verification

The checkout packages remain `lib/`, `tools/`, `live_data/` and `projects/`.
`AGENTS.md` names public ownership boundaries and external launch contracts.
Keep records and pure decisions separate from transport/store/runtime imports
where there is a coherent boundary. The shared library must not depend on the
application packages. Module entry paths, compute payloads and rolling helper
contracts remain supported interfaces.

## Complete safe suite

On an offline development machine, from the checkout root:

```sh
python3 run_tests.py
```

The runtime floor is Python 3.11 for this repository; Node 20+ and npm are
required for the dashboard. Git is required by isolated repository fixtures.
The runner creates a disposable Python environment, installs the test
requirements, runs pinned `npm ci`, full fail-fast pytest (including the Node
suite), all backend-layout fixture tests and the repository scan, and builds
the dashboard in scratch space. It prints the actual Python and Node versions.
It reports unrun phases after a failure and removes its own scratch environment.
It never builds the served `dashboard/dist`, touches CAN, or controls services.

On vanpi, send the gate to a Mac worker when one is up, and otherwise run it
directly; AGENTS.md "Safe automated suite" gives the rule. In short:

```sh
/home/pi/van_compute/scripts/van_compute.py available | jq .available
# true: a Mac worker will take the job (add --source-root <checkout> from a worktree)
/home/pi/van_compute/scripts/pi_compute.py run repo-full-suite --wait 60
# false, or the job's status shows "placement": "pi-local": from the checkout
nice -n 10 ionice -c2 -n7 python3 run_tests.py
```

`available` reads the worker heartbeats in the queue's `workers/` folder and
calls a worker available while its heartbeat is at most 45 s old, the age the
broker treats as a lease. A job submitted while it says `true` can still land
on the Pi if the Mac sleeps before claiming it, so check `placement` before
reading a failure.

A direct run outside the sandbox uses pip's and npm's per-user caches
(`~/.cache/pip`, `~/.npm`) because it sets no `XDG_CACHE_HOME`. With them warm
it downloads nothing; on 2026-11-15 they held 21 MB and 45 MB and did not grow
over a run. They grow only when `requirements-test.txt` resolves to new
versions or the lockfile changes; `pip cache purge` and `npm cache clean
--force` empty them. They predate this route and are not bounded by
anything the repository controls, and neither has an entry in the root-budget
table (`ROOTFS_PATH_BUDGETS_MIB`, in the Mac-managed scripts repo's
`pi/scripts/backup/backup_conf.sh`). Everything else the run writes is per run: the scratch
folder under the checkout's `tmp/` (a 117 MB venv plus the build output) is
removed at the end, and `dashboard/node_modules` (27 MB) is replaced by each
`npm ci`.

For change sets confined to `projects/vehicle_data/dashboard/`, `python3
run_tests.py --dashboard` (compute task `repo-dashboard-suite`) runs the same
preparation, pytest over `DASHBOARD_PYTEST_TARGETS` (the modules that read the
dashboard; `tests/test_run_tests.py` has a tripwire for the list), the layout
step and the scratch build. AGENTS.md declares it as the `dashboard` scoped gate
(scope, exclusions, and when the whole suite still runs).
`--pytest-only` and `--dashboard` are mutually exclusive. Every profile holds
`tmp/run_tests.lock` for the run, so a second run in the same checkout waits
(up to 30 minutes, then exits 2) instead of wiping the first run's
`node_modules`; worktrees have separate locks. When `XDG_CACHE_HOME` is set the
npm download cache lives in `$XDG_CACHE_HOME/npm` and `npm ci` runs with
`--prefer-offline`; pip's cache stays in the run's scratch folder, since the unhashed
Python requirements can't safely share a cache other jobs write.

When the checkout is not writable, the runner stops before writing anything
with a setup error (exit 2). A Pi-local compute job mounts its source that way,
and the suite cannot pass there (next section); the error points at the direct
run instead.

A wait timeout leaves the job running; use `wait`/`status` with the returned job
ID. Do not submit a duplicate merely because the wait ended. The existing
`repo-tests` task retains pytest argument forwarding and prepares dependencies
before running the Python/Node regression subset. It is not the full build and
layout gate. Other focused compute test tasks are selections, not full-suite
evidence. Compute owns host placement; a remote pass is not a Pi runtime pass.

Missing required executables/dependencies fail setup. A portable fixture may
skip an explicitly identified platform-only case or local vehicle evidence
that is absent from the source payload; that is not a live vehicle validation.
Browser checks are separate when UI behavior changes. Backend-only changes do
not require inventing a visual comparison.

### Pi-local placement

A suite job placed on the Pi cannot pass, and making it pass is not planned:
the direct run passes on vanpi without the sandbox, and lifting the sandbox's
limits would loosen the Pi's guardrails for no gain in coverage. The facts
below were verified 2026-11-15 against van-compute release `8ad2c255…` (the
source is read-only on the Pi at `/home/pi/van_compute/current/van_compute/`).

- Placement: the broker runs a job on the Pi only after every Mac lease has
  lapsed (45 s, then a 30 s grace), and `pi_compute.py` has no option to force
  it. The broker also holds or requeues a Pi-local job while any
  `tmp/locks/active-diagnostics-*.lock` is write-locked (a CAN transmitter,
  such as a drive helper), so it may not run during a drive.
- Sandbox (`engine.py` `_bubblewrap_runtime`): `--unshare-net` with only
  loopback, no `/etc/ssl`, source snapshot read-only (where `run_tests.py`
  stops with its setup error), uid 0 in a user namespace with no
  capabilities. The writable places are `TMPDIR`, `HOME` and the task cache,
  which only Pi-local jobs (offline) can fill.
- Limits (`broker.py` defaults, `limited_child.py`): per-process
  `RLIMIT_AS` 768 MiB, `RLIMIT_CPU` 1200 s, 256 open files, 1800 s wall; the
  broker unit adds `MemoryMax=1G` (broker and job together) and
  `CPUQuota=150%`.
- Python: the job's interpreter is the van-compute venv (pytest 9.1.1,
  numpy 1.26.4, can-isotp 2.0.7, no cantools). The runner's venv cannot
  download anything, so "Python test dependency install" would fail.
- Node: `npm ci` aborts with a V8 out-of-memory error under the 768 MiB
  address-space limit even from a full cache (it works at 1 GiB), and the
  esbuild binary reserves about 1.27 GB of address space at start and
  crashes below about 1.5 GiB. The dashboard build and `build.test.mjs`
  need esbuild.

Direct full runs on vanpi, outside the sandbox, on 2026-11-15:

- At `nice -n 15 ionice -c3`, load average 11–14 from other sessions: passed
  in 1,427 s, 1,181 s of it pytest.
- At the same priority, load average 18: failed. `tests/test_campaign_cli_entrypoints.py`
  starts a plan-only CLI that takes under 1 s, and it missed its 20 s timeout.
  At nice 15 a process gets about 3% of a core contended by nice-0 work, and
  mq-deadline (the Pi's I/O scheduler) may hold each idle-class read for its
  10 s `prio_aging_expire`. Hence the documented `nice -n 10 ionice -c2 -n7`.
- At `nice -n 10 ionice -c2 -n7`, load average 7–10: passed in 1,299 s,
  1,069 s of it pytest. The pytest process used 357 s of CPU (`RLIMIT_CPU` is
  1,200 s per process).
- At `nice -n 10 ionice -c2 -n7`, load average 4–6: passed in 926 s, 782 s of
  it pytest, whose process used 308 s of CPU.

Sampled each second, the process tree peaked at 343–385 MiB RSS. Address space
(VmPeak) ran far higher: 1,811 MiB for pytest, 1,080 MiB for `npm ci`, 4,911 MiB
for one Node test. VmPeak counts reservations, which can shrink under a limit:
that Node test (`test/readmeShots.test.mjs`) passes under a 1 GiB `ulimit -v`.
Whether pytest passes under 768 MiB was not tested; npm and esbuild do not
(above). With warm user caches the direct runs downloaded nothing.

Not planned: what van-compute would need for a Pi-local suite job to pass.

- An address-space limit of at least 2 GiB for this job, proven only for npm
  and esbuild; a trial run under the chosen limit would settle the rest.
- A way to provide the Python and npm dependencies offline.
- A wall-time limit with room over the 1,800 s default: the slowest direct run
  above took 1,427 s without the sandbox's `CPUQuota=150%`.
- Room under the broker's `MemoryMax=1G`, which covers broker and job
  together; the Mac's suite runs peak at 770–890 MB of process-group RSS.
- A way to run the source snapshot writable, or the refusal above stays.

A Pi-local suite job is therefore never evidence about a change set: run the
suite directly instead.

## Lasting checks and limits

`checks/check_layout.py` and `checks/backend_layout/` are repository-owned copies
of the backend-layout checker. They require no global skill installation at
runtime. The `layout` configuration declares the shared-library dependency
direction and excludes only the checker's own implementation from its layout
heuristics. The installed checker tests exercise its parser and rules. Their Git discovery
stops at each fixture's parent, so fixtures under an ignored checkout `tmp/`
remain independent; fixtures that initialize Git still exercise Git discovery.

```sh
python3 checks/check_layout.py --json
```

There is deliberately **no layout baseline key**. Consequently all findings
from this general checker are advisory, including declared dependency and target
findings. It does not turn a reported architectural violation into a failing
suite by itself. `tests/test_backend_contracts.py` independently enforces the
shared-library direction, selected pure-owner imports, the Contracts table,
service/npm targets, and compute entry packaging. The existing service tests
also enforce `python3 -m` launches and the checkout working directory.

Static scans do not prove dynamic imports, shell/Go dependency graphs, runtime
behavior, installed service state or every rollback host. Size and prefix
findings are review prompts. Shared filenames alone do not justify a feature
folder, and live safety ownership is not a cleanup target.

Only Jacob may choose to create/update baselines. No baseline change is needed
to run the suite. After a separately reviewed cleanup, the optional commands
are, from this checkout:

```sh
python3 ~/.claude/skills/consolidate/drift_check.py rebaseline
python3 checks/check_layout.py rebaseline
```

Neither command is part of the suite or this organization change.

## Scope left for separate work

The large correlation analyzer, warning replay and AlfaOBD campaign modules
need their own preservation cases before further extraction. Vehicle runtime
and safety/lease/restore ordering remain under their existing project gates.
The long vehicle-data and ECU-mapping READMEs contain operational handoffs;
topic extraction should be a separate documentation task coordinated with
their active owners. No service deployment or restart follows from these
offline organization checks.
