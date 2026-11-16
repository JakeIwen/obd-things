"""Opt-in, stage-1a DTC reads inside the existing active-drive ownership epoch.

The helper retains all CAN lifecycle effects and calls begin_cycle even when
an ordinary metric skips the rest of that cycle. A pass starts before fresh
send authority is acquired; stdout can never age an already-issued permit.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from lib.timeutil import finite_number, utc_now
from projects.vehicle_data import vehicle_speed, drive_dtc_contract as contract
from projects.vehicle_data import drive_dtc_transport, drive_dtc_watch
from projects.vehicle_data.radar_drive_steps import SessionEnding


@dataclass(frozen=True)
class Reservation:
    session: str
    origin: float
    first_due: float

    def __post_init__(self):
        if not contract.session_id_valid(self.session):
            raise ValueError("invalid drive-DTC reservation identity")
        contract.window_start(self.origin, self.first_due, 0)


def add_arguments(parser):
    parser.add_argument("--enable-drive-dtc", action="store_true",
                        help="internal: broker reserved the optional drive DTC capability")
    parser.add_argument("--drive-dtc-reservation")
    parser.add_argument("--drive-dtc-origin", type=float)
    parser.add_argument("--drive-dtc-first-due", type=float)


def reservation_from_arguments(args):
    """The reservation, or None; refuses an enabled launch without the startup grant.

    The capability is confirmed only through the grant pipe's ready event, so a
    launch with DTC arguments but no grant descriptor (not the broker's) would
    send unconfirmed. The legacy ungated start stays valid only with DTC off.
    """
    supplied = (args.drive_dtc_reservation, args.drive_dtc_origin, args.drive_dtc_first_due)
    if not args.enable_drive_dtc:
        if any(value is not None for value in supplied):
            raise ValueError("drive-DTC schedule requires --enable-drive-dtc")
        return None
    if getattr(args, "startup_grant_fd", None) is None:
        raise ValueError("--enable-drive-dtc requires the supervisor's --startup-grant-fd")
    return Reservation(*supplied)


def capabilities(reservation):
    return () if reservation is None else (contract.CAPABILITY,)


@dataclass
class _Pass:
    index: int
    window_end: float
    attempted: set[str] = field(default_factory=set)  # sent, or possibly sent
    settled: set[str] = field(default_factory=set)  # no further try in this pass
    last_attempt: float | None = None  # any module's latest try, sent or not
    tries: dict[str, int] = field(default_factory=dict)  # retryable not-sent tries
    last_try: dict[str, float] = field(default_factory=dict)
    held: dict[str, dict] = field(default_factory=dict)  # latest unrecorded retry


class DriveDtcSteps:
    """One module per cycle, bounded windows and a monotonic hour between passes.

    The hour holds twice within a session: between the starts of actual passes
    and between any two requests to the same module. A module that cannot be
    requested in a pass (no usable radar link, or its own hour would end after
    the window) gets one declared not-sent outcome there and never blocks the
    other; a pass that could request neither is not started and costs no hour.
    """

    def __init__(self, backend, sink, radar_steps, *, reservation=None,
                 rf_hub_profiles=(), watch_factory=None, transport_factory=None, wall_now=utc_now):
        self.backend, self.sink, self.radar = backend, sink, radar_steps
        self.reservation, self.wall_now = reservation, wall_now
        self.enabled = reservation is not None
        self.watch = None
        self.transport = None
        self.current = None
        self.finished_window = -1
        self.last_started = None
        self.last_request: dict[str, float] = {}  # per module, this session
        if not self.enabled:
            return
        self.minimum_start = backend.monotonic() + contract.MINIMUM_SESSION_SECONDS
        self.watch = (watch_factory or drive_dtc_watch.ForeignTesterWatch)(
            backend.channel, rf_hub_profiles=rf_hub_profiles, monotonic=backend.monotonic)
        try:
            self.watch.open()
        except (OSError, ValueError):
            self._disable("sentinel_unavailable")
            return
        try:
            self.transport = (transport_factory or drive_dtc_transport.DriveDtcTransport)(
                backend.channel, radar_link=radar_steps.poller, monotonic=backend.monotonic)
            self._emit(contract.STEP_EVENT, state="enabled", reason="capability",
                       detail="broker reserved stage-1a drive DTC reads")
        except BaseException:
            self.watch.close()
            raise

    def _emit(self, event_type, **payload):
        event = dict(type=event_type, session=self.reservation.session,
                     interface_mode=contract.INTERFACE_MODE, **payload)
        try:
            contract.validate_event(event)
        except ValueError:
            self.enabled = False
            self.current = None
            disabled = dict(type=contract.STEP_EVENT, session=self.reservation.session,
                            interface_mode=contract.INTERFACE_MODE, state="disabled",
                            reason="contract_error", detail="drive DTC event failed its contract")
            try:
                contract.validate_event(disabled)
            except ValueError:
                return False
            self.sink.emit(disabled.pop("type"), **disabled)
            return False
        self.sink.emit(event.pop("type"), **event)
        return True

    def _finish(self, reason):
        current = self.current
        if current is None:
            return
        # A suppressed retry is the module's final outcome once the pass ends.
        held, current.held = list(current.held.values()), {}
        for payload in held:
            if not self._emit(contract.READ_EVENT, **payload):
                return
        self.finished_window = current.index
        self.current = None
        self._emit(contract.PASS_EVENT, **{"pass": current.index}, state="finished",
                   reason=reason, detail="drive DTC pass ended")

    def _disable(self, reason):
        self.enabled = False
        self._emit(contract.STEP_EVENT, state="disabled", reason=reason,
                   detail="drive DTC reads disabled for this ownership epoch")

    def _service_blocked(self):
        service = self.radar.service
        return service is not None and (service.run_attempted or service.commands.pending())

    def begin_cycle(self):
        """Drain the watch even on cycles that ordinary polling later skips."""
        if self.watch is not None:
            self.watch.drain()

    def _window_end(self):
        return self.current.window_end

    def _unavailable(self, module, window_end):
        """Why a module cannot be requested at all in a pass ending at window_end."""
        if module == "radar_acc" and not self.radar.alignment_enabled:
            # No 0845 link, or its polling stopped after a failure this session:
            # leave the radar alone for the rest of the session.
            return "radar_unavailable"
        requested = self.last_request.get(module)
        if requested is not None and requested + contract.INTERVAL_SECONDS > window_end:
            return "module_hour"
        return None

    def _start_pass(self, now):
        if self.current is not None:
            if now > self._window_end():
                self._finish("window_closed")
            else:
                return True
        schedule = self.reservation
        first = contract.window_start(schedule.origin, schedule.first_due, 0)
        if now < max(first, self.minimum_start):
            return False
        index = int((now - first) // contract.INTERVAL_SECONDS)
        if index <= self.finished_window:
            return False
        window_end = contract.window_start(schedule.origin, schedule.first_due, index) + contract.WINDOW_SECONDS
        if all(self._unavailable(module, window_end) for module in contract.MODULES):
            return False  # nothing could be requested: no start, so no hour is consumed
        if not self.watch.drain():
            return False
        if self._service_blocked():
            self._disable("service_run_attempted")
            return False
        now = self.backend.monotonic()
        if not contract.pass_start_allowed(
            origin=schedule.origin, first_due=schedule.first_due, pass_index=index,
            now=now, previous_started=self.last_started,
        ):
            return False
        # Record and publish before fresh evidence/permits: even an unsent pass
        # consumes the hour, and output backpressure cannot expire a permit.
        self.current = _Pass(index, window_end)
        self.last_started = now
        return self._emit(contract.PASS_EVENT, **{"pass": index}, state="started",
                          reason="first_attempt", detail="drive DTC pass started",
                          started_monotonic=now)

    def _radar_has_margin(self, module):
        # Reached only while 0845 polling is enabled, that is, when a poll can occur.
        return module != "radar_acc" or (
            self.radar.next_alignment - self.backend.monotonic() >= contract.RADAR_MARGIN_SECONDS)

    def _mark_attempted(self, module):
        """Memory only: runs between permit consumption and the socket send."""
        self.current.attempted.add(module)
        self.last_request[module] = self.backend.monotonic()

    def _read_payload(self, module, result, started_at, evidence):
        samples = getattr(evidence, "rpm_samples", ())
        rpm = samples[-1] if samples else None
        if not finite_number(rpm) or rpm < 0:
            rpm = None
        return dict(**{"pass": self.current.index}, module=module, request=contract.REQUEST,
                    started_at=started_at, completed_at=self.wall_now(), rpm=rpm,
                    speed_mph=vehicle_speed.speed_mph(evidence), **result.event_fields())

    def _record(self, module, result, started_at, evidence):
        """Emit a module's first and final outcome in a pass, never every retry.

        A retryable not-sent outcome leaves the module open for at most
        NOT_SENT_ATTEMPTS tries, NOT_SENT_RETRY_SECONDS apart; repeats are held
        and only the last is emitted, when it turns out to be final.
        """
        current, now = self.current, self.backend.monotonic()
        payload = self._read_payload(module, result, started_at, evidence)
        if (result.outcome == contract.NOT_SENT and result.reason in contract.RETRYABLE_NOT_SENT
                and module not in current.attempted):
            tries = current.tries[module] = current.tries.get(module, 0) + 1
            current.last_try[module] = now
            if tries < contract.NOT_SENT_ATTEMPTS:
                if tries > 1:
                    current.held[module] = payload
                    return True
                return self._emit(contract.READ_EVENT, **payload)
        current.held.pop(module, None)
        current.settled.add(module)
        return self._emit(contract.READ_EVENT, **payload)

    def _after_record(self, result):
        if result.reason == "service_latch":
            self._finish("service_latch")
            self._disable("service_run_attempted")
        elif result.outcome == contract.NOT_SENT and result.reason in ("window_closed", "foreign_tester"):
            self._finish(result.reason)
        elif self.current.settled >= set(contract.MODULES):
            self._finish("all_modules_done")

    def _ready(self, module):
        """Timing conditions that a later cycle can satisfy; nothing is sent until then."""
        current, now = self.current, self.backend.monotonic()
        if current.last_attempt is not None and now - current.last_attempt < contract.MODULE_SPACING_SECONDS:
            return False
        tried = current.last_try.get(module)
        if tried is not None and now - tried < contract.NOT_SENT_RETRY_SECONDS:
            return False
        requested = self.last_request.get(module)
        if requested is not None and now - requested < contract.INTERVAL_SECONDS:
            return False
        return self._radar_has_margin(module)

    def _admission_problem(self, module):
        """Every check a send needs, as a not-sent reason, or None to send.

        Runs after fresh authority and again inside the transport after its
        preparation (BCM socket, reply drain), just before the permit is consumed.
        Consumes neither commands nor permits and emits nothing.
        """
        if self._service_blocked():
            return "service_latch"
        if self.backend.monotonic() > self._window_end():
            return "window_closed"
        if not self.watch.drain():
            return "foreign_tester"
        if not self._ready(module):
            return "deferred"
        return None

    def _next_module(self):
        """The first module ready now; settles unrequestable ones with their reason."""
        for module in contract.MODULES:
            if module in self.current.settled:
                continue
            reason = self._unavailable(module, self._window_end())
            if reason is not None:
                result = drive_dtc_transport.ReadResult(contract.NOT_SENT, reason)
                if not self._record(module, result, self.wall_now(), None):
                    return None
                self._after_record(result)
                if self.current is None:
                    return None
            elif self._ready(module):
                return module
        return None

    def _read(self, module):
        started_at = self.wall_now()
        permits = self.radar.authority.acquire(
            (contract.PURPOSES[module],), absent_detail="drive DTC read lost fresh running evidence")
        evidence = self.radar.authority.last_snapshot
        problem = "evidence_stale" if permits is None else self._admission_problem(module)
        if problem is not None:
            result = drive_dtc_transport.ReadResult(contract.NOT_SENT, problem)
        else:
            # No events or other blocking output may intervene between permit
            # issue and send; the transport repeats the admission checks last.
            result = self.transport.read(
                module, permits[0], admit=lambda: self._admission_problem(module),
                before_send=lambda: self._mark_attempted(module))
        # Module spacing counts every try, sent or not, but not a module
        # settled without one (an unusable radar, the module's own hour).
        self.current.last_attempt = self.backend.monotonic()
        if self._record(module, result, started_at, evidence):
            self._after_record(result)

    def run(self, snapshot):
        """Session-ending outcome, or None for ordinary/disabled/skipped work."""
        if not self.enabled:
            return None
        if self._service_blocked():
            self._finish("service_latch")
            self._disable("service_run_attempted")
            return None
        if not self._start_pass(self.backend.monotonic()):
            return None
        module = self._next_module()
        if module is None:
            return None
        try:
            self._read(module)
        except SessionEnding as ending:
            return ending.outcome
        return None

    def close(self):
        """Finish bookkeeping and release the receive-only watch before restoration."""
        try:
            if self.enabled:
                self._finish("session_ending")
        finally:
            if self.watch is not None:
                self.watch.close()
