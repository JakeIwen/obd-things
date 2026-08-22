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
        self.assertIn("--enable-advisory-notifications", unit)
        self.assertIn("--advisory-ntfy-topic van-telemetry", unit)
        self.assertIn("van-telemetry-web-tailscale.service", unit)
        self.assertIn("van-dtc-batch.path", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_web_listener_is_not_independently_boot_enabled(self):
        unit = (SYSTEMD_DIR / "van-telemetry-web.service").read_text()

        self.assertNotIn("WantedBy=multi-user.target", unit)
        self.assertNotIn("--enable-dtc-jobs", unit)

        tailscale = (
            SYSTEMD_DIR / "van-telemetry-web-tailscale.service"
        ).read_text()
        self.assertIn("--enable-dtc-jobs", tailscale)
        self.assertIn("--dtc-trusted-origin", tailscale)
        self.assertIn("EnvironmentFile=/etc/van-telemetry/tailscale-web.env", tailscale)
        self.assertIn("${VAN_TELEMETRY_TAILSCALE_BIND}", tailscale)
        self.assertIn("NoNewPrivileges=true", tailscale)
        self.assertIn("PartOf=van-telemetry.service", tailscale)
        self.assertIn("Requires=van-telemetry.service van-dtc-batch.path", tailscale)

    def test_dtc_worker_is_fixed_path_triggered_and_not_network_exposed(self):
        service = (SYSTEMD_DIR / "van-dtc-batch.service").read_text()
        path = (SYSTEMD_DIR / "van-dtc-batch.path").read_text()

        self.assertIn("tools/dtc_batch_request.py", service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("PartOf=van-telemetry.service", service)
        self.assertNotIn("AF_INET", service)
        self.assertNotIn("NoNewPrivileges=true", service)
        self.assertIn("dtc-batch.request.json", path)
        self.assertIn("PartOf=van-telemetry.service", path)


if __name__ == "__main__":
    unittest.main()
