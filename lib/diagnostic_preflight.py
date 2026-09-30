"""Existing diagnostic CLI host and post-arm interface checks."""

import subprocess

from lib import canbus


def prearm_conflict_errors():
    """Return host capabilities required before scoped link mutation."""

    errors = []
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        errors.append(
            "noninteractive sudo is unavailable; arm/restoration cannot be guaranteed"
        )
    return errors


def active_interface_errors(channel, bitrate):
    """Require the exact post-arm classical-CAN state."""

    errors = []
    interface = canbus.interface_state(channel)
    if (
        not isinstance(interface, canbus.InterfaceState)
        or interface.channel != channel
        or not interface.present
        or not interface.up
    ):
        errors.append(f"{channel} is missing or down; explicitly arm the intended bus first")
    elif interface.bitrate != bitrate:
        errors.append(f"{channel} bitrate is {interface.bitrate}, expected {bitrate}")
    if interface.present and interface.up and interface.fd_enabled is not False:
        errors.append(
            f"{channel} must prove classical CAN with FD off before active discovery"
        )
    if interface.present and interface.up and interface.listen_only:
        errors.append(
            f"{channel} is listen-only; discovery is active diagnostic traffic, so arm it explicitly"
        )
    if interface.controller_state != "ERROR-ACTIVE":
        errors.append(
            f"{channel} controller state is {interface.controller_state or 'unknown'}, "
            "expected ERROR-ACTIVE"
        )
    if interface.present and interface.up and interface.restart_ms != 0:
        errors.append(
            f"{channel} restart-ms is {interface.restart_ms}; active discovery requires 0"
        )
    return errors


def preflight(channel, bitrate):
    """Compatibility aggregate for an already-armed active interface."""

    return prearm_conflict_errors() + active_interface_errors(channel, bitrate)


# Preserve the historical callback identity recorded in route-call diagnostics.
# ecu_discover re-exports these names, so existing pickle references resolve too.
prearm_conflict_errors.__module__ = "tools.ecu_discover"
active_interface_errors.__module__ = "tools.ecu_discover"
preflight.__module__ = "tools.ecu_discover"
