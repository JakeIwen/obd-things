import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from lib import canbus
from live_data import live_data
from projects.tpms import tpms_logger
from tools import signal_correlate, uds_send


MODULE = SimpleNamespace(
    key="test_ecu",
    name="Test ECU",
    channel="can9",
    bitrate=500000,
    txid=0x18DA10F1,
    rxid=0x18DAF110,
    addressing_mode="normal_29bits",
)


class ActiveDiagnosticLockTests(unittest.TestCase):
    def test_uds_send_holds_lock_until_socket_is_closed(self):
        events = []
        sock = mock.Mock()
        sock.close.side_effect = lambda: events.append("close")

        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            mock.patch.object(
                uds_send.diagnostic_safety,
                "acquire_channel_lock",
                side_effect=lambda channel: events.append("lock") or object(),
            ) as acquire,
            mock.patch.object(
                uds_send.diagnostic_safety,
                "release_channel_lock",
                side_effect=lambda _handle: events.append("unlock"),
            ),
            mock.patch.object(uds_send, "preflight", return_value=[]),
            mock.patch.object(
                uds_send.uds, "open_module_socket", side_effect=lambda _m, timeout: (
                    events.append("open") or sock
                )
            ),
            mock.patch.object(uds_send.uds, "drain"),
            mock.patch.object(
                uds_send.uds, "request", return_value=(bytes.fromhex("62 F1 87 01"), "positive")
            ),
            mock.patch.object(
                uds_send.canbus,
                "restore_passive",
                side_effect=lambda *_args: events.append("restore") or True,
            ),
            mock.patch("builtins.print"),
        ):
            result = uds_send.main([
                "test_ecu", "22", "F1", "87", "--execute", "--confirm-parked",
                "--pair", "6/14", "--conditions", "parked test",
            ])

        self.assertEqual(result, 0)
        acquire.assert_called_once_with("can9")
        self.assertEqual(events, ["lock", "open", "close", "restore", "unlock"])

    def test_uds_send_contention_does_not_open_socket(self):
        with (
            mock.patch.object(uds_send, "get", return_value=MODULE),
            mock.patch.object(uds_send, "preflight", return_value=[]),
            mock.patch.object(
                uds_send.diagnostic_safety,
                "acquire_channel_lock",
                side_effect=uds_send.diagnostic_safety.ChannelLockError("can9 busy"),
            ),
            mock.patch.object(uds_send.uds, "open_module_socket") as open_socket,
            mock.patch("builtins.print"),
        ):
            result = uds_send.main([
                "test_ecu", "22", "F1", "87", "--execute", "--confirm-parked",
                "--pair", "6/14", "--conditions", "parked test",
            ])
        self.assertEqual(result, 2)
        open_socket.assert_not_called()

    def test_signal_capture_holds_lock_for_socket_lifetime(self):
        events = []
        sock = mock.Mock()
        sock.close.side_effect = lambda: events.append("close")

        with (
            mock.patch.object(
                signal_correlate.diagnostic_safety,
                "acquire_channel_lock",
                side_effect=lambda channel: events.append("lock") or object(),
            ) as acquire,
            mock.patch.object(
                signal_correlate.diagnostic_safety,
                "release_channel_lock",
                side_effect=lambda _handle: events.append("unlock"),
            ),
            mock.patch.object(signal_correlate, "preflight", return_value=[]),
            mock.patch.object(
                signal_correlate.uds,
                "open_module_socket",
                side_effect=lambda _m, timeout: events.append("open") or sock,
            ),
            mock.patch.object(signal_correlate.uds, "drain"),
            mock.patch.object(
                signal_correlate.uds,
                "request",
                return_value=(bytes.fromhex("50 03"), "positive"),
            ),
            mock.patch.object(signal_correlate.time, "time", return_value=0.0),
            mock.patch.object(signal_correlate.time, "monotonic", return_value=0.0),
            mock.patch.object(
                signal_correlate.canbus,
                "restore_passive",
                side_effect=lambda *_args: events.append("restore") or True,
            ),
            mock.patch.object(signal_correlate, "_dump") as dump,
            mock.patch("builtins.print"),
        ):
            report = signal_correlate.capture(
                MODULE,
                [0xF187],
                0.0,
                "unused.json",
                pair="6/14",
                conditions="parked test",
                confirmed_parked=True,
                confirmed_session_change=True,
                confirmed_no_active_routine=True,
            )

        acquire.assert_called_once_with("can9")
        self.assertEqual(report["status"], "complete")
        self.assertEqual(dump.call_count, 2)
        self.assertEqual(events, ["lock", "open", "close", "restore", "unlock"])

    def test_live_view_releases_after_keyboard_interrupt(self):
        events = []
        link = mock.Mock()
        link.request_attempts = 0
        link.drop.side_effect = lambda: events.append("drop")
        with (
            mock.patch.object(live_data, "preflight", return_value=[]),
            mock.patch.object(
                live_data.diagnostic_safety,
                "acquire_channel_lock",
                side_effect=lambda channel: events.append(f"lock:{channel}") or object(),
            ),
            mock.patch.object(
                live_data.diagnostic_safety,
                "release_channel_lock",
                side_effect=lambda _handle: events.append("unlock"),
            ),
            mock.patch.object(live_data, "Link", return_value=link),
            mock.patch.object(live_data, "render", side_effect=KeyboardInterrupt),
            mock.patch.object(
                live_data.canbus,
                "restore_passive",
                side_effect=lambda *_args: events.append("restore") or True,
            ),
            mock.patch.object(live_data.sys.stdout, "write"),
            mock.patch.object(live_data.sys.stdout, "flush"),
            mock.patch("builtins.print"),
        ):
            result = live_data.run(
                MODULE,
                [],
                refresh_hz=1.0,
                argv=[
                    "--execute",
                    "--confirm-parked",
                    "--confirm-engine-off",
                    "--confirm-session-change",
                    "--confirm-no-active-routine",
                    "--pair",
                    "6/14",
                    "--conditions",
                    "parked test",
                    "--seconds",
                    "0.1",
                    "--max-requests",
                    "2",
                ],
            )

        self.assertEqual(result, 130)
        self.assertEqual(events, ["lock:can9", "drop", "restore", "unlock"])

    def test_tpms_polling_holds_lock_across_recovery_until_socket_close(self):
        events = []
        sock = mock.Mock()
        sock.close.side_effect = lambda: events.append("close")
        initial = canbus.InterfaceState(
            "can9", True, True, 500000, True, "ERROR-ACTIVE", 0
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(tpms_logger, "CSV_PATH", os.path.join(directory, "drive.csv")),
                mock.patch.object(tpms_logger, "get", return_value=MODULE),
                mock.patch.object(
                    tpms_logger, "_locked_start_state", return_value=initial
                ),
                mock.patch.object(
                    tpms_logger,
                    "_arm_iface_locked",
                    side_effect=lambda *_args: events.append("arm") or True,
                ),
                mock.patch.object(
                    tpms_logger, "_active_session_gates_hold", return_value=True
                ),
                mock.patch.object(
                    tpms_logger.diagnostic_safety,
                    "acquire_channel_lock",
                    side_effect=lambda channel: events.append(f"lock:{channel}") or object(),
                ),
                mock.patch.object(
                    tpms_logger.diagnostic_safety,
                    "release_channel_lock",
                    side_effect=lambda _handle: events.append("unlock"),
                ),
                mock.patch.object(
                    tpms_logger.uds,
                    "open_socket",
                    side_effect=lambda *_args, **_kwargs: events.append("open") or sock,
                ),
                mock.patch.object(
                    tpms_logger, "read_did_evidence", side_effect=KeyboardInterrupt
                ),
                mock.patch.object(
                    tpms_logger.canbus,
                    "restore_interface_state",
                    side_effect=lambda *_args, **_kwargs: (
                        events.append("restore") or True
                    ),
                ) as restore,
                mock.patch("builtins.print"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    tpms_logger.log_session(auto=False)

        restore.assert_called_once_with(initial, noninteractive=True)
        self.assertEqual(
            events,
            ["lock:can9", "arm", "open", "close", "restore", "unlock"],
        )

    def test_tpms_restoration_failure_latches_before_unlock(self):
        events = []
        sock = mock.Mock()
        sock.close.side_effect = lambda: events.append("close")
        initial = canbus.InterfaceState(
            "can9", True, True, 500000, True, "ERROR-ACTIVE", 0
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    tpms_logger, "CSV_PATH", os.path.join(directory, "drive.csv")
                ),
                mock.patch.object(tpms_logger, "get", return_value=MODULE),
                mock.patch.object(
                    tpms_logger, "_locked_start_state", return_value=initial
                ),
                mock.patch.object(
                    tpms_logger,
                    "_arm_iface_locked",
                    side_effect=lambda *_args: events.append("arm") or True,
                ),
                mock.patch.object(
                    tpms_logger, "_active_session_gates_hold", return_value=True
                ),
                mock.patch.object(
                    tpms_logger.diagnostic_safety,
                    "acquire_channel_lock",
                    side_effect=lambda channel: (
                        events.append(f"lock:{channel}") or object()
                    ),
                ),
                mock.patch.object(
                    tpms_logger.diagnostic_safety,
                    "release_channel_lock",
                    side_effect=lambda _handle: events.append("unlock"),
                ),
                mock.patch.object(
                    tpms_logger.uds,
                    "open_socket",
                    side_effect=lambda *_args, **_kwargs: (
                        events.append("open") or sock
                    ),
                ),
                mock.patch.object(
                    tpms_logger, "read_did_evidence", side_effect=KeyboardInterrupt
                ),
                mock.patch.object(
                    tpms_logger.canbus,
                    "restore_interface_state",
                    side_effect=lambda *_args, **_kwargs: (
                        events.append("restore") or False
                    ),
                ),
                mock.patch.object(
                    tpms_logger,
                    "_latch_restoration_failure",
                    side_effect=lambda _channel: events.append("latch"),
                ) as latch,
                mock.patch("builtins.print"),
            ):
                with self.assertRaises(tpms_logger.RestorationFailed):
                    tpms_logger.log_session(auto=False)

        latch.assert_called_once_with("can9")
        self.assertEqual(
            events,
            [
                "lock:can9",
                "arm",
                "open",
                "close",
                "restore",
                "latch",
                "unlock",
            ],
        )


if __name__ == "__main__":
    unittest.main()
