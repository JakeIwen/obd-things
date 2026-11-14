"""Scripted drive-helper processes for the startup-cancellation tests.

``HelperLauncher`` stands in for ``subprocess.Popen``: it keeps a duplicate of the
startup-grant read end the supervisor passes, exactly as a child would inherit it,
so a test can observe whether a grant byte, EOF or nothing reached the child.
Nothing here touches CAN, a service or a real helper.
"""

import json
import os
import queue
import select
import subprocess
import threading
import unittest

from projects.vehicle_data.broker import TelemetryBroker
import tests.test_active_drive as drive_tests

GRANT_FLAG = "--startup-grant-fd"
WAIT_SECONDS = 10.0


class ScriptedStream:
    def __init__(self):
        self.lines = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        item = self.lines.get(timeout=WAIT_SECONDS)
        if item is None:
            raise StopIteration
        return item

    def close(self):
        self.lines.put(None)


class ScriptedHelper:
    """A child that exits only when the test (or a terminate) says so."""

    def __init__(self, pid=4321, *, die_on_terminate=True, terminate_status=-15):
        self.pid = pid
        self.stdout = ScriptedStream()
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.terminated = threading.Event()
        self.die_on_terminate = die_on_terminate
        self.terminate_status = terminate_status

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("helper", timeout)
        return self.returncode

    def emit(self, event):
        self.stdout.lines.put(json.dumps(event) + "\n")

    def ready(self, **overrides):
        event = {"type": "ready", "protocol": 1, "pid": self.pid}
        event.update(overrides)
        self.emit(event)

    def die(self, status=0):
        self.returncode = status
        self.stdout.lines.put(None)

    def terminate(self):
        self.terminate_calls += 1
        self.terminated.set()
        if self.die_on_terminate and self.returncode is None:
            self.die(self.terminate_status)

    def kill(self):
        self.kill_calls += 1
        if self.returncode is None:
            self.die(-9)


class HelperLauncher:
    """Popen factory that hands out ``process`` and keeps the child's grant end."""

    def __init__(self, process=None, *, on_spawn=None):
        self.process = process or ScriptedHelper()
        self.on_spawn = on_spawn
        self.spawned = threading.Event()
        self.commands = []
        self.kwargs = []
        self.child_fd = None

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        self.kwargs.append(kwargs)
        if GRANT_FLAG in command:
            fd = int(command[command.index(GRANT_FLAG) + 1])
            # What the child inherits: a second descriptor on the same pipe.
            self.child_fd = os.dup(fd)
            os.set_blocking(self.child_fd, False)
        if self.on_spawn is not None:
            self.on_spawn(self)
        self.spawned.set()
        return self.process

    def read_grant(self, timeout=WAIT_SECONDS):
        """b'G' when granted, b'' on EOF (never granted), None on timeout."""
        readable, _, _ = select.select([self.child_fd], [], [], timeout)
        if not readable:
            return None
        return os.read(self.child_fd, 1)

    def close(self):
        if self.child_fd is not None:
            try:
                os.close(self.child_fd)
            except OSError:
                pass
            self.child_fd = None


class StartupRaceCase(unittest.TestCase):
    """Common builders: threads, launchers and a broker with scripted supervisors."""

    def launcher(self, process=None, **kwargs):
        launcher = HelperLauncher(process, **kwargs)
        self.addCleanup(launcher.close)
        return launcher

    def run_in_thread(self, supervisor, stop_event):
        results = []
        thread = threading.Thread(
            target=lambda: results.append(supervisor.run(stop_event)),
            daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, WAIT_SECONDS)
        return thread, results

    def join(self, thread, results):
        thread.join(WAIT_SECONDS)
        self.assertFalse(thread.is_alive(), "supervisor did not finish")
        return results[0]

    def make_broker(self, *, active, auxiliary):
        return TelemetryBroker(
            acquirer=drive_tests.BrokerActiveDriveTests.Acquirer(),
            monotonic=drive_tests.BrokerActiveDriveTests.Clock(),
            active_drive_supervisor=active,
            active_drive_enabled=True,
            auxiliary_drive_supervisor=auxiliary,
            auxiliary_drive_enabled=True,
        )
