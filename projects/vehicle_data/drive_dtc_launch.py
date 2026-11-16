"""The active-drive helper's entry to the optional drive DTC step, without its contract.

Every C-CAN helper imports this before it can emit ``ready``. It loads the DTC
contract (``drive_dtc.json``) only when the broker passed DTC arguments, so a
damaged vocabulary file can never stop a flag-off helper from starting; with
DTC arguments it fails the capability closed: the helper starts without it, its
``ready`` carries no capability, and the broker cancels before the grant.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sys


def add_arguments(parser):
    parser.add_argument("--enable-drive-dtc", action="store_true",
                        help="internal: broker reserved the optional drive DTC capability")
    parser.add_argument("--drive-dtc-reservation")
    parser.add_argument("--drive-dtc-origin", type=float)
    parser.add_argument("--drive-dtc-first-due", type=float)


def _steps_module():
    # Imported here, not at module level: see the module docstring.
    from projects.vehicle_data import drive_dtc_steps
    return drive_dtc_steps


def reservation_from_arguments(args):
    """The reservation, or None; refuses an enabled launch without the startup grant.

    The capability is confirmed only through the grant pipe's ready event, so a
    launch with DTC arguments but no grant descriptor (not the broker's) would
    send unconfirmed. The legacy ungated start stays valid only with DTC off.
    """
    supplied = (args.drive_dtc_reservation, args.drive_dtc_origin, args.drive_dtc_first_due)
    if not args.enable_drive_dtc:
        if any(value is not None for value in supplied):
            raise ValueError("drive-DTC schedule requires --enable-drive-dtc")
        return None
    if getattr(args, "startup_grant_fd", None) is None:
        raise ValueError("--enable-drive-dtc requires the supervisor's --startup-grant-fd")
    try:
        steps = _steps_module()
    except Exception as exc:
        try:
            print(f"{datetime.now(timezone.utc).isoformat()} drive DTC contract unavailable "
                  f"({type(exc).__name__}: {exc}); starting without the capability",
                  file=sys.stderr, flush=True)
        except Exception:
            pass
        return None
    return steps.Reservation(*supplied)


def capabilities(reservation):
    return () if reservation is None else (_steps_module().contract.CAPABILITY,)


class _NoDtcSteps:
    """The flag-off step: no watch, no socket, no events, no contract."""

    def begin_cycle(self):
        pass

    def run(self, snapshot):
        return None

    def close(self):
        pass


def steps(backend, sink, radar_steps, *, reservation, rf_hub_profiles=()):
    if reservation is None:
        return _NoDtcSteps()
    return _steps_module().DriveDtcSteps(
        backend, sink, radar_steps, reservation=reservation, rf_hub_profiles=rf_hub_profiles)
