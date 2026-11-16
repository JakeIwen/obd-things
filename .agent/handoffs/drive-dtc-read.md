---
workstream: drive-dtc-read
status: plan awaiting review
tier: T3
last reviewed commit: none (plan not yet reviewed)
---

# Hourly DTC read during drives: plan

## Objective

Owner request (2026-11-15): "query error codes once per hour of driving if there is no scan
harvest", for modules "such as the PCM BCM DASM etc". The cluster shows latched ACC/radar
warnings, and nothing on the Pi has read a module's fault codes since the parked 15-module batch
on 2026-10-08 (03:12–03:13 MDT). The van's own F1 sweeps were harvested only twice
(2026-08-30 20:38 MDT and 2026-09-24 15:12 MDT) and are not expected again while the Pi polls on
every drive.

Tier T3: new CAN transmission while the van is driven, changes to the broker/helper contracts and
to the DTC history schema, and it cannot be verified by running it in a development session.
Build in the worktree branch `worktree-agent-ace5b03e8cf37934c`; a cross-family review comes
before any merge, install or deploy.

## Evidence this plan rests on (read-only, gathered 2026-11-15)

- `module_scans` (Oct 8, `19 02 FF`, parked): 300 DTC observations across 13 modules; **278 are
  status `0x40` only** (testNotCompletedThisOperationCycle); only 22 carry a failed, pending,
  confirmed or warning bit. Reply sizes with `FF`: BCM 106 DTCs (427 bytes, about 61
  ConsecutiveFrames), ABS 55, TCM 53, Uconnect 37, radar 8 (one not `0x40`-only), cluster 9.
- The van's own sweep (`2026-09-24_in_vehicle_f1_scan_exchanges.csv` and the archived capture,
  while driving): request `18DAxxF1#0319020D00000000` (DLC 8, zero padded); its FlowControl is
  unpadded `30 00 00` (DLC 3). Largest `0D` reply 27 bytes (BCM, Uconnect). Response-pending
  (`7F 19 78`) is common: radar 143 ms, TCM 310 ms, ORC 200 ms, ABS 435 ms. Otherwise the first
  response comes in 2–48 ms.
- **PCM does not support UDS `19`**: `19 02 0D` returns `7F 19 11` (serviceNotSupported). Its
  DTCs come from KWP2000 `18 00 FF 00` (`58 00` = none on 2026-09-24; AlfaOBD saw `58 03 …` in
  July). PCM multi-frame replies need an **8-byte zero-padded FlowControl** (unpadded `30 00 00`
  got no ConsecutiveFrame; the Pi's padded FC got `1A 87` on 2026-08-30). The July 25 PCM
  `19 02 FF` batch attempt timed out, and `lib/dtc_batch.py` marks the PCM unsupported.
- CAN-CH modules (ABS, EPS, HALF, ORC) are reached by the van's tester **from C-CAN through the
  gateway** (frame-by-frame forwarding, gateway FC `30 00 0A` on CAN-CH, replies copied to C-CAN
  as `18DAF2xx`). The Pi has never used that path. During such traffic `lib/canbus.py`
  `CANCH_DIAG_SIG` can misidentify C-CAN as CAN-CH (finding follow-up 3).
- `PcmElectricalPoller` reads the first frame on its socket with **no drain**; a stray PCM frame
  becomes a generator-duty failure, which ends the whole helper session. `RfHubPressurePoller`
  drains (unbounded, non-blocking) before each read; `RadarAlignmentPoller.drain` is bounded at 32
  frames and a failure there disables 0845 polling for the session.
- Harvester: `PI_DTC_TOOL_REQUESTS` already excludes `19 02 FF` for any target and deliberately
  does **not** contain `19 02 0D` (that request is how it recognises a van sweep).
  `PI_FIXED_REQUESTS` lacks PCM `22 21 85` (oil life) and every radar service-alignment request
  (`10 03`, `31 01/02/03 0251`, `3E 00`).
- The DTC cache (`dtc-cache.json`) is strictly validated by `insights.py` with exact key sets;
  per-module rows already carry `last_attempt_at`/`last_success_at`. The dashboard already merges
  the van's own `0D` sweep per module with a status-mask rule (`health.helpers.js` `mergeVanScan`).

## Decisions (the brief's five, with my position on each)

1. **Mechanism: accepted, with one dispute for the PCM.** The Pi reads codes itself, inside the
   C-CAN active-drive helper, through `FreshAuthority` (fresh RPM evidence plus active gates) and
   one-use transmit permits; physical 29-bit addressing, default session, one module at a time.
   Never `14`, `10`, `27`, `2E`, `31`, `2F` or `3E`. Dispute: the PCM cannot be read with `0x19`
   at all. Reading it needs KWP `18 00 FF 00` (also read-only), a padded FC and a drain added to
   the PCM poller's socket, so the PCM is phase 3 and needs an owner/parent decision to widen
   decision 1 (Open question 1).
2. **Cadence: accepted.** Settle period **300 s** of armed helper time in the current session;
   then a pass when the newest of (last Pi pass start, last harvested van sweep completion) is at
   least 3600 s old; then every 3600 s within the session. How sweeps are learned is in
   "Scheduling and the hourly budget".
3. **Opt-in: accepted.** Broker CLI flag `--enable-drive-dtc-reads` (off by default) is the switch;
   only a broker started with it passes the helper `--enable-drive-dtc` and
   `--drive-dtc-due-in SECONDS`. Off means removing the flag and restarting the broker (guarded).
   Today's `--enable-oil-life` has no off switch; this one does.
4. **Request and destination: changed.** Use **`19 02 8F`**, not `19 02 FF` and not the van's
   `0D`, recorded in the existing `module_scans` history with `request_hex = '19 02 8F'`. The
   justification follows. This needs a v2→v3 DTC-history migration (the
   `CHECK (request_hex = '19 02 FF')` constraint) and a request-mask-aware absence rule in the
   rebuild. The cache file schema stays v2; the source label comes from a small state file
   (Data flow).
5. **Module scope: phased.** Phase 1, C-CAN (owner-named BCM and DASM included): `radar_acc`
   (DASM), `bcm_ccan`, `cluster`, `rf_hub`, `tcm`, `shifter`, `telematics`. Phase 2, B-CAN through
   the B-CAN auxiliary helper: `ics_bcan`, `uconnect_bcan`, `climate_bcan`, `emcm2_bcan`. Phase 3,
   each its own owner decision: PCM via KWP `18`; CAN-CH (`abs_canch`, `eps_canch`, `half_canch`,
   `orc_canch`). Position on CAN-CH while moving: keep them parked-only (the existing batch) for
   now. Reading DTCs is read-only and the OEM's tester does it at speed, but the Pi has never used
   the gateway path, the ABS answers `78` for about 0.4 s, the path disturbs the C-CAN bus
   identity classifier, and the gain over the parked batch is small. If wanted later: first a
   parked, owner-authorized gateway test and the `CANCH_DIAG_SIG` reply-side fix, then a separate
   drive design.

### Why `19 02 8F`

- `FF` puts the 278 `0x40`-only "test not completed" records into every drive pass. During a
  drive those tests complete and the records drop out, so the rebuild would mark about 270 records
  "resolved" after the first pass and "recurred" at the next parked read, and records whose test
  completes only at highway speed would flap between passes. That makes `dtc_transitions`,
  `recurrence_count` and `episode_count` meaningless, and it costs a 61-frame BCM reply each hour.
- `0D` is what the OEM's tester sends while driving and is proven on all 14 UDS modules, but it
  is the harvester's signature for a van sweep: our own pass would be imported as "in-vehicle scan
  (source F1), passive", false provenance, unless the harvester learned our pass windows by time,
  a fragile coupling.
- `8F` = warningIndicatorRequested | confirmed | pending | testFailedThisOperationCycle |
  testFailed. On the Oct 8 data it keeps all 22 fault-relevant records and none of the 278
  incomplete-only ones; it includes the warning-lamp bit, which is what the latched cluster
  warnings are; replies stay small (a few frames); and its bytes differ from the van's sweep, so
  the harvester excludes it by adding one request to `PI_DTC_TOOL_REQUESTS`, the existing
  mechanism. Subfunction `02` is unchanged; only the mask byte differs from proven traffic, and
  ISO 14229 defines every mask value.
- History semantics: the rebuild uses `effective = status_availability_mask & request_mask`
  (request mask `FF` for every existing row, so existing derived state stays byte-identical). A
  record observed by a parked `FF` read with status `0x40` has mask `CF`/`4F`; a later `8F` read
  that omits it is not compatible (`(8F & CF) != CF`), so it is not resolved: the existing
  `retained_incompatible_status_mask` state applies, with no churn. A record seen by an `8F` pass
  and later absent from an `8F` or `FF` read resolves normally. A confirmed code seen only by a
  parked `FF` read and absent from an `8F` pass is retained until the next parked read
  (conservative).
- Cost: the migration, a two-line rebuild change in `lib/dtc.py`, and an `8F` pass cannot say
  "no DTCs at all" for a module whose old incomplete-only records remain (it reports
  `status_coverage_incomplete`, which is honest).

## Frames, addressing and reception (phase 1)

Everything the feature can put on the bus is two fixed frames per module, in one new file
(`drive_dtc_transport.py`), each behind its own one-use permit:

| module | request (29-bit, DLC 8) | FlowControl (only after a valid FirstFrame) |
| --- | --- | --- |
| `radar_acc` | `18DA2AF1#0319028F00000000` | `18DA2AF1#3000000000000000` |
| `bcm_ccan` | `18DA40F1#0319028F00000000` | `18DA40F1#3000000000000000` |
| `cluster` | `18DA60F1#0319028F00000000` | `18DA60F1#3000000000000000` |
| `rf_hub` | `18DAC7F1#0319028F00000000` | `18DAC7F1#3000000000000000` |
| `tcm` | `18DA18F1#0319028F00000000` | `18DA18F1#3000000000000000` |
| `shifter` | `18DA1FF1#0319028F00000000` | `18DA1FF1#3000000000000000` |
| `telematics` | `18DAC6F1#0319028F00000000` | `18DAC6F1#3000000000000000` |

- Request framing is the van tester's own (DLC 8, `00` padding) with the mask byte `8F`.
- FlowControl `30 00 00` = ContinueToSend, BlockSize 0, STmin 0: the parameters of the van's
  tester and of the Oct 8 kernel-ISO-TP batch, proven on every phase-1 module. BlockSize 0 means
  **exactly one FC per reply**, so one FC permit suffices (the radar's `30 01 00` would need one FC
  per ConsecutiveFrame). Padded to 8 bytes with `00`: padding is what the PCM requires and what
  the radar already accepts; the other modules have only been seen with the unpadded DLC-3 form
  (Open question 6). The 29-bit diagnostic IDs lose arbitration to every broadcast, so the burst
  cannot delay powertrain frames.
- Raw `CAN_RAW` sockets only, no kernel ISO-TP, so no FC can be sent automatically. Per-module
  socket filter: the exact EFF response ID `18DAF1tt`, RTR/ERR rejected (the RF Hub poller's
  pattern). The radar uses the open `RadarAlignmentPoller` link (`drain`/`send_fixed`/`receive`),
  as `RadarServiceTransport` does, so the radar keeps one tester socket and the 0845 read never
  finds a DTC reply queued. `rf_hub` uses its own socket; the TPMS poller drains before every read.
- Reception (`exchange(module)`), bounded at every step:
  1. drain this socket (bounded, 64 frames; overflow → `malformed`, nothing sent);
  2. consume the request permit, send the request;
  3. first frame within **P2 = 1.0 s**:
     - SingleFrame `59 02 mask [4n]` with 3 + 4n ≤ 7 → positive;
     - SingleFrame `7F 19 78` → response pending: new deadline **P2\* = 2.0 s**, at most **2**
       pendings, never resend;
     - SingleFrame `7F 19 nn` (other NRC) → negative, with the NRC; `7E`/`7F` are reported as
       `session_required` and never lead to a session change;
     - FirstFrame `1L LL 59 02 …` with 8 ≤ length ≤ **515** (3 + 4·128) and (length − 3) % 4 == 0
       → send the FC (its permit), then ConsecutiveFrames with sequence 1…F, 0… each within
       **N_Cr = 1.0 s**, until the declared length;
     - anything else (another service, a bad length, a FirstFrame that is not `59 02`) →
       `malformed`, **no FC sent**;
  4. hard cap per module **5.0 s** from the send; a module that exceeds it is `no_response`.
- Parsing reuses `lib.dtc.parse_dtc_list_response` (strict), so a reply the history cannot hold
  is `unparsed`, never half-stored.
- One attempt per module per pass; no retries.

## Where the step sits in the helper cycle

- A new `DriveDtcSteps` (sibling module `drive_dtc_steps.py`, the `RadarDriveSteps` pattern) is
  called once per cycle **after** `radar_steps.run(snapshot)` and before `_wait_for_next_cycle`.
  By then the cycle's PCM duty/torque, RF Hub, VVT, oil-life and radar reads are done, so the step
  only lengthens the tail of a cycle.
- At most **one module per cycle**, and at least **2.0 s** between module requests, so a
  7-module pass takes about 14 s. The PCM 1 Hz reads keep running between modules. Worst case a
  cycle is stretched by 5 s (two pendings, then nothing); the supervisor's 10 s silence limit is
  not at risk because a `dtc_read` event is emitted right after each module.
- Each module takes its request and FC permits from one `FreshAuthority.acquire` call (fresh
  0.35 s broadcast snapshot, at least three RPM samples at or above 400, identity, ERROR-ACTIVE,
  topology, inhibits). If a pending reply pushes the FirstFrame past the FC permit's 250 ms life,
  the step acquires a fresh FC permit (another snapshot of at most 0.35 s, inside the ECU's 1 s
  N_Bs); if that is refused, the result is `incomplete` and nothing more is sent. Stale evidence
  (`acquire` returns None) retries the module next cycle, at most 3 times, then records it
  `skipped` (nothing sent).
- Radar Service Drive Alignment: no pass starts while `ServiceAlignment.state` is in
  `ACTIVE_STATES`; if a run becomes active mid-pass (its command is handled earlier in the same
  cycle), the pass is abandoned with `radar_service_active` before its next module. The loop is
  single-threaded, so a module read and a service exchange can never interleave on the radar
  socket.
- `run_active_session` is over 80 lines and `active_drive.py` over 800: a tidying commit first
  moves the oil-life block into a sibling step function, so the DTC wiring (construct the step,
  call `run`, close its sockets in `finally`) adds no net lines.

## Scheduling and the hourly budget

- Broker side, at each helper launch (pure function in the contract):
  `newest = max(last Pi pass started_at, van-scan-cache last_scan.completed_at)`;
  `due_in = clamp(3600 − (now − newest), 0, 3600)`; with no record, 0. If `newest` is more than
  300 s in the future (clock jumped back; the Pi has no RTC), `due_in = 3600` (send later, not
  sooner).
- Helper side: first pass at `armed_at + max(300, due_in)` (monotonic); later passes every 3600 s
  from the previous pass start within the session. A drive under 5 minutes never reads. Short trips
  share the budget: after a pass at minute 5 of one trip, a trip 30 minutes later reads at its
  minute 15 (due_in 900 s) if it lasts that long.
- Persistence: `tmp/vehicle_data/drive-dtc-state.json`, written by the broker (atomic replace,
  `lib.dtc_batch.atomic_json`). One record of the last pass (`pass_id`, `started_at`,
  `finished_at`, `state`, `reason`, `modules_planned`, `modules_read`) plus the latest result per
  phase-1 module (`completed_at`, `outcome`, `pass_id`). Bound: fixed shape, at most 4 KiB, never
  appended; it falls under the existing `/home/pi/dev/obd-things/tmp` root budget (5120 MiB), so no
  new `ROOTFS_PATH_BUDGETS_MIB` entry is needed. The helper emits `dtc_pass started` one cycle
  (at least 1 s) before its first module request, so the broker normally has the start on disk
  before anything is sent. If the broker dies in between, the worst case is one extra pass within
  the hour.
- Learning of the van's own sweeps:
  - **already imported**: `van-scan-cache.json` `last_scan.completed_at` (validated by the existing
    insights validator) feeds `due_in` at launch. The harvester runs every 20 minutes on completed
    chunks, up to about 30 minutes behind; a sweep in that gap is not seen and we may read again.
    That is harmless (read-only, stored separately) and practically unreachable, because the van's
    client only starts while Pi F1 traffic is quiet, that is, while no helper runs.
  - **in progress**: a sentinel socket in the helper, bound to the C-CAN channel with filters for
    extended `18DA..F1` and `18DB..F1` (requests and FCs from tester address F1). It is drained
    each cycle with `recvmsg` (bounded at 512 frames; `SO_RXQ_OVFL` enabled). Frames this host
    sent (any of our sockets) arrive with `MSG_DONTROUTE`; a frame **without** it is a foreign F1
    tester (the van's client, or AlfaOBD on the Y-splitter). No pass starts unless the sentinel has
    been open and clean for the last **120 s**; a foreign frame mid-pass abandons the pass before
    its next module (`foreign_tester`); a queue overflow or a sentinel that cannot open counts as
    foreign (fail closed: no DTC sends that session, everything else unchanged).
- Broker defence in depth: a `dtc_pass started` earlier than `launch + max(300, due_in) − 5 s`, or
  less than 3595 s after the previous start, is a cadence violation. The broker stops that helper
  through the normal supervisor stop (restoration as today), records nothing more from it, and
  stops passing the DTC flag until it is restarted.

## Gates

A pass starts only when all of these hold:

1. the broker passed `--enable-drive-dtc` (handshake) and the helper is armed with its first
   running snapshot;
2. settle and due time reached; at least 3600 s since this session's last pass;
3. no radar service run active; sentinel open and clean for 120 s;
4. per module, at its send: `FreshAuthority.acquire` (fresh RPM evidence and every active gate),
   then one-use permits bound to the lock, channel, purpose and process.

A pass stops early on: `SessionEnding` (engine stopped, traffic gone, adapter/topology/inhibit
change; the session ends exactly as today); a radar service run; a foreign tester; a send `OSError`
(pass abandoned, `transport_error`; the session's own gates handle the adapter); a pass deadline of
**120 s**; helper termination. A `TransmitPermitError` other than expiry stays fatal
(`helper_failed`), as for every other transport.

Restoration and inhibits are unchanged: the transport's sockets and the sentinel close in the
session's `finally` before restoration; no new inhibit, latch or final reason is added, so
`ACTIVE_DRIVE_FAILURE_REASONS` and `helper_events.py` do not change.

Worst case if the helper dies mid-pass (SIGKILL between request and last ConsecutiveFrame): the
ECU stops on its own ISO-TP timer (N_Bs/N_Cr, about 1 s); it is in the default session with nothing
to restore, and no DTC or ECU state changes. The interface follows today's rule (unverified
restoration → wildcard inhibit and broker latch). The broker has already stored every module whose
event arrived; the pass counts against the hour.

## Data flow

Helper events (new types, validated by the pure contract `drive_dtc_contract.py` and dispatched in
`broker.handle_active_drive_event` before `validate_helper_status`; an old broker never receives
them because it never passes the flag):

```json
{"type": "dtc_pass", "pass_id": "<32 hex>", "state": "started|completed|abandoned",
 "reason": "<vocabulary>", "detail": "...", "modules": ["radar_acc", "..."],
 "modules_read": 0, "interface_mode": "armed_diagnostic"}
{"type": "dtc_read", "pass_id": "<32 hex>", "module": "bcm_ccan", "request": "19 02 8F",
 "outcome": "positive|negative|no_response|incomplete|malformed|skipped",
 "nrc": "78|null", "pending_count": 0, "response_hex": "59 02 4F ...|null",
 "started_at": "<UTC ISO>", "completed_at": "<UTC ISO>", "rpm": 1650.0, "speed_mph": 61.2,
 "detail": "...", "interface_mode": "armed_diagnostic"}
```

Validation: exact key sets; module in the phase-1 tuple; request exactly the constant; outcome in
the vocabulary (`drive_dtc.json`, shared with the dashboard); `response_hex` at most 515 bytes and
present only for `positive`; `pass_id` matches the open pass; timestamps within ±120 s of receipt
(otherwise the receipt time is used and noted). A violation is a protocol error, handled like any
other helper protocol error.

Broker handling (`drive_dtc_control.py`) builds a `lib.dtc.ModuleScan`:

- `positive` that parses → `success` (availability mask from the reply, DTC records);
- `positive` that does not parse → unavailable `unparsed`; `negative` →
  `negative_response_<NRC>`; `no_response` → `timeout`; `incomplete` → `incomplete_response`;
  `malformed` → `malformed_response`; `skipped` → no row (nothing was sent);
- `source_key = drive-dtc:<pass_id>:<module>`, `source_ref = drive-dtc/<pass_id>`,
  `request_hex = '19 02 8F'`, `physical_pair = 6/14`, the resolved `canN`, `conditions =
  "drive pass; engine running <rpm> rpm; <speed> mph; armed C-CAN helper"`.

One writer thread with a bounded queue (64 items; overflow drops the item with a journal line and
never blocks the supervisor) does `DtcHistory.record_scan`, the retention prune,
`dashboard_summary(compact=True, per_group_limit=25)` → `write_cache(--dtc-cache)` (the same call
the batch makes; `25` becomes one shared constant), then updates the state file. The harvester
writes other tables of the same SQLite file (WAL, busy_timeout 5 s); the parked batch cannot
overlap because it requires idle helpers.

History bound: each `8F` pass adds at most 7 `module_scans` rows and a few observations, but
`_rebuild_module` replays every scan of a module on each insert. Retention: per module, keep every
scan that produced a transition and the newest **48** drive-pass scans with none (two days of
hourly driving); delete older transition-free drive-pass scans. Removing a transition-free scan
changes only `observation_count` and the success/unavailable counters, not first/last seen, status
changes, resolutions or recurrences (a test pins this).

Dashboard: `/v1/diagnostics/dtcs` gains a `drive_reads` block built from the state file by a new
sibling of `insights.py` (size cap 16 KiB, validated; absent or invalid → omitted). A module whose
`last_attempt_at` equals its `drive_reads` `completed_at` shows "read while driving, <time in
Mountain>"; failures keep the existing "no answer (…) · last good read …" text. The cache file
schema stays v2 (the dtc-dashboard-descriptions decision). A new `driveDtc.helpers.js` sits beside
`health.helpers.js`; the existing `mergeVanScan` rule (newer read wins per module) needs no change.

Harvester: add `19 02 8F` to `PI_DTC_TOOL_REQUESTS` (counted as Pi activity, not foreign
traffic); our FCs are already skipped by its reassembly. The `22 21 85` fix is its own earlier
commit.

## Deployment

1. Merge after the T3 review, flag off. Update the main checkout; the helper code on disk changes,
   but a running broker never passes the new flag, so nothing new is sent.
2. Guarded parked restart (`s restart_broker`) with the flag still off: loads the new broker; the
   status hashes are unchanged (`tests/broker_status_hashes.json`).
3. Activation commit: add `--enable-drive-dtc-reads` to the tracked
   `projects/vehicle_data/systemd/van-telemetry.service` `ExecStart`. Install the unit the way the
   vehicle-data README's deployment section says (copy, `daemon-reload`), check
   `systemctl show -p KillMode,TimeoutStopUSec van-telemetry.service`, then `s restart_broker`
   parked. The DTC history migrates v2→v3 when the broker's writer first opens it at startup,
   after a file copy `dtc-history.sqlite3.v2-backup-<UTC stamp>` beside it (kept until
   validation, then deleted). Verify read-only: `metadata.schema_version = 3`, and the dumped
   `dtc_state`, `dtc_transitions` and `module_cache` equal the pre-migration dump.
4. Switch off without code: remove the flag (edit the installed unit, or a drop-in that restates
   `ExecStart` without it), `daemon-reload`, then `s restart_broker` (parked) or
   `s restart_broker --while-running`.
5. Code rollback: the new broker passes flags an old helper rejects, so switch off first (step 4),
   then revert and restart parked. Old `lib/dtc.py` refuses schema 3 (fail closed), so a rollback
   past the migration also restores the v2 backup, after exporting the drive-pass rows to
   `tmp/vehicle_data/` as JSON evidence.
6. First drive, to call phase 1 validated:
   - journal/status: helper armed as usual; `dtc_pass started` about 5 minutes after arming;
     seven `dtc_read` events; `dtc_pass completed`; final `restored: true`;
   - drive recording (C-CAN): per pass exactly seven `18DAttF1#0319028F00000000` requests and at
     most seven `#3000000000000000` FCs, each after a `59 02` FirstFrame; no other new identifier
     or payload; no foreign-tester deferral while only the Pi is on the bus (this also validates
     `MSG_DONTROUTE`);
   - PCM duty/torque, RF Hub, radar 0845 and oil life keep their cadence (no metric gap over 3 s in
     the history around the pass);
   - DB rows with `request_hex = '19 02 8F'`, no churn transitions, cache regenerated, the
     dashboard labels the seven modules "read while driving";
   - the next harvester report shows no `other_f1_traffic` window for the pass (and none for oil
     life after commit 1);
   - on a drive over 65 minutes, a second pass about 60 minutes after the first; on a later short
     trip, the budget carries over.

## Tests

- `tests/drive_dtc_support.py`: a scripted ECU (single-frame; multi-frame that asserts the padded FC
  arrives only after a valid FirstFrame; NRCs; 0–3 pendings; silence; malformed FirstFrame; wrong
  sequence number; late reply), reusing `Clock`, `FakeAuthority` and `Sink` from
  `tests/radar_service_support.py`.
- `test_drive_dtc_transport.py`: exact bytes and IDs per module from the closed table; nothing
  sendable for an unknown module; no FC after any malformed FirstFrame; exactly one FC; pending and
  length bounds; permit expiry → `skipped`; a wrong-purpose permit raises.
- `test_drive_dtc_steps.py`: settle, `due_in`, hourly repeat, short trips, one module per cycle,
  spacing, stale-evidence retries, radar-run deferral and abandonment, the sentinel
  (`MSG_DONTROUTE` present/absent, overflow), pass deadline, event shapes.
- `test_active_drive.py`: without the flag no DTC frame is ever sent (scan every sent frame); with
  it, frames only after settle; sockets closed before restoration.
- `test_transmit_permit`: the new purposes are allowlisted and bound per module.
- `test_drive_dtc_control.py`: event validation (unknown module, request, oversize, mismatched
  pass), ModuleScan mapping, writer queue and overflow, state-file atomicity, `due_in` including the
  van-scan cache and clock skew, cadence-violation stop.
- `lib/dtc`: v2→v3 migration on a fixture DB; an FF-only history rebuilds byte-identical; an `8F`
  absence does not resolve incomplete-only records; retention leaves derived history unchanged
  apart from the counts.
- Harvester: the sync test takes `22 21 85` from `pcm_electrical` and `19 02 8F` from the contract.
- Launch argv (`test_drive_helper_launch`): the helper flags appear only with the broker flag.
- Dashboard Node tests for `driveDtc.helpers.js`.
- Oracles: `broker_pure_oracle` status hashes unchanged (flag off, no `/v1/status` change);
  `gate_oracle` byte-identical old vs new (the DTC tools do not change); `active_route_effect_oracle`
  unchanged. One-off: dump a read-only copy of the live DTC history as rebuilt by old and new
  `lib/dtc.py`, before and after migration, and compare.
- Whole suite before every commit that leaves the worktree and before the review:
  `/home/pi/van_compute/scripts/pi_compute.py run repo-full-suite --wait 60` from the worktree.

## Code shape

New sibling modules, one concern each:

- `projects/vehicle_data/drive_dtc_contract.py` + `drive_dtc.json`: phase-1 module tuple, request
  and FC constants, timing bounds, event types/fields and the outcome/reason vocabulary, event
  validation, the `due_in` rule.
- `projects/vehicle_data/drive_dtc_transport.py`: the only code that can send the new frames.
- `projects/vehicle_data/drive_dtc_steps.py`: scheduler, gates, sentinel, per-cycle step.
- `projects/vehicle_data/drive_dtc_control.py`: broker side (validation, writer thread, state file,
  cadence check, launch arguments).
- A sibling of `insights.py` for the `drive_reads` block; `dashboard/src/views/driveDtc.helpers.js`.
- `lib/dtc_schema.py`: schema creation and migrations moved out of `lib/dtc.py` (tidying), then v3.

Extended in place: `transmit_permit.py` (purposes; 268 lines), `van_scan_harvest.py` (two set
literals), `lib/dtc.py` (request-mask table and effective mask, no net growth after the
extraction).

Over-limit files gain no net lines: `broker.py` (3624; tidying first moves `build_parser` to
`broker_cli.py`), `active_drive.py` (1176; oil-life step extraction), `insights.py` (1562; the
van-scan cache validator moves to a sibling), `lib/dtc.py` (1147). The radar's private
`_speed_mph` snapshot reader is reused (promoted beside `BroadcastSnapshot` in `ccan_powertrain`),
not copied.

AGENTS.md: map entries for the modules above and the state file in the Telemetry dashboard
section; the Contracts table needs no row (no new entry point). The vehicle-data README
"Coordinated engine-running diagnostic interval" frame list gains the 14 frames, and a new section
"Hourly DTC read during drives" holds the contract, deploy, rollback and validation record.

## Build order (commits)

1. Harvester bookkeeping: add `22 21 85` to `PI_FIXED_REQUESTS[0x10]`; the sync test takes it from
   `pcm_electrical`. (Optional 1b: the radar service requests, Open question 9.)
2. Tidying: `broker.build_parser` → `broker_cli.py` (verify `--help` output and the broker oracle
   identical).
3. Tidying: `lib/dtc.py` schema/migration code → `lib/dtc_schema.py`; `DEFAULT_DB`,
   `DEFAULT_CACHE` and the group limit defined once (verify the gate oracle and a DB dump).
4. Tidying: the oil-life block out of `run_active_session` (verify `test_active_drive` and the
   active-route oracle).
5. Tidying: the van-scan cache validator out of `insights.py`.
6. `lib/dtc`: request-mask table, effective-mask rebuild, v3 migration with backup, retention.
7. Contract + `drive_dtc.json` + transmit-permit purposes.
8. Transport + scripted ECU support + transport tests.
9. Steps (scheduler, gates, sentinel) + tests.
10. Helper wiring (`--enable-drive-dtc`, `--drive-dtc-due-in`) + tests.
11. Broker control, writer, state file, launch flags, dispatch; harvester `19 02 8F` exclusion.
12. `drive_reads` response block, dashboard label, Node tests.
13. Docs: vehicle-data README, AGENTS.md maps, this handoff.
14. Activation: the unit flag (separate, after the review and the parked deploy of 1–13).

## Open questions and risks (with recommendations)

1. **PCM (the owner named it)**: UDS `19` is unsupported. Options: KWP `18 00 FF 00` (proven reply
   `58 00`), or OBD-II mode `03`/`07` (emission codes only, unproven here). Recommend phase 3 with
   KWP `18 00 FF 00`, a padded FC, a bounded drain added to `PcmElectricalPoller` first (a stray
   frame now ends the session), and a `protocol` column or table for KWP results. Needs decision 1
   widened.
2. **Mask `8F` vs `FF`** (decision 4). Recommend `8F` for the reasons above; `FF` avoids the
   migration but makes the history churn.
3. **CAN-CH safety modules while moving**: recommend no (parked batch only) until a parked gateway
   test and the classifier fix; owner decision.
4. **B-CAN, phase 2**: the B-CAN helper has no transmit permits or RPM evidence of its own (its
   gates differ on purpose); it needs a design for fresh running evidence relayed from the broker,
   the same transport and its own handshake. Recommend after phase 1 has run on a few drives.
5. **Telematics `0xC6`**: may be the van's own tester (the sweep never queried it). Reading its DTCs
   is harmless and the Oct 8 batch did; recommend include, drop it if it answers oddly.
6. **Padded FC** on BCM/TCM/shifter/cluster/telematics/RF Hub: only the unpadded form is proven on
   them. Recommend padded everywhere (ISO-compliant, required by the PCM); a module that then sends
   no ConsecutiveFrame records `incomplete`, and a per-module FC form can be added after review.
7. **Engine running but parked** (idling an hour for heat): the helper's gate is RPM, not motion,
   so a pass happens. Recommend accept; a speed condition would add a gate for little gain.
8. **Retention N = 48** transition-free drive scans per module: recommend accept.
9. **Radar service-alignment requests** are also missing from the harvester exclusion; recommend
   adding them in commit 1b.
10. **The Pi's continuous F1 polling suppresses the van's own health check** (and with it any
    Uconnect Vehicle Health Report, if TBM2 is the client). Not changed by this plan; noted for the
    owner.
11. **Notification of a new current code during a drive** (advisory ntfy): out of scope; a later
    owner choice.
12. **The kill switch needs a broker restart** (parked, or `--while-running` through the helper
    hold). Recommend accept; a file-based pause would be a second switch to keep correct.

Could not be determined from the repo:

- Whether `MSG_DONTROUTE` reaches Python's `recvmsg` for `CAN_RAW` on this kernel (SocketCAN
  documents it; the sentinel fails closed if not, and the first drive shows it).
- Reply sizes and pending behaviour for mask `8F` specifically (only `0D` while driving and `FF`
  parked are recorded); the Oct 8 statuses suggest a few frames per module.
- Whether the C-CAN modules answer `78` more often at speed than parked.
- Whether modules seen only with an unpadded FC accept a padded one.
- What triggers the van's own sweep.

## Status

Plan awaiting cross-family review. Nothing built; no CAN traffic, service change or live write was
made. The read-only queries behind it used `mode=ro` on `tmp/vehicle_data/dtc-history.sqlite3` and
the archived 2026-09-24 capture on `/mnt/EXFAT512`.
