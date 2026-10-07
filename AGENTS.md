# obd-things — agent instructions and knowledge index

This file is the Codex entry point for the repository. Treat tracked documentation as the
durable memory of the project: verify it before experimenting, and update it when new facts are
established. Do not leave important findings only in chat/session memory or code comments.

## Local private context

If `AGENTS.secret.md` exists beside this file, read it after this file whenever a task involves
machine-specific infrastructure, private owner resources, secret locations, local network access, or
offline compute workers. It is an intentionally untracked local companion; never stage, commit, quote,
or promote its private contents into tracked documentation. Tracked agent files may reference a
same-directory `AGENTS.secret.md` when private context is necessary, but must remain complete and safe
when that file is absent.

## Start every task here

1. Read `git status` and preserve all existing work. A dirty worktree may be an in-progress handoff,
   not disposable output; inspect the current status/diff rather than assuming a clean tree.
2. Read the root `README.md` for hardware topology, safety boundaries, data conventions, and the
   research-first workflow.
3. Read `docs/bus-map.md` before any CAN reverse-engineering. It is the master map for physical bus
   parameters, verified broadcast frames/decodes, wake/sleep behavior, and the UDS module summary.
4. Read the target project's `projects/<name>/README.md` before changing that project. These are
   handoff documents, not generic introductions. Follow any deeper handoff they name; radar work, for
   example, starts with `projects/radar/docs/AGENT_HANDOFF.md` and its ruled-out list.
5. For UDS addressing, use `lib/modules.py` as the executable source of truth. DID namespaces are
   per ECU; keep them in the relevant project map and never merge them into a global DID list.
6. Read `docs/agent-context.md` when planning diagnostics, operating live services, handling AlfaOBD
   data, or working outside a single well-documented project. It preserves the cross-project constraints
   and environment facts migrated from Claude's external memory store.

For remaining dashboard mappings and live ACC settings, the September 22
[mapping assessment](projects/radar/findings/2026-09-22_acc_and_dashboard_mapping_outlook.md)
distinguishes unpopulated raw cluster inputs from unresolved signal decodes.
Radar alignment polling is validated; ACC set speed/state/gap and lead-object
data remain separate mapping work. Related-platform DBCs are candidates only.

## Telemetry dashboard (one app, port 8765)

The repository has exactly one telemetry dashboard: the Preact app in
`projects/vehicle_data/dashboard/` (built into the gitignored `dist/`), served on port 8765 by
`projects/vehicle_data/web_v2.py` in `van-telemetry-web.service` (LAN via a machine-local drop-in)
and `van-telemetry-web-tailscale.service` (Tailscale, guarded DTC jobs). The former static frontend
(`projects/vehicle_data/static/`) and the temporary parallel "v2" units were removed on 2026-09-27.
`web_v2.py` keeps its filename because the installed units execute it by path; `web.py` is its base
class and provides the `/v1` JSON API. The MacBook-managed Van Dashboard reads
`http://192.168.6.103:8765/v1/snapshot`, so `/v1` routes must keep working. Start with
`projects/vehicle_data/README.md` section "Telemetry dashboard" and
`projects/vehicle_data/dashboard/docs/design.md`.

Broker pure internals are split into `projects/vehicle_data/status_view.py`
(snapshot-only status builders), `vehicle_state.py` (passive evidence conclusions),
`wake_state.py` (pure parked-wake admission), and `helper_events.py`
(bus-parameterized status-event validation). Locks, clocks,
component calls and all helper/CAN lifecycle effects remain in `broker.py`.
`tests/test_broker_pure.py` pins status bytes and directly tests the bus-specific
helper contracts. `tests/broker_pure_oracle.py` captures generated helper/state
cases against whichever checkout `--repo-root` selects; compare old/new output
rather than keeping a frozen broker implementation in the permanent tests.

Oil-life telemetry is enabled (owner activation 2026-10-03):
`engine.oil_life_remaining` is PCM DID `2185`, unsigned percent, polled at
most once per minute during qualified engine-running intervals. The Service
card retains dated readings after shutdown; service records never reset the
PCM. The helper's `--enable-oil-life` broker handshake protects rolling
deployments. See the vehicle-data README and the September 22 gear/oil-life
finding for validation, test and deployment evidence; do not inject the
manual 82% support-check result as a new live observation.

## Code layout after the 2026-09/10 refactor

Zero-behaviour refactor waves (audit items #1–#8; deployed and validated on four drives) moved
code without changing behaviour. Old import paths remain as shims, so existing imports keep working.

- `projects/vehicle_data/historian.py` → facade over `history/`; `early_warning.py` → shim over
  `warning_engine/`; `api.py` → shim over `api_server.py`/`api_client.py`; broker status and
  helper-event logic live in `status_view.py`, `vehicle_state.py`, `helper_events.py`.
  Historian activity/regime decisions live in `history/trip_activity.py`.
- Shared helpers: `lib/candump_io.py`, `lib/capture_pipeline.py` (passive capture engine),
  `lib/broadcast_signals.py` (fixed broadcast decodes and frame IDs), `lib/reports.py`,
  `lib/cli_gates.py`, `lib/uds_session.py`, `lib/diagnostic_preflight.py`, `lib/timeutil.py`.
- For future refactors, run the old-tree/new-tree output scripts in `tests/*_oracle.py`:
  - `tests/gate_oracle.py` records every `--execute`/`--confirm-*` path of 16 tools (882 runs,
    CAN/ADB/process boundaries replaced by a sentinel); compare two runs with `--compare OLD NEW`.
  - The others cover the capture pipeline, broker status/events, active-route effects and the
    advisory lifecycle.
  - Use `tools/warning_replay.py` against an exported history for evaluator changes. `lib/uds.py`, the safety core and the
  protected DDL (`history/schema.py::_create_schema`) were deliberately left unchanged.
  - A read-only history export for replays is at vanpi
    `tmp/vehicle_data/warning_replay/export-20261003.sqlite3`. It has no opened episodes, so pair
    it with the advisory lifecycle oracle.
- Running the tests on the Mac:
  - System Python lacks `can-isotp` and pytest, so use an isolated venv with both
    plus `numpy` (required by the signal-correlate tests). Install `cantools` for
    the optional signal-field cross-checks. Verify the venv still has its packages;
    shared scratch environments may be removed by another session.
  - Point `TMPDIR` at a path that isn't a symlink. The `test_required_mount_*` tests fail under
    macOS's symlinked `/var/folders` temp dir.
  - Gate and oracle runs need `tmp/` and `tmp/ecu_mapping/` in the checkout.
    For byte-identical gate captures, keep the interpreter and venv outside both
    target roots: the gate harness redirects module constants beneath a repo's
    `tmp/`, including interpreter paths if the runtime itself lives there.
- Deliberately not done:
  - Moving `active_drive` onto `acquire_active_bus_route`. It would change the lock order,
    parent/child role ownership, restore revalidation and the `ip` argv, so it needs its own
    design and on-vehicle validation.
  - Collapsing `projects/battery/*_voltage.py`. They use different transports.
  - Deriving the `format.js` unit tables from the catalog. It would change rendering.
- Known gate inconsistencies, left as they are by owner decision:
  - `dtc_batch.py` writes `job.json` before taking its non-blocking lock, so refused runs stay
    auditable.
  - `routine_scan` checks the session before the dry run; `did_sweep` checks it after
    `--execute`.
  - `ecu_discover` accepts session bytes 0x80–0xFF.
  - `live_data` and `ignition_triggered_passive_capture` exit 1 on a gate failure; the other
    tools exit 2.
  - The `alfaobd_controller` subcommands `observe`/`wait` and the campaign/catalog `audit`
    subcommands are ungated.
- Deployment status and open follow-ups:
  - O1 (`0c93cd2`) removed the stale "locally armed" descriptions from the
    Tailscale web unit and `van-dtc-batch.path`. On 2026-10-05, read-only Pi checks
    confirmed installed files byte-identical to master and `NeedDaemonReload=no`.
  - O2 (`21523ec`) was deployed parked/asleep on 2026-10-05; broker instance
    `2026-10-05T12:10:03.822520Z`. Bounded GET workers leave every POST on the
    serving/main thread. Fable's cross-family review approved deployment.
    Matching wake-assisted battery reads reduced maximum in-command status
    latency from 7,821.107 ms to 20.590 ms; both sent exactly 75 B-CAN frames,
    no C/CH TX, and all status reads returned 200. Restart itself added no TX.
    All relevant units were active with zero restarts; final journals were clean
    and all three vehicle roles were healthy/listen-only with static counters.
    Retain the recorder's 20 s idle timeout and `voltage_mon` running skip:
    eight occupied POST slots can still starve GET admission, and GIL contention
    has not been isolated or validated over a drive. The first natural drive is
    still outstanding; see [.agent/handoffs/broker-api-concurrency.md](.agent/handoffs/broker-api-concurrency.md)
    for latency evidence, the rollback SHA/command and timestamp-anchored checks.

## MacBook-managed `~/scripts` tree

`/home/pi/scripts` (equivalently `~/scripts`) is managed from Jacob's MacBook,
which pushes updates into that directory on vanpi. Treat the entire tree as
read-only deployment output on this machine.

- Do not create, edit, delete, format, or copy files under `/home/pi/scripts`,
  including `python-automation/` and `van-dashboard-preview/`.
- Read-only inspection is allowed when diagnosing the deployed dashboard.
- For dashboard changes, give the dashboard agent precise integration
  instructions or work in the authoritative MacBook source tree after its
  location is provided; never patch the vanpi deployment copy as the source of
  truth.
- Do not install or restart dashboard services based on a locally modified
  `~/scripts` file. A MacBook-managed deployment or explicit owner-directed
  emergency procedure must own that change.

Local edits can be overwritten by the next MacBook push and can create a
misleading split between deployed files and their authoritative source.

## Data locations (imposed 2026-07-08; do not resurrect `dumps/` directories)

The offline CAN report CLI is `tools/can_workbench_report.py`; start with
`tools/can_workbench/README.md` for its manifest, pinned adapter contracts, and
verification record. It generates self-contained HTML plus JSON under this
checkout's `tmp/can_workbench/`, using saved inputs only. Field selections remain
investigation-local and do not replace the bus map or verified signal catalogue.
No dashboard/service integration or live CAN access is part of this component.

All machine-written output goes under `tmp/`, which is gitignored wholesale. Nothing a tool writes is
committed in place.

- `tmp/captures/` — raw candump logs and condition-named bus reference captures (`ccan/`, `bcan/`,
  and their `events/` subdirectories)
- `tmp/inventories/` — per-module identity, DTC, checkpointed DID, and routine reports
- `tmp/sweeps/` — completed DID compatibility text and `tools/signal_correlate.py` output
- `tmp/locks/` — advisory logical-role and resolved-channel lock files for participating CAN tools
- `tmp/<project>/` — project logger/tool output such as `tmp/radar/`, `tmp/battery/`, and `tmp/tpms/`

Cold, large, or completed machine evidence may live in the matching tree under
`/mnt/EXFAT512/obd-things/tmp/`. Read [`docs/data-archive.md`](docs/data-archive.md) before treating
a local symlink as missing data or moving archived compute jobs. Verify the mount is the expected
writable EXFAT volume before relying on an archived path. Keep active locks, current service state,
small inventories, benchmark inputs, and artifacts requiring guaranteed access on the Pi.

Committing data is deliberate promotion: move selected evidence into
`projects/<project>/findings/`, beside the analysis that cites it, then commit it there. A file's
location answers whether it is intended to be tracked. New tools must default output under `tmp/` and
may offer an `--out-dir`-style override. See `README.md` section "Data convention" for the rationale.

## Keep durable knowledge synchronized

When verifying a new fact, update its canonical map in the same change and include provenance:

- broadcast frame, decode, or wake/sleep behavior → `docs/bus-map.md`
- ECU addressing, bus, or addressing note → `lib/modules.py`
- DID/service/routine behavior → the relevant project DID map or handoff
- operational campaign state or next step → the relevant project `README.md`

Do not re-derive facts already recorded in these sources. Prefer correcting the canonical source over
adding a competing summary elsewhere.

## Vehicle and live-system safety

- Default to passive/listen-only CAN work. Treat transmission, routines, IO control, coding, DTC
  clearing, and service changes as actuation; follow the root README's safety gates and obtain any
  owner authorization it requires.
- Before manual bus work, inspect the current broker and TPMS service state and
  coordinate the exact logical role in scope. Shared passive observers may
  coexist; stop only a non-cooperating owner or a process blocking the required
  exclusive role/channel lease, never an owner merely because another bus is
  in use. As of 2026-08-21 the role-aware `van-telemetry` unit is installed,
  enabled, and active as the normal passive reconciler/broker. Its active-drive
  feature is enabled, but commissioning occurred with the vehicle asleep, so no
  helper ran and no active CAN path was exercised. The installed
  `tpms-logger` unit matches the tracked role-aware fallback but remains
  disabled/inactive and has not been live-validated; do not start it without
  following its deployment handoff. Read `projects/tpms/README.md` section
  "TPMS logger and telemetry ownership" first because services and campaigns
  evolve.
- C-CAN RF Hub wake traffic can also wake BCM accessory rails and boot the dashcam. Account for this
  observer effect when interpreting parked captures; details live in `docs/bus-map.md`.
- Do not expose the full VIN in tracked output. Use the `OBD_VIN` environment variable where tools
  require it and mask the unique serial in committed material. Raw gitignored logs may contain it.
- Exact live hardware/service state is temporal. Inspect the interface, service, cron, and repository
  state before acting; do not rely solely on an older handoff date.
- Never modify or disable the user's crontab, cron daemon, or unrelated background services without
  explicit permission. Stopping an active participating CAN service for manual
  bus work is the documented exception; restarting or changing enablement still
  follows that service's current deployment handoff.

## Working preferences

- Work research-first at major diagnostic forks: check repo and local OEM resources, search current
  OEM procedures/TSBs and relevant tool behavior, and ask what hardware/access the user has before
  beginning low-level reverse-engineering.
- The van is the user's full-time home and office. Prefer in-place DIY work and tools the user can run;
  shop/dealer drop-off is effectively unavailable unless the user explicitly reconsiders it.

## Claude transition notes

The former repository instruction file was `CLAUDE.md`; it now points here so there is one canonical
set of agent rules. Claude's `.claude/settings.local.json` contains historical local permission entries,
not project knowledge or authorization for Codex. Do not interpret those entries as permission to run
vehicle-affecting commands.
