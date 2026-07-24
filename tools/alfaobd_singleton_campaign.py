#!/usr/bin/env python3
"""Run a fail-closed AlfaOBD Status-monitor singleton campaign over ADB.

This tool automates only the already-connected ECU's **System status** tab.  It never
selects a vehicle/module, connects or disconnects an ECU, opens Tools/Preferences, changes
Android settings, or enters Active Diagnostics.  AlfaOBD remains the diagnostic transmitter;
the tool merely selects one read-only monitor item at a time and records exact host/device
artifact boundaries.

``plan`` is strictly offline and is the default mode of operation: it reads one JSON plan,
prints the expanded schedule, and performs no subprocess call or output write.  ``audit``
observes the attached Android device without tapping.  ``run`` requires explicit execution,
read-only-diagnostic, vehicle-condition, and known-stopped-monitor confirmations.

The monitor start/stop ImageButton exposes no accessibility state.  Consequently a process
restart after a tap intent is ambiguous by design: this tool never guesses or performs a
compensating tap on resume.  A live run writes an intent event and fsyncs it before every tap.

Machine output defaults below ``tmp/ecu_mapping/alfaobd_singleton/``.  For a long campaign,
pass ``--out-root /mnt/EXFAT512/obd-things/tmp/ecu_mapping/alfaobd-drive`` together with
``--require-mount /mnt/EXFAT512``; the campaign ID is appended only after the named mount's
identity and writability have been verified.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable
import xml.etree.ElementTree as ET
import zlib

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import diagnostic_safety
from lib.modules import MODULES


DEFAULT_OUT_ROOT = REPO / "tmp" / "ecu_mapping" / "alfaobd_singleton"
LOCK_DIR = REPO / "tmp" / "locks"
PACKAGE = "com.AlfaOBD.AlfaOBD"
REMOTE_LOG_ROOT = "/sdcard/Android/data/com.android.AlfaOBD/files/logs"
SAFE_ID_PREFIX = f"{PACKAGE}:id/"
CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")

MONITOR_PAGE_IDS = {
    f"{SAFE_ID_PREFIX}system_status_label",
    f"{SAFE_ID_PREFIX}connectStatus1",
    f"{SAFE_ID_PREFIX}checkParSelect",
    f"{SAFE_ID_PREFIX}bSelectParameters",
    f"{SAFE_ID_PREFIX}bStartmonitoring",
    f"{SAFE_ID_PREFIX}tB2",
}
ACTIVE_DIAGNOSTIC_IDS = {
    f"{SAFE_ID_PREFIX}activediag_label",
    f"{SAFE_ID_PREFIX}spinnerDiag",
    f"{SAFE_ID_PREFIX}bStart",
}
BLOCKING_DIALOG_TEXT = {
    "ECU verification failed",
    "SEND ISO TO ALFAOBD",
    "Failed!",
    "Interface message: NO DATA",
}
BLOCKED_SERVICES = ("tpms-logger", "tpms-drivesniff")
GLOBAL_UI_LOCK = LOCK_DIR / "alfaobd-singleton.lock"
MIN_PULL_TIMEOUT_SECONDS = 180.0
MAX_PULL_TIMEOUT_SECONDS = 3600.0
MIN_PULL_BYTES_PER_SECOND = 512 * 1024


class CampaignError(RuntimeError):
    """A fail-closed plan, UI, device, provenance, or safety failure."""


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)


@dataclass(frozen=True)
class UiNode:
    text: str
    resource_id: str
    class_name: str
    package: str
    checkable: bool
    checked: bool
    clickable: bool
    enabled: bool
    selected: bool
    bounds: Bounds


@dataclass(frozen=True)
class ArtifactStat:
    path: str
    size: int | None

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size}


@dataclass(frozen=True)
class CampaignPlan:
    campaign_id: str
    module_key: str
    expected_runtime: str
    expected_app_version: str
    expected_width: int
    expected_height: int
    expected_rotation: int
    dialog_labels: tuple[str, ...]
    gauges: tuple[str, ...]
    repeat_anchors: tuple[str, ...]
    segment_seconds: float
    settle_seconds: float
    verify_seconds: float
    min_free_bytes: int
    min_tablet_free_bytes: int
    artifacts: tuple[str, ...]
    required_segment_growth: tuple[str, ...]
    required_stop_stability: tuple[str, ...]
    screenshot_each_segment: bool

    @property
    def schedule(self) -> tuple[str, ...]:
        return self.gauges + self.repeat_anchors

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "module_key": self.module_key,
            "expected_runtime": self.expected_runtime,
            "expected_app_version": self.expected_app_version,
            "expected_screen": {
                "width": self.expected_width,
                "height": self.expected_height,
                "rotation": self.expected_rotation,
            },
            "dialog_labels": list(self.dialog_labels),
            "gauges": list(self.gauges),
            "repeat_anchors": list(self.repeat_anchors),
            "schedule": list(self.schedule),
            "segment_seconds": self.segment_seconds,
            "settle_seconds": self.settle_seconds,
            "verify_seconds": self.verify_seconds,
            "min_free_bytes": self.min_free_bytes,
            "min_tablet_free_bytes": self.min_tablet_free_bytes,
            "artifacts": list(self.artifacts),
            "required_segment_growth": list(self.required_segment_growth),
            "required_stop_stability": list(self.required_stop_stability),
            "screenshot_each_segment": self.screenshot_each_segment,
        }


def _bool(value: str | None) -> bool:
    return value == "true"


def _parse_bounds(value: str) -> Bounds:
    match = BOUNDS_RE.fullmatch(value)
    if not match:
        raise CampaignError(f"invalid UI bounds {value!r}")
    left, top, right, bottom = (int(part) for part in match.groups())
    if right <= left or bottom <= top:
        raise CampaignError(f"empty/reversed UI bounds {value!r}")
    return Bounds(left, top, right, bottom)


def parse_ui_xml(xml_text: str) -> tuple[int, list[UiNode]]:
    """Parse one uiautomator hierarchy without accepting malformed/foreign nodes."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise CampaignError(f"invalid UI XML: {exc}") from exc
    if root.tag != "hierarchy":
        raise CampaignError(f"unexpected UI XML root {root.tag!r}")
    try:
        rotation = int(root.attrib.get("rotation", "-1"))
    except ValueError as exc:
        raise CampaignError("invalid UI rotation") from exc
    nodes: list[UiNode] = []
    for element in root.iter("node"):
        package = element.attrib.get("package", "")
        if package not in ("", PACKAGE, "android"):
            raise CampaignError(f"foreign foreground package in hierarchy: {package!r}")
        nodes.append(
            UiNode(
                text=element.attrib.get("text", ""),
                resource_id=element.attrib.get("resource-id", ""),
                class_name=element.attrib.get("class", ""),
                package=package,
                checkable=_bool(element.attrib.get("checkable")),
                checked=_bool(element.attrib.get("checked")),
                clickable=_bool(element.attrib.get("clickable")),
                enabled=_bool(element.attrib.get("enabled")),
                selected=_bool(element.attrib.get("selected")),
                bounds=_parse_bounds(element.attrib.get("bounds", "")),
            )
        )
    if not nodes:
        raise CampaignError("UI hierarchy contains no nodes")
    return rotation, nodes


def _by_id(nodes: Iterable[UiNode], resource_id: str) -> list[UiNode]:
    return [node for node in nodes if node.resource_id == resource_id]


def _one_by_id(nodes: Iterable[UiNode], resource_id: str) -> UiNode:
    found = _by_id(nodes, resource_id)
    if len(found) != 1:
        raise CampaignError(
            f"expected exactly one {resource_id}, found {len(found)}"
        )
    return found[0]


def _clean_label(text: str) -> str:
    return text.strip().removesuffix(":").strip()


def _filename_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("._")
    if not cleaned:
        cleaned = "gauge"
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:60]}-{digest}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _decode_png_rgba(payload: bytes) -> tuple[int, int, bytes]:
    """Decode the Android RGBA PNG subset needed for the monitor-state oracle."""
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CampaignError("screenshot is not a PNG")
    offset = 8
    width = height = 0
    compressed = bytearray()
    while offset + 12 <= len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise CampaignError("truncated PNG screenshot")
        data = payload[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            if len(data) != 13:
                raise CampaignError("invalid PNG IHDR")
            width, height, depth, color, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", data)
            )
            if (
                depth,
                color,
                compression,
                filtering,
                interlace,
            ) != (8, 6, 0, 0, 0):
                raise CampaignError(
                    "screenshot PNG must be non-interlaced 8-bit RGBA"
                )
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
        offset = end
    if width <= 0 or height <= 0 or not compressed:
        raise CampaignError("screenshot PNG lacks IHDR/IDAT data")
    try:
        filtered = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise CampaignError(f"invalid PNG image data: {exc}") from exc
    stride = width * 4
    if len(filtered) != height * (stride + 1):
        raise CampaignError("unexpected PNG decompressed size")
    decoded = bytearray(height * stride)
    source = 0
    for y in range(height):
        filter_type = filtered[source]
        source += 1
        row = bytearray(filtered[source : source + stride])
        source += stride
        previous_start = (y - 1) * stride
        for x in range(stride):
            left = row[x - 4] if x >= 4 else 0
            above = decoded[previous_start + x] if y else 0
            upper_left = decoded[previous_start + x - 4] if y and x >= 4 else 0
            if filter_type == 1:
                row[x] = (row[x] + left) & 0xFF
            elif filter_type == 2:
                row[x] = (row[x] + above) & 0xFF
            elif filter_type == 3:
                row[x] = (row[x] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                predictor = left + above - upper_left
                distances = (
                    abs(predictor - left),
                    abs(predictor - above),
                    abs(predictor - upper_left),
                )
                nearest = (left, above, upper_left)[distances.index(min(distances))]
                row[x] = (row[x] + nearest) & 0xFF
            elif filter_type != 0:
                raise CampaignError(f"unsupported PNG filter type {filter_type}")
        start = y * stride
        decoded[start : start + stride] = row
    return width, height, bytes(decoded)


def monitor_visual_state(
    png: bytes,
    button: UiNode,
    *,
    expected_width: int,
    expected_height: int,
) -> str:
    """Classify AlfaOBD's play/stop-hand image, which XML does not expose."""
    width, height, pixels = _decode_png_rgba(png)
    if (width, height) != (expected_width, expected_height):
        raise CampaignError(
            f"screenshot size mismatch: expected {expected_width}x{expected_height}, "
            f"got {width}x{height}"
        )
    bounds = button.bounds
    if (
        bounds.left < 0
        or bounds.top < 0
        or bounds.right > width
        or bounds.bottom > height
    ):
        raise CampaignError("monitor button bounds escape screenshot")
    near_white = 0
    pixel_count = 0
    for y in range(bounds.top, bounds.bottom):
        for x in range(bounds.left, bounds.right):
            start = (y * width + x) * 4
            red, green, blue, alpha = pixels[start : start + 4]
            pixel_count += 1
            if alpha >= 240 and red >= 235 and green >= 235 and blue >= 235:
                near_white += 1
    # The tested AlfaOBD 2.4.4.0 stop-hand contains hundreds of white pixels;
    # the stopped play triangle contains none.  Keep a wide fail-closed gap.
    if near_white >= max(80, pixel_count // 30):
        return "running"
    if near_white <= 10:
        return "stopped"
    raise CampaignError(
        f"unrecognized AlfaOBD monitor icon ({near_white}/{pixel_count} white pixels)"
    )


def monitor_labels(nodes: Iterable[UiNode]) -> tuple[str, ...]:
    labels: list[tuple[int, str]] = []
    for node in nodes:
        match = re.fullmatch(re.escape(SAFE_ID_PREFIX) + r"labelPar(\d+)", node.resource_id)
        if match and node.text.strip():
            labels.append((int(match.group(1)), _clean_label(node.text)))
    return tuple(label for _, label in sorted(labels))


def validate_monitor_page(
    xml_text: str,
    *,
    expected_runtime: str,
    expected_rotation: int,
    expected_width: int,
    expected_height: int,
    expected_labels: Iterable[str] | None = None,
) -> list[UiNode]:
    rotation, nodes = parse_ui_xml(xml_text)
    ids = {node.resource_id for node in nodes}
    missing = MONITOR_PAGE_IDS - ids
    if missing:
        raise CampaignError(f"not the whitelisted System-status monitor page; missing {sorted(missing)}")
    if ids & ACTIVE_DIAGNOSTIC_IDS:
        raise CampaignError("Active Diagnostics controls are present; refusing all input")
    if (
        f"{SAFE_ID_PREFIX}dialog_title" in ids
        or any(resource_id.startswith("android:id/button") for resource_id in ids)
    ):
        raise CampaignError("a modal dialog is present over the monitor page")
    texts = {node.text.strip() for node in nodes if node.text.strip()}
    if texts & BLOCKING_DIALOG_TEXT:
        raise CampaignError(f"blocking AlfaOBD dialog present: {sorted(texts & BLOCKING_DIALOG_TEXT)}")
    status = _one_by_id(nodes, f"{SAFE_ID_PREFIX}system_status_label")
    if status.text.strip() != "System status":
        raise CampaignError(f"unexpected page title {status.text!r}")
    runtime = _one_by_id(nodes, f"{SAFE_ID_PREFIX}connectStatus1")
    if runtime.text.strip() != f"Connected to {expected_runtime}":
        raise CampaignError(
            f"runtime mismatch: expected 'Connected to {expected_runtime}', got {runtime.text!r}"
        )
    checkbox = _one_by_id(nodes, f"{SAFE_ID_PREFIX}checkParSelect")
    if checkbox.text.strip() != "Monitor parameters" or not checkbox.checked:
        raise CampaignError("Monitor parameters is not enabled")
    add_remove = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bSelectParameters")
    if add_remove.text.strip() != "ADD/REMOVE" or not add_remove.clickable or not add_remove.enabled:
        raise CampaignError("ADD/REMOVE is not an enabled whitelisted control")
    start_stop = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bStartmonitoring")
    if not start_stop.clickable or not start_stop.enabled:
        raise CampaignError("monitor start/stop control is not enabled")
    tab = _one_by_id(nodes, f"{SAFE_ID_PREFIX}tB2")
    if not tab.selected:
        raise CampaignError("System-status tab tB2 is not selected")
    if rotation != expected_rotation:
        raise CampaignError(f"rotation mismatch: expected {expected_rotation}, got {rotation}")
    root = nodes[0].bounds
    if root.right != expected_width or root.bottom != expected_height:
        raise CampaignError(
            f"screen mismatch: expected {expected_width}x{expected_height}, "
            f"got {root.right}x{root.bottom}"
        )
    if expected_labels is not None:
        actual = monitor_labels(nodes)
        wanted = tuple(expected_labels)
        if actual != wanted:
            raise CampaignError(f"monitor labels mismatch: expected {wanted}, got {actual}")
    return nodes


def dialog_rows(xml_text: str, expected_labels: Iterable[str]) -> tuple[list[UiNode], UiNode]:
    _, nodes = parse_ui_xml(xml_text)
    ids = {node.resource_id for node in nodes}
    if ids & ACTIVE_DIAGNOSTIC_IDS:
        raise CampaignError("Active Diagnostics controls are present in selection hierarchy")
    title = _one_by_id(nodes, f"{SAFE_ID_PREFIX}dialog_title")
    if title.text.strip() != "Select parameters to monitor":
        raise CampaignError(f"unexpected selection dialog title {title.text!r}")
    rows = [
        node
        for node in nodes
        if node.resource_id == "android:id/text1"
        and node.class_name == "android.widget.CheckedTextView"
        and node.checkable
        and node.clickable
        and node.enabled
    ]
    labels = tuple(_clean_label(row.text) for row in rows)
    wanted = tuple(expected_labels)
    if labels != wanted:
        raise CampaignError(
            "selection dialog labels changed or are not all visible: "
            f"expected {wanted}, got {labels}"
        )
    ok = _one_by_id(nodes, "android:id/button1")
    if ok.text.strip() != "OK" or not ok.clickable or not ok.enabled:
        raise CampaignError("selection dialog OK control is not enabled")
    return rows, ok


def load_plan(path: Path) -> CampaignPlan:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot read campaign plan {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CampaignError("campaign plan root must be a JSON object")
    if payload.get("schema_version") != 1:
        raise CampaignError("campaign schema_version must be 1")
    campaign_id = str(payload.get("campaign_id", ""))
    if not CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise CampaignError("campaign_id must be a safe 1-80 character filename component")

    def strings(key: str, *, allow_empty: bool = False) -> tuple[str, ...]:
        value = payload.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise CampaignError(f"{key} must be a JSON string list")
        cleaned = tuple(item.strip() for item in value)
        if not allow_empty and not cleaned:
            raise CampaignError(f"{key} must not be empty")
        if any(not item for item in cleaned):
            raise CampaignError(f"{key} contains an empty item")
        return cleaned

    dialog_labels = strings("dialog_labels")
    gauges = strings("gauges")
    repeat_anchors = strings("repeat_anchors", allow_empty=True)
    artifacts = strings("artifacts")
    uses_legacy_growth = "required_growth" in payload
    uses_split_growth = (
        "required_segment_growth" in payload or "required_stop_stability" in payload
    )
    if uses_legacy_growth and uses_split_growth:
        raise CampaignError(
            "use either legacy required_growth or the split "
            "required_segment_growth/required_stop_stability keys, not both"
        )
    if uses_split_growth:
        required_segment_growth = strings("required_segment_growth")
        required_stop_stability = strings("required_stop_stability")
    else:
        required_segment_growth = strings("required_growth")
        required_stop_stability = required_segment_growth
    if len(set(dialog_labels)) != len(dialog_labels):
        raise CampaignError("dialog_labels must be unique")
    if len(set(gauges)) != len(gauges):
        raise CampaignError("gauges must be unique")
    if len(set(artifacts)) != len(artifacts):
        raise CampaignError("artifacts must be unique")
    if len(set(required_segment_growth)) != len(required_segment_growth):
        raise CampaignError("required_segment_growth must be unique")
    if len(set(required_stop_stability)) != len(required_stop_stability):
        raise CampaignError("required_stop_stability must be unique")
    unknown = (set(gauges) | set(repeat_anchors)) - set(dialog_labels)
    if unknown:
        raise CampaignError(f"scheduled gauges absent from dialog_labels: {sorted(unknown)}")
    if not set(required_segment_growth) <= set(artifacts):
        raise CampaignError("required_segment_growth must be a subset of artifacts")
    if not set(required_stop_stability) <= set(required_segment_growth):
        raise CampaignError(
            "required_stop_stability must be a subset of required_segment_growth"
        )
    if any("/" in item or item in (".", "..") for item in artifacts):
        raise CampaignError("artifact entries must be plain filenames")

    screen = payload.get("expected_screen", {})
    if not isinstance(screen, dict):
        raise CampaignError("expected_screen must be an object")
    expected_width = int(screen.get("width", 800))
    expected_height = int(screen.get("height", 1280))
    expected_rotation = int(screen.get("rotation", 0))
    segment_seconds = float(payload.get("segment_seconds", 30.0))
    settle_seconds = float(payload.get("settle_seconds", 2.0))
    verify_seconds = float(payload.get("verify_seconds", 2.0))
    min_free_bytes = int(payload.get("min_free_bytes", 2 * 1024**3))
    min_tablet_free_bytes = int(
        payload.get("min_tablet_free_bytes", 512 * 1024**2)
    )
    if not 3 <= segment_seconds <= 3600:
        raise CampaignError("segment_seconds must be between 3 and 3600")
    if not 0 <= settle_seconds <= 60:
        raise CampaignError("settle_seconds must be between 0 and 60")
    if not 1 <= verify_seconds <= 30:
        raise CampaignError("verify_seconds must be between 1 and 30")
    if verify_seconds > segment_seconds:
        raise CampaignError("verify_seconds must not exceed segment_seconds")
    if min_free_bytes < 100 * 1024**2:
        raise CampaignError("min_free_bytes must be at least 100 MiB")
    if min_tablet_free_bytes < 100 * 1024**2:
        raise CampaignError("min_tablet_free_bytes must be at least 100 MiB")
    module_key = str(payload.get("module_key", "")).strip()
    expected_runtime = str(payload.get("expected_runtime", "")).strip()
    expected_app_version = str(
        payload.get("expected_app_version", "2.4.4.0")
    ).strip()
    if not module_key:
        raise CampaignError("module_key must not be empty")
    if module_key not in MODULES:
        raise CampaignError(
            f"unknown module_key {module_key!r}; known: {', '.join(MODULES)}"
        )
    if not expected_runtime:
        raise CampaignError("expected_runtime must not be empty")
    if not expected_app_version:
        raise CampaignError("expected_app_version must not be empty")
    screenshot_each_segment = payload.get("screenshot_each_segment", False)
    if not isinstance(screenshot_each_segment, bool):
        raise CampaignError("screenshot_each_segment must be a JSON boolean")
    return CampaignPlan(
        campaign_id=campaign_id,
        module_key=module_key,
        expected_runtime=expected_runtime,
        expected_app_version=expected_app_version,
        expected_width=expected_width,
        expected_height=expected_height,
        expected_rotation=expected_rotation,
        dialog_labels=dialog_labels,
        gauges=gauges,
        repeat_anchors=repeat_anchors,
        segment_seconds=segment_seconds,
        settle_seconds=settle_seconds,
        verify_seconds=verify_seconds,
        min_free_bytes=min_free_bytes,
        min_tablet_free_bytes=min_tablet_free_bytes,
        artifacts=artifacts,
        required_segment_growth=required_segment_growth,
        required_stop_stability=required_stop_stability,
        screenshot_each_segment=screenshot_each_segment,
    )


class CommandRunner:
    def run(
        self,
        command: list[str],
        *,
        timeout: float = 20.0,
        binary: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                text=not binary,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CampaignError(
                f"command timed out after {timeout}s: {command[0]}"
            ) from exc
        if check and result.returncode != 0:
            stderr = (
                result.stderr.decode(errors="replace")
                if binary and isinstance(result.stderr, bytes)
                else result.stderr
            )
            raise CampaignError(
                f"command failed ({result.returncode}): {command[0]}: {str(stderr).strip()}"
            )
        return result


class AdbClient:
    """Small typed ADB boundary; campaign code cannot issue arbitrary shell input."""

    def __init__(self, runner: CommandRunner, serial: str | None):
        self.runner = runner
        self.serial = serial

    def _base(self) -> list[str]:
        command = ["adb"]
        if self.serial:
            command += ["-s", self.serial]
        return command

    def resolve_serial(self) -> str:
        result = self.runner.run(["adb", "devices"], timeout=10)
        devices = []
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "device":
                devices.append(fields[0])
        if self.serial:
            if self.serial not in devices:
                raise CampaignError(f"ADB device {self.serial!r} is not connected/authorized")
            return self.serial
        if len(devices) != 1:
            raise CampaignError(f"expected exactly one authorized ADB device, found {devices}")
        self.serial = devices[0]
        return devices[0]

    def package_version(self) -> str:
        result = self.runner.run(
            self._base() + ["shell", "dumpsys", "package", PACKAGE], timeout=15
        )
        match = re.search(r"(?m)^\s*versionName=(\S+)\s*$", result.stdout)
        if not match:
            raise CampaignError("could not read AlfaOBD versionName")
        return match.group(1)

    def foreground_package(self) -> str:
        result = self.runner.run(
            self._base() + ["shell", "dumpsys", "window", "windows"], timeout=15
        )
        focus = next(
            (line for line in result.stdout.splitlines() if "mCurrentFocus" in line),
            "",
        )
        if PACKAGE not in focus:
            raise CampaignError(f"AlfaOBD is not foreground: {focus.strip()!r}")
        return PACKAGE

    def dump_ui(self) -> str:
        remote = f"/sdcard/window-obd-things-{os.getpid()}.xml"
        self.runner.run(
            self._base() + ["shell", "uiautomator", "dump", remote], timeout=20
        )
        result = self.runner.run(
            self._base() + ["exec-out", "cat", remote], timeout=20
        )
        return result.stdout

    def tap(self, node: UiNode) -> None:
        if not node.clickable or not node.enabled:
            raise CampaignError(f"refusing tap on disabled/nonclickable node {node.resource_id!r}")
        x, y = node.bounds.center
        self.runner.run(
            self._base() + ["shell", "input", "tap", str(x), str(y)], timeout=10
        )

    def screenshot(self) -> bytes:
        result = self.runner.run(
            self._base() + ["exec-out", "screencap", "-p"],
            timeout=20,
            binary=True,
        )
        return result.stdout

    def artifact_stat(self, filename: str) -> ArtifactStat:
        remote = f"{REMOTE_LOG_ROOT}/{filename}"
        errors: list[str] = []
        for command in (
            self._base() + ["shell", "stat", "-c", "%s", remote],
            self._base() + ["shell", "wc", "-c", remote],
        ):
            result = self.runner.run(
                command,
                timeout=10,
                check=False,
            )
            text = result.stdout.strip()
            fields = text.split()
            if result.returncode == 0 and fields and fields[0].isdigit():
                return ArtifactStat(path=remote, size=int(fields[0]))
            errors.append(f"{text} {result.stderr}".strip())
        combined = " ".join(errors).lower()
        # Do not treat a missing ``stat`` executable ("stat: not found") as a
        # missing artifact. The wc fallback must itself identify the path as
        # absent; every other two-command failure is ambiguous and fails closed.
        if "no such file" in combined:
            return ArtifactStat(path=remote, size=None)
        raise CampaignError(f"could not stat Android artifact {remote}: {errors}")

    def log_filesystem_free_bytes(self) -> int:
        result = self.runner.run(
            self._base() + ["shell", "df", "-k", REMOTE_LOG_ROOT],
            timeout=15,
        )
        lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
        for fields in reversed(lines):
            for index, field in enumerate(fields):
                if (
                    field.endswith("%")
                    and field[:-1].isdigit()
                    and index > 0
                    and fields[index - 1].isdigit()
                ):
                    return int(fields[index - 1]) * 1024
        raise CampaignError("could not parse Android log-filesystem free space")

    @staticmethod
    def pull_timeout_seconds(expected_size: int) -> float:
        if expected_size < 0:
            raise CampaignError("artifact pull size cannot be negative")
        estimate = 60.0 + expected_size / MIN_PULL_BYTES_PER_SECOND
        return min(
            MAX_PULL_TIMEOUT_SECONDS,
            max(MIN_PULL_TIMEOUT_SECONDS, estimate),
        )

    def pull_artifact(
        self,
        filename: str,
        destination: Path,
        *,
        expected_size: int,
    ) -> tuple[int, float]:
        remote = f"{REMOTE_LOG_ROOT}/{filename}"
        partial = destination.with_name(destination.name + ".partial")
        if destination.exists() or partial.exists():
            raise CampaignError(
                f"refusing to overwrite existing artifact pull {destination}"
            )
        timeout = self.pull_timeout_seconds(expected_size)
        result = self.runner.run(
            self._base() + ["pull", remote, str(partial)],
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise CampaignError(
                f"ADB pull failed for {remote}: rc={result.returncode}, "
                f"stderr={result.stderr.strip()!r}"
            )
        if not partial.is_file():
            raise CampaignError(f"ADB pull did not create a regular file for {remote}")
        pulled_size = partial.stat().st_size
        if pulled_size < expected_size:
            raise CampaignError(
                f"short ADB pull for {remote}: {pulled_size} < {expected_size}"
            )
        with partial.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return pulled_size, timeout


class EventWriter:
    def __init__(self, directory: Path):
        self.directory = directory
        self.events_path = directory / "events.jsonl"
        self.state_path = directory / "state.json"

    def event(self, event: str, **fields: object) -> None:
        record = {
            "event": event,
            "wall_time_utc": datetime.now(timezone.utc).isoformat(),
            "monotonic_s": time.monotonic(),
            **fields,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def state(self, payload: dict[str, object]) -> None:
        fd, temporary = tempfile.mkstemp(
            prefix=".state-", suffix=".json", dir=self.directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _service_active(runner: CommandRunner, service: str) -> bool:
    result = runner.run(
        ["systemctl", "is-active", service], timeout=10, check=False
    )
    state = result.stdout.strip()
    if result.returncode == 0 and state == "active":
        return True
    if state in {"inactive", "failed", "unknown"} and result.returncode in {3, 4}:
        return False
    raise CampaignError(
        f"cannot establish service state for {service}: "
        f"rc={result.returncode}, stdout={state!r}, stderr={result.stderr.strip()!r}"
    )


@contextmanager
def _ui_supervisor_lock():
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    handle = GLOBAL_UI_LOCK.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CampaignError(
                "another AlfaOBD audit/supervisor owns the global tablet UI lock"
            ) from None
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _artifact_stats(adb: AdbClient, plan: CampaignPlan) -> dict[str, ArtifactStat]:
    return {name: adb.artifact_stat(name) for name in plan.artifacts}


def _stats_dict(stats: dict[str, ArtifactStat]) -> dict[str, dict[str, object]]:
    return {name: stat.as_dict() for name, stat in stats.items()}


def _validate_growth(
    plan: CampaignPlan,
    before: dict[str, ArtifactStat],
    after: dict[str, ArtifactStat],
) -> None:
    for name in plan.required_segment_growth:
        old = before[name].size
        new = after[name].size
        if old is None or new is None:
            raise CampaignError(f"required artifact {name} was absent before/after segment")
        if new <= old:
            raise CampaignError(
                f"required artifact {name} did not grow ({old} -> {new})"
            )
    for name in plan.artifacts:
        old = before[name].size
        new = after[name].size
        if old is not None and new is None:
            raise CampaignError(
                f"artifact {name} disappeared during campaign after existing at "
                f"offset {old}"
            )
        if old is not None and new is not None and new < old:
            raise CampaignError(
                f"artifact {name} shrank/replaced during campaign ({old} -> {new})"
            )


def _validate_required_preexisting(
    plan: CampaignPlan,
    stats: dict[str, ArtifactStat],
) -> None:
    """Require every segment-growth witness before sending the start tap."""
    missing = [
        name for name in plan.required_segment_growth if stats[name].size is None
    ]
    if missing:
        raise CampaignError(
            "required segment-growth artifacts must already exist before monitoring: "
            + ", ".join(missing)
        )


def _any_required_artifact_grew(
    plan: CampaignPlan,
    before: dict[str, ArtifactStat],
    after: dict[str, ArtifactStat],
) -> bool:
    """Return true when at least one configured early activity witness grew.

    AlfaOBD does not update all enabled log files on the same cadence.  The
    binary Debug log can grow immediately while an ECU-specific Info log is
    still buffered.  This check is only an early liveness witness after the
    running icon appears; ``_validate_growth`` still requires every configured
    segment artifact to grow before the segment is accepted.
    """
    for name in plan.required_segment_growth:
        old = before[name].size
        new = after[name].size
        if old is not None and new is not None and new > old:
            return True
    return False


def _required_artifacts_stable(
    plan: CampaignPlan,
    before: dict[str, ArtifactStat],
    after: dict[str, ArtifactStat],
) -> bool:
    """Return true only when every configured activity witness has a stable size."""
    for name in plan.required_stop_stability:
        old = before[name].size
        new = after[name].size
        if old is None or new is None or new != old:
            return False
    return True


def _manual_reconcile_required(
    *,
    monitoring: bool,
    toggle_ambiguous: bool,
    ui_reconcile: bool,
    abnormal_reconcile: bool,
) -> bool:
    """Keep abnormal state sticky even when a cleanup tap later verifies stopped."""
    return (
        monitoring
        or toggle_ambiguous
        or ui_reconcile
        or abnormal_reconcile
    )


def _disk_guard(path: Path, minimum_free: int) -> int:
    anchor = path
    while not anchor.exists():
        if anchor.parent == anchor:
            raise CampaignError(f"no existing parent filesystem for {path}")
        anchor = anchor.parent
    free = shutil.disk_usage(anchor).free
    if free < minimum_free:
        raise CampaignError(
            f"disk free-space floor reached at {anchor}: {free} < {minimum_free}"
        )
    return free


def require_writable_mount(
    out_root: Path,
    required_mount: Path,
    *,
    is_mount: Callable[[Path], bool] = os.path.ismount,
    statvfs: Callable[[Path], os.statvfs_result] = os.statvfs,
    stat: Callable[[Path], os.stat_result] = os.stat,
    access: Callable[[Path, int], bool] = os.access,
    expected_device: int | None = None,
) -> int:
    """Require output to resolve below the same explicitly named writable mount."""
    if not out_root.is_absolute():
        raise CampaignError("--out-root must be an absolute path for a live campaign")
    if not required_mount.is_absolute():
        raise CampaignError("--require-mount must be an absolute path")
    try:
        resolved_mount = required_mount.resolve(strict=True)
        resolved_output = out_root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CampaignError(f"required mount is unavailable: {required_mount}") from exc
    try:
        inside = os.path.commonpath(
            (str(resolved_mount), str(resolved_output))
        ) == str(resolved_mount)
    except ValueError:
        inside = False
    if not inside:
        raise CampaignError(
            f"--out-root must resolve below required mount {resolved_mount}"
        )
    if not is_mount(resolved_mount):
        raise CampaignError(f"required path is not a mount point: {resolved_mount}")
    try:
        flags = statvfs(resolved_mount).f_flag
        device = stat(resolved_mount).st_dev
    except OSError as exc:
        raise CampaignError(f"cannot inspect required mount {resolved_mount}: {exc}") from exc
    if flags & getattr(os, "ST_RDONLY", 1):
        raise CampaignError(f"required mount is read-only: {resolved_mount}")
    if not access(resolved_mount, os.W_OK | os.X_OK):
        raise CampaignError(f"required mount is not writable by this user: {resolved_mount}")
    if expected_device is not None and device != expected_device:
        raise CampaignError(
            f"required mount device changed: {device} != {expected_device}"
        )
    return device


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_text(path: Path, payload: str) -> None:
    _write_bytes(path, payload.encode("utf-8"))


def _tap_with_intent(
    *,
    adb: AdbClient,
    writer: EventWriter,
    purpose: str,
    node: UiNode,
) -> None:
    writer.event(
        "tap_intent",
        purpose=purpose,
        resource_id=node.resource_id,
        text=node.text,
        bounds=[
            node.bounds.left,
            node.bounds.top,
            node.bounds.right,
            node.bounds.bottom,
        ],
    )
    adb.tap(node)
    writer.event("tap_returned", purpose=purpose)


def audit_device(plan: CampaignPlan, adb: AdbClient) -> tuple[str, list[UiNode]]:
    adb.resolve_serial()
    version = adb.package_version()
    if version != plan.expected_app_version:
        raise CampaignError(
            f"AlfaOBD version mismatch: expected {plan.expected_app_version}, got {version}"
        )
    adb.foreground_package()
    xml_text = adb.dump_ui()
    nodes = validate_monitor_page(
        xml_text,
        expected_runtime=plan.expected_runtime,
        expected_rotation=plan.expected_rotation,
        expected_width=plan.expected_width,
        expected_height=plan.expected_height,
    )
    return xml_text, nodes


def _tablet_disk_guard(plan: CampaignPlan, adb: AdbClient) -> int:
    free = adb.log_filesystem_free_bytes()
    if free < plan.min_tablet_free_bytes:
        raise CampaignError(
            "tablet log-filesystem free-space floor reached: "
            f"{free} < {plan.min_tablet_free_bytes}"
        )
    return free


def _capture_monitor_state(
    plan: CampaignPlan,
    adb: AdbClient,
    nodes: Iterable[UiNode],
    destination: Path | None = None,
) -> str:
    button = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bStartmonitoring")
    screenshot = adb.screenshot()
    state = monitor_visual_state(
        screenshot,
        button,
        expected_width=plan.expected_width,
        expected_height=plan.expected_height,
    )
    if destination is not None:
        _write_bytes(destination, screenshot)
    return state


def _select_singleton(
    plan: CampaignPlan,
    adb: AdbClient,
    writer: EventWriter,
    target: str,
    artifact_dir: Path,
    sequence: int,
) -> str:
    file_label = _filename_label(target)
    xml_text, nodes = audit_device(plan, adb)
    _write_text(
        artifact_dir / f"{sequence:04d}_{file_label}_before_select.xml", xml_text
    )
    add_remove = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bSelectParameters")
    _tap_with_intent(
        adb=adb, writer=writer, purpose="open_parameter_dialog", node=add_remove
    )
    time.sleep(0.4)
    dialog_xml = adb.dump_ui()
    rows, _ = dialog_rows(dialog_xml, plan.dialog_labels)
    _write_text(
        artifact_dir / f"{sequence:04d}_{file_label}_dialog_initial.xml",
        dialog_xml,
    )

    for label in plan.dialog_labels:
        refreshed = adb.dump_ui()
        refreshed_rows, _ = dialog_rows(refreshed, plan.dialog_labels)
        refreshed_by_label = {
            _clean_label(item.text): item for item in refreshed_rows
        }
        row = refreshed_by_label[label]
        should_be_checked = label == target
        if row.checked != should_be_checked:
            _tap_with_intent(
                adb=adb,
                writer=writer,
                purpose=f"{'check' if should_be_checked else 'uncheck'}:{label}",
                node=row,
            )
            time.sleep(0.15)
            after_toggle = adb.dump_ui()
            after_rows, _ = dialog_rows(after_toggle, plan.dialog_labels)
            after_by_label = {
                _clean_label(item.text): item for item in after_rows
            }
            if after_by_label[label].checked != should_be_checked:
                raise CampaignError(f"selection toggle did not verify for {label!r}")

    verified_xml = adb.dump_ui()
    verified_rows, ok = dialog_rows(verified_xml, plan.dialog_labels)
    checked = tuple(
        _clean_label(row.text) for row in verified_rows if row.checked
    )
    if checked != (target,):
        raise CampaignError(f"singleton selection failed: expected {(target,)}, got {checked}")
    _write_text(
        artifact_dir / f"{sequence:04d}_{file_label}_dialog_verified.xml",
        verified_xml,
    )
    _tap_with_intent(adb=adb, writer=writer, purpose="confirm_parameter_dialog", node=ok)
    time.sleep(0.5)
    monitor_xml = adb.dump_ui()
    validate_monitor_page(
        monitor_xml,
        expected_runtime=plan.expected_runtime,
        expected_rotation=plan.expected_rotation,
        expected_width=plan.expected_width,
        expected_height=plan.expected_height,
        expected_labels=(target,),
    )
    _write_text(
        artifact_dir / f"{sequence:04d}_{file_label}_selected.xml", monitor_xml
    )
    writer.event("singleton_selected", sequence=sequence, gauge=target)
    return monitor_xml


def _run_campaign_locked(
    plan: CampaignPlan,
    adb: AdbClient,
    runner: CommandRunner,
    out_root: Path,
    required_mount: Path,
    mount_device: int,
    conditions: str,
    termination_guard: object | None = None,
) -> Path:
    require_writable_mount(
        out_root,
        required_mount,
        expected_device=mount_device,
    )
    for service in BLOCKED_SERVICES:
        if _service_active(runner, service):
            raise CampaignError(
                f"{service} is active; stop it before the AlfaOBD campaign"
            )
    monitor_active = _service_active(runner, "system-event-monitor")
    xml_text, initial_nodes = audit_device(plan, adb)
    if _capture_monitor_state(plan, adb, initial_nodes) != "stopped":
        raise CampaignError(
            "AlfaOBD monitor is visually running; stop it manually before this campaign"
        )
    _disk_guard(out_root, plan.min_free_bytes)
    _tablet_disk_guard(plan, adb)
    require_writable_mount(
        out_root,
        required_mount,
        expected_device=mount_device,
    )

    campaign_dir = out_root / plan.campaign_id
    if campaign_dir.exists():
        raise CampaignError(
            f"campaign directory already exists; refusing overwrite/resume guess: {campaign_dir}"
        )
    artifact_dir = campaign_dir / "adb"
    pulled_dir = campaign_dir / "android_logs" / "final"
    artifact_dir.mkdir(parents=True)
    pulled_dir.mkdir(parents=True)
    lock_path = LOCK_DIR / f"alfaobd-singleton-{plan.campaign_id}.lock"
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CampaignError(f"another supervisor holds {lock_path}") from None
        writer = EventWriter(campaign_dir)
        _write_text(artifact_dir / "initial_monitor.xml", xml_text)
        _write_text(
            campaign_dir / "plan.json",
            json.dumps(plan.as_dict(), indent=2, sort_keys=True) + "\n",
        )
        writer.event(
            "campaign_started",
            campaign_id=plan.campaign_id,
            module_key=plan.module_key,
            serial=adb.serial,
            conditions=conditions,
            system_event_monitor_active=monitor_active,
        )
        writer.state(
            {
                "schema_version": 1,
                "campaign_id": plan.campaign_id,
                "phase": "ready",
                "next_sequence": 0,
                "manual_reconcile": False,
            }
        )

        monitoring = False
        toggle_ambiguous = False
        ui_reconcile = False
        abnormal_reconcile = False
        current_sequence = -1
        current_target = ""
        last_segment_offsets: dict[str, ArtifactStat] | None = None
        try:
            for sequence, target in enumerate(plan.schedule):
                require_writable_mount(
                    out_root,
                    required_mount,
                    expected_device=mount_device,
                )
                for service in BLOCKED_SERVICES:
                    if _service_active(runner, service):
                        raise CampaignError(
                            f"{service} became active during the AlfaOBD campaign"
                        )
                current_sequence = sequence
                current_target = target
                free_before = _disk_guard(campaign_dir, plan.min_free_bytes)
                tablet_free_before = _tablet_disk_guard(plan, adb)
                writer.event(
                    "segment_preflight",
                    sequence=sequence,
                    gauge=target,
                    free_bytes=free_before,
                    tablet_free_bytes=tablet_free_before,
                )
                ui_reconcile = True
                writer.state(
                    {
                        "schema_version": 1,
                        "campaign_id": plan.campaign_id,
                        "phase": "selection_in_progress",
                        "sequence": sequence,
                        "gauge": target,
                        "manual_reconcile": True,
                    }
                )
                _select_singleton(plan, adb, writer, target, artifact_dir, sequence)
                ui_reconcile = False
                before = _artifact_stats(adb, plan)
                _validate_required_preexisting(plan, before)
                writer.event(
                    "segment_offsets_before",
                    sequence=sequence,
                    gauge=target,
                    artifacts=_stats_dict(before),
                )
                current_xml, nodes = audit_device(plan, adb)
                validate_monitor_page(
                    current_xml,
                    expected_runtime=plan.expected_runtime,
                    expected_rotation=plan.expected_rotation,
                    expected_width=plan.expected_width,
                    expected_height=plan.expected_height,
                    expected_labels=(target,),
                )
                file_label = _filename_label(target)
                try:
                    before_start_state = _capture_monitor_state(
                        plan,
                        adb,
                        nodes,
                        artifact_dir
                        / f"{sequence:04d}_{file_label}_before_start_stopped.png",
                    )
                except BaseException:
                    abnormal_reconcile = True
                    raise
                if before_start_state != "stopped":
                    abnormal_reconcile = True
                    if before_start_state == "running":
                        monitoring = True
                        toggle_ambiguous = False
                    raise CampaignError(
                        "monitor icon is not stopped immediately before start"
                    )
                start_stop = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bStartmonitoring")
                toggle_ambiguous = True
                writer.state(
                    {
                        "schema_version": 1,
                        "campaign_id": plan.campaign_id,
                        "phase": "start_tap_intent",
                        "sequence": sequence,
                        "gauge": target,
                        "manual_reconcile": True,
                    }
                )
                _tap_with_intent(
                    adb=adb,
                    writer=writer,
                    purpose=f"start_monitor:{target}",
                    node=start_stop,
                )
                time.sleep(plan.verify_seconds)
                started_xml, started_nodes = audit_device(plan, adb)
                validate_monitor_page(
                    started_xml,
                    expected_runtime=plan.expected_runtime,
                    expected_rotation=plan.expected_rotation,
                    expected_width=plan.expected_width,
                    expected_height=plan.expected_height,
                    expected_labels=(target,),
                )
                started_state = _capture_monitor_state(
                    plan,
                    adb,
                    started_nodes,
                    artifact_dir
                    / f"{sequence:04d}_{file_label}_after_start_running.png",
                )
                if started_state != "running":
                    abnormal_reconcile = True
                    if started_state == "stopped":
                        toggle_ambiguous = False
                    raise CampaignError(
                        f"monitor icon after start is {started_state}, not running"
                    )
                monitoring = True
                toggle_ambiguous = False
                running_stats = _artifact_stats(adb, plan)
                writer.event(
                    "start_transition_observation",
                    sequence=sequence,
                    gauge=target,
                    artifacts=_stats_dict(running_stats),
                )
                if not _any_required_artifact_grew(plan, before, running_stats):
                    abnormal_reconcile = True
                    raise CampaignError(
                        "monitor start produced no growth in any configured "
                        "activity artifact; monitor activity is ambiguous"
                    )
                writer.state(
                    {
                        "schema_version": 1,
                        "campaign_id": plan.campaign_id,
                        "phase": "monitoring",
                        "sequence": sequence,
                        "gauge": target,
                        "manual_reconcile": True,
                    }
                )
                writer.event(
                    "segment_started",
                    sequence=sequence,
                    gauge=target,
                    dwell_seconds=plan.segment_seconds,
                )
                remaining = max(0.0, plan.segment_seconds - plan.verify_seconds)
                time.sleep(remaining)

                end_xml, nodes = audit_device(plan, adb)
                validate_monitor_page(
                    end_xml,
                    expected_runtime=plan.expected_runtime,
                    expected_rotation=plan.expected_rotation,
                    expected_width=plan.expected_width,
                    expected_height=plan.expected_height,
                    expected_labels=(target,),
                )
                _write_text(
                    artifact_dir / f"{sequence:04d}_{file_label}_monitor_end.xml",
                    end_xml,
                )
                if plan.screenshot_each_segment:
                    _write_bytes(
                        artifact_dir / f"{sequence:04d}_{file_label}_monitor_end.png",
                        adb.screenshot(),
                    )
                try:
                    before_stop_state = _capture_monitor_state(
                        plan,
                        adb,
                        nodes,
                        artifact_dir
                        / f"{sequence:04d}_{file_label}_before_stop_running.png",
                    )
                except BaseException:
                    abnormal_reconcile = True
                    raise
                if before_stop_state != "running":
                    abnormal_reconcile = True
                    if before_stop_state == "stopped":
                        monitoring = False
                        toggle_ambiguous = False
                    raise CampaignError(
                        "monitor icon is not running immediately before stop"
                    )
                start_stop = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bStartmonitoring")
                toggle_ambiguous = True
                writer.state(
                    {
                        "schema_version": 1,
                        "campaign_id": plan.campaign_id,
                        "phase": "stop_tap_intent",
                        "sequence": sequence,
                        "gauge": target,
                        "manual_reconcile": True,
                    }
                )
                writer.event(
                    "stop_tap_intent", sequence=sequence, gauge=target
                )
                _tap_with_intent(
                    adb=adb,
                    writer=writer,
                    purpose=f"stop_monitor:{target}",
                    node=start_stop,
                )
                time.sleep(plan.settle_seconds)
                stopped_xml, stopped_nodes = audit_device(plan, adb)
                validate_monitor_page(
                    stopped_xml,
                    expected_runtime=plan.expected_runtime,
                    expected_rotation=plan.expected_rotation,
                    expected_width=plan.expected_width,
                    expected_height=plan.expected_height,
                    expected_labels=(target,),
                )
                stopped_state = _capture_monitor_state(
                    plan,
                    adb,
                    stopped_nodes,
                    artifact_dir
                    / f"{sequence:04d}_{file_label}_after_stop_stopped.png",
                )
                if stopped_state == "running":
                    abnormal_reconcile = True
                    toggle_ambiguous = False
                if stopped_state != "stopped":
                    raise CampaignError(
                        f"monitor icon after stop is {stopped_state}, not stopped"
                    )
                monitoring = False
                toggle_ambiguous = False
                try:
                    after = _artifact_stats(adb, plan)
                    time.sleep(plan.verify_seconds)
                    stable = _artifact_stats(adb, plan)
                except BaseException:
                    # A visually stopped icon is insufficient when its configured
                    # stop-stability witnesses could not be observed.
                    abnormal_reconcile = True
                    raise
                writer.event(
                    "stop_transition_observation",
                    sequence=sequence,
                    gauge=target,
                    after_settle=_stats_dict(after),
                    after_verify=_stats_dict(stable),
                )
                if not _required_artifacts_stable(plan, after, stable):
                    abnormal_reconcile = True
                    raise CampaignError(
                        "required activity artifacts continued changing after the "
                        "stop tap; monitor state is ambiguous"
                    )
                writer.event("segment_stopped_verified", sequence=sequence, gauge=target)
                writer.event(
                    "segment_offsets_after",
                    sequence=sequence,
                    gauge=target,
                    artifacts=_stats_dict(stable),
                )
                try:
                    _validate_growth(plan, before, stable)
                except CampaignError:
                    abnormal_reconcile = True
                    raise
                last_segment_offsets = stable
                writer.state(
                    {
                        "schema_version": 1,
                        "campaign_id": plan.campaign_id,
                        "phase": "ready",
                        "next_sequence": sequence + 1,
                        "last_gauge": target,
                        "manual_reconcile": False,
                    }
                )
                writer.event("segment_complete", sequence=sequence, gauge=target)
        except BaseException as exc:
            if monitoring or toggle_ambiguous:
                # A failure observed while the toggle may be active is abnormal even
                # when the cleanup below later proves that the monitor is stopped.
                abnormal_reconcile = True
            if termination_guard is not None:
                termination_guard.begin_cleanup()
            writer.event(
                "campaign_error",
                sequence=current_sequence,
                gauge=current_target,
                error=type(exc).__name__,
                detail=str(exc),
                monitoring_assumed=monitoring,
                toggle_ambiguous=toggle_ambiguous,
                abnormal_reconcile=abnormal_reconcile,
            )
            cleanup_icon_stopped = False
            if monitoring and not toggle_ambiguous:
                try:
                    cleanup_xml, cleanup_nodes = audit_device(plan, adb)
                    validate_monitor_page(
                        cleanup_xml,
                        expected_runtime=plan.expected_runtime,
                        expected_rotation=plan.expected_rotation,
                        expected_width=plan.expected_width,
                        expected_height=plan.expected_height,
                        expected_labels=(current_target,),
                    )
                    _write_text(artifact_dir / "cleanup_monitor.xml", cleanup_xml)
                    cleanup_state = _capture_monitor_state(
                        plan,
                        adb,
                        cleanup_nodes,
                        artifact_dir / "cleanup_before_running.png",
                    )
                    if cleanup_state != "running":
                        if cleanup_state == "stopped":
                            abnormal_reconcile = True
                            monitoring = False
                            toggle_ambiguous = False
                        raise CampaignError(
                            f"cleanup expected running icon, got {cleanup_state}"
                        )
                    stop = _one_by_id(
                        cleanup_nodes, f"{SAFE_ID_PREFIX}bStartmonitoring"
                    )
                    _tap_with_intent(
                        adb=adb,
                        writer=writer,
                        purpose=f"cleanup_stop_monitor:{current_target}",
                        node=stop,
                    )
                    toggle_ambiguous = True
                    time.sleep(plan.settle_seconds)
                    cleanup_stopped_xml, cleanup_stopped_nodes = audit_device(
                        plan, adb
                    )
                    validate_monitor_page(
                        cleanup_stopped_xml,
                        expected_runtime=plan.expected_runtime,
                        expected_rotation=plan.expected_rotation,
                        expected_width=plan.expected_width,
                        expected_height=plan.expected_height,
                        expected_labels=(current_target,),
                    )
                    cleanup_stopped_state = _capture_monitor_state(
                        plan,
                        adb,
                        cleanup_stopped_nodes,
                        artifact_dir / "cleanup_after_stopped.png",
                    )
                    if cleanup_stopped_state != "stopped":
                        raise CampaignError(
                            "cleanup stop did not produce the stopped play icon"
                        )
                    monitoring = False
                    toggle_ambiguous = False
                    cleanup_icon_stopped = True
                    cleanup_after = _artifact_stats(adb, plan)
                    time.sleep(plan.verify_seconds)
                    cleanup_stable = _artifact_stats(adb, plan)
                    if not _required_artifacts_stable(
                        plan, cleanup_after, cleanup_stable
                    ):
                        abnormal_reconcile = True
                        raise CampaignError(
                            "cleanup stop tap was not followed by stable activity artifacts"
                        )
                    writer.event(
                        "cleanup_stop_verified",
                        artifacts=_stats_dict(cleanup_stable),
                    )
                except BaseException as cleanup_exc:
                    if not cleanup_icon_stopped:
                        toggle_ambiguous = True
                    writer.event(
                        "cleanup_ambiguous",
                        error=type(cleanup_exc).__name__,
                        detail=str(cleanup_exc),
                    )
            writer.state(
                {
                    "schema_version": 1,
                    "campaign_id": plan.campaign_id,
                    "phase": "failed",
                    "sequence": current_sequence,
                    "gauge": current_target,
                    "manual_reconcile": _manual_reconcile_required(
                        monitoring=monitoring,
                        toggle_ambiguous=toggle_ambiguous,
                        ui_reconcile=ui_reconcile,
                        abnormal_reconcile=abnormal_reconcile,
                    ),
                    "error": str(exc),
                }
            )
            raise

        try:
            require_writable_mount(
                out_root,
                required_mount,
                expected_device=mount_device,
            )
            if last_segment_offsets is None:
                raise CampaignError("campaign produced no completed segment offsets")
            writer.state(
                {
                    "schema_version": 1,
                    "campaign_id": plan.campaign_id,
                    "phase": "finalizing_artifacts",
                    "next_sequence": len(plan.schedule),
                    "manual_reconcile": False,
                }
            )
            final_source_stats = _artifact_stats(adb, plan)
            for required_name in plan.required_segment_growth:
                if final_source_stats[required_name].size is None:
                    raise CampaignError(
                        f"required final Android artifact disappeared: {required_name}"
                    )

            for filename in plan.artifacts:
                require_writable_mount(
                    out_root,
                    required_mount,
                    expected_device=mount_device,
                )
                source_size = final_source_stats[filename].size
                last_offset = last_segment_offsets[filename].size
                if source_size is None:
                    if last_offset is not None:
                        raise CampaignError(
                            f"previously existing Android artifact disappeared "
                            f"before final pull: {filename}"
                        )
                    writer.event(
                        "artifact_pull",
                        filename=filename,
                        source_present=False,
                        pulled=False,
                        size=None,
                        sha256=None,
                    )
                    continue
                if last_offset is not None and source_size < last_offset:
                    raise CampaignError(
                        f"Android artifact {filename} is shorter than its last "
                        f"recorded offset ({source_size} < {last_offset})"
                    )
                free_before_pull = _disk_guard(
                    campaign_dir, plan.min_free_bytes
                )
                if source_size > free_before_pull - plan.min_free_bytes:
                    raise CampaignError(
                        f"not enough host space to pull {filename} while preserving "
                        f"the free-space floor: size={source_size}, "
                        f"free={free_before_pull}, floor={plan.min_free_bytes}"
                    )
                destination = pulled_dir / filename
                pulled_size, pull_timeout = adb.pull_artifact(
                    filename,
                    destination,
                    expected_size=source_size,
                )
                if last_offset is not None and pulled_size < last_offset:
                    raise CampaignError(
                        f"pulled artifact {filename} is shorter than its last "
                        f"recorded offset ({pulled_size} < {last_offset})"
                    )
                writer.event(
                    "artifact_pull",
                    filename=filename,
                    source_present=True,
                    pulled=True,
                    source_size_before_pull=source_size,
                    last_segment_offset=last_offset,
                    size=pulled_size,
                    timeout_seconds=pull_timeout,
                    sha256=_sha256_file(destination),
                )
        except BaseException as finalization_exc:
            try:
                writer.event(
                    "artifact_finalization_error",
                    error=type(finalization_exc).__name__,
                    detail=str(finalization_exc),
                )
                writer.state(
                    {
                        "schema_version": 1,
                        "campaign_id": plan.campaign_id,
                        "phase": "artifact_finalization_failed",
                        "next_sequence": len(plan.schedule),
                        "manual_reconcile": False,
                        "error": str(finalization_exc),
                    }
                )
            except OSError:
                # A disappeared/unwritable required mount may prevent recording the
                # finalization failure there; the campaign is still never marked complete.
                pass
            raise
        writer.state(
            {
                "schema_version": 1,
                "campaign_id": plan.campaign_id,
                "phase": "complete",
                "next_sequence": len(plan.schedule),
                "manual_reconcile": False,
            }
        )
        writer.event("campaign_complete", segments=len(plan.schedule))
        return campaign_dir
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()


def run_campaign(
    plan: CampaignPlan,
    adb: AdbClient,
    runner: CommandRunner,
    out_root: Path,
    required_mount: Path,
    conditions: str,
) -> Path:
    """Serialize tablet input and reserve the module's Pi CAN channel."""
    with _ui_supervisor_lock():
        for service in BLOCKED_SERVICES:
            if _service_active(runner, service):
                raise CampaignError(
                    f"{service} is active; stop it before the AlfaOBD campaign"
                )
        mount_device = require_writable_mount(out_root, required_mount)
        module = MODULES[plan.module_key]
        try:
            channel_handle = diagnostic_safety.acquire_channel_observer_lock(
                module.channel
            )
        except diagnostic_safety.ChannelLockError as exc:
            raise CampaignError(str(exc)) from exc
        try:
            require_writable_mount(
                out_root,
                required_mount,
                expected_device=mount_device,
            )
            with diagnostic_safety.interrupt_on_termination() as guard:
                return _run_campaign_locked(
                    plan,
                    adb,
                    runner,
                    out_root,
                    required_mount,
                    mount_device,
                    conditions,
                    termination_guard=guard,
                )
        finally:
            diagnostic_safety.release_channel_lock(channel_handle)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    plan = subparsers.add_parser("plan", help="offline validation and schedule only")
    plan.add_argument("plan", type=Path)
    audit = subparsers.add_parser("audit", help="observe device/UI; never tap")
    audit.add_argument("plan", type=Path)
    audit.add_argument("--adb-serial")
    run = subparsers.add_parser("run", help="execute guarded singleton monitoring")
    run.add_argument("plan", type=Path)
    run.add_argument("--adb-serial")
    run.add_argument(
        "--campaign-id",
        help="safe per-run override so a tracked plan can be reused without overwriting",
    )
    run.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    run.add_argument(
        "--require-mount",
        type=Path,
        help="absolute writable mount that must contain --out-root",
    )
    run.add_argument("--conditions", required=True)
    run.add_argument("--execute", action="store_true")
    run.add_argument("--confirm-read-only-diagnostics", action="store_true")
    condition = run.add_mutually_exclusive_group()
    condition.add_argument("--confirm-parked-shakedown", action="store_true")
    condition.add_argument("--confirm-ordinary-driving", action="store_true")
    run.add_argument("--confirm-monitor-stopped", action="store_true")
    status = subparsers.add_parser("status", help="read an existing checkpoint; no ADB")
    status.add_argument("campaign_dir", type=Path)
    return parser


def _print_plan(plan: CampaignPlan) -> None:
    print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    print(
        "\nOFFLINE PLAN ONLY: no ADB, CAN, service, network, proxy, or output access occurred."
    )


def main(argv: list[str] | None = None, *, runner: CommandRunner | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command is None:
        _parser().print_help()
        return 2
    try:
        if args.command == "status":
            state_path = args.campaign_dir / "state.json"
            print(state_path.read_text(encoding="utf-8"), end="")
            return 0
        plan = load_plan(args.plan)
        if args.command == "plan":
            _print_plan(plan)
            return 0
        command_runner = runner or CommandRunner()
        adb = AdbClient(command_runner, args.adb_serial)
        if args.command == "audit":
            with _ui_supervisor_lock():
                xml_text, nodes = audit_device(plan, adb)
                monitor_state = _capture_monitor_state(plan, adb, nodes)
                stats = _artifact_stats(adb, plan)
                tablet_free_bytes = adb.log_filesystem_free_bytes()
            print(
                json.dumps(
                    {
                        "serial": adb.serial,
                        "package": PACKAGE,
                        "version": plan.expected_app_version,
                        "runtime": plan.expected_runtime,
                        "monitor_labels": list(monitor_labels(nodes)),
                        "monitor_state": monitor_state,
                        "ui_sha256": hashlib.sha256(
                            xml_text.encode("utf-8")
                        ).hexdigest(),
                        "artifacts": _stats_dict(stats),
                        "tablet_free_bytes": tablet_free_bytes,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            print("AUDIT ONLY: no screen input was sent.")
            return 0

        if not args.execute:
            raise CampaignError("run is inert without --execute")
        if not args.confirm_read_only_diagnostics:
            raise CampaignError("run requires --confirm-read-only-diagnostics")
        if not (args.confirm_parked_shakedown or args.confirm_ordinary_driving):
            raise CampaignError(
                "run requires --confirm-parked-shakedown or --confirm-ordinary-driving"
            )
        if not args.confirm_monitor_stopped:
            raise CampaignError(
                "run requires --confirm-monitor-stopped because AlfaOBD's toggle has no XML state"
            )
        if not args.conditions.strip():
            raise CampaignError("--conditions must describe the actual vehicle state")
        if args.require_mount is None:
            raise CampaignError("run requires --require-mount")
        if args.campaign_id:
            if not CAMPAIGN_ID_RE.fullmatch(args.campaign_id):
                raise CampaignError("--campaign-id is not a safe filename component")
            plan = replace(plan, campaign_id=args.campaign_id)
        campaign_dir = run_campaign(
            plan,
            adb,
            command_runner,
            args.out_root,
            args.require_mount,
            args.conditions.strip(),
        )
        print(f"Campaign complete: {campaign_dir}")
        return 0
    except (CampaignError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; inspect state.json for manual_reconcile.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
