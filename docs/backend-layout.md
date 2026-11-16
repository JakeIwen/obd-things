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
--force` empty them. Everything else the run writes is per run: the scratch
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

When the checkout is not writable (a Pi-local compute job mounts its source
snapshot read-only), the runner copies it into `TMPDIR`, skipping `.git`,
`tmp/` and bytecode as a snapshot does, and re-runs itself from the copy with
the same arguments. It refuses (exit 2) a `TMPDIR` inside the checkout, where the
copy would copy itself. The copied run gets its own process group. SIGTERM,
SIGINT or SIGHUP to the runner is forwarded to that group, escalated to SIGKILL
after 5 s (under van-compute's 10 s SIGTERM-to-SIGKILL grace), and the copy is
removed once the group is gone. The runner then ends by the same signal, as it
does when the copied run dies by one, so a shell sees 143 for SIGTERM, not 241.

SIGKILL can't be caught. Under van-compute the copy path happens only on the Pi,
where the job runs in bubblewrap's PID namespace with `--die-with-parent`: when
the broker kills a job every process in it dies, and the broker deletes the
job's work directory, which holds `TMPDIR` and the copy. A direct run killed
with SIGKILL leaves its copied run going until it finishes (each step has its
own timeout) and leaves one copy, the size of the checkout, in `TMPDIR` until
something cleans that.

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

Verified 2026-11-15 against van-compute release `8ad2c255…` (the source is
read-only on the Pi at `/home/pi/van_compute/current/van_compute/`). The three
suite tasks pass on Mac workers only. A Pi-local suite job fails:

- Placement: the broker runs a job on the Pi only after every Mac lease has
  lapsed (45 s, then a 30 s grace), and `pi_compute.py` has no option to force
  it. The broker also holds or requeues a Pi-local job while any
  `tmp/locks/active-diagnostics-*.lock` is write-locked (a CAN transmitter,
  such as a drive helper), so it may not run during a drive.
- Sandbox (`engine.py` `_bubblewrap_runtime`): `--unshare-net` with only
  loopback, no `/etc/ssl`, source snapshot read-only, uid 0 in a user
  namespace with no capabilities. The writable places are `TMPDIR`, `HOME`
  and the task cache, which only Pi-local jobs (offline) can fill.
- Limits (`broker.py` defaults, `limited_child.py`): per-process
  `RLIMIT_AS` 768 MiB, `RLIMIT_CPU` 1200 s, 256 open files, 1800 s wall; the
  broker unit adds `MemoryMax=1G` (broker and job together) and
  `CPUQuota=150%`.
- Python: the job's interpreter is the van-compute venv (pytest 9.1.1,
  numpy 1.26.4, can-isotp 2.0.7, no cantools). The runner's venv cannot
  download anything, so "Python test dependency install" fails.
- Node: `npm ci` aborts with a V8 out-of-memory error under the 768 MiB
  address-space limit even from a full cache (it works at 1 GiB), and the
  esbuild binary reserves about 1.27 GB of address space at start and
  crashes below about 1.5 GiB. The dashboard build and `build.test.mjs`
  need esbuild.

A direct full run on vanpi (2026-11-15, outside the sandbox, at `nice -n 15
ionice -c3` with a load average of 11–14 from other sessions) passed in
1,427 s, 1,181 s of it pytest. The next run at that priority failed when load
reached 18: `tests/test_campaign_cli_entrypoints.py` starts a plan-only CLI
that takes under 1 s, and it missed its 20 s timeout. At nice 15 a process gets
about 3% of a core contended by nice-0 work, and with the idle I/O class
mq-deadline (the Pi's scheduler) may hold each read for its 10 s
`prio_aging_expire`; hence the documented `nice -n 10 ionice -c2 -n7`. Sampled each second, its process tree peaked at 343 MiB RSS; the
pytest process alone reached 1,811 MiB of address space (VmPeak), so the
768 MiB `RLIMIT_AS` would stop pytest as well as npm and esbuild.

Passing on the Pi needs van-compute changes: an address-space limit of at
least 2 GiB for this job, a way to provide the Python and npm dependencies
offline, and a wall-time limit with room over the 1,800 s default, since the
direct run took 1,427 s without the sandbox's `CPUQuota=150%`. The Mac's suite
runs peak at 770–890 MB of process-group RSS, which leaves little room under
the Pi broker's `MemoryMax=1G`. Until those land, a Pi-local suite job is not
evidence about the change set: run the suite directly instead.

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
