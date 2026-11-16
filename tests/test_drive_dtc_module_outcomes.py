"""Per-module outcomes in a pass: an unusable radar, bounded retries, the per-module hour.

All I/O is scripted. The radar cases use the production RadarDriveSteps,
FreshAuthority and RadarAlignmentPoller, with an 0845 read that times out.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from projects.vehicle_data import (
    active_session_gates, drive_dtc_transport, radar_alignment, radar_drive_steps, transmit_permit,
)
from tests.drive_dtc_support import CHANNEL
from tests.radar_service_support import FakeAuthority
from tests.test_drive_dtc_steps import Harness


class SilentRadar:
    """Accepts the 0845 request and never answers it."""

    def __init__(self, clock):
        self.clock, self.timeout, self.sent = clock, None, []

    def settimeout(self, value):
        self.timeout = value

    def send(self, frame):
        self.sent.append(frame)
        return 16

    def recv(self, _size):
        if self.timeout == 0:
            raise BlockingIOError()
        self.clock.value += self.timeout
        raise TimeoutError('silent radar')


def production_radar(h, monkeypatch, *, with_poller):
    monkeypatch.setattr(transmit_permit.diagnostic_safety, 'validate_channel_lock', lambda *a: None)
    monkeypatch.setattr(active_session_gates, 'active_gate', lambda *a: None)
    backend = SimpleNamespace(
        monotonic=h.clock, channel=CHANNEL,
        broadcast_snapshot=lambda seconds: replace(h.authority.last_snapshot,
                                                   completed_monotonic=h.clock()))
    authority = radar_drive_steps.FreshAuthority(backend, object(), object(), snapshot_seconds=0.)
    authority.last_snapshot = h.authority.last_snapshot
    poller = None
    if with_poller:
        poller = radar_alignment.RadarAlignmentPoller(CHANNEL, monotonic=h.clock)
        poller.sock = SilentRadar(h.clock)
    h.radar = radar_drive_steps.RadarDriveSteps(backend, h.sink, poller, authority, next_due=1100.)
    h.step.radar = h.radar
    return poller


def full_cycle(h, now):
    h.clock.value = now
    h.radar.run(h.authority.last_snapshot)
    h.step.begin_cycle()
    return h.step.run(h.authority.last_snapshot)


def reads(h):
    return [(e['module'], e['outcome'], e['reason']) for e in h.events('dtc_read')]


@pytest.mark.parametrize('with_poller', [True, False])
def test_unusable_radar_never_starves_the_bcm(monkeypatch, with_poller):
    h = Harness()
    poller = production_radar(h, monkeypatch, with_poller=with_poller)
    full_cycle(h, 1100.)  # a real 0845 read fails, which stops 0845 polling for the session
    assert not h.radar.alignment_enabled
    if with_poller:
        assert len(poller.sock.sent) == 1
        assert h.events('metric_failure')[0]['reason'] == 'response_timeout'
    for now in (1300., 1302., 4900., 4902.):
        full_cycle(h, now)
    unavailable = ('radar_acc', 'not_sent', 'radar_unavailable')
    complete = ('bcm_ccan', 'complete', 'single_frame')
    assert reads(h) == [unavailable, complete, unavailable, complete]
    assert h.transport.reads == ['bcm_ccan', 'bcm_ccan']
    assert [(e['state'], e['reason']) for e in h.events('dtc_pass')] == [
        ('started', 'first_attempt'), ('finished', 'all_modules_done')] * 2
    if with_poller:
        assert len(poller.sock.sent) == 1  # nothing more was sent to the radar


def test_no_pass_starts_when_no_module_could_be_requested():
    h = Harness()
    h.radar.alignment_enabled = False
    h.step.last_request['bcm_ccan'] = 1300.5  # its hour would end after window 0 closes
    h.cycle(1300.)
    assert not h.events('dtc_pass') and not h.authority.requested
    # The hour was not consumed: window 1 starts as soon as the BCM's own hour allows.
    h.cycle(4900.)
    h.cycle(4900.5)  # the BCM's own hour
    assert reads(h) == [('radar_acc', 'not_sent', 'radar_unavailable'),
                        ('bcm_ccan', 'complete', 'single_frame')]
    assert h.events('dtc_pass')[0]['pass'] == 1


def test_module_whose_hour_ends_after_the_window_gets_one_declared_outcome():
    h = Harness()
    h.step.last_request['radar_acc'] = 1300.5
    h.cycle(1300.)
    h.cycle(1302.)
    assert reads(h) == [('radar_acc', 'not_sent', 'module_hour'),
                        ('bcm_ccan', 'complete', 'single_frame')]


def test_retries_are_spaced_capped_and_record_only_first_and_final():
    h = Harness()
    h.authority.script.extend([FakeAuthority.SKIP] * 6)
    for now in (1300., 1302., 1305., 1310., 1312., 1315., 1320., 1322., 1330.):
        h.cycle(now)
    # Three tries per module, ten seconds apart; each module's middle try is never emitted.
    assert h.authority.requested == [('radar_acc.dtc_read',), ('bcm_ccan.dtc_read',)] * 3
    stale = [('radar_acc', 'not_sent', 'evidence_stale'), ('bcm_ccan', 'not_sent', 'evidence_stale')]
    assert reads(h) == stale * 2
    assert not h.transport.reads
    assert h.events('dtc_pass')[-1]['reason'] == 'all_modules_done'


def test_a_held_retry_is_recorded_when_the_window_closes():
    h = Harness()
    h.authority.script.extend([FakeAuthority.SKIP] * 4)
    for now in (1880., 1882., 1890., 1892.):
        h.cycle(now)
    assert len(h.events('dtc_read')) == 2  # each module's first outcome only
    h.cycle(1901.)
    assert reads(h) == [('radar_acc', 'not_sent', 'evidence_stale'),
                        ('bcm_ccan', 'not_sent', 'evidence_stale')] * 2
    assert h.events('dtc_pass')[-1]['reason'] == 'window_closed'


def test_a_module_is_never_requested_twice_within_an_hour_in_a_session():
    h = Harness()
    h.cycle(1300.)  # the radar is sent
    h.authority.script.extend([FakeAuthority.SKIP] * 2)
    for now in (1302., 1312., 1322.):  # the BCM's evidence is stale twice, then it is sent
        h.cycle(now)
    assert h.transport.reads == ['radar_acc', 'bcm_ccan']
    # Window 1 opens at 4900, exactly an hour after the first pass and the radar.
    for now in (4900., 4902., 4921.9):
        h.cycle(now)
    assert h.transport.reads == ['radar_acc', 'bcm_ccan', 'radar_acc']
    h.cycle(4922.)  # the BCM waits for its own hour, not the pass's
    assert h.transport.reads == ['radar_acc', 'bcm_ccan', 'radar_acc', 'bcm_ccan']
    assert [e['started_monotonic'] for e in h.events('dtc_pass') if e['state'] == 'started'] == [
        1300., 4900.]
