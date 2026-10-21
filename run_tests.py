#!/usr/bin/env python3
"""Run every safe offline repository gate, failing at the first broken step."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "projects/vehicle_data/dashboard"


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


def suite_steps(
    *,
    base_python: str,
    test_python: str,
    npm: str,
    node: str,
    scratch: Path,
    pytest_args: tuple[str, ...],
    pytest_only: bool,
) -> tuple[Step, ...]:
    """Build the explicit suite plan; callers execute it in order."""
    preparation = (
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
            (npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"),
            cwd=DASHBOARD,
            timeout=300,
        ),
    )
    regression = Step(
        "Full Python and Node regression suite",
        (test_python, "-m", "pytest", "-q", "-ra", "-x", "tests", *pytest_args),
        timeout=1800,
    )
    if pytest_only:
        return (*preparation, regression)
    return (
        *preparation,
        regression,
        Step(
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
        ),
        Step(
            "Scratch dashboard production build",
            (node, "tools/build.mjs", "--out-dir", str(scratch / "dist")),
            cwd=DASHBOARD,
            timeout=180,
        ),
    )


def parse_args(argv: list[str] | None) -> tuple[bool, tuple[str, ...]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pytest-only",
        action="store_true",
        help="prepare dependencies and run pytest only; remaining arguments pass to pytest",
    )
    options, pytest_args = parser.parse_known_args(argv)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if pytest_args and not options.pytest_only:
        parser.error("pytest arguments require --pytest-only")
    return options.pytest_only, tuple(pytest_args)


def main(argv: list[str] | None = None) -> int:
    pytest_only, pytest_args = parse_args(argv)

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

    required_tmp = ROOT / "tmp/ecu_mapping"
    required_tmp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="suite-", dir=ROOT / "tmp") as scratch:
        suite_env = os.environ.copy()
        suite_env["TMPDIR"] = str(Path(scratch).resolve())
        steps = suite_steps(
            base_python=sys.executable,
            test_python=str(Path(scratch) / "venv/bin/python"),
            npm=npm,
            node=node,
            scratch=Path(scratch),
            pytest_args=pytest_args,
            pytest_only=pytest_only,
        )
        if run_steps(steps, suite_env):
            return 1
    if pytest_only:
        print("\nPytest gate passed.", flush=True)
    else:
        print("\nAll offline repository gates passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
