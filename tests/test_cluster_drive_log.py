import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

from projects.ecu_mapping import cluster_drive_log as drive
from tools.passive_drive_capture import InterfaceState


ACTIVE_STATE = InterfaceState(
    up=True,
    bitrate=500_000,
    listen_only=False,
    controller_state="ERROR-ACTIVE",
    rx_dropped=0,
    rx_missed=0,
)


class ClusterDrivePlanTests(unittest.TestCase):
    def test_profile_is_fixed_to_exact_cluster_reads(self):
        self.assertEqual(drive.MODULE.key, "cluster")
        self.assertEqual(drive.MODULE.txid, 0x18DA60F1)
        self.assertEqual(drive.MODULE.rxid, 0x18DAF160)
        self.assertEqual(
            drive.CLUSTER_DIDS,
            (0x1000, 0x1002, 0x0107, 0x1004, 0x1005),
        )
        self.assertEqual(drive.REQUEST_RATE_HZ, 5.0)
        self.assertEqual(drive.MAX_DURATION_SECONDS, 22 * 60 * 60)
        self.assertEqual(
            drive.EXPECTED_DATA_LENGTHS,
            {0x1000: 2, 0x1002: 1, 0x0107: 1, 0x1004: 1, 0x1005: 1},
        )

    def test_dry_run_never_touches_live_dependencies_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "must-not-exist"
            with (
                mock.patch.object(drive, "preflight_live") as preflight,
                mock.patch.object(
                    drive.diagnostic_safety, "acquire_channel_lock"
                ) as lock,
                mock.patch.object(drive, "RawCapture") as raw,
                mock.patch.object(drive.uds, "open_module_socket") as open_socket,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                result = drive.main(
                    [
                        "--out-root",
                        str(output),
                        "--require-mount",
                        "/mnt/EXFAT512",
                        "--duration-seconds",
                        "60",
                    ]
                )

            self.assertFalse(output.exists())

        self.assertEqual(result, 0)
        payload_text, _, footer = stdout.getvalue().partition("\nDRY RUN:")
        payload = json.loads(payload_text)
        self.assertEqual(payload["mode"], "plan_only")
        self.assertEqual(
            payload["request_payloads"],
            ["22 10 00", "22 10 02", "22 01 07", "22 10 04", "22 10 05"],
        )
        self.assertIn("no DiagnosticSessionControl or TesterPresent", payload["diagnostic_session_policy"])
        self.assertIn("may refresh an inherited S3 timer", " ".join(payload["does_not"]))
        self.assertTrue(payload["telemetry_publication"]["enabled"])
        self.assertEqual(
            set(payload["telemetry_publication"]["metrics"]),
            drive.TELEMETRY_METRICS,
        )
        self.assertTrue(footer)
        preflight.assert_not_called()
        lock.assert_not_called()
        raw.assert_not_called()
        open_socket.assert_not_called()

    def test_execute_gates_fail_before_preflight(self):
        with (
            mock.patch.object(drive, "preflight_live") as preflight,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = drive.main(
                [
                    "--out-root",
                    "/tmp/cluster-drive-fixture",
                    "--require-mount",
                    "/mnt/EXFAT512",
                    "--execute",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_execute_requires_exact_verified_pair(self):
        with (
            mock.patch.object(drive, "preflight_live") as preflight,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = drive.main(
                [
                    "--out-root",
                    "/tmp/cluster-drive-fixture",
                    "--require-mount",
                    "/mnt/EXFAT512",
                    "--execute",
                    "--confirm-driving-read-only",
                    "--confirm-started-parked",
                    "--confirm-no-other-diagnostics",
                    "--pair",
                    "3/11",
                    "--conditions",
                    "fixture",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_duration_and_disk_bounds_are_validated_offline(self):
        for arguments in (
            ["--duration-seconds", str(drive.MAX_DURATION_SECONDS + 1)],
            ["--soft-free-gib", "25", "--hard-free-gib", "25"],
            ["--soft-free-gib", "nan"],
        ):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                result = drive.main(
                    [
                        "--out-root",
                        "/tmp/cluster-drive-fixture",
                        *arguments,
                    ]
                )
            self.assertEqual(result, 2)


class ClusterDriveMountGuardTests(unittest.TestCase):
    def test_mount_loss_is_sticky_and_blocks_path_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            out_root = base / "missing-mount" / "dids"
            raw_root = base / "missing-mount" / "raw"
            checker = mock.Mock(
                side_effect=[123, 123, OSError("fixture mount disappeared")]
            )
            guard = drive.EvidenceMountGuard(
                out_root,
                raw_root,
                base / "missing-mount",
                123,
                checker=checker,
            )
            guard.check()
            with self.assertRaisesRegex(
                drive.DriveLogError,
                "all later path publication is disabled",
            ):
                guard.check()

            with (
                mock.patch.object(drive, "atomic_write_json") as writer,
                self.assertRaisesRegex(
                    drive.DriveLogError,
                    "all later path publication is disabled",
                ),
            ):
                drive.guarded_atomic_write_json(
                    guard,
                    out_root / "campaign" / "summary.json",
                    {"status": "failed"},
                )

            writer.assert_not_called()
            self.assertEqual(checker.call_count, 3)
            self.assertFalse((base / "missing-mount").exists())

    def test_raw_metadata_guard_runs_before_owner_path_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = mock.Mock(
                side_effect=drive.DriveLogError("fixture publication disabled")
            )
            raw = drive.RawCapture(Path(directory) / "raw", path_guard=guard)
            with (
                mock.patch.object(drive, "atomic_write_json") as writer,
                self.assertRaisesRegex(
                    drive.DriveLogError,
                    "fixture publication disabled",
                ),
            ):
                raw._write_owner("fixture")

            guard.assert_called_once_with()
            writer.assert_not_called()


class ClusterDriveRequestTests(unittest.TestCase):
    def test_positive_read_sends_one_exact_22_without_retry(self):
        sock = mock.Mock()
        with (
            mock.patch.object(drive.uds, "drain") as drain,
            mock.patch.object(
                drive.uds,
                "request",
                return_value=(bytes.fromhex("62 10 00 12 34"), "POSITIVE"),
            ) as request,
        ):
            result = drive.query_did(sock, 0x1000)

        drain.assert_called_once_with(sock)
        request.assert_called_once_with(
            sock,
            bytes.fromhex("22 10 00"),
            timeout=drive.REQUEST_TIMEOUT_S,
            retries=0,
        )
        self.assertEqual(result["category"], "positive")
        self.assertEqual(result["data_hex"], "12 34")

    def test_timeout_and_negative_are_recorded_without_replay(self):
        sock = mock.Mock()
        with (
            mock.patch.object(drive.uds, "drain"),
            mock.patch.object(
                drive.uds,
                "request",
                side_effect=[
                    (None, "NO_RESPONSE"),
                    (bytes.fromhex("7F 22 31"), "NEGATIVE"),
                ],
            ) as request,
        ):
            timeout = drive.query_did(sock, 0x1002)
            negative = drive.query_did(sock, 0x1002)

        self.assertEqual(timeout["category"], "timeout")
        self.assertIsNone(timeout["response_hex"])
        self.assertEqual(negative["category"], "negative_31_ambiguous")
        self.assertEqual(
            negative["negative_response_assignment"],
            "ambiguous_after_pre_send_drain",
        )
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.kwargs["retries"] == 0 for call in request.call_args_list))

    def test_wrong_echo_is_returned_for_caller_to_persist_then_abort(self):
        with (
            mock.patch.object(drive.uds, "drain"),
            mock.patch.object(
                drive.uds,
                "request",
                return_value=(bytes.fromhex("62 10 01 AA"), "UNEXPECTED"),
            ),
        ):
            result = drive.query_did(mock.Mock(), 0x1000)

        self.assertEqual(result["category"], "unexpected")
        self.assertEqual(result["response_hex"], "62 10 01 AA")

    def test_positive_length_must_match_reviewed_profile(self):
        with (
            mock.patch.object(drive.uds, "drain"),
            mock.patch.object(
                drive.uds,
                "request",
                return_value=(bytes.fromhex("62 10 00 12"), "POSITIVE"),
            ),
        ):
            result = drive.query_did(mock.Mock(), 0x1000)

        self.assertEqual(result["category"], "invalid_length")
        self.assertEqual(result["expected_data_length"], 2)

    def test_negative_response_must_be_exactly_three_bytes(self):
        with (
            mock.patch.object(drive.uds, "drain"),
            mock.patch.object(
                drive.uds,
                "request",
                return_value=(bytes.fromhex("7F 22 31 00"), "NEGATIVE"),
            ),
        ):
            result = drive.query_did(mock.Mock(), 0x1002)

        self.assertEqual(result["category"], "unexpected")

    def test_rate_callback_runs_after_drain_immediately_before_request(self):
        events = []
        with (
            mock.patch.object(
                drive.uds,
                "drain",
                side_effect=lambda _sock: events.append("drain"),
            ),
            mock.patch.object(
                drive.uds,
                "request",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("request") or (bytes.fromhex("62 10 02 00"), "POSITIVE")
                ),
            ),
        ):
            drive.query_did(
                mock.Mock(),
                0x1002,
                before_request=lambda: events.append("pace"),
            )

        self.assertEqual(events, ["drain", "pace", "request"])


class ClusterDriveTelemetryTests(unittest.TestCase):
    def test_exact_positive_dids_map_only_to_allowlisted_observations(self):
        metric, payload = drive.telemetry_observation_for_did(
            0x1004, bytes.fromhex("8E")
        )
        self.assertEqual(metric, "battery.voltage")
        self.assertAlmostEqual(payload["value"], 14.2)
        self.assertEqual(payload["quality"], "observed_alfa_scale")

        _metric, payload = drive.telemetry_observation_for_did(
            0x1004, bytes.fromhex("88")
        )
        self.assertEqual(payload["value"], 13.6)

        metric, payload = drive.telemetry_observation_for_did(
            0x1000, bytes.fromhex("12 34")
        )
        self.assertEqual(metric, "diagnostics.cluster.did.1000.raw")
        self.assertEqual(payload["value"], 0x1234)
        self.assertEqual(payload["unit"], "raw_u16_be")
        self.assertEqual(payload["quality"], "candidate")

        metric, payload = drive.telemetry_observation_for_did(
            0x0107, bytes.fromhex("03")
        )
        self.assertEqual(metric, "diagnostics.cluster.did.0107.raw")
        self.assertEqual(payload["value"], 3)
        self.assertEqual(payload["source"], "cluster.did.0107")

        with self.assertRaisesRegex(ValueError, "payload length"):
            drive.telemetry_observation_for_did(0x1000, b"\x01")

    def test_publisher_is_bounded_latest_value_and_drains_on_close(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def publish(self, metric, **payload):
                self.calls.append((metric, payload))
                return 202, {"accepted": True}

        client = FakeClient()
        publisher = drive.BestEffortTelemetryPublisher(
            "/tmp/fixture.sock",
            client_factory=lambda _path, _timeout: client,
        )
        publisher.submit(
            "diagnostics.cluster.did.1002.raw",
            {
                "value": 1,
                "unit": "raw_u8",
                "source": "cluster.did.1002",
                "bus": "c-can",
                "quality": "candidate",
            },
        )
        publisher.submit(
            "diagnostics.cluster.did.1002.raw",
            {
                "value": 2,
                "unit": "raw_u8",
                "source": "cluster.did.1002",
                "bus": "c-can",
                "quality": "candidate",
            },
        )
        publisher.start()
        report = publisher.close()

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1]["value"], 2)
        self.assertEqual(report["submitted"], 2)
        self.assertEqual(report["superseded"], 1)
        self.assertEqual(report["published"], 1)
        self.assertEqual(report["pending"], 0)
        self.assertFalse(report["thread_alive"])

    def test_publisher_clears_transient_error_after_later_success(self):
        class RecoveringClient:
            def __init__(self):
                self.calls = 0

            def publish(self, _metric, **_payload):
                self.calls += 1
                if self.calls == 1:
                    raise FileNotFoundError("broker starting")
                return 202, {"accepted": True}

        client = RecoveringClient()
        publisher = drive.BestEffortTelemetryPublisher(
            "/tmp/fixture.sock",
            client_factory=lambda _path, _timeout: client,
        )
        for metric, payload in (
            (
                "diagnostics.cluster.did.1002.raw",
                {
                    "value": 0,
                    "unit": "raw_u8",
                    "source": "cluster.did.1002",
                    "bus": "c-can",
                    "quality": "candidate",
                },
            ),
            (
                "vehicle.ignition_on",
                {
                    "value": True,
                    "unit": "boolean",
                    "source": "ccan.broadcast.0x2ef",
                    "bus": "c-can",
                    "quality": "verified",
                },
            ),
        ):
            publisher.submit(metric, payload)
        publisher.start()
        report = publisher.close()

        self.assertEqual(report["attempts"], 2)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["published"], 1)
        self.assertIsNone(report["last_error"])

    def test_publisher_rejects_any_metric_outside_fixed_allowlist(self):
        publisher = drive.BestEffortTelemetryPublisher("/tmp/fixture.sock")
        with self.assertRaisesRegex(ValueError, "non-allowlisted"):
            publisher.submit(
                "diagnostics.arbitrary.did",
                {
                    "value": 1,
                    "unit": "raw",
                    "source": "fixture",
                    "bus": "c-can",
                    "quality": "candidate",
                },
            )


class ClusterDriveInterfaceTests(unittest.TestCase):
    def test_active_interface_requires_armed_error_active_zero_new_drops(self):
        drive.validate_active_interface(ACTIVE_STATE)
        for changed in (
            InterfaceState(True, 500_000, True, "ERROR-ACTIVE", 0, 0),
            InterfaceState(True, 125_000, False, "ERROR-ACTIVE", 0, 0),
            InterfaceState(True, 500_000, False, "BUS-OFF", 0, 0),
            InterfaceState(True, 500_000, False, "ERROR-ACTIVE", 1, 0),
        ):
            with self.subTest(changed=changed), self.assertRaises(drive.DriveLogError):
                drive.validate_active_interface(changed, baseline=ACTIVE_STATE)

    def test_raw_command_is_full_bus_and_loss_accounted(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = drive.RawCapture(Path(directory), candump="/usr/bin/candump")

        self.assertEqual(
            raw.command,
            [
                "/usr/bin/candump",
                "-L",
                "-d",
                "-r",
                str(drive.RECEIVE_BUFFER),
                "can0",
            ],
        )
        self.assertNotIn(",", raw.command[-1])

    def test_recorder_children_are_isolated_and_get_parent_death_signal(self):
        popen = mock.Mock(return_value=object())
        with tempfile.TemporaryDirectory() as directory:
            raw = drive.RawCapture(Path(directory), popen=popen)
            raw._spawn(["fixture"], start_new_session=False)

        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["preexec_fn"], drive._set_parent_death_signal)

    def test_request_pacer_enforces_spacing_and_deadline(self):
        clock = [0.0]
        calls = []

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        pacer = drive.RequestPacer(
            0.5,
            lambda: calls.append(clock[0]),
            monotonic=monotonic,
            sleep=sleep,
        )
        pacer()
        first = pacer.last_request_call
        pacer()
        second = pacer.last_request_call
        self.assertGreaterEqual(second - first, drive.REQUEST_INTERVAL_S)
        clock[0] = 0.5
        with self.assertRaises(drive.CampaignLimitReached):
            pacer()

        clock[0] = 0.0
        health_calls = 0

        def slow_health():
            nonlocal health_calls
            health_calls += 1
            if health_calls == 2:
                clock[0] = 1.0

        boundary = drive.RequestPacer(
            1.0,
            slow_health,
            monotonic=monotonic,
            sleep=sleep,
        )
        with self.assertRaisesRegex(
            drive.CampaignLimitReached,
            "final health check",
        ):
            boundary()


class ClusterDriveHealthTests(unittest.TestCase):
    def test_startup_requires_first_exact_positive_for_every_did(self):
        failures = {did: 0 for did in drive.CLUSTER_DIDS}
        with self.assertRaisesRegex(drive.DriveLogError, "startup profile"):
            drive.enforce_did_health(
                0x1002,
                "timeout",
                startup_profile_validated=False,
                consecutive_failures=failures,
            )

    def test_each_did_has_its_own_three_failure_threshold_and_reset(self):
        failures = {did: 0 for did in drive.CLUSTER_DIDS}
        for _ in range(drive.MAX_CONSECUTIVE_DID_FAILURES - 1):
            drive.enforce_did_health(
                0x1002,
                "timeout",
                startup_profile_validated=True,
                consecutive_failures=failures,
            )
        self.assertEqual(failures[0x1002], 2)
        self.assertEqual(failures[0x1000], 0)
        drive.enforce_did_health(
            0x1002,
            "positive",
            startup_profile_validated=True,
            consecutive_failures=failures,
        )
        self.assertEqual(failures[0x1002], 0)
        for _ in range(drive.MAX_CONSECUTIVE_DID_FAILURES - 1):
            drive.enforce_did_health(
                0x1002,
                "negative_31_ambiguous",
                startup_profile_validated=True,
                consecutive_failures=failures,
            )
        with self.assertRaisesRegex(drive.DriveLogError, "3 consecutive"):
            drive.enforce_did_health(
                0x1002,
                "negative_31_ambiguous",
                startup_profile_validated=True,
                consecutive_failures=failures,
            )


class ClusterDriveRawEvidenceTests(unittest.TestCase):
    def _raw(self, root: Path) -> drive.RawCapture:
        raw = drive.RawCapture(root, candump="candump")
        raw.chunk = mock.Mock()
        raw.chunk.sequence = 0
        raw._wire_handle = io.StringIO()
        return raw

    def test_raw_stream_counts_exact_cluster_wire_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = self._raw(root)
            raw._consume_line(
                b"(1700000000.100001) can0 101#0011223344556677\n",
                fail_on_drop=True,
            )
            raw._consume_line(
                b"(1700000000.200002) can0 18DA60F1#03221000\n",
                fail_on_drop=True,
            )
            raw._consume_line(
                b"(1700000000.300003) can0 18DAF160#056210001234\n",
                fail_on_drop=True,
            )
            raw._consume_line(
                b"(1700000000.400004) can0 18DAF160#037F2278\n",
                fail_on_drop=True,
            )
            records = [
                json.loads(line)
                for line in raw._wire_handle.getvalue().splitlines()
            ]

        self.assertEqual(raw.frame_count, 4)
        self.assertEqual(raw.wire_request_counts, {"1000": 1})
        self.assertEqual(raw.wire_positive_counts, {"1000": 1})
        self.assertEqual(
            [record["classification"] for record in records],
            ["exact_request", "exact_positive_response", "response_pending"],
        )
        self.assertEqual(raw.wire_pending_responses, 1)
        self.assertEqual(records[0]["timestamp_epoch_us"], 1700000000200002)
        self.assertEqual(raw.chunk.write.call_count, 4)

    def test_internal_accounting_requires_contiguous_complete_frame_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = drive.RawCapture(Path(directory))
        raw.frame_count = 5
        raw.chunk_records = [
            {
                "sequence": 0,
                "streams": {"full": {"frames": 2}},
            },
            {
                "sequence": 1,
                "streams": {"full": {"frames": 3}},
            },
        ]
        raw.wire_frame_sequence = 3
        raw.wire_classification_counts.update(
            {"exact_request": 2, "exact_positive_response": 1}
        )

        accounting = raw._validate_internal_accounting()
        self.assertTrue(accounting["complete"])
        self.assertEqual(accounting["chunk_full_stream_frames"], 5)
        self.assertEqual(accounting["classified_wire_frames"], 3)

        raw.chunk_records[1]["sequence"] = 2
        with self.assertRaisesRegex(drive.DriveLogError, "not contiguous"):
            raw._validate_internal_accounting()
        raw.chunk_records[1]["sequence"] = 1
        raw.frame_count = 6
        with self.assertRaisesRegex(drive.DriveLogError, "ingested frames"):
            raw._validate_internal_accounting()
        raw.frame_count = 5
        raw.wire_frame_sequence = 4
        with self.assertRaisesRegex(drive.DriveLogError, "wire rows"):
            raw._validate_internal_accounting()

    def test_drop_notice_is_manifested_and_fails_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = self._raw(root)
            with self.assertRaisesRegex(drive.DriveLogError, "dropped frames"):
                raw._consume_line(
                    b"DROPCOUNT: dropped 2 CAN frames on 'can0' socket (total drops 2)\n",
                    fail_on_drop=True,
                )
            manifest = [
                json.loads(line)
                for line in raw.manifest_path.read_text().splitlines()
            ]

        self.assertEqual(raw.detected_drops, 2)
        self.assertEqual(manifest[0]["type"], "socket_drop")

    def test_malformed_line_fails_before_compression(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = self._raw(root)
            with self.assertRaisesRegex(drive.DriveLogError, "malformed"):
                raw._consume_line(
                    b"not a candump frame\n",
                    fail_on_drop=True,
                )
        raw.chunk.write.assert_not_called()

    def test_raw_stop_rejects_forced_termination(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)

        class FakeProcess:
            def __init__(self):
                self.stdout = os.fdopen(read_fd, "rb", buffering=0)
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def send_signal(self, _signal):
                return None

            def terminate(self):
                self.terminated = True
                self.returncode = -signal.SIGTERM

            def kill(self):
                self.returncode = -signal.SIGKILL

            def wait(self, timeout=None):
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("candump", timeout)
                return self.returncode

        process = FakeProcess()
        clock = _FakeClock(step=1.0)
        with tempfile.TemporaryDirectory() as directory:
            raw = drive.RawCapture(Path(directory))
            raw.process = process
            raw.chunk = mock.Mock()
            raw._stderr_handle = mock.Mock()
            with (
                mock.patch.object(drive.time, "monotonic", side_effect=clock),
                mock.patch.object(drive.time, "sleep"),
                self.assertRaisesRegex(drive.DriveLogError, "required SIGTERM"),
            ):
                raw.stop()

        self.assertTrue(process.terminated)

    def test_clean_candump_sigint_exit_codes_are_accepted(self):
        for returncode in (0, -signal.SIGINT, 128 + signal.SIGINT):
            with self.subTest(returncode=returncode):
                read_fd, write_fd = os.pipe()
                os.close(write_fd)

                class CleanProcess:
                    def __init__(self):
                        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
                        self.returncode = None
                        self.pid = 123

                    def poll(self):
                        return self.returncode

                    def send_signal(self, _signal):
                        self.returncode = returncode

                    def terminate(self):
                        raise AssertionError("clean stop must not terminate")

                    def kill(self):
                        raise AssertionError("clean stop must not kill")

                    def wait(self, timeout=None):
                        return self.returncode

                with tempfile.TemporaryDirectory() as directory:
                    raw = drive.RawCapture(Path(directory))
                    raw.process = CleanProcess()
                    raw.frame_count = 1
                    result = raw.stop_ingest()

                self.assertEqual(result["returncode"], returncode)
                self.assertTrue(raw.ingest_stopped)

    def test_wire_cross_validation_requires_exact_counts(self):
        result = drive.validate_wire_evidence(
            {
                "complete": True,
                "chunks": 1,
                "detected_socket_drops": 0,
                "wire_request_counts": {f"{did:04X}": 1 for did in drive.CLUSTER_DIDS},
                "wire_positive_counts": {f"{did:04X}": 1 for did in drive.CLUSTER_DIDS},
            },
            drive.Counter({did: 1 for did in drive.CLUSTER_DIDS}),
            drive.Counter({did: 1 for did in drive.CLUSTER_DIDS}),
        )
        self.assertTrue(result["complete"])

    def test_rotation_swaps_first_and_finalizes_old_chunk_asynchronously(self):
        old = mock.Mock()
        old.sequence = 0
        old.started_monotonic = 0.0
        new = mock.Mock()
        new.sequence = 1
        future = mock.Mock()
        future.done.return_value = False
        executor = mock.Mock()
        executor.submit.return_value = future
        with tempfile.TemporaryDirectory() as directory:
            raw = drive.RawCapture(Path(directory), rotation_seconds=10)
            raw.chunk = old
            with (
                mock.patch.object(raw, "_new_chunk", return_value=new),
                mock.patch.object(raw, "_write_owner"),
                mock.patch.object(
                    drive.concurrent.futures,
                    "ThreadPoolExecutor",
                    return_value=executor,
                ),
                mock.patch.object(drive.time, "monotonic", return_value=11.0),
            ):
                raw._rotate_if_due()

        self.assertIs(raw.chunk, new)
        executor.submit.assert_called_once()
        submitted_callable, submitted_verifier = executor.submit.call_args.args
        self.assertEqual(submitted_callable, old.finish)
        self.assertEqual(submitted_verifier, raw._verifier)
        old.finish.assert_not_called()

    def test_completed_async_chunk_is_harvested_before_next_spawn(self):
        record = {"complete": True, "sequence": 0, "streams": {}}
        future = mock.Mock()
        future.done.return_value = True
        future.result.return_value = record
        executor = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = drive.RawCapture(root)
            raw._pending_finalization = future
            raw._pending_submitted_at = 0.0
            raw._pending_sequence = 0
            raw._finalizer = executor
            with (
                mock.patch.object(drive.time, "monotonic", return_value=1.0),
                mock.patch.object(drive, "append_manifest") as append,
            ):
                raw._harvest_pending(wait=False)

        executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
        append.assert_called_once_with(raw.manifest_path, record)
        self.assertEqual(raw.chunk_records, [record])
        self.assertIsNone(raw._pending_finalization)

    def test_interrupted_inflight_exchange_has_one_record_allowance(self):
        requests = {f"{did:04X}": 1 for did in drive.CLUSTER_DIDS}
        positives = dict(requests)
        attempts = drive.Counter({did: 1 for did in drive.CLUSTER_DIDS})
        outcomes = drive.Counter({did: 1 for did in drive.CLUSTER_DIDS})
        requests["1002"] = 2
        positives["1002"] = 2
        result = drive.validate_wire_evidence(
            {
                "complete": True,
                "chunks": 1,
                "detected_socket_drops": 0,
                "wire_request_counts": requests,
                "wire_positive_counts": positives,
            },
            attempts,
            outcomes,
            interrupted=True,
            inflight_did=0x1002,
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["interrupted_inflight_allowance"], "1002")


class ClusterDriveIgnitionTests(unittest.TestCase):
    def test_observer_uses_exact_sff_filter_and_never_sends(self):
        sock = mock.Mock()
        frame = (
            (drive.IGNITION_CAN_ID).to_bytes(4, "little")
            + b"\x08\x00\x00\x00"
            + b"\x00" * 8
        )
        sock.recv.side_effect = [frame, BlockingIOError()]
        watcher = drive.IgnitionWatcher(socket_factory=mock.Mock(return_value=sock))
        with mock.patch.object(drive.time, "monotonic", return_value=10.0):
            watcher.open()
            self.assertTrue(watcher.poll())

        expected_filter = drive.struct.pack(
            "=II",
            drive.IGNITION_CAN_ID,
            drive.CAN_EFF_FLAG | drive.CAN_RTR_FLAG | drive.CAN_SFF_MASK,
        )
        sock.setsockopt.assert_called_once_with(
            drive.SOL_CAN_RAW,
            drive.CAN_RAW_FILTER,
            expected_filter,
        )
        sock.bind.assert_called_once_with(("can0",))
        sock.send.assert_not_called()
        self.assertEqual(watcher.last_seen_monotonic, 10.0)


class _FakeClock:
    def __init__(self, step=0.01):
        self.value = -step
        self.step = step

    def __call__(self):
        self.value += self.step
        return self.value


class ClusterDriveLifecycleTests(unittest.TestCase):
    def _live_args(self, out_root: Path) -> list[str]:
        return [
            "--out-root",
            str(out_root),
            "--raw-root",
            str(out_root.parent / "raw-root"),
            "--require-mount",
            "/mnt/fixture",
            "--campaign",
            "fixture-drive",
            "--duration-seconds",
            "1",
            "--execute",
            "--confirm-driving-read-only",
            "--confirm-started-parked",
            "--confirm-no-other-diagnostics",
            "--pair",
            "6/14",
            "--conditions",
            "started parked; ordinary driving fixture; AlfaOBD closed",
            "--no-telemetry-publish",
        ]

    def test_raw_starts_before_reads_and_cleanup_restores_before_unlock(self):
        events = []
        clock = _FakeClock()
        watcher = mock.Mock()
        watcher.ignition_lost.return_value = False
        sock = mock.Mock()
        sock.close.side_effect = lambda: events.append("socket_close")
        watcher.close.side_effect = lambda: events.append("watcher_close")
        lock_handle = object()

        def positive(_sock, did, *, before_request=None):
            if before_request is not None:
                before_request()
            events.append(f"read_{did:04X}")
            data = "00 00" if did == 0x1000 else "00"
            return {
                "did": f"{did:04X}",
                "expected_data_length": drive.EXPECTED_DATA_LENGTHS[did],
                "request_hex": f"22 {did >> 8:02X} {did & 0xFF:02X}",
                "response_hex": f"62 {did >> 8:02X} {did & 0xFF:02X} {data}",
                "data_hex": data,
                "category": "positive",
                "negative_response_assignment": None,
                "transport_status": "POSITIVE",
                "transport_error": None,
                "attempt_started_epoch_us": 1,
                "request_call_epoch_us": 1,
                "attempt_completed_epoch_us": 2,
                "timestamp_authority": "fixture",
                "elapsed_ms": 0.1,
            }

        with tempfile.TemporaryDirectory() as directory:
            out_root = Path(directory) / "capture-root"

            class FakeRaw:
                def __init__(self, run_dir, *, path_guard=None):
                    self.run_dir = run_dir
                    self.manifest_path = run_dir / "manifest.jsonl"
                    self.wire_path = run_dir / "cluster_wire.jsonl"
                    self.command = ["candump", "-L", "can0"]

                def start(self):
                    events.append("raw_start")

                def wait_for_first_frame(self):
                    events.append("raw_ready")

                def assert_alive(self):
                    return None

                def checkpoint(self):
                    return None

                def stop_ingest(self):
                    events.append("raw_stop")
                    return {
                        "started": True,
                        "returncode": 0,
                        "forced": False,
                        "frames": 10,
                        "detected_socket_drops": 0,
                    }

                def finalize(self):
                    events.append("raw_finalize")
                    return {
                        "started": True,
                        "complete": True,
                        "returncode": 0,
                        "forced": False,
                        "chunks": 1,
                        "detected_socket_drops": 0,
                        "wire_request_counts": {
                            f"{did:04X}": 1 for did in drive.CLUSTER_DIDS
                        },
                        "wire_positive_counts": {
                            f"{did:04X}": 1 for did in drive.CLUSTER_DIDS
                        },
                    }

            with (
                mock.patch.object(
                    drive,
                    "preflight_live",
                    return_value=(123, ACTIVE_STATE, 60 * 1024**3),
                ) as preflight,
                mock.patch.object(
                    drive,
                    "require_writable_mount",
                    return_value=123,
                ),
                mock.patch.object(
                    drive.diagnostic_safety,
                    "acquire_channel_lock",
                    return_value=lock_handle,
                ),
                mock.patch.object(
                    drive.diagnostic_safety,
                    "release_channel_lock",
                    side_effect=lambda handle: events.append("unlock"),
                ) as release,
                mock.patch.object(drive, "RawCapture", FakeRaw),
                mock.patch.object(
                    drive, "IgnitionWatcher", return_value=watcher
                ),
                mock.patch.object(
                    drive.uds, "open_module_socket", return_value=sock
                ),
                mock.patch.object(drive, "query_did", side_effect=positive),
                mock.patch.object(
                    drive,
                    "query_interface_state",
                    side_effect=lambda: events.append("final_interface") or ACTIVE_STATE,
                ),
                mock.patch.object(
                    drive.canbus,
                    "restore_passive",
                    side_effect=lambda *_args: events.append("restore") or True,
                ),
                mock.patch.object(drive.time, "monotonic", side_effect=clock),
                mock.patch.object(drive.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                result = drive.main(self._live_args(out_root))

            summary = json.loads(
                (out_root / "fixture-drive" / "summary.json").read_text()
            )
            samples = [
                json.loads(line)
                for line in (out_root / "fixture-drive" / "samples.jsonl")
                .read_text()
                .splitlines()
            ]

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(preflight.call_count, 2)
        self.assertEqual(events[0], "raw_start")
        first_read = next(index for index, event in enumerate(events) if event.startswith("read_"))
        self.assertLess(events.index("raw_start"), first_read)
        self.assertLess(events.index("socket_close"), events.index("restore"))
        self.assertLess(events.index("raw_stop"), events.index("final_interface"))
        self.assertLess(events.index("final_interface"), events.index("restore"))
        self.assertLess(events.index("restore"), events.index("unlock"))
        self.assertLess(events.index("unlock"), events.index("raw_finalize"))
        release.assert_called_once_with(lock_handle)
        flattened = [
            int(record["did"], 16)
            for record in samples
            if record["type"] == "did_attempt"
        ]
        self.assertGreaterEqual(len(flattened), len(drive.CLUSTER_DIDS))
        self.assertEqual(flattened[:5], list(drive.CLUSTER_DIDS))
        self.assertEqual(summary["status"], "complete")
        self.assertTrue(summary["restored_passive"])
        self.assertTrue(summary["lock_released"])
        self.assertTrue(summary["startup_profile_validated"])
        self.assertTrue(summary["wire_cross_validation"]["complete"])
        self.assertEqual(summary["fatal_errors"], [])

    def test_missing_ignition_sends_no_did_and_still_restores(self):
        events = []
        watcher = mock.Mock()
        watcher.wait_for_first.side_effect = drive.DriveLogError("no ignition fixture")
        lock_handle = object()

        with tempfile.TemporaryDirectory() as directory:
            out_root = Path(directory) / "capture-root"

            class FakeRaw:
                def __init__(self, run_dir, *, path_guard=None):
                    self.run_dir = run_dir
                    self.manifest_path = run_dir / "manifest.jsonl"
                    self.wire_path = run_dir / "cluster_wire.jsonl"
                    self.command = ["candump"]

                def start(self):
                    events.append("raw_start")

                def wait_for_first_frame(self):
                    return None

                def stop_ingest(self):
                    events.append("raw_stop")
                    raise drive.DriveLogError("raw stop fixture")

                def finalize(self):
                    raise AssertionError("failed ingest must not finalize")

                def close_after_ingest_failure(self):
                    events.append("raw_close")
                    return []

            with (
                mock.patch.object(
                    drive,
                    "preflight_live",
                    return_value=(123, ACTIVE_STATE, 60 * 1024**3),
                ),
                mock.patch.object(
                    drive,
                    "require_writable_mount",
                    return_value=123,
                ),
                mock.patch.object(
                    drive.diagnostic_safety,
                    "acquire_channel_lock",
                    return_value=lock_handle,
                ),
                mock.patch.object(
                    drive.diagnostic_safety, "release_channel_lock"
                ),
                mock.patch.object(drive, "RawCapture", FakeRaw),
                mock.patch.object(
                    drive, "IgnitionWatcher", return_value=watcher
                ),
                mock.patch.object(drive.uds, "open_module_socket") as open_socket,
                mock.patch.object(drive, "query_did") as query,
                mock.patch.object(
                    drive, "query_interface_state", return_value=ACTIVE_STATE
                ),
                mock.patch.object(
                    drive.canbus, "restore_passive", return_value=True
                ) as restore,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = drive.main(self._live_args(out_root))

            summary = json.loads(
                (out_root / "fixture-drive" / "summary.json").read_text()
            )

        self.assertEqual(result, 1)
        open_socket.assert_not_called()
        query.assert_not_called()
        restore.assert_called_once_with("can0", 500_000)
        self.assertEqual(events, ["raw_start", "raw_stop", "raw_close"])
        self.assertEqual(summary["status"], "failed")
        self.assertTrue(summary["restored_passive"])
        self.assertTrue(any("no ignition fixture" in item for item in summary["fatal_errors"]))
        self.assertTrue(any("raw stop fixture" in item for item in summary["fatal_errors"]))

    def test_interruption_during_inflight_request_is_graceful(self):
        events = []
        watcher = mock.Mock()
        watcher.ignition_lost.return_value = False
        lock_handle = object()

        class FakeRaw:
            def __init__(self, run_dir, *, path_guard=None):
                self.run_dir = run_dir
                self.manifest_path = run_dir / "manifest.jsonl"
                self.wire_path = run_dir / "cluster_wire.jsonl"
                self.command = ["candump"]

            def start(self):
                events.append("raw_start")

            def wait_for_first_frame(self):
                return None

            def assert_alive(self):
                return None

            def checkpoint(self):
                return None

            def stop_ingest(self):
                events.append("raw_stop")
                return {
                    "started": True,
                    "returncode": 0,
                    "forced": False,
                    "frames": 2,
                    "detected_socket_drops": 0,
                }

            def finalize(self):
                events.append("raw_finalize")
                return {
                    "started": True,
                    "complete": True,
                    "chunks": 1,
                    "detected_socket_drops": 0,
                    "wire_request_counts": {"1000": 1},
                    "wire_positive_counts": {},
                    "wire_negative_responses": 0,
                    "wire_other_endpoint_frames": 0,
                }

        def interrupt(_sock, _did, *, before_request=None):
            if before_request is not None:
                before_request()
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            out_root = Path(directory) / "capture-root"
            with (
                mock.patch.object(
                    drive,
                    "preflight_live",
                    return_value=(123, ACTIVE_STATE, 60 * 1024**3),
                ),
                mock.patch.object(
                    drive,
                    "require_writable_mount",
                    return_value=123,
                ),
                mock.patch.object(
                    drive.diagnostic_safety,
                    "acquire_channel_lock",
                    return_value=lock_handle,
                ),
                mock.patch.object(
                    drive.diagnostic_safety,
                    "release_channel_lock",
                    side_effect=lambda _handle: events.append("unlock"),
                ),
                mock.patch.object(drive, "RawCapture", FakeRaw),
                mock.patch.object(drive, "IgnitionWatcher", return_value=watcher),
                mock.patch.object(drive.uds, "open_module_socket", return_value=mock.Mock()),
                mock.patch.object(drive, "query_did", side_effect=interrupt),
                mock.patch.object(
                    drive, "query_interface_state", return_value=ACTIVE_STATE
                ),
                mock.patch.object(
                    drive.canbus,
                    "restore_passive",
                    side_effect=lambda *_args: events.append("restore") or True,
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                result = drive.main(self._live_args(out_root))

            summary = json.loads(
                (out_root / "fixture-drive" / "summary.json").read_text()
            )

        self.assertEqual(result, 130, stderr.getvalue())
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(summary["inflight_did_at_stop"], "1000")
        self.assertTrue(summary["wire_cross_validation"]["complete"])
        self.assertEqual(summary["fatal_errors"], [])
        self.assertLess(events.index("raw_stop"), events.index("restore"))
        self.assertLess(events.index("restore"), events.index("unlock"))
        self.assertLess(events.index("unlock"), events.index("raw_finalize"))


if __name__ == "__main__":
    unittest.main()
