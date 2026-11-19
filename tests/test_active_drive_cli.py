"""Closed helper CLI and parent-handshake ordering, without live I/O."""
import unittest
from unittest import mock

from projects.vehicle_data import active_drive
from tests.test_active_drive import TEST_CHANNEL, TEST_SERIAL, TEST_DEV_ID


class ActiveDriveCliTests(unittest.TestCase):
    def test_cli_is_closed_and_parent_failure_precedes_backend_construction(self):
        actions = {
            action.dest: action
            for action in active_drive.build_parser()._actions
        }
        self.assertEqual(
            set(actions),
            {
                "help",
                "channel",
                "expected_usb_serial",
                "expected_dev_id",
                "expected_parent_pid",
                "enable_oil_life", "enable_intake_ac_broadcasts", "enable_radar_service_alignment",
                "startup_grant_fd",
                "enable_drive_dtc", "drive_dtc_reservation",
                "drive_dtc_origin", "drive_dtc_first_due",
            },
        )
        self.assertIsNone(actions["startup_grant_fd"].default)
        self.assertFalse(actions["enable_oil_life"].default)
        self.assertIsNone(actions["channel"].choices)
        self.assertTrue(actions["channel"].required)
        self.assertTrue(actions["expected_usb_serial"].required)
        self.assertTrue(actions["expected_dev_id"].required)
        self.assertTrue(actions["expected_parent_pid"].required)

        with (
            mock.patch.object(
                active_drive,
                "_set_parent_death_signal",
                side_effect=RuntimeError("parent mismatch"),
            ),
            mock.patch.object(active_drive, "SystemBackend") as backend,
            mock.patch.object(active_drive, "run_active_session") as run_session,
        ):
            with self.assertRaisesRegex(SystemExit, "refused parent handshake"):
                active_drive.main(
                    [
                        "--channel",
                        TEST_CHANNEL,
                        "--expected-usb-serial",
                        TEST_SERIAL,
                        "--expected-dev-id",
                        hex(TEST_DEV_ID),
                        "--expected-parent-pid",
                        "4242",
                    ]
                )

        backend.assert_not_called()
        run_session.assert_not_called()

