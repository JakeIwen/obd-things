"""Every tracked systemd unit starts repo Python as a module from the repository root.

The scripts have no sys.path bootstrap, so a by-path ExecStart could not import lib.
"""

import configparser
import pathlib
import shlex
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKING_DIRECTORY = "/home/pi/dev/obd-things"


def launch_problems(unit_text: str) -> list[str]:
    """Reasons a unit's ExecStart breaks the module-launch contract (empty when it holds)."""
    unit = configparser.ConfigParser(interpolation=None, strict=False)
    unit.read_string(unit_text)
    service = unit["Service"]
    command = shlex.split(service["ExecStart"])
    if not pathlib.PurePath(command[0]).name.startswith("python"):
        return [f"runs repo script {word} by path" for word in command if word.endswith(".py")]
    problems = []
    if command[1:2] != ["-m"] or len(command) < 3:
        return [f"Python launch without -m: {command[:3]}"]
    if service.get("WorkingDirectory") != WORKING_DIRECTORY:
        problems.append(f"WorkingDirectory is {service.get('WorkingDirectory')!r}")
    if not REPO_ROOT.joinpath(*command[2].split(".")).with_suffix(".py").is_file():
        problems.append(f"module {command[2]} does not exist")
    return problems


def unit_text(exec_start: str, working_directory: str = WORKING_DIRECTORY) -> str:
    return f"[Service]\nWorkingDirectory={working_directory}\nExecStart={exec_start}\n"


class ServiceUnitModuleLaunchTests(unittest.TestCase):
    def test_tracked_units_hold_the_contract(self):
        # A glob, not git ls-files: the compute worker runs tests on a non-git snapshot.
        units = sorted(REPO_ROOT.glob("projects/**/*.service"))
        self.assertTrue(units)
        for filename in units:
            with self.subTest(unit=str(filename.relative_to(REPO_ROOT))):
                self.assertEqual(launch_problems(filename.read_text()), [])

    def test_contract_rejects_broken_launches(self):
        broken = {
            "missing -m": unit_text("/usr/bin/python3 projects.vehicle_data.broker serve"),
            "by path": unit_text("/usr/bin/python3 /home/pi/dev/obd-things/tools/dtc_scan.py"),
            "wrong cwd": unit_text("/usr/bin/python3 -m tools.dtc_scan", "/home/pi"),
            "no module": unit_text("/usr/bin/python3 -m tools.no_such_tool"),
            "shell by path": unit_text("/bin/sh -c true /home/pi/dev/obd-things/tools/dtc_scan.py"),
        }
        for label, text in broken.items():
            with self.subTest(label):
                self.assertNotEqual(launch_problems(text), [])


if __name__ == "__main__":
    unittest.main()
