# Hourly DTC read during drives (stage 1a)

At most once an hour during a qualified engine-running drive, the C-CAN active-drive helper
(`active_drive.py`) sends one single request frame `03 19 02 8F` to the radar (DASM, `radar_acc`)
and one to the BCM (`bcm_ccan`), and nothing else: never a flow control, a session change or a
retry after an attempted send. The broker flag `--enable-drive-dtc-reads` turns it on. It is **off
by default**, it is not in the tracked `van-telemetry.service` unit, and with it off no DTC frame,
watch socket or BCM socket exists.

Owner request (2026-11-15): "query error codes once per hour of driving if there is no scan
harvest", for modules "such as the PCM BCM DASM etc". Before it, nothing on the Pi had read a
module's fault codes since the parked 15-module batch of 2026-10-08 (03:12–03:13 MDT), and the
van's own F1 sweeps had been harvested twice (2026-08-30 and 2026-09-24); the van's client sweeps
only while the Pi's F1 polling is quiet.

This is T3 work (new transmission while driving, broker/helper contracts). Activation is a
separate, reviewed step (see "Activation and rollback").

## Decisions

- **Mechanism.** The Pi reads inside the existing C-CAN active-drive helper, within its exclusive
  ownership interval, with fresh engine-running evidence and a one-use transmit permit per send;
  physical 29-bit addressing, default session. Never `14`, `10`, `27`, `2E`, `2F`, `31` or `3E`.
- **Mask `19 02 8F`** (warning indicator requested, confirmed, pending, test failed this
  operation cycle, test failed). `FF` returns the "test not completed" bits that change through
  every drive (Oct 8: 278 of 300 records were status `0x40` only); `0D` is the harvester's
  signature for the van's own sweep. On the Oct 8 data `8F` keeps all 22 fault-relevant records.
- **Modules: the radar and the BCM only.** Cluster, RF Hub, TCM, shifter and telematics wait until
  these two have run clean on several drives; the PCM after that (it needs KWP, below); B-CAN and
  CAN-CH modules stay parked-only (`tools/dtc_batch.py`).
- **Cadence: elapsed time**, not accumulated driving time; engine running while parked counts,
  and trips shorter than five minutes never read. The alternative is described under "Open owner
  choices".
- **Storage.** The parked DTC history (`dtc-history.sqlite3`, the v2 `dtc-cache.json`) is
  unchanged; drive reads have their own bounded JSON file.

## What goes on the bus

Two fixed frames, built and sent only by `drive_dtc_transport.py`, each behind its own permit
purpose (literals in `transmit_permit.py`, pinned to `drive_dtc.json` by a test):

| module | frame (29-bit, DLC 8) | permit purpose |
| --- | --- | --- |
| `radar_acc` | `18DA2AF1#0319028F00000000` | `radar_acc.dtc_read` |
| `bcm_ccan` | `18DA40F1#0319028F00000000` | `bcm_ccan.dtc_read` |

The framing is the van tester's own while driving (`03 19 02 0D`, `00` padding); only the mask byte
differs. Each pass sends at most these two frames and receives about two to five, about 1–2 ms of
bus time at 500 kbit/s. The harvester (`van_scan_pi_requests.py`) recognizes both requests as Pi
traffic, so a Pi read is never mistaken for a van sweep.

## One exchange

On one clock, `backend.monotonic` (CLOCK_MONOTONIC):

1. `FreshAuthority.acquire` (`radar_drive_steps.py`): a fresh broadcast snapshot with running RPM,
   the active gates (identity, interface state, topology, inhibits) and one permit. Stale evidence
   gives `not_sent` / `evidence_stale`; a failed gate ends the helper session as for every step.
2. The admission checks (`DriveDtcSteps._admission_problem`), in this order: drain the
   foreign-tester watch (the only step that takes time); no service-alignment run attempted and no
   command pending (memory and a zero-timeout `select`); a clean watch; then one clock reading for
   module spacing, the module's own hour, the radar margin and, last, the send guard: the clock must
   be at least `SEND_GUARD_SECONDS` before the window end, which is the pass deadline.
3. Transport preparation: the radar borrows the open 0845 link and drains it (bounded, 32
   frames); the BCM gets a new `CAN_RAW` socket with an exact EFF filter for `18DAF140` (RTR and
   ERR rejected), drained, and closed after the exchange. A drain failure is `drain_failed`.
4. **The same admission checks again** (the transport's `admit()`), immediately before the permit
   is consumed, so neither preparation nor the drain inside the checks can carry a send past a
   check that has since failed. A refusal is `not_sent`, unattempted, with the permit unspent.
   `SEND_GUARD_SECONDS` (`drive_dtc_steps.py`) is the permit lifetime
   (`transmit_permit.PERMIT_TTL_SECONDS`, 250 ms) plus a declared 62.5 ms margin. A permit expires
   a lifetime after its evidence, which precedes the final check, so a request admitted at the
   last allowed instant is consumed before the window ends or not at all: no request can be on
   the wire after its window by more than the residual, which is the consume-and-send call itself
   (the in-memory marker and one `send` system call).
5. Permit consumption, then the in-memory attempted marker (`before_send`, no I/O), then the
   socket send. From the marker on, the module is never retried in that pass, even when the send
   raises ambiguously. An expired permit is `not_sent` / `permit_expired`, unattempted.
6. Receive until a final answer: the first answer within 1.0 s of the send; `7F 19 78`
   (response pending) extends the deadline to 2.5 s after the send, never further, and nothing is
   resent. SingleFrame `59 02 mask [code status]` → `complete` (zero or one code). FirstFrame
   `1L LL 59 02 mask c1 c2 c3` → `truncated`, with count = (length − 3) / 4, the availability mask
   and the first code without status; nothing is sent back. `7F 19 nn` → `negative`
   (`7E`/`7F` reported as `session_required`); anything else `malformed`; the deadline
   `no_response`.

Late replies: the BCM's arrive after its socket closed and reach no consumer. The radar's stay on
the poller's socket; `RadarAlignmentPoller.receive_reply` skips up to four identifiable late DTC
answers (a SingleFrame starting `59 02` or `7F 19`, a FirstFrame starting `59 02`) in the first
reply of a later 0845 read or service-alignment exchange, inside the original deadline, sending
nothing. A fifth falls through to the existing malformed handling. As margin, a radar read is
sent only when the next 0845 poll is at least 5 s away.

## Gates and cadence

All timing is declared in `drive_dtc.json` and enforced in `drive_dtc_steps.py`:

- **Session age.** No pass before the helper session is 300 s old.
- **Windows.** The broker's reservation fixes window k as `[t_k, t_k + 600]`,
  `t_k = origin + first_due + k·3600`. A pass starts only inside a window; a missed window is
  skipped, not carried over.
- **The hour between passes.** A pass starts only 3600 s or more after the previous actual pass
  start in the session. Its `started_monotonic` is recorded and `dtc_pass started` emitted before
  any authority or permit is acquired (output back-pressure cannot age a permit), and the pass
  consumes its hour even if nothing is then sent.
- **The hour per module.** The helper never requests a module within 3600 s of its own previous
  request to that module in the session, counted from the end of that exchange (after its frame
  left, so the send's own duration needs no bound). If that cannot happen by the window's last
  admissible instant, the module gets one `not_sent` / `module_hour` outcome for the pass. Across
  sessions the budget's session-end bound covers it (below).
- **One module per cycle**, at least 2 s after the previous try of either module, and the first
  module that is ready is read, so neither holds the other back.
- **An unusable radar.** When the helper has no 0845 poller, or 0845 polling stopped after a
  failure in this session, the radar is not read for the rest of the session: it gets one
  `not_sent` / `radar_unavailable` outcome per pass and the BCM proceeds. The 5 s radar margin
  applies only while a poll can actually occur.
- **No pass without a possible request.** A window in which neither module could be requested is
  not started, so its hour is not consumed.
- **Retries.** `evidence_stale`, `permit_expired`, `drain_failed` and `deferred` (ready before
  preparation, not after it) leave the module open: at most three tries per module per pass, 10 s
  apart. Only the first and the final outcome are emitted (a suppressed retry is emitted when the
  pass ends), so a repeated failure cannot rewrite the results file every cycle. `window_closed`,
  `foreign_tester` and `service_latch` end the pass.
- **Radar service alignment.** Once a service-alignment run has been attempted in the helper epoch
  (refused, failed, timed out or completed), or a command is pending on the pipe
  (`CommandReader.pending()`, non-consuming), the step stops for the epoch. A command arriving
  during a DTC exchange is read at the next intake and wins.
- **Foreign-tester watch** (`drive_dtc_watch.py`): one receive-only `CAN_RAW` socket opened with
  the step (if it cannot open, the step is disabled for the epoch), filtering 29-bit `18DA..Fx` /
  `18DB..Fx` and 11-bit `7DF`, `7E0`–`7E7`, with `SO_RXQ_OVFL`. Drained every cycle (at most 64
  frames, non-blocking) and before every send. A frame is the helper's own only if it carries
  `MSG_DONTROUTE` and equals a frame the helper can send. Anything else, a receive error,
  truncation, an overflow-counter change or a full drain makes the watch dirty for 120 s, and a
  dirty watch means `not_sent` / `foreign_tester`. Limits: `MSG_DONTROUTE` marks frames from this
  host, not this process, so a manual local `cansend` of exactly a helper frame is
  indistinguishable; a tester can still start after the final check.

Timing cost: a read adds nominally about 2.9 s to its cycle (fresh snapshot ≤ 0.35 s, the 2.5 s
deadline, a non-blocking drain) plus the active-gate cost, which is not a hard bound. Everything
the next cycle publishes is delayed by at most about that, twice per pass; command intake stays
far inside the broker's 15 s acknowledgement and the supervisor's 10 s silence limit
(`test_nominal_deadline_plus_scripted_gate_cost_keeps_command_and_silence_below_limits`).

## Capability handshake

- The broker adds `--enable-drive-dtc --drive-dtc-reservation <id> --drive-dtc-origin <monotonic>
  --drive-dtc-first-due <s>` to the helper's command line only when it runs with the flag, no
  latch is set and it holds a completed reservation (`drive_dtc_control.py`).
- The helper refuses DTC arguments without `--startup-grant-fd` before any parent handshake,
  backend, socket or interface effect; the legacy ungated start is valid only with DTC off.
- A helper launched with the arguments adds `"capabilities": ["drive_dtc_v1"]` to its `ready`
  event, and only then (an old broker accepts only the legacy keys). A broker that asked and gets
  a `ready` without the capability cancels before the grant (final `restored: null`, no inhibit)
  and launches without the capability for the rest of its lifetime.
- Old broker with new helper: no arguments, the step is off. New broker with the flag and an old
  helper: the helper rejects the unknown argument before `ready`; the rollback order prevents it.

## Cooldown budget (broker-owned)

`drive-dtc-budget.json` beside the configured DTC cache (normally `tmp/vehicle_data/`), at most
1 KiB: `reservation_id`, `boot_id`, `origin_monotonic`, `first_due`, `interval`, `window`,
`session_end_monotonic`, `last_pass_bound_monotonic`.

- **Computation before each launch.** A missing, unreadable or corrupt record, another boot, no
  pass bound, or a null session end is **uncertain**: `first_due = 3600` and the reservation
  records a synthetic pass bound at that moment, as if a pass had just happened. Otherwise
  `due_in = 3600 − (now − bound)`, clamped to 0–3600, and at least `3600 − age` of the last
  harvested van sweep (wall clock, consulted at launch only); `first_due = max(300, due_in)`.
- **Session end.** The broker records `bound = min(session_end, t_k + 600)` for the latest
  `t_k ≤ session_end`, the latest moment a pass could have started; it never uses helper events
  to shorten the cooldown. Every request of that session was sent before its window end (the send
  guard) and before the session ended, so the bound is after every request, and the next
  session's first pass, at least 3600 s after the bound, is at least 3600 s after any module's
  previous request (`tests/test_drive_dtc_cross_session.py`).
- **Recovery invariant.** Within one boot the earliest permitted pass time, the durable bound plus
  3600 s, never moves earlier: an update may raise or keep the bound, never lower or clear it, and
  short sessions cannot erase an uncertain-recovery wait (reserve at 100, end at 200, reserve at
  250: no pass before 3700). After a reboot the wait is a full hour.
- **Writer.** One `drive-dtc-budget` thread, started only with the flag, owns every budget read
  and write (temporary file, `fsync`, rename, directory `fsync`), holds no broker lock, and has a
  queue of two. The collector waits at most 2.0 s for a reservation; on timeout or error it
  launches without the capability. A late or stale session-end update is identity-checked and
  dropped; a dropped end leaves a null end, so the next launch waits a full hour.
- **What prevents what.** Within a session the helper's own rules (hour between pass starts, hour
  per module, window ends rechecked at the send) are the only prevention. The broker checks each
  `dtc_pass started` against the reservation (inside its window, 3600 s after the previous start,
  not later than receipt, pass index increasing): this **detects** a violation but cannot undo a
  frame already sent in the same cycle. A violation drops that session's results, stops the helper
  through the ordinary supervisor stop (restoration verified as for any stop) and disables the
  capability for the broker's lifetime. Across sessions in a boot the session-end bound prevents,
  and across reboots the full-hour wait.

## Helper events

Validated by `drive_dtc_contract.validate_event` before the helper emits them (an event that would
fail disables the step instead) and again by the broker. Every value is in `drive_dtc.json`.

```json
{"type": "dtc_step", "session": "<reservation_id>", "state": "enabled|disabled",
 "reason": "capability|sentinel_unavailable|service_run_attempted|contract_error",
 "detail": "...", "interface_mode": "armed_diagnostic"}
{"type": "dtc_pass", "session": "<reservation_id>", "pass": 0, "state": "started|finished",
 "reason": "first_attempt|all_modules_done|window_closed|service_latch|foreign_tester|session_ending",
 "started_monotonic": 12345.0, "detail": "...", "interface_mode": "armed_diagnostic"}
{"type": "dtc_read", "session": "<reservation_id>", "pass": 0, "module": "radar_acc|bcm_ccan",
 "request": "19 02 8F", "outcome": "complete|truncated|negative|no_response|malformed|not_sent",
 "reason": "<see drive_dtc.json outcomes>", "nrc": "31|null", "pending_count": 0,
 "frame_hex": "<at most 8 bytes>|null", "count": 6, "started_at": "<UTC ISO>",
 "completed_at": "<UTC ISO>", "rpm": 1650.0, "speed_mph": 61.2, "interface_mode": "armed_diagnostic"}
```

`session` is the reservation id and `pass` the window index, so passes from different launches are
never combined. `started_monotonic` appears only on `started`. `not_sent` reasons:
`evidence_stale`, `permit_expired`, `drain_failed`, `deferred`, `foreign_tester`, `service_latch`,
`window_closed`, `radar_unavailable`, `module_hour`. The broker hands the three types to
`DriveDtcControl.handle_event` before `validate_helper_status`; it validates and enqueues only and
never raises. `helper_events.py`, final events and `ACTIVE_DRIVE_FAILURE_REASONS` are unchanged.

## Storage

`drive-dtc-results.json` beside the DTC cache, written only by the broker's `drive-dtc-results`
thread (separate from the budget writer), from its in-memory state in arrival order.

- Content: `schema` 1, `coverage` ("failed, pending, confirmed or warning-lamp codes only (19 02
  8F)"), and the newest 200 passes keyed by `session` and `pass`, each with at most one read per
  module (the event plus its parsed frame; an unparsable frame is stored as `unparsed`, never a
  fault).
- Bounds: about 120 KiB typical; the writer never exceeds 256 KiB (oldest passes dropped first),
  so with its temporary copy at most 512 KiB. Before each write and at startup it removes only its
  own interrupted temporary files (`.drive-dtc-results.json.*.tmp`); if it cannot, that is a storage
  fault. The budget adds at most 2 KiB with its temporary copy. Both fall under the
  `/home/pi/dev/obd-things/tmp` root budget (5120 MiB in `ROOTFS_PATH_BUDGETS_MIB`).
- An unreadable, damaged or outdated results file (one the current vocabulary rejects) is moved
  to the fixed sibling `drive-dtc-results.invalid.json`, replacing any earlier one, so at most one
  set-aside copy exists (no larger than the bad file was), and recording starts empty. It never
  latches the feature off.
- A full queue (64) or a write failure latches `drive_dtc_storage_failed`: later helpers launch
  without the capability; the running helper keeps its budget-limited schedule and its results
  are dropped.
- `drive_reads.py` serves a validated, reparsed projection as the `drive_reads` block of the DTC
  response (omitted when no file exists). Any failure while reading it, including the
  `RecursionError` of deep nesting and the `OverflowError` of an enormous number, makes only that
  block unavailable; the parked and van sources are served unchanged. If the response is too
  large, `dtc_response_fit.py` sheds `drive_reads` first, then `in_vehicle_scan`: each is replaced
  by a small unavailable marker, and omitted entirely when even the marker does not fit, checking
  the encoded size after every change. The parked result is served whenever it fits on its own.

## Dashboard: three sources

`dashboard/src/views/dtcSources.helpers.js` merges, per module:

- **parked** (`dtc-cache.json`, request `FF`): per-module availability, last attempt and success,
  `absence_authoritative`, `result_state`; per record its status, own `last_seen_at` and
  observation state.
- **van** (`van-scan-cache.json`, request `0D`): each module's latest successful read only.
- **drive** (`drive-dtc-results.json`, request `8F`): every read of the newest 200 passes.

Rules:

1. Effective coverage of a filtered read is request mask AND the returned availability mask; a
   null or zero mask is zero coverage.
2. Failures never change codes; a drive failure newer than the drive's latest success is its own
   line ("drive read failed (no response)", or "drive read not sent (…)").
3. The parked records are the base, each labelled with its own date.
4. The van's latest success and the drive's latest `complete` success apply in time order: a
   listed code takes the read's status and time; an unlisted shown record older than the read whose
   status has a bit in the effective coverage becomes "not reported by the <source> read"; a record
   with no covered bit stays with its own date. New listed codes are added.
5. A `truncated` read newer than the latest `complete` changes no code: "N codes reported, list
   not retrieved; first <code>", with "count differs from the codes shown" when it does.
6. An empty filtered read says "no failed, pending, confirmed or warning-lamp codes" (van: "no
   failed, pending or confirmed codes") only when its effective coverage equals the request mask;
   with a null, zero or partial availability mask it says "zero codes reported" with its coverage.
   `result_state`, `no_dtcs` and `absence_authoritative` come from parked reads only.
7. The overall "No codes in the last parked read of every module" is labelled parked with its date
   and is withheld when a newer truncated drive read reported codes; the card then names the module
   and count. Beside newer filtered lines a module's parked status carries its date.
8. "Last read" is the newest successful read from any source; with none, the last attempt.
9. Every filtered result shows its source, coverage and Mountain date and time. Truncated parked
   groups keep their "+N more".

The dashboard's caveats page ("Three sources on one card") says the same for the owner.

## Differences with the flag off

The flag-off behaviour is unchanged except in two deliberate places:

- **Late-reply filter.** `receive_reply` runs with the flag off too: the first reply of an 0845
  read or service-alignment exchange skips up to four `59 02` / `7F 19` frames. Before, such a
  frame (for example the van's own `19 02 0D` sweep answered on `18DAF12A`) was malformed and
  ended 0845 polling for the epoch. Those bytes can never answer `22 0845` or a service request,
  so skipping them only keeps polling alive; deadlines and sends are unchanged.
- **DTC-typed events.** A flag-off broker never asks for them. If a helper emits one anyway, the
  broker writes one line to its journal per helper session and ignores the events; they never
  become a protocol error, a stop or an inhibit.

## Known unknowns for the first drives

- The first `19 02 8F` to either module: a `7F 19 12` or `7F 19 31` is stored as `negative`; the
  next step is a review, not a mask change in the field.
- Unanswered FirstFrames are an experiment. ISO 15765-2 has the sender wait N_Bs (typically 1 s)
  and abandon the message; the van's own tester left PCM FirstFrames unanswered more than a dozen
  times on 2026-09-24 and the PCM then answered normally. Whether the radar or the BCM logs
  anything is unknown. From Oct 8 statuses, expect the radar's reply to be a complete SingleFrame
  (one record, status `0x08`) and the BCM's a FirstFrame ("6 codes reported, list not retrieved":
  the owner's LED-lamp codes B1636, B1632, B162E, B162A, B104E, B104D).
- `MSG_DONTROUTE` through Python's `recvmsg` on this kernel is not yet observed; if absent, every
  check is dirty and nothing is sent.
- Actual scheduling latency of a read at speed, and `8F` reply sizes and pending behaviour.

## Activation and rollback

Activation needs the code review approved and a parked deploy of the merged code first.

1. Merge with the flag off; the harvester recognizes the requests from its next run.
2. `s restart_broker` (parked) to load the new broker, flag off; status hashes unchanged.
3. Activation: add `--enable-drive-dtc-reads` to `ExecStart` in the tracked
   `projects/vehicle_data/systemd/van-telemetry.service`, install it as the vehicle-data README
   "Serving and deployment" section says (copy, `daemon-reload`), check
   `systemctl show -p KillMode,TimeoutStopUSec van-telemetry.service`, then `s restart_broker`
   parked.
4. Off: remove the flag from the installed unit, `daemon-reload`, `s restart_broker`.
5. Rollback order: (1) flag off as in step 4; (2) revert the code; (3) `s restart_broker`. Never
   start an old broker while the unit still carries the flag: it cannot parse it. The budget and
   results files can stay.

First-drive acceptance:

- `ready` carries the capability; `dtc_step enabled`; about five minutes after launch (or later per
  the budget) `dtc_pass started`, two `dtc_read`, `dtc_pass finished`; final `restored: true`.
- The C-CAN recording around the pass holds one `18DA2AF1#0319028F00000000` and one
  `18DA40F1#0319028F00000000`, no frame from the Pi after either, no other new identifier or
  payload.
- The watch reports no foreign frame while only the Pi is on the bus; every metric's next sample
  is no later than its slot plus about 2.9 s; 0845 polling continues; no service start fails
  because of a DTC frame.
- The results file and the dashboard show the radar's `complete` and the BCM's `truncated` read
  with coverage, source and time; the budget record has `session_end_monotonic` set after the
  drive. A drive over 65 minutes has a second pass at least an hour after the first, and each
  module's second request is at least an hour after its first; after a stop shorter than an hour,
  no pass until the hour has elapsed.
- "Several clean drives" (before stage 1b or more modules): at least five passes on at least three
  drives meeting every check; the radar's reads show no unexplained new code; the BCM's count and
  first code stay consistent (they cannot show whether other BCM codes changed); and an
  owner-triggered parked `19 02 FF` batch afterwards, compared with Oct 8, shows whether either
  module logged anything after the unanswered FirstFrames.

## Not built yet

- **Stage 1b: flow control** for a segmented reply, with its own fresh authority and permit taken
  when the FirstFrame arrives (a permit lives 250 ms, so the request's permit can never cover it).
  Separate plan and review, after 1a has run clean on several drives.
- **The PCM.** It rejects UDS `19` (`7F 19 11`); its codes come from KWP2000 `18 00 FF 00`,
  answered `58 NN` then three bytes per code with KWP status semantics. Only `58 00` has been seen;
  a segmented reply needs the PCM's 8-byte zero-padded flow control (proven only for `1A 87`,
  2026-08-30). The PCM poller (`pcm_electrical.PcmElectricalPoller`) reads without a drain and its
  1 s reads end the session on a stray frame, so the design must keep every PCM answer off that
  socket or add a bounded drain (a change to a protected path). A parked, owner-authorized
  validation comes first.
- **Other modules.** Cluster, RF Hub, TCM, shifter and telematics after the radar and BCM run clean;
  B-CAN and CAN-CH modules stay parked-only.

## Open owner choices

- **Elapsed time or accumulated driving hours.** Accumulated hours would change the broker's
  computation only: keep `running_seconds_since_last_pass`, add each session's duration at its end,
  `due_in = 3600 − running_seconds_since_last_pass` (still at least 300 s into a session). Short
  trips would contribute; a lost end or a reboot resets the sum (conservative). The helper is
  unchanged.
- **When to build stage 1b and the PCM.**
- The Pi's continuous F1 polling suppresses the van's own health check; unchanged by this work.

## Code

`drive_dtc.json` and `drive_dtc_contract.py` (vocabulary, timing, schedule, parser, event
validator); `drive_dtc_transport.py` (the two frames); `drive_dtc_watch.py`;
`drive_dtc_steps.py` (the per-cycle step); `drive_dtc_launch.py` (the helper's entry: it loads
the contract only when DTC arguments are present, so a damaged `drive_dtc.json` cannot stop a
flag-off helper from starting, and with DTC arguments the helper starts without the capability);
`drive_dtc_budget.py` and `drive_dtc_control.py` (broker side); `drive_reads.py` (projection);
`dashboard/src/views/dtcSources.helpers.js`. Tests: `tests/test_drive_dtc_*.py`,
`tests/test_active_drive_dtc.py`, `tests/test_broker_drive_dtc.py`, `tests/test_drive_reads.py`,
`tests/test_radar_late_dtc.py`, `dashboard/test/dtcSources.test.mjs`.
`tests/test_drive_dtc_cadence.py` drives the production budget, control and step through
randomized sessions and asserts both hours.

## Review provenance

All on 2026-11-15, cross-family.

- Plan review 1: foreign-tester send-time gaps, service-alignment state, blocking reads and late
  replies, and a risky parked-history migration. The plan dropped the migration for separate
  storage and added the watch, the service latch and bounded exchanges.
- Plan review 2: each added mechanism opened a path to a protocol error or an inhibit (a
  helper-side fsync, a long command pause, an environment handshake). The design became one
  request frame per module, no flow control, a broker-owned budget and an explicit capability
  handshake.
- Plan review 3 (approved building): a strict hour between actual passes and a full hour after a
  reboot, one serialized budget writer with a 2 s bound, the three-source dashboard rule, and the
  late-reply filter.
- Code review 1 (rework): sends could pass a window that closed during transport preparation; a
  DTC launch without the startup grant bypassed the capability check; the codes card could claim
  no codes against newer evidence; a failed 0845 poll starved both modules; a bad drive file could
  hide the other sources. All fixed as described above.
- Code review 2 (approve with flag off): the same 0845 starvation; unthrottled not-sent retries;
  the hour applying to passes rather than each module; a bad results file disabling storage for
  good; the size fallback dropping the wrong block; the permit module depending on the DTC
  vocabulary file. All fixed as described above.
