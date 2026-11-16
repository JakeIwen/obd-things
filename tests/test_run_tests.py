import contextlib
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import signal
import sys
import subprocess
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import run_tests


class SuitePlanTests(unittest.TestCase):
    def test_compute_arguments_preserve_pytest_help_after_separator(self):
        self.assertEqual(
            run_tests.parse_args(["--pytest-only", "--", "--help"]),
            (run_tests.Profile.PYTEST_ONLY, ("--help",)),
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
            run_tests.parse_args(["--dashboard"]), (run_tests.Profile.DASHBOARD, ())
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

    def make_read_only_checkout(self, root: Path) -> None:
        """A tree shaped like a Pi-local van-compute snapshot: files 0400, dirs 0500."""
        (root / "pkg").mkdir(parents=True)
        (root / "pkg/module.py").write_text("VALUE = 1\n")
        (root / "tmp").mkdir()
        (root / "tmp/run_tests.lock").write_text("")
        (root / ".git").write_text("gitdir: elsewhere\n")
        (root / "run_tests.py").write_text(textwrap.dedent("""
            import os, sys
            from pathlib import Path
            here = Path(__file__).resolve().parent
            (here / "tmp").mkdir()  # fails in the read-only original
            first_path = os.environ["PYTHONPATH"].split(os.pathsep)[0]
            same_args = sys.argv[1:] == ["--dashboard"]
            raise SystemExit(3 if same_args and Path(first_path) == here else 4)
        """))
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            path.chmod(0o500 if path.is_dir() else 0o400)
        root.chmod(0o500)

    def test_read_only_checkout_reruns_from_writable_copy_with_same_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "source"
            self.make_read_only_checkout(root)
            out = StringIO()
            try:
                env = {"PYTHONPATH": f"{root}{os.pathsep}/elsewhere"}
                with mock.patch.dict(os.environ, env), redirect_stdout(out):
                    status = run_tests.run_from_copy(root, ["--dashboard"])
            finally:
                for path in [root, *root.rglob("*")]:
                    path.chmod(0o700)
        # 3: the child ran in a writable copy with the same arguments and a
        # PYTHONPATH pointing at the copy; its exit status passes through.
        self.assertEqual(status, 3)
        self.assertIn("is read-only; running from a scratch copy", out.getvalue())

    def test_checkout_copy_is_writable_and_skips_snapshot_exclusions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "source"
            self.make_read_only_checkout(root)
            try:
                copy = run_tests.copy_checkout(root, Path(folder) / "copy")
                self.assertEqual((copy / "pkg/module.py").read_text(), "VALUE = 1\n")
                (copy / "pkg/new.py").write_text("")
                (copy / "pkg/module.py").write_text("VALUE = 2\n")
                self.assertFalse((copy / "tmp").exists())
                self.assertFalse((copy / ".git").exists())
            finally:
                for path in [root, *root.rglob("*")]:
                    path.chmod(0o700)

    def test_pythonpath_entries_for_the_checkout_follow_the_copy(self):
        old, new = Path("/job/source"), Path("/job/tmp/copy")
        self.assertEqual(
            run_tests.retarget_pythonpath(f"/job/source{os.pathsep}/other", old, new),
            f"/job/tmp/copy{os.pathsep}/other",
        )
        self.assertIsNone(run_tests.retarget_pythonpath(None, old, new))

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


COPY_STUB = """
import os, signal, subprocess, sys, time
from pathlib import Path
mode = sys.argv[1]
if mode == "self-term":
    os.kill(os.getpid(), signal.SIGTERM)
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # the grandchild inherits this
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
Path(os.environ["SUITE_TEST_MARKER"]).write_text(f"{os.getpid()} {grandchild.pid}")
time.sleep(120)
"""

# Runs the real wrapper in its own process, as `python3 run_tests.py` would.
COPY_DRIVER = """
import sys
from pathlib import Path
import run_tests
run_tests.CHILD_STOP_GRACE_SECONDS = 0.5
run_tests.exit_with(run_tests.run_from_copy(Path(sys.argv[1]), [sys.argv[2]]))
"""


def pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # an unreaped zombie is already dead
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z"
    except OSError:
        return False


class CopyRunProcessTests(unittest.TestCase):
    """The read-only-checkout wrapper as a real process: cancellation and exit status."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.root = self.base / "source"
        self.scratch = self.base / "scratch"
        self.marker = self.base / "marker"
        (self.root / "pkg").mkdir(parents=True)
        (self.root / "run_tests.py").write_text(COPY_STUB)
        self.scratch.mkdir()
        self.addCleanup(lambda: [p.chmod(0o700) for p in (self.root, *self.root.rglob("*"))])

    def start(self, mode: str, *, tmpdir: Path | None = None) -> subprocess.Popen:
        for path in (self.root, *self.root.rglob("*")):
            if tmpdir is None or path != tmpdir:
                path.chmod(0o500 if path.is_dir() else 0o400)
        env = {
            **os.environ,
            "TMPDIR": str(tmpdir or self.scratch),
            "SUITE_TEST_MARKER": str(self.marker),
            "PYTHONPATH": str(Path(run_tests.__file__).resolve().parent),
        }
        return subprocess.Popen(
            [sys.executable, "-c", COPY_DRIVER, str(self.root), mode],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )

    def wait_for_marker(self) -> list[int]:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.marker.exists() and self.marker.read_text():
                return [int(pid) for pid in self.marker.read_text().split()]
            time.sleep(0.05)
        self.fail("the copy's runner never started")

    def assert_tree_and_copy_gone(self, pids: list[int]) -> None:
        deadline = time.monotonic() + 10
        while not all(pid_gone(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([pid for pid in pids if not pid_gone(pid)], [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def cancel(self, mode: str) -> None:
        wrapper = self.start(mode)
        pids = self.wait_for_marker()
        wrapper.send_signal(signal.SIGTERM)  # the wrapper alone, as `kill PID` would
        try:
            wrapper.wait(timeout=30)
        finally:
            for pid in pids:  # never leave a sleeper behind if an assertion fails
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.assertEqual(wrapper.returncode, -signal.SIGTERM)
        self.assert_tree_and_copy_gone(pids)

    def test_cancellation_stops_the_copied_run_tree_and_removes_the_copy(self):
        self.cancel("sleep")

    def test_cancellation_escalates_to_sigkill_when_the_tree_ignores_sigterm(self):
        self.cancel("ignore-term")

    def test_copied_run_killed_by_a_signal_ends_the_wrapper_by_that_signal(self):
        wrapper = self.start("self-term")
        wrapper.wait(timeout=60)
        # Not 241, the value SystemExit(-15) would give.
        self.assertEqual(wrapper.returncode, -signal.SIGTERM)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_tmpdir_inside_the_checkout_is_refused_before_copying(self):
        inside = self.root / ".scratch"
        inside.mkdir()
        # A writable folder inside an otherwise read-only checkout.
        wrapper = self.start("sleep", tmpdir=inside)
        _, errors = wrapper.communicate(timeout=60)
        self.assertEqual(wrapper.returncode, 2)
        self.assertIn("is inside the read-only checkout", errors)
        self.assertEqual(list(inside.iterdir()), [])
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
