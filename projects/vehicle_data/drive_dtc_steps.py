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
    attempted: set[str] = field(default_factory=set)
    last_attempt: float | None = None


class DriveDtcSteps:
    """One module per cycle, bounded windows and a monotonic hour between passes."""

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
        if self.current is not None:
            index = self.current.index
            self.finished_window = index
            self.current = None
            self._emit(contract.PASS_EVENT, **{"pass": index}, state="finished",
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
        return contract.window_start(self.reservation.origin, self.reservation.first_due,
                                     self.current.index) + contract.WINDOW_SECONDS

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
        if index <= self.finished_window or not self.watch.drain():
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
        self.current = _Pass(index)
        self.last_started = now
        return self._emit(contract.PASS_EVENT, **{"pass": index}, state="started",
                          reason="first_attempt", detail="drive DTC pass started",
                          started_monotonic=now)

    def _radar_has_margin(self, module):
        return module != "radar_acc" or (
            self.radar.next_alignment - self.backend.monotonic() >= contract.RADAR_MARGIN_SECONDS)

    def _mark_attempted(self, module):
        self.current.attempted.add(module)
        self.current.last_attempt = self.backend.monotonic()

    def _read_event(self, module, result, started_at, evidence):
        samples = getattr(evidence, "rpm_samples", ())
        rpm = samples[-1] if samples else None
        if not finite_number(rpm) or rpm < 0:
            rpm = None
        return self._emit(
            contract.READ_EVENT, **{"pass": self.current.index}, module=module,
            request=contract.REQUEST, started_at=started_at, completed_at=self.wall_now(),
            rpm=rpm, speed_mph=vehicle_speed.speed_mph(evidence), **result.event_fields(),
        )

    def _read(self, module):
        started_at = self.wall_now()
        permits = self.radar.authority.acquire(
            (contract.PURPOSES[module],), absent_detail="drive DTC read lost fresh running evidence")
        evidence = self.radar.authority.last_snapshot
        if permits is None:
            result = drive_dtc_transport.ReadResult(contract.NOT_SENT, "evidence_stale")
        else:
            # No events or other blocking output may intervene between permit
            # issue and send. These checks consume neither commands nor permits.
            blocked = self._service_blocked()
            clean = self.watch.drain()
            if blocked:
                result = drive_dtc_transport.ReadResult(contract.NOT_SENT, "service_latch")
            elif self.backend.monotonic() > self._window_end():
                result = drive_dtc_transport.ReadResult(contract.NOT_SENT, "window_closed")
            elif not clean:
                result = drive_dtc_transport.ReadResult(contract.NOT_SENT, "foreign_tester")
            elif not self._radar_has_margin(module):
                return
            else:
                result = self.transport.read(
                    module, permits[0], before_send=lambda: self._mark_attempted(module))
        if not self._read_event(module, result, started_at, evidence):
            return
        if result.reason == "service_latch":
            self._finish("service_latch")
            self._disable("service_run_attempted")
        elif result.reason in ("window_closed", "foreign_tester"):
            self._finish(result.reason)
        elif len(self.current.attempted) == len(contract.MODULES):
            self._finish("all_modules_done")

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
        module = next(key for key in contract.MODULES if key not in self.current.attempted)
        if not self._radar_has_margin(module):
            return None
        if (self.current.last_attempt is not None and
                self.backend.monotonic() - self.current.last_attempt < contract.MODULE_SPACING_SECONDS):
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
