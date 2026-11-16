"""Admission is repeated after transport preparation, through the production transport.

Preparation (the radar reply drain; the BCM socket's creation and drain) takes
time. A check that passed before it must not carry a send past a window end, a
newly pending service command or a newly dirty watch.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from projects.vehicle_data import (
    active_session_gates, drive_dtc_transport, radar_alignment, radar_drive_steps, transmit_permit,
)
from tests.drive_dtc_support import CHANNEL, EMPTY, ScriptedModule, can_frame
from tests.test_drive_dtc_steps import Harness

PREPARATION_SECONDS = 0.10


def production_transport(h, monkeypatch, module, on_prepare):
    """Real FreshAuthority permits and DriveDtcTransport; only socket I/O is scripted."""
    monkeypatch.setattr(transmit_permit.diagnostic_safety, 'validate_channel_lock', lambda *a: None)
    monkeypatch.setattr(active_session_gates, 'active_gate', lambda *a: None)
    sock = ScriptedModule(module, h.clock, [(0.05, can_frame(module, EMPTY))])
    backend = SimpleNamespace(
        monotonic=h.clock, channel=CHANNEL,
        broadcast_snapshot=lambda seconds: replace(h.authority.last_snapshot,
                                                   completed_monotonic=h.clock()))
    h.radar.authority = radar_drive_steps.FreshAuthority(
        backend, object(), object(), snapshot_seconds=0.)
    link = None
    if module == 'radar_acc':
        link = radar_alignment.RadarAlignmentPoller(CHANNEL, monotonic=h.clock)
        link.sock = sock
        drain = link.drain
        link.drain = lambda: (on_prepare(), drain())[1]

    def factory(*args):
        on_prepare()
        return sock

    h.step.transport = drive_dtc_transport.DriveDtcTransport(
        CHANNEL, radar_link=link, socket_factory=factory, monotonic=h.clock)
    return sock


def prepare_radar_first(h):
    """The pass starts at 1300 and the radar is read by the scripted transport."""
    h.cycle(1300.)
    assert h.transport.reads == ['radar_acc']


def changes(h, change):
    def on_prepare():
        h.clock.value += PREPARATION_SECONDS
        if change == 'pending':
            h.radar.service.commands.pending = lambda: True
        elif change == 'dirty':
            h.watch.clean = False
    return on_prepare


EXPECTED = {'window': 'window_closed', 'pending': 'service_latch', 'dirty': 'foreign_tester'}


@pytest.mark.parametrize('change', EXPECTED)
@pytest.mark.parametrize('module', ['radar_acc', 'bcm_ccan'])
def test_condition_changing_during_preparation_sends_nothing(monkeypatch, module, change):
    h = Harness()
    h.radar.service = SimpleNamespace(run_attempted=False,
                                      commands=SimpleNamespace(pending=lambda: False))
    if module == 'bcm_ccan':
        prepare_radar_first(h)
    sock = production_transport(h, monkeypatch, module, changes(h, change))
    # 1899.95 passes every check; preparation ends at 1900.05, past the window end.
    now = 1899.95 if change == 'window' else (1302. if module == 'bcm_ccan' else 1300.)
    h.cycle(now)
    assert sock.sent == []
    read = h.events('dtc_read')[-1]
    assert read['module'] == module
    assert (read['outcome'], read['reason']) == ('not_sent', EXPECTED[change])
    assert read['frame_hex'] is None and read['pending_count'] == 0
    if change == 'pending':
        assert not h.step.enabled
        assert h.events('dtc_step')[-1]['reason'] == 'service_run_attempted'
    else:
        assert h.events('dtc_pass')[-1]['reason'] == EXPECTED[change]


@pytest.mark.parametrize('module', ['radar_acc', 'bcm_ccan'])
def test_unchanged_conditions_still_send_once_after_preparation(monkeypatch, module):
    h = Harness()
    if module == 'bcm_ccan':
        prepare_radar_first(h)
    sock = production_transport(h, monkeypatch, module,
                                lambda: setattr(h.clock, 'value', h.clock() + PREPARATION_SECONDS))
    h.cycle(1302. if module == 'bcm_ccan' else 1300.)
    assert len(sock.sent) == 1
    assert h.events('dtc_read')[-1]['outcome'] == 'complete'
