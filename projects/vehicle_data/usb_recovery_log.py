"""Best-effort journal output for the USB drop recovery.

The recovery logs from inside its restoration transaction, which holds channel leases and display
pauses. The service's stdout is a blocking journal pipe, so a stalled sink would hold all of that
while a synchronous write waits. `emit` therefore only enqueues; one daemon thread writes and
flushes. When the bounded queue is full the line is dropped and counted, and the next line that is
delivered carries the count. stdout's file status flags are not touched: O_NONBLOCK on the shared
open file description would break every other writer in the broker. The incident record file is the
durable trail; this output is diagnostic only.
"""

from __future__ import annotations

import queue
import sys
import threading
import time

QUEUE_LINES = 64
ERROR_PREFIX = '<3>ERROR '  # systemd parses the leading <3> as the journal priority.


def write_stdout(line):
    stream = sys.stdout
    stream.write(line + '\n')
    stream.flush()


class BestEffortLog:
    def __init__(self, write=write_stdout, *, capacity=QUEUE_LINES, error_prefix=ERROR_PREFIX):
        self._write = write
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
            if dropped:
                message += f' [{dropped} earlier usb-drop-recovery log lines dropped]'
            try:
                self._write((self._error_prefix if error else '') + message)
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
