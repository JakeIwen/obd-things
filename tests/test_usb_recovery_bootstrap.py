"""The parked bootstrap has one evidence budget, not a new one for each child."""

import json
import os
import subprocess
from dataclasses import asdict
from functools import partial

import pytest

from projects.vehicle_data import usb_recovery_contract as contract
from projects.vehicle_data.usb_recovery_child import REAP_SECONDS
from projects.vehicle_data.usb_recovery_probe_client import ProbeFailed, run_probe
from tests.usb_recovery_support import C, assert_committed, assert_failed, lab, ready, target_indexes


class TimedChild:
    """Supply a real reply pipe, but advance the lab clock during wait rather than sleeping."""

    def __init__(self, lab, kind, duration, slow_reap):
        self.lab, self.duration = lab, duration
        self.slow_reap = slow_reap
        self.killed = False
        self.pid = 411
        start = lab.now
        read, write = os.pipe()
        self.stdout = os.fdopen(read, 'rb')
        reply = contract.ProbeReply(kind == contract.ProbeKind.IDENTITY, start, start + duration)
        with os.fdopen(write, 'w') as out:
            print(json.dumps(asdict(reply)), file=out)

    def wait(self, *, timeout):
        if self.killed:
            if self.slow_reap:
                self.lab.now += timeout
                raise subprocess.TimeoutExpired('unreaped observer', timeout)
            return -9
        self.lab.now += min(timeout, self.duration)
        if self.duration > timeout:
            raise subprocess.TimeoutExpired('fake kernel wait', timeout)
        return 0

    def kill(self):
        self.killed = True


@pytest.mark.parametrize('slow_reap', [False, True])
def test_four_individually_valid_waits_cannot_multiply_parked_exposure(ready, slow_reap):
    before, launched, submissions = ready.owned_files(), [], []
    ci, _ = target_indexes(ready)
    normal = ready.submit

    def submit(command, index):
        submissions.append((command, index, ready.now))
        reply = normal(command, index)
        ready.now += 0.4  # Submission, ACK and readback are not a fresh start for the first probe.
        return reply

    def popen(argv, **kwargs):
        kind = contract.ProbeKind(argv[-2])
        launched.append((kind, ready.now))
        return TimedChild(ready, kind, duration=1.3, slow_reap=slow_reap)

    ready.submit = submit
    ready.effects.probe = partial(run_probe, popen=popen, monotonic=ready.effects.monotonic)
    ready.run()
    assert_failed(ready, contract.Refusal.ENGINE_NOT_RUNNING, before)
    assert [k for k, _ in launched] == [contract.ProbeKind.IDENTITY] + [contract.ProbeKind.RPM] * 3
    assert [(cmd, idx) for cmd, idx, _ in submissions] == [('up-listen-only', ci), ('down', ci)]
    # No rollback check is bypassed. With prompt checks, only the bounded reap can delay submission.
    expected = contract.BOOTSTRAP_BUDGET_SECONDS + (REAP_SECONDS if slow_reap else 0.0)
    assert submissions[1][2] - submissions[0][2] == pytest.approx(expected)
    assert 'probe timed out' in ready.persisted().detail


def test_no_child_launched_when_budget_expires_between_identity_and_rpm(ready):
    before, launched = ready.owned_files(), []
    normal = ready.probe

    def probe(kind, channel, *, deadline):
        launched.append(kind)
        reply = normal(kind, channel)
        ready.now = deadline  # Identity returned in time; the parent was then descheduled.
        return reply

    ready.effects.probe = probe
    ready.run()
    assert_failed(ready, contract.Refusal.ENGINE_NOT_RUNNING, before)
    assert launched == [contract.ProbeKind.IDENTITY]
    assert not ready.up_indexes


def test_no_spawn_when_runner_itself_arrives_at_deadline():
    with pytest.raises(ProbeFailed, match='before launch'):
        run_probe(contract.ProbeKind.IDENTITY, 'fake', deadline=5, monotonic=lambda: 5,
                  popen=lambda *a, **kw: pytest.fail('budget exhausted before spawn'))


@pytest.mark.parametrize('when,reason', [('before_identity', contract.Refusal.WRONG_BUS),
                                        ('after_rpm', contract.Refusal.ENGINE_NOT_RUNNING)])
def test_readback_cannot_hide_exhausted_budget(ready, when, reason):
    before = ready.owned_files()
    normal = ready.effects.read_link

    def read(name, *, timeout=None):
        if timeout is not None and (when == 'before_identity' or ready.events('rpm')):
            assert name == ready.mapping[C]
            ready.now += timeout
        return normal(name)

    ready.effects.read_link = read
    ready.run()
    assert_failed(ready, reason, before)
    assert len(ready.events('rpm')) == (0 if when == 'before_identity' else 1)
    assert not ready.up_indexes and not ready.events('end')


def test_commit_probe_does_not_inherit_parked_bootstrap_budget(ready):
    probes = []

    def probe(kind, channel, *, deadline):
        probes.append((kind, deadline))
        if deadline is None:
            ready.now += contract.BOOTSTRAP_BUDGET_SECONDS + 1  # Commit evidence is dated from this fresh read.
        return ready.probe(kind, channel)

    ready.effects.probe = probe
    ready.run()
    assert_committed(ready)
    assert probes[0][1] is not None and probes[0][1] == probes[1][1]
    assert probes[2] == (contract.ProbeKind.RPM, None)
