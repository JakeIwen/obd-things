"""Running one admitted research attempt: a report or follow-up turn, or a Claude model check.

The attempt already holds the worker's slot and the shared CLI lock (``dtc_research_worker``
admits it). This module builds the launch from the pinned CLI copy and the tracked policy, runs it
under ``cli_process`` with the space and memory watchdogs, applies the verified-resume rule, keeps
the volume within its targets and returns a bounded outcome. Every research CLI process, including
an installer's ``--help`` check, starts through ``Runner.guarded_run``: the launch floor is checked
again just before each spawn, and a cancelled attempt never spawns. Nothing here talks HTTP.
"""

from dataclasses import dataclass
from pathlib import Path
import json
import sys
import time
import uuid

from projects.vehicle_data import cli_process, research_cli
from projects.vehicle_data.api_credits import (
    PRE_INIT_RESULT, CreditEnd, UsageTally, classify_credit_failure, scrub,
)
from projects.vehicle_data.api_credits_contract import API_CREDITS, PROTOCOL, SUBSCRIPTION
from projects.vehicle_data.api_credits_ledger import LedgerError
from projects.vehicle_data.dtc_research_contract import SIZES, TIMEOUTS, truncate_encoded
from projects.vehicle_data.research_credits import SUBSCRIPTION_PLAN
from projects.vehicle_data.research_memory import MIB, Watchdog, launch_refusal
from projects.vehicle_data.research_volume import FREE_FLOOR_MIB, PENDING_SCAN, RUN_CAP_MIB

# Stop reasons that are not turn reasons: the advisor reacts to them itself.
INTERNAL_REASONS = ("resume_unverified", PROTOCOL["fallbackReason"])
HELP_MAX_BYTES = 1024 * 1024  # a CLI's --help text is a few KiB


@dataclass
class TurnRequest:
    run_id: str
    provider: str
    model: str
    effort: str | None
    kind: str  # initial | resume
    turn_kind: str  # report | follow_up
    prompt: str
    timeout_s: int
    session_id: str | None = None
    budget_usd: float | None = None
    billing_capable: bool = False  # the advisor implements credit fallback and replay-required
    forced_billing: str | None = None


class Runner:
    def __init__(self, volume, identity, thresholds_for, clock=time.monotonic, meminfo=Path("/proc/meminfo"),
                 credits=None):
        self.volume, self.identity, self.thresholds_for, self.clock = volume, identity, thresholds_for, clock
        self.meminfo = meminfo
        self.credits = credits

    def run_turn(self, attempt, request, plan=SUBSCRIPTION_PLAN):
        """One turn. ``plan`` is the billing the worker chose at admission (maintenance runs get the
        subscription plan); a credit plan arrives with its ledger entry already open, which this
        method always settles or withdraws."""
        try:
            outcome = self._run_turn(attempt, request, plan)
        except BaseException:
            # An unexpected failure after the spawn may have spent: the entry settles as unknown.
            self._close_entry(plan, None, None, spawned=attempt.state == "running")
            raise
        outcome.setdefault("billing", plan.billing.choice)
        outcome.setdefault("credit_code", None)
        outcome.setdefault("diagnostics", {}).update(billing=outcome["billing"], credit_code=outcome["credit_code"])
        return scrub(outcome, plan.key)  # nothing that leaves the runner carries the installed key

    def _run_turn(self, attempt, request, plan):
        identity = self.identity.check(request.provider)
        token, problem = research_cli.read_token(self.volume.token)
        if identity.state != "ok" or (problem and not plan.credit):  # a credit launch never carries the token
            self._close_entry(plan, None, None, spawned=False)
            return _failed("provider_error", identity.reason or problem)
        run = self.volume.prepare_run(request.run_id)
        home, tmpdir = run / "home", self.volume.tmpdir(attempt.attempt_id)
        try:
            if request.kind == "initial":
                if not self.volume.drop_session(request.run_id):
                    self._close_entry(plan, None, None, spawned=False)
                    return _failed("provider_error", "the run's earlier session could not be removed")
                (run / "home").mkdir(parents=True, exist_ok=True, mode=0o700)
                session_id = str(uuid.uuid4())
                before = 0
            else:
                session_id = request.session_id
                resumed = self.volume.resume_session(request.run_id, session_id)
                if resumed is None or resumed[0] != plan.billing.choice:
                    self._close_entry(plan, None, None, spawned=False)
                    return _failed("session_missing", "the origin of this run's session is not recorded")
                _, before = resumed
            attempt.session_id = session_id
            marker = {"provider": "claude", "session_id": session_id, "billing": plan.billing.choice}
            # A crash anywhere after the marker is durable but before the scan leaves only replay.
            self.volume.save_session_record(request.run_id, {**marker, PENDING_SCAN: True} if plan.credit else marker)
            argv = research_cli.claude_argv(identity.executable, model=request.model, effort=request.effort,
                                             session_id=session_id, resume=request.kind == "resume",
                                             budget_usd=request.budget_usd)
            env = research_cli.claude_credit_env(home, tmpdir, plan.key) if plan.credit \
                else research_cli.claude_env(home, tmpdir, token)
            stream = research_cli.ClaudeStream(session_id, credit=plan.credit, key=plan.key)
            result, watchdog = self._launch(attempt, request, argv, env, run / "work", stream)
            outcome = self._outcome(request, stream, result, watchdog)
            self._close_entry(plan, stream, result, spawned=result.returncode is not None)
            outcome = self._credit_outcome(plan, request, stream, result, outcome)
            spawned = result.returncode is not None
            return self._finish_session(request, plan, marker, before, spawned, outcome)
        finally:
            self.volume.clear(tmpdir)
            self.volume.enforce_targets(busy_run=None)

    def _finish_session(self, request, plan, marker, before, spawned, outcome):
        session_id, size, unsafe = marker["session_id"], None, False
        if not plan.credit or spawned:
            inventory = self.volume.transcript_inventory(request.run_id, plan.key)
            size = inventory.matching_size(session_id)
            unsafe = inventory.inspection_error or inventory.marker_error or (bool(inventory.files) and size is None)
            if plan.credit:
                unsafe = unsafe or inventory.key_found or inventory.record != {**marker, PENDING_SCAN: True}
        if outcome["status"] == "complete" and request.kind == "resume" and (size is None or size <= before):
            outcome = _failed("resume_unverified", "the session file did not grow")
        if spawned and plan.credit and unsafe:
            self._retire_tainted_session(request.run_id)
        elif spawned and outcome.get("reason") in ("resume_unverified", "space_exhausted", "memory_exhausted",
                                                   PROTOCOL["fallbackReason"]):
            self.volume.drop_session(request.run_id)
            size = None
        elif spawned and plan.credit:
            # The child has been reaped and the whole inventory is verified. Only now is the
            # provenance safe to use (including for a subsequent credit resume).
            try:
                self.volume.save_session_record(request.run_id, marker)
            except OSError:
                unsafe = True  # keep the sanitized answer; do not advertise a resumable session
        outcome["session_present"] = size is not None and not unsafe
        outcome["session_id"] = session_id if outcome["session_present"] else None
        return outcome

    def _retire_tainted_session(self, run_id):
        """Retire an unsafe transcript. If poisoning fails, its pending marker still bars resume."""
        try:
            self.volume.poison_session(run_id)
        except OSError:
            pass
        self.volume.drop_session(run_id)

    def _close_entry(self, plan, stream, result, *, spawned):
        """Settle a credit launch's ledger entry from its process's own deltas, or withdraw it when
        nothing spawned. A settlement write failure keeps the answer; the entry settles as unknown at
        the next worker start."""
        if not plan.credit or self.credits is None:
            return
        if not spawned:
            self.credits.withdraw(plan)
            return
        clean = _confirmed_terminal_exit(stream, result)
        try:
            self.credits.settle(plan, stream.tally if stream else UsageTally(), clean_exit=clean)
        except LedgerError:
            print(json.dumps({"event": "dtc_research_credit_settlement_failed", "attempt_id": plan.attempt_id}),
                  file=sys.stderr, flush=True)

    def _credit_outcome(self, plan, request, stream, result, outcome):
        """A credit launch's outcome: billing noted, and a failure of the credits themselves becomes
        ``credit_fallback`` with its fixed code and the latch (the advisor relaunches on the subscription)."""
        if not plan.credit:
            return outcome
        outcome["billing"] = API_CREDITS
        if outcome["status"] != "failed":
            return outcome
        end = CreditEnd(result.stop_reason, result.returncode, False, stream.result, stream.typed_error,
                        stream.session_id is not None, result.stderr_tail)
        code = classify_credit_failure(end)
        if code is None:
            return outcome
        self.credits.latch(code, request.model)
        return {**outcome, "reason": PROTOCOL["fallbackReason"], "credit_code": code,
                "detail": "the API credits could not be used for this turn"}

    def _launch(self, attempt, request, argv, env, workdir, stream):
        def on_line(event):
            stream(event)
            attempt.phase, attempt.searches, attempt.pages = stream.phase, stream.searches, stream.pages

        return self.guarded_run(attempt, argv, cwd=workdir, env=env, stdin_bytes=request.prompt.encode(),
                                timeout_s=request.timeout_s, on_line=on_line, run_id=request.run_id)

    def guarded_run(self, attempt, argv, *, cwd, env, stdin_bytes, timeout_s, on_line, run_id=None, **options):
        """``(result, watchdog)`` for one research CLI process under every protection: the launch
        floor just before the spawn, the memory watchdog (immediate SIGKILL), the volume's space
        limits, the attempt's cancel and deadline, bounded output and process-group cleanup.
        ``options`` pass through to ``cli_process.run`` (``keep_stdin_open``, ``json_lines``, sizes)."""
        thresholds = self.thresholds_for()
        watchdog = Watchdog(thresholds, meminfo=self.meminfo)

        def before_spawn():
            refusal = launch_refusal(thresholds, self.meminfo)
            if refusal:
                raise cli_process.StopRun("memory_exhausted", refusal)

        def on_tick():
            reason = watchdog.check()
            if reason:
                raise cli_process.StopRun("memory_exhausted", reason, immediate=True)
            if self.volume.free_bytes() < FREE_FLOOR_MIB * MIB \
                    or (run_id and self.volume.run_footprint(run_id) > RUN_CAP_MIB * MIB):
                raise cli_process.StopRun("space_exhausted", "the research storage reached its limit")

        def on_spawn(pid):
            watchdog.pid = pid
            attempt.state = "running"

        result = cli_process.run(argv, cwd=cwd, env=env, stdin_bytes=stdin_bytes,
                                 deadline=self.clock() + timeout_s, cancelled=attempt.cancelled, on_line=on_line,
                                 on_tick=on_tick, on_spawn=on_spawn, before_spawn=before_spawn, **options)
        return result, watchdog

    def _outcome(self, request, stream, result, watchdog):
        diagnostics = {"exit_code": result.returncode, "stdout_bytes": result.stdout_bytes, "lines": result.lines,
                       "searches": stream.searches, "pages": stream.pages, **watchdog.figures(),
                       **stream.diagnostics()}
        if result.stop_reason == "cancelled":
            return {"status": "cancelled", "answer": None, "reason": "cancelled", "detail": "Stopped",
                    "diagnostics": diagnostics}
        if result.stop_reason == PRE_INIT_RESULT:
            outcome = _failed("provider_error", result.stop_detail)
        elif result.stop_reason:
            outcome = _failed(result.stop_reason, result.stop_detail)
        elif stream.answer and result.returncode == 0:
            cap = SIZES["reportEncodedBytes"] if request.turn_kind == "report" else SIZES["answerEncodedBytes"]
            # The SUMMARY line rides on top of the report; the advisor parses and cuts again.
            answer, truncated = truncate_encoded(stream.answer, cap + 4096)
            outcome = {"status": "complete", "answer": answer, "reason": None, "detail": None,
                       "mode": "resumed" if request.kind == "resume" else "initial", "truncated": truncated}
        elif stream.credit and stream.session_id is None and stream.result is None \
                and not (result.stderr_tail or "").strip():
            outcome = _failed("protocol_error", "Claude ended before it started and said nothing")
        else:
            outcome = _failed(stream.failure(), "the model provider did not return an answer")
        outcome["diagnostics"] = diagnostics
        return outcome

    def discover_claude(self, attempt, executable=None):
        """``(models, signed_in)`` from the pinned Claude copy (or a staged one), without a model turn."""
        token, problem = research_cli.read_token(self.volume.token)
        if executable is None:
            identity = self.identity.check("claude")
            if identity.state != "ok":
                raise RuntimeError(identity.reason)
            executable = identity.executable
        home = self.volume.catalog / attempt.attempt_id
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        found = {}

        def on_line(event):
            parsed = research_cli.parse_discovery(event)
            if parsed is not None:
                found["models"], found["signed_in"] = parsed
                raise cli_process.StopRun("discovered")

        try:
            env = research_cli.claude_env(home, home, token or "")
            if problem:
                env.pop("CLAUDE_CODE_OAUTH_TOKEN")
            result, _ = self.guarded_run(attempt, research_cli.claude_discovery_argv(executable), cwd=home, env=env,
                                         stdin_bytes=research_cli.INITIALIZE_REQUEST.encode(),
                                         timeout_s=TIMEOUTS["discoverySeconds"], on_line=on_line,
                                         keep_stdin_open=True)
        finally:
            self.volume.clear(home)
        if "models" not in found:
            raise RuntimeError(_stopped(result) or "Claude did not answer the model check")
        return found["models"], found["signed_in"] and not problem

    def help_text(self, attempt, executable, timeout_s):
        """A CLI's ``--help`` text, with no credentials, under the same guarded launch as a turn."""
        home = self.volume.tmpdir(attempt.attempt_id)
        lines = []
        try:
            result, _ = self.guarded_run(attempt, [str(executable), "--help"], cwd=home,
                                         env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, stdin_bytes=b"",
                                         timeout_s=timeout_s, on_line=lines.append, json_lines=False,
                                         max_line=HELP_MAX_BYTES, max_total=HELP_MAX_BYTES)
        finally:
            self.volume.clear(home)
        if result.stop_reason:
            raise RuntimeError(_stopped(result))
        return "\n".join(lines)


def _confirmed_terminal_exit(stream, result):
    """True when the process reported a terminal result and ended on its own: a signal death or an
    exit without a terminal result leaves the spend beyond the reported deltas unknown. A pre-init
    failure result we stopped ourselves counts, since it is the process's own final word."""
    if result is None or stream is None or stream.result is None or not stream.ended_on_result:
        return False  # work after the last result means spend the deltas never reported
    if result.stop_reason == PRE_INIT_RESULT:
        return True
    return result.stop_reason is None and result.returncode is not None and result.returncode >= 0


def _failed(reason, detail):
    return {"status": "failed", "answer": None, "reason": reason, "detail": detail}


def _stopped(result):
    """Why a run was stopped, as a sentence, or ``None`` when it was not."""
    if result.stop_reason in (None, "discovered"):
        return None
    return f"{result.stop_reason}: {result.stop_detail}" if result.stop_detail else result.stop_reason
