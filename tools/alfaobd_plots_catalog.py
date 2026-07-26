#!/usr/bin/env python3
"""Inventory AlfaOBD's scrollable Plots selector without changing its selection.

The existing singleton campaign intentionally supports only the bounded System
status selector.  This companion tool handles the distinct Plots surface,
``Select gauges to scan``, in two phases:

* ``plan`` validates a JSON plan completely offline.
* ``audit`` observes an already-open, stopped Plots page without sending input.
* ``inventory`` opens the selector, performs bounded overlapping swipes, records
  the exact live label order/check state, and exits with Android BACK.  It never
  taps a gauge row or the dialog's OK button, and it never starts a scan.

Live inventory requires explicit execution, parked/read-only-navigation, and
stopped-scan confirmations.  Every input intent is fsynced first.  The complete
catalog remains machine output below ``tmp/``; a later scalar campaign must pin
its hash before using the catalog to select anything.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Callable, Iterable


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import diagnostic_safety  # noqa: E402
from lib.modules import MODULES  # noqa: E402
from tools.alfaobd_singleton_campaign import (  # noqa: E402
    ACTIVE_DIAGNOSTIC_IDS,
    BLOCKED_SERVICES,
    BLOCKING_DIALOG_TEXT,
    CAMPAIGN_ID_RE,
    PACKAGE,
    SAFE_ID_PREFIX,
    AdbClient,
    Bounds,
    CampaignError,
    CommandRunner,
    EventWriter,
    UiNode,
    _disk_guard,
    _one_by_id,
    _service_active,
    _tap_with_intent,
    _ui_supervisor_lock,
    _write_bytes,
    _write_text,
    monitor_visual_state,
    parse_ui_xml,
)


DEFAULT_OUT_ROOT = REPO / "tmp" / "ecu_mapping" / "alfaobd_plots_catalog"
PLOTS_PAGE_IDS = {
    f"{SAFE_ID_PREFIX}plots_label",
    f"{SAFE_ID_PREFIX}connectStatus4",
    f"{SAFE_ID_PREFIX}bSelectPlots",
    f"{SAFE_ID_PREFIX}bStartscan",
    f"{SAFE_ID_PREFIX}tB5",
}
DIALOG_TITLE = "Select gauges to scan"
DIALOG_LIST_ID = "android:id/select_dialog_listview"
DIALOG_ROW_ID = "android:id/text1"
DIALOG_OK_ID = "android:id/button1"
CATALOG_HASH_DOMAIN = b"alfaobd-plots-catalog-v1\0"
MIN_FREE_BYTES = 100 * 1024**2
MAX_CONNECTION_TEXTS = 8
MAX_REQUIRED_LABELS = 256
MAX_PAGES_LIMIT = 512
PAGE_STABLE_SWIPES = 2
POST_SWIPE_UNCHANGED_OBSERVATIONS = 4
MAX_STABILITY_OBSERVATIONS = 10
PLOTS_PAGE_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CatalogPlan:
    campaign_id: str
    module_key: str
    expected_app_version: str
    expected_width: int
    expected_height: int
    expected_rotation: int
    expected_connection_texts: tuple[str, ...]
    expected_catalog_count: int
    expected_first_label: str
    expected_last_label: str
    required_labels: tuple[str, ...]
    expected_catalog_sha256: str | None
    max_pages: int
    swipe_duration_ms: int
    settle_seconds: float
    min_free_bytes: int
    screenshot_each_page: bool

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "module_key": self.module_key,
            "expected_app_version": self.expected_app_version,
            "expected_screen": {
                "width": self.expected_width,
                "height": self.expected_height,
                "rotation": self.expected_rotation,
            },
            "expected_connection_texts": list(self.expected_connection_texts),
            "expected_catalog_count": self.expected_catalog_count,
            "expected_first_label": self.expected_first_label,
            "expected_last_label": self.expected_last_label,
            "required_labels": list(self.required_labels),
            "max_pages": self.max_pages,
            "swipe_duration_ms": self.swipe_duration_ms,
            "settle_seconds": self.settle_seconds,
            "min_free_bytes": self.min_free_bytes,
            "screenshot_each_page": self.screenshot_each_page,
        }
        if self.expected_catalog_sha256 is not None:
            payload["expected_catalog_sha256"] = self.expected_catalog_sha256
        return payload


@dataclass(frozen=True)
class DialogPage:
    labels: tuple[str, ...]
    checked: tuple[bool, ...]
    list_bounds: Bounds
    ok: UiNode

    def as_dict(self) -> dict[str, object]:
        return {
            "labels": list(self.labels),
            "checked": list(self.checked),
            "list_bounds": [
                self.list_bounds.left,
                self.list_bounds.top,
                self.list_bounds.right,
                self.list_bounds.bottom,
            ],
        }


@dataclass(frozen=True)
class CatalogInventory:
    labels: tuple[str, ...]
    checked_by_label: dict[str, bool]
    pages: tuple[dict[str, object], ...]
    catalog_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "catalog_sha256": self.catalog_sha256,
            "label_count": len(self.labels),
            "catalog": [
                {
                    "zero_based_index": index,
                    "display_order_key": index + 1,
                    "label": label,
                    "checked": self.checked_by_label[label],
                }
                for index, label in enumerate(self.labels)
            ],
            "pages": list(self.pages),
        }


def _strings(
    payload: dict[str, object],
    key: str,
    *,
    maximum: int,
) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise CampaignError(f"{key} must be a non-empty JSON string list")
    if len(value) > maximum:
        raise CampaignError(f"{key} exceeds its {maximum}-item safety cap")
    if not all(isinstance(item, str) for item in value):
        raise CampaignError(f"{key} must contain only strings")
    cleaned = tuple(item.strip() for item in value)
    if any(not item for item in cleaned):
        raise CampaignError(f"{key} contains an empty item")
    if len(set(cleaned)) != len(cleaned):
        raise CampaignError(f"{key} must be unique")
    return cleaned


def load_plan(path: Path) -> CatalogPlan:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot read catalog plan {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CampaignError("catalog plan root must be a JSON object")
    if payload.get("schema_version") != 1:
        raise CampaignError("catalog plan schema_version must be 1")

    allowed_keys = {
        "schema_version",
        "campaign_id",
        "module_key",
        "expected_app_version",
        "expected_screen",
        "expected_connection_texts",
        "expected_catalog_count",
        "expected_first_label",
        "expected_last_label",
        "required_labels",
        "expected_catalog_sha256",
        "max_pages",
        "swipe_duration_ms",
        "settle_seconds",
        "min_free_bytes",
        "screenshot_each_page",
    }
    unknown = sorted(set(payload) - allowed_keys)
    if unknown:
        raise CampaignError(f"unknown catalog plan keys: {unknown}")

    campaign_id = str(payload.get("campaign_id", "")).strip()
    if not CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise CampaignError(
            "campaign_id must be a safe 1-80 character filename component"
        )
    module_key = str(payload.get("module_key", "")).strip()
    if module_key not in MODULES:
        raise CampaignError(
            f"unknown module_key {module_key!r}; known: {', '.join(MODULES)}"
        )
    expected_app_version = str(
        payload.get("expected_app_version", "2.4.4.0")
    ).strip()
    if not expected_app_version:
        raise CampaignError("expected_app_version must not be empty")

    screen = payload.get("expected_screen")
    if not isinstance(screen, dict):
        raise CampaignError("expected_screen must be an object")
    try:
        expected_width = int(screen["width"])
        expected_height = int(screen["height"])
        expected_rotation = int(screen["rotation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CampaignError(
            "expected_screen requires integer width, height, and rotation"
        ) from exc
    if expected_width < 320 or expected_height < 480:
        raise CampaignError("expected_screen is implausibly small")
    if expected_rotation not in {0, 1, 2, 3}:
        raise CampaignError("expected_screen rotation must be 0, 1, 2, or 3")

    expected_connection_texts = _strings(
        payload,
        "expected_connection_texts",
        maximum=MAX_CONNECTION_TEXTS,
    )
    required_labels = _strings(
        payload,
        "required_labels",
        maximum=MAX_REQUIRED_LABELS,
    )
    try:
        expected_catalog_count = int(payload["expected_catalog_count"])
        max_pages = int(payload.get("max_pages", 96))
        swipe_duration_ms = int(payload.get("swipe_duration_ms", 500))
        settle_seconds = float(payload.get("settle_seconds", 0.75))
        min_free_bytes = int(payload.get("min_free_bytes", 512 * 1024**2))
    except (KeyError, TypeError, ValueError) as exc:
        raise CampaignError("invalid numeric catalog-plan field") from exc
    if not 2 <= expected_catalog_count <= 4096:
        raise CampaignError("expected_catalog_count must be between 2 and 4096")
    if not 3 <= max_pages <= MAX_PAGES_LIMIT:
        raise CampaignError(
            f"max_pages must be between 3 and {MAX_PAGES_LIMIT}"
        )
    if not 100 <= swipe_duration_ms <= 2000:
        raise CampaignError("swipe_duration_ms must be between 100 and 2000")
    if not 0.1 <= settle_seconds <= 5.0:
        raise CampaignError("settle_seconds must be between 0.1 and 5.0")
    if min_free_bytes < MIN_FREE_BYTES:
        raise CampaignError("min_free_bytes must be at least 100 MiB")

    expected_first_label = str(payload.get("expected_first_label", "")).strip()
    expected_last_label = str(payload.get("expected_last_label", "")).strip()
    if not expected_first_label or not expected_last_label:
        raise CampaignError(
            "expected_first_label and expected_last_label must not be empty"
        )
    expected_hash_value = payload.get("expected_catalog_sha256")
    expected_catalog_sha256: str | None
    if expected_hash_value is None:
        expected_catalog_sha256 = None
    else:
        expected_catalog_sha256 = str(expected_hash_value).lower().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_catalog_sha256):
            raise CampaignError(
                "expected_catalog_sha256 must be a lowercase SHA-256 hex digest"
            )
    screenshot_each_page = payload.get("screenshot_each_page", False)
    if not isinstance(screenshot_each_page, bool):
        raise CampaignError("screenshot_each_page must be a JSON boolean")

    return CatalogPlan(
        campaign_id=campaign_id,
        module_key=module_key,
        expected_app_version=expected_app_version,
        expected_width=expected_width,
        expected_height=expected_height,
        expected_rotation=expected_rotation,
        expected_connection_texts=expected_connection_texts,
        expected_catalog_count=expected_catalog_count,
        expected_first_label=expected_first_label,
        expected_last_label=expected_last_label,
        required_labels=required_labels,
        expected_catalog_sha256=expected_catalog_sha256,
        max_pages=max_pages,
        swipe_duration_ms=swipe_duration_ms,
        settle_seconds=settle_seconds,
        min_free_bytes=min_free_bytes,
        screenshot_each_page=screenshot_each_page,
    )


def _ids(nodes: Iterable[UiNode]) -> set[str]:
    return {node.resource_id for node in nodes}


def plot_labels(nodes: Iterable[UiNode]) -> tuple[str, ...]:
    labels: list[tuple[int, str]] = []
    for node in nodes:
        match = re.fullmatch(
            re.escape(SAFE_ID_PREFIX) + r"Plot(\d+)Title",
            node.resource_id,
        )
        if match and node.text.strip():
            labels.append((int(match.group(1)), node.text.strip()))
    return tuple(label for _, label in sorted(labels))


def _validate_screen_geometry(
    rotation: int,
    nodes: list[UiNode],
    *,
    plan: CatalogPlan,
    surface: str,
    require_fullscreen: bool,
) -> None:
    if rotation != plan.expected_rotation:
        raise CampaignError(
            f"{surface} rotation mismatch: "
            f"expected {plan.expected_rotation}, got {rotation}"
        )
    root = nodes[0].bounds
    actual = (root.left, root.top, root.right, root.bottom)
    if require_fullscreen and actual != (
        0,
        0,
        plan.expected_width,
        plan.expected_height,
    ):
        raise CampaignError(
            f"{surface} screen mismatch: expected "
            f"{plan.expected_width}x{plan.expected_height} at (0,0), "
            f"got bounds {actual}"
        )
    for node in nodes:
        bounds = node.bounds
        if (
            bounds.left < 0
            or bounds.top < 0
            or bounds.right > plan.expected_width
            or bounds.bottom > plan.expected_height
        ):
            raise CampaignError(
                f"{surface} node {node.resource_id!r} extends outside "
                f"the expected {plan.expected_width}x{plan.expected_height} screen"
            )
        if (
            not require_fullscreen
            and (
                bounds.left < root.left
                or bounds.top < root.top
                or bounds.right > root.right
                or bounds.bottom > root.bottom
            )
        ):
            raise CampaignError(
                f"{surface} node {node.resource_id!r} extends outside "
                f"its dialog window {actual}"
            )


def validate_plots_page(
    xml_text: str,
    *,
    plan: CatalogPlan,
    expected_labels: Iterable[str] | None = None,
) -> list[UiNode]:
    rotation, nodes = parse_ui_xml(xml_text)
    _validate_screen_geometry(
        rotation,
        nodes,
        plan=plan,
        surface="Plots page",
        require_fullscreen=True,
    )
    ids = _ids(nodes)
    missing = PLOTS_PAGE_IDS - ids
    if missing:
        raise CampaignError(
            f"not the whitelisted Plots page; missing {sorted(missing)}"
        )
    if ids & ACTIVE_DIAGNOSTIC_IDS:
        raise CampaignError("Active Diagnostics controls are present; refusing all input")
    if (
        f"{SAFE_ID_PREFIX}dialog_title" in ids
        or any(resource_id.startswith("android:id/button") for resource_id in ids)
    ):
        raise CampaignError("a modal dialog is present over the Plots page")
    texts = {node.text.strip() for node in nodes if node.text.strip()}
    if texts & BLOCKING_DIALOG_TEXT:
        raise CampaignError(
            f"blocking AlfaOBD dialog present: {sorted(texts & BLOCKING_DIALOG_TEXT)}"
        )
    title = _one_by_id(nodes, f"{SAFE_ID_PREFIX}plots_label")
    if title.text.strip() != "Plotted Data":
        raise CampaignError(f"unexpected Plots page title {title.text!r}")
    connection = _one_by_id(nodes, f"{SAFE_ID_PREFIX}connectStatus4")
    if connection.text.strip() not in plan.expected_connection_texts:
        raise CampaignError(
            "Plots connection text mismatch: "
            f"expected one of {plan.expected_connection_texts}, "
            f"got {connection.text.strip()!r}"
        )
    selector = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bSelectPlots")
    if (
        selector.text.strip() != "SELECT GAUGES TO SCAN"
        or not selector.clickable
        or not selector.enabled
    ):
        raise CampaignError("Plots gauge selector is not the enabled whitelisted control")
    start_stop = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bStartscan")
    if not start_stop.clickable or not start_stop.enabled:
        raise CampaignError("Plots scan start/stop control is not enabled")
    tab = _one_by_id(nodes, f"{SAFE_ID_PREFIX}tB5")
    if not tab.selected:
        raise CampaignError("Plots tab tB5 is not selected")
    if expected_labels is not None:
        actual = plot_labels(nodes)
        wanted = tuple(expected_labels)
        if actual != wanted:
            raise CampaignError(
                f"Plots labels mismatch: expected {wanted}, got {actual}"
            )
    return nodes


def parse_dialog_page(
    xml_text: str,
    *,
    plan: CatalogPlan,
) -> DialogPage:
    rotation, nodes = parse_ui_xml(xml_text)
    _validate_screen_geometry(
        rotation,
        nodes,
        plan=plan,
        surface="Plots selector",
        require_fullscreen=False,
    )
    ids = _ids(nodes)
    if ids & ACTIVE_DIAGNOSTIC_IDS:
        raise CampaignError("Active Diagnostics controls are present in selector")
    title = _one_by_id(nodes, f"{SAFE_ID_PREFIX}dialog_title")
    if title.text.strip() != DIALOG_TITLE:
        raise CampaignError(f"unexpected selection dialog title {title.text!r}")
    list_node = _one_by_id(nodes, DIALOG_LIST_ID)
    if list_node.class_name != "android.widget.ListView" or not list_node.enabled:
        raise CampaignError("Plots selector list is not an enabled ListView")
    ok = _one_by_id(nodes, DIALOG_OK_ID)
    if ok.text.strip() != "OK" or not ok.clickable or not ok.enabled:
        raise CampaignError("Plots selector OK control is not enabled")
    unexpected_buttons = sorted(
        resource_id
        for resource_id in ids
        if re.fullmatch(r"android:id/button\d+", resource_id)
        and resource_id != DIALOG_OK_ID
    )
    if unexpected_buttons:
        raise CampaignError(
            f"unexpected modal controls in Plots selector: {unexpected_buttons}"
        )

    rows = [
        node
        for node in nodes
        if node.resource_id == DIALOG_ROW_ID
        and node.class_name == "android.widget.CheckedTextView"
        and node.checkable
        and node.clickable
        and node.enabled
    ]
    rows.sort(key=lambda node: (node.bounds.top, node.bounds.left))
    if not rows:
        raise CampaignError("Plots selector contains no visible enabled rows")
    labels = tuple(node.text.strip() for node in rows)
    if any(not label for label in labels):
        raise CampaignError("Plots selector contains an empty row label")
    if len(set(labels)) != len(labels):
        raise CampaignError(f"duplicate visible Plots labels: {labels}")
    for node in rows:
        bounds = node.bounds
        if (
            bounds.left < list_node.bounds.left
            or bounds.right > list_node.bounds.right
            or bounds.top < list_node.bounds.top
            or bounds.bottom > list_node.bounds.bottom
        ):
            raise CampaignError(
                f"Plots row {node.text!r} extends outside its ListView"
            )
    return DialogPage(
        labels=labels,
        checked=tuple(node.checked for node in rows),
        list_bounds=list_node.bounds,
        ok=ok,
    )


def merge_overlapping_page(
    accumulated: tuple[str, ...],
    page: tuple[str, ...],
) -> tuple[tuple[str, ...], int]:
    if not page:
        raise CampaignError("cannot merge an empty Plots page")
    if not accumulated:
        return page, len(page)
    overlap = 0
    for size in range(min(len(accumulated), len(page)), 0, -1):
        if accumulated[-size:] == page[:size]:
            overlap = size
            break
    if overlap == 0:
        raise CampaignError(
            "adjacent Plots pages have no suffix/prefix overlap; "
            "the swipe may have skipped rows"
        )
    additions = page[overlap:]
    duplicate_additions = sorted(set(additions) & set(accumulated))
    if duplicate_additions:
        raise CampaignError(
            f"Plots catalog cycled/repeated labels: {duplicate_additions}"
        )
    return accumulated + additions, len(additions)


def merge_preceding_page(
    accumulated: tuple[str, ...],
    page: tuple[str, ...],
) -> tuple[tuple[str, ...], int]:
    """Prepend an earlier top-to-bottom page using an exact overlap."""
    if not page:
        raise CampaignError("cannot merge an empty preceding Plots page")
    if not accumulated:
        return page, len(page)
    overlap = 0
    for size in range(min(len(accumulated), len(page)), 0, -1):
        if page[-size:] == accumulated[:size]:
            overlap = size
            break
    if overlap == 0:
        raise CampaignError(
            "adjacent reverse Plots pages have no suffix/prefix overlap; "
            "the swipe may have skipped rows"
        )
    additions = page[:-overlap]
    duplicate_additions = sorted(set(additions) & set(accumulated))
    if duplicate_additions:
        raise CampaignError(
            f"reverse Plots catalog cycled/repeated labels: {duplicate_additions}"
        )
    return additions + accumulated, len(additions)


def catalog_sha256(labels: Iterable[str]) -> str:
    canonical = json.dumps(
        list(labels),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(CATALOG_HASH_DOMAIN + canonical).hexdigest()


def validate_catalog(
    plan: CatalogPlan,
    inventory: CatalogInventory,
) -> list[str]:
    errors: list[str] = []
    labels = inventory.labels
    if len(labels) != plan.expected_catalog_count:
        errors.append(
            f"label count {len(labels)} != expected {plan.expected_catalog_count}"
        )
    if not labels or labels[0] != plan.expected_first_label:
        errors.append(
            f"first label {labels[0] if labels else None!r} "
            f"!= expected {plan.expected_first_label!r}"
        )
    if not labels or labels[-1] != plan.expected_last_label:
        errors.append(
            f"last label {labels[-1] if labels else None!r} "
            f"!= expected {plan.expected_last_label!r}"
        )
    missing = [label for label in plan.required_labels if label not in labels]
    if missing:
        errors.append(f"required labels absent: {missing}")
    if (
        plan.expected_catalog_sha256 is not None
        and inventory.catalog_sha256 != plan.expected_catalog_sha256
    ):
        errors.append(
            f"catalog SHA-256 {inventory.catalog_sha256} "
            f"!= expected {plan.expected_catalog_sha256}"
        )
    return errors


def _swipe_points(
    bounds: Bounds,
    *,
    toward: str,
) -> tuple[tuple[int, int], tuple[int, int]]:
    height = bounds.bottom - bounds.top
    width = bounds.right - bounds.left
    if height < 120 or width < 120:
        raise CampaignError(f"Plots ListView is implausibly small: {bounds}")
    x = bounds.left + width // 2
    low_y = bounds.top + (height * 4) // 5
    high_y = bounds.top + height // 5
    if toward == "later":
        start_y, end_y = low_y, high_y
    elif toward == "earlier":
        start_y, end_y = high_y, low_y
    else:
        raise CampaignError(f"unknown Plots swipe direction {toward!r}")
    if start_y == end_y:
        raise CampaignError("computed Plots swipe has no travel")
    return (x, start_y), (x, end_y)


def _record_dialog_page(
    *,
    plan: CatalogPlan,
    adb: AdbClient,
    writer: EventWriter,
    artifact_dir: Path,
    phase: str,
    index: int,
    xml_text: str,
    page: DialogPage,
    added: int,
    total: int,
    repeated: bool,
) -> dict[str, object]:
    if not re.fullmatch(r"[a-z_]+", phase):
        raise CampaignError(f"unsafe dialog-page phase {phase!r}")
    xml_name = f"dialog_{phase}_{index:04d}.xml"
    _write_text(artifact_dir / xml_name, xml_text)
    screenshot_name: str | None = None
    if plan.screenshot_each_page:
        screenshot_name = f"dialog_{phase}_{index:04d}.png"
        _write_bytes(artifact_dir / screenshot_name, adb.screenshot())
    record = {
        "phase": phase,
        "index": index,
        "xml": xml_name,
        "xml_sha256": hashlib.sha256(xml_text.encode("utf-8")).hexdigest(),
        "screenshot": screenshot_name,
        "added_labels": added,
        "catalog_labels_after_page": total,
        "stable_repeat": repeated,
        **page.as_dict(),
    }
    writer.event("dialog_page_observed", **record)
    return record


def _dialog_signature(page: DialogPage) -> tuple[object, ...]:
    return (
        page.labels,
        page.checked,
        page.list_bounds.left,
        page.list_bounds.top,
        page.list_bounds.right,
        page.list_bounds.bottom,
    )


def _observe_stable_dialog(
    plan: CatalogPlan,
    adb: AdbClient,
    writer: EventWriter,
    *,
    operation: str,
    pre_input_signature: tuple[object, ...] | None = None,
    sleep: Callable[[float], None],
) -> tuple[str, DialogPage, bool]:
    """Return a settled dialog page and whether a post-input transition occurred.

    A pair of stale UIAutomator dumps can repeat the page seen before a swipe.
    A changed page therefore needs two matching observations, while an
    unchanged page needs a longer run of matching observations.  Callers still
    require two separately delivered no-transition swipes before declaring a
    list boundary.
    """
    previous_signature: tuple[object, ...] | None = None
    unchanged_observations = 0
    for attempt in range(MAX_STABILITY_OBSERVATIONS):
        xml_text = adb.dump_ui()
        page = parse_dialog_page(xml_text, plan=plan)
        signature = _dialog_signature(page)
        unchanged = (
            pre_input_signature is not None
            and signature == pre_input_signature
        )
        unchanged_observations = (
            unchanged_observations + 1 if unchanged else 0
        )
        writer.event(
            "dialog_stability_observation",
            operation=operation,
            attempt=attempt,
            labels=list(page.labels),
            checked=list(page.checked),
            ui_sha256=hashlib.sha256(xml_text.encode("utf-8")).hexdigest(),
            matches_previous=signature == previous_signature,
            matches_pre_input=unchanged,
            unchanged_observations=unchanged_observations,
        )
        if pre_input_signature is None and signature == previous_signature:
            return xml_text, page, False
        if (
            pre_input_signature is not None
            and not unchanged
            and signature == previous_signature
        ):
            return xml_text, page, True
        if unchanged_observations >= POST_SWIPE_UNCHANGED_OBSERVATIONS:
            return xml_text, page, False
        previous_signature = signature
        sleep(plan.settle_seconds)
    raise CampaignError(
        f"Plots selector did not settle for {operation}"
    )


def _record_check_states(
    checked_by_label: dict[str, bool],
    page: DialogPage,
) -> None:
    for label, checked in zip(page.labels, page.checked, strict=True):
        if label in checked_by_label and checked_by_label[label] != checked:
            raise CampaignError(
                f"Plots row check state changed while only swiping: {label!r}"
            )
        checked_by_label[label] = checked


def _swipe_dialog(
    plan: CatalogPlan,
    adb: AdbClient,
    writer: EventWriter,
    page: DialogPage,
    *,
    phase: str,
    page_index: int,
    toward: str,
    sleep: Callable[[float], None],
    before_input: Callable[[], None] | None = None,
) -> tuple[str, DialogPage, bool]:
    if before_input is not None:
        before_input()
    adb.foreground_package()
    immediate_xml = adb.dump_ui()
    immediate_page = parse_dialog_page(immediate_xml, plan=plan)
    if _dialog_signature(immediate_page) != _dialog_signature(page):
        raise CampaignError(
            "Plots selector changed before swipe input; refusing stale coordinates"
        )
    writer.event(
        "swipe_preflight",
        phase=phase,
        page=page_index,
        toward=toward,
        ui_sha256=hashlib.sha256(
            immediate_xml.encode("utf-8")
        ).hexdigest(),
    )
    start, end = _swipe_points(immediate_page.list_bounds, toward=toward)
    pre_input_signature = _dialog_signature(immediate_page)
    writer.event(
        "swipe_intent",
        phase=phase,
        page=page_index,
        toward=toward,
        start=list(start),
        end=list(end),
        duration_ms=plan.swipe_duration_ms,
        committed_selection_change=False,
    )
    adb.swipe(
        start=start,
        end=end,
        duration_ms=plan.swipe_duration_ms,
    )
    writer.event(
        "swipe_returned",
        phase=phase,
        page=page_index,
        toward=toward,
    )
    sleep(plan.settle_seconds)
    return _observe_stable_dialog(
        plan,
        adb,
        writer,
        operation=f"{phase}:{page_index}:{toward}",
        pre_input_signature=pre_input_signature,
        sleep=sleep,
    )


def inventory_open_dialog(
    plan: CatalogPlan,
    adb: AdbClient,
    writer: EventWriter,
    artifact_dir: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
    before_input: Callable[[], None] | None = None,
) -> CatalogInventory:
    checked_by_label: dict[str, bool] = {}
    pages: list[dict[str, object]] = []

    # A reopened Android ListView normally starts at the top, but do not trust
    # that behavior.  Seek earlier until two independently delivered swipes
    # leave the same stable page.
    xml_text, page, _ = _observe_stable_dialog(
        plan,
        adb,
        writer,
        operation="initial_dialog",
        sleep=sleep,
    )
    top_stable = 0
    for index in range(plan.max_pages):
        _record_check_states(checked_by_label, page)
        pages.append(
            _record_dialog_page(
                plan=plan,
                adb=adb,
                writer=writer,
                artifact_dir=artifact_dir,
                phase="top_seek",
                index=index,
                xml_text=xml_text,
                page=page,
                added=0,
                total=0,
                repeated=top_stable > 0,
            )
        )
        next_xml, next_page, transitioned = _swipe_dialog(
            plan,
            adb,
            writer,
            page,
            phase="top_seek",
            page_index=index,
            toward="earlier",
            sleep=sleep,
            before_input=before_input,
        )
        if not transitioned:
            top_stable += 1
        else:
            top_stable = 0
        xml_text, page = next_xml, next_page
        if top_stable >= PAGE_STABLE_SWIPES:
            break
    else:
        raise CampaignError(
            f"Plots selector did not reach a stable top within {plan.max_pages} swipes"
        )
    if page.labels[0] != plan.expected_first_label:
        raise CampaignError(
            f"stable Plots top begins with {page.labels[0]!r}, "
            f"expected {plan.expected_first_label!r}"
        )

    accumulated: tuple[str, ...] = ()
    bottom_stable = 0
    bottom_reached = False
    for index in range(plan.max_pages):
        previous = accumulated
        accumulated, added = merge_overlapping_page(accumulated, page.labels)
        repeated = accumulated == previous
        _record_check_states(checked_by_label, page)
        pages.append(
            _record_dialog_page(
                plan=plan,
                adb=adb,
                writer=writer,
                artifact_dir=artifact_dir,
                phase="forward",
                index=index,
                xml_text=xml_text,
                page=page,
                added=added,
                total=len(accumulated),
                repeated=repeated,
            )
        )
        if len(accumulated) > plan.expected_catalog_count:
            raise CampaignError(
                "Plots catalog exceeded expected count before reaching the bottom: "
                f"{len(accumulated)} > {plan.expected_catalog_count}"
            )
        next_xml, next_page, transitioned = _swipe_dialog(
            plan,
            adb,
            writer,
            page,
            phase="forward",
            page_index=index,
            toward="later",
            sleep=sleep,
            before_input=before_input,
        )
        if not transitioned:
            bottom_stable += 1
        else:
            bottom_stable = 0
        xml_text, page = next_xml, next_page
        if bottom_stable >= PAGE_STABLE_SWIPES:
            bottom_reached = True
            break
    if not bottom_reached:
        raise CampaignError(
            f"Plots selector did not reach a stable bottom within {plan.max_pages} swipes"
        )
    if accumulated[-1] != plan.expected_last_label:
        raise CampaignError(
            f"stable Plots bottom ends with {accumulated[-1]!r}, "
            f"expected {plan.expected_last_label!r}"
        )

    # Independently reproduce the catalog in reverse.  This catches a forward
    # swipe that happened to retain an overlap while skipping an interior row.
    reverse_accumulated: tuple[str, ...] = ()
    reverse_top_stable = 0
    reverse_top_reached = False
    for index in range(plan.max_pages):
        previous = reverse_accumulated
        reverse_accumulated, added = merge_preceding_page(
            reverse_accumulated,
            page.labels,
        )
        repeated = reverse_accumulated == previous
        _record_check_states(checked_by_label, page)
        pages.append(
            _record_dialog_page(
                plan=plan,
                adb=adb,
                writer=writer,
                artifact_dir=artifact_dir,
                phase="reverse",
                index=index,
                xml_text=xml_text,
                page=page,
                added=added,
                total=len(reverse_accumulated),
                repeated=repeated,
            )
        )
        next_xml, next_page, transitioned = _swipe_dialog(
            plan,
            adb,
            writer,
            page,
            phase="reverse",
            page_index=index,
            toward="earlier",
            sleep=sleep,
            before_input=before_input,
        )
        if not transitioned:
            reverse_top_stable += 1
        else:
            reverse_top_stable = 0
        xml_text, page = next_xml, next_page
        if reverse_top_stable >= PAGE_STABLE_SWIPES:
            reverse_top_reached = True
            break
    if not reverse_top_reached:
        raise CampaignError(
            f"reverse Plots traversal did not reach the top within {plan.max_pages} swipes"
        )
    if reverse_accumulated != accumulated:
        raise CampaignError(
            "forward and reverse Plots catalog traversals disagree"
        )
    if set(checked_by_label) != set(accumulated):
        raise CampaignError("Plots check-state inventory does not cover the merged catalog")
    return CatalogInventory(
        labels=accumulated,
        checked_by_label=checked_by_label,
        pages=tuple(pages),
        catalog_sha256=catalog_sha256(accumulated),
    )


def _audit_page(
    plan: CatalogPlan,
    adb: AdbClient,
) -> tuple[str, list[UiNode], str, bytes]:
    adb.resolve_serial()
    version = adb.package_version()
    if version != plan.expected_app_version:
        raise CampaignError(
            f"AlfaOBD version mismatch: expected {plan.expected_app_version}, got {version}"
        )
    adb.foreground_package()
    xml_text = adb.dump_ui()
    nodes = validate_plots_page(xml_text, plan=plan)
    button = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bStartscan")
    screenshot = adb.screenshot()
    state = monitor_visual_state(
        screenshot,
        button,
        expected_width=plan.expected_width,
        expected_height=plan.expected_height,
    )
    return xml_text, nodes, state, screenshot


def _wait_for_plots_page(
    plan: CatalogPlan,
    adb: AdbClient,
    *,
    timeout_seconds: float = PLOTS_PAGE_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, list[UiNode]]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no UI observation"
    while time.monotonic() < deadline:
        try:
            xml_text = adb.dump_ui()
            nodes = validate_plots_page(xml_text, plan=plan)
            return xml_text, nodes
        except CampaignError as exc:
            last_error = str(exc)
        sleep(min(0.75, max(0.0, deadline - time.monotonic())))
    raise CampaignError(f"Plots page did not return after BACK: {last_error}")


def _close_dialog(
    plan: CatalogPlan,
    adb: AdbClient,
    writer: EventWriter,
    artifact_dir: Path,
) -> tuple[str, list[UiNode]]:
    current = adb.dump_ui()
    parse_dialog_page(current, plan=plan)
    writer.event(
        "back_intent",
        purpose="cancel_plots_selector_without_committing",
        ok_button_tapped=False,
        gauge_row_tapped=False,
    )
    adb.back()
    writer.event("back_returned", purpose="cancel_plots_selector_without_committing")
    xml_text, nodes = _wait_for_plots_page(plan, adb)
    _write_text(artifact_dir / "plots_after_back.xml", xml_text)
    return xml_text, nodes


def _write_catalog_report(
    path: Path,
    *,
    plan: CatalogPlan,
    inventory: CatalogInventory,
    validation_errors: list[str],
    conditions: str,
    serial: str | None,
) -> None:
    payload = {
        "schema_version": 1,
        "classification": (
            "live_ui_catalog_pinned_match"
            if not validation_errors and plan.expected_catalog_sha256 is not None
            else (
                "live_ui_catalog_unpinned_candidate"
                if not validation_errors
                else "live_ui_catalog_validation_failed"
            )
        ),
        "selection_committed": False,
        "gauge_rows_tapped": False,
        "dialog_ok_tapped": False,
        "scan_started": False,
        "conditions": conditions,
        "adb_serial": serial,
        "plan": plan.as_dict(),
        "validation": {
            "passed": not validation_errors,
            "errors": validation_errors,
            "hash_pinned_before_run": plan.expected_catalog_sha256 is not None,
        },
        **inventory.as_dict(),
    }
    _write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _assert_blocked_services_inactive(
    runner: CommandRunner,
    *,
    context: str,
) -> None:
    for service in BLOCKED_SERVICES:
        if _service_active(runner, service):
            raise CampaignError(
                f"{service} is active {context}; stop it before AlfaOBD navigation"
            )


def run_inventory(
    plan: CatalogPlan,
    adb: AdbClient,
    runner: CommandRunner,
    out_root: Path,
    conditions: str,
) -> Path:
    with _ui_supervisor_lock():
        _assert_blocked_services_inactive(runner, context="at campaign preflight")
        module = MODULES[plan.module_key]
        try:
            channel_handle = diagnostic_safety.acquire_channel_observer_lock(
                module.channel
            )
        except diagnostic_safety.ChannelLockError as exc:
            raise CampaignError(str(exc)) from exc
        try:
            with diagnostic_safety.interrupt_on_termination() as guard:
                return _run_inventory_locked(
                    plan,
                    adb,
                    runner,
                    out_root,
                    conditions,
                    termination_guard=guard,
                )
        finally:
            diagnostic_safety.release_channel_lock(channel_handle)


def _run_inventory_locked(
    plan: CatalogPlan,
    adb: AdbClient,
    runner: CommandRunner,
    out_root: Path,
    conditions: str,
    termination_guard: object | None = None,
) -> Path:
    _disk_guard(out_root, plan.min_free_bytes)
    xml_text, nodes, scan_state, screenshot = _audit_page(plan, adb)
    if scan_state != "stopped":
        raise CampaignError(
            "AlfaOBD Plots scan is visually running; stop it manually before inventory"
        )
    campaign_dir = out_root / plan.campaign_id
    if campaign_dir.exists():
        raise CampaignError(
            f"campaign directory already exists; refusing overwrite: {campaign_dir}"
        )
    artifact_dir = campaign_dir / "adb"
    artifact_dir.mkdir(parents=True)
    writer = EventWriter(campaign_dir)
    _write_text(
        campaign_dir / "plan.json",
        json.dumps(plan.as_dict(), indent=2, sort_keys=True) + "\n",
    )
    _write_text(artifact_dir / "plots_before.xml", xml_text)
    _write_bytes(artifact_dir / "plots_before_stopped.png", screenshot)
    writer.event(
        "catalog_inventory_started",
        campaign_id=plan.campaign_id,
        module_key=plan.module_key,
        serial=adb.serial,
        conditions=conditions,
        selected_plot_labels=list(plot_labels(nodes)),
        scan_state=scan_state,
        system_event_monitor_active=_service_active(runner, "system-event-monitor"),
        selection_committed=False,
    )
    writer.state(
        {
            "schema_version": 1,
            "campaign_id": plan.campaign_id,
            "phase": "ready_to_open_dialog",
            "manual_reconcile": False,
        }
    )

    selector_maybe_open = False
    inventory: CatalogInventory | None = None
    validation_errors: list[str] = []
    failure: BaseException | None = None
    try:
        _assert_blocked_services_inactive(
            runner,
            context="immediately before the selector input",
        )
        fresh_xml, fresh_nodes, fresh_scan_state, fresh_screenshot = _audit_page(
            plan,
            adb,
        )
        if fresh_scan_state != "stopped":
            raise CampaignError(
                "AlfaOBD Plots scan changed before selector input; "
                "stop it manually"
            )
        _write_text(artifact_dir / "plots_pre_input.xml", fresh_xml)
        _write_bytes(
            artifact_dir / "plots_pre_input_stopped.png",
            fresh_screenshot,
        )
        writer.event(
            "selector_preflight",
            ui_sha256=hashlib.sha256(
                fresh_xml.encode("utf-8")
            ).hexdigest(),
            scan_state=fresh_scan_state,
            selected_plot_labels=list(plot_labels(fresh_nodes)),
        )
        selector = _one_by_id(
            fresh_nodes,
            f"{SAFE_ID_PREFIX}bSelectPlots",
        )
        writer.state(
            {
                "schema_version": 1,
                "campaign_id": plan.campaign_id,
                "phase": "open_dialog_tap_intent",
                "manual_reconcile": True,
            }
        )
        selector_maybe_open = True
        try:
            _tap_with_intent(
                adb=adb,
                writer=writer,
                purpose="open_plots_selector",
                node=selector,
            )
        except BaseException as exc:
            writer.event(
                "selector_tap_ambiguous",
                error=type(exc).__name__,
                detail=str(exc),
                retry_attempted=False,
            )
            raise
        deadline = time.monotonic() + PLOTS_PAGE_TIMEOUT_SECONDS
        first_dialog_xml = ""
        while time.monotonic() < deadline:
            candidate = adb.dump_ui()
            try:
                parse_dialog_page(candidate, plan=plan)
            except CampaignError:
                time.sleep(0.75)
                continue
            first_dialog_xml = candidate
            break
        if not first_dialog_xml:
            raise CampaignError("Plots selector did not open")
        _write_text(artifact_dir / "dialog_opened.xml", first_dialog_xml)
        writer.state(
            {
                "schema_version": 1,
                "campaign_id": plan.campaign_id,
                "phase": "inventory_in_progress",
                "manual_reconcile": True,
            }
        )
        inventory = inventory_open_dialog(
            plan,
            adb,
            writer,
            artifact_dir,
            before_input=lambda: _assert_blocked_services_inactive(
                runner,
                context="before a selector swipe",
            ),
        )
        validation_errors = validate_catalog(plan, inventory)
        _write_catalog_report(
            campaign_dir / "catalog.json",
            plan=plan,
            inventory=inventory,
            validation_errors=validation_errors,
            conditions=conditions,
            serial=adb.serial,
        )
        writer.event(
            "catalog_observed",
            label_count=len(inventory.labels),
            catalog_sha256=inventory.catalog_sha256,
            checked_labels=[
                label
                for label in inventory.labels
                if inventory.checked_by_label[label]
            ],
            validation_errors=validation_errors,
        )
    except BaseException as exc:
        if termination_guard is not None:
            termination_guard.begin_cleanup()
        failure = exc
        writer.event(
            "catalog_inventory_error",
            error=type(exc).__name__,
            detail=str(exc),
            selector_maybe_open=selector_maybe_open,
        )
    finally:
        if termination_guard is not None:
            termination_guard.begin_cleanup()
        if selector_maybe_open:
            try:
                adb.foreground_package()
                reconcile_xml = adb.dump_ui()
                try:
                    parse_dialog_page(reconcile_xml, plan=plan)
                except CampaignError as dialog_error:
                    try:
                        after_nodes = validate_plots_page(
                            reconcile_xml,
                            plan=plan,
                        )
                    except CampaignError as page_error:
                        raise CampaignError(
                            "cannot reconcile ambiguous selector input: "
                            "neither the exact selector nor exact Plots page "
                            f"is visible (selector={dialog_error}; "
                            f"plots={page_error})"
                        ) from page_error
                    _write_text(
                        artifact_dir / "plots_reconcile_no_dialog.xml",
                        reconcile_xml,
                    )
                    button = _one_by_id(
                        after_nodes,
                        f"{SAFE_ID_PREFIX}bStartscan",
                    )
                    after_screenshot = adb.screenshot()
                    after_state = monitor_visual_state(
                        after_screenshot,
                        button,
                        expected_width=plan.expected_width,
                        expected_height=plan.expected_height,
                    )
                    _write_bytes(
                        artifact_dir / "plots_reconcile_no_dialog.png",
                        after_screenshot,
                    )
                    if after_state != "stopped":
                        raise CampaignError(
                            "selector is absent but Plots scan state is not "
                            f"stopped: {after_state}"
                        )
                    selector_maybe_open = False
                    writer.event(
                        "selector_absent_verified",
                        scan_state=after_state,
                        back_sent=False,
                        retry_attempted=False,
                    )
                else:
                    after_xml, after_nodes = _close_dialog(
                        plan,
                        adb,
                        writer,
                        artifact_dir,
                    )
                button = _one_by_id(after_nodes, f"{SAFE_ID_PREFIX}bStartscan")
                if selector_maybe_open:
                    after_screenshot = adb.screenshot()
                    after_state = monitor_visual_state(
                        after_screenshot,
                        button,
                        expected_width=plan.expected_width,
                        expected_height=plan.expected_height,
                    )
                    _write_bytes(
                        artifact_dir / "plots_after_back_stopped.png",
                        after_screenshot,
                    )
                    if after_state != "stopped":
                        raise CampaignError(
                            "Plots scan state changed during inventory: "
                            f"{after_state}"
                        )
                    selector_maybe_open = False
                    writer.event(
                        "dialog_cancelled_verified",
                        scan_state=after_state,
                        plot_labels=list(plot_labels(after_nodes)),
                        selection_committed=False,
                    )
            except BaseException as cleanup_exc:
                selector_maybe_open = True
                writer.event(
                    "dialog_cleanup_ambiguous",
                    error=type(cleanup_exc).__name__,
                    detail=str(cleanup_exc),
                )
                if failure is None:
                    failure = cleanup_exc

    if failure is not None:
        writer.state(
            {
                "schema_version": 1,
                "campaign_id": plan.campaign_id,
                "phase": "failed",
                "manual_reconcile": selector_maybe_open,
                "error": str(failure),
            }
        )
        raise failure
    if inventory is None:
        raise CampaignError("catalog inventory completed without an inventory")
    if validation_errors:
        writer.state(
            {
                "schema_version": 1,
                "campaign_id": plan.campaign_id,
                "phase": "catalog_validation_failed",
                "manual_reconcile": False,
                "catalog_sha256": inventory.catalog_sha256,
                "errors": validation_errors,
            }
        )
        raise CampaignError("; ".join(validation_errors))
    writer.state(
        {
            "schema_version": 1,
            "campaign_id": plan.campaign_id,
            "phase": "complete",
            "manual_reconcile": False,
            "catalog_sha256": inventory.catalog_sha256,
            "hash_pinned_before_run": plan.expected_catalog_sha256 is not None,
        }
    )
    writer.event(
        "catalog_inventory_complete",
        label_count=len(inventory.labels),
        catalog_sha256=inventory.catalog_sha256,
        hash_pinned_before_run=plan.expected_catalog_sha256 is not None,
    )
    return campaign_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    plan = subparsers.add_parser("plan", help="offline validation only")
    plan.add_argument("plan", type=Path)
    audit = subparsers.add_parser(
        "audit",
        help="observe an already-open Plots page; never send input",
    )
    audit.add_argument("plan", type=Path)
    audit.add_argument("--adb-serial")
    inventory = subparsers.add_parser(
        "inventory",
        help="open, swipe, and cancel the Plots selector without changing rows",
    )
    inventory.add_argument("plan", type=Path)
    inventory.add_argument("--adb-serial")
    inventory.add_argument("--campaign-id")
    inventory.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    inventory.add_argument("--conditions", required=True)
    inventory.add_argument("--execute", action="store_true")
    inventory.add_argument("--confirm-read-only-navigation", action="store_true")
    inventory.add_argument("--confirm-parked", action="store_true")
    inventory.add_argument("--confirm-scan-stopped", action="store_true")
    status = subparsers.add_parser(
        "status",
        help="read an existing checkpoint; no ADB",
    )
    status.add_argument("campaign_dir", type=Path)
    return parser


def _print_plan(plan: CatalogPlan) -> None:
    print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    print(
        "\nOFFLINE PLAN ONLY: no ADB, CAN, service, network, proxy, or output access occurred."
    )


def main(
    argv: list[str] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.command is None:
        _parser().print_help()
        return 2
    try:
        if args.command == "status":
            print((args.campaign_dir / "state.json").read_text(encoding="utf-8"), end="")
            return 0
        plan = load_plan(args.plan)
        if args.command == "plan":
            _print_plan(plan)
            return 0
        command_runner = runner or CommandRunner()
        adb = AdbClient(command_runner, args.adb_serial)
        if args.command == "audit":
            with _ui_supervisor_lock():
                xml_text, nodes, scan_state, _ = _audit_page(plan, adb)
            connection = _one_by_id(nodes, f"{SAFE_ID_PREFIX}connectStatus4")
            print(
                json.dumps(
                    {
                        "serial": adb.serial,
                        "package": PACKAGE,
                        "version": plan.expected_app_version,
                        "connection_text": connection.text.strip(),
                        "scan_state": scan_state,
                        "selected_plot_labels": list(plot_labels(nodes)),
                        "ui_sha256": hashlib.sha256(
                            xml_text.encode("utf-8")
                        ).hexdigest(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            print("AUDIT ONLY: no screen input was sent.")
            return 0

        if not args.execute:
            raise CampaignError("inventory is inert without --execute")
        if not args.confirm_read_only_navigation:
            raise CampaignError("inventory requires --confirm-read-only-navigation")
        if not args.confirm_parked:
            raise CampaignError("inventory requires --confirm-parked")
        if not args.confirm_scan_stopped:
            raise CampaignError(
                "inventory requires --confirm-scan-stopped because the icon "
                "has no accessibility state"
            )
        if not args.conditions.strip():
            raise CampaignError("--conditions must describe the actual vehicle/UI state")
        if args.campaign_id:
            if not CAMPAIGN_ID_RE.fullmatch(args.campaign_id):
                raise CampaignError("--campaign-id is not a safe filename component")
            plan = replace(plan, campaign_id=args.campaign_id)
        campaign_dir = run_inventory(
            plan,
            adb,
            command_runner,
            args.out_root,
            args.conditions.strip(),
        )
        print(f"Catalog inventory complete: {campaign_dir}")
        return 0
    except (CampaignError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Interrupted; inspect state.json and leave the selector with BACK if needed.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
