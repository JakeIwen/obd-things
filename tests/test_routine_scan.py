import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import routine_scan


class RoutineSelectionTests(unittest.TestCase):
    def test_positional_module_start_end_remain_hex(self):
        args = routine_scan.parser().parse_args(["radar_acc", "0200", "03FF"])

        self.assertEqual(args.module, "radar_acc")
        self.assertEqual(args.start, 0x0200)
        self.assertEqual(args.end, 0x03FF)

    def test_extras_are_deduplicated_when_the_range_overlaps(self):
        self.assertEqual(
            routine_scan.build_rids(0xFF00, 0xFF02),
            [0xFF00, 0xFF01, 0xFF02, 0xFF03],
        )

    def test_payload_builder_can_only_make_request_results(self):
        for rid in (0x0000, 0x0251, 0xFFFF):
            payload = routine_scan.routine_payload(rid)
            self.assertEqual(payload[:2], bytes.fromhex("31 03"))
            self.assertEqual(int.from_bytes(payload[2:4], "big"), rid)
            self.assertEqual(len(payload), 4)


class RoutineResponseTests(unittest.TestCase):
    def test_result_response_requires_exact_subfunction_and_rid_echo(self):
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("71 03 02 51 AA")),
            "positive_results",
        )
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("71 01 02 51")),
            "unexpected",
        )
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("71 03 02 52")),
            "unexpected",
        )

    def test_expected_negatives_timeout_and_other_nrc_are_distinct(self):
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("7F 31 24")),
            "request_sequence_error_candidate",
        )
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("7F 31 31")),
            "out_of_range_current_session",
        )
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("7F 31 22")),
            "conditions_not_correct",
        )
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("7F 31 33")),
            "security_denied",
        )
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("7F 31 7E")),
            "subfunction_not_supported_active_session",
        )
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("7F 31 7F")),
            "service_not_supported_active_session",
        )
        self.assertEqual(routine_scan.classify_routine_response(0x0251, None), "timeout")
        self.assertEqual(
            routine_scan.classify_routine_response(0x0251, bytes.fromhex("7F 22 31")),
            "unexpected",
        )

    def test_session_response_requires_requested_session_echo(self):
        self.assertEqual(
            routine_scan.classify_session_response(0x03, bytes.fromhex("50 03 00 32 01 F4")),
            "positive_echo",
        )
        self.assertEqual(
            routine_scan.classify_session_response(0x03, bytes.fromhex("50 02")),
            "unexpected",
        )
        self.assertEqual(
            routine_scan.classify_session_response(0x03, bytes.fromhex("7F 10 22")),
            "negative",
        )

    def test_tester_present_requires_exact_positive_echo(self):
        self.assertEqual(
            routine_scan.classify_tester_present_response(bytes.fromhex("7E 00")),
            "positive_echo",
        )
        self.assertEqual(
            routine_scan.classify_tester_present_response(bytes.fromhex("7E 00 AA")),
            "unexpected",
        )


class RoutineCliSafetyTests(unittest.TestCase):
    def test_dry_run_does_not_preflight_or_touch_can(self):
        with (
            mock.patch.object(routine_scan, "preflight") as preflight,
            mock.patch.object(routine_scan.uds, "open_module_socket") as open_socket,
            mock.patch.object(routine_scan.canbus, "restore_passive") as restore,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = routine_scan.main(["radar_acc", "0251", "0251"])

        self.assertEqual(result, 0)
        preflight.assert_not_called()
        open_socket.assert_not_called()
        restore.assert_not_called()

    def test_live_requires_every_gate_before_preflight(self):
        cases = (
            ["radar_acc", "--execute"],
            ["radar_acc", "--execute", "--confirm-parked"],
            ["radar_acc", "--execute", "--confirm-parked", "--pair", "6/14"],
            [
                "radar_acc", "--execute", "--pair", "6/14", "--conditions", "parked"
            ],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                with (
                    mock.patch.object(routine_scan, "preflight") as preflight,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    result = routine_scan.main(argv)
                self.assertEqual(result, 2)
                preflight.assert_not_called()

    def test_session_requires_both_separate_confirmations(self):
        cases = (
            ["radar_acc", "--session", "03"],
            ["radar_acc", "--session", "03", "--confirm-session-change"],
            ["radar_acc", "--session", "03", "--confirm-no-active-routine"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                with (
                    mock.patch.object(routine_scan, "preflight") as preflight,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    result = routine_scan.main(argv)
                self.assertEqual(result, 2)
                preflight.assert_not_called()

    def test_rate_and_timeout_must_be_finite_and_bounded(self):
        cases = (
            ["radar_acc", "--rate", "nan"],
            ["radar_acc", "--rate", "5.01"],
            ["radar_acc", "--timeout", "inf"],
            ["radar_acc", "--timeout", "0"],
            ["radar_acc", "--retries", "3"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                with (
                    mock.patch.object(routine_scan, "preflight") as preflight,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    result = routine_scan.main(argv)
                self.assertEqual(result, 2)
                preflight.assert_not_called()

    def test_expanded_live_scan_requires_separate_confirmation_before_preflight(self):
        with (
            mock.patch.object(routine_scan, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "0000", "0200", "--execute", "--confirm-parked",
                    "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_explicit_session_refuses_rate_below_keepalive_floor(self):
        with (
            mock.patch.object(routine_scan, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "0251", "0251", "--session", "03", "--rate", "0.49",
                    "--confirm-session-change", "--confirm-no-active-routine",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_live_default_session_generates_only_31_03_and_no_session_or_tester_present(self):
        sock = mock.Mock()
        payloads = []

        def respond(_sock, payload, **kwargs):
            payloads.append(bytes(payload))
            return bytes.fromhex("7F 31 31"), "NEGATIVE"

        report = {}
        with (
            mock.patch.object(routine_scan, "preflight", return_value=[]),
            mock.patch.object(routine_scan.uds, "open_module_socket", return_value=sock),
            mock.patch.object(routine_scan.uds, "drain"),
            mock.patch.object(routine_scan.uds, "request", side_effect=respond) as request,
            mock.patch.object(routine_scan.time, "sleep"),
            mock.patch.object(routine_scan.canbus, "restore_passive", return_value=True),
            mock.patch.object(routine_scan, "report_path", return_value="/tmp/routines.json"),
            mock.patch.object(routine_scan, "append_result_checkpoint"),
            mock.patch.object(
                routine_scan, "write_report", side_effect=lambda _path, data: report.update(data)
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "FF00", "FF03", "--execute", "--confirm-parked",
                    "--pair", "6/14", "--conditions", "ignition on, engine off",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(payloads), 4)  # extras overlap the range and are not repeated
        self.assertTrue(all(payload[:2] == bytes.fromhex("31 03") for payload in payloads))
        self.assertTrue(all(call.kwargs["retries"] == 0 for call in request.call_args_list))
        self.assertFalse(report["diagnostic_session_control_sent"])
        self.assertFalse(report["tester_present_sent"])
        self.assertEqual(report["transmit_counts"]["routine_results"], 4)

    def test_valid_gated_session_is_sent_once_before_result_requests(self):
        sock = mock.Mock()
        payloads = []

        def respond(_sock, payload, **kwargs):
            payload = bytes(payload)
            payloads.append(payload)
            if payload == bytes.fromhex("10 03"):
                return bytes.fromhex("50 03 00 32 01 F4"), "POSITIVE"
            return bytes.fromhex("7F 31 31"), "NEGATIVE"

        with (
            mock.patch.object(routine_scan, "preflight", return_value=[]),
            mock.patch.object(routine_scan.uds, "open_module_socket", return_value=sock),
            mock.patch.object(routine_scan.uds, "drain"),
            mock.patch.object(routine_scan.uds, "request", side_effect=respond),
            mock.patch.object(routine_scan.time, "sleep"),
            mock.patch.object(routine_scan.canbus, "restore_passive", return_value=True),
            mock.patch.object(routine_scan, "append_result_checkpoint"),
            mock.patch.object(routine_scan, "write_report"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "FF00", "FF03", "--session", "03",
                    "--confirm-session-change", "--confirm-no-active-routine", "--execute",
                    "--confirm-parked", "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(payloads[0], bytes.fromhex("10 03"))
        self.assertEqual(payloads.count(bytes.fromhex("10 03")), 1)
        self.assertTrue(all(payload[:2] == bytes.fromhex("31 03") for payload in payloads[1:]))
        self.assertNotIn(bytes.fromhex("3E 00"), payloads)

    def test_retry_transmit_count_is_not_overwritten_in_inherited_or_explicit_session(self):
        for explicit_session in (False, True):
            with self.subTest(explicit_session=explicit_session):
                sock = mock.Mock()
                report = {}
                routine_attempt = 0

                def respond(_sock, payload, **_kwargs):
                    nonlocal routine_attempt
                    payload = bytes(payload)
                    if payload == bytes.fromhex("10 03"):
                        return bytes.fromhex("50 03"), "POSITIVE"
                    routine_attempt += 1
                    if routine_attempt == 1:
                        return None, "NO_RESPONSE (timeout/empty after retries)"
                    return bytes.fromhex("7F 31 31"), "NEGATIVE"

                argv = [
                    "radar_acc", "FF00", "FF03", "--retries", "1", "--execute",
                    "--confirm-parked", "--pair", "6/14", "--conditions", "parked",
                ]
                if explicit_session:
                    argv.extend(
                        [
                            "--session", "03", "--confirm-session-change",
                            "--confirm-no-active-routine",
                        ]
                    )

                with (
                    mock.patch.object(routine_scan, "preflight", return_value=[]),
                    mock.patch.object(routine_scan.uds, "open_module_socket", return_value=sock),
                    mock.patch.object(routine_scan.uds, "drain"),
                    mock.patch.object(routine_scan.uds, "request", side_effect=respond),
                    mock.patch.object(routine_scan.time, "sleep"),
                    mock.patch.object(routine_scan.canbus, "restore_passive", return_value=True),
                    mock.patch.object(routine_scan, "append_result_checkpoint"),
                    mock.patch.object(
                        routine_scan,
                        "write_report",
                        side_effect=lambda _path, data: report.update(data),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    result = routine_scan.main(argv)

                self.assertEqual(result, 0)
                self.assertEqual(len(report["results"]), 4)
                self.assertEqual(report["transmit_counts"]["routine_results"], 5)
                self.assertEqual(report["results"][0]["attempt_count"], 2)

    def test_explicit_long_session_uses_bounded_tester_present(self):
        clock = [0.0]
        results = []
        counts = {}

        def monotonic():
            return clock[0]

        responses = {}
        keepalives = []

        def query(
            _sock,
            rid,
            _timeout,
            retries=0,
            request_attempts=None,
            responses_received=None,
        ):
            request_attempts["routine_results"] += 1
            responses_received["routine_results"] += 1
            clock[0] += 2.1
            return {
                "rid": f"{rid:04X}",
                "category": "out_of_range_current_session",
                "response_hex": "7F 31 31",
                "elapsed_s": 2.1,
            }

        with (
            mock.patch.object(routine_scan.time, "monotonic", side_effect=monotonic),
            mock.patch.object(routine_scan.time, "sleep"),
            mock.patch.object(routine_scan, "query_routine", side_effect=query),
            mock.patch.object(routine_scan.uds, "drain"),
            mock.patch.object(
                routine_scan.uds,
                "request",
                return_value=(bytes.fromhex("7E 00"), "POSITIVE"),
            ) as request,
        ):
            routine_scan.scan_routines(
                mock.Mock(), [0x0250, 0x0251], 0.75, 0, 1.0, results,
                keep_session=True, transmit_counts=counts,
                responses_received=responses,
                tester_present_results=keepalives,
            )

        request.assert_called_once_with(
            mock.ANY, bytes.fromhex("3E 00"), timeout=0.5, retries=0
        )
        self.assertEqual(counts, {"tester_present": 1, "routine_results": 2})
        self.assertEqual(responses, {"tester_present": 1, "routine_results": 2})
        self.assertEqual(keepalives[0]["category"], "positive_echo")
        self.assertTrue(keepalives[0]["validated_echo"])

    def test_response_pending_exhaustion_is_not_retried(self):
        results = []
        counts = {}
        pending = {
            "rid": "0251",
            "category": "timeout",
            "response_hex": None,
            "status": "NO_RESPONSE (responsePending deadline exceeded after 5s)",
        }
        def query_pending(*_args, request_attempts=None, **_kwargs):
            request_attempts["routine_results"] += 1
            return pending

        with (
            mock.patch.object(routine_scan, "query_routine", side_effect=query_pending) as query,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            routine_scan.scan_routines(
                mock.Mock(), [0x0251], 0.75, 2, 1.0, results, transmit_counts=counts
            )

        query.assert_called_once()
        self.assertEqual(counts["routine_results"], 1)
        self.assertEqual(results[0]["attempt_count"], 1)

    def test_receive_exception_counts_attempt_without_confirmed_response(self):
        attempts = {}
        responses = {}
        with (
            mock.patch.object(routine_scan.uds, "drain"),
            mock.patch.object(
                routine_scan.uds, "request", side_effect=OSError("receive fixture")
            ),
            self.assertRaisesRegex(OSError, "receive fixture"),
        ):
            routine_scan.query_routine(
                mock.Mock(),
                0x0251,
                0.5,
                request_attempts=attempts,
                responses_received=responses,
            )

        self.assertEqual(attempts["routine_results"], 1)
        self.assertNotIn("routine_results", responses)

    def test_bad_tester_present_is_persisted_and_marks_session_uncertain(self):
        sock = mock.Mock()
        report = {}

        def respond(_sock, payload, **_kwargs):
            payload = bytes(payload)
            if payload == bytes.fromhex("10 03"):
                return bytes.fromhex("50 03"), "POSITIVE"
            if payload == bytes.fromhex("3E 00"):
                return bytes.fromhex("7F 3E 22"), "NEGATIVE"
            raise AssertionError(f"unexpected payload {payload.hex()}")

        with (
            mock.patch.object(routine_scan, "preflight", return_value=[]),
            mock.patch.object(routine_scan.uds, "open_module_socket", return_value=sock),
            mock.patch.object(routine_scan.uds, "drain"),
            mock.patch.object(routine_scan.uds, "request", side_effect=respond),
            mock.patch.object(routine_scan.time, "sleep"),
            mock.patch.object(routine_scan, "TESTER_PRESENT_INTERVAL_S", 0.0),
            mock.patch.object(routine_scan.canbus, "restore_passive", return_value=True),
            mock.patch.object(routine_scan, "append_result_checkpoint"),
            mock.patch.object(
                routine_scan, "write_report", side_effect=lambda _path, data: report.update(data)
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "0251", "0251", "--session", "03",
                    "--confirm-session-change", "--confirm-no-active-routine", "--execute",
                    "--confirm-parked", "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 1)
        self.assertEqual(report["session_state"], "uncertain_after_tester_present_failure")
        self.assertEqual(report["tester_present_results"][0]["category"], "negative")
        self.assertFalse(report["tester_present_results"][0]["validated_echo"])
        self.assertEqual(report["request_attempts"]["session_control"], 1)
        self.assertEqual(report["responses_received"]["session_control"], 1)
        self.assertEqual(report["request_attempts"]["tester_present"], 1)
        self.assertEqual(report["responses_received"]["tester_present"], 1)
        self.assertEqual(report["request_attempts"]["routine_results"], 0)
        self.assertIn("explicit session is uncertain", report["fatal_error"])

    def test_jsonl_checkpoint_is_append_only_and_preserves_each_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routines.results.jsonl"
            first = {"rid": "0250", "category": "timeout"}
            second = {"rid": "0251", "category": "request_sequence_error_candidate"}

            routine_scan.append_result_checkpoint(str(path), first)
            routine_scan.append_result_checkpoint(str(path), second)

            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records, [first, second])

    def test_bad_session_echo_stops_before_any_routine_request_and_reports_failure(self):
        sock = mock.Mock()
        report = {}
        with (
            mock.patch.object(routine_scan, "preflight", return_value=[]),
            mock.patch.object(routine_scan.uds, "open_module_socket", return_value=sock),
            mock.patch.object(routine_scan.uds, "drain"),
            mock.patch.object(
                routine_scan.uds,
                "request",
                return_value=(bytes.fromhex("50 02"), "POSITIVE"),
            ) as request,
            mock.patch.object(routine_scan.canbus, "restore_passive", return_value=True),
            mock.patch.object(routine_scan, "append_result_checkpoint"),
            mock.patch.object(
                routine_scan, "write_report", side_effect=lambda _path, data: report.update(data)
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "0251", "0251", "--session", "03",
                    "--confirm-session-change", "--confirm-no-active-routine", "--execute",
                    "--confirm-parked", "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 1)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], bytes.fromhex("10 03"))
        self.assertEqual(report["results"], [])
        self.assertTrue(report["partial"])
        self.assertIn("exact 50 03 echo", report["fatal_error"])

    def test_failure_and_close_failure_still_restore_passive_and_preserve_partial_report(self):
        sock = mock.Mock()
        sock.close.side_effect = RuntimeError("close fixture")
        report = {}

        def partial_failure(_sock, _rids, _timeout, _retries, _rate, results, **_kwargs):
            results.append({"rid": "0251", "category": "timeout"})
            raise RuntimeError("scan fixture")

        with (
            mock.patch.object(routine_scan, "preflight", return_value=[]),
            mock.patch.object(routine_scan.uds, "open_module_socket", return_value=sock),
            mock.patch.object(routine_scan, "scan_routines", side_effect=partial_failure),
            mock.patch.object(routine_scan.canbus, "restore_passive", return_value=True) as restore,
            mock.patch.object(
                routine_scan, "write_report", side_effect=lambda _path, data: report.update(data)
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "0251", "0251", "--execute", "--confirm-parked",
                    "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 1)
        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can0", 500000)
        self.assertEqual(report["results"], [{"rid": "0251", "category": "timeout"}])
        self.assertTrue(report["partial"])
        self.assertIn("scan fixture", report["fatal_error"])
        self.assertIn("close fixture", report["fatal_error"])

    def test_keyboard_interrupt_preserves_partial_report_and_restores_passive(self):
        sock = mock.Mock()
        report = {}

        def interrupt(_sock, _rids, _timeout, _retries, _rate, results, **_kwargs):
            results.append({"rid": "0251", "category": "request_sequence_error_candidate"})
            raise KeyboardInterrupt

        with (
            mock.patch.object(routine_scan, "preflight", return_value=[]),
            mock.patch.object(routine_scan.uds, "open_module_socket", return_value=sock),
            mock.patch.object(routine_scan, "scan_routines", side_effect=interrupt),
            mock.patch.object(routine_scan.canbus, "restore_passive", return_value=True) as restore,
            mock.patch.object(
                routine_scan, "write_report", side_effect=lambda _path, data: report.update(data)
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "0251", "0251", "--execute", "--confirm-parked",
                    "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 130)
        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can0", 500000)
        self.assertTrue(report["interrupted"])
        self.assertTrue(report["partial"])
        self.assertEqual(len(report["results"]), 1)

    def test_sigterm_preserves_partial_report_and_restores_handlers_and_passive(self):
        sock = mock.Mock()
        report = {}
        installed = {}
        old_handlers = {
            routine_scan.signal.SIGTERM: mock.sentinel.old_term,
            routine_scan.signal.SIGHUP: mock.sentinel.old_hup,
        }

        def fake_signal(signum, handler):
            if callable(handler):
                installed[signum] = handler
            return old_handlers[signum]

        def interrupt(_sock, _rids, _timeout, _retries, _rate, results, **_kwargs):
            results.append({"rid": "0251", "category": "conditions_not_correct"})
            installed[routine_scan.signal.SIGTERM](routine_scan.signal.SIGTERM, None)

        with (
            mock.patch.object(routine_scan, "preflight", return_value=[]),
            mock.patch.object(
                routine_scan.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(routine_scan.diagnostic_safety, "release_channel_lock"),
            mock.patch.object(routine_scan.uds, "open_module_socket", return_value=sock),
            mock.patch.object(routine_scan, "scan_routines", side_effect=interrupt),
            mock.patch.object(routine_scan.canbus, "restore_passive", return_value=True) as restore,
            mock.patch.object(routine_scan.signal, "signal", side_effect=fake_signal) as set_signal,
            mock.patch.object(
                routine_scan,
                "write_report",
                side_effect=lambda _path, data: report.update(data),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "0251", "0251", "--execute", "--confirm-parked",
                    "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 130)
        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can0", 500000)
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["interruption_signal"], "SIGTERM")
        set_signal.assert_any_call(routine_scan.signal.SIGTERM, mock.sentinel.old_term)

    def test_first_signal_during_cleanup_cannot_skip_restore_or_unlock(self):
        sock = mock.Mock()
        report = {}
        current = {
            routine_scan.signal.SIGTERM: mock.sentinel.old_term,
            routine_scan.signal.SIGHUP: mock.sentinel.old_hup,
        }

        def fake_signal(signum, handler):
            previous = current[signum]
            current[signum] = handler
            return previous

        def signal_now(signum):
            current[signum](signum, None)

        sock.close.side_effect = lambda: signal_now(routine_scan.signal.SIGTERM)

        def restore(*_args):
            signal_now(routine_scan.signal.SIGHUP)
            return True

        def release(*_args):
            signal_now(routine_scan.signal.SIGTERM)

        with (
            mock.patch.object(routine_scan, "build_rids", return_value=[]),
            mock.patch.object(routine_scan, "preflight", return_value=[]),
            mock.patch.object(
                routine_scan.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(
                routine_scan.diagnostic_safety,
                "release_channel_lock",
                side_effect=release,
            ) as unlock,
            mock.patch.object(routine_scan.uds, "open_module_socket", return_value=sock),
            mock.patch.object(routine_scan.canbus, "restore_passive", side_effect=restore) as passive,
            mock.patch.object(routine_scan.signal, "signal", side_effect=fake_signal),
            mock.patch.object(
                routine_scan,
                "write_report",
                side_effect=lambda _path, data: report.update(data),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = routine_scan.main(
                [
                    "radar_acc", "0251", "0251", "--execute", "--confirm-parked",
                    "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 130)
        passive.assert_called_once_with("can0", 500000)
        unlock.assert_called_once_with(mock.sentinel.lock)
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["interruption_signal"], "SIGTERM")

    def test_rate_and_timeout_have_lower_and_upper_bounds(self):
        for option, value in (("--rate", "0.01"), ("--timeout", "5.01")):
            with self.subTest(option=option), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                result = routine_scan.main(["radar_acc", option, value])
            self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
