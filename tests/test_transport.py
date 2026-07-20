import unittest
import errno
from contextlib import contextmanager
from unittest import mock

from lib import uds
from lib.modules import Module, NORMAL_11BITS, NORMAL_29BITS, get


class ModuleTests(unittest.TestCase):
    def test_existing_modules_keep_29_bit_500k_defaults(self):
        radar = get("radar_acc")

        self.assertEqual(radar.addressing_mode, NORMAL_29BITS)
        self.assertEqual(radar.bitrate, 500000)

    def test_11_bit_module_accepts_standard_ids_and_bitrate(self):
        module = Module(
            "test_bcm",
            "Unverified test fixture",
            0x760,
            0x768,
            bus="b-can",
            bitrate=125000,
            addressing_mode=NORMAL_11BITS,
        )

        self.assertEqual(module.addressing_mode, NORMAL_11BITS)
        self.assertEqual(module.bitrate, 125000)

    def test_11_bit_module_rejects_extended_id(self):
        with self.assertRaisesRegex(ValueError, "11-bit CAN identifier"):
            Module(
                "bad",
                "Bad fixture",
                0x18DA40F1,
                0x768,
                addressing_mode=NORMAL_11BITS,
            )

    def test_module_rejects_unknown_addressing_mode(self):
        with self.assertRaisesRegex(ValueError, "unsupported addressing_mode"):
            Module("bad", "Bad fixture", 0x700, 0x708, addressing_mode="extended_magic")

    def test_module_rejects_identical_physical_ids(self):
        with self.assertRaisesRegex(ValueError, "must be different"):
            Module("bad", "Bad fixture", 0x7E0, 0x7E0, addressing_mode=NORMAL_11BITS)


class UdsTransportTests(unittest.TestCase):
    def test_open_socket_defaults_to_existing_29_bit_mode(self):
        sock = mock.Mock()
        with (
            mock.patch.object(uds.isotp, "socket", return_value=sock),
            mock.patch.object(uds.isotp, "Address") as address,
        ):
            result = uds.open_socket(0x18DA2AF1, 0x18DAF12A, timeout=0.75)

        self.assertIs(result, sock)
        address.assert_called_once_with(
            uds.isotp.AddressingMode.Normal_29bits,
            txid=0x18DA2AF1,
            rxid=0x18DAF12A,
        )
        sock.bind.assert_called_once_with("can0", address=address.return_value)
        sock.settimeout.assert_called_once_with(0.75)

    def test_open_module_socket_selects_11_bit_mode(self):
        module = Module(
            "test_bcm",
            "Unverified test fixture",
            0x760,
            0x768,
            channel="can9",
            bus="b-can",
            bitrate=125000,
            addressing_mode=NORMAL_11BITS,
        )
        sock = mock.Mock()
        with (
            mock.patch.object(uds.isotp, "socket", return_value=sock),
            mock.patch.object(uds.isotp, "Address") as address,
        ):
            uds.open_module_socket(module, timeout=0.25)

        address.assert_called_once_with(
            uds.isotp.AddressingMode.Normal_11bits,
            txid=0x760,
            rxid=0x768,
        )
        sock.bind.assert_called_once_with("can9", address=address.return_value)

    def test_recover_module_socket_uses_registry_bitrate_and_mode(self):
        module = Module(
            "test_bcm",
            "Unverified test fixture",
            0x760,
            0x768,
            channel="can9",
            bus="b-can",
            bitrate=125000,
            addressing_mode=NORMAL_11BITS,
        )
        reopened = object()
        with (
            mock.patch.object(uds, "bring_up_can", return_value=True) as bring_up,
            mock.patch.object(uds, "open_socket", return_value=reopened) as open_socket,
        ):
            result = uds.recover_module_socket(module, max_wait=1, timeout=0.25)

        self.assertIs(result, reopened)
        bring_up.assert_called_once_with("can9", 125000, lock_handle=mock.ANY)
        open_socket.assert_called_once_with(0x760, 0x768, "can9", 0.25, NORMAL_11BITS)

    def test_bring_up_can_mutates_only_inside_its_channel_lock(self):
        events = []
        handle = object()

        @contextmanager
        def locked(channel):
            events.append(("lock", channel))
            try:
                yield handle
            finally:
                events.append(("unlock", channel))

        def run(command, **_kwargs):
            events.append(("command", tuple(command)))
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(uds.diagnostic_safety, "channel_lock", side_effect=locked),
            mock.patch.object(uds.diagnostic_safety, "validate_channel_lock", return_value=handle),
            mock.patch.object(uds.subprocess, "run", side_effect=run),
            mock.patch.object(uds.time, "sleep"),
        ):
            self.assertTrue(uds.bring_up_can("can9", 125000))

        self.assertEqual(events[0], ("lock", "can9"))
        self.assertEqual(events[-1], ("unlock", "can9"))
        self.assertEqual([event[0] for event in events].count("command"), 3)

    def test_bring_up_can_lock_contention_never_runs_interface_command(self):
        with (
            mock.patch.object(
                uds.diagnostic_safety,
                "channel_lock",
                side_effect=uds.diagnostic_safety.ChannelLockError("busy"),
            ),
            mock.patch.object(uds.subprocess, "run") as run,
        ):
            self.assertFalse(uds.bring_up_can("can9", 500000))

        run.assert_not_called()

    def test_bring_up_can_rejects_invalid_supplied_lock_before_mutation(self):
        with (
            mock.patch.object(
                uds.diagnostic_safety,
                "validate_channel_lock",
                side_effect=uds.diagnostic_safety.ChannelLockError("wrong channel"),
            ),
            mock.patch.object(uds.subprocess, "run") as run,
            self.assertRaises(uds.diagnostic_safety.ChannelLockError),
        ):
            uds.bring_up_can("can9", 500000, lock_handle=object())

        run.assert_not_called()

    def test_recover_socket_holds_one_supplied_lock_across_reconfigure_and_open(self):
        handle = object()
        reopened = object()
        with (
            mock.patch.object(uds.diagnostic_safety, "validate_channel_lock", return_value=handle),
            mock.patch.object(uds, "bring_up_can", return_value=True) as bring_up,
            mock.patch.object(uds, "open_socket", return_value=reopened) as open_socket,
        ):
            result = uds.recover_socket(
                0x760,
                0x768,
                "can9",
                125000,
                max_wait=1,
                timeout=0.25,
                addressing_mode=NORMAL_11BITS,
                lock_handle=handle,
            )

        self.assertIs(result, reopened)
        bring_up.assert_called_once_with("can9", 125000, lock_handle=handle)
        open_socket.assert_called_once_with(0x760, 0x768, "can9", 0.25, NORMAL_11BITS)

    def test_recover_socket_rejects_invalid_bounds_before_lock_or_interface_use(self):
        for max_wait in (0, -1, float("nan"), float("inf"), True, "1"):
            with self.subTest(max_wait=max_wait):
                with (
                    mock.patch.object(uds.diagnostic_safety, "channel_lock") as channel_lock,
                    mock.patch.object(uds, "bring_up_can") as bring_up,
                    self.assertRaisesRegex(ValueError, "max_wait must be a positive finite"),
                ):
                    uds.recover_socket(0x760, 0x768, "can9", max_wait=max_wait)
                channel_lock.assert_not_called()
                bring_up.assert_not_called()

    def test_recover_socket_uses_monotonic_deadline_and_bounds_final_sleep(self):
        handle = object()
        with (
            mock.patch.object(uds.diagnostic_safety, "validate_channel_lock", return_value=handle),
            mock.patch.object(uds, "bring_up_can", return_value=False) as bring_up,
            mock.patch.object(uds.time, "monotonic", side_effect=(100.0, 100.0, 100.4, 101.0)),
            mock.patch.object(uds.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "within 1s"),
        ):
            uds.recover_socket(
                0x760, 0x768, "can9", max_wait=1, lock_handle=handle
            )

        bring_up.assert_called_once_with("can9", 500000, lock_handle=handle)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.6)

    def test_unknown_addressing_mode_fails_before_opening_socket(self):
        with mock.patch.object(uds.isotp, "socket") as socket_factory:
            with self.assertRaisesRegex(ValueError, "unsupported addressing_mode"):
                uds.open_socket(0x700, 0x708, addressing_mode="extended_magic")

        socket_factory.assert_not_called()

    def test_open_socket_closes_when_bind_fails(self):
        sock = mock.Mock()
        sock.bind.side_effect = OSError(errno.ENODEV, "adapter gone")
        with (
            mock.patch.object(uds.isotp, "socket", return_value=sock),
            mock.patch.object(uds.isotp, "Address"),
            self.assertRaises(OSError),
        ):
            uds.open_socket(0x18DA2AF1, 0x18DAF12A)

        sock.close.assert_called_once_with()

    def test_request_propagates_transport_error(self):
        sock = mock.Mock()
        sock.recv.side_effect = OSError(errno.ENODEV, "adapter gone")

        with self.assertRaises(OSError) as raised:
            uds.request(sock, bytes.fromhex("22 F1 87"), retries=0)

        self.assertEqual(raised.exception.errno, errno.ENODEV)

    def test_request_retries_initial_timeout(self):
        sock = mock.Mock()
        sock.recv.side_effect = TimeoutError()

        response, status = uds.request(sock, bytes.fromhex("22 F1 87"), retries=1)

        self.assertIsNone(response)
        self.assertIn("timeout", status)
        self.assertEqual(sock.send.call_count, 2)

    def test_request_applies_fresh_timeout_before_every_send(self):
        sock = mock.Mock()
        send_timeouts = []
        sock.send.side_effect = lambda _payload: send_timeouts.append(
            sock.settimeout.call_args.args[0]
        )
        sock.recv.side_effect = TimeoutError()

        uds.request(sock, bytes.fromhex("22 F1 87"), timeout=0.25, retries=1)

        self.assertEqual(send_timeouts, [0.25, 0.25])

    def test_request_freezes_one_shot_payload_before_retries(self):
        sock = mock.Mock()
        sock.recv.side_effect = TimeoutError()
        one_shot_payload = iter((0x22, 0xF1, 0x87))

        uds.request(sock, one_shot_payload, retries=1)

        self.assertEqual(
            sock.send.call_args_list,
            [mock.call(bytes.fromhex("22 F1 87")), mock.call(bytes.fromhex("22 F1 87"))],
        )

    def test_request_rejects_invalid_timeout_and_payload_before_socket_use(self):
        for invalid_timeout in (0, -1, float("nan"), float("inf"), True, "2", 10 ** 10000):
            with self.subTest(timeout=invalid_timeout):
                sock = mock.Mock()
                with self.assertRaisesRegex(ValueError, "positive finite"):
                    uds.request(sock, bytes.fromhex("22 F1 87"), timeout=invalid_timeout)
                sock.settimeout.assert_not_called()
                sock.send.assert_not_called()

        for invalid_payload in (b"", [], 3, True):
            with self.subTest(payload=invalid_payload):
                sock = mock.Mock()
                with self.assertRaises((TypeError, ValueError)):
                    uds.request(sock, invalid_payload)
                sock.settimeout.assert_not_called()
                sock.send.assert_not_called()

    def test_response_pending_is_bounded_and_never_retransmitted(self):
        sock = mock.Mock()
        sock.recv.return_value = bytes.fromhex("7F 22 78")

        response, status = uds.request(
            sock,
            bytes.fromhex("22 F1 87"),
            retries=3,
            response_pending_timeout=1.0,
            max_pending_responses=3,
        )

        self.assertIsNone(response)
        self.assertIn("responsePending count limit", status)
        self.assertIn("allowance 3 exceeded by reply 4", status)
        self.assertEqual(sock.recv.call_count, 4)
        sock.send.assert_called_once_with(bytes.fromhex("22 F1 87"))

    def test_response_pending_allows_exact_limit_then_final_response(self):
        sock = mock.Mock()
        pending = bytes.fromhex("7F 22 78")
        positive = bytes.fromhex("62 F1 87 31 32")
        sock.recv.side_effect = [pending, pending, pending, positive]

        response, status = uds.request(
            sock,
            bytes.fromhex("22 F1 87"),
            retries=3,
            response_pending_timeout=1.0,
            max_pending_responses=3,
        )

        self.assertEqual(response, positive)
        self.assertEqual(status, "POSITIVE")
        self.assertEqual(sock.recv.call_count, 4)
        sock.send.assert_called_once_with(bytes.fromhex("22 F1 87"))

    def test_response_pending_must_echo_current_request_sid(self):
        sock = mock.Mock()
        stale_pending = bytes.fromhex("7F 10 78")
        sock.recv.return_value = stale_pending

        response, status = uds.request(
            sock,
            bytes.fromhex("22 F1 87"),
            retries=3,
            response_pending_timeout=1.0,
            max_pending_responses=1,
        )

        self.assertEqual(response, stale_pending)
        self.assertIn("sid=10", status)
        self.assertNotIn("responsePending count limit", status)
        sock.send.assert_called_once_with(bytes.fromhex("22 F1 87"))

    def test_drain_restores_timeout_and_propagates_real_errors(self):
        sock = mock.Mock()
        sock.gettimeout.return_value = 0.75
        sock.recv.side_effect = BlockingIOError()

        uds.drain(sock)

        self.assertEqual(sock.settimeout.call_args_list, [mock.call(0), mock.call(0.75)])

        sock.reset_mock()
        sock.gettimeout.return_value = 0.5
        sock.recv.side_effect = OSError(errno.ENETDOWN, "bus down")
        with self.assertRaises(OSError):
            uds.drain(sock)
        self.assertEqual(sock.settimeout.call_args_list, [mock.call(0), mock.call(0.5)])

    def test_classify_handles_truncated_negative(self):
        self.assertIn("MALFORMED_NEGATIVE", uds.classify(b"\x22", b"\x7f"))

    def test_negative_response_details_are_structured(self):
        self.assertEqual(
            uds.negative_response_details(bytes.fromhex("7F 22 31")),
            {"request_sid": "22", "nrc": "31", "nrc_name": "requestOutOfRange"},
        )
        self.assertIsNone(uds.negative_response_details(bytes.fromhex("7F 22")))


if __name__ == "__main__":
    unittest.main()
