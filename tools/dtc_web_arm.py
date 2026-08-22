#!/usr/bin/env python3
"""Create one short-lived local authorization for the Tailscale DTC UI."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.dtc_web import ArmTokenStore, DEFAULT_ARM_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ttl-seconds", type=int, default=300)
    parser.add_argument("--arm-file", default=str(DEFAULT_ARM_PATH))
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        issued = ArmTokenStore(args.arm_file).issue(ttl_seconds=args.ttl_seconds)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    expires = datetime.fromtimestamp(
        float(issued["expires_at_epoch"]), timezone.utc
    ).isoformat()
    print("One-use parked DTC authorization (do not share it):")
    print(issued["token"])
    print(f"Expires: {expires}")
    print(
        "Use only with the transmission in Park, ignition ON, engine OFF, "
        "and the vehicle stationary. The worker re-proves those conditions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
