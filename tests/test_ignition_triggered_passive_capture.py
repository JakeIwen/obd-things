import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ignition_triggered_passive_capture",
    REPO / "tools" / "ignition_triggered_passive_capture.py",
)
arm = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = arm
SPEC.loader.exec_module(arm)


class IgnitionTriggeredPassiveCaptureTests(unittest.TestCase):
    def test_service_receive_buffer_matches_recorder(self):
        unit = (
            REPO
            / "projects"
            / "ecu_mapping"
            / "promaster-mapping-drive.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ExecStartPre=+/usr/sbin/sysctl -w net.core.rmem_max="
            f"{arm.passive.RECEIVE_BUFFER}",
            unit,
        )

    def test_default_plan_is_inert(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "must-not-exist" / "state.json"
            with mock.patch.object(
                arm,
                "execute",
                side_effect=AssertionError("execute must not run"),
            ), mock.patch(
                "subprocess.run",
                side_effect=AssertionError("subprocess must not run"),
            ):
                status = arm.main(["--state-path", str(state_path)])
            self.assertEqual(status, 0)
            self.assertFalse(state_path.exists())

    def test_execute_requires_all_live_gates(self):
        parser = arm.build_parser()
        args = parser.parse_args(["--execute"])
        with self.assertRaisesRegex(arm.ArmError, "--confirm-passive"):
            arm.validate_args(args)

        args = parser.parse_args(
            ["--execute", "--confirm-passive", "--confirm-one-drive"]
        )
        with self.assertRaisesRegex(arm.ArmError, "--conditions"):
            arm.validate_args(args)

    def test_child_is_fixed_passive_capture_with_ignition_stop(self):
        args = arm.build_parser().parse_args(
            [
                "--conditions",
                "ordinary drive",
                "--duration-seconds",
                "1234",
                "--ignition-absence-seconds",
                "25",
            ]
        )
        command = arm.child_command(args, "drive-test")
        self.assertIn("tools/passive_drive_capture.py", command[1])
        self.assertIn("--confirm-passive", command)
        self.assertEqual(
            command[command.index("--stop-after-id") + 1],
            "0x2EF",
        )
        self.assertEqual(
            command[command.index("--stop-after-id-absence-seconds") + 1],
            "25.0",
        )
        self.assertNotIn("--tx", command)

    def test_execute_waits_then_runs_one_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mount = root / "mount"
            mount.mkdir()
            out_root = mount / "captures"
            state_path = root / "state.json"
            args = arm.build_parser().parse_args(
                [
                    "--execute",
                    "--confirm-passive",
                    "--confirm-one-drive",
                    "--conditions",
                    "ordinary drive",
                    "--out-root",
                    str(out_root),
                    "--require-mount",
                    str(mount),
                    "--state-path",
                    str(state_path),
                ]
            )
            policy = arm.validate_args(args)
            ownership = mock.Mock()
            ownership.route = mock.Mock(
                channel="can7",
                bitrate=500000,
                role="c-can",
                pair="6/14",
                topology_fingerprint="fixture",
            )
            with mock.patch.object(
                arm.can_runtime_route,
                "acquire_passive_bus_route",
                return_value=ownership,
            ), mock.patch.object(
                arm.passive,
                "require_writable_mount",
                return_value=123,
            ) as mount_check, mock.patch.object(
                arm.passive,
                "preflight",
            ) as preflight, mock.patch.object(
                arm,
                "wait_for_ignition",
                return_value=1.0,
            ), mock.patch.object(
                arm,
                "campaign_id",
                return_value="pcm-plots-drive-test",
            ), mock.patch.object(
                arm.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run:
                self.assertEqual(arm.execute(args, policy), 0)

            self.assertEqual(mount_check.call_count, 2)
            self.assertEqual(preflight.call_count, 2)
            run.assert_called_once()
            state = json.loads(state_path.read_text())
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["campaign"], "pcm-plots-drive-test")


if __name__ == "__main__":
    unittest.main()
