#!/usr/bin/env python3
"""Run every safe offline repository gate, failing at the first broken step."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Iterator


ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "projects/vehicle_data/dashboard"
# The paths the dashboard profile covers; docs cite this constant.
DASHBOARD_SCOPE = "projects/vehicle_data/dashboard/"
# Backend file types inside DASHBOARD_SCOPE fall outside the gate: repo-wide
# guards the gate doesn't run read them anywhere under projects/
# (test_no_legacy_can_topology.LIVE_CAN_SUFFIXES, and
# test_service_units_start_modules globs projects/**/*.service).
DASHBOARD_EXCLUDED_SUFFIXES = (".py", ".sh", ".service", ".socket", ".timer")

# Pytest modules that read DASHBOARD_SCOPE. tests/test_run_tests.py guards the
# list with a text scan; indirect readers are added by hand.
DASHBOARD_PYTEST_TARGETS = (
    # runs the dashboard's whole `node --test` suite
    "tests/test_dashboard_v2_node.py",
    # reads dashboard/screenshots/manifest.json and the images
    "tests/test_dashboard_screenshot_manifest.py",
    # one test runs node on dashboard/src/views/parked.helpers.js
    "tests/test_maintenance.py",
    # scans launch targets including the dashboard package.json, indirectly
    # through checks/backend_layout/targets.py
    "tests/test_backend_contracts.py",
)

LOCK_PATH = ROOT / "tmp/run_tests.lock"
LOCK_TIMEOUT_SECONDS = 1800
LOCK_POLL_SECONDS = 2


class Profile(str, Enum):
    FULL = "full"
    PYTEST_ONLY = "pytest-only"
    DASHBOARD = "dashboard"


@dataclass(frozen=True)
class Step:
    name: str
    command: tuple[str, ...]
    cwd: Path = ROOT
    timeout: int = 300


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"required test executable is missing: {name}")
    return executable


def run_step(step: Step, env: dict[str, str]) -> None:
    print(f"\n==> {step.name}", flush=True)
    subprocess.run(
        step.command,
        cwd=step.cwd,
        env=env,
        check=True,
        timeout=step.timeout,
    )


def run_steps(steps: tuple[Step, ...], env: dict[str, str]) -> int:
    """Execute the plan in order and name phases left unrun after failure."""
    for index, step in enumerate(steps):
        try:
            run_step(step, env)
        except (OSError, subprocess.SubprocessError) as exc:
            remaining = ", ".join(item.name for item in steps[index + 1 :]) or "none"
            print(f"FAILED: {step.name}: {exc}", file=sys.stderr)
            print(f"Not run after fail-fast stop: {remaining}", file=sys.stderr)
            return 1
    return 0


def preparation_steps(
    *, base_python: str, test_python: str, npm: str, scratch: Path
) -> tuple[Step, ...]:
    return (
        Step(
            "Isolated Python test environment",
            (base_python, "-m", "venv", str(scratch / "venv")),
            timeout=180,
        ),
        Step(
            "Python test dependency install",
            (
                test_python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--requirement",
                str(ROOT / "requirements-test.txt"),
            ),
            timeout=600,
        ),
        Step(
            "Python test dependency check",
            (test_python, "-c", "import cantools, isotp, numpy, pytest"),
            timeout=60,
        ),
        Step(
            "Pinned dashboard dependency install",
            (npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--prefer-offline"),
            cwd=DASHBOARD,
            timeout=300,
        ),
    )


def layout_step(test_python: str) -> Step:
    return Step(
        "Backend layout fixture and repository checks",
        (
            test_python,
            "-m",
            "unittest",
            "discover",
            "-s",
            "checks",
            "-p",
            "test_layout_*.py",
            "-v",
        ),
        timeout=600,
    )


def build_step(node: str, scratch: Path) -> Step:
    return Step(
        "Scratch dashboard production build",
        (node, "tools/build.mjs", "--out-dir", str(scratch / "dist")),
        cwd=DASHBOARD,
        timeout=180,
    )


def suite_steps(
    *,
    base_python: str,
    test_python: str,
    npm: str,
    node: str,
    scratch: Path,
    pytest_args: tuple[str, ...],
    profile: Profile,
) -> tuple[Step, ...]:
    """Build the explicit suite plan for a profile; callers execute it in order."""
    preparation = preparation_steps(
        base_python=base_python, test_python=test_python, npm=npm, scratch=scratch
    )

    def pytest(name: str, targets: tuple[str, ...], timeout: int) -> Step:
        return Step(
            name,
            (test_python, "-m", "pytest", "-q", "-ra", "-x", *targets, *pytest_args),
            timeout=timeout,
        )

    plans = {
        Profile.FULL: lambda: (
            pytest("Full Python and Node regression suite", ("tests",), 1800),
            layout_step(test_python),
            build_step(node, scratch),
        ),
        Profile.PYTEST_ONLY: lambda: (
            pytest("Full Python and Node regression suite", ("tests",), 1800),
        ),
        Profile.DASHBOARD: lambda: (
            pytest("Dashboard-scoped Python and Node tests", DASHBOARD_PYTEST_TARGETS, 900),
            layout_step(test_python),
            build_step(node, scratch),
        ),
    }
    return (*preparation, *plans[profile]())


def suite_env(scratch: Path, environ: dict[str, str]) -> dict[str, str]:
    """Child environment: scratch TMPDIR; npm's cache under XDG_CACHE_HOME, pip's in scratch."""
    env = dict(environ)
    env["TMPDIR"] = str(scratch.resolve())
    # Compute jobs set XDG_CACHE_HOME to a cache other jobs of this repo can write.
    # npm verifies every tarball against package-lock.json's integrity hashes, so it
    # may share that cache. requirements-test.txt has ranges and no hashes, so a
    # poisoned pip cache could change what installs: pip keeps a per-run cache.
    if env.get("XDG_CACHE_HOME"):
        if "npm_config_cache" not in env:
            env["npm_config_cache"] = str(Path(env["XDG_CACHE_HOME"]) / "npm")
        if "PIP_CACHE_DIR" not in env:
            env["PIP_CACHE_DIR"] = str(scratch.resolve() / "pip-cache")
    return env


class LockTimeout(RuntimeError):
    pass


@contextmanager
def checkout_lock(
    path: Path, timeout: float = LOCK_TIMEOUT_SECONDS, poll: float = LOCK_POLL_SECONDS
) -> Iterator[None]:
    """Hold an exclusive lock so two runs never share one checkout's node_modules.

    `npm ci` deletes dashboard/node_modules, which a concurrent run's Node
    tests are reading. Worktrees have their own ROOT, hence their own lock.
    """
    with open(path, "a") as handle:
        deadline = time.monotonic() + timeout
        announced = False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced:
                    print("Another run_tests.py in this checkout is running; waiting for it.",
                          flush=True)
                    announced = True
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"gave up after {timeout:g}s waiting for {path}; "
                        "another run_tests.py in this checkout still holds it"
                    )
                time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parse_args(argv: list[str] | None) -> tuple[Profile, tuple[str, ...]]:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--pytest-only",
        action="store_true",
        help="prepare dependencies and run pytest only; remaining arguments pass to pytest",
    )
    group.add_argument(
        "--dashboard",
        action="store_true",
        help=f"scoped gate for changes confined to {DASHBOARD_SCOPE}: "
        "dashboard-related pytest modules, layout checks and scratch build",
    )
    options, pytest_args = parser.parse_known_args(argv)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if pytest_args and not options.pytest_only:
        parser.error("pytest arguments require --pytest-only")
    if options.pytest_only:
        profile = Profile.PYTEST_ONLY
    elif options.dashboard:
        profile = Profile.DASHBOARD
    else:
        profile = Profile.FULL
    return profile, tuple(pytest_args)


DONE_MESSAGES = {
    Profile.FULL: "All offline repository gates passed.",
    Profile.PYTEST_ONLY: "Pytest gate passed.",
    Profile.DASHBOARD: "Dashboard scoped gate passed (covers change sets inside DASHBOARD_SCOPE only).",
}


def run_plan(profile: Profile, pytest_args: tuple[str, ...], npm: str, node: str) -> int:
    with tempfile.TemporaryDirectory(prefix="suite-", dir=ROOT / "tmp") as folder:
        scratch = Path(folder)
        steps = suite_steps(
            base_python=sys.executable,
            test_python=str(scratch / "venv/bin/python"),
            npm=npm,
            node=node,
            scratch=scratch,
            pytest_args=pytest_args,
            profile=profile,
        )
        return run_steps(steps, suite_env(scratch, dict(os.environ)))


def main(argv: list[str] | None = None, lock_timeout: float | None = None) -> int:
    profile, pytest_args = parse_args(argv)

    try:
        require_executable("git")
        npm = require_executable("npm")
        node = require_executable("node")
    except RuntimeError as exc:
        print(f"SETUP ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Python: {sys.version.splitlines()[0]} ({sys.executable})", flush=True)
    subprocess.run([node, "--version"], check=True, timeout=30)
    subprocess.run([npm, "--version"], check=True, timeout=30)

    (ROOT / "tmp/ecu_mapping").mkdir(parents=True, exist_ok=True)
    timeout = LOCK_TIMEOUT_SECONDS if lock_timeout is None else lock_timeout
    try:
        with checkout_lock(LOCK_PATH, timeout):
            if run_plan(profile, pytest_args, npm, node):
                return 1
    except LockTimeout as exc:
        print(f"LOCK ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"\n{DONE_MESSAGES[profile]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
