import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.vehicle_data.usb_can_monitor import (
    KernelUevent,
    UsbCanIncidentMonitor,
)


UTC = timezone.utc


class FakeClock:
    def __init__(self):
        self.wall = datetime(2026, 8, 20, 11, 53, 2, tzinfo=UTC)
        self.mono = 100.0

    def wall_clock(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def uevent(
    action,
    devpath,
    *,
    seq,
    subsystem="usb",
    devtype="usb_device",
    product=None,
):
    fields = [
        f"{action}@{devpath}",
        f"ACTION={action}",
        f"DEVPATH={devpath}",
        f"SUBSYSTEM={subsystem}",
        f"DEVTYPE={devtype}",
        f"SEQNUM={seq}",
    ]
    if product is not None:
        fields.append(f"PRODUCT={product}")
    return ("\0".join(fields) + "\0").encode()


class FakeSysfs:
    def __init__(self, root: Path):
        self.root = root
        (root / "kernel" / "random").mkdir(parents=True)
        (root / "kernel" / "random" / "boot_id").write_text(
            "fixture-boot\n", encoding="ascii"
        )
        self.usb_root = root / "devices" / "platform" / "usb1"
        self.pi_hub = self.usb_root / "1-1"
        self.main_hub = self.pi_hub / "1-1.2"
        self.can_hub = self.main_hub / "1-1.2.4"
        self.board_a = self.can_hub / "1-1.2.4.2"
        self.board_b = self.can_hub / "1-1.2.4.4"
        for hub in (self.pi_hub, self.main_hub, self.can_hub):
            hub.mkdir(parents=True, exist_ok=True)
            (hub / "bDeviceClass").write_text("09\n", encoding="ascii")
            (hub / "idVendor").write_text("0bda\n", encoding="ascii")
            (hub / "idProduct").write_text("5411\n", encoding="ascii")
        self._board(self.board_a, "serial-a")
        self._board(self.board_b, "serial-b")
        devices = root / "bus" / "usb" / "devices"
        devices.mkdir(parents=True)
        for path in (self.can_hub, self.board_a, self.board_b):
            (devices / path.name).symlink_to(path)

    @staticmethod
    def _board(path: Path, serial: str):
        path.mkdir(parents=True)
        (path / "bDeviceClass").write_text("00\n", encoding="ascii")
        (path / "idVendor").write_text("1d50\n", encoding="ascii")
        (path / "idProduct").write_text("606f\n", encoding="ascii")
        (path / "serial").write_text(serial + "\n", encoding="ascii")

    def devpath(self, path: Path) -> str:
        return "/" + path.relative_to(self.root).as_posix()


class UsbCanMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.sysfs = FakeSysfs(self.root)
        self.clock = FakeClock()
        self.monitor = UsbCanIncidentMonitor(
            sysfs_root=self.root,
            expected_roles_by_serial={
                "serial-a": ("c-can", "b-can"),
                "serial-b": ("can-ch", "spare"),
            },
            wall_clock=self.clock.wall_clock,
            monotonic=self.clock.monotonic,
            queue_limit=8,
            recent_limit=8,
            seen_limit=16,
            boot_id="fixture-boot",
        )
        inventory = self.monitor.refresh_inventory()
        self.assertEqual(inventory["found_serials"], ["serial-a", "serial-b"])

    def healthy_roles(self):
        roles = {}
        for serial, names in {
            "serial-a": ("c-can", "b-can"),
            "serial-b": ("can-ch", "spare"),
        }.items():
            for role in names:
                roles[role] = {
                    "resolution": "resolved",
                    "safe": True,
                    "expected": {"usb_serial": serial},
                    "actual": {"present": True},
                }
        return {"generation": "topology-after-reset", "roles": roles}

    def test_parser_accepts_only_bounded_structured_fields(self):
        path = self.sysfs.devpath(self.sysfs.board_a)
        parsed = KernelUevent.parse(
            uevent("remove", path, seq=42, product="1d50/606f/2603")
        )
        self.assertEqual(parsed.action, "remove")
        self.assertEqual(parsed.devpath, path)
        self.assertEqual(parsed.seqnum, "42")
        with self.assertRaisesRegex(ValueError, "empty or oversized"):
            KernelUevent.parse(b"")

    def test_parent_hub_reset_is_one_incident_for_both_boards(self):
        hub_path = self.sysfs.devpath(self.sysfs.can_hub)
        board_a = self.sysfs.devpath(self.sysfs.board_a)
        board_b = self.sysfs.devpath(self.sysfs.board_b)

        removed = self.monitor.process_datagram(
            uevent("remove", hub_path, seq=100, product="0bda/5411/121")
        )
        self.assertEqual(removed["kind"], "usb_parent_hub_removed")
        self.assertEqual(
            removed["affected_serials"], ["serial-a", "serial-b"]
        )
        self.monitor.process_datagram(
            uevent("remove", board_a, seq=101, product="1d50/606f/2603")
        )
        self.monitor.process_datagram(
            uevent("remove", board_b, seq=102, product="1d50/606f/2603")
        )
        status = self.monitor.status_snapshot()
        self.assertEqual(status["active_count"], 1)
        incident_id = status["active"][0]["incident_id"]
        self.assertEqual(status["active"][0]["event_count"], 3)

        self.clock.advance(0.4)
        self.monitor.process_datagram(
            uevent("add", hub_path, seq=103, product="0bda/5411/121")
        )
        self.monitor.process_datagram(
            uevent("add", board_a, seq=104, product="1d50/606f/2603")
        )
        self.monitor.process_datagram(
            uevent("add", board_b, seq=105, product="1d50/606f/2603")
        )
        # A USB add edge alone is not accepted as recovery evidence.
        self.assertEqual(self.monitor.status_snapshot()["active_count"], 1)

        self.clock.advance(0.6)
        resolved = self.monitor.reconcile(self.healthy_roles())
        self.assertEqual(resolved, (incident_id,))
        status = self.monitor.status_snapshot()
        self.assertEqual(status["active_count"], 0)
        self.assertEqual(status["recent_incidents"][0]["state"], "resolved")
        self.assertEqual(
            status["recent_incidents"][0]["resolution"],
            "exact_serial_role_health_reestablished",
        )
        self.assertEqual(status["recent_events"][-1]["kind"], "usb_can_recovered")

    def test_unhealthy_reresolution_does_not_close_incident(self):
        board_a = self.sysfs.devpath(self.sysfs.board_a)
        self.monitor.process_datagram(
            uevent("remove", board_a, seq=200, product="1d50/606f/2603")
        )
        roles = self.healthy_roles()
        roles["roles"]["b-can"]["safe"] = False
        self.assertEqual(self.monitor.reconcile(roles), ())
        self.assertEqual(self.monitor.status_snapshot()["active_count"], 1)

    def test_cached_exact_board_remove_survives_missing_product_field(self):
        board_a = self.sysfs.devpath(self.sysfs.board_a)
        event = self.monitor.process_datagram(
            uevent("remove", board_a, seq=250, product=None)
        )
        self.assertEqual(event["kind"], "usb_can_adapter_removed")
        self.assertEqual(event["usb_serial"], "serial-a")

    def test_exact_event_id_is_deduplicated_and_ack_is_selective(self):
        board_a = self.sysfs.devpath(self.sysfs.board_a)
        payload = uevent(
            "remove", board_a, seq=300, product="1d50/606f/2603"
        )
        event = self.monitor.process_datagram(payload)
        self.assertIsNone(self.monitor.process_datagram(payload))
        batch = self.monitor.persistence_batch()
        self.assertEqual(len(batch["events"]), 1)
        self.assertEqual(self.monitor.acknowledge_events(["not-present"]), 0)
        self.assertEqual(self.monitor.acknowledge_events([event["event_id"]]), 1)
        self.assertEqual(self.monitor.persistence_batch()["events"], [])
        # Incident history survives acknowledgement of its immutable edge.
        self.assertEqual(self.monitor.status_snapshot()["active_count"], 1)

    def test_queue_is_bounded_and_irrelevant_usb_is_ignored(self):
        monitor = UsbCanIncidentMonitor(
            sysfs_root=self.root,
            expected_roles_by_serial={"serial-a": ("c-can", "b-can")},
            wall_clock=self.clock.wall_clock,
            monotonic=self.clock.monotonic,
            queue_limit=2,
            recent_limit=3,
            seen_limit=4,
            boot_id="fixture-boot",
        )
        monitor.refresh_inventory()
        unrelated = monitor.process_datagram(
            uevent(
                "remove",
                "/devices/platform/usb1/1-9",
                seq=400,
                product="1234/5678/1",
            )
        )
        self.assertIsNone(unrelated)
        board_a = self.sysfs.devpath(self.sysfs.board_a)
        for seq, action in ((401, "remove"), (402, "add"), (403, "remove")):
            monitor.process_datagram(
                uevent(action, board_a, seq=seq, product="1d50/606f/2603")
            )
        status = monitor.status_snapshot()
        self.assertEqual(status["pending_event_count"], 2)
        self.assertEqual(status["dropped_event_count"], 1)
        self.assertEqual(status["ignored_event_count"], 1)

    def test_netdev_edge_is_filtered_through_known_exact_board(self):
        board_a = self.sysfs.devpath(self.sysfs.board_a)
        netdev = board_a + ":1.0/net/can7"
        event = self.monitor.process_datagram(
            uevent(
                "remove",
                netdev,
                seq=500,
                subsystem="net",
                devtype="",
            )
        )
        self.assertEqual(event["kind"], "usb_can_netdev_removed")
        self.assertEqual(event["usb_serial"], "serial-a")
        self.assertEqual(self.monitor.status_snapshot()["active_count"], 1)


if __name__ == "__main__":
    unittest.main()
