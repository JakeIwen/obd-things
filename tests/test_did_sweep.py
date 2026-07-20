import argparse
import contextlib
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from tools import did_sweep


class DidSelectionTests(unittest.TestCase):
    def test_legacy_range_and_full_range_plan(self):
        bounded = did_sweep.parser().parse_args(["radar_acc", "0800", "08FF"])
        full = did_sweep.parser().parse_args(["radar_acc", "--full-range"])

        self.assertEqual(did_sweep.selected_range(bounded), (0x0800, 0x08FF))
        self.assertEqual(did_sweep.selected_range(full), (0x0000, 0xFFFF))

    def test_reversed_or_conflicting_range_is_rejected(self):
        reversed_args = did_sweep.parser().parse_args(["radar_acc", "0900", "0800"])
        conflicting = did_sweep.parser().parse_args(
            ["radar_acc", "0800", "08FF", "--full-range"]
        )

        with self.assertRaises(ValueError):
            did_sweep.selected_range(reversed_args)
        with self.assertRaises(ValueError):
            did_sweep.selected_range(conflicting)

    def test_session_rejects_response_suppression_bit(self):
        self.assertEqual(did_sweep.parse_session("03"), 0x03)
        for value in ("00", "80", "FF"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                did_sweep.parse_session(value)


class DidResponseTests(unittest.TestCase):
    def test_positive_requires_exact_did_echo(self):
        self.assertEqual(
            did_sweep.classify_did_response(0xF187, bytes.fromhex("62 F1 87 31")),
            "positive",
        )
        self.assertEqual(
            did_sweep.classify_did_response(0xF187, bytes.fromhex("62 F1 88 31")),
            "unexpected",
        )

    def test_nrc_categories_are_session_specific(self):
        self.assertEqual(
            did_sweep.classify_did_response(0x1234, bytes.fromhex("7F 22 31")),
            "out_of_range_current_session",
        )
        self.assertEqual(
            did_sweep.classify_did_response(0x1234, bytes.fromhex("7F 22 33")),
            "security_denied",
        )

    def test_tester_present_requires_exact_positive_echo(self):
        self.assertEqual(
            did_sweep.classify_tester_present_response(bytes.fromhex("7E 00")),
            "positive_echo",
        )
        self.assertEqual(
            did_sweep.classify_tester_present_response(bytes.fromhex("7E 00 AA")),
            "unexpected",
        )

    def test_query_sends_only_22_and_redacts_vin(self):
        full_vin = b"1M8GDM9AXKP042788"
        response = bytes.fromhex("62 F1 90") + full_vin
        with (
            mock.patch.object(did_sweep.uds, "drain"),
            mock.patch.object(did_sweep.uds, "request", return_value=(response, "POSITIVE")) as request,
        ):
            result = did_sweep.query_did(mock.Mock(), 0xF190, 0.5)

        request.assert_called_once_with(
            mock.ANY, bytes.fromhex("22 F1 90"), timeout=0.5, retries=0
        )
        self.assertEqual(result["ascii"], "1M8GDM9AXKP######")
        self.assertNotIn(full_vin.decode(), json.dumps(result))


class DidCliSafetyTests(unittest.TestCase):
    def test_dry_run_never_preflights_opens_can_or_writes(self):
        with (
            mock.patch.object(did_sweep, "preflight") as preflight,
            mock.patch.object(did_sweep.uds, "open_module_socket") as open_socket,
            mock.patch.object(did_sweep, "atomic_json") as write_report,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = did_sweep.main(["radar_acc", "0800", "08FF"])

        self.assertEqual(result, 0)
        preflight.assert_not_called()
        open_socket.assert_not_called()
        write_report.assert_not_called()

    def test_live_requires_parked_conditions_before_preflight(self):
        with (
            mock.patch.object(did_sweep, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = did_sweep.main(["radar_acc", "0800", "08FF", "--execute"])

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_implicit_full_range_cannot_execute(self):
        with (
            mock.patch.object(did_sweep, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = did_sweep.main(
                [
                    "radar_acc", "--execute", "--confirm-parked", "--pair", "6/14",
                    "--conditions", "parked", "--confirm-expanded-scan",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_session_change_requires_explicit_confirmation(self):
        with (
            mock.patch.object(did_sweep, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = did_sweep.main(
                [
                    "radar_acc", "F180", "F18F", "--session", "03", "--execute",
                    "--confirm-parked", "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_explicit_session_refuses_rate_below_keepalive_floor(self):
        with (
            mock.patch.object(did_sweep, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = did_sweep.main(
                [
                    "radar_acc", "F180", "F18F", "--session", "03", "--rate", "0.49",
                    "--confirm-session-change",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_unbounded_timeout_and_too_slow_rate_are_rejected(self):
        for option, value in (("--timeout", "5.01"), ("--rate", "0.09")):
            with (
                mock.patch.object(did_sweep, "preflight") as preflight,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = did_sweep.main(
                    ["radar_acc", "0800", "0800", option, value]
                )
            self.assertEqual(result, 2)
            preflight.assert_not_called()

    def test_failure_still_restores_passive_and_preserves_summary(self):
        sock = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "summary.json"
            results = root / "results.jsonl"
            with (
                mock.patch.object(did_sweep, "preflight", return_value=[]),
                mock.patch.object(did_sweep, "output_paths", return_value=(str(summary), str(results))),
                mock.patch.object(did_sweep.uds, "open_module_socket", return_value=sock),
                mock.patch.object(did_sweep.uds, "drain"),
                mock.patch.object(did_sweep.uds, "request", side_effect=OSError("adapter gone")),
                mock.patch.object(did_sweep.canbus, "restore_passive", return_value=True) as restore,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = did_sweep.main(
                    [
                        "radar_acc", "0800", "0800", "--execute", "--confirm-parked",
                        "--pair", "6/14", "--conditions", "parked",
                    ]
                )

            self.assertEqual(result, 1)
            restore.assert_called_once_with("can0", 500000)
            payload = json.loads(summary.read_text())
            self.assertEqual(payload["status"], "failed")
            self.assertIn("adapter gone", payload["fatal_error"])
            self.assertEqual(payload["request_attempts"]["did_reads"], 1)
            self.assertEqual(payload["responses_received"]["did_reads"], 0)
            self.assertNotIn("did_reads", payload["transmit_counts"])

    def test_failed_result_write_is_not_counted_as_written(self):
        real_open = open
        sock = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "summary.json"
            results = root / "results.jsonl"
            failing_results = mock.MagicMock()
            failing_results.__enter__.return_value = failing_results
            failing_results.__exit__.return_value = False
            failing_results.write.side_effect = OSError("results write fixture")

            def selective_open(path, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                if Path(path) == results and "a" in mode:
                    return failing_results
                return real_open(path, *args, **kwargs)

            with (
                mock.patch.object(did_sweep, "preflight", return_value=[]),
                mock.patch.object(
                    did_sweep, "output_paths", return_value=(str(summary), str(results))
                ),
                mock.patch.object(did_sweep.uds, "open_module_socket", return_value=sock),
                mock.patch.object(did_sweep.uds, "drain"),
                mock.patch.object(
                    did_sweep.uds,
                    "request",
                    return_value=(bytes.fromhex("62 08 00 01"), "POSITIVE"),
                ),
                mock.patch.object(did_sweep.canbus, "restore_passive", return_value=True),
                mock.patch("builtins.open", side_effect=selective_open),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = did_sweep.main(
                    [
                        "radar_acc", "0800", "0800", "--execute", "--confirm-parked",
                        "--pair", "6/14", "--conditions", "parked",
                    ]
                )

            payload = json.loads(summary.read_text())

        self.assertEqual(result, 1)
        self.assertEqual(payload["request_attempts"]["did_reads"], 1)
        self.assertEqual(payload["results_written"], 0)
        self.assertIn("results write fixture", payload["fatal_error"])

    def test_initial_summary_write_failure_still_restores_releases_and_publishes_failure(self):
        original_atomic_json = did_sweep.atomic_json
        calls = 0

        def fail_first_write(path, payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("initial write fixture")
            return original_atomic_json(path, payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "summary.json"
            results = root / "results.jsonl"
            with (
                mock.patch.object(did_sweep, "preflight", return_value=[]),
                mock.patch.object(
                    did_sweep.diagnostic_safety,
                    "acquire_channel_lock",
                    return_value=mock.sentinel.lock,
                ),
                mock.patch.object(
                    did_sweep.diagnostic_safety, "release_channel_lock"
                ) as release,
                mock.patch.object(
                    did_sweep, "output_paths", return_value=(str(summary), str(results))
                ),
                mock.patch.object(did_sweep, "atomic_json", side_effect=fail_first_write),
                mock.patch.object(did_sweep.uds, "open_module_socket") as open_socket,
                mock.patch.object(
                    did_sweep.canbus, "restore_passive", return_value=True
                ) as restore,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = did_sweep.main(
                    [
                        "radar_acc", "0800", "0800", "--execute", "--confirm-parked",
                        "--pair", "6/14", "--conditions", "parked",
                    ]
                )

            payload = json.loads(summary.read_text())

        self.assertEqual(result, 1)
        self.assertIn("initial write fixture", payload["fatal_error"])
        self.assertTrue(payload["restored_passive"])
        self.assertEqual(payload["status"], "failed")
        open_socket.assert_not_called()
        restore.assert_called_once_with("can0", 500000)
        release.assert_called_once_with(mock.sentinel.lock)

    def test_sigterm_and_repeated_sigterm_cannot_interrupt_cleanup_or_partial_summary(self):
        installed = {}
        old_handlers = {
            did_sweep.signal.SIGINT: mock.sentinel.old_int,
            did_sweep.signal.SIGTERM: mock.sentinel.old_term,
            did_sweep.signal.SIGHUP: mock.sentinel.old_hup,
        }

        def fake_signal(signum, handler):
            if callable(handler):
                installed[signum] = handler
            return old_handlers[signum]

        def interrupt_query(*_args, **_kwargs):
            installed[did_sweep.signal.SIGTERM](did_sweep.signal.SIGTERM, None)

        def restore_with_repeated_term(_channel, _bitrate):
            installed[did_sweep.signal.SIGTERM](did_sweep.signal.SIGTERM, None)
            installed[did_sweep.signal.SIGTERM](did_sweep.signal.SIGTERM, None)
            return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "summary.json"
            results = root / "results.jsonl"
            sock = mock.Mock()
            with (
                mock.patch.object(did_sweep, "preflight", return_value=[]),
                mock.patch.object(
                    did_sweep.diagnostic_safety,
                    "acquire_channel_lock",
                    return_value=mock.sentinel.lock,
                ),
                mock.patch.object(
                    did_sweep.diagnostic_safety, "release_channel_lock"
                ) as release,
                mock.patch.object(
                    did_sweep, "output_paths", return_value=(str(summary), str(results))
                ),
                mock.patch.object(did_sweep.uds, "open_module_socket", return_value=sock),
                mock.patch.object(did_sweep, "query_did", side_effect=interrupt_query),
                mock.patch.object(
                    did_sweep.canbus,
                    "restore_passive",
                    side_effect=restore_with_repeated_term,
                ) as restore,
                mock.patch.object(did_sweep.signal, "signal", side_effect=fake_signal),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = did_sweep.main(
                    [
                        "radar_acc", "0800", "0800", "--execute", "--confirm-parked",
                        "--pair", "6/14", "--conditions", "parked",
                    ]
                )

            payload = json.loads(summary.read_text())

        self.assertEqual(result, 130)
        self.assertTrue(payload["interrupted"])
        self.assertEqual(payload["interruption_signal"], "SIGTERM")
        self.assertEqual(payload["status"], "interrupted")
        self.assertTrue(payload["restored_passive"])
        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can0", 500000)
        release.assert_called_once_with(mock.sentinel.lock)

    def test_bad_tester_present_is_persisted_and_marks_explicit_session_uncertain(self):
        sock = mock.Mock()
        report = {}

        def respond(_sock, payload, **_kwargs):
            payload = bytes(payload)
            if payload == bytes.fromhex("10 03"):
                return bytes.fromhex("50 03"), "POSITIVE"
            if payload == bytes.fromhex("3E 00"):
                return bytes.fromhex("7F 3E 22"), "NEGATIVE"
            raise AssertionError(f"unexpected payload {payload.hex()}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "summary.json"
            results = root / "results.jsonl"
            with (
                mock.patch.object(did_sweep, "preflight", return_value=[]),
                mock.patch.object(
                    did_sweep, "output_paths", return_value=(str(summary), str(results))
                ),
                mock.patch.object(did_sweep.uds, "open_module_socket", return_value=sock),
                mock.patch.object(did_sweep.uds, "drain"),
                mock.patch.object(did_sweep.uds, "request", side_effect=respond),
                mock.patch.object(did_sweep.time, "sleep"),
                mock.patch.object(did_sweep, "TESTER_PRESENT_INTERVAL_S", 0.0),
                mock.patch.object(did_sweep.canbus, "restore_passive", return_value=True),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = did_sweep.main(
                    [
                        "radar_acc", "0800", "0800", "--session", "03",
                        "--confirm-session-change", "--execute", "--confirm-parked",
                        "--pair", "6/14", "--conditions", "parked",
                    ]
                )

            report.update(json.loads(summary.read_text()))

        self.assertEqual(result, 1)
        self.assertEqual(report["session_state"], "uncertain_after_tester_present_failure")
        self.assertEqual(report["tester_present_results"][0]["category"], "negative")
        self.assertFalse(report["tester_present_results"][0]["validated_echo"])
        self.assertEqual(report["request_attempts"]["session_control"], 1)
        self.assertEqual(report["responses_received"]["session_control"], 1)
        self.assertEqual(report["request_attempts"]["tester_present"], 1)
        self.assertEqual(report["responses_received"]["tester_present"], 1)
        self.assertEqual(report["request_attempts"]["did_reads"], 0)
        self.assertIn("explicit session is uncertain", report["fatal_error"])


if __name__ == "__main__":
    unittest.main()
