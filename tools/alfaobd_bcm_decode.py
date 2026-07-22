#!/usr/bin/env python3
"""Decode AlfaOBD BCM catalog fields against existing read-only evidence.

The AlfaOBD catalog stores field positions relative to the complete positive UDS
response: bit 0 is the MSB of the ``0x62`` response SID and bit 24 is therefore
the MSB of the first DID data byte.  This tool applies that mechanical layout to
captured responses, but deliberately leaves request names, response names, enum
labels, and unit IDs in their raw catalog form.  A plausible string-table lookup
is not proof of a human meaning.

No CAN, ADB, application, or vehicle-configuration interface is opened.  SQLite
is opened with ``mode=ro`` and the default JSON report is written under ``tmp/``.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Iterable, Sequence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from projects.ecu_mapping.alfalog import iter_exchanges


DEFAULT_DATABASE = REPO / "tmp/ecu_mapping/android_tablet/alfaobd.db"
DEFAULT_DECODED = REPO / "tmp/ecu_mapping/promaster_2022_debug.decoded.txt"
DEFAULT_MODULE_MAP = (
    REPO / "projects/ecu_mapping/findings/promaster_2022/module_did_map.txt"
)
DEFAULT_INVENTORY_GLOB = str(
    REPO / "tmp/inventories/bcm_ccan/dids_*.results.jsonl"
)
DEFAULT_OUTPUT = REPO / "tmp/ecu_mapping/android_tablet/bcm55851_decode.json"

HEX_RE = re.compile(r"^[0-9A-F]+$")
REQUEST_RE = re.compile(r"^22([0-9A-F]{4})$")
MODULE_MAP_ROW_RE = re.compile(
    r"^\s+(22[0-9A-F]{4})\s+reads=(\d+)\s+resp=([0-9A-F]*)"
)

NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x72: "generalProgrammingFailure",
    0x78: "requestCorrectlyReceivedResponsePending",
}

CATEGORY_HINT_FAMILIES = {
    "positive": "positive",
    "timeout": "timeout",
    "transport_error": "transport_error",
    "response_pending": "pending",
    "service_not_supported": "negative",
    "subfunction_not_supported": "negative",
    "incorrect_length_or_format": "negative",
    "conditions_not_correct": "negative",
    "out_of_range_current_session": "negative",
    "security_denied": "negative",
    "session_restricted": "negative",
    "negative_other": "negative",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def compact_hex(value: str) -> str:
    """Return uppercase hexadecimal without whitespace, validating byte parity."""
    compact = re.sub(r"\s+", "", value or "").upper()
    if not compact:
        raise ValueError("empty response")
    if not HEX_RE.fullmatch(compact):
        raise ValueError("response contains non-hexadecimal characters")
    if len(compact) % 2:
        raise ValueError("response has an odd number of hexadecimal digits")
    return compact


def normalize_request(value: str) -> str:
    compact = re.sub(r"\s+", "", value or "").upper()
    if not REQUEST_RE.fullmatch(compact):
        raise ValueError(f"expected one 22xxxx request, got {value!r}")
    return compact


def _hex_prefix(value: str) -> str:
    """Best-effort compact prefix used only to classify malformed evidence."""
    return re.sub(r"\s+", "", value or "").upper()


def classify_response(
    request: str,
    response: str,
    *,
    prefix_only: bool = False,
    category_hint: str = "",
) -> dict[str, Any]:
    """Classify a response while requiring the exact positive DID echo.

    Malformed/truncated evidence can retain a positive or negative *family* for
    coverage comparison, but only a byte-complete ``category=positive`` response
    may be passed to the field decoder.
    """
    request = normalize_request(request)
    expected_positive = "62" + request[2:]
    raw_compact = _hex_prefix(response)
    hinted_family = CATEGORY_HINT_FAMILIES.get(category_hint)
    if not raw_compact and hinted_family in ("timeout", "transport_error"):
        return {
            "category": category_hint,
            "status_family": hinted_family,
            "response_hex": "",
        }
    try:
        clean = compact_hex(response)
    except ValueError as error:
        if raw_compact.startswith(expected_positive):
            return {
                "category": "positive_malformed",
                "status_family": "positive",
                "response_hex": raw_compact,
                "error": str(error),
            }
        if raw_compact.startswith("7F22"):
            return {
                "category": "negative_malformed",
                "status_family": "negative",
                "response_hex": raw_compact,
                "error": str(error),
            }
        return {
            "category": "malformed",
            "status_family": "invalid",
            "response_hex": raw_compact,
            "error": str(error),
        }

    if clean.startswith(expected_positive):
        category = "positive_prefix" if prefix_only else "positive"
        return {
            "category": category,
            "status_family": "positive",
            "response_hex": clean,
            "data_hex": clean[6:],
        }
    if clean.startswith("7F22") and len(clean) >= 6:
        nrc = int(clean[4:6], 16)
        if nrc == 0x78:
            return {
                "category": "pending_prefix" if prefix_only else "pending",
                "status_family": "pending",
                "response_hex": clean,
                "nrc": f"{nrc:02X}",
                "nrc_name": NRC_NAMES[nrc],
            }
        return {
            "category": "negative_prefix" if prefix_only else "negative",
            "status_family": "negative",
            "response_hex": clean,
            "nrc": f"{nrc:02X}",
            "nrc_name": NRC_NAMES.get(nrc, "unknown"),
        }
    return {
        "category": "echo_mismatch",
        "status_family": "invalid",
        "response_hex": clean,
        "expected_positive_prefix": expected_positive,
    }


def extract_bits(response: bytes | str, bit_pos: int, bit_len: int) -> int:
    """Extract an MSB0 bit field from the complete UDS response.

    ``bit_pos=0`` addresses the 0x80 bit of the first byte.  Fields spanning
    bytes are accumulated most-significant bit first.
    """
    if isinstance(response, str):
        payload = bytes.fromhex(compact_hex(response))
    else:
        payload = bytes(response)
    if isinstance(bit_pos, bool) or not isinstance(bit_pos, int) or bit_pos < 0:
        raise ValueError("bit_pos must be a non-negative integer")
    if isinstance(bit_len, bool) or not isinstance(bit_len, int) or bit_len <= 0:
        raise ValueError("bit_len must be a positive integer")
    if bit_pos + bit_len > len(payload) * 8:
        raise ValueError(
            f"field bits {bit_pos}..{bit_pos + bit_len - 1} exceed "
            f"{len(payload) * 8}-bit response"
        )
    value = 0
    for index in range(bit_pos, bit_pos + bit_len):
        byte = payload[index // 8]
        value = (value << 1) | ((byte >> (7 - index % 8)) & 1)
    return value


def parse_catalog_int(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text, 10)
        if re.fullmatch(r"[+-]?0[Xx][0-9A-Fa-f]+", text):
            return int(text, 0)
    except ValueError:
        pass
    return None


def parse_catalog_decimal(value: str) -> Decimal | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def decimal_text(value: Decimal) -> str:
    """Serialize a finite Decimal without exponent notation."""
    return format(value, "f")


def open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _unique(rows: Sequence[dict[str, Any]], key: str, *, nonempty: bool = False) -> list[str]:
    values: list[str] = []
    for row in rows:
        value = "" if row.get(key) is None else str(row[key])
        if nonempty and not value:
            continue
        if value not in values:
            values.append(value)
    return values


def _field_definition(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    first = rows[0]
    table_rows = [row for row in rows if str(row.get("table_value") or "") != ""]
    numeric_rows = [row for row in rows if str(row.get("slope") or "") != ""]
    encoding = "enum" if table_rows else "numeric" if numeric_rows else "raw"
    result: dict[str, Any] = {
        "bit_pos": int(first["bit_pos"]),
        "bit_len": int(first["bit_len"]),
        "response_name_raw": first["response_name"],
        "human_name_status": "unresolved_catalog_raw",
        "encoding": encoding,
        "catalog_rowids": [int(row["_rowid"]) for row in rows],
        "hex_hint_raw": _unique(rows, "hex", nonempty=True),
    }
    if encoding == "enum":
        result["enum_choices_raw"] = [
            {
                "table_value_raw": row["table_value"],
                "table_name_raw": row["table_name"],
                "catalog_rowid": int(row["_rowid"]),
            }
            for row in table_rows
        ]
    elif encoding == "numeric":
        # Current catalog fields have one numeric definition.  Preserve lists if
        # a future catalog contains conflicts rather than silently selecting one.
        result["numeric_catalog_raw"] = {
            key: _unique(numeric_rows, key)
            for key in ("lower_level", "upper_level", "slope", "offset", "unit")
        }
    return result


def load_catalog(database: Path, device_id: int = 55851) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load exact CSV-membership rows for one BCM subtype from SQLite read-only."""
    connection = open_read_only(database)
    try:
        table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='FGA_BCM_DATA'"
        ).fetchone()
        if table is None:
            raise ValueError("database has no FGA_BCM_DATA table")
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT rowid AS _rowid, * FROM FGA_BCM_DATA "
                "WHERE instr(',' || replace(device_id, ' ', '') || ',', ',' || ? || ',') > 0 "
                "ORDER BY request, CAST(bit_pos AS INTEGER), CAST(bit_len AS INTEGER), rowid",
                (str(device_id),),
            )
        ]
        version_row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ver'"
        ).fetchone()
        version = (
            dict(connection.execute("SELECT * FROM ver LIMIT 1").fetchone())
            if version_row is not None
            else None
        )
    finally:
        connection.close()

    by_request: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        request = normalize_request(str(row["request"]))
        try:
            bit_pos = int(row["bit_pos"])
            bit_len = int(row["bit_len"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid field position in catalog row {row['_rowid']}") from error
        if bit_pos < 0 or bit_len <= 0:
            raise ValueError(f"invalid field position in catalog row {row['_rowid']}")
        by_request[request].append(row)

    requests: dict[str, Any] = {}
    for request, request_rows in sorted(by_request.items()):
        field_rows: dict[tuple[int, int, str], list[dict[str, Any]]] = defaultdict(list)
        for row in request_rows:
            key = (int(row["bit_pos"]), int(row["bit_len"]), row["response_name"])
            field_rows[key].append(row)
        fields = [
            _field_definition(group)
            for _, group in sorted(field_rows.items(), key=lambda item: item[0])
        ]
        requests[request] = {
            "request": request,
            "did": request[2:],
            "request_name_raw": _unique(request_rows, "request_name"),
            "human_name_status": "unresolved_catalog_raw",
            "catalog_row_count": len(request_rows),
            "field_count": len(fields),
            "fields": fields,
        }
    return requests, {"database_version": version, "catalog_row_count": len(rows)}


@dataclass(frozen=True)
class Evidence:
    request: str
    response: str
    source_kind: str
    source_path: str
    complete: bool = True
    date: str = ""
    timestamp: str = ""
    category_hint: str = ""
    occurrences: int = 1
    request_occurrences_total: int | None = None
    summary_path: str = ""
    module_key: str = ""
    module_txid: str = ""
    module_rxid: str = ""
    module_bus: str = ""
    module_bitrate: int | None = None
    module_channel: str = ""
    addressing_mode: str = ""
    physical_pair: str = ""
    requested_session: str = ""
    session_state: str = ""
    diagnostic_session_policy: str = ""
    conditions: str = ""
    campaign_status: str = ""
    campaign_partial: bool | None = None
    campaign_fatal_error: str = ""
    restored_passive: bool | None = None
    started_at: str = ""
    completed_at: str = ""


def load_decoded_evidence(path: Path, addr: str = "DA40F1") -> list[Evidence]:
    target = addr.upper().replace("0X", "")
    result = []
    for exchange in iter_exchanges(path):
        try:
            request = normalize_request(exchange["req"])
        except ValueError:
            continue
        if str(exchange["addr"]).upper() != target:
            continue
        result.append(
            Evidence(
                request=request,
                response=exchange["resp"],
                source_kind="decoded_trace",
                source_path=str(path),
                date=exchange.get("date") or "",
                timestamp=exchange.get("ts") or "",
            )
        )
    return result


def _canonical_can_id(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("boolean is not a CAN ID")
    if isinstance(value, int):
        number = value
    else:
        text = re.sub(r"[\s_]", "", str(value or "")).upper()
        if text.startswith("0X"):
            text = text[2:]
        if not text or not HEX_RE.fullmatch(text):
            raise ValueError(f"invalid CAN ID {value!r}")
        number = int(text, 16)
    if not 0 <= number <= 0x1FFFFFFF:
        raise ValueError(f"CAN ID outside 29-bit range: {value!r}")
    return f"{number:X}"


def _normal_fixed_endpoint(addr: str) -> tuple[str, str]:
    header = _canonical_can_id(addr)
    if len(header) == 6 and header.startswith("DA"):
        header = "18" + header
    if len(header) != 8 or not header.startswith("18DA"):
        raise ValueError(
            f"inventory provenance verification requires an 18DA normal-fixed header, got {addr!r}"
        )
    txid = int(header, 16)
    target = (txid >> 8) & 0xFF
    source = txid & 0xFF
    rxid = 0x18DA0000 | (source << 8) | target
    return f"{txid:08X}", f"{rxid:08X}"


def _inventory_summary_path(path: Path) -> Path:
    suffix = ".results.jsonl"
    if not path.name.endswith(suffix):
        raise ValueError(f"inventory filename must end in {suffix}: {path}")
    return path.with_name(path.name[: -len(suffix)] + ".summary.json")


def _load_inventory_context(
    path: Path,
    *,
    expected_txid: str,
    expected_rxid: str,
    expected_module_key: str,
    expected_bus: str,
    expected_bitrate: int,
) -> tuple[dict[str, Any], Path]:
    summary_path = _inventory_summary_path(path)
    if not summary_path.is_file():
        raise ValueError(f"inventory has no paired summary: {summary_path}")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid inventory summary JSON: {summary_path}") from error
    module = summary.get("module")
    if not isinstance(module, dict):
        raise ValueError(f"inventory summary has no module object: {summary_path}")
    module_key = str(module.get("key") or "")
    if module_key != expected_module_key:
        raise ValueError(
            f"inventory module mismatch in {summary_path}: "
            f"{module_key!r} != {expected_module_key!r}"
        )
    actual_txid = _canonical_can_id(module.get("txid"))
    actual_rxid = _canonical_can_id(module.get("rxid"))
    if actual_txid != _canonical_can_id(expected_txid) or actual_rxid != _canonical_can_id(
        expected_rxid
    ):
        raise ValueError(
            f"inventory endpoint mismatch in {summary_path}: "
            f"{actual_txid}:{actual_rxid} != {expected_txid}:{expected_rxid}"
        )
    addressing_mode = str(module.get("addressing_mode") or "")
    if addressing_mode != "normal_29bits":
        raise ValueError(
            f"inventory addressing mismatch in {summary_path}: {addressing_mode!r}"
        )
    module_bus = str(module.get("bus") or "")
    if module_bus != expected_bus:
        raise ValueError(
            f"inventory bus mismatch in {summary_path}: "
            f"{module_bus!r} != {expected_bus!r}"
        )
    try:
        module_bitrate = int(module.get("bitrate"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid inventory bitrate in {summary_path}") from error
    if module_bitrate != expected_bitrate:
        raise ValueError(
            f"inventory bitrate mismatch in {summary_path}: "
            f"{module_bitrate} != {expected_bitrate}"
        )
    declared_results = summary.get("results_jsonl")
    if declared_results and Path(str(declared_results)).name != path.name:
        raise ValueError(
            f"inventory summary names a different results file: {declared_results!r}"
        )
    requested_session = summary.get("requested_session")
    context = {
        "summary_path": str(summary_path),
        "module_key": module_key,
        "module_txid": actual_txid,
        "module_rxid": actual_rxid,
        "module_bus": module_bus,
        "module_bitrate": module_bitrate,
        "module_channel": str(module.get("channel") or summary.get("channel") or ""),
        "addressing_mode": addressing_mode,
        "physical_pair": str(summary.get("physical_pair") or ""),
        "conditions": str(summary.get("conditions") or ""),
        "requested_session": "" if requested_session is None else str(requested_session),
        "session_state": str(summary.get("session_state") or ""),
        "diagnostic_session_policy": str(
            summary.get("diagnostic_session_policy") or ""
        ),
        "started_at": str(summary.get("started_at") or ""),
        "completed_at": str(summary.get("completed_at") or ""),
        "campaign_status": str(summary.get("status") or ""),
        "campaign_partial": summary.get("partial"),
        "campaign_fatal_error": str(summary.get("fatal_error") or ""),
        "restored_passive": summary.get("restored_passive"),
        "results_written": summary.get("results_written"),
    }
    return context, summary_path


def load_inventory_evidence(
    paths: Iterable[Path],
    *,
    expected_txid: str,
    expected_rxid: str,
    expected_module_key: str = "bcm_ccan",
    expected_bus: str = "c-can",
    expected_bitrate: int = 500000,
) -> tuple[list[Evidence], list[Path], dict[str, Any]]:
    result = []
    summary_paths = []
    skipped_invalid_requests = []
    category_hint_mismatches = []
    campaign_contexts = []
    for path in paths:
        context, summary_path = _load_inventory_context(
            path,
            expected_txid=expected_txid,
            expected_rxid=expected_rxid,
            expected_module_key=expected_module_key,
            expected_bus=expected_bus,
            expected_bitrate=expected_bitrate,
        )
        summary_paths.append(summary_path)
        campaign_contexts.append(
            {key: value for key, value in context.items() if key != "results_written"}
        )
        rows_read = 0
        with path.open("r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                rows_read += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"{path}:{line_number}: invalid JSON") from error
                request_value = row.get("request_hex") or (
                    "22" + str(row.get("did", ""))
                )
                try:
                    request = normalize_request(request_value)
                except ValueError as error:
                    skipped_invalid_requests.append(
                        {"path": str(path), "line": line_number, "error": str(error)}
                    )
                    continue
                response = str(row.get("response_hex") or "")
                hint = str(row.get("category") or "")
                classified = classify_response(
                    request, response, category_hint=hint
                )
                expected_family = CATEGORY_HINT_FAMILIES.get(hint)
                if expected_family and expected_family != classified["status_family"]:
                    category_hint_mismatches.append(
                        {
                            "path": str(path),
                            "line": line_number,
                            "request": request,
                            "category_hint": hint,
                            "classified_family": classified["status_family"],
                        }
                    )
                result.append(
                    Evidence(
                        request=request,
                        response=response,
                        source_kind="inventory",
                        source_path=str(path),
                        category_hint=hint,
                        summary_path=context["summary_path"],
                        module_key=context["module_key"],
                        module_txid=context["module_txid"],
                        module_rxid=context["module_rxid"],
                        module_bus=context["module_bus"],
                        module_bitrate=context["module_bitrate"],
                        module_channel=context["module_channel"],
                        addressing_mode=context["addressing_mode"],
                        physical_pair=context["physical_pair"],
                        requested_session=context["requested_session"],
                        session_state=context["session_state"],
                        diagnostic_session_policy=context[
                            "diagnostic_session_policy"
                        ],
                        conditions=context["conditions"],
                        campaign_status=context["campaign_status"],
                        campaign_partial=context["campaign_partial"],
                        campaign_fatal_error=context["campaign_fatal_error"],
                        restored_passive=context["restored_passive"],
                        started_at=context["started_at"],
                        completed_at=context["completed_at"],
                    )
                )
        declared_count = context["results_written"]
        if declared_count is not None and int(declared_count) != rows_read:
            raise ValueError(
                f"inventory row-count mismatch for {path}: "
                f"summary={declared_count}, actual={rows_read}"
            )
    return result, summary_paths, {
        "files_loaded": len(summary_paths),
        "evidence_rows_loaded": len(result),
        "skipped_invalid_request_count": len(skipped_invalid_requests),
        "skipped_invalid_requests": skipped_invalid_requests,
        "category_hint_mismatch_count": len(category_hint_mismatches),
        "category_hint_mismatches": category_hint_mismatches,
        "campaign_contexts": campaign_contexts,
        "noncomplete_or_failed_campaign_count": sum(
            context["campaign_status"] not in ("", "complete")
            or bool(context["campaign_partial"])
            or bool(context["campaign_fatal_error"])
            or context["restored_passive"] is False
            for context in campaign_contexts
        ),
    }


def load_module_map_evidence(path: Path, addr: str = "DA40F1") -> list[Evidence]:
    """Read response prefixes from the tracked derived map for cross-checking only."""
    target = addr.upper().replace("0X", "")
    active = False
    result = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if line.startswith("## ATSH "):
                parts = line.split()
                active = len(parts) >= 3 and parts[2].upper() == target
                continue
            if active and line.startswith("## ATSH "):
                active = False
            if not active:
                continue
            match = MODULE_MAP_ROW_RE.match(line)
            if not match:
                continue
            request, occurrences, response = match.groups()
            result.append(
                Evidence(
                    request=request,
                    response=response,
                    source_kind="module_map",
                    source_path=str(path),
                    complete=False,
                    occurrences=1,
                    request_occurrences_total=int(occurrences),
                )
            )
    return result


def _single_raw_value(values: Sequence[str]) -> str | None:
    nonempty = [value for value in values if value != ""]
    return nonempty[0] if len(set(nonempty)) == 1 else None


def decode_field(field: dict[str, Any], response_hex: str) -> dict[str, Any]:
    bit_pos = field["bit_pos"]
    bit_len = field["bit_len"]
    result: dict[str, Any] = {
        "bit_pos": bit_pos,
        "bit_len": bit_len,
        "response_name_raw": field["response_name_raw"],
        "human_name_status": "unresolved_catalog_raw",
        "encoding": field["encoding"],
        "catalog_rowids": field["catalog_rowids"],
    }
    try:
        raw_value = extract_bits(response_hex, bit_pos, bit_len)
    except ValueError as error:
        result.update({"decode_status": "out_of_bounds", "error": str(error)})
        return result

    result.update(
        {
            "decode_status": "decoded_raw",
            "raw_value": raw_value,
            "raw_hex": f"{raw_value:0{(bit_len + 3) // 4}X}",
        }
    )
    if field["encoding"] == "enum":
        choices = field["enum_choices_raw"]
        matches = [
            choice
            for choice in choices
            if parse_catalog_int(choice["table_value_raw"]) == raw_value
        ]
        result.update(
            {
                "enum_match_status": (
                    "matched_raw_catalog_entry" if matches else "unmapped_raw_value"
                ),
                "enum_matches_raw": matches,
                "enum_choice_count": len(choices),
            }
        )
    elif field["encoding"] == "numeric":
        catalog = field["numeric_catalog_raw"]
        slope_raw = _single_raw_value(catalog["slope"])
        offset_raw = _single_raw_value(catalog["offset"])
        slope = parse_catalog_decimal(slope_raw or "")
        offset = parse_catalog_decimal(offset_raw or "")
        numeric: dict[str, Any] = {
            "catalog_raw": catalog,
            "formula": "raw_value * slope + offset",
            "physical_semantics_status": "unverified_vendor_catalog",
        }
        if slope is None or offset is None:
            numeric["scaling_status"] = "invalid_or_conflicting_catalog_number"
        else:
            numeric.update(
                {
                    "scaling_status": "applied_catalog_arithmetic",
                    "scaled_value_decimal": decimal_text(Decimal(raw_value) * slope + offset),
                }
            )
        lower_raw = _single_raw_value(catalog["lower_level"])
        upper_raw = _single_raw_value(catalog["upper_level"])
        lower = parse_catalog_int(lower_raw or "")
        upper = parse_catalog_int(upper_raw or "")
        if lower is None or upper is None:
            numeric["bounds_status"] = "missing_or_invalid_catalog_bounds"
        elif upper < lower:
            numeric["bounds_status"] = "ambiguous_catalog_bounds"
        else:
            numeric.update(
                {
                    "bounds_status": "checked_raw_value",
                    "raw_within_catalog_bounds": lower <= raw_value <= upper,
                }
            )
        result["numeric"] = numeric
    else:
        result["hex_hint_raw"] = field.get("hex_hint_raw", [])
    return result


def decode_positive(definition: dict[str, Any], response_hex: str) -> dict[str, Any]:
    classified = classify_response(definition["request"], response_hex)
    if classified["category"] != "positive":
        raise ValueError("field decoding requires a complete positive response with exact DID echo")
    fields = [decode_field(field, classified["response_hex"]) for field in definition["fields"]]
    decoded_count = sum(field["decode_status"] == "decoded_raw" for field in fields)
    return {
        "response_hex": classified["response_hex"],
        "data_hex": classified["data_hex"],
        "response_byte_count": len(classified["response_hex"]) // 2,
        "field_count": len(fields),
        "decoded_field_count": decoded_count,
        "out_of_bounds_field_count": len(fields) - decoded_count,
        "fields": fields,
    }


def _source_summary(evidence: Sequence[Evidence], catalog_requests: set[str]) -> dict[str, Any]:
    by_kind: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    sample_counts: dict[str, int] = defaultdict(int)
    request_occurrence_counts: dict[str, int] = defaultdict(int)
    for item in evidence:
        if item.request not in catalog_requests:
            continue
        classified = classify_response(
            item.request,
            item.response,
            prefix_only=not item.complete,
            category_hint=item.category_hint,
        )
        by_kind[item.source_kind][item.request].add(classified["status_family"])
        sample_counts[item.source_kind] += item.occurrences
        request_occurrence_counts[item.source_kind] += (
            item.request_occurrences_total
            if item.request_occurrences_total is not None
            else item.occurrences
        )
    result = {}
    for kind, requests in sorted(by_kind.items()):
        families = defaultdict(int)
        mixed = 0
        for statuses in requests.values():
            for status in statuses:
                families[status] += 1
            if len(statuses) > 1:
                mixed += 1
        result[kind] = {
            "catalog_requests_observed": len(requests),
            "positive_requests": families["positive"],
            "negative_requests": families["negative"],
            "pending_requests": families["pending"],
            "timeout_requests": families["timeout"],
            "transport_error_requests": families["transport_error"],
            "invalid_requests": families["invalid"],
            "mixed_status_requests": mixed,
            "evidence_sample_count": sample_counts[kind],
            "request_occurrences_total": request_occurrence_counts[kind],
            "occurrence_note": (
                "module_map request totals aggregate all variants; each displayed prefix "
                "is counted only as one evidence sample"
                if kind == "module_map"
                else "samples are complete source records"
            ),
        }
    return result


def _observations_for_request(request: str, evidence: Sequence[Evidence]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
    for item in evidence:
        if item.request != request:
            continue
        classified = classify_response(
            request,
            item.response,
            prefix_only=not item.complete,
            category_hint=item.category_hint,
        )
        key = (
            classified.get("response_hex", ""),
            classified["category"],
            item.source_kind,
            item.complete,
        )
        record = grouped.setdefault(
            key,
            {
                **classified,
                "source_kind": item.source_kind,
                "source_paths": [],
                "complete": item.complete,
                "sample_occurrences": 0,
                "request_occurrences_total": 0,
                "category_hints": [],
                "timestamps": [],
                "provenance": [],
            },
        )
        record["sample_occurrences"] += item.occurrences
        record["request_occurrences_total"] += (
            item.request_occurrences_total
            if item.request_occurrences_total is not None
            else item.occurrences
        )
        if item.source_path not in record["source_paths"]:
            record["source_paths"].append(item.source_path)
        if item.category_hint and item.category_hint not in record["category_hints"]:
            record["category_hints"].append(item.category_hint)
        stamp = " ".join(part for part in (item.date, item.timestamp) if part)
        if stamp and stamp not in record["timestamps"]:
            record["timestamps"].append(stamp)
        if item.summary_path:
            provenance = {
                "results_path": item.source_path,
                "summary_path": item.summary_path,
                "module_key": item.module_key,
                "module_txid": item.module_txid,
                "module_rxid": item.module_rxid,
                "module_bus": item.module_bus,
                "module_bitrate": item.module_bitrate,
                "module_channel": item.module_channel,
                "addressing_mode": item.addressing_mode,
                "physical_pair": item.physical_pair,
                "requested_session": item.requested_session or None,
                "session_state": item.session_state,
                "diagnostic_session_policy": item.diagnostic_session_policy,
                "conditions": item.conditions,
                "campaign_status": item.campaign_status,
                "campaign_partial": item.campaign_partial,
                "campaign_fatal_error": item.campaign_fatal_error or None,
                "restored_passive": item.restored_passive,
                "started_at": item.started_at,
                "completed_at": item.completed_at,
            }
            if provenance not in record["provenance"]:
                record["provenance"].append(provenance)
    return sorted(
        grouped.values(),
        key=lambda row: (row["source_kind"], row.get("response_hex", ""), row["category"]),
    )


def build_report(
    database: Path,
    *,
    device_id: int = 55851,
    decoded: Path | None = None,
    inventories: Sequence[Path] = (),
    module_map: Path | None = None,
    addr: str = "DA40F1",
) -> dict[str, Any]:
    definitions, catalog_meta = load_catalog(database, device_id)
    evidence: list[Evidence] = []
    inventory_audit: dict[str, Any] = {
        "files_loaded": 0,
        "evidence_rows_loaded": 0,
        "skipped_invalid_request_count": 0,
        "skipped_invalid_requests": [],
        "category_hint_mismatch_count": 0,
        "category_hint_mismatches": [],
        "campaign_contexts": [],
        "noncomplete_or_failed_campaign_count": 0,
    }
    source_files: list[tuple[str, Path]] = [("database", database)]
    if decoded is not None:
        evidence.extend(load_decoded_evidence(decoded, addr))
        source_files.append(("decoded_trace", decoded))
    if inventories:
        expected_txid, expected_rxid = _normal_fixed_endpoint(addr)
        inventory_evidence, inventory_summaries, inventory_audit = load_inventory_evidence(
            inventories, expected_txid=expected_txid, expected_rxid=expected_rxid
        )
        evidence.extend(inventory_evidence)
        source_files.extend(("inventory_results", path) for path in inventories)
        source_files.extend(("inventory_summary", path) for path in inventory_summaries)
    if module_map is not None:
        evidence.extend(load_module_map_evidence(module_map, addr))
        source_files.append(("module_map", module_map))

    catalog_requests = set(definitions)
    requests_report: dict[str, Any] = {}
    total_decoded_variants = 0
    total_decoded_fields = 0
    total_out_of_bounds_fields = 0
    invalid_numeric_fields: set[tuple[str, int, int, str]] = set()
    ambiguous_bounds_fields: set[tuple[str, int, int, str]] = set()
    for request, definition in definitions.items():
        observations = _observations_for_request(request, evidence)
        positive_responses: dict[str, set[str]] = defaultdict(set)
        for observation in observations:
            if observation["category"] != "positive" or not observation["complete"]:
                continue
            for source_path in observation["source_paths"]:
                positive_responses[observation["response_hex"]].add(source_path)
        decodes = []
        for response_hex, source_paths in sorted(positive_responses.items()):
            decoded_variant = decode_positive(definition, response_hex)
            decoded_variant["source_paths"] = sorted(source_paths)
            decodes.append(decoded_variant)
            total_decoded_variants += 1
            total_decoded_fields += decoded_variant["decoded_field_count"]
            total_out_of_bounds_fields += decoded_variant["out_of_bounds_field_count"]
            for field in decoded_variant["fields"]:
                numeric = field.get("numeric")
                if numeric and numeric["scaling_status"] != "applied_catalog_arithmetic":
                    invalid_numeric_fields.add(
                        (
                            request,
                            field["bit_pos"],
                            field["bit_len"],
                            field["response_name_raw"],
                        )
                    )
                if numeric and numeric["bounds_status"] == "ambiguous_catalog_bounds":
                    ambiguous_bounds_fields.add(
                        (
                            request,
                            field["bit_pos"],
                            field["bit_len"],
                            field["response_name_raw"],
                        )
                    )
        requests_report[request] = {
            **{key: value for key, value in definition.items() if key != "fields"},
            "field_definitions": definition["fields"],
            "observations": observations,
            "decoded_positive_variants": decodes,
        }

    source_records = []
    seen_sources: set[tuple[str, str]] = set()
    for kind, path in source_files:
        key = (kind, str(path))
        if key in seen_sources:
            continue
        seen_sources.add(key)
        source_records.append(
            {
                "kind": kind,
                "path": str(path),
                "sha256": sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )

    field_definitions = [field for definition in definitions.values() for field in definition["fields"]]
    encoding_counts = defaultdict(int)
    for field in field_definitions:
        encoding_counts[field["encoding"]] += 1
    return {
        "format": "alfaobd-bcm-catalog-decode-v1",
        "scope": {
            "device_id": device_id,
            "uds_address_header": addr.upper(),
            "database_open_mode": "read_only",
            "vehicle_io": "none",
            "human_labels": "unresolved_raw_catalog_strings_only",
            "bit_numbering": (
                "MSB0 over complete UDS response; bit 24 is first DID data-byte MSB"
            ),
            "numeric_formula": "raw_value * slope + offset",
            "physical_name_unit_scaling_status": "not independently validated",
        },
        "sources": source_records,
        "catalog": {
            **catalog_meta,
            "device_id": device_id,
            "request_count": len(definitions),
            "field_count": len(field_definitions),
            "field_encoding_counts": dict(sorted(encoding_counts.items())),
        },
        "evidence_summary": _source_summary(evidence, catalog_requests),
        "inventory_input_audit": inventory_audit,
        "decode_summary": {
            "complete_positive_response_variants": total_decoded_variants,
            "decoded_field_instances": total_decoded_fields,
            "out_of_bounds_field_instances": total_out_of_bounds_fields,
            "invalid_numeric_catalog_field_count": len(invalid_numeric_fields),
            "invalid_numeric_catalog_fields": [
                {
                    "request": item[0],
                    "bit_pos": item[1],
                    "bit_len": item[2],
                    "response_name_raw": item[3],
                }
                for item in sorted(invalid_numeric_fields)
            ],
            "ambiguous_numeric_bounds_field_count": len(ambiguous_bounds_fields),
            "ambiguous_numeric_bounds_fields": [
                {
                    "request": item[0],
                    "bit_pos": item[1],
                    "bit_len": item[2],
                    "response_name_raw": item[3],
                }
                for item in sorted(ambiguous_bounds_fields)
            ],
        },
        "requests": requests_report,
    }


def write_json_atomic(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{output.name}.",
            dir=output.parent,
            delete=False,
        ) as destination:
            temporary_name = destination.name
            json.dump(report, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--device-id", type=int, default=55851)
    parser.add_argument("--decoded", type=Path, default=DEFAULT_DECODED)
    parser.add_argument("--module-map", type=Path, default=DEFAULT_MODULE_MAP)
    parser.add_argument("--addr", default="DA40F1")
    parser.add_argument(
        "--inventory",
        type=Path,
        action="append",
        help="inventory JSONL (repeatable; defaults to tmp/inventories/bcm_ccan/dids_*.results.jsonl)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    inventories = args.inventory
    if inventories is None:
        inventories = [Path(path) for path in sorted(glob.glob(DEFAULT_INVENTORY_GLOB))]
    for path in (args.database, args.decoded, args.module_map, *inventories):
        if not path.is_file():
            raise SystemExit(f"required input does not exist: {path}")
    report = build_report(
        args.database,
        device_id=args.device_id,
        decoded=args.decoded,
        inventories=inventories,
        module_map=args.module_map,
        addr=args.addr,
    )
    write_json_atomic(args.output, report)
    print(f"wrote {args.output}")
    print(
        f"catalog: {report['catalog']['request_count']} requests, "
        f"{report['catalog']['field_count']} fields"
    )
    for kind, summary in report["evidence_summary"].items():
        print(
            f"{kind}: observed={summary['catalog_requests_observed']} "
            f"positive={summary['positive_requests']} "
            f"negative={summary['negative_requests']} "
            f"pending={summary['pending_requests']} "
            f"timeout={summary['timeout_requests']} "
            f"transport={summary['transport_error_requests']} "
            f"invalid={summary['invalid_requests']} "
            f"mixed={summary['mixed_status_requests']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
