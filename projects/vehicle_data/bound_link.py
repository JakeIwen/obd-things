"""Use the installed index-only setter and bind readback to the kernel instance."""

import errno
import hashlib
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from lib import canbus, netdev_instance
from lib.can_runtime_route import LinkExpectation
from lib.netdev_instance import NetdevInstance
from projects.vehicle_data import usb_recovery_contract as contract
from projects.vehicle_data.usb_recovery_contract import MutationOutcome, Refusal

_SEND_WINDOW_SECONDS = 2.0
_TERMINATE_WAIT_SECONDS = 2.0
_KERNEL_ERROR_LINE = re.compile(r"errno=([1-9][0-9]{0,9}) ([A-Z][A-Z0-9]*)\n?")


@dataclass(frozen=True)
class LinkRead:
    ifindex: int | None
    up: bool | None
    state: canbus.InterfaceState


def read_bound_state(name: str, *, run=None) -> LinkRead:
    try:
        if run is None:
            result = canbus.bounded_link_details(name, timeout=contract.IP_TIMEOUT_SECONDS)
        else:
            result = run(["ip", "-details", "link", "show", name], capture_output=True, text=True,
                         timeout=contract.IP_TIMEOUT_SECONDS)
        text = result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        text = None
    header = netdev_instance.first_line(text) if text is not None else None
    index, flags = header if header is not None else (None, None)
    return LinkRead(index, None if flags is None else "UP" in flags,
                    canbus.parse_interface_state(name, text))


def link_helper_ok(*, path: str | Path | None = None, sha256: str | None = None) -> bool:
    target = contract.LINK_HELPER_PATH if path is None else path
    expected = contract.LINK_HELPER_SHA256 if sha256 is None else sha256
    # Check ownership and contents of the same regular file, without following symlinks.
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            return bool(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                        and hashlib.file_digest(file, "sha256").hexdigest() == expected)
    except OSError:
        return False


@dataclass(frozen=True)
class _Submission:
    acknowledged: bool = False
    kernel_errno: int | None = None
    started: bool = True

    @property
    def certain(self) -> bool:
        return self.acknowledged or self.kernel_errno is not None


def _stop(process):
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=_TERMINATE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        # A surviving command can still own the pipe. Wait only for sudo here.
        try:
            process.wait(timeout=_TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass  # Still uncertain: only a positive cgroup check can authorize rollback.


def _submit(command, index, args, *, popen, monotonic) -> _Submission:
    try:
        deadline = monotonic() + _SEND_WINDOW_SECONDS
    except Exception:
        return _Submission(started=False)
    argv = ["sudo", "-n", "/usr/bin/python3", "-I", "-B", contract.LINK_HELPER_PATH,
            command, str(index), *args, "--not-after", str(deadline)]
    try:
        process = popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                        errors="replace")
    except OSError:
        return _Submission(started=False)
    try:
        try:
            out, _ = process.communicate(timeout=contract.IP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _stop(process)
            return _Submission()
        if process.returncode == 0:
            return _Submission(acknowledged=True)
        if process.returncode == 3 and (match := _KERNEL_ERROR_LINE.fullmatch(out)):
            code = int(match[1])
            if errno.errorcode.get(code, "UNKNOWN") == match[2]:
                return _Submission(kernel_errno=code)
        return _Submission()
    finally:
        if process.stdout is not None:
            process.stdout.close()


@dataclass(frozen=True)
class BringUpResult:
    outcome: MutationOutcome
    refusal: Refusal | None = None


def bring_up_listen_only(
    instance: NetdevInstance, bitrate: int, *, read_bound_state=read_bound_state,
    popen=subprocess.Popen, monotonic=time.monotonic,
) -> BringUpResult:
    try:
        before = read_bound_state(instance.channel)
        if before.ifindex != instance.ifindex:
            return BringUpResult(MutationOutcome.NOT_SUBMITTED, Refusal.INSTANCE_CHANGED)
        if not before.state.present or before.up is not False:
            return BringUpResult(MutationOutcome.NOT_SUBMITTED, Refusal.VERIFICATION_FAILED)
    except Exception:
        return BringUpResult(MutationOutcome.NOT_SUBMITTED, Refusal.VERIFICATION_FAILED)
    result = _submit("up-listen-only", instance.ifindex, (str(bitrate),),
                     popen=popen, monotonic=monotonic)
    if not result.started:
        return BringUpResult(MutationOutcome.NOT_SUBMITTED, Refusal.CONFIGURE_FAILED)
    if result.kernel_errno is not None:
        refusal = Refusal.INSTANCE_CHANGED if result.kernel_errno == errno.ENODEV else Refusal.CONFIGURE_FAILED
        return BringUpResult(MutationOutcome.KERNEL_REFUSED, refusal)
    if not result.acknowledged:
        return BringUpResult(MutationOutcome.CONFIGURE_FAILED, Refusal.CONFIGURE_FAILED)
    after = read_bound_state(instance.channel)
    if after.ifindex != instance.ifindex:
        return BringUpResult(MutationOutcome.INSTANCE_CHANGED, Refusal.INSTANCE_CHANGED)
    expected = LinkExpectation(bitrate, listen_only=True, channel=instance.channel)
    if after.up is not True or not expected.matches(after.state):
        return BringUpResult(MutationOutcome.VERIFICATION_FAILED, Refusal.VERIFICATION_FAILED)
    return BringUpResult(MutationOutcome.UP_VERIFIED)


def bring_down(
    instance: NetdevInstance, *, wait_quiet=None, read_bound_state=read_bound_state,
    present_ifindexes=netdev_instance.present_ifindexes, popen=subprocess.Popen,
    monotonic=time.monotonic,
) -> MutationOutcome:
    """Down by index; an uncertain submission requires a fresh positive quiescence check."""
    result = _submit("down", instance.ifindex, (), popen=popen, monotonic=monotonic)
    if result.kernel_errno == errno.ENODEV:
        return MutationOutcome.INSTANCE_GONE
    if not result.certain and (wait_quiet is None or wait_quiet() is not True):
        return MutationOutcome.ROLLBACK_UNVERIFIED
    after = read_bound_state(instance.channel)
    if after.ifindex == instance.ifindex:
        if after.state.present and after.up is False:
            return MutationOutcome.DOWN_VERIFIED
    else:
        indexes = present_ifindexes()
        if indexes is not None and instance.ifindex not in indexes:
            return MutationOutcome.INSTANCE_GONE
    return MutationOutcome.ROLLBACK_UNVERIFIED
