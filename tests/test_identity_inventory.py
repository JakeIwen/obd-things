import contextlib
import io
import json
import unittest
from unittest import mock

from tools import identity_inventory


class IdentitySelectionTests(unittest.TestCase):
    def test_default_set_is_bounded_and_excludes_vin(self):
        args = identity_inventory.parser().parse_args(["radar_acc"])

        selected = identity_inventory.selected_dids(args)

        self.assertEqual(len(selected), len(identity_inventory.IDENTITY_DIDS))
        self.assertNotIn(identity_inventory.VIN_DID, [did for did, _ in selected])
        self.assertIn(0xF1A5, [did for did, _ in selected])

    def test_vin_requires_explicit_option(self):
        args = identity_inventory.parser().parse_args(["radar_acc", "--include-vin"])

        selected = identity_inventory.selected_dids(args)

        self.assertIn(identity_inventory.VIN_DID, [did for did, _ in selected])

    def test_explicit_dids_replace_default_set(self):
        args = identity_inventory.parser().parse_args(
            ["radar_acc", "--did", "F187", "--did", "F191"]
        )

        self.assertEqual(
            identity_inventory.selected_dids(args),
            [(0xF187, "operator_supplied"), (0xF191, "operator_supplied")],
        )


class IdentityResponseTests(unittest.TestCase):
    def test_positive_response_extracts_data(self):
        category, data = identity_inventory.classify_identity_response(
            0xF187, bytes.fromhex("62 F1 87 36 38 35")
        )

        self.assertEqual(category, "positive")
        self.assertEqual(data, b"685")

    def test_negative_and_timeout_are_distinct(self):
        self.assertEqual(
            identity_inventory.classify_identity_response(0xF187, bytes.fromhex("7F 22 31"))[0],
            "negative",
        )
        self.assertEqual(identity_inventory.classify_identity_response(0xF187, None)[0], "timeout")
        self.assertEqual(
            identity_inventory.classify_identity_response(
                0xF187, bytes.fromhex("7F 19 31")
            )[0],
            "unexpected",
        )

    def test_masks_vin_embedded_in_composite_identity_data(self):
        raw = b"PART123 " + b"1M8GDM9AXKP042788" + b" SOFTWARE"

        masked = identity_inventory.mask_embedded_vins(raw)

        self.assertNotIn(b"1M8GDM9AXKP042788", masked)
        self.assertIn(b"1M8GDM9AXKP######", masked)
        self.assertEqual(len(masked), len(raw))

    def test_query_redacts_embedded_vin_from_every_output_view(self):
        full_vin = b"1M8GDM9AXKP042788"
        response = bytes.fromhex("62 F1 A0") + b"PART " + full_vin + b" END"
        with (
            mock.patch.object(identity_inventory.uds, "drain"),
            mock.patch.object(
                identity_inventory.uds,
                "request",
                return_value=(response, "POSITIVE"),
            ),
        ):
            result = identity_inventory.query_identity(mock.Mock(), 0xF1A0, "composite", 1.0)

        serialized = json.dumps(result)
        self.assertNotIn(full_vin.decode(), serialized)
        self.assertIn("PART 1M8GDM9AXKP###### END", result["ascii"])
        self.assertTrue(result["vin_redacted"])

    def test_direct_f190_redaction_does_not_depend_on_checksum(self):
        response = bytes.fromhex("62 F1 90") + b"ABCDEFGHIJKLMN123"

        safe, redacted = identity_inventory.redact_response_vins(0xF190, response)

        self.assertTrue(redacted)
        self.assertEqual(safe[3:], b"ABCDEFGHIJK######")

    def test_direct_f190_mask_does_not_reintroduce_a_trailing_embedded_vin(self):
        direct = b"ABCDEFGHIJKLMN123"
        embedded = b"1M8GDM9AXKP042788"
        response = bytes.fromhex("62 F1 90") + direct + b"/" + embedded

        safe, redacted = identity_inventory.redact_response_vins(0xF190, response)

        self.assertTrue(redacted)
        self.assertNotIn(direct, safe)
        self.assertNotIn(embedded, safe)
        self.assertIn(b"ABCDEFGHIJK######", safe)
        self.assertIn(b"1M8GDM9AXKP######", safe)


class IdentityCliSafetyTests(unittest.TestCase):
    def test_dry_run_never_preflights_or_opens_socket(self):
        with (
            mock.patch.object(identity_inventory, "preflight") as preflight,
            mock.patch.object(identity_inventory.uds, "open_module_socket") as open_socket,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = identity_inventory.main(["radar_acc"])

        self.assertEqual(result, 0)
        preflight.assert_not_called()
        open_socket.assert_not_called()

    def test_execute_requires_recorded_conditions(self):
        with (
            mock.patch.object(identity_inventory, "preflight") as preflight,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = identity_inventory.main(["radar_acc", "--execute"])

        self.assertEqual(result, 2)
        preflight.assert_not_called()

    def test_close_failure_still_restores_passive(self):
        sock = mock.Mock()
        sock.close.side_effect = RuntimeError("close failed")
        with (
            mock.patch.object(identity_inventory, "preflight", return_value=[]),
            mock.patch.object(identity_inventory, "selected_dids", return_value=[]),
            mock.patch.object(identity_inventory.uds, "open_module_socket", return_value=sock),
            mock.patch.object(identity_inventory.canbus, "restore_passive", return_value=True) as restore,
            mock.patch.object(identity_inventory, "write_report"),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = identity_inventory.main(
                [
                    "rf_hub", "--execute", "--confirm-parked", "--pair", "6/14",
                    "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 1)
        restore.assert_called_once_with("can0", 500000)

    def test_sigterm_preserves_report_closes_socket_and_restores_passive(self):
        sock = mock.Mock()
        report = {}
        installed = {}
        old_handlers = {
            identity_inventory.signal.SIGTERM: mock.sentinel.old_term,
            identity_inventory.signal.SIGHUP: mock.sentinel.old_hup,
        }

        def fake_signal(signum, handler):
            if callable(handler):
                installed[signum] = handler
            return old_handlers[signum]

        def interrupt_query(*_args, **_kwargs):
            installed[identity_inventory.signal.SIGTERM](
                identity_inventory.signal.SIGTERM, None
            )

        with (
            mock.patch.object(identity_inventory, "preflight", return_value=[]),
            mock.patch.object(
                identity_inventory.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(identity_inventory.diagnostic_safety, "release_channel_lock"),
            mock.patch.object(identity_inventory.uds, "open_module_socket", return_value=sock),
            mock.patch.object(identity_inventory, "query_identity", side_effect=interrupt_query),
            mock.patch.object(
                identity_inventory.canbus, "restore_passive", return_value=True
            ) as restore,
            mock.patch.object(identity_inventory.signal, "signal", side_effect=fake_signal) as set_signal,
            mock.patch.object(
                identity_inventory,
                "write_report",
                side_effect=lambda _path, data: report.update(data),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = identity_inventory.main(
                [
                    "radar_acc", "--did", "F187", "--execute", "--confirm-parked",
                    "--pair", "6/14", "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 130)
        sock.close.assert_called_once_with()
        restore.assert_called_once_with("can0", 500000)
        self.assertTrue(report["interrupted"])
        self.assertTrue(report["partial"])
        self.assertEqual(report["interruption_signal"], "SIGTERM")
        set_signal.assert_any_call(
            identity_inventory.signal.SIGTERM, mock.sentinel.old_term
        )

    def test_first_signal_during_cleanup_cannot_skip_restore_or_unlock(self):
        sock = mock.Mock()
        report = {}
        current = {
            identity_inventory.signal.SIGTERM: mock.sentinel.old_term,
            identity_inventory.signal.SIGHUP: mock.sentinel.old_hup,
        }

        def fake_signal(signum, handler):
            previous = current[signum]
            current[signum] = handler
            return previous

        def signal_now(signum):
            current[signum](signum, None)

        sock.close.side_effect = lambda: signal_now(identity_inventory.signal.SIGTERM)

        def restore(*_args):
            signal_now(identity_inventory.signal.SIGHUP)
            return True

        def release(*_args):
            signal_now(identity_inventory.signal.SIGTERM)

        with (
            mock.patch.object(identity_inventory, "preflight", return_value=[]),
            mock.patch.object(identity_inventory, "selected_dids", return_value=[]),
            mock.patch.object(
                identity_inventory.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(
                identity_inventory.diagnostic_safety,
                "release_channel_lock",
                side_effect=release,
            ) as unlock,
            mock.patch.object(identity_inventory.uds, "open_module_socket", return_value=sock),
            mock.patch.object(identity_inventory.canbus, "restore_passive", side_effect=restore) as passive,
            mock.patch.object(identity_inventory.signal, "signal", side_effect=fake_signal),
            mock.patch.object(
                identity_inventory,
                "write_report",
                side_effect=lambda _path, data: report.update(data),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = identity_inventory.main(
                [
                    "radar_acc", "--execute", "--confirm-parked", "--pair", "6/14",
                    "--conditions", "parked",
                ]
            )

        self.assertEqual(result, 130)
        passive.assert_called_once_with("can0", 500000)
        unlock.assert_called_once_with(mock.sentinel.lock)
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["interruption_signal"], "SIGTERM")

    def test_receive_exception_is_still_counted_as_an_attempt(self):
        report = {}
        with (
            mock.patch.object(identity_inventory, "preflight", return_value=[]),
            mock.patch.object(
                identity_inventory.uds, "open_module_socket", return_value=mock.Mock()
            ),
            mock.patch.object(identity_inventory.uds, "drain"),
            mock.patch.object(identity_inventory.uds, "request", side_effect=OSError("receive failed")),
            mock.patch.object(identity_inventory.canbus, "restore_passive", return_value=True),
            mock.patch.object(
                identity_inventory,
                "write_report",
                side_effect=lambda _path, data: report.update(data),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = identity_inventory.main(
                [
                    "radar_acc", "--did", "F187", "--execute", "--confirm-parked",
                    "--pair", "6/14", "--conditions", "parked",
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
                result = identity_inventory.main(["radar_acc", option, value])
            self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
