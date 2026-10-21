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

On vanpi, submit the same gate through the established offline compute queue:

```sh
/home/pi/van_compute/scripts/pi_compute.py run repo-full-suite --wait 60
```

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
