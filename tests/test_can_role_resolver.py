import contextlib
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from lib import canbus
from lib.can_role_resolver import (
    CanRoleResolutionError,
    CanRoleSpec,
    SysfsCanRoleResolver,
)
from lib.modules import MODULES
from projects.vehicle_data.can_interfaces import (
    ALL_CAN_ROLES,
    BOARD_A_SERIAL,
    BOARD_B_SERIAL,
    CAN_ROLE_SPECS,
    PassiveInterfaceManager,
    PassiveInterfaceUnavailable,
)


class FakeSysfs:
    def __init__(self, root: Path):
        self.root = root
        self.class_net = root / "sys" / "class" / "net"
        self.class_net.mkdir(parents=True)
        self.drivers = root / "sys" / "bus" / "usb" / "drivers"
        (self.drivers / "gs_usb").mkdir(parents=True)
        (self.drivers / "other_can").mkdir(parents=True)
        self.devices = root / "sys" / "devices"
        self.devices.mkdir(parents=True)

    def add_usb_board(
        self,
        name: str,
        serial: str,
        *,
        vid: str = "1d50",
        pid: str = "606f",
    ) -> Path:
        board = self.devices / name
        board.mkdir()
        (board / "idVendor").write_text(f"{vid}\n", encoding="utf-8")
        (board / "idProduct").write_text(f"{pid}\n", encoding="utf-8")
        (board / "serial").write_text(f"{serial}\n", encoding="utf-8")
        return board

    def add_channel(
        self,
        board: Path,
        channel: str,
        dev_id: int,
        *,
        driver: str = "gs_usb",
        dev_id_text: str | None = None,
    ) -> None:
        interface = board / f"interface-{channel}"
        interface.mkdir()
        (interface / "driver").symlink_to(self.drivers / driver)
        netdev = self.class_net / channel
        netdev.mkdir()
        (netdev / "device").symlink_to(interface)
        (netdev / "dev_id").write_text(
            f"{dev_id_text if dev_id_text is not None else hex(dev_id)}\n",
            encoding="utf-8",
        )

    def add_default_layout(
        self,
        *,
        ccan: str = "can7",
        bcan: str = "can2",
        canch: str = "can9",
        spare: str = "can4",
    ) -> dict[str, str]:
        board_a = self.add_usb_board("board-a", BOARD_A_SERIAL)
        board_b = self.add_usb_board("board-b", BOARD_B_SERIAL)
        self.add_channel(board_a, ccan, 0, dev_id_text="0")
        self.add_channel(board_a, bcan, 1, dev_id_text="0x1")
        self.add_channel(board_b, canch, 0)
        self.add_channel(board_b, spare, 1)
        return {
            "c-can": ccan,
            "b-can": bcan,
            "can-ch": canch,
            "spare": spare,
        }


def interface(
    channel: str,
    *,
    up: bool = True,
    bitrate: int | None = 500000,
    listen_only: bool = True,
    controller_state: str | None = "ERROR-ACTIVE",
    restart_ms: int | None = 0,
    fd_enabled: bool | None = False,
    one_shot: bool | None = False,
) -> canbus.InterfaceState:
    return canbus.InterfaceState(
        channel=channel,
        present=True,
        up=up,
        bitrate=bitrate,
        listen_only=listen_only,
        controller_state=controller_state,
        restart_ms=restart_ms,
        fd_enabled=fd_enabled,
        one_shot=one_shot,
    )


class FakeLocks:
    def __init__(self):
        self.events = []

    @contextlib.contextmanager
    def observer(self, name):
        self.events.append(("enter", name))
        try:
            yield object()
        finally:
            self.events.append(("exit", name))


class SequenceResolver:
    def __init__(self, *topologies):
        self.topologies = list(topologies)

    def resolve(self, _specs):
        if len(self.topologies) > 1:
            return self.topologies.pop(0)
        return self.topologies[0]


class CanRoleResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakeSysfs(Path(self.temp.name))

    def resolver(self):
        return SysfsCanRoleResolver(self.fake.class_net)

    def test_exact_serial_and_dev_id_resolve_scrambled_kernel_names(self):
        expected = self.fake.add_default_layout()

        topology = self.resolver().resolve(CAN_ROLE_SPECS)

        self.assertTrue(topology.all_resolved(ALL_CAN_ROLES))
        self.assertEqual(topology.channel_map(), expected)
        self.assertEqual(topology.resolution("c-can").device.usb_serial, BOARD_A_SERIAL)
        self.assertEqual(topology.resolution("c-can").device.dev_id, 0)
        self.assertEqual(topology.resolution("b-can").device.usb_serial, BOARD_A_SERIAL)
        self.assertEqual(topology.resolution("b-can").device.dev_id, 1)
        self.assertEqual(topology.resolution("can-ch").device.usb_serial, BOARD_B_SERIAL)
        self.assertEqual(topology.resolution("can-ch").device.dev_id, 0)
        self.assertEqual(topology.resolution("spare").device.usb_serial, BOARD_B_SERIAL)
        self.assertEqual(topology.resolution("spare").device.dev_id, 1)
        self.assertEqual(len(topology.fingerprint), 16)

    def test_missing_role_never_falls_back_to_another_channel(self):
        board_a = self.fake.add_usb_board("board-a", BOARD_A_SERIAL)
        self.fake.add_channel(board_a, "can0", 0)

        topology = self.resolver().resolve(CAN_ROLE_SPECS)

        self.assertEqual(topology.channel_for("c-can"), "can0")
        self.assertEqual(topology.resolution("b-can").state, "missing")
        with self.assertRaises(CanRoleResolutionError) as raised:
            topology.channel_for("b-can")
        self.assertEqual(raised.exception.state, "missing")

    def test_duplicate_exact_identity_is_ambiguous_and_never_selected(self):
        board_a = self.fake.add_usb_board("board-a", BOARD_A_SERIAL)
        self.fake.add_channel(board_a, "can0", 0)
        self.fake.add_channel(board_a, "can3", 0)

        topology = self.resolver().resolve(CAN_ROLE_SPECS)

        resolution = topology.resolution("c-can")
        self.assertEqual(resolution.state, "ambiguous")
        self.assertEqual([item.channel for item in resolution.matches], ["can0", "can3"])
        with self.assertRaises(CanRoleResolutionError):
            topology.channel_for("c-can")

    def test_wrong_vid_pid_or_driver_does_not_match_expected_role(self):
        wrong_usb = self.fake.add_usb_board(
            "wrong-usb", BOARD_A_SERIAL, vid="1209", pid="ca01"
        )
        correct_usb = self.fake.add_usb_board("correct-usb", BOARD_A_SERIAL)
        self.fake.add_channel(wrong_usb, "can0", 0)
        self.fake.add_channel(correct_usb, "can1", 0, driver="other_can")

        topology = self.resolver().resolve(CAN_ROLE_SPECS)

        self.assertEqual(topology.resolution("c-can").state, "missing")
        self.assertEqual([item.channel for item in topology.inventory], ["can0"])

    def test_malformed_expected_driver_candidate_is_reported_not_guessed(self):
        orphan = self.fake.devices / "orphan" / "interface-can8"
        orphan.mkdir(parents=True)
        (orphan / "driver").symlink_to(self.fake.drivers / "gs_usb")
        netdev = self.fake.class_net / "can8"
        netdev.mkdir()
        (netdev / "device").symlink_to(orphan)
        (netdev / "dev_id").write_text("not-a-number\n", encoding="utf-8")

        topology = self.resolver().resolve(CAN_ROLE_SPECS)

        self.assertEqual(topology.inventory, ())
        self.assertEqual(len(topology.issues), 1)
        self.assertEqual(topology.issues[0].reason, "invalid_dev_id")

    def test_non_can_netdev_with_dev_id_is_ignored_before_driver_discovery(self):
        virtual = self.fake.class_net / "virtual0"
        virtual.mkdir()
        (virtual / "dev_id").write_text("0\n", encoding="utf-8")
        # ARPHRD_ETHER. Some virtual Ethernet devices expose dev_id but have
        # no USB CAN ancestry; they must not degrade an otherwise clean scan.
        (virtual / "type").write_text("1\n", encoding="utf-8")
        expected = self.fake.add_default_layout()

        topology = self.resolver().resolve(CAN_ROLE_SPECS)

        self.assertEqual(topology.channel_map(), expected)
        self.assertEqual(topology.issues, ())
        self.assertNotIn("virtual0", [item.channel for item in topology.inventory])

    def test_duplicate_role_specs_are_rejected_before_resolution(self):
        spec = CAN_ROLE_SPECS[0]
        duplicate = CanRoleSpec(
            role=spec.role,
            board="duplicate",
            connector="CAN1",
            usb_serial="different",
            dev_id=9,
            bitrate=500000,
            pair="6/14",
        )
        with self.assertRaisesRegex(ValueError, "role names"):
            self.resolver().resolve((spec, duplicate))


class PassiveInterfaceManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakeSysfs(Path(self.temp.name))
        self.mapping = self.fake.add_default_layout()
        self.resolver = SysfsCanRoleResolver(self.fake.class_net)
        self.states = {
            self.mapping["c-can"]: interface(self.mapping["c-can"], bitrate=500000),
            self.mapping["b-can"]: interface(self.mapping["b-can"], bitrate=125000),
            self.mapping["can-ch"]: interface(self.mapping["can-ch"], bitrate=500000),
            self.mapping["spare"]: interface(
                self.mapping["spare"],
                up=False,
                bitrate=None,
                listen_only=False,
                controller_state="STOPPED",
            ),
        }

    def manager(self, **kwargs):
        return PassiveInterfaceManager(
            resolver=kwargs.pop("resolver", self.resolver),
            interface_state_reader=kwargs.pop(
                "interface_state_reader", lambda channel: self.states[channel]
            ),
            wall_clock=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
            **kwargs,
        )

    def test_status_reports_all_three_passive_buses_and_safe_spare(self):
        status = self.manager().status_snapshot()

        self.assertEqual(status["mode"], "read_only")
        self.assertFalse(status["configures_interfaces"])
        self.assertTrue(status["resolved"])
        self.assertTrue(status["vehicle_buses_ready"])
        self.assertTrue(status["passive_ready"])
        self.assertTrue(status["ready"])
        self.assertEqual(set(status["roles"]), set(ALL_CAN_ROLES))
        self.assertEqual(status["roles"]["c-can"]["channel"], self.mapping["c-can"])
        self.assertEqual(status["roles"]["b-can"]["expected"]["bitrate"], 125000)
        self.assertEqual(status["roles"]["can-ch"]["expected"]["pair"], "12/13")
        self.assertEqual(status["roles"]["spare"]["reason"], "spare_down")
        self.assertFalse(status["roles"]["spare"]["passive_ready"])
        self.assertTrue(status["roles"]["spare"]["safe"])

    def test_status_fails_closed_for_armed_wrong_rate_and_up_spare(self):
        self.states[self.mapping["c-can"]] = interface(
            self.mapping["c-can"], listen_only=False
        )
        self.states[self.mapping["b-can"]] = interface(
            self.mapping["b-can"], bitrate=500000
        )
        self.states[self.mapping["spare"]] = interface(self.mapping["spare"])

        status = self.manager().status_snapshot()

        self.assertFalse(status["ready"])
        self.assertFalse(status["passive_ready"])
        self.assertEqual(status["roles"]["c-can"]["reason"], "interface_armed")
        self.assertEqual(status["roles"]["b-can"]["reason"], "wrong_bitrate")
        self.assertEqual(status["roles"]["spare"]["reason"], "spare_up")

    def test_status_rejects_fd_or_nonzero_restart_policy(self):
        self.states[self.mapping["c-can"]] = interface(
            self.mapping["c-can"], fd_enabled=True
        )
        self.states[self.mapping["b-can"]] = interface(
            self.mapping["b-can"], bitrate=125000, restart_ms=100
        )

        status = self.manager().status_snapshot()

        self.assertFalse(status["ready"])
        self.assertEqual(
            status["roles"]["c-can"]["reason"], "fd_mode_not_classical"
        )
        self.assertEqual(
            status["roles"]["b-can"]["reason"], "restart_policy_mismatch"
        )

    def test_missing_spare_keeps_complete_passive_topology_not_ready(self):
        incomplete_root = Path(self.temp.name) / "incomplete"
        incomplete = FakeSysfs(incomplete_root)
        board_a = incomplete.add_usb_board("board-a", BOARD_A_SERIAL)
        board_b = incomplete.add_usb_board("board-b", BOARD_B_SERIAL)
        incomplete.add_channel(board_a, "can0", 0)
        incomplete.add_channel(board_a, "can1", 1)
        incomplete.add_channel(board_b, "can2", 0)
        states = {
            "can0": interface("can0", bitrate=500000),
            "can1": interface("can1", bitrate=125000),
            "can2": interface("can2", bitrate=500000),
        }
        manager = self.manager(
            resolver=SysfsCanRoleResolver(incomplete.class_net),
            interface_state_reader=lambda channel: states[channel],
        )

        status = manager.status_snapshot()

        self.assertTrue(status["vehicle_buses_ready"])
        self.assertFalse(status["passive_ready"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["resolved"])
        self.assertEqual(status["roles"]["spare"]["resolution"], "missing")

    def test_module_routing_uses_bus_without_persisting_ephemeral_channel(self):
        manager = self.manager()
        topology = manager.topology()

        self.assertIsNone(MODULES["pcm"].channel)
        self.assertIsNone(MODULES["uconnect_bcan"].channel)
        self.assertIsNone(MODULES["abs_canch"].channel)
        self.assertEqual(
            manager.module_channel(MODULES["pcm"], topology=topology),
            self.mapping["c-can"],
        )
        self.assertEqual(
            manager.module_channel(MODULES["uconnect_bcan"], topology=topology),
            self.mapping["b-can"],
        )
        self.assertEqual(
            manager.module_channel(MODULES["abs_canch"], topology=topology),
            self.mapping["can-ch"],
        )
        alias_module = SimpleNamespace(bus="CANCH", channel="definitely-wrong")
        self.assertEqual(
            manager.module_channel(alias_module, topology=topology),
            self.mapping["can-ch"],
        )

    def test_observe_holds_logical_and_dynamic_shared_locks(self):
        locks = FakeLocks()
        manager = self.manager(locks=locks)

        with (
            mock.patch.object(canbus, "ip_up") as ip_up,
        ):
            with manager.observe("ccan") as lease:
                self.assertEqual(lease.role, "c-can")
                self.assertEqual(lease.channel, self.mapping["c-can"])
                self.assertEqual(lease.usb_serial, BOARD_A_SERIAL)
                self.assertEqual(lease.bitrate, 500000)

        self.assertEqual(
            locks.events,
            [
                ("enter", "can-role-c-can"),
                ("enter", self.mapping["c-can"]),
                ("exit", self.mapping["c-can"]),
                ("exit", "can-role-c-can"),
            ],
        )
        ip_up.assert_not_called()

    def test_observe_fails_if_role_renames_while_locks_are_acquired(self):
        before = self.resolver.resolve(CAN_ROLE_SPECS)
        renamed_root = Path(self.temp.name) / "renamed"
        renamed = FakeSysfs(renamed_root)
        renamed.add_default_layout(ccan="can0", bcan="can1", canch="can2", spare="can3")
        after = SysfsCanRoleResolver(renamed.class_net).resolve(CAN_ROLE_SPECS)
        locks = FakeLocks()
        manager = self.manager(
            resolver=SequenceResolver(before, after),
            locks=locks,
        )

        with self.assertRaises(PassiveInterfaceUnavailable) as raised:
            with manager.observe("c-can"):
                self.fail("topology change must not yield a lease")

        self.assertEqual(raised.exception.reason, "topology_changed")
        self.assertEqual(
            locks.events,
            [
                ("enter", "can-role-c-can"),
                ("enter", self.mapping["c-can"]),
                ("exit", self.mapping["c-can"]),
                ("exit", "can-role-c-can"),
            ],
        )

    def test_observe_fails_closed_when_interface_state_is_not_passive(self):
        self.states[self.mapping["can-ch"]] = interface(
            self.mapping["can-ch"], controller_state="ERROR-WARNING"
        )
        with self.assertRaises(PassiveInterfaceUnavailable) as raised:
            with self.manager(locks=FakeLocks()).observe("can-ch"):
                self.fail("degraded interface must not yield a lease")
        self.assertEqual(raised.exception.reason, "controller_not_error_active")

    def test_status_does_not_query_interface_for_missing_or_ambiguous_role(self):
        board_a = self.fake.devices / "board-a"
        self.fake.add_channel(board_a, "can8", 0)
        queried = []
        manager = self.manager(
            interface_state_reader=lambda channel: (
                queried.append(channel) or self.states[channel]
            )
        )

        status = manager.status_snapshot()

        self.assertEqual(status["roles"]["c-can"]["resolution"], "ambiguous")
        self.assertNotIn(self.mapping["c-can"], queried)
        self.assertFalse(status["ready"])


if __name__ == "__main__":
    unittest.main()
