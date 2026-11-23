from hashlib import sha256
import io
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from lib import canbus
from lib.netdev_instance import NetdevInstance
from projects.vehicle_data import bound_link
from projects.vehicle_data.usb_recovery_contract import MutationOutcome as Outcome
from tests.test_canbus import PASSIVE_DETAILS, ip_details

INSTANCE = NetdevInstance("can0", 3, "/sys/devices/a", "1-1.4.2", 1, 96, "BOARD-A", 0)
UP = PASSIVE_DETAILS
DOWN = UP.replace("NOARP,UP,LOWER_UP,ECHO", "NOARP,ECHO").replace("state UP", "state DOWN").replace(
    "ERROR-ACTIVE", "STOPPED",
)
UNKNOWN = DOWN.replace("<NOARP,ECHO> ", "")
RENAMED = DOWN.replace("3: can0:", "7: can0:")
PREFIX = ["sudo", "-n", "/usr/bin/python3", "-I", "-B", "/usr/local/sbin/vanpi-can-link-by-index"]
DEADLINE = ["--not-after", "12.0"]


def read(text):
    return bound_link.read_bound_state("can0", run=lambda *_a, **_k: ip_details(text))


class FakeProcess:
    def __init__(self, code=0, out="", *, timeout=False):
        self.returncode = code
        self.stdout = io.StringIO()
        self.out = out
        self.timeout = timeout
        self.history = []

    def communicate(self, *, timeout):
        self.history.append(("communicate", timeout))
        if self.timeout:
            raise subprocess.TimeoutExpired([], timeout)
        return self.out, None

    def terminate(self):
        self.history.append("terminate")

    def kill(self):
        self.history.append("kill")

    def wait(self, *, timeout):
        self.history.append(('wait', timeout))
        if self.timeout:
            raise subprocess.TimeoutExpired([], timeout)
        return -9


def up(popen, *texts):
    readback = Mock(side_effect=[read(text) for text in texts])
    outcome = bound_link.bring_up_listen_only(INSTANCE, 500000, read_bound_state=readback,
                                              popen=popen, monotonic=lambda: 10.0)
    return outcome.outcome, outcome.refusal, readback.call_count


def test_up_uses_one_installed_index_only_command():
    popen = Mock(return_value=FakeProcess())
    assert up(popen, DOWN, UP) == (Outcome.UP_VERIFIED, None, 2)
    assert popen.call_count == 1
    assert popen.call_args.args == (PREFIX + ["up-listen-only", "3", "500000"] + DEADLINE,)


@pytest.mark.parametrize(("before", "outcome"), [
    (RENAMED, Outcome.INSTANCE_CHANGED), (UP, Outcome.VERIFICATION_FAILED),
    (UNKNOWN, Outcome.VERIFICATION_FAILED),
])
def test_up_does_not_submit_without_same_index_and_positive_down_evidence(before, outcome):
    popen = Mock()
    assert up(popen, before) == (Outcome.NOT_SUBMITTED, outcome, 1)
    popen.assert_not_called()


@pytest.mark.parametrize(("after", "outcome"), [
    (RENAMED, Outcome.INSTANCE_CHANGED),
    (UP.replace("ERROR-ACTIVE", "ERROR-WARNING"), Outcome.VERIFICATION_FAILED),
    (UP.replace("mtu 16", "mtu 72"), Outcome.VERIFICATION_FAILED),
    (UP.replace("restart-ms 0", "restart-ms 1"), Outcome.VERIFICATION_FAILED),
])
def test_up_does_not_claim_success_without_exact_readback(after, outcome):
    popen = Mock(return_value=FakeProcess())
    assert up(popen, DOWN, after) == (outcome, outcome, 2)
    assert popen.call_count == 1


def test_enodev_cannot_be_confused_with_a_replacement():
    popen = Mock(return_value=FakeProcess(3, "errno=19 ENODEV\n"))
    assert up(popen, DOWN) == (Outcome.KERNEL_REFUSED, Outcome.INSTANCE_CHANGED, 1)
    readback = Mock()
    assert bound_link.bring_down(INSTANCE, popen=popen, read_bound_state=readback) == Outcome.INSTANCE_GONE
    readback.assert_not_called()


def test_up_timeout_terminates_then_bounds_reap_without_trusting_readback():
    process = FakeProcess(timeout=True)
    assert up(Mock(return_value=process), DOWN) == (Outcome.CONFIGURE_FAILED, Outcome.CONFIGURE_FAILED, 1)
    assert process.history == [("communicate", 5), "terminate", ("communicate", 2), "kill", ("wait", 2)]
    assert process.stdout.closed


@pytest.mark.parametrize("process", [
    FakeProcess(1), FakeProcess(4), FakeProcess(5), FakeProcess(timeout=True),
    FakeProcess(3, ""), FakeProcess(3, "errno=1 ENODEV\n"),
])
def test_uncertain_down_never_reads_back_before_submitter_is_gone(process):
    popen = Mock(return_value=process)
    events = []

    def readback(name):
        events.append("read")
        return read(DOWN)

    def wait(quiet):
        events.append("wait")
        return quiet

    assert bound_link.bring_down(INSTANCE, popen=popen, read_bound_state=readback,
                                 wait_quiet=lambda: wait(False)) == Outcome.ROLLBACK_UNVERIFIED
    assert events == ["wait"]
    assert bound_link.bring_down(INSTANCE, popen=popen, read_bound_state=readback,
                                 wait_quiet=lambda: wait(True)) == Outcome.DOWN_VERIFIED
    assert events == ["wait", "wait", "read"]


@pytest.mark.parametrize(("text", "inventory", "outcome"), [
    (DOWN, None, Outcome.DOWN_VERIFIED), (RENAMED, {7}, Outcome.INSTANCE_GONE),
    (RENAMED, None, Outcome.ROLLBACK_UNVERIFIED),
    (RENAMED, {3, 7}, Outcome.ROLLBACK_UNVERIFIED),
    (UNKNOWN, None, Outcome.ROLLBACK_UNVERIFIED),
])
def test_down_readback_and_inventory(text, inventory, outcome):
    popen = Mock(return_value=FakeProcess())
    wait = Mock()
    assert bound_link.bring_down(INSTANCE, popen=popen, read_bound_state=lambda _: read(text),
                                 present_ifindexes=lambda: inventory, wait_quiet=wait,
                                 monotonic=lambda: 10.0) == outcome
    assert popen.call_count == 1
    assert popen.call_args.args == (PREFIX + ["down", "3"] + DEADLINE,)
    wait.assert_not_called()


@pytest.mark.parametrize(("text", "code", "index", "up_flag"), [
    (UP, 0, 3, True), (DOWN, 0, 3, False), (UNKNOWN, 0, 3, None),
    ("", 1, None, None),
])
def test_bound_readback_uses_one_reply_for_both_state_and_index(text, code, index, up_flag):
    run = Mock(return_value=ip_details(text, code))
    observed = bound_link.read_bound_state("can0", run=run)
    with patch.object(canbus.subprocess, "run", return_value=ip_details(text, code)):
        assert observed.state == canbus.interface_state("can0")
    assert observed.ifindex == index
    assert observed.up is up_flag
    assert run.call_count == 1
    assert run.call_args.args == (["ip", "-details", "link", "show", "can0"],)


@pytest.mark.parametrize(("uid", "mode", "expected_digest", "verified"), [
    (0, 0o755, True, True), (1000, 0o755, True, False),
    (0, 0o775, True, False), (0, 0o757, True, False), (0, 0o755, False, False),
])
def test_installed_helper_ownership_mode_and_digest(tmp_path, uid, mode, expected_digest, verified):
    path = tmp_path / "helper"
    path.write_bytes(b"reviewed bytes")
    path.chmod(mode)
    real_fstat = os.fstat

    def file_stat(fd):
        return SimpleNamespace(st_mode=real_fstat(fd).st_mode, st_uid=uid)

    digest = sha256(b"reviewed bytes" if expected_digest else b"other bytes").hexdigest()
    with patch.object(bound_link.os, "fstat", side_effect=file_stat):
        assert bound_link.link_helper_ok(path=path, sha256=digest) is verified


def test_installed_helper_is_not_followed_through_a_symlink(tmp_path):
    target = tmp_path / "helper"
    target.write_bytes(b"reviewed bytes")
    link = tmp_path / "alias"
    link.symlink_to(target)
    assert not bound_link.link_helper_ok(path=link, sha256=sha256(target.read_bytes()).hexdigest())
    assert not bound_link.link_helper_ok(path=tmp_path / "missing")
