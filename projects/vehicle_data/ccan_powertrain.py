"""Read evidence-qualified engine-health broadcasts from C-CAN.

This reader is receive-only.  It never configures the SocketCAN interface and
only accepts an already-UP, 500 kbit/s, listen-only, ERROR-ACTIVE C-CAN
interface.  Signal provenance lives in the PCM plots finding and the public
metric registry; this module only implements those fixed decodes.
"""

from __future__ import annotations

import errno
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable

from lib import canbus, diagnostic_safety


CHANNEL = "can0"
SFF_MASK = 0x7FF
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
FRAME_TYPE_FLAGS = CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_ERR_FLAG
FILTER_MASK = SFF_MASK | FRAME_TYPE_FLAGS
OIL_PRESSURE_ID = 0x41D
COOLANT_TEMPERATURE_ID = 0x2ED
IGNITION_ON_ID = 0x2EF
FILTER_IDS = (
    OIL_PRESSURE_ID,
    COOLANT_TEMPERATURE_ID,
    IGNITION_ON_ID,
)
KPA_TO_PSI = 0.14503773773020923


@dataclass(frozen=True)
class PassiveObservation:
    metric: str
    value: float | bool
    unit: str
    source: str
    quality: str
    detail: str


def decode_frame(can_id: int, data: bytes) -> PassiveObservation | None:
    """Decode one exact allowlisted C-CAN frame, or return ``None``."""
    if can_id == OIL_PRESSURE_ID and len(data) >= 3:
        native_kpa = float(data[2] * 4)
        return PassiveObservation(
            metric="engine.oil_pressure",
            value=native_kpa * KPA_TO_PSI,
            unit="psi",
            source="ccan.broadcast.0x41d",
            quality="observed_alfa_scale",
            detail=(
                "0x41D byte 2 x 4 kPa, converted to psi for telemetry"
            ),
        )
    if can_id == COOLANT_TEMPERATURE_ID and data:
        native_celsius = float(data[0] - 40)
        return PassiveObservation(
            metric="engine.coolant_temperature",
            value=native_celsius * 9.0 / 5.0 + 32.0,
            unit="°F",
            source="ccan.broadcast.0x2ed",
            quality="observed_alfa_scale",
            detail=(
                "0x2ED byte 0 - 40 °C, converted to °F for telemetry"
            ),
        )
    if can_id == IGNITION_ON_ID:
        return PassiveObservation(
            metric="vehicle.ignition_on",
            value=True,
            unit="boolean",
            source="ccan.broadcast.0x2ef",
            quality="verified",
            detail="0x2EF ignition-on presence gate observed",
        )
    return None


def _median_observation(
    samples: list[PassiveObservation],
) -> PassiveObservation:
    ordered = sorted(samples, key=lambda item: float(item.value))
    selected = ordered[len(ordered) // 2]
    return PassiveObservation(
        metric=selected.metric,
        value=selected.value,
        unit=selected.unit,
        source=selected.source,
        quality=selected.quality,
        detail=f"{selected.detail}; median of {len(samples)} frame(s)",
    )


def read_snapshot(
    channel: str = CHANNEL,
    *,
    timeout: float = 0.5,
    socket_factory: Callable[..., socket.socket] = socket.socket,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[PassiveObservation, ...]:
    """Read a short filtered snapshot without changing interface state."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    sock = socket_factory(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    samples: dict[str, list[PassiveObservation]] = {}
    try:
        filters = b"".join(
            struct.pack("=II", can_id, FILTER_MASK) for can_id in FILTER_IDS
        )
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, filters)
        sock.bind((channel,))
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            remaining = max(0.01, deadline - monotonic())
            sock.settimeout(remaining)
            try:
                frame = sock.recv(16)
            except socket.timeout:
                break
            except OSError as exc:
                if exc.errno == errno.ENETDOWN:
                    break
                raise
            can_id, dlc, raw_data = struct.unpack("=IB3x8s", frame)
            if can_id & FRAME_TYPE_FLAGS:
                continue
            observation = decode_frame(
                can_id & SFF_MASK, raw_data[: min(dlc, 8)]
            )
            if observation is None:
                continue
            samples.setdefault(observation.metric, []).append(observation)
            if all(metric in samples for metric in (
                "engine.oil_pressure",
                "engine.coolant_temperature",
                "vehicle.ignition_on",
            )):
                break
    finally:
        sock.close()
    return tuple(
        _median_observation(samples[metric])
        for metric in sorted(samples)
    )


class CcanPowertrainReader:
    """Safety wrapper for the fixed passive powertrain snapshot."""

    def __init__(
        self,
        *,
        channel: str = CHANNEL,
        probe_seconds: float = 0.25,
        read_timeout: float = 0.5,
    ):
        self.channel = channel
        self.probe_seconds = probe_seconds
        self.read_timeout = read_timeout

    def read(self) -> tuple[PassiveObservation, ...]:
        with diagnostic_safety.channel_observer_lock(self.channel):
            state = canbus.interface_state(self.channel)
            if not (
                state.present
                and state.up
                and state.bitrate == canbus.BITRATE_CCAN
                and state.listen_only
                and state.controller_state == "ERROR-ACTIVE"
            ):
                return ()
            if (
                canbus.identify_bus(
                    self.channel, probe=self.probe_seconds
                )
                != "c-can"
            ):
                return ()
            return read_snapshot(
                self.channel,
                timeout=self.read_timeout,
            )
