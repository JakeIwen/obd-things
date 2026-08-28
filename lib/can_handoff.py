"""Cooperative per-role fairness ahead of the authoritative CAN locks.

This lock does not identify a bus, configure an interface, or authorize CAN
I/O.  It only establishes scheduling order:

* broker receive-side work holds a shared ``passive_turn``;
* a reviewed wake holds an exclusive ``active_turn`` before taking the real
  logical-role and resolved-channel locks.

The required ordering is therefore handoff -> logical role -> resolved
channel.  Once an active turn is reserved, new cooperating broker observers
yield while an observer already in flight finishes normally.  Non-cooperating
tools remain safely excluded by the existing authoritative role/channel locks.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from lib import diagnostic_safety
from lib.vehicle_can_roles import CAN_BUS_ROLES, normalize_can_role


ACTIVE_WAIT_SECONDS = 1.25
ACTIVE_RETRY_SECONDS = 0.025


def lock_name(bus: str) -> str:
    role = normalize_can_role(bus)
    if role not in CAN_BUS_ROLES:
        raise ValueError(f"{bus!r} is not a connected vehicle bus")
    return f"can-handoff-{role}"


def gate_lock_name(bus: str) -> str:
    """Return the writer-preference gate ahead of one role's turn lock."""

    role = normalize_can_role(bus)
    if role not in CAN_BUS_ROLES:
        raise ValueError(f"{bus!r} is not a connected vehicle bus")
    return f"can-handoff-gate-{role}"


@contextmanager
def passive_turn(bus: str):
    """Hold one shared scheduling turn; confers no CAN capability."""

    # Readers pass through the gate only while acquiring their shared turn.
    # An active waiter holds the gate exclusively, so no later reader can
    # overtake it while the already-running readers drain.
    with diagnostic_safety.channel_observer_lock(gate_lock_name(bus)):
        handle = diagnostic_safety.acquire_channel_observer_lock(lock_name(bus))
    try:
        yield handle
    finally:
        diagnostic_safety.release_channel_lock(handle)


@contextmanager
def active_turn(
    bus: str,
    *,
    wait_seconds: float = ACTIVE_WAIT_SECONDS,
    retry_seconds: float = ACTIVE_RETRY_SECONDS,
):
    """Reserve the next cooperating active turn without bypassing CAN locks.

    The wait is deliberately bounded.  The exclusive gate prevents new
    cooperating readers from entering while an existing reader finishes; a
    reader that does not drain within the bound still fails the wake closed.
    """

    if wait_seconds < 0:
        raise ValueError("wait_seconds must be non-negative")
    if retry_seconds <= 0:
        raise ValueError("retry_seconds must be positive")

    deadline = time.monotonic() + wait_seconds

    def acquire_before_deadline(name: str, timeout_detail: str):
        while True:
            try:
                return diagnostic_safety.acquire_channel_lock(name)
            except diagnostic_safety.ChannelLockError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise diagnostic_safety.ChannelLockError(
                        timeout_detail
                    ) from None
                time.sleep(min(retry_seconds, remaining))

    role = normalize_can_role(bus)
    gate = acquire_before_deadline(
        gate_lock_name(role),
        f"the {role} active admission gate remained busy until the bounded "
        "handoff deadline",
    )
    try:
        handle = acquire_before_deadline(
            lock_name(role),
            f"an in-flight {role} passive observer did not drain before the "
            "bounded active handoff deadline",
        )
        try:
            yield handle
        finally:
            diagnostic_safety.release_channel_lock(handle)
    finally:
        diagnostic_safety.release_channel_lock(gate)


__all__ = (
    "ACTIVE_RETRY_SECONDS",
    "ACTIVE_WAIT_SECONDS",
    "active_turn",
    "gate_lock_name",
    "lock_name",
    "passive_turn",
)
