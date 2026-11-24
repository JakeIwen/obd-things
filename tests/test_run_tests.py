from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

import run_tests


class RedirectTests(unittest.TestCase):
    def test_target_narrowing(self):
        for args, default in (((), True), (("-k", "expr"), True),
                              (("-k", "tests"), True),
                              (("tests/test_run_tests.py",), False),
                              (("tests/test_run_tests.py::SuitePlanTests",), False)):
            with self.subTest(args=args):
                steps = run_tests.suite_steps(base_python="python", test_python="python",
                    npm="npm", node="node", scratch=Path("/tmp"), pytest_args=args,
                    profile=run_tests.Profile.PYTEST_ONLY)
                command = steps[-1].command
                self.assertEqual(command[6:7] == ("tests",), default)

    def test_redirect_profiles(self):
        cases = (([], "repo-full-suite"), (["--dashboard"], "repo-dashboard-suite"),
                 (["--pytest-only", "--", "-k", "expr"], "repo-tests"))
        for args, task in cases:
            with self.subTest(task=task), patch.object(run_tests, "VAN_COMPUTE", Path("/")), \
                 patch.object(run_tests, "ROOT", Path("/tmp")), \
                 patch.object(run_tests, "mac_worker_available", return_value=True), \
                 patch.object(run_tests, "run_plan", side_effect=AssertionError), \
                 redirect_stderr(StringIO()) as errors:
                self.assertEqual(run_tests.main(args), 3)
                self.assertIn(task, errors.getvalue())
                self.assertIn("--source-root /tmp", errors.getvalue())
                if task == "repo-tests":
                    self.assertIn("--arg=-k --arg=expr", errors.getvalue())

    def test_probe_errors_are_unavailable(self):
        results = [OSError("missing"), subprocess.TimeoutExpired("probe", 15),
                   subprocess.CalledProcessError(1, "probe")]
        for error in results:
            with self.subTest(error=error), patch.object(run_tests.subprocess, "run", side_effect=error):
                self.assertFalse(run_tests.mac_worker_available())
        with patch.object(run_tests.subprocess, "run") as run:
            run.return_value.stdout = "not json"
            self.assertFalse(run_tests.mac_worker_available())

    def test_local_paths_run_plan_without_real_priority_changes(self):
        cases = ((True, False, False), (True, True, False), (False, False, False))
        for exists, local, available in cases:
            with self.subTest(exists=exists, local=local), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                compute = root if exists else root / "missing"
                args = ["--local"] if local else []
                with patch.object(run_tests, "ROOT", root), \
                     patch.object(run_tests, "LOCK_PATH", root / "lock"), \
                     patch.object(run_tests, "VAN_COMPUTE", compute), \
                     patch.object(run_tests, "mac_worker_available", return_value=available) as probe, \
                     patch.object(run_tests, "lower_local_priority") as priority, \
                     patch.object(run_tests, "require_executable", side_effect=lambda name: name), \
                     patch.object(run_tests.subprocess, "run"), \
                     patch.object(run_tests, "run_plan", return_value=0) as plan, \
                     redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(run_tests.main(args), 0)
                self.assertEqual(probe.call_count, int(exists and not local))
                self.assertEqual(priority.call_count, int(exists))
                plan.assert_called_once()


class SuitePlanTests(unittest.TestCase):
    def test_compute_arguments_preserve_pytest_help_after_separator(self):
        self.assertEqual(
            run_tests.parse_args(["--pytest-only", "--", "--help"]),
            (run_tests.Profile.PYTEST_ONLY, ("--help",), False),
        )

    def plan(self, scratch: Path, *, profile):
        return run_tests.suite_steps(
            base_python="/runtime/python3",
            test_python="/scratch/venv/bin/python",
            npm="/runtime/npm",
            node="/runtime/node",
            scratch=scratch,
            pytest_args=("-k", "focused_case"),
            profile=profile,
        )

    def test_pytest_only_prepares_dependencies_and_forwards_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            steps = self.plan(Path(folder), profile=run_tests.Profile.PYTEST_ONLY)
        self.assertEqual(steps[-1].command[-2:], ("-k", "focused_case"))
        self.assertIn("-x", steps[-1].command)
        self.assertFalse(any("--out-dir" in step.command for step in steps))

    def test_full_plan_checks_layout_and_builds_only_in_scratch(self):
        with tempfile.TemporaryDirectory() as folder:
            scratch = Path(folder)
            steps = self.plan(scratch, profile=run_tests.Profile.FULL)
            build = steps[-1]
        self.assertEqual(steps[-2].command[-3:], ("-p", "test_layout_*.py", "-v"))
        self.assertEqual(build.command[-2], "--out-dir")
        self.assertEqual(Path(build.command[-1]), scratch / "dist")
        self.assertNotEqual(Path(build.command[-1]), run_tests.DASHBOARD / "dist")

    def test_dashboard_plan_targets_scope_with_layout_and_scratch_build(self):
        with tempfile.TemporaryDirectory() as folder:
            scratch = Path(folder)
            steps = self.plan(scratch, profile=run_tests.Profile.DASHBOARD)
        pytest = next(s for s in steps if "pytest" in s.command)
        for target in run_tests.DASHBOARD_PYTEST_TARGETS:
            self.assertIn(target, pytest.command)
        self.assertNotIn("tests", pytest.command)
        self.assertIn("-x", pytest.command)
        self.assertEqual(steps[-2].command[-3:], ("-p", "test_layout_*.py", "-v"))
        self.assertEqual(Path(steps[-1].command[-1]), scratch / "dist")

    def test_dashboard_profile_takes_no_passthrough_and_excludes_pytest_only(self):
        self.assertEqual(
            run_tests.parse_args(["--dashboard"]), (run_tests.Profile.DASHBOARD, (), False)
        )
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                run_tests.parse_args(["--dashboard", "-k", "x"])
            with self.assertRaises(SystemExit):
                run_tests.parse_args(["--dashboard", "--pytest-only"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            run_tests.parse_args(["-k", "x"])

    def test_dashboard_pytest_targets_cover_every_test_reading_the_dashboard(self):
        """Tripwire, not proof of completeness: a text scan for the dashboard path.

        Indirect readers such as test_backend_contracts.py are listed by hand.
        """
        tests = Path(run_tests.ROOT, "tests")
        readers = {
            f"tests/{path.name}"
            for path in tests.glob("test_*.py")
            if "vehicle_data/dashboard" in path.read_text(encoding="utf-8")
            and path.name != "test_run_tests.py"
        }
        listed = set(run_tests.DASHBOARD_PYTEST_TARGETS)
        self.assertEqual(readers - listed, set())
        for target in listed:
            self.assertTrue(Path(run_tests.ROOT, target).is_file(), target)

    def test_dashboard_scope_excludes_file_types_repo_wide_guards_scan(self):
        from tests.test_no_legacy_can_topology import LIVE_CAN_SUFFIXES

        excluded = set(run_tests.DASHBOARD_EXCLUDED_SUFFIXES)
        self.assertLessEqual(set(LIVE_CAN_SUFFIXES), excluded)
        # test_service_units_start_modules globs projects/**/*.service.
        self.assertIn(".service", excluded)

    def test_npm_install_prefers_offline_and_cache_follows_xdg(self):
        with tempfile.TemporaryDirectory() as folder:
            steps = self.plan(Path(folder), profile=run_tests.Profile.FULL)
            install = next(s for s in steps if s.command[1] == "ci")
            self.assertIn("--prefer-offline", install.command)
            env = run_tests.suite_env(Path(folder), {"XDG_CACHE_HOME": "/cache"})
            self.assertEqual(env["npm_config_cache"], "/cache/npm")
            # pip stays out of the shared cache: its requirements carry no hashes.
            self.assertEqual(
                Path(env["PIP_CACHE_DIR"]), Path(folder).resolve() / "pip-cache"
            )
            explicit = run_tests.suite_env(
                Path(folder), {"XDG_CACHE_HOME": "/cache", "npm_config_cache": "/mine"}
            )
            self.assertEqual(explicit["npm_config_cache"], "/mine")
            plain = run_tests.suite_env(Path(folder), {})
            self.assertNotIn("npm_config_cache", plain)
            self.assertNotIn("PIP_CACHE_DIR", plain)

    def test_checkout_lock_times_out_while_another_process_holds_it(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "run_tests.lock"
            holder = subprocess.Popen(
                [sys.executable, "-c", textwrap.dedent(f"""
                    import fcntl, sys, time
                    h = open({str(lock)!r}, "a")
                    fcntl.flock(h, fcntl.LOCK_EX)
                    print("held", flush=True)
                    time.sleep(30)
                """)],
                stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                out = StringIO()
                started = time.monotonic()
                with redirect_stdout(out), self.assertRaises(run_tests.LockTimeout):
                    with run_tests.checkout_lock(lock, timeout=0.5, poll=0.1):
                        self.fail("lock should be held elsewhere")
                self.assertGreaterEqual(time.monotonic() - started, 0.5)
                self.assertIn("Another run_tests.py in this checkout", out.getvalue())
            finally:
                holder.kill()
                holder.wait()
            with run_tests.checkout_lock(lock, timeout=1, poll=0.1):
                pass

    @unittest.skipIf(os.geteuid() == 0, "root writes through directory permissions")
    def test_read_only_checkout_is_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder) / "checkout"
            checkout.mkdir()
            shutil.copy2(run_tests.__file__, checkout / "run_tests.py")
            checkout.chmod(0o500)
            try:
                result = subprocess.run(
                    [sys.executable, "run_tests.py"],
                    cwd=checkout,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            finally:
                checkout.chmod(0o700)
            self.assertEqual(result.returncode, 2)
            self.assertIn("SETUP ERROR: checkout", result.stderr)
            self.assertIn("is read-only", result.stderr)
            self.assertIn("Safe automated suite", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(sorted(path.name for path in checkout.iterdir()), ["run_tests.py"])

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
