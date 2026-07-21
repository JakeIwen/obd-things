#!/usr/bin/env python3
"""Export read-only AlfaOBD SQLite catalog evidence for one vehicle/profile.

This tool never opens the database writable.  It preserves raw catalog fields and
adds explicitly unverified, mechanical zero-based candidates for ``(N)`` and
``(N,choice)`` references from ``res/raw/alfaobd5_en.txt``.  Those candidates are
not decoded labels: real catalog examples are semantically inconsistent under both
zero- and one-based direct indexing, so the raw placeholders remain authoritative.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any


CATALOG_TABLES = (
    "FGA_DIESEL_DYNAMIC",
    "FGA_DIESEL_STATIC",
    "FGA_PETROL_DATA",
    "FGA_IPC_DATA",
    "FGA_IPC_ROUTINES",
    "FGA_IPC_SNAPSHOT",
    "FGA_BCM_DATA",
    "FGA_ABS_DATA",
    "FGA_ABS_SNAPSHOT",
    "FGA_ENGINE_DATA",
    "Faults",
)
LABEL_REF_RE = re.compile(r"\((\d+)(?:,\s*(\d+))?\)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_labels(path: Path) -> list[str]:
    payload = path.read_bytes()
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = payload.decode("utf-16")
    else:
        text = payload.decode("utf-8-sig")
    return text.splitlines()


def candidate_zero_based_refs(value: Any, labels: list[str]) -> Any:
    """Mechanically substitute zero-based references; this is not a verified decode."""
    if not isinstance(value, str) or "(" not in value:
        return value

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(labels):
            return match.group(0)
        label = labels[index]
        choice = match.group(2)
        if choice is not None:
            alternatives = label.split("~|")
            choice_index = int(choice)
            if choice_index < len(alternatives):
                label = alternatives[choice_index]
        return label

    return LABEL_REF_RE.sub(replace, value)


def open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def has_table(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def device_membership_sql(table: str) -> str:
    # Table identifiers come only from CATALOG_TABLES, never user input.
    return (
        f'SELECT rowid AS _rowid, * FROM "{table}" '
        "WHERE instr(',' || replace(device_id, ' ', ''), ',' || ? || ',') > 0 "
        "ORDER BY rowid"
    )


def catalog_rows(
    connection: sqlite3.Connection, device_id: int, labels: list[str]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table in CATALOG_TABLES:
        if not has_table(connection, table):
            continue
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if "device_id" not in columns:
            continue
        rows = []
        for row in connection.execute(device_membership_sql(table), (str(device_id),)):
            raw = dict(row)
            candidate = {
                key: candidate_zero_based_refs(value, labels) for key, value in raw.items()
            }
            rows.append({"raw": raw, "candidate_zero_based": candidate})
        if rows:
            result[table] = rows
    return result


def model_rows(connection: sqlite3.Connection, model_code: int) -> list[dict[str, Any]]:
    if not has_table(connection, "ECUList"):
        return []
    query = """
        SELECT e.*, u.ID AS unit_id, u.unit_name, u.inittype, u.ecuaddress,
               u.canid, u.canidresp, u.adapter AS unit_adapter, u.baudrate
        FROM ECUList AS e
        LEFT JOIN ECUUnits AS u ON u.rowid = COALESCE(
            (SELECT min(exact.rowid) FROM ECUUnits AS exact WHERE exact.ECUNAME=e.ecutype),
            (SELECT min(fallback.rowid) FROM ECUUnits AS fallback WHERE fallback.ECUNAME=e.ECUNAME)
        )
        WHERE instr(',' || replace(e.Dodge_RAM, ' ', ''), ',' || ? || ',') > 0
        ORDER BY e.ID, u.ID
    """
    return [dict(row) for row in connection.execute(query, (str(model_code),))]


def isocodes(connection: sqlite3.Connection, device_id: int) -> list[dict[str, Any]]:
    if not has_table(connection, "isocodes"):
        return []
    return [
        dict(row)
        for row in connection.execute(
            "SELECT iso_code, device_id, device_type FROM isocodes "
            "WHERE device_id=? ORDER BY iso_code",
            (device_id,),
        )
    ]


def build_export(
    database: Path, labels_path: Path, model_code: int, device_ids: list[int]
) -> dict[str, Any]:
    labels = load_labels(labels_path)
    connection = open_read_only(database)
    try:
        version = (
            dict(connection.execute("SELECT * FROM ver LIMIT 1").fetchone())
            if has_table(connection, "ver")
            else None
        )
        devices = {}
        for device_id in device_ids:
            tables = catalog_rows(connection, device_id, labels)
            devices[str(device_id)] = {
                "isocodes": isocodes(connection, device_id),
                "table_row_counts": {table: len(rows) for table, rows in tables.items()},
                "tables": tables,
            }
        return {
            "format": "alfaobd-catalog-export-v1",
            "sources": {
                "database": str(database),
                "database_sha256": sha256(database),
                "labels": str(labels_path),
                "labels_sha256": sha256(labels_path),
                "label_count": len(labels),
                "label_reference_status": "unresolved",
                "candidate_zero_based_warning": (
                    "Mechanical candidate only; real catalog examples are semantically "
                    "inconsistent under direct zero- or one-based indexing. Raw fields win."
                ),
            },
            "database_version": version,
            "model_code": model_code,
            "model_rows": model_rows(connection, model_code),
            "devices": devices,
        }
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--model-code", type=int, default=88)
    parser.add_argument("--device-id", type=int, action="append", default=[])
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tmp/ecu_mapping/android_tablet/promaster_catalog.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.device_id:
        raise SystemExit("at least one --device-id is required")
    report = build_export(args.database, args.labels, args.model_code, args.device_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{args.output.name}.",
            dir=args.output.parent, delete=False
        ) as destination:
            temporary_name = destination.name
            json.dump(report, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_name, args.output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
