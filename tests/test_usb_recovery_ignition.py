"""The host ignition hint can prevent a bootstrap but cannot authorize a commit."""

import os
from types import SimpleNamespace

import pytest

from projects.vehicle_data import usb_recovery_contract as contract
from projects.vehicle_data.usb_recovery import ignition_flag_present
from tests.usb_recovery_support import (
    C, assert_committed, assert_deferred, assert_failed, assert_paused_and_resumed, lab, ready, target_indexes,
)


@pytest.fixture
def marker(ready, tmp_path, monkeypatch):
    parent = tmp_path / 'hooks'
    parent.mkdir()
    path = parent / 'ignition_is_on'
    monkeypatch.setattr(contract, 'IGNITION_FLAG_PATH', str(path))
    ready.effects.ignition_flag = ignition_flag_present
    return path


def test_missing_marker_never_starts_then_regular_marker_allows_recovery(ready, marker):
    ready.run()
    assert_deferred(ready, contract.Refusal.IGNITION_NOT_ON)
    assert not ready.events('pause') and not ready.events('lock')
    marker.touch()
    ready.run()
    assert_committed(ready)


def test_missing_marker_for_the_window_does_not_spend_the_boot(ready, marker):
    ready.run()
    ready.now += contract.RECOVERY_WINDOW_SECONDS + 1
    ready.run()
    assert_deferred(ready, contract.Refusal.WINDOW_EXPIRED, terminal=True)
    assert not ready.events('pause') and not ready.events('lock')


@pytest.mark.parametrize('denied', [False, True], ids=['directory', 'denied_parent_traversal'])
def test_unusable_marker_refuses_without_a_pause(ready, marker, monkeypatch, denied):
    if denied:
        marker.touch()
        real_stat = os.stat
        def stat(path, *args, **kwargs):
            if path == str(marker):
                raise PermissionError('search permission denied on parent')
            return real_stat(path, *args, **kwargs)
        monkeypatch.setattr(os, 'stat', stat)
    else:
        marker.mkdir()
    ready.run()
    assert_deferred(ready, contract.Refusal.IGNITION_NOT_ON)
    assert not ready.events('pause') and not ready.events('lock')


def test_marker_lost_between_precheck_and_locked_check(ready, marker):
    marker.touch()
    def check():
        if ready.events('lock'):
            marker.unlink(missing_ok=True)
        return ignition_flag_present()
    ready.effects.ignition_flag = check
    ready.run()
    assert_deferred(ready, contract.Refusal.IGNITION_NOT_ON)
    assert_paused_and_resumed(ready)
    assert len(ready.events('release')) == len(ready.events('lock')) == 6


@pytest.mark.parametrize('silent', [False, True], ids=['recognizable_bus_without_rpm', 'silent_bus'])
def test_stopped_producer_can_leave_positive_but_cannot_supply_rpm(ready, marker, silent):
    marker.touch()
    os.utime(marker, (1, 1))  # Last ON marker, with no producer updating or clearing it.
    before = ready.owned_files()
    ready.identify = lambda *args, **kwargs: 'silent' if silent else C
    ready.snapshot = lambda *args, **kwargs: SimpleNamespace(frame_count=1, rpm_samples=())
    ready.run()
    reason = contract.Refusal.WRONG_BUS if silent else contract.Refusal.ENGINE_NOT_RUNNING
    assert_failed(ready, reason, before)
    ci, _ = target_indexes(ready)
    assert ready.commands() == [('up-listen-only', ci), ('down', ci)]
    assert not ready.events('end')
