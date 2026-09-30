"""Shared AlfaOBD campaign records and durable artifact writers.

Execution, UI validation, and CampaignError remain in their original tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import time


PACKAGE = "com.AlfaOBD.AlfaOBD"


SAFE_ID_PREFIX = f"{PACKAGE}:id/"


CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


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


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_text(path: Path, payload: str) -> None:
    _write_bytes(path, payload.encode("utf-8"))


# Keep serialized type identities compatible with existing campaign artifacts.
Bounds.__module__ = "tools.alfaobd_singleton_campaign"
UiNode.__module__ = "tools.alfaobd_singleton_campaign"
ArtifactStat.__module__ = "tools.alfaobd_singleton_campaign"
CampaignPlan.__module__ = "tools.alfaobd_singleton_campaign"
EventWriter.__module__ = "tools.alfaobd_singleton_campaign"
