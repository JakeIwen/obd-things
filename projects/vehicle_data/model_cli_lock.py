"""The kernel lock that keeps a warning-chat CLI and a research CLI from running together.

Both launchers take one exclusive, non-blocking ``flock`` on ``LOCK_PATH`` just before they spawn
a model CLI and drop it after the child is reaped. The file is created at boot by
``systemd/van-assistant.tmpfiles.conf``; both units see it read-only, which is enough for
``flock``. The descriptor is close-on-exec, so the launching service process is always the
holder: if it dies the kernel releases the lock and ``KillMode=control-group`` removes its child
(an accepted crash-path window, see docs/dtc-research.md).
"""

import fcntl
import os
from pathlib import Path

LOCK_PATH = Path("/run/van-assistant/model-cli.lock")

ACQUIRED = "acquired"
HELD = "held"
MISSING = "missing"


class CliLock:
    def __init__(self, path=LOCK_PATH):
        self.path = Path(path)
        self._fd = None

    @property
    def held_here(self):
        return self._fd is not None

    def try_acquire(self):
        """``ACQUIRED``, ``HELD`` (another process has it) or ``MISSING`` (no lock file)."""
        if self._fd is not None:
            raise RuntimeError("the CLI lock is already held by this launcher")
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return MISSING
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return HELD
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        return ACQUIRED

    def release(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # closing the only descriptor drops the flock
