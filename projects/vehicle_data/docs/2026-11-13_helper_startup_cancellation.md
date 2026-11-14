# Drive-helper cancellation before startup permission

Date: 2026-11-13. Vehicle: the repository's 2022 gasoline ProMaster.
Scope: helper process supervision, not CAN decoding or a new vehicle operation.

## Incident evidence

Trip 114 lost active-helper and display collection after an engine stop/restart.
The persisted wildcard inhibit was independently read before recovery with
`python3 -m tools.can_operation_state status --channel can1`. Its name was
`vehicle-data-bcan-restoration-failed`, its channel was `*`, and its timestamp
was `2026-11-14T01:00:16.474075+00:00` (18:00:16 MST on November 13). Its reason:

> broker could not verify B-CAN restoration: active-drive helper exited without
> a final restoration event (status -15); listen-only restoration is unverified

The broker's cached auxiliary status independently retained the matching event
time, `2026-11-14T01:00:16.473504+00:00`, and `restoration_failed: true`.
These are persisted operation state and broker status, **not journal messages**.

The incident handoff reported this wider sequence from the historian and the
journal's interface-command records. Those queries were not retained; the times
below are attributed investigation results, not a second independent extraction:

| Local time (MST) | Reported observation |
|---|---|
| 17:47:45–48 | Trip begins; both helpers arm their resolved interfaces. |
| 17:57:34–36 | RPM falls below running qualification; interfaces restore to listen-only. C-CAN ends `engine_not_running`; B-CAN ends on an ICS response timeout. No inhibit yet. |
| 18:00:12–16 | Engine restart, RPM 124 then 787. |
| 18:00:16 | B-CAN exits `-15` with no final event; wildcard inhibit persists. |
| 18:00:18 | C-CAN polled metrics report `can_busy`. |
| 18:00:24 onward | Metrics report `inhibited`; driving continues until about 20:03. |

The handoff reported no interface-arm command at the restart. Absence of a log
line is supporting evidence, not a sufficient safety proof that no interface
effect occurred. The fix therefore does not infer safety from that absence or
from a later passive interface snapshot.

## Failure mechanism and remaining uncertainty

Both helper modules perform their imports and initial setup before installing
`interrupt_on_termination()`. The supervisor could send SIGTERM immediately
after starting either process. Under the default signal action the child exits
`-15` without executing its session's finalization. The old supervisor treats
all missing final events as unverified restoration and the broker persists the
wildcard inhibit. That conservative verdict is required once CAN effects are
possible, but it also misclassified the broker's own early cancellation.

B-CAN supervision starts alongside the C-CAN epoch. When C-CAN finishes—even
because its broker-side role lock immediately returns `can_busy`—the broker's
`finally` cancels B-CAN. B-CAN imports the C-CAN helper as well as its own code,
so an early C-CAN refusal can finish before B-CAN installs its termination guard.
C-CAN itself has the same pre-guard window when the broker is stopped.

The original C-CAN lock holder is **not established**. The broker's display
pause timeout is not this trigger: that branch returns before either helper
starts. Its existing pause/release handshake is retained, as are role/channel
lock ordering and the policy that C-CAN epoch completion stops B-CAN. The
broker-side role lock can still legitimately refuse another observer.

Relevant history and reasons retained:

- `096e01e`: subprocess supervision and missing-final fail-closed restoration.
  Its reason remains valid whenever interface changes may have occurred.
- `d14d90a`: independently supervised B-CAN odometer collection, bounded by the
  C-CAN running epoch. Changing that coupling is not needed for this repair.
- `390404e`: display-receiver pause/release before exclusive C-CAN ownership.
  A shared passive lease must not overlap the helper's exclusive acquisition.
- `f5344eb`: bounded credit when the supervisor itself stalls. The distinction
  between a silent helper and a stalled host remains necessary.

## Reviewed startup contract

The implementation uses `helper_startup_grant.py`, shared by the
supervisor and both helpers, to establish a fact the parent can prove:
**no startup grant was attempted, so this helper could not enter its CAN session**.

1. The supervisor creates a dedicated pipe and passes only its read descriptor
   to the child through a private optional flag. It registers per-launch state
   before process creation. Pipe failure never falls back to an ungated launch.
2. The child reaches the barrier as the first operation inside its existing
   session `try`, with the termination guard already installed. It validates
   the descriptor, emits one exact versioned ready event with its PID, and
   waits for one fixed grant byte. No CAN lock, socket or interface operation
   precedes that barrier. EOF, an invalid token or a changed parent refuses entry.
3. Under one lock, the parent either records cancellation or irreversibly marks
   a grant attempt **before** writing the byte. Failed or ambiguous writes count
   as attempted. Cancellation closes the grant pipe and prevents a later grant.
4. A reaped child explicitly cancelled before any grant attempt may return a
   synthetic final with `state: idle`, `reason: engine_not_running`,
   `restored: null`, and `interface_mode: unknown`. This means restoration was
   not needed; it does not assert a verified passive interface state. Protocol
   or reader failures and contradictory output are not excused.
5. Once a grant is attempted, the existing requirement for a final restoration
   event remains. Missing or failed restoration still persists a wildcard
   inhibit and the broker latch. Observations are never accepted before grant.

The barrier is inside the session rather than only in `main` so a cancellation
between grant consumption and session entry cannot escape its finalization.
Cancellation is checked in EOF handling as well as at the top of the loop:
closing stdout and becoming reapable are separate process-exit events.

The existing 10-second silence and cleanup bounds, 30-second per-window stall
credit, parent-death signal, diagnostic gates, frame allowlists, restoration
sequence and persistence are unchanged. A valid ready event starts a fresh
silence window: startup can consume up to 10 seconds before readiness and up to
10 more before the next event, plus the existing bounded host-stall credit.
A pre-ready silence timeout still fails closed; slow imports without explicit
broker cancellation are a separate residual false-inhibit class.

The startup pipe is separate from radar command stdin. The flag is optional
only for compatibility with an old broker launching updated helper files;
updated supervisors never downgrade. Rolling source back beneath a running
updated broker can leave an old helper rejecting the new flag; rollback must
include the guarded parked broker restart.

## Verification and activation status

The cross-family plan review approved this contract with the protected-session
barrier and cancellation-aware EOF handling as required conditions. Offline
implementation evidence:

- Baseline regression `20261114T042113Z-6ca89d5a` failed as intended against
  `bb486df`: C-CAN `can_busy` cancelled the launched B-CAN helper, and the broker
  attempted to persist the wildcard inhibit for its `-15` missing-final exit.
- Fixed focused run `20261114T042252Z-62ea7780`: 93 tests passed, including that
  regression and post-grant fail-closed cases.
- Complete `repo-full-suite` job `20261114T042725Z-ca54aaf1`: 2,038 tests and
  3,290 subtests passed, 8 skipped; layout verification and scratch dashboard
  build also passed. Placement was a Mac worker. The real-helper barrier tests
  substitute a parent-PID check for Linux `prctl` there.
- Owner-authorized bounded Pi verification ran
  `python3 -m unittest tests.test_helper_startup_processes.RealHelperBarrierTests -v`
  directly because the compute queue cannot request Linux placement. Both tests
  passed (5.482 s): each actual helper handled pipe EOF and SIGTERM at its
  startup barrier, with Linux parent-death setup enabled and an empty audit of
  socket, process, lock-file and interface effects. Neither received a grant.
- Preservation comparison against `bb486df` was byte-identical for all three
  oracles. Repaired-tree jobs: gate `20261114T043620Z-12d3b49d` (882 runs),
  broker pure `20261114T043622Z-48e5566d`, ordered active-route effects
  `20261114T043624Z-55f554bc` (144 scenarios). Their corresponding baseline jobs
  were `20261114T041026Z-a7748284`, `20261114T041027Z-e9e99eb7` and
  `20261114T041028Z-ba0eca2f`. These preserve legacy behavior; the new regression
  tests, not the oracles, exercise the changed cancellation path.

Implementation review found no unsafe weakening and approved the core grant
contract. The two selected follow-ups are fixed: complete B-CAN launch-failure
finals keep pipe/Popen errors distinct from restoration faults, and subprocess
test waits are bounded. Final `repo-full-suite` job
`20261114T045913Z-073d7b93` passed on `m4mac.06`: **2,040 tests, 3,292 subtests,
8 skips**, plus layout checks and scratch dashboard build. All 12 changed Python
files matched that job's source-manifest hashes. Claude Opus 5.5 completed the
T3 implementation review and recheck with no remaining findings, approving merge
and guarded parked/asleep deployment. Repair commit `0159ae3` was pushed and
activated on vanpi at **2026-11-13 22:07:57 MST** through the existing guarded
restart. Post-deployment checks confirmed:

- Broker and both web listeners active with new PIDs and zero crash restarts;
  the drive recorder remained active with its original PID.
- Cache-only LAN and Tailscale `/v1/status` both returned HTTP 200; vehicle
  asleep, helpers idle, no active inhibits, both restoration latches false.
- Fresh serial-resolved C-CAN/B-CAN/CAN-CH checks proved UP, listen-only,
  ERROR-ACTIVE, correct rates, classical CAN, ONE-SHOT off and restart-ms zero;
  the spare remained down.
- Every role's RX/TX/error counter was unchanged across activation. TX stayed
  C/B/CH/spare **8670/3544/0/0**. No inhibit was cleared for this deployment.
- No warning-or-higher entries appeared in the four relevant service journals
  during the post-deployment check.

The asleep restart proves safe activation, not the moving-vehicle lifecycle.
Natural-drive engine stop/restart validation remains pending.

Separate follow-up candidates from review, not changed here:

- The existing first-signal window between a session's normal return and
  `begin_cleanup()` can interrupt finalization. It still fails closed after a
  grant, but may leave an interface armed. Signal-safe cleanup transition
  hardening needs its own reviewed design and tests.
- Sustained C-CAN contention can still spawn and cancel a B-CAN process each
  collector cycle. This repair prevents false latching, not that CPU cost.
- A cancelled child taking pipe EOF may report `helper_failed`, whereas one
  taking SIGTERM reports `engine_not_running`; neither latches or blocks a new
  epoch when the child confirms no mutation.
- Supervisor bookkeeping and existing oversized runtime/test files remain
  code-shape review candidates. This change adds no size exemption or baseline
  update; broad runtime extraction is outside the repair.

Separate incident recovery completed on November 13 at 20:59:12 MST, before
this repair: the other workstream inspected the roles, ended the existing
inhibit and ran the guarded asleep broker restart on `bb486df`. It reported no
TX delta. An independent cache-only API read at about 21:03 MST confirmed the
vehicle asleep, no active inhibits, and both helper restoration latches false.
That recovery does not prove the startup defect is fixed.

Any later activation or rollback must use the existing guarded parked/asleep
restart, without clearing an inhibit just to make deployment pass. The next natural drive, preferably with an engine
stop/restart, must demonstrate both helpers rearming, fresh helper-fed metrics,
no restoration inhibit, and raw recorder continuation. No special engine start,
CAN transmission, or vehicle actuation is authorized by the offline tests.
