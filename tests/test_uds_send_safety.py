import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from tools import uds_send


MODULE = SimpleNamespace(
    key="test_ecu",
    name="Test ECU",
    channel="can9",
    bus="c-can",
    bitrate=500000,
    addressing_mode="normal_29bits",
    txid=0x18DA10F1,
    rxid=0x18DAF110,
)
READ_LIVE = [
    "test_ecu", "22", "F1", "87", "--execute", "--confirm-parked",
    "--pair", "6/14", "--conditions", "parked fixture",
]


class UdsSendClassificationTests(unittest.TestCase):
    def test_required_mutating_services_fail_closed(self):
        for payload in (
            "11 01", "14 FF", "27 01", "28 00", "2E F1 87", "2F 50 40 03",
            "31 01 02 51", "31 02 02 51", "85 01",
        ):
            with self.subTest(payload=payload):
                category, _ = uds_send.classify_payload(bytes.fromhex(payload))
                self.assertEqual(category, "mutation_actuation")

    def test_result_only_routine_is_a_diagnostic_read(self):
        category, label = uds_send.classify_payload(bytes.fromhex("31 03 02 51"))

        self.assertEqual(category, "diagnostic_read")
        self.assertIn("requestRoutineResults", label)

    def test_unknown_service_fails_into_actuation_gate(self):
        self.assertEqual(uds_send.classify_payload(bytes.fromhex("99 00"))[0], "mutation_actuation")


class UdsSendCliSafetyTests(unittest.TestCase):
    def test_dry_run_opens_nothing(self):
        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            mock.patch.object(uds_send, "preflight") as preflight,
            mock.patch.object(uds_send.uds, "open_module_socket") as open_socket,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = uds_send.main(["test_ecu", "22", "F1", "87"])

        self.assertEqual(result, 0)
        preflight.assert_not_called()
        open_socket.assert_not_called()

    def test_dry_run_prints_exact_required_flag_names(self):
        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            result = uds_send.main(["test_ecu", "10", "03"])

        self.assertEqual(result, 0)
        plan = stdout.getvalue()
        for flag in (
            "--execute", "--confirm-parked", "--pair PAIR", "--conditions DESCRIPTION",
            "--confirm-engine-off", "--confirm-session-change", "--confirm-no-active-routine",
        ):
            self.assertIn(flag, plan)

    def test_mutation_requires_engine_off_and_actuation_confirmation(self):
        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            mock.patch.object(uds_send, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = uds_send.main(
                [
                    "test_ecu", "2E", "F1", "87", "00", "--execute", "--confirm-parked",
                    "--pair", "6/14", "--conditions", "parked fixture",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("--confirm-engine-off", stderr.getvalue())
        self.assertIn("--confirm-session-change", stderr.getvalue())
        self.assertIn("--confirm-actuation", stderr.getvalue())
        preflight.assert_not_called()

    def test_session_change_requires_session_and_no_routine_confirmations(self):
        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            mock.patch.object(uds_send, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = uds_send.main(
                [
                    "test_ecu", "10", "03", "--execute", "--confirm-parked",
                    "--confirm-engine-off", "--pair", "6/14", "--conditions", "parked fixture",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("--confirm-session-change", stderr.getvalue())
        self.assertIn("--confirm-no-active-routine", stderr.getvalue())
        preflight.assert_not_called()

    def test_service_or_capture_preflight_error_prevents_lock(self):
        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            mock.patch.object(uds_send, "preflight", return_value=["tpms-logger is active"]),
            mock.patch.object(uds_send.diagnostic_safety, "acquire_channel_lock") as lock,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = uds_send.main(READ_LIVE)

        self.assertEqual(result, 2)
        lock.assert_not_called()

    def test_live_read_drains_sends_once_closes_restores_and_unlocks(self):
        sock = mock.Mock()
        handle = object()
        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            mock.patch.object(uds_send, "preflight", return_value=[]),
            mock.patch.object(
                uds_send.diagnostic_safety, "acquire_channel_lock", return_value=handle
            ),
            mock.patch.object(uds_send.diagnostic_safety, "release_channel_lock") as release,
            mock.patch.object(uds_send.uds, "open_module_socket", return_value=sock),
            mock.patch.object(uds_send.uds, "drain") as drain,
            mock.patch.object(
                uds_send.uds,
                "request",
                return_value=(bytes.fromhex("62 F1 87 31"), "POSITIVE"),
            ) as request,
            mock.patch.object(uds_send.canbus, "restore_passive", return_value=True) as restore,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = uds_send.main(READ_LIVE)

        self.assertEqual(result, 0)
        drain.assert_called_once_with(sock)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], bytes.fromhex("22 F1 87"))
        self.assertEqual(request.call_args.kwargs["retries"], 0)
        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can9", 500000)
        release.assert_called_once_with(handle)

    def test_restore_failure_returns_failure(self):
        sock = mock.Mock()
        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            mock.patch.object(uds_send, "preflight", return_value=[]),
            mock.patch.object(
                uds_send.diagnostic_safety, "acquire_channel_lock", return_value=object()
            ),
            mock.patch.object(uds_send.diagnostic_safety, "release_channel_lock"),
            mock.patch.object(uds_send.uds, "open_module_socket", return_value=sock),
            mock.patch.object(uds_send.uds, "drain"),
            mock.patch.object(uds_send.uds, "request", return_value=(None, "NO_RESPONSE")),
            mock.patch.object(uds_send.canbus, "restore_passive", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = uds_send.main(READ_LIVE)

        self.assertEqual(result, 1)

    def test_first_signal_during_cleanup_cannot_skip_restore_or_unlock(self):
        sock = mock.Mock()
        sig = uds_send.diagnostic_safety.signal
        current = {
            sig.SIGINT: mock.sentinel.old_int,
            sig.SIGTERM: mock.sentinel.old_term,
            sig.SIGHUP: mock.sentinel.old_hup,
        }

        def fake_signal(signum, handler):
            previous = current[signum]
            current[signum] = handler
            return previous

        def signal_now(signum):
            current[signum](signum, None)

        sock.close.side_effect = lambda: signal_now(sig.SIGTERM)

        def restore(*_args):
            signal_now(sig.SIGHUP)
            return True

        def release(*_args):
            signal_now(sig.SIGTERM)

        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            mock.patch.object(uds_send, "preflight", return_value=[]),
            mock.patch.object(
                uds_send.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(
                uds_send.diagnostic_safety,
                "release_channel_lock",
                side_effect=release,
            ) as unlock,
            mock.patch.object(uds_send.diagnostic_safety.signal, "signal", side_effect=fake_signal),
            mock.patch.object(uds_send.uds, "open_module_socket", return_value=sock),
            mock.patch.object(uds_send.uds, "drain"),
            mock.patch.object(
                uds_send.uds,
                "request",
                return_value=(bytes.fromhex("62 F1 87 31"), "POSITIVE"),
            ),
            mock.patch.object(uds_send.canbus, "restore_passive", side_effect=restore) as passive,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = uds_send.main(READ_LIVE)

        self.assertEqual(result, 130)
        passive.assert_called_once_with("can9", 500000)
        unlock.assert_called_once_with(mock.sentinel.lock)

    def test_combined_multibyte_token_is_rejected(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            uds_send.parser().parse_args(["test_ecu", "31", "03", "0251"])


if __name__ == "__main__":
    unittest.main()
