---
workstream: drive-dtc-read
status: plan awaiting review (second revision)
tier: T3
last reviewed commit: none (first plan reviewed as "rework"; this revision not yet reviewed)
---

# Hourly DTC read during drives: plan

## Objective

Owner request (2026-11-15): "query error codes once per hour of driving if there is no scan
harvest", for modules "such as the PCM BCM DASM etc". The cluster shows latched ACC/radar
warnings, and nothing on the Pi has read a module's fault codes since the parked 15-module batch on
2026-10-08 (03:12–03:13 MDT). The van's own F1 sweeps were harvested only twice (2026-08-30
20:38 MDT and 2026-09-24 15:12 MDT) and are not expected while the Pi polls on every drive.

Tier T3: new CAN transmission while the van is driven, new broker/helper contracts, and it cannot
be verified by running it in a development session. Build in the worktree branch
`worktree-agent-ace5b03e8cf37934c`; a cross-family review comes before any merge, install or
deploy.

## Settled decisions

These were settled by the parent session on the owner's behalf (2026-11-15).

1. **Mechanism.** The Pi reads codes itself, inside the C-CAN active-drive helper, with fresh
   engine-running evidence and one-use transmit permits; physical 29-bit addressing, default
   session, one module at a time. Only UDS `19 02 8F` and its flow control are sent; never `14`,
   `10`, `27`, `2E`, `2F`, `31` or `3E`.
2. **Mask `19 02 8F`** (warningIndicatorRequested | confirmed | pending |
   testFailedThisOperationCycle | testFailed). Reasons: `FF` returns the "test not completed" bits
   that change through every drive (on Oct 8, 278 of 300 records were status `0x40` only), and
   `0D` is the harvester's signature for the van's own sweep. On the Oct 8 data `8F` keeps all 22
   fault-relevant records, including the warning-lamp bit the latched cluster warnings depend on.
3. **First cut: the radar (DASM) and the BCM only**, each with the flow-control form already proven
   on that module while driving. Cluster, RF Hub, TCM, shifter and telematics wait until those two
   have run clean on several drives. The PCM is the next phase after that (below: what its design
   needs). B-CAN modules and the CAN-CH modules (ABS, EPS, HALF, ORC) stay parked-only (the
   existing batch).
4. **Storage.** The parked history (`module_scans`, `dtc_state`, `dtc_transitions`, the v2 cache
   and its `request_hex` CHECK) does not change. Drive reads go into their own separately
   versioned, bounded storage, and the dashboard merges them per module with their coverage
   shown.
5. **Cadence is an elapsed-time cooldown.** A pass is allowed during a qualified engine-running
   interval once the helper session is 300 s old, when at least 60 minutes have passed since the
   last pass started and since the last harvested van sweep completed. This is elapsed time, not
   accumulated driving time: a 20-minute drive followed by a 45-minute stop and another 20-minute
   drive reads once in each drive, and trips under five minutes never read (section "Accumulated
   driving hours" says what the other reading of "once per hour of driving" would take; the parent
   is putting that choice to the owner). Engine running while parked counts as a qualified
   interval.
6. **Opt-in and off switch.** Broker CLI flag `--enable-drive-dtc-reads`, off by default. Without
   it the broker never enables the helper's DTC step, so removing the flag disables every new send.
   The broker/helper handshake makes every mix of old and new broker and helper harmless.

## Evidence (read-only, 2026-11-15)

- Oct 8 parked `19 02 FF` history (counts confirmed independently by the parent): 300
  observations across 13 modules, 278 status `0x40` only, 22 matching `8F`. The radar had 8
  records, one matching `8F` (status `0x08`); the BCM had 106, six matching `8F`.
- The van's own sweep while driving (2026-09-24, finding `2026-09-24_in_vehicle_f1_scan.md` and
  its exchanges CSV; frames checked in the archived capture):
  - requests `18DAxxF1#0319020D00000000` (DLC 8, `00` padding);
  - radar `0x2A`: `19 02 0D` answered with a 7-byte SingleFrame after a response-pending
    (`7F 19 78`) for 143 ms; its 67-byte `22 F1A0` reply completed after the tester's unpadded
    FlowControl `18DA2AF1#300000` (DLC 3);
  - BCM `0x40`: `19 02 0D` answered with a 27-byte reply (FirstFrame 3.1 ms after the request,
    three ConsecutiveFrames) after `18DA40F1#300000` (DLC 3); no response-pending;
  - PCM: `19 02 0D` → `7F 19 11` (serviceNotSupported); KWP `18 00 FF 00` → `58 00`.
- The Pi's own drive polling to the radar uses `22 08 45` with FlowControl
  `18DA2AF1#3001000000000000` (padded, BlockSize 1), proven for a one-ConsecutiveFrame reply.
- Helper structure (verified in code): one thread runs the cycle serially (`active_drive.py`
  `run_active_session`): broadcast snapshot (≤0.35 s), PCM duty and torque, RF Hub pressure, VVT
  temperature every 5 s, oil life every 60 s, then `RadarDriveSteps.run` (0845 every 10 s and any
  owner-started service alignment), then a wait to the next 1 s slot.
  - `PcmElectricalPoller` reads the first frame on its socket with no drain; a stray frame ends the
    session through the generator-duty failure path.
  - `RfHubPressurePoller` drains before each read, but a frame that arrives during its reply wait
    is taken as the reply; a malformed one ends the session (`active_drive.py`, the
    `pressure.reason` branch).
  - `RadarAlignmentPoller.drain` reads at most 32 frames and raises beyond that; a malformed 0845
    reply disables 0845 polling for the epoch (`radar_drive_steps.py`).
- Radar service alignment (`radar_service_alignment.py`): a start that times out goes to `_fail`
  and clears the active phase although the ECU may have accepted it; a completed routine leaves the
  extended session to lapse on its own. "No active phase" therefore does not prove the radar is
  back in its default state.
- The bus-identity classifier already uses response-only CAN-CH diagnostic signatures
  (`lib/canbus.py` `CANCH_DIAG_SIG`, commit `2fe75a4`).
- Harvester: `PI_DTC_TOOL_REQUESTS` excludes `19 02 FF` for any target and deliberately not
  `19 02 0D`; `PI_FIXED_REQUESTS` lacks PCM `22 21 85` and the radar service requests.
- File sizes: `broker.py` 3624, `active_drive.py` 1176, `insights.py` 1562, `lib/dtc.py` 1147,
  `van_scan_harvest.py` 1050 lines; `run_active_session` is far over 80 lines.

## What goes on the bus

All new frames are in one module, `drive_dtc_transport.py`, each behind its own one-use permit:

| module | request (29-bit, DLC 8) | FlowControl, only after a valid FirstFrame |
| --- | --- | --- |
| `radar_acc` (DASM) | `18DA2AF1#0319028F00000000` | `18DA2AF1#300000` (DLC 3) |
| `bcm_ccan` | `18DA40F1#0319028F00000000` | `18DA40F1#300000` (DLC 3) |

- The request has the framing of the van's own driving sweep; only the mask byte differs (`8F`
  for `0D`). ISO 14229 defines every mask value; `8F` itself has not been sent to these modules
  before, so the first drive is its first use.
- The FlowControl is ContinueToSend, BlockSize 0, STmin 0, unpadded DLC 3: the exact form the van's
  tester used on both modules while driving. BlockSize 0 means one FlowControl per reply. No
  padded form is used; the PCM's padding requirement is not carried over to other modules.
- Expected replies from the Oct 8 statuses: radar one record (7 bytes, a SingleFrame, so normally
  no FlowControl); BCM six records (27 bytes: FirstFrame, FlowControl, three ConsecutiveFrames).
- Accepted reply length: radar at most 163 bytes (40 records, a FirstFrame and at most 23
  ConsecutiveFrames, which keeps everything the 0845 poller's socket also receives under its
  32-frame drain); BCM at most 515 bytes (128 records). A longer declared length gets no
  FlowControl and is recorded `incomplete`.
- Bus occupancy: a pass is about 10 frames (radar: request, one to three SingleFrames; BCM:
  request, FirstFrame, FlowControl, three ConsecutiveFrames). A 29-bit frame with 8 data bytes is
  roughly 130–160 bits with stuffing, about 0.3 ms at 500 kbit/s, so a pass occupies the bus for
  about 3–4 ms per hour, with the BCM's ConsecutiveFrames sent back to back for about 1 ms. While
  one of these frames is on the bus, any other frame that becomes ready, including powertrain
  broadcasts, waits for it to finish (up to one frame time); arbitration only decides which waiting
  frame goes next. For comparison, the helper already sends four to six requests a second.

## Reception, timing and endpoint ownership

Reception is a small state machine that advances in bounded slices, never in one long blocking
wait.

- **Sockets.** Each module transaction opens its own raw `CAN_RAW` socket (exact EFF response ID
  `18DAF1tt`, RTR/ERR rejected, `SO_TIMESTAMPNS` on) and closes it when the transaction ends. No
  kernel ISO-TP, so nothing is sent automatically.
- **Request slice.** A request is sent only on a cycle where at least 0.30 s remain before the next
  1 s slot after the cycle's other work. Its permits come from `FreshAuthority.acquire` (a fresh
  broadcast snapshot of up to 0.35 s, three RPM samples ≥ 400, identity, ERROR-ACTIVE, topology,
  inhibits). Then the sentinel check (below), the request, and a receive slice of at most 0.25 s.
- **Later slices.** If the transaction is still open, each later cycle gives it one receive slice of
  at most 0.25 s, after the radar steps, clamped to the time left before the next slot (no slice
  when under 0.10 s remains) and to the transaction deadline.
- **Deadlines** (measured on frame receive timestamps): first response 1.0 s after the request; each
  `7F 19 78` sets a new 2.0 s deadline, at most two; after the FlowControl, each ConsecutiveFrame
  within 1.0 s; hard cap 6.0 s from the request.
- **FlowControl.** Sent only (a) after a FirstFrame `1L LL 59 02 …` with a length within the
  module's bound and (length − 3) % 4 == 0, (b) within 0.5 s of that FirstFrame's receive
  timestamp (the ECU waits about 1 s for it), (c) with the FlowControl permit issued alongside the
  request and still unexpired, and (d) after a clean sentinel check. If any of these fails, no
  FlowControl is sent and the read is `incomplete`; the ECU abandons the reply on its own timer.
- **Outcomes.** `positive` (complete `59 02`); `negative` (any NRC other than `78`; `7E`/`7F`
  are labelled `session_required` and never lead to a session change); `no_response`; `incomplete`
  (a FirstFrame without the FlowControl, or ConsecutiveFrames missing or out of sequence);
  `malformed` (another service, a bad length, a FirstFrame that is not `59 02`); `skipped` (the
  request permit expired, or evidence was stale, **before** the request was sent: nothing went on
  the bus; retried next cycle, at most three times). A FlowControl permit that expires **after** the
  request was sent is `incomplete`, never `skipped`, and never retried.
- **Quiet guard and late replies.** When a transaction ends, its endpoint stays owned until the
  socket has seen 2.0 s without a frame from that module, up to 12 s from the request. Then the
  socket closes.
  - BCM: nothing else in the helper listens to `18DAF140`, so a late frame reaches no consumer.
  - Radar: the 0845 poller's socket also receives every radar reply frame. While the DTC step owns
    the radar endpoint (from the cycle before the request to the end of the quiet guard),
    `RadarDriveSteps.run` does neither the 0845 poll nor the service-alignment cycle; queued service
    commands wait in their pipe (at most 12 s, below the broker's 15 s request acknowledgement
    limit). The 0845 poller's own bounded drain removes the queued DTC frames at its next poll (at
    most 27 frames by the length bound). Residual: if the radar sends a frame after 2 s of silence
    and it lands inside a later 0845 reply wait, that read is malformed and 0845 polling stops for
    the rest of the epoch (the existing handling). If the radar is still sending at 12 s, the step
    releases the endpoint and no further radar DTC read happens that epoch.
- **Effect on the 1 s polling.** The PCM and RF Hub reads keep their place at the start of every
  cycle. A request cycle adds at most 0.35 s (the fresh snapshot) plus 0.25 s; a later cycle adds
  at most 0.25 s. The worst gap between PCM reads is therefore about 1.6 s, once per module per
  hour. The supervisor's 10 s silence limit is unaffected because the cycle's ordinary observations
  are still emitted every cycle.
- **Pass shape.** One module at a time, radar first, then the BCM; each module transaction
  completes (including its quiet guard) before the next request; at least 2.0 s between requests;
  pass deadline 60 s. A typical pass takes 10–20 s.
- Parsing reuses `lib.dtc.parse_dtc_list_response` (strict); a reply it rejects is stored as
  `unparsed`.

## Foreign-tester check

A sentinel `CAN_RAW` socket on the C-CAN channel, opened when the DTC step is constructed, with
filters for every diagnostic request a tester could send:

- 29-bit physical and functional requests from any tester-range source:
  `0x18DA00F0`/`0x18DB00F0` with mask `0x1FFF00F0` (source `F0`–`FF`, which includes our `F1`);
- 11-bit OBD requests: `0x7DF` and `0x7E0`–`0x7E7`.

It is read with `recvmsg` (`SO_RXQ_OVFL` on). A frame counts as ours only if it carries
`MSG_DONTROUTE` (created on this host) **and** is exactly one of the frames this helper can send (the
PCM, RF Hub, 0845, service-alignment and DTC frames, compared by ID and data). Anything else is
foreign: a frame without `MSG_DONTROUTE` (the van's own client, AlfaOBD on the Y-splitter, an OBD
dongle) or a local frame that is not one of the helper's (a manual `cansend`).

- The check runs immediately before **every** request and **every** FlowControl: drain the sentinel
  non-blocking, at most 256 frames. It fails closed (treated as foreign) on any receive error, a
  truncated frame or truncated metadata (`MSG_TRUNC`/`MSG_CTRUNC`), a changed overflow counter, or
  an exhausted drain budget.
- A pass starts only if the sentinel has been open for at least 120 s with no foreign frame. A
  foreign frame at any check abandons the pass (`foreign_tester`): no further request, and no
  FlowControl for a reply in progress.
- If the sentinel cannot open, the DTC step is disabled for the epoch.
- Residual race: a foreign tester can still start in the moment between our last check and our
  frame reaching the bus, or while we wait for a reply. Both testers then see replies on the same
  `18DAF1tt` IDs. Our read may then be malformed, or (only if the foreign tester sent the same
  module the same service at the same instant) accept a reply meant for it; the foreign tester may
  likewise see ours. Nothing we send changes ECU state, and the next check abandons the pass.

## Radar service alignment

`ServiceAlignment` gains a read-only `run_attempted` property, true once any start command has
created a run record in this helper epoch (including runs refused as moving or unsupported, and
runs that failed or timed out ambiguously). Once it is true, the DTC step reads nothing for the rest
of the epoch, whatever the run's outcome, because a timed-out start or a completed routine can
leave the radar in its extended session or routine with no active phase in the helper. A new epoch
(the helper restarts at the next engine-running interval) starts clean.

## Scheduling and the durable budget

The helper owns the budget, because it is the process about to send.

- **Budget file** `tmp/vehicle_data/drive-dtc-budget.json`: one record, rewritten in place,
  at most 1 KiB: `pass_id`, `reserved_at` (UTC), `boot_id` (`/proc/sys/kernel/random/boot_id`),
  `reserved_boottime` (`CLOCK_BOOTTIME` seconds), `clock_synchronized` (from `adjtimex`, not
  `STA_UNSYNC`).
- **Reservation before sending.** Before a pass's first request the helper writes the new record to
  a temporary file, `fsync`s it, renames it over the old one and `fsync`s the directory. Only when
  every step succeeds does it emit `dtc_pass reserved` and proceed. Any failure disables DTC reads
  for the epoch (event `dtc_pass` `disabled`, reason `budget_not_persisted`). The acknowledgement
  is the sending process's own completed `fsync`, so no cross-process handshake can be lost
  between reservation and send.
- **Eligibility, evaluated before every pass** (not only at launch):
  1. the DTC step is enabled for this epoch (handshake, sentinel open, no service run attempted,
     storage not disabled by the broker);
  2. the session is at least 300 s old (monotonic);
  3. the last reservation is at least 3600 s old: same `boot_id` → measured with `CLOCK_BOOTTIME`
     (immune to wall-clock jumps); different boot → measured by wall clock only if both the record
     and the current clock are synchronized, otherwise the pass waits until this helper session is
     3600 s old (a conservative reboot policy; with no record at all, only the 300 s settle
     applies);
  4. the latest harvested van sweep (`van-scan-cache.json` `last_scan.completed_at`, read fresh,
     size-capped at 256 KiB and validated by a small pure parser) completed at least 3600 s ago by
     a synchronized wall clock; a missing or invalid cache counts as no sweep;
  5. the sentinel has been clean for 120 s and no module transaction is open.
- **Within a session**, later passes follow the same rule, so a long drive reads about every 60
  minutes.
- **Harvester lag.** The harvester imports sweeps from completed recording chunks every 20 minutes,
  up to about 30 minutes behind. A van sweep in that gap is not yet in the cache; the sentinel covers
  a sweep in progress. A sweep that ran and ended while no helper was running (the van's client
  waits for Pi F1 traffic to be quiet) and is not imported yet can be followed by one Pi pass; both
  are stored separately and labelled by source.
- **Broker cross-check.** The broker records the monotonic time of every `dtc_pass reserved` event
  it receives. A second reservation less than 3595 s after the previous one within the same broker
  lifetime is a cadence violation: the broker stops that helper through the ordinary supervisor
  stop (the helper's restoration and final event are processed exactly as for any stop), records
  nothing more from it, and does not enable the DTC step for the rest of its lifetime.

### Accumulated driving hours (not implemented; for the owner's choice)

Reading "once per hour of driving" as accumulated qualified engine-running time would need: a
persisted counter of running seconds since the last pass (in the budget file), incremented by the
helper during each session and written durably at least once a minute and at session end; a pass
when the counter reaches 3600 s (plus the settle period in the current session); reset at
reservation. Short trips would then contribute; a crash would lose at most about a minute of
counted time; the extra cost is one small `fsync`ed write per minute of driving on the SD card.
The rest of this plan (transport, gates, storage, dashboard) is unchanged by that choice.

## Handshake and the off switch

- The broker enables the step through the helper's **environment**, not its argument list:
  `VAN_TELEMETRY_DRIVE_DTC=drive-dtc-v1:<broker pid>`. The new helper enables the DTC step only when
  the value has exactly its own protocol version and the PID equals its required
  `--expected-parent-pid`.
- The new broker always builds the child environment explicitly: it removes the variable when the
  flag is off and sets it only when the flag is on, so a value inherited from the unit's
  environment can never enable the step.
- Combinations:
  - old broker, new helper: no variable (or one carrying another PID) → the step is off; no new
    event type is ever emitted to a broker that does not know it;
  - new broker, old helper: the old helper ignores an unknown environment variable → it runs exactly
    as before, sends nothing new and never fails to start (an argument-list flag would have made it
    exit with an argparse error at every launch);
  - new broker with the flag removed: the variable is removed → the step is never constructed, and
    the transport and sentinel sockets are never opened.
- No `ACTIVE_DRIVE_FAILURE_REASONS` or final-event change; `helper_events.py` is unchanged.

## Gates and stopping

A request is sent only when: eligibility (above) holds for the pass; the module's endpoint is owned
by the DTC step; `FreshAuthority.acquire` succeeds (otherwise `skipped`, or `SessionEnding` ends the
session exactly as today); the sentinel check is clean; the permit is unexpired at the send.

A pass stops early on: `SessionEnding` (engine stopped, traffic gone, adapter/topology/inhibit
change), a foreign tester, a service-alignment command (run attempted), a send `OSError`
(`transport_error`; the session's own gates handle the adapter), the 60 s pass deadline, or helper
termination. A `TransmitPermitError` other than expiry stays fatal (`helper_failed`), as for every
other transport.

Restoration is unchanged: the DTC step's sockets (transaction and sentinel) close in the session's
`finally` before restoration. Worst case, the helper is killed mid-transaction: the ECU abandons
the reply on its own ISO-TP timer; the read changed no ECU state (default session, read service);
the interface follows today's rule (an unverified restoration sets the wildcard inhibit and the
broker latch). The budget was reserved before the request, so the pass counts.

## Storage

A separate SQLite file, `tmp/vehicle_data/drive-dtc.sqlite3`, written only by the broker. Reasons
for a separate file rather than additive tables in `dtc-history.sqlite3`: no shared WAL, schema
check or writer with the parked history, the harvester or the parked batch; rollback is deleting the
file; its bound is enforced independently.

- Schema (version 1, key `drive_dtc_schema_version` in its own `metadata` table), created only with
  `CREATE TABLE IF NOT EXISTS`, so opening it is additive and idempotent; a higher stored version
  makes the opener refuse (fail closed) without changing anything. Tables:
  - `drive_passes` (`pass_id` primary key, `reserved_at`, `finished_at`, `state`, `reason`,
    `boot_id`);
  - `drive_module_reads` (`id`, `pass_id` → `drive_passes` with cascade, `module_key`,
    `request_hex` CHECK `= '19 02 8F'`, `started_at`, `completed_at`, `outcome` CHECK in the
    vocabulary, `nrc`, `pending_count`, `status_availability_mask`, `dtc_count`, `response_hex`,
    `rpm`, `speed_mph`);
  - `drive_dtc_observations` (`read_id` → `drive_module_reads` with cascade, `raw_dtc`, `status`).
- Retention is a plain observation window: after each pass, delete passes beyond the newest 500 or
  older than 90 days (cascading to their reads and observations). Nothing is derived across deleted
  rows; "first seen" and counts are within the window only. Bound: 500 passes × 2 modules, each read
  at most about 1.6 KB of text, about 2 MiB with indexes; inside the existing
  `/home/pi/dev/obd-things/tmp` root budget (5120 MiB), so no new `ROOTFS_PATH_BUDGETS_MIB` entry.
- Writer: one broker thread with a bounded queue (64 items) processes events in arrival order:
  insert, prune, then regenerate the dashboard cache `tmp/vehicle_data/drive-dtc-cache.json`
  (schema 1, at most 64 KiB, atomic replace) from the database. Because one thread regenerates the
  whole cache from the database each time, an older publication cannot overwrite a newer one. A
  queue overflow or a write failure latches `drive_dtc_storage_failed` in the broker: it stops
  enabling the DTC step at later launches (no reads whose results cannot be stored) and reports the
  latch in its journal.
- Cache content per module: the latest attempt (time, outcome, reason) and, separately, the latest
  successful read (time, codes with status semantics from `lib.dtc.status_semantics`, the
  availability mask), the coverage text "failed, pending, confirmed or warning-lamp codes only
  (19 02 8F)", and the last pass summary.

## Helper events and broker handling

```json
{"type": "dtc_pass", "pass_id": "<32 hex>",
 "state": "reserved|completed|abandoned|disabled", "reason": "<vocabulary>",
 "detail": "...", "modules": ["radar_acc", "bcm_ccan"], "modules_read": 0,
 "interface_mode": "armed_diagnostic"}
{"type": "dtc_read", "pass_id": "<32 hex>", "module": "bcm_ccan", "request": "19 02 8F",
 "outcome": "positive|negative|no_response|incomplete|malformed|skipped",
 "nrc": "31|null", "pending_count": 0, "response_hex": "59 02 4F ...|null",
 "started_at": "<UTC ISO>", "completed_at": "<UTC ISO>", "rpm": 1650.0, "speed_mph": 61.2,
 "detail": "...", "interface_mode": "armed_diagnostic"}
```

- Validation in the pure contract module (`drive_dtc_contract.py`, vocabulary shared through
  `drive_dtc.json`): exact key sets; module in the first-cut tuple; request equal to the constant;
  outcome and reason in the vocabulary; `response_hex` present only for `positive` and within the
  module's length bound; `pass_id` matching the reserved pass; timestamps within ±120 s of receipt
  (otherwise the receipt time is stored and the difference noted). A violation is a protocol error,
  handled like any other helper protocol error.
- Dispatch: `broker.handle_active_drive_event` passes the two new types to `drive_dtc_control.py`
  before `validate_helper_status`. The broker accepts them only from a helper it launched with the
  variable set.
- `skipped` reads are stored as attempts that sent nothing (outcome `skipped`), so the history shows
  them but the dashboard never counts them as failures of the module.

## Dashboard

`/v1/diagnostics/dtcs` gains a `drive_reads` block from `drive-dtc-cache.json` (read by a new
sibling of `insights.py`; absent or invalid → omitted, the rest of the response unchanged). The
merge per module follows the van-sweep pattern, generalised:

- Successful observations and failed attempts are compared separately. Codes come from the newest
  **successful** read of each source; a failed attempt never hides another source's newer success.
  A failed drive attempt newer than every success shows as its own line ("drive read failed
  (reason) at <time>").
- A filtered read (`8F` from the Pi while driving, `0D` from the van's sweep) replaces the parked
  records only within its coverage and keeps the parked records outside it (for example "test not
  completed" records), as `mergeVanScan` already does for `0D`.
- Coverage is shown explicitly on each filtered result: "failed, pending, confirmed or warning-lamp
  codes only". An empty `8F` reply reads "none of those codes in the last drive read", never "no
  DTCs" for the module, and never changes the module's parked `no_dtcs` state.
- Times are shown in Mountain time with the source ("read while driving").
- The existing `mergeVanScan` compares the van's observation with the Pi's last **attempt**, so a
  failed Pi attempt can hide a newer successful van observation. That fix (compare successes) is a
  behaviour change in its own commit.

## Harvester

- `19 02 8F` is added to `PI_DTC_TOOL_REQUESTS` (any target), so our pass counts as Pi activity and
  never as a van sweep or foreign traffic; our FlowControls are already skipped by its reassembly.
- `22 21 85` (oil life) is added to `PI_FIXED_REQUESTS[0x10]`, and the exact radar service-alignment
  requests (`10 03`, `31 01 0251`, `31 02 0251`, `31 03 0251`, `3E 00`) to `PI_FIXED_REQUESTS[0x2A]`,
  each in its own commit; the sync tests take the bytes from the helper modules.
- `van_scan_harvest.py` is over 800 lines, so a tidying commit first moves the two request tables
  into a sibling module (`van_scan_pi_requests.py`), re-exported unchanged.

## PCM: what its later design needs (not designed here)

The owner named the PCM; it comes after the radar and BCM have run clean on several drives. Its
own plan and review must cover:

- the service: the PCM rejects UDS `19` (`7F 19 11`); its DTCs come from KWP2000 `18 00 FF 00`,
  whose reply is `58 NN` then three bytes per code (two-byte code, status byte with KWP semantics,
  not UDS bits), so it needs its own parsing, status presentation and storage protocol field;
- framing: the only PCM KWP DTC reply seen from the bus is `58 00` (a SingleFrame). A segmented KWP
  DTC reply has not been observed or proven; AlfaOBD's July `58 03 …` is not a bus capture of the
  framing. The PCM needs an 8-byte zero-padded FlowControl (proven only for `1A 87`, 2026-08-30);
- endpoint ownership: the PCM poller reads with no drain and its 1 s duty/torque reads end the
  session on a stray frame, so the design must suspend those reads during the transaction or give
  the PCM poller a bounded drain (a change to the protected PCM path), and state the metric gap;
- a parked, owner-authorized validation of a segmented KWP DTC read before any drive use.

## Deployment

Each step is safe on its own because nothing sends until the broker flag is on.

1. Land the commits (flag off). The harvester's request recognition is live from its next run.
   Helper code on disk changes, but no broker sets the variable.
2. Guarded parked restart (`s restart_broker`) to load the new broker, flag still off. The broker
   status hashes are unchanged (`tests/broker_status_hashes.json`).
3. Activation commit: add `--enable-drive-dtc-reads` to `ExecStart` in the tracked
   `projects/vehicle_data/systemd/van-telemetry.service`; install it the way the vehicle-data README
   deployment section says (copy, `daemon-reload`); check
   `systemctl show -p KillMode,TimeoutStopUSec van-telemetry.service`; `s restart_broker` parked.
   The drive store and cache are created empty at the broker's first DTC event.
4. Off: remove the flag (edit the installed unit, or a drop-in that restates `ExecStart` without it),
   `daemon-reload`, then `s restart_broker` (parked) or `s restart_broker --while-running`. The
   restart ends any running helper; every later helper is launched without the variable.
5. Rollback of code: any broker/helper mix is harmless (Handshake), so revert and restart parked in
   either order. The parked history was never touched; the drive store, its cache and the budget
   file can stay or be deleted.
6. First drive, to call the first cut validated:
   - journal/events: `dtc_pass reserved` about 5 minutes after arming, two `dtc_read` events,
     `dtc_pass completed`, final `restored: true`;
   - the C-CAN drive recording around the pass: one `18DA2AF1#0319028F00000000`, one
     `18DA40F1#0319028F00000000`, at most one `18DA40F1#300000` (after a `59 02` FirstFrame) and no
     radar FlowControl unless the radar's reply is segmented; no other new identifier or payload;
     the sentinel reports no foreign frame while only the Pi is on the bus (this also confirms
     `MSG_DONTROUTE` on this kernel);
   - the PCM duty/torque, RF Hub, oil-life and radar 0845 metrics have no gap over 2 s around the
     pass, and 0845 polling continues after it;
   - `drive-dtc.sqlite3` rows and the dashboard showing both modules with coverage and source;
   - the budget file holds the reservation; the next harvester report has no `other_f1_traffic`
     window for the pass (or for oil life);
   - on a drive over 65 minutes a second pass about 60 minutes after the first; after a stop under
     an hour, no pass in the next drive until the hour has elapsed.
   "Run clean on several drives", before widening the module list, means at least five passes on
   at least three drives with every check above.

## Tests

- `tests/drive_dtc_support.py`: a scripted module ECU (SingleFrame, segmented reply that asserts the
  unpadded DLC-3 FlowControl arrives only after a valid FirstFrame, NRCs, 0–3 pendings, silence,
  malformed FirstFrame, wrong sequence, late frames after the quiet guard), a scripted sentinel feed
  (local/foreign flags, truncation, overflow counter, receive errors, drain exhaustion), reusing
  `Clock`, `FakeAuthority` and `Sink` from `tests/radar_service_support.py`.
- Transport: exact IDs, data and DLC per module; no FlowControl after any rejected FirstFrame, after
  an expired FlowControl permit, after a late FirstFrame or after a foreign frame; exactly one
  FlowControl per reply; expiry before the request is `skipped`, after it `incomplete`; slices never
  exceed their clamp; the length bounds.
- Steps: settle; reservation before the first request (and no request when the write, either
  `fsync` or the rename fails); same-boot and cross-boot budget rules, clock jumps both ways;
  harvest refresh per pass; one transaction at a time; radar endpoint ownership skipping 0845 and
  service cycles, and the queued command delay bound; the epoch disable after any service attempt
  (including a refused and a timed-out start); the foreign-tester check before every request and
  FlowControl; pass deadline; event shapes.
- Helper (`test_active_drive.py`): without the variable, or with a wrong version or PID, no DTC frame
  is ever sent and no DTC socket is opened (scan every sent frame and socket); with it, frames only
  after settle; DTC sockets closed before restoration; the worst-case PCM gap.
- `test_transmit_permit`: the four new purposes, bound per module and frame.
- Broker control: event validation; the environment is built explicitly (inherited variable removed;
  set only with the flag; PID bound); cadence violation stops the helper through the normal stop and
  its final event is still processed; writer order, overflow and failure latch; old/new
  broker-helper combinations through the launch tests.
- Store: additive idempotent open, higher-version refusal, CHECK constraints, retention window,
  cache regeneration and size cap.
- Dashboard Node tests: success versus attempt comparison, coverage text, empty filtered reply,
  per-source precedence, the `mergeVanScan` fix.
- Harvester: sync tests for `22 21 85`, the radar service requests and `19 02 8F`.
- Oracles: `broker_pure_oracle` status hashes unchanged; `gate_oracle` byte-identical (no DTC tool
  changes); `active_route_effect_oracle` unchanged; every tidying commit compared old/new by output
  (tests plus, for the harvester move, a report run on a saved fixture).
- Whole suite before every commit that leaves the worktree and before the review:
  `/home/pi/van_compute/scripts/pi_compute.py run repo-full-suite --wait 60`. As of 2026-11-15 that
  job is red on master: `tests/test_notes_gate.py::test_no_test_module_names_a_note` runs
  `git ls-files` in a compute bundle without `.git`. It must be fixed (by its owner, or gated with a
  named environment skip) before this work can commit.

## Code shape

New modules, one concern each, with their AGENTS.md map entries in the same commit:

- `projects/vehicle_data/drive_dtc_contract.py` + `drive_dtc.json`: the module tuple, frames,
  length bounds, timing constants, event vocabulary and validation, the eligibility rule.
- `projects/vehicle_data/drive_dtc_transport.py`: the only code that can build or send the new
  frames; the transaction state machine.
- `projects/vehicle_data/drive_dtc_sentinel.py`: the foreign-tester socket and its check.
- `projects/vehicle_data/drive_dtc_budget.py`: the budget file (reserve, read, clock and boot
  rules).
- `projects/vehicle_data/drive_dtc_steps.py`: the per-cycle step (eligibility, slices, endpoint
  ownership, pass state).
- `projects/vehicle_data/drive_dtc_store.py`: the separate SQLite store, retention and cache writer.
- `projects/vehicle_data/drive_dtc_control.py`: broker side (environment, dispatch, writer thread,
  cadence check, latches).
- A sibling of `insights.py` for the `drive_reads` block; `dashboard/src/views/dtcSources.helpers.js`
  (the per-source merge, moved out of `health.helpers.js`).
- `projects/vehicle_data/van_scan_pi_requests.py` (the harvester's Pi request tables).

Extended in place: `transmit_permit.py` (purposes), `radar_service_alignment.py` (the
`run_attempted` property), `radar_drive_steps.py` (the endpoint-owned skip), `ccan_powertrain.py`
(the speed reader promoted from `radar_service_alignment._speed_mph`, not copied).

Over-limit files gain no net lines: `active_drive.py` / `run_active_session` (the oil-life block
moves to a sibling step first), `broker.py` (`build_parser` moves to `broker_cli.py` first),
`insights.py` (the new block is a sibling; wiring offset by moving the van-scan cache validator),
`van_scan_harvest.py` (request tables move out first). `lib/dtc.py` is not changed.

The vehicle-data README's frame list under "Coordinated engine-running diagnostic interval" gains
the four frames in the commit that wires the step; a new section "Hourly DTC read during drives"
holds the contract, deployment, rollback and validation record.

## Build order (each commit safe to land alone)

1. Tidying: harvester request tables → `van_scan_pi_requests.py` (output-identical).
2. Harvester: recognise `19 02 8F` from the Pi (before anything can send it).
3. Harvester: recognise `22 21 85`.
4. Harvester: recognise the radar service-alignment requests.
5. Tidying: oil-life block out of `run_active_session` (behaviour-identical; `test_active_drive`,
   active-route oracle).
6. Tidying: `broker.build_parser` → `broker_cli.py` (`--help` and broker oracle identical).
7. Tidying: `mergeVanScan` → `dtcSources.helpers.js`; the van-scan cache validator out of
   `insights.py` (outputs identical).
8. Dashboard fix: compare the van sweep with the Pi's last success, not last attempt.
9. Contract, vocabulary and the drive store (no sender).
10. Transmit-permit purposes, transport, sentinel, budget (library only, not wired).
11. `ServiceAlignment.run_attempted` and the endpoint-owned skip in `RadarDriveSteps` (inert unless
    a DTC step owns the endpoint).
12. Steps module (not wired).
13. Helper wiring behind the environment handshake (inert: no broker sets the variable).
14. Broker control, writer, dispatch, environment construction with the flag off by default, the
    `drive_reads` block and dashboard display; README frame list and section.
15. Activation: the unit flag (after the review and the parked deploy of 1–14).

## Review findings (first plan, rework) and where this plan answers them

| # | Finding | Position | Where |
| --- | --- | --- | --- |
| 1 | Foreign-tester send-time gap | Accepted | Foreign-tester check (before every request and FlowControl, fail closed, other testers and unexpected local frames, residual race) |
| 2 | Service-alignment inactivity is not ECU inactivity | Accepted (verified: `_fail` after a timed-out start; completion lets the session lapse) | Radar service alignment |
| 3 | Five-second blocking reads; late replies on shared endpoints | Accepted (verified: RF Hub malformed reply ends the session; 0845 failure disables polling) | Reception, timing and endpoint ownership |
| 4 | Migration backup and rollback could lose history | Accepted; the migration is removed | Storage (separate file, parked history untouched) |
| 5 | Migration from ordinary constructors; cache version; new-broker/old-helper | Accepted | Storage (additive idempotent schema); Handshake (environment, PID-bound) |
| 6 | Retention neither preserved history nor bounded growth | Accepted | Storage (plain window, no preservation claim) |
| 7 | Budget not durable before sending; launch-only harvest check | Accepted. The acknowledgement is the sending helper's own completed `fsync` rather than a broker reply, which leaves no message to lose between reservation and send | Scheduling and the durable budget |
| 8 | Limited coverage and source precedence | Accepted | Dashboard; Storage (single-writer cache) |
| 9 | First-drive scope; padded FC; "harmless" wording | Accepted | Settled decision 3; What goes on the bus (proven per-module FC, occupancy stated) |
| 10 | Obsolete classifier prerequisite; harvester size; build order | Accepted (verified `2fe75a4`, `van_scan_harvest.py` 1050 lines) | Evidence; Harvester; Build order |

## Open questions and risks

1. **Accumulated driving hours versus elapsed cooldown**: with the parent, for the owner. Elapsed is
   specified; the alternative's cost is stated above.
2. **The first `8F` request**: the mask byte has not been sent to these modules before. If a module
   answers `7F 19 12` or `31`, the read is stored as negative and the next step is a review, not a
   mask change in the field.
3. **`MSG_DONTROUTE` through Python's `recvmsg` on this kernel**: documented by SocketCAN, not yet
   observed here; if absent, every check fails closed and no pass runs (visible on the first drive).
4. **Radar late frames after the quiet guard**: the residual can stop 0845 polling for the rest of an
   epoch; first-drive evidence decides whether the guard needs lengthening.
5. **The radar's `8F` reply may be segmented** if it now holds more than one matching code; the
   unpadded FlowControl is proven on the radar for a 67-byte reply, so that path is covered, but it
   would be the first segmented DTC reply from the radar.
6. **The Pi's continuous F1 polling suppresses the van's own health check** (and with it any
   Uconnect Vehicle Health Report, if the telematics unit is the client). Unchanged by this plan; for
   the owner.
7. **Notification of a new current code during a drive** (advisory ntfy): out of scope.
8. **Red suite on master** (`test_notes_gate.py` in compute bundles): blocks committing this work
   until fixed.

Not determinable from the repository: what triggers the van's own sweep; reply sizes and pending
behaviour for `8F` specifically; whether the radar or BCM answer `78` more often at speed.

## Status

Second revision awaiting cross-family review. Nothing built; no CAN traffic, service change or live
write. The read-only queries used `mode=ro` on `tmp/vehicle_data/dtc-history.sqlite3` and the
archived 2026-09-24 capture on `/mnt/EXFAT512`.
