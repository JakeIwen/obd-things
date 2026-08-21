"""Opaque one-use authorization for coordinated engine-running CAN reads.

This is deliberately not a generic transmission permission.  A permit can be
issued only for one of the two fixed active-drive transports, while a live
exclusive resolved C-CAN diagnostic lock is held and the latest qualified C-CAN
snapshot contains three running-RPM samples.  It expires quickly and is
consumed by the first attempted transport use, successful or otherwise.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Callable

from lib import diagnostic_safety
from lib.modules import MODULES
from projects.vehicle_data import ccan_powertrain


PCM_GENERATOR_DUTY = "pcm.generator_field_duty"
PCM_CRANKSHAFT_TORQUE = "pcm.crankshaft_torque"
RF_HUB_PRESSURE = "rf_hub.pressure"
ALLOWED_PURPOSES = frozenset(
    (PCM_GENERATOR_DUTY, PCM_CRANKSHAFT_TORQUE, RF_HUB_PRESSURE)
)
RUNNING_RPM = 400.0
REQUIRED_RPM_SAMPLES = 3
PERMIT_TTL_SECONDS = 0.25

_PCM = MODULES["pcm"]
_RF_HUB = MODULES["rf_hub"]
if _PCM.bus != "c-can" or _RF_HUB.bus != "c-can":
    raise RuntimeError("active-drive transmit permits require C-CAN modules")
_CONSTRUCTION_TOKEN = object()


class TransmitPermitError(RuntimeError):
    """A fixed active-drive transmission was not safely authorized."""


class _TransmitPermit:
    """Opaque mutable capability; construct through :func:`issue` only."""

    __slots__ = (
        "_channel",
        "_purpose",
        "_lock_handle",
        "_issued_at",
        "_expires_at",
        "_monotonic",
        "_pid",
        "_used",
        "_state_lock",
    )

    def __init__(
        self,
        construction_token: object,
        *,
        purpose: str,
        channel: str,
        lock_handle: object,
        issued_at: float,
        expires_at: float,
        monotonic: Callable[[], float],
    ) -> None:
        if construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("transmit permits cannot be constructed directly")
        self._channel = channel
        self._purpose = purpose
        self._lock_handle = lock_handle
        self._issued_at = issued_at
        self._expires_at = expires_at
        self._monotonic = monotonic
        self._pid = os.getpid()
        self._used = False
        self._state_lock = threading.Lock()


def _monotonic_value(monotonic: Callable[[], float]) -> float:
    try:
        value = monotonic()
    except Exception as exc:
        raise TransmitPermitError(
            f"transmit-permit monotonic clock failed: {type(exc).__name__}: {exc}"
        ) from exc
    if isinstance(value, bool):
        raise TransmitPermitError("transmit-permit monotonic clock was not finite")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        raise TransmitPermitError(
            "transmit-permit monotonic clock was not finite"
        ) from None
    if not math.isfinite(normalized):
        raise TransmitPermitError(
            "transmit-permit monotonic clock was not finite"
        )
    return normalized


def _validate_running_snapshot(snapshot: object, *, now: float) -> float:
    if not isinstance(snapshot, ccan_powertrain.BroadcastSnapshot):
        raise TransmitPermitError(
            "transmit permit requires a qualified C-CAN broadcast snapshot"
        )
    if (
        isinstance(snapshot.frame_count, bool)
        or not isinstance(snapshot.frame_count, int)
        or snapshot.frame_count <= 0
    ):
        raise TransmitPermitError(
            "transmit permit requires observed C-CAN traffic"
        )
    samples = snapshot.rpm_samples
    if not isinstance(samples, tuple) or len(samples) < REQUIRED_RPM_SAMPLES:
        raise TransmitPermitError(
            "transmit permit requires three fresh engine-speed samples"
        )
    latest = samples[-REQUIRED_RPM_SAMPLES:]
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= RUNNING_RPM
        for value in latest
    ):
        raise TransmitPermitError(
            "transmit permit requires three latest RPM samples at or above 400"
        )
    completed = snapshot.completed_monotonic
    if isinstance(completed, bool):
        raise TransmitPermitError(
            "transmit permit requires timestamped fresh RPM evidence"
        )
    try:
        completed_at = float(completed)
    except (TypeError, ValueError, OverflowError):
        raise TransmitPermitError(
            "transmit permit requires timestamped fresh RPM evidence"
        ) from None
    evidence_age = now - completed_at
    if (
        not math.isfinite(completed_at)
        or evidence_age < 0
        or evidence_age >= PERMIT_TTL_SECONDS
    ):
        raise TransmitPermitError(
            "transmit permit RPM evidence was stale or from another clock"
        )
    return completed_at


def issue(
    lock_handle: object,
    snapshot: ccan_powertrain.BroadcastSnapshot,
    *,
    purpose: str,
    channel: str,
    monotonic: Callable[[], float] = time.monotonic,
) -> object:
    """Issue one short-lived capability for one fixed transport purpose."""
    if not isinstance(purpose, str) or purpose not in ALLOWED_PURPOSES:
        raise TransmitPermitError("transmit-permit purpose is not allowlisted")
    try:
        diagnostic_safety.validate_channel_lock(
            lock_handle,
            channel,
        )
    except (diagnostic_safety.ChannelLockError, OSError, ValueError) as exc:
        raise TransmitPermitError(
            f"a live exclusive {channel} diagnostic lock is required"
        ) from exc
    issued_at = _monotonic_value(monotonic)
    evidence_at = _validate_running_snapshot(snapshot, now=issued_at)
    return _TransmitPermit(
        _CONSTRUCTION_TOKEN,
        purpose=purpose,
        channel=channel,
        lock_handle=lock_handle,
        issued_at=issued_at,
        expires_at=evidence_at + PERMIT_TTL_SECONDS,
        monotonic=monotonic,
    )


def consume(permit: object, *, purpose: str, channel: str) -> None:
    """Consume and validate one permit immediately before a fixed raw send."""
    if type(permit) is not _TransmitPermit:
        raise TransmitPermitError("missing or invalid active-drive transmit permit")
    with permit._state_lock:
        if permit._used:
            raise TransmitPermitError(
                "active-drive transmit permit was already consumed"
            )
        # Every attempted use spends the capability, including a wrong-purpose,
        # expired, or released-lock attempt.
        permit._used = True
        if (
            not isinstance(purpose, str)
            or purpose not in ALLOWED_PURPOSES
            or purpose != permit._purpose
        ):
            raise TransmitPermitError(
                "active-drive transmit permit purpose did not match the fixed request"
            )
        if channel != permit._channel:
            raise TransmitPermitError(
                "active-drive transmit permit channel did not match its resolved C-CAN channel"
            )
        if os.getpid() != permit._pid:
            raise TransmitPermitError(
                "active-drive transmit permit cannot cross a process boundary"
            )
        try:
            diagnostic_safety.validate_channel_lock(
                permit._lock_handle,
                permit._channel,
            )
        except (diagnostic_safety.ChannelLockError, OSError, ValueError) as exc:
            raise TransmitPermitError(
                "active-drive transmit permit lost its exclusive C-CAN lock"
            ) from exc
        now = _monotonic_value(permit._monotonic)
        if now < permit._issued_at or now >= permit._expires_at:
            raise TransmitPermitError(
                "active-drive transmit permit expired before the fixed request"
            )
