import argparse
from pathlib import Path
import unittest
from unittest import mock

from projects.ecu_mapping import bcan_drive_recorder as recorder
from tools import passive_drive_capture as capture


PASSIVE_BCAN_DETAILS = """\
4: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc fq_codel state UP mode DEFAULT
    link/can
    can <LISTEN-ONLY> state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 0
          bitrate 125000 sample-point 0.875
    RX:  bytes packets errors dropped  missed   mcast
            4096      64      0       0       0       0
"""


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class BcanDriveRecorderTests(unittest.TestCase):
    def test_signature_gate_requires_three_known_ids_without_wrong_rate_errors(self):
        ids = {0x46C, 0x0A0, 0x3DC}
        self.assertTrue(recorder.bcan_signature_ready(ids, 0))
        self.assertTrue(recorder.bcan_signature_ready(ids, 1))
        self.assertFalse(recorder.bcan_signature_ready({0x46C, 0x0A0}, 0))
        self.assertFalse(
            recorder.bcan_signature_ready(ids, recorder.canbus.RX_ERR_ABORT)
        )
        self.assertFalse(recorder.bcan_signature_ready({0x123, 0x124, 0x125}, 0))

    def test_priority_ids_include_signatures_and_bcan_diagnostics(self):
        selected = recorder.priority_ids()
        self.assertTrue(recorder.canbus.BCAN_SIG <= selected)
        for module in recorder.MODULES.values():
            if module.bus == "b-can":
                self.assertIn(module.txid, selected)
                self.assertIn(module.rxid, selected)

    def test_interface_gate_requires_exact_passive_bcan_state(self):
        def runner(_command, **_kwargs):
            return Result(stdout=PASSIVE_BCAN_DETAILS)

        exact = recorder.canbus.InterfaceState(
            "can0", True, True, 125000, True, "ERROR-ACTIVE", 0, False
        )
        with (
            mock.patch.object(recorder, "CHANNEL", "can0"),
            mock.patch.object(
                recorder.canbus, "interface_state", return_value=exact
            ),
        ):
            state = recorder.query_interface(runner=runner)
        self.assertTrue(state.up)
        self.assertTrue(state.listen_only)
        self.assertEqual(state.bitrate, 125000)

        def ccan_runner(_command, **_kwargs):
            return Result(stdout=PASSIVE_BCAN_DETAILS.replace("125000", "500000"))

        with (
            mock.patch.object(recorder, "CHANNEL", "can0"),
            mock.patch.object(
                recorder.canbus, "interface_state", return_value=exact
            ),
            self.assertRaisesRegex(recorder.BcanRecorderError, "expected 125000"),
        ):
            recorder.query_interface(runner=ccan_runner)

        def armed_runner(_command, **_kwargs):
            return Result(stdout=PASSIVE_BCAN_DETAILS.replace("<LISTEN-ONLY>", ""))

        with (
            mock.patch.object(recorder, "CHANNEL", "can0"),
            mock.patch.object(
                recorder.canbus, "interface_state", return_value=exact
            ),
            self.assertRaisesRegex(recorder.BcanRecorderError, "not LISTEN-ONLY"),
        ):
            recorder.query_interface(runner=armed_runner)

    def test_validate_args_requires_explicit_passive_confirmation(self):
        parser = recorder.build_parser()
        args = parser.parse_args(["--execute"])
        with self.assertRaisesRegex(
            recorder.BcanRecorderError,
            "requires --confirm-passive-bcan",
        ):
            recorder.validate_args(args)

        args = parser.parse_args(["--execute", "--confirm-passive-bcan"])
        policy = recorder.validate_args(args)
        self.assertEqual(policy.hard_free_bytes, 25 * 1024**3)

    def test_plan_records_passive_contract_and_rearm_trigger(self):
        args = recorder.build_parser().parse_args([])
        policy = recorder.validate_args(args)
        plan = recorder.plan(args, policy)
        self.assertEqual(plan["interaction"], "passive_receive_only")
        self.assertEqual(plan["interface_requirement"]["bitrate"], 125000)
        self.assertEqual(plan["tracked_id"], "0x46C")
        self.assertEqual(plan["trigger"]["minimum_bcan_signature_ids"], 3)
        self.assertIn("transmit CAN", plan["does_not"])

    def test_dependency_gate_preserves_disk_floor(self):
        policy = capture.DiskPolicy(300, 200)
        free = recorder.validate_dependencies(
            Path("/unused"),
            policy,
            which=lambda name: f"/usr/bin/{name}",
            disk_free=lambda _path: 1000,
            rmem_max=lambda: capture.RECEIVE_BUFFER,
        )
        self.assertEqual(free, 1000)

    def test_topology_record_requires_signature_and_uses_exact_bcan_pair(self):
        setter = mock.Mock(return_value=object())
        signatures = frozenset((0x46C, 0x0A0, 0x3DC))

        with mock.patch.object(recorder, "CHANNEL", "can0"):
            recorder.record_bcan_topology(signatures, setter=setter)

        setter.assert_called_once_with(
            "can0",
            "b-can",
            pair="3/11",
            source=recorder.TOPOLOGY_SOURCE,
            note=mock.ANY,
        )
        with self.assertRaisesRegex(
            recorder.BcanRecorderError,
            "required signature witness",
        ):
            with mock.patch.object(recorder, "CHANNEL", "can0"):
                recorder.record_bcan_topology(
                    frozenset((0x46C, 0x0A0)), setter=setter
                )
        with self.assertRaisesRegex(
            recorder.BcanRecorderError,
            "required signature witness",
        ):
            with mock.patch.object(recorder, "CHANNEL", "can0"):
                recorder.record_bcan_topology(
                    frozenset((0x123, 0x124, 0x125)), setter=setter
                )

    def test_idle_probe_releases_observer_lock_before_sleep(self):
        args = recorder.build_parser().parse_args([])
        policy = recorder.validate_args(args)
        events = []

        class Handle:
            closed = False

        handle = Handle()

        def acquire(_channel):
            events.append("acquire")
            return handle

        def release(value):
            self.assertIs(value, handle)
            value.closed = True
            events.append("release")

        def sleep(_seconds):
            self.assertTrue(handle.closed)
            events.append("sleep")
            raise StopIteration

        with (
            mock.patch.object(recorder.capture, "require_writable_mount"),
            mock.patch.object(recorder, "validate_dependencies"),
            mock.patch.object(recorder, "write_state"),
            self.assertRaises(StopIteration),
        ):
            recorder.run_daemon(
                args,
                policy,
                sleep=sleep,
                acquire_observer=acquire,
                release_lock=release,
                probe=lambda _seconds: (frozenset(), 0),
            )

        self.assertEqual(events, ["acquire", "release", "sleep"])

    def test_ready_probe_keeps_same_observer_lock_through_record_start(self):
        args = recorder.build_parser().parse_args([])
        policy = recorder.validate_args(args)
        signatures = frozenset((0x46C, 0x0A0, 0x3DC))
        events = []

        class Handle:
            closed = False

        handle = Handle()

        def acquire(_channel):
            events.append("acquire")
            return handle

        def probe(_seconds):
            self.assertFalse(handle.closed)
            events.append("probe")
            return signatures, 0

        def topology(value):
            self.assertEqual(value, signatures)
            self.assertFalse(handle.closed)
            events.append("topology")

        def record(_args, _policy, value, observer_handle):
            self.assertEqual(value, signatures)
            self.assertIs(observer_handle, handle)
            self.assertFalse(handle.closed)
            events.append("record")
            raise StopIteration

        def release(value):
            self.assertIs(value, handle)
            value.closed = True
            events.append("release")

        with (
            mock.patch.object(recorder.capture, "require_writable_mount"),
            mock.patch.object(recorder, "validate_dependencies"),
            self.assertRaises(StopIteration),
        ):
            recorder.run_daemon(
                args,
                policy,
                acquire_observer=acquire,
                release_lock=release,
                probe=probe,
                topology_recorder=topology,
                interval_recorder=record,
            )

        self.assertEqual(
            events,
            ["acquire", "probe", "topology", "record", "release"],
        )


if __name__ == "__main__":
    unittest.main()
