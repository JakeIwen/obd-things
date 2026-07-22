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
   not disposable output. As of the Claude-to-Codex transition, the bus-wake refactor and TPMS drive
   tooling are uncommitted; inspect the current status/diff rather than assuming this note is current.
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

## Data locations (imposed 2026-07-08; do not resurrect `dumps/` directories)

All machine-written output goes under `tmp/`, which is gitignored wholesale. Nothing a tool writes is
committed in place.

- `tmp/captures/` — raw candump logs and condition-named bus reference captures (`ccan/`, `bcan/`,
  and their `events/` subdirectories)
- `tmp/inventories/` — per-module identity, DTC, checkpointed DID, and routine reports
- `tmp/sweeps/` — completed DID compatibility text and `tools/signal_correlate.py` output
- `tmp/locks/` — advisory per-channel lock files for participating active diagnostic tools
- `tmp/<project>/` — project logger/tool output such as `tmp/radar/`, `tmp/battery/`, and `tmp/tpms/`

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
- Before manual bus work, stop the background TPMS logger with
  `sudo systemctl stop tpms-logger`; restart it afterward. Read `projects/tpms/README.md` section
  "Infrastructure now running" first because services and campaigns evolve.
- C-CAN RF Hub wake traffic can also wake BCM accessory rails and boot the dashcam. Account for this
  observer effect when interpreting parked captures; details live in `docs/bus-map.md`.
- Do not expose the full VIN in tracked output. Use the `OBD_VIN` environment variable where tools
  require it and mask the unique serial in committed material. Raw gitignored logs may contain it.
- Exact live hardware/service state is temporal. Inspect the interface, service, cron, and repository
  state before acting; do not rely solely on an older handoff date.
- Never modify or disable the user's crontab, cron daemon, or unrelated background services without
  explicit permission. Stopping/restarting `tpms-logger` for manual bus work is the documented exception.

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
