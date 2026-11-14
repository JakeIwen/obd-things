"""Real child processes for the startup grant: pipes, signals and helper entries.

``tests.helper_startup_stub`` is an inert helper (no CAN) used with the real
supervisor and real ``Popen``.  The two real helper mains run only to their
barrier, with an audit hook proving no socket, subprocess, lock file or
interface effect happens before a grant; a grant is never sent to them.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from projects.vehicle_data.broker import ActiveDriveSupervisor
from projects.vehicle_data.drive_helper_launch import HELPER_CWD
from projects.vehicle_data.radar_service_control import HelperCommandPipe

STUB = "tests.helper_startup_stub"
WAIT_SECONDS = 60.0

AUDIT_LAUNCHER = textwrap.dedent(
    """
    import importlib, json, sys
    sys.dont_write_bytecode = True
    repo, module_name, *argv = sys.argv[1:]
    sys.path.insert(0, repo)
    seen = []
    WATCHED = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork",
               "os.forkpty", "os.mkdir", "os.rename", "os.remove", "os.rmdir",
               "os.symlink", "os.truncate"}
    def hook(event, args):
        if event in WATCHED or event.startswith("socket."):
            seen.append(event)
        elif event == "open":
            path = str(args[0])
            if "lock" in path or path.startswith(("/sys/", "/dev/", "/proc/net")):
                seen.append("open " + path)
    sys.addaudithook(hook)
    module = importlib.import_module(module_name)
    if sys.platform != "linux":
        # prctl(PR_SET_PDEATHSIG) is Linux-only; keep the parent-PID check so the
        # entry point still reaches its barrier here. The barrier does not use it.
        import os
        from projects.vehicle_data import active_drive
        def parent_only(expected):
            if os.getppid() != expected:
                raise RuntimeError("parent mismatch")
        active_drive._set_parent_death_signal = parent_only
    status = module.main(argv)
    sys.stderr.write(json.dumps(seen))
    sys.exit(status)
    """
)


class StubSupervisorTests(unittest.TestCase):
    def supervisor(self, **options):
        defaults = dict(
            channel="can9",
            expected_usb_serial="serial-x",
            expected_dev_id=0,
            event_handler=lambda _event: None,
            helper_module=STUB,
        )
        defaults.update(options)
        return ActiveDriveSupervisor(**defaults)

    def run_async(self, supervisor):
        results = []
        thread = threading.Thread(
            target=lambda: results.append(supervisor.run(threading.Event())), daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, WAIT_SECONDS)
        return thread, results

    def wait_for_process(self, pipe, thread):
        """Block until the child exists; fail at once if the supervisor gave up."""
        deadline = time.monotonic() + WAIT_SECONDS
        while not pipe.alive():
            if not thread.is_alive():
                self.fail("the supervisor finished without a helper process appearing")
            if time.monotonic() > deadline:
                self.fail("no helper process appeared")
            time.sleep(0.01)

    def test_a_spawn_failure_fails_the_wait_instead_of_hanging(self):
        def broken(_command, **_kwargs):
            raise OSError("no interpreter")

        pipe = HelperCommandPipe(popen_factory=broken)
        thread, results = self.run_async(self.supervisor(popen_factory=pipe.popen))
        with self.assertRaisesRegex(AssertionError, "without a helper process"):
            self.wait_for_process(pipe, thread)
        thread.join(WAIT_SECONDS)
        self.assertEqual(results[0]["reason"], "helper_start_failed")

    def test_real_pipe_grants_the_stub_and_radar_stdin_still_reaches_it(self):
        pipe = HelperCommandPipe()
        supervisor = self.supervisor(popen_factory=pipe.popen)
        thread, results = self.run_async(supervisor)
        self.wait_for_process(pipe, thread)
        self.assertTrue(pipe.send("calibrate\n"))
        thread.join(WAIT_SECONDS)

        result = results[0]
        self.assertIs(result["restored"], True)
        self.assertEqual(result["detail"], "stdin=calibrate")
        self.assertIsNone(supervisor._launch)

    def test_real_pipe_without_the_radar_flag_grants_and_finishes(self):
        supervisor = self.supervisor()
        result = supervisor.run(threading.Event())
        self.assertIs(result["restored"], True)
        self.assertEqual(result["detail"], "granted")

    def test_signal_before_the_barrier_is_a_clean_cancellation(self):
        with mock.patch.dict(os.environ, {"HELPER_STUB_MODE": "hold"}):
            pipe = HelperCommandPipe()
            supervisor = self.supervisor(popen_factory=pipe.popen)
            thread, results = self.run_async(supervisor)
            self.wait_for_process(pipe, thread)
            supervisor.stop()
            thread.join(WAIT_SECONDS)

        result = results[0]
        self.assertEqual(result["reason"], "engine_not_running")
        self.assertIsNone(result["restored"])
        self.assertEqual(result["interface_mode"], "unknown")
        self.assertIn(f"status {-signal.SIGTERM}", result["detail"])


class RealHelperBarrierTests(unittest.TestCase):
    HELPERS = ("projects.vehicle_data.active_drive", "projects.vehicle_data.bcan_auxiliary")

    def launch(self, module):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        script = Path(directory.name) / "audited_helper.py"
        script.write_text(AUDIT_LAUNCHER)
        read_fd, write_fd = os.pipe()
        self.addCleanup(self.close_quietly, write_fd)
        process = subprocess.Popen(
            [sys.executable, "-I", str(script), str(HELPER_CWD), module,
             "--channel", "can9", "--expected-usb-serial", "serial-x",
             "--expected-dev-id", "0x0", "--expected-parent-pid", str(os.getpid()),
             "--startup-grant-fd", str(read_fd)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            pass_fds=(read_fd,), cwd=directory.name,
        )
        os.close(read_fd)
        watchdog = threading.Timer(WAIT_SECONDS, process.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.addCleanup(process.wait)
        ready = json.loads(process.stdout.readline())
        self.assertEqual(ready, {"type": "ready", "protocol": 1, "pid": process.pid})
        return process, write_fd

    @staticmethod
    def close_quietly(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def finish(self, process):
        out, err = process.communicate(timeout=WAIT_SECONDS)
        finals = [json.loads(line) for line in out.splitlines()]
        audit = json.loads(err.splitlines()[-1])
        self.assertEqual(process.returncode, 0, err)
        return finals, audit

    def test_pipe_eof_before_the_grant_ends_without_any_can_effect(self):
        for module in self.HELPERS:
            with self.subTest(module=module):
                process, write_fd = self.launch(module)
                os.close(write_fd)
                (final,), audit = self.finish(process)
                self.assertEqual(audit, [])
                self.assertEqual(final["type"], "final")
                self.assertIsNone(final["restored"])
                self.assertEqual(final["interface_mode"], "unknown")
                self.assertEqual(final["reason"], "helper_failed")

    def test_termination_at_the_barrier_ends_without_any_can_effect(self):
        for module in self.HELPERS:
            with self.subTest(module=module):
                process, _write_fd = self.launch(module)
                process.send_signal(signal.SIGTERM)
                (final,), audit = self.finish(process)
                self.assertEqual(audit, [])
                self.assertEqual(final["reason"], "engine_not_running")
                self.assertIsNone(final["restored"])


if __name__ == "__main__":
    unittest.main()
