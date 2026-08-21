import unittest
from pathlib import Path

from lib import canbus, uds
from lib.modules import MODULES
from lib.vehicle_can_roles import CAN_BUS_ROLES, CAN_ROLE_SPECS, SPARE_ROLE


REPO = Path(__file__).resolve().parents[1]


class NoLegacyCanTopologyTests(unittest.TestCase):
    def test_registry_never_persists_ephemeral_socketcan_names(self):
        self.assertTrue(MODULES)
        self.assertTrue(all(module.channel is None for module in MODULES.values()))
        self.assertTrue(
            all(module.bus in CAN_BUS_ROLES for module in MODULES.values())
        )

    def test_installed_role_contract_has_three_independent_buses_and_one_spare(self):
        by_role = {spec.role: spec for spec in CAN_ROLE_SPECS}
        self.assertEqual(set(by_role), {*CAN_BUS_ROLES, SPARE_ROLE})
        self.assertEqual(
            {role: by_role[role].bitrate for role in CAN_BUS_ROLES},
            {"c-can": 500_000, "b-can": 125_000, "can-ch": 500_000},
        )
        self.assertEqual(
            {role: by_role[role].pair for role in CAN_BUS_ROLES},
            {"c-can": "6/14", "b-can": "3/11", "can-ch": "12/13"},
        )
        self.assertFalse(by_role[SPARE_ROLE].passive_required)

    def test_retired_single_adapter_entry_points_stay_removed(self):
        retired = (
            "bringup.sh",
            "tools/dump.sh",
            "tools/ccan_inventory_campaign.sh",
            "projects/vehicle_data/retune.py",
            "projects/ecu_mapping/cluster_drive_log.py",
            "projects/radar/auto_drive_logger.py",
            "projects/radar/did_hunt_log.py",
            "projects/radar/perturb_monitor.py",
            "projects/radar/radar_acc_align_0251.py",
            "projects/radar/radar_acc_baseline.py",
            "projects/radar/radar_acc_drive_log.py",
            "projects/radar/radar_acc_sda_drive.py",
        )
        self.assertEqual(
            [relative for relative in retired if (REPO / relative).exists()],
            [],
        )

    def test_shared_libraries_have_no_bus_hunting_or_channel_only_armer(self):
        for name in (
            "bring_up_passive",
            "controller_state",
            "detect_bus",
            "iface_bitrate",
            "is_listen_only",
            "poke_wake",
            "restore_passive",
            "tx_wake_burst",
            "wake",
        ):
            self.assertFalse(hasattr(canbus, name), name)
        for name in ("bring_up_can", "recover_socket", "recover_module_socket"):
            self.assertFalse(hasattr(uds, name), name)

    def test_can_tools_have_no_global_service_blocklist(self):
        for relative in (
            "tools/ecu_discover.py",
            "tools/passive_drive_capture.py",
        ):
            source = (REPO / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertNotIn("ACTIVE_CAN_CONFLICT_SERVICES", source)
                self.assertNotIn("SERVICE_BLOCKLIST", source)

    def test_tracked_units_do_not_encode_single_adapter_or_cross_bus_exclusion(self):
        units = tuple(
            sorted(
                path
                for path in REPO.rglob("*.service")
                if "tmp" not in path.relative_to(REPO).parts
            )
        )
        self.assertTrue(units)
        forbidden = (
            "pcan",
            "peak_usb",
            "bringup.sh",
            "can0",
            "sys-subsystem-net-devices-can0.device",
            "conflicts=",
        )
        for unit in units:
            text = unit.read_text(encoding="utf-8").lower()
            for token in forbidden:
                with self.subTest(unit=unit.name, token=token):
                    self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
