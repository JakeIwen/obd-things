"""UDS ReadDTCInformation parsing and cache-only DTC history.

This module deliberately has no SocketCAN imports and cannot transmit.  It provides the
service-0x19 parsing shared by inventory tools plus a small SQLite historian suitable for a
cache-only consumer.  A successful ``19 02 FF`` response, including an empty response, is the
only observation that can resolve a previously present DTC.  Timeouts and other unavailable
results preserve the last successful state.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as _datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence


READ_DTC_BY_STATUS_REQUEST = bytes.fromhex("19 02 FF")
SCHEMA_VERSION = 2

STATUS_BITS = (
    (0x01, "test_failed"),
    (0x02, "test_failed_this_operation_cycle"),
    (0x04, "pending"),
    (0x08, "confirmed"),
    (0x10, "test_not_completed_since_last_clear"),
    (0x20, "test_failed_since_last_clear"),
    (0x40, "test_not_completed_this_operation_cycle"),
    (0x80, "warning_indicator_requested"),
)


class DtcParseError(ValueError):
    """A service-0x19 response or imported inventory report is malformed."""


def _require_byte(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be an integer byte")
    return value


def decode_status(status: int) -> list[str]:
    """Return the ISO 14229 status-bit names set in *status*."""

    status = _require_byte(status, "status")
    return [name for mask, name in STATUS_BITS if status & mask]


def status_semantics(status: int) -> dict[str, Any]:
    """Return presentation-safe semantics without equating history/incomplete bits to a fault.

    ``display_group`` is intentionally conservative.  The independent boolean fields and complete
    flag list remain available so callers do not need to infer meaning from that grouping.
    """

    status = _require_byte(status, "status")
    current = bool(status & 0x01)
    pending = bool(status & 0x04)
    confirmed = bool(status & 0x08)
    warning = bool(status & 0x80)
    incomplete_mask = 0x10 | 0x40
    incomplete_only = bool(status) and not bool(status & ~incomplete_mask)
    if current:
        display_group = "current"
    elif pending:
        display_group = "pending"
    elif confirmed:
        display_group = "confirmed_history"
    elif warning:
        display_group = "warning_requested"
    elif incomplete_only:
        display_group = "incomplete_only"
    elif status:
        display_group = "other_history"
    else:
        display_group = "no_status_bits"
    return {
        "status": f"{status:02X}",
        "status_flags": decode_status(status),
        "display_group": display_group,
        "current": current,
        "pending": pending,
        "confirmed": confirmed,
        "warning_indicator_requested": warning,
        "incomplete_only": incomplete_only,
    }


def fca_dtc_name(dtc_bytes: bytes | bytearray | memoryview) -> str:
    """Render FCA's familiar ``P/C/B/Uxxxx-yy`` view of one three-byte DTC."""

    raw = bytes(dtc_bytes)
    if len(raw) != 3:
        raise ValueError("a DTC must contain exactly three bytes")
    first, second, failure_type = raw
    letter = "PCBU"[(first >> 6) & 0x03]
    code = ((first & 0x3F) << 8) | second
    return f"{letter}{code:04X}-{failure_type:02X}"


@dataclass(frozen=True)
class DtcRecord:
    raw_dtc: str
    status: int

    def __post_init__(self) -> None:
        normalized = self.raw_dtc.replace(" ", "").upper()
        try:
            raw = bytes.fromhex(normalized)
        except ValueError as exc:
            raise ValueError("raw_dtc must be six hexadecimal digits") from exc
        if len(raw) != 3:
            raise ValueError("raw_dtc must be six hexadecimal digits")
        _require_byte(self.status, "status")
        object.__setattr__(self, "raw_dtc", normalized)

    @property
    def fca_display(self) -> str:
        return fca_dtc_name(bytes.fromhex(self.raw_dtc))

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_dtc": self.raw_dtc,
            "fca_display": self.fca_display,
            **status_semantics(self.status),
        }


def parse_dtc_records(body: bytes | bytearray | memoryview) -> tuple[list[dict[str, Any]], bytes]:
    """Compatibility parser for four-byte DTC/status records.

    Complete records are returned as dictionaries; any incomplete suffix is returned separately.
    Strict callers should reject a non-empty suffix.
    """

    body = bytes(body)
    record_count = len(body) // 4
    records = []
    for index in range(record_count):
        offset = index * 4
        record = DtcRecord(body[offset : offset + 3].hex(), body[offset + 3])
        records.append(record.as_dict())
    return records, body[record_count * 4 :]


def parse_snapshot_identifiers(
    body: bytes | bytearray | memoryview,
) -> tuple[list[dict[str, Any]], bytes]:
    body = bytes(body)
    record_count = len(body) // 4
    records = []
    for index in range(record_count):
        offset = index * 4
        dtc = body[offset : offset + 3]
        records.append(
            {
                "raw_dtc": dtc.hex().upper(),
                "fca_display": fca_dtc_name(dtc),
                "snapshot_record": f"{body[offset + 3]:02X}",
            }
        )
    return records, body[record_count * 4 :]


def parse_positive_response(
    request: bytes | bytearray | memoryview,
    response: bytes | bytearray | memoryview,
) -> dict[str, Any]:
    """Parse the bounded ReadDTCInformation subfunctions used by ``dtc_inventory.py``.

    Parse problems are returned in the historical tool-compatible ``parse_error`` form.  New strict
    consumers should use :func:`parse_dtc_list_response` for ``19 02`` results.
    """

    request = bytes(request)
    response = bytes(response)
    if len(request) < 2:
        return {"parse_error": "short ReadDTCInformation request"}
    subfunction = request[1]
    if len(response) < 2 or response[:2] != bytes((0x59, subfunction)):
        return {"parse_error": "positive SID/subfunction echo mismatch"}
    if subfunction == 0x01:
        if len(response) < 6:
            return {"parse_error": "short reportNumberOfDTCByStatusMask response"}
        return {
            "status_availability_mask": f"{response[2]:02X}",
            "dtc_format_identifier": f"{response[3]:02X}",
            "dtc_count": int.from_bytes(response[4:6], "big"),
            "trailing_hex": response[6:].hex(" ").upper() if len(response) > 6 else None,
        }
    if subfunction in (0x02, 0x0A):
        if len(response) < 3:
            return {"parse_error": "short DTC-list response"}
        records, trailing = parse_dtc_records(response[3:])
        return {
            "status_availability_mask": f"{response[2]:02X}",
            "dtcs": records,
            "trailing_hex": trailing.hex(" ").upper() if trailing else None,
        }
    if subfunction == 0x03:
        records, trailing = parse_snapshot_identifiers(response[2:])
        return {
            "snapshots": records,
            "trailing_hex": trailing.hex(" ").upper() if trailing else None,
        }
    return {"parse_error": "unsupported local parser subfunction"}


def parse_dtc_list_response(
    response: bytes | bytearray | memoryview,
    *,
    expected_subfunction: int = 0x02,
) -> tuple[int, tuple[DtcRecord, ...]]:
    """Strictly parse one positive ``19 02``/``19 0A`` response.

    A three-byte ``59 02 <availability-mask>`` response is a successful zero-DTC observation.
    """

    expected_subfunction = _require_byte(expected_subfunction, "expected_subfunction")
    response = bytes(response)
    if len(response) < 3:
        raise DtcParseError("short DTC-list response")
    expected = bytes((0x59, expected_subfunction))
    if response[:2] != expected:
        raise DtcParseError(
            f"expected positive response {expected.hex(' ').upper()}, got "
            f"{response[:2].hex(' ').upper() or '<empty>'}"
        )
    body = response[3:]
    if len(body) % 4:
        raise DtcParseError("DTC-list response has an incomplete four-byte record")
    wire_records = tuple(
        DtcRecord(body[offset : offset + 3].hex(), body[offset + 3])
        for offset in range(0, len(body), 4)
    )
    records_by_code: dict[str, DtcRecord] = {}
    for record in wire_records:
        previous = records_by_code.get(record.raw_dtc)
        if previous is not None and previous.status != record.status:
            raise DtcParseError(
                f"DTC-list response repeats {record.raw_dtc} with conflicting status bytes"
            )
        records_by_code.setdefault(record.raw_dtc, record)
    # The installed TCM has twice returned one byte-identical U0415-00/40 record.  Preserve a
    # single state observation for that ECU quirk; conflicting duplicate statuses still fail.
    return response[2], tuple(records_by_code.values())


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def normalize_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DtcParseError("scan timestamp is missing")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise DtcParseError(f"invalid scan timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise DtcParseError("scan timestamp must include a UTC offset")
    return parsed.astimezone(_datetime.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class ModuleScan:
    """One module's complete ``19 02 FF`` result or an explicitly unavailable attempt."""

    source_key: str
    source_ref: str
    module_key: str
    module_name: str
    logical_bus: str
    resolved_channel: str | None
    bitrate: int
    started_at: str
    completed_at: str
    outcome: str
    unavailable_reason: str | None = None
    status_availability_mask: int | None = None
    dtcs: tuple[DtcRecord, ...] = ()
    physical_pair: str | None = None
    conditions: str | None = None
    request_hex: str = "19 02 FF"
    response_hex: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"success", "unavailable"}:
            raise ValueError("outcome must be 'success' or 'unavailable'")
        if not self.source_key or not self.module_key or not self.logical_bus:
            raise ValueError("source_key, module_key, and logical_bus are required")
        if self.resolved_channel is not None and not self.resolved_channel:
            raise ValueError("resolved_channel must be non-empty or None")
        if not isinstance(self.bitrate, int) or isinstance(self.bitrate, bool) or self.bitrate <= 0:
            raise ValueError("bitrate must be a positive integer")
        started_at = normalize_timestamp(self.started_at)
        completed_at = normalize_timestamp(self.completed_at)
        if completed_at < started_at:
            raise ValueError("completed_at cannot precede started_at")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)
        if self.outcome == "success":
            if self.unavailable_reason is not None:
                raise ValueError("a successful scan cannot have an unavailable_reason")
            if self.status_availability_mask is None:
                raise ValueError("a successful scan requires a status availability mask")
            _require_byte(self.status_availability_mask, "status_availability_mask")
        else:
            if not self.unavailable_reason:
                raise ValueError("an unavailable scan requires an unavailable_reason")
            if self.dtcs:
                raise ValueError("an unavailable scan cannot contain DTC observations")


def _normalized_hex(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return bytes.fromhex(value).hex(" ").upper()
    except ValueError:
        return ""


def scan_from_inventory_report(
    report: Mapping[str, Any],
    *,
    source_key: str,
    source_ref: str,
    module_registry: Mapping[str, Any] | None = None,
) -> ModuleScan:
    """Convert a completed ``tools/dtc_inventory.py`` JSON report into a history record."""

    if report.get("tool") != "tools/dtc_inventory.py":
        raise DtcParseError("not a tools/dtc_inventory.py report")
    if report.get("clear_service_implemented") is not False:
        raise DtcParseError("report does not prove that DTC clear was unavailable")
    if report.get("diagnostic_session_control_sent") is not False:
        raise DtcParseError("report includes or does not exclude DiagnosticSessionControl")
    module_data = report.get("module")
    if not isinstance(module_data, Mapping):
        raise DtcParseError("report module metadata is missing")
    module_key = module_data.get("key")
    if not isinstance(module_key, str) or not module_key:
        raise DtcParseError("report module key is missing")
    if module_registry is None:
        from lib.modules import MODULES

        module_registry = MODULES
    try:
        registry_module = module_registry[module_key]
    except KeyError as exc:
        raise DtcParseError(f"unregistered module {module_key!r}") from exc
    logical_bus = module_data.get("bus")
    if logical_bus != registry_module.bus:
        raise DtcParseError(
            f"report bus {logical_bus!r} does not match registry bus {registry_module.bus!r}"
        )
    bitrate = module_data.get("bitrate")
    if bitrate != registry_module.bitrate:
        raise DtcParseError(
            f"report bitrate {bitrate!r} does not match registry bitrate {registry_module.bitrate}"
        )
    resolved_channel = module_data.get("channel")
    if not isinstance(resolved_channel, str) or not resolved_channel:
        resolved_channel = None

    results = report.get("results")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes, bytearray)):
        raise DtcParseError("report results are missing")
    matching = [
        result
        for result in results
        if isinstance(result, Mapping)
        and _normalized_hex(result.get("request_hex")) == "19 02 FF"
    ]
    if len(matching) != 1:
        raise DtcParseError("report must contain exactly one 19 02 FF result")
    result = matching[0]
    category = result.get("category")
    response_text = result.get("response_hex")
    response_hex = _normalized_hex(response_text) or None
    if category == "positive":
        if response_hex is None:
            raise DtcParseError("positive 19 02 result has no valid response bytes")
        availability_mask, records = parse_dtc_list_response(bytes.fromhex(response_hex))
        outcome = "success"
        unavailable_reason = None
    else:
        availability_mask = None
        records = ()
        if category == "timeout":
            unavailable_reason = "timeout"
        elif category == "negative":
            negative = result.get("negative_response")
            nrc = negative.get("nrc") if isinstance(negative, Mapping) else None
            unavailable_reason = f"negative_response_nrc_{str(nrc).lower()}" if nrc else "negative_response"
        elif category in {"unexpected", None}:
            unavailable_reason = "unexpected_response"
        else:
            unavailable_reason = f"inventory_{str(category).lower()}"
        fatal_error = report.get("fatal_error")
        if fatal_error:
            unavailable_reason = "inventory_error"
        elif report.get("interrupted"):
            unavailable_reason = "inventory_interrupted"
        outcome = "unavailable"

    return ModuleScan(
        source_key=source_key,
        source_ref=source_ref,
        module_key=module_key,
        module_name=str(module_data.get("name") or registry_module.name),
        logical_bus=logical_bus,
        resolved_channel=resolved_channel,
        bitrate=bitrate,
        started_at=report.get("started_at"),
        completed_at=report.get("completed_at"),
        outcome=outcome,
        unavailable_reason=unavailable_reason,
        status_availability_mask=availability_mask,
        dtcs=records,
        physical_pair=report.get("physical_pair") if isinstance(report.get("physical_pair"), str) else None,
        conditions=report.get("conditions") if isinstance(report.get("conditions"), str) else None,
        response_hex=response_hex,
    )


class DtcHistory:
    """Durable scan history and current-state cache backed by SQLite."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "DtcHistory":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS module_scans (
                id INTEGER PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                source_ref TEXT NOT NULL,
                module_key TEXT NOT NULL,
                module_name TEXT NOT NULL,
                logical_bus TEXT NOT NULL,
                resolved_channel TEXT,
                bitrate INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('success', 'unavailable')),
                unavailable_reason TEXT,
                status_availability_mask INTEGER,
                dtc_count INTEGER,
                physical_pair TEXT,
                conditions TEXT,
                request_hex TEXT NOT NULL CHECK (request_hex = '19 02 FF'),
                response_hex TEXT,
                imported_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS module_scans_module_time
                ON module_scans(module_key, completed_at, id);
            CREATE TABLE IF NOT EXISTS dtc_observations (
                module_scan_id INTEGER NOT NULL REFERENCES module_scans(id) ON DELETE CASCADE,
                module_key TEXT NOT NULL,
                raw_dtc TEXT NOT NULL,
                status INTEGER NOT NULL,
                PRIMARY KEY (module_scan_id, raw_dtc)
            );
            CREATE INDEX IF NOT EXISTS dtc_observations_module_dtc
                ON dtc_observations(module_key, raw_dtc);
            CREATE TABLE IF NOT EXISTS module_cache (
                module_key TEXT PRIMARY KEY,
                module_name TEXT NOT NULL,
                logical_bus TEXT NOT NULL,
                resolved_channel TEXT,
                bitrate INTEGER NOT NULL,
                availability TEXT NOT NULL CHECK (availability IN ('available', 'unavailable')),
                unavailable_reason TEXT,
                last_attempt_at TEXT NOT NULL,
                last_success_at TEXT,
                successful_scans INTEGER NOT NULL,
                unavailable_scans INTEGER NOT NULL,
                consecutive_unavailable INTEGER NOT NULL,
                last_success_dtc_count INTEGER,
                status_availability_mask INTEGER,
                absence_authoritative INTEGER NOT NULL CHECK (absence_authoritative IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS dtc_state (
                module_key TEXT NOT NULL,
                raw_dtc TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                observation_count INTEGER NOT NULL,
                episode_count INTEGER NOT NULL,
                recurrence_count INTEGER NOT NULL,
                present INTEGER NOT NULL CHECK (present IN (0, 1)),
                status INTEGER NOT NULL,
                status_availability_mask INTEGER NOT NULL,
                status_changed_at TEXT NOT NULL,
                resolved_at TEXT,
                PRIMARY KEY (module_key, raw_dtc)
            );
            CREATE TABLE IF NOT EXISTS dtc_transitions (
                id INTEGER PRIMARY KEY,
                module_scan_id INTEGER NOT NULL REFERENCES module_scans(id) ON DELETE CASCADE,
                module_key TEXT NOT NULL,
                raw_dtc TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('discovered', 'status_changed', 'resolved', 'recurred')),
                from_present INTEGER,
                to_present INTEGER NOT NULL CHECK (to_present IN (0, 1)),
                from_status INTEGER,
                to_status INTEGER
            );
            CREATE INDEX IF NOT EXISTS dtc_transitions_module_time
                ON dtc_transitions(module_key, occurred_at, id);
            """
        )
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        existing_version = int(row["value"]) if row is not None else None
        if existing_version == 1:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE module_cache ADD COLUMN status_availability_mask INTEGER"
                )
                self.connection.execute(
                    """
                    ALTER TABLE module_cache ADD COLUMN absence_authoritative INTEGER
                    NOT NULL DEFAULT 0 CHECK (absence_authoritative IN (0, 1))
                    """
                )
                self.connection.execute(
                    """
                    ALTER TABLE dtc_state ADD COLUMN status_availability_mask INTEGER
                    NOT NULL DEFAULT 0
                    """
                )
                self.connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                module_keys = [
                    item["module_key"]
                    for item in self.connection.execute(
                        "SELECT DISTINCT module_key FROM module_scans"
                    ).fetchall()
                ]
                for module_key in module_keys:
                    self._rebuild_module(module_key)
        elif existing_version is not None and existing_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported DTC history schema {row['value']}; expected {SCHEMA_VERSION}"
            )
        self.connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def record_scan(self, scan: ModuleScan) -> dict[str, Any]:
        """Insert one idempotent scan and rebuild that module's chronological cache."""

        return self.record_scans((scan,))[0]

    def record_scans(self, scans: Iterable[ModuleScan]) -> list[dict[str, Any]]:
        """Atomically insert an import batch and rebuild each affected module once."""

        scans = tuple(scans)
        results: list[dict[str, Any]] = []
        affected_modules: set[str] = set()
        with self.connection:
            for scan in scans:
                result = self._insert_scan(scan)
                results.append(result)
                if result["inserted"]:
                    affected_modules.add(scan.module_key)
            for module_key in sorted(affected_modules):
                self._rebuild_module(module_key)
        return results

    def _insert_scan(self, scan: ModuleScan) -> dict[str, Any]:
        """Insert inside the caller's transaction without rebuilding derived state."""

        existing = self.connection.execute(
            "SELECT id FROM module_scans WHERE source_key = ?", (scan.source_key,)
        ).fetchone()
        if existing is not None:
            return {"inserted": False, "module_scan_id": existing["id"]}
        cursor = self.connection.execute(
            """
            INSERT INTO module_scans(
                source_key, source_ref, module_key, module_name, logical_bus,
                resolved_channel, bitrate, started_at, completed_at, outcome,
                unavailable_reason, status_availability_mask, dtc_count, physical_pair,
                conditions, request_hex, response_hex, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan.source_key,
                scan.source_ref,
                scan.module_key,
                scan.module_name,
                scan.logical_bus,
                scan.resolved_channel,
                scan.bitrate,
                scan.started_at,
                scan.completed_at,
                scan.outcome,
                scan.unavailable_reason,
                scan.status_availability_mask,
                len(scan.dtcs) if scan.outcome == "success" else None,
                scan.physical_pair,
                scan.conditions,
                scan.request_hex,
                scan.response_hex,
                _utc_now(),
            ),
        )
        module_scan_id = int(cursor.lastrowid)
        if scan.outcome == "success":
            self.connection.executemany(
                """
                INSERT INTO dtc_observations(module_scan_id, module_key, raw_dtc, status)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (module_scan_id, scan.module_key, record.raw_dtc, record.status)
                    for record in scan.dtcs
                ),
            )
        return {"inserted": True, "module_scan_id": module_scan_id}

    def _rebuild_module(self, module_key: str) -> None:
        scans = self.connection.execute(
            """
            SELECT * FROM module_scans
            WHERE module_key = ?
            ORDER BY completed_at, id
            """,
            (module_key,),
        ).fetchall()
        self.connection.execute("DELETE FROM dtc_transitions WHERE module_key = ?", (module_key,))
        self.connection.execute("DELETE FROM dtc_state WHERE module_key = ?", (module_key,))
        self.connection.execute("DELETE FROM module_cache WHERE module_key = ?", (module_key,))
        states: dict[str, dict[str, Any]] = {}
        successful_scans = 0
        unavailable_scans = 0
        consecutive_unavailable = 0
        last_success_at = None
        last_success_dtc_count = None
        last_success_status_mask = None
        last_absence_authoritative = False
        for scan in scans:
            if scan["outcome"] == "unavailable":
                unavailable_scans += 1
                consecutive_unavailable += 1
                continue
            successful_scans += 1
            consecutive_unavailable = 0
            last_success_at = scan["completed_at"]
            last_success_dtc_count = scan["dtc_count"]
            scan_status_mask = scan["status_availability_mask"]
            last_success_status_mask = scan_status_mask
            last_absence_authoritative = bool(scan_status_mask)
            observations = self.connection.execute(
                """
                SELECT raw_dtc, status FROM dtc_observations
                WHERE module_scan_id = ? ORDER BY raw_dtc
                """,
                (scan["id"],),
            ).fetchall()
            seen = {row["raw_dtc"] for row in observations}
            for row in observations:
                raw_dtc = row["raw_dtc"]
                status = row["status"]
                state = states.get(raw_dtc)
                if state is None:
                    states[raw_dtc] = {
                        "first_seen_at": scan["completed_at"],
                        "last_seen_at": scan["completed_at"],
                        "observation_count": 1,
                        "episode_count": 1,
                        "recurrence_count": 0,
                        "present": 1,
                        "status": status,
                        "status_availability_mask": scan_status_mask,
                        "status_changed_at": scan["completed_at"],
                        "resolved_at": None,
                    }
                    self._insert_transition(scan, raw_dtc, "discovered", None, 1, None, status)
                elif not state["present"]:
                    old_status = state["status"]
                    state.update(
                        {
                            "last_seen_at": scan["completed_at"],
                            "observation_count": state["observation_count"] + 1,
                            "episode_count": state["episode_count"] + 1,
                            "recurrence_count": state["recurrence_count"] + 1,
                            "present": 1,
                            "status": status,
                            "status_availability_mask": scan_status_mask,
                            "status_changed_at": (
                                scan["completed_at"]
                                if status != old_status
                                else state["status_changed_at"]
                            ),
                            "resolved_at": None,
                        }
                    )
                    self._insert_transition(
                        scan, raw_dtc, "recurred", 0, 1, old_status, status
                    )
                else:
                    old_status = state["status"]
                    state["last_seen_at"] = scan["completed_at"]
                    state["observation_count"] += 1
                    state["status"] = status
                    state["status_availability_mask"] = scan_status_mask
                    if status != old_status:
                        state["status_changed_at"] = scan["completed_at"]
                        self._insert_transition(
                            scan, raw_dtc, "status_changed", 1, 1, old_status, status
                        )
            for raw_dtc, state in states.items():
                if state["present"] and raw_dtc not in seen:
                    previous_mask = state["status_availability_mask"]
                    compatible_mask = (
                        bool(scan_status_mask)
                        and bool(previous_mask)
                        and (scan_status_mask & previous_mask) == previous_mask
                    )
                    if compatible_mask:
                        state["present"] = 0
                        state["resolved_at"] = scan["completed_at"]
                        self._insert_transition(
                            scan, raw_dtc, "resolved", 1, 0, state["status"], None
                        )
                    else:
                        last_absence_authoritative = False

        if not scans:
            return
        latest = scans[-1]
        self.connection.execute(
            """
            INSERT INTO module_cache(
                module_key, module_name, logical_bus, resolved_channel, bitrate,
                availability, unavailable_reason, last_attempt_at, last_success_at,
                successful_scans, unavailable_scans, consecutive_unavailable,
                last_success_dtc_count, status_availability_mask, absence_authoritative
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                module_key,
                latest["module_name"],
                latest["logical_bus"],
                latest["resolved_channel"],
                latest["bitrate"],
                "available" if latest["outcome"] == "success" else "unavailable",
                latest["unavailable_reason"],
                latest["completed_at"],
                last_success_at,
                successful_scans,
                unavailable_scans,
                consecutive_unavailable,
                last_success_dtc_count,
                last_success_status_mask,
                int(last_absence_authoritative),
            ),
        )
        self.connection.executemany(
            """
            INSERT INTO dtc_state(
                module_key, raw_dtc, first_seen_at, last_seen_at, observation_count,
                episode_count, recurrence_count, present, status, status_availability_mask,
                status_changed_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    module_key,
                    raw_dtc,
                    state["first_seen_at"],
                    state["last_seen_at"],
                    state["observation_count"],
                    state["episode_count"],
                    state["recurrence_count"],
                    state["present"],
                    state["status"],
                    state["status_availability_mask"],
                    state["status_changed_at"],
                    state["resolved_at"],
                )
                for raw_dtc, state in sorted(states.items())
            ),
        )

    def _insert_transition(
        self,
        scan: sqlite3.Row,
        raw_dtc: str,
        kind: str,
        from_present: int | None,
        to_present: int,
        from_status: int | None,
        to_status: int | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO dtc_transitions(
                module_scan_id, module_key, raw_dtc, occurred_at, kind,
                from_present, to_present, from_status, to_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan["id"],
                scan["module_key"],
                raw_dtc,
                scan["completed_at"],
                kind,
                from_present,
                to_present,
                from_status,
                to_status,
            ),
        )

    def snapshot(
        self,
        *,
        module_registry: Mapping[str, Any] | None = None,
        include_resolved: bool = False,
    ) -> dict[str, Any]:
        """Build a cache-only JSON-compatible view; this method performs no acquisition."""

        if module_registry is None:
            from lib.modules import MODULES

            module_registry = MODULES
        cached = {
            row["module_key"]: row
            for row in self.connection.execute("SELECT * FROM module_cache").fetchall()
        }
        modules = []
        for module_key, module in module_registry.items():
            cache = cached.get(module_key)
            if cache is None:
                modules.append(
                    {
                        "module_key": module_key,
                        "module_name": module.name,
                        "logical_bus": module.bus,
                        "resolved_channel": None,
                        "bitrate": module.bitrate,
                        "availability": "never_scanned",
                        "result_state": "never_scanned",
                        "unavailable_reason": None,
                        "last_attempt_at": None,
                        "last_success_at": None,
                        "successful_scans": 0,
                        "unavailable_scans": 0,
                        "consecutive_unavailable": 0,
                        "last_success_dtc_count": None,
                        "status_availability_mask": None,
                        "absence_authoritative": False,
                        "dtcs": [],
                    }
                )
                continue
            states = self.connection.execute(
                """
                SELECT * FROM dtc_state
                WHERE module_key = ? AND (? OR present = 1)
                ORDER BY present DESC, raw_dtc
                """,
                (module_key, int(include_resolved)),
            ).fetchall()
            dtcs = []
            for state in states:
                if not state["present"]:
                    observation_state = "resolved_history"
                elif cache["availability"] == "unavailable":
                    observation_state = "stale_after_unavailable_attempt"
                elif state["last_seen_at"] == cache["last_success_at"]:
                    observation_state = "observed_in_latest_success"
                else:
                    observation_state = "retained_incompatible_status_mask"
                dtcs.append(
                    {
                        "raw_dtc": state["raw_dtc"],
                        "fca_display": fca_dtc_name(bytes.fromhex(state["raw_dtc"])),
                        **status_semantics(state["status"]),
                        "present": bool(state["present"]),
                        "observation_state": observation_state,
                        "status_availability_mask": f"{state['status_availability_mask']:02X}",
                        "first_seen_at": state["first_seen_at"],
                        "last_seen_at": state["last_seen_at"],
                        "observation_count": state["observation_count"],
                        "episode_count": state["episode_count"],
                        "recurrence_count": state["recurrence_count"],
                        "status_changed_at": state["status_changed_at"],
                        "resolved_at": state["resolved_at"],
                    }
                )
            if cache["availability"] == "unavailable":
                result_state = "unavailable"
            elif cache["last_success_dtc_count"] == 0 and cache["absence_authoritative"]:
                result_state = "no_dtcs"
            elif cache["last_success_dtc_count"] == 0:
                result_state = "status_coverage_incomplete"
            else:
                result_state = "dtcs_present"
            modules.append(
                {
                    "module_key": module_key,
                    "module_name": module.name,
                    "logical_bus": cache["logical_bus"],
                    "resolved_channel": cache["resolved_channel"],
                    "bitrate": cache["bitrate"],
                    "availability": cache["availability"],
                    "result_state": result_state,
                    "unavailable_reason": cache["unavailable_reason"],
                    "last_attempt_at": cache["last_attempt_at"],
                    "last_success_at": cache["last_success_at"],
                    "successful_scans": cache["successful_scans"],
                    "unavailable_scans": cache["unavailable_scans"],
                    "consecutive_unavailable": cache["consecutive_unavailable"],
                    "last_success_dtc_count": cache["last_success_dtc_count"],
                    "status_availability_mask": (
                        f"{cache['status_availability_mask']:02X}"
                        if cache["status_availability_mask"] is not None
                        else None
                    ),
                    "absence_authoritative": bool(cache["absence_authoritative"]),
                    "dtcs": dtcs,
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "acquisition": "cache_only",
            "modules": modules,
        }

    def transitions(self, module_key: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT raw_dtc, occurred_at, kind, from_present, to_present,
                   from_status, to_status
            FROM dtc_transitions WHERE module_key = ? ORDER BY occurred_at, id
            """,
            (module_key,),
        ).fetchall()
        return [
            {
                "raw_dtc": row["raw_dtc"],
                "occurred_at": row["occurred_at"],
                "kind": row["kind"],
                "from_present": (
                    bool(row["from_present"]) if row["from_present"] is not None else None
                ),
                "to_present": bool(row["to_present"]),
                "from_status": (
                    f"{row['from_status']:02X}" if row["from_status"] is not None else None
                ),
                "to_status": (
                    f"{row['to_status']:02X}" if row["to_status"] is not None else None
                ),
            }
            for row in rows
        ]

    def dashboard_summary(
        self,
        *,
        module_registry: Mapping[str, Any] | None = None,
        compact: bool = False,
        per_group_limit: int = 25,
    ) -> dict[str, Any]:
        """Return module coverage plus exclusive, presentation-oriented active-DTC groups.

        Compact mode retains complete group counts and all module availability rows, but returns
        only the newest/highest-priority records in each group.  It is intended for a dedicated
        cache-only web GET, not the telemetry SSE stream.
        """

        if (
            not isinstance(per_group_limit, int)
            or isinstance(per_group_limit, bool)
            or not 1 <= per_group_limit <= 1000
        ):
            raise ValueError("per_group_limit must be an integer from 1 through 1000")

        snapshot = self.snapshot(module_registry=module_registry)
        modules = snapshot["modules"]
        groups: dict[str, list[dict[str, Any]]] = {
            "current": [],
            "pending": [],
            "confirmed_history": [],
            "incomplete_only": [],
            "other": [],
        }
        available = 0
        unavailable = 0
        never_scanned = 0
        modules_with_dtcs = 0
        modules_no_dtcs = 0
        modules_status_coverage_incomplete = 0
        modules_with_last_known_dtcs = 0
        attempt_times = []
        success_times = []
        for module in modules:
            availability = module["availability"]
            if availability == "available":
                available += 1
            elif availability == "unavailable":
                unavailable += 1
            else:
                never_scanned += 1
            if module["result_state"] == "dtcs_present":
                modules_with_dtcs += 1
            elif module["result_state"] == "no_dtcs":
                modules_no_dtcs += 1
            elif module["result_state"] == "status_coverage_incomplete":
                modules_status_coverage_incomplete += 1
            if module["dtcs"]:
                modules_with_last_known_dtcs += 1
            if module["last_attempt_at"]:
                attempt_times.append(module["last_attempt_at"])
            if module["last_success_at"]:
                success_times.append(module["last_success_at"])
            for dtc in module["dtcs"]:
                record = {
                    "module_key": module["module_key"],
                    "module_name": module["module_name"],
                    "logical_bus": module["logical_bus"],
                    "resolved_channel": module["resolved_channel"],
                    "module_availability": module["availability"],
                    "module_result_state": module["result_state"],
                    "latest_attempt_successful": module["availability"] == "available",
                    "last_attempt_at": module["last_attempt_at"],
                    "last_success_at": module["last_success_at"],
                    **dtc,
                }
                group = dtc["display_group"]
                groups[group if group in groups else "other"].append(record)
        group_counts = {name: len(records) for name, records in groups.items()}
        if compact:
            def priority(record: Mapping[str, Any]) -> tuple[Any, ...]:
                return (
                    bool(record["warning_indicator_requested"]),
                    bool(record["current"]),
                    bool(record["pending"]),
                    bool(record["confirmed"]),
                    record["last_seen_at"],
                    record["status_changed_at"],
                    record["module_key"],
                    record["raw_dtc"],
                )

            groups = {
                name: sorted(records, key=priority, reverse=True)[:per_group_limit]
                for name, records in groups.items()
            }
        return {
            "schema_version": snapshot["schema_version"],
            "generated_at": snapshot["generated_at"],
            "acquisition": "cache_only",
            "compact": compact,
            "per_group_limit": per_group_limit if compact else None,
            "group_counts": group_counts,
            "group_returned_counts": {
                name: len(records) for name, records in groups.items()
            },
            "groups_truncated": any(
                len(groups[name]) < total for name, total in group_counts.items()
            ),
            "coverage": {
                "total_modules": len(modules),
                "available_modules": available,
                "unavailable_modules": unavailable,
                "never_scanned_modules": never_scanned,
                "modules_with_dtcs": modules_with_dtcs,
                "modules_no_dtcs": modules_no_dtcs,
                "modules_status_coverage_incomplete": modules_status_coverage_incomplete,
                "modules_with_last_known_dtcs": modules_with_last_known_dtcs,
                "last_attempt_at": max(attempt_times) if attempt_times else None,
                "last_success_at": max(success_times) if success_times else None,
            },
            "groups": groups,
            "modules": [
                {key: value for key, value in module.items() if key != "dtcs"}
                for module in modules
            ],
        }


def write_cache(path: str | os.PathLike[str], snapshot: Mapping[str, Any]) -> None:
    """Atomically replace a JSON cache suitable for a read-only dashboard integration."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
