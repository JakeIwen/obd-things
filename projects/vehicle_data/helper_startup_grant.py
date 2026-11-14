"""Startup grant between the broker's drive-helper supervisor and a helper process.

A helper must not take CAN ownership until its supervisor says so. Before that
moment a supervisor that cancels the helper (the broker ended the running epoch)
may terminate it at any instruction and nothing needs restoring; afterwards a
helper killed without a final event may have left an interface armed.

The protocol is one pipe per launch and one JSON line:

1. The supervisor creates the pipe (``StartupGrant``), registers it before
   ``Popen``, hands the read end to the child with ``pass_fds`` and
   ``GRANT_FD_FLAG``, and closes its own copy of the read end.
2. The child (``ChildStartupGate``), inside its termination guard and before any
   lock, socket or interface effect, emits ``{"type":"ready","protocol":1,"pid":N}``
   and blocks reading exactly one ``GRANT_TOKEN`` byte.
3. The supervisor answers a valid ready event with ``StartupGrant.try_grant``,
   which refuses once cancelled and records the attempt irreversibly before the
   byte is written, so a cancel racing the write can never be mistaken for
   "never granted".

EOF, any other byte, a changed parent or a signal means no grant: the child ends
with a final event whose ``restored`` is null. Only an explicitly cancelled,
reaped child that was never granted may be reported clean without restoration
evidence (``cancelled_before_grant_final``); every grant attempt keeps the
supervisors' fail-closed handling.
"""

from __future__ import annotations

import fcntl
import os
import stat
import threading
from typing import Callable

PROTOCOL_VERSION = 1
GRANT_FD_FLAG = "--startup-grant-fd"
READY_EVENT_TYPE = "ready"
GRANT_TOKEN = b"G"
_READY_KEYS = frozenset({"type", "protocol", "pid"})


class StartupGrant:
    """Parent-side state of one launch: the pipe, cancellation and grant attempt."""

    def __init__(self) -> None:
        # os.pipe() ends are close-on-exec; only pass_fds re-enables the read end
        # in the one child, so no sibling helper ever inherits this write end.
        read_fd, write_fd = os.pipe()
        self._read_fd: int | None = read_fd
        self._write_fd: int | None = write_fd
        self._lock = threading.Lock()
        self.cancelled = False
        self.grant_attempted = False
        try:
            os.set_blocking(write_fd, False)
        except BaseException:
            self.close()
            raise

    @property
    def child_fd(self) -> int:
        if self._read_fd is None:
            raise ValueError("startup grant read end is already closed")
        return self._read_fd

    def launch_arguments(self) -> list[str]:
        return [GRANT_FD_FLAG, str(self.child_fd)]

    def release_read_end(self) -> None:
        """Close the parent's copy of the child's end (call right after Popen)."""
        with self._lock:
            self._close_fd("_read_fd")

    def cancel(self) -> None:
        """Forbid any future grant. Serialized with ``try_grant``."""
        with self._lock:
            self.cancelled = True
            self._close_fd("_write_fd")

    def try_grant(self, cancel_requested: Callable[[], bool] = lambda: False) -> bool:
        """Record the attempt, then write the token; False when refused by cancel.

        ``cancel_requested`` is evaluated under the lock, so a cancel that lands
        first (here or through ``cancel``) always wins. The attempt is marked
        before the write and never cleared, whatever the write does, because a
        partly delivered byte cannot be told from none.
        """
        with self._lock:
            if cancel_requested():
                self.cancelled = True
            if self.cancelled:
                self._close_fd("_write_fd")  # the child sees EOF, never a grant
            if self.cancelled or self.grant_attempted or self._write_fd is None:
                return False
            self.grant_attempted = True
            try:
                os.write(self._write_fd, GRANT_TOKEN)
            except OSError:
                pass
            finally:
                self._close_fd("_write_fd")
            return True

    def close(self) -> None:
        with self._lock:
            self._close_fd("_read_fd")
            self._close_fd("_write_fd")

    def _close_fd(self, attribute: str) -> None:
        fd = getattr(self, attribute)
        setattr(self, attribute, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def ready_event_problem(event: dict[str, object], child_pid: object) -> str | None:
    """Why ``event`` is not the one valid ready event of this child, or None."""
    if set(event) != _READY_KEYS:
        return "active-drive helper ready event has unexpected fields"
    protocol = event["protocol"]
    if type(protocol) is not int or protocol != PROTOCOL_VERSION:
        return "active-drive helper ready event has an unsupported protocol"
    pid = event["pid"]
    if type(pid) is not int or type(child_pid) is not int or pid != child_pid:
        return "active-drive helper ready event does not match the launched process"
    return None


def pregrant_event_problem(event: dict[str, object]) -> str | None:
    """Why a non-ready event is impossible before the grant, or None.

    Only a final that proves nothing was touched (restored null, mode unknown)
    can precede the grant.
    """
    if (
        event.get("type") == "final"
        and event.get("restored") is None
        and event.get("interface_mode", "unknown") == "unknown"
    ):
        return None
    return "active-drive helper emitted output before its startup grant"


def cancelled_before_grant_final(detail: str) -> dict[str, object]:
    """Final for a child cancelled and reaped without any grant attempt.

    It proves nothing needed restoring, not that restoration was verified.
    """
    return {
        "type": "final",
        "state": "idle",
        "reason": "engine_not_running",
        "detail": detail,
        "interface_mode": "unknown",
        "restored": None,
    }


class ChildStartupGate:
    """Child side of the protocol; call it first thing inside the guarded session.

    Returns None once granted, otherwise the refusal detail. A signal during the
    wait raises like any other interruption of the session, which its own
    finalization turns into a no-mutation final.
    """

    def __init__(
        self,
        fd: int,
        expected_parent_pid: int,
        *,
        getppid: Callable[[], int] = os.getppid,
    ) -> None:
        self.fd = fd
        self.expected_parent_pid = expected_parent_pid
        self._getppid = getppid

    def _fd_problem(self) -> str | None:
        if type(self.fd) is not int or self.fd < 3:
            return "startup grant descriptor is invalid"
        try:
            mode = os.fstat(self.fd).st_mode
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        except OSError:
            return "startup grant descriptor is not open"
        if not stat.S_ISFIFO(mode) or flags & os.O_ACCMODE != os.O_RDONLY:
            return "startup grant descriptor is not a read-only pipe"
        return None

    def __call__(self, sink) -> str | None:
        problem = self._fd_problem()
        if problem is not None:
            return problem
        try:
            sink.emit(
                READY_EVENT_TYPE, protocol=PROTOCOL_VERSION, pid=os.getpid()
            )
            token = os.read(self.fd, 1)
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass
        if token == b"":
            return "supervisor closed the startup grant pipe without a grant"
        if token != GRANT_TOKEN:
            return "startup grant token is invalid"
        if self._getppid() != self.expected_parent_pid:
            return "supervising broker changed before the startup grant"
        return None


def child_gate_from_arguments(
    fd: int | None, expected_parent_pid: int
) -> ChildStartupGate | None:
    """The gate for ``--startup-grant-fd``; None keeps the legacy ungated start."""
    return None if fd is None else ChildStartupGate(fd, expected_parent_pid)


__all__ = (
    "GRANT_FD_FLAG",
    "GRANT_TOKEN",
    "PROTOCOL_VERSION",
    "READY_EVENT_TYPE",
    "ChildStartupGate",
    "StartupGrant",
    "cancelled_before_grant_final",
    "child_gate_from_arguments",
    "pregrant_event_problem",
    "ready_event_problem",
)
