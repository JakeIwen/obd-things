import importlib.util
import pathlib
from types import SimpleNamespace
import unittest
from unittest import mock

from projects.vehicle_data.models import failure, success


REPO = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "projects" / "battery" / "voltage_mon.py"
if not MODULE_PATH.is_file():
    raise unittest.SkipTest(
        "repo-test source bundle omits projects/battery/voltage_mon.py"
    )
SPEC = importlib.util.spec_from_file_location("voltage_mon_under_test", MODULE_PATH)
voltage_mon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voltage_mon)


class SharedAcquireTests(unittest.TestCase):
    def test_available_shared_result_preserves_monitor_status_contract(self):
        result = success(
            metric="battery.voltage",
            unit="V",
            value=12.7,
            source="ccan.broadcast.0x2ef",
            bus="c-can",
            acquisition="passive",
            quality="approximate",
            observed_monotonic=10.0,
            detail="ok 0x2EF",
        )
        with mock.patch.object(
            voltage_mon._VOLTAGE_ACQUIRER, "acquire", return_value=result
        ) as acquire, mock.patch.object(
            voltage_mon, "BROKER_SOCKET", SimpleNamespace(exists=lambda: False)
        ):
            volts, status = voltage_mon.acquire()

        self.assertEqual(volts, 12.7)
        self.assertIn("passive C-CAN", status)
        self.assertIn("quality=approximate", status)
        acquire.assert_called_once_with("wake_if_asleep")

    def test_shared_failure_is_returned_without_fallback_can_action(self):
        result = failure(
            metric="battery.voltage",
            unit="V",
            reason="can_busy",
            detail="active acquisition inhibited by alfaobd",
        )
        with mock.patch.object(
            voltage_mon._VOLTAGE_ACQUIRER, "acquire", return_value=result
        ), mock.patch.object(
            voltage_mon, "BROKER_SOCKET", SimpleNamespace(exists=lambda: False)
        ):
            self.assertEqual(
                voltage_mon.acquire(),
                (None, "active acquisition inhibited by alfaobd"),
            )

    def test_canch_result_maps_to_existing_edge_notice_status(self):
        result = failure(
            metric="battery.voltage",
            unit="V",
            reason="wrong_bus",
            detail="CAN-CH is connected",
            bus="can-ch",
        )
        with mock.patch.object(
            voltage_mon._VOLTAGE_ACQUIRER, "acquire", return_value=result
        ), mock.patch.object(
            voltage_mon, "BROKER_SOCKET", SimpleNamespace(exists=lambda: False)
        ):
            self.assertEqual(
                voltage_mon.acquire(), (None, voltage_mon.CAN_CH_STATUS)
            )

    def test_running_broker_is_authoritative(self):
        payload = {
            "available": True,
            "value": 12.55,
            "bus": "b-can",
            "acquisition": "wake_assisted",
            "source": "bcan.broadcast.0x46c",
            "quality": "verified",
            "detail": "broker result",
        }
        client = mock.Mock()
        client.request.return_value = (200, payload)
        broker_path = SimpleNamespace(
            exists=lambda: True,
            is_socket=lambda: True,
            __str__=lambda _self: "/run/van-telemetry/api.sock",
        )
        with (
            mock.patch.object(voltage_mon, "BROKER_SOCKET", broker_path),
            mock.patch.object(
                voltage_mon, "TelemetryClient", return_value=client
            ) as client_class,
            mock.patch.object(
                voltage_mon._VOLTAGE_ACQUIRER, "acquire"
            ) as direct,
        ):
            volts, status = voltage_mon.acquire()

        self.assertEqual(volts, 12.55)
        self.assertIn("wake-assisted B-CAN", status)
        client_class.assert_called_once_with(
            str(broker_path), timeout=30.0
        )
        direct.assert_not_called()

    def test_broker_socket_failure_does_not_bypass_owner(self):
        broker_path = SimpleNamespace(
            exists=lambda: True,
            is_socket=lambda: True,
            __str__=lambda _self: "/run/van-telemetry/api.sock",
        )
        client = mock.Mock()
        client.request.side_effect = OSError("connection refused")
        with (
            mock.patch.object(voltage_mon, "BROKER_SOCKET", broker_path),
            mock.patch.object(
                voltage_mon, "TelemetryClient", return_value=client
            ),
            mock.patch.object(
                voltage_mon._VOLTAGE_ACQUIRER, "acquire"
            ) as direct,
        ):
            volts, status = voltage_mon.acquire()

        self.assertIsNone(volts)
        self.assertIn("direct CAN fallback withheld", status)
        direct.assert_not_called()


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
