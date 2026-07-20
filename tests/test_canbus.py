import subprocess
import unittest
from unittest import mock

from lib import canbus


PASSIVE_DETAILS = """\
3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 10
    link/can
    can <LISTEN-ONLY> state ERROR-ACTIVE (berr-counter tx 0 rx 0)
      bitrate 500000 sample-point 0.875
"""


def ip_details(stdout=PASSIVE_DETAILS, returncode=0):
    return subprocess.CompletedProcess(
        ["ip", "-details", "link", "show", "can0"], returncode, stdout, ""
    )


class PassiveRestoreTests(unittest.TestCase):
    def test_bring_up_passive_requires_command_success_and_matching_readback(self):
        with (
            mock.patch.object(canbus, "ip_up", return_value=True) as ip_up,
            mock.patch.object(canbus.subprocess, "run", return_value=ip_details()) as run,
        ):
            restored = canbus.bring_up_passive("can0", 500000)

        self.assertTrue(restored)
        ip_up.assert_called_once_with("can0", 500000, listen_only=True)
        run.assert_called_once_with(
            ["ip", "-details", "link", "show", "can0"], capture_output=True, text=True
        )

    def test_bring_up_passive_does_not_read_back_after_command_failure(self):
        with (
            mock.patch.object(canbus, "ip_up", return_value=False),
            mock.patch.object(canbus.subprocess, "run") as run,
        ):
            restored = canbus.bring_up_passive("can0", 500000)

        self.assertFalse(restored)
        run.assert_not_called()

    def test_bring_up_passive_fails_closed_on_each_invalid_readback(self):
        cases = {
            "readback command failed": ip_details(returncode=1),
            "interface down": ip_details(PASSIVE_DETAILS.replace(
                "<NOARP,UP,LOWER_UP,ECHO>", "<NOARP,ECHO>"
            )),
            "wrong bitrate": ip_details(PASSIVE_DETAILS.replace("bitrate 500000", "bitrate 125000")),
            "listen-only off": ip_details(PASSIVE_DETAILS.replace(" <LISTEN-ONLY>", "")),
            "bus off": ip_details(PASSIVE_DETAILS.replace("ERROR-ACTIVE", "BUS-OFF")),
            "missing controller state": ip_details(PASSIVE_DETAILS.replace(
                " state ERROR-ACTIVE (berr-counter tx 0 rx 0)", ""
            )),
        }

        for name, result in cases.items():
            with self.subTest(name=name):
                with (
                    mock.patch.object(canbus, "ip_up", return_value=True),
                    mock.patch.object(canbus.subprocess, "run", return_value=result),
                ):
                    self.assertFalse(canbus.bring_up_passive("can0", 500000))

    def test_bring_up_passive_fails_closed_on_readback_exception(self):
        with (
            mock.patch.object(canbus, "ip_up", return_value=True),
            mock.patch.object(canbus.subprocess, "run", side_effect=OSError("ip unavailable")),
        ):
            self.assertFalse(canbus.bring_up_passive("can0", 500000))

    def test_restore_passive_preserves_signature_and_verified_result(self):
        with mock.patch.object(canbus, "bring_up_passive", return_value=False) as bring_up:
            restored = canbus.restore_passive("can9", 125000)

        self.assertFalse(restored)
        bring_up.assert_called_once_with("can9", 125000)


class DetectBusCoordinationTests(unittest.TestCase):
    @staticmethod
    def _lock_patches(events):
        handle = object()

        def acquire(channel):
            events.append(("lock", channel))
            return handle

        def release(actual):
            if actual is not None:
                assert actual is handle
                events.append(("unlock", None))

        return (
            mock.patch.object(canbus.diagnostic_safety, "acquire_channel_lock", side_effect=acquire),
            mock.patch.object(canbus.diagnostic_safety, "release_channel_lock", side_effect=release),
        )

    def test_silent_detection_restores_500k_before_unlock_and_return(self):
        events = []
        acquire, release = self._lock_patches(events)

        def bring_up(_channel, bitrate):
            events.append(("bring_up", bitrate))
            return True

        def identify(_channel):
            events.append(("identify", None))
            return "silent"

        def restore(_channel, bitrate):
            events.append(("restore", bitrate))
            return True

        with (
            acquire,
            release,
            mock.patch.object(canbus, "bring_up_passive", side_effect=bring_up),
            mock.patch.object(canbus, "identify_bus", side_effect=identify),
            mock.patch.object(canbus, "restore_passive", side_effect=restore),
        ):
            result = canbus.detect_bus("can9")

        self.assertEqual(result, ("silent", 500000))
        self.assertEqual(
            events,
            [
                ("lock", "can9"),
                ("bring_up", 500000),
                ("identify", None),
                ("bring_up", 125000),
                ("identify", None),
                ("restore", 500000),
                ("unlock", None),
            ],
        )

    def test_detected_bus_keeps_detected_rate_but_still_unlocks(self):
        events = []
        acquire, release = self._lock_patches(events)
        with (
            acquire,
            release,
            mock.patch.object(canbus, "bring_up_passive", return_value=True) as bring_up,
            mock.patch.object(canbus, "identify_bus", side_effect=("silent", "b-can")),
            mock.patch.object(canbus, "restore_passive") as restore,
        ):
            self.assertEqual(canbus.detect_bus("can9"), ("b-can", 125000))

        self.assertEqual(
            bring_up.call_args_list,
            [mock.call("can9", 500000), mock.call("can9", 125000)],
        )
        restore.assert_not_called()
        self.assertEqual(events, [("lock", "can9"), ("unlock", None)])

    def test_probe_exception_restores_default_before_unlock(self):
        events = []
        acquire, release = self._lock_patches(events)

        def restore(_channel, bitrate):
            events.append(("restore", bitrate))
            return True

        with (
            acquire,
            release,
            mock.patch.object(canbus, "bring_up_passive", return_value=True),
            mock.patch.object(canbus, "identify_bus", side_effect=RuntimeError("probe failed")),
            mock.patch.object(canbus, "restore_passive", side_effect=restore),
            self.assertRaisesRegex(RuntimeError, "probe failed"),
        ):
            canbus.detect_bus("can9")

        self.assertEqual(
            events,
            [("lock", "can9"), ("restore", 500000), ("unlock", None)],
        )

    def test_silent_restore_failure_is_not_reported_as_success(self):
        events = []
        acquire, release = self._lock_patches(events)
        with (
            acquire,
            release,
            mock.patch.object(canbus, "bring_up_passive", return_value=True),
            mock.patch.object(canbus, "identify_bus", return_value="silent"),
            mock.patch.object(canbus, "restore_passive", return_value=False) as restore,
            self.assertRaises(canbus.PassiveRestoreError),
        ):
            canbus.detect_bus("can9")

        # The active-path restore failed, so the protected cleanup makes one more bounded attempt.
        self.assertEqual(restore.call_args_list, [mock.call("can9", 500000)] * 2)
        self.assertEqual(events[-1], ("unlock", None))


if __name__ == "__main__":
    unittest.main()
