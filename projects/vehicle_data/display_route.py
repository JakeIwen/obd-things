"""Passive lease admission and route validation shared by the receive-only display feeds.

The C-CAN display receiver and the B-CAN companion receiver both hold a shared
(`LOCK_SH`) role and channel lease behind the cooperative passive turn, and
re-prove the adapter identity and interface state about once a second. Only that
common part lives here; each receiver keeps its own armed-owner checks.
"""

from contextlib import contextmanager

from lib import can_handoff, can_operation_state


@contextmanager
def passive_display_route(manager, role):
    """Hold the shared passive turn and leases for ``role``; yield (channel, serial, dev_id)."""
    with can_handoff.passive_turn(role):
        with manager.observe(role) as lease:
            yield lease.channel, lease.usb_serial, lease.dev_id


def check_display_route(manager, role, label, bitrate, route, *, listen_only):
    """Raise RuntimeError unless ``route`` is still the healthy ``role`` adapter.

    Identity must match the resolved topology; the interface must be up at the
    fixed classical bitrate (FD, one-shot and auto-restart off), ERROR-ACTIVE,
    in the expected listen-only state, with no operation inhibit.
    """
    channel, serial, dev_id = route
    resolution = manager.topology().resolution(role)
    if (resolution.require_channel(), resolution.spec.usb_serial,
            resolution.spec.dev_id) != (channel, serial, dev_id):
        raise RuntimeError(f"{label} USB role changed")
    state = manager.interface_state_reader(channel)
    if not (state.channel == channel and state.present and state.up
            and state.bitrate == bitrate and state.fd_enabled is False
            and state.one_shot is False and state.restart_ms == 0
            and state.controller_state == "ERROR-ACTIVE"
            and state.listen_only is listen_only):
        raise RuntimeError(f"{label} display route is no longer healthy")
    if can_operation_state.active_inhibits(channel):
        raise RuntimeError(f"{label} operation inhibit is active")
