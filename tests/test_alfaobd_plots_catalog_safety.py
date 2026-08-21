import contextlib
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import alfaobd_plots_catalog as plots


def _node(
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
    attributes = " ".join(
        f'{key}="{escape(str(value), quote=True)}"'
        for key, value in values.items()
    )
    return f"<node {attributes}/>"


def _hierarchy(
    *nodes,
    rotation=0,
    root_bounds="[0,0][800,1280]",
):
    root = _node(
        class_name="android.widget.FrameLayout",
        bounds=root_bounds,
    )
    return (
        f'<?xml version="1.0"?><hierarchy rotation="{rotation}">'
        + root
        + "".join(nodes)
        + "</hierarchy>"
    )


def _plots_page_xml(*, selector_bounds="[3,163][740,214]", active=False):
    nodes = [
        _node(
            text="Plotted Data",
            resource_id=f"{plots.SAFE_ID_PREFIX}plots_label",
        ),
        _node(
            text="Connected to test PCM",
            resource_id=f"{plots.SAFE_ID_PREFIX}connectStatus4",
        ),
        _node(
            text="SELECT GAUGES TO SCAN",
            resource_id=f"{plots.SAFE_ID_PREFIX}bSelectPlots",
            class_name="android.widget.Button",
            clickable=True,
            bounds=selector_bounds,
        ),
        _node(
            resource_id=f"{plots.SAFE_ID_PREFIX}bStartscan",
            class_name="android.widget.ImageButton",
            clickable=True,
            bounds="[748,163][796,214]",
        ),
        _node(
            resource_id=f"{plots.SAFE_ID_PREFIX}tB5",
            class_name="android.widget.ImageButton",
            clickable=True,
            selected=True,
            bounds="[752,1232][800,1280]",
        ),
    ]
    if active:
        nodes.append(
            _node(
                text="Active Diagnostics",
                resource_id=f"{plots.SAFE_ID_PREFIX}activediag_label",
            )
        )
    return _hierarchy(*nodes)


def _dialog_xml(labels):
    rows = []
    for index, label in enumerate(labels):
        top = 410 + index * 70
        rows.append(
            _node(
                text=label,
                resource_id=plots.DIALOG_ROW_ID,
                class_name="android.widget.CheckedTextView",
                checkable=True,
                clickable=True,
                bounds=f"[110,{top}][690,{top + 60}]",
            )
        )
    return _hierarchy(
        _node(
            text=plots.DIALOG_TITLE,
            resource_id=f"{plots.SAFE_ID_PREFIX}dialog_title",
            bounds="[126,350][674,395]",
        ),
        _node(
            resource_id=plots.DIALOG_LIST_ID,
            class_name="android.widget.ListView",
            bounds="[100,400][700,800]",
        ),
        *rows,
        _node(
            text="OK",
            resource_id=plots.DIALOG_OK_ID,
            class_name="android.widget.Button",
            package="android",
            clickable=True,
            bounds="[598,810][662,865]",
        ),
    )


def _ambiguous_page_xml():
    return _hierarchy(
        _node(
            text="Unexpected page",
            resource_id=f"{plots.SAFE_ID_PREFIX}unexpected",
        )
    )


def _plan():
    return plots.CatalogPlan(
        campaign_id="plots-safety-test",
        module_key="pcm",
        expected_app_version="2.4.4.0",
        expected_width=800,
        expected_height=1280,
        expected_rotation=0,
        expected_connection_texts=("Connected to test PCM",),
        expected_catalog_count=4,
        expected_first_label="A",
        expected_last_label="D",
        required_labels=("A", "D"),
        expected_catalog_sha256=None,
        max_pages=12,
        swipe_duration_ms=500,
        settle_seconds=0.1,
        min_free_bytes=100 * 1024**2,
        screenshot_each_page=False,
    )


@contextlib.contextmanager
def _patched_inventory_runtime():
    class Ownership:
        route = mock.Mock(channel="can7", role="c-can")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def revalidate(self):
            return None

    ownership = Ownership()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                plots,
                "_ui_supervisor_lock",
                return_value=contextlib.nullcontext(),
            )
        )
        stack.enter_context(
            mock.patch.object(plots, "_service_active", return_value=False)
        )
        stack.enter_context(mock.patch.object(plots, "_disk_guard", return_value=10**9))
        stack.enter_context(
            mock.patch.object(plots, "monitor_visual_state", return_value="stopped")
        )
        stack.enter_context(
            mock.patch.object(
                plots.can_runtime_route,
                "acquire_passive_bus_route",
                return_value=ownership,
            )
        )
        yield


class _TapFailureAdb:
    """ADB fake whose tap may have reached Android before the command failed."""

    def __init__(
        self,
        *,
        initial_xml,
        fresh_xml,
        after_tap_xml,
        after_back_xml=None,
    ):
        self.serial = "SAFETY-SERIAL"
        self.initial_xml = initial_xml
        self.fresh_xml = fresh_xml
        self.after_tap_xml = after_tap_xml
        self.after_back_xml = after_back_xml or fresh_xml
        self.mode = "before_tap"
        self.before_tap_dumps = 0
        self.foreground_checks = 0
        self.taps = []
        self.back_calls = 0

    def resolve_serial(self):
        return self.serial

    def package_version(self):
        return "2.4.4.0"

    def foreground_package(self):
        self.foreground_checks += 1
        return plots.PACKAGE

    def dump_ui(self):
        if self.mode == "after_tap":
            return self.after_tap_xml
        if self.mode == "after_back":
            return self.after_back_xml
        self.before_tap_dumps += 1
        if self.before_tap_dumps == 1:
            return self.initial_xml
        return self.fresh_xml

    def screenshot(self):
        return b"synthetic screenshot"

    def tap(self, node):
        self.taps.append(node)
        self.mode = "after_tap"
        raise plots.CampaignError("ADB tap timed out after possible delivery")

    def back(self):
        self.back_calls += 1
        self.mode = "after_back"


class InventoryInputSafetyTests(unittest.TestCase):
    def _run(self, adb, out_root):
        with _patched_inventory_runtime():
            return plots.run_inventory(
                _plan(),
                adb,
                object(),
                out_root,
                "parked synthetic safety fixture",
            )

    def test_changed_surface_is_rejected_before_selector_tap(self):
        adb = _TapFailureAdb(
            initial_xml=_plots_page_xml(),
            fresh_xml=_plots_page_xml(active=True),
            after_tap_xml=_dialog_xml(("A", "B")),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(plots.CampaignError):
                self._run(adb, Path(directory))

        self.assertEqual(adb.taps, [])
        self.assertGreaterEqual(adb.foreground_checks, 2)

    def test_tap_uses_selector_from_freshly_revalidated_hierarchy(self):
        old_bounds = "[3,163][303,214]"
        fresh_bounds = "[303,163][703,214]"
        adb = _TapFailureAdb(
            initial_xml=_plots_page_xml(selector_bounds=old_bounds),
            fresh_xml=_plots_page_xml(selector_bounds=fresh_bounds),
            after_tap_xml=_ambiguous_page_xml(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(plots.CampaignError):
                self._run(adb, root)
            state = json.loads(
                (root / _plan().campaign_id / "state.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(len(adb.taps), 1)
        self.assertEqual(
            adb.taps[0].bounds,
            plots.Bounds(left=303, top=163, right=703, bottom=214),
        )
        self.assertTrue(state["manual_reconcile"])

    def test_tap_error_cancels_an_exactly_observed_selector(self):
        page = _plots_page_xml()
        adb = _TapFailureAdb(
            initial_xml=page,
            fresh_xml=page,
            after_tap_xml=_dialog_xml(("A", "B")),
            after_back_xml=page,
        )
        termination_guard = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                plots.diagnostic_safety,
                "interrupt_on_termination",
                return_value=contextlib.nullcontext(termination_guard),
            ):
                with self.assertRaisesRegex(
                    plots.CampaignError,
                    "possible delivery",
                ):
                    self._run(adb, root)
            state = json.loads(
                (root / _plan().campaign_id / "state.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(adb.back_calls, 1)
        self.assertFalse(state["manual_reconcile"])
        termination_guard.begin_cleanup.assert_called()

    def test_tap_error_preserves_reconcile_when_ui_is_ambiguous(self):
        page = _plots_page_xml()
        adb = _TapFailureAdb(
            initial_xml=page,
            fresh_xml=page,
            after_tap_xml=_ambiguous_page_xml(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                plots.CampaignError,
                "possible delivery",
            ):
                self._run(adb, root)
            state = json.loads(
                (root / _plan().campaign_id / "state.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(adb.back_calls, 0)
        self.assertTrue(state["manual_reconcile"])


class _StaleSwipeAdb:
    def __init__(self, dumps):
        self.dumps = list(dumps)
        self.dump_calls = 0
        self.swipes = []

    def foreground_package(self):
        return plots.PACKAGE

    def dump_ui(self):
        self.dump_calls += 1
        if len(self.dumps) > 1:
            return self.dumps.pop(0)
        return self.dumps[0]

    def swipe(self, *, start, end, duration_ms):
        self.swipes.append(
            {
                "start": start,
                "end": end,
                "duration_ms": duration_ms,
            }
        )


class StaleHierarchyTests(unittest.TestCase):
    def _swipe(self, adb, current_xml):
        current_page = plots.parse_dialog_page(current_xml, plan=_plan())
        with tempfile.TemporaryDirectory() as directory:
            writer = plots.EventWriter(Path(directory))
            return plots._swipe_dialog(
                _plan(),
                adb,
                writer,
                current_page,
                phase="forward",
                page_index=0,
                toward="later",
                sleep=lambda _seconds: None,
            )

    def test_post_swipe_wait_ignores_two_stale_pre_swipe_hierarchies(self):
        old_xml = _dialog_xml(("A", "B"))
        new_xml = _dialog_xml(("B", "C"))
        # The first old hierarchy is the immediate pre-input revalidation.
        # The next two are stale UIAutomator results observed after delivery.
        adb = _StaleSwipeAdb(
            [old_xml, old_xml, old_xml, new_xml, new_xml]
        )

        _, page, transitioned = self._swipe(adb, old_xml)

        self.assertEqual(page.labels, ("B", "C"))
        self.assertTrue(transitioned)
        self.assertEqual(len(adb.swipes), 1)
        self.assertGreaterEqual(adb.dump_calls, 5)

    def test_unchanged_boundary_settles_only_after_stale_grace(self):
        boundary_xml = _dialog_xml(("C", "D"))
        adb = _StaleSwipeAdb([boundary_xml] * 8)

        _, page, transitioned = self._swipe(adb, boundary_xml)

        self.assertEqual(page.labels, ("C", "D"))
        self.assertFalse(transitioned)
        self.assertEqual(len(adb.swipes), 1)
        self.assertGreaterEqual(adb.dump_calls, 5)

    def test_rotation_or_resize_before_swipe_sends_no_input(self):
        current_xml = _dialog_xml(("A", "B"))
        current_page = plots.parse_dialog_page(current_xml, plan=_plan())
        invalid_hierarchies = (
            current_xml.replace('rotation="0"', 'rotation="1"', 1),
            current_xml.replace(
                'bounds="[0,0][800,1280]"',
                'bounds="[0,0][1280,800]"',
                1,
            ),
        )
        for invalid_xml in invalid_hierarchies:
            with self.subTest(invalid_xml=invalid_xml[:80]):
                adb = _StaleSwipeAdb([invalid_xml])
                with tempfile.TemporaryDirectory() as directory:
                    writer = plots.EventWriter(Path(directory))
                    with self.assertRaisesRegex(
                        plots.CampaignError,
                        "rotation mismatch|extends outside",
                    ):
                        plots._swipe_dialog(
                            _plan(),
                            adb,
                            writer,
                            current_page,
                            phase="forward",
                            page_index=0,
                            toward="later",
                            sleep=lambda _seconds: None,
                        )

                self.assertEqual(adb.swipes, [])


if __name__ == "__main__":
    unittest.main()
