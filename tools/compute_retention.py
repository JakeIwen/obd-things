#!/usr/bin/env python3
"""Plan or apply 30-day van-compute retention for this checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence


REPO = Path(__file__).resolve().parents[1]
PYTHON = Path("/usr/bin/python3")
VAN_COMPUTE_RELEASE = Path("/home/pi/van_compute/current")
CHILD = """\
from van_compute import queue, retention

expected = {"done": 60, "failed": 31}
if retention.RETENTION_DAYS != expected:
    raise SystemExit(
        "van-compute retention policy changed; refusing the unchecked override: "
        + repr(retention.RETENTION_DAYS)
    )
retention.RETENTION_DAYS = {"done": 30, "failed": 30}
raise SystemExit(queue.main())
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete successful and failed jobs eligible after 30 days",
    )
    return parser


def retention_command(*, apply: bool) -> list[str]:
    command = [
        str(PYTHON),
        "-P",
        "-c",
        CHILD,
        "--root",
        str(REPO / "tmp" / "compute"),
        "prune",
        "--citations-root",
        str(REPO),
    ]
    if apply:
        command.append("--apply")
    return command


def run_retention(*, apply: bool) -> int:
    package = VAN_COMPUTE_RELEASE / "van_compute"
    if not package.is_dir():
        print(f"compute retention backend is missing: {package}", file=sys.stderr)
        return 2
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(VAN_COMPUTE_RELEASE)
    try:
        completed = subprocess.run(
            retention_command(apply=apply),
            check=False,
            env=environment,
        )
    except OSError as exc:
        print(f"could not start compute retention backend: {exc}", file=sys.stderr)
        return 2
    return completed.returncode


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_retention(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
