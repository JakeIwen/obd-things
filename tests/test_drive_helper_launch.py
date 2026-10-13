"""The broker starts its drive helpers as modules, and that launch really imports.

The helpers have no sys.path bootstrap, so this runs the exact command prefix and
working directory the supervisor hands to Popen (with ``--help``, which touches no CAN).
"""

import subprocess
import sys
import threading
import unittest

from projects.vehicle_data.broker import ActiveDriveSupervisor, AuxiliaryDriveSupervisor


class Process:
    stdout = iter(())

    def poll(self):
        return 0

    def wait(self, timeout=None):
        del timeout
        return 0

    def terminate(self):
        return None

    def kill(self):
        return None


class DriveHelperLaunchTests(unittest.TestCase):
    def test_each_supervisor_launch_starts_its_helper_module(self):
        cases = [
            (ActiveDriveSupervisor, "projects.vehicle_data.active_drive", "C-CAN telemetry owner"),
            (AuxiliaryDriveSupervisor, "projects.vehicle_data.bcan_auxiliary", "B-CAN ICS telemetry owner"),
        ]
        for supervisor_class, module, description in cases:
            with self.subTest(module=module):
                launches = []
                supervisor = supervisor_class(
                    channel="can7",
                    expected_usb_serial="serial-a",
                    expected_dev_id=0,
                    event_handler=lambda _event: None,
                    popen_factory=lambda command, **kwargs: launches.append((command, kwargs)) or Process(),
                )
                supervisor.run(threading.Event())

                command, kwargs = launches[0]
                self.assertEqual(command[:3], [sys.executable, "-m", module])
                result = subprocess.run(
                    [*command[:3], "--help"],
                    cwd=kwargs["cwd"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(description, result.stdout)


if __name__ == "__main__":
    unittest.main()
