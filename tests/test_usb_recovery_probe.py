"""A blocked receive-only observer must not hold the broker's restoration leases."""

import io
import json
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from functools import partial
from types import SimpleNamespace

import pytest

from lib import canbus
from projects.vehicle_data import ccan_powertrain, usb_recovery_probe as probe
from projects.vehicle_data.usb_recovery_child import REAP_SECONDS
from projects.vehicle_data.usb_recovery_contract import (ENGINE_EVIDENCE_MAX_AGE_SECONDS, PROBE_MODULE,
    PROBE_REPLY_BYTES, ProbeKind, ProbeReply, Refusal)
from projects.vehicle_data.usb_recovery_probe_client import REPO, ProbeFailed, run_probe
from tests.test_ccan_powertrain import FakeSocket, frame
from tests.usb_recovery_support import assert_committed, assert_failed, lab, ready, target_indexes


def substitute(code, children, expected_kind):
    def popen(argv, **kwargs):
        assert argv == [sys.executable, '-m', PROBE_MODULE, expected_kind, 'can0']
        assert kwargs['cwd'] == REPO and not kwargs.get('start_new_session')
        child = subprocess.Popen([sys.executable, '-c', code], **kwargs)
        children.append(child)
        return child
    return popen


@pytest.mark.parametrize('kind', tuple(ProbeKind))
def test_hung_observer_is_killed_and_rollback_releases_every_lease(ready, kind):
    before, children = ready.owned_files(), []
    ci, bi = target_indexes(ready)
    healthy = ready.probe

    def run(k, channel, *, deadline=None):
        if k != kind or (k == ProbeKind.RPM and len(ready.up_indexes) < 2):
            return healthy(k, channel)
        return run_probe(k, channel, timeout=0.3, popen=substitute('import time; time.sleep(60)', children, k))

    ready.effects.probe = run
    ready.run()
    assert_failed(ready, Refusal.WRONG_BUS if kind == ProbeKind.IDENTITY else Refusal.ENGINE_NOT_RUNNING, before)
    assert len(children) == 1 and children[0].returncode == -signal.SIGKILL and children[0].stdout.closed
    down = [('down', ci)] if kind == ProbeKind.IDENTITY else [('down', bi), ('down', ci)]
    assert ready.commands()[-len(down):] == down and not ready.up_indexes
    assert ready.events('release') == [('release', *args) for _, *args in reversed(ready.events('lock'))]
    assert len(ready.events('release')) == 6


def test_unreaped_passive_child_is_recorded_and_does_not_block_rollback(ready):
    history = []

    class Stuck:
        pid = 4321
        stdout = io.BytesIO()

        def wait(self, *, timeout):
            history.append(timeout)
            raise subprocess.TimeoutExpired('stuck probe', timeout)

        def kill(self):
            history.append('kill')

    before = ready.owned_files()
    ready.effects.probe = partial(run_probe, popen=lambda *_, **__: Stuck(), monotonic=ready.effects.monotonic)
    ready.run()
    assert_failed(ready, Refusal.WRONG_BUS, before)
    assert history[1:] == ['kill', REAP_SECONDS] and Stuck.stdout.closed
    assert 'probe unreaped pid=4321' in ready.persisted().detail
    assert any('probe unreaped pid=4321' in line and line.startswith('ERROR') for line in ready.logs)
    assert len(ready.events('release')) == 6 and not ready.up_indexes


VALID = asdict(ProbeReply(True, 101, 102))


@pytest.mark.parametrize('packet,status', [
    (b'{', 0), (b'[]\n', 0), (b'\xff\n', 0), (b'x' * PROBE_REPLY_BYTES + b'\n', 0),
    (json.dumps(VALID).encode(), 0), ((json.dumps(VALID) + '\n\n').encode(), 0),
    (json.dumps(VALID).encode() + b'\n', 1),
    *[((json.dumps({**VALID, **change}) + '\n').encode(), 0) for change in (
        dict(started_monotonic=99), dict(finished_monotonic=105), dict(finished_monotonic=100),
        dict(result=1), dict(started_monotonic=True), dict(result='true'), dict(result=None),
        dict(finished_monotonic=float('nan')), dict(extra=False),
    )],
    (b'{"result":false,"result":true,"started_monotonic":101,"finished_monotonic":102}\n', 0),
])
def test_no_evidence_from_bad_child_response(packet, status):
    code = f'import os,sys; os.write(1,{packet!r}); sys.exit({status})'
    with pytest.raises(ProbeFailed):
        run_probe(ProbeKind.RPM, 'can0', popen=substitute(code, [], ProbeKind.RPM),
                  monotonic=iter([100, 100, 103]).__next__)


def test_commit_dates_evidence_from_validated_child_start(ready):
    def run(k, channel, *, deadline=None):
        if len(ready.up_indexes) != 2:
            return ready.probe(k, channel)
        spawn = ready.now
        reply = ProbeReply(True, spawn + 1, spawn + 1.4)
        ready.now = spawn + 1.6
        return run_probe(k, channel, popen=substitute(f'print({json.dumps(asdict(reply))!r})', [], k),
                         monotonic=iter([spawn, spawn, ready.now]).__next__)

    ready.effects.probe = run
    ready.run()
    incident = assert_committed(ready)
    child_start = ready.now - 0.6
    assert incident.transaction.engine_seen_monotonic == pytest.approx(child_start)
    assert ready.events('end')[0][1] == pytest.approx(child_start + ENGINE_EVIDENCE_MAX_AGE_SECONDS)


@pytest.mark.parametrize('kind', tuple(ProbeKind))
def test_probe_cli_only_receives_and_closes_socket(kind, monkeypatch, capsys):
    frames = [frame(0x100, b'')] if kind == ProbeKind.IDENTITY else [frame(0x0fc, b'\x0b\xb8')] * 3
    sock = FakeSocket(frames)  # No send method: an attempted transmission would fail the test.
    factory = lambda *_: sock
    monkeypatch.setattr(canbus, 'socket', SimpleNamespace(
        socket=factory, AF_CAN=29, SOCK_RAW=3, CAN_RAW=1, timeout=TimeoutError))
    monkeypatch.setattr(canbus, 'rx_errors', lambda *_, **__: 0)
    monkeypatch.setattr(probe, 'observe', partial(probe.observe,
        snapshot=partial(ccan_powertrain.read_broadcast_snapshot, socket_factory=factory)))
    before = time.monotonic()
    assert probe.main([kind, 'can0']) == 0
    line, = capsys.readouterr().out.splitlines()
    result = ProbeReply(**json.loads(line))
    assert result.result is True and before <= result.started_monotonic <= result.finished_monotonic <= time.monotonic()
    assert sock.closed and sock.channel == ('can0',) and not sock.frames
    if kind == ProbeKind.RPM:
        assert sock.filters is not None


def test_failed_socket_setup_emits_one_negative_line_and_a_failing_exit(monkeypatch, capsys):
    def unavailable(*args):
        raise OSError('simulated device loss')

    monkeypatch.setattr(probe, 'observe', partial(probe.observe,
        snapshot=partial(ccan_powertrain.read_broadcast_snapshot, socket_factory=unavailable)))
    assert probe.main([ProbeKind.RPM, 'can0']) != 0
    line, = capsys.readouterr().out.splitlines()
    assert ProbeReply(**json.loads(line)).result is False
