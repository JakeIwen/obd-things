# Local Codex warning conversations

This integration adds **Ask Codex** to every supported Early Warning card,
including unresolved advisory episodes, current watches and sample-filter
events under Recovered Events. Clicking the card also opens the dialog, while
text selection and existing interactive controls keep their normal behavior.

## User experience

- Connect each browser/origin once with the local assistant access code.
- The first click creates an explanation; reopening the event restores its
  latest conversation instead of generating another paid/model response.
- Ask follow-ups in the dialog. Send, Stop, Retry Answer, Reconnect and New Chat
  handle the normal lifecycle. New Chat preserves the prior saved transcript.
- Closing the dialog leaves an in-progress response running. Reopen the event
  to see it. Polling pauses when the page is hidden.
- X, Escape, and the backdrop close the dialog. Model and Effort apply to the
  next question; answers record actual settings. Default / High resolves the
  local Codex default (currently `gpt-6-astra`) and explicitly requests High.
  Choices and allowed efforts come from Codex's local visible model catalog.
- Transcripts live privately on the Pi, keyed to the browser's random secret.
  Different LAN/Tailscale URLs are different browser origins and pair separately.
- The model gets a frozen, dated event evidence packet. It must not claim that
  old data describes the current vehicle. Follow-ups continue that context;
  New Chat refreshes cached evidence when the event is still available.

The browser never receives the ChatGPT login credentials or unrelated Codex
threads. The assistant access code is a separate local capability, **not an
OpenAI API key**. HTTP does not encrypt it: use a trusted LAN or, preferably,
the encrypted Tailscale path. Do not port-forward the dashboard.

## Architecture and boundaries

`warning_chat.py` is a separate, low-priority single-worker service, listening
only on `/run/van-telemetry-advisor/api.sock` (mode 0600). The web server proxies
only the fixed `/v1/assistant/` routes. Exact Host/Origin checks, JSON-only POSTs,
an access code and a separate random browser key protect the bridge. There is
no generic command, path, tool or native-Codex-thread parameter. Model/effort
are validated against local metadata, never accepted as arbitrary configuration.

The worker resolves event IDs against the broker's cached `/v1/health` data,
and adds the relevant cached metric/definition, vehicle evidence and cached
history comparison. It never requests an acquisition or queries raw SQLite.
Evidence is bounded to 48 KB and unique VIN-shaped strings are masked. The
explanation instructions distinguish a historical open episode from a current
fault and forbid inventing an OEM limit or reconstructing a regime-matched
trigger threshold from whole-trip averages.

The runner uses the installed `codex exec --json --ephemeral` interface and
replays the bounded conversation. It reuses Codex's existing ChatGPT sign-in,
not a new API key. CLI/user-config isolation prevents personal MCP servers,
plugins, hooks, shell tools, browser/computer/image tools, goals or child
agents from being enabled. A named read-only filesystem profile exposes only
platform-minimal paths and the empty temporary workspace; tool networking is
disabled. The service also excludes AF_CAN and has no device access. Its
ordinary outbound network access is for Codex inference/authentication, which
uses the owner's existing Codex allowance.

Only the configured default model and reviewed OpenAI transport/retry options
are inherited; explicit selections override model/effort. Alternate custom endpoints/credentials fail closed pending
review. The current `openai_resilient` provider contains supported OpenAI
transport options. No model-generated command is passed to a shell. Only final
agent-message text is returned; reasoning, raw stderr and tool output are not
exposed to the browser. Replies render as plain text, not executable HTML.

The advisor now reuses the existing Codex SQLite index: `sqlite_home` is the
existing `CODEX_HOME` (normally `/home/pi/.codex`), or the owner's explicit
absolute `sqlite_home` setting if present. It no longer points at the advisor's
private runtime directory. This avoids a separate history backfill while keeping
`--ephemeral`, history persistence disabled, and all model tool restrictions.
Reusing the index does not resume an unrelated thread or put its content into
the model prompt. Chats, approved rules, temporary workspaces and log-directory
output still live in the advisor StateDirectory. The old duplicate databases
are unused but deliberately left intact; no history or credentials are copied,
deleted, or reset. The installed unit already permits the existing Codex runtime
to maintain its index under `/home/pi/.codex`.

Bounds: one model job at a time; ten starts per minute globally; 300-second
turn timeout; 2,000-character questions; 12,000-character answers; 24 turns per
conversation; 160 KB assembled prompt; 2 MB process output; 200 stored chats.
An unavailable/failed worker does not block the broker or normal dashboard
updates. A restart marks interrupted answers retryable instead of silently
rerunning them. The chat does not acknowledge/resolve warnings or modify any
vehicle state.

The exact “Codex took too long. You can retry this question” error is the
runner's **overall subprocess deadline**, previously 180 seconds, now 300.
It includes generation and provider retries. The browser's separate 20-second
HTTP deadline applies only to submitting/polling the background job, not model
generation, and has a different error message.

### Startup and turn timing diagnostics

Each new turn saves a `diagnostics` record alongside its transcript, including
on timeout, cancellation or startup failure. Fixed milestone names record
command preparation, process launch, stdin delivery, first stdout/stderr,
`thread.started`, `turn.started`, first model-item activity, first final answer,
`turn.completed`, and process exit. Times are monotonic milliseconds from the
start of the run; missing milestones stay absent rather than becoming zero.
Child CPU time and available cgroup CPU/throttling deltas are recorded at exit.
The original five-minute limit and 30% CPU quota are unchanged.

Timing Details in the dialog shows the latest turn's milestones and expands
automatically on error. Progress updates every five seconds in memory; stage
transitions and final diagnostics are saved atomically. Progress-only updates
do not rebuild the transcript DOM. JSON `warning_codex_timing` records in the
advisor's journal preserve stage transitions, even if a later service restart
interrupts the turn. They contain generated run IDs, fixed labels, numeric
counts/times and bounded diagnostic categories only—not prompts, answers,
reasoning, raw stderr, credentials, paths or unrelated Codex thread IDs.

**These are local CLI observations, not HTTP transport instrumentation.** In
particular, `turn.started` does not prove that a remote server accepted a request.
The display and phase-specific timeout message distinguish startup, waiting for
a turn, waiting after turn start, response activity and process shutdown without
claiming a precise remote dispatch timestamp. Error-buffer classification can
add fixed filesystem/authentication/configuration/connection/retrying/backfill/
rate-limit signals; raw buffer content is never retained or logged.

For this indexing/timing update on an already-installed advisor, only run:

```bash
sudo systemctl restart van-telemetry-advisor.service
```

Reload the page and retry a question. No broker, web, CAN or Tailscale restart
is needed for this particular update. Inspect safe timing records with:

```bash
journalctl -u van-telemetry-advisor.service --since '10 minutes ago' -o cat --no-pager
```

Implementation and synthetic tests are complete. After the host-side update,
the owner reported that a real question completed successfully (September 20
local). This supports duplicate indexing as the timeout cause; the agent did
not independently capture that successful turn's timing trace. The managed
coding session cannot restart the host service itself.

### Live timeout diagnosis (September 20)

Owner restarted advisor at 23:33:52 UTC. A subsequent Astra/High turn ran from
23:45:30.796 to 23:50:31.050 UTC and hit the updated 300-second limit. Thus the
second failure was not stale deployment or the browser's connection timer.
Read-only inspection found the advisor cgroup used 89.84 CPU-seconds with
196.48 seconds of throttling under CPUQuota=30%. Its separate runtime state DB
had imported 240 existing Codex threads and backfill_state remained running,
last progressing at 23:49:22 UTC, without a successful completion timestamp.
The existing Codex sessions tree measured approximately 2 GB; the normal Codex
state DB's backfill was already complete. No conversation contents or auth
material were needed for these checks.

Strongest current explanation: overriding sqlite_home to an initially empty
advisor DB while retaining the existing CODEX_HOME triggers expensive duplicate
history indexing, aggravated by the CPU quota. It is not established whether
an inference request also started: the runner does not persist phase timings or
its bounded diagnostic buffer on timeout, and the advisor log DB was empty.
The follow-up implementation above reuses the normal index and records safe
startup/turn diagnostics. It retains login, model restrictions and the five-minute
limit; a live retry after advisor restart is still required. No database,
credential, quota or service was changed during this diagnosis; no live retry
was initiated. Do not merely raise the timeout or delete the user's history.

## Add Early Warning

Add Early Warning is available even without active events. A dedicated setup
chat produces structured threshold-rule proposals using established numeric
metrics and native units, above/below/absolute-magnitude-above comparisons,
distinct-observation persistence, maximum sample age/gap, a bounded window,
and optional fresh RPM >400 gating at each observation. Unsupported compound
logic or unmapped metrics requires a separate development task; Codex must not
silently approximate it.

Only the latest completed, validated proposal can be installed: the full rule
and units are shown, and its exact proposal ID plus explicit owner approval are
required. Ordinary explanation mode cannot install rules. Revisions create new
proposals, not edits to existing approved rules. Saved Custom Warnings lists
this browser's rules and allows removal, including from older conversations.
Removal does not erase historical advisories.

The application atomically writes approved data configuration (not executable
code) to `/var/lib/van-telemetry-advisor/early-warnings.json`, maximum 32 rules.
The upgraded broker reloads it during existing background warning evaluation;
no acquisition or service restart is requested by chat. Custom assessments join
the normal episode/notification pipeline. Saved versus broker-loaded status is
explicit. Malformed custom configuration does not suppress built-in warnings.

Fresh source/unit/quality checks, distinct observations, gap limits and optional
RPM evidence prevent stale, candidate or duplicate data from satisfying a rule.
Thresholds are owner monitoring references, not OEM limits or diagnoses. Chat
cannot remove built-in warnings, and Codex receives no shell, app-source,
filesystem-write, CAN or service-control tools. Approved configuration is the
bounded app-modification path.

API surface (all require the local capability and browser key):

- `GET /v1/assistant/status`
- `POST /v1/assistant/chats` — event selector, request ID, optional `new_chat`
- `GET /v1/assistant/chats/<id>`
- `POST /v1/assistant/chats/<id>/messages` — message or explicit retry
- `POST /v1/assistant/chats/<id>/cancel`
- `POST /v1/assistant/chats/<id>/apply-warning` or `/remove-warning`
- `POST /v1/assistant/warnings/<proposal-id>/remove` — owner-scoped removal

The UI script loads only when the web server advertises
`warning_chat_enabled`, so serving newer static HTML from an older web process
does not break the existing dashboard.

## Activation on vanpi

**Advisor installed; owner reports successful chat after the indexing fix.**
The owner previously restarted it at 17:33 MDT and confirmed a full 300-second
timeout. The managed session cannot restart system units, and its earlier real Codex
smoke test failed before answering with `failed to initialize in-process
app-server client: Read-only file system`. Do not bypass the coding sandbox or
weaken the model's permissions. Run these from the ordinary host shell:

```bash
sudo install -m 0644 /home/pi/dev/obd-things/projects/vehicle_data/systemd/van-telemetry-advisor.service /etc/systemd/system/van-telemetry-advisor.service
sudo systemctl daemon-reload
sudo systemctl enable --now van-telemetry-advisor.service
sudo systemctl restart van-telemetry-web.service van-telemetry-web-tailscale.service
```

For an already-installed advisor, restart the advisor and both web listeners
for the new timeout/settings/setup routes. The custom evaluator additionally
requires **one owner-performed parked broker restart with no active drive or
diagnostic work**. Subsequent rule additions/removals need no restart. Do not
change CAN interfaces. The unit permits Codex
to maintain its own `/home/pi/.codex` sign-in/runtime files, while keeping the
model's tool permissions restricted. It uses `StateDirectory` for chats and
the code, CPUQuota 30%, Nice 15, idle I/O scheduling and MemoryMax 768M.

Check the unit status/journal, then read
`/var/lib/van-telemetry-advisor/access-code` locally and paste it into the
dialog's password field. Do not paste it into an agent conversation or commit
it. The file belongs to `pi` and is mode 0600.

Chat accepts the listener's literal IP origin by default. If using a DNS URL,
add its exact origin to that web unit's existing ExecStart, for example
`--warning-chat-origin http://vanpi.lan:8765`; preserve all existing bind and
DTC options. Do not replace machine-local units with generic tracked ones.
`--no-warning-chat` disables this feature on a selected web listener.

Verify a real explanation and a follow-up after activation, then test Stop
and reopening. Confirm the normal telemetry stream stays responsive. Until
that check succeeds, report the integration as staged, not operational.

## Verification and sources

The isolated tests cover event lookup, cached-only telemetry access, strict
requests, code authentication, browser ownership, idempotency, global job
serialization, cancellation, restart recovery, new conversations, bounded
subprocess handling and same-origin web proxy behavior. Browser fixture
`tests/fixtures/warning_chat_preview.py` runs on loopback with a deterministic
responder and **no vehicle or real Codex access**. Its credential is fixture-only.
Browser tests exercised pairing, follow-ups, reopen, failed-answer retry,
cancellation, plain-text injection handling and 800/390-pixel layouts.
The updated setup fixture also passed proposal review, exact approval,
saved-versus-loaded display, owner removal, changed model/effort on the next
turn, X/Escape closing and reopen. The approved proposal collapses to keep
chat usable on small screens. Full suite: 1,211 passed, 4 skipped, 824 subtests;
later dashboard-focused verification: 62 passed, 25 subtests. No test created
a live warning or requested vehicle data beyond cached GETs.

Index-reuse/timing follow-up verification: 1,217 passed, 4 skipped, 827 subtests.
New subprocess fixtures cover ordered milestones, error/timeout phase reporting,
normal-index configuration, private diagnostic redaction, checkpoint/reopen
persistence and bounded heartbeat/cgroup counters. Browser timing fixture
verified success, startup timeout, automatic expansion, reopen and a 390px
layout without overflow. No real model answer was generated by these checks;
the owner subsequently reported a successful live retry.

The installed CLI was checked at version 0.153.4, including generated protocol
schemas. The JSONL and ephemeral behavior follow the official
[Codex noninteractive documentation](https://developers.openai.com/codex/noninteractive);
permission and feature controls follow the
[configuration reference](https://developers.openai.com/codex/config-reference).
