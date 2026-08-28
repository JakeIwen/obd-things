from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
import socket
import struct
import unittest
from unittest import mock

from lib import can_wake, canbus


class FakeOwnership:
    def __init__(self, role="b-can", *, release_result=True, events=None):
        self.route = SimpleNamespace(
            role=role,
            channel="can2" if role == "b-can" else "can7",
            bitrate=125_000 if role == "b-can" else 500_000,
            pair="3/11" if role == "b-can" else "6/14",
        )
        self.manager = object()
        self.release_result = release_result
        self.events = events if events is not None else []

    def release(self):
        self.events.append("release")
        return self.release_result


class FakeRawSocket:
    def __init__(self, responses=()):
        self.bound = None
        self.sent = []
        self.closed = False
        self.responses = list(responses)
        self.filters = []
        self.timeout = None

    def setsockopt(self, level, option, value):
        self.filters.append((level, option, value))

    def settimeout(self, timeout):
        self.timeout = timeout

    def bind(self, address):
        self.bound = address

    def send(self, frame):
        self.sent.append(frame)
        return len(frame)

    def recv(self, _size):
        if not self.responses:
            raise socket.timeout
        response = self.responses.pop(0)
        if response is None:
            raise socket.timeout
        return response

    def close(self):
        self.closed = True


class CanWakeTests(unittest.TestCase):
    def test_only_exact_b_and_c_logical_roles_exist(self):
        for role in ("can-ch", "can0", "ccan", None):
            with self.subTest(role=role), self.assertRaises(
                can_wake.CanWakeError
            ) as raised:
                can_wake.wake_once(role, prearm_check=lambda: ())
            self.assertEqual(raised.exception.reason, "unsupported_role")

    def test_wake_once_hides_physical_profile_and_restores_after_cleanup_guard(self):
        events = []
        ownership = FakeOwnership(events=events)

        class Guard:
            def begin_cleanup(self):
                events.append("begin_cleanup")

        @contextmanager
        def guard_context():
            yield Guard()

        @contextmanager
        def handoff_context(_role):
            events.append("handoff_enter")
            try:
                yield object()
            finally:
                events.append("handoff_exit")

        expected = can_wake.WakeResult(
            role="b-can", source="bcan.network_wake.0x7ff", detail="ok", voltage=12.5
        )
        with (
            mock.patch.object(
                can_wake.diagnostic_safety,
                "interrupt_on_termination",
                side_effect=guard_context,
            ),
            mock.patch.object(
                can_wake.can_runtime_route,
                "acquire_armed_bus_route",
                return_value=ownership,
            ) as acquire,
            mock.patch.object(
                can_wake.can_handoff,
                "active_turn",
                side_effect=handoff_context,
            ),
            mock.patch.object(
                can_wake._WakeSession,
                "trigger",
                side_effect=lambda _self=None: events.append("trigger") or expected,
                autospec=True,
            ),
        ):
            result = can_wake.wake_once(
                "b-can", prearm_check=lambda: (), manager="manager"
            )

        self.assertIs(result, expected)
        self.assertEqual(
            events,
            [
                "handoff_enter",
                "trigger",
                "begin_cleanup",
                "release",
                "handoff_exit",
            ],
        )
        acquire.assert_called_once()
        kwargs = acquire.call_args.kwargs
        self.assertEqual(acquire.call_args.args, ("b-can",))
        self.assertEqual(kwargs["asserted_pair"], "3/11")
        self.assertIs(kwargs["one_shot"], False)
        self.assertEqual(kwargs["wake_probe_policy"], "silent")
        self.assertNotIn("channel", kwargs)
        self.assertNotIn("bitrate", kwargs)
        self.assertNotIn("restart_ms", kwargs)

    def test_restoration_failure_overrides_a_successful_trigger(self):
        ownership = FakeOwnership(release_result=False)
        expected = can_wake.WakeResult(
            role="b-can", source="bcan.network_wake.0x7ff", detail="ok", voltage=12.5
        )
        with (
            mock.patch.object(
                can_wake.can_runtime_route,
                "acquire_armed_bus_route",
                return_value=ownership,
            ),
            mock.patch.object(
                can_wake._WakeSession, "trigger", return_value=expected
            ),
            mock.patch.object(
                can_wake.can_handoff,
                "active_turn",
                side_effect=lambda _role: nullcontext(object()),
            ),
            self.assertRaises(canbus.PassiveRestoreError),
        ):
            can_wake.wake_once("b-can", prearm_check=lambda: ())

    def test_busy_fairness_handoff_fails_before_role_ownership(self):
        handoff = mock.MagicMock()
        handoff.__enter__.side_effect = can_wake.diagnostic_safety.ChannelLockError(
            "shared turn held"
        )
        with (
            mock.patch.object(
                can_wake.can_handoff,
                "active_turn",
                return_value=handoff,
            ),
            mock.patch.object(
                can_wake.can_runtime_route,
                "acquire_armed_bus_route",
            ) as acquire,
            self.assertRaises(can_wake.CanWakeError) as raised,
        ):
            can_wake.wake_once("c-can", prearm_check=lambda: ())

        self.assertEqual(raised.exception.reason, "handoff_busy")
        acquire.assert_not_called()

    def test_c_profile_rejects_ignition_and_running_witnesses(self):
        route = SimpleNamespace(channel="can7")
        cases = (
            ([(0x2EF, b"\x00")], "0x2EF"),
            ([(0x0FC, (800 * 4).to_bytes(2, "big"))], "800 rpm"),
        )
        for frames, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(
                can_wake, "_recv_standard_frames", return_value=frames
            ):
                conflicts = can_wake._c_can_safety_conflicts(route)
            self.assertIn(expected, conflicts[0])

        with mock.patch.object(
            can_wake,
            "_recv_standard_frames",
            return_value=[(0x0FC, b"\x00\x00")],
        ):
            self.assertEqual(can_wake._c_can_safety_conflicts(route), ())

    def test_c_session_rearms_normal_retry_only_under_held_ownership(self):
        ownership = FakeOwnership("c-can")
        session = can_wake._WakeSession(
            can_wake._PROFILES["c-can"], ownership, lambda: ()
        )
        with (
            mock.patch.object(session, "_ensure_active") as ensure,
            mock.patch.object(can_wake.canbus, "ip_up", return_value=True) as ip_up,
        ):
            session._rearm_one_shot(False)

        self.assertEqual(ensure.call_count, 2)
        self.assertFalse(session._active_one_shot)
        ip_up.assert_called_once_with(
            "can7",
            500_000,
            listen_only=False,
            restart_ms=0,
            one_shot=False,
            noninteractive=True,
        )

    def test_b_profile_attempts_exact_burst_and_requires_verified_voltage(self):
        ownership = FakeOwnership()
        session = can_wake._WakeSession(
            can_wake._PROFILES["b-can"], ownership, lambda: ()
        )
        raw_socket = FakeRawSocket()
        voltage_data = bytes((0, 0, 0, 0, 0x13, 0x88, 0, 0))
        with (
            mock.patch.object(session, "_ensure_active"),
            mock.patch.object(can_wake.socket, "socket", return_value=raw_socket),
            mock.patch.object(can_wake.time, "sleep"),
            mock.patch.object(can_wake.canbus, "identify_bus", return_value="b-can"),
            mock.patch.object(
                can_wake,
                "_recv_standard_frames",
                return_value=[(0x46C, voltage_data)] * 3,
            ),
        ):
            result = session.trigger()

        self.assertEqual(len(raw_socket.sent), 75)
        self.assertEqual(raw_socket.bound, ("can2",))
        self.assertTrue(raw_socket.closed)
        self.assertEqual(result.voltage, 12.5)
        self.assertEqual(result.role, "b-can")

    def test_c_wake_uses_one_shot_single_frame_without_private_payload(self):
        ownership = FakeOwnership("c-can")
        session = can_wake._WakeSession(
            can_wake._PROFILES["c-can"], ownership, lambda: ()
        )
        response = struct.pack(
            "=IB3x8s",
            can_wake.CAN_EFF_FLAG | can_wake._C_CAN_RFH_RXID,
            8,
            bytes.fromhex("07 62 FE FF 33 43 36 4C"),
        )
        wake_transport = FakeRawSocket()
        validation_transport = FakeRawSocket((None, response))
        with (
            mock.patch.object(session, "_ensure_active"),
            mock.patch.object(session, "_rearm_one_shot") as rearm,
            mock.patch.object(
                can_wake.socket,
                "socket",
                side_effect=(wake_transport, validation_transport),
            ),
            mock.patch.object(
                can_wake.canbus,
                "identify_bus",
                side_effect=("silent", "c-can", "c-can"),
            ),
            mock.patch.object(can_wake.time, "sleep"),
        ):
            result = session.trigger()

        self.assertEqual(len(wake_transport.sent), 10)
        self.assertEqual(len(validation_transport.sent), 1)
        can_id, dlc, data = struct.unpack("=IB3x8s", wake_transport.sent[0])
        self.assertEqual(can_id, can_wake.CAN_EFF_FLAG | can_wake._C_CAN_RFH_TXID)
        self.assertEqual(dlc, 8)
        self.assertEqual(data, can_wake._C_CAN_WAKE_REQUEST_DATA)
        self.assertNotIn("33 43 36 4C", result.detail)
        self.assertIn("positive DID echo", result.detail)
        self.assertTrue(session._profile.one_shot)
        rearm.assert_called_once_with(False)
        self.assertTrue(wake_transport.closed)
        self.assertTrue(validation_transport.closed)


if __name__ == "__main__":
    unittest.main()
