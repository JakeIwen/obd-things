#!/usr/bin/env python3
"""Keep completed compute history within 2 GiB and at most 30 days old."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Sequence


REPO = Path(__file__).resolve().parents[1]
PYTHON = Path("/usr/bin/python3")
BACKEND = Path("/home/pi/van_compute/scripts/van_compute.py")
KEEP_DAYS = 30
MAX_HISTORY_BYTES = 2 * 1024**3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete expired jobs, then older uncited jobs until history fits 2 GiB",
    )
    return parser


def retention_command(*, apply: bool) -> list[str]:
    command = [
        str(PYTHON),
        str(BACKEND),
        "--root",
        str(REPO / "tmp" / "compute"),
        "prune",
        "--citations-root",
        str(REPO),
        "--keep-days",
        str(KEEP_DAYS),
        "--max-history-bytes",
        str(MAX_HISTORY_BYTES),
    ]
    if apply:
        command.append("--apply")
    return command


def run_retention(*, apply: bool) -> int:
    if not BACKEND.is_file():
        print(f"compute retention backend is missing: {BACKEND}", file=sys.stderr)
        return 2
    try:
        completed = subprocess.run(
            retention_command(apply=apply),
            check=False,
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
