import argparse
import contextlib
import errno
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data import drive_recorder
from tools import passive_drive_capture as capture


TEST_CHANNEL = "can7"
TEST_SERIAL = "serial-a"
TEST_B_CHANNEL = "can8"
TEST_B_SERIAL = "serial-a"
TEST_H_CHANNEL = "can9"
TEST_H_SERIAL = "serial-b"


def ready_status():
    return {
        "service": "van-telemetry",
        "active_drive": {
            "enabled": True,
            "state": "armed_diagnostic",
            "reason": "running_gate_satisfied",
            "interface_mode": "armed_diagnostic",
            "restoration_failed": False,
            "helper_pid": 1234,
        },
        "current_owner": {"kind": "broker_active_drive"},
        "interface": {
            "channel": TEST_CHANNEL,
            "adapter_present": True,
            "up": True,
            "bitrate": 500000,
            "listen_only": False,
            "controller_state": "ERROR-ACTIVE",
            "active_inhibits": [],
            "topology": {
                "usable": True,
                "bus": "c-can",
                "pair": "6/14",
            },
            "role_interfaces": {
                "roles": {
                    "c-can": {
                        "channel": TEST_CHANNEL,
                        "expected": {
                            "usb_serial": TEST_SERIAL,
                            "dev_id": 0,
                        },
                    },
                    "b-can": {
                        "resolution": "resolved",
                        "channel": TEST_B_CHANNEL,
                        "passive_ready": True,
                        "safe": True,
                        "expected": {
                            "usb_serial": TEST_B_SERIAL,
                            "dev_id": 1,
                            "bitrate": 125000,
                            "pair": "3/11",
                        },
                        "actual": {
                            "present": True,
                            "up": True,
                            "bitrate": 125000,
                            "fd_enabled": False,
                            "one_shot": False,
                            "listen_only": True,
                            "controller_state": "ERROR-ACTIVE",
                            "restart_ms": 0,
                        },
                    },
                    "can-ch": {
                        "resolution": "resolved",
                        "channel": TEST_H_CHANNEL,
                        "passive_ready": True,
                        "safe": True,
                        "expected": {
                            "usb_serial": TEST_H_SERIAL,
                            "dev_id": 0,
                            "bitrate": 500000,
                            "pair": "12/13",
                        },
                        "actual": {
                            "present": True,
                            "up": True,
                            "bitrate": 500000,
                            "fd_enabled": False,
                            "one_shot": False,
                            "listen_only": True,
                            "controller_state": "ERROR-ACTIVE",
                            "restart_ms": 0,
                        },
                    },
                }
            },
        },
        "vehicle_state": {
            "running": True,
            "basis": "qualified_ccan_0x0fc_engine_speed",
        },
    }


def auxiliary_ready_status():
    status = ready_status()
    status["auxiliary_drive"] = {
        "enabled": True,
        "state": "armed_diagnostic",
        "reason": "running_gate_satisfied",
        "interface_mode": "armed_diagnostic",
        "restoration_failed": False,
        "helper_pid": 2345,
    }
    bcan = status["interface"]["role_interfaces"]["roles"]["b-can"]
    bcan["passive_ready"] = False
    bcan["operating_mode"] = "armed_diagnostic"
    bcan["armed_owner"] = "broker_auxiliary_drive"
    bcan["topology_usable"] = True
    bcan["actual"]["listen_only"] = False
    status["current_owner"]["roles"] = ["c-can", "b-can"]
    return status


def interface(*, listen_only, bitrate=500000):
    return capture.InterfaceState(
        up=True,
        bitrate=bitrate,
        listen_only=listen_only,
        controller_state="ERROR-ACTIVE",
        rx_dropped=0,
        rx_missed=0,
    )


class DriveRecorderTests(unittest.TestCase):
    def test_ready_status_requires_exact_broker_owner_and_topology(self):
        self.assertTrue(drive_recorder.broker_armed_ready(ready_status()))
        for mutate in (
            lambda value: value["active_drive"].update(state="idle"),
            lambda value: value["current_owner"].update(kind="broker"),
            lambda value: value["interface"].update(listen_only=True),
            lambda value: value["interface"]["topology"].update(pair="3/11"),
            lambda value: value["vehicle_state"].update(running=False),
        ):
            value = ready_status()
            mutate(value)
            self.assertFalse(drive_recorder.broker_armed_ready(value))

        wrong_channel = ready_status()
        wrong_channel["interface"]["channel"] = "c-can"
        self.assertFalse(drive_recorder.broker_armed_ready(wrong_channel))

    def test_broker_route_preserves_dynamic_channel_and_usb_identity(self):
        status = ready_status()
        self.assertEqual(
            drive_recorder.broker_c_can_route(status),
            (TEST_CHANNEL, TEST_SERIAL, 0),
        )

    def test_secondary_routes_require_exact_passive_broker_evidence(self):
        status = ready_status()
        route = drive_recorder.broker_secondary_route(status, "b-can")
        self.assertEqual(route.channel, TEST_B_CHANNEL)
        self.assertEqual(route.bitrate, 125000)
        self.assertEqual(route.pair, "3/11")

        status["interface"]["role_interfaces"]["roles"]["b-can"][
            "actual"
        ]["listen_only"] = False
        with self.assertRaisesRegex(
            drive_recorder.BrokerOwnershipLost, "passive or auxiliary-owned b-can"
        ):
            drive_recorder.broker_secondary_route(status, "b-can")

    def test_bcan_route_accepts_only_exact_broker_auxiliary_owner(self):
        status = auxiliary_ready_status()
        self.assertTrue(drive_recorder.broker_armed_ready(status))
        route = drive_recorder.broker_secondary_route(status, "b-can")
        self.assertEqual(route.ownership, "broker_auxiliary_drive_companion")
        self.assertEqual(route.channel, TEST_B_CHANNEL)

        status["auxiliary_drive"]["helper_pid"] = None
        with self.assertRaisesRegex(
            drive_recorder.BrokerOwnershipLost, "auxiliary-owned"
        ):
            drive_recorder.broker_secondary_route(status, "b-can")

    def test_auxiliary_safety_accepts_broker_owned_armed_then_passive(self):
        status = auxiliary_ready_status()
        client = mock.Mock()
        client.request.return_value = (200, status)
        route = drive_recorder.broker_secondary_route(status, "b-can")
        states = iter(
            (
                interface(listen_only=False, bitrate=125000),
                interface(listen_only=True, bitrate=125000),
            )
        )
        check = drive_recorder.AuxiliaryBcanSafetyCheck(
            client,
            route,
            require_initial_armed=True,
            interface_reader=lambda: next(states),
        )

        self.assertFalse(check().listen_only)
        self.assertTrue(check().listen_only)

    def test_interface_query_rejects_reused_channel_usb_identity(self):
        resolver = mock.Mock()
        resolver.inventory.return_value = (
            (
                SimpleNamespace(
                    channel="can7",
                    usb_vid="1d50",
                    usb_pid="606f",
                    usb_serial="other-board",
                    dev_id=0,
                ),
            ),
            (),
        )

        with self.assertRaisesRegex(
            drive_recorder.DriveRecorderError, "no longer matches"
        ):
            drive_recorder.query_interface(
                channel="can7",
                expected_usb_serial="serial-a",
                expected_dev_id=0,
                role_resolver=resolver,
                runner=mock.Mock(),
            )

    def test_interface_query_parses_resolved_non_can0_channel(self):
        details = """\
7: can7: <NOARP,UP,LOWER_UP,ECHO> mtu 16 state UP mode DEFAULT
    link/can
    can state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 0
          bitrate 500000 sample-point 0.875
    RX:  bytes packets errors dropped  missed   mcast
            4096      64      0       0       0       0
"""
        runner = mock.Mock(
            return_value=SimpleNamespace(returncode=0, stdout=details, stderr="")
        )

        state = drive_recorder.query_interface(
            channel="can7",
            runner=runner,
        )

        self.assertTrue(state.up)
        self.assertEqual(state.controller_state, "ERROR-ACTIVE")
        self.assertEqual(runner.call_args.args[0][-1], "can7")

    def test_interface_query_rejects_duplicate_usb_identity(self):
        resolver = mock.Mock()
        resolver.inventory.return_value = (
            tuple(
                SimpleNamespace(
                    channel=channel,
                    usb_vid="1d50",
                    usb_pid="606f",
                    usb_serial="serial-a",
                    dev_id=0,
                )
                for channel in ("can7", "can8")
            ),
            (),
        )

        with self.assertRaisesRegex(
            drive_recorder.DriveRecorderError, "no longer matches"
        ):
            drive_recorder.query_interface(
                channel="can7",
                expected_usb_serial="serial-a",
                expected_dev_id=0,
                role_resolver=resolver,
                runner=mock.Mock(),
            )

    def test_coordinated_safety_accepts_passive_without_broker_request(self):
        client = mock.Mock()
        check = drive_recorder.CoordinatedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: interface(listen_only=True),
        )
        self.assertTrue(check().listen_only)
        client.request.assert_not_called()

    def test_coordinated_safety_accepts_only_broker_owned_armed_state(self):
        client = mock.Mock()
        client.request.return_value = (200, ready_status())
        check = drive_recorder.CoordinatedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: interface(listen_only=False),
        )
        self.assertFalse(check().listen_only)

        blocked = ready_status()
        blocked["current_owner"] = {"kind": "external_inhibit"}
        client.request.return_value = (200, blocked)
        with self.assertRaisesRegex(
            drive_recorder.DriveRecorderError,
            "not owned by the reviewed broker",
        ):
            check()

    def test_armed_status_retry_recovers_transient_eagain(self):
        client = mock.Mock()
        client.request.side_effect = (
            BlockingIOError(errno.EAGAIN, "temporarily unavailable"),
            (200, ready_status()),
        )
        clock = [10.0]
        sleeps = []

        def advance(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        check = drive_recorder.CoordinatedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: interface(listen_only=False),
            status_sleep=advance,
            status_monotonic=lambda: clock[0],
        )

        self.assertFalse(check().listen_only)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(sleeps, [0.05])

    def test_status_retry_classifies_only_observed_transient_errors(self):
        self.assertTrue(
            drive_recorder._transient_broker_status_error(TimeoutError("timed out"))
        )
        self.assertTrue(
            drive_recorder._transient_broker_status_error(
                BlockingIOError(errno.EAGAIN, "temporarily unavailable")
            )
        )
        self.assertFalse(
            drive_recorder._transient_broker_status_error(
                ConnectionRefusedError("broker absent")
            )
        )

    def test_armed_status_retry_accepts_exact_passive_recovery(self):
        client = mock.Mock()
        client.request.side_effect = BlockingIOError(
            errno.EAGAIN, "temporarily unavailable"
        )
        states = iter(
            (
                interface(listen_only=False),
                interface(listen_only=True),
            )
        )
        check = drive_recorder.CoordinatedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: next(states),
            status_sleep=mock.Mock(),
        )

        self.assertTrue(check().listen_only)
        self.assertEqual(client.request.call_count, 1)

    def test_armed_status_retry_exhaustion_is_bounded_ownership_loss(self):
        client = mock.Mock()
        client.request.side_effect = BlockingIOError(
            errno.EAGAIN, "temporarily unavailable"
        )
        clock = [20.0]

        def advance(seconds):
            clock[0] += seconds

        check = drive_recorder.CoordinatedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: interface(listen_only=False),
            status_sleep=advance,
            status_monotonic=lambda: clock[0],
        )

        with self.assertRaisesRegex(
            drive_recorder.BrokerOwnershipLost,
            r"attempts=5.*BlockingIOError",
        ):
            check()
        self.assertEqual(
            client.request.call_count,
            drive_recorder.BROKER_STATUS_RETRY_ATTEMPTS,
        )

    def test_timeout_retries_stop_at_deadline_without_using_all_attempts(self):
        clock = [30.0]
        client = mock.Mock()

        def timeout(*_args, **_kwargs):
            clock[0] += 2.0
            raise TimeoutError("timed out")

        client.request.side_effect = timeout
        check = drive_recorder.CoordinatedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: interface(listen_only=False),
            status_sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            status_monotonic=lambda: clock[0],
        )

        with self.assertRaisesRegex(
            drive_recorder.BrokerOwnershipLost,
            r"attempts=3/5.*elapsed=6\.150s.*TimeoutError",
        ):
            check()
        self.assertEqual(client.request.call_count, 3)

    def test_armed_status_retry_does_not_retry_nontransient_error(self):
        client = mock.Mock()
        client.request.side_effect = ConnectionRefusedError("broker absent")
        check = drive_recorder.CoordinatedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: interface(listen_only=False),
        )

        with self.assertRaisesRegex(
            drive_recorder.BrokerOwnershipLost,
            "non-transient.*ConnectionRefusedError",
        ):
            check()
        self.assertEqual(client.request.call_count, 1)

    def test_initial_gate_requires_armed_once_then_accepts_restoration(self):
        client = mock.Mock()
        client.request.return_value = (200, ready_status())
        states = iter(
            (
                interface(listen_only=False),
                interface(listen_only=True),
            )
        )
        check = drive_recorder.InitialArmedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: next(states),
        )
        self.assertFalse(check().listen_only)
        self.assertTrue(check().listen_only)
        self.assertEqual(client.request.call_count, 1)

        blocked = drive_recorder.InitialArmedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: interface(listen_only=True),
        )
        with self.assertRaises(drive_recorder.BrokerOwnershipLost):
            blocked()

    def test_initial_gate_rejects_passive_recovery_before_first_armed_proof(self):
        client = mock.Mock()
        client.request.side_effect = BlockingIOError(
            errno.EAGAIN, "temporarily unavailable"
        )
        states = iter(
            (
                interface(listen_only=False),
                interface(listen_only=True),
            )
        )
        check = drive_recorder.InitialArmedSafetyCheck(
            client,
            channel=TEST_CHANNEL,
            interface_reader=lambda: next(states),
            status_sleep=mock.Mock(),
        )

        with self.assertRaisesRegex(
            drive_recorder.BrokerOwnershipLost,
            "disappeared during recorder startup",
        ):
            check()

    def test_daemon_waits_after_ownership_loss_instead_of_crashing(self):
        client = mock.Mock()
        client.request.return_value = (200, ready_status())
        args = SimpleNamespace(
            socket="/unused.sock",
            state_path=Path("/unused-state.json"),
            out_root=Path("/unused-output"),
        )
        sleep = mock.Mock(side_effect=KeyboardInterrupt)
        with (
            mock.patch.object(
                drive_recorder,
                "record_one_interval",
                side_effect=drive_recorder.BrokerOwnershipLost(
                    "bounded broker-status attribution exhausted"
                ),
            ) as record,
            mock.patch.object(drive_recorder, "write_state") as write_state,
            self.assertRaises(KeyboardInterrupt),
        ):
            drive_recorder.run_daemon(
                args,
                capture.DiskPolicy(300, 200),
                client=client,
                sleep=sleep,
            )

        record.assert_called_once()
        sleep.assert_called_once_with(drive_recorder.WAIT_SECONDS)
        self.assertEqual(write_state.call_args.kwargs["status"], "waiting")

    def test_daemon_recovers_ownership_loss_through_real_capture_cleanup(self):
        """Exercise Recorder.run, including the final error propagation boundary."""
        for failure, recoverable, cleanup_error in (
            (drive_recorder.BrokerOwnershipLost("broker ownership disappeared"), True, None),
            (capture.CaptureError("compressor failed"), False, None),
            (OSError("disk unavailable"), False, None),
            (drive_recorder.BrokerOwnershipLost("broker ownership disappeared"), False,
             capture.CaptureError("final compressor validation failed")),
        ):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                client = mock.Mock()
                client.request.return_value = (200, ready_status())
                process = mock.Mock()
                process.stdout.fileno.return_value = 91
                process.poll.return_value = None
                chunk = mock.Mock()
                chunk.sequence = 0
                chunk.finish.return_value = {
                    "type": "chunk", "sequence": 0, "streams": {}, "complete": True,
                }
                chunk.finish.side_effect = cleanup_error
                recorder = capture.Recorder(
                    root, frozenset(), 600, 120, capture.DiskPolicy(300, 200),
                    channel=TEST_CHANNEL, bitrate=500000,
                    popen=lambda *_args, **_kwargs: process,
                    disk_free=lambda _path: 1000,
                    safety_check=lambda: interface(listen_only=False),
                    mount_check=lambda: None,
                    health_check=mock.Mock(side_effect=failure),
                    install_signal_handlers=False,
                )
                args = SimpleNamespace(socket="/unused.sock", state_path=root / "state.json",
                                       out_root=root)
                with (
                    mock.patch.object(capture, "Chunk", return_value=chunk),
                    mock.patch.object(capture.os, "set_blocking"),
                    mock.patch.object(capture.selectors, "DefaultSelector"),
                    mock.patch.object(recorder, "_stop_process"),
                    mock.patch.object(drive_recorder, "record_one_interval",
                                      side_effect=lambda *_args: recorder.run()),
                ):
                    expected = KeyboardInterrupt if recoverable else drive_recorder.DriveRecorderError
                    with self.assertRaises(expected):
                        drive_recorder.run_daemon(
                            args, capture.DiskPolicy(300, 200), client=client,
                            sleep=mock.Mock(side_effect=KeyboardInterrupt),
                        )
                self.assertEqual(json.loads(args.state_path.read_text())["status"],
                                 "waiting" if recoverable else "error")
                checkpoint = json.loads((root / "checkpoint.json").read_text())
                self.assertFalse(checkpoint["success"])
                self.assertEqual(checkpoint["error"], str(cleanup_error or failure))
                chunk.finish.assert_called_once()

    def test_validate_args_requires_explicit_execute_confirmation(self):
        parser = drive_recorder.build_parser()
        args = parser.parse_args(["--execute"])
        with self.assertRaisesRegex(
            drive_recorder.DriveRecorderError,
            "requires --confirm-broker-owned-receive-only",
        ):
            drive_recorder.validate_args(args)
        args = parser.parse_args(
            ["--execute", "--confirm-broker-owned-receive-only"]
        )
        policy = drive_recorder.validate_args(args)
        self.assertEqual(policy.hard_free_bytes, 25 * 1024**3)

    def test_plan_is_receive_only_and_has_storage_floors(self):
        args = drive_recorder.build_parser().parse_args([])
        policy = drive_recorder.validate_args(args)
        plan = drive_recorder.plan(args, policy)
        self.assertEqual(
            plan["interaction"],
            "synchronized_three_bus_receive_only_companion",
        )
        self.assertIn("transmit CAN", plan["does_not"])
        self.assertEqual(plan["stop_after_id"], "0x2EF")
        self.assertEqual(plan["roles"], ["c-can", "b-can", "can-ch"])

    def test_dependency_gate_accepts_armed_receive_state(self):
        state, free = drive_recorder.validate_dependencies(
            Path("/unused"),
            capture.DiskPolicy(300, 200),
            lambda: interface(listen_only=False),
            which=lambda name: f"/usr/bin/{name}",
            disk_free=lambda _path: 1000,
            rmem_max=lambda: capture.RECEIVE_BUFFER,
        )
        self.assertFalse(state.listen_only)
        self.assertEqual(free, 1000)

    def test_interval_binds_recorder_to_resolved_channel_and_bitrate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                out_root=root,
                require_mount=root,
                state_path=root / "state.json",
                conditions="receive-only fixture",
                rotation_seconds=600,
                duration_seconds=3600,
                ignition_absence_seconds=20.0,
            )
            calls = []

            class FakeRecorder:
                def __init__(self, run_dir, *args, **kwargs):
                    self.run_dir = run_dir
                    self.kwargs = kwargs
                    calls.append(self)

                def run(self):
                    callback = self.kwargs.get("started_callback")
                    if callback is not None:
                        callback()
                        stop = self.kwargs["external_stop_requested"]
                        while not stop():
                            drive_recorder.time.sleep(0.001)
                    return 0

            client = mock.Mock()
            manager = mock.Mock()
            leases = {
                "b-can": drive_recorder.PassiveInterfaceLease(
                    role="b-can",
                    channel=TEST_B_CHANNEL,
                    usb_serial=TEST_B_SERIAL,
                    dev_id=1,
                    bitrate=125000,
                    pair="3/11",
                    topology_generation="test-generation",
                ),
                "can-ch": drive_recorder.PassiveInterfaceLease(
                    role="can-ch",
                    channel=TEST_H_CHANNEL,
                    usb_serial=TEST_H_SERIAL,
                    dev_id=0,
                    bitrate=500000,
                    pair="12/13",
                    topology_generation="test-generation",
                ),
            }
            manager.observe.side_effect = lambda role: contextlib.nullcontext(
                leases[role]
            )
            with (
                mock.patch.object(
                    drive_recorder.capture,
                    "require_writable_mount",
                    return_value=Path("/dev/mock"),
                ),
                mock.patch.object(
                    drive_recorder,
                    "validate_dependencies",
                    return_value=(interface(listen_only=False), 1000),
                ),
                mock.patch.object(
                    drive_recorder,
                    "read_broker_status",
                    return_value=ready_status(),
                ),
                mock.patch.object(
                    drive_recorder,
                    "campaign_id",
                    return_value="drive-test",
                ),
                mock.patch.object(
                    drive_recorder,
                    "priority_ids",
                    return_value=frozenset((0x2EF,)),
                ),
                mock.patch.object(drive_recorder, "write_state"),
                mock.patch.object(
                    drive_recorder.capture,
                    "read_rmem_max",
                    return_value=capture.RECEIVE_BUFFER,
                ),
                mock.patch.object(
                    drive_recorder.capture,
                    "Recorder",
                    side_effect=FakeRecorder,
                ) as recorder_class,
                mock.patch.object(
                    drive_recorder,
                    "PassiveLeaseSafetyCheck",
                    side_effect=lambda _manager, lease: (
                        lambda: interface(
                            listen_only=True, bitrate=lease.bitrate
                        )
                    ),
                ),
                mock.patch.object(
                    drive_recorder.capture,
                    "campaign_file_lock",
                    return_value=contextlib.nullcontext(),
                ),
                mock.patch.object(
                    drive_recorder.shutil,
                    "which",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
            ):
                result = drive_recorder.record_one_interval(
                    args,
                    capture.DiskPolicy(300, 200),
                    ready_status(),
                    client,
                    interface_manager=manager,
                )

        self.assertEqual(result.name, "drive-test")
        self.assertEqual(recorder_class.call_count, 3)
        by_role = {item.run_dir.name: item for item in calls}
        self.assertEqual(set(by_role), {"c-can", "b-can", "can-ch"})
        self.assertEqual(by_role["c-can"].kwargs["channel"], TEST_CHANNEL)
        self.assertEqual(
            by_role["c-can"].kwargs["bitrate"], drive_recorder.BITRATE
        )
        self.assertEqual(by_role["b-can"].kwargs["channel"], TEST_B_CHANNEL)
        self.assertEqual(by_role["b-can"].kwargs["bitrate"], 125000)
        self.assertEqual(by_role["can-ch"].kwargs["channel"], TEST_H_CHANNEL)
        self.assertEqual(by_role["can-ch"].kwargs["bitrate"], 500000)
        self.assertEqual(
            by_role["b-can"].kwargs["required_start_id"], 0x46C
        )
        self.assertEqual(
            by_role["can-ch"].kwargs["required_start_id"], 0x0DA
        )


if __name__ == "__main__":
    unittest.main()


broker_armed_ready = drive_recorder.broker_armed_ready


def _refresh_during_armed_interval(controller_state="ERROR-ACTIVE"):
    """The scheduled voltage_mon acquisition refreshes interface status mid-drive
    (2026-09-06 diagnosis; recorder stopped at 18:00 and 20:00 on 2026-09-24)."""
    import copy
    from types import SimpleNamespace
    from unittest import mock

    from projects.vehicle_data.broker import TelemetryBroker
    from projects.vehicle_data.can_runtime import RoleAwareVoltageAcquirer
    from projects.vehicle_data.models import failure

    seed = ready_status()
    roles = copy.deepcopy(seed["interface"]["role_interfaces"])
    ccan = roles["roles"]["c-can"]
    ccan.update(resolution="resolved", passive_ready=False, safe=False,
                reason="interface_armed", detail="can7 is not listen-only")
    ccan["actual"] = {
        "present": True, "up": True, "bitrate": 500000,
        "fd_enabled": False, "one_shot": False, "listen_only": False,
        "controller_state": controller_state, "restart_ms": 0,
    }
    manager = SimpleNamespace(
        status_snapshot=lambda: copy.deepcopy(roles),
        channel_for_bus=lambda _bus: "can7",
    )
    source = RoleAwareVoltageAcquirer(manager, inhibit_reader=lambda _channel: [])
    broker = TelemetryBroker(acquirer=source, monotonic=lambda: 100.0)
    broker._interface_status = copy.deepcopy(seed["interface"])
    broker._active_drive.update(seed["active_drive"])
    broker._vehicle_state = copy.deepcopy(seed["vehicle_state"])
    broker._vehicle_state_observed_monotonic = 100.0
    assert broker_armed_ready(broker.status_response())
    with mock.patch.object(source, "acquire", return_value=failure(
        metric="battery.voltage", unit="V", reason="can_busy",
        detail="B-CAN is already owned", bus="b-can", acquisition="passive",
    )):
        broker.acquire("battery.voltage", "wake_if_asleep")
    return broker.status_response()


def test_scheduled_status_refresh_keeps_the_recorder_admitted():
    after = _refresh_during_armed_interval()
    assert after["current_owner"]["kind"] == "broker_active_drive"
    assert after["interface"]["listen_only"] is False
    assert after["interface"]["topology"]["usable"] is True
    assert broker_armed_ready(after) is True


def test_refresh_with_an_unhealthy_controller_still_rejects():
    after = _refresh_during_armed_interval(controller_state="ERROR-PASSIVE")
    assert broker_armed_ready(after) is False


def _bcan_refresh_during_armed_interval(*, auxiliary_armed=True, **bcan_actual):
    """The 2026-09-27 22:00Z voltage_mon refresh, B-CAN side.

    The broker's pre-arm snapshot reports B-CAN ``reason=ready``/``safe=true``.
    The scheduled acquisition re-probes every role while the helpers own their
    channels: an auxiliary-armed B-CAN comes back ``interface_armed`` with
    ``safe=false`` (the passive-observer contract), which the recorder's route
    check used to require.
    """
    import copy
    from types import SimpleNamespace
    from unittest import mock

    from projects.vehicle_data.broker import TelemetryBroker
    from projects.vehicle_data.can_runtime import RoleAwareVoltageAcquirer
    from projects.vehicle_data.models import failure

    seed = auxiliary_ready_status() if auxiliary_armed else ready_status()
    seed_bcan = seed["interface"]["role_interfaces"]["roles"]["b-can"]
    # What the broker actually retains from its last listen-only probe.
    seed_bcan.update(reason="ready", passive_ready=True, safe=True)
    seed_bcan["actual"]["listen_only"] = True
    for key in ("operating_mode", "armed_owner", "topology_usable"):
        # Status-overlay fields; the retained manager snapshot never has them.
        seed_bcan.pop(key, None)
    roles = copy.deepcopy(seed["interface"]["role_interfaces"])
    ccan = roles["roles"]["c-can"]
    ccan.update(resolution="resolved", passive_ready=False, safe=False,
                reason="interface_armed", detail="can7 is not listen-only")
    ccan["actual"] = {
        "present": True, "up": True, "bitrate": 500000,
        "fd_enabled": False, "one_shot": False, "listen_only": False,
        "controller_state": "ERROR-ACTIVE", "restart_ms": 0,
    }
    bcan = roles["roles"]["b-can"]
    if auxiliary_armed:
        bcan.update(passive_ready=False, safe=False, reason="interface_armed",
                    detail="can8 is not listen-only")
        bcan["actual"]["listen_only"] = False
    bcan["actual"].update(bcan_actual)
    manager = SimpleNamespace(
        status_snapshot=lambda: copy.deepcopy(roles),
        channel_for_bus=lambda _bus: "can7",
    )
    source = RoleAwareVoltageAcquirer(manager, inhibit_reader=lambda _channel: [])
    broker = TelemetryBroker(
        acquirer=source,
        monotonic=lambda: 100.0,
        auxiliary_drive_supervisor=SimpleNamespace(stop=lambda: None),
        auxiliary_drive_enabled=True,
    )
    broker._interface_status = copy.deepcopy(seed["interface"])
    broker._active_drive.update(seed["active_drive"])
    if auxiliary_armed:
        broker._auxiliary_drive.update(seed["auxiliary_drive"])
        broker._auxiliary_drive["owner_route"] = {
            "channel": TEST_B_CHANNEL, "usb_serial": TEST_B_SERIAL, "dev_id": 1,
        }
    else:
        broker._auxiliary_drive.update(
            state="blocked_until_engine_stop", reason="response_timeout",
            interface_mode="listen_only",
        )
    broker._vehicle_state = copy.deepcopy(seed["vehicle_state"])
    broker._vehicle_state_observed_monotonic = 100.0
    before = broker.status_response()
    assert broker_armed_ready(before)
    before_route = drive_recorder.broker_secondary_route(before, "b-can")
    with mock.patch.object(source, "acquire", return_value=failure(
        metric="battery.voltage", unit="V", reason="can_busy",
        detail="C-CAN is already owned", bus="c-can", acquisition="passive",
    )):
        broker.acquire("battery.voltage", "wake_if_asleep")
    return before_route, broker.status_response()


def test_bcan_auxiliary_route_survives_the_scheduled_refresh():
    before_route, after = _bcan_refresh_during_armed_interval()
    assert before_route.ownership == "broker_auxiliary_drive_companion"
    bcan = after["interface"]["role_interfaces"]["roles"]["b-can"]
    # The passive-observer contract is untouched: armed B-CAN is not "safe".
    assert bcan["safe"] is False and bcan["passive_ready"] is False
    assert bcan["armed_owner"] == "broker_auxiliary_drive"
    assert broker_armed_ready(after) is True
    assert drive_recorder.broker_secondary_route(after, "b-can") == before_route


def test_bcan_passive_route_survives_the_scheduled_refresh():
    # Auxiliary helper blocked/idle: B-CAN stays listen-only on the shared lease.
    before_route, after = _bcan_refresh_during_armed_interval(auxiliary_armed=False)
    assert before_route.ownership == "shared_passive_observer"
    assert broker_armed_ready(after) is True
    assert drive_recorder.broker_secondary_route(after, "b-can") == before_route


def _assert_bcan_rejected(status):
    import pytest

    with pytest.raises(drive_recorder.BrokerOwnershipLost, match="auxiliary-owned b-can"):
        drive_recorder.broker_secondary_route(status, "b-can")


def test_bcan_refresh_still_rejects_unverified_armed_routes():
    for fault in (
        {"controller_state": "ERROR-PASSIVE"},
        {"controller_state": "BUS-OFF"},
        {"bitrate": 500000},
        {"fd_enabled": True},
        {"one_shot": True},
        {"restart_ms": 100},
        {"up": False},
    ):
        _before, after = _bcan_refresh_during_armed_interval(**fault)
        bcan = after["interface"]["role_interfaces"]["roles"]["b-can"]
        assert bcan.get("armed_owner") is None, fault
        assert bcan["topology_usable"] is False, fault
        _assert_bcan_rejected(after)


def test_bcan_armed_route_requires_the_broker_verified_owner_markers():
    status = auxiliary_ready_status()
    bcan = status["interface"]["role_interfaces"]["roles"]["b-can"]
    # A stale safe/passive bit can never stand in for the owner proof.
    bcan["safe"] = True
    for key, value in (("armed_owner", None), ("topology_usable", False)):
        faulty = __import__("copy").deepcopy(status)
        faulty["interface"]["role_interfaces"]["roles"]["b-can"][key] = value
        _assert_bcan_rejected(faulty)
    # An armed B-CAN with no auxiliary owner is still never passive.
    orphan = __import__("copy").deepcopy(status)
    del orphan["auxiliary_drive"]
    _assert_bcan_rejected(orphan)
    # Identity drift after the refresh is rejected by the broker overlay.
    wrong_identity = auxiliary_ready_status()
    wrong_identity["interface"]["role_interfaces"]["roles"]["b-can"][
        "channel"
    ] = "can9"
    route = drive_recorder.broker_secondary_route(auxiliary_ready_status(), "b-can")
    assert drive_recorder.broker_secondary_route(wrong_identity, "b-can") != route


def test_bcan_refresh_rejects_a_foreign_owner_route_or_restoration_latch():
    import copy
    from types import SimpleNamespace
    from unittest import mock

    from projects.vehicle_data.broker import TelemetryBroker
    from projects.vehicle_data.can_runtime import RoleAwareVoltageAcquirer
    from projects.vehicle_data.models import failure

    for variant in ("foreign_owner", "latched", "inhibit"):
        seed = auxiliary_ready_status()
        roles = copy.deepcopy(seed["interface"]["role_interfaces"])
        roles["roles"]["c-can"].update(
            resolution="resolved", passive_ready=False, safe=False,
            reason="interface_armed",
            actual={"present": True, "up": True, "bitrate": 500000,
                    "fd_enabled": False, "one_shot": False,
                    "listen_only": False, "controller_state": "ERROR-ACTIVE",
                    "restart_ms": 0},
        )
        roles["roles"]["b-can"].update(passive_ready=False, safe=False,
                                       reason="interface_armed")
        for key in ("armed_owner", "topology_usable", "operating_mode"):
            roles["roles"]["b-can"].pop(key, None)
        manager = SimpleNamespace(status_snapshot=lambda: copy.deepcopy(roles),
                                  channel_for_bus=lambda _bus: "can7")
        inhibits = ["other-tool"] if variant == "inhibit" else []
        source = RoleAwareVoltageAcquirer(
            manager, inhibit_reader=lambda _channel: [{"name": n} for n in inhibits]
        )
        broker = TelemetryBroker(
            acquirer=source, monotonic=lambda: 100.0,
            auxiliary_drive_supervisor=SimpleNamespace(stop=lambda: None),
            auxiliary_drive_enabled=True,
        )
        broker._active_drive.update(seed["active_drive"])
        broker._auxiliary_drive.update(seed["auxiliary_drive"])
        broker._auxiliary_drive["owner_route"] = {
            "channel": TEST_B_CHANNEL,
            "usb_serial": "other-serial" if variant == "foreign_owner" else TEST_B_SERIAL,
            "dev_id": 1,
        }
        broker._auxiliary_drive_restoration_latched = variant == "latched"
        broker._vehicle_state = copy.deepcopy(seed["vehicle_state"])
        broker._vehicle_state_observed_monotonic = 100.0
        with mock.patch.object(source, "acquire", return_value=failure(
            metric="battery.voltage", unit="V", reason="can_busy",
            detail="busy", bus="c-can", acquisition="passive",
        )):
            broker.acquire("battery.voltage", "wake_if_asleep")
        _assert_bcan_rejected(broker.status_response())


# --- Secondary-route resilience (2026-09-27): C-CAN keeps recording while a
# lost B-CAN/CAN-CH route is re-proven and re-admitted as a new segment. ----


def _route(role="b-can", channel=TEST_B_CHANNEL):
    return drive_recorder.CaptureRoute(
        role=role, channel=channel, usb_serial=TEST_B_SERIAL, dev_id=1,
        bitrate=125000, pair="3/11", ownership="shared_passive_observer",
    )


def _admitted(route=None):
    closed = []
    stack = contextlib.ExitStack()
    stack.callback(lambda: closed.append(True))
    return (route or _route(), lambda: interface(listen_only=True, bitrate=125000), stack), closed


class _SegmentRecorder:
    """Writes one chunk file per segment, then behaves as scripted."""

    def __init__(self, role_dir, sequence_start, outcome, stop_event):
        self.role_dir = role_dir
        self.sequence_start = sequence_start
        self.outcome = outcome
        self.stop_event = stop_event

    def run(self):
        self.role_dir.mkdir(exist_ok=True)
        (self.role_dir / f"chunk_{self.sequence_start:06d}_full.candump.zst").write_bytes(b"x")
        if self.outcome is None:
            self.stop_event.wait(5)
            return 0
        raise self.outcome


def _supervisor(tmp, outcomes, *, admitted=True, admit_results=None):
    import threading

    stop = threading.Event()
    settled = threading.Event()
    built = []
    outcomes = list(outcomes)

    def factory(route, check, sequence_start):
        recorder = _SegmentRecorder(Path(tmp) / "b-can", sequence_start, outcomes.pop(0), stop)
        built.append((route, sequence_start))
        return recorder

    first, first_closed = _admitted()
    later = list(admit_results or [])

    def admitter(role, status, client, manager):
        result = later.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result, interface(listen_only=True, bitrate=125000)

    supervisor = drive_recorder.SecondaryRoleSupervisor(
        role="b-can",
        role_dir=Path(tmp) / "b-can",
        client=mock.Mock(),
        interface_manager=mock.Mock(),
        recorder_factory=factory,
        stop_event=stop,
        settled_event=settled,
        events_path=Path(tmp) / drive_recorder.ROUTE_EVENTS_NAME,
        admitted=first if admitted else None,
        admission_detail=None if admitted else "b-can unproven at start",
        status_reader=lambda _client: ready_status(),
        admitter=admitter,
        readmit_interval_seconds=0.01,
        readmit_backoff_seconds=0.0,
    )
    return supervisor, stop, settled, built, first_closed


def _events(tmp):
    path = Path(tmp) / drive_recorder.ROUTE_EVENTS_NAME
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_secondary_route_loss_ends_only_that_segment_and_readmits():
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        second, second_closed = _admitted(_route(channel="can5"))
        supervisor, stop, settled, built, first_closed = _supervisor(
            tmp,
            [drive_recorder.SecondaryRouteLost("broker does not prove b-can"), None],
            admit_results=[
                drive_recorder.BrokerOwnershipLost("still unproven"),
                second,
            ],
        )
        thread = threading.Thread(target=supervisor.run)
        thread.start()
        for _ in range(500):
            if len(built) == 2:
                break
            drive_recorder.time.sleep(0.01)
        assert supervisor.state == "recording"
        stop.set()
        thread.join(5)
        assert not thread.is_alive()

        assert settled.is_set()
        assert first_closed == [True] and second_closed == [True]
        # The re-admitted segment continues chunk numbering on the new channel.
        assert [(route.channel, start) for route, start in built] == [
            (TEST_B_CHANNEL, 0), ("can5", 1),
        ]
        assert [item["end"] for item in supervisor.segments] == ["route_lost", "completed"]
        assert supervisor.continuous() is False
        kinds = [event["type"] for event in _events(tmp)]
        assert kinds == [
            "secondary_segment_start",
            "secondary_segment_end",
            "secondary_awaiting_route",
            "secondary_segment_start",
            "secondary_segment_end",
        ]


def test_secondary_missing_start_signature_is_a_route_loss_not_fatal():
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        supervisor, stop, _settled, built, _closed = _supervisor(
            tmp,
            [capture.CaptureError("required start CAN ID 0x46C was not observed within 5.0 seconds")],
            admit_results=[],
        )
        supervisor.readmit_backoff_seconds = 60.0
        thread = threading.Thread(target=supervisor.run)
        thread.start()
        for _ in range(500):
            if supervisor.state == "awaiting_route":
                break
            drive_recorder.time.sleep(0.01)
        stop.set()
        thread.join(5)
        assert supervisor.segments[0]["end"] == "start_signature_missing"
        assert len(built) == 1


def test_secondary_storage_or_drop_failure_stays_fatal():
    import pytest

    with tempfile.TemporaryDirectory() as tmp:
        for failure in (
            capture.CaptureError("SocketCAN interface loss accounting changed"),
            capture.CaptureError("final zstd chunk failed validation"),
            OSError("mount vanished"),
        ):
            supervisor, *_rest = _supervisor(tmp, [failure])
            with pytest.raises(type(failure)):
                supervisor.run()
            assert supervisor.segments[-1]["end"] == "error"
            assert supervisor.state == "failed"


def test_secondary_unproven_at_start_joins_later():
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        later, _closed = _admitted()
        supervisor, stop, settled, built, _first = _supervisor(
            tmp, [None], admitted=False, admit_results=[later],
        )
        # Startup never waits on an unproven secondary.
        assert settled.is_set()
        thread = threading.Thread(target=supervisor.run)
        thread.start()
        for _ in range(500):
            if built:
                break
            drive_recorder.time.sleep(0.01)
        stop.set()
        thread.join(5)
        assert [start for _route, start in built] == [0]
        assert supervisor.continuous() is False
        assert _events(tmp)[0] == {**_events(tmp)[0], "type": "secondary_awaiting_route",
                                   "detail": "b-can unproven at start"}


def test_readmission_requires_the_primary_interval_and_a_fresh_route_proof():
    with tempfile.TemporaryDirectory() as tmp:
        supervisor, *_rest = _supervisor(tmp, [], admitted=False, admit_results=[])
        lost_primary = ready_status()
        lost_primary["active_drive"]["state"] = "idle"
        supervisor.status_reader = lambda _client: lost_primary
        supervisor.admitter = mock.Mock()
        assert supervisor._try_admit() is None
        supervisor.admitter.assert_not_called()
        assert "primary C-CAN" in supervisor.admission_detail

        # Real admission: the broker route must match a fresh shared lease.
        supervisor.status_reader = lambda _client: ready_status()
        supervisor.admitter = drive_recorder.admit_secondary_route
        lease = drive_recorder.PassiveInterfaceLease(
            role="b-can", channel="can3", usb_serial=TEST_B_SERIAL, dev_id=1,
            bitrate=125000, pair="3/11", topology_generation="g",
        )
        released = []

        @contextlib.contextmanager
        def observe(_role):
            try:
                yield lease
            finally:
                released.append(True)

        supervisor.interface_manager = SimpleNamespace(observe=observe)
        assert supervisor._try_admit() is None
        assert "route changed" in supervisor.admission_detail
        assert released == [True]


def test_passive_lease_revalidation_failure_is_a_secondary_route_loss():
    import pytest

    lease = drive_recorder.PassiveInterfaceLease(
        role="can-ch", channel=TEST_H_CHANNEL, usb_serial=TEST_H_SERIAL, dev_id=0,
        bitrate=500000, pair="12/13", topology_generation="g",
    )
    manager = mock.Mock()
    manager.observe.side_effect = drive_recorder.PassiveInterfaceUnavailable(
        "can-ch", "interface_armed", "can9 is not listen-only"
    )
    check = drive_recorder.PassiveLeaseSafetyCheck(manager, lease)
    with pytest.raises(drive_recorder.SecondaryRouteLost, match="can-ch passive route"):
        check()


def test_real_recorder_preserves_secondary_route_loss_and_continues_numbering():
    import pytest

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        process = mock.Mock()
        process.stdout.fileno.return_value = 91
        process.poll.return_value = None
        chunk = mock.Mock()
        chunk.sequence = 4
        chunk.finish.return_value = {"type": "chunk", "sequence": 4, "streams": {}, "complete": True}
        recorder = capture.Recorder(
            root, frozenset(), 600, 120, capture.DiskPolicy(300, 200),
            channel=TEST_B_CHANNEL, bitrate=125000, sequence_start=4,
            popen=lambda *_args, **_kwargs: process,
            disk_free=lambda _path: 1000,
            safety_check=lambda: interface(listen_only=True, bitrate=125000),
            mount_check=lambda: None,
            health_check=mock.Mock(side_effect=drive_recorder.SecondaryRouteLost("gone")),
            install_signal_handlers=False,
        )
        with (
            mock.patch.object(capture, "Chunk", return_value=chunk) as chunk_class,
            mock.patch.object(capture.os, "set_blocking"),
            mock.patch.object(capture.selectors, "DefaultSelector"),
            mock.patch.object(recorder, "_stop_process"),
            pytest.raises(drive_recorder.SecondaryRouteLost),
        ):
            recorder.run()
        assert chunk_class.call_args.args[1] == 4
        end = json.loads((root / "checkpoint.json").read_text())
        assert end["success"] is False and end["error"] == "gone"
    with pytest.raises(ValueError):
        capture.Recorder(
            Path("/unused"), frozenset(), 600, 120, capture.DiskPolicy(300, 200),
            channel="can1", bitrate=125000, sequence_start=-1,
        )


def _interval_args(root):
    return SimpleNamespace(
        out_root=root, require_mount=root, state_path=root / "state.json",
        conditions="receive-only fixture", rotation_seconds=600,
        duration_seconds=3600, ignition_absence_seconds=20.0,
    )


def _run_interval(root, initial_status, recorder_class, leases):
    manager = mock.Mock()
    manager.observe.side_effect = lambda role: contextlib.nullcontext(leases[role])
    with (
        mock.patch.object(drive_recorder.capture, "require_writable_mount", return_value=Path("/dev/mock")),
        mock.patch.object(drive_recorder, "validate_dependencies", return_value=(interface(listen_only=False), 1000)),
        mock.patch.object(drive_recorder, "read_broker_status", return_value=ready_status()),
        mock.patch.object(drive_recorder, "campaign_id", return_value="drive-test"),
        mock.patch.object(drive_recorder, "priority_ids", return_value=frozenset((0x2EF,))),
        mock.patch.object(drive_recorder.capture, "read_rmem_max", return_value=capture.RECEIVE_BUFFER),
        mock.patch.object(drive_recorder.capture, "Recorder", side_effect=recorder_class),
        mock.patch.object(
            drive_recorder, "PassiveLeaseSafetyCheck",
            side_effect=lambda _manager, lease: (lambda: interface(listen_only=True, bitrate=lease.bitrate)),
        ),
        mock.patch.object(drive_recorder.capture, "campaign_file_lock", return_value=contextlib.nullcontext()),
        mock.patch.object(drive_recorder.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
        mock.patch.object(drive_recorder, "SECONDARY_READMIT_INTERVAL_SECONDS", 0.01),
        mock.patch.object(drive_recorder, "SECONDARY_READMIT_BACKOFF_SECONDS", 0.0),
    ):
        return drive_recorder.record_one_interval(
            _interval_args(root), capture.DiskPolicy(300, 200), initial_status,
            mock.Mock(), interface_manager=manager,
        )


def _leases():
    return {
        "b-can": drive_recorder.PassiveInterfaceLease(
            role="b-can", channel=TEST_B_CHANNEL, usb_serial=TEST_B_SERIAL, dev_id=1,
            bitrate=125000, pair="3/11", topology_generation="g",
        ),
        "can-ch": drive_recorder.PassiveInterfaceLease(
            role="can-ch", channel=TEST_H_CHANNEL, usb_serial=TEST_H_SERIAL, dev_id=0,
            bitrate=500000, pair="12/13", topology_generation="g",
        ),
    }


def test_interval_keeps_ccan_recording_through_a_bcan_route_loss():
    """The 2026-09-27 shape: B-CAN lost mid-drive, C-CAN and CAN-CH continue."""
    import threading

    calls = []
    bcan_segments = []
    readmitted = threading.Event()

    class FakeRecorder:
        def __init__(self, run_dir, *args, **kwargs):
            self.run_dir = run_dir
            self.kwargs = kwargs
            calls.append(self)

        def run(self):
            role = self.run_dir.name
            if role == "c-can":
                # Primary keeps recording until the lost B-CAN is re-admitted.
                assert readmitted.wait(5)
                return 0
            self.kwargs["started_callback"]()
            if role == "b-can":
                bcan_segments.append(self.kwargs["sequence_start"])
                if len(bcan_segments) == 1:
                    (self.run_dir / "chunk_000000_full.candump.zst").write_bytes(b"x")
                    raise drive_recorder.SecondaryRouteLost(
                        "broker does not prove an exact passive or auxiliary-owned b-can route"
                    )
                readmitted.set()
            stop = self.kwargs["external_stop_requested"]
            while not stop():
                drive_recorder.time.sleep(0.001)
            return 0

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        run_dir = _run_interval(root, ready_status(), FakeRecorder, _leases())
        capture_set = json.loads((run_dir / "capture-set.json").read_text())
        events = [json.loads(line) for line in (run_dir / "route-events.jsonl").read_text().splitlines()]
        run_json = json.loads((run_dir / "run.json").read_text())

    assert bcan_segments == [0, 1]
    assert capture_set["primary_complete"] is True
    assert capture_set["complete"] is False
    assert capture_set["roles"]["c-can"]["continuous"] is True
    assert capture_set["roles"]["can-ch"]["continuous"] is True
    assert [s["end"] for s in capture_set["roles"]["b-can"]["segments"]] == ["route_lost", "completed"]
    assert ("b-can", "secondary_segment_end") in {(e["role"], e["type"]) for e in events}
    assert run_json["roles_required"] == ["c-can"]
    assert run_json["secondary_admission"] == {"b-can": "admitted", "can-ch": "admitted"}


def test_interval_starts_ccan_when_a_secondary_is_unproven_at_admission():
    initial = ready_status()
    initial["interface"]["role_interfaces"]["roles"]["can-ch"]["passive_ready"] = False
    started = []

    class FakeRecorder:
        def __init__(self, run_dir, *args, **kwargs):
            self.run_dir = run_dir
            self.kwargs = kwargs
            started.append(run_dir.name)

        def run(self):
            if self.run_dir.name == "c-can":
                return 0
            self.kwargs["started_callback"]()
            stop = self.kwargs["external_stop_requested"]
            while not stop():
                drive_recorder.time.sleep(0.001)
            return 0

    leases = _leases()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with mock.patch.object(drive_recorder, "SECONDARY_READMIT_BACKOFF_SECONDS", 60.0):
            run_dir = _run_interval(root, initial, FakeRecorder, leases)
        run_json = json.loads((run_dir / "run.json").read_text())
        capture_set = json.loads((run_dir / "capture-set.json").read_text())
    assert "c-can" in started and "b-can" in started
    assert run_json["secondary_admission"]["can-ch"].startswith("awaiting_route:")
    assert "can-ch" not in run_json["routes"]
    assert capture_set["complete"] is False
    assert capture_set["roles"]["can-ch"]["continuous"] is False


def test_interval_secondary_fatal_failure_still_fails_the_campaign():
    import pytest

    class FakeRecorder:
        def __init__(self, run_dir, *args, **kwargs):
            self.run_dir = run_dir
            self.kwargs = kwargs

        def run(self):
            if self.run_dir.name == "c-can":
                health = self.kwargs["health_check"]
                for _ in range(500):
                    health()
                    drive_recorder.time.sleep(0.01)
                return 0
            self.kwargs["started_callback"]()
            if self.run_dir.name == "can-ch":
                raise capture.CaptureError("candump reported 3 dropped frames (3 total)")
            stop = self.kwargs["external_stop_requested"]
            while not stop():
                drive_recorder.time.sleep(0.001)
            return 0

    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(drive_recorder.DriveRecorderError, match="dropped frames"):
            _run_interval(Path(directory), ready_status(), FakeRecorder, _leases())
