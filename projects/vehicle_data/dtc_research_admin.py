"""Owner CLI for the research rollback controls: ``pause``, ``resume``, ``drained`` and ``rollback-ready``.

Run as the advisor's user from the repo root:
``python3 -m projects.vehicle_data.dtc_research_admin pause|resume|drained|rollback-ready [--socket PATH]``.
It talks to the advisor's own Unix socket, on routes no web listener relays. ``drained`` exits 0
when everything in flight has settled and ``rollback-ready`` when, besides, credits are off and no
API-created session is left; both exit 1 otherwise (repeat until 0); the other verbs
exit 0 on success. The reply is printed as JSON. The procedure is docs/dtc-research.md
"Promotional API credits"; this module holds no policy.
"""

import argparse
import json
import sys

from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.api_credits_contract import ADMIN_ROUTES

DEFAULT_SOCKET = "/run/van-telemetry-advisor/api.sock"  # warning_chat.DEFAULT_SOCKET; not imported to keep the CLI light
VERBS = {"pause": ("POST", ADMIN_ROUTES["pause"]), "resume": ("POST", ADMIN_ROUTES["resume"]),
         "drained": ("GET", ADMIN_ROUTES["drained"]), "rollback-ready": ("GET", ADMIN_ROUTES["rollbackReady"])}
SUCCESS_FIELD = {"drained": "drained", "rollback-ready": "rollback_ready"}  # the reply field that must be true


def run(verb, socket_path, client_factory=TelemetryClient, out=sys.stdout):
    """The exit code for one verb; the advisor's reply goes to ``out``."""
    method, path = VERBS[verb]
    try:
        status, reply = client_factory(socket_path, timeout=30).request(method, path, {} if method == "POST" else None)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"advisor unreachable: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(reply, sort_keys=True), file=out)
    if status != 200:
        return 2
    return 0 if verb not in SUCCESS_FIELD or reply.get(SUCCESS_FIELD[verb]) is True else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verb", choices=sorted(VERBS))
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args(argv)
    return run(args.verb, args.socket)


if __name__ == "__main__":
    raise SystemExit(main())
