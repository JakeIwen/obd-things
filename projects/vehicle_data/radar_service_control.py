"""Broker side of the owner-started radar Service Drive Alignment.

The broker never touches CAN for this. Its ``RadarServiceControl`` builds the
active-drive helper's supervisor with the feature flag and a stdin pipe added,
relays one validated start or stop command, records what the helper reports,
and answers the local API. The helper repeats every vehicle gate itself
(radar_service_alignment.py).

Run events are appended to ``tmp/radar/service_alignment/`` so a run can be
reviewed beside the drive recorder's raw capture.
"""

from __future__ import annotations

import dataclasses
import json
import math
import pathlib
import re
import subprocess
import threading
import time
import uuid

from lib.timeutil import finite_number, utc_now
from projects.vehicle_data.http_common import exact_json
from projects.vehicle_data.radar_service_alignment import (
    ACTIVE_STATES, API_PATH, COMPLETED, ENABLE_FLAG, EVENT_FIELDS, EVENT_TYPE, FAILED, IDLE,
    MAX_DETAIL_CHARS, MAX_RUN_SECONDS, MAX_SPEED_MPH, REASONS, REQUEST_ID, REQUESTED, RUNNING,
    STALL_SECONDS, STARTING, START_COMMAND, STATIONARY_MAX_MPH, STOPPED, STOPPING, STOP_COMMAND,
    VOCABULARY, encode_command,
)


REPO = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = REPO / "tmp" / "radar" / "service_alignment"
START_REQUEST = VOCABULARY["start_request"]
STOP_REQUEST = VOCABULARY["stop_request"]
HELPER_STATES = frozenset((IDLE, STARTING, RUNNING, STOPPING, COMPLETED, STOPPED, FAILED))
EVENT_KEYS = frozenset(("type", *EVENT_FIELDS))
ROUTINE_STATUS = re.compile(r"[0-9A-F]{2}(?: [0-9A-F]{2}){3}")
# The helper reads its pipe once per cycle (about a second, longer under load).
REQUEST_ACK_SECONDS = 15.0
START_MIN_INTERVAL_SECONDS = 10.0
LIMITS = {"stationary_max_mph": STATIONARY_MAX_MPH, "stall_seconds": STALL_SECONDS,
          "max_run_seconds": MAX_RUN_SECONDS}


def validate_event(event: dict[str, object]) -> dict[str, object]:
    """Strictly check one helper event; a malformed one ends the helper."""
    if set(event) != EVENT_KEYS or event["interface_mode"] != "armed_diagnostic":
        raise ValueError("radar service event has unexpected fields or interface mode")
    if event["state"] not in HELPER_STATES or type(event["supported"]) is not bool:
        raise ValueError("radar service event state is invalid")
    reason, detail, request_id = event["reason"], event["detail"], event["request_id"]
    if reason is not None and reason not in REASONS:
        raise ValueError("radar service event reason is not in the shared vocabulary")
    if not isinstance(detail, str) or len(detail) > MAX_DETAIL_CHARS:
        raise ValueError("radar service event detail is invalid")
    if request_id is not None and not (isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id)):
        raise ValueError("radar service event request id is invalid")
    progress, routine_status = event["progress"], event["routine_status"]
    if progress is not None and (type(progress) is not int or not 0 <= progress <= 100):
        raise ValueError("radar service event progress is invalid")
    if routine_status is not None and not (
            isinstance(routine_status, str) and ROUTINE_STATUS.fullmatch(routine_status)):
        raise ValueError("radar service event routine status is invalid")
    for name, limit in (("elapsed_seconds", MAX_RUN_SECONDS + 3600), ("speed_mph", MAX_SPEED_MPH)):
        value = event[name]
        if value is not None and not (finite_number(value) and 0 <= value <= limit):
            raise ValueError(f"radar service event {name} is invalid")
    return event


@dataclasses.dataclass
class TrackedRun:
    """One requested run as the API reports it (``asdict`` is the ``run`` payload)."""

    request_id: str
    requested_at: str
    updated_at: str
    state: str = REQUESTED
    reason: str | None = None
    detail: str = "start request sent to the helper"
    progress: int | None = None
    routine_status: str | None = None
    elapsed_seconds: float | None = None
    speed_mph: float | None = None
    finished_at: str | None = None
    stop_requested: bool = False

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def _set(self, state: str, reason: str | None, detail: str, stamp: str) -> None:
        self.state, self.reason, self.detail, self.updated_at = state, reason, detail, stamp
        self.finished_at = None if self.active else stamp

    def apply(self, event: dict[str, object], stamp: str) -> None:
        """Adopt the helper's report; a reading it omits keeps its last value."""
        for name in ("progress", "routine_status", "elapsed_seconds", "speed_mph"):
            if event[name] is not None:
                setattr(self, name, event[name])
        self._set(event["state"], event["reason"], event["detail"], stamp)

    def fail(self, reason: str, detail: str, stamp: str) -> None:
        self._set(FAILED, reason, detail, stamp)


class HelperCommandPipe:
    """Starts the helper with the feature flag and stdin pipe; sends command lines."""

    def __init__(self, popen_factory=subprocess.Popen, *, on_start=lambda: None):
        self._popen_factory = popen_factory
        self._on_start = on_start
        self._lock = threading.Lock()
        self._process = None

    def popen(self, command, **kwargs):
        process = self._popen_factory([*command, ENABLE_FLAG], stdin=subprocess.PIPE, **kwargs)
        with self._lock:
            self._process = process
        self._on_start()
        return process

    def alive(self) -> bool:
        with self._lock:
            process = self._process
        try:
            return process is not None and process.poll() is None
        except OSError:
            return False

    def send(self, line: str) -> bool:
        with self._lock:
            process = self._process
            try:
                if process is None or process.poll() is not None or process.stdin is None:
                    return False
                process.stdin.write(line)
                process.stdin.flush()
            except (OSError, ValueError):
                return False
            return True


class RadarServiceControl:
    """One tracked service-alignment run and the command pipe to the helper."""

    def __init__(self, *, log_dir=DEFAULT_LOG_DIR, popen_factory=subprocess.Popen,
                 monotonic=time.monotonic):
        self._pipe = HelperCommandPipe(popen_factory, on_start=self._helper_started)
        self._log_dir = pathlib.Path(log_dir) if log_dir is not None else None
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._supported = False
        self._run: TrackedRun | None = None
        self._requested_monotonic = -math.inf
        self._log_path: pathlib.Path | None = None
        self._log_error: str | None = None

    # -- helper wiring ---------------------------------------------------

    def supervisor(self, supervisor_factory, *, event_handler, **kwargs):
        """The helper supervisor, with the command pipe and run-event routing added."""
        def route(event):
            if isinstance(event, dict) and event.get("type") == EVENT_TYPE:
                return self.handle_event(event)
            return event_handler(event)
        return supervisor_factory(event_handler=route, popen_factory=self._pipe.popen, **kwargs)

    def _helper_started(self) -> None:
        with self._lock:
            self._supported = False

    def handle_event(self, event: dict[str, object]) -> None:
        validate_event(event)
        with self._lock:
            self._supported = event["supported"]
            run = self._run
            if run is None or event["request_id"] != run.request_id:
                return
            # Also applies after the broker gave up waiting: a late report from
            # the helper is the truth about a routine that did start.
            run.apply(event, utc_now())
            self._log({"at": run.updated_at, **event})

    # -- tracking (lock held) --------------------------------------------

    def _log(self, record: dict[str, object]) -> None:
        if self._log_path is None:
            return
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._log_error = str(exc)
        else:
            self._log_error = None

    def _fail(self, reason: str, detail: str) -> None:
        self._run.fail(reason, detail, utc_now())
        self._log({"at": self._run.updated_at, "type": "broker", "state": FAILED,
                   "reason": reason, "detail": detail})

    def _settle(self) -> None:
        """End a tracked run the helper can no longer report on."""
        run = self._run
        if run is None or not run.active:
            return
        if not self._pipe.alive():
            self._fail("helper_ended", "the engine-running interval ended before the routine reported an end")
        elif (run.state == REQUESTED
              and self._monotonic() - self._requested_monotonic > REQUEST_ACK_SECONDS):
            self._fail("not_delivered", "the helper did not acknowledge the request")

    def _helper(self) -> dict[str, object]:
        """Whether the helper could take a start now, and the sentence that says why not."""
        running = self._pipe.alive()
        ready = running and self._supported
        if ready:
            detail = "the engine-running helper is armed and can run the routine"
        elif running:
            detail = "the helper is starting or cannot reach the radar in this interval"
        else:
            detail = "the engine is not running, so the telemetry helper is not armed"
        return {"running": running, "ready": ready, "detail": detail}

    def _status(self, **refusal: object) -> dict[str, object]:
        """The API body; ``refusal`` adds ``available=False`` with a reason and detail."""
        return {
            "available": not refusal,
            "helper": self._helper(),
            "run": None if self._run is None else dataclasses.asdict(self._run),
            "limits": LIMITS,
            "log_error": self._log_error,
            **refusal,
        }

    def _refused(self, code: int, reason: str, detail: str) -> tuple[int, dict[str, object]]:
        return code, self._status(reason=reason, detail=detail)

    # -- local API -------------------------------------------------------

    def status_response(self) -> tuple[int, dict[str, object]]:
        with self._lock:
            self._settle()
            return 200, self._status()

    def request(self, payload: object) -> tuple[int, dict[str, object]]:
        with self._lock:
            self._settle()
            if exact_json(payload, START_REQUEST):
                return self._start()
            if exact_json(payload, STOP_REQUEST):
                return self._stop()
            return self._refused(400, "invalid_request",
                                 "body must be the exact start or stop request")

    def _start(self) -> tuple[int, dict[str, object]]:
        if self._run is not None and self._run.active:
            return self._refused(409, "already_active", "a service alignment is already active")
        helper = self._helper()
        if not helper["ready"]:
            return self._refused(409, "helper_not_ready", helper["detail"])
        now = self._monotonic()
        if now - self._requested_monotonic < START_MIN_INTERVAL_SECONDS:
            return self._refused(429, "rate_limited", "wait a few seconds before starting again")
        stamp = utc_now()
        run = self._run = TrackedRun(uuid.uuid4().hex, requested_at=stamp, updated_at=stamp)
        self._requested_monotonic = now
        if self._log_dir is not None:
            name = re.sub(r"[^0-9T]", "", stamp.split(".")[0])
            self._log_path = self._log_dir / f"{name}Z_{run.request_id[:8]}.jsonl"
        self._log({"at": stamp, "type": "broker", "state": REQUESTED, "request_id": run.request_id})
        if not self._pipe.send(encode_command(START_COMMAND, run.request_id)):
            self._fail("not_delivered", "the start request could not be written to the helper")
            return self._refused(503, "not_delivered", run.detail)
        return 202, self._status()

    def _stop(self) -> tuple[int, dict[str, object]]:
        run = self._run
        if run is None or not run.active:
            return self._refused(409, "not_active", "no service alignment is active")
        if not self._pipe.send(encode_command(STOP_COMMAND, run.request_id)):
            return self._refused(503, "not_delivered", "the stop request could not be written to the helper")
        run.stop_requested = True
        self._log({"at": utc_now(), "type": "broker", "state": "stop_requested"})
        return 202, self._status()
