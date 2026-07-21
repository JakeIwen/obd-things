import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.modules import MODULES, NORMAL_11BITS
from tools import ecu_discover


class CandidateTests(unittest.TestCase):
    def test_default_profile_is_bounded_and_matches_verified_registry_endpoints(self):
        candidates = ecu_discover.PROMASTER_CCAN_CANDIDATES

        expected_keys = (
            "tcm", "shifter", "radar_acc", "bcm_ccan", "cluster", "telematics", "rf_hub",
        )
        self.assertEqual(tuple(candidate.label for candidate in candidates), expected_keys)
        self.assertEqual(len(candidates), 7)
        self.assertIn("pcm", MODULES)
        # PCM is verified but needs its legacy 10 92 -> 1A 87 recipe, so it deliberately
        # remains outside this ordinary default-session 22 F187 profile.
        self.assertNotIn(0x18DA10F1, [candidate.txid for candidate in candidates])
        self.assertIn(0x18DA60F1, [candidate.txid for candidate in candidates])
        self.assertIn(0x18DAC6F1, [candidate.txid for candidate in candidates])
        for candidate in candidates:
            module = MODULES[candidate.label]
            self.assertEqual(candidate.txid, module.txid)
            self.assertEqual(candidate.rxid, module.rxid)
            self.assertEqual(candidate.bus, module.bus)
            self.assertEqual(candidate.bitrate, module.bitrate)
            self.assertEqual(candidate.addressing_mode, module.addressing_mode)
            self.assertEqual(candidate.source, ecu_discover.DEFAULT_PROFILE_SOURCE)
        self.assertEqual(ecu_discover.DISCOVERY_DID, 0xF187)
        self.assertNotEqual(ecu_discover.DISCOVERY_DID, 0xF190)  # never collect VIN for presence

    def test_custom_11_bit_pair_is_explicit_and_validated(self):
        args = argparse.Namespace(
            bus="b-can",
            bitrate=125000,
            addressing_mode=NORMAL_11BITS,
            channel="can9",
        )

        candidate = ecu_discover.custom_candidate("bcm_guess=760:768", args, 1)

        self.assertEqual(candidate.txid, 0x760)
        self.assertEqual(candidate.rxid, 0x768)
        self.assertEqual(candidate.addressing_mode, NORMAL_11BITS)
        self.assertEqual(candidate.bitrate, 125000)

    def test_custom_target_can_select_fixed_dlc_padding(self):
        args = ecu_discover.parser().parse_args(
            ["--target", "pcm=18DA10F1:18DAF110", "--tx-padding", "00"]
        )

        candidate = ecu_discover.build_targets(args)[0]

        self.assertEqual(candidate.tx_padding, 0x00)
        self.assertEqual(candidate.module("can0").txid, 0x18DA10F1)

    def test_padding_is_rejected_without_custom_target(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = ecu_discover.main(["--tx-padding", "00"])

        self.assertEqual(rc, 2)
        self.assertIn("only apply with --target", stderr.getvalue())

    def test_custom_11_bit_pair_rejects_extended_id(self):
        args = argparse.Namespace(
            bus="b-can",
            bitrate=125000,
            addressing_mode=NORMAL_11BITS,
            channel="can9",
        )

        with self.assertRaisesRegex(ValueError, "11-bit CAN identifier"):
            ecu_discover.custom_candidate("bad=18DA40F1:768", args, 1)

    def test_custom_pair_rejects_functional_broadcast_id(self):
        args = argparse.Namespace(
            bus="obd",
            bitrate=500000,
            addressing_mode=NORMAL_11BITS,
            channel="can0",
        )

        with self.assertRaisesRegex(argparse.ArgumentTypeError, "functional-broadcast"):
            ecu_discover.custom_candidate("functional=7DF:7E8", args, 1)

    def test_expanded_profile_is_all_physical_29_bit_target_addresses(self):
        args = ecu_discover.parser().parse_args(["--all-29bit-targets"])

        candidates = ecu_discover.build_targets(args)

        self.assertEqual(len(candidates), 255)
        self.assertEqual(candidates[0].txid, 0x18DA00F1)
        self.assertEqual(candidates[0].rxid, 0x18DAF100)
        self.assertEqual(candidates[-1].txid, 0x18DAFFF1)
        self.assertEqual(candidates[-1].rxid, 0x18DAF1FF)
        self.assertNotIn(0x18DAF1F1, [candidate.txid for candidate in candidates])

    def test_bounded_address_byte_range_does_not_repeat_other_targets(self):
        args = ecu_discover.parser().parse_args(["--address-byte-range", "F2", "FF"])

        candidates = ecu_discover.build_targets(args)

        self.assertEqual(len(candidates), 14)
        self.assertEqual(candidates[0].txid, 0x18DAF2F1)
        self.assertEqual(candidates[-1].txid, 0x18DAFFF1)

    def test_bounded_address_byte_range_excludes_tester_address(self):
        args = ecu_discover.parser().parse_args(["--address-byte-range", "F0", "F2"])

        candidates = ecu_discover.build_targets(args)

        self.assertEqual([candidate.txid for candidate in candidates], [0x18DAF0F1, 0x18DAF2F1])

    def test_reversed_address_byte_range_is_rejected(self):
        args = ecu_discover.parser().parse_args(["--address-byte-range", "FF", "F2"])

        with self.assertRaisesRegex(argparse.ArgumentTypeError, "START"):
            ecu_discover.build_targets(args)

    def test_tester_only_address_byte_range_is_rejected(self):
        args = ecu_discover.parser().parse_args(["--address-byte-range", "F1", "F1"])

        with self.assertRaisesRegex(argparse.ArgumentTypeError, "reserved tester"):
            ecu_discover.build_targets(args)

    def test_duplicate_custom_physical_pair_is_rejected(self):
        args = ecu_discover.parser().parse_args(
            ["--target", "first=760:768", "--target", "second=760:768",
             "--addressing-mode", "normal_11bits", "--bitrate", "125000"]
        )

        with self.assertRaisesRegex(argparse.ArgumentTypeError, "duplicate physical"):
            ecu_discover.build_targets(args)


class ResponseTests(unittest.TestCase):
    def test_classifies_positive_negative_and_timeout(self):
        request = bytes.fromhex("22 F1 87")

        self.assertEqual(
            ecu_discover.classify_response(request, bytes.fromhex("62 F1 87 31 32"), "POSITIVE"),
            "positive",
        )
        self.assertEqual(
            ecu_discover.classify_response(request, bytes.fromhex("7F 22 31"), "NEGATIVE"),
            "negative",
        )
        self.assertEqual(ecu_discover.classify_response(request, None, "NO_RESPONSE"), "timeout")

    def test_classifies_legacy_1a87_positive(self):
        self.assertEqual(
            ecu_discover.classify_response(
                bytes.fromhex("1A 87"), bytes.fromhex("5A 87 01 02"), "POSITIVE"
            ),
            "positive",
        )

    def test_classifies_exact_legacy_session_echo(self):
        self.assertEqual(
            ecu_discover.classify_session_response(0x92, bytes.fromhex("50 92")),
            "positive_echo",
        )
        self.assertEqual(
            ecu_discover.classify_session_response(0x92, bytes.fromhex("50 12")),
            "unexpected",
        )

    def test_session_preamble_must_validate_before_identity_probe(self):
        candidate = ecu_discover.normal_29bit_candidate("pcm", "PCM candidate", 0x10, "fixture")
        sock = mock.Mock()
        with (
            mock.patch.object(ecu_discover.uds, "open_module_socket", return_value=sock),
            mock.patch.object(ecu_discover.uds, "drain") as drain,
            mock.patch.object(
                ecu_discover.uds,
                "request",
                side_effect=[
                    (bytes.fromhex("50 92"), "POSITIVE"),
                    (bytes.fromhex("5A 87 01 02"), "POSITIVE"),
                ],
            ) as request,
        ):
            result = ecu_discover.scan_target(
                candidate,
                "can0",
                0.5,
                request_payload=bytes.fromhex("1A 87"),
                session=0x92,
            )

        self.assertEqual(result["session_category"], "positive_echo")
        self.assertEqual(result["category"], "positive")
        self.assertTrue(result["session_request_attempted"])
        self.assertTrue(result["request_attempted"])
        self.assertEqual(drain.call_count, 2)
        self.assertEqual(
            [call.args[1] for call in request.call_args_list],
            [bytes.fromhex("10 92"), bytes.fromhex("1A 87")],
        )
        sock.close.assert_called_once_with()

    def test_bad_session_echo_skips_identity_probe(self):
        candidate = ecu_discover.normal_29bit_candidate("pcm", "PCM candidate", 0x10, "fixture")
        sock = mock.Mock()
        with (
            mock.patch.object(ecu_discover.uds, "open_module_socket", return_value=sock),
            mock.patch.object(ecu_discover.uds, "drain"),
            mock.patch.object(
                ecu_discover.uds,
                "request",
                return_value=(bytes.fromhex("50 12"), "POSITIVE"),
            ) as request,
        ):
            result = ecu_discover.scan_target(
                candidate,
                "can0",
                0.5,
                request_payload=bytes.fromhex("1A 87"),
                session=0x92,
            )

        self.assertEqual(result["category"], "session_unexpected")
        self.assertFalse(result["request_attempted"])
        request.assert_called_once()
        sock.close.assert_called_once_with()

    def test_transport_receive_error_is_an_attempt_without_response_and_closes_socket(self):
        candidate = ecu_discover.PROMASTER_CCAN_CANDIDATES[0]
        sock = mock.Mock()
        with (
            mock.patch.object(ecu_discover.uds, "open_module_socket", return_value=sock),
            mock.patch.object(ecu_discover.uds, "drain") as drain,
            mock.patch.object(
                ecu_discover.uds, "request", side_effect=OSError("receive fixture")
            ),
        ):
            result = ecu_discover.scan_target(candidate, "can0", 0.5)

        self.assertEqual(result["category"], "transport_error")
        self.assertTrue(result["request_attempted"])
        self.assertFalse(result["response_received"])
        drain.assert_called_once_with(sock)
        sock.close.assert_called_once_with()


class CliSafetyTests(unittest.TestCase):
    def test_preflight_rejects_background_drive_capture(self):
        with (
            mock.patch.object(ecu_discover, "tpms_logger_active", return_value=False),
            mock.patch.object(ecu_discover, "service_active", return_value=True),
            mock.patch.object(ecu_discover.canbus, "iface_bitrate", return_value=500000),
            mock.patch.object(ecu_discover.canbus, "is_listen_only", return_value=False),
            mock.patch.object(ecu_discover.canbus, "controller_state", return_value="ERROR-ACTIVE"),
            mock.patch.object(ecu_discover.subprocess, "run", return_value=mock.Mock(returncode=0)),
        ):
            errors = ecu_discover.preflight("can0", 500000)

        self.assertTrue(any("promaster-drive-capture" in error for error in errors))

    def test_preflight_requires_known_error_active_controller_state(self):
        for state in (None, "ERROR-WARNING", "ERROR-PASSIVE", "BUS-OFF"):
            with self.subTest(state=state):
                with (
                    mock.patch.object(
                        ecu_discover, "tpms_logger_active", return_value=False
                    ),
                    mock.patch.object(ecu_discover, "service_active", return_value=False),
                    mock.patch.object(
                        ecu_discover.canbus, "iface_bitrate", return_value=500000
                    ),
                    mock.patch.object(
                        ecu_discover.canbus, "is_listen_only", return_value=False
                    ),
                    mock.patch.object(
                        ecu_discover.canbus, "controller_state", return_value=state
                    ),
                    mock.patch.object(
                        ecu_discover.subprocess, "run", return_value=mock.Mock(returncode=0)
                    ),
                ):
                    errors = ecu_discover.preflight("can0", 500000)

                self.assertTrue(any("expected ERROR-ACTIVE" in error for error in errors))

    def test_dry_run_does_not_preflight_or_open_can(self):
        with (
            mock.patch.object(ecu_discover, "preflight") as preflight,
            mock.patch.object(ecu_discover, "scan_targets") as scan_targets,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = ecu_discover.main([])

        self.assertEqual(result, 0)
        preflight.assert_not_called()
        scan_targets.assert_not_called()

    def test_pcm_legacy_session_plan_is_dry_run(self):
        with (
            mock.patch.object(ecu_discover, "preflight") as preflight,
            mock.patch.object(ecu_discover, "scan_targets") as scan_targets,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = ecu_discover.main(
                [
                    "--target", "pcm_candidate=18DA10F1:18DAF110",
                    "--probe", "legacy-1a87",
                    "--session", "92",
                ]
            )

        self.assertEqual(result, 0)
        preflight.assert_not_called()
        scan_targets.assert_not_called()

    def test_session_plan_is_restricted_and_live_confirmation_is_required(self):
        invalid_plans = (
            ["--session", "92"],
            [
                "--target", "pcm_candidate=18DA10F1:18DAF110",
                "--session", "92",
            ],
            [
                "--target", "one=18DA10F1:18DAF110",
                "--target", "two=18DA11F1:18DAF111",
                "--probe", "legacy-1a87",
                "--session", "92",
            ],
            ["--confirm-session-change"],
        )
        for argv in invalid_plans:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ecu_discover.main(argv), 2)

        with (
            mock.patch.object(ecu_discover, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = ecu_discover.main(
                [
                    "--target", "pcm_candidate=18DA10F1:18DAF110",
                    "--probe", "legacy-1a87",
                    "--session", "92",
                    "--execute", "--confirm-parked", "--confirm-custom-physical",
                    "--pair", "6/14", "--conditions", "fixture",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_session_probe_report_counts_preamble_and_identity_separately(self):
        report = {}

        def fake_scan(targets, _channel, _timeout, _rate, results, **kwargs):
            self.assertEqual(len(targets), 1)
            self.assertEqual(kwargs["session"], 0x92)
            results.append(
                {
                    "present": True,
                    "category": "positive",
                    "request_attempted": True,
                    "response_received": True,
                    "session_request_attempted": True,
                    "session_response_received": True,
                }
            )

        with (
            mock.patch.object(ecu_discover, "preflight", return_value=[]),
            mock.patch.object(
                ecu_discover.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(ecu_discover.diagnostic_safety, "release_channel_lock"),
            mock.patch.object(ecu_discover, "scan_targets", side_effect=fake_scan),
            mock.patch.object(ecu_discover.canbus, "restore_passive", return_value=True),
            mock.patch.object(ecu_discover, "report_path", return_value="/tmp/pcm-probe.json"),
            mock.patch.object(
                ecu_discover, "write_report", side_effect=lambda _path, payload: report.update(payload)
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = ecu_discover.main(
                [
                    "--target", "pcm_candidate=18DA10F1:18DAF110",
                    "--probe", "legacy-1a87", "--session", "92",
                    "--execute", "--confirm-parked", "--confirm-custom-physical",
                    "--confirm-session-change", "--pair", "6/14",
                    "--conditions", "fixture",
                ]
            )

        self.assertEqual(result, 0)
        self.assertTrue(report["diagnostic_session_control_sent"])
        self.assertEqual(report["requested_session"], "92")
        self.assertEqual(report["ecu_session"], "explicit_92")
        self.assertEqual(report["request_attempts"], 2)
        self.assertEqual(report["responses_received"], 2)

    def test_execute_requires_conditions_before_preflight(self):
        with (
            mock.patch.object(ecu_discover, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = ecu_discover.main(["--execute"])

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_expanded_execute_requires_explicit_confirmation(self):
        with (
            mock.patch.object(ecu_discover, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = ecu_discover.main(
                [
                    "--all-29bit-targets",
                    "--execute",
                    "--pair",
                    "6/14",
                    "--conditions",
                    "test fixture only",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_bounded_range_execute_requires_explicit_confirmation(self):
        with (
            mock.patch.object(ecu_discover, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = ecu_discover.main(
                [
                    "--address-byte-range", "F2", "FF",
                    "--execute",
                    "--confirm-parked",
                    "--pair", "6/14",
                    "--conditions", "test fixture only",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_custom_execute_requires_physical_assertion(self):
        with (
            mock.patch.object(ecu_discover, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = ecu_discover.main(
                [
                    "--target", "bcm=760:768",
                    "--addressing-mode", "normal_11bits",
                    "--execute",
                    "--pair", "3/11",
                    "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_nonfinite_timeout_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            result = ecu_discover.main(["--timeout", "nan"])

        self.assertEqual(result, 2)

    def test_unbounded_timeout_and_too_slow_rate_are_rejected(self):
        for argv in (["--timeout", "5.01"], ["--rate", "0.09"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                result = ecu_discover.main(argv)
            self.assertEqual(result, 2)

    def test_execute_restores_passive_and_reports_unexpected_failure(self):
        report = {}

        def remember_report(_path, payload):
            report.update(payload)

        with (
            mock.patch.object(ecu_discover, "preflight", return_value=[]),
            mock.patch.object(ecu_discover, "scan_targets", side_effect=RuntimeError("fixture failure")),
            mock.patch.object(ecu_discover.canbus, "restore_passive", return_value=True) as restore,
            mock.patch.object(ecu_discover, "report_path", return_value="/tmp/test-discovery.json"),
            mock.patch.object(ecu_discover, "write_report", side_effect=remember_report),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = ecu_discover.main(
                [
                    "--execute", "--confirm-parked", "--pair", "6/14",
                    "--conditions", "test fixture only",
                ]
            )

        self.assertEqual(result, 1)
        restore.assert_called_once_with("can0", 500000)
        self.assertTrue(report["restored_passive"])
        self.assertEqual(report["fatal_error"], "RuntimeError: fixture failure")
        self.assertFalse(report["functional_broadcast"])
        self.assertEqual(report["target_selection"], "promaster_ccan_verified_endpoints")

    def test_int_term_and_hup_publish_partial_report_and_cannot_break_cleanup(self):
        argv = [
            "--execute", "--confirm-parked", "--pair", "6/14",
            "--conditions", "test fixture only",
        ]
        for terminating_signal in (
            ecu_discover.signal.SIGINT,
            ecu_discover.signal.SIGTERM,
            ecu_discover.signal.SIGHUP,
        ):
            with self.subTest(signal=terminating_signal):
                installed = {}
                old_handlers = {
                    ecu_discover.signal.SIGINT: mock.sentinel.old_int,
                    ecu_discover.signal.SIGTERM: mock.sentinel.old_term,
                    ecu_discover.signal.SIGHUP: mock.sentinel.old_hup,
                }

                def fake_signal(signum, handler):
                    if callable(handler):
                        installed[signum] = handler
                    return old_handlers[signum]

                def interrupt_scan(_targets, _channel, _timeout, _rate, results, **_kwargs):
                    results.append(
                        {
                            "request_attempted": True,
                            "response_received": True,
                            "present": True,
                            "category": "positive",
                        }
                    )
                    installed[terminating_signal](terminating_signal, None)

                def restore_with_repeated_signals(_channel, _bitrate):
                    installed[terminating_signal](terminating_signal, None)
                    installed[terminating_signal](terminating_signal, None)
                    return True

                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "discovery.json"
                    with (
                        mock.patch.object(ecu_discover, "preflight", return_value=[]),
                        mock.patch.object(
                            ecu_discover.diagnostic_safety,
                            "acquire_channel_lock",
                            return_value=mock.sentinel.lock,
                        ),
                        mock.patch.object(
                            ecu_discover.diagnostic_safety, "release_channel_lock"
                        ) as release,
                        mock.patch.object(ecu_discover, "scan_targets", side_effect=interrupt_scan),
                        mock.patch.object(
                            ecu_discover.canbus,
                            "restore_passive",
                            side_effect=restore_with_repeated_signals,
                        ) as restore,
                        mock.patch.object(ecu_discover, "report_path", return_value=str(path)),
                        mock.patch.object(ecu_discover.signal, "signal", side_effect=fake_signal),
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        result = ecu_discover.main(argv)

                    payload = json.loads(path.read_text())

                self.assertEqual(result, 130)
                self.assertTrue(payload["partial"])
                self.assertTrue(payload["interrupted"])
                self.assertEqual(
                    payload["interruption_signal"],
                    ecu_discover.signal.Signals(terminating_signal).name,
                )
                self.assertTrue(payload["restored_passive"])
                self.assertEqual(payload["request_attempts"], 1)
                self.assertEqual(payload["responses_received"], 1)
                restore.assert_called_once_with("can0", 500000)
                release.assert_called_once_with(mock.sentinel.lock)


if __name__ == "__main__":
    unittest.main()
