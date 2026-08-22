"""Read evidence-qualified engine-health broadcasts from C-CAN.

This module is receive-only and never configures or transmits through
SocketCAN. Callers must supply the channel resolved for the C-CAN USB identity
and hold the appropriate logical-role and resolved-channel locks. The
coordinated active-drive owner may use the bounded snapshot primitive while it
holds exclusive ownership and honestly reports the interface as armed. Signal
provenance lives in the PCM plots finding and the public metric registry; this
module only implements those fixed decodes.
"""

from __future__ import annotations

import errno
import math
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

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
TRANSMISSION_TEMPERATURE_MAX_DELTA_C = 10.0
TRANSMISSION_TEMPERATURE_DELTA_WINDOW_SECONDS = 1.0
TRANSMISSION_TEMPERATURE_METRIC = "transmission.oil_temperature"
TRANSMISSION_TEMPERATURE_SOURCE = "ccan.broadcast.0x1f7"


@dataclass(frozen=True)
class PassiveObservation:
    metric: str
    value: float | bool
    unit: str
    source: str
    quality: str
    detail: str


@dataclass(frozen=True)
class DataQualityEvent:
    """Bounded evidence that one raw value was excluded from telemetry.

    These events describe acquisition quality only.  They are deliberately
    separate from vehicle-health advisories and never authorize CAN traffic.
    """

    metric: str
    source: str
    reason: str
    detail: str
    previous_value_c: float
    rejected_value_c: float
    delta_c: float
    elapsed_seconds: float
    rejection_count: int = 1

    def coalesced_with(self, newer: "DataQualityEvent") -> "DataQualityEvent":
        if (
            newer.metric != self.metric
            or newer.source != self.source
            or newer.reason != self.reason
        ):
            raise ValueError("cannot coalesce unlike data-quality events")
        return DataQualityEvent(
            metric=newer.metric,
            source=newer.source,
            reason=newer.reason,
            detail=newer.detail,
            previous_value_c=newer.previous_value_c,
            rejected_value_c=newer.rejected_value_c,
            delta_c=newer.delta_c,
            elapsed_seconds=newer.elapsed_seconds,
            rejection_count=self.rejection_count + newer.rejection_count,
        )


class TransmissionTemperaturePlausibilityGate:
    """Stateful raw-frame implementation of the OEM P0711 delta criterion.

    A jump greater than 10 degrees C in less than one second is rejected.  A
    rejected level never becomes the comparison baseline: while raw frames
    continue without a one-second observation gap, values still more than 10
    degrees C from the last good level remain quarantined.  Returning to the
    last-good neighborhood clears the quarantine.  A gap of at least one
    second begins a new evidence window because the strict OEM criterion can
    no longer establish when a change occurred.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_good_c: float | None = None
        self._last_seen_monotonic: float | None = None
        self._quarantined = False

    @property
    def quarantined(self) -> bool:
        with self._lock:
            return self._quarantined

    def evaluate(
        self,
        value_c: float,
        observed_monotonic: float,
    ) -> DataQualityEvent | None:
        # The normal passive and active owners call this serially, but keeping
        # the full state transition atomic also makes accidental shared-reader
        # use deterministic rather than corrupting the last-good baseline.
        with self._lock:
            return self._evaluate_unlocked(value_c, observed_monotonic)

    def _evaluate_unlocked(
        self,
        value_c: float,
        observed_monotonic: float,
    ) -> DataQualityEvent | None:
        if not math.isfinite(value_c) or not math.isfinite(observed_monotonic):
            raise ValueError("transmission temperature and timestamp must be finite")
        value_c = float(value_c)
        observed_monotonic = float(observed_monotonic)
        previous_c = self._last_good_c
        previous_seen = self._last_seen_monotonic
        if previous_c is None or previous_seen is None:
            self._last_good_c = value_c
            self._last_seen_monotonic = observed_monotonic
            self._quarantined = False
            return None

        elapsed = observed_monotonic - previous_seen
        delta_c = abs(value_c - previous_c)
        if elapsed <= 0:
            return DataQualityEvent(
                metric=TRANSMISSION_TEMPERATURE_METRIC,
                source=TRANSMISSION_TEMPERATURE_SOURCE,
                reason="implausible_transition",
                detail=(
                    "raw 0x1F7 transmission-oil temperature arrived without "
                    "a newer monotonic frame timestamp; last good retained"
                ),
                previous_value_c=previous_c,
                rejected_value_c=value_c,
                delta_c=delta_c,
                elapsed_seconds=elapsed,
            )

        self._last_seen_monotonic = observed_monotonic
        if elapsed >= TRANSMISSION_TEMPERATURE_DELTA_WINDOW_SECONDS:
            self._last_good_c = value_c
            self._quarantined = False
            return None
        if delta_c <= TRANSMISSION_TEMPERATURE_MAX_DELTA_C:
            self._last_good_c = value_c
            self._quarantined = False
            return None

        was_quarantined = self._quarantined
        self._quarantined = True
        qualifier = " remains quarantined" if was_quarantined else " was rejected"
        return DataQualityEvent(
            metric=TRANSMISSION_TEMPERATURE_METRIC,
            source=TRANSMISSION_TEMPERATURE_SOURCE,
            reason="implausible_transition",
            detail=(
                f"raw 0x1F7 transmission-oil temperature{qualifier}: "
                f"{value_c:.3f} degrees C differs from last good "
                f"{previous_c:.3f} degrees C by {delta_c:.3f} degrees C in "
                f"{elapsed:.3f}s; OEM-context limit is more than 10 degrees C "
                "within less than one second"
            ),
            previous_value_c=previous_c,
            rejected_value_c=value_c,
            delta_c=delta_c,
            elapsed_seconds=elapsed,
        )


@dataclass(frozen=True)
class BroadcastSnapshot:
    """One bounded raw-broadcast sample collected without changing CAN state."""

    observations: tuple[PassiveObservation, ...]
    rpm_samples: tuple[float, ...]
    frame_count: int
    completed_monotonic: float | None = None
    quality_events: tuple[DataQualityEvent, ...] = ()


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
    channel: str,
    *,
    timeout: float = 0.5,
    include_battery: bool = False,
    required_rpm_samples: int = 1,
    socket_factory: Callable[..., socket.socket] = socket.socket,
    monotonic: Callable[[], float] = time.monotonic,
    temperature_gate: TransmissionTemperaturePlausibilityGate | None = None,
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
    quality_events: dict[tuple[str, str, str], DataQualityEvent] = {}
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
            frame_observed_monotonic = monotonic()
            observations = decode_frame_observations(
                can_id & SFF_MASK, raw_data[: min(dlc, 8)]
            )
            for observation in observations:
                if (
                    temperature_gate is not None
                    and observation.metric == TRANSMISSION_TEMPERATURE_METRIC
                    and observation.source == TRANSMISSION_TEMPERATURE_SOURCE
                ):
                    value_c = (float(observation.value) - 32.0) * 5.0 / 9.0
                    rejection = temperature_gate.evaluate(
                        value_c,
                        frame_observed_monotonic,
                    )
                    if rejection is not None:
                        key = (
                            rejection.metric,
                            rejection.source,
                            rejection.reason,
                        )
                        previous_event = quality_events.get(key)
                        quality_events[key] = (
                            rejection
                            if previous_event is None
                            else previous_event.coalesced_with(rejection)
                        )
                        # The two shaft-speed observations from this same 0x1F7
                        # frame remain valid and continue through aggregation.
                        continue
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
        quality_events=tuple(quality_events.values()),
    )


def read_snapshot(
    channel: str,
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
