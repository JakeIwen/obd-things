import ast
import re
import unittest
from pathlib import Path

from lib import canbus, uds
from lib.modules import MODULES
from lib.vehicle_can_roles import CAN_BUS_ROLES, CAN_ROLE_SPECS, SPARE_ROLE


REPO = Path(__file__).resolve().parents[1]


# These are maintained execution paths, not dated findings or offline capture
# parsers.  Historical evidence deliberately retains the adapter and netdev
# names that appeared on the wire at the time.
LIVE_CAN_ROOTS = (
    REPO / "lib",
    REPO / "live_data",
    REPO / "projects" / "battery",
    REPO / "projects" / "vehicle_data",
)
LIVE_CAN_FILES = (
    REPO / "projects" / "ecu_mapping" / "bcan_drive_recorder.py",
    REPO / "projects" / "ecu_mapping" / "promaster-bcan-recorder.service",
    REPO / "projects" / "ecu_mapping" / "promaster-mapping-drive.service",
    REPO / "projects" / "tpms" / "drive_sniff.py",
    REPO / "projects" / "tpms" / "tpms_logger.py",
    REPO / "projects" / "tpms" / "tpms-drivesniff.service",
    REPO / "projects" / "tpms" / "tpms-logger.service",
    REPO / "tools" / "can_wake.py",
    REPO / "tools" / "did_sweep.py",
    REPO / "tools" / "dtc_batch.py",
    REPO / "tools" / "dtc_inventory.py",
    REPO / "tools" / "ecu_discover.py",
    REPO / "tools" / "identity_inventory.py",
    REPO / "tools" / "ignition_triggered_passive_capture.py",
    REPO / "tools" / "passive_drive_capture.py",
    REPO / "tools" / "routine_scan.py",
    REPO / "tools" / "signal_correlate.py",
    REPO / "tools" / "three_bus_capture.py",
    REPO / "tools" / "uds_send.py",
)


def maintained_live_can_files():
    suffixes = {".py", ".service", ".sh", ".socket", ".timer"}
    found = set()
    for root in LIVE_CAN_ROOTS:
        if not root.exists():
            continue
        found.update(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in suffixes
            and not path.name.startswith("._")
        )
    found.update(path for path in LIVE_CAN_FILES if path.exists())
    return tuple(sorted(found))


def _node_mentions_bitrate(node):
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and "bitrate" in child.id.lower():
            return True
        if isinstance(child, ast.Attribute) and "bitrate" in child.attr.lower():
            return True
    return False


def _node_selects_logical_role(node):
    roles = set(CAN_BUS_ROLES)
    for child in ast.walk(node):
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else (child.target,)
            value = child.value
            names = {
                target.id
                for target in targets
                if isinstance(target, ast.Name)
            }
            if names & {"bus", "role", "logical_bus", "logical_role"}:
                if isinstance(value, ast.Constant) and value.value in roles:
                    return True
        if isinstance(child, ast.Return):
            value = child.value
            if isinstance(value, ast.Constant) and value.value in roles:
                return True
    return False


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

        # A bitrate cannot identify the attached high-speed branch: both
        # permanent C-CAN and CAN-CH roles are 500 kbit/s.
        high_speed = {
            role for role in CAN_BUS_ROLES if by_role[role].bitrate == 500_000
        }
        self.assertEqual(high_speed, {"c-can", "can-ch"})

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

    def test_maintained_live_paths_do_not_name_retired_adapter_or_fixed_netdev(self):
        paths = maintained_live_can_files()
        self.assertTrue(paths)
        forbidden = {
            "retired adapter token": re.compile(r"(?i)\bpcan(?:-usb)?\b|\bpeak_usb\b"),
            "fixed SocketCAN netdev": re.compile(r"(?<![A-Za-z0-9_])can0(?![A-Za-z0-9_])"),
        }
        for path in paths:
            source = path.read_text(encoding="utf-8")
            for label, pattern in forbidden.items():
                with self.subTest(path=path.relative_to(REPO), guard=label):
                    self.assertIsNone(pattern.search(source))

    def test_bitrate_is_never_reverse_mapped_to_a_logical_bus(self):
        """Rates validate a serial-resolved role; they never select that role."""

        reverse_map = re.compile(
            r"(?:125_?000|500_?000)\s*:\s*['\"](?:b-can|c-can|can-ch)['\"]"
        )
        for path in maintained_live_can_files():
            if path.suffix != ".py":
                continue
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(REPO), guard="reverse map"):
                self.assertIsNone(reverse_map.search(source))
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.If, ast.IfExp)):
                    continue
                if not _node_mentions_bitrate(node.test):
                    continue
                with self.subTest(
                    path=path.relative_to(REPO), line=getattr(node, "lineno", None)
                ):
                    self.assertFalse(_node_selects_logical_role(node))

    def test_passive_signature_identification_has_a_reviewed_role_owner(self):
        """Keep identify_bus as corroboration, never topology discovery."""

        approved = {
            Path("lib/can_runtime_route.py"),
            Path("lib/can_wake.py"),
            Path("projects/battery/bcan_voltage.py"),
            Path("projects/battery/ccan_voltage.py"),
            Path("projects/tpms/tpms_logger.py"),
            Path("projects/vehicle_data/active_drive.py"),
            Path("projects/vehicle_data/can_runtime.py"),
            Path("projects/vehicle_data/sources.py"),
        }
        callers = set()
        for path in REPO.rglob("*.py"):
            relative = path.relative_to(REPO)
            if (
                "tmp" in relative.parts
                or relative.parts[0] == "tests"
                or path.name.startswith("._")
            ):
                continue
            if relative == Path("lib/canbus.py"):
                continue
            if "canbus.identify_bus" in path.read_text(encoding="utf-8"):
                callers.add(relative)
        self.assertTrue(callers)
        self.assertEqual(callers - approved, set())

    def test_live_can_paths_have_no_global_service_exclusion(self):
        for path in maintained_live_can_files():
            source = path.read_text(encoding="utf-8")
            relative = path.relative_to(REPO)
            with self.subTest(path=relative):
                self.assertNotIn("ACTIVE_CAN_CONFLICT_SERVICES", source)
                self.assertNotIn("SERVICE_BLOCKLIST", source)
                self.assertIsNone(
                    re.search(
                        r"(?s)['\"]systemctl['\"].{0,80}['\"]stop['\"]",
                        source,
                    )
                )

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
