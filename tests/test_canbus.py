import subprocess
import unittest
from unittest import mock

from lib import canbus


PASSIVE_DETAILS = """\
3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 10
    link/can
    can <LISTEN-ONLY> state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 0
      bitrate 500000 sample-point 0.875
"""


def ip_details(stdout=PASSIVE_DETAILS, returncode=0):
    return subprocess.CompletedProcess(
        ["ip", "-details", "link", "show", "can0"], returncode, stdout, ""
    )


class PassiveRestoreTests(unittest.TestCase):
    def test_noninteractive_ip_up_uses_sudo_n_for_both_link_changes(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(
                canbus.subprocess, "run", return_value=completed
            ) as run,
            mock.patch.object(canbus.time, "sleep"),
        ):
            self.assertTrue(
                canbus.ip_up(
                    "can0",
                    125000,
                    listen_only=True,
                    restart_ms=0,
                    noninteractive=True,
                )
            )

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    ["ip", "link", "show", "can0"], capture_output=True
                ),
                mock.call(
                    ["sudo", "-n", "ip", "link", "set", "can0", "down"],
                    capture_output=True,
                ),
                mock.call(
                    [
                        "sudo",
                        "-n",
                        "ip",
                        "link",
                        "set",
                        "can0",
                        "up",
                        "type",
                        "can",
                        "bitrate",
                        "125000",
                        "fd",
                        "off",
                        "listen-only",
                        "on",
                        "restart-ms",
                        "0",
                    ],
                    capture_output=True,
                ),
            ],
        )

    def test_interface_state_reports_classical_and_fd_mtu(self):
        with mock.patch.object(
            canbus.subprocess, "run", return_value=ip_details()
        ):
            self.assertIs(canbus.interface_state("can0").fd_enabled, False)

        fd_details = PASSIVE_DETAILS.replace("mtu 16", "mtu 72").replace(
            "<LISTEN-ONLY>", "<FD,LISTEN-ONLY>"
        )
        with mock.patch.object(
            canbus.subprocess, "run", return_value=ip_details(fd_details)
        ):
            self.assertIs(canbus.interface_state("can0").fd_enabled, True)

    def test_exact_restore_rejects_fd_enabled_or_unproved_initial_state(self):
        baseline = dict(
            channel="can0",
            present=True,
            up=True,
            bitrate=500000,
            listen_only=True,
            controller_state="ERROR-ACTIVE",
            restart_ms=0,
        )
        with mock.patch.object(canbus, "ip_up") as ip_up:
            self.assertFalse(
                canbus.restore_interface_state(
                    canbus.InterfaceState(**baseline, fd_enabled=True)
                )
            )
            self.assertFalse(
                canbus.restore_interface_state(
                    canbus.InterfaceState(**baseline, fd_enabled=None)
                )
            )
        ip_up.assert_not_called()


class IdentifyBusTests(unittest.TestCase):
    def test_canch_broadcast_signature_wins_over_forwarded_ccan_ids(self):
        ids = {
            0x0DA, 0x0DC, 0x106, 0x10E, 0x117,
            0x0EE,  # also present in the ordinary C-CAN signature
        }
        with mock.patch.object(canbus, "probe_ids", return_value=(ids, 0)):
            self.assertEqual(canbus.identify_bus("can9"), "can-ch")

    def test_one_canch_broadcast_id_is_not_enough_to_claim_grey(self):
        with mock.patch.object(canbus, "probe_ids", return_value=({0x0DA, 0x0EE}, 0)):
            self.assertEqual(canbus.identify_bus("can9"), "c-can")

    def test_canch_physical_diagnostic_exchange_is_decisive(self):
        with mock.patch.object(canbus, "probe_ids", return_value=({0x18DAF128}, 0)):
            self.assertEqual(canbus.identify_bus("can9"), "can-ch")

if __name__ == "__main__":
    unittest.main()
