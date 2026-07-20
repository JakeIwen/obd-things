"""Cross-process safety primitives for active diagnostic tools."""

import fcntl
import os
import re
import signal
import time
from contextlib import contextmanager


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCK_DIR = os.path.join(REPO, "tmp", "locks")


class ChannelLockError(RuntimeError):
    """Another participating transmitter already owns a SocketCAN channel."""


def _validate_channel_name(channel):
    if not isinstance(channel, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", channel):
        raise ValueError(f"unsafe SocketCAN channel name: {channel!r}")
    return channel


def acquire_channel_lock(channel):
    """Acquire a nonblocking exclusive lock for one SocketCAN channel.

    Returns an open handle that must be passed to :func:`release_channel_lock`. The file lives
    under gitignored ``tmp/`` and carries diagnostic PID/time metadata for troubleshooting.
    """
    _validate_channel_name(channel)
    os.makedirs(LOCK_DIR, exist_ok=True)
    path = os.path.join(LOCK_DIR, f"active-diagnostics-{channel}.lock")
    handle = open(path, "a+", encoding="utf-8")
    locked = False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired_epoch={time.time():.6f}\n")
        handle.flush()
        # Treat the returned handle as a small capability: interface-changing helpers can verify
        # that their caller still owns the lock for the exact channel before mutating SocketCAN.
        handle._diagnostic_lock_channel = channel
        handle._diagnostic_lock_held = True
        return handle
    except BlockingIOError:
        handle.close()
        raise ChannelLockError(
            f"another active diagnostic tool already holds the {channel} lock"
        ) from None
    except BaseException:
        try:
            if locked:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        raise


def release_channel_lock(handle):
    """Release and close a handle returned by :func:`acquire_channel_lock`."""
    if handle is None:
        return
    try:
        handle._diagnostic_lock_held = False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def validate_channel_lock(handle, channel):
    """Require a live lock capability for ``channel`` and return it.

    This intentionally validates only handles created by :func:`acquire_channel_lock`; an
    arbitrary open file or a released/wrong-channel handle cannot opt a caller into interface
    mutation or transport recovery.
    """
    _validate_channel_name(channel)
    if (
        handle is None
        or getattr(handle, "closed", True)
        or not getattr(handle, "_diagnostic_lock_held", False)
        or getattr(handle, "_diagnostic_lock_channel", None) != channel
    ):
        raise ChannelLockError(f"a held {channel} active-diagnostics lock is required")
    try:
        handle.fileno()
    except (AttributeError, OSError, ValueError):
        raise ChannelLockError(f"a held {channel} active-diagnostics lock is required") from None
    return handle


@contextmanager
def channel_lock(channel):
    """Hold a channel lock for the complete body of a ``with`` statement."""
    handle = acquire_channel_lock(channel)
    try:
        yield handle
    finally:
        release_channel_lock(handle)


@contextmanager
def interrupt_on_termination():
    """Yield a guard that turns INT/TERM/HUP into one cleanup-safe interruption.

    Call ``guard.begin_cleanup()`` at the top of the caller's ``finally`` block and keep socket
    close, passive restoration, and lock release *inside* this context. The first TERM/HUP during
    active work raises ``KeyboardInterrupt``; repeated signals and every TERM/HUP after cleanup
    begins are recorded and ignored. Original handlers are restored only after the protected
    cleanup has completed. This also prevents a second Ctrl-C from cutting through cleanup.
    """

    class TerminationGuard:
        def __init__(self):
            self.received_signal = None
            self.cleanup_started = False
            self._interruption_raised = False

        def begin_cleanup(self):
            """Make INT/TERM/HUP non-raising for the remainder of protected cleanup."""
            self.cleanup_started = True

        def handle(self, signum, _frame):
            if self.received_signal is None:
                self.received_signal = signum
            if self.cleanup_started or self._interruption_raised:
                return
            self._interruption_raised = True
            raise KeyboardInterrupt

    guard = TerminationGuard()
    old_handlers = {}

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old_handlers[signum] = signal.signal(signum, guard.handle)
        yield guard
    finally:
        # Also protects handler restoration when a caller exits without explicitly starting a
        # cleanup phase. Callers still must begin cleanup before their own safety operations.
        guard.begin_cleanup()
        for signum, old_handler in old_handlers.items():
            signal.signal(signum, old_handler)
