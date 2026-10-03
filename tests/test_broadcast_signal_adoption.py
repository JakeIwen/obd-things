"""Offline differential tests at consumers of the shared broadcast table."""

import random
import socket
import unittest
from types import SimpleNamespace
from unittest import mock

from lib import can_wake


def legacy_wake_conflicts(frames):
    ignition_seen = any(can_id == 0x2EF for can_id, _data in frames)
    if ignition_seen:
        return ("verified C-CAN ignition-on gate 0x2EF is present",)
    for can_id, data in frames:
        if can_id != 0x0FC or len(data) < 2:
            continue
        rpm = (int.from_bytes(data[:2], "big") & 0xFFFC) / 4.0
        if rpm >= 400.0:
            return (f"verified C-CAN engine speed is {rpm:.0f} rpm",)
    return ()


def legacy_engine_running(frames):
    rpms = [
        (int.from_bytes(data[:2], "big") & 0xFFFC) / 4.0
        for _can_id, data in frames
        if len(data) >= 2
    ]
    if not rpms:
        return None
    return max(rpms) >= 400.0


def rpm_payloads():
    for raw in range(65536):
        yield raw.to_bytes(2, "big")
    rng = random.Random(0x0FC)
    for length in range(9):
        for _ in range(256):
            yield rng.randbytes(length)


class WakeBroadcastAdoptionTests(unittest.TestCase):
    def test_both_wake_sites_exhaustive_and_short_frames(self):
        route = SimpleNamespace(channel="offline", role="c-can")
        session = can_wake._WakeSession(
            can_wake._PROFILES["c-can"], SimpleNamespace(route=route), lambda: ()
        )
        frames = []
        with (
            mock.patch.object(socket, "socket", side_effect=AssertionError("no CAN I/O")),
            mock.patch.object(can_wake, "_recv_standard_frames", new=lambda *args: frames),
            mock.patch.object(session, "_ensure_active", new=lambda: None),
        ):
            for data in rpm_payloads():
                frames[:] = [(0x0FC, data)]
                self.assertEqual(can_wake._c_can_safety_conflicts(route), legacy_wake_conflicts(frames))
                self.assertIs(session.engine_running(), legacy_engine_running(frames))
            for special in (
                [], [(0x2EF, b"")], [(0x123, b"\xff\xff")],
                [(0x0FC, b"\0\0"), (0x0FC, b"\xff\xff")],
                [(0x0FC, b"\xff\xff"), (0x2EF, b"")],
            ):
                frames[:] = special
                self.assertEqual(can_wake._c_can_safety_conflicts(route), legacy_wake_conflicts(frames))
                self.assertIs(session.engine_running(), legacy_engine_running(frames))

    def test_wake_read_exceptions_are_unchanged(self):
        route = SimpleNamespace(channel="offline", role="c-can")
        session = can_wake._WakeSession(
            can_wake._PROFILES["c-can"], SimpleNamespace(route=route), lambda: ()
        )
        with (
            mock.patch.object(socket, "socket", side_effect=AssertionError("no CAN I/O")),
            mock.patch.object(can_wake, "_recv_standard_frames", side_effect=OSError("offline")),
            mock.patch.object(session, "_ensure_active", new=lambda: None),
        ):
            self.assertEqual(can_wake._c_can_safety_conflicts(route), ("could not verify C-CAN parked state: offline",))
            with self.assertRaises(can_wake.CanWakeError) as raised:
                session.engine_running()
            self.assertEqual(raised.exception.reason, "source_unavailable")
            self.assertEqual(str(raised.exception), "could not read the fixed C-CAN engine-speed witness: offline")

    def test_wake_identifiers_remain_literal_baseline_values(self):
        self.assertEqual(can_wake._B_CAN_VOLTAGE_ID, 0x46C)
        self.assertEqual(can_wake._C_CAN_ENGINE_SPEED_ID, 0x0FC)
        self.assertEqual(can_wake._C_CAN_IGNITION_GATE_ID, 0x2EF)


if __name__ == "__main__":
    unittest.main()
