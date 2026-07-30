import dataclasses
import inspect
import socket
import struct
import unittest

from lib.modules import MODULES
from projects.vehicle_data import ccan_powertrain
from projects.vehicle_data import pcm_electrical as pcm
from projects.vehicle_data import transmit_permit


def response_frame(data, *, can_id=0x18DAF110, dlc=None, extended=True):
    payload = bytes(data)
    if dlc is None:
        dlc = len(payload)
    raw_id = can_id | (pcm.CAN_EFF_FLAG if extended else 0)
    return struct.pack(
        pcm.CAN_FRAME_FORMAT,
        raw_id,
        dlc,
        payload.ljust(8, b"\0"),
    )


class FakeSocket:
    def __init__(self, response):
        self.response = response
        self.sent = []
        self.filters = []
        self.bound = None
        self.timeout = None
        self.closed = False
        self.recv_calls = 0

    def setsockopt(self, level, option, value):
        self.filters.append((level, option, value))

    def bind(self, address):
        self.bound = address

    def send(self, frame):
        self.sent.append(bytes(frame))
        return len(frame)

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, _size):
        self.recv_calls += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


class FakeDiagnosticLock:
    closed = False
    _diagnostic_lock_held = True
    _diagnostic_lock_channel = "can0"
    _diagnostic_lock_mode = "exclusive"

    def fileno(self):
        return 98


def authorization(
    *,
    purpose=transmit_permit.PCM_GENERATOR_DUTY,
    clock=lambda: 1.0,
    rpm_samples=(750.0, 751.0, 752.0),
    frame_count=12,
    lock_handle=None,
):
    evidence_at = clock()
    snapshot = ccan_powertrain.BroadcastSnapshot(
        observations=(),
        rpm_samples=tuple(rpm_samples),
        frame_count=frame_count,
        completed_monotonic=evidence_at,
    )
    return transmit_permit.issue(
        lock_handle or FakeDiagnosticLock(),
        snapshot,
        purpose=purpose,
        monotonic=clock,
    )


class PcmElectricalProfileTests(unittest.TestCase):
    def test_registry_has_one_immutable_reviewed_profile(self):
        self.assertEqual(tuple(pcm.PCM_ELECTRICAL_PROFILES), ("generator.field_duty",))
        profile = pcm.PCM_ELECTRICAL_PROFILES["generator.field_duty"]
        module = MODULES["pcm"]

        self.assertIs(profile, pcm.GENERATOR_FIELD_DUTY_PROFILE)
        self.assertEqual(profile.did, 0x01A1)
        self.assertEqual(profile.request_id, module.txid)
        self.assertEqual(profile.response_id, module.rxid)
        self.assertEqual(profile.channel, module.channel)
        self.assertEqual(profile.bitrate, module.bitrate)
        self.assertEqual(profile.bus, module.bus)
        self.assertEqual(profile.source, "pcm.did.01a1")
        self.assertEqual(profile.maximum, 101.0)
        with self.assertRaises(TypeError):
            pcm.PCM_ELECTRICAL_PROFILES["arbitrary"] = profile
        with self.assertRaises(dataclasses.FrozenInstanceError):
            profile.did = 0x1234

    def test_public_poller_has_no_did_payload_id_or_session_argument(self):
        self.assertFalse(hasattr(pcm, "poll_generator_field_duty"))

        constructor = inspect.signature(
            pcm.PcmElectricalPoller
        ).parameters
        self.assertEqual(
            tuple(constructor),
            ("channel", "timeout_seconds", "socket_factory", "monotonic"),
        )
        self.assertEqual(
            tuple(inspect.signature(pcm.PcmElectricalPoller.poll).parameters),
            ("self", "permit"),
        )
        for forbidden in ("did", "payload", "request_id", "response_id", "session"):
            self.assertNotIn(forbidden, constructor)

    def test_poller_rejects_another_channel_before_opening_socket(self):
        socket_calls = []
        with self.assertRaises(ValueError):
            pcm.PcmElectricalPoller(
                channel="can1",
                socket_factory=lambda *_args: socket_calls.append(_args),
            )
        self.assertEqual(socket_calls, [])


class PcmElectricalWireTests(unittest.TestCase):
    def poll(self, response, *, ticks=(10.0, 10.0)):
        fake = FakeSocket(response)
        times = iter(ticks)
        poller = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: fake,
            monotonic=lambda: next(times),
        )
        try:
            result = poller.poll(authorization())
        finally:
            poller.close()
        return result, fake

    def test_exact_literal_request_and_positive_response(self):
        result, fake = self.poll(
            response_frame(bytes.fromhex("05 62 01 A1 80 00 00 00"), dlc=8)
        )

        expected = struct.pack(
            pcm.CAN_FRAME_FORMAT,
            pcm.CAN_EFF_FLAG | 0x18DA10F1,
            8,
            bytes.fromhex("03 22 01 A1 00 00 00 00"),
        )
        self.assertEqual(fake.sent, [expected])
        self.assertEqual(fake.bound, ("can0",))
        self.assertTrue(fake.closed)
        self.assertTrue(result.available)
        self.assertEqual(result.value, 100.0)
        self.assertEqual(result.raw_value, 0x8000)
        self.assertEqual(result.source, "pcm.did.01a1")
        self.assertEqual(
            result.acquisition, "physical_read_data_by_identifier"
        )

        filter_id, filter_mask = struct.unpack("=II", fake.filters[0][2])
        self.assertEqual(filter_id, pcm.CAN_EFF_FLAG | 0x18DAF110)
        self.assertEqual(
            filter_mask,
            pcm.CAN_EFF_FLAG | pcm.CAN_RTR_FLAG | pcm.CAN_EFF_MASK,
        )

    def test_reusable_poller_open_does_not_transmit_and_context_closes(self):
        fake = FakeSocket(
            response_frame(bytes.fromhex("05 62 01 A1 40 00 00 00"), dlc=8)
        )
        times = iter((1.0, 1.0, 2.0, 2.0))
        poller = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: fake,
            monotonic=lambda: next(times),
        )

        with poller as opened:
            self.assertIs(opened, poller)
            self.assertTrue(poller.is_open)
            self.assertEqual(fake.sent, [])
            first = poller.poll(authorization())
            second = poller.poll(authorization())

        self.assertTrue(first.available)
        self.assertTrue(second.available)
        self.assertEqual(fake.bound, ("can0",))
        self.assertEqual(len(fake.sent), 2)
        self.assertFalse(poller.is_open)
        self.assertTrue(fake.closed)

    def test_context_exception_closes_without_transmitting(self):
        fake = FakeSocket(b"")
        poller = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: fake,
        )

        with self.assertRaisesRegex(RuntimeError, "stop active owner"):
            with poller:
                raise RuntimeError("stop active owner")

        self.assertFalse(poller.is_open)
        self.assertTrue(fake.closed)
        self.assertEqual(fake.sent, [])

    def test_termination_during_socket_open_closes_without_transmitting(self):
        fake = FakeSocket(b"")

        def interrupted_bind(_address):
            raise KeyboardInterrupt

        fake.bind = interrupted_bind
        poller = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: fake,
        )

        with self.assertRaises(KeyboardInterrupt):
            poller.open()

        self.assertTrue(fake.closed)
        self.assertFalse(poller.is_open)
        self.assertEqual(fake.sent, [])

    def test_missing_invalid_wrong_and_stale_permits_never_send(self):
        response = response_frame(
            bytes.fromhex("05 62 01 A1 40 00 00 00"),
            dlc=8,
        )

        missing_socket = FakeSocket(response)
        missing = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: missing_socket
        )
        with self.assertRaises(TypeError):
            missing.poll()
        self.assertEqual(missing_socket.sent, [])
        self.assertFalse(missing.is_open)

        invalid_socket = FakeSocket(response)
        invalid = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: invalid_socket
        )
        invalid_result = invalid.poll(object())
        invalid.close()
        self.assertEqual(invalid_result.reason, "response_rejected")
        self.assertEqual(invalid_socket.sent, [])

        wrong_socket = FakeSocket(response)
        wrong = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: wrong_socket
        )
        wrong_result = wrong.poll(
            authorization(purpose=transmit_permit.RF_HUB_PRESSURE)
        )
        wrong.close()
        self.assertEqual(wrong_result.reason, "response_rejected")
        self.assertEqual(wrong_socket.sent, [])

        clock = type("Clock", (), {"value": 1.0})()
        stale_permit = authorization(clock=lambda: clock.value)
        clock.value += transmit_permit.PERMIT_TTL_SECONDS
        stale_socket = FakeSocket(response)
        stale = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: stale_socket
        )
        stale_result = stale.poll(stale_permit)
        stale.close()
        self.assertEqual(stale_result.reason, "response_rejected")
        self.assertEqual(stale_socket.sent, [])

    def test_reused_or_released_lock_permit_never_sends_again(self):
        response = response_frame(
            bytes.fromhex("05 62 01 A1 40 00 00 00"),
            dlc=8,
        )
        fake = FakeSocket(response)
        poller = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: fake,
            monotonic=lambda: 1.0,
        )
        permit = authorization()

        first = poller.poll(permit)
        second = poller.poll(permit)
        poller.close()

        self.assertTrue(first.available)
        self.assertEqual(second.reason, "response_rejected")
        self.assertEqual(len(fake.sent), 1)

        lock_handle = FakeDiagnosticLock()
        released_permit = authorization(lock_handle=lock_handle)
        lock_handle._diagnostic_lock_held = False
        released_socket = FakeSocket(response)
        released = pcm.PcmElectricalPoller(
            socket_factory=lambda *_args: released_socket
        )
        released_result = released.poll(released_permit)
        released.close()
        self.assertEqual(released_result.reason, "response_rejected")
        self.assertEqual(released_socket.sent, [])

    def test_low_rpm_or_no_traffic_cannot_issue_permit(self):
        for rpm_samples, frame_count in (
            ((750.0, 0.0, 752.0), 12),
            ((750.0, 751.0), 12),
            ((750.0, 751.0, 752.0), 0),
            ((750.0, float("nan"), 752.0), 12),
        ):
            with self.subTest(
                rpm_samples=rpm_samples,
                frame_count=frame_count,
            ):
                with self.assertRaises(
                    transmit_permit.TransmitPermitError
                ):
                    authorization(
                        rpm_samples=rpm_samples,
                        frame_count=frame_count,
                    )

        old_snapshot = ccan_powertrain.BroadcastSnapshot(
            observations=(),
            rpm_samples=(750.0, 751.0, 752.0),
            frame_count=12,
            completed_monotonic=1.0,
        )
        with self.assertRaisesRegex(
            transmit_permit.TransmitPermitError,
            "stale",
        ):
            transmit_permit.issue(
                FakeDiagnosticLock(),
                old_snapshot,
                purpose=transmit_permit.RF_HUB_PRESSURE,
                monotonic=lambda: (
                    1.0 + transmit_permit.PERMIT_TTL_SECONDS
                ),
            )

    def test_wrong_service_or_did_echo_is_malformed(self):
        for payload in (
            "05 63 01 A1 80 00 00 00",
            "05 62 01 A2 80 00 00 00",
        ):
            with self.subTest(payload=payload):
                result, fake = self.poll(
                    response_frame(bytes.fromhex(payload), dlc=8)
                )
                self.assertFalse(result.available)
                self.assertEqual(result.reason, "malformed_response")
                self.assertEqual(len(fake.sent), 1)

    def test_wrong_response_identifier_or_addressing_form_is_malformed(self):
        for frame in (
            response_frame(
                bytes.fromhex("05 62 01 A1 80 00 00 00"),
                can_id=0x18DAF111,
                dlc=8,
            ),
            response_frame(
                bytes.fromhex("05 62 01 A1 80 00 00 00"),
                can_id=0x110,
                dlc=8,
                extended=False,
            ),
        ):
            result, _fake = self.poll(frame)
            self.assertEqual(result.reason, "malformed_response")

    def test_truncated_oversized_and_first_frame_are_rejected_without_fc(self):
        cases = {
            "truncated": response_frame(
                bytes.fromhex("05 62 01 A1 80"), dlc=5
            ),
            "oversized": response_frame(
                bytes.fromhex("06 62 01 A1 80 00 FF 00"), dlc=8
            ),
            "first_frame": response_frame(
                bytes.fromhex("10 05 62 01 A1 80 00 00"), dlc=8
            ),
        }
        literal = struct.pack(
            pcm.CAN_FRAME_FORMAT,
            pcm.CAN_EFF_FLAG | 0x18DA10F1,
            8,
            bytes.fromhex("03 22 01 A1 00 00 00 00"),
        )
        for name, frame in cases.items():
            with self.subTest(name=name):
                result, fake = self.poll(frame)
                self.assertEqual(result.reason, "malformed_response")
                # One literal request only: no retry, session traffic,
                # TesterPresent, or ISO-TP FlowControl.
                self.assertEqual(fake.sent, [literal])

    def test_session_negative_is_distinct_from_other_rejection(self):
        for nrc in (0x7E, 0x7F):
            with self.subTest(nrc=nrc):
                result, _fake = self.poll(
                    response_frame(bytes((0x03, 0x7F, 0x22, nrc, 0, 0, 0, 0)), dlc=8)
                )
                self.assertEqual(result.reason, "session_required")

        rejected, _fake = self.poll(
            response_frame(
                bytes.fromhex("03 7F 22 22 00 00 00 00"), dlc=8
            )
        )
        self.assertEqual(rejected.reason, "response_rejected")

    def test_timeout_is_structured_and_socket_is_closed(self):
        result, fake = self.poll(socket.timeout())
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "response_timeout")
        self.assertEqual(len(fake.sent), 1)
        self.assertTrue(fake.closed)

    def test_slight_overshoot_is_not_clamped(self):
        raw = 0x8003
        result, _fake = self.poll(
            response_frame(
                bytes((0x05, 0x62, 0x01, 0xA1, raw >> 8, raw & 0xFF, 0, 0)),
                dlc=8,
            )
        )
        self.assertTrue(result.available)
        self.assertGreater(result.value, 100.0)
        self.assertAlmostEqual(result.value, raw * 100.0 / 32768.0)

    def test_physically_implausible_value_is_rejected(self):
        raw = 0x9000
        result, _fake = self.poll(
            response_frame(
                bytes((0x05, 0x62, 0x01, 0xA1, raw >> 8, raw & 0xFF, 0, 0)),
                dlc=8,
            )
        )
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "response_rejected")
        self.assertIn("implausible", result.detail)


if __name__ == "__main__":
    unittest.main()
