#!/usr/bin/env python3
"""Read-only-by-default CLI client for the local telemetry broker."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.vehicle_data.api import TelemetryClient
from projects.vehicle_data.broker import DEFAULT_SOCKET


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--timeout", type=float, default=10.0)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("metrics")
    get = sub.add_parser("get")
    get.add_argument("metric", choices=("battery.voltage",))
    acquire = sub.add_parser("acquire")
    acquire.add_argument("metric", choices=("battery.voltage",))
    acquire.add_argument(
        "--mode",
        choices=("passive", "wake_if_asleep"),
        default="passive",
    )
    acquire.add_argument(
        "--confirm-wake",
        action="store_true",
        help="required for wake_if_asleep because it may transmit and power accessory rails",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if (
        args.command == "acquire"
        and args.mode == "wake_if_asleep"
        and not args.confirm_wake
    ):
        raise SystemExit(
            "wake_if_asleep requires --confirm-wake; it may transmit on CAN"
        )
    client = TelemetryClient(args.socket, timeout=args.timeout)
    if args.command == "status":
        status, payload = client.request("GET", "/v1/status")
    elif args.command == "metrics":
        status, payload = client.request("GET", "/v1/metrics")
    elif args.command == "get":
        status, payload = client.request(
            "GET", f"/v1/metrics/{args.metric}"
        )
    else:
        status, payload = client.request(
            "POST",
            f"/v1/acquisitions/{args.metric}",
            {"mode": args.mode},
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
