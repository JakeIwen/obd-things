import contextlib
import io
import json
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from lib.vehicle_can_roles import CAN_BUS_ROLES
from projects.vehicle_data.can_interfaces import (
    PassiveInterfaceLease,
    PassiveInterfaceUnavailable,
)
from tools import three_bus_capture as capture


CHANNELS = {"c-can": "can7", "b-can": "can8", "can-ch": "can9"}
ROLES_BY_CHANNEL = {channel: role for role, channel in CHANNELS.items()}
SERIALS = {
    "c-can": "207C3384413250013",
    "b-can": "207C3384413250013",
    "can-ch": "207E33A4413250013",
}
DEV_IDS = {"c-can": 0, "b-can": 1, "can-ch": 0}
BITRATES = {"c-can": 500_000, "b-can": 125_000, "can-ch": 500_000}
PAIRS = {"c-can": "6/14", "b-can": "3/11", "can-ch": "12/13"}
NOW = datetime(2026, 8, 20, 20, 30, tzinfo=timezone.utc)


def lease(role, *, generation="stable-topology", channel=None):
    return PassiveInterfaceLease(
        role=role,
        channel=channel or CHANNELS[role],
        usb_serial=SERIALS[role],
        dev_id=DEV_IDS[role],
        bitrate=BITRATES[role],
        pair=PAIRS[role],
        topology_generation=generation,
    )


class FakeManager:
    def __init__(self, lease_factory=lease, *, unavailable=()):
        self.lease_factory = lease_factory
        self.unavailable = set(unavailable)
        self.events = []
        self.calls = {role: 0 for role in CAN_BUS_ROLES}
        self.attempted = {role: threading.Event() for role in CAN_BUS_ROLES}
        self._active = {role: 0 for role in CAN_BUS_ROLES}
        self._lock = threading.Lock()

    @contextmanager
    def observe(self, role):
        with self._lock:
            self.calls[role] += 1
            call = self.calls[role]
            self.attempted[role].set()
        if role in self.unavailable:
            raise PassiveInterfaceUnavailable(
                role, "interface_down", f"{role} is down"
            )
        current = self.lease_factory(role, call)
        with self._lock:
            self.events.append(("enter", role, current.channel, call))
            self._active[role] += 1
        try:
            yield current
        finally:
            with self._lock:
                self._active[role] -= 1
                self.events.append(("exit", role, current.channel, call))

    def active_count(self, role):
        with self._lock:
            return self._active[role]


def stable_lease(role, _call):
    return lease(role)


class ImmediateProcess:
    def __init__(self, returncode=0):
        self.returncode = None
        self.planned_returncode = returncode
        self.signals = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self.planned_returncode
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)
        self.returncode = -signum

    def terminate(self):
        self.terminated = True
        self.returncode = -signal.SIGTERM

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL


class BlockingProcess(ImmediateProcess):
    def __init__(self, *, on_wait=None):
        super().__init__(0)
        self.on_wait = on_wait
        self.done = threading.Event()

    def wait(self, timeout=None):
        if self.on_wait is not None:
            callback, self.on_wait = self.on_wait, None
            callback()
        bounded = min(float(timeout or 2.0), 2.0)
        if not self.done.wait(bounded):
            raise subprocess.TimeoutExpired("candump", timeout)
        return self.returncode

    def send_signal(self, signum):
        super().send_signal(signum)
        self.done.set()

    def terminate(self):
        super().terminate()
        self.done.set()

    def kill(self):
        super().kill()
        self.done.set()


class StubbornUntilTerminateProcess(ImmediateProcess):
    def wait(self, timeout=None):
        if not self.terminated and not self.killed:
            raise subprocess.TimeoutExpired("candump", timeout)
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)


class GateExitProcess(ImmediateProcess):
    def __init__(self, gates, returncode=7):
        super().__init__(returncode)
        self.gates = tuple(gates)

    def wait(self, timeout=None):
        for gate in self.gates:
            if not gate.wait(2.0):
                raise AssertionError("healthy role child did not start")
        return super().wait(timeout)


class GateRotateProcess(ImmediateProcess):
    def __init__(self, gates):
        super().__init__(0)
        self.gates = tuple(gates)
        self.first_wait = True

    def wait(self, timeout=None):
        if self.first_wait:
            self.first_wait = False
            for gate in self.gates:
                if not gate.wait(2.0):
                    raise AssertionError("healthy role child did not start")
            raise subprocess.TimeoutExpired("candump", timeout)
        return super().wait(timeout)


class LeaseTests(unittest.TestCase):
    def test_exact_role_is_revalidated_while_outer_lease_remains_held(self):
        manager = FakeManager(stable_lease)
        with manager.observe("b-can") as outer:
            self.assertEqual(manager.active_count("b-can"), 1)
            capture.revalidate_exact_lease(manager, outer)
            self.assertEqual(manager.active_count("b-can"), 1)

        self.assertEqual(manager.calls["b-can"], 2)
        self.assertEqual(manager.active_count("b-can"), 0)
        self.assertEqual(manager.calls["c-can"], 0)
        self.assertEqual(manager.calls["can-ch"], 0)

    def test_role_change_during_revalidation_fails_only_that_lease(self):
        def changing(role, call):
            channel = CHANNELS[role] if call == 1 else "can70"
            return lease(
                role,
                channel=channel,
                generation=f"generation-{call}",
            )

        manager = FakeManager(changing)
        with manager.observe("c-can") as outer:
            with self.assertRaisesRegex(capture.CaptureError, "c-can changed"):
                capture.revalidate_exact_lease(manager, outer)
        self.assertEqual(manager.calls["c-can"], 2)
        self.assertEqual(manager.calls["b-can"], 0)

    def test_unrelated_topology_generation_churn_keeps_role_lease_valid(self):
        def unrelated_churn(role, call):
            return lease(role, generation=f"whole-topology-{call}")

        manager = FakeManager(unrelated_churn)
        with manager.observe("b-can") as outer:
            capture.revalidate_exact_lease(manager, outer)

        self.assertEqual(manager.calls["b-can"], 2)
        self.assertEqual(manager.calls["c-can"], 0)
        self.assertEqual(manager.calls["can-ch"], 0)


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = capture.create_session(self.root, wall_clock=lambda: NOW)
        self.events = capture.EventLog(
            self.paths.events,
            wall_clock=lambda: NOW,
            stderr=io.StringIO(),
        )
        self.config = capture.CaptureConfig(
            capture_root=self.root,
            chunk_seconds=60,
            retry_seconds=1,
            max_session_seconds=120,
            min_free_bytes=0,
            ignition_state="engine-running",
            wake_condition="vehicle already awake before observer started",
            conditions="three permanent DLC taps; all termination jumpers removed",
            candump="/usr/bin/candump",
        )

    def test_command_contains_exactly_one_resolved_interface(self):
        self.assertEqual(
            capture.candump_command(
                "/usr/bin/candump",
                lease("b-can"),
                receive_buffer_bytes=1_048_576,
            ),
            [
                "/usr/bin/candump",
                "-L",
                "-d",
                "-r",
                "1048576",
                "can8",
            ],
        )

    def test_role_chunk_has_unambiguous_route_and_loss_metadata(self):
        calls = []

        def factory(command, **kwargs):
            calls.append((command, kwargs))
            kwargs["stdout"].write(b"(1.000000) can7 123#0102\n")
            kwargs["stdout"].flush()
            kwargs["stderr"].write(
                b"DROPCOUNT: dropped 3 CAN frames on 'can7' socket (total drops 3)\n"
                b"read: Network is down\n"
            )
            kwargs["stderr"].flush()
            return ImmediateProcess(7)

        outcome = capture.run_role_chunk(
            config=self.config,
            paths=self.paths,
            chunk_number=1,
            lease=lease("c-can"),
            duration_seconds=60,
            stop=capture.StopController(),
            event_log=self.events,
            process_factory=factory,
            wall_clock=lambda: NOW,
            kernel_rmem_max=4_194_304,
        )

        self.assertEqual(outcome.role, "c-can")
        self.assertEqual(outcome.reason, "process-exit")
        self.assertEqual(outcome.returncode, 7)
        self.assertEqual(outcome.dropped_frames, 3)
        self.assertTrue(outcome.interface_loss_indicated)
        self.assertEqual(calls[0][0][-1:], ["can7"])
        self.assertNotIn("can8", calls[0][0])
        self.assertNotIn("can9", calls[0][0])
        metadata_path = next(
            (self.paths.session / "c-can").glob("*.metadata.json")
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["schema"], "obd-things.independent-bus-capture.v2")
        self.assertEqual(metadata["role"], "c-can")
        self.assertEqual(metadata["route"]["pair"], "6/14")
        self.assertEqual(metadata["route"]["channel"], "can7")
        self.assertFalse(metadata["configures_interfaces"])
        self.assertFalse(metadata["transmits_can_frames"])
        loss = metadata["loss_reporting"]["summary"]
        self.assertEqual(loss["dropped_frames"], 3)
        self.assertTrue(loss["interface_loss_indicated"])

    def test_role_chunk_timeout_rotates_only_registered_child(self):
        process = GateRotateProcess(())
        stop = capture.StopController()
        moments = iter((0.0, 0.0, 61.0))
        outcome = capture.run_role_chunk(
            config=self.config,
            paths=self.paths,
            chunk_number=1,
            lease=lease("can-ch"),
            duration_seconds=60,
            stop=stop,
            event_log=self.events,
            process_factory=lambda *_args, **_kwargs: process,
            monotonic=moments.__next__,
            wall_clock=lambda: NOW,
        )

        self.assertEqual(outcome.reason, "chunk-rotate")
        self.assertEqual(process.signals, [signal.SIGINT])
        self.assertFalse(process.terminated)
        self.assertFalse(process.killed)

    def test_global_stop_promptly_escalates_child_that_ignores_sigint(self):
        process = StubbornUntilTerminateProcess()
        stop = capture.StopController()
        stop.request(reason="test-stop")
        outcome = capture.run_role_chunk(
            config=self.config,
            paths=self.paths,
            chunk_number=1,
            lease=lease("b-can"),
            duration_seconds=3600,
            stop=stop,
            event_log=self.events,
            process_factory=lambda *_args, **_kwargs: process,
            monotonic=lambda: 0.0,
            wall_clock=lambda: NOW,
        )

        self.assertEqual(outcome.reason, "stop-requested")
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = capture.CaptureConfig(
            capture_root=self.root,
            chunk_seconds=60,
            retry_seconds=1,
            max_session_seconds=120,
            min_free_bytes=10,
            candump="/usr/bin/candump",
        )

    def run_supervisor(self, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            return capture.run_supervisor(
                self.config,
                disk_usage=kwargs.pop(
                    "disk_usage", lambda _path: SimpleNamespace(free=1_000_000)
                ),
                wall_clock=lambda: NOW,
                kernel_rmem_max_reader=lambda: 212_992,
                **kwargs,
            )

    def test_unavailable_role_does_not_block_healthy_role_children(self):
        manager = FakeManager(stable_lease, unavailable=("c-can",))
        stop = capture.StopController()
        started = {role: threading.Event() for role in CAN_BUS_ROLES}
        processes = {}

        def maybe_stop():
            self.assertTrue(manager.attempted["c-can"].wait(2.0))
            self.assertTrue(started["can-ch"].wait(2.0))
            stop.request(reason="test-complete")

        def factory(command, **_kwargs):
            role = ROLES_BY_CHANNEL[command[-1]]
            started[role].set()
            process = BlockingProcess(on_wait=maybe_stop if role == "b-can" else None)
            processes[role] = process
            return process

        result = self.run_supervisor(
            manager=manager,
            stop=stop,
            process_factory=factory,
        )

        self.assertEqual(result, 0)
        self.assertFalse(started["c-can"].is_set())
        self.assertTrue(started["b-can"].is_set())
        self.assertTrue(started["can-ch"].is_set())
        self.assertGreaterEqual(manager.calls["c-can"], 1)
        self.assertEqual(manager.calls["b-can"], 2)
        self.assertEqual(manager.calls["can-ch"], 2)
        self.assertIn(signal.SIGINT, processes["b-can"].signals)
        self.assertIn(signal.SIGINT, processes["can-ch"].signals)
        session = next(self.root.glob("session_*"))
        self.assertEqual(list((session / "c-can").glob("*.candump")), [])
        self.assertEqual(len(list((session / "b-can").glob("*.candump"))), 1)
        self.assertEqual(len(list((session / "can-ch").glob("*.candump"))), 1)

    def test_one_role_child_exit_retries_without_restarting_healthy_children(self):
        manager = FakeManager(stable_lease)
        stop = capture.StopController()
        started = {role: threading.Event() for role in CAN_BUS_ROLES}
        counts = {role: 0 for role in CAN_BUS_ROLES}
        commands = {role: [] for role in CAN_BUS_ROLES}
        lock = threading.Lock()

        def factory(command, **_kwargs):
            role = ROLES_BY_CHANNEL[command[-1]]
            with lock:
                counts[role] += 1
                number = counts[role]
                commands[role].append(list(command))
            started[role].set()
            if role == "c-can" and number == 1:
                return GateExitProcess(
                    (started["b-can"], started["can-ch"]), returncode=5
                )
            if role == "c-can" and number == 2:
                return BlockingProcess(
                    on_wait=lambda: stop.request(reason="test-complete")
                )
            return BlockingProcess()

        result = self.run_supervisor(
            manager=manager,
            stop=stop,
            process_factory=factory,
        )

        self.assertEqual(result, 0)
        self.assertEqual(counts, {"c-can": 2, "b-can": 1, "can-ch": 1})
        for role in CAN_BUS_ROLES:
            self.assertTrue(
                all(command[-1] == CHANNELS[role] for command in commands[role])
            )
            self.assertTrue(
                all(
                    other not in command
                    for command in commands[role]
                    for other_role, other in CHANNELS.items()
                    if other_role != role
                )
            )
        session = next(self.root.glob("session_*"))
        self.assertEqual(len(list((session / "c-can").glob("*.candump"))), 2)
        self.assertEqual(len(list((session / "b-can").glob("*.candump"))), 1)
        self.assertEqual(len(list((session / "can-ch").glob("*.candump"))), 1)
        summary = json.loads((session / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["mode"], "three_independent_receive_only_workers")
        self.assertEqual(summary["role_stats"]["c-can"]["chunks_started"], 2)
        self.assertEqual(summary["role_stats"]["b-can"]["chunks_started"], 1)

    def test_low_disk_from_one_worker_interrupts_all_healthy_children(self):
        manager = FakeManager(stable_lease)
        stop = capture.StopController()
        started = {role: threading.Event() for role in CAN_BUS_ROLES}
        processes = {}
        ccan_disk_checks = 0
        disk_lock = threading.Lock()

        def disk_usage(_path):
            nonlocal ccan_disk_checks
            if threading.current_thread().name.endswith("c-can"):
                with disk_lock:
                    ccan_disk_checks += 1
                    if ccan_disk_checks >= 2:
                        return SimpleNamespace(free=0)
            return SimpleNamespace(free=1_000_000)

        def factory(command, **_kwargs):
            role = ROLES_BY_CHANNEL[command[-1]]
            started[role].set()
            if role == "c-can":
                process = GateRotateProcess(
                    (started["b-can"], started["can-ch"])
                )
            else:
                process = BlockingProcess()
            processes[role] = process
            return process

        result = self.run_supervisor(
            manager=manager,
            stop=stop,
            process_factory=factory,
            disk_usage=disk_usage,
        )

        self.assertEqual(result, 0)
        self.assertEqual(stop.reason, "low-disk")
        self.assertIn(signal.SIGINT, processes["b-can"].signals)
        self.assertIn(signal.SIGINT, processes["can-ch"].signals)

    def test_global_session_bound_interrupts_all_role_children(self):
        manager = FakeManager(stable_lease)
        stop = capture.StopController()
        started = {role: threading.Event() for role in CAN_BUS_ROLES}
        processes = {}

        def monotonic():
            if all(event.is_set() for event in started.values()):
                return 10_000.0
            return 0.0

        def factory(command, **_kwargs):
            role = ROLES_BY_CHANNEL[command[-1]]
            process = BlockingProcess()
            processes[role] = process
            started[role].set()
            return process

        result = self.run_supervisor(
            manager=manager,
            stop=stop,
            process_factory=factory,
            monotonic=monotonic,
        )

        self.assertEqual(result, 0)
        self.assertEqual(stop.reason, "max-session")
        self.assertEqual(set(processes), set(CAN_BUS_ROLES))
        for process in processes.values():
            self.assertIn(signal.SIGINT, process.signals)

    def test_unexpected_all_worker_exit_is_nonzero(self):
        def vanish(**kwargs):
            kwargs["stats"].finished = True

        stop = capture.StopController()
        with mock.patch.object(capture, "run_role_worker_guarded", vanish):
            result = self.run_supervisor(
                manager=FakeManager(stable_lease),
                stop=stop,
                process_factory=lambda *_args, **_kwargs: ImmediateProcess(),
            )

        self.assertEqual(result, 1)
        self.assertEqual(stop.reason, "workers-finished")


class ConcurrencyAndHelperTests(unittest.TestCase):
    def test_signal_stop_interrupts_every_registered_role_child(self):
        stop = capture.StopController()
        processes = {role: BlockingProcess() for role in CAN_BUS_ROLES}
        for role, process in processes.items():
            stop.register_child(role, process)

        stop.request(signal.SIGTERM, reason="signal")

        self.assertTrue(stop.requested)
        self.assertEqual(stop.signal_number, signal.SIGTERM)
        for process in processes.values():
            self.assertEqual(process.signals, [signal.SIGINT])

    def test_event_log_serializes_concurrent_json_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.touch()
            event_log = capture.EventLog(
                path,
                wall_clock=lambda: NOW,
                stderr=io.StringIO(),
            )
            threads = [
                threading.Thread(
                    target=event_log.write,
                    kwargs={"event": "fixture", "worker": index},
                )
                for index in range(20)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 20)
            self.assertEqual({row["worker"] for row in rows}, set(range(20)))

    def test_check_reports_healthy_roles_even_when_one_is_unavailable(self):
        manager = FakeManager(stable_lease, unavailable=("c-can",))
        payload = capture.check_roles(manager)

        self.assertFalse(payload["passive_ready"])
        self.assertFalse(payload["roles"]["c-can"]["passive_ready"])
        self.assertTrue(payload["roles"]["b-can"]["passive_ready"])
        self.assertTrue(payload["roles"]["can-ch"]["passive_ready"])
        self.assertEqual(manager.calls["c-can"], 1)
        self.assertEqual(manager.calls["b-can"], 2)
        self.assertEqual(manager.calls["can-ch"], 2)

    def test_receive_buffer_is_bounded_by_current_kernel_max(self):
        self.assertEqual(capture.selected_rcvbuf(16_777_216, 212_992), 212_992)
        self.assertEqual(capture.selected_rcvbuf(1_000, 2_000), 1_000)
        self.assertIsNone(capture.selected_rcvbuf(0, 2_000))
        self.assertIsNone(capture.selected_rcvbuf(1_000, None))

    def test_recorder_has_no_interface_mutation_or_transmit_entry_point(self):
        source = Path(capture.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "ip link",
            "cansend",
            "bring_up_passive",
            "restore_interface_state",
            "restore_passive",
            "sudo",
            "systemctl",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
