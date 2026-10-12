"""Owner-started ACC radar Service Drive Alignment: shared contract and run policy.

This is actuation: routine 0x0251 recalibrates the forward radar while the van
is driven. It runs only after an explicit dashboard request relayed by the
broker, only inside an armed engine-running interval, and only starts with the
van stationary. The sequence is the one that completed on 2026-06-27
(projects/radar/findings/adjustment_1_results_3.md): extended session, start
the routine once, hold the session with TesterPresent, read the progress byte.
A second ``10 03`` would reset a running routine, so it is sent once per run.

The frames themselves are in radar_service_transport.py. This module holds
what the helper, the broker and the dashboard must agree on (state names, end
reasons and API path from the JSON file beside it; the helper's event fields
and the command line format) and the helper's run state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
import enum
import json
import math
import os
from pathlib import Path
import re
import sys

from lib.timeutil import finite_number
from projects.vehicle_data.radar_service_transport import (
    ENTER_SESSION, KEEP_ALIVE, NEGATIVE, POSITIVE, READ_STATUS, ROUTINE_COMPLETED, ROUTINE_IDLE,
    SKIPPED, START_ROUTINE, STOP_ROUTINE, Exchange, RadarServiceTransport, Reply, RoutineStatus,
)


VOCABULARY = json.loads(Path(__file__).with_suffix(".json").read_text())
EVENT_TYPE = VOCABULARY["event_type"]
API_PATH = VOCABULARY["path"]
LISTENER_DISABLED = VOCABULARY["listener_disabled_reason"]
STATES = VOCABULARY["states"]
IDLE, REQUESTED, STARTING, RUNNING, STOPPING, COMPLETED, STOPPED, FAILED = (
    STATES[name] for name in (
        "idle", "requested", "starting", "running", "stopping", "completed", "stopped", "failed",
    )
)
ACTIVE_STATES = frozenset(VOCABULARY["active_states"])
REASONS = frozenset(VOCABULARY["reasons"])
# Broker-to-helper handshake, like --enable-oil-life: only a broker that
# understands the events below passes it, and only then is stdin read.
ENABLE_FLAG = "--enable-radar-service-alignment"

# One command per stdin line, broker to helper.
START_COMMAND, STOP_COMMAND = "start", "stop"
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
# Fields of every helper event besides ``type``. The broker rejects an event
# outside these limits and ends the helper, so the emitter enforces them.
EVENT_FIELDS = (
    "state", "reason", "detail", "supported", "request_id", "progress", "routine_status",
    "elapsed_seconds", "speed_mph", "interface_mode",
)
MAX_DETAIL_CHARS = 400
MAX_SPEED_MPH = 200.0

STATIONARY_MAX_MPH = 0.5
# No fixed duration: the June run needed 17 minutes of steady driving. A run
# ends when progress has not advanced for this long, as the June script did.
STALL_SECONDS = 600.0
MAX_RUN_SECONDS = 3600.0
# The radar leaves the extended session about 5 s after the last request. With
# nothing heard for longer than that the routine cannot still be running.
SILENCE_SECONDS = 8.0
SKIPPED_STEP_LIMIT = 5
LOST_ROUTINE_READS = 3
HEARTBEAT_SECONDS = 5.0
START_NRC_HINTS = {
    0x22: "conditions not correct",
    0x24: "request sequence error (already running?)",
    0x31: "request out of range",
    0x33: "security access denied",
    0x7F: "not supported in the active session (the session timed out)",
}
HOLD_PURPOSES = KEEP_ALIVE.purposes + READ_STATUS.purposes
EVIDENCE_ABSENT = "radar service alignment ended: fresh running evidence absent"


def encode_command(name: str, request_id: str) -> str:
    return json.dumps({"command": name, "request_id": request_id}, separators=(",", ":")) + "\n"


def parse_command(line: bytes) -> dict[str, str] | None:
    try:
        command = json.loads(line)
    except (UnicodeDecodeError, ValueError):
        return None
    if (not isinstance(command, dict) or set(command) != {"command", "request_id"}
            or command["command"] not in (START_COMMAND, STOP_COMMAND)
            or not isinstance(command["request_id"], str)
            or not REQUEST_ID.fullmatch(command["request_id"])):
        return None
    return command


class CommandReader:
    """Non-blocking reader of the broker's command lines on the helper's stdin."""

    MAX_BUFFER = 1024

    def __init__(self, descriptor: int):
        os.set_blocking(descriptor, False)
        self.descriptor = descriptor
        self.buffer = b""
        self.closed = False

    def poll(self) -> list[dict[str, str]]:
        if self.closed:
            return []
        try:
            chunk = os.read(self.descriptor, 4096)
        except BlockingIOError:
            return []
        except OSError:
            chunk = b""
        if not chunk:
            self.closed = True
            return []
        *lines, self.buffer = (self.buffer + chunk).split(b"\n")
        if len(self.buffer) > self.MAX_BUFFER:
            self.buffer = b""
        commands = []
        for line in lines:
            command = parse_command(line)
            if command is None:
                print("ignored an invalid radar service command line", file=sys.stderr, flush=True)
            else:
                commands.append(command)
        return commands


def _speed_mph(snapshot: object) -> float | None:
    for observation in getattr(snapshot, "observations", ()):
        if getattr(observation, "metric", None) == "vehicle.speed":
            value = getattr(observation, "value", None)
            if finite_number(value) and 0 <= value <= MAX_SPEED_MPH:
                return float(value)
    return None


class Phase(enum.Enum):
    """What the next cycle does for the current run."""

    SESSION = "enter the extended session"
    START = "start the routine"
    HOLD = "hold the session and read progress"
    STOP = "stop the routine"


PHASE_STATE = {Phase.SESSION: STARTING, Phase.START: STARTING, Phase.HOLD: RUNNING,
               Phase.STOP: STOPPING}


@dataclass
class _Run:
    """Bookkeeping for one requested run; a new request starts from a new one."""

    request_id: str
    progress: int | None = None
    routine_status: str | None = None
    started: float | None = None
    best: int = -1
    best_at: float = 0.0
    heard_at: float = 0.0
    lost_reads: int = 0
    skipped: int = 0
    ending: tuple[str, str, str] | None = None


class ServiceAlignment:
    """One routine run at a time: start sequence, session hold, progress, end.

    ``authority.acquire(purposes, absent_detail=...)`` returns one-use permits
    from fresh running evidence, or None when the evidence aged out and nothing
    may be sent this cycle. When the helper session itself must end it raises,
    and the caller reports that through :meth:`session_ending`.
    """

    def __init__(self, link, sink, authority, commands, *, monotonic):
        self.transport = None if link is None else RadarServiceTransport(link)
        self.sink = sink
        self.authority = authority
        self.commands = commands
        self.monotonic = monotonic
        self.phase: Phase | None = None
        self.state = IDLE
        self.reason = None
        self.detail = "no service alignment has run in this engine-running interval"
        self.run: _Run | None = None
        self.speed_mph = None
        self._emitted = None
        self._emitted_at = -math.inf
        self._phases = {
            Phase.SESSION: self._enter_session, Phase.START: self._start_routine,
            Phase.HOLD: self._hold, Phase.STOP: self._stop_routine,
        }

    # -- events ----------------------------------------------------------

    def _emit(self) -> None:
        """Report a change at once, an unchanged state as a slow heartbeat."""
        run, now = self.run, self.monotonic()
        signature = (self.state, self.reason,
                     run and (run.request_id, run.progress, run.routine_status))
        if signature == self._emitted and now - self._emitted_at < HEARTBEAT_SECONDS:
            return
        self._emitted, self._emitted_at = signature, now
        started = run.started if run else None
        self.sink.emit(
            EVENT_TYPE, state=self.state, reason=self.reason,
            detail=self.detail[:MAX_DETAIL_CHARS], supported=self.transport is not None,
            request_id=run.request_id if run else None,
            progress=run.progress if run else None,
            routine_status=run.routine_status if run else None,
            elapsed_seconds=None if started is None else round(now - started, 1),
            speed_mph=self.speed_mph, interface_mode="armed_diagnostic",
        )

    def _enter(self, phase: Phase, detail: str) -> None:
        self.phase, self.state, self.reason, self.detail = phase, PHASE_STATE[phase], None, detail
        self.run.skipped = 0
        self._emit()

    def _finish(self, state: str, reason: str | None, detail: str) -> None:
        self.phase, self.state, self.reason, self.detail = None, state, reason, detail
        self._emit()

    def _fail(self, reason: str, detail: str) -> None:
        self._finish(FAILED, reason, detail)

    def _begin_stop(self, state: str, reason: str, detail: str) -> None:
        self.run.ending = (state, reason, detail)
        self._enter(Phase.STOP, detail)

    # -- commands --------------------------------------------------------

    def _accept(self, command: dict[str, str]) -> None:
        if command["command"] == STOP_COMMAND:
            if self.run is None or command["request_id"] != self.run.request_id:
                return
            if self.phase is Phase.HOLD:
                self._begin_stop(STOPPED, "owner_stopped", "stopped from the dashboard")
            elif self.phase in (Phase.SESSION, Phase.START):
                self._finish(STOPPED, "owner_stopped",
                             "stopped from the dashboard before the routine started")
            return
        if self.phase is not None:
            return
        self.run = _Run(command["request_id"])
        if self.transport is None:
            return self._fail("not_supported", "the radar transport is not available in this interval")
        if self.speed_mph is None or self.speed_mph > STATIONARY_MAX_MPH:
            shown = "unknown" if self.speed_mph is None else f"{self.speed_mph:.1f} mph"
            return self._fail("start_refused_moving",
                              f"start requires a stationary van; vehicle speed is {shown}")
        self._enter(Phase.SESSION, "entering the radar's extended diagnostic session")

    # -- phases ----------------------------------------------------------

    def _step(self, spec: Exchange) -> Reply | None:
        """Reply to one start/stop exchange, or None while a skipped send may be retried."""
        permits = self.authority.acquire(spec.purposes, absent_detail=EVIDENCE_ABSENT)
        if permits is not None:
            return self.transport.exchange(spec, *permits)
        self.run.skipped += 1
        if self.run.skipped < SKIPPED_STEP_LIMIT:
            return None
        return Reply(SKIPPED, detail=f"{spec.name}: running evidence stayed stale")

    def _enter_session(self) -> None:
        reply = self._step(ENTER_SESSION)
        if reply is None:
            return
        if reply.kind == POSITIVE:
            self._enter(Phase.START, "extended session entered; starting routine 0251")
        else:
            self._fail("session_rejected" if reply.kind == NEGATIVE else "no_response", reply.detail)

    def _start_routine(self) -> None:
        reply = self._step(START_ROUTINE)
        if reply is None:
            return
        if reply.kind == POSITIVE:
            run = self.run
            run.started = run.best_at = run.heard_at = self.monotonic()
            self._enter(Phase.HOLD, "routine started; drive straight and steady until it completes")
        elif reply.kind == NEGATIVE:
            hint = START_NRC_HINTS.get(reply.nrc)
            self._fail("start_rejected", reply.detail + (f" ({hint})" if hint else ""))
        else:
            self._fail("no_response", reply.detail)

    def _hold(self) -> None:
        run, now = self.run, self.monotonic()
        if now - run.started >= MAX_RUN_SECONDS:
            return self._begin_stop(FAILED, "time_limit", "stopped at the one-hour limit")
        if now - run.best_at >= STALL_SECONDS:
            return self._begin_stop(
                FAILED, "stalled",
                f"progress stayed at {max(run.best, 0)}% for {STALL_SECONDS / 60:g} minutes")
        permits = self.authority.acquire(HOLD_PURPOSES, absent_detail=EVIDENCE_ABSENT)
        if permits is not None:
            keepalive = self.transport.exchange(KEEP_ALIVE, permits[0])
            status = self.transport.exchange(READ_STATUS, permits[1], permits[2])
            if keepalive.kind == POSITIVE or status.kind in (POSITIVE, NEGATIVE):
                run.heard_at = self.monotonic()
            if status.kind in (POSITIVE, NEGATIVE):
                return self._routine_reply(status)
        if self.monotonic() - run.heard_at >= SILENCE_SECONDS:
            self._fail("no_response",
                       f"the radar has not answered for {SILENCE_SECONDS:g} s; its session has ended")

    def _routine_reply(self, status: Reply) -> None:
        run = self.run
        if status.kind == NEGATIVE:
            return self._routine_lost("routine_reset", status.detail)
        routine = RoutineStatus.from_reply(status)
        run.routine_status = routine.text
        if routine.state == ROUTINE_IDLE:
            return self._routine_lost("routine_reset", f"routine reports idle ({routine.text})")
        if routine.state == ROUTINE_COMPLETED and routine.progress != 100:
            return self._routine_lost("routine_ended", f"routine ended below 100% ({routine.text})")
        run.lost_reads = 0
        if routine.progress is not None:
            run.progress = routine.progress
            if routine.progress > run.best:
                run.best, run.best_at = routine.progress, self.monotonic()
        if routine.state == ROUTINE_COMPLETED:
            # The June run committed in the sample that first read 03/100%.
            # Sending nothing further lets the session lapse on its own.
            return self._finish(COMPLETED, None, f"routine completed at 100% ({routine.text})")
        self._emit()

    def _routine_lost(self, reason: str, detail: str) -> None:
        self.run.lost_reads += 1
        if self.run.lost_reads >= LOST_ROUTINE_READS:
            self._fail(reason, detail)

    def _stop_routine(self) -> None:
        reply = self._step(STOP_ROUTINE)
        if reply is not None:
            state, reason, detail = self.run.ending
            self._finish(state, reason, f"{detail}; stop request {reply.kind}")

    # -- entry points ----------------------------------------------------

    def run_cycle(self, snapshot: object) -> None:
        """Take queued commands, then advance the current run by one step."""
        self.speed_mph = _speed_mph(snapshot)
        if self._emitted is None:
            self._emit()
        for command in self.commands.poll():
            self._accept(command)
        if self.phase is not None:
            self._phases[self.phase]()

    def session_ending(self, detail: str) -> None:
        """The helper session is ending now; an active run ends with it."""
        if self.phase is not None:
            self._fail("helper_ended", detail)
