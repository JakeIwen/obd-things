"""Running one admitted research attempt: a report or follow-up turn, or a Claude model check.

The attempt already holds the worker's slot and the shared CLI lock (``dtc_research_worker``
admits it). This module builds the launch from the pinned CLI copy and the tracked policy, runs it
under ``cli_process`` with the space and memory watchdogs, applies the verified-resume rule, keeps
the volume within its targets and returns a bounded outcome. Nothing here talks HTTP.
"""

from dataclasses import dataclass
from pathlib import Path
import time
import uuid

from projects.vehicle_data import cli_process, research_cli
from projects.vehicle_data.dtc_research_contract import SIZES, TIMEOUTS, truncate_encoded
from projects.vehicle_data.research_memory import MIB, Watchdog
from projects.vehicle_data.research_volume import FREE_FLOOR_MIB, RUN_CAP_MIB

# Stop reasons that are not turn reasons: the advisor reacts to them itself.
INTERNAL_REASONS = ("resume_unverified",)


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


class Runner:
    def __init__(self, volume, identity, thresholds_for, clock=time.monotonic, meminfo=Path("/proc/meminfo")):
        self.volume, self.identity, self.thresholds_for, self.clock = volume, identity, thresholds_for, clock
        self.meminfo = meminfo

    def run_turn(self, attempt, request):
        identity = self.identity.check(request.provider)
        token, problem = research_cli.read_token(self.volume.token)
        if identity.state != "ok" or problem:
            return _failed("provider_error", identity.reason or problem)
        run = self.volume.prepare_run(request.run_id)
        home, tmpdir = run / "home", self.volume.tmpdir(attempt.attempt_id)
        try:
            if request.kind == "initial":
                self.volume.drop_session(request.run_id)
                (run / "home").mkdir(parents=True, exist_ok=True, mode=0o700)
                session_id = str(uuid.uuid4())
                before = 0
            else:
                session_id = request.session_id
                before = research_cli.session_size(research_cli.claude_session_files(home, session_id))
            attempt.session_id = session_id
            self.volume.save_session_record(request.run_id, {"provider": "claude", "session_id": session_id})
            argv = research_cli.claude_argv(identity.executable, model=request.model, effort=request.effort,
                                             session_id=session_id, resume=request.kind == "resume",
                                             budget_usd=request.budget_usd)
            env = research_cli.claude_env(home, tmpdir, token)
            stream, result, watchdog = self._launch(attempt, request, argv, env, run / "work",
                                                    research_cli.ClaudeStream(session_id))
            outcome = self._outcome(request, stream, result, watchdog)
            if outcome["status"] == "complete" and request.kind == "resume":
                after = research_cli.session_size(research_cli.claude_session_files(home, session_id))
                if after <= before:
                    outcome = _failed("resume_unverified", "the session file did not grow")
            if outcome.get("reason") in ("resume_unverified", "space_exhausted", "memory_exhausted"):
                self.volume.drop_session(request.run_id)
            outcome["session_present"] = bool(research_cli.claude_session_files(home, session_id))
            outcome["session_id"] = session_id if outcome["session_present"] else None
            return outcome
        finally:
            self.volume.clear(tmpdir)
            self.volume.enforce_targets(busy_run=None)

    def _launch(self, attempt, request, argv, env, workdir, stream):
        watchdog = Watchdog(self.thresholds_for(), meminfo=self.meminfo)

        def on_line(event):
            stream(event)
            attempt.phase, attempt.searches, attempt.pages = stream.phase, stream.searches, stream.pages
            if attempt.state == "accepted":
                attempt.state = "running"

        def on_tick():
            reason = watchdog.check()
            if reason:
                raise cli_process.StopRun("memory_exhausted", reason, immediate=True)
            if self.volume.free_bytes() < FREE_FLOOR_MIB * MIB \
                    or self.volume.run_footprint(request.run_id) > RUN_CAP_MIB * MIB:
                raise cli_process.StopRun("space_exhausted", "the research storage reached its limit")

        def on_spawn(pid):
            watchdog.pid = pid
            attempt.state = "running"

        result = cli_process.run(argv, cwd=workdir, env=env, stdin_bytes=request.prompt.encode(),
                                 deadline=self.clock() + request.timeout_s, cancelled=attempt.cancelled,
                                 on_line=on_line, on_tick=on_tick, on_spawn=on_spawn)
        return stream, result, watchdog

    def _outcome(self, request, stream, result, watchdog):
        diagnostics = {"exit_code": result.returncode, "stdout_bytes": result.stdout_bytes, "lines": result.lines,
                       "searches": stream.searches, "pages": stream.pages, **watchdog.figures(),
                       **stream.diagnostics()}
        if result.stop_reason == "cancelled":
            return {"status": "cancelled", "answer": None, "reason": "cancelled", "detail": "Stopped",
                    "diagnostics": diagnostics}
        if result.stop_reason:
            outcome = _failed(result.stop_reason, result.stop_detail)
        elif stream.answer and result.returncode == 0:
            cap = SIZES["reportEncodedBytes"] if request.turn_kind == "report" else SIZES["answerEncodedBytes"]
            # The SUMMARY line rides on top of the report; the advisor parses and cuts again.
            answer, truncated = truncate_encoded(stream.answer, cap + 4096)
            outcome = {"status": "complete", "answer": answer, "reason": None, "detail": None,
                       "mode": "resumed" if request.kind == "resume" else "initial", "truncated": truncated}
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
            cli_process.run(research_cli.claude_discovery_argv(executable), cwd=home, env=env,
                            stdin_bytes=research_cli.INITIALIZE_REQUEST.encode(),
                            deadline=self.clock() + TIMEOUTS["discoverySeconds"], cancelled=attempt.cancelled,
                            on_line=on_line, keep_stdin_open=True)
        finally:
            self.volume.clear(home)
        if "models" not in found:
            raise RuntimeError("Claude did not answer the model check")
        return found["models"], found["signed_in"] and not problem


def _failed(reason, detail):
    return {"status": "failed", "answer": None, "reason": reason, "detail": detail}
