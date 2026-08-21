import argparse
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from projects.vehicle_data import drive_recorder
from tools import passive_drive_capture as capture


TEST_CHANNEL = "can7"
TEST_SERIAL = "serial-a"


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
                    }
                }
            },
        },
        "vehicle_state": {
            "running": True,
            "basis": "qualified_ccan_0x0fc_engine_speed",
        },
    }


def interface(*, listen_only):
    return capture.InterfaceState(
        up=True,
        bitrate=500000,
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
            "receive_only_broker_armed_companion",
        )
        self.assertIn("transmit CAN", plan["does_not"])
        self.assertEqual(plan["stop_after_id"], "0x2EF")

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


if __name__ == "__main__":
    unittest.main()
