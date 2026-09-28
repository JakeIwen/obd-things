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
# Owner-referenced cluster display frames (2026-09-27 drive):
# 0x0E0 B0 is the traffic-sign speed limit in mph (10 Hz); 0x5A0 is the
# cluster ACC frame (1 Hz plus on change) whose B3 is the set speed in mph.
SPEED_LIMIT_ID = 0x0E0
ACC_DISPLAY_ID = 0x5A0
DISPLAY_FRAME_IDS = (SPEED_LIMIT_ID, ACC_DISPLAY_ID)
FILTER_IDS = (
    OIL_PRESSURE_ID,
    COOLANT_TEMPERATURE_ID,
    ENGINE_SPEED_ID,
    TARGET_CRANK_TORQUE_ID,
    VEHICLE_SPEED_ID,
    TRANSMISSION_SHAFT_SPEED_ID,
    IGNITION_ON_ID,
) + DISPLAY_FRAME_IDS
ACTIVE_FILTER_IDS = FILTER_IDS + (SYSTEM_VOLTAGE_ID,)
KPA_TO_PSI = 0.14503773773020923
KMH_TO_MPH = 0.621371192237334
NM_TO_LB_FT = 0.7375621492772656
TRANSMISSION_TEMPERATURE_MAX_DELTA_C = 10.0
TRANSMISSION_TEMPERATURE_DELTA_WINDOW_SECONDS = 1.0
TRANSMISSION_TEMPERATURE_METRIC = "transmission.oil_temperature"
TRANSMISSION_TEMPERATURE_SOURCE = "ccan.broadcast.0x1f7"
SPEED_LIMIT_SOURCE = "ccan.broadcast.0x0e0"
ACC_DISPLAY_SOURCE = "ccan.broadcast.0x5a0"
# Candidate ACC state enum, ((B6 & 1) << 2) | (B7 >> 6); raw 3 (seen once for
# 80 ms at CANC), 6 and 7 are unmapped and never published.
ACC_STATE_NAMES = {
    0: "off",
    1: "ready",
    2: "engaged",
    4: "override",
    5: "standby",
}
# The set speed is only meaningful while ACC is ready/engaged/override/standby.
# Ready reads 0 until the first SET, and 0 is never published.
ACC_SET_SPEED_STATES = frozenset({1, 2, 4, 5})
DISPLAY_SPEED_MAX_MPH = 120
MPH_TO_KMH = 1.609344
# How long a display frame counts as recently seen, so a snapshot need not keep
# listening for it (see LowRateFrameWait).
DISPLAY_REFRESH_SECONDS = {SPEED_LIMIT_ID: 0.0, ACC_DISPLAY_ID: 2.0}


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


class LowRateFrameWait:
    """Decide which low-rate display frames a bounded snapshot waits for.

    A snapshot normally ends as soon as the powertrain set is complete, often
    within about 0.1 s. That would sample the 1 Hz ``0x5A0`` ACC frame in only
    a small fraction of cycles, and could systematically miss a 10 Hz frame
    whose phase trails the last required frame. With this helper a snapshot
    keeps listening, never past its own timeout, for a display frame that was
    not seen within its refresh interval. Values are never carried across
    snapshots: the broker stamps each observation on receipt, so only a frame
    actually received in this snapshot is published.

    If a wanted frame stays absent through several extended waits and for at
    least ``absent_seconds`` (its module is not transmitting), waiting for it
    backs off so an absent frame cannot pin every snapshot at its full
    timeout. A 0.35-0.5 s window misses a 1 Hz frame about half the time, so
    both conditions are needed. A frame seen incidentally during the back-off
    clears it.
    """

    def __init__(
        self,
        refresh_seconds: dict[int, float] | None = None,
        *,
        misses_before_backoff: int = 6,
        absent_seconds: float = 10.0,
        backoff_seconds: float = 30.0,
    ) -> None:
        self.refresh_seconds = dict(
            DISPLAY_REFRESH_SECONDS if refresh_seconds is None else refresh_seconds
        )
        self.misses_before_backoff = misses_before_backoff
        self.absent_seconds = absent_seconds
        self.backoff_seconds = backoff_seconds
        self._lock = threading.Lock()
        self._last_seen: dict[int, float] = {}
        self._misses: dict[int, int] = {}
        self._backoff_until: dict[int, float] = {}

    def wanted(self, now: float) -> frozenset[int]:
        with self._lock:
            wanted = set()
            for can_id, refresh in self.refresh_seconds.items():
                if now < self._backoff_until.get(can_id, float("-inf")):
                    continue
                last = self._last_seen.get(can_id)
                if last is None or now - last >= refresh:
                    wanted.add(can_id)
            return frozenset(wanted)

    def finish(
        self,
        wanted: frozenset[int],
        seen: dict[int, float],
        *,
        extended_to_deadline: bool,
        now: float,
    ) -> None:
        """Record one snapshot: ``seen`` maps display ids to frame times."""
        with self._lock:
            for can_id, observed in seen.items():
                if can_id not in self.refresh_seconds:
                    continue
                self._last_seen[can_id] = observed
                self._misses[can_id] = 0
                self._backoff_until.pop(can_id, None)
            if not extended_to_deadline:
                return
            for can_id in wanted:
                if can_id in seen:
                    continue
                misses = self._misses.get(can_id, 0) + 1
                last = self._last_seen.get(can_id)
                if misses >= self.misses_before_backoff and (
                    last is None or now - last >= self.absent_seconds
                ):
                    self._backoff_until[can_id] = now + self.backoff_seconds
                    misses = 0
                self._misses[can_id] = misses


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
    if can_id == SPEED_LIMIT_ID and data:
        limit = int(data[0])
        # 0 is what the cluster sends with no limit (key-off/none); it is not
        # a speed, so it is never published and the metric goes stale.
        if not 0 < limit <= DISPLAY_SPEED_MAX_MPH:
            return ()
        return (
            PassiveObservation(
                metric="vehicle.speed_limit",
                value=limit,
                unit="mph",
                source=SPEED_LIMIT_SOURCE,
                quality="verified",
                detail="0x0E0 byte 0 raw mph (cluster speed-limit display)",
            ),
        )
    if can_id == ACC_DISPLAY_ID and len(data) >= 8:
        return _decode_acc_display(data)
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


def acc_state_raw(data: bytes) -> int:
    """Candidate 3-bit ACC state: B6 bit0 is the high bit, B7 bits 7:6 below it."""
    return ((data[6] & 0x01) << 2) | (data[7] >> 6)


def _decode_acc_display(data: bytes) -> tuple[PassiveObservation, ...]:
    state = acc_state_raw(data)
    observations = []
    name = ACC_STATE_NAMES.get(state)
    if name is not None:
        observations.append(
            PassiveObservation(
                metric="acc.state",
                value=name,
                unit="state",
                source=ACC_DISPLAY_SOURCE,
                quality="candidate",
                detail=(
                    f"0x5A0 ((B6 & 1) << 2) | (B7 >> 6) = {state} ({name}); "
                    "candidate enum"
                ),
            )
        )
    mph = int(data[3])
    kmh = int(data[2])
    if (
        state in ACC_SET_SPEED_STATES
        and 0 < mph <= DISPLAY_SPEED_MAX_MPH
        # B2 is the same set speed in km/h (round(mph x 1.609344)); a pair
        # that disagrees is not a set-speed frame this decode understands.
        and abs(kmh - mph * MPH_TO_KMH) <= 1.0
    ):
        observations.append(
            PassiveObservation(
                metric="acc.set_speed",
                value=mph,
                unit="mph",
                source=ACC_DISPLAY_SOURCE,
                quality="verified",
                detail=(
                    f"0x5A0 byte 3 raw mph (byte 2 = {kmh} km/h) while the "
                    f"candidate ACC state is {name}"
                ),
            )
        )
    return tuple(observations)


def decode_frame(can_id: int, data: bytes) -> PassiveObservation | None:
    """Decode the first allowlisted observation, retained for callers/tests."""
    observations = decode_frame_observations(can_id, data)
    return observations[0] if observations else None


# Frozen ZF 9HP48 ratio bands for the gear estimate (see metrics.TRANSMISSION_GEAR_ESTIMATE).
GEAR_ESTIMATE_SOURCE = "derived.ccan_0x1f7_shaft_ratio"
GEAR_RATIO_BANDS = (
    ("R", 3.8358209),
    ("1", 4.7191978),
    ("2", 2.8451178),
    ("3", 1.9101382),
    ("4", 1.3819561),
    ("5", 0.9999915),
    ("6", 0.8080387),
    ("7", 0.6990113),
)
GEAR_RATIO_TOLERANCE = 0.03
GEAR_MIN_OUTPUT_RPM = 100.0
GEAR_MIN_ENGINE_RPM = 500.0
GEAR_MIN_SPEED_MPH = 3.0
# A forward 1->2 upshift sweeps the ratio through R's band (4.72 -> 2.85 passes 3.84); the
# 2026-09-24 drive did so at 8.2 and 13.8 mph. Reverse is never driven that fast.
GEAR_MAX_REVERSE_SPEED_MPH = 6.0


def gear_from_ratio(ratio: float) -> str | None:
    """Gear label for a turbine/output ratio, or None outside every frozen band."""
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    for label, center in GEAR_RATIO_BANDS:
        if abs(ratio - center) <= center * GEAR_RATIO_TOLERANCE:
            return label
    return None


def gear_estimate(observations) -> "PassiveObservation | None":
    """Derive the gear estimate from one snapshot's median observations (moving only)."""
    values = {o.metric: o.value for o in observations}
    output = values.get("transmission.output_speed")
    turbine = values.get("transmission.turbine_speed")
    rpm = values.get("engine.rpm")
    speed = values.get("vehicle.speed")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (output, turbine, rpm, speed)):
        return None
    if output <= GEAR_MIN_OUTPUT_RPM or rpm <= GEAR_MIN_ENGINE_RPM or speed <= GEAR_MIN_SPEED_MPH:
        return None
    ratio = float(turbine) / float(output)
    label = gear_from_ratio(ratio)
    if label is None or (label == "R" and speed > GEAR_MAX_REVERSE_SPEED_MPH):
        return None
    return PassiveObservation(
        metric="transmission.gear_estimate",
        value=label,
        unit="gear",
        source=GEAR_ESTIMATE_SOURCE,
        quality="candidate",
        detail=f"0x1F7 shaft ratio {ratio:.3f} within 3 % of the frozen gear {label} band",
    )


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
    display_wait: LowRateFrameWait | None = None,
) -> BroadcastSnapshot:
    """Read a short filtered snapshot without changing interface state.

    The safety wrappers decide whether the caller is a listen-only observer or
    the exclusive owner of an armed diagnostic interval. This primitive only
    receives allowlisted standard broadcast identifiers; it never configures
    or transmits through the interface.

    Without ``display_wait`` the display frames (0x0E0, 0x5A0) are decoded
    only when they arrive before the powertrain set completes. With it, the
    snapshot may keep listening for them, bounded by ``timeout``.
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
    # Display frames keep only their latest frame's observations, so a state
    # change inside the window cannot pair a new state with an old set speed.
    display_latest: dict[int, tuple[PassiveObservation, ...]] = {}
    display_counts: dict[int, int] = {}
    display_seen: dict[int, float] = {}
    wanted_display = (
        display_wait.wanted(monotonic()) if display_wait is not None else frozenset()
    )
    # Only a snapshot that had everything else and still listened to its
    # deadline is evidence that a wanted display frame is absent.
    extended_for_display = False
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
            standard_id = can_id & SFF_MASK
            observations = decode_frame_observations(
                standard_id, raw_data[: min(dlc, 8)]
            )
            if standard_id in DISPLAY_FRAME_IDS:
                display_seen[standard_id] = frame_observed_monotonic
                display_counts[standard_id] = (
                    display_counts.get(standard_id, 0) + 1
                )
                display_latest[standard_id] = observations
                observations = ()
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
            if len(rpm_samples) >= required_rpm_samples and all(
                metric in samples for metric in required_metrics
            ):
                if wanted_display.issubset(display_seen):
                    extended_for_display = False
                    break
                extended_for_display = True
    finally:
        sock.close()
    medians = tuple(_median_observation(samples[metric]) for metric in sorted(samples))
    gear = gear_estimate(medians)
    display = tuple(
        PassiveObservation(
            metric=observation.metric,
            value=observation.value,
            unit=observation.unit,
            source=observation.source,
            quality=observation.quality,
            detail=(
                f"{observation.detail}; latest of "
                f"{display_counts[can_id]} frame(s)"
            ),
        )
        for can_id in sorted(display_latest)
        for observation in display_latest[can_id]
    )
    completed = monotonic()
    if display_wait is not None:
        display_wait.finish(
            wanted_display,
            display_seen,
            extended_to_deadline=extended_for_display,
            now=completed,
        )
    return BroadcastSnapshot(
        observations=medians + ((gear,) if gear is not None else ()) + display,
        rpm_samples=tuple(rpm_samples),
        frame_count=frame_count,
        completed_monotonic=completed,
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
