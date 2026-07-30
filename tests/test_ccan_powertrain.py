import socket
import struct
import unittest
from itertools import count

from projects.vehicle_data import ccan_powertrain


class FakeSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False
        self.filters = None
        self.channel = None

    def setsockopt(self, _level, _option, value):
        self.filters = value

    def bind(self, address):
        self.channel = address

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        if not self.frames:
            raise socket.timeout
        return self.frames.pop(0)

    def close(self):
        self.closed = True


def frame(can_id, data):
    payload = bytes(data)
    return struct.pack("=IB3x8s", can_id, len(payload), payload.ljust(8, b"\0"))


class DecodeTests(unittest.TestCase):
    def test_fixed_decodes(self):
        battery = ccan_powertrain.decode_frame(0x41A, b"\xbe")
        self.assertEqual(battery.metric, "battery.voltage")
        self.assertEqual(battery.value, 13.5)
        self.assertEqual(battery.quality, "verified")

        oil = ccan_powertrain.decode_frame(0x41D, b"\x00\x00\x36")
        self.assertEqual(oil.metric, "engine.oil_pressure")
        self.assertAlmostEqual(oil.value, 31.328151349725194)
        self.assertEqual(oil.unit, "psi")

        coolant = ccan_powertrain.decode_frame(0x2ED, b"\x7e")
        self.assertEqual(coolant.metric, "engine.coolant_temperature")
        self.assertAlmostEqual(coolant.value, 186.8)
        self.assertEqual(coolant.unit, "°F")

        rpm = ccan_powertrain.decode_frame(0x0FC, b"\x2f\xa3")
        self.assertEqual(rpm.metric, "engine.rpm")
        self.assertEqual(rpm.value, 3048.0)
        self.assertEqual(rpm.unit, "rpm")

        target = ccan_powertrain.decode_frame(
            0x100, bytes.fromhex("4F B9 F4 3E E0 00 0F CF")
        )
        self.assertEqual(target.metric, "engine.target_crankshaft_torque")
        self.assertAlmostEqual(target.value, 3.0 * ccan_powertrain.NM_TO_LB_FT)
        self.assertEqual(target.unit, "lb-ft")

        speed = ccan_powertrain.decode_frame(
            0x101, bytes.fromhex("00 60 00 00 00 00 00 00")
        )
        self.assertEqual(speed.metric, "vehicle.speed")
        self.assertAlmostEqual(
            speed.value, 48.0 * ccan_powertrain.KMH_TO_MPH
        )
        self.assertEqual(speed.unit, "mph")

        shaft_speeds = ccan_powertrain.decode_frame_observations(
            0x1F7, bytes.fromhex("00 2D 10 00 05 B6 00 00")
        )
        self.assertEqual(
            [item.metric for item in shaft_speeds],
            [
                "transmission.output_speed",
                "transmission.oil_temperature",
                "transmission.turbine_speed",
            ],
        )
        self.assertEqual(shaft_speeds[0].value, 360.5)
        self.assertAlmostEqual(shaft_speeds[1].value, 134.6)
        self.assertEqual(shaft_speeds[1].unit, "°F")
        self.assertEqual(shaft_speeds[2].value, 731.0)

        wrapped_output = ccan_powertrain.decode_frame_observations(
            0x1F7, bytes.fromhex("01 6F A1 38 12 90 03 D0")
        )
        self.assertAlmostEqual(
            wrapped_output[0].value,
            ((1 << 16) | 0x6FA1) / 32.0,
        )
        self.assertAlmostEqual(wrapped_output[1].value, 172.4)
        self.assertEqual(wrapped_output[2].value, 2376.0)

        ignition = ccan_powertrain.decode_frame(0x2EF, b"\xff\x21")
        self.assertIs(ignition.value, True)
        self.assertIsNone(ccan_powertrain.decode_frame(0x123, b"\x00"))

    def test_short_payloads_are_rejected(self):
        self.assertIsNone(ccan_powertrain.decode_frame(0x41D, b"\x00\x00"))
        self.assertIsNone(ccan_powertrain.decode_frame(0x2ED, b""))
        self.assertIsNone(ccan_powertrain.decode_frame(0x0FC, b"\x00"))
        self.assertIsNone(ccan_powertrain.decode_frame(0x100, b"\x00" * 4))
        self.assertIsNone(ccan_powertrain.decode_frame(0x101, b"\x00" * 2))
        self.assertIsNone(ccan_powertrain.decode_frame(0x1F7, b"\x00" * 5))

    def test_snapshot_rejects_extended_frame_with_same_low_identifier(self):
        fake = FakeSocket(
            [
                frame(
                    ccan_powertrain.CAN_EFF_FLAG | 0x41D,
                    b"\x00\x00\x36",
                ),
            ]
        )
        ticks = count(start=0.0, step=0.1)
        observations = ccan_powertrain.read_snapshot(
            timeout=0.5,
            socket_factory=lambda *_args: fake,
            monotonic=lambda: next(ticks),
        )
        self.assertEqual(observations, ())

    def test_kernel_filters_do_not_use_can_error_flag(self):
        fake = FakeSocket([])
        ticks = count(start=0.0, step=0.1)
        ccan_powertrain.read_snapshot(
            timeout=0.5,
            socket_factory=lambda *_args: fake,
            monotonic=lambda: next(ticks),
        )

        entries = [
            struct.unpack("=II", fake.filters[offset : offset + 8])
            for offset in range(0, len(fake.filters), 8)
        ]
        self.assertEqual(
            [can_id for can_id, _mask in entries],
            list(ccan_powertrain.FILTER_IDS),
        )
        for _can_id, mask in entries:
            self.assertEqual(mask & ccan_powertrain.SFF_MASK, 0x7FF)
            self.assertTrue(mask & ccan_powertrain.CAN_EFF_FLAG)
            self.assertTrue(mask & ccan_powertrain.CAN_RTR_FLAG)
            self.assertFalse(mask & ccan_powertrain.CAN_ERR_FLAG)

    def test_active_snapshot_filter_explicitly_includes_system_voltage(self):
        fake = FakeSocket([])
        ticks = count(start=0.0, step=0.1)
        ccan_powertrain.read_broadcast_snapshot(
            timeout=0.5,
            include_battery=True,
            socket_factory=lambda *_args: fake,
            monotonic=lambda: next(ticks),
        )

        entries = [
            struct.unpack("=II", fake.filters[offset : offset + 8])
            for offset in range(0, len(fake.filters), 8)
        ]
        self.assertEqual(
            [can_id for can_id, _mask in entries],
            list(ccan_powertrain.ACTIVE_FILTER_IDS),
        )
        self.assertIn(ccan_powertrain.SYSTEM_VOLTAGE_ID, {
            can_id for can_id, _mask in entries
        })

    def test_snapshot_rejects_malformed_raw_frame_and_classic_dlc(self):
        cases = (
            b"\0" * 15,
            struct.pack("=IB3x8s", 0x0FC, 9, b"\0" * 8),
        )
        for raw_frame in cases:
            with self.subTest(length=len(raw_frame)):
                fake = FakeSocket([raw_frame])
                ticks = count(start=0.0, step=0.1)
                with self.assertRaises(RuntimeError):
                    ccan_powertrain.read_broadcast_snapshot(
                        timeout=0.5,
                        socket_factory=lambda *_args: fake,
                        monotonic=lambda: next(ticks),
                    )
                self.assertTrue(fake.closed)

    def test_snapshot_uses_median_and_closes_socket(self):
        fake = FakeSocket(
            [
                frame(0x41D, b"\x00\x00\x34"),
                frame(0x41D, b"\x00\x00\x36"),
                frame(0x41D, b"\x00\x00\x35"),
                frame(0x2ED, b"\x7e"),
                frame(0x0FC, b"\x0b\xb8"),
                frame(0x100, bytes.fromhex("4F B9 F4 3E E0 00 0F CF")),
                frame(0x101, bytes.fromhex("00 60 00 00 00 00 00 00")),
                frame(0x1F7, bytes.fromhex("00 2D 10 00 05 B6 00 00")),
                frame(0x2EF, b"\xff\x21"),
            ]
        )
        ticks = count(start=0.0, step=0.01)
        observations = ccan_powertrain.read_snapshot(
            timeout=1.0,
            socket_factory=lambda *_args: fake,
            monotonic=lambda: next(ticks),
        )
        by_metric = {item.metric: item for item in observations}

        self.assertAlmostEqual(
            by_metric["engine.oil_pressure"].value,
            30.748000398804357,
        )
        self.assertAlmostEqual(
            by_metric["engine.coolant_temperature"].value,
            186.8,
        )
        self.assertEqual(by_metric["engine.rpm"].value, 750.0)
        self.assertAlmostEqual(
            by_metric["engine.target_crankshaft_torque"].value,
            3.0 * ccan_powertrain.NM_TO_LB_FT,
        )
        self.assertAlmostEqual(
            by_metric["vehicle.speed"].value,
            48.0 * ccan_powertrain.KMH_TO_MPH,
        )
        self.assertEqual(by_metric["transmission.output_speed"].value, 360.5)
        self.assertAlmostEqual(
            by_metric["transmission.oil_temperature"].value,
            134.6,
        )
        self.assertEqual(by_metric["transmission.turbine_speed"].value, 731.0)
        self.assertIs(by_metric["vehicle.ignition_on"].value, True)
        self.assertEqual(fake.channel, ("can0",))
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
