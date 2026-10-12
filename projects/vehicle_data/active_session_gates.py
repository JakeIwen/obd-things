"""Gates of the C-CAN active-drive session, and the outcome type both drive helpers return.

``active_drive.run_active_session`` and the radar steps it calls
(radar_drive_steps.py) repeat these checks before every transmission. They
read state through the helper's backend and never change it.

The B-CAN auxiliary helper shares only :class:`SessionOutcome`. Its gates look
similar but are not the same checks: it re-identifies the bus every cycle,
requires restart timing of exactly zero, and reports different text, so they
stay in bcan_auxiliary.py.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from lib import canbus
from lib.can_runtime_route import LinkExpectation


BITRATE = 500000
PAIR = "6/14"
RUNNING_RPM = 400.0
REQUIRED_RPM_SAMPLES = 3


@dataclass
class SessionOutcome:
    reason: str
    detail: str
    restored: bool | None = None


def safe_passive_state(state: object) -> bool:
    return LinkExpectation(BITRATE, listen_only=True).matches(state)


def safe_active_state(state: object, initial: canbus.InterfaceState) -> bool:
    return bool(
        isinstance(state, canbus.InterfaceState)
        and state.present
        and state.up
        and state.bitrate == BITRATE
        and state.fd_enabled is False
        and state.one_shot is False
        and not state.listen_only
        and state.controller_state == "ERROR-ACTIVE"
        and state.restart_ms
        == (initial.restart_ms if initial.restart_ms is not None else 0)
    )


def backend_identity_matches(backend: object) -> bool:
    """Keep legacy injected test backends compatible; live backend always checks."""
    checker = getattr(backend, "identity_matches", None)
    return bool(checker()) if callable(checker) else True


def running_snapshot(snapshot: object) -> bool:
    samples = getattr(snapshot, "rpm_samples", ())
    return bool(
        getattr(snapshot, "frame_count", 0) > 0
        and len(samples) >= REQUIRED_RPM_SAMPLES
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= RUNNING_RPM
            for value in samples[-REQUIRED_RPM_SAMPLES:]
        )
    )


def topology_failure(topology: object) -> SessionOutcome | None:
    if not getattr(topology, "usable", False):
        return SessionOutcome(
            "wrong_bus",
            f"same-boot topology is unusable: {getattr(topology, 'reason', 'unknown')}",
        )
    if getattr(topology, "bus", None) != "c-can":
        return SessionOutcome(
            "wrong_bus",
            f"same-boot topology reports {getattr(topology, 'bus', 'unknown')}",
        )
    if getattr(topology, "pair", None) != PAIR:
        return SessionOutcome(
            "wrong_bus",
            f"C-CAN topology must explicitly report DLC pins {PAIR}",
        )
    return None


def active_gate(
    backend: object,
    initial: canbus.InterfaceState,
) -> SessionOutcome | None:
    if not backend_identity_matches(backend):
        return SessionOutcome(
            "adapter_unhealthy",
            "resolved C-CAN USB identity changed or disappeared",
        )
    if not safe_active_state(backend.interface_state(), initial):
        return SessionOutcome(
            "adapter_unhealthy",
            "active SocketCAN state changed or left ERROR-ACTIVE",
        )
    failure = topology_failure(backend.topology())
    if failure is not None:
        return failure
    inhibits = backend.inhibits()
    if inhibits:
        names = ",".join(
            str(item.get("name", "invalid")) for item in inhibits
        )
        return SessionOutcome(
            "inhibited",
            f"external-operation inhibit appeared: {names}",
        )
    return None
