#!/usr/bin/env python3
"""Prepare a guarded, one-scalar-at-a-time AlfaOBD Plots campaign.

This tool is deliberately separate from :mod:`alfaobd_plots_catalog`.  The
catalog walker permanently remains non-mutating; this runner is the narrowly
gated consumer of a *reviewed and pinned* catalog.

``plan`` and ``audit`` are offline.  They read only the scalar plan and its
referenced files, report every execution blocker, and never construct an ADB
client, run a subprocess, inspect services, or create output.  ``status`` reads
an existing checkpoint without ADB.  ``run`` is rejected before any ADB or
output access unless all of the following were pinned before invocation:

* a non-null live-catalog SHA-256 in the catalog plan;
* the exact reviewed catalog report and its SHA-256;
* its sibling completion state and SHA-256 proving clean selector cleanup;
* the catalog-plan source SHA-256 and human review provenance; and
* exact ``(display_order_key, zero_based_index, label)`` triples for every
  scheduled target.

Live execution is intentionally disabled in this implementation.  The
unreachable, synthetic-tested selector primitive can consume a fresh matching
inventory, commit exactly one scalar, and prove the scan remains stopped.  The
pure artifact validator proves non-shrinking Debug/CSV growth and a stable
post-stop CSV tail.  A future enabled path must wrap those primitives in
cleanup ownership, inventory the complete selector again before any row, OK,
or scan tap, never tap AlfaOBD's recording or bookmark controls, and reconcile
every potentially ambiguous input without guessing or retrying.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.alfaobd_plots_catalog import (  # noqa: E402
    AdbClient,
    CatalogPlan,
    CatalogInventory,
    DialogPage,
    EventWriter,
    _dialog_signature,
    _observe_stable_dialog,
    _swipe_dialog,
    _wait_for_plots_page,
    catalog_sha256,
    load_plan_bytes as load_catalog_plan_bytes,
    monitor_visual_state,
    parse_dialog_page,
    plot_labels,
    validate_plots_page,
)
from tools.alfaobd_singleton_campaign import (  # noqa: E402
    CAMPAIGN_ID_RE,
    CampaignError,
    SAFE_ID_PREFIX,
    _one_by_id,
)


DEFAULT_OUT_ROOT = (
    REPO / "tmp" / "ecu_mapping" / "alfaobd_plots_scalar"
)
TMP_ROOT = (REPO / "tmp").resolve()
DEBUG_ARTIFACT = "AlfaOBD_Debug.bin"
CSV_ARTIFACT = "Gauges_Data.csv"
MAX_TARGETS = 64
MAX_SCHEDULE = 128
MAX_INITIAL_CHECKED = 16
MIN_FREE_BYTES = 100 * 1024**2
CATALOG_REVIEW_CLASSIFICATIONS = {
    "live_ui_catalog_unpinned_candidate",
    "live_ui_catalog_pinned_match",
}
SCALAR_REVIEW_HASH_DOMAIN = b"alfaobd-plots-scalar-plan-review-v1\0"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is not permitted")


@dataclass(frozen=True)
class ScalarTarget:
    target_id: str
    display_order_key: int
    zero_based_index: int
    label: str

    def as_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "display_order_key": self.display_order_key,
            "zero_based_index": self.zero_based_index,
            "label": self.label,
        }


@dataclass(frozen=True)
class CatalogReview:
    inventory_run_id: str | None
    catalog_report_path: Path | None
    catalog_report_sha256: str | None
    catalog_state_sha256: str | None
    catalog_plan_sha256: str | None
    scalar_plan_sha256: str | None
    reviewed_at_utc: str | None
    reviewed_by: str | None
    review_note: str | None

    def as_dict(self, *, plan_dir: Path) -> dict[str, object]:
        report: str | None = None
        if self.catalog_report_path is not None:
            try:
                report = str(self.catalog_report_path.relative_to(plan_dir))
            except ValueError:
                report = str(self.catalog_report_path)
        return {
            "inventory_run_id": self.inventory_run_id,
            "catalog_report_path": report,
            "catalog_report_sha256": self.catalog_report_sha256,
            "catalog_state_sha256": self.catalog_state_sha256,
            "catalog_plan_sha256": self.catalog_plan_sha256,
            "scalar_plan_sha256": self.scalar_plan_sha256,
            "reviewed_at_utc": self.reviewed_at_utc,
            "reviewed_by": self.reviewed_by,
            "review_note": self.review_note,
        }


@dataclass(frozen=True)
class ScalarPlan:
    source_path: Path
    source_sha256: str
    reviewable_sha256: str
    campaign_id: str
    catalog_plan_path: Path
    catalog_plan_source_sha256: str
    catalog_plan: CatalogPlan
    review: CatalogReview
    targets: tuple[ScalarTarget, ...]
    schedule_ids: tuple[str, ...]
    segment_seconds: float
    settle_seconds: float
    verify_seconds: float
    flush_timeout_seconds: float
    min_free_bytes: int
    min_tablet_free_bytes: int
    artifacts: tuple[str, ...]
    required_segment_growth: tuple[str, ...]
    required_stop_stability: tuple[str, ...]
    stop_stability_observations: int
    recording_oracle_samples: int
    recording_oracle_interval_seconds: float
    max_initial_checked: int
    screenshot_each_segment: bool

    @property
    def target_by_id(self) -> dict[str, ScalarTarget]:
        return {target.target_id: target for target in self.targets}

    @property
    def schedule(self) -> tuple[ScalarTarget, ...]:
        by_id = self.target_by_id
        return tuple(by_id[target_id] for target_id in self.schedule_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "reviewable_sha256": self.reviewable_sha256,
            "catalog_plan_path": str(self.catalog_plan_path),
            "catalog_plan_source_sha256": (
                self.catalog_plan_source_sha256
            ),
            "catalog_plan": self.catalog_plan.as_dict(),
            "catalog_review": self.review.as_dict(
                plan_dir=self.source_path.parent
            ),
            "targets": [target.as_dict() for target in self.targets],
            "schedule": list(self.schedule_ids),
            "expanded_schedule": [
                target.as_dict() for target in self.schedule
            ],
            "segment_seconds": self.segment_seconds,
            "settle_seconds": self.settle_seconds,
            "verify_seconds": self.verify_seconds,
            "flush_timeout_seconds": self.flush_timeout_seconds,
            "min_free_bytes": self.min_free_bytes,
            "min_tablet_free_bytes": self.min_tablet_free_bytes,
            "artifacts": list(self.artifacts),
            "required_segment_growth": list(
                self.required_segment_growth
            ),
            "required_stop_stability": list(
                self.required_stop_stability
            ),
            "stop_stability_observations": (
                self.stop_stability_observations
            ),
            "recording_oracle_samples": self.recording_oracle_samples,
            "recording_oracle_interval_seconds": (
                self.recording_oracle_interval_seconds
            ),
            "max_initial_checked": self.max_initial_checked,
            "screenshot_each_segment": self.screenshot_each_segment,
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _reviewable_plan_sha256(payload: dict[str, object]) -> str:
    """Hash the complete scalar plan while excluding only its self-hash."""
    try:
        canonical_payload = json.loads(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise CampaignError(
            f"scalar plan is not canonical finite JSON: {exc}"
        ) from exc
    review = canonical_payload.get("catalog_review")
    if not isinstance(review, dict):
        raise CampaignError("catalog_review must be an object")
    review["scalar_plan_sha256"] = None
    canonical = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(
        SCALAR_REVIEW_HASH_DOMAIN + canonical
    ).hexdigest()


def _nullable_text(
    payload: dict[str, object],
    key: str,
    *,
    maximum: int,
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CampaignError(f"catalog_review.{key} must be a string or null")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > maximum:
        raise CampaignError(
            f"catalog_review.{key} exceeds {maximum} characters"
        )
    return cleaned


def _plain_filename_list(
    payload: dict[str, object],
    key: str,
) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise CampaignError(f"{key} must be a non-empty string list")
    if len(value) > 16 or not all(isinstance(item, str) for item in value):
        raise CampaignError(f"{key} must contain at most 16 strings")
    cleaned = tuple(item.strip() for item in value)
    if (
        any(
            not item
            or "/" in item
            or "\\" in item
            or item in {".", ".."}
            for item in cleaned
        )
        or len(set(cleaned)) != len(cleaned)
    ):
        raise CampaignError(
            f"{key} must contain unique plain non-empty filenames"
        )
    return cleaned


def _resolve_reference(plan_path: Path, value: object, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CampaignError(f"{key} must be a non-empty path string")
    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = plan_path.parent / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CampaignError(f"cannot resolve {key} {candidate}: {exc}") from exc


def _resolve_optional_report(
    plan_path: Path,
    value: object,
) -> Path | None:
    if value is None:
        return None
    path = _resolve_reference(plan_path, value, "catalog_report_path")
    try:
        inside_tmp = os.path.commonpath(
            (str(TMP_ROOT), str(path))
        ) == str(TMP_ROOT)
    except ValueError:
        inside_tmp = False
    if not inside_tmp:
        raise CampaignError(
            "catalog_review.catalog_report_path must resolve below repo tmp/"
        )
    return path


def _finite_number(
    payload: dict[str, object],
    key: str,
    default: int | float,
) -> float:
    value = payload.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise CampaignError(f"{key} must be a finite JSON number")
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise CampaignError(
            f"{key} must be a finite JSON number"
        ) from exc
    if not math.isfinite(converted):
        raise CampaignError(f"{key} must be a finite JSON number")
    return converted


def _strict_integer(
    payload: dict[str, object],
    key: str,
    default: int,
) -> int:
    value = payload.get(key, default)
    if type(value) is not int:
        raise CampaignError(f"{key} must be a JSON integer")
    return value


def load_plan(path: Path) -> ScalarPlan:
    try:
        source = path.resolve(strict=True)
        raw = source.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise CampaignError(f"cannot read scalar plan {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CampaignError("scalar plan root must be a JSON object")
    if payload.get("schema_version") != 1:
        raise CampaignError("scalar plan schema_version must be 1")
    allowed = {
        "schema_version",
        "campaign_id",
        "catalog_plan_path",
        "catalog_review",
        "targets",
        "schedule",
        "segment_seconds",
        "settle_seconds",
        "verify_seconds",
        "flush_timeout_seconds",
        "min_free_bytes",
        "min_tablet_free_bytes",
        "artifacts",
        "required_segment_growth",
        "required_stop_stability",
        "stop_stability_observations",
        "recording_oracle_samples",
        "recording_oracle_interval_seconds",
        "max_initial_checked",
        "screenshot_each_segment",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise CampaignError(f"unknown scalar plan keys: {unknown}")

    campaign_value = payload.get("campaign_id")
    if not isinstance(campaign_value, str):
        raise CampaignError("campaign_id must be a string")
    campaign_id = campaign_value.strip()
    if not CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise CampaignError(
            "campaign_id must be a safe 1-80 character filename component"
        )
    catalog_path = _resolve_reference(
        source, payload.get("catalog_plan_path"), "catalog_plan_path"
    )
    try:
        catalog_raw = catalog_path.read_bytes()
    except OSError as exc:
        raise CampaignError(
            f"cannot read catalog plan {catalog_path}: {exc}"
        ) from exc
    catalog_plan_source_sha256 = hashlib.sha256(catalog_raw).hexdigest()
    catalog_plan = load_catalog_plan_bytes(
        catalog_raw,
        source=str(catalog_path),
    )

    review_payload = payload.get("catalog_review")
    if not isinstance(review_payload, dict):
        raise CampaignError("catalog_review must be an object")
    review_allowed = {
        "inventory_run_id",
        "catalog_report_path",
        "catalog_report_sha256",
        "catalog_state_sha256",
        "catalog_plan_sha256",
        "scalar_plan_sha256",
        "reviewed_at_utc",
        "reviewed_by",
        "review_note",
    }
    review_unknown = sorted(set(review_payload) - review_allowed)
    if review_unknown:
        raise CampaignError(
            f"unknown catalog_review keys: {review_unknown}"
        )
    report_path = _resolve_optional_report(
        source, review_payload.get("catalog_report_path")
    )
    review = CatalogReview(
        inventory_run_id=_nullable_text(
            review_payload, "inventory_run_id", maximum=80
        ),
        catalog_report_path=report_path,
        catalog_report_sha256=_nullable_text(
            review_payload, "catalog_report_sha256", maximum=64
        ),
        catalog_state_sha256=_nullable_text(
            review_payload, "catalog_state_sha256", maximum=64
        ),
        catalog_plan_sha256=_nullable_text(
            review_payload, "catalog_plan_sha256", maximum=64
        ),
        scalar_plan_sha256=_nullable_text(
            review_payload, "scalar_plan_sha256", maximum=64
        ),
        reviewed_at_utc=_nullable_text(
            review_payload, "reviewed_at_utc", maximum=64
        ),
        reviewed_by=_nullable_text(
            review_payload, "reviewed_by", maximum=120
        ),
        review_note=_nullable_text(
            review_payload, "review_note", maximum=1000
        ),
    )

    targets_payload = payload.get("targets")
    if (
        not isinstance(targets_payload, list)
        or not 1 <= len(targets_payload) <= MAX_TARGETS
    ):
        raise CampaignError(
            f"targets must contain 1-{MAX_TARGETS} target objects"
        )
    targets: list[ScalarTarget] = []
    for index, item in enumerate(targets_payload):
        if not isinstance(item, dict):
            raise CampaignError(f"targets[{index}] must be an object")
        if set(item) != {
            "target_id",
            "display_order_key",
            "zero_based_index",
            "label",
        }:
            raise CampaignError(
                f"targets[{index}] must contain exactly target_id, "
                "display_order_key, zero_based_index, and label"
            )
        target_id_value = item["target_id"]
        label_value = item["label"]
        if not isinstance(target_id_value, str):
            raise CampaignError(
                f"targets[{index}].target_id must be a string"
            )
        if not isinstance(label_value, str):
            raise CampaignError(
                f"targets[{index}].label must be a string"
            )
        target_id = target_id_value.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", target_id):
            raise CampaignError(f"unsafe targets[{index}].target_id")
        label = label_value.strip()
        if not label or len(label) > 300:
            raise CampaignError(f"invalid targets[{index}].label")
        if (
            type(item["display_order_key"]) is not int
            or type(item["zero_based_index"]) is not int
        ):
            raise CampaignError(
                f"targets[{index}] indices must be JSON integers"
            )
        order_key = item["display_order_key"]
        zero_index = item["zero_based_index"]
        if order_key != zero_index + 1:
            raise CampaignError(
                f"targets[{index}] display_order_key must equal "
                "zero_based_index + 1"
            )
        if not 0 <= zero_index < catalog_plan.expected_catalog_count:
            raise CampaignError(
                f"targets[{index}] zero_based_index is outside catalog"
            )
        targets.append(
            ScalarTarget(target_id, order_key, zero_index, label)
        )
    for field, values in (
        ("target_id", [target.target_id for target in targets]),
        (
            "display_order_key",
            [target.display_order_key for target in targets],
        ),
        (
            "zero_based_index",
            [target.zero_based_index for target in targets],
        ),
        ("label", [target.label for target in targets]),
    ):
        if len(set(values)) != len(values):
            raise CampaignError(f"target {field} values must be unique")

    schedule_payload = payload.get("schedule")
    if (
        not isinstance(schedule_payload, list)
        or not 1 <= len(schedule_payload) <= MAX_SCHEDULE
        or not all(isinstance(item, str) for item in schedule_payload)
    ):
        raise CampaignError(
            f"schedule must contain 1-{MAX_SCHEDULE} target IDs"
        )
    schedule = tuple(item.strip() for item in schedule_payload)
    target_ids = {target.target_id for target in targets}
    unresolved = sorted(set(schedule) - target_ids)
    if any(not item for item in schedule) or unresolved:
        raise CampaignError(
            f"schedule contains empty/unknown target IDs: {unresolved}"
        )

    segment_seconds = _finite_number(
        payload, "segment_seconds", 45.0
    )
    settle_seconds = _finite_number(
        payload, "settle_seconds", 2.0
    )
    verify_seconds = _finite_number(
        payload, "verify_seconds", 3.0
    )
    flush_timeout_seconds = _finite_number(
        payload, "flush_timeout_seconds", 30.0
    )
    min_free_bytes = _strict_integer(
        payload, "min_free_bytes", 1024**3
    )
    min_tablet_free_bytes = _strict_integer(
        payload, "min_tablet_free_bytes", 512 * 1024**2
    )
    oracle_samples = _strict_integer(
        payload, "recording_oracle_samples", 5
    )
    stability_observations = _strict_integer(
        payload, "stop_stability_observations", 3
    )
    oracle_interval = _finite_number(
        payload, "recording_oracle_interval_seconds", 0.22
    )
    max_initial_checked = _strict_integer(
        payload, "max_initial_checked", MAX_INITIAL_CHECKED
    )
    if not 5 <= segment_seconds <= 3600:
        raise CampaignError("segment_seconds must be between 5 and 3600")
    if not 0.5 <= settle_seconds <= 30:
        raise CampaignError("settle_seconds must be between 0.5 and 30")
    if not 1 <= verify_seconds <= 60:
        raise CampaignError("verify_seconds must be between 1 and 60")
    if not 5 <= flush_timeout_seconds <= 120:
        raise CampaignError(
            "flush_timeout_seconds must be between 5 and 120"
        )
    if min_free_bytes < MIN_FREE_BYTES:
        raise CampaignError("min_free_bytes must be at least 100 MiB")
    if min_tablet_free_bytes < MIN_FREE_BYTES:
        raise CampaignError(
            "min_tablet_free_bytes must be at least 100 MiB"
        )
    if not 3 <= oracle_samples <= 12:
        raise CampaignError(
            "recording_oracle_samples must be between 3 and 12"
        )
    if not 3 <= stability_observations <= 12:
        raise CampaignError(
            "stop_stability_observations must be between 3 and 12"
        )
    if not 0.1 <= oracle_interval <= 1.0:
        raise CampaignError(
            "recording_oracle_interval_seconds must be between 0.1 and 1"
        )
    if not 1 <= max_initial_checked <= MAX_INITIAL_CHECKED:
        raise CampaignError(
            f"max_initial_checked must be between 1 and {MAX_INITIAL_CHECKED}"
        )

    artifacts = _plain_filename_list(payload, "artifacts")
    required_growth = _plain_filename_list(
        payload, "required_segment_growth"
    )
    required_stability = _plain_filename_list(
        payload, "required_stop_stability"
    )
    if not set(required_growth) <= set(artifacts):
        raise CampaignError(
            "required_segment_growth must be a subset of artifacts"
        )
    if not set(required_stability) <= set(artifacts):
        raise CampaignError(
            "required_stop_stability must be a subset of artifacts"
        )
    if not {DEBUG_ARTIFACT, CSV_ARTIFACT} <= set(artifacts):
        raise CampaignError(
            f"artifacts must include {DEBUG_ARTIFACT} and {CSV_ARTIFACT}"
        )
    if not {DEBUG_ARTIFACT, CSV_ARTIFACT} <= set(required_growth):
        raise CampaignError(
            "required_segment_growth must include Debug and Gauges CSV"
        )
    if CSV_ARTIFACT not in required_stability:
        raise CampaignError(
            "required_stop_stability must include Gauges_Data.csv"
        )
    screenshot_each = payload.get("screenshot_each_segment", True)
    if not isinstance(screenshot_each, bool):
        raise CampaignError(
            "screenshot_each_segment must be a JSON boolean"
        )

    return ScalarPlan(
        source_path=source,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        reviewable_sha256=_reviewable_plan_sha256(payload),
        campaign_id=campaign_id,
        catalog_plan_path=catalog_path,
        catalog_plan_source_sha256=catalog_plan_source_sha256,
        catalog_plan=catalog_plan,
        review=review,
        targets=tuple(targets),
        schedule_ids=schedule,
        segment_seconds=segment_seconds,
        settle_seconds=settle_seconds,
        verify_seconds=verify_seconds,
        flush_timeout_seconds=flush_timeout_seconds,
        min_free_bytes=min_free_bytes,
        min_tablet_free_bytes=min_tablet_free_bytes,
        artifacts=artifacts,
        required_segment_growth=required_growth,
        required_stop_stability=required_stability,
        stop_stability_observations=stability_observations,
        recording_oracle_samples=oracle_samples,
        recording_oracle_interval_seconds=oracle_interval,
        max_initial_checked=max_initial_checked,
        screenshot_each_segment=screenshot_each,
    )


def _review_report_errors(plan: ScalarPlan) -> list[str]:
    review = plan.review
    if review.catalog_report_path is None:
        return ["catalog_review.catalog_report_path is not populated"]
    try:
        report_raw = review.catalog_report_path.read_bytes()
    except OSError as exc:
        return [f"cannot read reviewed catalog report: {exc}"]
    actual_hash = hashlib.sha256(report_raw).hexdigest()
    if actual_hash != review.catalog_report_sha256:
        return [
            "reviewed catalog report SHA-256 mismatch: "
            f"{actual_hash} != {review.catalog_report_sha256}"
        ]
    try:
        report = json.loads(
            report_raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        return [f"invalid reviewed catalog report: {exc}"]
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["reviewed catalog report root is not an object"]
    if (
        type(report.get("schema_version")) is not int
        or report.get("schema_version") != 1
    ):
        errors.append("reviewed report schema_version is not exactly 1")
    classification = report.get("classification")
    if (
        not isinstance(classification, str)
        or classification not in CATALOG_REVIEW_CLASSIFICATIONS
    ):
        errors.append("reviewed report has an unacceptable classification")
    validation = report.get("validation")
    if (
        not isinstance(validation, dict)
        or validation.get("passed") is not True
        or validation.get("errors") != []
    ):
        errors.append("reviewed report validation did not pass cleanly")
    expected_hash_pinned = (
        classification == "live_ui_catalog_pinned_match"
    )
    if (
        not isinstance(validation, dict)
        or validation.get("hash_pinned_before_run")
        is not expected_hash_pinned
    ):
        errors.append(
            "reviewed report hash-pinned validation state contradicts "
            "its classification"
        )
    if (
        not isinstance(report.get("conditions"), str)
        or not report["conditions"].strip()
    ):
        errors.append("reviewed report lacks non-empty vehicle/UI conditions")
    if (
        not isinstance(report.get("adb_serial"), str)
        or not report["adb_serial"].strip()
    ):
        errors.append("reviewed report lacks a resolved ADB serial")
    for key in (
        "selection_committed",
        "gauge_rows_tapped",
        "dialog_ok_tapped",
        "scan_started",
    ):
        if report.get(key) is not False:
            errors.append(
                f"reviewed report does not prove {key}=false"
            )
    pages = report.get("pages")
    if not isinstance(pages, list) or not pages:
        errors.append("reviewed report lacks catalog traversal pages")
    else:
        phases = {
            phase
            for page in pages
            if isinstance(page, dict)
            and isinstance((phase := page.get("phase")), str)
        }
        if not {"forward", "reverse"} <= phases:
            errors.append(
                "reviewed report does not preserve both forward and reverse "
                "catalog traversal"
            )
    if (
        report.get("catalog_sha256")
        != plan.catalog_plan.expected_catalog_sha256
    ):
        errors.append(
            "reviewed report catalog SHA-256 does not match catalog plan"
        )
    catalog = report.get("catalog")
    if not isinstance(catalog, list):
        errors.append("reviewed report catalog is absent")
    else:
        if len(catalog) != plan.catalog_plan.expected_catalog_count:
            errors.append("reviewed report catalog count mismatch")
        labels: list[str] = []
        checked_count = 0
        for index, row in enumerate(catalog):
            if not isinstance(row, dict):
                errors.append(
                    f"reviewed report catalog row {index} is not an object"
                )
                continue
            label = row.get("label")
            if (
                type(row.get("zero_based_index")) is not int
                or type(row.get("display_order_key")) is not int
                or row.get("zero_based_index") != index
                or row.get("display_order_key") != index + 1
            ):
                errors.append(
                    "reviewed report catalog indices are not exact/sequential "
                    f"at row {index}"
                )
            if (
                not isinstance(label, str)
                or not label
                or label != label.strip()
            ):
                errors.append(
                    f"reviewed report catalog row {index} has an invalid label"
                )
            else:
                labels.append(label)
            if not isinstance(row.get("checked"), bool):
                errors.append(
                    f"reviewed report catalog row {index} lacks boolean checked state"
                )
            elif row["checked"]:
                checked_count += 1
        if checked_count > plan.max_initial_checked:
            errors.append(
                "reviewed report initial checked-gauge count "
                f"{checked_count} exceeds cap {plan.max_initial_checked}"
            )
        if len(labels) == len(catalog):
            if len(set(labels)) != len(labels):
                errors.append(
                    "reviewed report catalog labels are not unique"
                )
            recomputed = catalog_sha256(labels)
            if report.get("catalog_sha256") != recomputed:
                errors.append(
                    "reviewed report catalog SHA-256 does not match its "
                    "full ordered labels"
                )
            if (
                recomputed
                != plan.catalog_plan.expected_catalog_sha256
            ):
                errors.append(
                    "full ordered report labels do not match the pinned "
                    "catalog SHA-256"
                )
            if (
                not labels
                or labels[0]
                != plan.catalog_plan.expected_first_label
            ):
                errors.append(
                    "reviewed report first catalog label mismatch"
                )
            if (
                not labels
                or labels[-1]
                != plan.catalog_plan.expected_last_label
            ):
                errors.append(
                    "reviewed report last catalog label mismatch"
                )
            missing_required = sorted(
                set(plan.catalog_plan.required_labels) - set(labels)
            )
            if missing_required:
                errors.append(
                    "reviewed report omits catalog required labels: "
                    f"{missing_required}"
                )
        if (
            type(report.get("label_count")) is not int
            or report.get("label_count") != len(catalog)
        ):
            errors.append(
                "reviewed report label_count does not match catalog length"
            )
        for target in plan.targets:
            if target.zero_based_index >= len(catalog):
                errors.append(
                    f"reviewed report omits target {target.target_id}"
                )
                continue
            row = catalog[target.zero_based_index]
            expected = {
                "zero_based_index": target.zero_based_index,
                "display_order_key": target.display_order_key,
                "label": target.label,
            }
            if not isinstance(row, dict) or any(
                row.get(key) != value for key, value in expected.items()
            ):
                errors.append(
                    "reviewed report target triple mismatch for "
                    f"{target.target_id}"
                )
    report_plan = report.get("plan")
    if not isinstance(report_plan, dict):
        errors.append("reviewed report embedded plan is absent")
    elif report_plan.get("campaign_id") != review.inventory_run_id:
        errors.append(
            "catalog_review.inventory_run_id does not match report plan"
        )
    elif classification in CATALOG_REVIEW_CLASSIFICATIONS:
        expected_report_plan = plan.catalog_plan.as_dict()
        expected_report_plan["campaign_id"] = review.inventory_run_id
        if classification == "live_ui_catalog_unpinned_candidate":
            expected_report_plan.pop("expected_catalog_sha256", None)
        try:
            actual_plan_json = json.dumps(
                report_plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            expected_plan_json = json.dumps(
                expected_report_plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            errors.append(
                f"reviewed report embedded plan is not canonical JSON: {exc}"
            )
        else:
            if actual_plan_json != expected_plan_json:
                errors.append(
                    "reviewed report embedded plan does not match the "
                    "reviewed catalog plan safety fields"
                )

    state_path = review.catalog_report_path.parent / "state.json"
    try:
        state_raw = state_path.read_bytes()
    except OSError as exc:
        errors.append(f"cannot read reviewed catalog completion state: {exc}")
    else:
        state_hash = hashlib.sha256(state_raw).hexdigest()
        if state_hash != review.catalog_state_sha256:
            errors.append(
                "reviewed catalog completion-state SHA-256 mismatch: "
                f"{state_hash} != {review.catalog_state_sha256}"
            )
        try:
            state = json.loads(
                state_raw.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError) as exc:
            errors.append(f"invalid reviewed catalog completion state: {exc}")
        else:
            if not isinstance(state, dict):
                errors.append(
                    "reviewed catalog completion state root is not an object"
                )
            else:
                if (
                    type(state.get("schema_version")) is not int
                    or state.get("schema_version") != 1
                ):
                    errors.append(
                        "reviewed catalog completion state schema is not 1"
                    )
                if state.get("campaign_id") != review.inventory_run_id:
                    errors.append(
                        "reviewed catalog completion state campaign mismatch"
                    )
                if state.get("phase") != "complete":
                    errors.append(
                        "reviewed catalog completion state is not complete"
                    )
                if state.get("manual_reconcile") is not False:
                    errors.append(
                        "reviewed catalog completion state does not prove "
                        "manual_reconcile=false"
                    )
                if (
                    state.get("catalog_sha256")
                    != plan.catalog_plan.expected_catalog_sha256
                ):
                    errors.append(
                        "reviewed catalog completion-state hash does not "
                        "match the pinned catalog"
                    )
                if (
                    state.get("hash_pinned_before_run")
                    is not expected_hash_pinned
                ):
                    errors.append(
                        "reviewed catalog completion-state pin status "
                        "contradicts report classification"
                    )
    return errors


def execution_blockers(plan: ScalarPlan) -> list[str]:
    blockers: list[str] = []
    catalog = plan.catalog_plan
    review = plan.review
    if catalog.module_key not in {"pcm", "tcm"}:
        blockers.append("catalog module_key must be one of 'pcm' or 'tcm'")
    if catalog.expected_catalog_sha256 is None:
        blockers.append(
            "catalog expected_catalog_sha256 is null; inventory/review/pin it"
        )
    actual_catalog_plan_hash = plan.catalog_plan_source_sha256
    if review.catalog_plan_sha256 is None:
        blockers.append("catalog_review.catalog_plan_sha256 is not populated")
    elif not re.fullmatch(r"[0-9a-f]{64}", review.catalog_plan_sha256):
        blockers.append(
            "catalog_review.catalog_plan_sha256 is not lowercase SHA-256"
        )
    elif review.catalog_plan_sha256 != actual_catalog_plan_hash:
        blockers.append(
            "catalog plan source SHA-256 differs from reviewed provenance"
        )
    if review.catalog_report_sha256 is None:
        blockers.append(
            "catalog_review.catalog_report_sha256 is not populated"
        )
    elif not re.fullmatch(r"[0-9a-f]{64}", review.catalog_report_sha256):
        blockers.append(
            "catalog_review.catalog_report_sha256 is not lowercase SHA-256"
        )
    if review.catalog_state_sha256 is None:
        blockers.append(
            "catalog_review.catalog_state_sha256 is not populated"
        )
    elif not re.fullmatch(r"[0-9a-f]{64}", review.catalog_state_sha256):
        blockers.append(
            "catalog_review.catalog_state_sha256 is not lowercase SHA-256"
        )
    if review.scalar_plan_sha256 is None:
        blockers.append("catalog_review.scalar_plan_sha256 is not populated")
    elif not re.fullmatch(r"[0-9a-f]{64}", review.scalar_plan_sha256):
        blockers.append(
            "catalog_review.scalar_plan_sha256 is not lowercase SHA-256"
        )
    elif review.scalar_plan_sha256 != plan.reviewable_sha256:
        blockers.append(
            "scalar plan review SHA-256 differs from reviewed provenance"
        )
    for field_name, value in (
        ("inventory_run_id", review.inventory_run_id),
        ("reviewed_at_utc", review.reviewed_at_utc),
        ("reviewed_by", review.reviewed_by),
        ("review_note", review.review_note),
    ):
        if value is None:
            blockers.append(f"catalog_review.{field_name} is not populated")
    if (
        review.inventory_run_id is not None
        and not CAMPAIGN_ID_RE.fullmatch(review.inventory_run_id)
    ):
        blockers.append("catalog_review.inventory_run_id is unsafe")
    if review.reviewed_at_utc is not None:
        try:
            parsed = datetime.fromisoformat(
                review.reviewed_at_utc.replace("Z", "+00:00")
            )
            if parsed.tzinfo is None:
                raise ValueError("timezone absent")
        except ValueError:
            blockers.append(
                "catalog_review.reviewed_at_utc must be timezone-aware ISO-8601"
            )
    if (
        catalog.expected_catalog_sha256 is not None
        and review.catalog_report_sha256 is not None
        and re.fullmatch(r"[0-9a-f]{64}", review.catalog_report_sha256)
    ):
        blockers.extend(_review_report_errors(plan))
    elif review.catalog_report_path is None:
        blockers.append("catalog_review.catalog_report_path is not populated")
    return list(dict.fromkeys(blockers))


def require_execution_ready(plan: ScalarPlan) -> None:
    blockers = execution_blockers(plan)
    if blockers:
        raise CampaignError(
            "scalar plan is not execution-ready (no ADB/output accessed): "
            + "; ".join(blockers)
        )


def _catalog_page_start(
    inventory: CatalogInventory,
    page: DialogPage,
) -> int:
    """Return a page's exact catalog offset or fail on a non-contiguous view."""
    try:
        start = inventory.labels.index(page.labels[0])
    except ValueError as exc:
        raise CampaignError(
            f"visible Plots row is absent from pinned catalog: {page.labels[0]!r}"
        ) from exc
    expected = inventory.labels[start : start + len(page.labels)]
    if expected != page.labels:
        raise CampaignError(
            "visible Plots rows are not one exact contiguous pinned-catalog slice"
        )
    return start


def _toggle_exact_dialog_row(
    *,
    plan: CatalogPlan,
    adb: AdbClient,
    writer: EventWriter,
    page: DialogPage,
    label: str,
    expected_checked: bool,
    operation: str,
    sleep: Callable[[float], None] = time.sleep,
    before_input: Callable[[], None] | None = None,
) -> DialogPage:
    """Flip one exact visible row and prove no other row or geometry changed."""
    if label not in page.labels:
        raise CampaignError(f"target row is not visible: {label!r}")
    index = page.labels.index(label)
    if page.checked[index] == expected_checked:
        return page
    if before_input is not None:
        before_input()
    adb.foreground_package()
    immediate_xml = adb.dump_ui()
    immediate = parse_dialog_page(immediate_xml, plan=plan)
    if _dialog_signature(immediate) != _dialog_signature(page):
        raise CampaignError(
            "Plots selector changed before row input; refusing stale coordinates"
        )
    row = immediate.rows[index]
    writer.event(
        "scalar_row_tap_intent",
        operation=operation,
        label=label,
        prior_checked=row.checked,
        expected_checked=expected_checked,
        bounds=[
            row.bounds.left,
            row.bounds.top,
            row.bounds.right,
            row.bounds.bottom,
        ],
        retry_permitted=False,
    )
    try:
        adb.tap(row)
    except BaseException as exc:
        writer.event(
            "scalar_row_tap_ambiguous",
            operation=operation,
            label=label,
            error=type(exc).__name__,
            detail=str(exc),
            retry_attempted=False,
        )
        raise
    writer.event(
        "scalar_row_tap_returned",
        operation=operation,
        label=label,
    )
    sleep(plan.settle_seconds)
    _, changed, transitioned = _observe_stable_dialog(
        plan,
        adb,
        writer,
        operation=f"{operation}:verify",
        pre_input_signature=_dialog_signature(immediate),
        sleep=sleep,
    )
    if not transitioned:
        raise CampaignError(
            f"Plots row tap produced no verified state change for {label!r}"
        )
    if (
        changed.labels != immediate.labels
        or changed.list_bounds != immediate.list_bounds
    ):
        raise CampaignError(
            f"Plots row tap changed list content/geometry for {label!r}"
        )
    expected_states = list(immediate.checked)
    expected_states[index] = expected_checked
    if changed.checked != tuple(expected_states):
        raise CampaignError(
            f"Plots row tap changed an unexpected check state for {label!r}"
        )
    writer.event(
        "scalar_row_toggle_verified",
        operation=operation,
        label=label,
        checked=expected_checked,
    )
    return changed


def select_single_scalar_in_open_dialog(
    *,
    plan: ScalarPlan,
    inventory: CatalogInventory,
    target: ScalarTarget,
    adb: AdbClient,
    writer: EventWriter,
    sleep: Callable[[float], None] = time.sleep,
    before_input: Callable[[], None] | None = None,
) -> tuple[str, tuple[object, ...]]:
    """Commit exactly one pinned scalar from an already-inventoried selector.

    ``inventory_open_dialog`` leaves the selector at its verified top boundary.
    This primitive consumes that state, removes any catalog-reviewed prior
    selections, selects the one exact target triple, commits with the sole OK
    control, and proves that the stopped Plots page renders only that target.
    It never starts a scan or touches either recording control.

    The future live supervisor must retain cleanup ownership around this call:
    before OK, BACK can cancel pending toggles; after an ambiguous OK return,
    only an exact Plots-page/selector reconciliation may decide what happened.
    """
    catalog_plan = plan.catalog_plan
    expected_hash = catalog_plan.expected_catalog_sha256
    if expected_hash is None or inventory.catalog_sha256 != expected_hash:
        raise CampaignError(
            "live selector inventory does not match the pinned catalog hash"
        )
    if len(inventory.labels) != catalog_plan.expected_catalog_count:
        raise CampaignError(
            "live selector inventory count does not match the pinned plan"
        )
    if not 0 <= target.zero_based_index < len(inventory.labels):
        raise CampaignError(
            "target index is outside the live catalog: "
            f"{target.target_id}"
        )
    if (
        target.display_order_key != target.zero_based_index + 1
        or inventory.labels[target.zero_based_index] != target.label
    ):
        raise CampaignError(
            f"target triple does not match live inventory: {target.target_id}"
        )
    selected = dict(inventory.checked_by_label)
    if set(selected) != set(inventory.labels):
        raise CampaignError(
            "live selector check-state map does not cover the complete catalog"
        )
    if any(type(value) is not bool for value in selected.values()):
        raise CampaignError("live selector check-state map is not boolean")

    _, page, _ = _observe_stable_dialog(
        catalog_plan,
        adb,
        writer,
        operation=f"scalar:{target.target_id}:initial",
        sleep=sleep,
    )
    if _catalog_page_start(inventory, page) != 0:
        raise CampaignError(
            "scalar selection requires the inventoried selector at its top boundary"
        )

    toggled_labels: set[str] = set()
    for page_index in range(catalog_plan.max_pages):
        start = _catalog_page_start(inventory, page)
        for visible_index, label in enumerate(page.labels):
            catalog_index = start + visible_index
            if page.checked[visible_index] != selected[label]:
                raise CampaignError(
                    f"live check state drifted after inventory for {label!r}"
                )
            should_be_checked = catalog_index == target.zero_based_index
            if page.checked[visible_index] != should_be_checked:
                page = _toggle_exact_dialog_row(
                    plan=catalog_plan,
                    adb=adb,
                    writer=writer,
                    page=page,
                    label=label,
                    expected_checked=should_be_checked,
                    operation=(
                        f"scalar:{target.target_id}:page:{page_index}:"
                        f"{'check' if should_be_checked else 'uncheck'}"
                    ),
                    sleep=sleep,
                    before_input=before_input,
                )
                selected[label] = should_be_checked
                toggled_labels.add(label)

        checked = tuple(
            label for label in inventory.labels if selected[label]
        )
        if checked == (target.label,):
            break
        next_xml, next_page, transitioned = _swipe_dialog(
            catalog_plan,
            adb,
            writer,
            page,
            phase="scalar_seek",
            page_index=page_index,
            toward="later",
            sleep=sleep,
            before_input=before_input,
        )
        if not transitioned:
            raise CampaignError(
                "reached the Plots selector bottom before singleton state was proven"
            )
        page = next_page
    else:
        raise CampaignError(
            "could not establish the singleton Plots selection within max_pages"
        )

    if before_input is not None:
        before_input()
    adb.foreground_package()
    immediate_xml = adb.dump_ui()
    immediate = parse_dialog_page(immediate_xml, plan=catalog_plan)
    if _dialog_signature(immediate) != _dialog_signature(page):
        raise CampaignError(
            "Plots selector changed before OK input; refusing stale coordinates"
        )
    writer.event(
        "scalar_dialog_ok_tap_intent",
        target_id=target.target_id,
        label=target.label,
        checked_labels=[target.label],
        toggled_labels=sorted(toggled_labels),
        retry_permitted=False,
    )
    try:
        adb.tap(immediate.ok)
    except BaseException as exc:
        writer.event(
            "scalar_dialog_ok_tap_ambiguous",
            target_id=target.target_id,
            error=type(exc).__name__,
            detail=str(exc),
            retry_attempted=False,
        )
        raise
    writer.event(
        "scalar_dialog_ok_tap_returned",
        target_id=target.target_id,
    )
    xml_text, nodes = _wait_for_plots_page(
        catalog_plan,
        adb,
        sleep=sleep,
    )
    nodes = validate_plots_page(
        xml_text,
        plan=catalog_plan,
        expected_labels=(target.label,),
    )
    button = _one_by_id(nodes, f"{SAFE_ID_PREFIX}bStartscan")
    screenshot = adb.screenshot()
    scan_state = monitor_visual_state(
        screenshot,
        button,
        expected_width=catalog_plan.expected_width,
        expected_height=catalog_plan.expected_height,
    )
    if scan_state != "stopped":
        raise CampaignError(
            f"Plots scan state changed while selecting {target.label!r}: {scan_state}"
        )
    if plot_labels(nodes) != (target.label,):
        raise CampaignError(
            f"Plots page did not prove singleton label {target.label!r}"
        )
    writer.event(
        "scalar_singleton_selection_committed",
        target_id=target.target_id,
        label=target.label,
        scan_state=scan_state,
        scan_started=False,
        recording_control_tapped=False,
    )
    return xml_text, tuple(nodes)


def validate_post_stop_artifact_observations(
    plan: ScalarPlan,
    *,
    before: dict[str, int | None],
    observations: list[dict[str, int | None]],
) -> dict[str, int]:
    """Prove growth and a quiet, non-shrinking post-stop artifact tail.

    AlfaOBD buffers ``Gauges_Data.csv`` and closes it only after a clean scan
    stop.  A single larger size is therefore insufficient: every required
    activity witness must grow relative to the pre-start offset, no configured
    artifact may shrink/disappear, and each stop-stability artifact must have
    the same present size in the final configured number of observations.
    """
    expected = set(plan.artifacts)
    if set(before) != expected:
        raise CampaignError(
            "pre-segment artifact snapshot does not exactly match the plan"
        )
    if len(observations) < plan.stop_stability_observations:
        raise CampaignError(
            "insufficient post-stop artifact observations for stability"
        )

    def validated(
        snapshot: dict[str, int | None],
        *,
        description: str,
    ) -> dict[str, int | None]:
        if set(snapshot) != expected:
            raise CampaignError(
                f"{description} artifact snapshot does not exactly match the plan"
            )
        for name, size in snapshot.items():
            if size is not None and (
                type(size) is not int or size < 0
            ):
                raise CampaignError(
                    f"{description} artifact size is invalid for {name}"
                )
        return snapshot

    previous = validated(before, description="pre-segment")
    for index, raw in enumerate(observations):
        current = validated(
            raw,
            description=f"post-stop observation {index}",
        )
        for name in plan.artifacts:
            old = previous[name]
            new = current[name]
            if old is not None and new is None:
                raise CampaignError(
                    f"artifact {name} disappeared after scan stop"
                )
            if old is not None and new is not None and new < old:
                raise CampaignError(
                    f"artifact {name} shrank after scan stop ({old} -> {new})"
                )
        previous = current

    final = observations[-1]
    for name in plan.required_segment_growth:
        old = before[name]
        new = final[name]
        if old is None or new is None:
            raise CampaignError(
                f"required artifact {name} was absent before/after the segment"
            )
        if new <= old:
            raise CampaignError(
                f"required artifact {name} did not grow ({old} -> {new})"
            )

    stable_tail = observations[-plan.stop_stability_observations :]
    for name in plan.required_stop_stability:
        sizes = [snapshot[name] for snapshot in stable_tail]
        if sizes[0] is None or len(set(sizes)) != 1:
            raise CampaignError(
                f"required post-stop artifact {name} is not stable across "
                f"{plan.stop_stability_observations} observations: {sizes}"
            )
    return {
        name: size
        for name, size in final.items()
        if size is not None
    }


def offline_audit(plan: ScalarPlan) -> dict[str, object]:
    """Return a complete no-subprocess/no-output readiness audit."""
    blockers = execution_blockers(plan)
    review_report: dict[str, object] = {
        "configured": plan.review.catalog_report_path is not None,
        "path": (
            str(plan.review.catalog_report_path)
            if plan.review.catalog_report_path is not None
            else None
        ),
        "expected_sha256": plan.review.catalog_report_sha256,
        "errors": [],
    }
    if plan.review.catalog_report_path is not None:
        review_report["actual_sha256"] = _file_sha256(
            plan.review.catalog_report_path
        )
        review_report["errors"] = _review_report_errors(plan)
    return {
        "schema_version": 1,
        "mode": "offline_scalar_plan_audit",
        "implementation_status": "offline_gates_only",
        "live_execution_enabled": False,
        "pinning_prerequisites_ready": not blockers,
        "execution_ready": False,
        "pinning_blockers": blockers,
        "live_blocker": (
            "live selector mutation/scan execution is intentionally disabled "
            "until the cleanup-owning start/dwell/stop/pull supervisor is "
            "implemented and the live catalog is reviewed"
        ),
        "deferred_live_requirements": [
            (
                "require an existing same-boot alfaobd external-operation "
                "inhibit or atomically own a unique inhibit"
            ),
            (
                "never overwrite or remove a generic external-operation "
                "inhibit"
            ),
            (
                "wire the tested selector primitive into a supervisor that "
                "re-inventories and hash-matches the full live catalog before "
                "any row, OK, or scan tap"
            ),
            (
                "wire the tested post-stop artifact validator into a bounded "
                "start/dwell/stop/pull supervisor"
            ),
        ],
        "scalar_plan": {
            "path": str(plan.source_path),
            "sha256": plan.source_sha256,
            "reviewable_sha256": plan.reviewable_sha256,
        },
        "catalog_plan": {
            "path": str(plan.catalog_plan_path),
            "sha256": plan.catalog_plan_source_sha256,
            "expected_catalog_sha256": (
                plan.catalog_plan.expected_catalog_sha256
            ),
            "module_key": plan.catalog_plan.module_key,
            "expected_catalog_count": (
                plan.catalog_plan.expected_catalog_count
            ),
        },
        "catalog_review": review_report,
        "targets": [target.as_dict() for target in plan.targets],
        "schedule": [target.as_dict() for target in plan.schedule],
        "artifact_policy": {
            "artifacts": list(plan.artifacts),
            "required_segment_growth": list(
                plan.required_segment_growth
            ),
            "required_stop_stability": list(
                plan.required_stop_stability
            ),
            "csv_may_lag_while_running": True,
            "csv_growth_required_after_clean_stop": True,
            "csv_stability_required_after_growth": True,
            "identical_post_stop_size_observations": (
                plan.stop_stability_observations
            ),
            "recording_button_taps_permitted": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    plan_parser = subparsers.add_parser(
        "plan",
        help="validate and print the scalar plan entirely offline",
    )
    plan_parser.add_argument("plan", type=Path)
    audit_parser = subparsers.add_parser(
        "audit",
        help="audit pins, reviewed report, triples, and artifacts offline",
    )
    audit_parser.add_argument("plan", type=Path)
    run_parser = subparsers.add_parser(
        "run",
        help="validate offline pins/confirmations, then refuse (live path disabled)",
    )
    run_parser.add_argument("plan", type=Path)
    run_parser.add_argument("--adb-serial")
    run_parser.add_argument("--campaign-id")
    run_parser.add_argument(
        "--out-root",
        type=Path,
        default=DEFAULT_OUT_ROOT,
    )
    run_parser.add_argument("--require-mount", type=Path)
    run_parser.add_argument("--conditions", required=True)
    run_parser.add_argument("--passive-capture-campaign", required=True)
    run_parser.add_argument("--execute", action="store_true")
    run_parser.add_argument(
        "--confirm-read-only-diagnostics",
        action="store_true",
    )
    live_condition = run_parser.add_mutually_exclusive_group()
    live_condition.add_argument(
        "--confirm-parked-shakedown",
        action="store_true",
    )
    live_condition.add_argument(
        "--confirm-ordinary-driving",
        action="store_true",
    )
    run_parser.add_argument(
        "--confirm-scan-stopped",
        action="store_true",
    )
    run_parser.add_argument(
        "--confirm-debug-recording-enabled",
        action="store_true",
    )
    run_parser.add_argument(
        "--confirm-gauges-recording-enabled",
        action="store_true",
    )
    run_parser.add_argument(
        "--confirm-catalog-reviewed",
        action="store_true",
    )
    status_parser = subparsers.add_parser(
        "status",
        help="read an existing state.json; no ADB or subprocess",
    )
    status_parser.add_argument("campaign_dir", type=Path)
    return parser


def _print_plan(plan: ScalarPlan) -> None:
    audit = offline_audit(plan)
    payload = plan.as_dict()
    payload.update(
        {
            "implementation_status": audit["implementation_status"],
            "live_execution_enabled": False,
            "pinning_prerequisites_ready": audit[
                "pinning_prerequisites_ready"
            ],
            "execution_ready": False,
            "pinning_blockers": audit["pinning_blockers"],
            "live_blocker": audit["live_blocker"],
        }
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    print(
        "\nOFFLINE PLAN ONLY: no ADB, CAN, subprocess, service, network, "
        "proxy, mount, or output access occurred."
    )


def _validate_run_confirmations(
    args: argparse.Namespace,
) -> None:
    """Validate flags without touching ADB, services, mounts, or output."""
    if not args.execute:
        raise CampaignError("run is inert without --execute")
    if not args.confirm_read_only_diagnostics:
        raise CampaignError(
            "run requires --confirm-read-only-diagnostics"
        )
    if not (
        args.confirm_parked_shakedown
        or args.confirm_ordinary_driving
    ):
        raise CampaignError(
            "run requires --confirm-parked-shakedown or "
            "--confirm-ordinary-driving"
        )
    if not args.confirm_scan_stopped:
        raise CampaignError("run requires --confirm-scan-stopped")
    if not args.confirm_debug_recording_enabled:
        raise CampaignError(
            "run requires --confirm-debug-recording-enabled"
        )
    if not args.confirm_gauges_recording_enabled:
        raise CampaignError(
            "run requires --confirm-gauges-recording-enabled"
        )
    if not args.confirm_catalog_reviewed:
        raise CampaignError("run requires --confirm-catalog-reviewed")
    if not args.conditions.strip():
        raise CampaignError(
            "--conditions must describe the actual vehicle/UI state"
        )
    if args.require_mount is None:
        raise CampaignError("run requires --require-mount")
    if not args.out_root.is_absolute():
        raise CampaignError("--out-root must be absolute")
    if not args.require_mount.is_absolute():
        raise CampaignError("--require-mount must be absolute")
    try:
        output_inside = os.path.commonpath(
            (
                str(args.require_mount),
                str(args.out_root),
            )
        ) == str(args.require_mount)
    except ValueError:
        output_inside = False
    if not output_inside:
        raise CampaignError(
            "--out-root must be lexically below --require-mount"
        )
    if not CAMPAIGN_ID_RE.fullmatch(args.passive_capture_campaign):
        raise CampaignError(
            "--passive-capture-campaign must be a safe campaign ID"
        )
    if args.campaign_id is not None:
        if not CAMPAIGN_ID_RE.fullmatch(args.campaign_id):
            raise CampaignError("--campaign-id is unsafe")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
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
        if args.command == "audit":
            print(
                json.dumps(
                    offline_audit(plan),
                    indent=2,
                    sort_keys=True,
                )
            )
            print(
                "OFFLINE AUDIT ONLY: no ADB, CAN, subprocess, service, "
                "network, proxy, mount, or output access occurred."
            )
            return 0

        # Load/validate every cryptographic pin, review field, report row, and
        # exact target triple before even checking CLI live confirmations.
        # There is intentionally no CommandRunner or AdbClient construction in
        # this implementation.
        require_execution_ready(plan)
        _validate_run_confirmations(args)
        raise CampaignError(
            "live execution is intentionally disabled in this version; "
            "all pins and confirmations passed, but no ADB, service, mount, "
            "CAN, or output access was attempted"
        )
    except (CampaignError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
