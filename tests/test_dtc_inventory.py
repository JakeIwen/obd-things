import contextlib
import io
import unittest
from unittest import mock

from tools import dtc_inventory


class DtcDecodeTests(unittest.TestCase):
    def test_count_response(self):
        parsed = dtc_inventory.parse_positive_response(
            bytes.fromhex("19 01 FF"), bytes.fromhex("59 01 FF 01 00 08")
        )

        self.assertEqual(parsed["status_availability_mask"], "FF")
        self.assertEqual(parsed["dtc_format_identifier"], "01")
        self.assertEqual(parsed["dtc_count"], 8)

    def test_dtc_records_have_raw_fca_and_status_views(self):
        parsed = dtc_inventory.parse_positive_response(
            bytes.fromhex("19 02 FF"), bytes.fromhex("59 02 FF 55 03 31 8F")
        )

        self.assertEqual(parsed["dtcs"][0]["raw_dtc"], "550331")
        self.assertEqual(parsed["dtcs"][0]["fca_display"], "C1503-31")
        self.assertIn("confirmed", parsed["dtcs"][0]["status_flags"])
        self.assertIn("warning_indicator_requested", parsed["dtcs"][0]["status_flags"])

    def test_snapshot_identifier_records(self):
        parsed = dtc_inventory.parse_positive_response(
            bytes.fromhex("19 03"), bytes.fromhex("59 03 55 03 31 01")
        )

        self.assertEqual(parsed["snapshots"][0]["raw_dtc"], "550331")
        self.assertEqual(parsed["snapshots"][0]["snapshot_record"], "01")

    def test_query_requires_matching_positive_subfunction(self):
        sock = mock.Mock()
        with (
            mock.patch.object(dtc_inventory.uds, "drain"),
            mock.patch.object(
                dtc_inventory.uds,
                "request",
                return_value=(bytes.fromhex("59 03"), "POSITIVE"),
            ),
        ):
            result = dtc_inventory.query(sock, "dtcs", bytes.fromhex("19 02 FF"), 1.0)

        self.assertEqual(result["category"], "unexpected")


class DtcCliSafetyTests(unittest.TestCase):
    def test_request_set_contains_no_clear_service(self):
        requests = (*dtc_inventory.DEFAULT_REQUESTS, dtc_inventory.SUPPORTED_DTCS_REQUEST)
        self.assertTrue(all(payload[0] == 0x19 for _, payload in requests))
        self.assertTrue(all(payload[0] != 0x14 for _, payload in requests))

    def test_supported_dtc_catalog_is_opt_in(self):
        default_args = dtc_inventory.parser().parse_args(["rf_hub"])
        expanded_args = dtc_inventory.parser().parse_args(["rf_hub", "--include-supported"])

        self.assertNotIn(bytes.fromhex("19 0A"), [p for _, p in dtc_inventory.selected_requests(default_args)])
        self.assertIn(bytes.fromhex("19 0A"), [p for _, p in dtc_inventory.selected_requests(expanded_args)])

    def test_dry_run_does_not_preflight_or_open_socket(self):
        with (
            mock.patch.object(dtc_inventory, "preflight") as preflight,
            mock.patch.object(dtc_inventory.uds, "open_module_socket") as open_socket,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = dtc_inventory.main(["rf_hub"])

        self.assertEqual(result, 0)
        preflight.assert_not_called()
        open_socket.assert_not_called()

    def test_sighup_preserves_report_closes_socket_and_restores_passive(self):
        sock = mock.Mock()
        report = {}
        installed = {}
        old_handlers = {
            dtc_inventory.signal.SIGINT: mock.sentinel.old_int,
            dtc_inventory.signal.SIGTERM: mock.sentinel.old_term,
            dtc_inventory.signal.SIGHUP: mock.sentinel.old_hup,
        }

        def fake_signal(signum, handler):
            if callable(handler):
                installed[signum] = handler
            return old_handlers[signum]

        def interrupt_query(*_args, **_kwargs):
            installed[dtc_inventory.signal.SIGHUP](dtc_inventory.signal.SIGHUP, None)

        with (
            mock.patch.object(dtc_inventory, "preflight", return_value=[]),
            mock.patch.object(
                dtc_inventory.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(dtc_inventory.diagnostic_safety, "release_channel_lock"),
            mock.patch.object(dtc_inventory.uds, "open_module_socket", return_value=sock),
            mock.patch.object(dtc_inventory, "query", side_effect=interrupt_query),
            mock.patch.object(dtc_inventory.canbus, "restore_passive", return_value=True) as restore,
            mock.patch.object(dtc_inventory.signal, "signal", side_effect=fake_signal) as set_signal,
            mock.patch.object(
                dtc_inventory,
                "write_report",
                side_effect=lambda _path, data: report.update(data),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = dtc_inventory.main(
                [
                    "rf_hub", "--execute", "--confirm-parked", "--pair", "6/14",
                    "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 130)
        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can0", 500000)
        self.assertTrue(report["interrupted"])
        self.assertTrue(report["partial"])
        self.assertEqual(report["interruption_signal"], "SIGHUP")
        set_signal.assert_any_call(dtc_inventory.signal.SIGHUP, mock.sentinel.old_hup)

    def test_first_signal_during_cleanup_cannot_skip_restore_or_unlock(self):
        sock = mock.Mock()
        report = {}
        current = {
            dtc_inventory.signal.SIGINT: mock.sentinel.old_int,
            dtc_inventory.signal.SIGTERM: mock.sentinel.old_term,
            dtc_inventory.signal.SIGHUP: mock.sentinel.old_hup,
        }

        def fake_signal(signum, handler):
            previous = current[signum]
            current[signum] = handler
            return previous

        def signal_now(signum):
            current[signum](signum, None)

        sock.close.side_effect = lambda: signal_now(dtc_inventory.signal.SIGHUP)

        def restore(*_args):
            signal_now(dtc_inventory.signal.SIGTERM)
            return True

        def release(*_args):
            signal_now(dtc_inventory.signal.SIGHUP)

        with (
            mock.patch.object(dtc_inventory, "preflight", return_value=[]),
            mock.patch.object(dtc_inventory, "selected_requests", return_value=[]),
            mock.patch.object(
                dtc_inventory.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(
                dtc_inventory.diagnostic_safety,
                "release_channel_lock",
                side_effect=release,
            ) as unlock,
            mock.patch.object(dtc_inventory.uds, "open_module_socket", return_value=sock),
            mock.patch.object(dtc_inventory.canbus, "restore_passive", side_effect=restore) as passive,
            mock.patch.object(dtc_inventory.signal, "signal", side_effect=fake_signal),
            mock.patch.object(
                dtc_inventory,
                "write_report",
                side_effect=lambda _path, data: report.update(data),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = dtc_inventory.main(
                [
                    "rf_hub", "--execute", "--confirm-parked", "--pair", "6/14",
                    "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 130)
        passive.assert_called_once_with("can0", 500000)
        unlock.assert_called_once_with(mock.sentinel.lock)
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["interruption_signal"], "SIGHUP")

    def test_receive_exception_is_still_counted_as_an_attempt(self):
        report = {}
        with (
            mock.patch.object(dtc_inventory, "preflight", return_value=[]),
            mock.patch.object(dtc_inventory.uds, "open_module_socket", return_value=mock.Mock()),
            mock.patch.object(dtc_inventory.uds, "drain"),
            mock.patch.object(dtc_inventory.uds, "request", side_effect=OSError("receive failed")),
            mock.patch.object(dtc_inventory.canbus, "restore_passive", return_value=True),
            mock.patch.object(
                dtc_inventory,
                "write_report",
                side_effect=lambda _path, data: report.update(data),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = dtc_inventory.main(
                [
                    "rf_hub", "--execute", "--confirm-parked", "--pair", "6/14",
                    "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 1)
        self.assertEqual(report["request_attempts"], 1)
        self.assertEqual(report["responses_received"], 0)
        self.assertEqual(report["results"], [])
        self.assertTrue(report["partial"])

    def test_rate_and_timeout_have_lower_and_upper_bounds(self):
        for option, value in (("--rate", "0.01"), ("--timeout", "5.01")):
            with self.subTest(option=option), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                result = dtc_inventory.main(["rf_hub", option, value])
            self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
