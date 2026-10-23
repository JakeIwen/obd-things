from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import unittest

import run_tests


class SuitePlanTests(unittest.TestCase):
    def test_compute_arguments_preserve_pytest_help_after_separator(self):
        self.assertEqual(
            run_tests.parse_args(["--pytest-only", "--", "--help"]),
            (True, ("--help",)),
        )

    def plan(self, scratch: Path, *, pytest_only: bool):
        return run_tests.suite_steps(
            base_python="/runtime/python3",
            test_python="/scratch/venv/bin/python",
            npm="/runtime/npm",
            node="/runtime/node",
            scratch=scratch,
            pytest_args=("-k", "focused_case"),
            pytest_only=pytest_only,
        )

    def test_pytest_only_prepares_dependencies_and_forwards_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            steps = self.plan(Path(folder), pytest_only=True)
        self.assertEqual(steps[-1].command[-2:], ("-k", "focused_case"))
        self.assertIn("-x", steps[-1].command)
        self.assertFalse(any("--out-dir" in step.command for step in steps))

    def test_full_plan_checks_layout_and_builds_only_in_scratch(self):
        with tempfile.TemporaryDirectory() as folder:
            scratch = Path(folder)
            steps = self.plan(scratch, pytest_only=False)
            build = steps[-1]
        self.assertEqual(steps[-2].command[-3:], ("-p", "test_layout_*.py", "-v"))
        self.assertEqual(build.command[-2], "--out-dir")
        self.assertEqual(Path(build.command[-1]), scratch / "dist")
        self.assertNotEqual(Path(build.command[-1]), run_tests.DASHBOARD / "dist")

    def test_execution_stops_after_real_child_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            steps = (
                run_tests.Step("failure", (sys.executable, "-c", "raise SystemExit(7)"), root),
                run_tests.Step("later phase", (
                    sys.executable, "-c", "from pathlib import Path; Path('later').touch()"
                ), root),
            )
            errors = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(errors):
                status = run_tests.run_steps(steps, os.environ.copy())
            self.assertEqual(status, 1)
            self.assertFalse((root / "later").exists())
            self.assertIn("FAILED: failure", errors.getvalue())
            self.assertIn("Not run after fail-fast stop: later phase", errors.getvalue())

    def test_execution_preserves_order_and_child_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            steps = (
                run_tests.Step("write", (sys.executable, "-c",
                    "import os; from pathlib import Path; "
                    "Path('result').write_text(os.environ['OBD_SUITE_VALUE'])"), root),
                run_tests.Step("read", (sys.executable, "-c",
                    "from pathlib import Path; assert Path('result').read_text() == 'fixture'"), root),
            )
            env = {**os.environ, "OBD_SUITE_VALUE": "fixture"}
            with redirect_stdout(StringIO()):
                status = run_tests.run_steps(steps, env)
            self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
