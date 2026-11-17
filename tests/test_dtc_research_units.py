"""Structural contract of the research units: the sandbox lines a reviewer relies on."""

import configparser
from pathlib import Path
import unittest

from projects.vehicle_data.model_cli_lock import LOCK_PATH
from projects.vehicle_data.dtc_research_contract import WORKER

SYSTEMD = Path(__file__).resolve().parents[1] / "projects" / "vehicle_data" / "systemd"


def service(name):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    text = (SYSTEMD / name).read_text()
    # Repeated keys (two BindReadOnlyPaths lines) are joined so each value can be checked.
    values = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.strip("[]")
        elif "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values.setdefault((section, key), []).append(value)
    return values


class ResearchUnitTests(unittest.TestCase):
    def test_worker_sandbox(self):
        unit = service("van-telemetry-research.service")
        get = lambda key: " ".join(unit.get(("Service", key), []))  # noqa: E731
        self.assertEqual(get("ProtectHome"), "tmpfs")
        self.assertEqual(get("ProtectSystem"), "strict")
        self.assertEqual(set(get("RestrictAddressFamilies").split()), {"AF_UNIX", "AF_INET", "AF_INET6"})
        for blocked in ("localhost", "link-local", "10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16",
                        "fc00::/7"):
            self.assertIn(blocked, get("IPAddressDeny").split())
        hidden = get("InaccessiblePaths").split()
        for path in ("-/run/van-telemetry", "-/run/van-telemetry-advisor", "-/var/lib/van-telemetry-advisor",
                     "-/var/lib/van-telemetry", "-/home/pi/dev/obd-things/tmp"):
            self.assertIn(path, hidden)
        self.assertEqual(get("ReadWritePaths"), WORKER["volume"])
        self.assertEqual(get("KillMode"), "control-group")
        self.assertEqual(get("Type"), "notify")
        self.assertTrue(get("WatchdogSec"))
        self.assertEqual(get("OOMScoreAdjust"), "800")
        self.assertNotIn(("Service", "Slice"), unit)
        self.assertIn("var-lib-vanresearch.mount", get_unit(unit, "Requires"))
        resolv = [v for v in unit[("Service", "BindReadOnlyPaths")] if v.endswith(":/etc/resolv.conf")]
        source = Path(resolv[0].split(":")[0].replace("/home/pi/dev/obd-things/", str(SYSTEMD.parents[2]) + "/"))
        self.assertTrue(source.is_file())
        self.assertNotIn("192.168.", source.read_text())

    def test_mount_and_tmpfiles(self):
        mount = service("var-lib-vanresearch.mount")
        self.assertEqual(mount[("Mount", "Where")], [WORKER["volume"]])
        self.assertEqual(set(mount[("Mount", "Options")][0].split(",")) >= {"loop", "nodev", "nosuid", "noexec"}, True)
        tmpfiles = (SYSTEMD / "van-assistant.tmpfiles.conf").read_text()
        self.assertIn(f"f {LOCK_PATH} 0640 pi pi -", tmpfiles)

    def test_advisor_unit_hides_the_research_volume(self):
        unit = service("van-telemetry-advisor.service")
        self.assertIn("-" + WORKER["volume"], " ".join(unit[("Service", "InaccessiblePaths")]).split())


def get_unit(unit, key):
    return " ".join(unit.get(("Unit", key), []))


if __name__ == "__main__":
    unittest.main()
