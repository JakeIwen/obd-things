import contextlib
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tools import signal_correlate


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


class SignalCorrelateCliSafetyTests(unittest.TestCase):
    def test_capture_is_dry_run_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "not-created.json")
            with (
                mock.patch.object(signal_correlate, "get", return_value=MODULE),
                mock.patch.object(signal_correlate, "capture") as capture,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = signal_correlate.main(
                    ["capture", "test_ecu", "--seconds", "1", "-o", path]
                )

            self.assertEqual(result, 0)
            capture.assert_not_called()
            self.assertFalse(os.path.exists(path))

    def test_execute_requires_all_session_and_condition_gates_before_capture(self):
        with (
            mock.patch.object(signal_correlate, "get", return_value=MODULE),
            mock.patch.object(signal_correlate, "capture") as capture,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = signal_correlate.main(["capture", "test_ecu", "--execute"])

        self.assertEqual(result, 2)
        capture.assert_not_called()

    def test_preflight_service_or_capture_error_prevents_socket_open(self):
        with (
            mock.patch.object(
                signal_correlate, "preflight", return_value=["promaster-drive-capture is active"]
            ),
            mock.patch.object(signal_correlate.uds, "open_module_socket") as open_socket,
        ):
            with self.assertRaisesRegex(signal_correlate.CaptureError, "drive-capture"):
                signal_correlate.capture(
                    MODULE,
                    [0xF187],
                    1.0,
                    "unused.json",
                    pair="6/14",
                    conditions="parked fixture",
                    confirmed_parked=True,
                    confirmed_session_change=True,
                    confirmed_no_active_routine=True,
                )
        open_socket.assert_not_called()

    def test_unmatched_analysis_glob_is_friendly(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = signal_correlate.main(["analyze", "/definitely/missing/*.json"])

        self.assertEqual(result, 2)
        self.assertIn("matched no existing", stderr.getvalue())

    def test_analysis_prints_the_selected_newest_match(self):
        with tempfile.TemporaryDirectory() as directory:
            older = os.path.join(directory, "capture_01.json")
            newer = os.path.join(directory, "capture_02.json")
            open(older, "w").close()
            open(newer, "w").close()
            with (
                mock.patch.object(signal_correlate, "analyze") as analyze,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                result = signal_correlate.main(["analyze", older, newer])

        self.assertEqual(result, 0)
        self.assertIn(f"analyze selected: {newer}", stdout.getvalue())
        analyze.assert_called_once_with(newer, "0845:0:4:>i4", 25, 0.5)


class SignalCorrelateLiveSafetyTests(unittest.TestCase):
    def test_exact_session_and_did_echo_with_bounded_requests_and_restore(self):
        sock = mock.Mock()
        requests = []

        def request(_sock, payload, **_kwargs):
            payload = bytes(payload)
            requests.append(payload)
            if payload == bytes.fromhex("10 03"):
                return bytes.fromhex("50 03 00 32 01 F4"), "POSITIVE"
            if payload == bytes.fromhex("22 F1 87"):
                return bytes.fromhex("62 F1 87 31"), "POSITIVE"
            raise AssertionError(payload)

        with (
            mock.patch.object(signal_correlate, "preflight", return_value=[]),
            mock.patch.object(
                signal_correlate.diagnostic_safety, "acquire_channel_lock", return_value=object()
            ),
            mock.patch.object(signal_correlate.diagnostic_safety, "release_channel_lock") as release,
            mock.patch.object(signal_correlate.uds, "open_module_socket", return_value=sock),
            mock.patch.object(signal_correlate.uds, "drain"),
            mock.patch.object(signal_correlate.uds, "request", side_effect=request),
            mock.patch.object(signal_correlate.canbus, "restore_passive", return_value=True) as restore,
            mock.patch.object(signal_correlate, "_dump"),
            mock.patch.object(signal_correlate.time, "sleep"),
            mock.patch("builtins.print"),
        ):
            report = signal_correlate.capture(
                MODULE,
                [0xF187],
                5.0,
                "unused.json",
                request_rate=10.0,
                max_requests=2,
                pair="6/14",
                conditions="parked fixture",
                confirmed_parked=True,
                confirmed_session_change=True,
                confirmed_no_active_routine=True,
            )

        self.assertEqual(requests, [bytes.fromhex("10 03"), bytes.fromhex("22 F1 87")])
        self.assertEqual(report["request_attempts"], 2)
        self.assertEqual(report["samples"][0]["data"]["F187"], "31")
        self.assertEqual(report["status"], "complete")
        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can9", 500000)
        release.assert_called_once()

    def test_bad_session_echo_aborts_reads_but_still_restores(self):
        sock = mock.Mock()
        with (
            mock.patch.object(signal_correlate, "preflight", return_value=[]),
            mock.patch.object(
                signal_correlate.diagnostic_safety, "acquire_channel_lock", return_value=object()
            ),
            mock.patch.object(signal_correlate.diagnostic_safety, "release_channel_lock"),
            mock.patch.object(signal_correlate.uds, "open_module_socket", return_value=sock),
            mock.patch.object(signal_correlate.uds, "drain"),
            mock.patch.object(
                signal_correlate.uds,
                "request",
                return_value=(bytes.fromhex("50 02"), "UNEXPECTED"),
            ) as request,
            mock.patch.object(signal_correlate.canbus, "restore_passive", return_value=True) as restore,
            mock.patch.object(signal_correlate, "_dump"),
            mock.patch("builtins.print"),
        ):
            report = signal_correlate.capture(
                MODULE,
                [0xF187],
                1.0,
                "unused.json",
                max_requests=2,
                pair="6/14",
                conditions="parked fixture",
                confirmed_parked=True,
                confirmed_session_change=True,
                confirmed_no_active_routine=True,
            )

        self.assertEqual(request.call_count, 1)
        self.assertEqual(report["status"], "failed")
        self.assertIn("exact 50 03", report["fatal_error"])
        restore.assert_called_once_with("can9", 500000)

    def test_atomic_checkpoint_replaces_complete_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "capture.json")
            signal_correlate._dump(MODULE, [0xF187], 1.0, [], path)

            with open(path) as handle:
                payload = json.load(handle)

            self.assertEqual(payload["module"], "test_ecu")
            self.assertEqual(payload["dids"], ["F187"])
            self.assertFalse(any(".tmp-" in name for name in os.listdir(directory)))

    def test_first_signal_during_cleanup_cannot_skip_restore_or_unlock(self):
        sock = mock.Mock()
        sig = signal_correlate.diagnostic_safety.signal
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
            mock.patch.object(signal_correlate, "preflight", return_value=[]),
            mock.patch.object(
                signal_correlate.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(
                signal_correlate.diagnostic_safety,
                "release_channel_lock",
                side_effect=release,
            ) as unlock,
            mock.patch.object(
                signal_correlate.diagnostic_safety.signal,
                "signal",
                side_effect=fake_signal,
            ),
            mock.patch.object(signal_correlate.uds, "open_module_socket", return_value=sock),
            mock.patch.object(signal_correlate.uds, "drain"),
            mock.patch.object(
                signal_correlate.uds,
                "request",
                return_value=(bytes.fromhex("50 03"), "POSITIVE"),
            ),
            mock.patch.object(signal_correlate.canbus, "restore_passive", side_effect=restore) as passive,
            mock.patch.object(signal_correlate, "_dump"),
            mock.patch("builtins.print"),
        ):
            report = signal_correlate.capture(
                MODULE,
                [0xF187],
                0.0,
                "unused.json",
                max_requests=2,
                pair="6/14",
                conditions="parked fixture",
                confirmed_parked=True,
                confirmed_session_change=True,
                confirmed_no_active_routine=True,
            )

        self.assertEqual(report["status"], "interrupted")
        self.assertTrue(report["interrupted"])
        passive.assert_called_once_with("can9", 500000)
        unlock.assert_called_once_with(mock.sentinel.lock)


if __name__ == "__main__":
    unittest.main()
