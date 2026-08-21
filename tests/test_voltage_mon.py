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
            source="ccan.broadcast.0x41a",
            bus="c-can",
            acquisition="passive",
            quality="verified",
            observed_monotonic=10.0,
            detail="ok 0x41A [verified affine]",
        )
        with mock.patch.object(
            voltage_mon._VOLTAGE_ACQUIRER, "acquire", return_value=result
        ) as acquire, mock.patch.object(
            voltage_mon, "BROKER_SOCKET", SimpleNamespace(exists=lambda: False)
        ):
            volts, status = voltage_mon.acquire()

        self.assertEqual(volts, 12.7)
        self.assertIn("passive C-CAN", status)
        self.assertIn("quality=verified", status)
        acquire.assert_called_once_with("passive")

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

    def test_running_broker_is_authoritative(self):
        payload = {
            "available": True,
            "value": 12.55,
            "bus": "c-can",
            "acquisition": "passive",
            "source": "ccan.broadcast.0x41a",
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
        self.assertIn("passive C-CAN", status)
        client_class.assert_called_once_with(
            str(broker_path), timeout=30.0
        )
        client.request.assert_called_once_with(
            "POST",
            "/v1/acquisitions/battery.voltage",
            {"mode": "passive"},
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

if __name__ == "__main__":
    unittest.main()
