"""Startup grant between the drive-helper supervisor and its helpers.

Scripted helper processes only: no CAN, no real helper, no service.  The
supervisor, grant pipe, event rules and broker orchestration are the production
code.  Cancelling before any grant must end clean (restored null); once a grant
was attempted every missing-final path must still latch the restoration inhibit.
"""

import io
import json
import os
import subprocess
import threading
import unittest
from unittest import mock

import projects.vehicle_data.broker as broker_module
from projects.vehicle_data import active_drive, bcan_auxiliary
from projects.vehicle_data import helper_startup_grant as grant_module
from projects.vehicle_data.broker import ActiveDriveSupervisor, AuxiliaryDriveSupervisor
from projects.vehicle_data.radar_service_control import ENABLE_FLAG, HelperCommandPipe
from tests.helper_startup_support import (
    GRANT_FLAG,
    ScriptedHelper,
    StartupRaceCase,
    WAIT_SECONDS,
)

FINAL_RESTORED = {
    "type": "final",
    "state": "idle",
    "reason": "engine_not_running",
    "detail": "RPM gate lost",
    "interface_mode": "listen_only",
    "restored": True,
}
ARMED_STATUS = {
    "type": "status",
    "state": "armed_diagnostic",
    "reason": "running_gate_satisfied",
    "detail": "armed",
    "interface_mode": "armed_diagnostic",
}
CLEAN_SHAPE = {
    "type": "final",
    "state": "idle",
    "reason": "engine_not_running",
    "interface_mode": "unknown",
    "restored": None,
}


def failing_grant_write():
    """Make only the grant byte's write fail, leaving every other os.write alone."""
    real_write = os.write

    def write(fd, data):
        if data == grant_module.GRANT_TOKEN:
            raise BlockingIOError("pipe full")
        return real_write(fd, data)

    return mock.patch.object(grant_module.os, "write", write)


def fd_is_open(fd):
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


class SupervisorCase(StartupRaceCase):
    def setUp(self):
        self.grants = []
        real = grant_module.StartupGrant

        def recording():
            grant = real()
            self.grants.append(grant)
            return grant

        patcher = mock.patch.object(broker_module, "StartupGrant", recording)
        patcher.start()
        self.addCleanup(patcher.stop)

    def supervisor(self, launcher, cls=AuxiliaryDriveSupervisor, events=None, **over):
        options = dict(
            channel="can8",
            expected_usb_serial="serial-b",
            expected_dev_id=1,
            event_handler=(events if events is not None else []).append,
            popen_factory=launcher,
            queue_poll_seconds=0.002,
        )
        options.update(over)
        return cls(**options)

    def start(self, supervisor, launcher, stop_event=None):
        stop_event = stop_event or threading.Event()
        thread, results = self.run_in_thread(supervisor, stop_event)
        return thread, results, stop_event

    def assertClean(self, result):
        for key, value in CLEAN_SHAPE.items():
            self.assertEqual(result[key], value, key)
        self.assertIn("cancelled", result["detail"])

    def assertFailsClosed(self, result, fragment=None):
        self.assertEqual(result["type"], "final")
        self.assertEqual(result["reason"], "restoration_failed")
        self.assertIs(result["restored"], False)
        self.assertEqual(result["interface_mode"], "armed_diagnostic")
        if fragment is not None:
            self.assertIn(fragment, result["detail"])

    def assertGrantClosed(self, supervisor, index=0):
        grant = self.grants[index]
        self.assertIsNone(grant._read_fd)
        self.assertIsNone(grant._write_fd)
        self.assertIsNone(supervisor._launch)


class GrantPipeTests(unittest.TestCase):
    def setUp(self):
        self.grant = grant_module.StartupGrant()
        self.child = os.dup(self.grant.child_fd)
        os.set_blocking(self.child, False)
        self.addCleanup(self.close_child)
        self.addCleanup(self.grant.close)

    def close_child(self):
        try:
            os.close(self.child)
        except OSError:
            pass

    def read(self):
        try:
            return os.read(self.child, 1)
        except BlockingIOError:
            return None

    def test_pipe_ends_are_private_until_passed_and_write_end_never_blocks(self):
        self.assertFalse(os.get_inheritable(self.grant.child_fd))
        self.assertFalse(os.get_blocking(self.grant._write_fd))

    def test_grant_marks_the_attempt_before_the_byte_is_written(self):
        seen = []
        real_write = os.write

        def write(fd, data):
            seen.append(self.grant.grant_attempted)
            return real_write(fd, data)

        with mock.patch.object(grant_module.os, "write", write):
            self.assertTrue(self.grant.try_grant())
        self.assertEqual(seen, [True])
        self.assertEqual(self.read(), grant_module.GRANT_TOKEN)
        self.assertIsNone(self.grant._write_fd)

    def test_cancel_wins_over_a_later_grant_and_the_child_sees_eof(self):
        self.grant.cancel()
        self.assertFalse(self.grant.try_grant())
        self.assertFalse(self.grant.grant_attempted)
        self.assertEqual(self.read(), b"")

    def test_pending_cancel_is_checked_inside_the_grant_lock(self):
        self.assertFalse(self.grant.try_grant(lambda: True))
        self.assertTrue(self.grant.cancelled)
        self.assertFalse(self.grant.grant_attempted)
        self.assertEqual(self.read(), b"")

    def test_cancel_after_the_attempt_cannot_undo_it(self):
        self.assertTrue(self.grant.try_grant())
        self.grant.cancel()
        self.assertTrue(self.grant.grant_attempted)
        self.assertFalse(self.grant.try_grant())

    def test_failed_write_still_counts_as_an_attempt(self):
        with failing_grant_write():
            self.assertTrue(self.grant.try_grant())
        self.assertTrue(self.grant.grant_attempted)
        self.assertIsNone(self.grant._write_fd)
        self.assertEqual(self.read(), b"")

    def test_close_releases_both_ends(self):
        ends = (self.grant._read_fd, self.grant._write_fd)
        self.grant.close()
        self.grant.close()
        self.assertEqual((self.grant._read_fd, self.grant._write_fd), (None, None))
        # The child's duplicate keeps the read side alive; the write side is gone.
        self.assertEqual(self.read(), b"")
        self.assertTrue(all(fd is not None for fd in ends))


class EventRuleTests(unittest.TestCase):
    def test_only_the_exact_ready_event_of_this_child_is_valid(self):
        ready = {"type": "ready", "protocol": 1, "pid": 77}
        self.assertIsNone(grant_module.ready_event_problem(ready, 77))
        bad = [
            {**ready, "extra": 1},
            {"type": "ready", "protocol": 1},
            {**ready, "protocol": 2},
            {**ready, "protocol": True},
            {**ready, "protocol": "1"},
            {**ready, "pid": 78},
            {**ready, "pid": True},
        ]
        for event in bad:
            with self.subTest(event=event):
                self.assertIsNotNone(grant_module.ready_event_problem(event, 77))
        self.assertIsNotNone(grant_module.ready_event_problem(ready, None))

    def test_only_a_no_mutation_final_may_precede_the_grant(self):
        final = {"type": "final", "reason": "helper_failed", "restored": None,
                 "interface_mode": "unknown"}
        self.assertIsNone(grant_module.pregrant_event_problem(final))
        for event in (
            ARMED_STATUS,
            {"type": "observation"},
            FINAL_RESTORED,
            {**final, "restored": False},
            {**final, "interface_mode": "armed_diagnostic"},
        ):
            with self.subTest(event=event):
                self.assertIsNotNone(grant_module.pregrant_event_problem(event))


class CancellationTests(SupervisorCase):
    def test_stop_event_before_launch_spawns_nothing(self):
        launcher = self.launcher()
        supervisor = self.supervisor(launcher)
        stopped = threading.Event()
        stopped.set()
        result = supervisor.run(stopped)

        self.assertClean(result)
        self.assertEqual(launcher.commands, [])
        self.assertEqual(self.grants, [])
        self.assertIsNone(supervisor._launch)

    def test_stop_alone_cancels_an_unready_helper(self):
        launcher = self.launcher()
        supervisor = self.supervisor(launcher)
        thread, results, stop_event = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        supervisor.stop()
        result = self.join(thread, results)

        self.assertFalse(stop_event.is_set())
        self.assertClean(result)
        self.assertEqual(launcher.process.terminate_calls, 1)
        self.assertFalse(self.grants[0].grant_attempted)
        self.assertEqual(launcher.read_grant(), b"")
        self.assertGrantClosed(supervisor)

    def test_stop_event_alone_cancels_an_unready_helper(self):
        launcher = self.launcher()
        supervisor = self.supervisor(launcher)
        thread, results, stop_event = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        stop_event.set()
        result = self.join(thread, results)

        self.assertClean(result)
        self.assertEqual(launcher.read_grant(), b"")
        self.assertGrantClosed(supervisor)

    def test_eof_before_the_child_is_reaped_is_not_a_protocol_error(self):
        class LateReap(ScriptedHelper):
            polls_after_eof = 0

            def poll(self):
                if self.terminated.is_set() and self.returncode is None:
                    self.polls_after_eof += 1
                    if self.polls_after_eof > 40:
                        self.returncode = -15
                return self.returncode

            def terminate(self):
                self.terminate_calls += 1
                self.terminated.set()
                self.stdout.lines.put(None)  # EOF now, exit status only later

        launcher = self.launcher(LateReap())
        supervisor = self.supervisor(launcher)
        thread, results, stop_event = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        supervisor.stop()
        result = self.join(thread, results)

        self.assertClean(result)
        self.assertNotIn("closed stdout", result["detail"])

    def test_cancel_during_popen_is_honoured(self):
        for how in ("stop", "stop_event"):
            with self.subTest(how=how):
                holder = {}
                stop_event = threading.Event()

                def cancel(_launcher):
                    if how == "stop":
                        holder["supervisor"].stop()
                    else:
                        stop_event.set()

                launcher = self.launcher(on_spawn=cancel)
                supervisor = self.supervisor(launcher)
                holder["supervisor"] = supervisor
                result = supervisor.run(stop_event)

                self.assertClean(result)
                self.assertEqual(launcher.process.terminate_calls, 1)
                self.assertEqual(launcher.read_grant(), b"")

    def test_stop_before_the_ready_line_is_read_blocks_the_grant(self):
        process = ScriptedHelper(die_on_terminate=False)
        launcher = self.launcher(process)
        supervisor = self.supervisor(launcher)
        thread, results, stop_event = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        stop_event.set()
        self.assertTrue(process.terminated.wait(WAIT_SECONDS))
        process.ready()  # the helper raced the cancel and announced itself
        process.die(-15)
        result = self.join(thread, results)

        self.assertClean(result)
        self.assertFalse(self.grants[0].grant_attempted)
        self.assertEqual(launcher.read_grant(), b"")

    def test_helper_that_dies_on_its_own_before_ready_is_not_called_clean(self):
        process = ScriptedHelper()
        launcher = self.launcher(process)
        supervisor = self.supervisor(launcher)
        thread, results, _stop = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        process.die(-11)
        self.assertFailsClosed(self.join(thread, results), "status -11")

    def test_unreaped_cancelled_child_still_fails_closed(self):
        process = ScriptedHelper(die_on_terminate=False)
        launcher = self.launcher(process)
        supervisor = self.supervisor(launcher, shutdown_timeout_seconds=0.05)
        thread, results, _stop = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        supervisor.stop()
        result = self.join(thread, results)
        self.assertFailsClosed(result)

    def test_pre_ready_silence_without_cancel_still_fails_closed(self):
        launcher = self.launcher()
        supervisor = self.supervisor(launcher, event_silence_timeout_seconds=0.05)
        thread, results, _stop = self.start(supervisor, launcher)
        self.assertFailsClosed(self.join(thread, results), "event-silence")


class GrantedLaunchTests(SupervisorCase):
    def test_valid_ready_is_granted_hidden_and_the_final_returned_once(self):
        events = []
        launcher = self.launcher()
        supervisor = self.supervisor(launcher, ActiveDriveSupervisor, events)
        thread, results, _stop = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        launcher.process.ready()
        self.assertEqual(launcher.read_grant(), grant_module.GRANT_TOKEN)
        launcher.process.emit(ARMED_STATUS)
        launcher.process.emit(FINAL_RESTORED)
        launcher.process.die(0)
        result = self.join(thread, results)

        self.assertEqual(result, FINAL_RESTORED)
        self.assertEqual(events, [ARMED_STATUS])
        command, kwargs = launcher.commands[0], launcher.kwargs[0]
        self.assertEqual(command[-3], "--enable-oil-life")
        self.assertEqual(command[-2], GRANT_FLAG)
        self.assertEqual(kwargs["pass_fds"], (int(command[-1]),))
        self.assertEqual(kwargs["cwd"], broker_module.HELPER_CWD)
        self.assertGrantClosed(supervisor)

    def test_parent_read_end_is_closed_once_the_child_has_it(self):
        launcher = self.launcher()
        supervisor = self.supervisor(launcher)
        thread, results, _stop = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        launcher.process.ready()
        self.assertEqual(launcher.read_grant(), grant_module.GRANT_TOKEN)
        self.assertIsNone(self.grants[0]._read_fd)
        launcher.process.emit(FINAL_RESTORED)
        launcher.process.die(0)
        self.join(thread, results)

    def test_radar_stdin_pipe_and_grant_descriptor_coexist(self):
        launcher = self.launcher()
        pipe = HelperCommandPipe(popen_factory=launcher)
        supervisor = self.supervisor(launcher, ActiveDriveSupervisor, popen_factory=pipe.popen)
        thread, results, _stop = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        launcher.process.ready()
        self.assertEqual(launcher.read_grant(), grant_module.GRANT_TOKEN)
        command, kwargs = launcher.commands[0], launcher.kwargs[0]
        self.assertEqual(command[-3:], [GRANT_FLAG, command[-2], ENABLE_FLAG])
        self.assertIs(kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(kwargs["pass_fds"], (int(command[-2]),))
        launcher.process.emit(FINAL_RESTORED)
        launcher.process.die(0)
        self.join(thread, results)

    def test_no_ready_final_that_proves_no_mutation_is_returned_as_is(self):
        final = {"type": "final", "state": "idle", "reason": "helper_failed",
                 "detail": "startup grant refused", "interface_mode": "unknown",
                 "restored": None}
        launcher = self.launcher()
        supervisor = self.supervisor(launcher)
        thread, results, _stop = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        launcher.process.emit(final)
        launcher.process.die(0)
        self.assertEqual(self.join(thread, results), final)

    def test_pipe_creation_failure_never_launches_ungated(self):
        launcher = self.launcher()
        supervisor = self.supervisor(launcher)
        with mock.patch.object(broker_module, "StartupGrant", side_effect=OSError("emfile")):
            result = supervisor.run(threading.Event())
        self.assertEqual(result["reason"], "helper_start_failed")
        self.assertIsNone(result["restored"])
        self.assertEqual(launcher.commands, [])

    def test_failed_popen_closes_both_pipe_ends(self):
        def broken(_command, **_kwargs):
            raise OSError("no such interpreter")

        supervisor = self.supervisor(broken)
        result = supervisor.run(threading.Event())
        self.assertEqual(result["reason"], "helper_start_failed")
        self.assertGrantClosed(supervisor)


class ProtocolViolationTests(SupervisorCase):
    def run_script(self, script, *, granted):
        launcher = self.launcher()
        supervisor = self.supervisor(launcher)
        thread, results, _stop = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        script(launcher.process)
        result = self.join(thread, results)
        self.assertEqual(self.grants[0].grant_attempted, granted)
        self.assertGrantClosed(supervisor)
        return result, launcher

    def test_malformed_ready_events_are_protocol_errors_and_never_granted(self):
        ready = {"type": "ready", "protocol": 1, "pid": 4321}
        malformed = [
            {**ready, "extra": 1},
            {"type": "ready", "protocol": 1},
            {**ready, "protocol": 2},
            {**ready, "protocol": True},
            {**ready, "pid": 1},
        ]
        for event in malformed:
            with self.subTest(event=event):
                self.grants.clear()
                result, launcher = self.run_script(
                    lambda process, event=event: process.emit(event), granted=False
                )
                self.assertFailsClosed(result, "ready event")
                self.assertEqual(launcher.read_grant(), b"")

    def test_second_ready_is_a_protocol_error_after_the_grant(self):
        def script(process):
            process.ready()
            process.ready()

        result, launcher = self.run_script(script, granted=True)
        self.assertFailsClosed(result, "repeated its ready event")
        self.assertEqual(launcher.read_grant(), grant_module.GRANT_TOKEN)

    def test_output_before_ready_is_a_protocol_error(self):
        result, launcher = self.run_script(
            lambda process: process.emit(ARMED_STATUS), granted=False
        )
        self.assertFailsClosed(result, "before its startup grant")
        self.assertEqual(launcher.read_grant(), b"")

    def test_contradictory_final_before_ready_is_a_protocol_error(self):
        def script(process):
            process.emit(FINAL_RESTORED)
            process.die(0)

        result, _launcher = self.run_script(script, granted=False)
        self.assertFailsClosed(result, "before its startup grant")

    def test_non_json_before_ready_is_a_protocol_error(self):
        result, _launcher = self.run_script(
            lambda process: process.stdout.lines.put("not json\n"), granted=False
        )
        self.assertFailsClosed(result, "invalid JSON")


class PostGrantFailClosedTests(SupervisorCase):
    def granted_then_killed(self, *, armed, stop_with="stop"):
        launcher = self.launcher()
        supervisor = self.supervisor(launcher)
        thread, results, stop_event = self.start(supervisor, launcher)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        launcher.process.ready()
        self.assertEqual(launcher.read_grant(), grant_module.GRANT_TOKEN)
        if armed:
            launcher.process.emit(ARMED_STATUS)
        if stop_with == "stop":
            supervisor.stop()
        else:
            stop_event.set()
        return self.join(thread, results)

    def test_killed_after_the_grant_without_a_final_fails_closed(self):
        for armed in (False, True):
            for stop_with in ("stop", "stop_event"):
                with self.subTest(armed=armed, stop_with=stop_with):
                    result = self.granted_then_killed(armed=armed, stop_with=stop_with)
                    self.assertFailsClosed(result, "without a final restoration event")

    def test_ambiguous_grant_write_stays_tainted(self):
        with failing_grant_write():
            launcher = self.launcher()
            supervisor = self.supervisor(launcher)
            thread, results, _stop = self.start(supervisor, launcher)
            self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
            launcher.process.ready()
            self.assertEqual(launcher.read_grant(), b"")
            supervisor.stop()
            result = self.join(thread, results)
        self.assertTrue(self.grants[0].grant_attempted)
        self.assertFailsClosed(result)

    def broker_with_held_auxiliary(self, *, armed):
        launcher = self.launcher()
        holder = {}
        auxiliary = self.supervisor(
            launcher,
            event_handler=lambda event: holder["broker"].handle_auxiliary_drive_event(event),
        )
        proceed = threading.Event()

        class BusyActive:
            def run(self, _stop):
                assert launcher.spawned.wait(WAIT_SECONDS)
                assert proceed.wait(WAIT_SECONDS)
                return {"type": "final", "state": "idle", "reason": "can_busy",
                        "detail": "another operation owns can7",
                        "interface_mode": "unknown", "restored": None}

        broker = self.make_broker(active=BusyActive(), auxiliary=auxiliary)
        holder["broker"] = broker
        broker._passive_engine_evidence = "running"

        def drive_helper():
            self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
            launcher.process.ready()
            self.assertEqual(launcher.read_grant(), grant_module.GRANT_TOKEN)
            if armed:
                launcher.process.emit(ARMED_STATUS)
            proceed.set()

        driver = threading.Thread(target=drive_helper, daemon=True)
        driver.start()
        self.addCleanup(driver.join, WAIT_SECONDS)
        with mock.patch(
            "projects.vehicle_data.broker.can_operation_state.begin_inhibit"
        ) as begin_inhibit:
            broker._run_active_drive_if_ready()
        return broker, begin_inhibit

    def test_broker_cancel_after_the_grant_latches_the_inhibit_and_the_broker(self):
        for armed in (False, True):
            with self.subTest(armed=armed):
                broker, begin_inhibit = self.broker_with_held_auxiliary(armed=armed)
                begin_inhibit.assert_called_once()
                self.assertEqual(begin_inhibit.call_args.kwargs["channel"], "*")
                self.assertTrue(broker._auxiliary_drive_restoration_latched)
                self.assertTrue(
                    broker.status_response()["auxiliary_drive"]["restoration_failed"]
                )


class ChildGateTests(unittest.TestCase):
    class Sink:
        def __init__(self):
            self.events = []

        def emit(self, event_type, **payload):
            self.events.append({"type": event_type, **payload})

    def pipe(self, token=None, *, keep_open=False):
        read_fd, write_fd = os.pipe()
        if token:
            os.write(write_fd, token)
        if keep_open:
            self.addCleanup(os.close, write_fd)
        else:
            os.close(write_fd)
        self.addCleanup(self.close_quietly, read_fd)
        return read_fd

    @staticmethod
    def close_quietly(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def gate(self, fd, parent=1234, ppid=1234):
        return grant_module.ChildStartupGate(fd, parent, getppid=lambda: ppid)

    def test_exact_token_from_the_expected_parent_grants_and_closes_the_fd(self):
        fd = self.pipe(grant_module.GRANT_TOKEN)
        sink = self.Sink()
        self.assertIsNone(self.gate(fd)(sink))
        self.assertEqual(
            sink.events,
            [{"type": "ready", "protocol": 1, "pid": os.getpid()}],
        )
        self.assertFalse(fd_is_open(fd))

    def test_refusals_never_grant(self):
        cases = {
            "eof": (self.pipe(None), 1234),
            "wrong token": (self.pipe(b"X"), 1234),
            "parent changed": (self.pipe(grant_module.GRANT_TOKEN), 1),
        }
        for name, (fd, ppid) in cases.items():
            with self.subTest(name):
                refusal = self.gate(fd, ppid=ppid)(self.Sink())
                self.assertIsInstance(refusal, str)
                self.assertFalse(fd_is_open(fd))

    def test_invalid_descriptors_are_refused_without_a_ready_event(self):
        regular = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(self.close_quietly, regular)
        read_fd, write_fd = os.pipe()
        self.addCleanup(self.close_quietly, read_fd)
        self.addCleanup(self.close_quietly, write_fd)
        closed = os.dup(read_fd)
        os.close(closed)
        for name, fd in (
            ("stdout", 1),
            ("stderr", 2),
            ("closed", closed),
            ("not a pipe", regular),
            ("write end", write_fd),
        ):
            with self.subTest(name):
                sink = self.Sink()
                self.assertIsInstance(self.gate(fd)(sink), str)
                self.assertEqual(sink.events, [])

    def test_a_signal_while_waiting_propagates_and_closes_the_fd(self):
        fd = self.pipe(None, keep_open=True)
        with mock.patch.object(grant_module.os, "read", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.gate(fd)(self.Sink())
        self.assertFalse(fd_is_open(fd))

    def test_no_flag_means_the_legacy_ungated_start(self):
        self.assertIsNone(grant_module.child_gate_from_arguments(None, 99))
        self.assertIsInstance(
            grant_module.child_gate_from_arguments(5, 99), grant_module.ChildStartupGate
        )


class SessionBarrierTests(unittest.TestCase):
    MODULES = (active_drive, bcan_auxiliary)

    class Untouchable:
        channel = "can9"  # read only to name the lock; every other attribute is a touch

        def __init__(self):
            self.touched = []

        def __getattr__(self, name):
            self.touched.append(name)
            raise AttributeError(name)

    class Guard:
        cleanup_started = False

        def begin_cleanup(self):
            self.cleanup_started = True

    def run_session(self, module, gate, order=None):
        backend = self.Untouchable()
        stream = io.StringIO()
        sink = module.JsonEventSink(stream=stream)
        guard = self.Guard()

        def acquire(_channel):
            if order is not None:
                order.append("acquire")
            raise module.diagnostic_safety.ChannelLockError("held elsewhere")

        with mock.patch.object(module.diagnostic_safety, "acquire_channel_lock", acquire):
            outcome = module.run_active_session(
                backend, sink, termination_guard=guard, startup_gate=gate
            )
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        return outcome, events, backend, guard

    def test_refused_gate_ends_with_a_no_mutation_final_before_any_effect(self):
        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                order = []
                outcome, events, backend, guard = self.run_session(
                    module, lambda _sink: "supervisor refused", order
                )
                self.assertEqual(order, [])
                self.assertEqual(backend.touched, [])
                self.assertEqual(outcome.reason, "helper_failed")
                self.assertIsNone(outcome.restored)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["type"], "final")
                self.assertIsNone(events[0]["restored"])
                self.assertEqual(events[0]["interface_mode"], "unknown")
                self.assertTrue(guard.cleanup_started)

    def test_termination_at_the_barrier_is_a_clean_no_mutation_final(self):
        def interrupted(_sink):
            raise KeyboardInterrupt

        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                order = []
                outcome, events, backend, _guard = self.run_session(module, interrupted, order)
                self.assertEqual(order, [])
                self.assertEqual(backend.touched, [])
                self.assertEqual(outcome.reason, "engine_not_running")
                self.assertIsNone(events[-1]["restored"])

    def test_the_barrier_runs_before_the_channel_lock(self):
        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                order = []

                def gate(_sink, order=order):
                    order.append("gate")

                outcome, _events, _backend, _guard = self.run_session(module, gate, order)
                self.assertEqual(order, ["gate", "acquire"])
                self.assertEqual(outcome.reason, "can_busy")

    def test_without_a_gate_the_session_starts_exactly_as_before(self):
        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                order = []
                outcome, _events, _backend, _guard = self.run_session(module, None, order)
                self.assertEqual(order, ["acquire"])
                self.assertEqual(outcome.reason, "can_busy")

    def test_flag_is_optional_and_numeric_on_both_helper_clis(self):
        required = ["--channel", "can9", "--expected-usb-serial", "s",
                    "--expected-dev-id", "0x1", "--expected-parent-pid", "5"]
        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                parser = module.build_parser()
                self.assertIsNone(parser.parse_args(required).startup_grant_fd)
                parsed = parser.parse_args([*required, GRANT_FLAG, "7"])
                self.assertEqual(parsed.startup_grant_fd, 7)
                with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                    parser.parse_args([*required, GRANT_FLAG, "x"])


if __name__ == "__main__":
    unittest.main()
