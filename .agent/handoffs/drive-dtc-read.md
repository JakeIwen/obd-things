---
workstream: drive-dtc-read
status: plan approved to build (third review), activation not approved
tier: T3
last reviewed commit: 7c44321 (third review: approve to build with listed changes, written in here)
---

# Hourly DTC read during drives: plan

## Objective

Owner request (2026-11-15): "query error codes once per hour of driving if there is no scan
harvest", for modules "such as the PCM BCM DASM etc". The cluster shows latched ACC/radar
warnings, and nothing on the Pi has read a module's fault codes since the parked 15-module batch on
2026-10-08 (03:12–03:13 MDT). The van's own F1 sweeps were harvested only twice (2026-08-30
20:38 MDT and 2026-09-24 15:12 MDT) and are not expected while the Pi polls on every drive.

Tier T3: new CAN transmission while the van is driven and new broker/helper contracts; it cannot be
verified by running it in a development session. Build in the worktree branch
`worktree-agent-ace5b03e8cf37934c`. The third cross-family review approved **building** this plan;
**activation** (turning the broker flag on) is a separate step that needs the code review first.

Design rule: this is one small read an hour, so prefer designs in which a failure path does not
exist over designs that handle it. Nothing may block the helper's polling thread on file I/O, and no
expected condition may end in a helper protocol error or an inhibit.

## Settled decisions

Settled by the parent session on the owner's behalf (2026-11-15).

1. **Mechanism.** The Pi reads codes itself inside the C-CAN active-drive helper
   (`projects/vehicle_data/active_drive.py`), with fresh engine-running evidence and a one-use
   transmit permit per send; physical 29-bit addressing, default session. Never `14`, `10`, `27`,
   `2E`, `2F`, `31` or `3E`.
2. **Mask `19 02 8F`** (warningIndicatorRequested | confirmed | pending |
   testFailedThisOperationCycle | testFailed). `FF` returns the "test not completed" bits that change
   through every drive (Oct 8: 278 of 300 records were status `0x40` only); `0D` is the harvester's
   signature for the van's own sweep. On the Oct 8 data `8F` keeps all 22 fault-relevant records.
3. **Modules: the radar (`radar_acc`, the DASM) and the BCM (`bcm_ccan`) only.** Cluster, RF Hub,
   TCM, shifter and telematics wait until these two have run clean on several drives; the PCM is the
   phase after that; B-CAN and CAN-CH modules stay parked-only (the existing batch,
   `tools/dtc_batch.py`).
4. **Storage.** The parked history (`tmp/vehicle_data/dtc-history.sqlite3` tables `module_scans`,
   `dtc_state`, `dtc_transitions`, the v2 `dtc-cache.json`, the `request_hex` CHECK) does not change.
   Drive reads have their own bounded storage.
5. **Cadence: an elapsed-time cooldown.** A pass may start during a qualified engine-running
   interval once the helper session is 300 s old, when at least 3600 s have passed since the latest
   moment a previous pass could have started and since the last harvested van sweep completed; within
   a session the helper also keeps at least 3600 monotonic seconds between the starts of actual
   passes. After a reboot the wait is a full hour, unconditionally. This is elapsed time, not
   accumulated driving time; trips under five minutes never read ("Accumulated driving hours" below
   says what the other reading takes; the owner chooses). Engine running while parked counts.
6. **Opt-in.** Broker CLI flag `--enable-drive-dtc-reads`, off by default. Removing it disables
   every new send.

## Staging

- **Stage 1a (this plan): one request frame per module per pass, no flow control at all.**
- **Stage 1b (separate plan and review, after 1a has run clean on several drives):** a flow control
  for a segmented reply, with its own fresh authority and permit taken when the FirstFrame arrives
  (a permit lives 250 ms, `transmit_permit.PERMIT_TTL_SECONDS`, so the request's permit can never
  cover it after a pending response).

Expected stage-1a results, from the Oct 8 statuses: the radar had one record matching `8F` (status
`0x08`), so its reply should be a complete SingleFrame; the BCM had six (the owner's LED-lamp codes
B1636, B1632, B162E, B162A, B104E, B104D), so its reply should be a FirstFrame, recorded as "6 codes
reported, list not retrieved", with the first code.

**Leaving a FirstFrame unanswered is a staged experiment.** ISO 15765-2 has the sender wait N_Bs
(typically 1 s) for a flow control, then abandon the message. The van's own tester left PCM
FirstFrames unanswered more than a dozen times in one sweep on 2026-09-24, and the PCM then answered
normally and reported zero stored codes. The Pi's 0845 poller also sends no flow control for a
FirstFrame it rejects. That does not show how the radar or the BCM behave: transport layers can
report such a timeout as an error, and whether either module logs anything is unknown. The first
drives and a later complete read check for it (Deployment, step 6).

## What goes on the bus

Two fixed frames, both in `drive_dtc_transport.py`, each behind its own permit purpose:

| module | frame (29-bit, DLC 8) | permit purpose |
| --- | --- | --- |
| `radar_acc` (DASM) | `18DA2AF1#0319028F00000000` | `radar_acc.dtc_read` |
| `bcm_ccan` | `18DA40F1#0319028F00000000` | `bcm_ccan.dtc_read` |

- The framing is the van tester's own while driving (`03 19 02 0D`, DLC 8, `00` padding), seen in the
  archived 2026-09-24 capture; only the mask byte differs. `8F` has not been sent to these modules
  before.
- Bus occupancy: each pass sends two frames and receives about two to five (a SingleFrame or a
  FirstFrame, plus any response-pending frames). A 29-bit frame with 8 data bytes is about 130–160
  bits with stuffing, about 0.3 ms at 500 kbit/s, so a pass occupies roughly 1–2 ms of bus time.
  While one of these frames is on the bus, any other frame that becomes ready waits for it to
  finish. The helper already sends four to six requests a second.

## The exchange

Each module read is one synchronous, bounded exchange in one helper cycle, on one clock
(`backend.monotonic`, which is `time.monotonic`, CLOCK_MONOTONIC).

- Socket:
  - radar: the open `RadarAlignmentPoller` link (`drain`, `send_fixed`, `receive`, and the new
    `receive_reply` below), the pattern of `RadarServiceTransport`. The radar keeps one tester socket.
  - BCM: a raw `CAN_RAW` socket opened for the exchange (exact EFF filter `18DAF140`, RTR/ERR
    rejected) and closed after it. Nothing else in the helper listens to `18DAF140`.
- Steps, in order:
  1. `FreshAuthority.acquire((purpose,))` (`radar_drive_steps.py`): fresh snapshot, RPM evidence and
     the active gates; `None` → `not_sent` (`evidence_stale`);
  2. the foreign-tester and service checks (sections below);
  3. `drain` (radar: the poller's bounded 32-frame drain; a drain failure → `not_sent`,
     `drain_failed`);
  4. consume the permit; immediately after successful consumption, the transport calls its
     `before_send` hook to **mark the module attempted for this pass** (in memory only), then
     performs the fixed socket send. No event emission or other I/O occurs between permit
     consumption and sending. From the marker onward the module is never retried in this pass,
     even if the send raises ambiguously (an `OSError` after which the frame may or may not have
     gone out);
  5. receive until a final answer or the deadline:
     - first answer within 1.0 s of the send;
     - `7F 19 78` (response pending) extends the deadline to 2.5 s after the send, never beyond;
       the request is never resent;
     - SingleFrame `59 02 mask [code status]` (length 3 or 7) → `complete` (zero or one code);
     - FirstFrame `1L LL 59 02 mask c1 c2 c3` with (length − 3) % 4 == 0 → `truncated`, with
       count = (length − 3) / 4, the availability mask and the first code without its status;
       nothing is sent back;
     - `7F 19 nn` (other NRC) → `negative` (`7E`/`7F` reported as `session_required`; nothing changes
       the session);
     - any other frame → `malformed`; deadline → `no_response`.
- Exceptions: `OSError`, `ValueError` and timeouts become outcomes (as in the radar poller).
  `ExpiredTransmitPermitError` raised before the frame is sent becomes `not_sent`
  (`permit_expired`); the module is then not marked attempted and can retry inside the same pass
  window. This does not move the recorded pass start or refund its hourly budget. Any other `TransmitPermitError` stays
  fatal (`helper_failed`), as for every existing transport.
- Late frames:
  - BCM: after its socket closes, a late frame reaches no consumer.
  - Radar: a late answer stays queued on the poller's socket. It is removed by the next bounded
    `drain`, and if it arrives during a later radar exchange it is skipped there (next section). As
    extra margin, the radar read is sent only when the next 0845 poll is at least 5 s away
    (`RadarDriveSteps.next_alignment`).

## Late radar replies in later radar exchanges

`RadarAlignmentPoller` (`radar_alignment.py`) gains `receive_reply(deadline, max_stale=4)`: it calls
`receive(deadline)` and discards, up to four times within the **same** deadline, a frame that is
identifiably a late reply to our DTC read:

- a SingleFrame whose payload starts `59 02`, or is `7F 19 xx`;
- a FirstFrame whose data starts `59 02`.

These can never be a valid answer to the radar's other requests: the 0845 read is answered `62` or
`7F 22`, the service-alignment requests `50`, `71`, `7E` or `7F 10`/`7F 31`/`7F 3E`. No
ConsecutiveFrame of a DTC reply can exist, because stage 1a never sends a flow control. Nothing extra
is sent and no deadline is extended. A fifth stale frame in one exchange falls through to the
existing malformed handling.

Two call sites use it for their **first** frame only:

- `RadarAlignmentPoller.poll` (the 0845 read);
- `RadarServiceTransport._payload` in `radar_service_transport.py` (each service-alignment exchange,
  including the acknowledgement of `31 01 0251`).

So a late DTC answer can neither stop 0845 polling for the session nor make a routine-start
acknowledgement fail ambiguously. The change in `radar_service_transport.py` is one call
substitution in `_payload`; I judge it acceptable because the filter matches only service bytes
(`0x59`, `0x7F 0x19`) that no service-alignment exchange can produce, it keeps every existing
deadline and validation, and it sends nothing. It lands in its own commit with scripted-radar tests
(stale frames before a valid 0845 answer, before the `10 03` and `31 01` answers, and five stale
frames falling through).

## Timing, from the loop as it is

The cycle (`active_drive.py` `run_active_session`, lines 704–985) runs serially: broadcast snapshot
(`ACTIVE_SNAPSHOT_SECONDS = 0.35`, line 63), PCM duty and torque, RF Hub, VVT temperature when due,
oil life when due, `radar_steps.run` (0845 when due, service-alignment command intake), then
`_wait_for_next_cycle` (lines 584–595), which starts the next cycle at
`max(next_cycle + 1.0, cycle_started)`, that is, immediately when a cycle overran.

The DTC step runs after `radar_steps.run` and before `_wait_for_next_cycle`, at most one module per
cycle, the two modules at least 2 s apart. Its duration D is nominally about 2.9 s at most: the
fresh snapshot in `FreshAuthority.acquire` (≤ 0.35 s), the 2.5 s exchange deadline and a
non-blocking watch drain. `acquire` also runs the active gates (identity, interface state, topology,
inhibits; `active_session_gates.py` `active_gate`), so D is nominal, not a hard bound (see Build
checks). Added delay relative to each metric's normal cadence, twice per pass:

- everything the next cycle publishes (broadcast observations, PCM duty and torque at 1 Hz, the RF
  Hub round-robin at 1 Hz): its next sample comes at most about D later than its normal slot;
- VVT temperature (5 s), radar 0845 (10 s) and oil life (60 s): delayed by at most about D, only
  when their next read falls due in that window;
- service-alignment command intake: at most about D later than it would otherwise be read (intake
  stays in `radar_steps.run`; the DTC step never reads or holds commands);
- the supervisor's 10 s event-silence limit: the cycle's observations are emitted before the DTC
  step, so silence grows by at most about D.

## Radar service alignment

- `ServiceAlignment` (`radar_service_alignment.py`) gains a read-only `run_attempted` property: true
  once any start command has created a run record in this helper epoch, whatever its outcome
  (refused, failed, timed out, completed). A timed-out start goes to `_fail` although the radar may
  have accepted it, and a completed routine leaves the extended session to lapse on its own.
- `CommandReader` gains a non-consuming `pending()` check: its partial-line buffer is not empty, or
  `select` reports the pipe readable (including end of file).
- Before the radar send and again before the BCM send, the step stops reading for the rest of the
  epoch when `run_attempted` or `pending()` is true. A command arriving during a radar exchange is
  read at the next intake (at most about D later) and wins: the step never consumes or defers it, and
  the latch then blocks the BCM send.
- The radar exchange's window, from the start of `acquire` to the end of receive, is nominally about
  2.9 s, well under the broker's 15 s acknowledgement deadline (`REQUEST_ACK_SECONDS` in
  `radar_service_control.py`).
- Without the radar service flag there is no command pipe; only `run_attempted` applies (it stays
  false).

## Foreign-tester watch

- One `CAN_RAW` socket opened when the DTC step is constructed (if it cannot open, the step is
  disabled for the epoch and reports so), with filters for tester requests: 29-bit
  `0x18DA00F0`/`0x18DB00F0` with mask `0x1FFF00F0` (sources `F0`–`FF`, including our `F1`) and
  11-bit `0x7DF` and `0x7E0`–`0x7E7`. `SO_RXQ_OVFL` is on.
- Drained every cycle, non-blocking, at most 64 frames, with `recvmsg`, so the helper's own polling
  (about four to six frames a cycle) never fills it; and drained again immediately before each DTC
  send.
- A frame is ours only if it carries `MSG_DONTROUTE` and equals one of the frames this helper can
  send (the fixed PCM, RF Hub, 0845, service-alignment and DTC frames). Anything else is foreign.
  The watch is "dirty" for 120 s after a foreign frame, and also after any receive error, truncation
  (`MSG_TRUNC`/`MSG_CTRUNC`), overflow-counter change or a drain that reaches its 64-frame bound.
- A DTC send happens only when the watch is clean at the check just before it. A dirty watch means
  `not_sent` (`foreign_tester`), never a helper failure.
- Attribution limit: `MSG_DONTROUTE` marks frames created on this host, not by this process. The
  only other local senders are the repo's own diagnostic tools, which take the channel lock this
  helper holds exclusively and so cannot run during the epoch, and manual commands such as `cansend`
  that ignore the lock. A manual local command that sends exactly one of the helper's own frames is
  indistinguishable from the helper.
- Residual race: a foreign tester can start between our last check and our frame reaching the bus, or
  while we wait for the answer. Both testers then receive answers on the same `18DAF1tt` ID; our read
  may be `malformed`, or may accept an answer meant for the other tester if it asked the same module
  the same service at that moment. Nothing we send changes ECU state.

## Cooldown budget (broker-owned)

The broker reserves the budget before it launches a helper with the capability. The helper never
writes a file, and no acknowledgement round trip exists.

### Record

`tmp/vehicle_data/drive-dtc-budget.json`, one JSON object, at most 1 KiB:

- `schema`; `reservation_id` (32 hex, new for every reservation); `boot_id`
  (`/proc/sys/kernel/random/boot_id`);
- `origin_monotonic` (CLOCK_MONOTONIC at reservation), `first_due`, `interval` (3600), `window`
  (600);
- `session_end_monotonic` (null until that reservation's session ends);
- `last_pass_bound_monotonic` (the latest moment a pass could have started, carried forward within a
  boot).

### Computation (before each launch)

1. If the record is missing, unreadable or corrupt, has another `boot_id`, or its
   `session_end_monotonic` is still null (a session that ended without its update): **uncertain** →
   `due_in = 3600`. There is no wall-clock shortcut across a reboot.
2. Otherwise `elapsed = now − last_pass_bound_monotonic` (no bound yet → no constraint from earlier
   passes).
3. Harvested van sweep: `van-scan-cache.json` `last_scan.completed_at` (wall clock); if its age by
   the current wall clock is under 3600 s, `due_in` is at least `3600 − age`. (A wall-clock jump can
   only move a Pi pass relative to a van sweep; our own cadence uses only the monotonic clock.)
4. `due_in = clamp(3600 − elapsed, 0, 3600)` combined with step 3 (the larger), and
   `first_due = max(300, due_in)`.

### Helper schedule

Launch arguments: `--enable-drive-dtc --drive-dtc-reservation <reservation_id> --drive-dtc-origin
<origin_monotonic> --drive-dtc-first-due <first_due>`. Window k (k = 0, 1, …) is
`[t_k, t_k + 600]` with `t_k = origin + first_due + k·3600`. A pass starts only inside a window **and** at least 3600 monotonic seconds after the previous
actual pass start in this session. Before acquiring fresh authority or any permit for its first
module, the helper requires a clean foreign-tester watch and clear service-attempt/pending-command
checks, records `started_monotonic` from CLOCK_MONOTONIC, and emits `dtc_pass started`. This reading
is the start used for the helper's 3600-second rule. The pass consumes the hour even if no module
is subsequently sent. Authority acquisition and sends happen in a later step, so emitting the
start event cannot consume a permit's 250 ms lifetime. A pass that cannot start in its window (gates, dirty watch, stale
evidence, the 3600 s rule) is skipped, not carried over.

### Session end

When the helper's supervision ends, the broker computes
`last_pass_bound = min(session_end, t_k + 600)` for the latest `t_k ≤ session_end` (unchanged if
the session ended before `t_0`) and submits a session-end update for that `reservation_id`. This
assumes every scheduled pass happened at the latest moment it could have started, so even if every
helper event is lost, consecutive pass starts are at least 3600 s apart: within a session by the
helper's own rule, across sessions in a boot by the bound, across reboots by the full-hour wait.

### Writer

- **Which thread launches helpers today:** the broker's collector thread. `_collector_loop` (line
  3284) calls `_run_active_drive_if_ready` (line 2944, call at line 3311), which reaches
  `_supervise_drive_epoch` (line 3040) and `self.active_drive_supervisor.run(...)` (line 3159); the
  supervisor builds the command line and starts the helper inside `run`. The reservation happens on
  that path, just before `run`, and nothing else on it touches the budget.
- **One serialized writer:** a dedicated `drive-dtc-budget` thread, started with the broker only when
  the flag is on, owns the in-memory record and does all budget file I/O (loading at startup, every
  read and every write; a write is temporary file, `fsync`, rename, directory `fsync`). It holds no
  broker lock and is never called while the collector holds `self._lock`.
- **Bounded outstanding work:** its queue holds at most two operations. A full queue rejects a new
  reservation at once (launch without the capability) and drops a session-end update (the next
  launch then sees a null `session_end` and waits a full hour).
- **The whole reservation operation is bounded to 2 s of launch delay.** The collector submits a
  request and waits on its result for at most 2.0 s, which covers the writer's queue wait, its reading
  of `van-scan-cache.json`, the computation and the durable write. On timeout, or any error, the
  collector marks the request abandoned and launches without the capability. The writer skips an
  abandoned request it has not yet started; one already being written completes, leaving a record
  whose session never had the capability. Its `session_end` stays null, so the next launch waits a
  full hour: conservative.
- **Stale updates are rejected:** a session-end update applies only if its `reservation_id` equals
  the record's current one, so it can never overwrite a newer reservation.
- Rollback keeps the budget file.

### Other

- Helper events are not part of the budget. The broker checks `dtc_pass started` using its
  `started_monotonic` field, never receipt time: the field must be inside the reserved window, at
  least 3600 s after the previous start in the session, and no later than the broker's own
  monotonic reading at receipt. An arbitrarily delayed event is valid, including a window-edge
  start received after the window closes. A violation (a code fault) makes the broker drop that
  helper's results, stop it through the ordinary supervisor stop (its restoration and final event
  are processed as for any stop), and launch without the capability for the rest of its lifetime.
- Harvested sweeps are consulted at launch only (a stated relaxation). A sweep imported after a
  launch is first seen at the next launch, so it does not strictly suppress a pass in the current
  session; the watch covers a sweep in progress. The van's client only sweeps while Pi F1 traffic is
  quiet, so such a sweep ran before the session.

### Accumulated driving hours (not implemented; for the owner's choice)

A change to the broker's computation only: keep `running_seconds_since_last_pass` in the record, add
each session's duration (origin to `session_end`) at session end, set
`due_in = 3600 − running_seconds_since_last_pass` (still at least 300 s into a session, and the
helper's 3600 s rule then applies to accumulated running time, which within one session equals
elapsed time). Short trips contribute; a lost `session_end` or a reboot resets the accumulation
(assumes a pass happened), which is conservative. The helper is unchanged.

## Capability handshake

- Explicit broker-added arguments (the oil-life pattern, `--enable-oil-life` at broker.py line 390):
  the four arguments above are added only when the broker was started with
  `--enable-drive-dtc-reads`, has no latch set, and holds a completed reservation.
- Positive confirmation in the startup exchange (new; oil life has none): a helper launched with
  `--enable-drive-dtc` adds `"capabilities": ["drive_dtc_v1"]` to its `ready` event, and only then.
  An old broker validates `ready` with the exact keys `type`, `protocol`, `pid`
  (`helper_startup_grant.ready_event_problem`), so the key appears only for a broker that asked. The
  new validator accepts the optional key and keeps legacy events valid.
- A new broker that passed the argument and receives a valid `ready` without the capability does
  **not** grant: it takes the existing cancel-before-grant path (the helper is reaped and the broker
  synthesises the final with `restored: null`; no inhibit), never the protocol-error path, and
  launches without the capability for the rest of its lifetime.
- Combinations:
  - old broker, new helper: no argument → the step is off; no new event type is ever emitted;
  - new broker without the flag, any helper: no argument → no DTC send, no watch socket;
  - new broker with the flag, old helper: the old helper rejects the unknown argument and exits
    before `ready`, which the supervisor treats as unverified restoration (as for oil life). It
    arises only if the checkout is reverted under a running flagged broker; the rollback order
    prevents it.
- Rollback order: (1) remove `--enable-drive-dtc-reads` from the installed unit, `daemon-reload`,
  `s restart_broker` (the new broker, flag off); (2) revert the code; (3) `s restart_broker`. Never
  start an old broker while the unit still carries the flag: it cannot parse it and fails to start.

## Helper events

Every value the helper can emit is in the declared vocabulary (`drive_dtc.json`), and every event is
valid on its own, without any other event or broker state. The helper validates each event with the
contract function the broker uses before emitting it; an event that would fail is not emitted, and
the step disables itself for the epoch instead.

```json
{"type": "dtc_step", "session": "<reservation_id>", "state": "enabled|disabled",
 "reason": "capability|sentinel_unavailable|service_run_attempted|contract_error",
 "detail": "...", "interface_mode": "armed_diagnostic"}
{"type": "dtc_pass", "session": "<reservation_id>", "pass": 0, "state": "started|finished",
 "reason": "first_attempt|all_modules_done|window_closed|service_latch|foreign_tester|session_ending",
 "started_monotonic": 12345.0,
 "detail": "...", "interface_mode": "armed_diagnostic"}
{"type": "dtc_read", "session": "<reservation_id>", "pass": 0, "module": "radar_acc|bcm_ccan",
 "request": "19 02 8F",
 "outcome": "complete|truncated|negative|no_response|malformed|not_sent",
 "reason": "<NRC text, or for not_sent: evidence_stale|permit_expired|foreign_tester|service_latch|drain_failed|window_closed>",
 "nrc": "31|null", "pending_count": 0, "frame_hex": "<at most 8 bytes>|null",
 "count": 6, "started_at": "<UTC ISO>", "completed_at": "<UTC ISO>",
 "rpm": 1650.0, "speed_mph": 61.2, "interface_mode": "armed_diagnostic"}
```

- `session` is the launch's reservation id and `pass` the window index k, so passes from different
  launches are never combined. `dtc_pass started` requires `started_monotonic`; `finished` omits
  it. The start is recorded and emitted before fresh authority or permits are acquired, even if
  no module is ultimately sent. The `first_attempt` reason denotes admission to the first
  attempt, not proof that a request was sent. Per-module events carry the pass identity and
  their existing wall-time observation fields, not a second monotonic pass start. `count` is present for `complete` (0 or 1) and `truncated` (from the FirstFrame
  length), otherwise null. `frame_hex` is the final answer frame as received.
- The broker parses `frame_hex` itself; a frame it cannot parse is stored as `unparsed`, a storage
  value, never a protocol error.
- Dispatch: `broker.handle_active_drive_event` hands the three types to `drive_dtc_control.py`
  before `validate_helper_status`. That handler only validates and enqueues (`put_nowait`); it never
  raises for a full queue or a storage error.
- No change to `ACTIVE_DRIVE_FAILURE_REASONS`, final events or `helper_events.py`.

## Storage

One JSON file, `tmp/vehicle_data/drive-dtc-results.json`, written only by one broker thread (not the
budget writer).

- Content: `schema` (1), `coverage` ("failed, pending, confirmed or warning-lamp codes only
  (19 02 8F)"), and the newest 200 passes' reads, each keyed by `session` and `pass`: the event fields
  plus the parsed result (`complete`: the code if any, its status and `lib.dtc.status_semantics`, the
  availability mask; `truncated`: count, first code, availability mask). This file is also what the
  dashboard reads.
- Bound: at most 200 passes × 2 reads, about 300 bytes each, about 120 KiB; the writer refuses to
  exceed 256 KiB (dropping the oldest passes first). Written by temporary file and rename, so the
  transient peak is two copies, at most 512 KiB. No database, so no journal or WAL. The budget record
  adds at most 2 KiB with its temporary file. Both lie under the existing
  `/home/pi/dev/obd-things/tmp` root budget (5120 MiB in `ROOTFS_PATH_BUDGETS_MIB`), so no new entry.
- Retention: a window of the newest 200 passes (about 200 hours of driving at one pass an hour);
  nothing is derived across dropped passes.
- Failure: a full queue (64 items) or a write error drops the results and latches
  `drive_dtc_storage_failed` in the broker, which launches later helpers without the capability. The
  running helper continues its schedule, which the budget still limits to one pass an hour; its
  results are dropped. That is the whole effect of a storage failure.
- One thread rewrites the whole file from its in-memory state in arrival order, so an older
  publication cannot overwrite a newer one.

## Dashboard: the three-source rule

### What each source exposes

- **parked** (`dtc-cache.json`, the compact v2 cache from `lib/dtc.py` `dashboard_summary`): per
  module `availability`, `last_attempt_at`, `last_success_at`, `status_availability_mask` of the last
  success, `absence_authoritative`, `result_state`; per record (at most 25 per display group, with
  `groups_truncated`) `raw_dtc`, `status` and its semantics, `present`, `observation_state`, the
  record's own `status_availability_mask`, `first_seen_at`, `last_seen_at`. Request mask `FF`.
- **van** (`van-scan-cache.json`, `van_scan_harvest_store.cache_payload`): per module **only its
  latest successful** in-vehicle result (`observed_at`, `scan_started_at`, `protocol`,
  `status_availability_mask` (may be null), `dtc_count`, `dtcs` with status), plus the latest scan's
  facts. No failed attempts and no earlier results. Request mask `0D`.
- **drive** (`drive-dtc-results.json`): per module every read of the newest 200 passes, successes and
  failures, `complete` or `truncated`, each with its availability mask. Request mask `8F`.

### Rule, per module

The merge reflects at most one van read (its latest success) and one drive read (the latest
`complete` success), plus the latest `truncated` drive read and failure lines.

1. **Effective coverage** of a filtered read = request mask AND the read's returned availability
   mask (van: `0D & mask`; drive: `8F & mask`). A null or zero mask gives zero coverage: such a read
   can add or update codes it lists but never clears anything. (`lib/dtc.py` lines 756–771 apply the
   same guard to parked absence conclusions.)
2. **Failures apart from successes.** Failed attempts never change codes. A drive failure newer than
   the drive's latest success shows as its own line ("drive read failed (no response) at 14:05 MST");
   the parked failure line comes from the parked module row; the van exposes no failures. This keeps
   the fix to `health.helpers.js` (line 508 compares the van read with the Pi's last attempt).
3. **Base:** the parked returned records, each labelled with its **own** `last_seen_at` and
   `observation_state` (retained records can be older than the module's last parked success).
4. **Filtered reads applied in chronological order** (van latest success and drive latest `complete`
   success, older first). For a read at time t with effective coverage E, and each shown record r with
   `r.last_seen_at < t`:
   - the read lists r's code → r takes its status and time t;
   - the read does not list it and r's last known status has a bit in E → r moves to "not reported by
     the <source> read at t";
   - otherwise (no bit of r's status in E, for example `0x40` only) r stays, labelled with its own
     date, so it is never implied current.
   A code the read lists that is not shown is added, labelled with t.
5. **A `truncated` drive read** newer than the drive's latest `complete` read changes no code. It
   shows "<count> codes reported at t, list not retrieved; first <code>", and "count differs from the
   codes shown" when the number of shown codes whose status has a bit in its effective coverage
   differs.
6. **Empty filtered result:** an empty `complete` read reads "no failed, pending, confirmed or
   warning-lamp codes at t" (van: "no failed, pending or confirmed codes at t"); never "no DTCs".
   `result_state`, `no_dtcs` and `absence_authoritative` come from parked reads only (today's
   `mergeVanScan` sets them from the van read; that is fixed in its own commit).
7. **Truncated parked groups** keep their "+N more" line; filtered reads act only on returned records,
   and counts change only for returned records.
8. Every filtered result shows its coverage text, its source and its time in Mountain time.

## PCM: what its later design needs (not designed here)

The owner named the PCM; it follows after the radar and BCM have run clean on several drives. Its own
plan and review must cover:

- the service: the PCM rejects UDS `19` (`7F 19 11`); its codes come from KWP2000 `18 00 FF 00`,
  answered `58 NN` then three bytes per code with KWP status semantics, so parsing, presentation and
  storage differ;
- framing: only `58 00` (a SingleFrame) has been seen on the bus. A segmented KWP DTC reply has not
  been observed; a stage-1a-style read would record a count; a full list needs the PCM's 8-byte
  zero-padded flow control (proven only for `1A 87`, 2026-08-30);
- endpoint: the PCM poller (`pcm_electrical.PcmElectricalPoller`) reads with no drain, and its 1 s
  duty/torque reads end the session on a stray frame, so the design must keep every PCM answer off
  that socket or give it a bounded drain (a change to the protected PCM path);
- a parked, owner-authorized validation first.

## Deployment

Review approval covers building only. Steps 3 onward need the code review of the built change.

1. Land the commits with the flag off. The harvester recognises the new request from its next run;
   no broker passes the new arguments.
2. `s restart_broker` (parked) to load the new broker, flag off. The status hashes are unchanged
   (`tests/broker_status_hashes.json`).
3. Activation (after the code review approves it): add `--enable-drive-dtc-reads` to `ExecStart` in
   the tracked `projects/vehicle_data/systemd/van-telemetry.service`; install it the way the
   vehicle-data README deployment section says (copy, `daemon-reload`); check
   `systemctl show -p KillMode,TimeoutStopUSec van-telemetry.service`; `s restart_broker` parked.
4. Off: rollback-order step (1). Every later helper launches without the arguments.
5. Rollback: the rollback order above. The budget file stays; the results file can stay.
6. First drives:
   - `ready` carries the capability; `dtc_step enabled`; `dtc_pass started`, two `dtc_read` and
     `dtc_pass finished` about 5 minutes after launch (or later, per the budget); final
     `restored: true`;
   - the C-CAN recording around the pass: one `18DA2AF1#0319028F00000000` and one
     `18DA40F1#0319028F00000000`, no frame from us after either, no other new identifier or payload;
   - the watch reports no foreign frame while only the Pi is on the bus (this confirms
     `MSG_DONTROUTE` on this kernel; if it is missing, every check is dirty and nothing is sent);
   - around each read, every metric's next sample is no later than its normal slot plus about D;
     0845 polling continues afterwards and no service start fails because of a DTC frame;
   - the results file and the dashboard show the radar's `complete` read and the BCM's `truncated`
     read with coverage, source and time;
   - the budget record has `session_end_monotonic` set after the drive; on a drive over 65 minutes a
     second pass at least an hour after the first; after a stop under an hour, no pass until the hour
     has elapsed.
   - "Several clean drives", before stage 1b or more modules: at least five passes on at least three
     drives meeting every check above; the radar's complete reads show no unexplained new code; the
     BCM's count and first code stay consistent (a count and first code cannot show whether other
     BCM codes changed); and an owner-triggered parked `19 02 FF` batch afterwards, compared with the
     Oct 8 batch, shows whether the radar or the BCM logged anything new after the unanswered
     FirstFrames.

## Tests

- `tests/drive_dtc_support.py`: a scripted module ECU (SingleFrame, FirstFrame, NRCs, 0–3 pendings,
  silence, malformed frames, late answers), a scripted watch feed (local/foreign flags, truncation,
  overflow, errors, bound reached), reusing `Clock`, `FakeAuthority` and `Sink` from
  `tests/radar_service_support.py`.
- Transport: exact frame per module; never any other frame (no flow control under any input); the
  2.5 s deadline; `not_sent` before the send versus outcomes after it; attempted marked before send,
  including an `OSError` raised by send.
- Stale-frame filter: stale `59 02`/`7F 19` frames before a valid 0845 answer and before the
  `10 03` and `31 01` answers are skipped within the original deadline; a fifth falls through; legit
  `7F 22`/`7F 31` answers are never skipped.
- Step: windows and the 3600 s rule between actual pass starts; one module per cycle and 2 s spacing;
  the radar read only with the next 0845 at least 5 s away; the service latch (`run_attempted`,
  `pending()`, rechecked before the BCM); the watch drained every cycle and checked before each send;
  every emitted event passes the contract validator.
- Helper (`test_active_drive.py`): without the arguments no DTC frame and no new socket (scan every
  sent frame and socket); `ready` carries the capability only with them.
- Broker: budget computation (uncertain cases, reboot, harvested sweep), the 2 s bound with a writer
  blocked in I/O (launch without capability within 2 s), full queue, abandoned requests, stale
  session-end rejection, the missing-capability cancel-before-grant path, the out-of-window pass stop,
  event handling that never raises, storage failure latch, results-file bound.
- `test_transmit_permit`: the two purposes.
- Dashboard Node tests: every rule above, including null masks, own `last_seen_at`, empty and
  truncated reads, and the `mergeVanScan` fixes.
- Harvester: sync tests for `19 02 8F`, `22 21 85` and the radar service requests.
- Oracles: `broker_pure_oracle` status hashes unchanged; `gate_oracle` byte-identical; every tidying
  commit compared old/new by output.
- Whole suite before every commit that leaves the worktree and before the code review:
  `/home/pi/van_compute/scripts/pi_compute.py run repo-full-suite --wait 60`. As of 2026-11-15 it is
  red on master: `tests/test_notes_gate.py::test_no_test_module_names_a_note` runs `git ls-files`
  in a compute bundle without `.git`. That must be fixed before this work can commit.

## Build checks from review

For the builder, and for the code reviewer to confirm:

1. **Timing is nominal.** The 2.9 s figure excludes the active-gate costs inside
   `FreshAuthority.acquire`. Add a test that drives one radar exchange at its deadline together with a
   service command arriving mid-exchange, and asserts both the command-intake delay and the
   event-silence gap against the 15 s acknowledgement deadline and the 10 s silence limit.
2. **`dtc_pass` is defined in the contract** (`drive_dtc_contract.py` and `drive_dtc.json`) with the
   session/reservation identity, exactly as in Helper events; nothing combines passes across
   sessions.
3. **Unanswered FirstFrames are a staged experiment** whose effect on the radar and the BCM is
   unknown. Acceptance must not claim "no new BCM code" from a count and first code (Deployment,
   step 6 states what each read can show).
4. **Launch-only harvest consultation is a stated relaxation**; nothing in code or docs promises
   strict suppression after a mid-session import.
5. **A missing capability confirmation takes cancellation before grant**, never the protocol-error
   path (`broker.py` lines 566–575 and 664–680); a test proves the final is `restored: null` with no
   inhibit.
6. **Extraction commits carry output-equivalence evidence** (old/new outputs compared, recorded in
   the commit message).
7. **Approval covers building, not activation.** Turning the broker flag on is a separate step after
   the code review.
8. **Actual start time, not event receipt.** A start at the final instant of its window delivered
   after the window closes is accepted. A start before its window, less than 3600 s after the
   previous start, or later than receipt takes the normal cadence-violation stop with verified
   restoration, not a protocol error. Expiry before the first send leaves that module unattempted
   and the recorded pass start unchanged. The session-end budget remains conservative and never
   uses events to shorten a cooldown.

## Code shape

New modules, one concern each, with their AGENTS.md map entries in the same commit:

- `projects/vehicle_data/drive_dtc_contract.py` + `drive_dtc.json`: frames, purposes, timing
  constants, schedule rule, event vocabulary and validator.
- `projects/vehicle_data/drive_dtc_transport.py`: the only code that builds or sends the two frames.
- `projects/vehicle_data/drive_dtc_watch.py`: the foreign-tester watch.
- `projects/vehicle_data/drive_dtc_steps.py`: the per-cycle step.
- `projects/vehicle_data/drive_dtc_budget.py`: the broker's budget record, computation and writer
  thread.
- `projects/vehicle_data/drive_dtc_control.py`: broker side (arguments, capability check, dispatch,
  results writer, latches).
- A sibling of `insights.py` for the `drive_reads` block; `dashboard/src/views/dtcSources.helpers.js`
  (the three-source merge, moved out of `health.helpers.js`).
- `projects/vehicle_data/van_scan_pi_requests.py` (the harvester's Pi request tables).

Extended in place: `transmit_permit.py` (two purposes), `radar_alignment.py` (`receive_reply`, used
by `poll`), `radar_service_transport.py` (one call in `_payload`), `radar_service_alignment.py`
(`run_attempted`, `CommandReader.pending`), `helper_startup_grant.py` (the optional `capabilities`
key in `ready`), `vehicle_speed.py` (a new pure sibling holding the speed reader and bound promoted from
`radar_service_alignment._speed_mph`; the radar module retains its original aliases,
while `ccan_powertrain.py` is unchanged). Fixed speed decoding remains in
`lib/broadcast_signals.py`, without duplicated IDs or scaling.

Over-limit files gain no net lines: `active_drive.py` / `run_active_session` (the oil-life block
moves to a sibling step first), `broker.py` (`build_parser` moves to `broker_cli.py` first),
`insights.py` (the van-scan cache validator moves out), `van_scan_harvest.py` (request tables move
out). `lib/dtc.py` is not changed. The vehicle-data README frame list gains the two frames in the
commit that wires the step.

## Build order (each commit safe to land alone)

1. Tidying: harvester request tables → `van_scan_pi_requests.py` (output-identical).
2. Harvester: recognise `19 02 8F` from the Pi (before anything can send it).
3. Harvester: recognise `22 21 85`.
4. Harvester: recognise the radar service-alignment requests.
5. Tidying: oil-life block out of `run_active_session` (behaviour-identical).
6. Tidying: `broker.build_parser` → `broker_cli.py` (`--help` and broker oracle identical).
7. Tidying: `mergeVanScan` → `dtcSources.helpers.js`; the van-scan cache validator out of
   `insights.py` (outputs identical).
8. Dashboard fixes: compare successes, not attempts; no `no_dtcs`/`absence_authoritative` from a
   filtered read.
9. Radar stale-frame filter (`receive_reply` in the 0845 poll and the service transport's first
   frame), with its tests. It only skips frames that are malformed today.
10. Contract, vocabulary, transmit-permit purposes, transport, watch (library only).
11. `run_attempted`, `CommandReader.pending`, the optional `ready` capability key (inert).
12. Step module and helper wiring behind `--enable-drive-dtc` (inert: no broker passes it).
13. Broker: budget writer, control, results writer, capability check, the flag (off by default), the
    `drive_reads` block and dashboard display; README frame list and section.
14. Activation: the unit flag, only after the code review approves it and after the parked deploy of
    1–13.

## Review findings and where this plan answers them

First review (of the first plan):

| # | Finding | Position | Where |
| --- | --- | --- | --- |
| 1 | Foreign-tester send-time gap | Accepted (resolved) | Foreign-tester watch |
| 2 | Service inactivity is not ECU inactivity | Accepted (resolved) | Radar service alignment |
| 3 | Blocking reads; late replies on shared endpoints | Accepted | The exchange; Late radar replies |
| 4 | Migration backup and rollback | Accepted (resolved: no migration) | Storage |
| 5 | Migration activation; new-broker/old-helper | Accepted (resolved) | Storage; Capability handshake |
| 6 | Retention | Accepted | Storage |
| 7 | Budget durability; harvest refresh | Accepted in part: durability on the broker; harvest at launch only (stated relaxation) | Cooldown budget |
| 8 | Coverage and source precedence | Accepted | Dashboard |
| 9 | First-drive scope; FC form; wording | Accepted (resolved) | Settled decision 3; Staging |
| 10 | Classifier prerequisite; build order | Accepted (resolved) | Build order |

Second review (of `8031e8d`):

| # | Finding | Position | Where |
| --- | --- | --- | --- |
| 1 | Helper-side `fsync` can stall into an inhibit | Accepted (resolved): the helper writes nothing | Cooldown budget |
| 2 | Service-command delay past its 15 s deadline | Accepted (resolved) | Radar service alignment; Timing |
| 3 | Watch maintenance and attribution | Accepted (resolved) | Foreign-tester watch |
| 4 | Timing, permit lifetime, radar queue, timestamps | Accepted | Staging; The exchange; Late radar replies; Timing |
| 5 | Environment handshake | Accepted (resolved) | Capability handshake |
| 6 | Cooldown recovery; storage shutdown | Accepted | Cooldown budget; Storage |
| 7 | Expected failures as protocol errors | Accepted | Helper events |
| 8 | Three-source merge | Accepted | Dashboard |
| 9 | Storage bound and gap wording | Accepted | Storage; Deployment step 6 |

Third review (of `7c44321`, approve to build with listed changes):

| # | Finding | Position | Where |
| --- | --- | --- | --- |
| 1 | Cooldown not guaranteed (windows 50 min apart; clock jumps across reboot; attempt marking) | Accepted | Settled decision 5; Cooldown budget (helper 3600 s rule, full hour after reboot); The exchange step 4 |
| 2 | Budget writer ordering and isolation | Accepted | Cooldown budget: Writer |
| 3 | Dashboard freshness and absence | Accepted | Dashboard: what each source exposes; rule |
| 4 | Late radar replies during a routine start | Accepted | Late radar replies in later radar exchanges |
| 5 | Nominal timing; undefined `dtc_pass` | Accepted for the build | Timing; Helper events; Build checks 1–2 |
| 6 | Unanswered FirstFrames are an experiment | Accepted for the build | Staging; Deployment step 6; Build check 3 |
| 7 | Departures and build order acceptable; cancellation before grant; output evidence | Accepted for the build | Capability handshake; Build checks 4–6 |

## Remaining residuals

- An unexpected exception in the step ends the session as `helper_failed` with verified restoration
  (the existing path for every step).
- More than four identifiable stale radar frames in one later exchange fall through to the existing
  malformed handling (0845 polling stops for the epoch, or that service run fails before the routine
  is confirmed).
- New broker plus old helper is prevented only by the rollback order (as for oil life).
- The foreign-tester residual race and attribution limit (Foreign-tester watch).

## Open questions

1. **Elapsed cooldown or accumulated driving hours**: for the owner; both costs above.
2. **Whether the radar or the BCM logs anything after an unanswered FirstFrame**: unknown; checked by
   the first drives and a later parked batch.
3. **The first `8F` request** to these modules: a `7F 19 12`/`31` is stored as `negative`; the next
   step is a review, not a mask change in the field.
4. **`MSG_DONTROUTE` through Python's `recvmsg` on this kernel**: documented, not yet observed here;
   if absent, nothing is sent (visible on the first drive).
5. **The Pi's continuous F1 polling suppresses the van's own health check**: unchanged; for the
   owner.
6. **Red suite on master** (`test_notes_gate.py` in compute bundles): blocks committing the build.

Not determinable from the repository: what triggers the van's own sweep; `8F` reply sizes and
pending behaviour at speed for these two modules.

## Status

Plan approved to build by the third cross-family review, with its findings 1–4 written in above and
5–7 carried as build checks. Activation is not approved. Nothing built; no CAN traffic, service
change or live write. The read-only queries behind it used `mode=ro` on
`tmp/vehicle_data/dtc-history.sqlite3` and the archived 2026-09-24 capture on `/mnt/EXFAT512`.
