"""Subprocess termination tests for the Unix telemetry API."""

from __future__ import annotations

import os
import pathlib
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.vehicle_data_api_concurrency_support import (
    REPO_ROOT,
    TMP_BASE,
    request_bytes,
    read_http,
    unix_socket,
)


SUBPROCESS_SCRIPT = r'''
import os
import pathlib
import sys
import threading
from dataclasses import replace

from lib import can_wake
from lib.diagnostic_safety import interrupt_on_termination
from projects.vehicle_data import api_server
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.metrics import METRICS

HANDSHAKE = int(os.environ["O2_HANDSHAKE_FD"])
SOCKET = pathlib.Path(os.environ["O2_SOCKET"])
MODE = os.environ["O2_MODE"]

def report(message):
    os.write(HANDSHAKE, (message + "\n").encode())

def control(expected):
    received = sys.stdin.readline().strip()
    if received != expected:
        raise AssertionError((expected, received))

class FakeWakeTurn:
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return None

class FakeWakeSession:
    def __init__(self, state):
        self.state = state
        self.closed = False
    def trigger(self):
        self.state["started"].set()
        report("ACTIVE main=" + str(threading.current_thread() is threading.main_thread()))
        self.state["block"].wait()
        raise AssertionError("fake wake was unexpectedly released")
    def close(self):
        if self.closed:
            return
        self.closed = True
        report("RESTORE_WAIT")
        self.state["restoring"].set()
        control("restore")
        self.state["restored"] = True
        self.state["owner"].restored = True
        report("RESTORED")

class FakeAcquirer:
    channel = "can7"
    def __init__(self):
        self.started = threading.Event()
        self.block = threading.Event()
        self.restoring = threading.Event()
        self.restored = MODE in ("idle", "read")
        self.calls = []
        if MODE == "wake":
            can_wake.can_handoff.active_turn = lambda _role: FakeWakeTurn()
            state = {
                "started": self.started,
                "block": self.block,
                "restoring": self.restoring,
                "restored": False,
                "owner": self,
            }
            can_wake._open_wake_session = lambda *_args, **_kwargs: FakeWakeSession(state)
            self._wake_state = state
    def acquire(self, mode):
        self.calls.append(mode)
        self.started.set()
        try:
            if MODE == "wake":
                return can_wake.wake_once("b-can", prearm_check=lambda: ())
            if MODE == "interrupt":
                report("ACTIVE main=" + str(threading.current_thread() is threading.main_thread()))
                control("interrupt")
                raise KeyboardInterrupt
            raise AssertionError("unexpected acquisition in " + MODE)
        finally:
            if MODE == "interrupt":
                self.restored = True
                report("RESTORED")
    def status_snapshot(self):
        return {"channel": self.channel, "adapter_present": True, "up": True,
                "bitrate": 125000, "fd_enabled": False, "one_shot": False,
                "listen_only": True, "controller_state": "ERROR-ACTIVE",
                "topology": {"bus": "b-can", "usable": True, "reason": ""},
                "active_inhibits": []}

state = FakeAcquirer()
MAIN_THREAD_ID = None
class HandshakeServer(api_server.UnixHTTPServer):
    def serve_forever(self, *args, **kwargs):
        global MAIN_THREAD_ID
        MAIN_THREAD_ID = threading.get_ident()
        report("READY")
        return super().serve_forever(*args, **kwargs)
    def _submit_post(self, handler):
        job = super()._submit_post(handler)
        if handler.headers.get("X-O2-Queued") == "yes":
            report("QUEUED")
        return job
    def server_close(self):
        report("SERVER_CLOSE restored=" + str(state.restored) + " socket=" + str(SOCKET.exists()) + " calls=" + str(len(state.calls)))
        return super().server_close()

api_server.UnixHTTPServer = HandshakeServer
definitions = dict(METRICS)
definitions["battery.voltage"] = replace(
    METRICS["battery.voltage"], passive_min_interval_seconds=0.0,
    wake_min_interval_seconds=0.0,
)
broker = TelemetryBroker(acquirer=state, definitions=definitions, collector_interval_seconds=0.0)
if MODE == "read":
    def blocked_status():
        report("READ_ACTIVE worker=" + str(threading.get_ident()) + " main=" + str(MAIN_THREAD_ID)
               + " owns_lock=" + str(broker._lock._is_owned()))
        threading.Event().wait()
        raise AssertionError("blocked reader was unexpectedly released")
    broker.status_response = blocked_status
original_close = broker.close
def close():
    report("BEFORE_CLOSE restored=" + str(state.restored) + " socket=" + str(SOCKET.exists()))
    return original_close()
broker.close = close
try:
    with interrupt_on_termination() as termination:
        try:
            api_server.serve_unix(broker, str(SOCKET))
        finally:
            termination.begin_cleanup()
            report("SERVE_FINALLY restored=" + str(state.restored) + " socket=" + str(SOCKET.exists()))
            broker.close()
except KeyboardInterrupt:
    if MODE != "interrupt":
        raise  # Real SIGTERM cases must retain the production -SIGINT exit.
    report("INTERRUPT_PROPAGATED")  # Unit case catches its explicitly raised exception.
'''


class HandshakeReader:
    def __init__(self, fd: int):
        self.fd = fd
        self.buffer = bytearray()

    def line(self, timeout: float = 1.0) -> str:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("subprocess handshake timed out")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                raise AssertionError("subprocess handshake timed out")
            chunk = os.read(self.fd, 4096)
            if not chunk:
                raise AssertionError("subprocess closed handshake pipe")
            self.buffer.extend(chunk)
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        return line.decode()

    def rest(self, timeout: float = 1.0) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("subprocess handshake EOF timed out")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                raise AssertionError("subprocess handshake EOF timed out")
            chunk = os.read(self.fd, 4096)
            if not chunk:
                return bytes(self.buffer).decode()
            self.buffer.extend(chunk)


def start_signal_child(tmp_name: str, mode: str, repo_root=REPO_ROOT):
    read_fd, write_fd = os.pipe()
    socket_path = str(pathlib.Path(tmp_name) / "child.sock")
    env = {
        **os.environ,
        "O2_HANDSHAKE_FD": str(write_fd),
        "O2_SOCKET": socket_path,
        "O2_MODE": mode,
        "PYTHONPATH": str(repo_root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", SUBPROCESS_SCRIPT],
        cwd=repo_root,
        env=env,
        pass_fds=(write_fd,),
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(write_fd)
    return process, read_fd, socket_path


class SubprocessTerminationTests(unittest.TestCase):
    def _prepare_signal_case(
        self, mode, socket_path, reader, lines, clients, concurrent_reads
    ):
        lines.append(reader.line())
        self.assertEqual(lines[-1], "READY")
        if mode in ("wake", "interrupt"):
            sock = unix_socket(socket_path)
            clients.append(sock)
            request_bytes(sock, "POST", "/v1/acquisitions/battery.voltage",
                          {"mode": "wake_if_asleep" if mode == "wake" else "passive"})
            lines.append(reader.line())
            self.assertEqual(lines[-1], "ACTIVE main=True", lines)
        elif mode == "read":
            sock = unix_socket(socket_path)
            clients.append(sock)
            request_bytes(sock, "GET", "/v1/status")
            lines.append(reader.line())
            self.assertTrue(lines[-1].startswith("READ_ACTIVE "), lines)
            fields = dict(item.split("=", 1) for item in lines[-1].split()[1:])
            self.assertEqual(fields["owns_lock"], "False")
            self.assertEqual(fields["worker"] != fields["main"], concurrent_reads)
        else:
            # READY is inside serve_unix's try; this also proves loop startup.
            sock = unix_socket(socket_path)
            clients.append(sock)
            request_bytes(sock, "GET", "/v1/metrics")
            self.assertEqual(read_http(sock)[0], 200)

        if mode == "interrupt":
            queued = unix_socket(socket_path)
            clients.append(queued)
            request_bytes(queued, "POST", "/v1/acquisitions/battery.voltage",
                          {"mode": "passive"}, headers={"X-O2-Queued": "yes"})
            lines.append(reader.line())
            self.assertEqual(lines[-1], "QUEUED")

    def _send_signal(self, mode, process, reader, lines):
        if mode == "interrupt":
            process.stdin.write(b"interrupt\n")
            process.stdin.flush()
        else:
            process.terminate()
        if mode == "wake":
            lines.append(reader.line())
            self.assertEqual(lines[-1], "RESTORE_WAIT")
            os.kill(process.pid, signal.SIGTERM)
            os.kill(process.pid, signal.SIGTERM)
            process.stdin.write(b"restore\n")  # Restore cannot finish before these signals.
            process.stdin.flush()

    def _assert_signal_exit(self, mode, process, reader, lines, started_signal, socket_path):
        return_code = process.wait(timeout=1.5)
        elapsed = time.monotonic() - started_signal
        lines.extend(reader.rest().splitlines())
        stderr = process.stderr.read().decode()
        # Only the explicit unit exception is caught by its test harness.
        self.assertEqual(return_code, 0 if mode == "interrupt" else -signal.SIGINT,
                         (lines, stderr))
        self.assertLess(elapsed, 2.0)
        server_close = next(line for line in lines if line.startswith("SERVER_CLOSE"))
        self.assertIn("restored=True", server_close)
        self.assertIn("socket=True", server_close)
        self.assertIn("calls=" + ("1" if mode in ("wake", "interrupt") else "0"), server_close)
        before = next(line for line in lines if line.startswith("BEFORE_CLOSE"))
        self.assertIn("restored=True", before)
        self.assertIn("socket=False", before)
        self.assertLess(lines.index(server_close), lines.index(before))
        self.assertFalse(pathlib.Path(socket_path).exists())
        if mode in ("wake", "interrupt"):
            self.assertLess(lines.index("RESTORED"), lines.index(server_close))
        if mode == "interrupt":
            self.assertIn("INTERRUPT_PROPAGATED", lines)
        return elapsed

    def _signal_case(self, mode: str, *, repo_root=REPO_ROOT, concurrent_reads=True):
        tmp = tempfile.TemporaryDirectory(prefix="o2-signal-", dir=TMP_BASE)
        process, read_fd, socket_path = start_signal_child(tmp.name, mode, repo_root)
        reader = HandshakeReader(read_fd)
        lines = []
        clients = []
        try:
            self._prepare_signal_case(
                mode, socket_path, reader, lines, clients, concurrent_reads
            )
            started_signal = time.monotonic()
            self._send_signal(mode, process, reader, lines)
            return self._assert_signal_exit(
                mode, process, reader, lines, started_signal, socket_path
            )
        finally:
            for sock in clients:
                sock.close()
            if process.poll() is None:
                process.kill()
                process.wait(1.0)
            process.stdin.close()
            process.stderr.close()
            os.close(read_fd)
            tmp.cleanup()

    def test_keyboard_interrupt_on_main_unwinds_serve_unix_and_cancels_queued_post(self):
        # The child is a real main thread; a bounded parent prevents a regression
        # from hanging pytest itself. Unlike the SIGTERM cases, this unit catches
        # its explicitly raised KeyboardInterrupt only after serve_unix unwinds.
        self._signal_case("interrupt")

    def test_idle_main_thread_termination_unlinks_before_broker_close(self):
        self._signal_case("idle")

    def test_real_wake_once_restores_under_repeated_term_before_server_close(self):
        # The child patches only can_wake's ownership/session seams, so the real
        # wake_once termination guard and exact serve_unix finally are exercised
        # without opening a CAN socket. Parent acknowledgement keeps restoration
        # inside the shield until both repeated TERM signals have been sent.
        self._signal_case("wake")

    def test_blocked_read_without_broker_lock_does_not_delay_signal_exit(self):
        self._signal_case("read")


if __name__ == "__main__":
    unittest.main()
