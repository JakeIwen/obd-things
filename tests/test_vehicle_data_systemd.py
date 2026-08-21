import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "projects" / "vehicle_data" / "systemd"


class VehicleDataSystemdTests(unittest.TestCase):
    def test_broker_uses_serial_resolved_roles_and_durable_history(self):
        unit = (SYSTEMD_DIR / "van-telemetry.service").read_text()

        self.assertNotIn("sys-subsystem-net-devices-can0.device", unit)
        self.assertIn("--can-interface-mode dual-usbcanfd", unit)
        self.assertIn("StateDirectory=van-telemetry", unit)
        self.assertIn("--history-db /var/lib/van-telemetry/history.sqlite3", unit)
        self.assertIn("--history-interval 5", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_web_listener_is_not_independently_boot_enabled(self):
        unit = (SYSTEMD_DIR / "van-telemetry-web.service").read_text()

        self.assertNotIn("WantedBy=multi-user.target", unit)


if __name__ == "__main__":
    unittest.main()
