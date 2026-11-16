---
workstream: drive-dtc-read
status: plan awaiting review (third revision)
tier: T3
last reviewed commit: none (8031e8d reviewed as "rework"; this revision not yet reviewed)
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
`worktree-agent-ace5b03e8cf37934c`; a cross-family review comes before any merge, install or
deploy.

Design rule for this feature: it is one small read an hour, so prefer designs in which a failure
path does not exist over designs that handle it. Nothing in it may block the helper's polling
thread on file I/O, and no expected condition may end in a helper protocol error or an inhibit.

## Settled decisions

Settled by the parent session on the owner's behalf (2026-11-15).

1. **Mechanism.** The Pi reads codes itself inside the C-CAN active-drive helper, with fresh
   engine-running evidence and a one-use transmit permit per send; physical 29-bit addressing,
   default session. Never `14`, `10`, `27`, `2E`, `2F`, `31` or `3E`.
2. **Mask `19 02 8F`** (warningIndicatorRequested | confirmed | pending |
   testFailedThisOperationCycle | testFailed). `FF` returns the "test not completed" bits that
   change through every drive (Oct 8: 278 of 300 records were status `0x40` only); `0D` is the
   harvester's signature for the van's own sweep. On the Oct 8 data `8F` keeps all 22
   fault-relevant records.
3. **Modules: the radar (DASM) and the BCM only.** Cluster, RF Hub, TCM, shifter and telematics
   wait until these two have run clean on several drives; the PCM is the phase after that; B-CAN
   and CAN-CH modules stay parked-only.
4. **Storage.** The parked history (`module_scans`, `dtc_state`, `dtc_transitions`, the v2 cache,
   its `request_hex` CHECK) does not change. Drive reads have their own bounded storage.
5. **Cadence is an elapsed-time cooldown**: a pass is allowed during a qualified engine-running
   interval once the helper session is 300 s old, when at least 60 minutes have passed since the
   last pass could have happened and since the last harvested van sweep completed. This is elapsed
   time, not accumulated driving time; trips under five minutes never read. "Accumulated driving
   hours" below says what the other reading takes; the parent is putting the choice to the owner.
   Engine running while parked counts.
6. **Opt-in.** Broker CLI flag `--enable-drive-dtc-reads`, off by default. Removing it disables
   every new send.

## Staging

- **Stage 1a (this plan): one request frame per module per pass, no flow control at all.**
- **Stage 1b (separate plan and review, after 1a has run clean on several drives):** add a flow
  control for a segmented reply, with its own fresh authority and permit taken when the FirstFrame
  arrives (a permit lives 250 ms, so the request's permit can never cover it after a pending
  response).

Why stage: sending no flow control removes the paired-permit lifetime problem, every late
ConsecutiveFrame, the separate radar socket and the endpoint-ownership window, which were the
sources of most failure paths in the earlier revisions. What 1a gives up: for a segmented reply it
records the count and the first code only.

What 1a returns, from the Oct 8 statuses: the radar had one record matching `8F` (status `0x08`),
so its reply should be a complete SingleFrame; the BCM had six (its LED-lamp codes), so its reply
should be a FirstFrame, recorded as "6 codes reported, list not retrieved; first code B1636".

Leaving a FirstFrame unanswered:

- ISO 15765-2: the sender waits N_Bs (typically 1 s) for a flow control, then abandons the message
  and returns to idle. It is a transport-level timeout, not a diagnostic request.
- Seen on this van: the van's own tester left PCM FirstFrames unanswered more than a dozen times in
  one sweep on 2026-09-24 (the PCM ignores its unpadded flow control, and for `22 1FD9` it sent
  none), and the PCM then answered its next requests normally and reported zero stored codes
  (`58 00`). The Pi's 0845 poller also sends no flow control for a FirstFrame it rejects.
- Not known: whether the radar or the BCM logs anything for an abandoned transmission (no such
  code is in the repo's reviewed DTC tables or the Oct 8 results). The first drives check both
  modules' next `8F` replies for any new code.

## What goes on the bus

Two fixed frames, both in `drive_dtc_transport.py`, each behind its own one-use permit purpose:

| module | frame (29-bit, DLC 8) | permit purpose |
| --- | --- | --- |
| `radar_acc` (DASM) | `18DA2AF1#0319028F00000000` | `radar_acc.dtc_read` |
| `bcm_ccan` | `18DA40F1#0319028F00000000` | `bcm_ccan.dtc_read` |

- The framing is the van tester's own while driving (`03 19 02 0D`, DLC 8, `00` padding); only the
  mask byte differs. `8F` has not been sent to these modules before.
- Bus occupancy: each pass sends two frames and receives about two to five (a SingleFrame or a
  FirstFrame, plus any response-pending frames). A 29-bit frame with 8 data bytes is about 130–160
  bits with stuffing, about 0.3 ms at 500 kbit/s, so a pass occupies roughly 1–2 ms of bus time.
  While one of these frames is on the bus, any other frame that becomes ready waits for it to
  finish. For comparison, the helper already sends four to six requests a second.

## The exchange

Each module read is one synchronous, bounded exchange in one helper cycle, on one clock
(`backend.monotonic`, which is `time.monotonic`, CLOCK_MONOTONIC).

- Socket:
  - radar: the open `RadarAlignmentPoller` link (`drain`, `send_fixed`, `receive`), the pattern of
    `RadarServiceTransport`. The radar keeps one tester socket.
  - BCM: a raw `CAN_RAW` socket opened for the exchange (exact EFF filter `18DAF140`, RTR/ERR
    rejected) and closed after it. Nothing else in the helper listens to `18DAF140`.
- Steps: `FreshAuthority.acquire((purpose,))`, then the foreign-tester and service checks, then
  `drain` (radar: the poller's bounded 32-frame drain; a drain failure means the read is not sent),
  then the send, then receive until a final answer or the deadline:
  - first answer within 1.0 s of the send;
  - `7F 19 78` (response pending) extends the deadline to 2.5 s after the send, and never beyond;
    the request is never resent;
  - SingleFrame `59 02 mask [code status]` (length 3 or 7) → `complete` (zero or one code);
  - FirstFrame `1L LL 59 02 mask c1 c2 c3` with (length − 3) % 4 == 0 → `truncated`, with
    count = (length − 3) / 4 and the first code without its status; nothing is sent back;
  - `7F 19 nn` (other NRC) → `negative` with the NRC (`7E`/`7F` reported as `session_required`;
    nothing changes the session);
  - any other frame → `malformed`;
  - deadline → `no_response`.
- Exceptions: `OSError`, `ValueError` and timeouts become outcomes (as in the radar poller).
  `ExpiredTransmitPermitError` before the send becomes `not_sent` (`permit_expired`). Any other
  `TransmitPermitError` stays fatal (`helper_failed`), as for every existing transport.
- Late frames:
  - BCM: after the socket closes, a late frame reaches no consumer.
  - Radar: a frame that arrives after the exchange (a final answer later than 2.5 s, or an
    unanswered FirstFrame's nothing) stays queued on the poller's socket and is removed by the next
    `drain` (0845 poll or service exchange). The radar read is sent only when the next 0845 poll is
    at least 5 s away (`RadarDriveSteps.next_alignment`), so a late answer normally lands in the
    queue, not inside an 0845 wait. Residual: a radar answer later than about 5 s could land inside
    an 0845 wait; that read is malformed and 0845 polling stops for the rest of the epoch (the
    existing handling, no inhibit). The same residual applies to a service-alignment exchange begun
    within seconds of a radar read: its first reply could be the late answer, and that run fails
    safely before the routine starts and can be requested again.

## Timing, from the loop as it is

The cycle (`active_drive.py` `run_active_session`, lines 704–985) runs serially: broadcast
snapshot (`ACTIVE_SNAPSHOT_SECONDS = 0.35`, line 63), PCM duty and torque, RF Hub, VVT temperature
when due, oil life when due, `radar_steps.run` (0845 when due, service-alignment commands), then
`_wait_for_next_cycle` (lines 584–595), which starts the next cycle at `max(next_cycle + 1.0,
cycle_started)`, that is, immediately when a cycle overran.

The DTC step runs after `radar_steps.run` and before `_wait_for_next_cycle`, at most one module per
cycle, the two modules at least 2 s apart. Its duration D is at most 0.35 s (the fresh snapshot in
`FreshAuthority.acquire`) + 2.5 s (exchange deadline) + a non-blocking sentinel drain (at most 64
frames), about 2.9 s. Added delay relative to each metric's normal cadence, twice per pass:

- everything the next cycle publishes (broadcast observations, PCM duty and torque at 1 Hz, the RF
  Hub round-robin at 1 Hz): its next sample is at most D later than its normal slot;
- VVT temperature (5 s), radar 0845 (10 s) and oil life (60 s): delayed by at most D, only when
  their next read falls due in that window;
- service-alignment command intake: at most D later than it would otherwise be read (intake stays
  in `radar_steps.run`; the DTC step never reads or holds commands);
- the supervisor's 10 s event-silence limit: the cycle's observations are emitted before the DTC
  step, so silence grows by at most D.

## Radar service alignment

- `ServiceAlignment` gains a read-only `run_attempted` property: true once any start command has
  created a run record in this helper epoch, whatever its outcome (refused, failed, timed out,
  completed). A timed-out start or a completed routine can leave the radar in its extended session
  with no active phase in the helper.
- `CommandReader` gains a non-consuming `pending()` check: its partial-line buffer is not empty, or
  `select` reports the pipe readable (including end of file).
- Before the radar send and again before the BCM send, the step reads nothing further for the rest
  of the epoch when `run_attempted` is true or `pending()` is true. A command that arrives during a
  radar exchange is read at the next intake (at most D later) and wins: the step never consumes or
  defers it, and the latch then blocks the BCM send.
- The radar exchange's window, from the start of `acquire` to the end of receive, is at most about
  2.9 s, well under the broker's 15 s acknowledgement deadline (`REQUEST_ACK_SECONDS`).
- Without the radar service flag there is no command pipe, so only `run_attempted` applies (it stays
  false).

## Foreign-tester watch

- One `CAN_RAW` socket opened when the DTC step is constructed (if it cannot open, the step is
  disabled for the epoch and reports so), with filters for tester requests: 29-bit
  `0x18DA00F0`/`0x18DB00F0` with mask `0x1FFF00F0` (sources `F0`–`FF`, including our `F1`) and
  11-bit `0x7DF` and `0x7E0`–`0x7E7`. `SO_RXQ_OVFL` is on.
- Drained every cycle, non-blocking, at most 64 frames, with `recvmsg`, so our own polling (about
  four to six frames a cycle) never fills it; and drained again immediately before each DTC send.
- A frame is ours only if it carries `MSG_DONTROUTE` and equals one of the frames this helper can
  send (the fixed PCM, RF Hub, 0845, service-alignment and DTC frames). Anything else is foreign.
  The watch is "dirty" for 120 s after a foreign frame, and also after any receive error, truncation
  (`MSG_TRUNC`/`MSG_CTRUNC`), overflow-counter change or a drain that hits its 64-frame bound.
- A DTC send happens only when the watch is clean at the check just before it. A dirty watch means
  "not sent this window" (`not_sent`, `foreign_tester`), never a helper failure.
- Attribution limit, stated plainly: `MSG_DONTROUTE` marks frames created on this host, not by this
  process. The only other local senders are the repo's own diagnostic tools, which take the channel
  lock this helper holds exclusively and so cannot run during the epoch, and manual commands such as
  `cansend` that ignore the lock. A manual local command that sends exactly one of the helper's own
  frames is indistinguishable from the helper.
- Residual race: a foreign tester can start between our last check and our frame reaching the bus,
  or while we wait for the answer. Both testers then receive answers on the same `18DAF1tt` ID; our
  read may be `malformed`, or may accept an answer meant for the other tester if it asked the same
  module the same service at that moment. Nothing we send changes ECU state.

## Cooldown budget (broker-owned)

The broker reserves the budget before it launches a helper with the capability, so the helper never
writes a file and no acknowledgement round trip exists.

- **Budget record** `tmp/vehicle_data/drive-dtc-budget.json`, one JSON object, at most 1 KiB:
  `schema`, `boot_id`, `origin_monotonic` and `origin_wall` (the launch), `clock_synchronized`
  (`adjtimex` without `STA_UNSYNC`), `first_due`, `interval` (3600), `window` (600),
  `session_end_monotonic`/`_wall` (null until the session ends), `last_pass_bound_monotonic`/`_wall`
  (the latest time a pass could have happened, carried forward).
- **Before each launch with the capability** (on a short-lived broker thread, never the helper):
  1. read the record; compute `elapsed` since the later of `last_pass_bound` and the newest
     harvested van sweep (`van-scan-cache.json` `last_scan.completed_at`, wall clock):
     - same `boot_id`: by CLOCK_MONOTONIC (one clock shared by both processes in a boot);
     - different boot: by wall clock only if the record and the current clock are both
       synchronized and the result is not negative;
     - uncertain (no record, unreadable or corrupt record, `session_end` still null from a crash,
       different boot without a trustworthy clock): `elapsed = 0`, a full hour's wait;
  2. `due_in = clamp(3600 − elapsed, 0, 3600)`, `first_due = max(300, due_in)`;
  3. write the new record (temporary file, `fsync`, rename, directory `fsync`), carrying
     `last_pass_bound` forward, with a 2 s limit. If the write fails (read-only or full disk, any
     error) or does not finish in 2 s, launch the helper **without** the capability. A write that
     completes late leaves a record claiming a schedule that never ran, which only makes the next
     computation more conservative.
  4. launch with `--enable-drive-dtc --drive-dtc-origin <origin_monotonic> --drive-dtc-first-due
     <first_due>`.
- **Helper schedule**, fully determined by those arguments and CLOCK_MONOTONIC: pass k may start
  only within `[origin + first_due + k·3600, origin + first_due + k·3600 + window]` (k = 0, 1, …).
  A pass that cannot start in its window (gates, dirty watch, stale evidence) is skipped, not
  carried over.
- **At session end** the broker sets `session_end` and `last_pass_bound = min(session_end, t_k +
  window)` for the latest window start `t_k` at or before `session_end` (unchanged if the session
  ended before `t_0`), and rewrites the record without waiting for it. If that write is lost, the
  next launch sees `session_end` null and waits a full hour.
- The bound assumes every scheduled pass happened, so even if every helper event is lost there is
  never more than one pass per hour.
- Helper events are not part of the budget. A `dtc_pass` outside the scheduled windows (a code
  fault) makes the broker drop its results, stop the helper through the ordinary supervisor stop
  (its restoration and final event are processed as for any stop), and launch without the
  capability for the rest of its lifetime.
- Harvested sweeps are checked at launch only. A sweep imported after a launch is first seen at the
  next launch. The sentinel covers a sweep in progress; a sweep that ran before the session and was
  imported mid-session (the harvester runs every 20 minutes, up to about 30 minutes behind) can be
  followed by one Pi pass in that session. Both are stored separately and labelled by source.
- Rollback keeps the budget file.

### Accumulated driving hours (not implemented; for the owner's choice)

With the budget on the broker this is a change to the broker's computation only: keep
`running_seconds_since_last_pass` in the record, add each session's duration (origin to
`session_end`) at session end, and set `due_in = 3600 − running_seconds_since_last_pass` (still at
least 300 s into a session). Short trips then contribute; a lost `session_end` resets the
accumulation (assumes a pass happened), which is conservative. The helper is unchanged.

## Capability handshake

- Explicit broker-added arguments (the oil-life pattern): the broker adds `--enable-drive-dtc` and
  its two schedule arguments only when started with `--enable-drive-dtc-reads`, with no latch set and
  with a reserved budget.
- Positive confirmation in the startup exchange (new; oil life has none today): a helper launched
  with `--enable-drive-dtc` adds `"capabilities": ["drive_dtc_v1"]` to its `ready` event, and only
  then, because an old broker validates `ready` with the exact keys `type`, `protocol`, `pid`. A new
  broker that passed the argument requires the capability; if a valid `ready` lacks it, the broker
  does not grant (the existing cancel-before-grant path: the helper is reaped with a final of
  `restored: null`, no inhibit) and launches without the capability for the rest of its lifetime.
- Combinations:
  - old broker, new helper: no argument → the step is off; no new event type is ever emitted;
  - new broker without the flag, any helper: no argument → no DTC send, no sentinel socket;
  - new broker with the flag, old helper: the old helper rejects the unknown argument and exits
    before `ready`, which the supervisor treats as unverified restoration (as for oil life). It
    arises only if the checkout is reverted under a running flagged broker; the rollback order
    below prevents it.
- Rollback order: (1) remove `--enable-drive-dtc-reads` from the installed unit, `daemon-reload`,
  `s restart_broker` (the new broker, flag off); (2) revert the code; (3) `s restart_broker`. Never
  start an old broker while the unit still carries the flag: it cannot parse it and fails to start.

## Helper events

Every value the helper can emit is in the declared vocabulary (`drive_dtc.json`), and every event is
valid without any reservation or pass state, so no expected condition can be rejected by the broker.
The helper validates each event with the same contract function the broker uses before emitting it;
an event that would fail is not emitted, and the step disables itself for the epoch instead.

```json
{"type": "dtc_step", "state": "enabled|disabled",
 "reason": "capability|sentinel_unavailable|service_run_attempted|contract_error",
 "detail": "...", "interface_mode": "armed_diagnostic"}
{"type": "dtc_read", "pass": 0, "module": "radar_acc|bcm_ccan", "request": "19 02 8F",
 "outcome": "complete|truncated|negative|no_response|malformed|not_sent",
 "reason": "<NRC text, or for not_sent: evidence_stale|permit_expired|foreign_tester|service_latch|drain_failed|window_closed>",
 "nrc": "31|null", "pending_count": 0, "frame_hex": "<at most 8 bytes>|null",
 "count": 6, "started_at": "<UTC ISO>", "completed_at": "<UTC ISO>",
 "rpm": 1650.0, "speed_mph": 61.2, "interface_mode": "armed_diagnostic"}
```

- `pass` is the schedule index k. `count` is present for `complete` (0 or 1) and `truncated` (from
  the FirstFrame length), otherwise null. `frame_hex` is the final answer frame as received (at
  most 8 bytes).
- The broker parses `frame_hex` itself. A frame it cannot parse is stored with the stored outcome
  `unparsed`; that is a storage value, not a helper value, and never a protocol error.
- Dispatch: `broker.handle_active_drive_event` hands `dtc_step` and `dtc_read` to
  `drive_dtc_control.py` before `validate_helper_status`. That handler only validates and enqueues
  (`put_nowait`); it never raises for a full queue or a storage error.
- No change to `ACTIVE_DRIVE_FAILURE_REASONS`, final events or `helper_events.py`.

## Storage

One JSON file, `tmp/vehicle_data/drive-dtc-results.json`, written only by one broker thread.

- Content: `schema` (1), `coverage` ("failed, pending, confirmed or warning-lamp codes only
  (19 02 8F)"), and the newest 200 passes' reads: per read the event fields plus the parsed code
  (`complete` with one code: code, status and `lib.dtc.status_semantics`) or the count and first code
  (`truncated`). This file is also what the dashboard reads.
- Bound: at most 200 passes × 2 reads, about 300 bytes each, about 120 KiB; the writer refuses to
  exceed 256 KiB (it drops the oldest passes first). Written by temporary file and rename, so the
  transient peak is two copies, at most 512 KiB. No database, so no journal or WAL. The budget record
  adds at most 2 KiB with its temporary file. Both lie under the existing
  `/home/pi/dev/obd-things/tmp` root budget (5120 MiB), so no new `ROOTFS_PATH_BUDGETS_MIB` entry.
- Retention semantics: a window of the newest 200 passes (about 200 hours of driving at one pass an
  hour). Nothing is derived across dropped passes.
- Failure: a full queue (64 items) or a write error drops the results and latches
  `drive_dtc_storage_failed` in the broker, which launches later helpers without the capability. The
  running helper continues its schedule, which the budget still limits to one pass an hour; its
  results are dropped. That is the whole effect of a storage failure.
- Because one thread rewrites the whole file from its in-memory state in arrival order, an older
  publication cannot overwrite a newer one.

## Dashboard: the three-source rule

Per module there are up to three kinds of successful observation:

- **parked** `19 02 FF` (all status bits; the existing cache);
- **van** `19 02 0D` (failed, pending, confirmed);
- **drive** `19 02 8F` (failed, failed this cycle, pending, confirmed, warning lamp), `complete` or
  `truncated`.

Algorithm, per module:

1. Successes and failed attempts are separate lists. Failed attempts never change codes; a source's
   failed attempt newer than that source's latest success is shown as its own line ("drive read
   failed (no response) at 14:05 MST").
2. Start from the latest parked success's returned records, each labelled with that read's date.
3. Apply the van and drive successes newer than the parked success in chronological order. A
   `complete` filtered read with mask M at time t:
   - a shown code it lists takes its status and time t;
   - a shown code it does not list, whose last known status has a bit in M, is moved to "not
     reported by the <source> read at t" (its covered bits were clear then);
   - a shown code whose last known status has no bit in M (for example `0x40` only) is left alone,
     still labelled with its own earlier date, so it is never implied current;
   - a code it lists that is not shown is added, labelled with t.
4. A `truncated` drive read changes no code. It shows a module line "<count> codes reported at t,
   list not retrieved; first <code>", and "count differs from the codes shown" when the number of
   shown codes with a bit in `8F` differs.
5. An empty `complete` filtered read reads "no failed, pending, confirmed or warning-lamp codes at
   t" (van: "no failed, pending or confirmed codes at t"); never "no DTCs". The module's
   `result_state`, `no_dtcs` and `absence_authoritative` come from parked reads only.
6. A truncated parked group (`groups_truncated` in the compact cache) keeps its "+N more" line;
   filtered reads act only on returned records, and counts change only for returned records.
7. Every filtered result shows its coverage text and its source and time in Mountain time.

The existing `mergeVanScan` compares the van read with the Pi's last attempt and sets `no_dtcs` and
`absence_authoritative` for van results. Both are fixed in their own behaviour commit, before the
drive source is added.

## PCM: what its later design needs (not designed here)

The owner named the PCM; it follows after the radar and BCM have run clean on several drives. Its
own plan and review must cover:

- the service: the PCM rejects UDS `19` (`7F 19 11`); its codes come from KWP2000 `18 00 FF 00`,
  answered `58 NN` then three bytes per code with KWP status semantics, so parsing, presentation and
  storage differ;
- framing: only `58 00` (a SingleFrame) has been seen on the bus. A segmented KWP DTC reply has not
  been observed; a stage-1a-style read would record a count; a full list needs the PCM's 8-byte
  zero-padded flow control (proven only for `1A 87`, 2026-08-30);
- endpoint: the PCM poller reads with no drain, and its 1 s duty/torque reads end the session on a
  stray frame, so the design must keep every PCM answer off that socket or give it a bounded drain
  (a change to the protected PCM path);
- a parked, owner-authorized validation first.

## Deployment

1. Land the commits with the flag off. The harvester recognises the new request from its next run;
   no broker passes the new argument.
2. `s restart_broker` (parked) to load the new broker, flag off. The status hashes are unchanged
   (`tests/broker_status_hashes.json`).
3. Activation: add `--enable-drive-dtc-reads` to `ExecStart` in the tracked
   `projects/vehicle_data/systemd/van-telemetry.service`; install it the way the vehicle-data README
   deployment section says (copy, `daemon-reload`); check
   `systemctl show -p KillMode,TimeoutStopUSec van-telemetry.service`; `s restart_broker` parked.
4. Off: the rollback order's step (1). Every later helper launches without the argument.
5. Rollback: the rollback order above. The budget file stays; the results file can stay.
6. First drive:
   - `ready` carries the capability; `dtc_step enabled`; two `dtc_read` events about 5 minutes after
     launch; final `restored: true`;
   - the C-CAN recording around the pass: one `18DA2AF1#0319028F00000000` and one
     `18DA40F1#0319028F00000000`, no frame from us after either, no other new identifier or payload;
   - the watch reports no foreign frame while only the Pi is on the bus (this confirms
     `MSG_DONTROUTE` on this kernel; if it is missing, every check is dirty and nothing is sent);
   - in the history around each read: no metric's next sample later than its normal slot plus about
     2.9 s; 0845 polling continues afterwards;
   - the results file and the dashboard show the radar's `complete` read and the BCM's `truncated`
     read with coverage, source and time;
   - the budget record has `session_end` set after the drive; on a drive over 65 minutes a second pass
     about an hour after the first; after a stop under an hour, no pass until the hour has elapsed.
   - "Several clean drives" before stage 1b or more modules: at least five passes on at least three
     drives meeting every check, and no new code in either module's following reads.

## Tests

- `tests/drive_dtc_support.py`: a scripted module ECU (SingleFrame, FirstFrame, NRCs, 0–3 pendings,
  silence, malformed frames, late answers), a scripted watch feed (local/foreign flags, truncation,
  overflow, errors, bound reached), reusing `Clock`, `FakeAuthority` and `Sink` from
  `tests/radar_service_support.py`.
- Transport: exact frame per module; never any other frame (no flow control under any input); the
  2.5 s deadline; `not_sent` before the send versus outcomes after it.
- Step: schedule windows from the arguments; one module per cycle and 2 s spacing; the radar read
  only with the next 0845 at least 5 s away; the service latch (`run_attempted`, `pending()`,
  rechecked before the BCM); the watch drained every cycle and checked before each send; D within
  bound; every emitted event passes the contract validator.
- Helper (`test_active_drive.py`): without the argument no DTC frame and no new socket (scan every
  sent frame and socket); `ready` carries the capability only with the argument.
- Broker: budget computation (same boot, other boot synchronized and not, corrupt record, missing
  `session_end`, harvested sweep newer than the bound), the write timeout and failure paths (launch
  without capability), session-end update, the missing-capability cancel path, the out-of-window
  pass stop, event handling that never raises, storage failure latch, results-file bound.
- `test_transmit_permit`: the two purposes.
- Dashboard Node tests: the seven rules above, the `mergeVanScan` fixes.
- Harvester: sync tests for `19 02 8F`, `22 21 85` and the radar service requests.
- Oracles: `broker_pure_oracle` status hashes unchanged; `gate_oracle` byte-identical; every tidying
  commit compared old/new by output.
- Whole suite before every commit that leaves the worktree and before the review:
  `/home/pi/van_compute/scripts/pi_compute.py run repo-full-suite --wait 60`. As of 2026-11-15 it is
  red on master: `tests/test_notes_gate.py::test_no_test_module_names_a_note` runs `git ls-files`
  in a compute bundle without `.git`. That must be fixed before this work can commit.

## Code shape

New modules, one concern each, with their AGENTS.md map entries in the same commit:

- `projects/vehicle_data/drive_dtc_contract.py` + `drive_dtc.json`: frames, purposes, timing
  constants, schedule rule, event vocabulary and validator.
- `projects/vehicle_data/drive_dtc_transport.py`: the only code that builds or sends the two frames.
- `projects/vehicle_data/drive_dtc_watch.py`: the foreign-tester watch.
- `projects/vehicle_data/drive_dtc_steps.py`: the per-cycle step.
- `projects/vehicle_data/drive_dtc_budget.py`: the broker's budget record and computation.
- `projects/vehicle_data/drive_dtc_control.py`: broker side (arguments, capability check, dispatch,
  writer thread, latches).
- A sibling of `insights.py` for the `drive_reads` block; `dashboard/src/views/dtcSources.helpers.js`
  (the three-source merge, moved out of `health.helpers.js`).
- `projects/vehicle_data/van_scan_pi_requests.py` (the harvester's Pi request tables).

Extended in place: `transmit_permit.py` (two purposes), `radar_service_alignment.py`
(`run_attempted`, `CommandReader.pending`), `helper_startup_grant.py` (the optional `capabilities`
key in `ready`), `ccan_powertrain.py` (the speed reader promoted from
`radar_service_alignment._speed_mph`).

Over-limit files gain no net lines: `active_drive.py` / `run_active_session` (the oil-life block
moves to a sibling step first), `broker.py` (`build_parser` moves to `broker_cli.py` first),
`insights.py` (the van-scan cache validator moves out), `van_scan_harvest.py` (request tables move
out). `lib/dtc.py` and `radar_alignment.py` are not changed. The vehicle-data README frame list
gains the two frames in the commit that wires the step.

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
9. Contract, vocabulary, transmit-permit purposes, transport, watch (library only).
10. `run_attempted`, `CommandReader.pending`, the optional `ready` capability key (inert).
11. Step module and helper wiring behind `--enable-drive-dtc` (inert: no broker passes it).
12. Broker: budget, control, writer, capability check, the flag (off by default), the
    `drive_reads` block and dashboard display; README frame list and section.
13. Activation: the unit flag (after the review and the parked deploy of 1–12).

## Review findings and where this plan answers them

First review (of the first plan):

| # | Finding | Position | Where |
| --- | --- | --- | --- |
| 1 | Foreign-tester send-time gap | Accepted | Foreign-tester watch |
| 2 | Service inactivity is not ECU inactivity | Accepted (resolved) | Radar service alignment |
| 3 | Blocking reads; late replies on shared endpoints | Accepted | The exchange; Timing |
| 4 | Migration backup and rollback | Accepted (resolved: no migration) | Storage |
| 5 | Migration activation; new-broker/old-helper | Accepted | Storage; Capability handshake |
| 6 | Retention | Accepted | Storage |
| 7 | Budget durability; harvest refresh | Accepted in part: durability on the broker; harvest checked at launch only (see below) | Cooldown budget |
| 8 | Coverage and source precedence | Accepted | Dashboard |
| 9 | First-drive scope; FC form; wording | Accepted (resolved) | Settled decision 3; Staging |
| 10 | Classifier prerequisite; build order | Accepted (resolved) | Build order |

Second review (of `8031e8d`):

| # | Finding | Position | Where |
| --- | --- | --- | --- |
| 1 | Helper-side `fsync` can stall into an inhibit | Accepted; the helper writes nothing (the reviewer's asynchronous-helper remedy replaced by the parent's broker-side reservation) | Cooldown budget |
| 2 | Service-command delay past its 15 s deadline | Accepted; intake is never paused, the radar window is at most about 2.9 s, latch rechecked before the BCM | Radar service alignment; Timing |
| 3 | Watch maintenance and attribution | Accepted | Foreign-tester watch |
| 4 | Timing, permit lifetime, radar queue, timestamps | Accepted; no flow control in 1a, one clock, the radar socket shared, bounds derived from the loop | Staging; The exchange; Timing |
| 5 | Environment handshake | Accepted; explicit argument, confirmation in `ready`, rollback order | Capability handshake |
| 6 | Cooldown recovery; storage shutdown | Accepted | Cooldown budget; Storage |
| 7 | Expected failures as protocol errors | Accepted | Helper events |
| 8 | Three-source merge | Accepted | Dashboard |
| 9 | Storage bound and gap wording | Accepted | Storage; Deployment step 6 |

Departures, stated:

- **Harvest eligibility is checked at launch, not before each pass** (first review, finding 7, and
  an earlier parent decision). A check before each pass needs the helper to read a file or a
  broker-to-helper channel; the schedule fixed at launch needs neither. The cost: a van sweep
  imported between a launch and that session's pass can be followed by one Pi pass in that session.
  The van's client only sweeps while Pi F1 traffic is quiet, so such a sweep ran before the session.
- **Positive capability confirmation is new**, not the oil-life mechanism: oil life has a
  broker-added argument and no confirmation.

## Counts against the previous revision (8031e8d)

- Transmit paths: 2 fixed frames (one request per module), down from 4 (two requests and two flow
  controls) on per-module sockets plus a sliced transaction state machine.
- New helper paths to a protocol error, unverified restoration or inhibit under expected conditions:
  none, down from four (a helper `fsync` stall past the silence limit, a `disabled` event rejected
  without a reservation, `unparsed` outside the vocabulary, and the environment handshake enabling
  the step under an old broker).
- What remains:
  - an unexpected exception in the step ends the session as `helper_failed` with verified
    restoration (the existing path for every step);
  - two degrading residuals with no inhibit: a very late radar answer can stop 0845 polling for the
    epoch, or fail a service start begun within seconds of a radar read;
  - new broker plus old helper, procedure-guarded by the rollback order (as for oil life).

## Open questions and risks

1. **Elapsed cooldown or accumulated driving hours**: with the parent, for the owner; both costs
   above.
2. **Whether the radar or the BCM logs anything after an unanswered FirstFrame**: unknown; checked on
   the first drives.
3. **The first `8F` request** to these modules: a `7F 19 12`/`31` is stored as `negative`; the next
   step is a review, not a mask change in the field.
4. **`MSG_DONTROUTE` through Python's `recvmsg` on this kernel**: documented, not yet observed here;
   if absent, nothing is sent (visible on the first drive).
5. **Stage 1a's BCM result is a count and first code.** Its six expected codes are the owner's
   known LED-lamp codes; a change in the count is the signal. Stage 1b retrieves the list.
6. **The Pi's continuous F1 polling suppresses the van's own health check**: unchanged; for the
   owner.
7. **Red suite on master** (`test_notes_gate.py` in compute bundles): blocks committing this work.

Not determinable from the repository: what triggers the van's own sweep; `8F` reply sizes and
pending behaviour at speed for these two modules.

## Status

Third revision awaiting cross-family review. Nothing built; no CAN traffic, service change or live
write. The read-only queries used `mode=ro` on `tmp/vehicle_data/dtc-history.sqlite3` and the
archived 2026-09-24 capture on `/mnt/EXFAT512`.
