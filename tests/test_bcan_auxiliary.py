import io
import json
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from lib import canbus
from projects.vehicle_data import bcan_auxiliary


def can_frame(can_id, payload):
    return struct.pack(
        bcan_auxiliary.CAN_FRAME_FORMAT,
        can_id,
        len(payload),
        payload.ljust(8, b"\x00"),
    )


class FakeSocket:
    def __init__(self, response):
        self.response = response
        self.sent = None
        self.closed = False
        self.bound = None
        self.options = []

    def setsockopt(self, *args):
        self.options.append(args)

    def settimeout(self, _value):
        pass

    def setblocking(self, _value):
        pass

    def bind(self, value):
        self.bound = value

    def send(self, value):
        self.sent = value
        return len(value)

    def recv(self, _size):
        if self.sent is None:
            raise BlockingIOError
        response, self.response = self.response, None
        if response is None:
            raise TimeoutError
        return response

    def close(self):
        self.closed = True


def interface(listen_only):
    return canbus.InterfaceState(
        channel="can8",
        present=True,
        up=True,
        bitrate=125000,
        listen_only=listen_only,
        controller_state="ERROR-ACTIVE",
        restart_ms=0,
        fd_enabled=False,
        one_shot=False,
    )


class FakePoller:
    def __init__(self):
        self.closed = False
        self.polls = 0

    def poll(self):
        self.polls += 1
        return bcan_auxiliary.OdometerResult(
            True,
            value=53191.86,
            detail="fixed replay",
        )

    def close(self):
        self.closed = True


class FakeBackend:
    channel = "can8"

    def __init__(self):
        self.armed = False
        self.restored = False
        self.poller = FakePoller()
        self.now = 0.0
        self.sleeps = 0

    def identity_matches(self):
        return True

    def interface_state(self):
        return interface(not self.armed)

    def topology(self):
        return SimpleNamespace(usable=True, bus="b-can", pair="3/11")

    def inhibits(self):
        return ()

    def identify_bus(self):
        return "b-can"

    def arm(self, _initial):
        self.armed = True
        return True

    def restore(self, _initial):
        self.armed = False
        self.restored = True
        return True

    def open_poller(self):
        return self.poller

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.sleeps > 1:
            raise KeyboardInterrupt


class BcanAuxiliaryTests(unittest.TestCase):
    def test_raw_poller_sends_only_fixed_single_frame_and_decodes_candidate(self):
        response = can_frame(
            bcan_auxiliary.ICS.rxid | bcan_auxiliary.CAN_EFF_FLAG,
            b"\x06\x62\x20\x01\x0d\x0f\xe8",
        )
        sock = FakeSocket(response)
        poller = bcan_auxiliary.IcsOdometerPoller(
            "can8", socket_factory=lambda *_args: sock
        )
        poller.open()
        result = poller.poll()
        poller.close()

        self.assertTrue(result.available)
        self.assertAlmostEqual(result.value, 53191.86, places=3)
        can_id, dlc, payload = struct.unpack(
            bcan_auxiliary.CAN_FRAME_FORMAT, sock.sent
        )
        self.assertEqual(can_id, bcan_auxiliary.ICS.txid | bcan_auxiliary.CAN_EFF_FLAG)
        self.assertEqual(dlc, 4)
        self.assertEqual(payload[:4], b"\x03\x22\x20\x01")
        self.assertEqual(payload[4:], b"\x00" * 4)
        self.assertTrue(sock.closed)

    def test_raw_decoder_rejects_wrong_echo_padding_and_session_nrc(self):
        wrong = can_frame(
            bcan_auxiliary.ICS.rxid | bcan_auxiliary.CAN_EFF_FLAG,
            b"\x06\x62\x20\x02\x00\x00\x01",
        )
        self.assertEqual(
            bcan_auxiliary.IcsOdometerPoller._decode(wrong).reason,
            "malformed_response",
        )
        padded = struct.pack(
            bcan_auxiliary.CAN_FRAME_FORMAT,
            bcan_auxiliary.ICS.rxid | bcan_auxiliary.CAN_EFF_FLAG,
            8,
            b"\x06\x62\x20\x01\x00\x00\x01\xff",
        )
        self.assertEqual(
            bcan_auxiliary.IcsOdometerPoller._decode(padded).reason,
            "malformed_response",
        )
        negative = can_frame(
            bcan_auxiliary.ICS.rxid | bcan_auxiliary.CAN_EFF_FLAG,
            b"\x03\x7f\x22\x7e",
        )
        self.assertEqual(
            bcan_auxiliary.IcsOdometerPoller._decode(negative).reason,
            "session_required",
        )

    def test_session_emits_candidate_observation_and_restores(self):
        backend = FakeBackend()
        stream = io.StringIO()
        sink = bcan_auxiliary.JsonEventSink(stream)
        lock = object()
        with (
            mock.patch.object(
                bcan_auxiliary.diagnostic_safety,
                "acquire_channel_lock",
                return_value=lock,
            ),
            mock.patch.object(
                bcan_auxiliary.diagnostic_safety,
                "release_channel_lock",
            ) as release,
        ):
            outcome = bcan_auxiliary.run_active_session(backend, sink)

        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        observation = next(item for item in events if item["type"] == "observation")
        self.assertEqual(observation["metric"], "vehicle.odometer")
        self.assertEqual(observation["source"], "ics.did.2001")
        self.assertEqual(observation["quality"], "candidate")
        self.assertTrue(outcome.restored)
        self.assertTrue(backend.restored)
        self.assertTrue(backend.poller.closed)
        release.assert_called_once_with(lock)
        self.assertEqual(events[-1]["type"], "final")
        self.assertTrue(events[-1]["restored"])

    def test_startup_signature_retry_is_bounded_and_signature_only(self):
        backend = FakeBackend()
        backend.identify_bus = mock.Mock(
            side_effect=("unknown", "unknown", "b-can")
        )
        backend.sleep = mock.Mock()

        blocked = bcan_auxiliary._startup_gate(
            backend,
            interface(True),
            active=False,
        )

        self.assertIsNone(blocked)
        self.assertEqual(backend.identify_bus.call_count, 3)
        self.assertEqual(backend.sleep.call_count, 2)
        backend.sleep.assert_called_with(
            bcan_auxiliary.STARTUP_SIGNATURE_RETRY_SECONDS
        )

        unhealthy = FakeBackend()
        unhealthy.identity_matches = mock.Mock(return_value=False)
        unhealthy.identify_bus = mock.Mock()
        unhealthy.sleep = mock.Mock()
        blocked = bcan_auxiliary._startup_gate(
            unhealthy,
            interface(True),
            active=False,
        )
        self.assertEqual(blocked.reason, "adapter_unhealthy")
        unhealthy.identify_bus.assert_not_called()
        unhealthy.sleep.assert_not_called()

    def test_startup_signature_retry_exhaustion_remains_wrong_bus(self):
        backend = FakeBackend()
        backend.identify_bus = mock.Mock(return_value="unknown")
        backend.sleep = mock.Mock()

        blocked = bcan_auxiliary._startup_gate(
            backend,
            interface(True),
            active=False,
        )

        self.assertEqual(blocked.reason, "wrong_bus")
        self.assertEqual(
            backend.identify_bus.call_count,
            bcan_auxiliary.STARTUP_SIGNATURE_ATTEMPTS,
        )
        self.assertEqual(
            backend.sleep.call_count,
            bcan_auxiliary.STARTUP_SIGNATURE_ATTEMPTS - 1,
        )

    def test_cli_exposes_no_did_payload_session_or_cadence_option(self):
        destinations = {action.dest for action in bcan_auxiliary.build_parser()._actions}
        self.assertEqual(
            destinations,
            {
                "help",
                "channel",
                "expected_usb_serial",
                "expected_dev_id",
                "expected_parent_pid",
            },
        )


if __name__ == "__main__":
    unittest.main()
