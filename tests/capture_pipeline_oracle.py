#!/usr/bin/env python3
"""Offline differential capture oracle; never starts a CAN tool or opens sockets.

Run with the prepared Python environment and real zstd installed::

    python tests/capture_pipeline_oracle.py OUTPUT.json

This exercises the actual streaming/compression/finalization code. Only candump,
clock, finalizer scheduling, interface/route/lock/storage probes are faked. The
three-bus tool writes raw files, not zstd; each independent role worker is run
through rotation and session completion. The broker recorder exercises its
supported primary-only interval (both secondaries unproven), including actual
secondary supervisor threads and their shutdown. Separate cases exercise child
exit and DROPCOUNT failures. This is not a substitute for the existing concurrent
three-role and safety-gate tests.
"""
from __future__ import annotations

import argparse
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from projects.ecu_mapping import bcan_drive_recorder as bcan
from projects.vehicle_data import drive_recorder as drive
from projects.vehicle_data.can_interfaces import PassiveInterfaceLease
from tests.test_vehicle_drive_recorder import ready_status
from tools import ignition_triggered_passive_capture as ignition
from tools import passive_drive_capture as passive
from tools import three_bus_capture as three

WALL = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
UTC = WALL.isoformat()
ZSTD = shutil.which("zstd")
REAL_POPEN = subprocess.Popen
REAL_RUN = subprocess.run
REAL_READ = os.read
REAL_THREAD_START = threading.Thread.start
FREE = 100 * 1024**3


def forbid_hardware(event, args):
    if event == "subprocess.Popen" and Path(args[0]).name != "zstd":
        raise AssertionError(f"unexpected executable: {args[0]}")
    if event == "socket.__new__":
        raise AssertionError("no sockets allowed in capture oracle")


def frame_batch(channel, number):
    return b"".join(
        f"(170000000{number}.123456) {channel} {can_id}#{payload}\n".encode()
        for can_id, payload in (
            ("101", "0102030405060708"),
            ("18DA10F1", "032201A100000000"),
            ("18DA40F1", "032201A100000000"),
            ("46C", "0100000000000000"),
            ("0DA", "0100000000000000"),
            ("2EF", "0100000000000000"),
        )
    )


class InlineFinalizer:
    """Remove only background compression scheduling, not compression itself."""

    def __init__(self, **kwargs):
        pass

    def submit(self, fn, *args):
        result = Future()
        try:
            result.set_result(fn(*args))
        except BaseException as exc:
            result.set_exception(exc)
        return result

    def shutdown(self, **kwargs):
        pass


class PipeChild:
    def __init__(self, environment, command):
        self.environment = environment
        self.command = command
        self.channel = command[-1]
        reader, self.writer = os.pipe()
        self.stdout = os.fdopen(reader, "rb", buffering=0)
        self.returncode = None
        self.batches = 0
        self.tail_written = False

    def poll(self):
        return self.returncode

    def emit(self):
        self.batches += 1
        data = frame_batch(self.channel, self.batches)
        if self.environment.case == "drops" and self.batches == 3:
            data += b"DROPCOUNT: dropped 2 CAN frames on 'can7' (total drops 2)\n"
        os.write(self.writer, data)
        if self.environment.case == "child_exit" and self.batches == 3:
            self.returncode = 7
        self.environment.calls.append(["candump.emit", self.channel, self.batches])

    def send_signal(self, signum):
        self.environment.calls.append(["candump.signal", self.channel, int(signum)])
        self.returncode = -int(signum)
        if not self.tail_written:
            os.write(self.writer, f"(1700000010.000001) {self.channel} 101#CAFE".encode())
            self.tail_written = True

    def terminate(self):
        self.environment.calls.append(["candump.terminate", self.channel])
        self.returncode = -signal.SIGTERM

    def kill(self):
        self.environment.calls.append(["candump.kill", self.channel])
        self.returncode = -signal.SIGKILL

    def wait(self, timeout=None):
        self.environment.calls.append(["candump.wait", self.channel, timeout])
        return self.returncode

    def close(self):
        self.stdout.close()
        os.close(self.writer)


class ScriptedSelector:
    def __init__(self, environment):
        self.environment = environment
        self.child = None

    def register(self, fileobj, events):
        self.child = next(p for p in self.environment.children if p.stdout is fileobj)
        self.environment.calls.append(["selector.register", self.child.channel, events])

    def select(self, timeout=None):
        self.environment.now += 6.0
        self.environment.calls.append(["selector.select", self.child.channel, timeout])
        self.child.emit()
        return [(SimpleNamespace(fd=self.child.stdout.fileno()), selectors.EVENT_READ)]

    def close(self):
        self.environment.calls.append(["selector.close"])


class Environment:
    def __init__(self, root, case):
        self.root = root
        self.case = case
        self.calls = []
        self.now = 0.0
        self.children = []

    def disk_free(self, path):
        self.calls.append(["disk_free", str(path)])
        return FREE

    def mount(self, output, mount, **kwargs):
        self.calls.append(["mount", str(output), str(mount), kwargs])
        return 123

    def rmem(self):
        self.calls.append(["rmem_max"])
        return passive.RECEIVE_BUFFER

    def state(self, channel="can7", bitrate=500000, listen_only=True):
        self.calls.append(["interface", channel, bitrate, listen_only])
        return passive.InterfaceState(True, bitrate, listen_only, "ERROR-ACTIVE", 0, 0)

    def run(self, command, **kwargs):
        self.calls.append(["run", command, kwargs])
        if Path(command[0]).name != "zstd":
            raise AssertionError(f"unexpected run: {command}")
        return REAL_RUN(command, **kwargs)

    def popen(self, command, **kwargs):
        recorded = {
            key: getattr(value, "name", value)
            for key, value in kwargs.items()
        }
        self.calls.append(["popen", command, recorded])
        if command[0] == "/oracle/candump":
            child = PipeChild(self, command)
            self.children.append(child)
            kwargs["stderr"].write(b"oracle candump stderr\n")
            return child
        if Path(command[0]).name != "zstd":
            raise AssertionError(f"unexpected Popen: {command}")
        return REAL_POPEN(command, **kwargs)

    @contextmanager
    def lock(self, path):
        self.calls.append(["campaign_lock.enter", str(path)])
        try:
            yield
        finally:
            self.calls.append(["campaign_lock.exit", str(path)])

    @contextmanager
    def installed(self):
        original_recorder = passive.Recorder

        def recorder(*args, **kwargs):
            kwargs.update(popen=self.popen, runner=self.run, disk_free=self.disk_free)
            return original_recorder(*args, **kwargs)

        with ExitStack() as stack:
            for module in (passive, bcan, drive, ignition):
                stack.enter_context(mock.patch.object(module, "utc_now", lambda: UTC))
            stack.enter_context(mock.patch.object(passive, "Recorder", recorder))
            stack.enter_context(mock.patch.object(passive, "require_writable_mount", self.mount))
            stack.enter_context(mock.patch.object(passive, "read_rmem_max", self.rmem))
            stack.enter_context(mock.patch.object(passive, "campaign_file_lock", self.lock))
            stack.enter_context(mock.patch.object(passive.time, "monotonic", lambda: self.now))
            stack.enter_context(mock.patch.object(passive.selectors, "DefaultSelector", lambda: ScriptedSelector(self)))
            stack.enter_context(mock.patch.object(passive.concurrent.futures, "ThreadPoolExecutor", InlineFinalizer))
            stack.enter_context(mock.patch.object(passive.shutil, "which", lambda name: "/oracle/candump" if name == "candump" else ZSTD))
            stack.enter_context(mock.patch.object(passive.signal, "signal", self.signal))
            try:
                yield
            finally:
                for child in self.children:
                    child.close()

    def signal(self, signum, handler):
        self.calls.append(["signal.handler", int(signum), "restore" if handler == "old-handler" else "install"])
        return "old-handler"


def run_passive(env):
    route = SimpleNamespace(role="c-can", channel="can7", bitrate=500000,
                            pair="6/14", topology_fingerprint="oracle-topology")

    def acquire(role):
        env.calls.append(["route.acquire", role])
        return SimpleNamespace(
            route=route,
            revalidate=lambda: env.calls.append(["route.revalidate"]),
            release=lambda: env.calls.append(["route.release"]),
        )

    def preflight(output, policy, *, channel, bitrate):
        env.calls.append(["preflight", str(output), channel, bitrate])
        return env.state(channel, bitrate), env.disk_free(output)

    with (
        mock.patch.object(passive.can_runtime_route, "acquire_passive_bus_route", acquire),
        mock.patch.object(passive, "preflight", preflight),
        mock.patch.object(passive, "runtime_safety_check", lambda **kw: env.state(kw["channel"], kw["bitrate"])),
    ):
        return passive.main([
            "--out-root", str(env.root / "out"), "--require-mount", str(env.root),
            "--campaign", "oracle-drive", "--execute", "--confirm-passive",
            "--conditions", "oracle conditions", "--rotation-seconds", "10",
            "--duration-seconds", "22",
        ])


def run_ignition(env):
    args = ignition.build_parser().parse_args([
        "--out-root", str(env.root / "out"), "--require-mount", str(env.root),
        "--state-path", str(env.root / "state.json"), "--duration-seconds", "1300",
        "--conditions", "oracle conditions",
    ])
    policy = ignition.validate_args(args)
    route = SimpleNamespace(role="c-can", channel="can7", bitrate=500000,
                            pair="6/14", topology_fingerprint="oracle-topology")

    def acquire(role, **kwargs):
        env.calls.append(["route.acquire", role, kwargs])
        return SimpleNamespace(
            route=route,
            revalidate=lambda: env.calls.append(["route.revalidate"]),
            release=lambda: env.calls.append(["route.release"]),
        )

    def child(command, **kwargs):
        if command[0] != sys.executable:
            return REAL_RUN(command, **kwargs)
        env.calls.append(["passive_child", command, kwargs])
        # The Python child boundary is faked, but its actual entry point runs
        # with the unchanged argv (default 600-second rotation).
        return SimpleNamespace(returncode=passive.main(command[2:]))

    with (
        mock.patch.object(ignition, "campaign_id", lambda: "oracle-ignition"),
        mock.patch.object(ignition, "wait_for_ignition", lambda **kw: env.calls.append(["ignition.wait", kw])),
        mock.patch.object(passive.can_runtime_route, "acquire_passive_bus_route", acquire),
        mock.patch.object(passive, "preflight", lambda path, policy, **kw: (env.state(kw["channel"], kw["bitrate"]), env.disk_free(path))),
        mock.patch.object(passive, "runtime_safety_check", lambda **kw: env.state(kw["channel"], kw["bitrate"])),
        mock.patch.object(ignition.subprocess, "run", child),
    ):
        return ignition.execute(args, policy)


def run_bcan(env):
    args = bcan.build_parser().parse_args([
        "--out-root", str(env.root / "out"), "--require-mount", str(env.root),
        "--state-path", str(env.root / "state.json"), "--rotation-seconds", "10",
        "--duration-seconds", "22",
    ])
    policy = bcan.validate_args(args)
    handle = SimpleNamespace(closed=False, _diagnostic_lock_held=True,
                             _diagnostic_lock_channel="can8", _diagnostic_lock_mode="observer")
    with (
        mock.patch.object(bcan, "CHANNEL", "can8"),
        mock.patch.object(bcan, "campaign_id", lambda: "oracle-bcan"),
        mock.patch.object(bcan, "query_interface", lambda: env.state("can8", 125000)),
        mock.patch.object(bcan, "validate_dependencies", lambda path, policy: env.disk_free(path)),
    ):
        env.calls.append(["observer.enter", "can8"])
        try:
            bcan.record_one_interval(args, policy, frozenset({0x46C, 0x0A0, 0x3DC}), handle)
        finally:
            env.calls.append(["observer.exit", "can8"])
    return 0


def run_drive(env):
    args = drive.build_parser().parse_args([
        "--out-root", str(env.root / "out"), "--require-mount", str(env.root),
        "--state-path", str(env.root / "state.json"), "--rotation-seconds", "10",
        "--duration-seconds", "22",
    ])
    policy = drive.validate_args(args)
    status = ready_status()
    # Deliberately exercise the documented degraded, primary-only session.
    roles = status["interface"]["role_interfaces"]["roles"]
    for role in drive.SECONDARY_ROLES:
        roles.pop(role, None)
    ready = {role: threading.Event() for role in drive.SECONDARY_ROLES}
    original_state = drive.SecondaryRoleSupervisor._set_state

    def set_state(supervisor, state):
        original_state(supervisor, state)
        if state == "awaiting_route":
            ready[supervisor.role].set()

    def start(thread):
        REAL_THREAD_START(thread)
        role = thread.name.removeprefix("broker-drive-recorder-")
        if role in ready and not ready[role].wait(10):
            raise AssertionError(f"secondary supervisor did not settle: {role}")

    def dependencies(path, policy, check):
        return check(), env.disk_free(path)

    def initial_check(*args, **kwargs):
        return lambda: env.state(kwargs["channel"], 500000, False)

    with (
        mock.patch.object(drive, "campaign_id", lambda: "oracle-broker"),
        mock.patch.object(drive, "InitialArmedSafetyCheck", initial_check),
        mock.patch.object(drive, "validate_dependencies", dependencies),
        mock.patch.object(drive, "read_broker_status", lambda client: status),
        mock.patch.object(drive, "SECONDARY_READMIT_INTERVAL_SECONDS", 3600),
        mock.patch.object(drive.SecondaryRoleSupervisor, "_set_state", set_state),
        mock.patch.object(threading.Thread, "start", start),
    ):
        drive.record_one_interval(args, policy, status, object(), interface_manager=object())
    return 0


def run_three(env):
    paths = three.create_session(env.root / "out", wall_clock=lambda: WALL)
    config = three.CaptureConfig(capture_root=paths.root, chunk_seconds=1,
                                 max_session_seconds=2, retry_seconds=1,
                                 min_free_bytes=0, candump="/oracle/candump")
    events = three.EventLog(paths.events, wall_clock=lambda: WALL)
    for role, channel, bitrate, pair in (
        ("c-can", "can7", 500000, "6/14"),
        ("b-can", "can8", 125000, "3/11"),
        ("can-ch", "can9", 500000, "12/13"),
    ):
        env.now = 0.0
        lease = PassiveInterfaceLease(role, channel, "oracle-serial", 0, bitrate, pair, "oracle-topology")
        stop = three.StopController()
        # Retry sleeps advance the fake clock; production decision logic remains.
        stop.wait = lambda seconds: setattr(env, "now", env.now + seconds) or False

        @contextmanager
        def observe(requested):
            env.calls.append(["observe.enter", requested])
            try:
                yield lease
            finally:
                env.calls.append(["observe.exit", requested])

        class Child:
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                env.calls.append(["candump.wait", role, timeout])
                if self.returncode is not None:
                    return self.returncode
                if env.case == "child_exit":
                    self.returncode = 7
                    return 7
                env.now += timeout
                raise subprocess.TimeoutExpired("/oracle/candump", timeout)

            def send_signal(self, signum):
                env.calls.append(["candump.signal", role, int(signum)])
                self.returncode = -int(signum)

        def popen(command, **kwargs):
            env.calls.append(["popen", command, {k: getattr(v, "name", v) for k, v in kwargs.items()}])
            kwargs["stdout"].write(frame_batch(channel, 1))
            kwargs["stdout"].write(b"DROPCOUNT: dropped 2 CAN frames (total drops 2)\n")
            kwargs["stderr"].write(b"Dropped 2 CAN frames\n")
            return Child()

        three.run_role_worker(
            role, config=config, paths=paths,
            manager=SimpleNamespace(observe=observe), stop=stop,
            stats=three.RoleWorkerStats(role), session_start=0.0,
            event_log=events, process_factory=popen,
            disk_usage=lambda path: SimpleNamespace(free=env.disk_free(path)),
            monotonic=lambda: env.now, wall_clock=lambda: WALL,
            kernel_rmem_max=passive.RECEIVE_BUFFER,
        )
    return 0


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        entry = {"mode": oct(stat.S_IMODE(path.stat().st_mode))}
        if path.is_dir():
            entry["kind"] = "directory"
        else:
            entry["kind"] = "file"
            raw = path.read_bytes()
            if path.suffix in (".zst", ".partial"):
                entry["compressed_hex"] = raw.hex()
                entry["text"] = REAL_RUN([ZSTD, "-dc", "--", str(path)], check=True,
                                          capture_output=True).stdout.decode()
            else:
                entry["text"] = raw.decode()
        result[key] = entry
    return result


def collect():
    if ZSTD is None:
        raise RuntimeError("real zstd is required")
    sys.addaudithook(forbid_hardware)
    result = {}
    old_umask = os.umask(0o022)
    try:
        for name, runner in (("three", run_three), ("bcan", run_bcan),
                             ("passive", run_passive), ("drive", run_drive),
                             ("ignition", run_ignition)):
            for case in ("success", "child_exit", "drops"):
                with tempfile.TemporaryDirectory(prefix="capture-oracle-") as temporary:
                    root = Path(temporary).resolve()
                    env = Environment(root, case)
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        try:
                            with env.installed():
                                code = runner(env)
                            exception = None
                        except Exception as exc:
                            code = None
                            exception = {"type": type(exc).__name__, "message": str(exc)}
                    entry = {"returncode": code, "exception": exception,
                             "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
                             "calls": env.calls, "files": snapshot(root)}
                    if name == "three" or case == "success":
                        if code != 0 or exception is not None:
                            raise AssertionError(f"{name}.{case} did not complete: {entry}")
                        chunks = [p for p in entry["files"] if p.endswith((".candump", ".zst"))]
                        if len(chunks) < 2:
                            raise AssertionError(f"{name}.{case} did not rotate")
                    elif name == "passive":
                        if code != 2 or exception is not None:
                            raise AssertionError(f"unexpected passive failure: {entry}")
                    elif exception is None or exception["type"] not in ("CaptureError", "ArmError"):
                        raise AssertionError(f"unexpected {name} failure: {entry}")
                    encoded = json.dumps(entry, sort_keys=True)
                    encoded = encoded.replace(str(root), "<RUN>").replace(str(REPO), "<REPO>")
                    encoded = encoded.replace(f"_{os.getpid()}", "_<PID>")
                    result[f"{name}.{case}"] = json.loads(encoded)
    finally:
        os.umask(old_umask)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = collect()
    args.output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    for key, value in data.items():
        chunks = [p for p in value["files"] if p.endswith((".candump", ".zst"))]
        print(f"{key}: returncode={value['returncode']} exception={value['exception']} chunks={len(chunks)}")


if __name__ == "__main__":
    main()
