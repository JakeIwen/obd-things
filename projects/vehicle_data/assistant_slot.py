"""The warning chat's admission: one model job at a time and at most ten starts a minute.

``ModelSlot`` is the in-process authority the warning chat takes before it launches Codex and
releases after the Codex process has been reaped.
"""

from collections import deque
import threading
import time

from projects.vehicle_data.assistant_common import ChatError

STARTS_PER_WINDOW = 10
WINDOW_SECONDS = 60
BUSY_MESSAGE = "Codex is busy or the short-term limit was reached. Try again shortly"


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
    def __init__(self, window=None):
        self.window = window or StartWindow()
        self._slot = threading.Lock()

    @property
    def busy(self):
        return self._slot.locked()

    def try_acquire(self):
        """Take the slot or raise the warning chat's 429."""
        if self.window.full() or not self._slot.acquire(blocking=False):
            raise ChatError(429, BUSY_MESSAGE)

    def started(self):
        self.window.record()

    def release(self):
        self._slot.release()
