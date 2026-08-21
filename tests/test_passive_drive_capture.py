import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "passive_drive_capture", REPO / "tools" / "passive_drive_capture.py"
)
capture = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = capture
SPEC.loader.exec_module(capture)


PASSIVE_DETAILS = """\
4: can7: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc fq_codel state UP mode DEFAULT
    link/can
    can <LISTEN-ONLY> state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 0
          bitrate 500000 sample-point 0.875
    RX:  bytes packets errors dropped  missed   mcast
            4096      64      0       0       0       0
"""

TEST_CHANNEL = "can7"
TEST_BITRATE = 500_000


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeStdout:
    def __init__(self, descriptor=91):
        self.descriptor = descriptor

    def fileno(self):
        return self.descriptor


class FakeProcess:
    def __init__(self, *, exit_on_signal=False, ignore_terminate=False, descriptor=91):
        self.stdout = FakeStdout(descriptor)
        self.returncode = None
        self.exit_on_signal = exit_on_signal
        self.ignore_terminate = ignore_terminate
        self.events = []

    def poll(self):
        return self.returncode

    def send_signal(self, signum):
        self.events.append(("signal", signum))
        if self.exit_on_signal:
            self.returncode = 128 + signum

    def terminate(self):
        self.events.append(("terminate", None))
        if not self.ignore_terminate:
            self.returncode = -capture.signal.SIGTERM

    def kill(self):
        self.events.append(("kill", None))
        self.returncode = -capture.signal.SIGKILL

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        return self.returncode


class FakeSelector:
    def __init__(self, descriptor, event_batches, on_select=None):
        self.key = type("SelectorKey", (), {"fd": descriptor})()
        self.event_batches = list(event_batches)
        self.on_select = on_select

    def register(self, _fileobj, _events):
        return self.key

    def select(self, timeout=None):
        if self.on_select is not None:
            self.on_select(timeout)
        if not self.event_batches:
            return []
        ready = self.event_batches.pop(0)
        return [(self.key, capture.selectors.EVENT_READ)] if ready else []

    def close(self):
        pass


class RecordingChunk:
    writes = []

    def __init__(
        self,
        _run_dir,
        sequence,
        _full_enabled,
        _priority_enabled,
        _stderr_handle,
        **_kwargs,
    ):
        self.sequence = sequence
        self.started_monotonic = capture.time.monotonic()
        self.aborted = False

    def write(self, line, _priority_ids):
        self.writes.append(line)

    def finish(self, _verifier):
        return {
            "type": "chunk",
            "sequence": self.sequence,
            "streams": {},
            "complete": True,
        }

    def abort(self):
        self.aborted = True


def interface_state_with_counters(dropped, missed):
    state = capture.InterfaceState(
        up=True,
        bitrate=TEST_BITRATE,
        listen_only=True,
        controller_state="ERROR-ACTIVE",
        rx_dropped=dropped,
    )
    # This keeps the test useful while the production dataclass gains the
    # corresponding parsed field, without replacing the real InterfaceState.
    object.__setattr__(state, "rx_missed", missed)
    return state


class PassiveDriveCaptureTests(unittest.TestCase):
    def test_interface_parser_requires_all_passive_fields(self):
        state = capture.parse_interface_state(
            PASSIVE_DETAILS,
            channel=TEST_CHANNEL,
        )
        self.assertTrue(state.up)
        self.assertTrue(state.listen_only)
        self.assertEqual(state.bitrate, 500000)
        self.assertEqual(state.controller_state, "ERROR-ACTIVE")

        armed = capture.parse_interface_state(
            PASSIVE_DETAILS.replace("<LISTEN-ONLY>", ""),
            channel=TEST_CHANNEL,
        )
        self.assertFalse(armed.listen_only)

        dynamic = capture.parse_interface_state(
            PASSIVE_DETAILS.replace("can7", "can3"),
            channel="can3",
        )
        self.assertTrue(dynamic.up)
        self.assertEqual(dynamic.bitrate, 500000)

    def test_candump_parser_and_priority_selection(self):
        line = b"(1784704278.475609) can0 18DA10F1#0322100000000000\n"
        timestamp, can_id = capture.parse_candump_line(line)
        self.assertAlmostEqual(timestamp, 1784704278.475609)
        self.assertEqual(can_id, 0x18DA10F1)
        self.assertTrue(capture.is_priority_line(line, frozenset({0x18DA10F1})))
        self.assertFalse(capture.is_priority_line(line, frozenset({0x101})))
        self.assertEqual(capture.parse_candump_line(b"not a frame\n"), (None, None))
        self.assertEqual(
            capture.parse_drop_line(
                b"DROPCOUNT: dropped 3 CAN frames on 'can0' socket (total drops 7)\n"
            ),
            (3, 7),
        )

    def test_ccan_correlation_profile_includes_broadcast_and_diagnostic_ids(self):
        parser = capture.build_parser()
        args = parser.parse_args(["--out-root", "/tmp/plan"])
        selected = capture.resolved_priority_ids(args)
        for can_id in (0x0FC, 0x100, 0x101, 0x2ED, 0x412, 0x41B, 0x41D):
            with self.subTest(can_id=f"0x{can_id:X}"):
                self.assertIn(can_id, selected)
        self.assertIn(0x18DA60F1, selected)
        self.assertIn(0x18DAF160, selected)

        args = parser.parse_args(
            [
                "--out-root",
                "/tmp/plan",
                "--priority-profile",
                "none",
                "--priority-id",
                "0x123",
            ]
        )
        self.assertEqual(capture.resolved_priority_ids(args), frozenset({0x123}))

    def test_default_disk_policy_preserves_25_gib(self):
        args = capture.build_parser().parse_args(["--out-root", "/tmp/plan"])
        policy = capture.validate_args(args)
        self.assertEqual(policy.soft_free_bytes, 30 * 1024**3)
        self.assertEqual(policy.hard_free_bytes, 25 * 1024**3)
        plan = capture.plan(args, policy)
        self.assertEqual(plan["interface_requirement"]["logical_role"], "c-can")
        self.assertEqual(
            plan["interface_requirement"]["channel"],
            "resolved at execution by USB serial/dev_id",
        )
        self.assertNotIn("blocked_services", plan)

    def test_disk_policy_has_two_stage_degradation(self):
        policy = capture.DiskPolicy(soft_free_bytes=300, hard_free_bytes=200)
        self.assertEqual(policy.action(301), "full")
        self.assertEqual(policy.action(300), "priority-only")
        self.assertEqual(policy.action(201), "priority-only")
        self.assertEqual(policy.action(200), "stop")
        with self.assertRaises(ValueError):
            capture.DiskPolicy(soft_free_bytes=100, hard_free_bytes=100)

    def test_preflight_accepts_exact_passive_state_and_inactive_services(self):
        commands = []

        def runner(command, **_kwargs):
            commands.append(command)
            if command[:4] == ["ip", "-details", "-statistics", "link"]:
                return Result(stdout=PASSIVE_DETAILS)
            if command[:2] == ["systemctl", "is-active"]:
                return Result(returncode=3, stdout="inactive\n")
            raise AssertionError(command)

        state, free = capture.preflight(
            Path("/unused"),
            capture.DiskPolicy(300, 200),
            channel=TEST_CHANNEL,
            bitrate=TEST_BITRATE,
            runner=runner,
            which=lambda name: f"/usr/bin/{name}",
            disk_free=lambda _path: 1000,
            rmem_max=lambda: capture.RECEIVE_BUFFER,
        )
        self.assertTrue(state.listen_only)
        self.assertEqual(free, 1000)
        self.assertEqual(
            [command[-1] for command in commands if command[0] == "systemctl"],
            [],
        )

    def test_preflight_rejects_armed_interface_without_global_service_query(self):
        def runner(command, **_kwargs):
            if command[0] == "ip":
                return Result(stdout=PASSIVE_DETAILS.replace("<LISTEN-ONLY>", ""))
            raise AssertionError(f"unrelated service query: {command}")

        with self.assertRaisesRegex(capture.CaptureError, "not LISTEN-ONLY"):
            capture.preflight(
                Path("/unused"),
                capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
                runner=runner,
                which=lambda name: f"/usr/bin/{name}",
                disk_free=lambda _path: 1000,
                rmem_max=lambda: capture.RECEIVE_BUFFER,
            )

    def test_preflight_rejects_clipped_receive_buffer(self):
        def runner(command, **_kwargs):
            if command[0] == "ip":
                return Result(stdout=PASSIVE_DETAILS)
            return Result(returncode=3, stdout="inactive\n")

        with self.assertRaisesRegex(capture.CaptureError, "net.core.rmem_max"):
            capture.preflight(
                Path("/unused"),
                capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
                runner=runner,
                which=lambda name: f"/usr/bin/{name}",
                disk_free=lambda _path: 1000,
                rmem_max=lambda: capture.RECEIVE_BUFFER - 1,
            )

    def test_plan_default_has_no_subprocess_or_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "must-not-exist"
            with mock.patch.object(capture, "execute") as execute_mock, mock.patch(
                "subprocess.run", side_effect=AssertionError("subprocess forbidden")
            ), mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess forbidden")):
                status = capture.main(
                    [
                        "--out-root",
                        str(root),
                        "--priority-id",
                        "0x101",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertFalse(root.exists())
            execute_mock.assert_not_called()

    def test_recovery_plan_has_no_subprocess_or_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "must-not-exist"
            with mock.patch.object(capture, "execute_recovery") as execute_mock, mock.patch(
                "subprocess.run", side_effect=AssertionError("subprocess forbidden")
            ), mock.patch(
                "subprocess.Popen", side_effect=AssertionError("subprocess forbidden")
            ), mock.patch(
                "fcntl.flock", side_effect=AssertionError("lock/write forbidden")
            ):
                status = capture.main(
                    [
                        "--out-root",
                        str(root),
                        "--recover-partials",
                        "--campaign",
                        "interrupted-drive",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertFalse(root.exists())
            execute_mock.assert_not_called()

    def test_recovery_execute_requires_exact_campaign_confirmation_and_mount(self):
        parser = capture.build_parser()
        with self.assertRaisesRegex(capture.CaptureError, "exact --campaign"):
            capture.validate_args(
                parser.parse_args(["--out-root", "/tmp/recovery", "--recover-partials"])
            )

        with self.assertRaisesRegex(capture.CaptureError, "--confirm-recovery"):
            capture.validate_args(
                parser.parse_args(
                    [
                        "--out-root",
                        "/tmp/recovery",
                        "--recover-partials",
                        "--campaign",
                        "interrupted-drive",
                        "--execute",
                    ]
                )
            )

        with self.assertRaisesRegex(capture.CaptureError, "--require-mount"):
            capture.validate_args(
                parser.parse_args(
                    [
                        "--out-root",
                        "/tmp/recovery",
                        "--recover-partials",
                        "--campaign",
                        "interrupted-drive",
                        "--execute",
                        "--confirm-recovery",
                    ]
                )
            )

        policy = capture.validate_args(
            parser.parse_args(
                [
                    "--out-root",
                    "/mnt/EXFAT512/obd-things/tmp/captures/ccan",
                    "--require-mount",
                    "/mnt/EXFAT512",
                    "--recover-partials",
                    "--campaign",
                    "interrupted-drive",
                    "--execute",
                    "--confirm-recovery",
                ]
            )
        )
        self.assertIsInstance(policy, capture.DiskPolicy)

    def test_campaign_lock_excludes_recovery_or_second_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with capture.campaign_file_lock(run_dir):
                with self.assertRaisesRegex(capture.CaptureError, "already active"):
                    with capture.campaign_file_lock(run_dir):
                        self.fail("a second campaign owner acquired the same lock")

            # Releasing the first owner must make a later recovery possible.
            with capture.campaign_file_lock(run_dir):
                pass

    def test_runtime_safety_check_does_not_query_unrelated_services(self):
        def runner(command, **_kwargs):
            if command[0] == "ip":
                return Result(stdout=PASSIVE_DETAILS)
            raise AssertionError(f"unrelated service query: {command}")

        state = capture.runtime_safety_check(
            channel=TEST_CHANNEL,
            bitrate=TEST_BITRATE,
            runner=runner,
        )
        self.assertTrue(state.listen_only)

    def test_required_mount_must_contain_output_and_be_writable(self):
        class Stat:
            f_flag = 0

        with tempfile.TemporaryDirectory() as temporary:
            mount = Path(temporary)
            output = mount / "obd-things" / "captures"
            capture.require_writable_mount(
                output,
                mount,
                is_mount=lambda path: path == mount,
                statvfs=lambda _path: Stat(),
            )
            with self.assertRaisesRegex(capture.CaptureError, "below required mount"):
                capture.require_writable_mount(
                    Path("/tmp/outside"),
                    mount,
                    is_mount=lambda _path: True,
                    statvfs=lambda _path: Stat(),
                )

            Stat.f_flag = getattr(capture.os, "ST_RDONLY", 1)
            with self.assertRaisesRegex(capture.CaptureError, "read-only"):
                capture.require_writable_mount(
                    output,
                    mount,
                    is_mount=lambda _path: True,
                    statvfs=lambda _path: Stat(),
                )

    def test_execute_requires_both_live_gates(self):
        parser = capture.build_parser()
        args = parser.parse_args(
            ["--out-root", "/tmp/capture-test", "--execute", "--conditions", "driving"]
        )
        with self.assertRaisesRegex(capture.CaptureError, "--confirm-passive"):
            capture.validate_args(args)

        args = parser.parse_args(
            ["--out-root", "/tmp/capture-test", "--execute", "--confirm-passive"]
        )
        with self.assertRaisesRegex(capture.CaptureError, "--conditions"):
            capture.validate_args(args)

        args = parser.parse_args(
            [
                "--out-root",
                "/tmp/capture-test",
                "--execute",
                "--confirm-passive",
                "--conditions",
                "driving",
            ]
        )
        with self.assertRaisesRegex(capture.CaptureError, "--require-mount"):
            capture.validate_args(args)

    def test_execute_holds_role_lease_and_uses_its_resolved_channel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = capture.build_parser().parse_args(
                [
                    "--bus",
                    "can-ch",
                    "--out-root",
                    str(root / "captures"),
                    "--require-mount",
                    str(root),
                    "--campaign",
                    "role-test",
                    "--priority-profile",
                    "none",
                    "--execute",
                    "--confirm-passive",
                    "--conditions",
                    "passive role integration test",
                ]
            )
            policy = capture.validate_args(args)
            route = mock.Mock(
                role="can-ch",
                channel="can7",
                bitrate=500_000,
                pair="12/13",
                topology_fingerprint="stable-generation",
            )
            ownership = mock.Mock(route=route)
            state = interface_state_with_counters(0, 0)
            recorder = mock.Mock()
            recorder.run.return_value = 0

            with (
                mock.patch.object(
                    capture.can_runtime_route,
                    "acquire_passive_bus_route",
                    return_value=ownership,
                ) as acquire,
                mock.patch.object(
                    capture,
                    "require_writable_mount",
                    return_value="/dev/fake",
                ),
                mock.patch.object(
                    capture,
                    "preflight",
                    return_value=(state, 1000),
                ) as preflight,
                mock.patch.object(capture, "read_rmem_max", return_value=capture.RECEIVE_BUFFER),
                mock.patch.object(capture.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
                mock.patch.object(capture, "Recorder", return_value=recorder) as recorder_class,
            ):
                self.assertEqual(capture.execute(args, policy), 0)

            acquire.assert_called_once_with("can-ch")
            ownership.revalidate.assert_called_once_with()
            ownership.release.assert_called_once_with()
            self.assertEqual(preflight.call_count, 2)
            for call in preflight.call_args_list:
                self.assertEqual(call.kwargs["channel"], "can7")
                self.assertEqual(call.kwargs["bitrate"], 500_000)
            self.assertEqual(recorder_class.call_args.kwargs["channel"], "can7")
            self.assertEqual(recorder_class.call_args.kwargs["bitrate"], 500_000)

    def test_atomic_metadata_and_manifest_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "nested" / "checkpoint.json"
            manifest = root / "nested" / "manifest.jsonl"
            capture.atomic_write_json(checkpoint, {"status": "running", "sequence": 3})
            capture.append_manifest(manifest, {"type": "chunk", "sequence": 3})
            self.assertEqual(json.loads(checkpoint.read_text())["sequence"], 3)
            rows = [json.loads(line) for line in manifest.read_text().splitlines()]
            self.assertEqual(rows, [{"sequence": 3, "type": "chunk"}])

    def test_partial_recovery_uses_injected_verifier_and_keeps_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "chunk_000001_full.candump.zst.partial"
            invalid = root / "chunk_000002_full.candump.zst.partial"
            valid.write_bytes(b"valid-compressed-placeholder")
            invalid.write_bytes(b"truncated-placeholder")

            recovered = capture.recover_partials(
                root, lambda path: path.name.startswith("chunk_000001")
            )

            final = root / "chunk_000001_full.candump.zst"
            self.assertEqual(len(recovered), 1)
            self.assertTrue(final.exists())
            self.assertFalse(valid.exists())
            self.assertTrue(invalid.exists())
            self.assertEqual(recovered[0]["sha256"], capture.sha256_file(final))

    def test_stop_process_reaps_and_final_drains_after_consumer_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = capture.Recorder(
                Path(temporary),
                frozenset(),
                rotation_seconds=600,
                duration_seconds=1,
                policy=capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
            )
            process = FakeProcess(ignore_terminate=True)
            clock = [0.0]
            served_first = [False]
            served_tail = [False]
            read_after_kill = [False]
            consumed = []

            def fake_read(_descriptor, _size):
                if process.returncode == -capture.signal.SIGKILL:
                    read_after_kill[0] = True
                    if not served_tail[0]:
                        served_tail[0] = True
                        return b"tail\n"
                    return b""
                if not served_first[0]:
                    served_first[0] = True
                    return b"first\n"
                raise BlockingIOError

            def consume(data):
                consumed.append(data)
                if data == b"first\n":
                    raise RuntimeError("consumer failed")

            def advance_clock(_seconds):
                clock[0] += 10

            with mock.patch.object(capture.os, "read", side_effect=fake_read), mock.patch.object(
                capture.time, "monotonic", side_effect=lambda: clock[0]
            ), mock.patch.object(capture.time, "sleep", side_effect=advance_clock):
                with self.assertRaisesRegex(Exception, "consumer failed"):
                    recorder._stop_process(process, consume)

            self.assertIn(("terminate", None), process.events)
            self.assertIn(("kill", None), process.events)
            self.assertTrue(any(event[0] == "wait" for event in process.events))
            self.assertTrue(read_after_kill[0], "stdout was not drained after forced exit")
            self.assertEqual(consumed.count(b"first\n"), 1)
            self.assertEqual(consumed.count(b"tail\n"), 1)

    def test_stop_process_reports_forced_kill_as_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = capture.Recorder(
                Path(temporary),
                frozenset(),
                rotation_seconds=600,
                duration_seconds=1,
                policy=capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
            )
            process = FakeProcess(ignore_terminate=True)
            clock = [0.0]

            def fake_read(_descriptor, _size):
                if process.returncode == -capture.signal.SIGKILL:
                    return b""
                raise BlockingIOError

            def advance_clock(_seconds):
                clock[0] += 10

            with mock.patch.object(capture.os, "read", side_effect=fake_read), mock.patch.object(
                capture.time, "monotonic", side_effect=lambda: clock[0]
            ), mock.patch.object(capture.time, "sleep", side_effect=advance_clock):
                with self.assertRaisesRegex(capture.CaptureError, "(?i)forced|kill|terminate"):
                    recorder._stop_process(process, lambda _data: None)

            self.assertIn(("kill", None), process.events)
            self.assertTrue(any(event[0] == "wait" for event in process.events))

    def test_drop_handling_processes_each_buffered_line_once(self):
        frame_before = b"(1784704278.100000) can0 101#0102030405060708\n"
        drop = b"DROPCOUNT: dropped 2 CAN frames on 'can0' socket (total drops 2)\n"
        frame_after = b"(1784704278.100100) can0 103#0807060504030201\n"
        payload = frame_before + drop + frame_after

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            process = FakeProcess(descriptor=92)
            selector = FakeSelector(process.stdout.fileno(), [True])
            first_read = [True]
            RecordingChunk.writes = []

            def fake_read(_descriptor, _size):
                if first_read[0]:
                    first_read[0] = False
                    process.returncode = 0
                    return payload
                return b""

            recorder = capture.Recorder(
                run_dir,
                frozenset({0x101, 0x103}),
                rotation_seconds=600,
                duration_seconds=60,
                policy=capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
                popen=lambda *_args, **_kwargs: process,
                disk_free=lambda _path: 1000,
                safety_check=lambda: interface_state_with_counters(0, 0),
                mount_check=lambda: None,
            )
            with mock.patch.object(capture, "Chunk", RecordingChunk), mock.patch.object(
                capture.selectors, "DefaultSelector", return_value=selector
            ), mock.patch.object(capture.os, "set_blocking"), mock.patch.object(
                capture.os, "read", side_effect=fake_read
            ), mock.patch.object(
                capture.signal, "signal", return_value=capture.signal.SIG_DFL
            ):
                with self.assertRaisesRegex(
                    capture.CaptureError, "(?i)(dropped.*2|2.*dropped)"
                ):
                    recorder.run()

            self.assertEqual(RecordingChunk.writes, [frame_before, drop, frame_after])

    def test_interface_counter_increase_fails_capture(self):
        for counter_name, final_dropped, final_missed in (
            ("rx_dropped", 11, 4),
            ("rx_missed", 10, 5),
        ):
            with self.subTest(counter=counter_name), tempfile.TemporaryDirectory() as temporary:
                run_dir = Path(temporary)
                process = FakeProcess(exit_on_signal=True, descriptor=93)
                clock = [0.0]
                selector = FakeSelector(
                    process.stdout.fileno(),
                    [False, False],
                    on_select=lambda _timeout: clock.__setitem__(0, clock[0] + 0.6),
                )
                states = [
                    interface_state_with_counters(10, 4),
                    interface_state_with_counters(final_dropped, final_missed),
                ]
                safety_calls = [0]

                def safety_check():
                    index = min(safety_calls[0], len(states) - 1)
                    safety_calls[0] += 1
                    return states[index]

                recorder = capture.Recorder(
                    run_dir,
                    frozenset({0x101}),
                    rotation_seconds=600,
                    duration_seconds=1,
                    policy=capture.DiskPolicy(300, 200),
                    channel=TEST_CHANNEL,
                    bitrate=TEST_BITRATE,
                    popen=lambda *_args, **_kwargs: process,
                    disk_free=lambda _path: 1000,
                    safety_check=safety_check,
                    mount_check=lambda: None,
                )
                RecordingChunk.writes = []
                with mock.patch.object(capture, "Chunk", RecordingChunk), mock.patch.object(
                    capture.selectors, "DefaultSelector", return_value=selector
                ), mock.patch.object(capture.os, "set_blocking"), mock.patch.object(
                    capture.os, "read", return_value=b""
                ), mock.patch.object(
                    capture.signal, "signal", return_value=capture.signal.SIG_DFL
                ), mock.patch.object(
                    capture.time, "monotonic", side_effect=lambda: clock[0]
                ):
                    with self.assertRaisesRegex(
                        capture.CaptureError, "(?i)dropped|missed|counter"
                    ):
                        recorder.run()

                self.assertGreaterEqual(
                    safety_calls[0],
                    2,
                    "capture never compared a final interface counter snapshot",
                )

    def test_hard_floor_before_duration_is_error_in_manifest_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            process = FakeProcess(exit_on_signal=True, descriptor=95)
            clock = [0.0]
            selector = FakeSelector(
                process.stdout.fileno(),
                [False],
                on_select=lambda _timeout: clock.__setitem__(0, clock[0] + 1),
            )
            free_values = iter((1000, 200, 200))
            baseline = interface_state_with_counters(0, 0)
            recorder = capture.Recorder(
                run_dir,
                frozenset({0x101}),
                rotation_seconds=600,
                duration_seconds=100,
                policy=capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
                popen=lambda *_args, **_kwargs: process,
                disk_free=lambda _path: next(free_values),
                safety_check=lambda: baseline,
                mount_check=lambda: None,
            )
            RecordingChunk.writes = []
            with mock.patch.object(capture, "Chunk", RecordingChunk), mock.patch.object(
                capture.selectors, "DefaultSelector", return_value=selector
            ), mock.patch.object(capture.os, "set_blocking"), mock.patch.object(
                capture.os, "read", return_value=b""
            ), mock.patch.object(
                capture.signal, "signal", return_value=capture.signal.SIG_DFL
            ), mock.patch.object(
                capture.time, "monotonic", side_effect=lambda: clock[0]
            ):
                with self.assertRaisesRegex(capture.CaptureError, "hard free-space floor"):
                    recorder.run()

            records = [
                json.loads(line)
                for line in (run_dir / "manifest.jsonl").read_text().splitlines()
            ]
            capture_end = next(row for row in records if row["type"] == "capture_end")
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
            self.assertEqual(capture_end["reason"], "disk_hard_floor")
            self.assertFalse(capture_end["success"])
            self.assertFalse(capture_end["duration_complete"])
            self.assertEqual(capture_end["requested_duration_seconds"], 100)
            self.assertLess(capture_end["elapsed_seconds"], 100)
            self.assertEqual(checkpoint["status"], "error")
            self.assertFalse(checkpoint["success"])
            self.assertFalse(checkpoint["duration_complete"])

    def test_signal_before_duration_is_error_even_if_observed_after_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            process = FakeProcess(exit_on_signal=True, descriptor=97)
            clock = [0.0]
            installed_handlers = {}

            def fake_signal(signum, handler):
                previous = installed_handlers.get(signum, capture.signal.SIG_DFL)
                installed_handlers[signum] = handler
                return previous

            def request_stop(_timeout):
                clock[0] = 99.5
                installed_handlers[capture.signal.SIGTERM](
                    capture.signal.SIGTERM, None
                )
                clock[0] = 100.5

            selector = FakeSelector(
                process.stdout.fileno(),
                [False],
                on_select=request_stop,
            )
            baseline = interface_state_with_counters(0, 0)
            recorder = capture.Recorder(
                run_dir,
                frozenset({0x101}),
                rotation_seconds=600,
                duration_seconds=100,
                policy=capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
                popen=lambda *_args, **_kwargs: process,
                disk_free=lambda _path: 1000,
                safety_check=lambda: baseline,
                mount_check=lambda: None,
            )
            RecordingChunk.writes = []
            with mock.patch.object(capture, "Chunk", RecordingChunk), mock.patch.object(
                capture.selectors, "DefaultSelector", return_value=selector
            ), mock.patch.object(capture.os, "set_blocking"), mock.patch.object(
                capture.os, "read", return_value=b""
            ), mock.patch.object(
                capture.signal, "signal", side_effect=fake_signal
            ), mock.patch.object(
                capture.time, "monotonic", side_effect=lambda: clock[0]
            ):
                with self.assertRaisesRegex(
                    capture.CaptureError, "interrupted by signal"
                ):
                    recorder.run()

            records = [
                json.loads(line)
                for line in (run_dir / "manifest.jsonl").read_text().splitlines()
            ]
            capture_end = next(row for row in records if row["type"] == "capture_end")
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
            self.assertEqual(capture_end["reason"], "signal")
            self.assertEqual(capture_end["signal_number"], capture.signal.SIGTERM)
            self.assertEqual(capture_end["signal_elapsed_seconds"], 99.5)
            self.assertFalse(capture_end["success"])
            self.assertFalse(capture_end["duration_complete"])
            self.assertGreater(capture_end["elapsed_seconds"], 100)
            self.assertEqual(checkpoint["status"], "error")
            self.assertFalse(checkpoint["success"])

    def test_tracked_id_absence_cleanly_completes_before_duration(self):
        tracked_frame = b"(1784704278.100000) can0 2EF#0102030405060708\n"
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            process = FakeProcess(exit_on_signal=True, descriptor=98)
            clock = [0.0]
            selector = FakeSelector(
                process.stdout.fileno(),
                [True, False, False],
                on_select=lambda _timeout: clock.__setitem__(0, clock[0] + 1),
            )
            first_read = [True]

            def fake_read(_descriptor, _size):
                if first_read[0]:
                    first_read[0] = False
                    return tracked_frame
                raise BlockingIOError

            baseline = interface_state_with_counters(0, 0)
            recorder = capture.Recorder(
                run_dir,
                frozenset({0x2EF}),
                rotation_seconds=600,
                duration_seconds=100,
                policy=capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
                stop_after_id=0x2EF,
                stop_after_id_absence_seconds=2,
                popen=lambda *_args, **_kwargs: process,
                disk_free=lambda _path: 1000,
                safety_check=lambda: baseline,
                mount_check=lambda: None,
            )
            RecordingChunk.writes = []
            with mock.patch.object(capture, "Chunk", RecordingChunk), mock.patch.object(
                capture.selectors, "DefaultSelector", return_value=selector
            ), mock.patch.object(capture.os, "set_blocking"), mock.patch.object(
                capture.os, "read", side_effect=fake_read
            ), mock.patch.object(
                capture.signal, "signal", return_value=capture.signal.SIG_DFL
            ), mock.patch.object(
                capture.time, "monotonic", side_effect=lambda: clock[0]
            ):
                self.assertEqual(recorder.run(), 0)

            records = [
                json.loads(line)
                for line in (run_dir / "manifest.jsonl").read_text().splitlines()
            ]
            capture_end = next(row for row in records if row["type"] == "capture_end")
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
            self.assertEqual(capture_end["reason"], "tracked_id_absent")
            self.assertTrue(capture_end["success"])
            self.assertFalse(capture_end["duration_complete"])
            self.assertEqual(capture_end["tracked_id"], "0x2EF")
            self.assertEqual(capture_end["tracked_id_absence_seconds"], 2)
            self.assertEqual(checkpoint["status"], "complete")

    def test_required_start_id_timeout_fails_bounded_and_records_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            process = FakeProcess(exit_on_signal=True, descriptor=99)
            clock = [0.0]
            selector = FakeSelector(
                process.stdout.fileno(),
                [False, False],
                on_select=lambda _timeout: clock.__setitem__(0, clock[0] + 1),
            )
            baseline = interface_state_with_counters(0, 0)
            recorder = capture.Recorder(
                run_dir,
                frozenset({0x2EF}),
                rotation_seconds=600,
                duration_seconds=100,
                policy=capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
                required_start_id=0x2EF,
                required_start_id_timeout_seconds=2,
                popen=lambda *_args, **_kwargs: process,
                disk_free=lambda _path: 1000,
                safety_check=lambda: baseline,
                mount_check=lambda: None,
                candump_extra_args=("-D",),
            )
            self.assertEqual(
                recorder._candump_command(),
                [
                    "candump",
                    "-L",
                    "-D",
                    "-d",
                    "-r",
                    str(capture.RECEIVE_BUFFER),
                    TEST_CHANNEL,
                ],
            )
            RecordingChunk.writes = []
            with mock.patch.object(capture, "Chunk", RecordingChunk), mock.patch.object(
                capture.selectors, "DefaultSelector", return_value=selector
            ), mock.patch.object(capture.os, "set_blocking"), mock.patch.object(
                capture.os, "read", side_effect=BlockingIOError
            ), mock.patch.object(
                capture.signal, "signal", return_value=capture.signal.SIG_DFL
            ), mock.patch.object(
                capture.time, "monotonic", side_effect=lambda: clock[0]
            ):
                with self.assertRaisesRegex(
                    capture.CaptureError,
                    "required start CAN ID 0x2EF",
                ):
                    recorder.run()

            rows = [
                json.loads(line)
                for line in (run_dir / "manifest.jsonl").read_text().splitlines()
            ]
            start = next(row for row in rows if row["type"] == "capture_start")
            end = next(row for row in rows if row["type"] == "capture_end")
            self.assertIn("-D", start["candump_command"])
            self.assertEqual(end["reason"], "required_start_id_missing")
            self.assertFalse(end["success"])

    def test_priority_only_degradation_marks_full_stream_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            process = FakeProcess(exit_on_signal=True, descriptor=96)
            clock = [0.0]
            selector = FakeSelector(
                process.stdout.fileno(),
                [False, False],
                on_select=lambda _timeout: clock.__setitem__(0, clock[0] + 1),
            )
            free_values = iter((1000, 250, 250))
            baseline = interface_state_with_counters(0, 0)
            recorder = capture.Recorder(
                run_dir,
                frozenset({0x101}),
                rotation_seconds=600,
                duration_seconds=2,
                policy=capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
                popen=lambda *_args, **_kwargs: process,
                disk_free=lambda _path: next(free_values),
                safety_check=lambda: baseline,
                mount_check=lambda: None,
            )
            RecordingChunk.writes = []
            with mock.patch.object(capture, "Chunk", RecordingChunk), mock.patch.object(
                capture.selectors, "DefaultSelector", return_value=selector
            ), mock.patch.object(capture.os, "set_blocking"), mock.patch.object(
                capture.os, "read", return_value=b""
            ), mock.patch.object(
                capture.signal, "signal", return_value=capture.signal.SIG_DFL
            ), mock.patch.object(
                capture.time, "monotonic", side_effect=lambda: clock[0]
            ):
                self.assertEqual(recorder.run(), 0)

            records = [
                json.loads(line)
                for line in (run_dir / "manifest.jsonl").read_text().splitlines()
            ]
            self.assertTrue(
                any(
                    row.get("type") == "mode_change"
                    and row.get("mode") == "priority-only"
                    for row in records
                )
            )
            capture_end = next(row for row in records if row["type"] == "capture_end")
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
            self.assertTrue(capture_end["success"])
            self.assertTrue(capture_end["duration_complete"])
            self.assertFalse(capture_end["full_stream_complete"])
            self.assertFalse(checkpoint["full_stream_complete"])

    def test_pending_chunk_finalization_age_fails_closed(self):
        class DelayedFuture:
            def __init__(self, clock):
                self.clock = clock

            def done(self):
                self.clock[0] += capture.MAX_PENDING_FINALIZATION_SECONDS + 1
                return False

            def result(self):
                return {
                    "type": "chunk",
                    "sequence": 0,
                    "streams": {},
                    "complete": True,
                }

        class DelayedExecutor:
            def __init__(self, clock):
                self.clock = clock
                self.future = DelayedFuture(clock)

            def submit(self, _function, *_args):
                return self.future

            def shutdown(self, wait=True, cancel_futures=False):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            process = FakeProcess(exit_on_signal=True, descriptor=94)
            clock = [0.0]
            selector = FakeSelector(
                process.stdout.fileno(),
                [False],
                on_select=lambda _timeout: clock.__setitem__(0, clock[0] + 11),
            )
            executor = DelayedExecutor(clock)
            baseline = interface_state_with_counters(0, 0)
            recorder = capture.Recorder(
                run_dir,
                frozenset({0x101}),
                rotation_seconds=10,
                duration_seconds=1000,
                policy=capture.DiskPolicy(300, 200),
                channel=TEST_CHANNEL,
                bitrate=TEST_BITRATE,
                popen=lambda *_args, **_kwargs: process,
                disk_free=lambda _path: 1000,
                safety_check=lambda: baseline,
                mount_check=lambda: None,
            )
            RecordingChunk.writes = []
            with mock.patch.object(capture, "Chunk", RecordingChunk), mock.patch.object(
                capture.selectors, "DefaultSelector", return_value=selector
            ), mock.patch.object(
                capture.concurrent.futures,
                "ThreadPoolExecutor",
                return_value=executor,
            ), mock.patch.object(capture.os, "set_blocking"), mock.patch.object(
                capture.os, "read", return_value=b""
            ), mock.patch.object(
                capture.signal, "signal", return_value=capture.signal.SIG_DFL
            ), mock.patch.object(
                capture.time, "monotonic", side_effect=lambda: clock[0]
            ):
                with self.assertRaisesRegex(
                    capture.CaptureError,
                    rf"finalization exceeded {capture.MAX_PENDING_FINALIZATION_SECONDS}s",
                ):
                    recorder.run()

            self.assertTrue(any(event[0] == "wait" for event in process.events))


if __name__ == "__main__":
    unittest.main()
