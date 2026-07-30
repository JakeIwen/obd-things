"""Read evidence-qualified engine-health broadcasts from C-CAN.

This module is receive-only and never configures or transmits through
SocketCAN. :class:`CcanPowertrainReader` requires the normal already-UP,
500-kbit/s, listen-only, ERROR-ACTIVE C-CAN state and an observer lock. The
coordinated active-drive owner may instead use the lower-level bounded snapshot
primitive while it already holds the exclusive lock and honestly reports the
interface as armed. Signal provenance lives in the PCM plots finding and the
public metric registry; this module only implements those fixed decodes.
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
# Linux SocketCAN numeric constants keep fake-socket offline tests portable to
# Python builds that do not expose AF_CAN/CAN_RAW (for example macOS workers).
# Real CAN access still fails normally on a host without SocketCAN.
AF_CAN = getattr(socket, "AF_CAN", 29)
CAN_RAW = getattr(socket, "CAN_RAW", 1)
SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
CAN_RAW_FILTER = getattr(socket, "CAN_RAW_FILTER", 1)
SFF_MASK = 0x7FF
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
FRAME_TYPE_FLAGS = CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_ERR_FLAG
# CAN_ERR_FLAG has special receive-filter semantics in Linux SocketCAN.  Adding
# it to a normal data-frame filter mask prevents ordinary frames from matching.
# EFF/RTR still constrain the kernel filter, and decode_frame's caller rejects
# every frame carrying any FRAME_TYPE_FLAGS before decoding.
FILTER_MASK = SFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG
OIL_PRESSURE_ID = 0x41D
COOLANT_TEMPERATURE_ID = 0x2ED
ENGINE_SPEED_ID = 0x0FC
TARGET_CRANK_TORQUE_ID = 0x100
VEHICLE_SPEED_ID = 0x101
TRANSMISSION_SHAFT_SPEED_ID = 0x1F7
IGNITION_ON_ID = 0x2EF
SYSTEM_VOLTAGE_ID = 0x41A
FILTER_IDS = (
    OIL_PRESSURE_ID,
    COOLANT_TEMPERATURE_ID,
    ENGINE_SPEED_ID,
    TARGET_CRANK_TORQUE_ID,
    VEHICLE_SPEED_ID,
    TRANSMISSION_SHAFT_SPEED_ID,
    IGNITION_ON_ID,
)
ACTIVE_FILTER_IDS = FILTER_IDS + (SYSTEM_VOLTAGE_ID,)
KPA_TO_PSI = 0.14503773773020923
KMH_TO_MPH = 0.621371192237334
NM_TO_LB_FT = 0.7375621492772656


@dataclass(frozen=True)
class PassiveObservation:
    metric: str
    value: float | bool
    unit: str
    source: str
    quality: str
    detail: str


@dataclass(frozen=True)
class BroadcastSnapshot:
    """One bounded raw-broadcast sample collected without changing CAN state."""

    observations: tuple[PassiveObservation, ...]
    rpm_samples: tuple[float, ...]
    frame_count: int
    completed_monotonic: float | None = None


def decode_frame_observations(
    can_id: int, data: bytes
) -> tuple[PassiveObservation, ...]:
    """Decode every allowlisted observation in one C-CAN frame."""
    if can_id == SYSTEM_VOLTAGE_ID and data:
        return (
            PassiveObservation(
                metric="battery.voltage",
                value=4.0 + float(data[0]) * 0.05,
                unit="V",
                source="ccan.broadcast.0x41a",
                quality="verified",
                detail="0x41A byte 0 x 0.05 V + 4.0 V",
            ),
        )
    if can_id == OIL_PRESSURE_ID and len(data) >= 3:
        native_kpa = float(data[2] * 4)
        return (
            PassiveObservation(
                metric="engine.oil_pressure",
                value=native_kpa * KPA_TO_PSI,
                unit="psi",
                source="ccan.broadcast.0x41d",
                quality="observed_alfa_scale",
                detail=(
                    "0x41D byte 2 x 4 kPa, converted to psi for telemetry"
                ),
            ),
        )
    if can_id == COOLANT_TEMPERATURE_ID and data:
        native_celsius = float(data[0] - 40)
        return (
            PassiveObservation(
                metric="engine.coolant_temperature",
                value=native_celsius * 9.0 / 5.0 + 32.0,
                unit="°F",
                source="ccan.broadcast.0x2ed",
                quality="observed_alfa_scale",
                detail=(
                    "0x2ED byte 0 - 40 °C, converted to °F for telemetry"
                ),
            ),
        )
    if can_id == ENGINE_SPEED_ID and len(data) >= 2:
        native_rpm = float(
            (int.from_bytes(data[:2], "big") & 0xFFFC) / 4.0
        )
        return (
            PassiveObservation(
                metric="engine.rpm",
                value=native_rpm,
                unit="rpm",
                source="ccan.broadcast.0x0fc",
                quality="observed_alfa_scale",
                detail=(
                    "0x0FC bytes 0-1 big-endian, low 2 bits masked, / 4 rpm"
                ),
            ),
        )
    if can_id == TARGET_CRANK_TORQUE_ID and len(data) >= 5:
        native_nm = float((int.from_bytes(data[3:5], "big") >> 5) - 500)
        return (
            PassiveObservation(
                metric="engine.target_crankshaft_torque",
                value=native_nm * NM_TO_LB_FT,
                unit="lb-ft",
                source="ccan.broadcast.0x100",
                quality="observed_alfa_scale",
                detail=(
                    "0x100 bytes 3-4 big-endian >> 5, then -500 Nm; "
                    "TCM target, not measured output; converted to lb-ft"
                ),
            ),
        )
    if can_id == VEHICLE_SPEED_ID and len(data) >= 3:
        native_kmh = float(
            (
                ((data[0] & 0x01) << 11)
                | (data[1] << 3)
                | (data[2] >> 5)
            )
            / 16.0
        )
        return (
            PassiveObservation(
                metric="vehicle.speed",
                value=native_kmh * KMH_TO_MPH,
                unit="mph",
                source="ccan.broadcast.0x101",
                quality="observed_alfa_scale",
                detail=(
                    "0x101 packed 12-bit speed / 16 km/h, converted to mph"
                ),
            ),
        )
    if can_id == TRANSMISSION_SHAFT_SPEED_ID and len(data) >= 6:
        output_raw = (
            ((data[0] & 0x01) << 16)
            | int.from_bytes(data[1:3], "big")
        )
        output_rpm = float(output_raw / 32.0)
        oil_raw = int.from_bytes(data[3:4], "big", signed=True)
        oil_celsius = float(oil_raw * 0.375 + 57.0)
        turbine_rpm = float(int.from_bytes(data[4:6], "big") / 2.0)
        return (
            PassiveObservation(
                metric="transmission.output_speed",
                value=output_rpm,
                unit="rpm",
                source="ccan.broadcast.0x1f7",
                quality="observed_alfa_scale",
                detail=(
                    "0x1F7 packed 17-bit output speed "
                    "(byte0 bit0, then bytes 1-2) / 32 rpm"
                ),
            ),
            PassiveObservation(
                metric="transmission.oil_temperature",
                value=oil_celsius * 9.0 / 5.0 + 32.0,
                unit="°F",
                source="ccan.broadcast.0x1f7",
                quality="observed_alfa_scale",
                detail=(
                    "0x1F7 byte 3 signed x 0.375 + 57 °C, converted to °F "
                    "for telemetry"
                ),
            ),
            PassiveObservation(
                metric="transmission.turbine_speed",
                value=turbine_rpm,
                unit="rpm",
                source="ccan.broadcast.0x1f7",
                quality="observed_alfa_scale",
                detail="0x1F7 bytes 4-5 big-endian / 2 rpm",
            ),
        )
    if can_id == IGNITION_ON_ID:
        return (
            PassiveObservation(
                metric="vehicle.ignition_on",
                value=True,
                unit="boolean",
                source="ccan.broadcast.0x2ef",
                quality="verified",
                detail="0x2EF ignition-on presence gate observed",
            ),
        )
    return ()


def decode_frame(can_id: int, data: bytes) -> PassiveObservation | None:
    """Decode the first allowlisted observation, retained for callers/tests."""
    observations = decode_frame_observations(can_id, data)
    return observations[0] if observations else None


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


def read_broadcast_snapshot(
    channel: str = CHANNEL,
    *,
    timeout: float = 0.5,
    include_battery: bool = False,
    required_rpm_samples: int = 1,
    socket_factory: Callable[..., socket.socket] = socket.socket,
    monotonic: Callable[[], float] = time.monotonic,
) -> BroadcastSnapshot:
    """Read a short filtered snapshot without changing interface state.

    The safety wrappers decide whether the caller is a listen-only observer or
    the exclusive owner of an armed diagnostic interval. This primitive only
    receives allowlisted standard broadcast identifiers; it never configures
    or transmits through the interface.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if (
        not isinstance(required_rpm_samples, int)
        or isinstance(required_rpm_samples, bool)
        or required_rpm_samples < 1
    ):
        raise ValueError("required_rpm_samples must be a positive integer")
    sock = socket_factory(AF_CAN, socket.SOCK_RAW, CAN_RAW)
    samples: dict[str, list[PassiveObservation]] = {}
    rpm_samples: list[float] = []
    frame_count = 0
    try:
        filter_ids = ACTIVE_FILTER_IDS if include_battery else FILTER_IDS
        filters = b"".join(
            struct.pack("=II", can_id, FILTER_MASK) for can_id in filter_ids
        )
        sock.setsockopt(SOL_CAN_RAW, CAN_RAW_FILTER, filters)
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
            if len(frame) != 16:
                raise RuntimeError(
                    f"raw SocketCAN broadcast frame length {len(frame)} is not 16"
                )
            can_id, dlc, raw_data = struct.unpack("=IB3x8s", frame)
            if dlc > 8:
                raise RuntimeError(
                    f"classic CAN broadcast DLC {dlc} exceeds 8"
                )
            if can_id & FRAME_TYPE_FLAGS:
                continue
            frame_count += 1
            observations = decode_frame_observations(
                can_id & SFF_MASK, raw_data[: min(dlc, 8)]
            )
            for observation in observations:
                samples.setdefault(observation.metric, []).append(observation)
                if observation.metric == "engine.rpm":
                    rpm_samples.append(float(observation.value))
            required_metrics = (
                "engine.oil_pressure",
                "engine.coolant_temperature",
                "engine.rpm",
                "engine.target_crankshaft_torque",
                "vehicle.speed",
                "transmission.output_speed",
                "transmission.oil_temperature",
                "transmission.turbine_speed",
                "vehicle.ignition_on",
            ) + (("battery.voltage",) if include_battery else ())
            if (
                len(rpm_samples) >= required_rpm_samples
                and all(metric in samples for metric in required_metrics)
            ):
                break
    finally:
        sock.close()
    return BroadcastSnapshot(
        observations=tuple(
            _median_observation(samples[metric])
            for metric in sorted(samples)
        ),
        rpm_samples=tuple(rpm_samples),
        frame_count=frame_count,
        completed_monotonic=monotonic(),
    )


def read_snapshot(
    channel: str = CHANNEL,
    *,
    timeout: float = 0.5,
    socket_factory: Callable[..., socket.socket] = socket.socket,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[PassiveObservation, ...]:
    """Compatibility wrapper for the normal listen-only snapshot."""
    return read_broadcast_snapshot(
        channel,
        timeout=timeout,
        socket_factory=socket_factory,
        monotonic=monotonic,
    ).observations


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
