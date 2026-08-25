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


class SharedAcquireTests(unittest.TestCase):
    def test_absent_broker_never_falls_back_to_direct_can(self):
        path = SimpleNamespace(
            exists=lambda: False,
            __str__=lambda _self: "/run/van-telemetry/api.sock",
        )
        with mock.patch.object(voltage_mon, "BROKER_SOCKET", path):
            volts, status = voltage_mon.acquire()

        self.assertIsNone(volts)
        self.assertIn("direct CAN fallback withheld", status)

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
        ):
            volts, status = voltage_mon.acquire()

        self.assertEqual(volts, 12.55)
        self.assertIn("wake-assisted B-CAN", status)
        client_class.assert_called_once_with(
            str(broker_path), timeout=30.0
        )
        client.request.assert_called_once_with(
            "POST",
            "/v1/acquisitions/battery.voltage",
            {"mode": "wake_if_asleep"},
        )

    def test_passive_only_never_requests_wake_mode(self):
        payload = {
            "available": False,
            "reason": "bus_asleep",
            "detail": "resolved c-can produced no passive traffic",
            "bus": "c-can",
            "acquisition": "passive",
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
            mock.patch.object(voltage_mon, "TelemetryClient", return_value=client),
        ):
            voltage_mon.acquire(passive_only=True)

        client.request.assert_called_once_with(
            "POST",
            "/v1/acquisitions/battery.voltage",
            {"mode": "passive"},
        )

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
        ):
            volts, status = voltage_mon.acquire()

        self.assertIsNone(volts)
        self.assertIn("direct CAN fallback withheld", status)

    def test_unreachable_ntfy_does_not_skip_sampling_or_consume_alert_edge(self):
        with (
            mock.patch.object(voltage_mon.sys, "argv", ["voltage_mon.py"]),
            mock.patch.object(voltage_mon, "NTFY_VOLTAGE_URL", "https://ntfy.invalid/topic"),
            mock.patch.object(voltage_mon, "have_connectivity", return_value=False),
            mock.patch.object(voltage_mon, "acquire", return_value=(12.5, "ok")) as acquire,
            mock.patch.object(voltage_mon.bv, "append_csv"),
            mock.patch.object(voltage_mon, "maybe_alert") as maybe_alert,
            mock.patch.object(voltage_mon.os, "makedirs"),
            mock.patch("builtins.open", mock.mock_open()),
            mock.patch.object(voltage_mon.fcntl, "flock"),
            self.assertRaises(SystemExit),
        ):
            voltage_mon.main()

        acquire.assert_called_once_with(passive_only=False)
        maybe_alert.assert_not_called()

    def test_no_notify_does_not_mutate_alert_state(self):
        with (
            mock.patch.object(
                voltage_mon.sys,
                "argv",
                ["voltage_mon.py", "--no-notify", "--passive-only"],
            ),
            mock.patch.object(voltage_mon, "acquire", return_value=(11.8, "ok")) as acquire,
            mock.patch.object(voltage_mon.bv, "append_csv"),
            mock.patch.object(voltage_mon, "maybe_alert") as maybe_alert,
            mock.patch.object(voltage_mon.os, "makedirs"),
            mock.patch("builtins.open", mock.mock_open()),
            mock.patch.object(voltage_mon.fcntl, "flock"),
            self.assertRaises(SystemExit),
        ):
            voltage_mon.main()

        acquire.assert_called_once_with(passive_only=True)
        maybe_alert.assert_not_called()

if __name__ == "__main__":
    unittest.main()
