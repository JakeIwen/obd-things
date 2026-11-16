"""Opt-in helper integration using scripted CAN packets, pipes and clocks only."""
from contextlib import nullcontext
import io
import json
import os
import socket
from types import SimpleNamespace

import pytest

from lib import diagnostic_safety
from projects.vehicle_data import (
    active_drive, drive_dtc_contract as contract, drive_dtc_steps,
    drive_dtc_transport, drive_dtc_watch, helper_startup_grant, radar_alignment,
)
from tests.drive_dtc_support import CHANNEL, SESSION, EMPTY, FIRST, ScriptedModule, WatchFeed, can_frame
from tests.radar_service_support import Clock, FakeRadar
from tests.test_active_drive import FakeBackend, snapshot, rpm_observation, speed_observation


@pytest.fixture
def rig(monkeypatch):
    # The scripted radar exercises open() on hosts without Linux SocketCAN.
    monkeypatch.setattr(socket, 'AF_CAN', active_drive.AF_CAN, raising=False)
    monkeypatch.setattr(socket, 'CAN_RAW', active_drive.CAN_RAW, raising=False)
    clock, events, trace = Clock(0.), [], []
    radar_sock = FakeRadar()
    radar_sock.setsockopt = lambda *args: trace.append('radar_filter')
    radar_sock.bind = lambda address: trace.append('radar_bind')
    answer = radar_sock._answer

    def reply(data):
        if data == contract.REQUEST_DATA:
            radar_sock._reply(bytes.fromhex('59 02 FF'))
        else:
            answer(data)

    radar_sock._answer = reply
    feed = WatchFeed()
    close_watch = feed.close
    feed.close = lambda: trace.append('watch_close') or close_watch()
    bcm_sock = ScriptedModule('bcm_ccan', clock, [(0., can_frame('bcm_ccan', FIRST))])
    backend = FakeBackend(snapshots=[], events=trace)
    backend.monotonic = clock
    backend.sleep = lambda seconds: setattr(clock, 'value', clock() + seconds)
    backend.broadcast_snapshot = lambda timeout: snapshot(
        (750, 751, 752) if clock() < 305 else (0, 0, 0),
        rpm_observation(750 if clock() < 305 else 0), speed_observation(61.2),
        completed_monotonic=clock(),
    )
    backend.open_radar = lambda: radar_alignment.RadarAlignmentPoller(
        CHANNEL, monotonic=clock, socket_factory=lambda *args: radar_sock).open()
    opened = []
    watch_type = drive_dtc_watch.ForeignTesterWatch
    transport_type = drive_dtc_transport.DriveDtcTransport

    def watch(*args, **kwargs):
        opened.append('watch')
        return watch_type(*args, **kwargs, socket_factory=lambda *a: feed)

    def transport(*args, **kwargs):
        return transport_type(*args, **kwargs, socket_factory=lambda *a: opened.append('bcm') or bcm_sock)

    monkeypatch.setattr(drive_dtc_watch, 'ForeignTesterWatch', watch)
    monkeypatch.setattr(drive_dtc_transport, 'DriveDtcTransport', transport)
    monkeypatch.setattr(diagnostic_safety, 'acquire_channel_lock', lambda channel: object())
    monkeypatch.setattr(diagnostic_safety, 'validate_channel_lock', lambda *args: None)
    monkeypatch.setattr(diagnostic_safety, 'release_channel_lock', lambda handle: trace.append('unlock'))
    monkeypatch.setattr(active_drive.can_operation_state, 'begin_inhibit',
                        lambda *args, **kw: pytest.fail('unexpected restoration inhibit'))
    sink = SimpleNamespace(emit=lambda kind, **payload: events.append(dict(type=kind, **payload)))
    return SimpleNamespace(
        clock=clock, backend=backend, events=events, trace=trace, sink=sink,
        radar_sock=radar_sock, feed=feed, bcm_sock=bcm_sock, opened=opened,
    )


@pytest.mark.parametrize('enabled', [False, True])
def test_opt_in_is_the_only_path_to_new_sockets_frames_and_events(rig, enabled):
    reservation = drive_dtc_steps.Reservation(SESSION, 0., 300.) if enabled else None
    outcome = active_drive.run_active_session(
        rig.backend, rig.sink, drive_dtc=reservation,
        termination_guard=SimpleNamespace(begin_cleanup=lambda: rig.trace.append('cleanup_guard')),
    )
    assert outcome.restored and outcome.reason == 'engine_not_running'
    dtc_events = [event for event in rig.events if event['type'] in contract.EVENT_TYPES]
    for event in dtc_events:
        contract.validate_event(event)
    new_frames = [data for data in rig.radar_sock.sent if data == contract.REQUEST_DATA]
    bcm_data = [frame[8:] for frame in rig.bcm_sock.sent]
    if enabled:
        assert rig.opened == ['watch', 'bcm']
        assert new_frames == [contract.REQUEST_DATA]
        assert bcm_data == [contract.REQUEST_DATA]
        assert rig.feed.closed and rig.bcm_sock.closed
        assert rig.trace.index('cleanup_guard') < rig.trace.index('watch_close') < rig.trace.index('restore')
        assert [event['outcome'] for event in dtc_events if event['type'] == 'dtc_read'] == ['complete', 'truncated']
        assert [event['started_monotonic'] for event in dtc_events
                if event['type'] == 'dtc_pass' and event['state'] == 'started'] == [300.]
    else:
        assert not rig.opened and not new_frames and not bcm_data and not dtc_events
    assert rig.radar_sock.closed and rig.backend.restore_count == 1
    assert rig.trace[-1] == 'unlock'
    assert rig.events[-1]['type'] == 'final' and rig.events[-1]['restored'] is True


@pytest.mark.parametrize('reason', ['stale', 'expired'])
def test_watch_drains_even_when_normal_pcm_polling_continues_early(rig, monkeypatch, reason):
    rig.backend.broadcast_snapshot = lambda timeout: snapshot(
        (750, 751, 752) if rig.clock() < 3. else (0, 0, 0), rpm_observation(),
        completed_monotonic=rig.clock(),
    )
    if reason == 'stale':
        monkeypatch.setattr(active_drive.transmit_permit, 'issue',
                            lambda *a, **kw: (_ for _ in ()).throw(
                                active_drive.transmit_permit.StaleTransmitEvidenceError('test age')))
    else:
        rig.backend.pcm.poll = lambda permit: SimpleNamespace(reason='transmit_permit_expired')
    outcome = active_drive.run_active_session(
        rig.backend, rig.sink, drive_dtc=drive_dtc_steps.Reservation(SESSION, 0., 300.))
    assert outcome.restored and rig.feed.reads == 4  # three skipped cycles + stopping cycle
    assert rig.feed.closed and not rig.bcm_sock.sent


@pytest.mark.parametrize('enabled', [False, True])
def test_main_ready_advertises_only_requested_capability(rig, monkeypatch, enabled):
    stream = io.StringIO()
    monkeypatch.setattr(active_drive, 'JsonEventSink', lambda: SinkToStream(stream))
    monkeypatch.setattr(active_drive, '_set_parent_death_signal', lambda parent: None)
    monkeypatch.setattr(active_drive, 'SystemBackend', lambda *a, **kw: rig.backend)
    guard = SimpleNamespace(begin_cleanup=lambda: rig.trace.append('cleanup_guard'))
    monkeypatch.setattr(diagnostic_safety, 'interrupt_on_termination', lambda: nullcontext(guard))
    read_fd, write_fd = os.pipe()
    os.write(write_fd, helper_startup_grant.GRANT_TOKEN)
    os.close(write_fd)
    args = ['--channel', CHANNEL, '--expected-usb-serial', 'test', '--expected-dev-id', '0',
            '--expected-parent-pid', str(os.getppid()), '--startup-grant-fd', str(read_fd)]
    if enabled:
        args += ['--enable-drive-dtc', '--drive-dtc-reservation', SESSION,
                 '--drive-dtc-origin', '0', '--drive-dtc-first-due', '300']
    assert active_drive.main(args) == 0
    output = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert output[-1]['reason'] == 'engine_not_running' and output[-1]['restored'] is True
    first = output[0]
    assert first['type'] == 'ready'
    assert first.get('capabilities') == ([contract.CAPABILITY] if enabled else None)
    assert helper_startup_grant.ready_event_problem(first, os.getpid()) is None


class SinkToStream:
    def __init__(self, stream):
        self.stream = stream

    def emit(self, kind, **payload):
        self.stream.write(json.dumps(dict(type=kind, **payload)) + '\n')


@pytest.mark.parametrize('flags', [
    ['--enable-drive-dtc'], ['--drive-dtc-origin', '0'],
    ['--enable-drive-dtc', '--drive-dtc-reservation', SESSION, '--drive-dtc-origin', 'nan',
     '--drive-dtc-first-due', '300'],
    ['--enable-drive-dtc', '--drive-dtc-reservation', SESSION, '--drive-dtc-origin', '0',
     '--drive-dtc-first-due', '299'],
])
def test_invalid_schedule_refused_before_parent_or_backend_effects(monkeypatch, flags):
    monkeypatch.setattr(active_drive, '_set_parent_death_signal', lambda *a: pytest.fail('parent effect'))
    monkeypatch.setattr(active_drive, 'SystemBackend', lambda *a, **kw: pytest.fail('backend constructed'))
    args = ['--channel', CHANNEL, '--expected-usb-serial', 'test', '--expected-dev-id', '0',
            '--expected-parent-pid', '42'] + flags
    with pytest.raises(SystemExit):
        active_drive.main(args)


def test_enabled_launch_without_startup_grant_is_refused_before_any_effect(monkeypatch):
    """Valid DTC arguments without --startup-grant-fd could not confirm the capability."""
    monkeypatch.setattr(active_drive, '_set_parent_death_signal', lambda *a: pytest.fail('parent effect'))
    monkeypatch.setattr(active_drive, 'SystemBackend', lambda *a, **kw: pytest.fail('backend constructed'))
    monkeypatch.setattr(active_drive, 'run_active_session', lambda *a, **kw: pytest.fail('session started'))
    monkeypatch.setattr(drive_dtc_steps, 'DriveDtcSteps', lambda *a, **kw: pytest.fail('step constructed'))
    args = ['--channel', CHANNEL, '--expected-usb-serial', 'test', '--expected-dev-id', '0',
            '--expected-parent-pid', '42', '--enable-drive-dtc', '--drive-dtc-reservation', SESSION,
            '--drive-dtc-origin', '0', '--drive-dtc-first-due', '300']
    with pytest.raises(SystemExit, match='requires the supervisor.s --startup-grant-fd'):
        active_drive.main(args)


def test_legacy_ungated_launch_stays_accepted_with_dtc_off(monkeypatch):
    started = []
    monkeypatch.setattr(active_drive, '_set_parent_death_signal', lambda *a: None)
    monkeypatch.setattr(active_drive, 'SystemBackend', lambda *a, **kw: object())
    monkeypatch.setattr(diagnostic_safety, 'interrupt_on_termination', lambda: nullcontext(object()))

    def session(*args, **kwargs):
        started.append(kwargs)
        return SimpleNamespace(restored=True)

    monkeypatch.setattr(active_drive, 'run_active_session', session)
    assert active_drive.main(['--channel', CHANNEL, '--expected-usb-serial', 'test',
                              '--expected-dev-id', '0', '--expected-parent-pid', '42']) == 0
    assert started[0]['drive_dtc'] is None and started[0]['startup_gate'] is None


def test_unexpected_step_failure_still_closes_watch_under_guard_and_restores(rig, monkeypatch):
    def fail_read(*args, **kwargs):
        raise RuntimeError('injected step fault')

    # The composition factory is patched by rig; patch its actual constructed
    # transport instead through the step's public construction seam.
    transport_type = drive_dtc_transport.DriveDtcTransport

    def faulting_transport(*args, **kwargs):
        transport = transport_type(*args, **kwargs)
        transport.read = fail_read
        return transport

    monkeypatch.setattr(drive_dtc_transport, 'DriveDtcTransport', faulting_transport)
    outcome = active_drive.run_active_session(
        rig.backend, rig.sink, drive_dtc=drive_dtc_steps.Reservation(SESSION, 0., 300.),
        termination_guard=SimpleNamespace(begin_cleanup=lambda: rig.trace.append('cleanup_guard')),
    )
    assert outcome.restored and outcome.reason == 'helper_failed'
    assert 'injected step fault' in outcome.detail
    assert rig.feed.closed and rig.radar_sock.closed
    assert rig.trace.index('cleanup_guard') < rig.trace.index('watch_close') < rig.trace.index('restore')
    assert rig.events[-1]['type'] == 'final' and rig.events[-1]['restored'] is True
