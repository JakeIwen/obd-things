"""Pure route predicates: no interface, filesystem, lock or socket access."""
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import unittest

from lib import canbus
from lib.can_role_resolver import NetdevIdentity
from lib.can_runtime_route import LinkExpectation, NetdevIdentityExpectation


def passive():
    return canbus.InterfaceState(
        channel="can7", present=True, up=True, bitrate=500000,
        listen_only=True, controller_state="ERROR-ACTIVE", restart_ms=0,
        fd_enabled=False, one_shot=False,
    )


def device():
    return NetdevIdentity("can7", "gs_usb", "1d50", "606f", "serial-a", 0, "/fixture")


class LinkExpectationTests(unittest.TestCase):
    def test_exact_passive_and_active(self):
        state = passive()
        for listen_only in (True, False):
            for bitrate in (125000, 500000):
                for restart_ms in (0, 100, None):
                    with self.subTest(listen_only=listen_only, bitrate=bitrate, restart_ms=restart_ms):
                        expectation = LinkExpectation(bitrate, listen_only, restart_ms)
                        current = replace(state, bitrate=bitrate, listen_only=listen_only,
                                          restart_ms=restart_ms)
                        self.assertTrue(expectation.matches(current))
                        self.assertFalse(expectation.matches(replace(current, listen_only=not listen_only)))

    def test_required_fields_fail_independently(self):
        expectation = LinkExpectation(500000, True, channel="can7")
        for field, values in {
            "channel": ("can8", None), "present": (False, None),
            "up": (False, None), "bitrate": (None, 125000),
            "fd_enabled": (None, True, 0), "one_shot": (None, True, 0),
            "listen_only": (False, None), "restart_ms": (None, 100),
            "controller_state": (None, "ERROR-PASSIVE", "BUS-OFF"),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertFalse(expectation.matches(replace(passive(), **{field: value})))

    def test_unknown_is_not_fd_off_or_one_shot_off(self):
        for field in ("fd_enabled", "one_shot"):
            for value in (None, 0, "", []):
                with self.subTest(field=field, value=value):
                    self.assertFalse(LinkExpectation(500000, True).matches(
                        replace(passive(), **{field: value})))

    def test_preserves_truthiness_and_restart_equality(self):
        self.assertTrue(LinkExpectation(500000, True).matches(
            replace(passive(), present=1, up=1, listen_only=1, restart_ms=False)))
        for value in (False, None, 0):
            with self.subTest(listen_only=value):
                self.assertTrue(LinkExpectation(500000, False).matches(
                    replace(passive(), listen_only=value)))
        self.assertFalse(LinkExpectation(500000, True).matches(replace(passive(), restart_ms=None)))

    def test_channel_is_opt_in(self):
        state = replace(passive(), channel="can8")
        self.assertTrue(LinkExpectation(500000, True).matches(state))
        self.assertFalse(LinkExpectation(500000, True, channel="can7").matches(state))

    def test_one_shot_is_explicit_identity_comparison(self):
        expectation = LinkExpectation(500000, False, one_shot=True)
        self.assertTrue(expectation.matches(replace(passive(), listen_only=False, one_shot=True)))
        self.assertFalse(expectation.matches(replace(passive(), listen_only=False, one_shot=1)))

    def test_type_policy_is_explicit_and_does_not_swallow_errors(self):
        expectation = LinkExpectation(500000, True)
        duck = SimpleNamespace(**vars(passive()))
        self.assertFalse(expectation.matches(duck))
        self.assertTrue(expectation.matches(duck, require_interface_state=False))
        self.assertFalse(expectation.matches(None))
        with self.assertRaisesRegex(AttributeError, "present"):
            expectation.matches(None, require_interface_state=False)

    def test_field_order_and_short_circuit_are_preserved(self):
        reads = []
        class Readback:
            def __getattr__(self, name):
                reads.append(name)
                return getattr(passive(), name)
        self.assertTrue(LinkExpectation(500000, True, channel="can7").matches(
            Readback(), require_interface_state=False))
        self.assertEqual(reads, ["channel", "present", "up", "bitrate", "fd_enabled",
                                 "one_shot", "listen_only", "controller_state", "restart_ms"])
        reads.clear()
        self.assertFalse(LinkExpectation(125000, True).matches(
            Readback(), require_interface_state=False))
        self.assertEqual(reads, ["present", "up", "bitrate"])

    def test_expectation_is_immutable(self):
        expectation = LinkExpectation(500000, True)
        with self.assertRaises(FrozenInstanceError):
            expectation.bitrate = 125000


class NetdevIdentityExpectationTests(unittest.TestCase):
    def setUp(self):
        self.expectation = NetdevIdentityExpectation("can7", "serial-a", 0)

    def test_matches_one_device_and_consumes_entire_inventory(self):
        seen = []
        def inventory():
            for item in (device(), replace(device(), usb_serial="other")):
                seen.append(item)
                yield item
        self.assertTrue(self.expectation.matches_inventory(inventory()))
        self.assertEqual(len(seen), 2)
        self.assertFalse(self.expectation.matches_inventory(()))

    def test_every_identity_component_and_channel(self):
        for field, value in (
            ("driver", "other"), ("usb_vid", "FFFF"), ("usb_pid", "FFFF"),
            ("usb_serial", "other"), ("dev_id", 1), ("channel", "can8"),
        ):
            with self.subTest(field=field):
                self.assertFalse(self.expectation.matches_inventory((replace(device(), **{field: value}),)))
        # sysfs path is not part of these two consumers' identity predicates.
        self.assertTrue(self.expectation.matches_inventory((replace(device(), sysfs_path="/changed"),)))

    def test_duplicates_fail_even_if_only_one_has_expected_channel(self):
        for second in (device(), replace(device(), channel="can8")):
            with self.subTest(second=second):
                self.assertFalse(self.expectation.matches_inventory((device(), second)))

    def test_driver_filter_difference_is_explicit(self):
        wrong_driver = replace(device(), driver="other")
        self.assertFalse(self.expectation.matches_inventory((wrong_driver,)))
        unchecked = replace(self.expectation, driver=None)
        self.assertTrue(unchecked.matches_inventory((wrong_driver,)))
        self.assertFalse(unchecked.matches_inventory((device(), wrong_driver)))

    def test_does_not_normalize_or_validate_caller_inputs(self):
        self.assertFalse(replace(self.expectation, usb_serial=" serial-a ").matches_inventory((device(),)))
        self.assertFalse(replace(self.expectation, usb_vid="1D50").matches_inventory((device(),)))
        # The helper preserves equality; active_drive validates bool dev_id first,
        # while the recorder historically compares it without that validation.
        self.assertTrue(replace(self.expectation, dev_id=False).matches_inventory((device(),)))

    def test_identity_expectation_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.expectation.channel = "can8"


if __name__ == "__main__":
    unittest.main()
