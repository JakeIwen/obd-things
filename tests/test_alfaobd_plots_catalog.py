import contextlib
from html import escape
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools import alfaobd_plots_catalog as plots


REPO = Path(__file__).resolve().parents[1]


def node(
    *,
    text="",
    resource_id="",
    class_name="android.widget.TextView",
    package=plots.PACKAGE,
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
    attrs = " ".join(
        f'{key}="{escape(str(value), quote=True)}"'
        for key, value in values.items()
    )
    return f"<node {attrs}/>"


def hierarchy(*nodes, rotation=0, root_bounds="[0,0][800,1280]"):
    root = node(
        class_name="android.widget.FrameLayout",
        bounds=root_bounds,
    )
    return (
        f'<?xml version="1.0"?><hierarchy rotation="{rotation}">'
        + root
        + "".join(nodes)
        + "</hierarchy>"
    )


def plots_page_xml(
    *,
    connection="Status: Connected to Chrysler Pentastar/Hemi engine Model Year 2021",
    labels=(),
    selected_tab=True,
    include_active=False,
):
    nodes = [
        node(
            text="Plotted Data",
            resource_id=f"{plots.SAFE_ID_PREFIX}plots_label",
        ),
        node(
            text=connection,
            resource_id=f"{plots.SAFE_ID_PREFIX}connectStatus4",
        ),
        node(
            text="SELECT GAUGES TO SCAN",
            resource_id=f"{plots.SAFE_ID_PREFIX}bSelectPlots",
            class_name="android.widget.Button",
            clickable=True,
            bounds="[3,163][740,214]",
        ),
        node(
            resource_id=f"{plots.SAFE_ID_PREFIX}bStartscan",
            class_name="android.widget.ImageButton",
            clickable=True,
            bounds="[748,163][796,214]",
        ),
        node(
            resource_id=f"{plots.SAFE_ID_PREFIX}tB5",
            class_name="android.widget.ImageButton",
            clickable=True,
            selected=selected_tab,
            bounds="[752,1232][800,1280]",
        ),
    ]
    for index, label in enumerate(labels, 1):
        nodes.append(
            node(
                text=label,
                resource_id=f"{plots.SAFE_ID_PREFIX}Plot{index}Title",
                bounds=f"[4,{216 + index * 40}][796,{237 + index * 40}]",
            )
        )
    if include_active:
        nodes.append(
            node(
                text="Active Diagnostics",
                resource_id=f"{plots.SAFE_ID_PREFIX}activediag_label",
            )
        )
    return hierarchy(*nodes)


def dialog_xml(labels, *, checked=(), title=plots.DIALOG_TITLE):
    list_bounds = "[100,400][700,800]"
    rows = []
    for index, label in enumerate(labels):
        top = 410 + index * 80
        rows.append(
            node(
                text=label,
                resource_id=plots.DIALOG_ROW_ID,
                class_name="android.widget.CheckedTextView",
                checkable=True,
                checked=label in checked,
                clickable=True,
                bounds=f"[110,{top}][690,{top + 70}]",
            )
        )
    return hierarchy(
        node(
            text=title,
            resource_id=f"{plots.SAFE_ID_PREFIX}dialog_title",
            bounds="[126,350][674,395]",
        ),
        node(
            resource_id=plots.DIALOG_LIST_ID,
            class_name="android.widget.ListView",
            bounds=list_bounds,
        ),
        *rows,
        node(
            resource_id="android:id/buttonPanel",
            class_name="android.widget.LinearLayout",
            package="android",
            bounds="[100,800][700,880]",
        ),
        node(
            text="OK",
            resource_id=plots.DIALOG_OK_ID,
            class_name="android.widget.Button",
            package="android",
            clickable=True,
            bounds="[598,810][662,865]",
        ),
    )


def plan_payload():
    return {
        "schema_version": 1,
        "campaign_id": "plots-test",
        "module_key": "pcm",
        "expected_app_version": "2.4.4.0",
        "expected_screen": {"width": 800, "height": 1280, "rotation": 0},
        "expected_connection_texts": [
            "Status: Connected to Chrysler Pentastar/Hemi engine Model Year 2021"
        ],
        "expected_catalog_count": 10,
        "expected_first_label": "A",
        "expected_last_label": "J",
        "required_labels": ["A", "F", "J"],
        "max_pages": 20,
        "swipe_duration_ms": 500,
        "settle_seconds": 0.1,
        "min_free_bytes": 104857600,
        "screenshot_each_page": False,
    }


def make_plan(payload=None):
    values = payload or plan_payload()
    return plots.CatalogPlan(
        campaign_id=values["campaign_id"],
        module_key=values["module_key"],
        expected_app_version=values["expected_app_version"],
        expected_width=values["expected_screen"]["width"],
        expected_height=values["expected_screen"]["height"],
        expected_rotation=values["expected_screen"]["rotation"],
        expected_connection_texts=tuple(values["expected_connection_texts"]),
        expected_catalog_count=values["expected_catalog_count"],
        expected_first_label=values["expected_first_label"],
        expected_last_label=values["expected_last_label"],
        required_labels=tuple(values["required_labels"]),
        expected_catalog_sha256=values.get("expected_catalog_sha256"),
        max_pages=values["max_pages"],
        swipe_duration_ms=values["swipe_duration_ms"],
        settle_seconds=values["settle_seconds"],
        min_free_bytes=values["min_free_bytes"],
        screenshot_each_page=values["screenshot_each_page"],
    )


class PlanTests(unittest.TestCase):
    def write_plan(self, directory, payload=None):
        path = Path(directory) / "plan.json"
        path.write_text(
            json.dumps(payload or plan_payload()),
            encoding="utf-8",
        )
        return path

    def test_tracked_pcm_catalog_plan_is_valid(self):
        plan = plots.load_plan(
            REPO
            / "projects"
            / "ecu_mapping"
            / "configs"
            / "alfaobd_pcm_plots_catalog.json"
        )
        self.assertEqual(plan.module_key, "pcm")
        self.assertEqual(plan.expected_catalog_count, 193)
        self.assertEqual(plan.expected_first_label, "Vehicle speed, km/h")
        self.assertEqual(plan.expected_last_label, "Transfer speed, rpm")
        self.assertIn("Engine oil pressure, KPa", plan.required_labels)
        self.assertIn("Current engine torque, Nm", plan.required_labels)
        self.assertNotIn(
            "Status: Connected. Device model not determined",
            plan.expected_connection_texts,
        )
        self.assertIsNone(plan.expected_catalog_sha256)

    def test_plan_is_offline_and_creates_no_output(self):
        class ExplodingRunner:
            def run(self, *args, **kwargs):
                raise AssertionError("offline plan invoked a subprocess")

        with tempfile.TemporaryDirectory() as directory:
            plan_path = self.write_plan(directory)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = plots.main(
                    ["plan", str(plan_path)],
                    runner=ExplodingRunner(),
                )

            self.assertEqual(result, 0)
            self.assertIn("OFFLINE PLAN ONLY", stdout.getvalue())
            self.assertEqual(
                sorted(Path(directory).iterdir()),
                [plan_path],
            )

    def test_unknown_key_and_bad_hash_are_rejected(self):
        payload = plan_payload()
        payload["typo"] = True
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(plots.CampaignError, "unknown"):
                plots.load_plan(self.write_plan(directory, payload))

        payload = plan_payload()
        payload["expected_catalog_sha256"] = "not-a-digest"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(plots.CampaignError, "SHA-256"):
                plots.load_plan(self.write_plan(directory, payload))

    def test_missing_live_gate_fails_before_subprocess_or_output(self):
        class ExplodingRunner:
            def run(self, *args, **kwargs):
                raise AssertionError("missing gates invoked a subprocess")

        with tempfile.TemporaryDirectory() as directory:
            plan_path = self.write_plan(directory)
            out_root = Path(directory) / "out"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = plots.main(
                    [
                        "inventory",
                        str(plan_path),
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


class UiParsingTests(unittest.TestCase):
    def test_plots_page_requires_exact_surface_and_returns_labels(self):
        plan = make_plan()
        nodes = plots.validate_plots_page(
            plots_page_xml(labels=("A",)),
            plan=plan,
            expected_labels=("A",),
        )
        self.assertEqual(plots.plot_labels(nodes), ("A",))

        with self.assertRaisesRegex(plots.CampaignError, "tB5"):
            plots.validate_plots_page(
                plots_page_xml(selected_tab=False),
                plan=plan,
            )
        with self.assertRaisesRegex(plots.CampaignError, "Active Diagnostics"):
            plots.validate_plots_page(
                plots_page_xml(include_active=True),
                plan=plan,
            )
        with self.assertRaisesRegex(plots.CampaignError, "connection text"):
            plots.validate_plots_page(
                plots_page_xml(
                    connection="Status: Connected. Device model not determined"
                ),
                plan=plan,
            )

    def test_dialog_page_preserves_exact_unicode_and_checked_state(self):
        page = plots.parse_dialog_page(
            dialog_xml(
                ("Coolant temperature, °C", "Engine oil pressure, KPa"),
                checked=("Engine oil pressure, KPa",),
            ),
            plan=make_plan(),
        )
        self.assertEqual(
            page.labels,
            ("Coolant temperature, °C", "Engine oil pressure, KPa"),
        )
        self.assertEqual(page.checked, (False, True))

        with self.assertRaisesRegex(plots.CampaignError, "title"):
            plots.parse_dialog_page(
                dialog_xml(("A", "B"), title="Wrong"),
                plan=make_plan(),
            )

    def test_dialog_rejects_row_outside_list(self):
        xml = dialog_xml(("A", "B")).replace(
            "[110,410][690,480]",
            "[90,410][690,480]",
        )
        with self.assertRaisesRegex(plots.CampaignError, "outside"):
            plots.parse_dialog_page(xml, plan=make_plan())


class MergeTests(unittest.TestCase):
    def test_forward_and_reverse_overlap_reproduce_catalog(self):
        forward, added = plots.merge_overlapping_page(
            ("A", "B", "C", "D"),
            ("C", "D", "E", "F"),
        )
        self.assertEqual(forward, ("A", "B", "C", "D", "E", "F"))
        self.assertEqual(added, 2)
        reverse, added = plots.merge_preceding_page(
            ("C", "D", "E", "F"),
            ("A", "B", "C", "D"),
        )
        self.assertEqual(reverse, forward)
        self.assertEqual(added, 2)

    def test_missing_overlap_and_cycle_fail_closed(self):
        with self.assertRaisesRegex(plots.CampaignError, "no suffix/prefix"):
            plots.merge_overlapping_page(("A", "B"), ("C", "D"))
        with self.assertRaisesRegex(plots.CampaignError, "cycled"):
            plots.merge_overlapping_page(
                ("A", "B", "C"),
                ("C", "A"),
            )

    def test_catalog_hash_is_exact_and_window_independent(self):
        labels = ("Coolant temperature, °C", "Oil pressure, KPa")
        digest = plots.catalog_sha256(labels)
        self.assertEqual(digest, plots.catalog_sha256(tuple(labels)))
        self.assertNotEqual(
            digest,
            plots.catalog_sha256(("Coolant temperature, |C", "Oil pressure, KPa")),
        )

    def test_catalog_report_uses_explicit_database_key_and_list_index(self):
        labels = (
            "Vehicle speed, km/h",
            "Front left wheel speed, km/h",
            "Front right wheel speed, km/h",
            "Rear left wheel speed, km/h",
            "Rear right wheel speed, km/h",
            "Target cruise speed, km/h",
            "Engine speed, rpm",
        )
        inventory = plots.CatalogInventory(
            labels=labels,
            checked_by_label={label: False for label in labels},
            pages=(),
            catalog_sha256=plots.catalog_sha256(labels),
        )
        engine = inventory.as_dict()["catalog"][6]
        self.assertEqual(engine["zero_based_index"], 6)
        self.assertEqual(engine["display_order_key"], 7)
        self.assertEqual(engine["label"], "Engine speed, rpm")


class FakeDialogAdb:
    def __init__(self, pages, checked=()):
        self.pages = tuple(tuple(page) for page in pages)
        self.checked = set(checked)
        self.index = 0
        self.swipes = []
        self.dump_count = 0

    def foreground_package(self):
        return plots.PACKAGE

    def dump_ui(self):
        self.dump_count += 1
        return dialog_xml(self.pages[self.index], checked=self.checked)

    def swipe(self, *, start, end, duration_ms):
        self.swipes.append(
            {"start": start, "end": end, "duration_ms": duration_ms}
        )
        if start[1] > end[1]:
            self.index = min(len(self.pages) - 1, self.index + 1)
        else:
            self.index = max(0, self.index - 1)

    def screenshot(self):
        raise AssertionError("screenshots disabled in the fixture")


class TraversalTests(unittest.TestCase):
    def test_bidirectional_inventory_is_deterministic_and_never_taps(self):
        pages = (
            ("A", "B", "C", "D"),
            ("C", "D", "E", "F"),
            ("E", "F", "G", "H"),
            ("G", "H", "I", "J"),
        )
        adb = FakeDialogAdb(pages, checked=("F",))
        plan = make_plan()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "adb"
            artifact_dir.mkdir()
            writer = plots.EventWriter(root)
            inventory = plots.inventory_open_dialog(
                plan,
                adb,
                writer,
                artifact_dir,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(
                inventory.labels,
                tuple("ABCDEFGHIJ"),
            )
            self.assertTrue(inventory.checked_by_label["F"])
            self.assertEqual(
                inventory.catalog_sha256,
                plots.catalog_sha256(tuple("ABCDEFGHIJ")),
            )
            self.assertTrue(
                any(page["phase"] == "reverse" for page in inventory.pages)
            )
            self.assertGreater(len(adb.swipes), len(pages))
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertTrue(any(row["event"] == "swipe_intent" for row in events))
            self.assertFalse(
                any(
                    row.get("event") == "tap_intent"
                    or row.get("gauge_row_tapped") is True
                    or row.get("ok_button_tapped") is True
                    for row in events
                )
            )

    def test_skipped_page_fails_without_fabricating_catalog(self):
        adb = FakeDialogAdb(
            (
                ("A", "B", "C", "D"),
                ("E", "F", "G", "H"),
            )
        )
        payload = plan_payload()
        payload["expected_catalog_count"] = 8
        payload["expected_last_label"] = "H"
        plan = make_plan(payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "adb"
            artifact_dir.mkdir()
            writer = plots.EventWriter(root)
            with self.assertRaisesRegex(plots.CampaignError, "no suffix/prefix"):
                plots.inventory_open_dialog(
                    plan,
                    adb,
                    writer,
                    artifact_dir,
                    sleep=lambda _seconds: None,
                )

    def test_validation_distinguishes_unpinned_candidate_and_mismatch(self):
        labels = tuple("ABCDEFGHIJ")
        inventory = plots.CatalogInventory(
            labels=labels,
            checked_by_label={label: False for label in labels},
            pages=(),
            catalog_sha256=plots.catalog_sha256(labels),
        )
        self.assertEqual(plots.validate_catalog(make_plan(), inventory), [])

        payload = plan_payload()
        payload["expected_catalog_sha256"] = "0" * 64
        errors = plots.validate_catalog(make_plan(payload), inventory)
        self.assertTrue(any("SHA-256" in error for error in errors))


class TypedAdbTests(unittest.TestCase):
    def test_swipe_and_back_remain_typed_commands(self):
        class Runner:
            def __init__(self):
                self.commands = []

            def run(self, command, **_kwargs):
                self.commands.append(command)
                return None

        runner = Runner()
        adb = plots.AdbClient(runner, "SERIAL")
        adb.swipe(start=(400, 700), end=(400, 500), duration_ms=500)
        adb.back()
        self.assertEqual(
            runner.commands,
            [
                [
                    "adb",
                    "-s",
                    "SERIAL",
                    "shell",
                    "input",
                    "swipe",
                    "400",
                    "700",
                    "400",
                    "500",
                    "500",
                ],
                [
                    "adb",
                    "-s",
                    "SERIAL",
                    "shell",
                    "input",
                    "keyevent",
                    "KEYCODE_BACK",
                ],
            ],
        )
        with self.assertRaisesRegex(plots.CampaignError, "duration"):
            adb.swipe(start=(0, 0), end=(1, 1), duration_ms=20)


if __name__ == "__main__":
    unittest.main()
