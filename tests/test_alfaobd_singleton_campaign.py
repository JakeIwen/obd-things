import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zlib

from tools import alfaobd_singleton_campaign as campaign

REPO = Path(__file__).resolve().parents[1]


def node(
    *,
    text="",
    resource_id="",
    class_name="android.widget.TextView",
    package=campaign.PACKAGE,
    checkable=False,
    checked=False,
    clickable=False,
    enabled=True,
    selected=False,
    bounds="[0,0][10,10]",
):
    values = {
        "text": text,
        "resource-id": resource_id,
        "class": class_name,
        "package": package,
        "content-desc": "",
        "checkable": str(checkable).lower(),
        "checked": str(checked).lower(),
        "clickable": str(clickable).lower(),
        "enabled": str(enabled).lower(),
        "focusable": "false",
        "focused": "false",
        "scrollable": "false",
        "long-clickable": "false",
        "password": "false",
        "selected": str(selected).lower(),
        "bounds": bounds,
    }
    attrs = " ".join(f'{key}="{value}"' for key, value in values.items())
    return f"<node {attrs}/>"


def hierarchy(*nodes, rotation=0):
    root = node(
        class_name="android.widget.FrameLayout",
        bounds="[0,0][800,1280]",
    )
    return (
        f'<?xml version="1.0"?><hierarchy rotation="{rotation}">'
        + root
        + "".join(nodes)
        + "</hierarchy>"
    )


def rgba_png(width, height, pixels):
    raw = b"".join(
        b"\x00" + pixels[row * width * 4 : (row + 1) * width * 4]
        for row in range(height)
    )

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk("IHDR".encode(), struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk("IDAT".encode(), zlib.compress(raw))
        + chunk("IEND".encode(), b"")
    )


def monitor_xml(runtime="Instrument panel Continental", labels=()):
    nodes = [
        node(
            text="System status",
            resource_id=f"{campaign.SAFE_ID_PREFIX}system_status_label",
        ),
        node(
            text=f"Connected to {runtime}",
            resource_id=f"{campaign.SAFE_ID_PREFIX}connectStatus1",
        ),
        node(
            text="Monitor parameters",
            resource_id=f"{campaign.SAFE_ID_PREFIX}checkParSelect",
            checkable=True,
            checked=True,
            clickable=True,
        ),
        node(
            text="ADD/REMOVE",
            resource_id=f"{campaign.SAFE_ID_PREFIX}bSelectParameters",
            clickable=True,
        ),
        node(
            resource_id=f"{campaign.SAFE_ID_PREFIX}bStartmonitoring",
            class_name="android.widget.ImageButton",
            clickable=True,
        ),
        node(
            resource_id=f"{campaign.SAFE_ID_PREFIX}tB2",
            class_name="android.widget.ImageButton",
            clickable=True,
            selected=True,
        ),
    ]
    for index, label in enumerate(labels, 1):
        nodes.append(
            node(
                text=f"{label}: ",
                resource_id=f"{campaign.SAFE_ID_PREFIX}labelPar{index}",
            )
        )
    return hierarchy(*nodes)


def selection_xml(labels, checked=()):
    rows = [
        node(
            text=f"{label}: ",
            resource_id="android:id/text1",
            class_name="android.widget.CheckedTextView",
            package=campaign.PACKAGE,
            checkable=True,
            checked=label in checked,
            clickable=True,
            bounds=f"[10,{100 + index * 40}][790,{138 + index * 40}]",
        )
        for index, label in enumerate(labels)
    ]
    return hierarchy(
        node(
            text="Select parameters to monitor",
            resource_id=f"{campaign.SAFE_ID_PREFIX}dialog_title",
        ),
        *rows,
        node(
            text="OK",
            resource_id="android:id/button1",
            class_name="android.widget.Button",
            package="android",
            clickable=True,
            bounds="[700,1100][790,1180]",
        ),
    )


def plan_payload():
    labels = [
        "Actual Gear",
        "Engine speed",
        "Vehicle speed",
        "Battery Voltage (+30)",
    ]
    return {
        "schema_version": 1,
        "campaign_id": "cluster-test",
        "module_key": "cluster",
        "expected_runtime": "Instrument panel Continental",
        "expected_app_version": "2.4.4.0",
        "expected_screen": {"width": 800, "height": 1280, "rotation": 0},
        "dialog_labels": labels,
        "gauges": ["Engine speed", "Vehicle speed"],
        "repeat_anchors": ["Engine speed"],
        "segment_seconds": 5,
        "settle_seconds": 0,
        "verify_seconds": 1,
        "min_free_bytes": 104857600,
        "min_tablet_free_bytes": 104857600,
        "artifacts": [
            "AlfaOBD_Debug.bin",
            "MARELLI_DASH_EP_Info.log",
            "Gauges_Data.csv",
        ],
        "required_segment_growth": [
            "AlfaOBD_Debug.bin",
            "MARELLI_DASH_EP_Info.log",
        ],
        "required_stop_stability": [
            "MARELLI_DASH_EP_Info.log",
        ],
    }


class PlanTests(unittest.TestCase):
    def write_plan(self, directory, payload=None):
        path = Path(directory) / "plan.json"
        path.write_text(json.dumps(payload or plan_payload()), encoding="utf-8")
        return path

    def test_plan_is_offline_and_creates_no_output_directory(self):
        class ExplodingRunner:
            def run(self, *args, **kwargs):
                raise AssertionError("offline plan invoked subprocess")

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_plan(directory)
            output = Path(directory) / "should-not-exist"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = campaign.main(
                    ["plan", str(path)], runner=ExplodingRunner()
                )

            self.assertEqual(result, 0)
            self.assertFalse(output.exists())
            self.assertIn("OFFLINE PLAN ONLY", stdout.getvalue())
            self.assertIn('"schedule"', stdout.getvalue())

    def test_schedule_appends_nonconsecutive_anchor_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = campaign.load_plan(self.write_plan(directory))

        self.assertEqual(
            plan.schedule,
            ("Engine speed", "Vehicle speed", "Engine speed"),
        )

    def test_tracked_cluster_shakedown_plan_is_valid(self):
        plan = campaign.load_plan(
            REPO
            / "projects"
            / "ecu_mapping"
            / "configs"
            / "alfaobd_cluster_singleton_shakedown.json"
        )
        self.assertEqual(plan.module_key, "cluster")
        self.assertEqual(len(plan.gauges), 5)
        self.assertEqual(
            plan.schedule[-2:],
            ("Engine speed", "Battery Voltage (+30)"),
        )
        self.assertEqual(
            set(plan.required_segment_growth),
            {"AlfaOBD_Debug.bin", "MARELLI_DASH_EP_Info.log"},
        )
        self.assertEqual(
            plan.required_stop_stability,
            ("MARELLI_DASH_EP_Info.log",),
        )

    def test_tracked_cluster_scaling_drive_plan_is_valid(self):
        plan = campaign.load_plan(
            REPO
            / "projects"
            / "ecu_mapping"
            / "configs"
            / "alfaobd_cluster_scaling_drive.json"
        )
        self.assertEqual(plan.module_key, "cluster")
        self.assertEqual(
            plan.gauges,
            (
                "Battery Voltage (+30)",
                "Engine speed",
                "Vehicle speed",
                "Actual Gear",
                "Outside temperature",
            ),
        )
        self.assertEqual(
            plan.schedule,
            (
                "Battery Voltage (+30)",
                "Engine speed",
                "Vehicle speed",
                "Actual Gear",
                "Outside temperature",
                "Engine speed",
                "Vehicle speed",
                "Battery Voltage (+30)",
            ),
        )
        self.assertEqual(plan.schedule[1:3], plan.schedule[5:7])
        self.assertEqual(plan.segment_seconds, 45)
        self.assertEqual(
            set(plan.required_segment_growth),
            {"AlfaOBD_Debug.bin", "MARELLI_DASH_EP_Info.log"},
        )
        self.assertEqual(
            plan.required_stop_stability,
            ("MARELLI_DASH_EP_Info.log",),
        )

    def test_unknown_gauge_and_unsafe_artifact_are_rejected(self):
        payload = plan_payload()
        payload["gauges"] = ["Unknown"]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(campaign.CampaignError, "absent"):
                campaign.load_plan(self.write_plan(directory, payload))

        payload = plan_payload()
        payload["screenshot_each_segment"] = "false"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(campaign.CampaignError, "JSON boolean"):
                campaign.load_plan(self.write_plan(directory, payload))

        payload = plan_payload()
        payload["artifacts"] = ["../VIN.log"]
        payload["required_segment_growth"] = ["../VIN.log"]
        payload["required_stop_stability"] = ["../VIN.log"]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(campaign.CampaignError, "plain filenames"):
                campaign.load_plan(self.write_plan(directory, payload))

    def test_missing_run_gates_fail_before_any_command_or_output(self):
        class ExplodingRunner:
            def run(self, *args, **kwargs):
                raise AssertionError("missing gates invoked subprocess")

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_plan(directory)
            out_root = Path(directory) / "out"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = campaign.main(
                    [
                        "run",
                        str(path),
                        "--conditions",
                        "parked fixture",
                        "--out-root",
                        str(out_root),
                    ],
                    runner=ExplodingRunner(),
                )

            self.assertEqual(result, 2)
            self.assertFalse(out_root.exists())
            self.assertIn("--execute", stderr.getvalue())

    def test_execute_requires_named_mount_before_any_command_or_output(self):
        class ExplodingRunner:
            def run(self, *args, **kwargs):
                raise AssertionError("missing mount invoked subprocess")

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_plan(directory)
            out_root = Path(directory) / "out"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = campaign.main(
                    [
                        "run",
                        str(path),
                        "--conditions",
                        "parked fixture",
                        "--out-root",
                        str(out_root),
                        "--execute",
                        "--confirm-read-only-diagnostics",
                        "--confirm-parked-shakedown",
                        "--confirm-monitor-stopped",
                    ],
                    runner=ExplodingRunner(),
                )

            self.assertEqual(result, 2)
            self.assertFalse(out_root.exists())
            self.assertIn("--require-mount", stderr.getvalue())


class UiGuardTests(unittest.TestCase):
    def test_accepts_exact_status_monitor_and_labels(self):
        xml_text = monitor_xml(labels=("Engine speed",))
        nodes = campaign.validate_monitor_page(
            xml_text,
            expected_runtime="Instrument panel Continental",
            expected_rotation=0,
            expected_width=800,
            expected_height=1280,
            expected_labels=("Engine speed",),
        )

        self.assertEqual(campaign.monitor_labels(nodes), ("Engine speed",))

    def test_rejects_runtime_rotation_and_active_diagnostics(self):
        with self.assertRaisesRegex(campaign.CampaignError, "runtime mismatch"):
            campaign.validate_monitor_page(
                monitor_xml(runtime="Wrong"),
                expected_runtime="Instrument panel Continental",
                expected_rotation=0,
                expected_width=800,
                expected_height=1280,
            )

        with self.assertRaisesRegex(campaign.CampaignError, "rotation mismatch"):
            campaign.validate_monitor_page(
                monitor_xml().replace(
                    '<hierarchy rotation="0">', '<hierarchy rotation="1">'
                ),
                expected_runtime="Instrument panel Continental",
                expected_rotation=0,
                expected_width=800,
                expected_height=1280,
            )

        dangerous = monitor_xml().replace(
            "</hierarchy>",
            node(
                text="START",
                resource_id=f"{campaign.SAFE_ID_PREFIX}bStart",
                clickable=True,
            )
            + "</hierarchy>",
        )
        with self.assertRaisesRegex(campaign.CampaignError, "Active Diagnostics"):
            campaign.validate_monitor_page(
                dangerous,
                expected_runtime="Instrument panel Continental",
                expected_rotation=0,
                expected_width=800,
                expected_height=1280,
            )

    def test_dialog_requires_exact_complete_label_order_and_single_ok(self):
        labels = ("Actual Gear", "Engine speed", "Vehicle speed")
        rows, ok = campaign.dialog_rows(
            selection_xml(labels, checked=("Engine speed",)), labels
        )

        self.assertEqual(
            [_label(row.text) for row in rows],
            list(labels),
        )
        self.assertEqual([_label(row.text) for row in rows if row.checked], ["Engine speed"])
        self.assertEqual(ok.resource_id, "android:id/button1")

        with self.assertRaisesRegex(campaign.CampaignError, "labels changed"):
            campaign.dialog_rows(selection_xml(labels), tuple(reversed(labels)))

    def test_label_filename_cannot_escape_output_directory(self):
        name = campaign._filename_label("../../Brake switch / value")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)
        self.assertRegex(name, r"-[0-9a-f]{8}$")

    def test_monitor_page_rejects_modal_overlay(self):
        modal = monitor_xml().replace(
            "</hierarchy>",
            node(
                text="CONTINUE",
                resource_id="android:id/button1",
                class_name="android.widget.Button",
                package="android",
                clickable=True,
            )
            + "</hierarchy>",
        )
        with self.assertRaisesRegex(campaign.CampaignError, "modal dialog"):
            campaign.validate_monitor_page(
                modal,
                expected_runtime="Instrument panel Continental",
                expected_rotation=0,
                expected_width=800,
                expected_height=1280,
            )

    def test_monitor_icon_visual_oracle_distinguishes_play_and_stop_hand(self):
        width = height = 20
        red = bytes((220, 29, 17, 255))
        white = bytes((255, 255, 255, 255))
        stopped = red * (width * height)
        running = white * 100 + red * (width * height - 100)
        button = campaign.UiNode(
            text="",
            resource_id=f"{campaign.SAFE_ID_PREFIX}bStartmonitoring",
            class_name="android.widget.ImageButton",
            package=campaign.PACKAGE,
            checkable=False,
            checked=False,
            clickable=True,
            enabled=True,
            selected=False,
            bounds=campaign.Bounds(0, 0, width, height),
        )
        self.assertEqual(
            campaign.monitor_visual_state(
                rgba_png(width, height, stopped),
                button,
                expected_width=width,
                expected_height=height,
            ),
            "stopped",
        )
        self.assertEqual(
            campaign.monitor_visual_state(
                rgba_png(width, height, running),
                button,
                expected_width=width,
                expected_height=height,
            ),
            "running",
        )


def _label(text):
    return text.strip().removesuffix(":").strip()


class ProvenanceTests(unittest.TestCase):
    def test_foreground_package_accepts_app_owned_android_7_dialog(self):
        class Result:
            returncode = 0
            stdout = (
                "  mCurrentFocus=Window{48ff0d0d0 u0 Select gauges to scan}\n"
                "  mFocusedApp=AppWindowToken{token=Token{activity "
                "com.AlfaOBD.AlfaOBD/.AlfaOBDConnect}}\n"
            )
            stderr = ""

        class Runner:
            def run(self, _command, **_kwargs):
                return Result()

        adb = campaign.AdbClient(Runner(), "fixture")
        self.assertEqual(adb.foreground_package(), campaign.PACKAGE)

    def test_foreground_package_rejects_foreign_dialog_and_app(self):
        class Result:
            returncode = 0
            stdout = (
                "  mCurrentFocus=Window{abc u0 Foreign dialog}\n"
                "  mFocusedApp=AppWindowToken{com.android.settings/.Settings}\n"
            )
            stderr = ""

        class Runner:
            def run(self, _command, **_kwargs):
                return Result()

        adb = campaign.AdbClient(Runner(), "fixture")
        with self.assertRaisesRegex(campaign.CampaignError, "not foreground"):
            adb.foreground_package()

    def test_dump_ui_uses_one_compressed_adb_round_trip(self):
        class Result:
            returncode = 0
            stdout = '<hierarchy rotation="0"></hierarchy>'
            stderr = ""

        class Runner:
            def __init__(self):
                self.commands = []

            def run(self, command, **_kwargs):
                self.commands.append(command)
                return Result()

        runner = Runner()
        adb = campaign.AdbClient(runner, "fixture")
        self.assertEqual(adb.dump_ui(), Result.stdout)
        self.assertEqual(len(runner.commands), 1)
        command = runner.commands[0]
        self.assertEqual(command[-4:-1], ["exec-out", "sh", "-c"])
        self.assertIn("uiautomator dump --compressed", command[-1])
        self.assertIn("&& cat ", command[-1])

    def test_artifact_stat_does_not_confuse_missing_utility_with_missing_file(self):
        class Result:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        class Runner:
            def __init__(self, results):
                self.results = iter(results)

            def run(self, *_args, **_kwargs):
                return next(self.results)

        adb = campaign.AdbClient(
            Runner(
                [
                    Result(127, stderr="sh: stat: not found"),
                    Result(1, stderr="wc: permission denied"),
                ]
            ),
            "fixture",
        )
        with self.assertRaisesRegex(campaign.CampaignError, "could not stat"):
            adb.artifact_stat("AlfaOBD_Debug.bin")

        adb = campaign.AdbClient(
            Runner(
                [
                    Result(1, stderr="stat: No such file or directory"),
                    Result(1, stderr="wc: No such file or directory"),
                ]
            ),
            "fixture",
        )
        self.assertIsNone(adb.artifact_stat("AlfaOBD_Debug.bin").size)

    def test_artifact_stat_uses_direct_android_argv_and_parses_wc_filename(self):
        class Result:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        class Runner:
            def __init__(self):
                self.commands = []

            def run(self, command, **_kwargs):
                self.commands.append(command)
                if "stat" in command:
                    return Result(1, stderr="stat unavailable")
                return Result(
                    0,
                    stdout=(
                        "2962706 "
                        "/sdcard/Android/data/com.android.AlfaOBD/files/logs/"
                        "AlfaOBD_Debug.bin\n"
                    ),
                )

        runner = Runner()
        adb = campaign.AdbClient(runner, "fixture")
        self.assertEqual(adb.artifact_stat("AlfaOBD_Debug.bin").size, 2962706)
        self.assertEqual(
            runner.commands[0][-5:-1], ["shell", "stat", "-c", "%s"]
        )
        self.assertEqual(runner.commands[1][-4:-1], ["shell", "wc", "-c"])
        self.assertNotIn("sh", runner.commands[0][2:])
        self.assertNotIn("sh", runner.commands[1][2:])

    def test_required_growth_and_artifact_shrink_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan_payload()), encoding="utf-8")
            plan = campaign.load_plan(path)

        before = {
            name: campaign.ArtifactStat(name, 100) for name in plan.artifacts
        }
        after = {
            name: campaign.ArtifactStat(name, 110) for name in plan.artifacts
        }
        campaign._validate_growth(plan, before, after)

        after["AlfaOBD_Debug.bin"] = campaign.ArtifactStat(
            "AlfaOBD_Debug.bin", 100
        )
        with self.assertRaisesRegex(campaign.CampaignError, "did not grow"):
            campaign._validate_growth(plan, before, after)

        after = {
            name: campaign.ArtifactStat(name, 110) for name in plan.artifacts
        }
        after["Gauges_Data.csv"] = campaign.ArtifactStat(
            "Gauges_Data.csv", 99
        )
        with self.assertRaisesRegex(campaign.CampaignError, "shrank"):
            campaign._validate_growth(plan, before, after)

        after = {
            name: campaign.ArtifactStat(name, 110) for name in plan.artifacts
        }
        after["Gauges_Data.csv"] = campaign.ArtifactStat(
            "Gauges_Data.csv", None
        )
        with self.assertRaisesRegex(campaign.CampaignError, "disappeared"):
            campaign._validate_growth(plan, before, after)

    def test_early_activity_accepts_one_witness_but_segment_requires_all(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan_payload()), encoding="utf-8")
            plan = campaign.load_plan(path)

        before = {
            name: campaign.ArtifactStat(name, 100) for name in plan.artifacts
        }
        grew = {
            name: campaign.ArtifactStat(name, 110) for name in plan.artifacts
        }
        one_stale = dict(grew)
        one_stale["MARELLI_DASH_EP_Info.log"] = campaign.ArtifactStat(
            "MARELLI_DASH_EP_Info.log", 100
        )
        self.assertTrue(campaign._any_required_artifact_grew(plan, before, grew))
        self.assertTrue(
            campaign._any_required_artifact_grew(plan, before, one_stale)
        )
        with self.assertRaisesRegex(campaign.CampaignError, "did not grow"):
            campaign._validate_growth(plan, before, one_stale)
        none_grew = {
            name: campaign.ArtifactStat(name, 100) for name in plan.artifacts
        }
        self.assertFalse(
            campaign._any_required_artifact_grew(plan, before, none_grew)
        )
        self.assertTrue(campaign._required_artifacts_stable(plan, grew, grew))
        debug_only_changed = dict(grew)
        debug_only_changed["AlfaOBD_Debug.bin"] = campaign.ArtifactStat(
            "AlfaOBD_Debug.bin", 111
        )
        self.assertTrue(
            campaign._required_artifacts_stable(plan, grew, debug_only_changed)
        )
        changed = dict(grew)
        changed["MARELLI_DASH_EP_Info.log"] = campaign.ArtifactStat(
            "MARELLI_DASH_EP_Info.log", 111
        )
        self.assertFalse(
            campaign._required_artifacts_stable(plan, grew, changed)
        )

    def test_required_segment_growth_artifacts_must_preexist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan_payload()), encoding="utf-8")
            plan = campaign.load_plan(path)

        stats = {
            name: campaign.ArtifactStat(name, 100) for name in plan.artifacts
        }
        stats["AlfaOBD_Debug.bin"] = campaign.ArtifactStat(
            "AlfaOBD_Debug.bin", None
        )
        with self.assertRaisesRegex(campaign.CampaignError, "already exist"):
            campaign._validate_required_preexisting(plan, stats)

    def test_abnormal_reconciliation_remains_sticky_after_verified_stop(self):
        self.assertTrue(
            campaign._manual_reconcile_required(
                monitoring=False,
                toggle_ambiguous=False,
                ui_reconcile=False,
                abnormal_reconcile=True,
            )
        )
        self.assertFalse(
            campaign._manual_reconcile_required(
                monitoring=False,
                toggle_ambiguous=False,
                ui_reconcile=False,
                abnormal_reconcile=False,
            )
        )

    def test_required_mount_contains_output_is_writable_and_keeps_identity(self):
        class Vfs:
            f_flag = 0

        class ReadOnlyVfs:
            f_flag = getattr(campaign.os, "ST_RDONLY", 1)

        class Stat:
            st_dev = 42

        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            output = mount / "obd-things" / "alfa"
            self.assertEqual(
                campaign.require_writable_mount(
                    output,
                    mount,
                    is_mount=lambda path: path == mount,
                    statvfs=lambda _path: Vfs(),
                    stat=lambda _path: Stat(),
                    access=lambda _path, _mode: True,
                ),
                42,
            )
            with self.assertRaisesRegex(campaign.CampaignError, "device changed"):
                campaign.require_writable_mount(
                    output,
                    mount,
                    is_mount=lambda _path: True,
                    statvfs=lambda _path: Vfs(),
                    stat=lambda _path: Stat(),
                    access=lambda _path, _mode: True,
                    expected_device=41,
                )
            with self.assertRaisesRegex(campaign.CampaignError, "read-only"):
                campaign.require_writable_mount(
                    output,
                    mount,
                    is_mount=lambda _path: True,
                    statvfs=lambda _path: ReadOnlyVfs(),
                    stat=lambda _path: Stat(),
                    access=lambda _path, _mode: True,
                )
            with self.assertRaisesRegex(campaign.CampaignError, "not writable"):
                campaign.require_writable_mount(
                    output,
                    mount,
                    is_mount=lambda _path: True,
                    statvfs=lambda _path: Vfs(),
                    stat=lambda _path: Stat(),
                    access=lambda _path, _mode: False,
                )
            with self.assertRaisesRegex(campaign.CampaignError, "below required mount"):
                campaign.require_writable_mount(
                    Path("/tmp/outside"),
                    mount,
                    is_mount=lambda _path: True,
                    statvfs=lambda _path: Vfs(),
                    stat=lambda _path: Stat(),
                    access=lambda _path, _mode: True,
                )

    def test_artifact_pull_is_size_aware_atomic_and_rejects_short_copy(self):
        class PullRunner:
            def __init__(self, payload, returncode=0):
                self.payload = payload
                self.returncode = returncode
                self.timeouts = []

            def run(self, command, **kwargs):
                self.timeouts.append(kwargs["timeout"])
                if self.returncode == 0:
                    Path(command[-1]).write_bytes(self.payload)
                return mock.Mock(
                    returncode=self.returncode,
                    stdout="",
                    stderr="pull failed" if self.returncode else "",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            short_runner = PullRunner(b"short")
            adb = campaign.AdbClient(short_runner, "serial")
            destination = root / "short.bin"
            with self.assertRaisesRegex(campaign.CampaignError, "short ADB pull"):
                adb.pull_artifact(
                    "AlfaOBD_Debug.bin",
                    destination,
                    expected_size=10,
                )
            self.assertFalse(destination.exists())
            self.assertTrue((root / "short.bin.partial").exists())

            failed_runner = PullRunner(b"", returncode=1)
            adb = campaign.AdbClient(failed_runner, "serial")
            destination = root / "failed.bin"
            with self.assertRaisesRegex(campaign.CampaignError, "ADB pull failed"):
                adb.pull_artifact(
                    "AlfaOBD_Debug.bin",
                    destination,
                    expected_size=10,
                )
            self.assertFalse(destination.exists())

            good_runner = PullRunner(b"complete")
            adb = campaign.AdbClient(good_runner, "serial")
            destination = root / "complete.bin"
            pulled_size, timeout = adb.pull_artifact(
                "AlfaOBD_Debug.bin",
                destination,
                expected_size=8,
            )
            self.assertEqual(pulled_size, 8)
            self.assertEqual(destination.read_bytes(), b"complete")
            self.assertGreaterEqual(timeout, campaign.MIN_PULL_TIMEOUT_SECONDS)
            self.assertLessEqual(timeout, campaign.MAX_PULL_TIMEOUT_SECONDS)

        self.assertGreater(
            campaign.AdbClient.pull_timeout_seconds(1024**3),
            campaign.AdbClient.pull_timeout_seconds(1024),
        )
        self.assertEqual(
            campaign.AdbClient.pull_timeout_seconds(1024**4),
            campaign.MAX_PULL_TIMEOUT_SECONDS,
        )

    def test_unavailable_logical_role_prevents_device_audit_and_directory_creation(self):
        plan = campaign.CampaignPlan(
            campaign_id="blocked",
            module_key="cluster",
            expected_runtime="Instrument panel Continental",
            expected_app_version="2.4.4.0",
            expected_width=800,
            expected_height=1280,
            expected_rotation=0,
            dialog_labels=("Engine speed",),
            gauges=("Engine speed",),
            repeat_anchors=(),
            segment_seconds=5,
            settle_seconds=0,
            verify_seconds=1,
            min_free_bytes=104857600,
            min_tablet_free_bytes=104857600,
            artifacts=("AlfaOBD_Debug.bin",),
            required_segment_growth=("AlfaOBD_Debug.bin",),
            required_stop_stability=("AlfaOBD_Debug.bin",),
            screenshot_each_segment=False,
        )
        adb = mock.Mock()
        runner = mock.Mock()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                campaign, "require_writable_mount", return_value=1234
            ),
            mock.patch.object(
                campaign.can_runtime_route,
                "acquire_passive_bus_route",
                side_effect=RuntimeError("c-can role busy"),
            ) as acquire,
        ):
            out = Path(directory) / "out"
            with self.assertRaisesRegex(campaign.CampaignError, "c-can role busy"):
                campaign.run_campaign(
                    plan,
                    adb,
                    runner,
                    out,
                    Path(directory),
                    "fixture",
                )
            self.assertFalse(out.exists())
            adb.resolve_serial.assert_not_called()
            acquire.assert_called_once_with("c-can")

    def test_service_query_error_is_not_treated_as_inactive(self):
        runner = mock.Mock()
        runner.run.return_value = mock.Mock(
            returncode=1,
            stdout="",
            stderr="Failed to connect to bus",
        )
        with self.assertRaisesRegex(campaign.CampaignError, "cannot establish"):
            campaign._service_active(runner, "tpms-logger")


if __name__ == "__main__":
    unittest.main()
