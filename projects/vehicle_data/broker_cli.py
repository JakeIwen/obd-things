"""Command-line contract and defaults for the telemetry broker."""

from __future__ import annotations

import argparse
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_SOCKET = "/run/van-telemetry/api.sock"
DEFAULT_HISTORY_DB = REPO / "tmp" / "vehicle_data" / "history.sqlite3"
DEFAULT_DTC_CACHE = REPO / "tmp" / "vehicle_data" / "dtc-cache.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve allowlisted cached vehicle telemetry over a Unix socket."
    )
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--socket-mode", default="0660")
    parser.add_argument(
        "--can-interface-mode",
        choices=("dual-usbcanfd",),
        required=True,
        help=(
            "required safety gate: select independent serial/dev_id role "
            "routing for the two installed dual-channel adapters; each healthy "
            "bus remains usable when another role is unavailable, and "
            "fixed-channel mode is retired"
        ),
    )
    parser.add_argument("--collector-interval", type=float, default=1.0)
    parser.add_argument(
        "--history-db",
        default=str(DEFAULT_HISTORY_DB),
        help="SQLite telemetry history (offline/cache-only)",
    )
    parser.add_argument(
        "--history-interval",
        type=float,
        default=5.0,
        help="seconds between cache snapshots stored in SQLite (default: 5)",
    )
    parser.add_argument(
        "--engine-off-voltage-state",
        help=(
            "atomic engine-off passive-voltage sample; defaults beside the "
            "history database"
        ),
    )
    parser.add_argument(
        "--dtc-cache",
        default=str(DEFAULT_DTC_CACHE),
        help="atomic saved-DTC JSON cache exposed read-only to the dashboard",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="disable SQLite history and early-warning evaluation",
    )
    parser.add_argument(
        "--enable-advisory-notifications",
        action="store_true",
        help=(
            "deliver persisted warning episodes through the queue-aware "
            "host ntfy helper (requires --advisory-ntfy-topic)"
        ),
    )
    parser.add_argument(
        "--advisory-ntfy-topic",
        help="fixed ntfy topic used only when advisory delivery is enabled",
    )
    parser.add_argument("--probe-seconds", type=float, default=0.75)
    parser.add_argument("--read-timeout", type=float, default=2.0)
    parser.add_argument("--no-collector", action="store_true")
    parser.add_argument(
        "--enable-drive-dtc-reads", action="store_true",
        help="opt in to bounded hourly radar/BCM DTC reads during qualified drives",
    )
    parser.add_argument(
        "--no-active-drive",
        action="store_true",
        help="disable the coordinated engine-running diagnostic collector",
    )
    parser.add_argument(
        "--no-auxiliary-drive",
        action="store_true",
        help="disable the fixed engine-running B-CAN ICS odometer candidate",
    )
    return parser


