import io
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data import pcm_temperature_support as support


def frame(data):
    raw = bytes.fromhex(data)
    return support.struct.pack(support.pcm.CAN_FRAME_FORMAT, 0x98DAF110, len(raw), raw.ljust(8, b"\0"))


class TemperatureSupportTests(unittest.TestCase):
    def test_oil_life_plan_is_one_exact_padded_read_without_io(self):
        with mock.patch.object(support.can_runtime_route, "acquire_armed_bus_route") as arm, \
                mock.patch.object(support.can_handoff, "active_turn") as turn, \
                mock.patch.object(support.socket, "socket") as sock, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(support.main(["--profile", "oil-life"]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["requests"], ["03 22 21 85 00 00 00 00"])
        self.assertEqual(plan["maximum_requests"], 1)
        self.assertFalse(plan["session_change"])
        self.assertFalse(plan["flow_control"])
        self.assertEqual(plan["retries"], 0)
        arm.assert_not_called()
        sock.assert_not_called()
        turn.assert_not_called()

    def test_busy_handoff_never_arms_or_transmits(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(support, "REPO", Path(tmp)),
            mock.patch.object(support.can_handoff, "active_turn", side_effect=
                              support.diagnostic_safety.ChannelLockError("handoff busy")),
            mock.patch.object(support.can_runtime_route, "acquire_armed_bus_route") as arm,
            mock.patch.object(support.socket, "socket") as sock,
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(support.main(["--profile", "oil-life", "--execute",
                                          "--confirm-parked-ignition-on-engine-off"]), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["results"], [])
        self.assertIsNone(report["restored_passive"])
        self.assertIn("handoff busy", report["error"])
        arm.assert_not_called()
        sock.assert_not_called()

    def test_oil_life_decodes_percent_and_rejects_invalid_shapes(self):
        for raw in (0, 17, 100):
            reply = support.decode_reply(0x2185, frame(f"04 62 21 85 {raw:02X}"))
            self.assertEqual(reply["value_percent"], raw)
            self.assertNotIn("value_c", reply)
        self.assertEqual(support.decode_reply(0x2185, frame("03 7F 22 12"))["status"],
                         "negative_response")
        for data in ("04 62 21 85 65", "04 62 21 85 FF", "04 62 21 86 11",
                     "05 62 21 85 11 00", "10 08 62 21 85 11 00 00"):
            with self.assertRaises(ValueError):
                support.decode_reply(0x2185, frame(data))
        with self.assertRaises(ValueError):
            support.decode_reply(0x1234, frame("04 62 12 34 11"))

    def test_plan_never_acquires_a_route_or_opens_can(self):
        with mock.patch.object(support.can_runtime_route, "acquire_armed_bus_route") as arm, \
                mock.patch.object(support.socket, "socket") as sock, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(support.main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["maximum_requests"], 2)
        arm.assert_not_called()
        sock.assert_not_called()

    def test_vehicle_gate_requires_all_three_independent_witnesses(self):
        observations = [SimpleNamespace(metric="vehicle.ignition_on", value=True),
                        SimpleNamespace(metric="vehicle.speed", value=0)]
        snap = SimpleNamespace(frame_count=50, rpm_samples=(0, 0, 0), observations=observations)
        self.assertEqual(support.stationary_errors(snap), [])
        for rpm, speed, ignition in (((0, 0), 0, True), ((0, 0, 700), 0, True),
                                     ((0, 0, 0), 1, True), ((0, 0, 0), 0, False)):
            snap.rpm_samples = rpm
            snap.observations = [SimpleNamespace(metric="vehicle.ignition_on", value=ignition),
                                 SimpleNamespace(metric="vehicle.speed", value=speed)]
            self.assertTrue(support.stationary_errors(snap))

    def test_exact_reply_classification(self):
        self.assertEqual(support.decode_reply(0xF45C, frame("03 7F 22 12"))["nrc"], "12")
        self.assertEqual(support.decode_reply(0xF45C, frame("04 62 F4 5C 64"))["value_c"], 60)
        self.assertEqual(support.decode_reply(0x069F, frame("04 62 06 9F A0"))["value_c"], 96)
        for data in ("05 62 06 9F A0 00", "04 62 06 9E A0", "10 08 62 06 9F A0 00 00"):
            with self.assertRaises(ValueError):
                support.decode_reply(0x069F, frame(data))

    def test_live_fixed_pair_restores_and_stops_on_lost_vehicle_gate(self):
        for profile, gate_failed, restored in (
            (profile, failed, restored)
            for profile in ("temperature", "oil-life")
            for failed, restored in ((False, True), (True, True), (False, False))
        ):
            with self.subTest(gate_failed=gate_failed, restored=restored), tempfile.TemporaryDirectory() as tmp:
                events = []

                @contextmanager
                def handoff(bus):
                    self.assertEqual(bus, "c-can")
                    events.append("handoff_enter")
                    try:
                        yield
                    finally:
                        events.append("handoff_exit")

                owner = mock.Mock()
                owner.route.channel = "can7"

                def acquire(*args, **kwargs):
                    self.assertEqual(events, ["handoff_enter"])
                    events.append("arm")
                    return owner

                def release():
                    events.append("restore")
                    return restored

                owner.release.side_effect = release
                sock = mock.MagicMock()
                sock.__enter__.return_value = sock
                sock.send.return_value = 16
                sock.recv.side_effect = ([frame("04 62 21 85 11")] if profile == "oil-life"
                                        else [frame("03 7F 22 12"), frame("04 62 06 9F A0")])
                with (
                    mock.patch.object(support, "REPO", Path(tmp)),
                    mock.patch.object(support.can_handoff, "active_turn", side_effect=handoff),
                    mock.patch.object(support.can_runtime_route, "acquire_armed_bus_route", side_effect=acquire),
                    mock.patch.object(support.can_runtime_route, "revalidate_bus_route"),
                    mock.patch.object(support.can_runtime_route, "_is_exact_armed_state", return_value=True),
                    mock.patch.object(support.diagnostic_safety, "validate_channel_lock"),
                    mock.patch.object(support.canbus, "interface_state"),
                    mock.patch.object(support.can_operation_state, "active_inhibits", return_value=[]),
                    mock.patch.object(support.can_operation_state, "load_topology", return_value=SimpleNamespace(
                        usable=True, bus="c-can", pair="6/14")),
                    mock.patch.object(support, "vehicle_gate", return_value=["engine started"] if gate_failed else []),
                    mock.patch.object(support.socket, "socket", return_value=sock),
                    mock.patch.object(support.time, "sleep"),
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output,
                ):
                    result = support.main(["--profile", profile, "--execute",
                                           "--confirm-parked-ignition-on-engine-off"])
                owner.release.assert_called_once()
                self.assertEqual(events, ["handoff_enter", "arm", "restore", "handoff_exit"])
                self.assertEqual(result, 0 if restored and not gate_failed else 1)
                expected_count = 1 if profile == "oil-life" else 2
                self.assertEqual(sock.send.call_count, 0 if gate_failed else expected_count)
                if not gate_failed:
                    sent = [support.struct.unpack(support.pcm.CAN_FRAME_FORMAT, c.args[0])[2]
                            for c in sock.send.call_args_list]
                    if profile == "oil-life":
                        self.assertEqual(sent, [bytes.fromhex("03 22 21 85 00 00 00 00")])
                        report = json.loads(output.getvalue())
                        self.assertEqual(report["results"][0]["value_percent"], 17)
                        self.assertIn("oil-life-support-", report["report_path"])
                    else:
                        self.assertEqual(sent, [bytes.fromhex("03 22 F4 5C 00 00 00 00"),
                                                bytes.fromhex("03 22 06 9F 00 00 00 00")])
                        self.assertEqual([r["status"] for r in json.loads(output.getvalue())["results"]],
                                         ["negative_response", "positive"])
