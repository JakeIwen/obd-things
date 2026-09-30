"""Cache-only history, early-warning, and DTC dashboard integration.

This module deliberately has no CAN transport.  It stores broker snapshots in
the offline historian and serves bounded summaries from SQLite or an atomic
JSON DTC cache.  In particular, reading the DTC dashboard can never start a
diagnostic request or clear a code.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Mapping, Protocol, Sequence

from lib.modules import MODULES
from lib.timeutil import nonnegative_int as _nonnegative_int
from projects.vehicle_data import drift_warnings
from projects.vehicle_data.api import MAX_RESPONSE_BYTES
from projects.vehicle_data.dtc_descriptions import describe_dtc
from projects.vehicle_data.early_warning import (
    EarlyWarningEvaluator,
    InfrastructureHealthEvaluator,
)
from projects.vehicle_data.historian import TelemetryHistorian


DEFAULT_HISTORY_METRICS = (
    "battery.voltage",
    "engine.oil_pressure",
    "engine.coolant_temperature",
    "engine.vvt_oil_temperature",
    "engine.crankshaft_power",
    "transmission.oil_temperature",
    "generator.field_duty",
    "tire.pressure.fl",
    "tire.pressure.fr",
    "tire.pressure.rl",
    "tire.pressure.rr",
)
MAX_DTC_CACHE_BYTES = MAX_RESPONSE_BYTES
MAINTENANCE_CHECK_INTERVAL_SECONDS = 60 * 60
DRIFT_INTERVAL = timedelta(hours=24)
DTC_CACHE_SCHEMA_VERSION = 2
MAX_DELIVERY_ERROR_CHARS = 1000
USB_REMOVAL_KINDS = frozenset(
    (
        "usb_parent_hub_removed",
        "usb_can_adapter_removed",
        "usb_can_netdev_removed",
    )
)
DTC_GROUPS = (
    "current",
    "pending",
    "confirmed_history",
    "incomplete_only",
    "other",
)
DTC_AVAILABILITY = frozenset(("available", "unavailable", "never_scanned"))
DTC_RESULT_STATES = frozenset(
    (
        "dtcs_present",
        "no_dtcs",
        "status_coverage_incomplete",
        "unavailable",
        "never_scanned",
    )
)
DTC_OBSERVATION_STATES = frozenset(
    (
        "observed_in_latest_success",
        "stale_after_unavailable_attempt",
        "retained_incompatible_status_mask",
    )
)
_HEX_BYTE = re.compile(r"[0-9A-F]{2}\Z")
_RAW_DTC = re.compile(r"[0-9A-F]{6}\Z")

_COVERAGE_KEYS = frozenset(
    (
        "total_modules",
        "available_modules",
        "unavailable_modules",
        "never_scanned_modules",
        "modules_with_dtcs",
        "modules_no_dtcs",
        "modules_status_coverage_incomplete",
        "modules_with_last_known_dtcs",
        "last_attempt_at",
        "last_success_at",
    )
)
_MODULE_KEYS = frozenset(
    (
        "module_key",
        "module_name",
        "logical_bus",
        "resolved_channel",
        "bitrate",
        "availability",
        "result_state",
        "unavailable_reason",
        "last_attempt_at",
        "last_success_at",
        "successful_scans",
        "unavailable_scans",
        "consecutive_unavailable",
        "last_success_dtc_count",
        "status_availability_mask",
        "absence_authoritative",
    )
)
_DTC_RECORD_KEYS = frozenset(
    (
        "module_key",
        "module_name",
        "logical_bus",
        "resolved_channel",
        "module_availability",
        "module_result_state",
        "latest_attempt_successful",
        "last_attempt_at",
        "last_success_at",
        "raw_dtc",
        "fca_display",
        "status",
        "status_flags",
        "display_group",
        "current",
        "pending",
        "confirmed",
        "warning_indicator_requested",
        "incomplete_only",
        "present",
        "observation_state",
        "status_availability_mask",
        "first_seen_at",
        "last_seen_at",
        "observation_count",
        "episode_count",
        "recurrence_count",
        "status_changed_at",
        "resolved_at",
    )
)


def _unavailable(kind: str, detail: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "available": False,
        "reason": f"{kind}_unavailable",
        "detail": detail,
    }


class DtcCacheValidationError(ValueError):
    """A saved DTC cache does not match the compact schema-v2 contract."""


def _timestamp_or_none(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _exact_keys(value: object, expected: frozenset[str], label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise DtcCacheValidationError(f"{label} must be an object")
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise DtcCacheValidationError(
            f"{label} fields do not match schema-v2 "
            f"(missing={missing}, extra={extra})"
        )
    return value


def _validate_module_rows(rows: object) -> dict[str, Mapping]:
    if not isinstance(rows, list):
        raise DtcCacheValidationError("modules must be an array")
    expected_modules = frozenset(MODULES)
    if len(rows) != len(expected_modules):
        raise DtcCacheValidationError(
            f"modules must contain all {len(expected_modules)} registry rows"
        )
    by_key: dict[str, Mapping] = {}
    for index, candidate in enumerate(rows):
        row = _exact_keys(candidate, _MODULE_KEYS, f"modules[{index}]")
        module_key = row["module_key"]
        if not isinstance(module_key, str) or module_key not in expected_modules:
            raise DtcCacheValidationError(
                f"modules[{index}].module_key is not a registry module"
            )
        if module_key in by_key:
            raise DtcCacheValidationError(f"duplicate module row {module_key!r}")
        registry = MODULES[module_key]
        if row["logical_bus"] != registry.bus or row["bitrate"] != registry.bitrate:
            raise DtcCacheValidationError(
                f"module row {module_key!r} does not match registry bus/bitrate"
            )
        if not isinstance(row["module_name"], str) or not row["module_name"].strip():
            raise DtcCacheValidationError(f"module row {module_key!r} has no name")
        if row["resolved_channel"] is not None and (
            not isinstance(row["resolved_channel"], str)
            or not row["resolved_channel"]
        ):
            raise DtcCacheValidationError(
                f"module row {module_key!r} has an invalid resolved channel"
            )
        availability = row["availability"]
        result_state = row["result_state"]
        if availability not in DTC_AVAILABILITY or result_state not in DTC_RESULT_STATES:
            raise DtcCacheValidationError(
                f"module row {module_key!r} has an unknown availability/result state"
            )
        expected_results = {
            "never_scanned": {"never_scanned"},
            "unavailable": {"unavailable"},
            "available": {
                "dtcs_present",
                "no_dtcs",
                "status_coverage_incomplete",
            },
        }
        if result_state not in expected_results[availability]:
            raise DtcCacheValidationError(
                f"module row {module_key!r} has inconsistent availability/result state"
            )
        for field in (
            "successful_scans",
            "unavailable_scans",
            "consecutive_unavailable",
        ):
            if not _nonnegative_int(row[field]):
                raise DtcCacheValidationError(
                    f"module row {module_key!r}.{field} must be nonnegative"
                )
        if not _timestamp_or_none(row["last_attempt_at"]) or not _timestamp_or_none(
            row["last_success_at"]
        ):
            raise DtcCacheValidationError(
                f"module row {module_key!r} has an invalid timestamp"
            )
        dtc_count = row["last_success_dtc_count"]
        if dtc_count is not None and not _nonnegative_int(dtc_count):
            raise DtcCacheValidationError(
                f"module row {module_key!r} has an invalid last DTC count"
            )
        mask = row["status_availability_mask"]
        if mask is not None and (
            not isinstance(mask, str) or _HEX_BYTE.fullmatch(mask) is None
        ):
            raise DtcCacheValidationError(
                f"module row {module_key!r} has an invalid status mask"
            )
        if not isinstance(row["absence_authoritative"], bool):
            raise DtcCacheValidationError(
                f"module row {module_key!r}.absence_authoritative must be boolean"
            )
        successful_scans = row["successful_scans"]
        if successful_scans == 0:
            if row["last_success_at"] is not None or dtc_count is not None or mask is not None:
                raise DtcCacheValidationError(
                    f"module row {module_key!r} claims success fields without a success"
                )
        elif row["last_success_at"] is None or mask is None or dtc_count is None:
            raise DtcCacheValidationError(
                f"module row {module_key!r} is missing its successful result fields"
            )
        if availability == "never_scanned":
            if (
                row["last_attempt_at"] is not None
                or successful_scans != 0
                or row["unavailable_scans"] != 0
                or row["unavailable_reason"] is not None
                or row["absence_authoritative"]
            ):
                raise DtcCacheValidationError(
                    f"never-scanned module row {module_key!r} contains scan state"
                )
        else:
            if row["last_attempt_at"] is None:
                raise DtcCacheValidationError(
                    f"module row {module_key!r} has no latest attempt timestamp"
                )
            if availability == "unavailable":
                if not isinstance(row["unavailable_reason"], str) or not row[
                    "unavailable_reason"
                ]:
                    raise DtcCacheValidationError(
                        f"unavailable module row {module_key!r} has no reason"
                    )
            elif row["unavailable_reason"] is not None:
                raise DtcCacheValidationError(
                    f"available module row {module_key!r} has an unavailable reason"
                )
        if result_state == "no_dtcs" and (
            dtc_count != 0 or not row["absence_authoritative"]
        ):
            raise DtcCacheValidationError(
                f"module row {module_key!r} does not prove authoritative zero DTCs"
            )
        if result_state == "status_coverage_incomplete" and (
            dtc_count != 0 or row["absence_authoritative"]
        ):
            raise DtcCacheValidationError(
                f"module row {module_key!r} misstates incomplete status coverage"
            )
        if result_state == "dtcs_present" and (
            not isinstance(dtc_count, int) or dtc_count <= 0
        ):
            raise DtcCacheValidationError(
                f"module row {module_key!r} has no positive DTC count"
            )
        by_key[module_key] = row
    if frozenset(by_key) != expected_modules:
        raise DtcCacheValidationError("modules do not cover the complete registry")
    return by_key


def _validate_dtc_record(
    candidate: object,
    *,
    group: str,
    index: int,
    modules: Mapping[str, Mapping],
) -> None:
    record = _exact_keys(candidate, _DTC_RECORD_KEYS, f"groups.{group}[{index}]")
    module_key = record["module_key"]
    module = modules.get(module_key) if isinstance(module_key, str) else None
    if module is None:
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] references an unknown module"
        )
    for field, module_field in (
        ("module_name", "module_name"),
        ("logical_bus", "logical_bus"),
        ("resolved_channel", "resolved_channel"),
        ("module_availability", "availability"),
        ("module_result_state", "result_state"),
        ("last_attempt_at", "last_attempt_at"),
        ("last_success_at", "last_success_at"),
    ):
        if record[field] != module[module_field]:
            raise DtcCacheValidationError(
                f"groups.{group}[{index}].{field} disagrees with its module row"
            )
    if record["latest_attempt_successful"] is not (
        module["availability"] == "available"
    ):
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] has inconsistent latest-attempt state"
        )
    if not isinstance(record["raw_dtc"], str) or _RAW_DTC.fullmatch(
        record["raw_dtc"]
    ) is None:
        raise DtcCacheValidationError(f"groups.{group}[{index}] has an invalid DTC")
    if not isinstance(record["status"], str) or _HEX_BYTE.fullmatch(
        record["status"]
    ) is None:
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] has an invalid status byte"
        )
    if not isinstance(record["status_flags"], list) or not all(
        isinstance(flag, str) for flag in record["status_flags"]
    ):
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] has invalid status flags"
        )
    for field in (
        "current",
        "pending",
        "confirmed",
        "warning_indicator_requested",
        "incomplete_only",
        "present",
    ):
        if not isinstance(record[field], bool):
            raise DtcCacheValidationError(
                f"groups.{group}[{index}].{field} must be boolean"
            )
    if record["present"] is not True:
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] is not a present saved state"
        )
    observation_state = record["observation_state"]
    if observation_state not in DTC_OBSERVATION_STATES:
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] has an unknown observation state"
        )
    if observation_state == "stale_after_unavailable_attempt" and module[
        "availability"
    ] != "unavailable":
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] stale state disagrees with module availability"
        )
    if observation_state != "stale_after_unavailable_attempt" and module[
        "availability"
    ] != "available":
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] observation state requires an available module"
        )
    expected_display_group = group if group != "other" else None
    if expected_display_group is not None and record["display_group"] != expected_display_group:
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] has the wrong display group"
        )
    if group == "other" and record["display_group"] in DTC_GROUPS[:-1]:
        raise DtcCacheValidationError(
            f"groups.other[{index}] contains a standard grouped record"
        )
    semantic_flag = {
        "current": "current",
        "pending": "pending",
        "confirmed_history": "confirmed",
        "incomplete_only": "incomplete_only",
    }.get(group)
    if semantic_flag is not None and record[semantic_flag] is not True:
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] lacks its grouping status bit"
        )
    mask = record["status_availability_mask"]
    if not isinstance(mask, str) or _HEX_BYTE.fullmatch(mask) is None:
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] has an invalid status mask"
        )
    for field in ("first_seen_at", "last_seen_at", "status_changed_at"):
        if not isinstance(record[field], str) or not record[field].strip():
            raise DtcCacheValidationError(
                f"groups.{group}[{index}].{field} is not a timestamp"
            )
    if record["resolved_at"] is not None:
        raise DtcCacheValidationError(
            f"groups.{group}[{index}] unexpectedly contains a resolved record"
        )
    for field in ("observation_count", "episode_count", "recurrence_count"):
        if not _nonnegative_int(record[field]):
            raise DtcCacheValidationError(
                f"groups.{group}[{index}].{field} must be nonnegative"
            )


def _validate_dtc_cache(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise DtcCacheValidationError("saved DTC cache root is not a JSON object")
    required_top_level = frozenset(
        (
            "schema_version",
            "generated_at",
            "acquisition",
            "compact",
            "per_group_limit",
            "group_counts",
            "group_returned_counts",
            "groups_truncated",
            "coverage",
            "groups",
            "modules",
        )
    )
    _exact_keys(payload, required_top_level, "saved DTC cache")
    if payload["schema_version"] != DTC_CACHE_SCHEMA_VERSION:
        raise DtcCacheValidationError("saved DTC cache is not schema version 2")
    if payload["acquisition"] != "cache_only" or payload["compact"] is not True:
        raise DtcCacheValidationError("saved DTC cache is not a compact cache-only summary")
    if not _timestamp_or_none(payload["generated_at"]) or payload["generated_at"] is None:
        raise DtcCacheValidationError("saved DTC cache has no generation timestamp")
    limit = payload["per_group_limit"]
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise DtcCacheValidationError("saved DTC cache has an invalid group limit")

    groups = _exact_keys(payload["groups"], frozenset(DTC_GROUPS), "groups")
    counts = _exact_keys(
        payload["group_counts"], frozenset(DTC_GROUPS), "group_counts"
    )
    returned = _exact_keys(
        payload["group_returned_counts"],
        frozenset(DTC_GROUPS),
        "group_returned_counts",
    )
    modules = _validate_module_rows(payload["modules"])
    for group in DTC_GROUPS:
        entries = groups[group]
        if not isinstance(entries, list):
            raise DtcCacheValidationError(f"groups.{group} must be an array")
        if not _nonnegative_int(counts[group]) or not _nonnegative_int(returned[group]):
            raise DtcCacheValidationError(f"{group} counts must be nonnegative integers")
        if returned[group] != len(entries) or returned[group] > counts[group]:
            raise DtcCacheValidationError(f"{group} returned count does not match its records")
        if returned[group] > limit:
            raise DtcCacheValidationError(f"groups.{group} exceeds the compact limit")
        for index, record in enumerate(entries):
            _validate_dtc_record(record, group=group, index=index, modules=modules)
    expected_truncated = any(returned[group] < counts[group] for group in DTC_GROUPS)
    if not isinstance(payload["groups_truncated"], bool) or payload[
        "groups_truncated"
    ] != expected_truncated:
        raise DtcCacheValidationError("groups_truncated does not match group counts")

    coverage = _exact_keys(payload["coverage"], _COVERAGE_KEYS, "coverage")
    for field in _COVERAGE_KEYS - {"last_attempt_at", "last_success_at"}:
        if not _nonnegative_int(coverage[field]):
            raise DtcCacheValidationError(f"coverage.{field} must be nonnegative")
    if not _timestamp_or_none(coverage["last_attempt_at"]) or not _timestamp_or_none(
        coverage["last_success_at"]
    ):
        raise DtcCacheValidationError("coverage timestamps are invalid")
    module_rows = list(modules.values())
    expected_coverage = {
        "total_modules": len(module_rows),
        "available_modules": sum(row["availability"] == "available" for row in module_rows),
        "unavailable_modules": sum(
            row["availability"] == "unavailable" for row in module_rows
        ),
        "never_scanned_modules": sum(
            row["availability"] == "never_scanned" for row in module_rows
        ),
        "modules_with_dtcs": sum(
            row["result_state"] == "dtcs_present" for row in module_rows
        ),
        "modules_no_dtcs": sum(
            row["result_state"] == "no_dtcs" for row in module_rows
        ),
        "modules_status_coverage_incomplete": sum(
            row["result_state"] == "status_coverage_incomplete" for row in module_rows
        ),
    }
    for field, expected in expected_coverage.items():
        if coverage[field] != expected:
            raise DtcCacheValidationError(
                f"coverage.{field} does not match the module rows"
            )
    if coverage["modules_with_last_known_dtcs"] > len(module_rows):
        raise DtcCacheValidationError(
            "coverage.modules_with_last_known_dtcs exceeds total modules"
        )
    attempt_times = [row["last_attempt_at"] for row in module_rows if row["last_attempt_at"]]
    success_times = [row["last_success_at"] for row in module_rows if row["last_success_at"]]
    if coverage["last_attempt_at"] != (max(attempt_times) if attempt_times else None):
        raise DtcCacheValidationError("coverage.last_attempt_at does not match module rows")
    if coverage["last_success_at"] != (max(success_times) if success_times else None):
        raise DtcCacheValidationError("coverage.last_success_at does not match module rows")
    returned_modules = {
        record["module_key"]
        for group in DTC_GROUPS
        for record in groups[group]
    }
    if len(returned_modules) > coverage["modules_with_last_known_dtcs"]:
        raise DtcCacheValidationError(
            "returned DTC records exceed modules with last-known DTC state"
        )
    return payload


MAX_VAN_SCAN_CACHE_BYTES = 256 * 1024
VAN_SCAN_CACHE_SCHEMA_VERSION = 1
VAN_SCAN_CACHE_NAME = "van-scan-cache.json"
_HEX_TEXT = re.compile(r"(?:[0-9A-F]{2})+\Z")


def _validate_van_scan_cache(payload: object) -> dict[str, object]:
    """Validate ``van_scan_harvest.py``'s cache of the van's own (source F1) health checks."""

    if not isinstance(payload, dict):
        raise DtcCacheValidationError("in-vehicle scan cache root is not a JSON object")
    if payload.get("schema_version") != VAN_SCAN_CACHE_SCHEMA_VERSION:
        raise DtcCacheValidationError("in-vehicle scan cache is not schema version 1")
    if payload.get("type") != "in_vehicle_scan_cache":
        raise DtcCacheValidationError("in-vehicle scan cache has the wrong type")
    if not isinstance(payload.get("provenance"), str):
        raise DtcCacheValidationError("in-vehicle scan cache has no provenance")
    if not _nonnegative_int(payload.get("scan_count")):
        raise DtcCacheValidationError("in-vehicle scan cache scan_count is invalid")
    last_scan = payload.get("last_scan")
    if last_scan is not None:
        if not isinstance(last_scan, dict):
            raise DtcCacheValidationError("in-vehicle scan cache last_scan must be an object")
        for field in ("started_at", "completed_at"):
            if not isinstance(last_scan.get(field), str) or not _timestamp_or_none(last_scan[field]):
                raise DtcCacheValidationError(f"last_scan.{field} is not a timestamp")
        if not isinstance(last_scan.get("pi_quiet"), bool):
            raise DtcCacheValidationError("last_scan.pi_quiet must be a boolean")
    modules = payload.get("modules")
    if not isinstance(modules, list):
        raise DtcCacheValidationError("in-vehicle scan cache modules must be an array")
    seen: set[str] = set()
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            raise DtcCacheValidationError(f"modules[{index}] must be an object")
        key = module.get("module_key")
        if not isinstance(key, str) or not key or key in seen:
            raise DtcCacheValidationError(f"modules[{index}].module_key is invalid or repeated")
        seen.add(key)
        observed = module.get("observed_at")
        if not isinstance(observed, str) or not _timestamp_or_none(observed):
            raise DtcCacheValidationError(f"modules[{index}].observed_at is not a timestamp")
        dtcs = module.get("dtcs")
        if not isinstance(dtcs, list):
            raise DtcCacheValidationError(f"modules[{index}].dtcs must be an array")
        for position, record in enumerate(dtcs):
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("raw_dtc"), str)
                or not _HEX_TEXT.fullmatch(record["raw_dtc"])
                or not isinstance(record.get("fca_display"), str)
                or not isinstance(record.get("status"), str)
                or _HEX_BYTE.fullmatch(record["status"]) is None
                or not isinstance(record.get("display_group"), str)
            ):
                raise DtcCacheValidationError(
                    f"modules[{index}].dtcs[{position}] is not a valid code record"
                )
    return payload


class AdvisoryNotificationSink(Protocol):
    """Optional delivery boundary; no external sink is implemented here."""

    enabled: bool

    def deliver(self, payload: Mapping[str, object]) -> None: ...


class DisabledAdvisoryNotificationSink:
    """Safe default which cannot perform an external notification."""

    enabled = False

    def deliver(self, payload: Mapping[str, object]) -> None:
        raise RuntimeError("advisory notification delivery is disabled")


class AdvisoryNotificationDispatcher:
    """Drain persisted, deduplicated outbox rows only when explicitly enabled."""

    def __init__(
        self,
        historian: TelemetryHistorian,
        *,
        sink: AdvisoryNotificationSink | None = None,
        enabled: bool = False,
    ) -> None:
        self.historian = historian
        self.sink = sink or DisabledAdvisoryNotificationSink()
        self.enabled = bool(enabled and self.sink.enabled)
        self._lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._status: dict[str, object] = {
            "enabled": self.enabled,
            "sink": type(self.sink).__name__,
            "last_attempt_at": None,
            "last_delivered": 0,
            "last_failed": 0,
            "last_error": None,
        }

    def status(self) -> dict[str, object]:
        with self._lock:
            return json.loads(json.dumps(self._status))

    def dispatch(
        self,
        *,
        at: datetime | None = None,
        limit: int = 10,
    ) -> dict[str, object]:
        # Serialize the complete claim-by-query/deliver/mark cycle.  Without
        # this boundary, two callers can fetch the same pending row and both
        # invoke an external sink before either marks it delivered.
        with self._dispatch_lock:
            return self._dispatch_once(at=at, limit=limit)

    @staticmethod
    def _delivery_error(exc: Exception) -> str:
        detail = f"{type(exc).__name__}: {exc}"
        if len(detail) > MAX_DELIVERY_ERROR_CHARS:
            detail = detail[: MAX_DELIVERY_ERROR_CHARS - 1] + "…"
        return detail

    def _dispatch_once(
        self,
        *,
        at: datetime | None = None,
        limit: int = 10,
    ) -> dict[str, object]:
        moment = at or datetime.now(timezone.utc)
        attempted_at = moment.astimezone(timezone.utc).isoformat()
        if not self.enabled:
            with self._lock:
                self._status.update(
                    {
                        "last_attempt_at": attempted_at,
                        "last_delivered": 0,
                        "last_failed": 0,
                        "last_error": "delivery_disabled",
                    }
                )
            return self.status()
        delivered = 0
        failed = 0
        last_error = None
        for item in self.historian.pending_advisory_notifications(
            at=moment,
            limit=limit,
        ):
            try:
                self.sink.deliver(item["payload"])
            except Exception as exc:
                failed += 1
                last_error = self._delivery_error(exc)
                self.historian.mark_advisory_notification_failed(
                    int(item["id"]),
                    error=last_error,
                    attempted_at=moment,
                )
            else:
                delivered += 1
                self.historian.mark_advisory_notification_delivered(
                    int(item["id"]),
                    delivered_at=moment,
                )
        with self._lock:
            self._status.update(
                {
                    "last_attempt_at": attempted_at,
                    "last_delivered": delivered,
                    "last_failed": failed,
                    "last_error": last_error,
                }
            )
        return self.status()


class TelemetryInsights:
    """Thread-safe facade for the broker's slower cache-only data products."""

    def __init__(
        self,
        historian: TelemetryHistorian,
        *,
        warning_evaluator: EarlyWarningEvaluator | None = None,
        infrastructure_evaluator: InfrastructureHealthEvaluator | None = None,
        notification_sink: AdvisoryNotificationSink | None = None,
        enable_notification_delivery: bool = False,
        dtc_cache_path: str | Path,
        van_scan_cache_path: str | Path | None = None,
        history_metrics: Sequence[str] = DEFAULT_HISTORY_METRICS,
        maintenance_check_interval_seconds: float = (
            MAINTENANCE_CHECK_INTERVAL_SECONDS
        ),
    ) -> None:
        self.historian = historian
        self.warning_evaluator = warning_evaluator or EarlyWarningEvaluator(
            historian
        )
        self.infrastructure_evaluator = (
            infrastructure_evaluator or InfrastructureHealthEvaluator(historian)
        )
        self.notification_dispatcher = AdvisoryNotificationDispatcher(
            historian,
            sink=notification_sink,
            enabled=enable_notification_delivery,
        )
        self.dtc_cache_path = Path(dtc_cache_path)
        # The van's own health checks, harvested passively beside the Pi's DTC cache.
        self.van_scan_cache_path = (
            Path(van_scan_cache_path)
            if van_scan_cache_path is not None
            else self.dtc_cache_path.with_name(VAN_SCAN_CACHE_NAME)
        )
        self._van_scan_identity: tuple[int, int, int, int] | None = None
        self._van_scan_payload: dict[str, object] | None = None
        self.history_metrics = tuple(dict.fromkeys(history_metrics))
        if not self.history_metrics:
            raise ValueError("at least one history metric is required")
        if maintenance_check_interval_seconds <= 0:
            raise ValueError("maintenance_check_interval_seconds must be positive")
        self.maintenance_check_interval_seconds = maintenance_check_interval_seconds
        self._maintenance_lock = threading.Lock()
        self._maintenance_next_check = 0.0
        self._maintenance_hook: dict[str, object] = {
            "interval_seconds": maintenance_check_interval_seconds,
            "last_checked_at": None,
            "last_result": None,
            "last_error": None,
        }
        self._advisory_lock = threading.Lock()
        self._advisory_hook: dict[str, object] = {
            "mode": "on_ingest",
            "last_evaluated_at": None,
            "last_result": None,
            "last_error": None,
        }
        self._dtc_lock = threading.Lock()
        self._dtc_identity: tuple[int, int, int, int] | None = None
        self._dtc_payload: dict[str, object] | None = None
        # Tier-2 drift notices: evaluated at most daily from the maintenance
        # hook, cached here and merged into every advisory persistence call.
        self._drift_lock = threading.Lock()
        self._drift: dict[str, object] = {
            "last_run_at": None,
            "last_error": None,
            "assessments": [],
        }
        try:
            self._drift_path = drift_warnings.state_path_for(historian)
        except Exception:
            self._drift_path = None
        if self._drift_path is not None:
            state = drift_warnings.load_state(self._drift_path)
            if state is not None:
                # Findings saved by other rule revisions are kept for continuity
                # but re-evaluated at the first maintenance check.
                current = drift_warnings.state_is_current(state["assessments"])
                self._drift["last_run_at"] = state["last_run_at"] if current else None
                self._drift["assessments"] = state["assessments"]

    def close(self) -> None:
        self.historian.close()

    def ingest_snapshot(
        self,
        snapshot: Mapping[str, object],
        *,
        captured_at: datetime,
        ingest_key: str,
    ):
        result = self.historian.ingest_snapshot(
            snapshot,
            captured_at=captured_at,
            ingest_key=ingest_key,
        )
        checkpoint_complete = False
        consumed_event_ids: tuple[str, ...] = ()
        checkpoint_error: str | None = "duplicate_snapshot_not_evaluated"
        monitor = snapshot.get("_usb_can_monitor")
        monitor = monitor if isinstance(monitor, Mapping) else {}
        monitor_events = monitor.get("events", [])
        monitor_events = monitor_events if isinstance(monitor_events, list) else []
        monitor_removal_event_ids = tuple(
            event.get("event_id")
            for event in monitor_events
            if isinstance(event, Mapping)
            and event.get("kind") in USB_REMOVAL_KINDS
            and isinstance(event.get("event_id"), str)
        )
        if not result.duplicate:
            (
                checkpoint_complete,
                consumed_event_ids,
                checkpoint_error,
            ) = self._evaluate_and_persist_advisories(
                result.snapshot_id,
                captured_at=captured_at,
                monitor_removal_event_ids=monitor_removal_event_ids,
            )
        self._maybe_run_maintenance(captured_at=captured_at)
        try:
            return replace(
                result,
                advisory_checkpoint_complete=checkpoint_complete,
                advisory_consumed_event_ids=consumed_event_ids,
                advisory_checkpoint_error=checkpoint_error,
            )
        except TypeError:
            # Lightweight injected historian results used by offline callers
            # may not be dataclasses.  Preserve their shape while exposing the
            # same explicit acknowledgement contract.
            result.advisory_checkpoint_complete = checkpoint_complete
            result.advisory_consumed_event_ids = consumed_event_ids
            result.advisory_checkpoint_error = checkpoint_error
            return result

    def _evaluate_and_persist_advisories(
        self,
        snapshot_id: int,
        *,
        captured_at: datetime,
        monitor_removal_event_ids: tuple[str, ...] = (),
    ) -> tuple[bool, tuple[str, ...], str | None]:
        evaluated_at = captured_at.astimezone(timezone.utc).isoformat()
        try:
            vehicle = self.warning_evaluator.evaluate(at=captured_at)
            infrastructure = self.infrastructure_evaluator.evaluate(
                snapshot_id,
                at=captured_at,
            )
            vehicle_assessments = list(vehicle.get("assessments", []))
            infrastructure_assessments = list(infrastructure.get("assessments", []))
            evaluated_rules = {
                assessment.get("rule")
                for assessment in (*vehicle_assessments, *infrastructure_assessments)
                if isinstance(assessment, Mapping)
            }
            drift = [
                assessment
                for assessment in self._drift_assessments()
                if assessment.get("rule") not in evaluated_rules
            ]
            assessments = [
                *vehicle_assessments,
                *drift,
                *infrastructure_assessments,
            ]
            authoritative_rule_keys = tuple(
                assessment.get("rule")
                for assessment in assessments
                if isinstance(assessment, Mapping)
            )
            persistence = self.historian.record_advisory_assessments(
                assessments,
                evaluated_at=captured_at,
                authoritative_rule_keys=authoritative_rule_keys,
            )
            persistence_result = persistence.as_dict()
            usb_event_ids: tuple[str, ...] = ()
            for assessment in infrastructure.get("assessments", []):
                if (
                    isinstance(assessment, Mapping)
                    and assessment.get("rule")
                    == "usb_can_transient_disconnect"
                ):
                    current = assessment.get("current")
                    current = current if isinstance(current, Mapping) else {}
                    values = current.get("event_ids", [])
                    if isinstance(values, list):
                        usb_event_ids = tuple(
                            value for value in values if isinstance(value, str)
                        )
                    break
            self.historian.mark_usb_can_advisory_events_consumed(
                usb_event_ids,
                consumed_at=captured_at,
                snapshot_id=snapshot_id,
            )
            consumed_event_ids = (
                self.historian.usb_can_advisory_consumed_event_ids(
                    monitor_removal_event_ids
                )
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with self._advisory_lock:
                self._advisory_hook.update(
                    {
                        "last_evaluated_at": evaluated_at,
                        "last_result": None,
                        "last_error": error,
                    }
                )
            return False, (), error
        else:
            delivery_error = None
            try:
                delivery = self.notification_dispatcher.dispatch(at=captured_at)
            except Exception as exc:
                # Advisory persistence and USB event consumption are already
                # durable.  A delivery-boundary failure remains represented by
                # the outbox and must not make the broker retain/replay the
                # kernel event queue.
                delivery_error = (
                    f"notification dispatch failed: {type(exc).__name__}: {exc}"
                )
                delivery = {
                    "enabled": self.notification_dispatcher.enabled,
                    "last_error": delivery_error,
                }
            with self._advisory_lock:
                self._advisory_hook.update(
                    {
                        "last_evaluated_at": evaluated_at,
                        "last_result": {
                            "vehicle_assessments": len(
                                vehicle.get("assessments", [])
                            ),
                            "infrastructure_assessments": len(
                                infrastructure.get("assessments", [])
                            ),
                            "drift_assessments": len(drift),
                            "persistence": persistence_result,
                            "delivery": delivery,
                        },
                        "last_error": delivery_error,
                    }
                )
            return True, consumed_event_ids, None

    def _maybe_run_maintenance(self, *, captured_at: datetime) -> None:
        """Run the historian's daily-gated maintenance at most once per hour."""

        now_monotonic = time.monotonic()
        with self._maintenance_lock:
            if now_monotonic < self._maintenance_next_check:
                return
            # Reserve the interval before doing work so concurrent ingest calls
            # cannot start duplicate retention passes.
            self._maintenance_next_check = (
                now_monotonic + self.maintenance_check_interval_seconds
            )
        checked_at = captured_at.astimezone(timezone.utc).isoformat()
        try:
            maintenance = self.historian.maybe_run_maintenance(now=captured_at)
            maintenance_result = maintenance.as_dict()
        except Exception as exc:
            with self._maintenance_lock:
                self._maintenance_hook.update(
                    {
                        "last_checked_at": checked_at,
                        "last_result": None,
                        "last_error": f"{type(exc).__name__}: {exc}",
                    }
                )
        else:
            with self._maintenance_lock:
                self._maintenance_hook.update(
                    {
                        "last_checked_at": checked_at,
                        "last_result": maintenance_result,
                        "last_error": None,
                    }
                )
        # A separate second step: it runs whether maintenance succeeded, was
        # not due, or raised, and its failures never touch the maintenance hook.
        self._maybe_run_drift(captured_at=captured_at)

    def _drift_assessments(self) -> list[dict[str, object]]:
        with self._drift_lock:
            return copy.deepcopy(list(self._drift["assessments"]))

    def _maybe_run_drift(self, *, captured_at: datetime) -> None:
        """Run the tier-2 drift job when the last run is 24 h old (by data time).

        The gate compares snapshot times, never ``time.monotonic()``; the caller
        already limits this to one check per maintenance interval.
        """

        try:
            moment = captured_at.astimezone(timezone.utc)
            with self._drift_lock:
                last_text = self._drift["last_run_at"]
                previous = {
                    item.get("rule"): item.get("state")
                    for item in self._drift["assessments"]
                    if isinstance(item, Mapping)
                }
            last = None
            if isinstance(last_text, str):
                try:
                    last = datetime.fromisoformat(last_text.replace("Z", "+00:00"))
                except ValueError:
                    last = None
            if last is not None and last.tzinfo is not None and (
                timedelta(0) <= moment - last < DRIFT_INTERVAL
            ):
                return
            assessments = drift_warnings.evaluate_drift(
                self.historian,
                at=moment,
                previous_state=previous,
            )
            assessments = json.loads(json.dumps(list(assessments), allow_nan=False))
        except Exception as exc:
            with self._drift_lock:
                self._drift["last_error"] = f"{type(exc).__name__}: {exc}"
            return
        last_run_at = moment.isoformat()
        with self._drift_lock:
            self._drift.update(
                {
                    "last_run_at": last_run_at,
                    "last_error": None,
                    "assessments": assessments,
                }
            )
            path = self._drift_path
        if path is None:
            return
        try:
            drift_warnings.save_state(
                path,
                last_run_at=last_run_at,
                assessments=assessments,
            )
        except Exception as exc:
            with self._drift_lock:
                self._drift["last_error"] = (
                    f"drift state not saved: {type(exc).__name__}: {exc}"
                )

    def drift_status(self) -> dict[str, object]:
        with self._drift_lock:
            assessments = self._drift["assessments"]
            return {
                "last_run_at": self._drift["last_run_at"],
                "last_error": self._drift["last_error"],
                "finding_count": sum(
                    1
                    for item in assessments
                    if isinstance(item, Mapping) and item.get("state") == "warning"
                ),
            }

    def history_response(self) -> dict[str, object]:
        """Return bounded aggregates plus at most 96 downsampled points/metric."""

        summary = self.historian.dashboard_summary(
            metrics=self.history_metrics
        )
        comparisons = summary.get("trip_comparison", {}).get("metrics", {})
        windows = summary.get("windows", {})
        seven = windows.get("7d", {}).get("metrics", {})
        thirty = windows.get("30d", {}).get("metrics", {})
        trends: dict[str, object] = {}
        for metric in self.history_metrics:
            series = self.historian.metric_series(
                metric,
                window_seconds=24 * 60 * 60,
                max_points=96,
            )
            current = comparisons.get(metric, {}).get("current_trip")
            seven_metric = seven.get(metric)
            thirty_metric = thirty.get(metric)
            units = series.get("units") or []
            unit = units[0] if len(units) == 1 else None
            if unit is None:
                for candidate in (current, seven_metric, thirty_metric):
                    candidate_units = (
                        candidate.get("units")
                        if isinstance(candidate, Mapping)
                        else None
                    )
                    if isinstance(candidate_units, list) and len(candidate_units) == 1:
                        unit = candidate_units[0]
                        break
            trends[metric] = {
                "unit": unit,
                "current_trip": current,
                "prior_trips": comparisons.get(metric, {}).get("prior_trips"),
                "current_minus_prior_median": comparisons.get(metric, {}).get(
                    "current_minus_prior_median"
                ),
                "days_7": seven_metric,
                "days_30": thirty_metric,
                "sparkline": series.get("points", []),
                "series": {
                    key: series.get(key)
                    for key in (
                        "start_at",
                        "end_at",
                        "bucket_seconds",
                        "point_limit",
                        "mixed_provenance",
                        "series_basis",
                        "rollup_backlog",
                    )
                },
            }
        summary["metric_trends"] = trends
        with self._maintenance_lock:
            summary["maintenance_hook"] = json.loads(
                json.dumps(self._maintenance_hook)
            )
        summary["available"] = True
        summary["detail"] = (
            "Bounded, cache-only history; missing and stale observations remain "
            "explicit coverage gaps."
        )
        return summary

    def health_response(self) -> dict[str, object]:
        summary = self.warning_evaluator.evaluate()
        # New lists, never in-place extension: an evaluator may hand back the
        # same summary object on every call.
        existing = summary.get("assessments")
        existing = list(existing) if isinstance(existing, list) else []
        active = summary.get("active")
        active = list(active) if isinstance(active, list) else []
        known = {item.get("rule") for item in existing if isinstance(item, Mapping)}
        drift = [
            item for item in self._drift_assessments() if item.get("rule") not in known
        ]
        summary["assessments"] = [*existing, *drift]
        summary["active"] = [
            *active,
            *(copy.deepcopy(item) for item in drift if item.get("state") == "warning"),
        ]
        summary["drift"] = self.drift_status()
        try:
            summary["episodes"] = self.historian.advisory_summary()
        except (AttributeError, RuntimeError, ValueError) as exc:
            summary["episodes"] = _unavailable(
                "advisory_history",
                f"persisted advisory history is unavailable: {exc}",
            )
        try:
            summary["data_quality"] = self.historian.data_quality_summary()
        except (AttributeError, RuntimeError, ValueError) as exc:
            summary["data_quality"] = _unavailable(
                "data_quality_history",
                f"persisted data-quality history is unavailable: {exc}",
            )
        try:
            summary["usb_can_incidents"] = (
                self.historian.usb_can_incident_summary()
            )
        except (AttributeError, RuntimeError, ValueError) as exc:
            summary["usb_can_incidents"] = _unavailable(
                "usb_can_incident_history",
                f"persisted USB CAN incident history is unavailable: {exc}",
            )
        with self._advisory_lock:
            summary["evaluation_hook"] = json.loads(
                json.dumps(self._advisory_hook)
            )
        summary["notification_delivery"] = self.notification_dispatcher.status()
        summary["available"] = True
        summary["detail"] = (
            "History-relative advisory evidence only; this is not a diagnosis "
            "or an opaque health score."
        )
        from projects.vehicle_data.health_summary import health_overview
        return health_overview(summary)

    def acknowledge_episode(
        self,
        episode_id: int,
        *,
        note: str | None = None,
    ) -> dict[str, object]:
        """Backend-only acknowledgement; no web route is exposed here."""

        return self.historian.acknowledge_advisory_episode(
            episode_id,
            note=note,
        )

    def dispatch_notifications(self) -> dict[str, object]:
        """Explicit dispatcher hook; disabled by default in every deployment."""

        return self.notification_dispatcher.dispatch()

    def dtc_response(self) -> dict[str, object]:
        """Saved Pi DTC cache plus the van's own latest health-check results.

        ``in_vehicle_scan`` is additive: the Pi's groups, modules and coverage are unchanged, and
        the in-vehicle results (a ``19 02 0D`` status mask read by the van's own diagnostic
        client, harvested passively) are never merged into them here.  Neither part can start a
        diagnostic request.
        """

        payload = self._pi_dtc_response()
        payload["in_vehicle_scan"] = self._van_scan_response()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) >= MAX_RESPONSE_BYTES:
            payload["in_vehicle_scan"] = {
                "available": False,
                "reason": "in_vehicle_scan_too_large",
                "detail": "in-vehicle scan results exceed the broker transport limit",
            }
        return payload

    def _van_scan_response(self) -> dict[str, object]:
        """Read the harvester's bounded atomic cache; never perform diagnostic I/O."""

        path = self.van_scan_cache_path
        try:
            with path.open("rb") as handle:
                stat_result = os.fstat(handle.fileno())
                identity = (
                    int(stat_result.st_ino),
                    int(stat_result.st_mtime_ns),
                    int(stat_result.st_ctime_ns),
                    int(stat_result.st_size),
                )
                if identity == self._van_scan_identity and self._van_scan_payload is not None:
                    return copy.deepcopy(self._van_scan_payload)
                raw = handle.read(MAX_VAN_SCAN_CACHE_BYTES + 1)
        except FileNotFoundError:
            return {
                "available": False,
                "reason": "not_harvested",
                "detail": "No in-vehicle health check has been harvested yet.",
            }
        except OSError as exc:
            return {
                "available": False,
                "reason": "in_vehicle_scan_unavailable",
                "detail": f"in-vehicle scan cache read failed: {exc}",
            }
        if len(raw) > MAX_VAN_SCAN_CACHE_BYTES:
            return {
                "available": False,
                "reason": "in_vehicle_scan_unavailable",
                "detail": "in-vehicle scan cache exceeds its 256 KiB limit",
            }
        try:
            cache = _validate_van_scan_cache(json.loads(raw))
        except (UnicodeDecodeError, json.JSONDecodeError, DtcCacheValidationError) as exc:
            return {
                "available": False,
                "reason": "in_vehicle_scan_unavailable",
                "detail": f"in-vehicle scan cache is invalid: {exc}",
            }
        modules = []
        for module in cache["modules"]:
            records = []
            for record in module["dtcs"]:
                records.append(
                    {
                        **record,
                        **describe_dtc(module["module_key"], record["fca_display"]),
                    }
                )
            modules.append({**module, "dtcs": records})
        payload = {
            "available": True,
            "acquisition": "passive_recording",
            "source": "in_vehicle_scan",
            "provenance": cache["provenance"],
            "tester_source": cache.get("tester_source"),
            "dtc_request": cache.get("dtc_request"),
            "status_mask_note": cache.get("status_mask_note"),
            "generated_at": cache.get("generated_at"),
            "scan_count": cache["scan_count"],
            "last_scan": cache["last_scan"],
            "modules": modules,
        }
        self._van_scan_identity = identity
        self._van_scan_payload = payload
        return copy.deepcopy(payload)

    def _pi_dtc_response(self) -> dict[str, object]:
        """Read one bounded atomic cache; never perform diagnostic I/O."""

        with self._dtc_lock:
            try:
                with self.dtc_cache_path.open("rb") as handle:
                    stat_result = os.fstat(handle.fileno())
                    if stat_result.st_size > MAX_DTC_CACHE_BYTES:
                        return _unavailable(
                            "dtc_cache",
                            "saved DTC cache exceeds the broker's one-megabyte limit",
                        )
                    identity = (
                        int(stat_result.st_ino),
                        int(stat_result.st_mtime_ns),
                        int(stat_result.st_ctime_ns),
                        int(stat_result.st_size),
                    )
                    if (
                        identity == self._dtc_identity
                        and self._dtc_payload is not None
                    ):
                        return json.loads(json.dumps(self._dtc_payload))
                    raw = handle.read(MAX_DTC_CACHE_BYTES + 1)
                if len(raw) > MAX_DTC_CACHE_BYTES:
                    return _unavailable(
                        "dtc_cache",
                        "saved DTC cache exceeds the broker's one-megabyte limit",
                    )
                payload = _validate_dtc_cache(json.loads(raw))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                return _unavailable("dtc_cache", f"saved DTC cache is invalid: {exc}")
            except DtcCacheValidationError as exc:
                return _unavailable(
                    "dtc_cache", f"saved DTC cache schema is invalid: {exc}"
                )
            payload["available"] = True
            payload["acquisition"] = "cache_only"
            described = 0
            description_total = 0
            for entries in payload["groups"].values():
                for record in entries:
                    description = describe_dtc(
                        record.get("module_key"),
                        record.get("fca_display"),
                    )
                    record.update(description)
                    description_total += 1
                    if description["description_reviewed"]:
                        described += 1
            payload["description_catalog"] = {
                "scope": "reporting_module_and_fca_display_code",
                "reviewed_records": described,
                "returned_records": description_total,
                "sources": (
                    "Reviewed short titles from the local 2022 ProMaster OEM "
                    "service information and repository-verified vehicle findings"
                ),
                "detail": (
                    "A DTC title explains the code definition; it is not a diagnosis "
                    "or proof of current vehicle state."
                ),
            }
            payload.setdefault(
                "detail",
                "Saved ReadDTCInformation results only; this endpoint cannot scan or clear.",
            )
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode()
            if len(encoded) >= MAX_RESPONSE_BYTES:
                return _unavailable(
                    "dtc_cache",
                    "saved DTC cache response exceeds the broker transport limit",
                )
            self._dtc_identity = identity
            self._dtc_payload = payload
            return json.loads(encoded)


__all__ = (
    "AdvisoryNotificationDispatcher",
    "AdvisoryNotificationSink",
    "DEFAULT_HISTORY_METRICS",
    "DisabledAdvisoryNotificationSink",
    "DTC_CACHE_SCHEMA_VERSION",
    "DtcCacheValidationError",
    "MAINTENANCE_CHECK_INTERVAL_SECONDS",
    "MAX_VAN_SCAN_CACHE_BYTES",
    "VAN_SCAN_CACHE_NAME",
    "MAX_DTC_CACHE_BYTES",
    "TelemetryInsights",
)
