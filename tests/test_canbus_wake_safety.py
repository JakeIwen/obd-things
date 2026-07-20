import unittest
import signal
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from lib import canbus, diagnostic_safety, uds
from lib import modules


MODULE = SimpleNamespace(
    key="rf_hub",
    channel="can9",
    bitrate=500000,
    addressing_mode="normal_29bits",
    txid=0x18DAC7F1,
    rxid=0x18DAF1C7,
)


@contextmanager
def fake_channel_lock(events=None):
    events = events if events is not None else []
    handle = object()

    def acquire(_channel):
        events.append("lock")
        return handle

    def release(actual_handle):
        if actual_handle is not None:
            assert actual_handle is handle
            events.append("unlock")

    with (
        mock.patch.object(canbus.diagnostic_safety, "acquire_channel_lock", side_effect=acquire),
        mock.patch.object(canbus.diagnostic_safety, "release_channel_lock", side_effect=release),
    ):
        yield handle


class WakeBurstSafetyTests(unittest.TestCase):
    def test_burst_locks_closes_and_restores_before_success(self):
        events = []
        sock = mock.Mock()
        sock.send.side_effect = lambda _frame: events.append("send") or 16
        sock.close.side_effect = lambda: events.append("close")
        with (
            fake_channel_lock(events),
            mock.patch.object(canbus, "ip_up", return_value=True),
            mock.patch.object(canbus, "is_listen_only", return_value=False),
            mock.patch.object(canbus.socket, "socket", return_value=sock),
            mock.patch.object(canbus, "WAKE_N", 2),
            mock.patch.object(canbus.time, "sleep"),
            mock.patch.object(
                canbus, "restore_passive", side_effect=lambda *_args: events.append("restore") or True
            ),
        ):
            result = canbus.tx_wake_burst("can9", 125000)

        self.assertTrue(result)
        self.assertEqual(events, ["lock", "send", "send", "close", "restore", "unlock"])
        sock.bind.assert_called_once_with(("can9",))

    def test_all_failed_sends_are_not_false_success(self):
        sock = mock.Mock()
        sock.send.side_effect = OSError("not accepted")
        with (
            fake_channel_lock(),
            mock.patch.object(canbus, "ip_up", return_value=True),
            mock.patch.object(canbus, "is_listen_only", return_value=False),
            mock.patch.object(canbus.socket, "socket", return_value=sock),
            mock.patch.object(canbus, "WAKE_N", 2),
            mock.patch.object(canbus.time, "sleep"),
            mock.patch.object(canbus, "restore_passive", return_value=True),
        ):
            self.assertFalse(canbus.tx_wake_burst("can9", 125000))
        sock.close.assert_called_once_with()

    def test_restore_failure_is_propagated_after_socket_close(self):
        sock = mock.Mock()
        with (
            fake_channel_lock(),
            mock.patch.object(canbus, "ip_up", return_value=True),
            mock.patch.object(canbus, "is_listen_only", return_value=False),
            mock.patch.object(canbus.socket, "socket", return_value=sock),
            mock.patch.object(canbus, "WAKE_N", 1),
            mock.patch.object(canbus.time, "sleep"),
            mock.patch.object(canbus, "restore_passive", return_value=False),
        ):
            with self.assertRaisesRegex(canbus.PassiveRestoreError, "could not verify"):
                canbus.tx_wake_burst("can9", 125000)
        sock.close.assert_called_once_with()

    def test_lock_contention_never_arms(self):
        with (
            mock.patch.object(
                canbus.diagnostic_safety,
                "acquire_channel_lock",
                side_effect=diagnostic_safety.ChannelLockError("busy"),
            ),
            mock.patch.object(canbus.diagnostic_safety, "release_channel_lock") as release,
            mock.patch.object(canbus, "ip_up") as ip_up,
        ):
            with self.assertRaises(diagnostic_safety.ChannelLockError):
                canbus.tx_wake_burst("can9", 125000)
        ip_up.assert_not_called()
        release.assert_called_once_with(None)

    def test_sigterm_during_burst_runs_close_restore_and_unlock_before_propagating(self):
        events = []
        sock = mock.Mock()

        def interrupt_send(_frame):
            events.append("send")
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        sock.send.side_effect = interrupt_send
        sock.close.side_effect = lambda: events.append("close")
        with (
            fake_channel_lock(events),
            mock.patch.object(canbus, "ip_up", return_value=True),
            mock.patch.object(canbus, "is_listen_only", return_value=False),
            mock.patch.object(canbus.socket, "socket", return_value=sock),
            mock.patch.object(canbus, "WAKE_N", 2),
            mock.patch.object(canbus.time, "sleep"),
            mock.patch.object(
                canbus, "restore_passive", side_effect=lambda *_args: events.append("restore") or True
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                canbus.tx_wake_burst("can9", 125000)

        self.assertEqual(events, ["lock", "send", "close", "restore", "unlock"])

    def test_sigterm_during_cleanup_is_ignored_until_restore_and_unlock_finish(self):
        events = []
        sock = mock.Mock()
        sock.send.return_value = 16

        def interrupt_close():
            events.append("close")
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        sock.close.side_effect = interrupt_close
        with (
            fake_channel_lock(events),
            mock.patch.object(canbus, "ip_up", return_value=True),
            mock.patch.object(canbus, "is_listen_only", return_value=False),
            mock.patch.object(canbus.socket, "socket", return_value=sock),
            mock.patch.object(canbus, "WAKE_N", 1),
            mock.patch.object(canbus.time, "sleep"),
            mock.patch.object(
                canbus, "restore_passive", side_effect=lambda *_args: events.append("restore") or True
            ),
        ):
            self.assertTrue(canbus.tx_wake_burst("can9", 125000))

        self.assertEqual(events, ["lock", "close", "restore", "unlock"])


class AddressedWakeSafetyTests(unittest.TestCase):
    def test_exact_did_echo_is_success_with_no_retry_and_verified_restore(self):
        sock = mock.Mock()
        with (
            fake_channel_lock(),
            mock.patch.object(canbus, "ip_up", return_value=True),
            mock.patch.object(canbus, "is_listen_only", return_value=False),
            mock.patch.object(modules, "get", return_value=MODULE),
            mock.patch.object(uds, "open_module_socket", return_value=sock),
            mock.patch.object(uds, "drain") as drain,
            mock.patch.object(
                uds, "request", return_value=(bytes.fromhex("62 F1 90 31"), "POSITIVE")
            ) as request,
            mock.patch.object(canbus, "restore_passive", return_value=True) as restore,
        ):
            result = canbus.poke_wake("can9", 500000)

        self.assertTrue(result)
        drain.assert_called_once_with(sock)
        self.assertEqual(request.call_args.kwargs["retries"], 0)
        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can9", 500000)

    def test_unrelated_response_is_not_false_success(self):
        sock = mock.Mock()
        with (
            fake_channel_lock(),
            mock.patch.object(canbus, "ip_up", return_value=True),
            mock.patch.object(canbus, "is_listen_only", return_value=False),
            mock.patch.object(modules, "get", return_value=MODULE),
            mock.patch.object(uds, "open_module_socket", return_value=sock),
            mock.patch.object(uds, "drain"),
            mock.patch.object(
                uds, "request", return_value=(bytes.fromhex("62 F1 87 31"), "UNEXPECTED")
            ),
            mock.patch.object(canbus, "restore_passive", return_value=True),
        ):
            self.assertFalse(canbus.poke_wake("can9", 500000))

    def test_transport_failure_still_closes_and_restores(self):
        sock = mock.Mock()
        with (
            fake_channel_lock(),
            mock.patch.object(canbus, "ip_up", return_value=True),
            mock.patch.object(canbus, "is_listen_only", return_value=False),
            mock.patch.object(modules, "get", return_value=MODULE),
            mock.patch.object(uds, "open_module_socket", return_value=sock),
            mock.patch.object(uds, "drain"),
            mock.patch.object(uds, "request", side_effect=OSError("adapter gone")),
            mock.patch.object(canbus, "restore_passive", return_value=True) as restore,
        ):
            self.assertFalse(canbus.poke_wake("can9", 500000))

        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can9", 500000)


class WakeOrchestrationSafetyTests(unittest.TestCase):
    def test_failed_wake_attempts_propagate_final_restore_failure(self):
        with (
            mock.patch.object(canbus, "detect_bus", return_value=("silent", 500000)),
            mock.patch.object(canbus, "poke_wake", return_value=False),
            mock.patch.object(canbus, "tx_wake_burst", return_value=False),
            mock.patch.object(canbus, "restore_passive", return_value=False),
        ):
            with self.assertRaises(canbus.PassiveRestoreError):
                canbus.wake("can9")


if __name__ == "__main__":
    unittest.main()
