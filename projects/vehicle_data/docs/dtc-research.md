# DTC research with AI

Each code on the dashboard's Codes card can be researched by a model the owner picks. The model
searches the web for what that code means on this van and writes a report; the report is kept on
the Pi, its one-line summary shows on the code's row, and the owner can ask follow-ups in the same
model session. Status (2026-11-16): built and reviewed in plan; not yet deployed. This is a partial
milestone: **Claude only**. Codex research is blocked (below) until a pinned Codex version passes
the tool-inventory check.

The workstream's handoff note (`.agent/handoffs/dtc-research.md`) holds the deploy runbook while
the work is open; this file is the standing contract.

## Who does what

| Part | Process | Owns |
| --- | --- | --- |
| Routes, records, ledger, catalog | advisor (`warning_chat.py`, `van-telemetry-advisor.service`) | `dtc_research.py` (routes and following each attempt), `dtc_research_store.py`, `dtc_research_ledger.py`, `dtc_research_reconcile.py`, `dtc_research_context.py`, `dtc_research_prompt.py`, `assistant_models.py`, `dtc_research_service.py` (wiring) |
| Launching research CLIs | research worker (`dtc_research_worker.py`, `van-telemetry-research.service`) | `research_attempts.py` (the slot), `research_runner.py`, `research_cli.py`, `research_cli_policy.py`, `research_memory.py`, `research_volume.py`, `research_maintenance.py`, `cli_process.py` |
| Installing the CLI copy | root, by the runbook | `research_cli_install.py` (`systemd/install_research_cli.sh`) |
| Owner sign-in | owner shell | `dtc_research_signin.py` (`s dtc_research_signin claude`) |
| Shared vocabulary | advisor, worker, dashboard | `dtc_research.json` (+ `dtc_research_contract.py`) |
| Warning-chat / research exclusion | both services | `model_cli_lock.py`, `assistant_slot.py`, `systemd/van-assistant.tmpfiles.conf` |

The advisor never launches a research CLI; the worker never talks to the broker. The model gets
only web search and web fetch.

## Routes (all behind the access code and browser key, relayed by `web.py`)

- `GET /v1/assistant/models` → `{available, refreshing, groups: [{provider, label, available, reason,
  source: discovered|fallback, checked_at, models: [{id, label, efforts, default_effort}]}],
  default: {provider, model, effort} | null}`. A model with no efforts is started with
  `effort: null`. While `refreshing`, the UI re-fetches every 2 s, at most 15 times.
- `GET /v1/assistant/dtc-research` → `{available, items: [{module_key, fca_display, run_id, state, busy,
  summary, summary_note, provider, model, effort, updated_at, runs}]}` (one per code, newest first).
- `GET /v1/assistant/dtc-research/code/<module_key>/<fca_display>` → `{available, module_key,
  fca_display, runs: [RunSummary…], selected: Run | null}`.
- `GET /v1/assistant/dtc-research/run/<run_id>` → `{available, run: Run}`;
  `…/run/<run_id>/progress` → `{available, progress: Progress}`.
- `POST /v1/assistant/dtc-research` with exactly `{action: "start", module_key, fca_display, provider,
  model, effort, request_id}`, `{action: "ask", run_id, question, request_id}` (1–2,000 characters)
  or `{action: "cancel", run_id, request_id}` → `{available, run: Run}`. A repeated `request_id`
  returns the same run and starts nothing.

`RunSummary` = `{run_id, module_key, fca_display, created_at, updated_at, provider, model, effort,
state (running|done|failed|cancelled), busy, summary, summary_note (null|missing|truncated),
duration_s, error, reason, turn_count, can_continue, continue_mode (resume|replay|none)}`;
`Progress` = `{run_id, state, busy, phase, progress, activity: {searches, pages}, elapsed_s,
turn_count, updated_at}`; `Run` = `RunSummary` plus `{phase, progress, activity, elapsed_s,
format_warnings, turns}`. Turns, report first: assistant `{role, kind: report|answer, text, at,
state, error, reason, mode: initial|resumed|replayed, duration_s}` and user `{role, kind: question,
text, at}`.

Errors: `{available: false, reason, detail}` with `400 invalid_request`, `404 not_found`,
`409 run_busy | turn_limit | record_full | cannot_continue`, `429 busy | rate_limited |
daily_limit`, `503 provider_unavailable | worker_unavailable | storage_error`. `busy` covers another
research job (report, follow-up, model check, install maintenance), a warning chat answering a
question, the advisor still checking on the worker after a restart, and low free memory; `detail`
says which. Turn reasons: `timeout, cancelled, interrupted, worker_unavailable, provider_error,
auth, upstream_rate_limit, budget_exceeded, policy_violation, protocol_error, space_exhausted,
memory_exhausted, daily_limit`.

Polling: while a dialog shows a busy run, poll `/progress` every second and fetch the run when
`turn_count`, `state` or `busy` changes; fetch the list when the Codes card mounts, after a dialog
closes and when a run stops being busy.

Sizes (as encoded, worst case 12 bytes per character): report 96 KiB, answer 48 KiB, question
12 KiB; one run 384 KiB (a follow-up is refused with `record_full` when its worst case would not
fit); 60 runs in total. A code reply is under 600 KiB, under the relay's 1 MiB limit
(`tests/test_dtc_research_relay.py` checks this through the real relay).

## What the model is given

The browser supplies only the code's key. The advisor gathers: the code's record from the broker's
cached `/v1/diagnostics/dtcs` (Pi scan, in-vehicle scan or drive read), the owner's ignore note for
the code, the module registry entry, the description-table entry, and the hand-reviewed
`dtc_research_facts.json` (vehicle facts; per-code owner notes, seeded with the LED-lamp codes).
VIN-shaped strings are masked everywhere. Residual: the owner's own text (his follow-up questions,
his ignore note) is passed as he wrote it, so a location he types reaches the model provider and
possibly a search query. Nothing else is promised. The system text tells the model web pages are
untrusted and asks for the report format (`SUMMARY:` line, then What it means / Likely causes on
this van / What to check / Sources).

## The research worker's sandbox

`van-telemetry-research.service`: user `pi`; home is an empty read-only tmpfs except the checkout
(read-only, minus `tmp/`, `.git` and `AGENTS.secret.md`); no access to the owner's `~/.codex`,
`~/.claude`, `~/secrets`, the advisor's state (records, access code), the broker's state, or the
sockets under `/run/van-telemetry`, `/run/van-telemetry-advisor`, `/run/dbus`, `/run/user`,
`/run/tailscale`; writes only to the research volume; a 32 MiB tmpfs `/tmp`; no LAN, Tailscale,
link-local or loopback addresses (`IPAddressDeny`, enforced at connect time); public DNS only
(`systemd/research-resolv.conf`); no CAN, packet or netlink sockets; `OOMScoreAdjust=800`;
`Type=notify` with a 30 s watchdog pinged from the serving loop; `KillMode=control-group`.

Claude runs from a research-owned root copy under `/opt/van-research-cli/claude/<version>/`, with
its own home per run, `CLAUDE_CODE_OAUTH_TOKEN` from the volume's token file, no settings, no MCP,
no slash commands, `--restricted`, `--tools WebSearch,WebFetch` with `dontAsk` (the pre-execution
gates), a per-turn `--max-budget-usd` (5 report, 2 follow-up). Stream checks are a supplement: an
init event offering another tool, a call to one, a stdout line that is not JSON, or more than
16 MiB of output stops the turn. A copy is usable only when its whole installed file set hashes to
the digest its passing verification was recorded against (`identity.json`).

**Codex is blocked.** Codex 0.167 still offers local tools (`exec`, `wait`, `request_user_input`,
the collaboration tools) with every tool feature disabled, as its rollout's `additional_tools`
record shows; research is web-only, so the policy disables Codex. Enabling it (follow-up F3) needs a
review of a pinned version's tool construction, a verification step that fails on a missing,
malformed or incomplete `additional_tools` record and covers every offered model, and wiring the
Codex launch path in `research_runner.py` (the argv builder, stream reader and tool-record parser in
`research_cli.py` exist and are tested).

## One CLI at a time

- The worker's in-process slot admits one attempt (turn, model check or installer maintenance) at
  a time; it is taken before the launch request is acknowledged and released only after the child
  process group is reaped. Each attempt has an `attempt_id`; the worker remembers every one it
  admitted (4,096 tombstones) and never launches one twice. The advisor sends a launch for an
  attempt at most once and afterwards only asks about it; a launch carries the worker instance it
  expects. An attempt the worker does not know, or whose outcome it dropped, becomes `interrupted`.
- The warning chat and research exclude each other with a `flock` on
  `/run/van-assistant/model-cli.lock`, held by the launching process from just before spawning
  until the child is reaped. If a holder dies, the kernel releases the lock and systemd kills the
  holder's child with its control group; until that stop completes (up to the unit's
  `TimeoutStopSec`, 10 s) a second CLI can start beside the dying one. That crash-path overlap is
  an accepted limitation, not a "never overlap" guarantee.
- After an advisor restart research admission stays closed until the worker has said what it
  holds; nothing is inferred from a timeout or a missing path.

## Memory: best-effort protection, not isolation

This Pi boots with `cgroup_disable=memory`, so no unit's `MemoryMax` is enforced; a hard bound on a
runaway CLI needs the memory controller (an owner decision for the whole Pi). The worker refuses to
launch below a free-memory floor and SIGKILLs the attempt's process group at once (no grace) when
the tree passes its cap or free memory falls to critical. The thresholds come from
`research_cli_policy.json`; until real peaks have been measured **for the installed Claude copy**
(`memory.cli_digest`), research turns are refused (`not_activated`). Installing a different copy
requires measuring again. Provisional values for the measurement itself: floor 900 MiB, cap
700 MiB, critical 300 MiB.

## Storage and its bounds

- Advisor: `/var/lib/van-telemetry-advisor/dtc-research/runs/*.json` (at most 60 records of at most
  420 KiB, about 25 MiB) and `admissions.json` (the rolling 24-hour launch ledger: at most 60
  entries, about 9 KiB at its largest; refused launches are removed at once and never counted).
- Worker: everything research CLIs write is on `/var/lib/vanresearch`, a 224 MiB preallocated
  ext4 image (`/var/lib/vanresearch.img`) loop-mounted `nodev,nosuid,noexec`: the token, the
  private Codex home, per-run homes and sessions, temp files. Its SD footprint never grows. The
  worker keeps sessions under 128 MiB in total and 16 MiB per run (oldest dropped first; those runs
  replay), deletes Codex logs past 48 MiB, and kills a turn when free space falls under 16 MiB.
- CLI copies: `/opt/van-research-cli` (Claude about 348 MiB, plus at most one staged release).

## Deploy, activation and rollback

Root steps, in order: `systemd/install_dtc_research.sh` (image, tmpfiles lock, units, mount, worker,
advisor restart; `--dry-run` prints them), restart both web listeners, then
`systemd/install_research_cli.sh claude` (stage, worker verification inside a maintenance
reservation that is confirmed still open before promotion, promotion, identity record, worker
restart). The owner signs in (`claude setup-token`, then `s dtc_research_signin claude`). Then
`install_research_cli.sh claude --measure` and `measure_assistant_peak` give the peaks that a
reviewed commit records in `research_cli_policy.json`; deploying it activates research.
`--budget-check` shows whether the subscription CLI enforces the cost cap. The exact runbook with
checks and stop conditions is in the workstream note.

Rollback: disable `van-telemetry-research.service` and `var-lib-vanresearch.mount`, reinstall the
previous advisor unit and code, restart the advisor and the web listeners (research routes then
404 and the dashboard hides its controls). Remove data and credentials by revoking the Claude token
(claude.ai account settings), deleting `/var/lib/vanresearch.img`, `/opt/van-research-cli` and
`/var/lib/van-telemetry-advisor/dtc-research`.

Tests: `tests/test_research_worker.py`, `test_dtc_research_manager.py`, `test_dtc_research_relay.py`,
`test_dtc_research_pure.py`, `test_assistant_slot.py`, `test_research_cli_install.py`,
`test_dtc_research_units.py`, with the scripted fake CLI in `tests/fake_research_cli.py`.
