"""Drive DTC scheduling through public cycle operations; all I/O is scripted."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
from types import SimpleNamespace

import pytest

from projects.vehicle_data import (
    active_session_gates, ccan_powertrain, drive_dtc_contract as contract,
    drive_dtc_steps as steps, drive_dtc_transport, radar_alignment,
    radar_drive_steps, radar_service_alignment, transmit_permit,
)
from tests.drive_dtc_support import (
    CHANNEL, SESSION, EMPTY, PENDING, ScriptedModule, DtcSink, can_frame,
)
from tests.radar_service_support import Clock, FakeAuthority, FakeRadar
from tests.test_active_drive import snapshot, rpm_observation, speed_observation
from tests.test_radar_service_alignment import Harness as ServiceHarness


class Watch:
    def __init__(self, channel=None, **kwargs):
        self.clean = True
        self.drains = 0
        self.closed = False
        self.error = None
        self.on_drain = lambda: None

    def open(self):
        if self.error:
            raise self.error
        return self

    def drain(self):
        self.drains += 1
        self.on_drain()
        return self.clean

    def close(self):
        self.closed = True


class Transport:
    def __init__(self):
        self.reads = []
        self.results = []
        self.after_send = lambda: None

    def read(self, module, permit, *, before_send):
        result = (self.results.pop(0) if self.results else
                  drive_dtc_transport.ReadResult('complete', 'single_frame', True,
                                                frame_hex=EMPTY.hex(' ').upper(), count=0))
        if result.attempted:
            before_send()
        self.after_send()
        self.reads.append(module)
        return result


class Harness:
    def __init__(self, *, first_due=300, session_origin=1000., constructed_at=1000.):
        self.clock = Clock(constructed_at)
        self.sink, self.watch, self.transport = DtcSink(), Watch(), Transport()
        self.authority = FakeAuthority()
        self.authority.last_snapshot = snapshot((1600, 1601, 1602),
                                               rpm_observation(1602), speed_observation(61.2))
        self.radar = SimpleNamespace(service=None, poller=None, authority=self.authority,
                                     next_alignment=math.inf)
        self.step = steps.DriveDtcSteps(
            SimpleNamespace(channel=CHANNEL, monotonic=self.clock), self.sink, self.radar,
            reservation=steps.Reservation(SESSION, session_origin, first_due),
            watch_factory=lambda *a, **kw: self.watch,
            transport_factory=lambda *a, **kw: self.transport,
            wall_now=lambda: (datetime(2026, 11, 15, tzinfo=timezone.utc)
                              + timedelta(seconds=self.clock())).isoformat(),
        )

    def cycle(self, now):
        self.clock.value = now
        self.step.begin_cycle()
        return self.step.run(self.authority.last_snapshot)

    def events(self, kind):
        return [event for event in self.sink.events if event['type'] == kind]


def test_start_window_one_module_per_cycle_spacing_and_evidence():
    h = Harness()
    h.cycle(1299.999)
    assert not h.transport.reads
    h.cycle(1300.)
    assert h.transport.reads == ['radar_acc']
    h.cycle(1301.999)
    assert h.transport.reads == ['radar_acc']
    h.cycle(1302.)
    assert h.transport.reads == ['radar_acc', 'bcm_ccan']
    assert [e['state'] for e in h.events('dtc_pass')] == ['started', 'finished']
    assert h.events('dtc_pass')[0]['started_monotonic'] == 1300.
    assert h.events('dtc_pass')[-1]['reason'] == 'all_modules_done'
    for event in h.events('dtc_read'):
        assert event['rpm'] == 1602 and event['speed_mph'] == 61.2
        assert event['session'] == SESSION and event['pass'] == 0
    h.cycle(1400.)
    assert len(h.transport.reads) == 2
    assert h.watch.drains >= 6


def test_missed_windows_are_not_carried_and_helpers_wait_own_five_minutes():
    h = Harness(constructed_at=1002.)
    h.cycle(1300.)
    assert not h.transport.reads
    h.cycle(1900.001)
    assert not h.transport.reads
    h.cycle(4900.)
    assert h.events('dtc_pass')[0]['pass'] == 1


def test_actual_pass_start_hour_separation_even_at_adjacent_window_edges():
    h = Harness()
    h.cycle(1900.)
    h.cycle(1902.)
    assert len(h.transport.reads) == 1  # the window ended before the BCM slot
    h.cycle(4900.)
    h.cycle(5499.999)
    assert len(h.transport.reads) == 1
    h.cycle(5500.)
    assert len(h.transport.reads) == 2
    starts = h.events('dtc_pass')[::2]
    assert [e['started_monotonic'] for e in starts] == [1900., 5500.]
    assert [e['pass'] for e in starts] == [0, 1]


def test_expiry_and_stale_evidence_do_not_change_start_or_consume_module():
    h = Harness()
    h.transport.results.append(drive_dtc_transport.ReadResult('not_sent', 'permit_expired'))
    h.cycle(1300.)
    h.authority.script.append(FakeAuthority.SKIP)
    h.cycle(1301.)
    h.cycle(1302.)
    h.cycle(1304.)
    assert h.transport.reads == ['radar_acc', 'radar_acc', 'bcm_ccan']
    assert [e['reason'] for e in h.events('dtc_read')][:2] == ['permit_expired', 'evidence_stale']
    starts = [e for e in h.events('dtc_pass') if e['state'] == 'started']
    assert len(starts) == 1 and starts[0]['started_monotonic'] == 1300.


def test_an_unsent_pass_still_consumes_the_hour():
    h = Harness()
    h.authority.script.extend([FakeAuthority.SKIP] * 3)
    h.cycle(1900.)
    h.cycle(1901.)
    h.cycle(4900.)
    assert not h.transport.reads
    assert [e['started_monotonic'] for e in h.events('dtc_pass') if e['state'] == 'started'] == [1900.]


def test_radar_margin_is_rechecked_after_authority_cost():
    h = Harness()
    h.radar.next_alignment = 1304.999
    h.cycle(1300.)
    assert not h.authority.requested
    h.radar.next_alignment = 1306.
    acquire = h.authority.acquire

    def slow_authority(*args, **kwargs):
        permits = acquire(*args, **kwargs)
        h.clock.value += 1.001
        return permits

    h.authority.acquire = slow_authority
    h.cycle(1300.)
    assert not h.transport.reads
    h.authority.acquire = acquire
    h.radar.next_alignment = 1310.
    h.cycle(1302.)
    assert h.transport.reads == ['radar_acc']


def test_dirty_watch_blocks_start_and_is_rechecked_before_each_module():
    h = Harness()
    h.watch.clean = False
    h.cycle(1300.)
    assert not h.events('dtc_pass')
    h.watch.clean = True
    h.cycle(1301.)
    acquire = h.authority.acquire

    def dirty_during_authority(*args, **kwargs):
        h.watch.clean = False
        return acquire(*args, **kwargs)

    h.authority.acquire = dirty_during_authority
    h.cycle(1303.)
    assert h.transport.reads == ['radar_acc']
    assert h.events('dtc_read')[-1]['reason'] == 'foreign_tester'
    assert h.events('dtc_pass')[-1]['reason'] == 'foreign_tester'


@pytest.mark.parametrize('before_authority', [False, True])
def test_pending_service_is_nonconsuming_and_latches_for_epoch(before_authority):
    h = Harness()
    commands = SimpleNamespace(pending=lambda: before_authority)
    h.radar.service = SimpleNamespace(run_attempted=False, commands=commands)
    acquire = h.authority.acquire

    def queue_service(*args, **kwargs):
        commands.pending = lambda: True
        return acquire(*args, **kwargs)

    h.authority.acquire = queue_service
    h.cycle(1300.)
    commands.pending = lambda: False
    h.cycle(4900.)
    assert not h.transport.reads
    assert h.events('dtc_step')[-1]['reason'] == 'service_run_attempted'
    if not before_authority:
        assert h.events('dtc_read')[-1]['reason'] == 'service_latch'


@pytest.mark.parametrize('state', ['refused', 'timedout', 'completed'])
def test_real_service_attempt_latch_survives_terminal_states(state):
    fake = FakeRadar(progress=(100,))
    with ServiceHarness(fake) as service_h:
        service_h.commands.pending = lambda: bool(service_h.commands.queued)
        service_h.commands.send('start')
        if state == 'timedout':
            fake.silent = True
        service_h.cycle(30. if state == 'refused' else 0.)
        if state == 'completed':
            for _ in range(4):
                service_h.cycle()
        assert service_h.steps.service.run_attempted
        assert service_h.steps.service.state == (
            radar_service_alignment.COMPLETED if state == 'completed' else radar_service_alignment.FAILED)
        h = Harness()
        h.radar.service = service_h.steps.service
        h.cycle(1300.)
        h.cycle(4900.)
        assert not h.transport.reads
        assert h.events('dtc_step')[-1]['state'] == 'disabled'


def test_ambiguous_send_is_never_retried_and_final_cleanup_closes_watch():
    h = Harness()
    h.transport.results.append(drive_dtc_transport.ReadResult('no_response', 'send_error', True))
    h.cycle(1300.)
    h.cycle(1301.)
    h.step.close()
    assert h.transport.reads == ['radar_acc']
    assert h.events('dtc_pass')[-1]['reason'] == 'session_ending'
    assert h.watch.closed


def test_gate_failure_ends_session_without_read():
    h = Harness()
    outcome = active_session_gates.SessionOutcome('engine_not_running', 'fresh evidence lost')
    h.authority.script.append(outcome)
    assert h.cycle(1300.) is outcome
    assert not h.transport.reads
    h.step.close()
    assert h.watch.closed


def test_watch_open_failure_disables_and_contract_error_never_emits_invalid_event():
    h = Harness()
    h.step.close()
    h.watch.error = OSError('watch unavailable')
    bad_watch = steps.DriveDtcSteps(
        h.step.backend, h.sink, h.radar, reservation=h.step.reservation,
        watch_factory=lambda *a, **kw: h.watch,
    )
    assert h.events('dtc_step')[-1]['reason'] == 'sentinel_unavailable'
    bad_watch.run(None)
    assert not h.transport.reads
    h = Harness()
    h.transport.results.append(drive_dtc_transport.ReadResult('bad_result', 'bad_reason', True))
    h.cycle(1300.)
    assert not h.events('dtc_read')
    assert h.events('dtc_step')[-1]['reason'] == 'contract_error'
    h.cycle(4900.)
    assert h.transport.reads == ['radar_acc']
    h.step.close()
    assert h.watch.closed


def test_window_rechecked_after_blocking_started_output_without_issuing_stale_permit():
    h = Harness()
    emit = h.sink.emit

    def delayed_started(kind, **payload):
        emit(kind, **payload)
        if kind == 'dtc_pass' and payload['state'] == 'started':
            h.clock.value += 2.5

    h.sink.emit = delayed_started
    h.cycle(1900.)
    assert h.events('dtc_pass')[0]['started_monotonic'] == 1900.
    assert not h.transport.reads
    assert h.events('dtc_read')[-1]['reason'] == 'window_closed'
    assert h.events('dtc_pass')[-1]['reason'] == 'window_closed'


def test_nominal_deadline_plus_scripted_gate_cost_keeps_command_and_silence_below_limits(monkeypatch):
    """A measured scenario, not a hard latency bound on sysfs/host scheduling."""
    h = Harness()
    h.clock.value = 1300.

    class TimedCommands:
        arrival = math.inf
        consumed_at = None

        def pending(self):
            return self.consumed_at is None and h.clock() >= self.arrival

        def poll(self):
            if not self.pending():
                return []
            self.consumed_at = h.clock()
            return [dict(command='start', request_id='b' * 32)]

    commands = TimedCommands()
    sock = ScriptedModule('radar_acc', h.clock, [(0.1, can_frame('radar_acc', PENDING))])
    sock.on_send = lambda: setattr(commands, 'arrival', h.clock() + .8)
    link = radar_alignment.RadarAlignmentPoller(CHANNEL, monotonic=h.clock)
    link.sock = sock
    h.step.transport = drive_dtc_transport.DriveDtcTransport(CHANNEL, radar_link=link, monotonic=h.clock)
    backend = SimpleNamespace(
        monotonic=h.clock, channel=CHANNEL,
        broadcast_snapshot=lambda seconds: (setattr(h.clock, 'value', h.clock() + seconds)
            or replace(h.authority.last_snapshot, completed_monotonic=h.clock())),
    )
    authority = radar_drive_steps.FreshAuthority(backend, object(), object(), snapshot_seconds=.35)
    h.radar = radar_drive_steps.RadarDriveSteps(
        backend, h.sink, link, authority, next_due=math.inf, service_commands=commands)
    h.step.radar = h.radar

    def gate_cost(*args):
        h.clock.value += .08

    monkeypatch.setattr(active_session_gates, 'active_gate', gate_cost)
    monkeypatch.setattr(transmit_permit.diagnostic_safety, 'validate_channel_lock', lambda *a: None)
    previous_observation = h.clock()
    evidence = h.authority.last_snapshot
    h.radar.run(evidence)  # normal command intake occurs before the DTC read
    h.step.begin_cycle()
    h.step.run(evidence)
    assert h.clock() == pytest.approx(1302.93)
    assert h.events('dtc_read')[-1]['outcome'] == 'no_response'
    assert h.events('dtc_read')[-1]['pending_count'] == 1
    assert len(sock.sent) == 1 and commands.consumed_at is None
    h.clock.value += .35  # the next normal broadcast snapshot
    h.radar.run(evidence)  # real service code consumes the queued start (refused: moving)
    h.step.begin_cycle()
    h.step.run(evidence)
    assert commands.consumed_at - commands.arrival < 15.
    assert commands.consumed_at - previous_observation < 10.
    assert h.radar.service.run_attempted
    assert not h.step.enabled and h.events('dtc_step')[-1]['reason'] == 'service_run_attempted'


def test_window_edge_read_may_finish_late_without_moving_pass_start():
    h = Harness()
    h.transport.after_send = lambda: setattr(h.clock, 'value', h.clock() + 2.5)
    h.cycle(1899.)
    assert h.transport.reads == ['radar_acc']
    assert h.events('dtc_pass')[0]['started_monotonic'] == 1899.
    assert h.events('dtc_read')[0]['completed_at'] > h.events('dtc_read')[0]['started_at']
    h.cycle(h.clock())
    assert h.transport.reads == ['radar_acc']
    assert h.events('dtc_pass')[-1]['reason'] == 'window_closed'


@pytest.mark.parametrize('value,expected', [(0., 0.), (61.2, 61.2), (200., 200.),
                                           (200.01, None), (-1, None), (True, None),
                                           (float('nan'), None), (float('inf'), None)])
def test_promoted_speed_reader_preserves_service_boundaries(value, expected):
    evidence = snapshot((750, 751, 752), speed_observation(value))
    assert ccan_powertrain.speed_mph(evidence) == expected
    assert radar_service_alignment._speed_mph(evidence) == expected
