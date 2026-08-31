import argparse
import contextlib
import errno
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
