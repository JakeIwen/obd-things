import configparser
import pathlib
import shlex
import subprocess
import tempfile
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

    def test_path_keeps_lifecycle_without_waiting_for_the_late_broker(self):
        path = configparser.ConfigParser(interpolation=None)
        path.read(SYSTEMD_DIR / "van-dtc-batch.path")
        unit = path["Unit"]
        self.assertTrue(unit.getboolean("DefaultDependencies", fallback=True))
        self.assertEqual(unit.get("After", "").split(), [])
        self.assertEqual(unit["Requires"], "van-telemetry.service")
        self.assertEqual(unit["PartOf"], "van-telemetry.service")
        self.assertEqual(path["Install"]["WantedBy"], "multi-user.target")

        broker = configparser.ConfigParser(interpolation=None)
        broker.read(SYSTEMD_DIR / "van-telemetry.service")
        for name in ("van-dtc-batch.path", "van-telemetry-web.service",
                     "van-telemetry-web-tailscale.service"):
            self.assertIn(name, broker["Unit"]["Wants"].split())
            self.assertNotIn(name, broker["Unit"].get("Requires", "").split())
            self.assertNotIn(name, broker["Unit"].get("After", "").split())

    def test_early_watchers_do_not_wait_for_normal_services(self):
        for filename in sorted(SYSTEMD_DIR.iterdir()):
            if filename.suffix not in {".path", ".timer", ".socket"}:
                continue
            with self.subTest(unit=filename.name):
                unit = configparser.ConfigParser(interpolation=None)
                unit.read(filename)
                if unit["Unit"].getboolean("DefaultDependencies", fallback=True):
                    after = unit["Unit"].get("After", "").split()
                    self.assertFalse([name for name in after
                                      if name.endswith(".service")])

    def test_lan_listener_orders_after_network_without_hardening_regression(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(SYSTEMD_DIR / "van-telemetry-web.service")
        self.assertIn("network-online.target", unit["Unit"]["After"].split())
        self.assertIn("network-online.target", unit["Unit"]["Wants"].split())
        self.assertIn("van-telemetry.service", unit["Unit"]["After"].split())
        self.assertEqual(unit["Unit"]["PartOf"], "van-telemetry.service")
        self.assertIn("--bind 127.0.0.1", unit["Service"]["ExecStart"])
        self.assertNotIn("ExecStartPre", unit["Service"])

        override = configparser.ConfigParser(interpolation=None)
        override.read(SYSTEMD_DIR / "van-telemetry-web.service.d/20-wait-lan.conf")
        service = override["Service"]
        self.assertEqual(service["TimeoutStartSec"], "90")
        self.assertEqual(service["RestrictAddressFamilies"], "AF_NETLINK")
        self.assertNotIn("ExecStart", service)
        self.assertEqual(unit["Service"]["Restart"], "on-failure")
        self.assertTrue(unit["Service"].getboolean("NoNewPrivileges"))

    def test_lan_wait_retries_until_exact_ipv4_address_is_present(self):
        override = configparser.ConfigParser(interpolation=None)
        override.read(SYSTEMD_DIR / "van-telemetry-web.service.d/20-wait-lan.conf")
        command = shlex.split(override["Service"]["ExecStartPre"])
        self.assertEqual(command[:2], ["/bin/sh", "-ec"])
        # Substitute only external reads/sleeps; execute the unit's actual loop.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            counter = root / "polls"
            counter.write_text("0\n")
            ip = root / "ip"
            ip.write_text(
                "#!/bin/sh\n"
                '[ "$*" = "-4 -o address show dev eth0" ] || exit 99\n'
                f"read count < {shlex.quote(str(counter))}\n"
                'count=$((count + 1))\n'
                f'printf "%s\\n" "$count" > {shlex.quote(str(counter))}\n'
                'case "$count" in\n'
                '1) exit 1 ;;\n'
                '2) exit 0 ;;\n'
                '3) printf "2: eth0 inet 192.168.6.10/24 scope global\\n" ;;\n'
                '4) printf "2: eth0 inet 192.168.6.1030/24 scope global\\n" ;;\n'
                '5) printf "2: eth0 inet 192.168.6.103/24 scope global\\n" ;;\n'
                '*) exit 99 ;;\n'
                'esac\n'
            )
            ip.chmod(0o700)
            command[2] = command[2].replace("/usr/sbin/ip", shlex.quote(str(ip)))
            command[2] = command[2].replace("/usr/bin/sleep 1", "printf 'retry\\n'")
            result = subprocess.run(command, capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["retry"] * 4)
            self.assertEqual(counter.read_text(), "5\n")


if __name__ == "__main__":
    unittest.main()
