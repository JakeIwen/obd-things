from dataclasses import replace
import pickle
import socket
from types import SimpleNamespace
import unittest
from unittest import mock

from lib import diagnostic_preflight, vin_redaction
from lib.canbus import InterfaceState
from tools import did_sweep, ecu_discover, identity_inventory


class DiagnosticPreflightTests(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(socket, "socket", side_effect=AssertionError("no sockets"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_reexports_and_callback_identity(self):
        for name in ("prearm_conflict_errors", "active_interface_errors", "preflight"):
            function = getattr(diagnostic_preflight, name)
            self.assertIs(getattr(ecu_discover, name), function)
            self.assertEqual(function.__module__, "tools.ecu_discover")
            self.assertIs(pickle.loads(pickle.dumps(function)), function)

    def test_prearm_sudo_call_and_failures(self):
        for code in (0, 1, 127):
            with self.subTest(code=code), mock.patch.object(
                diagnostic_preflight.subprocess, "run", return_value=SimpleNamespace(returncode=code)
            ) as run:
                self.assertEqual(diagnostic_preflight.prearm_conflict_errors(), [] if code == 0 else [
                    "noninteractive sudo is unavailable; arm/restoration cannot be guaranteed"
                ])
                run.assert_called_once_with(["sudo", "-n", "true"], capture_output=True)

    def test_preflight_sudo_then_interface_and_ordered_errors(self):
        state = InterfaceState("can9", True, True, 125000, True, None, 100, True)
        events = []

        def sudo(*args, **kwargs):
            events.append("sudo")
            return SimpleNamespace(returncode=1)

        def interface(channel):
            events.append(("interface", channel))
            return state

        with mock.patch.object(diagnostic_preflight.subprocess, "run", side_effect=sudo), \
             mock.patch.object(diagnostic_preflight.canbus, "interface_state", side_effect=interface):
            errors = diagnostic_preflight.preflight("can9", 500000)
        self.assertEqual(events, ["sudo", ("interface", "can9")])
        self.assertEqual(errors, [
            "noninteractive sudo is unavailable; arm/restoration cannot be guaranteed",
            "can9 bitrate is 125000, expected 500000",
            "can9 must prove classical CAN with FD off before active discovery",
            "can9 is listen-only; discovery is active diagnostic traffic, so arm it explicitly",
            "can9 controller state is unknown, expected ERROR-ACTIVE",
            "can9 restart-ms is 100; active discovery requires 0",
        ])

    def test_interface_shapes_and_non_interface_quirk(self):
        good = InterfaceState("can9", True, True, 500000, False, "ERROR-ACTIVE", 0, False)
        for state, expected in (
            (good, []),
            (replace(good, channel="can8"), [
                "can9 is missing or down; explicitly arm the intended bus first"
            ]),
            (replace(good, present=False, fd_enabled=None, restart_ms=None), [
                "can9 is missing or down; explicitly arm the intended bus first"
            ]),
            (replace(good, up=False, fd_enabled=None, restart_ms=None), [
                "can9 is missing or down; explicitly arm the intended bus first"
            ]),
        ):
            with self.subTest(state=state), mock.patch.object(
                diagnostic_preflight.canbus, "interface_state", return_value=state
            ):
                self.assertEqual(diagnostic_preflight.active_interface_errors("can9", 500000), expected)
        with mock.patch.object(diagnostic_preflight.canbus, "interface_state", return_value=None):
            with self.assertRaises(AttributeError):
                diagnostic_preflight.active_interface_errors("can9", 500000)

    def test_host_exception_does_not_inspect_interface(self):
        with mock.patch.object(diagnostic_preflight.subprocess, "run", side_effect=OSError("sudo")), \
             mock.patch.object(diagnostic_preflight.canbus, "interface_state") as interface:
            with self.assertRaises(OSError):
                diagnostic_preflight.preflight("can9", 500000)
        interface.assert_not_called()


class VinRedactionTests(unittest.TestCase):
    def test_original_names_reexported(self):
        for name in (
            "VIN_DID", "VIN_ALLOWED", "VIN_WEIGHTS", "VIN_TRANSLITERATION",
            "valid_vin_checksum", "mask_embedded_vins", "redact_response_vins",
        ):
            self.assertIs(getattr(identity_inventory, name), getattr(vin_redaction, name))
        self.assertIs(did_sweep.redact_response_vins, vin_redaction.redact_response_vins)

    def test_checksum_and_embedded_offsets(self):
        # Public checksum example, not the owner's vehicle identity.
        example = b"1M8GDM9AXKP042788"
        self.assertTrue(vin_redaction.valid_vin_checksum(example))
        self.assertTrue(vin_redaction.valid_vin_checksum(example.lower()))
        self.assertFalse(vin_redaction.valid_vin_checksum(example[:-1]))
        self.assertFalse(vin_redaction.valid_vin_checksum(b"I" + example[1:]))
        raw = b"prefix\x00" + example + b" middle " + example.lower() + b"\x00suffix"
        expected = b"prefix\x00" + example[:11] + b"###### middle " + example[:11].lower() + b"######\x00suffix"
        self.assertEqual(vin_redaction.mask_embedded_vins(raw), expected)
        self.assertEqual(len(raw), len(expected))

    def test_direct_vin_field_masks_even_without_checksum(self):
        raw = b"\x62\xf1\x90ABCDEFGHIJKLMNOPQ"
        expected = b"\x62\xf1\x90ABCDEFGHIJK######"
        self.assertEqual(vin_redaction.redact_response_vins(0xF190, raw), (expected, True))
        self.assertEqual(vin_redaction.redact_response_vins(0xF1A0, raw), (raw, False))
        self.assertEqual(vin_redaction.redact_response_vins(0xF190, None), (None, False))
        self.assertEqual(vin_redaction.redact_response_vins(0xF190, b""), (b"", False))


if __name__ == "__main__":
    unittest.main()
