# DTC research with AI

Each code on the dashboard's Codes card can be researched by a model the owner picks. The model
searches the web for what that code means on this van and writes a report; the report is kept on
the Pi, its one-line summary shows on the code's row, and the owner can ask follow-ups in the same
model session. Status (2026-11-17): merged, installed on vanpi with the worker-status fix
described under "Worker status", and activated for Claude 2.1.325 by the measured memory policy
(see "Memory: two bounds"). This is a partial milestone: **Claude only**. Codex research is blocked (below) until a pinned Codex version passes
the tool-inventory check.

The workstream's handoff note (`.agent/handoffs/dtc-research.md`) holds the deploy runbook while
the work is open; this file is the standing contract.

## Who does what

| Part | Process | Owns |
| --- | --- | --- |
| Routes, records, ledger, catalog | advisor (`warning_chat.py`, `van-telemetry-advisor.service`) | `dtc_research.py` (routes and following each attempt), `dtc_research_store.py`, `dtc_research_ledger.py`, `dtc_research_reconcile.py`, `dtc_research_context.py`, `dtc_research_prompt.py`, `assistant_models.py`, `dtc_research_service.py` (wiring) |
| Launching research CLIs | research worker (`dtc_research_worker.py`, `van-telemetry-research.service`) | `research_attempts.py` (the slot), `research_runner.py`, `research_cli.py`, `research_cli_policy.py`, `research_memory.py`, `research_volume.py`, `research_credits.py` (policy flag, key file, ledger and the per-launch plan), `api_credits.py` (pure rules: key reader, billing choice, classifier, spend tally), `api_credits_ledger.py`, `research_maintenance.py`, `research_worker_facts.py` (background identity and DNS checks behind `/status`), `cli_process.py` |
| Installing the CLI copy | root, by the runbook | `research_cli_install.py` (`systemd/install_research_cli.sh`) |
| Owner sign-in | owner shell | `dtc_research_signin.py` (`s dtc_research_signin claude`; `api-key` installs the promotional API key) |
| Credit fallback, rollback pause, balance view | advisor | `dtc_research_fallback.py`, `dtc_research_pause.py` and `dtc_research_admin.py` (owner CLI), `api_credits_snapshot.py`, `api_credits_view.py`, `dtc_research_log.py` (see "Promotional API credits") |
| Shared vocabulary | advisor, worker, dashboard | `dtc_research.json` (+ `dtc_research_contract.py`); `api_credits.json` (+ `api_credits_contract.py`) for credit billing |
| Warning-chat / research exclusion | both services | `model_cli_lock.py`, `assistant_slot.py`, `systemd/van-assistant.tmpfiles.conf` |
| JSON replies on the Unix sockets | both services | `assistant_http.py` (a caller that leaves is one log line, never a traceback) |

The advisor never launches a research CLI; the worker never talks to the broker. The model gets
only web search and web fetch.

## Routes (all behind the access code and browser key, relayed by `web.py`)

The one assistant route relayed without the code is the pairing-PIN redeem
(`POST /v1/assistant/pairing/redeem`), which returns the code to a device that types a valid PIN;
see [warning chat](warning-chat.md) section "Pairing another device". Every research route below
still answers 401 without the code.

- `GET /v1/assistant/models` → `{available, refreshing, groups: [{provider, label, available, reason,
  source: discovered|fallback, checked_at, models: [{id, label, efforts, default_effort}]}],
  default: {provider, model, effort} | null}`. A model with no efforts is started with
  `effort: null`. While `refreshing`, the UI re-fetches every 2 s, at most 15 times.
- `GET /v1/assistant/research/credits` → the promotional-credit view (see "Promotional API credits").
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
VIN-shaped strings (17 characters from the VIN alphabet, any case; a lower- or mixed-case token needs a digit) and, when the advisor's environment has `OBD_VIN`, that exact VIN in any case with spaces or hyphens between its characters, are masked in every prompt as it is built (`dtc_research_prompt`): the context,
each follow-up question on resume, and every replayed question and answer. The records on the Pi
keep the owner's text as he typed it; only what leaves the Pi is masked. Residual: apart from that
mask the owner's own text (his follow-up questions, his ignore note) is passed as he wrote it, so a
location he types reaches the model provider and possibly a search query. An unknown VIN typed with spaces or hyphens between groups is not recognised: any 17 letters and digits in groups would also match prose. Nothing else is promised. The system text tells the model web pages are
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
its own home per run, `CLAUDE_CODE_OAUTH_TOKEN` from the volume's token file (a credit launch gets `ANTHROPIC_API_KEY` instead; see "Promotional API credits"), no settings, no MCP,
no slash commands, `--restricted`, `--tools WebSearch,WebFetch` with `dontAsk` (the pre-execution
gates), a per-turn `--max-budget-usd` (5 report, 2 follow-up; a stop threshold checked after the fact, and a turn can take more than one launch, see "Promotional API credits"). Stream checks are a supplement: an
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
  expects. An attempt whose outcome the worker dropped becomes `interrupted`. One the worker says
  it does not know may only have a launch request still in transit, so the advisor first fences it
  (`POST /attempts/<id>/fence`, atomic with admission: the worker records a tombstone, so a late
  request with that id is refused as `duplicate_attempt`) and calls it `interrupted` only once the
  fence is confirmed; a `known: false` from a newer worker instance needs no fence, since a late
  request names the old instance and is refused as `stale_instance`. Cancelling an unknown id
  fences it too. Every research CLI process (turns, the model check, an installer's `--help`)
  starts through the runner's one guarded launch: the launch floor is checked just before each
  spawn, the memory and space watchdogs run, and an already-cancelled attempt never spawns.
- The warning chat and research exclude each other with a `flock` on
  `/run/van-assistant/model-cli.lock`, held by the launching process from just before spawning
  until the child is reaped. If a holder dies, the kernel releases the lock and systemd kills the
  holder's child with its control group; until that stop completes (up to the unit's
  `TimeoutStopSec`, 10 s) a second CLI can start beside the dying one. That crash-path overlap is
  an accepted limitation, not a "never overlap" guarantee.
- After an advisor restart research admission stays closed until the worker has said what it
  holds; nothing is inferred from a timeout or a missing path.

## Worker status (`GET /status`)

The advisor's worker client gives up after 5 s and the web relay after 12 s, so the worker's status
must answer fast on a loaded Pi. Its slow facts are kept off the request path:

- **CLI identity and sign-in.** A copy is checked by hashing its whole file set again whenever any
  file's inode, size or mtime changed (Claude 2.1.325 is 348 MiB: 2.5 s of CPU on an idle Pi 4, so
  at least 8 s under the unit's `CPUQuota=30%`, `Nice=15` and idle I/O class, and much longer while
  another job loads the Pi). One background thread (`research_worker_facts.Facts`) does that hash
  and reads the token, at start, when asked and at least once a minute. A status call asks for a
  fresh check and waits at most 1 s for it (`STATUS_WAIT_SECONDS`); otherwise it answers from the
  last one. Only one hash runs at a time. Request paths never hash: a copy whose files changed since
  the last hash reads `identity: checking` (launches refused as `provider_unavailable`, "try again
  in a minute") until the background hash has finished.
- **Public DNS.** `DnsProbe` resolves `api.anthropic.com` (IPv4 only: this Pi boots with
  `ipv6.disable=1`) on its own thread, at most one lookup at a time and at most one per 30 s, through
  the unit's resolvers (`systemd/research-resolv.conf`: 1.1.1.1 and 9.9.9.9, `timeout:2
  attempts:2`, so one lookup can legitimately take about 8 s). **Freshness rule:** public DNS counts
  as usable while a lookup succeeded within the last 300 s, whatever later lookups did; one slow or
  failed lookup on a phone hotspot neither hides research nor refuses a launch. The status display
  and the launch gate use the same rule. A refused launch says DNS, gives the last error and says
  to try again, and starts a new lookup so the retry has a fresh answer.

Reply shape: `{instance_id, pid, job, providers: {claude|codex: {identity:
ok|changed|missing|unverified|disabled|checking, signed_in, reason}}, memory_policy: {activated,
reason}, checked_at, checks_current, dns_ok, dns: {ok, result: ok|failed|pending, reason,
checked_at, seconds, last_ok_at, fresh_seconds}, volume: {free_bytes}, mem_available_bytes}`.
`checked_at` is when the identity facts were taken (`null` before the first check finishes, and the
providers then read `checking`); `checks_current` is false when the reply could not wait for a fresh
check. `dns.reason` is the last lookup's error, or "no answer within 10 s" while one is still
waiting. Times are UTC ISO strings.

Nothing else on the request path can wait: the HTTP server is threaded (one thread per request; a
caller that stalls mid-request is dropped after 15 s), the attempt table's lock is held only for
reads of small local files during an admission check, the maintenance reservation holds the slot
but no lock, and status never touches the shared CLI `flock`. A caller that leaves while a reply is
written, or while its request is read, costs one `assistant_http_client_gone` log line on either
service, never a traceback.

## Memory: two bounds

The kernel memory controller is on (owner decision 2026-11-17: `cgroup_enable=memory` ends the
boot command line, after the device tree's `cgroup_disable=memory`), so the unit's
`MemoryMax=768M` is a hard bound enforced by the kernel on the whole cgroup: the worker, its CLI
children, their tmpfs `/tmp` and charged page cache. With `OOMPolicy=stop` and
`KillMode=control-group`, a cgroup OOM kill stops the whole unit; `Restart=` brings it back and the
attempt ends `interrupted`. Evidence of that bound is in the cgroup's `memory.events` (`max`,
`oom_kill`); this kernel has PSI off, so there is no `memory.pressure` file.

The worker's own guard stays first and is the finer one. It refuses to launch below a free-memory
floor and SIGKILLs the attempt's process group at once (no grace) when the tree passes its cap or
free memory falls to critical. Its watchdog sums the RSS of the attempt's process tree, which is
not the whole cgroup charge (tmpfs and page cache are not counted), so the unit's `MemoryMax` is
the backstop, not a replacement. The thresholds come from `research_cli_policy.json`; until real
peaks have been measured **for the installed Claude copy** (`memory.cli_digest`), research turns
are refused (`not_activated`). Installing a different copy requires measuring again. Provisional
values for the measurement itself: floor 900 MiB, cap 700 MiB, critical 300 MiB.

Measured 2026-11-17 on Claude 2.1.325 (`sonnet`/`medium` report and a resumed follow-up): peak
tree `VmHWM` 233.7 MiB (report) and 208.1 MiB (follow-up), lowest `MemAvailable` 1,345 MiB; the
unit's cgroup `memory.current` peaked at 276 MiB with no `memory.events` counter raised. A
warning-chat Codex turn peaked at 141.9 MiB (advisor cgroup 190 MiB), below the research peak, so
the reserve stays 384 MiB. Active thresholds: tree cap 384 MiB (peak x 1.5, rounded up to 64),
launch floor 640 MiB (peak + reserve, rounded up to 64), critical 300 MiB.

## Storage and its bounds

- Advisor: `/var/lib/van-telemetry-advisor/dtc-research/runs/*.json` (at most 60 records of at most
  420 KiB, about 25 MiB; the oldest finished run is evicted only once a new run's launch has been
  admitted, so a refused start never costs a report, and a 61st record exists only during that
  launch request; an eviction whose file cannot be removed drops the record from memory at once and the
  file is retried by the next start and every reconciliation pass, and any excess loaded at startup is
  trimmed by the first pass) and `admissions.json` (the rolling 24-hour launch ledger: at most 60
  entries, about 9 KiB at its largest; refused launches are removed at once and never counted; a credit fallback's relaunch counts), the rollback `paused` file and `credit-snapshot.json` (the last valid Mac snapshot, under 16 KiB).
- Worker: everything research CLIs write is on `/var/lib/vanresearch`, a 224 MiB preallocated
  ext4 image (`/var/lib/vanresearch.img`) loop-mounted `nodev,nosuid,noexec`: the token, the
  the API key and the credit ledger (`api-credits.json`, at most 2,000 entries), the private Codex home, per-run homes and sessions, temp files. Its SD footprint never grows. The
  worker keeps sessions under 128 MiB in total and 16 MiB per run (oldest dropped first; those runs
  replay), deletes Codex logs past 48 MiB, and kills a turn when free space falls under 16 MiB.
- CLI copies: `/opt/van-research-cli` (Claude about 348 MiB, plus at most one staged release).

## Promotional API credits

While the owner holds promotional Anthropic API credits, research turns bill them first and fall
back to the subscription (the research OAuth token) when the credits cannot be used. The credits'
hard stop is `until` in `api_credits.json` (2026-12-08 00:00 UTC; grants "Nov 25" and "Dec 8", each
expiring at 00:00 UTC). After it research is subscription-only and nothing errors.
`api_credits.json` (loaded by `api_credits_contract.py`) is the one copy of the billing values and
reasons, credit codes, protocol field names, routes, paths, `until`, the verified grant table, and
the ledger, snapshot and fallback limits; the worker, the advisor and the dashboard all read it.

**What bills credits.** Only a research turn (a report or a follow-up) admitted by the worker.
Model checks, installer maintenance (`measure`, `budget_check`), discovery and `--help` runs never
do, and the warning chat never does (it runs Codex and has no Claude provider). Spend is display
only: the API's own refusal is the guard, because the Console organization behind the key holds no
purchased credit and no auto-reload, so an exhausted promotional lane fails at the API and cannot
spill into paid credit. No billing choice depends on a balance estimate.

### The key

The research API key is a research-owned credential at `/var/lib/vanresearch/anthropic-api-key`
(mode 0600) beside `claude-oauth-token`, inside the boundary the research threat model already
documents. It needs no sandbox change: the OAuth path already reaches `api.anthropic.com`.

```sh
python3 -m projects.vehicle_data.dtc_research_signin api-key              # hidden prompt
python3 -m projects.vehicle_data.dtc_research_signin api-key --from-file <path>
python3 -m projects.vehicle_data.dtc_research_signin api-key --remove
python3 -m projects.vehicle_data.dtc_research_signin status               # installed or not, never the value
```

(run as `pi` from the checkout; `s dtc_research_signin api-key` is the same.) The installer accepts
only a model key, `^sk-ant-(?:api\d{2}|usr)-[A-Za-z0-9_-]{20,300}$`, and names the refusal for an admin
key (`sk-ant-admin…`) or an OAuth token (`sk-ant-oat…`). Model keys carry either prefix:
`sk-ant-api03-`, or `sk-ant-usr-`, which the owner's Console organization issued on 2026-11-19
(`api.anthropic.com` accepted it as `x-api-key` on the free token-count endpoint). It refuses when no terminal can hide the
input (a `getpass` warning is an error), when the research image is not really the mount at the
volume (a bare directory on the SD card is refused, for the OAuth writer too), and for a
`--from-file` that is not one bounded, regular, non-symlink file (1 KiB at most) holding one line:
the bare key or `[export ]ANTHROPIC_PROMO_API_KEY=<key>`. The file is parsed, never sourced or
echoed. The write is a fresh `O_EXCL|O_NOFOLLOW` 0600 temporary file, fsync, rename, directory
fsync. The worker re-reads the key at every credit launch with the same strict reader (regular
file, no group or other bits, bounded, one key), so installing, rotating or removing a key needs no
restart.

A **dedicated Console key** for research (same organization, so the same grants, revocable alone)
is recommended. The owner's shared key (`/home/pi/secrets/.anthropic_promo_key`) also works but
widens the research boundary to that shared billing key; a key from any other organization must not
be used.

### The billing choice

The worker chooses per launch, before spawning, from the request and the run's session record;
the first match wins. The result is `api_credits`, `subscription` with a reason, or a pre-spawn
refusal.

1. `legacy`: the request lacks `billing_capable` (an older advisor): subscription.
2. `forced`: the advisor asked for `billing: subscription` (a fallback or replay successor).
3. `ineligible`: not a research turn (maintenance, discovery, `--help`).
4. `disabled`: `claude.api_credits.enabled` is false in `research_cli_policy.json`.
5. `no_key`: no valid key file.
6. `expired`: now is at or after `until`.
7. `unavailable`: an active latch, account-wide (`exhausted`, `invalid_key`, `key_ignored`) or for
   this launch's model (`model_unavailable`).
8. `ledger_unavailable` or `ledger_full`: the ledger could not take the reservation (below).
9. Otherwise `api_credits`.

The reasons are computed at admission, with the ledger reservation inside the attempt table's lock,
so an admission that fails after the reservation withdraws it.

**Session origin.** `session.json` records `billing` (`api_credits` or `subscription`; a record
without it is `subscription`). A follow-up resumes only on its session's origin, because no
transcript is resumed under a different credential. A subscription-created session always resumes
on the subscription; when credits are eligible anyway the reason is `ineligible`. An API-created
session resumes only on credits: when the choice above is subscription for any reason, the worker
refuses before spawn with `replay_required` (HTTP 409, definitive, nothing spawned, the same shape
as `session_missing`), and the advisor withdraws that ledger entry (it is not counted) and
relaunches the follow-up as a forced-subscription `replay` in a fresh session. A legacy request
(`billing_capable` absent) resuming an API-created session is `session_missing`, which the advisor
already answers with a replay.

**Credit launch.** The environment is the subscription one without `CLAUDE_CODE_OAUTH_TOKEN`, plus
`ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL=https://api.anthropic.com`; the argv is unchanged. The
key goes only into that one child's environment, never the worker's, an argv, a prompt, a record, a
reply, the journal or an exception (the worker's exception boundary redacts `sk-ant-…` text).
`ClaudeStream` checks the init event's `apiKeySource`: a credit launch must report exactly
`ANTHROPIC_API_KEY` (anything else stops it as the worker's own `key_ignored` failure, which falls
back), and a subscription launch must report exactly `none` (anything else is `policy_violation`).
Init is evidence, not a barrier before the spend.

### Credit failures and the fallback

Only a credit launch is classified, in this precedence: the worker's own stops win (`cancelled`,
`timeout`, memory or space, `policy_violation`, `resume_unverified`, `protocol_error`); then a
terminal result's own type (`error_max_budget_usd`, an overload or rate limit) is not a credit
failure; then a typed `assistant.error` (`billing_error` is `exhausted`, `authentication_failed`
is `invalid_key`, `model_not_found` is `model_unavailable`) counts only when the process then
ended with a failed terminal result or a nonzero exit; then bounded text of the terminal result's
`result` and `errors[]` (and the stderr tail only when the process exited before init) against the
exhausted, invalid-key and model patterns. Tool results and assistant text are never read. A credit
launch may accept as its first event a failed terminal `result` with no answer, as diagnostic
evidence only (it authorizes nothing and ends the run); a failure with no matching text, no stderr
and no init is classified `invalid_key`. A pre-init success result, malformed output, a session
mismatch or a tool-policy violation still fails closed.

A credit failure ends the worker's attempt with `{status: failed, reason: credit_fallback,
credit_code, billing: api_credits}`, sets the matching latch, and drops the failed run's session.
The worker never runs a second process for it. Latches clear only when the research worker
restarts (including `exhausted`: the grants do not refill).

The advisor then relaunches the same turn as one visible turn: a fresh, **counted** (it counts
toward the daily 60 launches, ledger kind `fallback`), forced-subscription attempt. A report starts
over with its original masked first prompt in a fresh session; a follow-up becomes a `replay` in a
fresh session. A turn has at most one fallback, since the fallback cannot fail on credits.

- **Only the current attempt drives the turn.** An outcome for an earlier attempt is acknowledged
  and ignored, so a duplicate delivery can neither start another fallback nor settle the successor.
  The fallback attempt records `fallback_of`.
- **Cancel intent is durable.** `cancel` saves `cancel_requested` on the turn before forwarding to
  the worker; a fallback, a replay-required replay, a `resume_unverified` replay, a restart's
  reconciliation and a late outcome all settle the turn as cancelled instead of launching.
- **A pending fallback is durable.** Before the first send the turn records
  `pending_fallback: {predecessor, deadline}` (now + 120 s), saved; each definitive refusal is
  saved too. After an advisor restart reconciliation continues a pending fallback until its
  deadline.
- **Busy retry.** The slot is free once the credit attempt ends, so a model check, maintenance,
  another job or the warning chat can take it first. A definitive pre-spawn `busy` or
  `warning_chat` refusal is retried by a thread of its own (never under the manager lock) every
  2 s until the deadline while the turn is not cancelled; after 120 s the turn fails with the
  usual busy wording. A lost or uncertain reply is never resent (the send-once rule). At the daily
  cap the turn fails as `daily_limit`.

Cost: `--max-budget-usd` (5 report, 2 follow-up) is a stop threshold the CLI checks after the
fact, not a cap. One logical turn may now cost one credit leg, one counted replay and one counted
fallback launch, and take up to two process timeouts for the billing legs.

### The ledger

`/var/lib/vanresearch/api-credits.json`, written only by the worker under one lock, display-only
spend. Record: `{schema_version, entries: [{attempt_id, model, started_at, settled_at, known_usd,
unknown, state: open|settled}], latches: {account, models}, last_fallback}`. It is written through
`lib.reports.atomic_json(fsync=True, fsync_dir=True)` (the directory option is new; default off),
and the in-memory record is replaced only after the write succeeded.

- **Open before spawn.** The entry is written durably before the credit process spawns. If that
  write fails the launch is subscription (`ledger_unavailable`); a launch that never spawns
  withdraws its entry.
- **Settle** from the process's own per-result deltas: `known_usd` is the sum of `costUSD` over
  validated `model_usage_since_last_result` objects of this credit process only, deduplicated by
  result number, including failed results. A gap in the numbering, a result without a valid delta,
  no result at all, or a process that did not exit cleanly sets `unknown: true` (the known spend is
  kept). The cumulative `total_cost_usd`, which a resume restores from the transcript, is never
  read. A settlement write failure keeps the answer and leaves the entry `open`; the next worker
  start settles it as `unknown`.
- **Bound, no compaction.** At 2,000 entries (unreachable at 60 launches a day before `until`)
  new credit launches are refused as `ledger_full` and nothing is discarded. At worker start,
  entries are deleted once `until` + 30 days has passed.
- **Latches and `last_fallback`** live in the same file. A latch applies at once even when its
  write fails. A file that exists but cannot be trusted makes every launch `ledger_unavailable`
  until it is repaired.

### The balance view

The advisor builds the view; the dashboard only renders it.

- **Snapshot.** The Mac's `com.jacobr.usage-budget` LaunchAgent (every 30 min) pushes
  `claude-promo-credits.json` to the Pi's `~/.agent/usage-budget/` over the LAN, so it goes stale
  whenever the Mac is away. The advisor reads it bounded (16 KiB, 8 grants) with
  `api_credits_snapshot.validate_snapshot`, a disclosed copy of `validate()` in
  `~/config/shared/promo-api/credits.py` with four differences: no age rejection (the view says
  `stale` instead, once `fetched_at` is older than 3,600 s), the "expires today" test runs at
  `fetched_at`, up to 300 s of Mac/Pi clock skew is tolerated, and every grant label must be one of
  the contract's verified grants (a yearless label cannot be dated otherwise, so a snapshot with
  another label is rejected whole). A version-1 error record (the Mac's lookup failed; a zero
  balance looks the same) means "balance unknown". The last valid snapshot is kept in memory and in
  the advisor's `credit-snapshot.json`; a cache write failure only loses the cache.
- **Spend since the snapshot.** An entry settled at or before `fetched_at` is already reflected. An
  entry that started after `fetched_at` draws on the earliest-expiring grant live at its start,
  spilling to the next. An entry that straddles the fetch or is still open (including one that
  started after it) is subtracted in full and counted `uncertain`, so the shown balance may be
  lower than the real remainder. An expired grant's remainder is lost, so only live grants are listed and
  totalled. `unknown_spend_count` counts entries with unknown spend.
- **Uncertain.** The balance is an estimate that can lag the account by up to the push interval; a
  run with unknown spend means the real remainder may be lower than shown.

### Routes

- `GET /v1/assistant/research/credits` (access code and browser key like the other research
  routes, relayed by the web listeners) replies `{key_installed, enabled, until, expired,
  ledger_available, new_research: {billing, reason, latched_models}, balance: {state: known|unknown, total_usd, as_of,
  stale, grants: [{label, expires, remaining_usd}]}, spent_since_snapshot_usd, uncertain_count,
  unknown_spend_count, last_fallback}`. It is built from the worker's local `GET /credits` (never
  the key or a path to it); an unreachable worker answers 503 `worker_unavailable`. `new_research`
  is what a **new** run bills by default and why; follow-ups keep their run's billing.
  `ledger_available` is passed through from the worker's `/credits`; a reply without it counts as
  unavailable, and then `new_research` is `subscription` (reason `ledger_unavailable`) and the
  balance is `unknown`, since spend cannot be accounted.
- **Admin routes** (`adminRoutes` in the contract: `POST /admin/dtc-research/pause`, `POST
  …/resume`, `GET …/drained`, `GET …/rollback-ready`) exist only on the advisor's own Unix socket, which is owner-only, and
  skip the access code. No web listener relays them (`tests/test_dtc_research_relay.py` checks that
  they answer 404 through the relay with valid pairing credentials).
- Protocol additions between advisor and worker: the worker's `GET /status` carries top-level
  `billing_capable: true`; the advisor adds `billing_capable: true` to a `POST /turns` body only
  when it saw that, and `billing: subscription` on a fallback or replay-required successor; every
  turn outcome carries `billing` and `credit_code`. A body without `billing_capable` is the old
  protocol, so a new worker with an old advisor never returns `credit_fallback` or
  `replay_required`, and a new advisor with an old worker gets subscription behaviour. The
  `dtc_research_timing` journal line gains `billing` and `credit_code`.

The dashboard's System page shows a read-only "Claude API credits" card (`ApiCreditsCard.jsx`,
polled every 60 s while visible) to a paired browser once a key is installed: the estimated
balance by grant with its as-of time and a "stale" mark, "uncertain" and "unknown spend" counts
with info popovers, what a new run bills and why, any latched model and the last fallback. After
`until` it is one line, "API credits expired · research uses the subscription". It never reports
an error: a refused or failed poll hides it until a later one succeeds.

### Threat model additions

- Key at rest: one research-owned file on the research volume, pi 0600. Same-UID processes can
  read it, as with the OAuth token (accepted in the research threat model). The owner's shared key
  is not exposed to research unless the owner chooses to install it.
- Key in use: only in one credit CLI child's environment; the CLI is offered WebSearch and
  WebFetch only, `--restricted`, no Bash, no file tools, no MCP. Subscription, discovery,
  maintenance and `--help` children never get it.
- Billing integrity: `apiKeySource` is checked both ways, the advisor must declare the capability
  before any credit launch, and `until` is a hard stop. The advisor builds prompts and never sees
  the key.
- Network and filesystem: unchanged for the research unit; the advisor reads one more file it
  could already read.

### Off switch and expiry

- **Off, with a restart:** set `claude.api_credits.enabled` to `false` in
  `research_cli_policy.json` (a reviewed commit) and restart the research unit. A worker that
  starts with credits disabled also drops every API-created session (`session.json`
  `billing: api_credits`), deleting its transcript files, so those follow-ups replay.
- **Off, with no restart:** `python3 -m projects.vehicle_data.dtc_research_signin api-key
  --remove`. The next launch is `no_key`; an API-created session then takes `replay_required`.
- **Expiry:** from `until` every launch is `subscription (expired)`, which outranks every latch;
  the card shows one line; nothing errors. Entries are deleted 30 days later at the worker's next
  start. The key can be removed and the code left in place.

### Rollback procedure

No arbitrary-order hot rollback is promised: a rolled-back advisor ignores the pause file and a
rolled-back worker does not understand `replay_required`. Run as `pi` from the checkout. The
admin CLI talks to `/run/van-telemetry-advisor/api.sock` (`--socket` overrides) and prints the
advisor's JSON reply; `drained` and `rollback-ready` exit 0 only when their condition holds.

1. Pause new research (completions, cancels, reconciliation and owed fallback or replay successors
   continue; new `start` and `ask` are refused as `provider_unavailable`, "Research is paused…",
   and a start already gathering context is refused just before it would launch):

   ```sh
   python3 -m projects.vehicle_data.dtc_research_admin pause
   ```

2. Wait until drained, both upgraded processes still running; repeat until it exits 0 (its
   `reasons` list names what remains):

   ```sh
   python3 -m projects.vehicle_data.dtc_research_admin drained
   ```

   `drained` holds the manager lock and is true only when the advisor is paused and idle (not
   reconciling, no start in progress, no busy turn, no `pending_fallback`), the worker reports no
   running job, no held turn outcome belongs to an unsettled turn (those of settled turns are
   acknowledged, retrying a lost acknowledgment; every attempt in the advisor's records is
   asked about), and the worker instance did not change during the check. A held
   `credit_fallback` keeps it not drained until the subscription answer or the requested cancel
   has settled the turn; it is never discarded to pass. Held maintenance and discovery outcomes
   have no consumer and do not block. It does not look at API-created sessions: a completed API
   report keeps its transcript until step 3.
3. Disable credits and restart the worker, still paused: set `claude.api_credits.enabled` to
   `false` in `research_cli_policy.json`, deploy that, then `sudo systemctl restart
   van-telemetry-research.service`. The worker drops its API-created sessions at start. Then wait
   until the worker is safe for the old code; repeat until it exits 0:

   ```sh
   python3 -m projects.vehicle_data.dtc_research_admin rollback-ready
   ```

   `rollback-ready` is `drained` and, from the worker's `/credits`, `enabled: false`, no
   API-created session (`api_sessions`, counted by transcript, not marker; an old worker looks
   for the transcript) and a `ledger_available` answer (either value: an unreadable ledger does not
   block rollback, since with credits off and no API session no credential is at stake). The worker
   must advertise billing: an old
   worker, or any reply that cannot attest to these, is never ready (`drained` alone stays
   satisfiable with an old worker).
4. Roll the **worker** back first (previous code, restart `van-telemetry-research.service`) and
   check that `/status` answers with a new `instance_id`. Then roll the **advisor** back last
   (previous code, restart `van-telemetry-advisor.service`): its startup is where admission
   reopens, because the old advisor ignores the pause file.
5. Only after the old advisor is verified running, remove the pause file with no module needed, so
   an unexpected restart of the upgraded advisor before then stays paused:

   ```sh
   rm /var/lib/van-telemetry-advisor/dtc-research/paused
   ```

After step 3 the three mixed states (an old advisor with `replay_required`, an old worker with an
API session, an old advisor with a held `credit_fallback`) are unreachable. `resume` (the same CLI)
clears the pause on the upgraded advisor when the rollback is abandoned.

Tests: `tests/test_api_credits.py` (key reader, billing choice, classifier, spend tally, ledger),
`test_research_credits.py` (the worker's credit launches against the scripted CLI),
`test_research_signin_api_key.py`, `test_api_credits_view.py` (snapshot validator, balance view),
`test_dtc_research_fallback.py`, `test_dtc_research_admin.py` (pause and `drained`), with the
scripted worker in `tests/dtc_research_scripted_worker.py`; the dashboard card by
`dashboard/test/apiCredits.helpers.test.mjs`.

## Deploy, activation and rollback

Root steps, in order: `systemd/install_dtc_research.sh` (image, tmpfiles lock, units, mount, worker,
advisor restart; `--dry-run` prints them), restart both web listeners, then
`systemd/install_research_cli.sh claude` (stage, worker verification inside a maintenance
reservation that is confirmed still open before promotion, promotion, identity record, worker
restart). The owner signs in (`claude setup-token`, then `s dtc_research_signin claude`). Then
`install_research_cli.sh claude --measure` and `measure_assistant_peak` give the peaks that a
reviewed commit records in `research_cli_policy.json`; deploying it activates research.
`--budget-check` shows whether the subscription CLI enforces the cost cap. The exact runbook with
checks and stop conditions is in the workstream note. Every worker status check there is bounded:

```sh
curl -sS -m 5 --unix-socket /run/van-telemetry-research/worker.sock http://worker/status \
  | jq '{checks_current, checked_at, providers, memory_policy, dns_ok, dns, job}'
```

It must answer within the 5 s; Pi load from other jobs no longer matters for that. Right after a
worker (re)start the providers read `checking` until the copy's first hash finishes (tens of seconds
on a busy Pi): ask again until `checks_current` is true and no provider reads `checking`, then
judge the fields.

Rollback of the whole research feature: disable `van-telemetry-research.service` and `var-lib-vanresearch.mount`, reinstall the
previous advisor unit and code, restart the advisor and the web listeners (research routes then
404 and the dashboard hides its controls). Remove data and credentials by revoking the Claude token
(claude.ai account settings), deleting `/var/lib/vanresearch.img`, `/opt/van-research-cli` and
`/var/lib/van-telemetry-advisor/dtc-research`.

Tests: `tests/test_research_worker.py`, `test_research_worker_status.py` (status bounds, DNS
evidence and freshness, callers that leave), `test_dtc_research_manager.py`, `test_dtc_research_relay.py`,
`test_dtc_research_pure.py`, `test_assistant_slot.py`, `test_research_cli_install.py`,
`test_dtc_research_units.py` (the credit tests are listed in "Promotional API credits"), with the scripted fake CLI in `tests/fake_research_cli.py`.
