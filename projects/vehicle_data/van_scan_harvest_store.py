"""SQLite storage for passively harvested in-vehicle diagnostic scans."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

from lib.dtc import fca_dtc_name, status_semantics


PROVENANCE = "in-vehicle scan (source F1), passive"
VAN_DTC_REQUEST = bytes.fromhex("19 02 0D")
CACHE_SCHEMA_VERSION = 1
DB_SCHEMA_KEY = "in_vehicle_scan_schema_version"
DB_SCHEMA_VERSION = 1


def _utc_now() -> str:
    """Use the entry module's clock when imported there, preserving patch targets."""
    module = sys.modules.get("projects.vehicle_data.van_scan_harvest")
    if module is None:
        candidate = sys.modules.get("__main__")
        if Path(str(getattr(candidate, "__file__", ""))).name == "van_scan_harvest.py":
            module = candidate
    now = getattr(module, "utc_now", None)
    if callable(now):
        return now()
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def kwp_dtc_name(raw: bytes) -> str:
    first, second = raw[0], raw[1]
    letter = "PCBU"[(first >> 6) & 0x03]
    return f"{letter}{((first & 0x3F) << 8) | second:04X}"


class InVehicleScanStore:
    """Clearly sourced in-vehicle scan tables beside the Pi's ``19 02 FF`` history.

    The Pi's ``module_scans`` table keeps its ``request_hex = '19 02 FF'`` constraint and its
    derived state; these tables never feed it.  Versioned by the ``metadata`` row
    ``in_vehicle_scan_schema_version``; future versions migrate with ``ALTER TABLE`` like
    ``lib.dtc.DtcHistory``.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "InVehicleScanStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS in_vehicle_scans (
                id INTEGER PRIMARY KEY,
                scan_key TEXT NOT NULL UNIQUE,
                campaign TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                buses TEXT NOT NULL,
                tester_source TEXT NOT NULL CHECK (tester_source = 'F1'),
                provenance TEXT NOT NULL CHECK (provenance = '{PROVENANCE}'),
                dtc_request_hex TEXT NOT NULL CHECK (dtc_request_hex = '19 02 0D'),
                exchange_count INTEGER NOT NULL,
                modules_queried INTEGER NOT NULL,
                modules_answered INTEGER NOT NULL,
                pi_quiet INTEGER NOT NULL CHECK (pi_quiet IN (0, 1)),
                pi_requests_overlapping INTEGER NOT NULL,
                report_path TEXT,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS in_vehicle_module_results (
                id INTEGER PRIMARY KEY,
                scan_id INTEGER NOT NULL REFERENCES in_vehicle_scans(id) ON DELETE CASCADE,
                module_key TEXT NOT NULL,
                module_name TEXT NOT NULL,
                logical_bus TEXT NOT NULL,
                target_address TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('success', 'negative_response', 'no_response', 'incomplete', 'unparsed')
                ),
                protocol TEXT CHECK (protocol IS NULL OR protocol IN ('uds', 'kwp2000')),
                request_hex TEXT NOT NULL,
                response_hex TEXT,
                nrc TEXT,
                status_availability_mask INTEGER,
                dtc_count INTEGER,
                UNIQUE (scan_id, module_key)
            );
            CREATE INDEX IF NOT EXISTS in_vehicle_module_results_module_time
                ON in_vehicle_module_results(module_key, observed_at, id);
            CREATE TABLE IF NOT EXISTS in_vehicle_dtc_observations (
                module_result_id INTEGER NOT NULL
                    REFERENCES in_vehicle_module_results(id) ON DELETE CASCADE,
                module_key TEXT NOT NULL,
                raw_dtc TEXT NOT NULL,
                status INTEGER NOT NULL,
                PRIMARY KEY (module_result_id, raw_dtc)
            );
            """
        )
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (DB_SCHEMA_KEY,)
        ).fetchone()
        if row is not None and int(row["value"]) != DB_SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported in-vehicle scan schema {row['value']}; expected {DB_SCHEMA_VERSION}"
            )
        self.connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)",
            (DB_SCHEMA_KEY, str(DB_SCHEMA_VERSION)),
        )
        self.connection.commit()

    def import_report(self, report: Mapping[str, Any], *, report_path: str | None = None) -> dict[str, Any]:
        if report.get("classification") != "in_vehicle_health_check":
            return {"inserted": False, "reason": "not_a_health_check"}
        with self.connection:
            existing = self.connection.execute(
                "SELECT id FROM in_vehicle_scans WHERE scan_key = ?", (report["scan_id"],)
            ).fetchone()
            if existing is not None:
                return {"inserted": False, "reason": "already_imported", "scan_row": existing["id"]}
            cursor = self.connection.execute(
                """
                INSERT INTO in_vehicle_scans(
                    scan_key, campaign, started_at, completed_at, buses, tester_source, provenance,
                    dtc_request_hex, exchange_count, modules_queried, modules_answered, pi_quiet,
                    pi_requests_overlapping, report_path, imported_at
                ) VALUES (?, ?, ?, ?, ?, 'F1', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report["scan_id"],
                    report["campaign"],
                    report["started_at"],
                    report["completed_at"],
                    json.dumps(report["buses"]),
                    PROVENANCE,
                    report["dtc_request"],
                    report["exchange_count"],
                    report["modules_queried"],
                    report["modules_answered"],
                    int(bool(report["pi_quiet"])),
                    int(report["pi_requests_overlapping"]),
                    report_path,
                    _utc_now(),
                ),
            )
            scan_row = int(cursor.lastrowid)
            modules = 0
            for module in report["modules"]:
                dtc = module.get("dtc")
                if not dtc:
                    continue
                cursor = self.connection.execute(
                    """
                    INSERT INTO in_vehicle_module_results(
                        scan_id, module_key, module_name, logical_bus, target_address, observed_at,
                        outcome, protocol, request_hex, response_hex, nrc, status_availability_mask,
                        dtc_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_row,
                        module["module_key"],
                        module["module_name"],
                        module["logical_bus"],
                        module["target_address"],
                        dtc["observed_at"],
                        dtc["outcome"],
                        dtc.get("protocol"),
                        dtc["request_hex"],
                        dtc.get("response_hex"),
                        dtc.get("nrc"),
                        int(dtc["status_availability_mask"], 16) if dtc.get("status_availability_mask") else None,
                        len(dtc["dtcs"]) if dtc["outcome"] == "success" else None,
                    ),
                )
                result_row = int(cursor.lastrowid)
                if dtc["outcome"] == "success":
                    self.connection.executemany(
                        """
                        INSERT OR IGNORE INTO in_vehicle_dtc_observations(
                            module_result_id, module_key, raw_dtc, status
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            (result_row, module["module_key"], record["raw_dtc"], int(record["status"], 16))
                            for record in dtc["dtcs"]
                        ),
                    )
                modules += 1
        return {"inserted": True, "scan_row": scan_row, "module_results": modules}

    def cache_payload(self) -> dict[str, Any]:
        """Latest successful in-vehicle result per module plus the latest scan's facts."""

        last = self.connection.execute(
            "SELECT * FROM in_vehicle_scans ORDER BY completed_at DESC, id DESC LIMIT 1"
        ).fetchone()
        scan_count = self.connection.execute("SELECT COUNT(*) FROM in_vehicle_scans").fetchone()[0]
        rows = self.connection.execute(
            """
            SELECT r.*, s.scan_key, s.started_at AS scan_started_at FROM in_vehicle_module_results r
            JOIN in_vehicle_scans s ON s.id = r.scan_id
            WHERE r.outcome = 'success'
            ORDER BY r.module_key, r.observed_at DESC, r.id DESC
            """
        ).fetchall()
        modules = []
        seen = set()
        for row in rows:
            if row["module_key"] in seen:
                continue
            seen.add(row["module_key"])
            observations = self.connection.execute(
                "SELECT raw_dtc, status FROM in_vehicle_dtc_observations WHERE module_result_id = ? ORDER BY raw_dtc",
                (row["id"],),
            ).fetchall()
            dtcs = []
            for observation in observations:
                raw = observation["raw_dtc"]
                if row["protocol"] == "kwp2000":
                    dtcs.append(
                        {
                            "raw_dtc": raw,
                            "fca_display": kwp_dtc_name(bytes.fromhex(raw)),
                            "status": f"{observation['status']:02X}",
                            "status_flags": [],
                            "display_group": "other",
                            "current": False,
                            "pending": False,
                            "confirmed": False,
                            "warning_indicator_requested": False,
                            "incomplete_only": False,
                        }
                    )
                else:
                    dtcs.append(
                        {
                            "raw_dtc": raw,
                            "fca_display": fca_dtc_name(bytes.fromhex(raw)),
                            **status_semantics(observation["status"]),
                        }
                    )
            modules.append(
                {
                    "module_key": row["module_key"],
                    "module_name": row["module_name"],
                    "logical_bus": row["logical_bus"],
                    "observed_at": row["observed_at"],
                    "scan_id": row["scan_key"],
                    "scan_started_at": row["scan_started_at"],
                    "protocol": row["protocol"],
                    "status_availability_mask": (
                        f"{row['status_availability_mask']:02X}"
                        if row["status_availability_mask"] is not None
                        else None
                    ),
                    "dtc_count": row["dtc_count"],
                    "dtcs": dtcs,
                }
            )
        last_scan = None
        if last is not None:
            last_scan = {
                "scan_id": last["scan_key"],
                "started_at": last["started_at"],
                "completed_at": last["completed_at"],
                "buses": json.loads(last["buses"]),
                "modules_queried": last["modules_queried"],
                "modules_answered": last["modules_answered"],
                "pi_quiet": bool(last["pi_quiet"]),
            }
        return {
            "schema_version": CACHE_SCHEMA_VERSION,
            "type": "in_vehicle_scan_cache",
            "generated_at": _utc_now(),
            "provenance": PROVENANCE,
            "tester_source": "F1",
            "dtc_request": VAN_DTC_REQUEST.hex(" ").upper(),
            "status_mask_note": (
                "The van's own check asks only for codes with test-failed, pending or confirmed "
                "bits (mask 0D); it cannot show or clear codes outside that mask."
            ),
            "scan_count": int(scan_count),
            "last_scan": last_scan,
            "modules": modules,
        }

