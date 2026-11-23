"""Keep journal writes off the thread that holds the restoration leases.

The writer uses a private descriptor, not Python's buffered stdout: a full journal pipe
must not hold the stream lock during interpreter shutdown. The incident file is the durable trail.
"""

from __future__ import annotations

import os
import queue
import select
import sys
import threading
import time

QUEUE_LINES = 64
ERROR_PREFIX = '<3>ERROR '  # systemd parses the leading <3> as the journal priority.


# One pipe write of at most PIPE_BUF bytes is atomic, so a line is never interleaved with another
# writer's. Longer lines are truncated to fit, newline included.
PIPE_BUF = getattr(select, 'PIPE_BUF', 512)
MAX_LINE_BYTES = min(PIPE_BUF, 4096)


def fit_line(line, limit=MAX_LINE_BYTES):
    """Encode `line` plus a newline in at most `limit` bytes, cutting on a character boundary."""
    return clip_text(line, limit - 1).encode('utf-8') + b'\n'


def clip_text(text, limit):
    """`text` cut to at most `limit` UTF-8 bytes on a character boundary."""
    return text.encode('utf-8', 'replace')[:max(limit, 0)].decode('utf-8', 'ignore')


class FdSink:
    """Writes whole lines to a private duplicate of a descriptor with os.write only."""

    def __init__(self, fd):
        self._fd = fd

    @classmethod
    def for_stdout(cls):
        """Duplicate stdout's descriptor once; None when it has none (the sink then drops lines)."""
        try:
            return cls(os.dup(sys.stdout.fileno()))
        except (OSError, ValueError, AttributeError):
            try:
                return cls(os.dup(1))
            except OSError:
                return None

    def __call__(self, line):
        view = memoryview(fit_line(line))
        while view:
            view = view[os.write(self._fd, view):]


def _discard(_line):
    pass


class BestEffortLog:
    def __init__(self, write=None, *, capacity=QUEUE_LINES, error_prefix=ERROR_PREFIX):
        self._write = write if write is not None else FdSink.for_stdout() or _discard
        self._error_prefix = error_prefix
        self._lines = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._dropped = 0
        self._thread = None

    def start(self):
        """Start the writer once. Lines queued earlier are delivered then."""
        with self._lock:
            if self._thread is None:
                try:
                    thread = threading.Thread(target=self._run, name='usb-recovery-log', daemon=True)
                    thread.start()
                except Exception:
                    return False  # No writer: lines queue, then drop. Diagnostics never block recovery.
                self._thread = thread
        return True

    def emit(self, message, *, error=False):
        """Queue one line. Never blocks or raises; False means the line was dropped."""
        try:
            self._lines.put_nowait((message, error))
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False

    def _restore_dropped(self, count):
        with self._lock:
            self._dropped += count

    def _take_dropped(self):
        with self._lock:
            count, self._dropped = self._dropped, 0
        return count

    def _run(self):
        while True:
            message, error = self._lines.get()
            dropped = self._take_dropped()
            prefix = self._error_prefix if error else ''
            if dropped:
                # Cut the message, not the count: the sink truncates an over-long line at its end.
                suffix = f' [{dropped} earlier usb-drop-recovery log lines dropped]'
                room = MAX_LINE_BYTES - 1 - len((prefix + suffix).encode('utf-8'))
                message = clip_text(message, room) + suffix
            try:
                self._write(prefix + message)
            except Exception:
                # A broken sink loses this line; the count it should have carried goes to the next one.
                self._restore_dropped(dropped)
            finally:
                self._lines.task_done()

    def drain(self, timeout=10):
        """Test and diagnostic seam: wait until every queued line was handed to the sink."""
        deadline = time.monotonic() + timeout
        while self._lines.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return not self._lines.unfinished_tasks
