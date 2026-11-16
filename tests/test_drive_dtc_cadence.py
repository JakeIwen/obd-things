"""Randomized sessions through the production budget, broker control and helper step.

Real: DriveDtcBudget (its writer thread and files in tmp_path), DriveDtcControl
(reservation, launch arguments, ready check, event cadence check, session end),
the helper's argument parsing and DriveDtcSteps (windows, the hour between pass
starts, the per-module hour, retries, admission). Scripted: the clock, the boot
id, the watch, the radar's fresh authority and the transport's socket I/O; the
watch drain, authority and preparation each take time, and a permit expires a
lifetime after its evidence, as consume() enforces.

Asserted for every request actually sent: a pass start is at least an hour after
the previous one in the same boot; a module is never requested again within an
hour, in the same boot or across a reboot (real time); no request is sent after
its window end; a reservation made from
uncertain history (reboot, corrupted record, lost session end) has no pass in
its first hour; and the broker never has to reject a well-behaved helper.
"""
from datetime import datetime, timezone
import json
import random
import threading
from types import SimpleNamespace
import uuid

import pytest

from projects.vehicle_data import (
    active_drive, drive_dtc_budget as budget_mod, drive_dtc_launch, drive_dtc_contract as contract,
    drive_dtc_control as control_mod, drive_dtc_steps as steps_mod, transmit_permit,
)
from projects.vehicle_data.drive_dtc_transport import ReadResult

WALL = datetime(2026, 11, 15, tzinfo=timezone.utc)
HELPER_ARGS = ['--channel', 'can7', '--expected-usb-serial', 'x', '--expected-dev-id', '0',
               '--expected-parent-pid', '1', '--startup-grant-fd', '9']


class World:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.mono, self.real, self.boot_n = 50., 0., 1
        self.starts, self.sends, self.uncertain, self.window_ends = [], [], [], []
        self.rejections = 0

    @property
    def boot(self):
        return str(uuid.UUID(int=self.boot_n))

    def clock(self):
        return self.mono

    def advance(self, seconds):
        self.mono += seconds
        self.real += seconds

    def reboot(self):
        self.real += self.rng.uniform(5, 4000)
        self.boot_n += 1
        self.mono = self.rng.uniform(10, 120)


def write_plain(path, payload):
    """The record's content without fsync; durability has its own tests."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')


class Results:
    failed = SimpleNamespace(is_set=lambda: False)

    def enqueue(self, event):
        return True

    def discard_session(self, session):
        pass

    def close(self, timeout=0.):
        pass


class Helper:
    """The production step behind the helper's own argument parser."""

    def __init__(self, world, control, argv, session_end):
        rng = world.rng
        args = active_drive.build_parser().parse_args(HELPER_ARGS + argv)
        self.reservation = drive_dtc_launch.reservation_from_arguments(args)
        self.world, self.control, self.session_end = world, control, session_end
        helper = self

        class Sink:
            def emit(self, event_type, **payload):
                event = json.loads(json.dumps({'type': event_type, **payload}))
                if event_type == contract.PASS_EVENT and event['state'] == 'started':
                    world.starts.append((world.boot, event['started_monotonic'],
                                         world.real - (world.mono - event['started_monotonic'])))
                control.handle_event(event)

        class Watch:
            def __init__(self, *args, **kwargs):
                pass

            def open(self):
                return self

            def close(self):
                pass

            def drain(self):
                world.advance(rng.uniform(0., 0.12))  # a drain takes time, even inside admit()
                return rng.random() > 0.02

        class Authority:
            last_snapshot = SimpleNamespace(rpm_samples=(800.,), observations=())

            def acquire(self, purposes, *, absent_detail):
                world.advance(rng.uniform(0.05, 0.35))
                self.evidence_at = world.mono  # a permit expires a lifetime after its evidence
                return None if rng.random() < 0.08 else (object(),)

        class Transport:
            def __init__(self, *args, **kwargs):
                pass

            def read(self, module, permit, *, admit, before_send):
                world.advance(rng.uniform(0., 0.15))  # preparation
                refused = admit()
                if refused is not None:
                    return ReadResult(contract.NOT_SENT, refused)
                expired = world.mono - helper.radar.authority.evidence_at >= transmit_permit.PERMIT_TTL_SECONDS
                if expired or rng.random() < 0.05:  # consume() refuses an expired permit
                    return ReadResult(contract.NOT_SENT, 'permit_expired')
                before_send()
                reservation, index = helper.reservation, helper.steps.current.index
                world.window_ends.append((world.mono, contract.window_start(
                    reservation.origin, reservation.first_due, index) + contract.WINDOW_SECONDS))
                world.sends.append((world.boot, world.mono, world.real, module,
                                    reservation.session, index))
                world.advance(rng.uniform(0.01, 2.5))
                return ReadResult(contract.NO_RESPONSE, 'timeout', True)

        self.radar = SimpleNamespace(poller=None, service=None, authority=Authority(),
                                     next_alignment=0., alignment_enabled=rng.random() > 0.1)
        self.steps = steps_mod.DriveDtcSteps(
            SimpleNamespace(monotonic=world.clock, channel='can7'), Sink(), self.radar,
            reservation=self.reservation, watch_factory=Watch, transport_factory=Transport,
            wall_now=lambda: '2026-11-15T20:00:00Z')

    def run(self, stop_event):
        world, rng = self.world, self.world.rng
        while world.mono < self.session_end and not stop_event.is_set():
            self.radar.next_alignment = world.mono + rng.choice((3., 10.))
            self.steps.begin_cycle()
            self.steps.run(None)
            step = rng.choice((1., 1.3, 2.9, 7.)) if rng.random() < 0.97 else rng.uniform(5, 120)
            world.advance(min(step, max(0., self.session_end - world.mono) + 0.001))
        if stop_event.is_set():
            world.rejections += 1
        self.steps.close()
        return {'type': 'final', 'restored': True}


class Supervisor:
    def __init__(self, world, control, length):
        self.world, self.control, self.length = world, control, length

    def run(self, stop_event):
        argv = self.control.launch_arguments()
        self.world.advance(self.world.rng.uniform(0., 3.))
        ready = {'type': 'ready', 'protocol': 1, 'pid': 1}
        if argv:
            ready['capabilities'] = [contract.CAPABILITY]
        assert self.control.accept_ready(ready)
        if not argv:
            self.world.advance(self.length)
            return {'type': 'final', 'restored': True}
        return Helper(self.world, self.control, argv, self.world.mono + self.length).run(stop_event)


def broker(world, directory):
    store = budget_mod.DriveDtcBudget(directory, monotonic=world.clock, boot_id=lambda: world.boot,
                                      wall_now=lambda: WALL, write=write_plain)
    return store, control_mod.DriveDtcControl(enabled=True, budget=store, results=Results(),
                                              monotonic=world.clock)


def simulate(seed, sessions, directory):
    world = World(seed)
    store, control = broker(world, directory)
    uncertain = True  # no record yet
    for _ in range(sessions):
        roll = world.rng.random()
        if roll < 0.15:
            store.close(5)
            if roll < 0.06:
                world.reboot()
            elif roll < 0.09:
                (directory / budget_mod.BUDGET_FILENAME).write_text('{"schema": 1, damaged')
            if roll < 0.09:
                uncertain = True
            store, control = broker(world, directory)
        lose_end = world.rng.random() < 0.1
        if lose_end:
            store.end = lambda reservation_id, ended: False
        length = world.rng.uniform(0, 400) if world.rng.random() < 0.5 else world.rng.uniform(400, 9000)
        launched = world.mono
        control.run(Supervisor(world, control, length), threading.Event())
        if uncertain:
            world.uncertain.append((world.boot, launched))
        uncertain = lose_end
        if lose_end:
            del store.end
        world.advance(world.rng.choice((world.rng.uniform(1, 60), world.rng.uniform(60, 3000),
                                        world.rng.uniform(3000, 9000))))
    store.close(5)
    return world


@pytest.mark.parametrize('seed', [3, 11, 29])
def test_production_step_and_broker_keep_both_hours(tmp_path, seed):
    world = simulate(seed, 60, tmp_path)
    assert len(world.starts) >= 10 and len(world.sends) >= 10  # not vacuous
    assert world.rejections == 0
    by_boot = {}
    for boot, started, _real in world.starts:
        by_boot.setdefault(boot, []).append(started)
    for starts in by_boot.values():
        starts.sort()
        assert all(b - a >= contract.INTERVAL_SECONDS for a, b in zip(starts, starts[1:]))
    for module in contract.MODULES:
        sends = sorted((real, boot, mono) for boot, mono, real, sent, *_ in world.sends if sent == module)
        for (real_a, boot_a, mono_a), (real_b, boot_b, mono_b) in zip(sends, sends[1:]):
            assert real_b - real_a >= contract.INTERVAL_SECONDS
            if boot_a == boot_b:
                assert mono_b - mono_a >= contract.INTERVAL_SECONDS
    for boot, launched in world.uncertain:
        assert not [s for b, s, _ in world.starts if b == boot and launched <= s < launched + 3600]
    assert all(sent < end for sent, end in world.window_ends)  # never on the wire after its window
    passes = [(session, index, module) for *_, module, session, index in world.sends]
    assert len(passes) == len(set(passes))
