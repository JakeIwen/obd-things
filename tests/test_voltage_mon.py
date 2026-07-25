import importlib.util
import pathlib
from types import SimpleNamespace
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "projects" / "battery" / "voltage_mon.py"
if not MODULE_PATH.is_file():
    raise unittest.SkipTest(
        "repo-test source bundle omits projects/battery/voltage_mon.py"
    )
SPEC = importlib.util.spec_from_file_location("voltage_mon_under_test", MODULE_PATH)
voltage_mon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voltage_mon)


class PassiveAcquireTests(unittest.TestCase):
    def safe_interface(self, bitrate=500000, bus="c-can"):
        return (
            mock.patch.object(voltage_mon.cv, "iface_bitrate", return_value=bitrate),
            mock.patch.object(voltage_mon.cv.canbus, "is_listen_only", return_value=True),
            mock.patch.object(voltage_mon.cv.canbus, "controller_state", return_value="ERROR-ACTIVE"),
            mock.patch.object(voltage_mon.cv.canbus, "identify_bus", return_value=bus),
        )

    def test_canch_is_detected_passively_without_read_wake_or_bringup(self):
        bitrate, listen_only, state, identify = self.safe_interface(bus="can-ch")
        with (
            bitrate,
            listen_only,
            state,
            identify,
            mock.patch.object(voltage_mon.cv, "read_voltage") as c_read,
            mock.patch.object(voltage_mon.bv, "read_voltage") as b_read,
            mock.patch.object(voltage_mon.cv, "read_with_wake") as c_wake,
            mock.patch.object(voltage_mon.bv, "read_with_wake") as b_wake,
            mock.patch.object(voltage_mon.cv, "bring_up_passive") as c_up,
            mock.patch.object(voltage_mon.bv, "bring_up_passive") as b_up,
            mock.patch.object(voltage_mon, "_record_observed_topology"),
        ):
            self.assertEqual(voltage_mon.acquire(), (None, voltage_mon.CAN_CH_STATUS))

        c_read.assert_not_called()
        b_read.assert_not_called()
        c_wake.assert_not_called()
        b_wake.assert_not_called()
        c_up.assert_not_called()
        b_up.assert_not_called()

    def test_awake_ccan_read_is_passive_and_bounded(self):
        bitrate, listen_only, state, identify = self.safe_interface()
        with (
            bitrate,
            listen_only,
            state,
            identify,
            mock.patch.object(
                voltage_mon.cv, "read_voltage", return_value=(12.7, "ok")
            ) as read,
            mock.patch.object(voltage_mon.cv.canbus, "ip_up") as ip_up,
            mock.patch.object(voltage_mon, "_record_observed_topology"),
        ):
            volts, status = voltage_mon.acquire()

        self.assertEqual(volts, 12.7)
        self.assertIn("passive C-CAN", status)
        read.assert_called_once_with(timeout=2.0)
        ip_up.assert_not_called()

    def test_down_interface_skips_before_bus_probe(self):
        with (
            mock.patch.object(voltage_mon.cv, "iface_bitrate", return_value=None),
            mock.patch.object(voltage_mon.cv.canbus, "identify_bus") as identify,
            mock.patch.object(voltage_mon.cv.canbus, "ip_up") as ip_up,
        ):
            volts, status = voltage_mon.acquire()

        self.assertIsNone(volts)
        self.assertIn("skipped without interface changes", status)
        identify.assert_not_called()
        ip_up.assert_not_called()

    def test_armed_interface_skips_without_probe_or_mutation(self):
        with (
            mock.patch.object(voltage_mon.cv, "iface_bitrate", return_value=500000),
            mock.patch.object(voltage_mon.cv.canbus, "is_listen_only", return_value=False),
            mock.patch.object(voltage_mon.cv.canbus, "identify_bus") as identify,
            mock.patch.object(voltage_mon.cv.canbus, "ip_up") as ip_up,
        ):
            volts, status = voltage_mon.acquire()

        self.assertIsNone(volts)
        self.assertIn("active operation", status)
        identify.assert_not_called()
        ip_up.assert_not_called()

    def test_silent_bus_with_unknown_topology_is_never_woken(self):
        bitrate, listen_only, state, identify = self.safe_interface(bus="silent")
        lock_handle = object()
        with (
            bitrate,
            listen_only,
            state,
            identify,
            mock.patch.object(
                voltage_mon.diagnostic_safety,
                "acquire_channel_lock",
                return_value=lock_handle,
            ),
            mock.patch.object(
                voltage_mon.diagnostic_safety, "release_channel_lock"
            ) as release,
            mock.patch.object(
                voltage_mon.can_operation_state,
                "active_inhibits",
                return_value=(),
            ),
            mock.patch.object(
                voltage_mon.can_operation_state,
                "load_topology",
                return_value=SimpleNamespace(
                    bus="unknown",
                    usable=False,
                    reason="topology record missing",
                ),
            ),
            mock.patch.object(voltage_mon.cv.canbus, "poke_wake") as poke,
            mock.patch.object(voltage_mon.cv.canbus, "tx_wake_burst") as burst,
            mock.patch.object(voltage_mon.cv.canbus, "ip_up") as ip_up,
        ):
            volts, status = voltage_mon.acquire()

        self.assertIsNone(volts)
        self.assertIn("topology record missing", status)
        poke.assert_not_called()
        burst.assert_not_called()
        ip_up.assert_not_called()
        release.assert_called_once_with(lock_handle)

    def test_silent_bus_external_inhibit_blocks_wake_capable_topology(self):
        bitrate, listen_only, state, identify = self.safe_interface(bus="silent")
        lock_handle = object()
        with (
            bitrate,
            listen_only,
            state,
            identify,
            mock.patch.object(
                voltage_mon.diagnostic_safety,
                "acquire_channel_lock",
                return_value=lock_handle,
            ),
            mock.patch.object(
                voltage_mon.diagnostic_safety, "release_channel_lock"
            ),
            mock.patch.object(
                voltage_mon.can_operation_state,
                "active_inhibits",
                return_value=({"name": "alfaobd"},),
            ),
            mock.patch.object(
                voltage_mon.can_operation_state,
                "load_topology",
                return_value=SimpleNamespace(bus="c-can", usable=True),
            ) as topology,
            mock.patch.object(voltage_mon.cv.canbus, "poke_wake") as poke,
            mock.patch.object(voltage_mon.cv.canbus, "tx_wake_burst") as burst,
        ):
            volts, status = voltage_mon.acquire()

        self.assertIsNone(volts)
        self.assertIn("inhibited by alfaobd", status)
        topology.assert_called_once_with("can0")
        poke.assert_not_called()
        burst.assert_not_called()

    def test_silent_canch_topology_notifies_despite_inhibit_without_wake(self):
        bitrate, listen_only, state, identify = self.safe_interface(bus="silent")
        with (
            bitrate,
            listen_only,
            state,
            identify,
            mock.patch.object(
                voltage_mon.diagnostic_safety,
                "acquire_channel_lock",
                return_value=object(),
            ),
            mock.patch.object(
                voltage_mon.diagnostic_safety, "release_channel_lock"
            ),
            mock.patch.object(
                voltage_mon.can_operation_state,
                "active_inhibits",
                return_value=({"name": "alfaobd"},),
            ) as inhibits,
            mock.patch.object(
                voltage_mon.can_operation_state,
                "load_topology",
                return_value=SimpleNamespace(bus="can-ch", usable=True),
            ),
            mock.patch.object(voltage_mon.cv.canbus, "poke_wake") as poke,
            mock.patch.object(voltage_mon.cv.canbus, "tx_wake_burst") as burst,
        ):
            self.assertEqual(
                voltage_mon.acquire(), (None, voltage_mon.CAN_CH_STATUS)
            )
        poke.assert_not_called()
        burst.assert_not_called()
        inhibits.assert_not_called()

    def test_silent_ccan_wakes_only_under_lock_and_revalidates(self):
        bitrate, listen_only, state, _identify = self.safe_interface(bus="silent")
        lock_handle = object()
        with (
            bitrate,
            listen_only,
            state,
            mock.patch.object(
                voltage_mon.cv.canbus,
                "identify_bus",
                side_effect=("silent", "silent", "c-can"),
            ),
            mock.patch.object(
                voltage_mon.diagnostic_safety,
                "acquire_channel_lock",
                return_value=lock_handle,
            ),
            mock.patch.object(
                voltage_mon.diagnostic_safety, "release_channel_lock"
            ) as release,
            mock.patch.object(
                voltage_mon.can_operation_state,
                "active_inhibits",
                return_value=(),
            ),
            mock.patch.object(
                voltage_mon.can_operation_state,
                "load_topology",
                return_value=SimpleNamespace(
                    bus="c-can",
                    usable=True,
                    reason="",
                    source="explicit_test",
                ),
            ),
            mock.patch.object(
                voltage_mon.cv.canbus, "poke_wake", return_value=True
            ) as poke,
            mock.patch.object(
                voltage_mon.cv,
                "read_voltage",
                return_value=(12.6, "ok"),
            ),
        ):
            volts, status = voltage_mon.acquire()

        self.assertEqual(volts, 12.6)
        self.assertIn("autonomous wake", status)
        poke.assert_called_once_with(
            "can0", 500000, lock_handle=lock_handle
        )
        release.assert_called_once_with(lock_handle)

    def test_lock_contention_blocks_silent_wake(self):
        bitrate, listen_only, state, identify = self.safe_interface(bus="silent")
        with (
            bitrate,
            listen_only,
            state,
            identify,
            mock.patch.object(
                voltage_mon.diagnostic_safety,
                "acquire_channel_lock",
                side_effect=voltage_mon.diagnostic_safety.ChannelLockError(
                    "busy"
                ),
            ),
            mock.patch.object(voltage_mon.cv.canbus, "poke_wake") as poke,
            mock.patch.object(voltage_mon.cv.canbus, "tx_wake_burst") as burst,
        ):
            volts, status = voltage_mon.acquire()

        self.assertIsNone(volts)
        self.assertIn("another participating CAN operation", status)
        poke.assert_not_called()
        burst.assert_not_called()


class GreyNoticeTests(unittest.TestCase):
    def test_first_grey_detection_sends_notice_and_persists_edge(self):
        state = {"low": False, "last_alert": None}
        with (
            mock.patch.object(voltage_mon, "_load_state", return_value=state),
            mock.patch.object(voltage_mon, "_save_state") as save,
            mock.patch.object(voltage_mon, "notify") as notify,
        ):
            handled = voltage_mon.handle_grey_adapter(
                voltage_mon.CAN_CH_STATUS, allow_send=True
            )

        self.assertTrue(handled)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["title"], "Van CAN monitor")
        self.assertTrue(state["grey_connected"])
        save.assert_called_once_with(state)

    def test_repeated_grey_detection_does_not_spam_ntfy(self):
        state = {"low": False, "last_alert": None, "grey_connected": True}
        with (
            mock.patch.object(voltage_mon, "_load_state", return_value=state),
            mock.patch.object(voltage_mon, "_save_state") as save,
            mock.patch.object(voltage_mon, "notify") as notify,
        ):
            handled = voltage_mon.handle_grey_adapter(
                voltage_mon.CAN_CH_STATUS, allow_send=True
            )

        self.assertTrue(handled)
        notify.assert_not_called()
        save.assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
