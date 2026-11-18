# Local Codex warning conversations

This integration adds **Ask Codex** to every supported Early Warning card,
including unresolved advisory episodes, current watches and sample-filter
events under Recovered Events. Clicking the card also opens the dialog, while
text selection and existing interactive controls keep their normal behavior.

## User experience

- Connect each browser/origin once with the local assistant access code: paste it, or type
  a one-time 6-digit PIN that an already connected browser shows under **Pair another device**
  (see "Pairing another device" below). The PIN field comes first, for the driving tablet.
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
silently approximate it. The catalog includes `generator.field_duty` (the PCM's
commanded alternator field duty, not current or temperature); built-in warnings
still never use it as primary evidence, but a rule the owner defines himself may,
and the setup prompt tells the model to say what it is. The advisor and the
broker both read the catalog. When a metric joins it, restart and verify the broker
first and the advisor second: the broker rejects the whole rules file, not one rule,
when it meets a metric it does not know, so a rule saved by a newer advisor would
switch off every custom warning until the broker catches up. If the guarded broker
restart cannot run, leave the advisor as it is.

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

The same advisor also serves the diagnostic-code research routes (`/v1/assistant/models`,
`/v1/assistant/dtc-research*`; contract in [DTC research](dtc-research.md)). A warning-chat
Codex and a research CLI never run at the same time: the warning chat's slot
(`assistant_slot.ModelSlot`) also takes the shared CLI lock `/run/van-assistant/model-cli.lock`,
and while research holds it a new question gets `429` "A diagnostic-code research job is running;
ask again when it finishes". When the lock file is absent (research not installed) the warning
chat carries on without it.

API surface (all require the local capability and browser key):

- `GET /v1/assistant/status`
- `POST /v1/assistant/chats` — event selector, request ID, optional `new_chat`
- `GET /v1/assistant/chats/<id>`
- `POST /v1/assistant/chats/<id>/messages` — message or explicit retry
- `POST /v1/assistant/chats/<id>/cancel`
- `POST /v1/assistant/chats/<id>/apply-warning` or `/remove-warning`
- `POST /v1/assistant/warnings/<proposal-id>/remove` — owner-scoped removal

The dashboard (`dashboard/src/dialogs/WarningChat.jsx`, served by `web_v2.py`)
offers Ask Codex only when the listener advertises `warning_chat_enabled`, so a
listener started with `--no-warning-chat` simply hides it.

## Pairing another device

Owner request (2026-11-17): the driving tablet (Android, portrait, on-screen keyboard, no shared
clipboard) cannot receive the 43-character access code, and guests on the van Wi-Fi must stay out.
A browser that already holds the code shows a one-time 6-digit PIN; the new device types it once
and receives the code, which it stores exactly as if pasted. Both dialogs share it.

`assistant_pairing.json` is the contract (routes, actions, states, reasons, limits) read by
`assistant_pairing.py` and the dashboard (`src/assistantAccess.js`, `src/assistantPairing.js`).

| Route | Needs the code | Body | Reply |
| --- | --- | --- | --- |
| `GET /v1/assistant/pairing` | yes | none | `{available, pin_id, state, expires_in_s, locked_for_s}` |
| `POST /v1/assistant/pairing` | yes | exactly `{action: "create"}` | `{available, pin, pin_id, expires_in_s}`; 429 `pairing_locked` while locked |
| `POST /v1/assistant/pairing` | yes | exactly `{action: "cancel", pin_id}` | the status; cancels only the named active PIN; idempotent |
| `POST /v1/assistant/pairing/redeem` | **no** | exactly `{pin: "<6 ASCII digits>"}` | 200 `{available, access_code}`; 403 `pin_rejected`; 429 `pairing_locked` + `retry_after_s`; 400 `invalid_request` |

`state` is `none` (nothing made since the advisor started), `active`, `redeemed`, `expired`,
`wrong_tries` (burned by the per-PIN limit or a lockout) or `cancelled`. `pin_id` (16 hex) goes
only to code holders. The PIN travels only in a body, never a path or query string, so no request
line logged anywhere can carry it.

Redeem is the only assistant route relayed without the code: the web relay matches the exact
method and path (prefix and suffix variants are 404), forwards no assistant header for it, and
keeps the same-origin, `Origin`-required, JSON and size (64-byte) gates. In the advisor, only that
exact path is handled before `authorize`; every other route, the owner pairing route included,
still answers 401 without the code at both hops.

Limits (all in memory; an advisor restart drops the PIN and the window):

- One active PIN; `create` replaces it (the old one becomes `cancelled`). 10^6 values from
  `secrets.randbelow`, leading zeros kept. Lifetime 300 s on the monotonic clock, single use (of
  two simultaneous correct redeems exactly one gets the code). 5 failed redeems burn a PIN.
- One failure window for every listener and client: 10 failures in a rolling 900 s lock redeem
  and `create` and burn the active PIN. Refusals while locked are not counted, so the lock lifts
  when the oldest failure is 900 s old.
- A request that fails transport or JSON parsing, or is not exactly `{pin: <6 ASCII digits>}`, is
  refused with its own status (403 for a failed origin check and 415 for a non-JSON type at the
  relay, 400 for a bad size, bad JSON or the wrong shape) and is **not counted**: it never reaches a comparison, so it gives no extra guesses and says nothing about a
  PIN. A well-formed PIN that is wrong, expired, burned or offered while no PIN is active gets the
  one identical 403 (same status and body, no attempts-left count) and **is counted**.
- The match is `hmac.compare_digest` on the 6 ASCII bytes; with no PIN active the advisor compares
  against a dummy so both cases do the same comparison work. This is not a claim that whole-request
  timing is constant (lock waits and bookkeeping differ by microseconds, well below LAN jitter).
  The 429 reveals only that 10 failures happened, which is true whether or not a PIN exists.

Arithmetic: a guest who knows a PIN is active gets at most 5 guesses on it, 1 in 200,000. New PINs
give no extra guesses, because the window caps all guessing at 10 per 15 minutes: at most 10 in
10^6 per 15 minutes even with a PIN always active (about 0.1 % per day in that unrealistic case).
A PIN is realistically active for a minute or two a few times a year.

Denial of service (accepted): a guest who spends 10 guesses every 15 minutes keeps pairing locked.
The paste path still works, and the lock is visible (the panel says pairing is paused; the journal
shows `locked`).

Logging: `assistant_pairing.py` writes one JSON line per event to the advisor's stderr,
`{"event": "assistant_pairing", "action": created|redeemed|rejected|burned|locked|unlocked|cancelled|expired,
"failures_in_window": n}`, never the PIN, the `pin_id` or the code; malformed requests and
refusals while locked are not logged one by one. Separately, the web listener still logs one
request line per HTTP request (client address, method, path, status), as for every route; it
holds no header or body, so no PIN or code.

Threat model:

- **LAN guest** (curl or a browser on the van Wi-Fi): can call redeem and forge Host and Origin
  from curl, so only the PIN and the limits stand in the way. Can lock pairing and see that 10
  failures happened. Cannot reach any other assistant route without the code, and a rejection does
  not say whether a PIN is active. An active attacker on the LAN could already read the code from
  any assistant request (plain HTTP); a passive sniffer learns nothing new. Whoever reads the PIN
  off the owner's screen and types it first gets the code; the tablet then sees a rejection while
  the panel says "Device paired", the cue to rotate the code.
- **Tailscale peer**: the same, over an encrypted path, sharing the one failure window.
- **Hostile web page**: cannot send the JSON POST (no preflight is ever granted; a mismatched
  `Origin` is 403) or read a reply. DNS rebinding carries an untrusted Host: 403.
- **Journal reader**: sees pairing timing and counts only.

UI: the unpaired form shows the PIN field first (`inputmode="numeric"`,
`autocomplete="one-time-code"`, focused), then "Or paste the access code". A connected dialog has
a collapsed **Pair another device**: opening it makes a PIN shown as `042 917` in large digits
with a countdown, polls the status every 2 s, and stops on `redeemed` ("Device paired"),
`expired`, `wrong_tries`, a replaced `pin_id` or a lock. Closing it, or the dialog, cancels the
PIN. A newly paired device keeps its own chat history and saved-warning list (the client id is not
transferred).

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
it. The file belongs to `pi` and is mode 0600. Other devices then pair with a
PIN from that browser (see "Pairing another device").

Rotating the code (also the response to a suspected leak, or to a "Device paired"
the owner did not cause):

```bash
sudo systemctl stop van-telemetry-advisor && sudo rm -f /var/lib/van-telemetry-advisor/access-code && sudo systemctl start van-telemetry-advisor
```

Every browser then gets 401 and re-pairs: paste the new code on one, PINs on the rest.

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


## Historical event evidence (September 21 UTC)

Episode explanations now use the separate bounded event-detail reader, including
resolved events by ID. Opening, first warning, last usable, latest assessment,
rule/baseline provenance, completeness and lifecycle records remain distinct.
The same versioned system guide is provided to the UI and agent. Each turn
records its evidence revision/date; Refresh Evidence & New Chat preserves the
old packet. Baseline input arrays are omitted from prompts with explicit archive
references, and additional bounded omissions are labeled. See
[event history](event-history.md) for the authoritative data/lifecycle contract,
validation, and pending broker/web/advisor activation. The agent's command,
file, model-tool, and CAN restrictions remain in force.
