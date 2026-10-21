import configparser
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tools import compute_retention


REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "tools" / "systemd"


class ComputeRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        self.record = self.root / "arguments.json"
        self.backend = self.root / "van_compute.py"
        self.backend.write_text(
            """import argparse
import json
import os
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--root')
parser.add_argument('command', choices=['prune'])
parser.add_argument('--citations-root')
parser.add_argument('--keep-days', type=int, required=True)
parser.add_argument('--max-history-bytes', type=int, required=True)
parser.add_argument('--apply', action='store_true')
parser.parse_args()
Path(os.environ['COMPUTE_RETENTION_TEST_RECORD']).write_text(
    json.dumps(sys.argv[1:]), encoding='utf-8'
)
raise SystemExit(int(os.environ.get('COMPUTE_RETENTION_TEST_EXIT', '0')))
""",
            encoding="utf-8",
        )
        self.constants = (
            mock.patch.object(compute_retention, "PYTHON", Path(sys.executable)),
            mock.patch.object(compute_retention, "BACKEND", self.backend),
            mock.patch.object(compute_retention, "REPO", self.repo),
            mock.patch.dict(
                os.environ,
                {"COMPUTE_RETENTION_TEST_RECORD": str(self.record)},
                clear=False,
            ),
        )
        for patcher in self.constants:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.constants):
            patcher.stop()
        self.temporary.cleanup()

    def recorded_arguments(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_default_runs_backend_plan_without_apply(self):
        self.assertEqual(compute_retention.main([]), 0)
        self.assertEqual(
            self.recorded_arguments(),
            [
                "--root",
                str(self.repo / "tmp" / "compute"),
                "prune",
                "--citations-root",
                str(self.repo),
                "--keep-days",
                "30",
                "--max-history-bytes",
                "2147483648",
            ],
        )

    def test_apply_is_forwarded_explicitly(self):
        self.assertEqual(compute_retention.main(["--apply"]), 0)
        self.assertEqual(self.recorded_arguments()[-1], "--apply")

    def test_backend_exit_status_is_preserved(self):
        with mock.patch.dict(
            os.environ, {"COMPUTE_RETENTION_TEST_EXIT": "75"}, clear=False
        ):
            self.assertEqual(compute_retention.main([]), 75)

    def test_old_backend_refuses_unsupported_cap_without_work(self):
        self.backend.write_text(
            "import argparse\nargparse.ArgumentParser().parse_args()\n",
            encoding="utf-8",
        )

        self.assertEqual(compute_retention.main(["--apply"]), 2)
        self.assertFalse(self.record.exists())

    def test_missing_backend_has_clear_error(self):
        stderr = StringIO()
        with mock.patch.object(
            compute_retention, "BACKEND", self.root / "missing"
        ):
            with redirect_stderr(stderr):
                result = compute_retention.main([])

        self.assertEqual(result, 2)
        self.assertIn("compute retention backend is missing", stderr.getvalue())

    def test_backend_start_error_has_clear_error(self):
        stderr = StringIO()
        with mock.patch.object(compute_retention, "PYTHON", self.root / "missing-python"):
            with redirect_stderr(stderr):
                result = compute_retention.main([])

        self.assertEqual(result, 2)
        self.assertIn("could not start compute retention backend", stderr.getvalue())

    def test_help_and_invalid_flag_use_argparse(self):
        stdout = StringIO()
        with redirect_stdout(stdout), self.assertRaisesRegex(SystemExit, "0"):
            compute_retention.main(["--help"])
        self.assertIn("--apply", stdout.getvalue())

        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaisesRegex(SystemExit, "2"):
            compute_retention.main(["--unknown"])
        self.assertIn("unrecognized arguments: --unknown", stderr.getvalue())


class ComputeRetentionSystemdTests(unittest.TestCase):
    @staticmethod
    def read_unit(name):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read(SYSTEMD / name)
        return parser

    def test_service_runs_repo_module_with_fixed_scope_and_apply(self):
        service = self.read_unit("obd-compute-retention.service")["Service"]

        self.assertEqual(service["User"], "pi")
        self.assertEqual(service["WorkingDirectory"], "/home/pi/dev/obd-things")
        self.assertEqual(
            service["ExecStart"],
            "/usr/bin/python3 -m tools.compute_retention --apply",
        )
        writable = service["ReadWritePaths"].split()
        self.assertIn("/home/pi/dev/obd-things/tmp/compute", writable)
        self.assertIn("/home/pi/van_compute/scripts", writable)

    def test_timer_is_persistent_and_hourly(self):
        timer = self.read_unit("obd-compute-retention.timer")["Timer"]

        self.assertTrue(timer.getboolean("Persistent"))
        self.assertEqual(timer["OnCalendar"].lower(), "hourly")
        self.assertEqual(timer["RandomizedDelaySec"], "5min")
        self.assertEqual(timer["Unit"], "obd-compute-retention.service")

    def test_service_module_exists(self):
        self.assertTrue((REPO / "tools" / "compute_retention.py").is_file())

    def test_path_unit_triggers_on_both_completed_states(self):
        path = SYSTEMD / "obd-compute-retention.path"
        watched = [
            line.partition("=")[2]
            for line in path.read_text().splitlines()
            if line.startswith("PathChanged=")
        ]
        self.assertEqual(set(watched), {
            "/home/pi/dev/obd-things/tmp/compute/done",
            "/home/pi/dev/obd-things/tmp/compute/failed",
        })
        self.assertEqual(
            self.read_unit(path.name)["Path"]["Unit"],
            "obd-compute-retention.service",
        )


if __name__ == "__main__":
    unittest.main()
