"""Vendor-derived AlfaOBD ZF9HP Plots definitions and payload decoder.

The definitions in this module are scoped to AlfaOBD's ``ZF9HP`` profile.  They
come from the owner-supplied APK's ordered request table, parameter database,
and the profile-specific ``Ln0/z1;->r2()V`` decoder.  They are not proof that a
particular vehicle/TCM supports every DID.

``decode_payload`` expects only the data bytes after the positive ``62 <DID>``
echo.  ``decode_positive_response`` accepts the complete positive response.
No CAN, ADB, file, network, or vehicle interface is opened here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType


@dataclass(frozen=True)
class SignalDefinition:
    order: int
    did: int
    key: str
    label: str
    unit: str
    byte_offset: int
    width: int
    signed: bool
    scale: Decimal
    offset: Decimal


@dataclass(frozen=True)
class DecodedSignal:
    definition: SignalDefinition
    raw_value: int
    value: Decimal


def _signal(
    order: int,
    did: int,
    key: str,
    label: str,
    unit: str,
    *,
    byte_offset: int = 0,
    width: int = 2,
    signed: bool = False,
    scale: str = "1",
    offset: str = "0",
) -> SignalDefinition:
    return SignalDefinition(
        order=order,
        did=did,
        key=key,
        label=label,
        unit=unit,
        byte_offset=byte_offset,
        width=width,
        signed=signed,
        scale=Decimal(scale),
        offset=Decimal(offset),
    )


CATALOG = (
    _signal(1, 0xF40D, "vehicle_speed", "Vehicle speed", "km/h", width=1),
    _signal(2, 0x2B1B, "front_left_wheel_speed", "Front left wheel speed", "km/h", scale="0.0078125"),
    _signal(3, 0x2B1C, "front_right_wheel_speed", "Front right wheel speed", "km/h", scale="0.0078125"),
    _signal(4, 0x2B1D, "rear_left_wheel_speed", "Rear left wheel speed", "km/h", scale="0.0078125"),
    _signal(5, 0x2B1E, "rear_right_wheel_speed", "Rear right wheel speed", "km/h", scale="0.0078125"),
    _signal(6, 0xF40C, "engine_speed", "Engine speed", "rpm", scale="0.25"),
    _signal(7, 0x0500, "torque_converter_slip_speed", "Torque Converter Slip Speed", "rpm", signed=True),
    _signal(8, 0x2102, "turbine_speed", "Turbine speed", "rpm", scale="0.25"),
    _signal(9, 0x2103, "gearbox_output_revs", "Gearbox output revs", "rpm", scale="0.25"),
    _signal(
        10,
        0x2106,
        "output_shaft_vehicle_speed",
        "Vehicle Speed Calculated By Output Shaft Speed",
        "km/h",
        scale="0.01",
    ),
    _signal(11, 0x212F, "valve_supply_voltage", "Voltage Power Supply Of Valves", "V", scale="0.001"),
    _signal(
        12,
        0x051D,
        "accelerator_pedal_relative_position",
        "Accelerator Pedal Relative position",
        "%",
        width=1,
        scale="0.392",
    ),
    _signal(13, 0xF449, "throttle_position", "Throttle position", "%", width=1, scale="0.392156999999999"),
    _signal(14, 0xF405, "water_temperature", "Water temperature", "°C", width=1, offset="-40"),
    _signal(15, 0x0301, "tcu_chip_temperature", "TCU chip temperature", "°C", width=1, offset="-40"),
    _signal(16, 0x04FE, "gearbox_oil_temperature", "Gearbox oil temperature", "°C", width=1, offset="-40"),
    _signal(17, 0x1018, "actual_crankshaft_torque", "Actual Crankshaft Torque", "Nm", offset="-500"),
    _signal(
        18,
        0x101A,
        "crankshaft_torque_without_tcu_requests",
        "Crankshaft Torque, without TCU Torque Requests",
        "Nm",
        offset="-500",
    ),
    _signal(19, 0x101B, "target_crankshaft_torque", "Target Crankshaft Torque", "Nm", offset="-500"),
    _signal(
        20,
        0x101D,
        "transmission_torque_intervention",
        "Transmission Torque Intervention",
        "Nm",
        scale="0.25",
        offset="-500",
    ),
    _signal(
        21,
        0x101F,
        "maximum_engine_torque_requested",
        "Maximum Engine Torque Requested By Transmission",
        "Nm",
        offset="-500",
    ),
    _signal(
        22,
        0x1020,
        "slow_path_transmission_torque_intervention",
        "Slow Path Transmission Torque Intervention",
        "Nm",
        offset="-500",
    ),
    _signal(23, 0x1024, "shift_solenoid_b_current", "Shift Solenoid B Current", "mA", scale="0.1"),
    _signal(24, 0x1025, "shift_solenoid_c_current", "Shift Solenoid C Current", "mA", scale="0.1"),
    _signal(25, 0x1026, "shift_solenoid_d_current", "Shift Solenoid D Current", "mA", scale="0.1"),
    _signal(26, 0x1027, "shift_solenoid_e_current", "Shift Solenoid E Current", "mA", scale="0.1"),
    _signal(27, 0x1028, "torque_converter_solenoid_current", "Torque Converter Solenoid Current", "mA", scale="0.1"),
    _signal(28, 0x1029, "system_pressure_solenoid_current", "System Pressure Solenoid Current", "mA", scale="0.1"),
    _signal(29, 0x102C, "shift_solenoid_a_current", "Shift Solenoid A Current", "mA", scale="0.1"),
    _signal(30, 0x1037, "shift_solenoid_f_current", "Shift Solenoid F Current", "mA", scale="0.1"),
    _signal(31, 0x1038, "park_solenoid_current", "Park Solenoid", "mA", scale="0.1"),
    _signal(32, 0x1039, "park_magnet_solenoid_current", "Park Magnet Solenoid", "mA", scale="0.1"),
    _signal(33, 0x211F, "clutch_b_filling_pressure", "Clutch B - Filling Pressure", "mbar", width=1, signed=True, scale="10"),
    _signal(34, 0x2120, "clutch_b_filling_counter", "Clutch B - Filling Counter", "N", width=1),
    _signal(35, 0x2121, "clutch_b_filling_time", "Clutch B - Filling Time", "msec", width=1, signed=True, scale="2"),
    _signal(36, 0x2122, "clutch_b_fast_filling_counter", "Clutch B - Fast Filling Counter", "N", width=1),
    _signal(37, 0x2123, "clutch_c_filling_pressure", "Clutch C - Filling Pressure", "mbar", width=1, signed=True, scale="10"),
    _signal(38, 0x2124, "clutch_c_filling_counter", "Clutch C - Filling Counter", "N", width=1),
    _signal(39, 0x2125, "clutch_c_filling_time", "Clutch C - Filling Time", "msec", width=1, signed=True, scale="2"),
    _signal(40, 0x2126, "clutch_c_fast_filling_counter", "Clutch C - Fast Filling Counter", "N", width=1),
    _signal(41, 0x2127, "clutch_d_filling_pressure", "Clutch D - Filling Pressure", "mbar", width=1, signed=True, scale="10"),
    _signal(42, 0x2128, "clutch_d_filling_counter", "Clutch D - Filling Counter", "N", width=1),
    _signal(43, 0x2129, "clutch_d_filling_time", "Clutch D - Filling Time", "msec", width=1, signed=True, scale="2"),
    _signal(44, 0x212A, "clutch_d_fast_filling_counter", "Clutch D - Fast Filling Counter", "N", width=1),
    _signal(45, 0x212B, "clutch_e_filling_pressure", "Clutch E - Filling Pressure", "mbar", width=1, signed=True, scale="10"),
    _signal(46, 0x212C, "clutch_e_filling_counter", "Clutch E - Filling Counter", "N", width=1),
    _signal(47, 0x212D, "clutch_e_filling_time", "Clutch E - Filling Time", "msec", width=1, signed=True, scale="2"),
    _signal(48, 0x212E, "clutch_e_fast_filling_counter", "Clutch E - Fast Filling Counter", "N", width=1),
    _signal(49, 0x213B, "tcc_boost_time_offset", "TCC boost time offset", "msec", signed=True, scale="10"),
    _signal(50, 0x213C, "tcc_base_point_adapt", "TCC base point adapt", "mA", signed=True),
    _signal(51, 0x213D, "gear_engagement_0", "Gear engagement 0", "mbar", signed=True),
    _signal(52, 0x213D, "gear_engagement_1", "Gear engagement 1", "mbar", byte_offset=2, signed=True),
    _signal(53, 0x213D, "gear_engagement_2", "Gear engagement 2", "mbar", byte_offset=4, signed=True),
    _signal(54, 0x213E, "gear_disengagement_0", "Gear disengagement 0", "mbar", signed=True),
    _signal(55, 0x213E, "gear_disengagement_1", "Gear disengagement 1", "mbar", byte_offset=2, signed=True),
    _signal(56, 0x213E, "gear_disengagement_2", "Gear disengagement 2", "mbar", byte_offset=4, signed=True),
)


def _by_did() -> MappingProxyType:
    grouped: dict[int, list[SignalDefinition]] = {}
    for definition in CATALOG:
        grouped.setdefault(definition.did, []).append(definition)
    return MappingProxyType(
        {did: tuple(definitions) for did, definitions in grouped.items()}
    )


BY_DID = _by_did()

PRIORITY_DIDS = (
    0xF40C,
    0x0500,
    0x2102,
    0x2103,
    0xF405,
    0x0301,
    0x04FE,
    0x1018,
    0x101A,
    0x101B,
    0x101D,
    0x101F,
    0x1020,
)


def decode_payload(did: int, payload: bytes) -> tuple[DecodedSignal, ...]:
    """Decode one ZF9HP DID payload using the recovered AlfaOBD formulas."""
    try:
        definitions = BY_DID[did]
    except KeyError as exc:
        raise KeyError(f"unknown ZF9HP DID {did:04X}") from exc

    decoded = []
    for definition in definitions:
        end = definition.byte_offset + definition.width
        if len(payload) < end:
            raise ValueError(
                f"ZF9HP DID {did:04X} needs at least {end} payload byte(s); "
                f"received {len(payload)}"
            )
        raw = int.from_bytes(
            payload[definition.byte_offset:end],
            byteorder="big",
            signed=definition.signed,
        )
        decoded.append(
            DecodedSignal(
                definition=definition,
                raw_value=raw,
                value=Decimal(raw) * definition.scale + definition.offset,
            )
        )
    return tuple(decoded)


def decode_positive_response(response: bytes) -> tuple[DecodedSignal, ...]:
    """Validate and decode a complete ``62 <DID> <payload>`` response."""
    if len(response) < 3:
        raise ValueError("positive response is shorter than 62 <DID>")
    if response[0] != 0x62:
        raise ValueError(f"expected positive ReadDataByIdentifier SID 62, got {response[0]:02X}")
    did = int.from_bytes(response[1:3], byteorder="big")
    return decode_payload(did, response[3:])
