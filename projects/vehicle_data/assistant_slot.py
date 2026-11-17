"""The warning chat's admission: one model job at a time and at most ten starts a minute.

``ModelSlot`` is the in-process authority the warning chat takes before it launches Codex and
releases after the Codex process has been reaped. It also holds the shared CLI lock for that
time, so a warning-chat Codex and a research CLI never run together (docs/dtc-research.md).
"""

from collections import deque
import sys
import threading
import time

from projects.vehicle_data.assistant_common import ChatError
from projects.vehicle_data.model_cli_lock import CliLock, HELD, MISSING

STARTS_PER_WINDOW = 10
WINDOW_SECONDS = 60
BUSY_MESSAGE = "Codex is busy or the short-term limit was reached. Try again shortly"
RESEARCH_RUNNING = "A diagnostic-code research job is running; ask again when it finishes"


class StartWindow:
    """Sliding window of start times; callers serialize access."""

    def __init__(self, limit=STARTS_PER_WINDOW, seconds=WINDOW_SECONDS, clock=time.monotonic):
        self.limit, self.seconds, self.clock = limit, seconds, clock
        self.recent = deque()
        self._checked_at = None

    def full(self):
        stamp = self._checked_at = self.clock()
        while self.recent and stamp - self.recent[0] > self.seconds:
            self.recent.popleft()
        return len(self.recent) >= self.limit

    def record(self):
        """Count the start checked by the last ``full()`` call, at that check's time."""
        self.recent.append(self._checked_at if self._checked_at is not None else self.clock())


class ModelSlot:
    def __init__(self, window=None, cli_lock=None):
        self.window = window or StartWindow()
        self._slot = threading.Lock()
        self.cli_lock = cli_lock or CliLock()
        self._missing_logged = False

    @property
    def busy(self):
        return self._slot.locked()

    def try_acquire(self):
        """Take the slot or raise the warning chat's 429."""
        if self.window.full() or not self._slot.acquire(blocking=False):
            raise ChatError(429, BUSY_MESSAGE)
        try:
            state = self.cli_lock.try_acquire()
        except OSError:
            self._slot.release()
            raise
        if state == HELD:
            self._slot.release()
            raise ChatError(429, RESEARCH_RUNNING)
        if state == MISSING and not self._missing_logged:
            # Research refuses to launch without this file, so nothing can overlap.
            self._missing_logged = True
            print("shared CLI lock file is absent; warning chat continues without it", file=sys.stderr, flush=True)

    def started(self):
        self.window.record()

    def release(self):
        self.cli_lock.release()
        self._slot.release()
