"""Offline differential tests at consumers of the shared broadcast table."""

import random
import socket
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from lib import can_wake
from projects.ecu_mapping import rke_front_unlock as rke
from projects.tpms import tpms_logger


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


class ReceiveOnlySocket:
    def __init__(self, frames):
        self.frames = iter(frames)

    def recv(self, _size):
        try:
            return next(self.frames)
        except StopIteration:
            raise socket.timeout

    def setsockopt(self, *_args):
        pass

    def bind(self, _address):
        pass

    def settimeout(self, _timeout):
        pass

    def close(self):
        pass

    def send(self, _data):
        raise AssertionError("differential test must never transmit")


def legacy_tpms_running(frames):
    consecutive = 0
    for frame in frames:
        if len(frame) != 16:
            return False
        can_id, dlc, data = struct.unpack("=IB3x8s", frame)
        if (
            can_id & (0x80000000 | 0x40000000 | 0x20000000)
            or (can_id & 0x7FF) != 0x0FC
            or not 2 <= dlc <= 8
        ):
            continue
        rpm = (int.from_bytes(data[:2], "big") & 0xFFFC) / 4.0
        if rpm >= 400.0:
            consecutive += 1
            if consecutive >= 3:
                return True
        else:
            consecutive = 0
    return False


class TpmsBroadcastAdoptionTests(unittest.TestCase):
    def check_frames(self, frames):
        sock = ReceiveOnlySocket(frames)
        actual = tpms_logger.engine_running(
            "offline", socket_factory=lambda *_args: sock, monotonic=lambda: 0.0
        )
        self.assertIs(actual, legacy_tpms_running(frames))

    def test_tpms_rpm_exhaustive_and_every_short_dlc(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no CAN I/O")):
            for data in rpm_payloads():
                frame = struct.pack("=IB3x8s", 0x0FC, len(data), data)
                self.check_frames([frame] * 3)
            for can_id in (0x0FC, 0x2EF, 0x800000FC, 0x400000FC, 0x200000FC):
                for dlc in range(256):
                    self.check_frames([struct.pack("=IB3x8s", can_id, dlc, b"\xff" * 8)] * 3)
            running = struct.pack("=IB3x8s", 0x0FC, 2, b"\xff\xff")
            stopped = struct.pack("=IB3x8s", 0x0FC, 2, b"\0\0")
            self.check_frames([running, running, stopped, running, running])
            self.check_frames([running, running, stopped, running, running, running])
            self.check_frames([b"short"])
        self.assertEqual(tpms_logger.ENGINE_SPEED_ID, 0x0FC)
        self.assertEqual(tpms_logger.IGN_BCAST, 0x2EF)


def legacy_rke_rpm_rejection(can_id, data):
    if can_id == 0x2EF:
        raise rke.ReplayError("ignition witness 0x2EF appeared during synchronization")
    if can_id == 0x0FC and len(data) >= 2:
        rpm = (int.from_bytes(data[:2], "big") & 0xFFFC) / 4.0
        if rpm >= 400.0:
            raise rke.ReplayError(f"engine speed became {rpm:.0f} rpm")
    raise rke.ReplayError("no three-frame CRC-valid sequential 0x1EF streak before timeout")


class RkeBroadcastAdoptionTests(unittest.TestCase):
    def check_rejection(self, can_id, data):
        sock = ReceiveOnlySocket([struct.pack("=IB3x8s", can_id, len(data), data)])
        with self.assertRaises(rke.ReplayError) as expected:
            legacy_rke_rpm_rejection(can_id, data)
        with self.assertRaises(rke.ReplayError) as actual:
            rke.synchronize_and_send(sock, clock=lambda: 0.0)
        self.assertIs(type(actual.exception), type(expected.exception))
        self.assertEqual(str(actual.exception), str(expected.exception))

    def test_rke_rpm_exhaustive_and_every_short_frame(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no CAN I/O")):
            for data in rpm_payloads():
                self.check_rejection(0x0FC, data)
            for length in range(9):
                self.check_rejection(0x2EF, bytes(length))
                self.check_rejection(0x123, bytes(length))
        self.assertEqual(rke.IGNITION_ID, 0x2EF)
        self.assertEqual(rke.RPM_ID, 0x0FC)
        self.assertEqual(rke.B_CAN_ACCESS_IDS, (0x46C, 0x5B2, 0x5E2))


if __name__ == "__main__":
    unittest.main()
