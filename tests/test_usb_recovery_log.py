"""The recovery's journal output must never hold the restoration transaction (plan M5)."""

import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib import can_operation_state as operation
from projects.vehicle_data.usb_recovery_contract import IncidentState as State, MutationOutcome, Refusal
from projects.vehicle_data.usb_recovery_log import ERROR_PREFIX, QUEUE_LINES, BestEffortLog
from tests.usb_recovery_support import ACTIVE, assert_failed, lab, target_indexes

REPO = Path(__file__).resolve().parents[1]
PROMPT = 0.5  # Seconds a call may take when nothing is allowed to block; a stalled sink would take forever.


class StuckSink:
    """A sink whose first write never returns until released, like a full journal pipe."""

    def __init__(self):
        self.release, self.entered, self.lines = threading.Event(), threading.Event(), []

    def __call__(self, line):
        self.entered.set()
        self.release.wait(30)
        self.lines.append(line)


@pytest.fixture
def stuck():
    sink = StuckSink()
    yield sink
    sink.release.set()


def test_full_queue_drops_counts_and_reports_the_count_in_the_next_delivered_line(stuck):
    journal = BestEffortLog(stuck, capacity=2)
    journal.start()
    assert journal.emit('first') and stuck.entered.wait(5)  # The writer holds 'first', blocked.
    assert journal.emit('queued 1') and journal.emit('queued 2')
    started = time.monotonic()
    assert [journal.emit(f'lost {n}') for n in range(5)] == [False] * 5
    assert time.monotonic() - started < PROMPT
    stuck.release.set()
    deadline = time.monotonic() + 5
    while len(stuck.lines) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stuck.lines == ['first', 'queued 1 [5 earlier usb-drop-recovery log lines dropped]', 'queued 2']


def test_writer_survives_a_sink_that_raises():
    seen = []

    def sink(line):
        seen.append(line)
        if line == 'bad':
            raise BrokenPipeError()

    journal = BestEffortLog(sink)
    journal.start()
    journal.emit('bad'), journal.emit('good')
    deadline = time.monotonic() + 5
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ['bad', 'good']


def test_error_lines_carry_the_journal_priority():
    seen = []
    journal = BestEffortLog(seen.append)
    journal.start()
    journal.emit('plain'), journal.emit('boom', error=True)
    deadline = time.monotonic() + 5
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ['plain', ERROR_PREFIX + 'boom']


def test_rollback_of_two_instances_completes_while_the_sink_never_drains(lab, stuck):
    lab.new_runtime(log=stuck)  # The sink is the journal writer's target; log() never calls it.
    lab.incident()
    lab.replace_board()
    ci, bi = target_indexes(lab)
    before = lab.owned_files()
    lab.effects.end = lambda *args, **kwargs: operation.EndResult('mismatch')
    down = lab.effects.down
    lab.effects.down = lambda inst, *, wait_quiet: (
        MutationOutcome.ROLLBACK_UNVERIFIED if inst.ifindex == bi else down(inst, wait_quiet=wait_quiet))
    # Flood far past the queue's capacity, with the writer stuck on its first line.
    slow = []
    for _ in range(QUEUE_LINES * 2):
        started = time.monotonic()
        lab.recovery.log('flood', error=True)
        slow.append(time.monotonic() - started)
    assert max(slow) < PROMPT
    done = threading.Thread(target=lab.run, daemon=True)
    done.start()
    done.join(10)
    assert not done.is_alive(), 'rollback waited on the journal sink'
    txn = assert_failed(lab, Refusal.COMMIT_REFUSED, before)
    assert [m.outcome for m in txn.mutations] == [MutationOutcome.DOWN_VERIFIED, MutationOutcome.ROLLBACK_UNVERIFIED]
    resumed = [event for event in lab.trace if event[0] == 'resume']
    assert len(resumed) == 2
    assert len(lab.events('release')) == len(lab.events('lock'))
    assert not lab.recovery.log('after', error=True)  # Queue still full: dropped, not blocked.


CHILD = '''
import sys
from pathlib import Path
import pytest
from lib import can_operation_state as operation
from projects.vehicle_data.usb_recovery_contract import MutationOutcome
from tests.usb_recovery_support import RecoveryLab

lab = RecoveryLab(Path(sys.argv[1]), pytest.MonkeyPatch())
lab.new_runtime(stdout_journal=True)  # Writes the enabled line to the real stdout pipe.
lab.incident()
lab.replace_board()
lab.effects.end = lambda *a, **k: operation.EndResult("mismatch")
lab.effects.down = lambda inst, *, wait_quiet: MutationOutcome.ROLLBACK_UNVERIFIED
lab.run()
sys.stdin.buffer.read(1)
'''


def _child_env():
    env = os.environ.copy()
    env.pop('PYTHONUNBUFFERED', None)
    return env


def test_healthy_pipe_shows_a_real_failed_transaction_before_the_process_exits(tmp_path):
    with subprocess.Popen([sys.executable, '-c', CHILD, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          cwd=REPO, env=_child_env()) as child:
        try:
            seen, deadline = b'', time.monotonic() + 30
            while b'state=failed' not in seen and time.monotonic() < deadline:
                if select.select([child.stdout], [], [], 0.5)[0]:
                    seen += os.read(child.stdout.fileno(), 65536)
            assert child.poll() is None, 'lines were only visible at exit'
            lines = seen.decode().splitlines()
            assert 'usb-drop-recovery enabled' in lines[0]
            errors = [line for line in lines if 'rollback_unverified' in line]
            assert len(errors) == 2 and all(line.startswith(ERROR_PREFIX) for line in errors)
            assert 'role=' in errors[0] and errors[0] != errors[1]
            terminal = [line for line in lines if 'state=failed' in line]
            assert terminal and 'reason=commit_refused' in terminal[-1]
            assert lines.index(terminal[-1]) > lines.index(errors[-1])
        finally:
            child.communicate(b'x', timeout=10)


SIMPLE_CHILD = '''
import sys
from projects.vehicle_data.usb_recovery import RecoveryEffects, UsbDropRecovery
recovery = UsbDropRecovery.if_enabled(True, None, effects=RecoveryEffects(helper_ok=lambda: True))
sys.stdin.buffer.read(1)
'''


def test_log_calls_return_promptly_when_the_real_pipe_is_full():
    code = SIMPLE_CHILD.replace("sys.stdin.buffer.read(1)", '''import time
t = time.monotonic()
for n in range(2000):
    recovery.log("x" * 200 + str(n), error=True)
print("ELAPSED", time.monotonic() - t, file=sys.stderr, flush=True)
sys.stdin.buffer.read(1)''')
    # Nobody reads the child's stdout: after about 64 KiB the pipe is full and the writer blocks.
    with subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=REPO, env=_child_env()) as child:
        try:
            assert select.select([child.stderr], [], [], 20)[0], 'log() blocked on a full pipe'
            elapsed = float(child.stderr.readline().split()[1])
            assert elapsed < 5
        finally:
            child.kill()


def test_a_raising_sink_does_not_change_the_recovery_outcome(lab):
    def broken(_line):
        raise BrokenPipeError()

    lab.new_runtime(log=broken)
    lab.incident()
    lab.replace_board()
    lab.run()
    assert lab.recovery.flush_log()
    assert lab.persisted().state == State.COMMITTED


def test_a_dropped_opening_line_does_not_invalidate_launch_evidence(lab, stuck):
    lab.new_runtime(log=stuck)
    lab.recovery.log('first')
    assert stuck.entered.wait(5)
    for _ in range(QUEUE_LINES):
        assert lab.recovery.log('fill')
    assert not lab.recovery.log('full')
    lab.incident(ACTIVE)
    lab.replace_board()
    lab.run()
    assert lab.persisted().state == State.COMMITTED


def test_a_failed_durable_incident_write_still_invalidates_launch_evidence(lab):
    def refuse(*args, **kwargs):
        raise OSError('disk full')

    lab.store.write_incident = refuse
    lab.incident(ACTIVE)
    assert lab.recovery.incident.state == State.INELIGIBLE
    assert lab.recovery.incident.reason == Refusal.INCIDENT_RECORD_UNWRITTEN


def test_drop_count_survives_an_intermediate_raising_write():
    seen = []

    def sink(line):
        if line.startswith('bad'):
            raise BrokenPipeError()
        seen.append(line)

    entered, gate = threading.Event(), threading.Event()

    def gated(line):
        entered.set()
        gate.wait(10)
        sink(line)

    journal = BestEffortLog(gated, capacity=2)
    journal.start()
    journal.emit('block')
    assert entered.wait(5)
    journal.emit('bad'), journal.emit('good')
    assert [journal.emit('lost') for _ in range(3)] == [False] * 3
    gate.set()
    deadline = time.monotonic() + 5
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen[0] == 'block' and seen[1] == 'good [3 earlier usb-drop-recovery log lines dropped]'


def test_start_failure_never_raises(monkeypatch):
    class Broken:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading, 'Thread', Broken)
    journal = BestEffortLog(lambda line: None)
    assert journal.start() is False
    assert journal.emit('queued') is True
