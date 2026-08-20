import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "projects" / "vehicle_data" / "systemd"


class VehicleDataSystemdTests(unittest.TestCase):
    def test_broker_is_activated_and_bound_by_can0_device(self):
        unit = (SYSTEMD_DIR / "van-telemetry.service").read_text()

        self.assertIn(
            "BindsTo=sys-subsystem-net-devices-can0.device",
            unit,
        )
        self.assertIn(
            "After=local-fs.target sys-subsystem-net-devices-can0.device",
            unit,
        )
        self.assertIn(
            "WantedBy=sys-subsystem-net-devices-can0.device",
            unit,
        )
        self.assertNotIn("WantedBy=multi-user.target", unit)

    def test_web_listener_is_not_independently_boot_enabled(self):
        unit = (SYSTEMD_DIR / "van-telemetry-web.service").read_text()

        self.assertNotIn("WantedBy=multi-user.target", unit)


if __name__ == "__main__":
    unittest.main()
