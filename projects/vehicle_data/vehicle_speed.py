"""Read the already-decoded vehicle-speed observation without any I/O.

The broadcast ID/scaling remain solely in lib.broadcast_signals; this module
selects a finite observation and applies the established service-display bound.
"""
from __future__ import annotations

from lib.timeutil import finite_number

MAX_SPEED_MPH = 200.0


def speed_mph(snapshot: object) -> float | None:
    """Read one finite, physically bounded vehicle-speed observation."""
    for observation in getattr(snapshot, "observations", ()):
        if getattr(observation, "metric", None) == "vehicle.speed":
            value = getattr(observation, "value", None)
            if finite_number(value) and 0 <= value <= MAX_SPEED_MPH:
                return float(value)
    return None
