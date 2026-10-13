"""Every tracked systemd unit starts repo Python as a module from the repository root.

The scripts have no sys.path bootstrap, so a by-path ExecStart could not import lib.
"""

import configparser
import pathlib
import shlex
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CODE_DIRS = ("tools/", "projects/", "lib/", "live_data/")


class ServiceUnitModuleLaunchTests(unittest.TestCase):
    def test_python_units_start_existing_modules_from_the_repository_root(self):
        # A glob, not git ls-files: the compute worker runs tests on a non-git snapshot.
        units = sorted(REPO_ROOT.glob("projects/**/*.service"))
        self.assertTrue(units)
        for filename in units:
            with self.subTest(unit=str(filename.relative_to(REPO_ROOT))):
                unit = configparser.ConfigParser(interpolation=None, strict=False)
                unit.read(filename)
                command = shlex.split(unit["Service"]["ExecStart"])
                self.assertFalse([word for word in command if word.endswith(".py")
                                  and any(part in word for part in CODE_DIRS)], command)
                if command[0].endswith("python3") and command[1] == "-m":
                    self.assertEqual(unit["Service"]["WorkingDirectory"], "/home/pi/dev/obd-things")
                    module = REPO_ROOT.joinpath(*command[2].split(".")).with_suffix(".py")
                    self.assertTrue(module.is_file(), command[2])


if __name__ == "__main__":
    unittest.main()
