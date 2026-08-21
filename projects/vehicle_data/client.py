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
from projects.vehicle_data.metrics import METRICS


GET_METRICS = tuple(sorted(METRICS))
ACQUIRE_METRICS = tuple(
    sorted(
        name
        for name, definition in METRICS.items()
        if definition.allowed_acquisition_modes
    )
)
PUBLISH_METRICS = tuple(
    sorted(
        name
        for name, definition in METRICS.items()
        if any(source.publisher_allowed for source in definition.sources)
    )
)


def scalar_json(value: str):
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            "value must be a JSON scalar (quote string values)"
        ) from exc
    if decoded is None or isinstance(decoded, (list, dict)):
        raise argparse.ArgumentTypeError("value must be a non-null JSON scalar")
    return decoded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--timeout", type=float, default=10.0)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("metrics")
    get = sub.add_parser("get")
    get.add_argument("metric", choices=GET_METRICS)
    acquire = sub.add_parser("acquire")
    acquire.add_argument("metric", choices=ACQUIRE_METRICS)
    acquire.add_argument(
        "--mode",
        choices=("passive",),
        default="passive",
    )
    publish = sub.add_parser(
        "publish",
        help="publish one exact allowlisted observation over the local Unix API",
    )
    publish.add_argument("metric", choices=PUBLISH_METRICS)
    publish.add_argument("--value", required=True, type=scalar_json)
    publish.add_argument("--unit", required=True)
    publish.add_argument("--source", required=True)
    publish.add_argument("--bus", required=True)
    publish.add_argument("--quality", required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    client = TelemetryClient(args.socket, timeout=args.timeout)
    if args.command == "status":
        status, payload = client.request("GET", "/v1/status")
    elif args.command == "metrics":
        status, payload = client.request("GET", "/v1/metrics")
    elif args.command == "get":
        status, payload = client.request(
            "GET", f"/v1/metrics/{args.metric}"
        )
    elif args.command == "acquire":
        status, payload = client.request(
            "POST",
            f"/v1/acquisitions/{args.metric}",
            {"mode": args.mode},
        )
    else:
        status, payload = client.publish(
            args.metric,
            value=args.value,
            unit=args.unit,
            source=args.source,
            bus=args.bus,
            quality=args.quality,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
