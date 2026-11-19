"""Owner CLI for the below-empty fuel gauge's floor: ``list``, ``set <points>``, ``restore <n>``.

Run from the repo root: ``python3 -m projects.vehicle_data.fuel_floor list [--json] [--socket PATH]``.
It talks to the broker's local socket (a route no web listener relays), so the broker stays the
only writer of ``fuel-floor.json``. ``set`` and ``restore`` keep the value they replace in the
earlier-floors list; ``restore`` takes the number ``list`` prints beside an earlier floor. The
contract and its limits are the vehicle-data README section "Fuel gauge below empty".
Exit codes: 0 done, 1 the broker refused, 2 unreachable or bad arguments.
"""

import argparse
import json
import sys

from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.broker_cli import DEFAULT_SOCKET
from projects.vehicle_data.fuel_floor_contract import API_PATH, RESTORE_ACTION, SET_ACTION


def _floor_line(row):
    when = row["committed_at"] or "never"
    return f"{row['value']:g} points  ({row['source']}, {when})"


def format_listing(reply):
    """The reply as lines for a person."""
    lines = [f"floor in force: {_floor_line(reply['floor_record'])}",
             f"gallons per point: {reply['gal_per_point']:g}"]
    episode = reply["episode"]
    if episode is not None:
        lines.append(f"open episode: {episode['gallons']:g} gal past the van's empty, "
                     f"raw {episode['raw']:g}, shown {episode['adjusted']:g} %"
                     + (" (implausible, held at the bound)" if episode["implausible"] else ""))
    lines.append("earlier floors (newest first; restore <n>):")
    lines += [f"  {number}. {_floor_line(row)}" for number, row in enumerate(reply["earlier_floors"], 1)] or ["  none"]
    lines.append("episodes (newest first):")
    lines += [f"  {row['started_at']} .. {row['ended_at']}: deepest {row['deepest_raw']:g}, "
              f"{row['gallons']:g} gal, ended by {row['end_reason']} at van {row['van_reading_at_end']} %"
              + (", implausible" if row["implausible"] else "")
              + (", set the floor" if row["floor_committed"] else "")
              for row in reply["episodes"]] or ["  none"]
    if reply.get("storage_error"):
        lines.append(f"storage problem: {reply['storage_error']}")
    return lines


def run(args, client_factory=TelemetryClient, out=sys.stdout):
    """The exit code for one parsed command; the reply goes to ``out``."""
    if args.verb == "list":
        method, body = "GET", None
    elif args.verb == "set":
        method, body = "POST", {"action": SET_ACTION, "points": args.value}
    else:
        method, body = "POST", {"action": RESTORE_ACTION, "index": int(args.value)}
    try:
        status, reply = client_factory(args.socket, timeout=10).request(method, API_PATH, body)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"broker unreachable: {exc}", file=sys.stderr)
        return 2
    if status != 200:
        print(f"refused ({status}): {reply.get('detail') or reply.get('reason')}", file=sys.stderr)
        return 1
    print(json.dumps(reply, sort_keys=True) if args.json else "\n".join(format_listing(reply)), file=out)
    return 0


def parse(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--socket", default=DEFAULT_SOCKET)
    common.add_argument("--json", action="store_true", help="print the broker's reply as JSON")
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("list", parents=[common])
    verbs.add_parser("set", parents=[common]).add_argument(
        "value", type=float, help="points on the van's gauge, e.g. -4.5")
    verbs.add_parser("restore", parents=[common]).add_argument(
        "value", type=int, help="earlier floor number from list")
    return parser.parse_args(argv)


def main(argv=None):
    return run(parse(argv))


if __name__ == "__main__":
    raise SystemExit(main())
