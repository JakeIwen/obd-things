"""Inert stand-in for a drive helper, started as ``python -m tests.helper_startup_stub``.

Speaks the real startup-grant protocol through ``ChildStartupGate`` and touches
nothing else: no CAN, no lock, no socket.  ``HELPER_STUB_MODE=hold`` never
announces itself and blocks, like a helper that has not reached its barrier yet;
the default mode finishes after the grant, echoing one stdin line when the radar
flag is present, so a test can see the grant descriptor and stdin coexist.
"""

import argparse
import json
import os
import sys

from projects.vehicle_data import helper_startup_grant


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel")
    parser.add_argument("--expected-usb-serial")
    parser.add_argument("--expected-dev-id")
    parser.add_argument("--expected-parent-pid", type=int)
    parser.add_argument("--enable-oil-life", action="store_true")
    parser.add_argument("--enable-radar-service-alignment", action="store_true")
    parser.add_argument(helper_startup_grant.GRANT_FD_FLAG, type=int, default=None)
    args = parser.parse_args(argv)
    if os.environ.get("HELPER_STUB_MODE") == "hold":
        sys.stdin.read()
        return 0
    gate = helper_startup_grant.child_gate_from_arguments(
        args.startup_grant_fd, args.expected_parent_pid
    )
    refusal = gate(_Sink()) if gate is not None else None
    if refusal is not None:
        emit({"type": "final", "state": "idle", "reason": "helper_failed",
              "detail": refusal, "interface_mode": "unknown", "restored": None})
        return 0
    detail = "granted"
    if args.enable_radar_service_alignment:
        detail = "stdin=" + sys.stdin.readline().strip()
    emit({"type": "final", "state": "idle", "reason": "engine_not_running",
          "detail": detail, "interface_mode": "listen_only", "restored": True})
    return 0


class _Sink:
    @staticmethod
    def emit(event_type, **payload):
        emit({"type": event_type, **payload})


if __name__ == "__main__":
    raise SystemExit(main())
