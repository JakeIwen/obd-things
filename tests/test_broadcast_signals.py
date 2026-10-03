"""Literal pre-table decodes and exhaustive offline differential coverage."""

from dataclasses import FrozenInstanceError
import random
import unittest

from lib import broadcast_signals as bs
from projects.vehicle_data import ccan_powertrain as cp
from projects.vehicle_data.ccan_powertrain import PassiveObservation, _decode_acc_display


# These are deliberately literal baseline values, not aliases of the table.
SYSTEM_VOLTAGE_ID = 0x41A
OIL_PRESSURE_ID = 0x41D
COOLANT_TEMPERATURE_ID = 0x2ED
ENGINE_SPEED_ID = 0x0FC
TARGET_CRANK_TORQUE_ID = 0x100
VEHICLE_SPEED_ID = 0x101
TRANSMISSION_SHAFT_SPEED_ID = 0x1F7
SPEED_LIMIT_ID = 0x0E0
ACC_DISPLAY_ID = 0x5A0
IGNITION_ON_ID = 0x2EF
KPA_TO_PSI = 0.14503773773020923
KMH_TO_MPH = 0.621371192237334
NM_TO_LB_FT = 0.7375621492772656
DISPLAY_SPEED_MAX_MPH = 120
SPEED_LIMIT_SOURCE = "ccan.broadcast.0x0e0"


def legacy_observations(
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

def table_observations(can_id, data):
    return tuple(
        PassiveObservation(
            metric=signal.metric,
            value=value,
            unit=signal.units,
            source=signal.source,
            quality=signal.quality,
            detail=signal.provenance,
        )
        for signal, value in bs.decode(can_id, data)
    )


def legacy_b_can_voltage(data):
    if len(data) >= 6:
        return (((data[4] << 8) | data[5]) & 0x1FFF) / 400.0
    return None


class BroadcastSignalsTests(unittest.TestCase):
    def check_observations(self, can_id, data):
        expected = legacy_observations(can_id, data)
        actual = table_observations(can_id, data)
        # repr includes every metadata string, tuple order, and value type
        # (1, 1.0, True), and distinguishes signed zero and adjacent floats.
        self.assertEqual(repr(actual), repr(expected), (can_id, data.hex()))
        self.assertEqual(repr(cp.decode_frame_observations(can_id, data)), repr(expected))

    def test_frozen_rows_and_unique_keys(self):
        self.assertEqual(len(bs.SIGNALS), 11)
        self.assertEqual(len({(s.bus, s.can_id, s.metric) for s in bs.SIGNALS}), 11)
        with self.assertRaises(FrozenInstanceError):
            bs.ENGINE_SPEED.can_id = 1

    def test_every_raw_byte(self):
        for raw in range(256):
            data = bytes((raw, 0, raw, raw, 0, 0, 0, 0))
            for can_id in (0x41A, 0x41D, 0x2ED, 0x1F7):
                self.check_observations(can_id, data)

    def test_every_rpm_word_including_masked_bits(self):
        for raw in range(65536):
            self.check_observations(0x0FC, raw.to_bytes(2, "big"))

    def test_every_target_torque_word_including_masked_bits(self):
        for raw in range(65536):
            self.check_observations(0x100, b"\0" * 3 + raw.to_bytes(2, "big"))

    def test_every_vehicle_speed_field(self):
        for raw in range(4096):
            self.check_observations(0x101, bytes((raw >> 11, (raw >> 3) & 255, (raw & 7) << 5)))

    def test_every_output_shaft_field(self):
        for raw in range(131072):
            self.check_observations(0x1F7, raw.to_bytes(3, "big") + b"\0" * 3)

    def test_every_turbine_word(self):
        for raw in range(65536):
            self.check_observations(0x1F7, b"\0" * 4 + raw.to_bytes(2, "big"))

    def test_every_b_can_voltage_word_including_status_bits(self):
        for raw in range(65536):
            data = b"\0" * 4 + raw.to_bytes(2, "big")
            actual = bs.B_CAN_VOLTAGE.decode(data)
            self.assertEqual(actual.hex(), legacy_b_can_voltage(data).hex())
            self.assertEqual(bs.decode(0x46C, data, bus="b-can"), ((bs.B_CAN_VOLTAGE, actual),))

    def test_seeded_payloads_and_every_short_length(self):
        rng = random.Random(0xB10ADCA57)
        ids = (0x41A, 0x41D, 0x2ED, 0x0FC, 0x100, 0x101, 0x1F7, 0x2EF, 0x7FF, 0x46C)
        for length in range(9):
            for _ in range(256):
                data = rng.randbytes(length)
                for can_id in ids:
                    self.check_observations(can_id, data)
                    self.check_observations(can_id, bytearray(data))
                self.assertEqual(bs.B_CAN_VOLTAGE.decode(data), legacy_b_can_voltage(data))

    def test_presence_and_frame_identity(self):
        self.assertIs(bs.IGNITION_ON.decode(b""), True)
        self.assertEqual(bs.decode(0x2EF, b""), ((bs.IGNITION_ON, True),))
        self.assertEqual(bs.decode(0x2EF, b"", bus="b-can"), ())
        self.assertEqual(bs.decode(0x800000FC, b"\0" * 8), ())
        self.assertEqual(bs.decode(0x41A, b""), ())
        self.assertEqual(bs.decode(0x46C, b"\0" * 8), ())

    def test_overlong_bytes_preserve_ignored_tail(self):
        for row in bs.SIGNALS:
            for length in (9, 64, 65, 128):
                data = bytes(range(length))
                if row.bus == "c-can":
                    self.check_observations(row.can_id, data)
                else:
                    self.assertEqual(row.decode(data), legacy_b_can_voltage(data))


if __name__ == "__main__":
    unittest.main()
