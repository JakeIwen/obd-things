import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest import mock

from lib.modules import bind_channel
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
    def setUp(self):
        self.ownerships = []

        def acquire_route(module, **_kwargs):
            ownership = mock.Mock()
            ownership.route = SimpleNamespace(
                module=bind_channel(module, "can0"),
                channel="can0",
                pair="6/14",
                topology_fingerprint="fixture-topology",
            )
            ownership.manager = mock.Mock()
            ownership.release.return_value = True
            self.ownerships.append(ownership)
            return ownership

        acquire = mock.patch.object(
            dtc_inventory.can_runtime_route,
            "acquire_armed_module_route",
            side_effect=acquire_route,
        )
        revalidate = mock.patch.object(
            dtc_inventory.can_runtime_route,
            "revalidate_module_route",
        )
        inhibits = mock.patch.object(
            dtc_inventory.can_operation_state,
            "active_inhibits",
            return_value=(),
        )
        interface_state = mock.patch.object(
            dtc_inventory.canbus,
            "interface_state",
            side_effect=lambda channel: dtc_inventory.canbus.InterfaceState(
                channel, True, True, 500000, False, "ERROR-ACTIVE", 0, False
            ),
        )
        acquire.start()
        revalidate.start()
        inhibits.start()
        interface_state.start()
        self.addCleanup(interface_state.stop)
        self.addCleanup(inhibits.stop)
        self.addCleanup(revalidate.stop)
        self.addCleanup(acquire.stop)

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

    def test_runtime_route_holds_role_then_channel_and_reports_identity_source(self):
        ownership = mock.Mock()
        ownership.route = SimpleNamespace(
            module=bind_channel(dtc_inventory.get("rf_hub"), "can7"),
            channel="can7",
            pair="6/14",
            topology_fingerprint="topology-a",
        )
        ownership.manager = mock.Mock()
        ownership.release.return_value = True
        report = {}
        with (
            mock.patch.object(
                dtc_inventory.can_runtime_route,
                "acquire_armed_module_route",
                return_value=ownership,
            ) as acquire,
            mock.patch.object(dtc_inventory, "preflight", return_value=[]),
            mock.patch.object(dtc_inventory, "selected_requests", return_value=[]),
            mock.patch.object(
                dtc_inventory.uds, "open_module_socket", return_value=mock.Mock()
            ),
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
                    "rf_hub",
                    "--resolve-runtime",
                    "--execute",
                    "--confirm-parked",
                    "--pair",
                    "6/14",
                    "--conditions",
                    "parked",
                ]
            )

        self.assertEqual(result, 0)
        acquire.assert_called_once()
        ownership.release.assert_called_once_with()
        self.assertEqual(report["module"]["channel"], "can7")
        self.assertEqual(report["module"]["route_source"], "usb_serial_and_dev_id")
        self.assertEqual(report["module"]["expected_physical_pair"], "6/14")

    def test_runtime_route_rejects_wrong_asserted_physical_pair_before_can(self):
        with (
            mock.patch.object(
                dtc_inventory.can_runtime_route,
                "acquire_armed_module_route",
                side_effect=RuntimeError("asserted pair 3/11 does not match 6/14"),
            ),
            mock.patch.object(dtc_inventory, "preflight") as preflight,
            mock.patch.object(
                dtc_inventory.uds, "open_module_socket"
            ) as open_socket,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = dtc_inventory.main(
                [
                    "rf_hub",
                    "--resolve-runtime",
                    "--execute",
                    "--confirm-parked",
                    "--pair",
                    "3/11",
                    "--conditions",
                    "parked",
                ]
            )

        self.assertEqual(result, 2)
        preflight.assert_not_called()
        open_socket.assert_not_called()

    def test_runtime_route_honors_wildcard_inhibit_under_both_locks(self):
        ownership = mock.Mock()
        ownership.route = SimpleNamespace(
            module=bind_channel(dtc_inventory.get("rf_hub"), "can7"),
            channel="can7",
            pair="6/14",
            topology_fingerprint="topology-a",
        )
        ownership.manager = mock.Mock()
        ownership.release.return_value = True
        stderr = io.StringIO()
        with (
            mock.patch.object(
                dtc_inventory.can_runtime_route,
                "acquire_armed_module_route",
                return_value=ownership,
            ) as acquire,
            mock.patch.object(
                dtc_inventory.can_operation_state,
                "active_inhibits",
                return_value=(
                    {
                        "name": "vehicle-data-restoration-failed",
                        "channel": "*",
                    },
                ),
            ) as inhibits,
            mock.patch.object(dtc_inventory, "preflight") as preflight,
            mock.patch.object(
                dtc_inventory.uds, "open_module_socket"
            ) as open_socket,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            result = dtc_inventory.main(
                [
                    "rf_hub",
                    "--resolve-runtime",
                    "--execute",
                    "--confirm-parked",
                    "--pair",
                    "6/14",
                    "--conditions",
                    "parked",
                ]
            )

        self.assertEqual(result, 2)
        acquire.assert_called_once()
        inhibits.assert_called_once_with("can7")
        ownership.release.assert_called_once_with()
        self.assertIn("restoration-failed", stderr.getvalue())
        preflight.assert_not_called()
        open_socket.assert_not_called()

    def test_fd_enabled_interface_is_rejected_before_preflight_or_socket(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(
                dtc_inventory.canbus,
                "interface_state",
                return_value=dtc_inventory.canbus.InterfaceState(
                    channel="can0",
                    present=True,
                    up=True,
                    bitrate=500000,
                    listen_only=False,
                    controller_state="ERROR-ACTIVE",
                    restart_ms=0,
                    fd_enabled=True,
                ),
            ),
            mock.patch.object(dtc_inventory, "preflight") as preflight,
            mock.patch.object(
                dtc_inventory.uds, "open_module_socket"
            ) as open_socket,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            result = dtc_inventory.main(
                [
                    "rf_hub",
                    "--execute",
                    "--confirm-parked",
                    "--pair",
                    "6/14",
                    "--conditions",
                    "parked",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("FD off", stderr.getvalue())
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
            mock.patch.object(dtc_inventory.uds, "open_module_socket", return_value=sock),
            mock.patch.object(dtc_inventory, "query", side_effect=interrupt_query),
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
        self.ownerships[-1].release.assert_called_once_with()
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

        def release_route():
            signal_now(dtc_inventory.signal.SIGTERM)
            return True

        ownership = mock.Mock()
        ownership.route = SimpleNamespace(
            module=bind_channel(dtc_inventory.get("rf_hub"), "can0"),
            channel="can0",
            pair="6/14",
            topology_fingerprint="fixture",
        )
        ownership.manager = mock.Mock()
        ownership.release.side_effect = release_route

        with (
            mock.patch.object(dtc_inventory, "preflight", return_value=[]),
            mock.patch.object(dtc_inventory, "selected_requests", return_value=[]),
            mock.patch.object(
                dtc_inventory.can_runtime_route,
                "acquire_armed_module_route",
                return_value=ownership,
            ),
            mock.patch.object(dtc_inventory.uds, "open_module_socket", return_value=sock),
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
        ownership.release.assert_called_once_with()
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["interruption_signal"], "SIGHUP")

    def test_receive_exception_is_still_counted_as_an_attempt(self):
        report = {}
        with (
            mock.patch.object(dtc_inventory, "preflight", return_value=[]),
            mock.patch.object(dtc_inventory.uds, "open_module_socket", return_value=mock.Mock()),
            mock.patch.object(dtc_inventory.uds, "drain"),
            mock.patch.object(dtc_inventory.uds, "request", side_effect=OSError("receive failed")),
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
