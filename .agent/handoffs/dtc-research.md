---
workstream: dtc-research
tier: T3
status: activated on vanpi 2026-11-17 16:40:50 MST (0393eee, Astra ship) and the live acceptance check passed (17:21-17:31 MST); open: F1-F3, the flaky manager test below, the live-peak note below, and closing the workstream (promote to AGENTS.md, delete this note)
last reviewed commit: 0393eee (activation policy, Astra verdict ship); implementation 7df1eae; plan 31ed98b
---

# DTC research with an LLM (advisor backend)

## Objective

On the dashboard's diagnostic-codes card, every code gets "Research with AI": a model the owner
picks (Claude through the `claude` CLI, GPT through the `codex` CLI, each with an effort level)
searches the web for what that code means on this van, the report is kept on the Pi, its
one-line summary shows on the code's row, and the owner can keep asking follow-ups in the same
model session. This note is the backend plan (worker `dtc-research-backend`). The UI is built by
`dtc-frontend` against the API in section 2, final for this round; the parent's `dtc-design.md`
section 9b is superseded where they differ.

Tier T3: a new systemd unit and mount, credentials, web-capable models on the same
machine as a vehicle-control system, and behaviour that cannot be fully verified without real
model runs. Build in a worktree branch; cross-family review before merge.

**This round is a partial milestone, not completion of the owner's two-provider request.** It
ships Claude only. Codex is explicitly blocked: Codex 0.167 offers local tools besides web search
under every configuration tried (section 3, "Codex tool evidence"), so Codex research stays
`provider_unavailable` ("Codex is not yet verified for research on this CLI version") until a
pinned version passes the production tool-inventory check (follow-up F3, which also wires the
Codex launch and verification paths; their pure parts are built and tested).

## Build status (2026-11-17)

The plan (four rounds) and implementation (three rounds) both finished cross-family Astra review
with `Verdict: ship`, and the feature was merged by `75577ee`. The historical review-fix sections
below describe closed findings, not pending reviews. The standing contract is
`projects/vehicle_data/docs/dtc-research.md`; the deploy runbook is section 7. This remains a
Claude-only milestone; Codex research stays unavailable until F3.

### Activation and live acceptance check, 2026-11-17 16:40-17:31 MST

`0393eee` (Astra `ship`, one low fixed; whole suite 3,028 passed, 1 skipped) fast-forwarded and
pushed; the research unit file reinstalled (byte-identical to master; only its comment changed);
worker restarted 16:40:50: `memory_policy.activated` true, Claude `ok`, signed in.

- Owner in the browser (17:21-17:26): B162E-15 `sonnet`/`medium` report complete in 38 s, the four
  headings, 5 http(s) sources, summary 138 characters shown on the row; session file under that
  run's `home/.claude/projects/`. Follow-up with a synthetic VIN: `mode: resumed`, 23 s. The
  outgoing prompt in the session file reads `VIN [vehicle identifier omitted]`; the synthetic VIN
  appears nowhere on the research volume (the advisor record keeps the owner's text, by design).
  A warning-chat question during the follow-up got the warning chat's 429. A report started on
  `shifter`/P1C73-24 and stopped after 12 s ended `cancelled` (exit 143).
- Headless, through the LAN listener's routes with the access code read by a root script and never
  printed (owner approved headless testing): both units restarted 17:28:51/57; models available
  again at 17:30:22 (85 s: the advisor's worker check and discovery); a follow-up on the B162E-15
  run `resumed`, complete in 22 s. `radar_acc`/C1418-78 started; a second start 4 s later got 429
  `busy` ("another research job is running"); the advisor alone was restarted mid-turn
  (17:30:53) and the report was adopted and finished `done` in 43 s, summary 139 characters.
- **Live peaks above the recorded one:** `dtc_research_timing` tree `VmHWM` 244.4 MiB (B162E-15
  report) and 251.0 MiB (C1418-78 report) against the recorded `research_peak_mib` 233.7; other
  turns 207.9-212.6, cancelled 172.4. Recomputed with 251.0 the thresholds are unchanged (cap
  376.5 → 384, floor 635 → 640), 133 MiB under the cap, so research stays offered. The section 8
  rule ("re-measure before the feature stays offered") was not followed literally; the next policy
  change should record the larger observed peak.
- Cgroups (sampled every 0.5 s, 16:41-17:31): research `memory.current` peak 359 MiB of 768
  (it includes the 348 MiB CLI copy's page cache from the hash), advisor peak 17 MiB after its
  restarts; `memory.events` all 0; lowest `MemAvailable` 809 MiB. Volume 197 KiB used of 204 MiB.
  No traceback in either journal; both units NRestarts 0.
- **Flaky test to fix:** `tests/test_dtc_research_manager.py::ManagerTests::test_a_refused_replay_after_an_unverified_resume_keeps_the_resumed_mode`
  failed once ("run did not settle", line 223) in another session's Mac suite on a tip containing
  `0393eee`, then passed on resubmission; it passed in this session's Pi suite. Timing-sensitive.

### Measurement and activation commit, 2026-11-17 15:13-16:09 MST (after the memory-controller reboot)

The Pi was rebooted at 15:10:50 with the kernel memory controller on (O1 closed), so the research
and advisor units' `MemoryMax=768M` are enforced; `~/.agent/handoffs/memory-controller.md` holds that
verification. Preconditions at 15:15: worker status Claude `ok`, `signed_in` true, Codex `disabled`,
memory not activated, `dns_ok` true, `job` null; volume mounted (`/dev/loop0`,
`nosuid,nodev,noexec`); van asleep; `MemAvailable` 1,529 MiB.

- `install_research_cli.sh claude --measure` (15:15:58-15:16:59, Claude 2.1.325, digest
  `76d6e870…d60f`): `result` `measured`, both turns `complete`. Report: peak tree RSS 223.6 MiB,
  `VmHWM` 233.7 MiB, lowest `MemAvailable` 1,344.7 MiB. Follow-up: 208.1 / 208.1 / 1,386.3 MiB.
  The unit's cgroup, sampled every 0.5 s: `memory.current` peak 276 MiB, `memory.peak` 284 MiB
  (set by the worker's first CLI hash at boot), `memory.events` all 0.
- `--budget-check` (15:17:28-15:17:49, run before activation while research was idle; it does not
  depend on activation): `result` `enforced`, `reason` `budget_exceeded`, cap $0.01, prompt 3,782
  words, **`total_cost_usd` 0.084156**. The cap stops a turn only after a model call has pushed it
  over, so one call can overshoot it (here 8x); with the 5 and 2 USD caps the overshoot is bounded
  by one call's cost.
- Warning-chat peak (`measure_assistant_peak`, the owner's question answered 16:08:36-16:08:51):
  `child_seen` true, peak tree RSS and `VmHWM` 141.9 MiB, lowest `MemAvailable` 790.2 MiB; advisor
  cgroup `memory.peak` 190 MiB, `memory.events` all 0; the turn completed (exit 0).
- Policy block from `research_cli_install.memory_block(measurement, 141.9)`: research peak 233.7,
  tree cap 384, launch floor 640, critical 300, reserve 384 (the warning-chat peak is below the
  research peak, so it does not grow), `measured: true`, `measured_at` 2026-11-17.

### Pi installation retry, 2026-11-17 05:02-05:07 MST: installed and verified (not activated)

Master `232361e` (worker-status fix merged). Runbook section 7 steps 0-5 ran in order, each dry run
first; every check passed. Not done, by design: owner sign-in, `--measure`, `--budget-check`, the
activation commit, any model turn, anything for Codex. No broker restart (`van-telemetry.service`
still from 04:06:21), nothing sent on CAN, no package change, no reboot. Evidence (survives a
reboot): `/home/pi/Documents/agent-shared/from-agents/dash-20261116/research-deploy/retry-20261117/`.

- Preconditions, verified read-only: clean `master == origin/master == 232361e`; step 0 budget
  lines (`/var/lib/vanresearch.img:232`, `/var/lib/van-telemetry-advisor:160`,
  `/opt/van-research-cli:1024`) and Borg exclusions present; van asleep, running false; 27 GiB free
  on `/`; load 1.9; `MemAvailable` 1,555 MiB. Retained from the first attempt, as expected: image
  (224 MiB, root 0600), the verified 2.1.325 copy (`identity.json` passed, digests equal), empty
  staging, unit and tmpfiles files identical to master, research unit and mount disabled/inactive,
  installed advisor unit equal to the saved pre-research copy.
- Step 1-2 (05:02:29-05:02:35): dry run matched (image kept, the other 13 steps in order); install
  enabled the mount (`/dev/loop0`, ext4, `nosuid,nodev,noexec,noatime`, root `pi 700`, 204 MiB
  usable), the worker (NRestarts 0, OOMScoreAdjust 800, ControlGroup
  `/system.slice/van-telemetry-research.service`; bpftool lists `sd_fw_ingress`/`sd_fw_egress`) and
  restarted the advisor on the master unit. Lock `-rw-r----- pi pi`. Status at 05:02:40 answered in
  1.02 s with `checking` (`checks_current` false); at 05:02:44 in 0.64 s with Claude `ok`, not
  signed in; Codex `disabled` (not-yet-verified reason); memory not activated; DNS `ok` in 0.088 s.
  First background hash after the worker start (05:02:34): about 11 s.
- Step 3 (05:03:01): both web units restarted; `/v1/assistant/models` without key → 401 on the
  second try (1 s).
- Step 4 (owner decision for this stage): advisor active, NRestarts 0, `/v1/assistant/status`
  without key → 401, advisor journal clean. The owner asks one real warning-chat question himself
  later.
- Step 5 (05:03:17-05:04:25): reservation opened, verification passed, 2.1.325 re-promoted,
  `identity.json` `2.1.325 passed True` (verified 05:04:23), no `last_failed_install`. Worker
  restarted active at 05:04:25.43; the runbook's poll printed `ready=yes` on round 3: rounds 1-2
  answered in 1.03 s and 1.05 s with `checking`, round 3 in 0.03 s with Claude `ok`. **First hash
  after the restart: 12.9 s** (`checked_at` 05:04:38.28). Final status: Claude `ok`,
  `signed_in` false ("Claude is not signed in for research yet"), Codex `disabled`, memory not
  activated, `dns.result` `ok`, `seconds` 0.1, `job` null. Journals of the worker, advisor and both
  web units since 05:02: no traceback, no error, no `assistant_http_client_gone`.
- Sandbox probes (namespace visibility, not reached in the first attempt): a probe joined the
  worker's cgroup and mount namespace as pi. `/home/pi` holds only `dev`; `secrets`, `.claude`,
  `.codex`, `.local/bin/claude` do not exist there; checkout `tmp`, `.git`, `AGENTS.secret.md`,
  `/run/van-telemetry`, `/run/van-telemetry-advisor`, `/run/dbus`, `/run/user`, `/run/tailscale`,
  `/var/lib/van-telemetry`, `/var/lib/van-telemetry-advisor` are mode-000 inaccessible; the
  checkout is not writable; `/etc/resolv.conf` is the tracked public-resolver file. Connects to
  127.0.0.1, 192.168.6.103:8765, 192.168.6.1:80, 169.254.1.1 and 100.100.100.100 all timed out
  (dropped) while the same LAN targets connect from outside the cgroup; 1.1.1.1:53 connected and
  `api.anthropic.com` resolved. In the advisor's namespace `/var/lib/vanresearch` is mode 000 and
  the lock is visible 0640. Worker process `NoNewPrivs 1`, `Seccomp 2`. Not probed: the address
  family filter and seccomp from inside (a probe process does not inherit them; the unit settings
  and the process's seccomp mode were read instead).
- HTTP, LAN and Tailscale listeners: `/`, `/v1/snapshot`, `/v1/diagnostics/dtc-ignores` 200;
  `GET /v1/assistant/models`, `/dtc-research`, `/status` and a same-origin POST without key 401;
  cross-origin POST 403.
- Frontend (05:06:22-05:06:29, load 3.1 before): `npm ci` and `npm run build` rebuilt the same
  bundle (build `4eec25b5e3f6`, identical outputs, `builtAt` 05:06:29); served
  `assets/chunk.5WIUNV4V.js` contains `Research with AI`.
- Disk: `/` 28 G used, 27 G available before and after. Image 224 MiB logical, 4.5 MiB allocated;
  volume 16 KiB used of 204 MiB; `/opt/van-research-cli` 348 MiB; advisor state 5.8 MiB.
- Units left: mount and research worker enabled and active; advisor active (master unit, with
  `InaccessiblePaths=-/var/lib/vanresearch`); both web units active; all NRestarts 0. The memory
  controller is still off until the next reboot (`cgroup_enable=memory` was added to the boot
  command line today), so `MemoryMax` is not yet enforced.

Remaining, in order: the owner's research sign-in (`claude setup-token`, then
`s dtc_research_signin claude`; check `s dtc_research_signin status`) and one real warning-chat
question; memory measurement (runbook step 7, `uptime` calm); the reviewed activation commit
(step 8); the budget check (step 9); the live acceptance check with real Claude turns (section 8).
F1-F3 stay open; O1 is closed.

### Pi installation attempt, 2026-11-17 03:53-04:12 (all times MST; superseded by the retry above)

The pre-activation installation stopped at its post-CLI-install status/journal check. The research service and mount were left disabled
and inactive; the previous advisor unit is restored and the advisor was restarted. Runtime
rollback is incomplete: the pre-merge code and old frontend were not restored, because another
session advanced master and owns the live dashboard deployment. No research credentials or real
model turns were created. No broker restart or CAN operation was performed by this install session.

- Preconditions: clean `master == origin/master == 111d950`; vehicle asleep, running false;
  27 GiB available on `/`; step-zero root budgets and all backup exclusions deployed. Debian
  `bpftool` 7.1.0+6.1.192-2 installed and held, unchanged. Full suite on `111d950` passed on Mac
  worker `m4mac.04`, job `20261117T104911Z-74270f0c`: 2,983 passed, 13 skipped, 3,610 subtests,
  layout and scratch build passed. At 04:00:10 load was 3.24 / 2.93 / 3.64 and `MemAvailable`
  1,359 MiB. No memory measurement was run.
- Both installer dry runs matched the documented paths. Installed mount/worker/tmpfiles/advisor
  at 04:00:23–26. Mount was `/dev/loop0`, ext4, `nosuid,nodev,noexec,noatime`; volume root pi:pi
  0700, image root:root 0600, lock pi:pi 0640. Worker active, NRestarts=0, OOMScoreAdjust=800;
  bpftool confirmed ingress and egress programs, before and after the CLI install. Its BTF warning
  did not prevent the attachment check. Initial `/status`: Claude missing, Codex disabled,
  memory not activated, `dns_ok: true`.
- Built dashboard `4eec25b5e3f6` (previous served build `e471a1d1daea`) and restarted both web
  units at 04:01:02. An immediate connection-refused probe preceded listener readiness; the
  bounded readiness retry reached the expected 401. The documented warning status route
  `/v1/assistant/status` also returned 401. A mistaken GET of the POST-only `/assistant/chats`
  returned 404 and was replaced with that documented status check. Jacob's addendum accepts
  unauthenticated 401 plus clean advisor logs for this stage; he will ask a real warning-chat
  question at sign-in. The access code was never read.
- Claude 2.1.325 installed and promoted successfully at about 04:02:42; identity verification
  `passed`, evidence digest equals installed digest. New worker started 04:02:43, NRestarts=0.
  The first post-install `/status` probe timed out at exactly its local 10-second curl deadline
  with no bytes. A subsequent probe with a 30-second bound returned 200: identity `ok`,
  `signed_in: false`, reason `Claude is not signed in for research yet`; Codex disabled with
  its not-yet-verified reason; memory not activated; `dns_ok: false`.
- The journal showed two `BrokenPipeError` tracebacks, request headers being written by
  `WorkerHandler.reply` at `dtc_research_worker.py:309` (`end_headers`, outside its body-write
  exception guard). The first began printing at 04:03:05 ("Exception occurred during processing
  of request from" with an empty Unix-socket address), but its traceback lines only reached the
  journal at 04:03:17, interleaved with the second's: formatting a traceback (reading three source
  files) took the worker about 12 s, so the whole process was starved at that moment.
- Cause analysis and fix: next subsection.
- Per the brief's failed-check rollback rule, stopped and disabled the research worker and
  mount, restored the saved pre-install advisor unit, daemon-reloaded, and restarted the advisor.
  Did not delete installed files or data. Warning/advisor and web units remained active with
  NRestarts=0; both listeners returned 200 for `/`, `/v1/snapshot`, and
  `/v1/diagnostics/dtc-ignores`; 401 without credentials for warning status, research models and
  research list; cross-origin research POST returned 403. Research routes are still registered
  (not 404), because the current merged advisor/web code remains installed.
- Coordination: `Drive dashboard tiles and DTC research` confirmed it advanced master to
  `4d231f3` around 04:01 and owns frontend build, web restarts and guarded broker restart for
  the sparkline work. Its range `111d950..4d231f3` changes no research/advisor code or units,
  and its full suite passed (`20261117T104856Z-12016fb2`, Astra review ship). Its deploy may
  have overlapped this attempt; overlap is a possible pressure source, not a proven cause.
  Do not overwrite its checkout/dist or revert its work to complete this rollback.
- Retained footprint: image logical size 224 MiB, allocated about 4.5 MiB after mkfs; CLI tree
  348 MiB; advisor state 5.8 MiB; root still 27 GiB available. The image, CLI, disabled unit
  files, tmpfiles entry and lock remain for diagnosis/retry. The image is not mounted.
- Bounded evidence/rollback snapshot: `/tmp/research-install-20261117-0353/` contains
  `install.log`, `research-advisor-journal.txt`, `advisor-before.service`, `build-before.json`,
  `dist-before.tgz`, `disk-before.txt`, and `disk-after.txt`. The prior advisor file matches the
  saved copy under `Documents/agent-shared/from-agents/dash-20261116/research-deploy/before/`.
  No credentials or raw snapshot data were saved. The namespace visibility probes were not run
  before stopping, so filesystem isolation is not yet live-verified.

### Worker status fix (2026-11-17, after the install stopped)

Reviewed and merged into master (`232361e`); installed by the 05:02 retry above. No sandbox
setting changed.

What caused each failure (times MST):

1. **Status past its 10 s deadline: established mechanism, exact duration not measured.**
   `status()` called `IdentityChecker.check("claude")` inline, and the first check after the CLI
   install (a fresh worker, empty cache) hashed the whole 348 MiB copy. Measured afterwards on this
   Pi: 2.46 s of CPU for that hash at load 2 (5.2 s wall at `nice 15`), so at least 8.2 s inside the
   unit's `CPUQuota=30%`, before `Nice=15` and the idle I/O class meet another job's `npm ci` and
   esbuild. Concurrent status calls each hashed (the cache had no lock), sharing the same 30 %
   quota, and the attempt table's lock could be held through such a hash too (admission and the
   maintenance step called the identity check under it; a launch or step right after a copy change
   would also have stalled the serving loop's watchdog pings, which take that lock).
2. **The second disconnected caller: probable, not established.** The socket is pi-only; the other
   callers are the advisor's (5 s client timeout): the reconciler's 30 s restart check and the
   model catalog's 60 s retry, both of which call `/status`. No advisor log names the request.
3. **`dns_ok` false: probable, not established.** No resolver error was kept. The old probe gave
   `getaddrinfo` 2 s, less than one resolver timeout (`timeout:2`, two servers, two attempts), on a
   thread competing for the same starved 30 % quota while hashes ran, and cached a false for 60 s.
   At 04:10–04:12, host-side A and AAAA queries to 1.1.1.1 and 9.9.9.9 answered in 41–60 ms. The
   unit's sandbox has no time-dependent DNS factor: neither resolver address is in
   `IPAddressDeny`, there is no `IPAddressAllow`, the filter is address-only (TCP fallback to port
   53 is allowed), the resolver file is a read-only bind of a tracked file, and `ipv6.disable=1`
   only means AAAA answers are unusable (queries go over IPv4). Not tested: whether this uplink's NAT
   drops one reply of glibc's parallel A/AAAA pair (the probe now asks for A only; the CLI's own
   lookups are unchanged; `options single-request-reopen` in the resolver file is the candidate if
   real turns show DNS stalls).
4. **Tracebacks: established.** `end_headers()` sat outside the reply's `BrokenPipeError` guard. The
   advisor's `ChatHandler.reply` had the same defect; `web.py`'s `_json` was already guarded.

What changed (see `docs/dtc-research.md` "Worker status" for the contract):

- `research_worker_facts.py` (new): `Facts`, one background thread that hashes and reads the token;
  a status call waits at most 1 s for a fresh check, else answers from the last one with
  `checked_at` and `checks_current: false` (`identity: checking` before the first). `DnsProbe`: one
  lookup at a time, at most one per 30 s, IPv4 only, keeps `result`, `reason`, `checked_at`,
  `seconds`, `last_ok_at`; "no answer within 10 s" while a lookup is still waiting.
- DNS freshness rule (the one launch condition that is looser than before): DNS is usable while a
  lookup succeeded within the last 300 s, whatever later lookups did. Before, a success was cached
  for 60 s and then a fresh 2 s probe decided. Status and the launch gate use the same rule; a
  refusal names DNS, its last error and "try again", and starts a new lookup.
- `research_cli_policy.IdentityChecker`: a lock on its cache, one hash at a time, and
  `check(..., rehash=False)` for request paths, which returns the new state `checking` instead of
  hashing. Admission, discovery, the maintenance step and activation use it, so a changed copy is
  refused (retryable) until the background hash has run; the runner's own in-thread check before a
  spawn still hashes. Nothing launches where it was refused before, apart from the DNS rule.
- `assistant_http.py` (new): `send_json` guards the whole reply and `QuietDisconnects` the request
  read; a caller that leaves is one `assistant_http_client_gone` line (server, stage, error, path).
  Used by both the worker and the advisor. The worker's handler drops a caller stalled mid-request
  after 15 s.
- `assistant_models.provider_available`: the picker says "still checking public DNS" or "has not
  answered in the last 5 minutes" from the worker's `dns` evidence.
- Tests: `tests/test_research_worker_status.py` (status within its bound while a hash, a DNS lookup,
  a maintenance reservation, the CLI lock and other in-flight requests all hold; callers gone before
  the headers, mid-body and mid-request on the worker and before the advisor's reply; DNS success,
  failure, timeout, gap and freshness; the launch gate with a fresh and a stale success).

What a re-install must verify live (tests cannot): every `/status` answers within the bounded
command's 5 s, including the first one after the CLI install's worker restart, which reads
`checking` until the first hash finishes; record how long that took (`checked_at` minus the
restart). `dns.result` `ok` with `dns.seconds`; if not, `dns.reason`, then ask again after a
minute. The journal has no traceback; an `assistant_http_client_gone` line must be explained by a
caller that gave up. Load from another job no longer has to be avoided for status checks; still
check `uptime` before the memory measurement.

The retry above verified each of these live (status bounds, first-hash time, DNS evidence, clean
journals, namespace probes). Next: owner Claude sign-in and warning-chat question; memory
measurement; reviewed activation commit; budget check and real-turn acceptance. F1–F3
remain open (O1 closed 2026-11-17).

### Implementation review fixes (2026-11-17, on top of `fef26e3`)

Each has a regression test through a public entry point that failed on `fef26e3` and passes now.

1. (high) VIN masking of every prompt part: the three builders in `dtc_research_prompt` mask their
   whole output, the follow-up question on resume and every replayed question and answer included.
   Records keep the owner's text as typed (parent's preference); only what leaves the Pi is masked.
   The mask moved from `dtc_research_context` into `dtc_research_prompt`.
   `test_follow_up_questions_never_leave_the_pi_with_a_vin`.
2. (high) A refused start never deletes research: eviction runs only after the new run's launch was
   admitted. `test_a_refused_start_at_the_run_cap_keeps_every_record`.
3. (medium) Concurrent starts: a start reserves its request id and code before gathering
   (`ResearchManager.starting`; a second start for either is `run_busy`), reserved starts count in
   the 10-a-minute window, and the reconciliation state is rechecked after gathering.
   `test_concurrent_starts_with_one_request_id_launch_once_and_keep_the_cap`.
4. (medium) Record-write failures: progress saves are caught; `_finish` saves a copy and changes the
   live record only once it is on disk (`_settle`), returning `False` otherwise, so the follow
   thread and the reconciler never acknowledge an unsaved outcome and retry; a follow thread that
   fails anyway hands its turn back to reconciliation (`reconciling`, state `unknown`); the
   reconciler loop survives errors; a save failure before a launch is `storage_error`.
   `test_a_failed_record_write_keeps_following_and_saves_before_acknowledging`,
   `test_reconciliation_does_not_acknowledge_an_outcome_it_could_not_save`,
   `test_a_failed_follow_thread_hands_its_turn_back_to_reconciliation`.
5. (medium) One guarded launch: `Runner.guarded_run` starts every research CLI process (turns,
   discovery, the installer's `--help`, read as bounded plain lines) with the launch floor checked
   just before each spawn (`cli_process.run(before_spawn=...)`), the memory and space watchdogs,
   cancel, deadline, bounded output and process-group reaping; every maintenance step, verify
   included, checks the floor on admission. A spawn refused for memory fails the turn
   `memory_exhausted` but keeps the run's session. `MaintenanceGuardTests` (three tests).
6. (medium) Fencing: `POST /attempts/<id>/fence` (section 2) and fencing on cancel of an unknown id;
   the advisor declares `interrupted` for a `known: false` from the addressed instance only after the
   fence is confirmed (section 4, "Reconciliation").
   `test_an_unknown_attempt_is_fenced_before_it_is_called_interrupted`,
   `test_fencing_an_unknown_attempt_refuses_its_later_launch`.
7. (low) `cli_process.run` returns `cancelled` without spawning when the attempt is already
   cancelled. `test_an_accepted_attempt_cancelled_before_its_spawn_never_spawns`.

### Re-check fixes (2026-11-17, on top of `57ae766`)

Each has a regression test that failed before its fix.

1. (medium) A failed start can never strand a running record: `Store.delete` removes the record from
   memory first and never raises; a file it cannot unlink is retried by the next start, the idle
   reconciler and every reconciliation pass. The caller sees the original `storage_error`. A leftover
   file with a running turn and no attempt id loads `reconciling` and the first pass settles it
   `failed`/`interrupted` (already the behaviour; now tested).
   `test_a_failed_start_whose_rollback_cannot_delete_leaves_no_running_record`,
   `test_a_leftover_running_record_with_no_attempt_loads_as_interrupted`.
2. (medium) VIN masking is case-insensitive (lower or mixed case needs a digit, so long words are not
   masked) and also masks `OBD_VIN` itself in any case with spaces or hyphens between its characters.
   A spaced VIN the service does not know is deliberately not masked: any 17 letters and digits in
   groups would match prose and DTC lists. `VinMaskTests`.
3. (low) `_launch` restores `turn["mode"]` when it withdraws a refused launch, so a refused replay
   leaves `resumed` and a retry replays again. `test_a_refused_replay_after_an_unverified_resume_keeps_the_resumed_mode`.
4. (low) `StartWindow.record()` stamps the start with its own clock reading.
   `test_each_recorded_start_carries_its_own_time_not_another_requests_check`.
5. (low, docs) `ResearchManager.trim()` (after each admitted launch and in every reconciliation pass)
   keeps the 60-record cap and retries unremoved files, so "a 61st record exists only during a launch
   request" holds. `test_an_eviction_that_cannot_remove_its_file_is_retried_by_the_next_start_and_the_reconciler`.

Hang guards: the concurrency tests in `test_dtc_research_manager.py` and `test_research_worker.py` use
daemon threads with 20 s joins and barrier timeouts, so a deadlock is a named failure.

Accepted limitation: a cancel that lands between the worker's pre-spawn check and `Popen` can still
create a child, which the first pump step stops before any prompt is fed.

The fake CLI (`tests/fake_research_cli.py`) also logs each turn's prompt (`prompts.jsonl`,
`WorkerRig.prompts()`) and can write a file at the end of a turn (`write_at_end`).


### Per file (all under `projects/vehicle_data/` unless a path is given)

Done (every piece below has tests through its public entry point):
- `assistant_common.py`, `assistant_slot.py` (tidy, committed `249ad07`, suite green job `20261117T040313Z-83a201af`; then the CLI lock was added inside `ModelSlot`) — done
- `model_cli_lock.py` — done (`tests/test_assistant_slot.py`)
- `dtc_research.json`, `dtc_research_contract.py` — done (`tests/test_dtc_research_pure.py`)
- `dtc_research_prompt.py`, `dtc_research_context.py`, `dtc_research_facts.json` — done (pure tests)
- `dtc_research_ledger.py` — done (pure tests)
- `dtc_research_store.py`, `dtc_research.py`, `dtc_research_reconcile.py`, `assistant_models.py` + `assistant_models.json`, `research_worker_client.py`, `dtc_research_service.py` — done (`tests/test_dtc_research_manager.py`, `tests/test_dtc_research_relay.py`)
- `warning_chat.py` (wiring: research routes outside `manager.lock`, `reason` in error replies, `--research-socket`, `--no-dtc-research`), `web.py` (relay clause) — done (`tests/test_warning_chat.py` unchanged and passing)
- `dtc_research_worker.py`, `research_attempts.py`, `research_runner.py`, `research_cli.py` (Claude parts), `research_cli_policy.py` + `.json`, `research_memory.py`, `research_volume.py`, `cli_process.py` — done (`tests/test_research_worker.py`)
- `research_maintenance.py` (`verify_claude` through the installer; `measure` and `budget_check` in `tests/test_research_maintenance.py`)
- `research_cli_install.py` + `systemd/install_research_cli.sh` — install / dry run / busy / expired reservation / failed verification (`tests/test_research_cli_install.py`), `--measure` and `--budget-check` (`tests/test_research_maintenance.py`)
- `measure_assistant_peak.py` — done (`tests/test_research_tools.py`, fake `/proc` tree)
- `dtc_research_signin.py` — done (`tests/test_research_tools.py`: mode 0600, refusals, token never printed)
- `systemd/van-telemetry-research.service`, `var-lib-vanresearch.mount`, `van-assistant.tmpfiles.conf`, `research-resolv.conf`, advisor unit one-line change — done (`tests/test_dtc_research_units.py`, `test_service_units_start_modules.py`, `test_backend_contracts.py`)
- `systemd/install_dtc_research.sh` — written; `--dry-run` run by hand and correct (16 steps); the real run is untestable here
- `research_cli.py` Codex parts (`codex_argv`, `codex_env`, `CodexStream`, `offered_local_tools`) — pure parts tested (`tests/test_research_tools.py`); no launch path (Codex is disabled; F3)
- Docs: `docs/dtc-research.md`, pointers in `docs/warning-chat.md` and `README.md` — done
- `AGENTS.md`: Contracts-table row for `dtc_research_worker.py` only (the contract test requires it) — done
- Tests: `tests/fake_research_cli.py`, `tests/research_support.py`, `tests/test_assistant_slot.py`, `test_dtc_research_pure.py`, `test_dtc_research_manager.py`, `test_dtc_research_relay.py`, `test_dtc_research_units.py`, `test_research_cli_install.py`, `test_research_worker.py` — done

Not started: Codex launch path in `research_runner.py` and the per-model Codex verification step in
`research_maintenance.py` (deliberately out of scope this round: F3).


### Test commands and results

- Targeted (worktree root, on the Pi): `python3 -m pytest tests/test_research_worker.py
  tests/test_research_maintenance.py tests/test_research_tools.py tests/test_dtc_research_manager.py
  tests/test_dtc_research_relay.py tests/test_dtc_research_pure.py tests/test_assistant_slot.py
  tests/test_research_cli_install.py tests/test_dtc_research_units.py tests/test_warning_chat.py
  tests/test_backend_contracts.py tests/test_service_units_start_modules.py -q`.
- Whole suite on the Mac worker: `/home/pi/van_compute/scripts/pi_compute.py run repo-full-suite
  --source-root <worktree> --wait 5`, then `pi_compute.py wait <id> --timeout 1200`.
- Tests that bind unix sockets keep their paths short (`/tmp/rs*`): macOS limits `AF_UNIX` paths to
  about 104 bytes and the compute worker's temp directories are long.
- The memory watchdog reads `/proc`, which the Mac lacks: its decision logic is tested on a fake
  `/proc` tree (`tests/research_support.proc_entry`), and the end-to-end kill test skips where there
  is no `/proc`.
- Results: `20261117T043949Z-04960a39` failed only on that end-to-end watchdog test (no `/proc` on the
  Mac); `20261117T044834Z-d7f9fd53` ran the whole suite on the code tree (everything but this note) and passed.

### Departures from the plan (and why)

- Launches are sent inside the POST, not from a background thread: a worker refusal then reaches
  the dashboard as the API error (`429 busy`, `503 provider_unavailable` for `not_activated`) and a
  refused start keeps no record. Broker reads 2 s, worker request 5 s, inside the relay's 12 s. The
  research lock is held across that one launch request.
- Worker routes as built: `GET /status`; `POST /turns`, `/discover`, `/maintenance` (202 or 409
  `{reason, detail}`); `GET /attempts/<id>`, `POST /attempts/<id>/ack`, `POST /attempts/<id>/cancel`
  for every attempt kind (the plan named per-kind routes); `POST /maintenance/<id>/step`
  `{"step": "verify"|"measure"|"budget_check"}` (verify also names the staged executable),
  `GET /maintenance/<id>`, `POST /maintenance/<id>/end`; `POST /sweep`; `POST /runs/<id>/delete`.
- `WorkerUnreachable.sent`: a refused connection or missing socket is certainly-not-sent (ledger
  entry removed, `503 worker_unavailable`); any other transport failure is ambiguous and only asked
  about afterwards (send-once). Needed because otherwise a start with the worker down returned 200
  and only later turned `interrupted`.
- The worker cuts answers to the cap plus 4 KiB (room for the `SUMMARY:` line), so an
  `/attempts/<id>` reply is under about 110 KiB (the plan said 112 KiB at exactly the cap).
- Ledger: 60 entries with the longest timestamps measure 9,087 bytes; the bound is 12 KiB.
- A failed upgrade verification records `last_failed_install` and keeps the verified copy in service.
- With no lock file the warning chat carries on (logs once); the worker refuses to launch without it.
- The catalog treats a policy-disabled provider as settled and re-checks every 60 s while Claude is
  installed but unavailable.
- Advisor flag `--no-dtc-research` (not used by the unit).
- Codex launch and verification wiring moved into F3 (pure parts built).

### Learned while building (not in the plan)

- Codex 0.167 with all tool features disabled still offers `exec`, `wait`, `request_user_input(_async)`
  and the collaboration tools in its rollout `additional_tools` record; `sleep_tool=false` removes
  `clock.sleep`; there is no generic `disabled_tools` key (strict config refuses it). Disabling
  `code_mode_host` makes Codex emit an `item.completed` of type `error` at turn start.
- Until the 2026-11-17 reboot the Pi kernel had `cgroup_disable=memory` and no `MemoryMax` was
  enforced; since then the memory controller is on (O1 closed, section 9).
- `api_server` imports radar service code; the worker must not import `projects.vehicle_data.api`
  (it has its own `prepare_socket`); `api_client.TelemetryClient` is light and fine.
- The fake CLI's scenario and call log must live outside the release directory, or the identity
  digest changes under the test (`tests/fake_research_cli.install(..., state=...)`).
- A staged copy is made read-only (0555), so a non-root cleanup must chmod before `rmtree`
  (`research_cli_install._discard`).
- AF_UNIX paths are limited to about 104 bytes on macOS; tests that bind sockets use a short `/tmp`
  directory.
- In tests, wait for the manager's follow threads (`manager.following`) to empty before removing the
  temp directory, or a late save raises in a thread.
- The root-run installer must run Python with `-B` so no root-owned bytecode lands in the checkout.


### Not verified until the live deploy

- Real Claude turns: the stream format, `--max-budget-usd` enforcement (`--budget-check`), the memory
  peaks (`--measure`, then the reviewed memory-policy commit that activates research), session resume
  across turns on the real CLI and web search quality.
- The unit, mount, tmpfiles, resolver file and installer scripts run for real (`install_dtc_research.sh`
  was only dry-run; the 16 steps are in the runbook), the sandbox directives on this kernel, and the
  token sign-in (`s dtc_research_signin claude`).
- Codex: no launch path, by design (F3).

### Remaining steps, in order

1. Runbook section 7 steps 6-9: owner sign-in, memory measurement, the reviewed memory-policy
   (activation) commit, budget check (steps 0-5 are done and verified, Build status).
2. Section 8's live acceptance check with real Claude turns.
3. F3: pin a Codex version that passes the production tool-inventory check, then wire its launch and
   verification paths.

## Facts established (2026-11-16, read-only unless stated)

- Installed CLIs: `claude` 2.1.325 (`~/.local/share/claude/versions/2.1.325`, one 348 MiB file;
  2.1.316 and 2.1.319 are also kept); `codex` 0.167.0 (release directory
  `~/.codex/packages/standalone/releases/0.167.0-aarch64-unknown-linux-musl`, 459 MiB: `bin/codex`
  266 MiB, `bin/codex-code-mode-host` 69 MiB, `bin/codex-exec-server` 67 MiB, voice host and
  GStreamer libraries, and `codex-path/` with `rg` and `bwrap`, about 5 MiB). The warning chat was
  last documented against Codex 0.153.4. The owner's Claude settings disable its auto-updater.
- `van-telemetry-advisor.service` installed equals the tracked unit, no drop-ins, active:
  `ProtectHome=read-only`, `ReadWritePaths=/home/pi/.codex`, `PrivateTmp=true` (`/tmp` is ext4 on
  the SD root), `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, no IP filter, `CPUQuota=30%`,
  `MemoryMax=768M`, default `KillMode=control-group`. State `/var/lib/van-telemetry-advisor` is
  `pi`-owned, mode 0700, 5.8 MB.
- Interactive Claude Code here gets `ANTHROPIC_BASE_URL=http://127.0.0.1:18764` (the local model
  gateway), hooks on seven events and a default model from `~/.claude/settings.json`.
  `~/.codex/AGENTS.md` (58 KB) and `~/.claude/ABOUT-ME.md` name LAN addresses and hosts. `~/.codex`
  also holds 327 MB of the owner's Codex history, `auth.json`, hooks and plugins.
- DNS is the router (`/etc/resolv.conf`: `nameserver 192.168.6.1`). cgroup BPF IP filtering works
  (`van-compute-broker.service` runs with `IPAddressDeny=any`). `/opt` holds `WidevineCdm` and
  `pigpio`.
- Relay limits: advisor replies reach the browser through `web.py` `_assistant_request`, read by
  `TelemetryClient` (`api_client.py`), which refuses a body over `http_common.MAX_RESPONSE_BYTES`
  = 1,048,576 bytes; both layers encode with `json.dumps` defaults (`ensure_ascii`), so a
  non-ASCII BMP character costs 6 bytes and a non-BMP one 12.
- DTC data: the broker's cached `/v1/diagnostics/dtcs` (Pi groups truncated at 25 per group,
  `in_vehicle_scan.modules[].dtcs`, optional `drive_reads`). Codes match
  `[PBCU][0-9A-F]{4}-[0-9A-F]{2}` (FCA) or `[PBCU][0-9A-F]{4}` (KWP). Meanings:
  `dtc_descriptions.describe_dtc`; modules: `lib.modules.MODULES` (`lib.modules.get` exits the
  process on an unknown key; not used).
- Rootfs budgets (`ROOTFS_PATH_BUDGETS_MIB`, MacBook-managed `backup_conf.sh`, measured with
  `du -skx <path>` per entry): `/var/lib/van-telemetry:2560` exists; nothing covers
  `/var/lib/van-telemetry-advisor`, `/opt` or a new image file.
- **The kernel's memory cgroup controller was disabled on this Pi** (true on 2026-11-16; enabled
  2026-11-17, see O1): `/proc/cmdline` contains
  `cgroup_disable=memory`, `/sys/fs/cgroup/cgroup.controllers` lists only `cpuset cpu io pids`,
  and no unit has a `memory.current` file. So `MemoryMax=` is accepted by systemd but enforced for
  no unit, including the advisor's existing `MemoryMax=768M` and `van-compute-broker`'s 1G.
  `CPUQuota=` (cpu controller) and `TasksMax=` (pids) are enforced. There is no PSI
  (`/proc/pressure/memory` absent), no systemd-oomd and no earlyoom; the kernel OOM killer and
  `oom_score_adj` are what exist.
- Memory (`free -m`, two readings on 2026-11-16): 3,620 MiB total; available 933 and 1,239 MiB;
  zram swap 1,810 MiB with 1,143 MiB used. Resident sizes of the relevant services (`ps`, main
  PIDs): broker 54 MiB, LAN web 27 MiB, Tailscale web 10 MiB, advisor 8 MiB idle, van-compute
  broker 11 MiB. The large consumers at that moment were the owner's interactive agent sessions
  (`claude` processes of 147–592 MiB each) and a headless Chromium (about 1 GB across processes).
  A headless research `claude -p` is expected near the smaller interactive sizes; the live check
  measures it.
- systemd places a hyphenated slice name in a hierarchy (`a-b.slice` lives under `a.slice`), so
  a unit's cgroup path must be taken from `systemctl show -p ControlGroup --value <unit>` or
  `/proc/<pid>/cgroup`, never built by hand. This plan uses no custom slice.

### Probes run (none spent model usage)

1. `--help` of `claude`, `codex`, `codex exec`, `codex exec resume`, `codex delete`, `codex login`,
   `claude setup-token`; `codex features list`.
2. Claude `initialize` control request in stream-json mode with an empty private home and no
   credentials: immediate `control_response` with models `default`, `opus`, `fable`, `sonnet`,
   `haiku` (efforts `low`..`max`) and `account: {apiProvider, tokenSource}`; no model turn.
3. Codex with an empty, unauthenticated private `CODEX_HOME` (every model request fails with 401,
   so nothing is spent): `web_search="live"` parses; under `--strict-config` an unknown
   `features.<name>` or an unknown key (`disabled_tools`) fails at config load; stdout is pure
   JSONL (warnings go to stderr); non-fatal notices arrive as `item.completed` items of type
   `error` ("Code Mode is unavailable because code-mode host is disabled…", "Falling back from
   WebSockets to HTTPS transport…").
4. Codex tool exposure: `RUST_LOG=trace` does not log the request's tool definitions in this
   build. A non-ephemeral run writes a rollout whose `response_item` of type `additional_tools`
   lists every function/custom tool offered to the model, recorded at turn start, before the
   first request (seen in the unauthenticated run); the owner's own 0.167 exec rollouts carry the
   same record. Hosted tools such as web search are not in that record; they execute on
   OpenAI's side.
5. With `shell_tool`, `unified_exec`, `code_mode`, `code_mode_host`, `apps`, `plugins`,
   `remote_plugin`, `hooks`, `multi_agent`, `multi_agent_v2`, `collaboration_modes`,
   `browser_use`, `browser_use_external`, `in_app_browser`, `computer_use`, `view_image`,
   `image_generation`, `goals`, `memories`, `shell_snapshot`, `skill_search`, `tool_suggest` and
   `sleep_tool` all disabled, the record still lists: `functions.exec` (custom; the code-mode tool,
   which Codex itself reports will "fail closed" with the host disabled), `functions.wait`,
   `functions.request_user_input`, `functions.request_user_input_async`, and the
   `collaboration` namespace (`spawn_agent`, `send_message`, `followup_task`,
   `interrupt_agent`, `list_agents`, `wait_agent`). `sleep_tool=false` removes `clock.sleep`.
   Every listed Codex model's metadata says `tool_mode: code_mode_only`.

6. Read-only memory and cgroup inspection behind the facts above (`free`, `ps`, `/proc/cmdline`,
   `/sys/fs/cgroup`, `systemctl show`).

Probe files were deleted.

## Decisions

Owner and parent decisions are marked (parent); the rest are this plan's, with reasons.

1. **Research runs in its own unit**, `van-telemetry-research.service` (the "research worker"),
   whose namespace has no `~/.codex`, `~/.claude`, broker or advisor socket, advisor state or LAN,
   and whose CLIs write only to a fixed-size volume. The advisor keeps the routes, auth, context
   gathering, records, the research rate limit, the durable daily ledger and reconciliation, and
   drives the worker over a Unix socket. The advisor unit gains one line (section 7), hiding the
   research volume.
2. **Advisor-private sign-ins** (parent: yes): Claude via `CLAUDE_CODE_OAUTH_TOKEN` from a token
   file made with `claude setup-token`; Codex via a private `CODEX_HOME` signed in with
   `codex login --device-auth`. Both on the research volume; the owner does both after deploy;
   until then the provider reports `provider_unavailable` with a reason the UI shows.
3. **Research is shared across every browser holding the access code** (parent: yes).
4. **One atomic admission authority per service, plus one kernel lock between them** (parent).
   The advisor's existing single slot stays the warning chat's authority. The research worker's
   own in-process single slot covers everything that launches a research CLI: report turns,
   follow-ups, model discovery, install verification, the memory measurement and the budget
   check; the worker is the only thing that ever launches a research CLI, and the installer and
   every check ask it and get `busy`. **For the initial rollout a warning chat and a research CLI
   never run at the same time** (parent, reversing the fourth revision): both launchers take the
   same exclusive non-blocking `flock` on `/run/van-assistant/model-cli.lock` before spawning and
   hold it until the child is reaped (section 4). This Pi's kernel enforces no `MemoryMax`, so
   memory protection is best-effort, not isolation (section 4).
5. **Hard storage bound by construction**: a preallocated 224 MiB ext4 image, loop-mounted
   `nodev,nosuid,noexec` at `/var/lib/vanresearch`, holds everything the research CLIs can write;
   the worker's `/tmp` is a 32 MiB tmpfs. Advisor-side records have code bounds (only the advisor
   writes them).
6. **Research-owned CLI copies** (parent): root-owned, read-only copies of reviewed releases under
   `/opt/van-research-cli/`, outside the owner's release directories and outside the `noexec`
   volume, so his CLI updates neither change nor disable research. One release per provider plus
   one staging slot. Updating is one deliberate command that stages, verifies and records the new
   identity (section 3); verification evidence is tied to the digest of the complete installed
   file set.
7. **Pre-execution gates**: Claude's are its explicit flags (`--tools`, `--allowed-tools`,
   `--permission-mode dontAsk`, `--restricted`, no settings, no MCP). Codex's is a verified
   production inventory of offered local tools, which 0.167 fails (above). No provider-API mock.
8. **Public DNS only** (parent): no router exception; where public DNS is blocked, research is
   unavailable and says why.
9. **No automatic repository-note excerpts** (parent): the context is the broker record, the
   module entry, the description entry and a tracked, hand-reviewed facts file.
10. **Default model** (parent): the tracked default stays Codex `gpt-6-astra` / `high`; while
    Codex is unavailable the served `default` falls through to Claude `opus` / `high` by the rule
    in section 2.
11. **Claude per-turn budget caps** `--max-budget-usd 5` (report) and `2` (follow-up) (parent).
12. **Context gathering and launching run in the advisor's per-turn thread**, so the relay's
    12-second request timeout covers only validation and record creation.

## 1. Module layout

Advisor process (`van-telemetry-advisor.service`), new files in `projects/vehicle_data/`:

| File | Owns |
| --- | --- |
| `dtc_research.json` | Vocabulary shared with the dashboard and the worker: routes, actions, states, phases, modes, error and turn reasons, limits, poll intervals, provider ids and labels, effort order. |
| `dtc_research_contract.py` | Loads the JSON; constants, regexes, exact-body validators, `relay_allowed(method, path)` for `web.py`. Pure. |
| `dtc_research.py` | `DtcResearchManager`: routes, idempotency, ledger checks, per-turn thread (context, prompt, launch, poll, parse, save), cancel, size-capped projections. |
| `dtc_research_store.py` | Records: load, atomic save (`engine_off_voltage._atomic_json`), encoded-size accounting, count cap, eviction, keep-list. |
| `dtc_research_ledger.py` | Durable rolling 24-hour admission ledger. |
| `dtc_research_reconcile.py` | Startup and connection-loss reconciliation with the worker: asks the worker which attempt it holds (instance id plus attempt id), adopts or cancels it, fences in-flight launch requests by re-asking with the same attempt id (section 4). |
| `dtc_research_context.py` | Context snapshot: broker DTC lookup, broker ignore note, module entry, description entry, facts file, VIN masking. |
| `dtc_research_facts.json` | Tracked, hand-reviewed vehicle facts and optional per-code owner notes. |
| `dtc_research_prompt.py` | System text, first-turn, follow-up and replay prompts, report parsing, encoded-size truncation. Pure; the worker imports it for truncation. |
| `assistant_models.py` + `assistant_models.json` | Catalog cache, serialized refresh thread, tracked fallback list, default, validation. |
| `research_worker_client.py` | The advisor's client for the worker socket (fixed routes, short timeouts). |
| `assistant_common.py` (tidying) | `ChatError`, `require_keys`, `ID_RE`, `TOKEN_RE`, `now`, moved from `warning_chat.py`, which re-exports them. |
| `assistant_slot.py` (tidying, then one behaviour commit) | `ModelSlot`: the warning chat's in-process single slot and its 10-starts-per-60-s window, moved out of `WarningChatManager` unchanged by the tidying commit (`try_acquire()`, `release()`, `busy`). A later behaviour commit makes `try_acquire()` also take the shared CLI lock and `release()` drop it; that is the only change to the warning chat's launch path. Research uses its own window instance in the advisor. |
| `model_cli_lock.py` | The shared kernel lock: opens `/run/van-assistant/model-cli.lock` read-only with `O_CLOEXEC`, `flock(LOCK_EX | LOCK_NB)`, release, and the holder note (section 4). Imported by both the advisor and the worker. |
| `measure_assistant_peak.py` | Read-only acceptance tool: samples the process tree under a unit's main PID (from `systemctl show -p MainPID`) every 200 ms and reports the peak summed RSS and summed `VmHWM` of a warning-chat turn. |

Research worker process (`van-telemetry-research.service`):

| File | Owns |
| --- | --- |
| `dtc_research_worker.py` | Unix-socket server at `/run/van-telemetry-research/worker.sock`; `instance_id` per process start; the research admission slot (one job at a time, each identified by an `attempt_id`, taken before a launch request is acknowledged and released only after the child is reaped); systemd watchdog pings from the request loop; status. |
| `research_memory.py` | The memory policy's activation state and thresholds (from the tracked policy), launch admission on `MemAvailable`, and the running attempt's 500 ms process-tree RSS watchdog with immediate SIGKILL on critical pressure (section 4); reads `/proc` only. |
| `research_cli.py` | Argv and environment builders (first turn, resume); stream interpreters (session id, counts, answer, tool guard); session-file location, growth check, deletion; outcome truncation. |
| `research_cli_policy.py` + `research_cli_policy.json` | Tracked policy: per provider, the flags, the Claude model ids, the Codex feature set and the allowed local tool names (empty for Codex). Runtime identity check against the installed identity record; the verification run used by the install command. |
| `cli_process.py` | Generic JSONL subprocess runner: bounded stdin, strict line parsing (fail closed), deadline, cancel, new session per child, SIGTERM then SIGKILL of the process group, reaping in `finally`, periodic watchdog callback. |
| `research_volume.py` | Volume layout, footprint measurement, watchdog thresholds, eviction to targets, startup cleanup, sweep. |
| `dtc_research_signin.py` | Owner CLI: `claude` reads the token without echo and writes it 0600; `status` prints which providers are signed in. Never prints a token. |

Systemd and docs (`projects/vehicle_data/systemd/` unless noted): `var-lib-vanresearch.mount`,
`van-telemetry-research.service`, `research-resolv.conf`, `install_dtc_research.sh` (first
deploy), `install_research_cli.sh` (stage, ask the worker to verify, record and promote a CLI
copy), the one-line advisor unit change, and `docs/dtc-research.md`.

Wiring only in the large files: `warning_chat.py` builds the catalog, ledger, store, reconciler
and `DtcResearchManager` (with its own start window), adds `--research-socket`, routes
`/v1/assistant/models` and `/v1/assistant/dtc-research*` to `server.research.handle(...)` after
`manager.authorize(...)` and outside `manager.lock`, and calls `research.close()` on shutdown.
`web.py` adds `or dtc_research_contract.relay_allowed(method, path)` to `_assistant_request`'s
`allowed` expression; every existing relay gate is unchanged. The tidying commit lands first with
`tests/test_warning_chat.py` passing unchanged.

## 2. API

### Frontend-facing routes

Differences from the 39f54d3 text, all in this section and nothing else: `busy` also covers
post-restart reconciliation and low free memory (it still covers another research job, the model
check and a running warning chat); `rate_limited` counts research starts only; turn reason
`memory_exhausted` is new. Routes, request and reply shapes, states, the other reasons, size
limits and the polling contract are unchanged.

All require `X-Van-Assistant-Key` and `X-Van-Chat-Client`; without them every route answers 401
with the existing `{"available": false, "detail": ...}` shape. All replies carry
`Cache-Control: no-store`. A listener or advisor without these routes answers 404, and the UI
hides the research controls.

**`GET /v1/assistant/models`**

```json
{"available": true, "refreshing": false,
 "groups": [
  {"provider": "claude", "label": "Claude", "available": true, "reason": null,
   "source": "discovered", "checked_at": "<iso>",
   "models": [{"id": "opus", "label": "Opus",
               "efforts": ["low", "medium", "high", "xhigh", "max"], "default_effort": "high"}]},
  {"provider": "codex", "label": "GPT (Codex)", "available": false,
   "reason": "Codex is not yet verified for research on this CLI version",
   "source": "fallback", "checked_at": null, "models": []}],
 "default": {"provider": "claude", "model": "opus", "effort": "high"}}
```

- `source`: `discovered` or `fallback`. An unavailable group has `models: []` and a `reason`.
- `efforts` may be empty; such a model is started with `effort: null`.
- `default`: the tracked default when its provider and model are available, else the first model
  of the first available group with its `default_effort`, else `null`.
- The reply never waits. `refreshing: true` means a refresh is running: the UI shows the list it
  has, disables Start, and re-fetches every 2,000 ms, at most 15 times.

**`GET /v1/assistant/dtc-research`** — one item per code (its newest run), newest first, at most 60:

```json
{"available": true,
 "items": [{"module_key": "radar_acc", "fca_display": "C1418-78", "run_id": "<32 hex>",
            "state": "done", "busy": false, "summary": "…", "summary_note": null,
            "provider": "claude", "model": "opus", "effort": "high",
            "updated_at": "<iso>", "runs": 2}]}
```

**`GET /v1/assistant/dtc-research/code/<module_key>/<fca_display>`** — `module_key` matches
`[a-z][a-z0-9_]{0,39}`, `fca_display` matches `[PBCU][0-9A-F]{4}(-[0-9A-F]{2})?`, else 400:

```json
{"available": true, "module_key": "radar_acc", "fca_display": "C1418-78",
 "runs": [<RunSummary>, ...], "selected": <Run> | null}
```

`runs` newest first; `selected` is the newest run in full, `null` when there are none.

**`GET /v1/assistant/dtc-research/run/<run_id>`** → `{"available": true, "run": <Run>}`
(`run_id`: 32 lowercase hex; unknown → 404 `not_found`).

**`GET /v1/assistant/dtc-research/run/<run_id>/progress`** → `{"available": true, "progress": <Progress>}`.

**`POST /v1/assistant/dtc-research`**, exact bodies (missing or extra key → 400):

- `{"action": "start", "module_key", "fca_display", "provider", "model", "effort", "request_id"}`
- `{"action": "ask", "run_id", "question", "request_id"}` — 1–2,000 characters after trimming,
  at most 12 KiB as encoded JSON.
- `{"action": "cancel", "run_id", "request_id"}` — stops the running turn; a no-op success when
  nothing runs.

Reply `{"available": true, "run": <Run>}`. `request_id` is 32 lowercase hex; a repeated one
returns the same run and starts nothing.

Shapes:

- `RunSummary`: `{run_id, module_key, fca_display, created_at, updated_at, provider, model,
  effort, state, busy, summary, summary_note, duration_s, error, reason, turn_count,
  can_continue, continue_mode}`.
- `Progress`: `{run_id, state, busy, phase, progress, activity: {searches, pages}, elapsed_s,
  turn_count, updated_at}`.
- `Run`: every `RunSummary` field plus `{phase, progress, activity, elapsed_s, format_warnings, turns}`.
- `turns`, in order, the report first:
  - assistant: `{role: "assistant", kind: "report" | "answer", text, at, state, error, reason,
    mode, duration_s}` (`text` is `null` until complete; the report's excludes its `SUMMARY:` line);
  - user: `{role: "user", kind: "question", text, at}`.

Field meanings:

- `state` (run, from the report turn): `running`, `done`, `failed`, `cancelled`.
- `busy`: the last assistant turn is running (a `done` run is busy during a follow-up).
- turn `state`: `running`, `complete`, `failed`, `cancelled`.
- `mode`: `initial`, `resumed` (the same CLI session, verified), `replayed` (the transcript was
  replayed into a new session; the UI says so under that answer).
- `phase`: `preparing`, `starting`, `searching`, `reading`, `writing`, `finishing`, or `null`.
  `progress`: owner-facing text built on the Pi (`Searching the web · 3 searches, 1 page`), or `null`.
- `elapsed_s`: seconds since the running turn started, computed on the Pi; `null` when idle.
- `summary`: plain text, at most 140 characters, or `null`; `summary_note`: `null`, `missing`, `truncated`.
- `format_warnings`: missing report headings, informational.
- `error` (sentence) and `reason` (code) describe the last failed turn.
- `can_continue`: report complete, nothing running, fewer than 24 turns, room in the record.
  `continue_mode`: `resume`, `replay`, `none`.
- Times are ISO 8601 UTC; the UI shows Mountain time.

Errors on every route: `{"available": false, "reason": "<reason>", "detail": "<sentence>"}`.

| HTTP | reason | When |
| --- | --- | --- |
| 400 | `invalid_request` | validation; model or effort not offered for that provider |
| 404 | `not_found` | unknown `run_id` or route |
| 409 | `run_busy` | ask while that run's turn is running; start while a run for that code is running |
| 409 | `turn_limit` | 24 turns reached |
| 409 | `record_full` | the run cannot hold another question and answer within its size limit |
| 409 | `cannot_continue` | ask on a run whose report failed or was cancelled |
| 429 | `busy` | detail says which: another research job holds the worker's slot (a report, a follow-up, the model check, or install maintenance); a warning chat is answering a question; the advisor is still asking the worker what it holds after a restart or lost contact; or the Pi has too little free memory to start one now |
| 429 | `rate_limited` | 10 research starts in the last 60 s (research's own window; no longer shared with the warning chat) |
| 429 | `daily_limit` | 60 research launches in the last 24 h |
| 503 | `provider_unavailable` | not signed in, not verified on this CLI version, CLI copy missing, or public DNS unreachable (detail says which) |
| 503 | `worker_unavailable` | the research worker is not running or not answering |
| 503 | `storage_error` | the ledger or a record could not be written |

Turn `reason` values (`Run.reason`, `turns[].reason`): `timeout`, `cancelled`, `interrupted`,
`worker_unavailable`, `provider_error`, `auth`, `upstream_rate_limit`, `budget_exceeded`,
`policy_violation`, `protocol_error`, `space_exhausted`, `memory_exhausted`, `daily_limit`.
(`memory_exhausted`, added in the fourth revision: the worker's memory watchdog stopped the turn.)

Size guarantees: measured as the advisor encodes (`ensure_ascii`, worst case 12 bytes per
character): report text at most 96 KiB encoded, answer 48 KiB, question 12 KiB (cut by encoded
size, ending in `[truncated on the Pi]`); one `Run` at most 384 KiB (a follow-up is refused with
`record_full` unless the current encoded size plus 12 + 48 + 2 KiB fits); a `RunSummary` or list
item at most 3 KiB; 60 runs in total. So a code reply is at most about 565 KiB, the list 181 KiB,
`Progress` under 2 KiB, all under the relay's 1,048,576 bytes, which is not raised.

Polling contract:

- While a dialog shows a run with `busy: true`, poll its `/progress` every 1,000 ms and fetch
  `/run/<run_id>` when `turn_count`, `state` or `busy` changes. Pause while the page is hidden;
  stop when the dialog closes.
- Fetch the list when the Codes card mounts with an assistant key, after a dialog closes, and
  when a poll sees a run go from busy to not busy.
- Fetch the code route when a dialog opens; `Runs` switches with `/run/<run_id>`.

### Worker API (advisor ↔ worker only)

Unix socket `/run/van-telemetry-research/worker.sock`, mode 0600, user `pi`. Exact JSON bodies;
every field revalidated by the worker; argv built only from code, the tracked policy and the
installed identity; `prompt` is data on stdin. Identities: `instance_id` (random per worker
process start) and `attempt_id` (32 hex, chosen by the caller for each launch and, for research
turns, saved in the record's turn before the launch request; distinct from `run_id`, since a turn
may have an initial attempt and a replay attempt).

**Send-once rule** (finding 1): the advisor sends a launch request for a given `attempt_id` at most
once, ever. After any doubt (no reply, a timeout, an advisor restart, a changed `instance_id`) it
only asks about that attempt (`GET /turns/<attempt_id>`), and every new launch uses a new
`attempt_id` with a new ledger admission. Every launch request carries `expected_instance_id`, the
worker instance the advisor last saw in `GET /status`; a worker with a different `instance_id`
answers 409 `stale_instance` and launches nothing.

The worker's slot is the only admission authority for research CLIs (section 4). Every launching
request (`POST /turns`, `/discover`, `/maintenance`) is handled under the worker's slot lock: if
`expected_instance_id` matches, the `attempt_id` has never been seen by this instance, and the
slot is free, the worker takes the slot for that attempt, records a tombstone, and answers 202.
Otherwise it answers 409 with `{"reason": "stale_instance"|"duplicate_attempt"|"busy"|
"warning_chat"|"low_memory"|"not_activated", "detail"}` and launches nothing; a repeated
`attempt_id` is always `duplicate_attempt`, even if its outcome has been dropped. The slot is
released only after the attempt's process group has been reaped. The shared CLI lock is taken
inside the slot just before each spawn (section 4); failing it answers `warning_chat` for a request
that has not yet been acknowledged, or ends an already accepted attempt as `failed` with reason
`busy` (only possible for multi-spawn maintenance; turns spawn once, right after acceptance).

Tombstones: every `attempt_id` accepted in this instance's lifetime is kept as
`{attempt_id, kind, state, accepted_at, outcome_state: pending|held|acknowledged|dropped}`, at most
4,096 entries (about 0.5 MiB; with one launch at a time and 60 research turns a day that is weeks
of uptime). When full, the oldest acknowledged or dropped tombstone is evicted; if none is, new
launches are refused with `busy` and the worker logs it. Outcomes (the answer text) are kept until
the advisor acknowledges them, at most 16; a 17th unacknowledged one drops the oldest outcome,
whose tombstone then says `dropped`.

- `GET /status` → `{instance_id, pid, job: null | {attempt_id, run_id, kind:
  turn|discovery|maintenance, state: accepted|running|finished, started_at},
  providers: {claude|codex: {identity: ok|changed|missing|unverified|disabled|checking, signed_in,
  reason}}, memory_policy: {activated, reason}, checked_at, checks_current, dns_ok, dns: {ok,
  result, reason, checked_at, seconds, last_ok_at, fresh_seconds}, volume: {free_bytes},
  mem_available_bytes}` as built (the plan's `used_bytes` and `cli_lock` were not built).
  `accepted` covers the moment between acknowledgement and spawn. It answers within about 1 s: the
  identity facts and DNS come from background checks (`docs/dtc-research.md` "Worker status").
- `POST /turns` `{"attempt_id", "expected_instance_id", "run_id", "provider", "model", "effort",
  "kind": "initial|resume", "session_id" (resume only), "prompt", "timeout_s", "budget_usd"
  (Claude)}` → 202 or 409 as above.
- `GET /turns/<attempt_id>` → `{instance_id, known: true, state: accepted|running|finished, phase,
  activity, session_id, outcome_state, outcome}` or `{instance_id, known: false}`. `outcome`
  (present when `outcome_state` is `held`; only after the process group is reaped) is
  `{status: complete|failed|cancelled, answer, reason, detail, mode, diagnostics}`. The worker
  truncates `answer` to the encoded caps (96 KiB report, 48 KiB answer) and `diagnostics` is
  fixed-shape numbers, so this reply is under 112 KiB.
- `POST /turns/<attempt_id>/ack` → the advisor has saved the outcome; the worker discards it
  (tombstone `acknowledged`).
- `POST /turns/<attempt_id>/cancel` (also for discovery) → idempotent; cancels an attempt still
  in `accepted`, which then never spawns. Cancelling an id the worker has never seen fences it.
- `POST /attempts/<attempt_id>/fence` → resolve-or-fence, under the same lock as admission: a known
  attempt answers with its `GET` state; an unknown one gets a tombstone (`state: finished`,
  `outcome_state: dropped`), so a later launch with that id is refused `duplicate_attempt`, and the
  reply is `{instance_id, known: false, fenced: true}`. A full table answers 409 `busy` (the
  advisor keeps asking).
- `POST /discover` `{"attempt_id", "expected_instance_id", "provider": "claude"}` → 202, then
  `GET /discover/<attempt_id>` returns the models once the child is reaped (at most 30 s).
  `GET /discover/codex` reads files only and takes no slot.
- `POST /maintenance` `{"attempt_id", "expected_instance_id", "purpose": "install|measure|budget_check"}`
  (used only by `install_research_cli.sh`, running as root on the same socket) → 202 or 409; then
  `POST /maintenance/<attempt_id>/verify|measure|budget-check` runs that step inside the held slot
  and `GET /maintenance/<attempt_id>` returns its result; `POST /maintenance/<attempt_id>/end`
  releases the slot. A reservation ends by itself after 20 minutes or with the worker process.
- `POST /sweep` `{"keep": [run_id, ...]}` → deletes run directories and Codex rollouts not kept
  (it never touches a running job; reconciliation cancels those explicitly).
- `DELETE /runs/<run_id>` → deletes that run's session files.


## 3. CLI invocations and the tool policy

Paths: `V = /var/lib/vanresearch` (volume), `R = V/runs/<run_id>`, `C = /opt/van-research-cli`.
Every child runs with `start_new_session=True`, stdin a pipe, stdout and stderr pipes, cwd
`R/work` (empty, kept for the run's life: Claude keys a session to its cwd),
`TMPDIR=V/tmp/<attempt_id>` (emptied on every exit path and at worker start), and an environment
built only from the names listed below.

### Research-owned CLI copies and identities

Layout (root-owned, directories 0755, files 0755/0644, never writable by `pi`):

```
C/claude/<version>/claude              the single Claude binary
C/codex/<version>/bin/codex            Codex main binary
C/codex/<version>/codex-path/          its companion rg and bwrap (copied as shipped)
C/staging/<provider>-<version>/        at most one staged release at a time
C/identity.json                        per provider: version, executable path, the complete file
                                       list (relative path, mode, size, SHA-256) of its release
                                       directory, the file-set digest, verified_at, verification
                                       result, and evidence bound to that digest
```

The file-set digest is SHA-256 over the sorted `(relative path, mode, size, SHA-256)` lines of
every regular file, symlink and directory in the provider's release directory. Verification
evidence records the digest it was obtained against; the worker accepts the evidence only when
the installed tree's recomputed digest equals it (extra, missing or changed files all fail).

Codex's code-mode host, exec server and voice components are not copied: research disables
those features, and a release that needs them to start fails verification.

`sudo <checkout>/projects/vehicle_data/systemd/install_research_cli.sh claude|codex` (the one
deliberate update command):

1. Refuses if `C/staging` is not empty (a lock file in `C` serializes concurrent runs of the
   command itself).
2. Copies the owner's currently installed release (`readlink -f ~pi/.local/bin/claude`, or the
   Codex release directory's `bin/codex` and `codex-path/`) into `C/staging/<provider>-<version>`,
   root-owned and read-only, and computes the file list and digest.
3. Opens a maintenance reservation (`POST /maintenance` with a fresh `attempt_id`): the worker
   takes its slot for it, and keeps it through verification, promotion and the worker restart, so
   no research turn can start in between. If the worker answers 409 `busy` (its slot holds a
   research turn, discovery or another maintenance, or the warning chat holds the shared CLI lock
   when verification is about to spawn), the command deletes the staging copy, prints the
   worker's sentence ("research is busy" / "a warning chat is running; run this again later") and
   exits non-zero; it never launches a CLI itself. Within the reservation it asks for
   verification (`POST /maintenance/<attempt_id>/verify`) of the staged copy inside the worker's own
   sandbox, so the check runs under production conditions; the worker recomputes the staged digest
   before launching anything, and takes the shared CLI lock around each spawn.
   - Claude: `--help` lists every flag the runner uses; the `initialize` handshake answers and
     lists models. No model turn. Claude's pre-execution gate is its flags, so no tool inventory
     is needed.
   - Codex (needs the private sign-in): for **every Codex model the policy would offer** (tool
     exposure can differ by model: each model's metadata carries its own `tool_mode`,
     `shell_type` and `experimental_supported_tools`), one first turn ("Reply with OK.") and one
     resume of it, with the exact production argv and the production private home, through the
     staged binary. All of them run inside one `verify` attempt, so they hold the worker's slot
     throughout. For each turn it reads the rollout's `additional_tools` record and compares every
     function, custom and namespace tool name offered with the tracked allowlist (empty: web
     search is hosted and not in that record). A missing record, an unparseable one, one without
     a `tools` list, an entry without a type and name, or a turn whose record cannot be located
     **fails** verification; it is never read as an empty list. It also checks that the
     rollout's instruction messages contain no `AGENTS.md` marker. Models that pass are recorded;
     only recorded models are offered. These are deliberate admin launches: they run under the
     maintenance reservation but are not counted in the owner-facing daily ledger.
4. On success: replaces `C/<provider>/` with the staged release (old release deleted), records
   the identity, file list, digest and the verification evidence (models and tool names seen,
   date, CLI version, the digest verified) in `C/identity.json`, then `systemctl restart
   van-telemetry-research.service` while the reservation is still held; the reservation ends with
   the old worker process, and the new instance starts with a free slot and the new identity. On
   failure: deletes the staging copy, records the failed result and its reason in
   `C/identity.json` (the provider stays or becomes unavailable with that reason), ends the
   reservation (`POST /maintenance/<attempt_id>/end`) and prints it.
5. A reservation the installer abandons (it dies or hangs) ends by itself after 20 minutes; the
   worker logs it. Other modes of the same command use the same reservation: `--budget-check`
   (section 8) and `--measure` (section 4, memory activation).

The worker reads `C/identity.json` at start and before each launch, `stat`s every recorded file
and re-hashes the tree when any `(device, inode, size, mtime)` or the directory listing changed
since its last check; a digest mismatch, a missing or extra file, or a non-passing verification
makes that provider unavailable
(`identity: changed|missing|unverified`). The owner's own CLI updates touch none of this.

### Codex tool evidence and why Codex is unavailable this round

The probes above show that Codex 0.167, with every tool feature it lists disabled, still offers
`exec` (code mode), `wait`, `request_user_input(_async)` and the six collaboration tools, and
that the config has no generic tool-removal key (`disabled_tools` is refused). `exec` reports it
will fail closed without the code-mode host, but that is the CLI's statement, not a
pre-execution gate this plan can verify, and `spawn_agent` multiplies model calls. So the
verification in step 3 would fail for 0.167, and Codex research ships as `provider_unavailable`
("Codex is not yet verified for research on this CLI version"). The production rollout record is
the evidence mechanism, so no mock is needed. Follow-up F3 is the source-level review that could
enable it.

The Codex invocation is still built, tested with fake CLIs, and used by `verify`:

```
C/codex/<version>/bin/codex exec --ignore-user-config --ignore-rules --strict-config
  --skip-git-repo-check --json --color never -C R/work
  -c approval_policy="never"  -c default_permissions="dtc-research"
  -c 'permissions.dtc-research.filesystem={":minimal"="read", ":workspace_roots"="read"}'
  -c permissions.dtc-research.network.enabled=false
  -c web_search="live"  -c project_doc_max_bytes=0  -c history.persistence="none"
  -c developer_instructions=<SYSTEM>  -c log_dir="V/codex-home/log"
  -c model="<model>"  -c model_reasoning_effort="<effort>"
  -c features.<name>=false   (the tracked set)
  -
```

Resume: `exec resume` with the same options (no `-C`; cwd `R/work`) and `<thread_id> -`.
Environment: `CODEX_HOME=V/codex-home`, `HOME=R/home`, `PATH=/usr/bin:/bin`, `LANG`, `LC_ALL`,
`TMPDIR`, `SSL_CERT_FILE`/`SSL_CERT_DIR` when set. Stream supplements: allowed item types
`agent_message`, `reasoning`, `web_search`, and `error` (a notice, classified, never fatal on its
own); anything else kills the run (`policy_violation`). At runtime the worker also reads each
new turn's `additional_tools` record as soon as the rollout has it and kills the run if any name
falls outside the allowlist, or if the record is missing, malformed or incomplete by the time the
first model item arrives. Verified resume: `thread.started` reports the stored id and that
rollout grew; otherwise the answer is discarded and the turn replays once.

### Claude

Environment: `HOME=R/home`, `CLAUDE_CONFIG_DIR=R/home/.claude`, `PATH=/usr/bin:/bin`, `LANG`,
`LC_ALL`, `TMPDIR`, `SSL_CERT_FILE`/`SSL_CERT_DIR` when set,
`CLAUDE_CODE_OAUTH_TOKEN=<V/claude-oauth-token>`, `DISABLE_AUTOUPDATER=1`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. The worker unit sets no `ANTHROPIC_*`, `OPENAI_*` or
proxy variables, and the allowlist would drop them anyway. The token file must be a regular,
non-symlink file, mode 0600, at most 4 KiB, one line of `[A-Za-z0-9._-]`; read at each launch,
never logged, stored or put in argv.

First turn (prompt on stdin):

```
C/claude/<version>/claude -p --input-format text --output-format stream-json --verbose
  --session-id <uuid4, chosen by the worker, reported in the first status>
  --model <model> [--effort <effort>]  --system-prompt <SYSTEM>
  --tools WebSearch,WebFetch  --allowed-tools WebSearch,WebFetch
  --permission-mode dontAsk  --permission-prompts none
  --setting-sources ""  --strict-mcp-config  --restricted  --disable-slash-commands
  --max-budget-usd 5
```

Resume: `--resume <session_id>` instead of `--session-id`, `--max-budget-usd 2`.

Pre-execution gates: `--tools` defines what is offered; `dontAsk` with `--allowed-tools` denies
any other call before it executes; `--restricted` removes code-running tools and refuses bypass
mode; no settings, MCP or slash commands load. Supplements: the first event must be
`system`/`init` with `tools` within `{WebSearch, WebFetch}` and `mcp_servers` empty; any other
`tool_use` name kills the run (`policy_violation`). Counts: `WebSearch` → search, `WebFetch` →
page. The answer is the `result` event's `result`, accepted only with `subtype == "success"` and
`is_error` false; `error_max_budget_usd` → `budget_exceeded`; `total_cost_usd` is kept in
diagnostics.

Verified resume (Claude): `resumed` only when the init `session_id` equals the stored id **and**
`R/home/.claude/projects/*/<session_id>.jsonl` is larger after the turn than before it. An init
with another id kills the run at once; either failure discards the answer, deletes the new
session's files, and the advisor replays once.

### Both providers

- Strict output: a stdout line that is not a JSON object, longer than 4 MiB, or a stdout total
  over 16 MiB kills the run (`protocol_error`). Stderr is kept as an 8 KiB rolling buffer for
  classification only, never returned, stored or logged raw.
- Replay: when the session file is absent or a resume fails verification, the advisor writes a
  `replay` ledger entry and launches a first-turn invocation (new `attempt_id`) whose prompt is
  the saved context, the transcript (oldest follow-ups dropped first past 160 KB with an explicit
  line, the report always kept) and the question; `mode: "replayed"`; at most one replay per turn.
- Model and effort are fixed at start. Timeouts: report 600 s, follow-up 300 s, discovery 30 s,
  verification 600 s.
- Every exit path (cancel, deadline, output limit, policy violation, space watchdog, exception):
  SIGTERM to the process group, 3 s, SIGKILL, wait; then `TMPDIR` emptied, the volume cleanup run,
  and the shared CLI lock and the slot released, in that order. The memory watchdog's critical
  path is the exception: SIGKILL to the process group at once, no grace (section 4). Worker stop
  cancels the job; systemd's control-group kill is the backstop.
- Diagnostics also record the attempt tree's peak summed RSS and summed `VmHWM` (500 ms samples).
- Diagnostics: numeric milestones, exit code, byte counts, fixed categories, cost and token
  counts; logged as `dtc_research_timing` JSON lines with no prompt, answer, URL or session id.

## 4. Admission, reconciliation, memory and the model catalog

### Admission

- **Warning chat**: the advisor's existing in-process single slot and its 10-per-60-s window
  (moved into `assistant_slot.ModelSlot` by the tidying commit, same behaviour), plus the shared
  CLI lock below.
- **Research**: the worker's in-process single slot, the only admission authority for anything
  that launches a research CLI: report turns, follow-ups, Claude model discovery, and the
  installer's maintenance (verification, memory measurement, budget check). The installer and
  checks are clients of the worker; none of them launches a CLI. The slot is taken under one lock
  in the request handler before the 202 is sent and released only after the attempt's process
  group is reaped (section 2, worker API), so there is no window between acceptance and spawn.
- The advisor adds, before asking the worker: research's own 10-starts-per-60-s window and the
  durable daily ledger (section 6). It treats the worker's answer as the truth about research
  business: a 409 becomes the frontend's 429 `busy` (or 503 `provider_unavailable` for
  `not_activated`), and the ledger entry for that request is removed (section 6).

### The shared CLI lock: a warning chat and a research CLI never run together

For the initial rollout (parent's decision, reversing the fourth revision) the two services
exclude each other with one kernel-backed reservation, not by observing each other:

- The lock file is `/run/van-assistant/model-cli.lock`, created at boot by a tracked tmpfiles
  entry (`d /run/van-assistant 0750 pi pi -` and `f /run/van-assistant/model-cli.lock 0640 pi pi
  -`). Both units see it read-only (`ProtectSystem=strict`); `flock` works on a read-only file
  descriptor, so neither unit needs write access to `/run`.
- `model_cli_lock.acquire()` opens it `O_RDONLY | O_CLOEXEC` and calls
  `flock(LOCK_EX | LOCK_NB)`. The launching process holds it from just before it spawns the child
  until after the child is reaped. `O_CLOEXEC` keeps the descriptor out of the child, so the
  holder is always the launching service process itself.
- If the holder dies, the kernel releases the lock; `KillMode=control-group` (both units) then
  kills the dead holder's child with its cgroup. Residual: from the holder's death until
  systemd's stop of that unit completes (SIGTERM, then SIGKILL after the unit's
  `TimeoutStopSec`, 10 s for both units) the other service can take the lock and spawn, so a
  second CLI can run beside a dying one for up to that long. This is an accepted crash-path
  limitation, not a "never overlap" guarantee; docs/dtc-research.md says so.
- Warning chat, exactly where it changes: only inside `assistant_slot.ModelSlot`. `try_acquire()`
  (called from `WarningChatManager.start_turn` at the point where today's code calls
  `self.slot.acquire(blocking=False)`)
  takes the in-process slot, then the CLI lock; if the lock is held, it releases the slot and
  raises `ChatError(429, "A diagnostic-code research job is running; ask again when it finishes")`.
  `release()` (called from `WarningChatManager._run`'s `finally`, after `CodexRunner.run` has
  waited for and reaped the Codex process) drops the lock and then the slot. Nothing else in the
  warning chat changes.
- Research: the worker takes the lock inside its slot immediately before each spawn and releases
  it after reaping. If the lock is held, a not-yet-acknowledged request answers 409
  `warning_chat` ("a warning chat is answering a question"), which the advisor turns into 429
  `busy` with that sentence.
- No cgroup reads and no HTTP between the services take part in this exclusion. The model has no
  tool that can open a file; a process inside the worker unit could at worst hold the lock, which
  blocks the warning chat with `busy` and changes nothing else.

### Reconciliation (advisor restart, lost contact)

The advisor never infers that research is idle from a timeout, a refused connection, a missing
path or a process listing, and never re-sends a launch (the send-once rule, section 2). Its
research state is one of `unknown`, `idle`, `holding <attempt>`.

- At advisor start the state is `unknown`, and running turns in records are marked
  `reconciling`, not failed. Research requests get 429 `busy` ("the assistant is checking on a
  research job after a restart") while `unknown` and the worker is reachable, and 503
  `worker_unavailable` while it is not.
- Every second while `unknown`, the advisor calls `GET /status`:
  - `job` is a turn whose `attempt_id` a record holds: **adopt** it (state `holding`; polling of
    `GET /turns/<attempt_id>` resumes; a held outcome, including one kept from before the advisor
    restarted, is saved and then acknowledged).
  - `job` is any other turn or a discovery: **cancel** it and stay `unknown` until a later status
    shows `job: null`. A maintenance job is not cancelled; the advisor waits for it.
  - `job: null`: every remaining `reconciling` turn is asked about with `GET /turns/<its attempt>`:
    a held outcome is saved and then acknowledged (a failed save leaves it held and the pass is
    retried); `known: false` (fenced first when it comes from the instance now running, see the
    last bullet), or `outcome_state: dropped`, makes the turn `failed` with reason `interrupted`
    (it is never relaunched under the same id; the owner may ask again, which is a new attempt and
    a new ledger admission). Then `/sweep` with the kept run ids, and the state becomes `idle`.
- Lost contact while `holding`: the advisor keeps the turn `running` and keeps asking about the
  same `attempt_id`, launching nothing, until the worker answers. An answer from a new
  `instance_id` with `known: false` means the attempt died with the old process (its child was
  killed by the control-group stop), and the turn becomes `interrupted`.
- A launch request whose reply was lost is never repeated: the advisor asks about the attempt.
  `known: true` → adopt; `known: false` from the expected instance → the request may still be in
  transit, so the advisor fences the attempt (`POST /attempts/<id>/fence`) and the turn becomes
  `interrupted` only once the fence is confirmed (`fenced: true`); if the fence finds the attempt
  known after all, the advisor follows it. `known: false` from a new instance → `interrupted`
  with no fence (a late request names the old instance and is refused `stale_instance`). An
  attempt cancelled while `accepted` never spawns.
- An outcome is acknowledged only after the record holding it is saved; a failed write keeps it
  held on the worker and the follow thread (or the reconciler) retries. A follow thread that fails
  for any other reason hands its turn back to `reconciling` and the state to `unknown`.

Liveness: the worker is `Type=notify` with `WatchdogSec=30`, pinged from the request-handling
loop itself, with `KillMode=control-group`. A worker whose server stalls is killed by systemd with
its whole cgroup (the CLI child included, which also releases the shared CLI lock) and restarted
with a new `instance_id`. Until then the advisor answers research requests with `busy` or
`worker_unavailable` and launches nothing. The warning chat continues, except that while the
stalled worker still holds the CLI lock (it does for as long as its attempt's child lives) the
warning chat answers `busy` until systemd clears the worker.

### Memory: best-effort protection, not isolation

The memory controller is on since the 2026-11-17 reboot (owner decision, O1 closed), so the
unit's `MemoryMax=768M` is a kernel-enforced bound on the whole cgroup (worker, CLI children,
tmpfs, charged page cache). Everything below is the worker's own, finer guard; its watchdog sums
process-tree RSS, which is not the whole cgroup charge, so the unit limit stays the backstop.

- **One assistant CLI at a time** on the Pi (the shared CLI lock above), so the warning chat and
  research never add up.
- **OOM ordering** (enforced by the kernel's victim choice only): `OOMScoreAdjust=800` on the
  worker unit, inherited by its CLI child, so if the Pi runs out of memory the kernel kills a
  research CLI before the broker, the web listeners or the warning chat. It sets no limit.
- **Launch admission** (worker, under its slot): refused with 409 `low_memory` when
  `MemAvailable` is below the launch floor, for every launching request and maintenance step; then
  checked again just before each spawn (`Runner.guarded_run`, also for a maintenance step's second
  spawn and the installer's `--help`), where a failure ends the attempt `memory_exhausted` without
  spawning.
- **Run-time watchdog** (worker, every 500 ms during an attempt): sums the RSS of the attempt's
  process tree (the child and its descendants, from `/proc/<pid>/task/*/children`). If the tree
  exceeds its cap, or `MemAvailable` falls below the critical threshold, the worker sends
  **SIGKILL to the process group at once, with no SIGTERM grace**, reaps it and fails the turn
  with `memory_exhausted`. Ordinary cancels and deadlines keep SIGTERM, 3 s, SIGKILL.
- **Activation gate**: the tracked policy (`research_cli_policy.json`, `memory` section) holds
  `measured: false` until the acceptance sequence has recorded real peaks. While it is false the
  worker refuses `/turns` with `not_activated`, and the advisor reports both providers
  `provider_unavailable` ("research is not activated on this Pi until its memory use has been
  measured"). Discovery and maintenance still work, so the measurement can run. The activation is
  bound to the measured CLI identity: the policy's `memory.cli_digest` must equal the file-set
  digest of the Claude copy now installed (`research_memory.activation`), so installing a
  different copy turns research off ("the installed Claude copy differs from the one measured")
  until it is measured again and the new digest is committed.

Initial (provisional) thresholds, used only for the acceptance measurement:

| Threshold | Provisional value |
| --- | --- |
| Launch floor (`MemAvailable` before spawn) | 900 MiB |
| Process-tree cap (immediate SIGKILL) | 700 MiB |
| Critical `MemAvailable` (immediate SIGKILL) | 300 MiB |

Measured on 2026-11-16: 3,620 MiB total, `MemAvailable` 933 and 1,239 MiB, zram swap 1,810 MiB
with 1,143 MiB used; broker and web listeners about 90 MiB together. The 200–350 MiB research and
150 MiB warning-chat figures used earlier were estimates and are not used for any threshold.

What the acceptance sequence must record before activation (section 7, step 9):

1. `install_research_cli.sh claude --measure`: within a maintenance reservation, one real Claude
   report turn (`sonnet` / `medium` on a fixed code) and one resumed follow-up, each sampled every
   500 ms; records the peak summed RSS and the peak summed `VmHWM` of the process tree, and the
   lowest `MemAvailable` seen, for each.
2. A warning-chat Codex turn sampled by `measure_assistant_peak.py --unit
   van-telemetry-advisor.service` (read-only): the same three figures.
3. The parent writes into the tracked policy, in a reviewed commit: `research_peak_mib` = the
   larger `VmHWM` peak of the two research turns; `tree_cap_mib` = `research_peak_mib` × 1.5,
   rounded up to 64 MiB; `launch_floor_mib` = `research_peak_mib` + a reserve of 384 MiB (room
   for the broker and system services to grow, page cache the broker's SQLite needs, and the
   largest ordinary transient on this Pi); `critical_mib` = 300; `warning_chat_peak_mib` for the
   record; the measurement date and CLI versions; and `measured: true`. If the warning-chat peak is
   larger than the research peak, the reserve grows to cover it.
4. Deploying that commit restarts the worker, which then accepts turns.

### Model catalog

- One refresh thread in the advisor, serialized, one provider at a time; an entry is fresh for 15
  minutes after a success and 60 s after a failure; a stale `GET /models` wakes the thread and
  replies at once with `refreshing: true`.
- Claude discovery (only when the worker reports Claude `identity: ok` and signed in): the
  advisor sends `POST /discover` with a new `attempt_id`; the worker takes its slot or answers 409
  (then the refresh retries 60 s later and the cached or fallback list is served). The worker runs
  the `initialize` handshake with a fresh `V/catalog/claude-home` it deletes afterwards and
  releases the slot after reaping. Models: the response list minus `default`, intersected with
  the policy's Claude ids; labels from `displayName` without "(recommended)"; efforts from
  `supportedEffortLevels`; `default_effort` `high` when offered. `account.tokenSource == "none"`
  → unavailable. Discovery is not counted in the start window or the ledger.
- Discovery spawns a Claude CLI, so it also takes the shared CLI lock: while it runs (at most
  30 s), research starts get 429 `busy` ("checking which models are available") and warning-chat
  starts get the warning chat's 429 naming the research service; a discovery that finds the lock
  held by the warning chat is refused and retried 60 s later. The dialog keeps Start disabled
  while `refreshing` is true, so the owner's own research flow does not meet it.
- Codex: unavailable unless its identity is `ok` (verified). When verified, discovery reads
  `V/codex-home/models_cache.json` through the worker (no child, no gate), entries with
  `visibility == "list"` and `supports_search_tool`, sorted by `priority`.
- Both providers are unavailable, with that reason, when the worker's DNS check fails its
  freshness rule (no successful lookup of `api.anthropic.com` through the bound resolvers in the
  last 300 s; lookups run in the background, one at a time, at most one per 30 s).
- Validation of `start`: provider available; model id matches
  `[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,119}` and is in the current list; effort is one of the model's
  efforts, or `null` exactly when it has none.
- Tracked fallback `assistant_models.json`: Claude `opus`, `sonnet`, `haiku`; Codex `gpt-6-astra`,
  `gpt-6.1-sol`, `gpt-6.1-terra`; default `codex / gpt-6-astra / high`. A fallback list never
  makes an unavailable provider available.

## 5. Threat model

**What the research child processes can see** (the worker unit, section 7): a read-only empty
tmpfs over `/home` with only the obd-things checkout (read-only, minus `tmp/`, `.git` and
`AGENTS.secret.md`); the root-owned CLI copies under `/opt/van-research-cli` (read-only, like
all of `/opt`); the research volume `V` (read-write: run homes, the private Codex home, the Claude
token); a 32 MiB private `/tmp`; read-only `/usr`, `/etc` and the rest of the system. Not
visible: `~/.codex`, `~/.claude`, `~/secrets`, `~/.ssh`, `~/.aws`, `~/.config`, the advisor's
state (records, access code), `/var/lib/van-telemetry`, and the sockets under
`/run/van-telemetry`, `/run/van-telemetry-advisor`, `/run/dbus`, `/run/user`, `/run/tailscale`.
Public internet only; no CAN, packet or netlink sockets, no devices.

**Who else can see the volume.** The advisor unit hides it (`InaccessiblePaths`), so the warning
chat's Codex cannot read the research credentials. Other processes running as `pi` without such
a namespace (interactive shells, other `pi` services) can read it, as they can read
`~/.codex/auth.json` today; root can read everything. Accepted, and stated in the docs.

**If a local tool slipped through** (a CLI offering one the gates did not anticipate): it would
run inside the worker's namespace, so it could read the checkout's tracked code and docs, the CLI
copies, the system's world-readable files, and the volume, whose valuable contents are the
worker's own private credentials (the Claude token and the private Codex `auth.json`) and other
runs' transcripts. It could reach the public internet, so it could exfiltrate those. It could not
reach anything of the owner's (home, his CLI credentials and histories, the advisor's state, the
broker, the LAN, CAN). Remedy if that happens: revoke the Claude token and sign the private Codex
home out; the owner's own sign-ins are unaffected.

**Hostile page steering the model.**
- *Read*: Claude is offered only `WebSearch` and `WebFetch` (flag-gated, init-checked); Codex is
  disabled this round. The prompt holds the broker record, the module and description entries,
  the hand-reviewed facts file, the owner's ignore note for the code if any, and his follow-up
  questions. VIN-shaped strings are masked in all of it, in every prompt as it is built (records on
  the Pi keep the owner's text as typed). Apart from that mask the owner's own text is passed as he
  wrote it: a location he types goes to the provider and possibly into a search query. That
  residual is documented in the dialog text and `docs/dtc-research.md`; nothing else is promised.
- *Fetch local services*: Claude's WebFetch runs on the Pi; the worker's `IPAddressDeny` (cgroup
  BPF at connect time, so redirects and DNS rebinding are covered) blocks loopback, link-local,
  multicast, `0/8`, `10/8`, `100.64/10`, `172.16/12`, `192.168/16`, `fc00::/7`. No DNS exception.
- *Send out*: at most the prompt content above, via queries or fetched URLs; fetched sites see the
  van's public IP. Accepted.
- *Persist*: injected text can shape the report and later answers; the UI labels every summary and
  report as AI research.
- *Actuate*: no AF_CAN, no devices, no broker socket, LAN listeners unreachable, WebFetch is GET only.

**Request forgery.** Unchanged relay gates: trusted `Host` (defeats DNS rebinding to the
listener), exact same-origin `Origin` on POST, `Sec-Fetch-Site` absent or `same-origin`, JSON
only, and custom headers that force a CORS preflight the listener never grants. The access code
crosses the LAN in clear HTTP (as for the warning chat; Tailscale encrypts).

**Cost and runaway.** One research CLI at a time (the worker's slot) and one warning-chat CLI at a
time (the advisor's slot); 10 research starts per 60 s; 60 research
launches per rolling 24 h in a durable ledger; 600 / 300 s deadlines; the Claude budget caps
(if the live check shows them enforced); 16 MiB output; 24 turns; per-text size caps; idempotent
request ids; no automatic retries beyond one counted replay.

**Model output on the dashboard.** JSON strings with control characters other than newline and
tab removed; the UI renders the Markdown subset to Preact nodes, links `http`/`https` only, never
`innerHTML`, no images; the summary is plain text.

**Credentials at rest.** `V/claude-oauth-token` and `V/codex-home/auth.json`, mode 0600 inside
the 0700 volume root. Not backed up (the image is excluded): after a restore the owner signs in
again, deliberately, since the image is churn and re-creatable.

## 6. Storage

### Advisor side, `/var/lib/van-telemetry-advisor/dtc-research/` (SD root)

`runs/<run_id>.json` per run and `admissions.json`.

Record (`schema_version: 1`): `run_id`, `module_key`, `fca_display`, `provider`, `model`,
`effort`, `created_at`, `updated_at`, `request_ids` (at most 48), `context` (at most 16 KiB),
`session` (`id`, `state: present|absent`), `turns` (`{kind, question, answer, summary,
summary_note, format_warnings, state, error, reason, mode, attempt_ids, started_at, completed_at,
duration_s, activity, diagnostics}`). Written atomically before any reply that depends on them,
and a turn's `attempt_id` before its launch request. Files over 420 KiB or symlinks are skipped
at load with a log line.

Bounds: at most 60 runs (run 61 evicts the oldest finished run by `created_at`, only once run 61's
launch has been admitted, so a refused start never costs a report; a busy or reconciling run is
never evicted); a record at most 416 KiB; all records at most 25 MiB.
Eviction deletes the record and calls the worker's `POST /runs/<run_id>/delete`; a later sweep
catches misses.

Ledger (finding 2): `{"version": 1, "launches": [{"at", "run_id", "attempt_id", "kind":
"initial|resume|replay"}]}`. Every entry counts; the file holds at most 60 entries, pruned to the
last 24 h on every write. Measured at its largest (60 entries, the longest timestamps): 9,087
bytes, so the stated bound is 12 KiB (`tests/test_dtc_research_pure.py` checks it). Only the
advisor writes it, under its research lock.
Before each launch request the advisor prunes, refuses with `daily_limit` when 60 entries remain,
appends the new entry, and writes atomically with fsync; only then does it call the worker. If the
worker answers 409 (a definitive refusal: under the send-once rule that attempt is never sent
again), the advisor removes that entry at once with another atomic write, and logs the refusal to
the journal (no diagnostic collection is kept in the file). Every other outcome counts: failed,
cancelled, replay, and a request whose answer was lost. So the file never holds more than 60
entries, and no counted entry is ever dropped to make room. A failed write refuses the launch
(`storage_error`); if removing a refused entry fails, the entry stays and counts (conservative).
Discovery and maintenance are not owner research launches and are not in the ledger; they still
take the worker's slot. Eviction and restarts do not lower the count. An unreadable ledger is never overwritten: research launches are refused until it
is readable or removed by hand (documented).

### Worker side, the research volume `V = /var/lib/vanresearch`

`/var/lib/vanresearch.img`, 224 MiB, preallocated (`fallocate`), ext4 with no reserved blocks,
loop-mounted `nodev,nosuid,noexec,noatime` by `var-lib-vanresearch.mount`: its SD footprint is
exactly the file's size and never grows.

```
V/claude-oauth-token
V/codex-home/           private CODEX_HOME (auth.json, models_cache.json, sessions/, *.sqlite, log/)
V/catalog/claude-home/  discovery's Claude home, deleted after each discovery
V/runs/<run_id>/work/   empty cwd
V/runs/<run_id>/home/   HOME; .claude/ holds that run's Claude session and config
V/runs/<run_id>/session.json   {provider, session_id, rollout_path}, written by the worker
V/tmp/<attempt_id>/     TMPDIR of the running attempt
```

Enforcement:

- During every job a watchdog runs every 2 s: free space below 16 MiB, or the run's footprint
  (its `R/` plus its Codex rollout) over 16 MiB, kills the job (`space_exhausted`), deletes that
  run's session files (later follow-ups replay) and empties its `TMPDIR`. Overshoot is at most what
  a CLI writes in 2 s; the volume is the hard stop, and an I/O error from a full volume is handled
  the same way.
- After every job and at worker start: session files are dropped, oldest last-use first, until
  they total at most 128 MiB (records stay; those runs replay); over 48 MiB of Codex-home
  non-session files, the worker deletes `V/codex-home/logs_2.sqlite*` (diagnostics, recreated by
  Codex; no Codex process runs then); still over 64 MiB, Codex is unavailable (storage).
- At worker start `V/tmp/` and `V/catalog/` are emptied; on sweep, run directories and Codex
  rollouts not kept are deleted. Deletion is deterministic: `rm -r R/home` for Claude; the rollout
  path recorded in `session.json` for Codex. Codex's leftover index rows stay inside the volume.

Operating target about 150 MiB used on the volume; true worst case 224 MiB.

### SD root totals and the Mac hand-off

| Path | Worst case | Kind |
| --- | --- | --- |
| `/var/lib/vanresearch.img` | 224 MiB | fixed file (everything research CLIs write) |
| `/var/lib/van-telemetry-advisor/dtc-research/` | 25 MiB + 8 KiB | code bounds, advisor-written |
| `/opt/van-research-cli` | 1,000 MiB | CLI copies: Claude 348 MiB and Codex 271 MiB when installed, plus one staged release (at most about 380 MiB); written only by the install command |
| worker `/tmp`, `/run/van-telemetry-research` | 0 on SD | tmpfs |

Research data written at runtime: at most 249 MiB, under the 256 MiB ceiling. CLI copies are a
separate, deliberately installed cost: 348 MiB this round (Claude only, since the Codex install
fails verification and leaves nothing behind), at most 1,000 MiB.

Entries for the parent's Mac hand-off (`ROOTFS_PATH_BUDGETS_MIB` and the backup exclusions are
MacBook-managed; no existing entry covers any of these paths):

- Budgets: `/var/lib/vanresearch.img:232`, `/var/lib/van-telemetry-advisor:160` (25 MiB of
  research records plus the existing warning chat: 200 chats it loads only under 512 KiB each,
  about 100 MiB, and its Codex runtime directory), `/opt/van-research-cli:1024`.
- Backup exclusions: `/var/lib/vanresearch.img`, `/var/lib/vanresearch` (churn; holds the private
  credentials, deliberately not backed up), `/opt/van-research-cli` (re-creatable from the owner's
  installed CLIs by the install command). Research records under the advisor state stay backed up.

## 7. Units, deploy and rollback

No custom slice: both units stay in `system.slice`, where systemd puts them by default. Any
cgroup path is taken from `systemctl show -p ControlGroup --value <unit>`.

`systemd/van-assistant.tmpfiles.conf` (installed as `/etc/tmpfiles.d/van-assistant.conf`), the
shared CLI lock's directory and file, recreated at every boot:

```
d /run/van-assistant 0750 pi pi -
f /run/van-assistant/model-cli.lock 0640 pi pi -
```

`systemd/var-lib-vanresearch.mount`:

```
[Unit]
Description=Fixed-size volume for DTC research CLI state (hard SD bound)
[Mount]
What=/var/lib/vanresearch.img
Where=/var/lib/vanresearch
Type=ext4
Options=loop,nodev,nosuid,noexec,noatime
[Install]
WantedBy=multi-user.target
```

`systemd/van-telemetry-research.service`:

```
[Unit]
Description=Web research runner for diagnostic codes (Claude and Codex CLIs, no LAN)
Requires=var-lib-vanresearch.mount
After=var-lib-vanresearch.mount network-online.target
Wants=network-online.target

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=30
KillMode=control-group
User=pi
Group=pi
WorkingDirectory=/home/pi/dev/obd-things
ExecStart=/usr/bin/python3 -m projects.vehicle_data.dtc_research_worker
Restart=on-failure
RestartSec=5
RuntimeDirectory=van-telemetry-research
RuntimeDirectoryMode=0700
UMask=0077
NoNewPrivileges=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=tmpfs
BindReadOnlyPaths=/home/pi/dev/obd-things
BindReadOnlyPaths=/home/pi/dev/obd-things/projects/vehicle_data/systemd/research-resolv.conf:/etc/resolv.conf
InaccessiblePaths=-/home/pi/dev/obd-things/tmp -/home/pi/dev/obd-things/.git -/home/pi/dev/obd-things/AGENTS.secret.md
InaccessiblePaths=-/run/van-telemetry -/run/van-telemetry-advisor -/run/dbus -/run/user -/run/tailscale -/var/lib/van-telemetry -/var/lib/van-telemetry-advisor
ReadWritePaths=/var/lib/vanresearch
TemporaryFileSystem=/tmp:size=32M,mode=1777
IPAddressDeny=localhost link-local multicast 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 172.16.0.0/12 192.168.0.0/16 fc00::/7
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
Nice=15
IOSchedulingClass=idle
CPUQuota=30%
# Kernel-enforced backstop for the whole cgroup; the worker's own cap is tighter. See docs/dtc-research.md.
MemoryMax=768M
OOMScoreAdjust=800
TasksMax=96
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
```

`research-resolv.conf`: `nameserver 1.1.1.1`, `nameserver 9.9.9.9`, `options timeout:2 attempts:2`.
`/opt` is read-only under `ProtectSystem=strict` and is not hidden; the CLI copies are executed
from there. `AF_UNIX` stays allowed (the worker's socket and systemd notify); no model tool
opens Unix sockets, and the sensitive ones are hidden. Denying loopback assumes neither CLI uses
TCP to itself; if the live check shows otherwise, the fix is a reviewed change, never
`IPAddressAllow=localhost`.

The advisor unit (`van-telemetry-advisor.service`) gains exactly one line and nothing else (it
already has the default `KillMode=control-group`, and its read-only view of `/run` is enough to
take the shared CLI lock):

```
InaccessiblePaths=-/var/lib/vanresearch
```

It tightens: the research credentials are hidden from the warning chat's Codex. The warning
chat's sandbox, slice, network, CPU and memory settings are otherwise unchanged.

`systemd/install_dtc_research.sh` (sudo, idempotent, prints each step): creates the image with
`fallocate -l 224M` and `mkfs.ext4 -q -m 0 -L vanresearch` only if absent; installs the tmpfiles
entry and runs `systemd-tmpfiles --create /etc/tmpfiles.d/van-assistant.conf`; installs the mount
and worker units and the advisor unit; `daemon-reload`; `enable --now` the mount; `chown pi:pi` and
`chmod 700` the volume root; creates `/opt/van-research-cli` root-owned 0755; `enable --now` the
worker; restarts the advisor; prints status.

### Deploy runbook (exact; the parent runs it after the implementation review)

`R=/home/pi/dev/obd-things/projects/vehicle_data/systemd` below. Nothing here has a CAN role and
the broker is untouched. Stop at the first failed check and roll back (end of this section).

0. **Preconditions.** The branch is merged into master in `/home/pi/dev/obd-things`, the whole
   suite is green on the merged tree, the implementation review says `ship`. The Mac hand-off is
   done and on the Pi: rootfs budgets `/var/lib/vanresearch.img:232`,
   `/var/lib/van-telemetry-advisor:160`, `/opt/van-research-cli:1024`; backup exclusions
   `/var/lib/vanresearch.img`, `/var/lib/vanresearch`, `/opt/van-research-cli`
   (check: `grep -n vanresearch /home/pi/scripts/backup/backup_conf.sh` shows them). `uptime` calm.
1. **Dry run.** `sh $R/install_dtc_research.sh --dry-run`. Expect the 16 `+ …` lines in this order:
   `fallocate -l 224M /var/lib/vanresearch.img`, `chmod 600`, `mkfs.ext4 -q -m 0 -L vanresearch`,
   tmpfiles install and `systemd-tmpfiles --create`, the three unit installs, `daemon-reload`,
   `enable --now var-lib-vanresearch.mount`, `chown pi:pi` and `chmod 700 /var/lib/vanresearch`,
   `install -d … /opt/van-research-cli`, `enable --now van-telemetry-research.service`,
   `restart van-telemetry-advisor.service`, `status`. Stop if any path differs.
2. **Install.** `sudo $R/install_dtc_research.sh`. Expect the same lines and a final status with
   the mount `active (mounted)`, the worker `active (running)`, the advisor `active (running)`.
   Checks:
   - `findmnt /var/lib/vanresearch` → a `/dev/loopN` source, `ext4`, options including
     `nosuid,nodev,noexec`. `stat -c '%U %a' /var/lib/vanresearch` → `pi 700`.
   - `ls -l /run/van-assistant/model-cli.lock` → `-rw-r----- pi pi`.
   - `systemctl show -p ActiveState,NRestarts,ControlGroup,OOMScoreAdjust van-telemetry-research.service`
     → `active`, `0`, the ControlGroup systemd reports (expected `/system.slice/van-telemetry-research.service`;
     use the reported value below, never a hand-built path), `800`.
   - `sudo bpftool cgroup show /sys/fs/cgroup$(systemctl show -p ControlGroup --value van-telemetry-research.service)`
     → lists `cgroup_inet_ingress` and `cgroup_inet_egress` programs (the IP filter is attached).
   - Worker status, bounded (as pi; `W=/run/van-telemetry-research/worker.sock`):
     `curl -sS -m 5 --unix-socket $W http://worker/status | jq '{checks_current, checked_at, providers, memory_policy, dns_ok, dns, job}'`
     → answers within the 5 s (no answer, or curl's "Operation timed out", is a stop). Right after a
     start the providers may read `checking` with `checks_current` false: ask again until
     `checks_current` is true. Then expect `providers.claude.identity`: on a first install, before the
     CLI step, `missing` ("no claude copy is installed for research"); on a retry after the
     2026-11-17 attempt, the verified 2.1.325 copy under `/opt/van-research-cli` is still on disk
     (the rollback kept it, with the 224 MiB volume image, `identity.json` and the unit files; the
     two units are only disabled and inactive), so expect `checking` until the first background
     hash finishes (about 3 s idle, much longer under CPUQuota), then `ok` with `signed_in` false.
     Either way `providers.codex.identity` `disabled`, `memory_policy.activated`
     false, `dns_ok` true with `dns.result` `ok`. `dns_ok` false: read `dns.reason` and
     `dns.checked_at`, ask again after a minute; `dns.result` `pending` only means the first lookup
     has not finished. A lasting failure with a resolver error means public DNS is blocked on this
     network: research stays unavailable, which is expected behaviour, not a deploy failure, but
     note it.
   - `journalctl -u van-telemetry-research.service -u van-telemetry-advisor.service -b --since -5min --no-pager`
     → no traceback. An `assistant_http_client_gone` line means a caller left before its reply;
     it is not a failure when a timed-out check of yours explains it.
   Stop: the mount or the worker not active, the IP filter not attached, or a status call that did
   not answer within its 5 s.
3. **Web listeners.** `sudo systemctl restart van-telemetry-web.service van-telemetry-web-tailscale.service`.
   Check: `curl -s -o /dev/null -w '%{http_code}\n' -H 'Origin: http://192.168.6.103:8765' http://192.168.6.103:8765/v1/assistant/models`
   → `401` (route relayed, key missing). `404` means the old web code is running: stop.
4. **Warning-chat regression.** Ask one warning-chat question on the dashboard; it is answered.
   The advisor journal has no lock error. Stop otherwise (the advisor's one-line unit change and
   the lock in its slot are the suspects).
5. **Claude copy.** `sudo $R/install_research_cli.sh claude --dry-run` (prints the source
   `/home/pi/.local/share/claude/versions/<version>` and the plan), then
   `sudo $R/install_research_cli.sh claude`. Expect `maintenance reservation … open`, then
   `promoted /opt/van-research-cli/claude/<version>; restarting van-telemetry-research.service`.
   Checks: `sudo python3 -c "import json;d=json.load(open('/opt/van-research-cli/identity.json'));e=d['providers']['claude'];print(e['version'],e['verification']['result'],e['verification']['digest']==e['digest'])"`
   → `<version> passed True`; worker status (the bounded command in step 2) → the restarted worker
   reads `checking` until it has hashed the new 348 MiB copy (record how long: `checked_at` minus
   the restart time); poll it, a request of at most 5 s then a 5 s pause, 24 rounds (at most
   4 minutes). The loop stops only on a status body that is exactly one JSON object with
   `checks_current` true and Claude not `checking`; a curl failure or an empty body counts as not
   ready (jq 1.6's `-e` exits 0 on empty input, so it is not trusted alone). It prints `ready=yes`
   or `ready=no`; `ready=no` is a stop:
   `ready=no; for i in $(seq 24); do b=$(curl -sS -m 5 --unix-socket $W http://worker/status) && printf %s "$b" | jq -es 'length==1 and (.[0].checks_current==true and .[0].providers.claude.identity!="checking")' >/dev/null && { ready=yes; break; }; sleep 5; done; echo ready=$ready`,
   then the bounded command → `providers.claude.identity` `ok`, `signed_in` false, `dns_ok` true.
   Each call must answer within its 5 s whatever else the Pi is doing.
   Stop: `stopped: …` printed (the reason says why; nothing was promoted).
6. **Owner sign-in** (the owner, as pi, each command under 80 characters):
   `claude setup-token`, then `s dtc_research_signin claude` (paste the token; it is not echoed).
   Check: `s dtc_research_signin status` → `Claude: signed in`; worker status (bounded command,
   `checks_current` true) → `providers.claude.signed_in` true. The dashboard still shows research as not activated.
7. **Memory measurement.** In one shell: `cd /home/pi/dev/obd-things && python3 -m projects.vehicle_data.measure_assistant_peak`;
   ask one warning-chat question; it prints `peak_tree_rss_mib`, `peak_tree_hwm_mib`,
   `lowest_available_mib` (exit 1 means no Codex child was seen: ask again). Then
   `sudo $R/install_research_cli.sh claude --measure --warning-chat-peak-mib <peak_tree_hwm_mib>`
   (two real Claude turns, about 5–15 minutes). Expect `"result": "measured"`, both turns
   `complete`, and a printed `memory` block (also saved as `/opt/van-research-cli/measurement.json`).
   Stop: `incomplete` (read the turn reasons; do not activate).
8. **Activation.** Paste the printed `memory` block into `research_cli_policy.json` in a reviewed
   commit (its `cli_digest` is the installed copy's digest), merge, then
   `sudo systemctl restart van-telemetry-research.service`. Checks: worker status (the step 5 poll,
   then the bounded command) → `memory_policy.activated` true; `GET /v1/assistant/models` (dashboard or curl with the access
   code) → the Claude group `available: true`, `source: discovered` within about a minute.
9. **Budget check.** `sudo $R/install_research_cli.sh claude --budget-check` (one real Opus turn
   capped at $0.01). Record `result`: `enforced`, `not_enforced` (tell the owner the caps are not
   enforced; the turn deadlines are then the only per-turn spend bound) or `inconclusive`, with
   `total_cost_usd`.
10. **Live acceptance check** (section 8).

Rollback (any step): `sudo systemctl disable --now van-telemetry-research.service var-lib-vanresearch.mount`;
`git -C /home/pi/dev/obd-things show <pre-merge>:projects/vehicle_data/systemd/van-telemetry-advisor.service | sudo tee /etc/systemd/system/van-telemetry-advisor.service >/dev/null`;
`sudo systemctl daemon-reload`; check out the pre-merge code in the main checkout; `sudo systemctl
restart van-telemetry-advisor.service van-telemetry-web.service van-telemetry-web-tailscale.service`.
The warning chat is then exactly as before; research routes 404 and the dashboard hides them. To
remove data and credentials: revoke the Claude token in the claude.ai account settings, then
`sudo rm /var/lib/vanresearch.img /etc/tmpfiles.d/van-assistant.conf`, `sudo rm -r /opt/van-research-cli`,
`rm -r /var/lib/van-telemetry-advisor/dtc-research`.


## 8. Tests and the live check

Offline (`tests/test_dtc_research*.py`, whole suite, no network, no real model):

- **Fake CLIs** (kept for process handling): Python scripts written to a temp directory and
  registered in a temp identity record with their real hashes and file-set digest; they record
  argv, cwd and environment and play a scenario from a file beside them. Scenarios: success with
  searches and fetches; init with an extra tool; a forbidden `tool_use`; a Codex
  `command_execution` item; a Codex `additional_tools` record with `exec`; a missing, malformed
  and incomplete `additional_tools` record (each fails, none is read as empty); a Codex `error`
  notice item (not fatal); non-JSON stdout; a 5 MiB line; no `turn.completed`; non-zero exit;
  resume with another session id; resume whose session file does not grow; a hang; output past the
  cap; a grandchild that must be gone after cancel (by PID); writing past the per-run footprint;
  a child whose RSS passes the tree cap and that ignores SIGTERM (it must be gone within one
  watchdog tick, with no grace period); an `initialize` response.
- **Identity**: a changed fake (new hash), an extra or missing file in the release tree (digest
  mismatch), or a failed verification make the provider unavailable with the right reason and
  nothing launches; the install command's staging, promotion and failure paths against a temp
  `C`; Codex with the 0.167-shaped tool record fails verification and stays unavailable; Codex
  verification covers every offered model and records only the passing ones.
- **Environment**: `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and proxy variables
  set in the test process never reach a child; the token comes from the file, never argv; bad
  token files make Claude unavailable.
- **Sessions**: the session id and `attempt_id` are in the record while the fake still runs; a
  missing session goes straight to replay; replay has `mode: "replayed"`, a new id and attempt, a
  `replay` ledger entry, and the old files are gone; the verified-resume rules.
- **Sizes**: the largest encodable state (60 runs for one code, every text at its cap in non-BMP
  characters, runs at the 384 KiB limit) fetched through the real relay (`web.py` handler on a
  loopback port, the advisor `ChatHandler` on a Unix socket, `TelemetryClient` between): 200 and
  each body within its computed bound; `record_full` when the next follow-up cannot fit; the
  worker's `/turns/<attempt>` reply with a maximal outcome stays under 112 KiB through
  `TelemetryClient`.
- **Admission, CLI lock and reconciliation** (real worker server on a temp socket with fake CLIs;
  the CLI lock on a temp file; fakes record start and end times and each test asserts no overlap):
  - simultaneous research start and install maintenance, released together from a barrier:
    exactly one gets 202, the other 409 `busy`; one fake CLI runs;
  - CLI lock, both race directions: a warning-chat turn and a research turn started together from
    a barrier, once with each side scheduled first: exactly one spawns, the other answers its
    `busy` sentence (the warning chat's 429 naming research, or the worker's `warning_chat`);
  - CLI lock holder killed mid-run: a holder process killed with SIGKILL releases the lock (the
    kernel drops it) and the other side then acquires it; with a process-group kill standing in
    for the unit's control-group kill, the holder's fake child is gone too;
  - connection loss after the worker accepted an attempt but before the child exists (the spawn
    held on a barrier, the advisor's connection dropped): the slot stays held; the advisor only
    asks about the attempt and never re-sends it; the attempt either completes and is adopted or
    is cancelled while `accepted` and never spawns; nothing second launches;
  - send-once: the advisor never sends a launch for an `attempt_id` twice in any scenario (a test
    double of the worker records every launch request); a deliberately repeated request is
    answered `duplicate_attempt` and launches nothing;
  - outcome dropped: 17 completed unacknowledged attempts drop the oldest outcome; asking about it
    returns `known: true, outcome_state: dropped`, and the advisor marks that turn `interrupted`;
    more than 16 completed attempts never cause a relaunch;
  - tombstone bound: with the table full and the oldest entries acknowledged, the oldest is
    evicted; full of unacknowledged entries, new launches are refused with `busy`;
  - worker restart between acceptance and the advisor's next question: the new instance answers
    `known: false` with a new `instance_id`, and the turn becomes `interrupted`; a launch carrying
    the old `expected_instance_id` is refused with `stale_instance`;
  - advisor restart during a report: state `unknown`, the turn adopted by `attempt_id`, its outcome
    saved and acknowledged, no second launch;
  - advisor restart during discovery: the discovery cancelled, research `busy` until the worker
    reports `job: null`, then `idle`;
  - an unresponsive worker (server paused) whose fake child is alive: the advisor stays
    `unknown`/`holding` across many HTTP timeouts and launches nothing; after the worker comes back
    with a new `instance_id`, the turn becomes `interrupted`;
  - maintenance across promotion and restart: while a reservation is held (through verification,
    a simulated promotion and until the worker process exits), a research start gets `busy`; an
    abandoned reservation ends after its timeout;
  - memory: `low_memory` refusal with an injected `/proc/meminfo`; critical `MemAvailable` during a
    run kills the process group at once; `not_activated` refuses turns while `measured` is false,
    while discovery and maintenance still run.
- **Ledger**: 59 counted launches, advisor restart, one more allowed, the 61st refused; eviction
  does not lower the count; failed, cancelled and replay launches count; 59 counted launches mixed
  with 500 worker refusals (409) leave a file of exactly 59 entries, all still counted, and the
  60th launch is admitted; an unwritable ledger refuses; old entries are pruned.
- **Store and volume**: restart recovery; the 60-run cap with a busy run; eviction calls the worker
  delete; sweep; session-total and Codex-home cleanup order; `TMPDIR` emptied on every exit path.
- **Prompt and context**: summary valid, missing, too long, wrapped in bold, multiline; heading
  check; encoded truncation; VIN masking of every prompt part; the facts file's schema, valid
  module keys and code format, and no VIN-shaped string; no browser-supplied meaning text.
- **Routes**: auth; exact bodies; start → progress → done; idempotency; every error reason; cancel;
  a code with no runs; Codex start refused with `provider_unavailable`.
- **Relay**: `web.py` forwards exactly the new routes and still refuses cross-origin, non-JSON and
  unknown paths.
- **Units (structural contract test)**: the worker unit's `ProtectHome=tmpfs`, `IPAddressDeny`
  ranges, address families without `AF_CAN`/`AF_PACKET`/`AF_NETLINK`, the inaccessible sockets and
  state, `Requires=` the mount, `WatchdogSec=`, `KillMode=control-group`, `OOMScoreAdjust=`, no
  `Slice=`, and that its resolv.conf source exists; the advisor unit differs from master by
  exactly the one line; the tmpfiles entry names the lock path `model_cli_lock.py` uses.
- `tests/test_warning_chat.py` passes unchanged.

Live acceptance check (the parent, after deploy and the Claude sign-in; spends real usage):

1. Claude `sonnet` / `medium` on `radar_acc` / `C1418-78`: poll to `done`; summary at most 140
   characters, the four headings, at least one http(s) source; its session file is under that
   run's `R/home`. Ask a follow-up: `mode: "resumed"`.
2. Restart `van-telemetry-research.service`, then `van-telemetry-advisor.service`; ask another
   follow-up: `resumed`. Then start a new report, restart only the advisor mid-turn, and confirm
   the turn is adopted and completes.
3. Exclusion against the installed units: while a report runs, a warning-chat question gets the
   warning chat's 429 naming research, and `sudo <checkout>/projects/vehicle_data/systemd/
   install_research_cli.sh claude --budget-check` prints the busy message and launches nothing;
   while a warning-chat question is being answered, starting research gets 429 `busy` ("a warning
   chat is answering a question").
4. Budget cap, designed to discriminate, with research idle: the same `--budget-check` (a
   maintenance reservation in the worker's slot; it changes no identity and is not in the ledger)
   launches Claude `opus` with the production argv, `--max-budget-usd 0.01` and a fixed prompt of
   about 3,000 words asking for a 400-word answer, whose expected cost at Opus prices is well over
   $0.05. Outcomes: `error_max_budget_usd` → enforced; `success` with `total_cost_usd` over 0.01 →
   **not enforced** (the caps are documented as ineffective, the timeouts become the only per-turn
   spend bound, and the parent tells the owner); `success` at or under 0.01, or no cost reported →
   inconclusive (not counted as enforced; documented). The recorded cost is kept with the result.
5. Confirm the activated thresholds are the measured ones (`GET /status` `memory_policy`) and
   record the research tree's peak RSS from the live turns' `dtc_research_timing` lines; a live
   peak above the recorded `research_peak_mib` means re-measuring before the feature stays offered.
6. Record elapsed times and `df -h /var/lib/vanresearch`.

Codex has no live check this round; it is unavailable by design.

## 9. Follow-ups (not this round)

- **F1. Warning chat onto a private Codex home** (parent: later). It still uses the owner's
  `~/.codex` (his `AGENTS.md`, history and `auth.json` visible to its process).
- **F2. The warning chat's Codex tool restrictions.** Its feature list predates 0.167 and its
  stream check is a denylist of four item types. Evidence from this round (probe 5, with an even
  longer disable list than the warning chat's) shows 0.167 still offers `exec` (code mode, which
  reports it fails closed without its host), `wait`, `request_user_input(_async)` and the
  collaboration tools (`spawn_agent` and five others). Exposure: the warning chat's prompt has no
  web content (broker evidence and the owner's question only), web search is disabled, and its
  permission profile gives any command read access to platform-minimal paths and an empty
  workspace with network off. Unverified: whether `exec` truly fails closed, and what a spawned
  sub-agent inherits. The advisor unit itself has unrestricted outbound network, LAN included.
  Remedy: the same rollout-record verification and allowlist for the warning chat's configuration.
- **F3. Enabling Codex research.** A source-level review of the pinned Codex version's tool
  construction (how `exec`, `wait`, `request_user_input` and the collaboration namespace are
  registered, whether any configuration removes them, and whether `exec` without the code-mode
  host can execute anything), or a later Codex version whose production rollout record passes the
  allowlist. Then the owner's Codex sign-in and `install_research_cli.sh codex` enable it with no
  code change.
- **O1 (closed 2026-11-17). The memory controller.** The owner chose to enable it:
  `cgroup_enable=memory` ends `/boot/firmware/cmdline.txt` and took effect at the 15:10 Mountain
  reboot, verified from `/proc/cmdline`, dmesg and `cgroup.controllers`. Every unit's `MemoryMax`,
  research's and the advisor's 768M included, is now enforced. Workstream record and the one-week
  check: `~/.agent/handoffs/memory-controller.md` (outside this repo).
- Optional hardening not adopted now because of unknown CLI breakage: `RestrictNamespaces=`,
  `SystemCallFilter=@system-service` on the worker.

## Work remaining

The implementation is reviewed and merged. Build status above is the authoritative installation
record: the 05:02 retry installed everything up to activation and passed every check; the
research worker and mount are enabled and active, research is activated and the live acceptance
check passed (Build status). Next: fix the flaky manager test, record the larger live peak at the
next policy change, then F1-F3. The Mac rootfs
budget and exclusions are deployed. The dashboard is merged; its current live deployment belongs
to the dashboard-round session and must not be reverted by research rollback. Preserve
F1–F3. When the workstream closes, promote durable facts and the completed runbook into the
standing docs before deleting this note.
