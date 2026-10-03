"""Fixed broadcast geometry and existing telemetry decodes for this vehicle.

This is a pure, receive-side table, not a bus reader or a signal discovery API.
The values and provenance preserve the existing readers' arithmetic.  In
particular, division is explicit (multiplying by its reciprocal can round
differently), and display enums, validity policies and stateful quality gates
remain with their consumers.  A row's minimum DLC is the reader's guard, not
necessarily the minimum number of bytes occupied by its field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from lib.signal_fields import SignalField


KPA_TO_PSI = 0.14503773773020923
KMH_TO_MPH = 0.621371192237334
NM_TO_LB_FT = 0.7375621492772656


def _fahrenheit(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


def _psi(kpa: float) -> float:
    return kpa * KPA_TO_PSI


def _mph(kmh: float) -> float:
    return kmh * KMH_TO_MPH


def _lb_ft(nm: float) -> float:
    return nm * NM_TO_LB_FT


@dataclass(frozen=True)
class BroadcastSignal:
    """One fixed field, with the original observation metadata and DLC guard.

    Numeric decoding applies ``raw * scale / divisor + offset`` before the
    optional unit conversion.  ``field=None`` denotes an ignition presence
    witness, including a zero-length frame; absence is never decoded as False.
    """

    bus: str
    can_id: int
    metric: str
    field: SignalField | None
    scale: float
    offset: float
    units: str
    quality: str
    min_dlc: int
    provenance: str
    divisor: float = 1.0
    conversion: Callable[[float], float] | None = None

    @property
    def source(self) -> str:
        return f"{self.bus.replace('-', '')}.broadcast.0x{self.can_id:03x}"

    def decode(self, data: bytes | bytearray) -> float | bool | None:
        """Return the existing value, or None below this reader's DLC guard.

        Trailing bytes do not affect these fields.  Slice before extraction so
        the bit helper's CAN-FD size limit does not change the old readers'
        behavior for an overlong byte string supplied by an offline caller.
        """
        if len(data) < self.min_dlc:
            return None
        if self.field is None:
            return True
        raw = self.field.extract(data[:self.field.required_payload_bytes])
        value = raw * self.scale / self.divisor + self.offset
        return value if self.conversion is None else self.conversion(value)


SYSTEM_VOLTAGE = BroadcastSignal(
    bus="c-can", can_id=0x41A, metric="battery.voltage",
    field=SignalField(7, 8, "big"), scale=0.05, offset=4.0,
    units="V", quality="verified", min_dlc=1,
    provenance="0x41A byte 0 x 0.05 V + 4.0 V",
)
OIL_PRESSURE = BroadcastSignal(
    bus="c-can", can_id=0x41D, metric="engine.oil_pressure",
    field=SignalField(23, 8, "big"), scale=4.0, offset=0.0,
    units="psi", quality="observed_alfa_scale", min_dlc=3,
    provenance="0x41D byte 2 x 4 kPa, converted to psi for telemetry",
    conversion=_psi,
)
COOLANT_TEMPERATURE = BroadcastSignal(
    bus="c-can", can_id=0x2ED, metric="engine.coolant_temperature",
    field=SignalField(7, 8, "big"), scale=1.0, offset=-40.0,
    units="°F", quality="observed_alfa_scale", min_dlc=1,
    provenance="0x2ED byte 0 - 40 °C, converted to °F for telemetry",
    conversion=_fahrenheit,
)
ENGINE_SPEED = BroadcastSignal(
    bus="c-can", can_id=0x0FC, metric="engine.rpm",
    field=SignalField(7, 14, "big"), scale=1.0, offset=0.0,
    units="rpm", quality="observed_alfa_scale", min_dlc=2,
    provenance="0x0FC bytes 0-1 big-endian, low 2 bits masked, / 4 rpm",
)
TARGET_CRANK_TORQUE = BroadcastSignal(
    bus="c-can", can_id=0x100, metric="engine.target_crankshaft_torque",
    field=SignalField(31, 11, "big"), scale=1.0, offset=-500.0,
    units="lb-ft", quality="observed_alfa_scale", min_dlc=5,
    provenance=(
        "0x100 bytes 3-4 big-endian >> 5, then -500 Nm; "
        "TCM target, not measured output; converted to lb-ft"
    ),
    conversion=_lb_ft,
)
VEHICLE_SPEED = BroadcastSignal(
    bus="c-can", can_id=0x101, metric="vehicle.speed",
    field=SignalField(0, 12, "big"), scale=1.0, offset=0.0,
    units="mph", quality="observed_alfa_scale", min_dlc=3,
    provenance="0x101 packed 12-bit speed / 16 km/h, converted to mph",
    divisor=16.0, conversion=_mph,
)
TRANSMISSION_OUTPUT_SPEED = BroadcastSignal(
    bus="c-can", can_id=0x1F7, metric="transmission.output_speed",
    field=SignalField(0, 17, "big"), scale=1.0, offset=0.0,
    units="rpm", quality="observed_alfa_scale", min_dlc=6,
    provenance=(
        "0x1F7 packed 17-bit output speed "
        "(byte0 bit0, then bytes 1-2) / 32 rpm"
    ),
    divisor=32.0,
)
TRANSMISSION_OIL_TEMPERATURE = BroadcastSignal(
    bus="c-can", can_id=0x1F7, metric="transmission.oil_temperature",
    field=SignalField(31, 8, "big", signed=True), scale=0.375, offset=57.0,
    units="°F", quality="observed_alfa_scale", min_dlc=6,
    provenance=(
        "0x1F7 byte 3 signed x 0.375 + 57 °C, converted to °F "
        "for telemetry"
    ),
    conversion=_fahrenheit,
)
TRANSMISSION_TURBINE_SPEED = BroadcastSignal(
    bus="c-can", can_id=0x1F7, metric="transmission.turbine_speed",
    field=SignalField(39, 16, "big"), scale=1.0, offset=0.0,
    units="rpm", quality="observed_alfa_scale", min_dlc=6,
    provenance="0x1F7 bytes 4-5 big-endian / 2 rpm", divisor=2.0,
)
IGNITION_ON = BroadcastSignal(
    bus="c-can", can_id=0x2EF, metric="vehicle.ignition_on",
    field=None, scale=1.0, offset=0.0,
    units="boolean", quality="verified", min_dlc=0,
    provenance="0x2EF ignition-on presence gate observed",
)
B_CAN_VOLTAGE = BroadcastSignal(
    bus="b-can", can_id=0x46C, metric="battery.voltage",
    field=SignalField(36, 13, "big"), scale=1.0, offset=0.0,
    units="V", quality="verified", min_dlc=6,
    provenance="docs/bus-map.md B-CAN 0x46C low-13-bit /400 decode",
    divisor=400.0,
)

SIGNALS = (
    SYSTEM_VOLTAGE,
    OIL_PRESSURE,
    COOLANT_TEMPERATURE,
    ENGINE_SPEED,
    TARGET_CRANK_TORQUE,
    VEHICLE_SPEED,
    TRANSMISSION_OUTPUT_SPEED,
    TRANSMISSION_OIL_TEMPERATURE,
    TRANSMISSION_TURBINE_SPEED,
    IGNITION_ON,
    B_CAN_VOLTAGE,
)


def decode(
    can_id: int, data: bytes | bytearray, *, bus: str = "c-can"
) -> tuple[tuple[BroadcastSignal, float | bool], ...]:
    """Decode matching rows in table order, omitting short or unknown frames.

    Callers retain frame-flag checks, physical bus validation, sane-range
    filters, and all stateful gates.  In particular this function does not
    accept an absent ignition frame as evidence that ignition is off.
    """
    values = []
    for signal in SIGNALS:
        if signal.bus == bus and signal.can_id == can_id:
            value = signal.decode(data)
            if value is not None:
                values.append((signal, value))
    return tuple(values)
