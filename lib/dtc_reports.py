"""Offline saved DTC inventory loading and semantic report identity."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Any

from lib.dtc import DtcParseError, scan_from_inventory_report


def load_inventory(path: str) -> tuple[Any, dict[str, Any]]:
    report_path = Path(path)
    raw = report_path.read_bytes()
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DtcParseError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise DtcParseError(f"{path}: report root must be an object")
    digest = hashlib.sha256(raw).hexdigest()
    # Validate first so the identity is derived from the same normalized timestamps, payload
    # bytes, outcome, and registry metadata that will actually be persisted.  This keeps JSON
    # formatting, equivalent timezone offsets, and hex spelling from creating duplicate scans.
    scan = scan_from_inventory_report(
        report,
        source_key="dtc-inventory-v2:validation-pending",
        source_ref=str(report_path),
    )
    semantic_identity = {
        "identity_version": 2,
        "tool": "tools/dtc_inventory.py",
        "module": {
            "key": scan.module_key,
            "bus": scan.logical_bus,
            "bitrate": scan.bitrate,
        },
        "started_at": scan.started_at,
        "completed_at": scan.completed_at,
        "outcome": scan.outcome,
        "unavailable_reason": scan.unavailable_reason,
        "status_availability_mask": scan.status_availability_mask,
        "request_hex": scan.request_hex,
        "response_hex": scan.response_hex,
        "dtcs": [
            {"raw_dtc": record.raw_dtc, "status": record.status} for record in scan.dtcs
        ],
    }
    semantic_bytes = json.dumps(
        semantic_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    semantic_digest = hashlib.sha256(semantic_bytes).hexdigest()
    scan = replace(
        scan,
        source_key=f"dtc-inventory-v2:{semantic_digest}",
    )
    preview = {
        "source_ref": str(report_path),
        "source_sha256": digest,
        "source_key": scan.source_key,
        "module_key": scan.module_key,
        "logical_bus": scan.logical_bus,
        "resolved_channel": scan.resolved_channel,
        "completed_at": scan.completed_at,
        "outcome": scan.outcome,
        "unavailable_reason": scan.unavailable_reason,
        "dtc_count": len(scan.dtcs) if scan.outcome == "success" else None,
        "explicit_zero_dtcs": scan.outcome == "success" and not scan.dtcs,
    }
    return scan, preview
