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
        oil = ccan_powertrain.decode_frame(0x41D, b"\x00\x00\x36")
        self.assertEqual(oil.metric, "engine.oil_pressure")
        self.assertAlmostEqual(oil.value, 31.328151349725194)
        self.assertEqual(oil.unit, "psi")

        coolant = ccan_powertrain.decode_frame(0x2ED, b"\x7e")
        self.assertEqual(coolant.metric, "engine.coolant_temperature")
        self.assertAlmostEqual(coolant.value, 186.8)
        self.assertEqual(coolant.unit, "°F")

        ignition = ccan_powertrain.decode_frame(0x2EF, b"\xff\x21")
        self.assertIs(ignition.value, True)
        self.assertIsNone(ccan_powertrain.decode_frame(0x123, b"\x00"))

    def test_short_payloads_are_rejected(self):
        self.assertIsNone(ccan_powertrain.decode_frame(0x41D, b"\x00\x00"))
        self.assertIsNone(ccan_powertrain.decode_frame(0x2ED, b""))

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

    def test_snapshot_uses_median_and_closes_socket(self):
        fake = FakeSocket(
            [
                frame(0x41D, b"\x00\x00\x34"),
                frame(0x41D, b"\x00\x00\x36"),
                frame(0x41D, b"\x00\x00\x35"),
                frame(0x2ED, b"\x7e"),
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
        self.assertIs(by_metric["vehicle.ignition_on"].value, True)
        self.assertEqual(fake.channel, ("can0",))
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
