"""Two helper sessions through the production budget, broker control and step.

The broker's session-end bound is the last scheduled window end. A module's
request is never on the wire after its window ends (the send guard), so the
next session's request to that module comes at least an hour after it.
"""
import threading
from types import SimpleNamespace

import pytest

from projects.vehicle_data import (
    active_drive, drive_dtc_budget as budget_mod, drive_dtc_contract as contract,
    drive_dtc_control as control_mod, drive_dtc_steps as steps_mod, transmit_permit,
)
from projects.vehicle_data.drive_dtc_transport import ReadResult
from tests.test_drive_dtc_cadence import HELPER_ARGS, WALL, Results, write_plain

BOOT = '00000000-0000-0000-0000-000000000001'


class World:
    def __init__(self):
        self.clock, self.in_admit, self.sends = 1000., False, []

    def __call__(self):
        return self.clock


def helper_session(world, control, cycles, *, drain_seconds):
    """Run the production step at the given clock values; each admission drain takes time."""
    args = active_drive.build_parser().parse_args(HELPER_ARGS + control.launch_arguments())
    reservation = steps_mod.reservation_from_arguments(args)

    class Watch:
        def __init__(self, *a, **kw):
            pass

        def open(self):
            return self

        def close(self):
            pass

        def drain(self):
            if world.in_admit:
                world.clock += drain_seconds
            return True

    class Authority:
        last_snapshot = SimpleNamespace(rpm_samples=(800.,), observations=())
        issued = None

        def acquire(self, purposes, *, absent_detail):
            Authority.issued = world.clock
            return (object(),)

    class Transport:
        def __init__(self, *a, **kw):
            pass

        def read(self, module, permit, *, admit, before_send):
            world.in_admit = True
            try:
                refused = admit()
            finally:
                world.in_admit = False
            if refused is not None:
                return ReadResult(contract.NOT_SENT, refused)
            if world.clock - Authority.issued >= transmit_permit.PERMIT_TTL_SECONDS:
                return ReadResult(contract.NOT_SENT, 'permit_expired')  # as consume() would
            before_send()
            world.sends.append((module, world.clock))
            return ReadResult(contract.NO_RESPONSE, 'timeout', True)

    radar = SimpleNamespace(poller=None, service=None, authority=Authority(),
                            next_alignment=0., alignment_enabled=False)

    class Supervisor:
        def run(self, stop):
            steps = steps_mod.DriveDtcSteps(
                SimpleNamespace(monotonic=world, channel='can7'),
                SimpleNamespace(emit=lambda kind, **payload: control.handle_event({'type': kind, **payload})),
                radar, reservation=reservation, watch_factory=Watch, transport_factory=Transport,
                wall_now=lambda: '2026-11-15T20:00:00Z')
            for now in cycles(reservation):
                world.clock = max(world.clock, now)
                steps.begin_cycle()
                steps.run(None)
            steps.close()
            world.clock += 5.
            return {'type': 'final', 'restored': True}

    assert control.accept_ready({'type': 'ready', 'protocol': 1, 'pid': 1,
                                 'capabilities': [contract.CAPABILITY]})
    return Supervisor()


@pytest.mark.parametrize('first_session', ['last_admissible_instant', 'reviewed_late_drain'])
def test_cross_session_gap_for_a_module_is_never_under_an_hour(tmp_path, first_session):
    world = World()
    budget = budget_mod.DriveDtcBudget(tmp_path, monotonic=world, boot_id=lambda: BOOT,
                                       wall_now=lambda: WALL, write=write_plain)
    control = control_mod.DriveDtcControl(enabled=True, budget=budget, results=Results(), monotonic=world)
    try:
        def first(reservation):
            end = contract.window_start(reservation.origin, reservation.first_due, 0) + contract.WINDOW_SECONDS
            if first_session == 'last_admissible_instant':
                return [end - steps_mod.SEND_GUARD_SECONDS]
            return [end - 0.05]  # the re-review: checked 0.05 s before the end, a 0.10 s drain

        def second(reservation):
            start = contract.window_start(reservation.origin, reservation.first_due, 0)
            return [start, start + 2.]

        drain = 0.10 if first_session == 'reviewed_late_drain' else 0.
        control.run(_Launch(lambda: helper_session(world, control, first, drain_seconds=drain)),
                    threading.Event())
        world.clock += 100.
        control.run(_Launch(lambda: helper_session(world, control, second, drain_seconds=0.)),
                    threading.Event())
    finally:
        budget.close(5)
    bcm = [at for module, at in world.sends if module == 'bcm_ccan']
    gaps = [b - a for a, b in zip(bcm, bcm[1:])]
    assert all(gap >= contract.INTERVAL_SECONDS for gap in gaps), gaps
    # At the last admissible instant the first session sends; a late drain refuses it.
    assert len(bcm) == (2 if first_session == 'last_admissible_instant' else 1)


class _Launch:
    """The broker's supervisor: reservation arguments exist only inside control.run."""

    def __init__(self, build):
        self.build = build

    def run(self, stop):
        return self.build().run(stop)
