#!/usr/bin/env python3
"""Manage fail-closed topology and external-operation gates for unattended CAN wake."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import can_operation_state as operation_state  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    topology_set = subparsers.add_parser(
        "topology-set",
        help="record the physically connected branch for this boot",
    )
    topology_set.add_argument(
        "bus", choices=("c-can", "b-can", "can-ch", "unknown")
    )
    topology_set.add_argument("--channel", default="can0")
    topology_set.add_argument("--pair", default="")
    topology_set.add_argument("--source", required=True)
    topology_set.add_argument("--note", default="")

    topology_show = subparsers.add_parser("topology-show")
    topology_show.add_argument("--channel", default="can0")

    inhibit_begin = subparsers.add_parser("inhibit-begin")
    inhibit_begin.add_argument("name")
    inhibit_begin.add_argument("--channel", default="can0")
    inhibit_begin.add_argument("--reason", required=True)

    inhibit_end = subparsers.add_parser("inhibit-end")
    inhibit_end.add_argument("name")

    status = subparsers.add_parser("status")
    status.add_argument("--channel", default="can0")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "topology-set":
            result = asdict(
                operation_state.set_topology(
                    args.channel,
                    args.bus,
                    pair=args.pair,
                    source=args.source,
                    note=args.note,
                )
            )
        elif args.command == "topology-show":
            result = asdict(operation_state.load_topology(args.channel))
        elif args.command == "inhibit-begin":
            result = operation_state.begin_inhibit(
                args.name,
                channel=args.channel,
                reason=args.reason,
            )
        elif args.command == "inhibit-end":
            result = {
                "name": args.name,
                "removed": operation_state.end_inhibit(args.name),
            }
        else:
            result = operation_state.status(args.channel)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
