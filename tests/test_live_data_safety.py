import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from live_data import live_data


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
METRICS = [live_data.Metric(0xF187, "identity", lambda data: data[0], 1.0, "raw")]
LIVE_ARGS = [
    "--execute",
    "--confirm-parked",
    "--confirm-engine-off",
    "--confirm-session-change",
    "--confirm-no-active-routine",
    "--pair",
    "6/14",
    "--conditions",
    "parked fixture",
    "--seconds",
    "1",
]


class LiveDataLinkTests(unittest.TestCase):
    def test_shared_bounded_request_requires_exact_session_and_did_echo(self):
        sock = mock.Mock()
        responses = [
            (bytes.fromhex("50 03 00 32 01 F4"), "POSITIVE"),
            (bytes.fromhex("62 F1 87 31 32"), "POSITIVE"),
        ]
        with (
            mock.patch.object(live_data.uds, "open_module_socket", return_value=sock),
            mock.patch.object(live_data.uds, "drain") as drain,
            mock.patch.object(live_data.uds, "request", side_effect=responses) as request,
            mock.patch.object(live_data.time, "sleep"),
            mock.patch.object(live_data.time, "monotonic", return_value=0.0),
            mock.patch.object(live_data.uds, "bring_up_can") as bring_up,
        ):
            link = live_data.Link(MODULE, request_rate=5.0, max_requests=3)
            data = link.read_did(0xF187)

        self.assertEqual(data, bytes.fromhex("31 32"))
        self.assertEqual(link.request_attempts, 2)
        self.assertEqual(request.call_args_list[0].args[1], bytes.fromhex("10 03"))
        self.assertEqual(request.call_args_list[1].args[1], bytes.fromhex("22 F1 87"))
        self.assertEqual(drain.call_count, 2)
        bring_up.assert_not_called()

    def test_bad_session_echo_closes_and_aborts_without_rearming(self):
        sock = mock.Mock()
        with (
            mock.patch.object(live_data.uds, "open_module_socket", return_value=sock),
            mock.patch.object(live_data.uds, "drain"),
            mock.patch.object(
                live_data.uds,
                "request",
                return_value=(bytes.fromhex("50 02"), "UNEXPECTED"),
            ),
            mock.patch.object(live_data.uds, "bring_up_can") as bring_up,
        ):
            link = live_data.Link(MODULE)
            with self.assertRaisesRegex(live_data.LinkError, "exact 50 03"):
                link.read_did(0xF187)

        sock.close.assert_called_once_with()
        bring_up.assert_not_called()

    def test_wrong_did_echo_drops_link_instead_of_misassociating(self):
        sock = mock.Mock()
        with (
            mock.patch.object(live_data.uds, "open_module_socket", return_value=sock),
            mock.patch.object(live_data.uds, "drain"),
            mock.patch.object(
                live_data.uds,
                "request",
                side_effect=[
                    (bytes.fromhex("50 03"), "POSITIVE"),
                    (bytes.fromhex("62 F1 88 31"), "POSITIVE"),
                ],
            ),
            mock.patch.object(live_data.time, "sleep"),
        ):
            link = live_data.Link(MODULE)
            with self.assertRaisesRegex(live_data.LinkError, "exact 62 F187"):
                link.read_did(0xF187)

        sock.close.assert_called_once_with()
        self.assertIsNone(link.sock)


class LiveDataCliSafetyTests(unittest.TestCase):
    def test_render_labels_target_cycle_and_request_cap(self):
        link = SimpleNamespace(
            m=MODULE,
            connected=False,
            request_rate=3.0,
            read_did=mock.Mock(),
        )
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            live_data.render(link, [], "fixture", 1.0, 0.5, 1)

        display = stdout.getvalue()
        self.assertIn("target cycle 2.0 Hz", display)
        self.assertIn("request cap 3/s", display)
        self.assertNotIn(" refresh ", display)

    def test_direct_view_is_dry_run_by_default(self):
        with (
            mock.patch.object(live_data, "preflight") as preflight,
            mock.patch.object(live_data.diagnostic_safety, "acquire_channel_lock") as lock,
            mock.patch.object(live_data.uds, "open_module_socket") as open_socket,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = live_data.run(MODULE, METRICS, argv=[])

        self.assertEqual(result, 0)
        preflight.assert_not_called()
        lock.assert_not_called()
        open_socket.assert_not_called()

    def test_service_or_drive_capture_preflight_error_prevents_lock(self):
        with (
            mock.patch.object(
                live_data, "preflight", return_value=["tpms-logger is active", "drive capture active"]
            ),
            mock.patch.object(live_data.diagnostic_safety, "acquire_channel_lock") as lock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(SystemExit, "tpms-logger.*drive capture"):
                live_data.run(MODULE, METRICS, argv=LIVE_ARGS)
        lock.assert_not_called()

    def test_keyboard_interrupt_closes_restores_and_releases(self):
        link = mock.Mock(request_attempts=0)
        lock_handle = object()
        with (
            mock.patch.object(live_data, "preflight", return_value=[]),
            mock.patch.object(
                live_data.diagnostic_safety, "acquire_channel_lock", return_value=lock_handle
            ),
            mock.patch.object(live_data.diagnostic_safety, "release_channel_lock") as release,
            mock.patch.object(live_data, "Link", return_value=link),
            mock.patch.object(live_data, "render", side_effect=KeyboardInterrupt),
            mock.patch.object(live_data.canbus, "restore_passive", return_value=True) as restore,
            mock.patch.object(live_data.sys.stdout, "write"),
            mock.patch.object(live_data.sys.stdout, "flush"),
            mock.patch("builtins.print"),
        ):
            result = live_data.run(MODULE, METRICS, argv=LIVE_ARGS)

        self.assertEqual(result, 130)
        link.drop.assert_called_once_with()
        restore.assert_called_once_with("can9", 500000)
        release.assert_called_once_with(lock_handle)

    def test_restore_failure_is_not_reported_as_success(self):
        link = mock.Mock(request_attempts=0)
        with (
            mock.patch.object(live_data, "preflight", return_value=[]),
            mock.patch.object(
                live_data.diagnostic_safety, "acquire_channel_lock", return_value=object()
            ),
            mock.patch.object(live_data.diagnostic_safety, "release_channel_lock"),
            mock.patch.object(live_data, "Link", return_value=link),
            mock.patch.object(live_data, "render", side_effect=KeyboardInterrupt),
            mock.patch.object(live_data.canbus, "restore_passive", return_value=False),
            mock.patch.object(live_data.sys.stdout, "write"),
            mock.patch.object(live_data.sys.stdout, "flush"),
            mock.patch("builtins.print"),
        ):
            with self.assertRaisesRegex(SystemExit, "restoration could not be verified"):
                live_data.run(MODULE, METRICS, argv=LIVE_ARGS)

    def test_deadline_clamps_refresh_sleep_to_remaining_duration(self):
        link = mock.Mock(request_attempts=0)
        monotonic_values = iter((0.0, 0.0, 0.0, 0.1, 0.2, 1.0))
        with (
            mock.patch.object(live_data, "preflight", return_value=[]),
            mock.patch.object(
                live_data.diagnostic_safety, "acquire_channel_lock", return_value=object()
            ),
            mock.patch.object(live_data.diagnostic_safety, "release_channel_lock"),
            mock.patch.object(live_data, "Link", return_value=link),
            mock.patch.object(live_data, "render"),
            mock.patch.object(live_data.time, "monotonic", side_effect=lambda: next(monotonic_values)),
            mock.patch.object(live_data.time, "sleep") as sleep,
            mock.patch.object(live_data.canbus, "restore_passive", return_value=True),
            mock.patch.object(live_data.sys.stdout, "write"),
            mock.patch.object(live_data.sys.stdout, "flush"),
            mock.patch("builtins.print"),
        ):
            result = live_data.run(
                MODULE,
                METRICS,
                argv=["10", *LIVE_ARGS, "--max-requests", "2"],
            )

        self.assertEqual(result, 0)
        sleep.assert_called_once_with(0.8)

    def test_first_signal_during_cleanup_cannot_skip_restore_or_unlock(self):
        link = mock.Mock(request_attempts=0)
        sig = live_data.diagnostic_safety.signal
        current = {
            sig.SIGTERM: mock.sentinel.old_term,
            sig.SIGHUP: mock.sentinel.old_hup,
        }

        def fake_signal(signum, handler):
            previous = current[signum]
            current[signum] = handler
            return previous

        def signal_now(signum):
            current[signum](signum, None)

        link.drop.side_effect = lambda: signal_now(sig.SIGTERM)

        def render_once(*_args):
            link.request_attempts = 1000

        def restore(*_args):
            signal_now(sig.SIGHUP)
            return True

        def release(*_args):
            signal_now(sig.SIGTERM)

        with (
            mock.patch.object(live_data, "preflight", return_value=[]),
            mock.patch.object(
                live_data.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.sentinel.lock,
            ),
            mock.patch.object(
                live_data.diagnostic_safety,
                "release_channel_lock",
                side_effect=release,
            ) as unlock,
            mock.patch.object(live_data.diagnostic_safety.signal, "signal", side_effect=fake_signal),
            mock.patch.object(live_data, "Link", return_value=link),
            mock.patch.object(live_data, "render", side_effect=render_once),
            mock.patch.object(live_data.time, "sleep"),
            mock.patch.object(live_data.canbus, "restore_passive", side_effect=restore) as passive,
            mock.patch.object(live_data.sys.stdout, "write"),
            mock.patch.object(live_data.sys.stdout, "flush"),
            mock.patch("builtins.print"),
        ):
            result = live_data.run(MODULE, METRICS, argv=LIVE_ARGS)

        self.assertEqual(result, 130)
        passive.assert_called_once_with("can9", 500000)
        unlock.assert_called_once_with(mock.sentinel.lock)


if __name__ == "__main__":
    unittest.main()
