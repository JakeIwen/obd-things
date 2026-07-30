#!/usr/bin/env python3
"""Decode legacy FCA engineering HSQL cache files without modifying them.

The legacy ``.eng``/``.bndl`` engineering bundles surveyed in this repository
contain a custom HSQLDB-derived cache format identified by
``hsqldb.cache_version=1.8.0.x``.  It is not the stock HSQLDB 1.8 row layout.
This tool reads an already extracted ``db.data``, ``db.script``, and
``db.properties`` set, derives its table schemas and constraints from the
script, validates the decoded rows, and writes JSON only to standard output.

It never opens CAN interfaces, executes diagnostic requests, runs bundle code,
or writes to any input file.  Redirect stdout under ``tmp/`` when a persistent
report is wanted.

Examples::

    python3 tools/fca_hsql_decode.py path/db.data \
      --script path/db.script --properties path/db.properties --summary

    python3 tools/fca_hsql_decode.py path/db.data \
      --script path/db.script --properties path/db.properties \
      --labels path/Label.properties --xmit 22F187 --xmit 2231D0 \
      > tmp/fca_hsql_join.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sys
from typing import Iterable, Sequence


DATA_HEADER_SIZE = 32
FCA_CACHE_VERSION = "1.8.0.x"

SQL_TYPE_KIND = {
    "BIGINT": "bigint",
    "BIT": "boolean",
    "BOOLEAN": "boolean",
    "CHAR": "string",
    "DOUBLE": "double",
    "FLOAT": "double",
    "INTEGER": "integer",
    "LONGVARCHAR": "string",
    "REAL": "double",
    "SMALLINT": "smallint",
    "TINYINT": "smallint",
    "VARCHAR": "string",
    "VARCHAR_IGNORECASE": "string",
}

CREATE_TABLE_RE = re.compile(
    r"^CREATE\s+CACHED\s+TABLE\s+([A-Z0-9_]+)\((.*)\)$", re.IGNORECASE
)
SET_TABLE_RE = re.compile(
    r"^SET\s+TABLE\s+([A-Z0-9_]+)\s+INDEX'([^']+)'$", re.IGNORECASE
)
PRIMARY_KEY_RE = re.compile(r"PRIMARY\s+KEY\s*\(([^)]+)\)", re.IGNORECASE)
FOREIGN_KEY_RE = re.compile(
    r"FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+"
    r"([A-Z0-9_]+)\s*\(([^)]+)\)",
    re.IGNORECASE,
)

Scalar = str | int | float | bool | None
Row = dict[str, Scalar]
Schema = tuple[tuple[str, str], ...]


class DecodeError(ValueError):
    """Raised when an input violates the validated FCA cache structure."""


@dataclass(frozen=True)
class ForeignKey:
    child_table: str
    child_columns: tuple[str, ...]
    parent_table: str
    parent_columns: tuple[str, ...]


@dataclass(frozen=True)
class ScriptDefinition:
    schemas: dict[str, Schema]
    primary_keys: dict[str, tuple[str, ...]]
    foreign_keys: tuple[ForeignKey, ...]
    roots: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DecodedDatabase:
    definition: ScriptDefinition
    rows: dict[str, list[Row]]
    row_offsets: dict[str, list[int]]
    metadata: dict[str, object]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _split_sql_list(value: str) -> list[str]:
    """Split a SQL definition list without splitting nested parentheses."""
    fields: list[str] = []
    start = 0
    depth = 0
    quoted = False
    pos = 0
    while pos < len(value):
        char = value[pos]
        if char == "'":
            if quoted and pos + 1 < len(value) and value[pos + 1] == "'":
                pos += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    raise DecodeError("unbalanced SQL parentheses")
            elif char == "," and depth == 0:
                fields.append(value[start:pos].strip())
                start = pos + 1
        pos += 1
    if quoted or depth:
        raise DecodeError("unterminated SQL quote or parentheses")
    fields.append(value[start:].strip())
    return fields


def _column_names(value: str) -> tuple[str, ...]:
    columns = tuple(part.strip().upper() for part in value.split(","))
    if not columns or any(not column for column in columns):
        raise DecodeError(f"invalid SQL column list {value!r}")
    return columns


def parse_script(text: str) -> ScriptDefinition:
    """Derive cached-table schemas, keys, and index roots from ``db.script``."""
    schemas: dict[str, Schema] = {}
    primary_keys: dict[str, tuple[str, ...]] = {}
    foreign_keys: list[ForeignKey] = []
    roots: list[tuple[str, int]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        table_match = CREATE_TABLE_RE.fullmatch(line)
        if table_match:
            table = table_match.group(1).upper()
            if table in schemas:
                raise DecodeError(f"duplicate table definition for {table}")
            columns: list[tuple[str, str]] = []
            inline_primary: list[str] = []
            table_primary: tuple[str, ...] | None = None

            for definition in _split_sql_list(table_match.group(2)):
                primary_match = PRIMARY_KEY_RE.search(definition)
                has_primary_key = bool(
                    re.search(r"\bPRIMARY\s+KEY\b", definition, re.IGNORECASE)
                )
                foreign_match = FOREIGN_KEY_RE.search(definition)
                upper = definition.upper()

                if upper.startswith("PRIMARY KEY") or (
                    upper.startswith("CONSTRAINT ") and primary_match
                ):
                    if not primary_match:
                        raise DecodeError(
                            f"cannot parse primary key in {table}: {definition}"
                        )
                    if table_primary is not None:
                        raise DecodeError(f"multiple primary keys in {table}")
                    table_primary = _column_names(primary_match.group(1))
                    continue

                if (
                    upper.startswith("CONSTRAINT ")
                    or upper.startswith("FOREIGN KEY")
                    or upper.startswith("UNIQUE")
                    or upper.startswith("CHECK")
                ):
                    if foreign_match:
                        child_columns = _column_names(foreign_match.group(1))
                        parent_columns = _column_names(foreign_match.group(3))
                        if len(child_columns) != len(parent_columns):
                            raise DecodeError(
                                f"foreign key column-count mismatch in {table}"
                            )
                        foreign_keys.append(
                            ForeignKey(
                                child_table=table,
                                child_columns=child_columns,
                                parent_table=foreign_match.group(2).upper(),
                                parent_columns=parent_columns,
                            )
                        )
                    continue

                parts = definition.split()
                if len(parts) < 2:
                    raise DecodeError(
                        f"cannot parse column in {table}: {definition}"
                    )
                column = parts[0].upper()
                sql_type = parts[1].split("(", 1)[0].upper()
                try:
                    kind = SQL_TYPE_KIND[sql_type]
                except KeyError as error:
                    raise DecodeError(
                        f"unsupported SQL type {sql_type} in {table}.{column}"
                    ) from error
                if any(existing == column for existing, _kind in columns):
                    raise DecodeError(f"duplicate column {table}.{column}")
                columns.append((column, kind))
                if has_primary_key:
                    inline_primary.append(column)

            schemas[table] = tuple(columns)
            key = table_primary or tuple(inline_primary)
            if key:
                primary_keys[table] = key
            continue

        root_match = SET_TABLE_RE.fullmatch(line)
        if root_match:
            table = root_match.group(1).upper()
            values = root_match.group(2).split()
            if not values:
                raise DecodeError(f"missing index root for {table}")
            try:
                root = int(values[0])
            except ValueError as error:
                raise DecodeError(f"invalid index root for {table}") from error
            if any(existing == table for existing, _root in roots):
                raise DecodeError(f"duplicate index roots for {table}")
            roots.append((table, root))

    if not schemas:
        raise DecodeError("db.script contains no CREATE CACHED TABLE definitions")
    for table, _root in roots:
        if table not in schemas:
            raise DecodeError(f"index root references undefined table {table}")

    for table, columns in primary_keys.items():
        known = {name for name, _kind in schemas[table]}
        missing = set(columns) - known
        if missing:
            raise DecodeError(f"{table} primary key has unknown columns {missing}")
    for key in foreign_keys:
        if key.parent_table not in schemas:
            raise DecodeError(
                f"{key.child_table} foreign key references undefined "
                f"table {key.parent_table}"
            )
        child_known = {name for name, _kind in schemas[key.child_table]}
        parent_known = {name for name, _kind in schemas[key.parent_table]}
        if set(key.child_columns) - child_known:
            raise DecodeError(f"{key.child_table} foreign key has unknown columns")
        if set(key.parent_columns) - parent_known:
            raise DecodeError(f"{key.parent_table} foreign key has unknown columns")

    return ScriptDefinition(
        schemas=schemas,
        primary_keys=primary_keys,
        foreign_keys=tuple(foreign_keys),
        roots=tuple(roots),
    )


def parse_properties(text: str) -> dict[str, str]:
    """Parse the simple key/value subset used by HSQL ``db.properties``."""
    properties: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key.strip()] = value.strip()
    return properties


def decode_modified_utf8(raw: bytes) -> str:
    """Decode the Java modified UTF-8 form used by HSQLDB 1.8 strings."""
    code_units: list[int] = []
    pos = 0
    while pos < len(raw):
        lead = raw[pos]
        if 0x01 <= lead <= 0x7F:
            code_units.append(lead)
            pos += 1
            continue
        if 0xC0 <= lead <= 0xDF:
            if pos + 1 >= len(raw):
                raise DecodeError("truncated two-byte modified UTF-8 sequence")
            second = raw[pos + 1]
            if second & 0xC0 != 0x80:
                raise DecodeError("invalid modified UTF-8 continuation byte")
            code_units.append(((lead & 0x1F) << 6) | (second & 0x3F))
            pos += 2
            continue
        if 0xE0 <= lead <= 0xEF:
            if pos + 2 >= len(raw):
                raise DecodeError("truncated three-byte modified UTF-8 sequence")
            second, third = raw[pos + 1], raw[pos + 2]
            if second & 0xC0 != 0x80 or third & 0xC0 != 0x80:
                raise DecodeError("invalid modified UTF-8 continuation byte")
            code_units.append(
                ((lead & 0x0F) << 12)
                | ((second & 0x3F) << 6)
                | (third & 0x3F)
            )
            pos += 3
            continue
        raise DecodeError(f"invalid modified UTF-8 lead byte 0x{lead:02x}")

    utf16 = b"".join(struct.pack(">H", unit) for unit in code_units)
    return utf16.decode("utf-16-be", errors="surrogatepass")


def _take(payload: bytes, pos: int, count: int) -> tuple[bytes, int]:
    end = pos + count
    if count < 0 or end > len(payload):
        raise DecodeError("short row")
    return payload[pos:end], end


def parse_row(record: bytes, schema: Schema) -> Row:
    """Decode one aligned, node-less FCA RowOutputBinary record."""
    if len(record) < 8:
        raise DecodeError("short record")
    declared = struct.unpack_from(">I", record, 0)[0]
    if declared != len(record):
        raise DecodeError(
            f"record declares {declared} bytes but contains {len(record)}"
        )

    pos = 4
    row: Row = {}
    for name, kind in schema:
        marker_raw, pos = _take(record, pos, 1)
        marker = marker_raw[0]
        if marker == 0:
            row[name] = None
            continue
        if marker != 1:
            raise DecodeError(f"bad marker 0x{marker:02x} for {name}")

        if kind == "smallint":
            raw, pos = _take(record, pos, 2)
            row[name] = struct.unpack(">h", raw)[0]
        elif kind == "integer":
            raw, pos = _take(record, pos, 4)
            row[name] = struct.unpack(">i", raw)[0]
        elif kind == "bigint":
            raw, pos = _take(record, pos, 8)
            row[name] = struct.unpack(">q", raw)[0]
        elif kind == "double":
            raw, pos = _take(record, pos, 8)
            value = struct.unpack(">d", raw)[0]
            if not math.isfinite(value):
                raise DecodeError(f"non-finite floating-point value for {name}")
            row[name] = value
        elif kind == "boolean":
            raw, pos = _take(record, pos, 1)
            if raw[0] not in (0, 1):
                raise DecodeError(f"bad boolean 0x{raw[0]:02x} for {name}")
            row[name] = bool(raw[0])
        elif kind == "string":
            raw, pos = _take(record, pos, 4)
            byte_count = struct.unpack(">I", raw)[0]
            raw, pos = _take(record, pos, byte_count)
            row[name] = decode_modified_utf8(raw)
        else:
            raise DecodeError(f"unsupported row kind {kind!r}")

    if any(record[pos:]):
        raise DecodeError(f"nonzero trailing bytes at record offset +0x{pos:x}")
    return row


def split_records(
    data: bytes, *, start: int = DATA_HEADER_SIZE
) -> list[tuple[int, bytes]]:
    """Split aligned cache records from ``data`` and require exact EOF."""
    if len(data) < start:
        raise DecodeError(
            f"data file is {len(data)} bytes, shorter than {start}-byte header"
        )
    output: list[tuple[int, bytes]] = []
    pos = start
    while pos < len(data):
        if pos + 4 > len(data):
            raise DecodeError(f"truncated record header at 0x{pos:x}")
        size = struct.unpack_from(">I", data, pos)[0]
        if size < 8 or size % 8 or pos + size > len(data):
            raise DecodeError(f"invalid record size {size} at 0x{pos:x}")
        output.append((pos, data[pos : pos + size]))
        pos += size
    return output


def decode_database(
    data_path: Path, script_path: Path, properties_path: Path
) -> DecodedDatabase:
    """Read and decode one extracted FCA database without writing any file."""
    data = data_path.read_bytes()
    script_bytes = script_path.read_bytes()
    properties_bytes = properties_path.read_bytes()
    try:
        script_text = script_bytes.decode("ascii")
        properties_text = properties_bytes.decode("ascii")
    except UnicodeDecodeError as error:
        raise DecodeError("script and properties files must be ASCII") from error

    definition = parse_script(script_text)
    properties = parse_properties(properties_text)
    cache_version = properties.get("hsqldb.cache_version")
    if cache_version != FCA_CACHE_VERSION:
        raise DecodeError(
            f"unsupported hsqldb.cache_version={cache_version!r}; "
            f"expected the FCA custom marker {FCA_CACHE_VERSION!r}"
        )
    try:
        scale = int(properties["hsqldb.cache_file_scale"])
    except KeyError as error:
        raise DecodeError("missing hsqldb.cache_file_scale") from error
    except ValueError as error:
        raise DecodeError("invalid hsqldb.cache_file_scale") from error
    if scale <= 0:
        raise DecodeError("hsqldb.cache_file_scale must be positive")

    all_records = split_records(data)
    record_by_offset = {offset: record for offset, record in all_records}
    counts = {table: 0 for table in definition.schemas}
    positive_roots: dict[str, int] = {}
    for table, root in definition.roots:
        if root <= 0:
            continue
        offset = root * scale
        try:
            index_record = record_by_offset[offset]
        except KeyError as error:
            raise DecodeError(
                f"{table} root 0x{offset:x} is not a record boundary"
            ) from error
        if len(index_record) < 8:
            raise DecodeError(f"short index record for {table}")
        counts[table] = struct.unpack_from(">I", index_record, 4)[0]
        positive_roots[table] = offset

    total_rows = sum(counts.values())
    if total_rows > len(all_records):
        raise DecodeError(
            f"index counts claim {total_rows} rows but only "
            f"{len(all_records)} records exist"
        )
    index_record_count = len(all_records) - total_rows
    index_offsets = {
        offset for offset, _record in all_records[:index_record_count]
    }
    for table, offset in positive_roots.items():
        if offset not in index_offsets:
            raise DecodeError(
                f"{table} root 0x{offset:x} falls outside the index prefix"
            )

    row_records = all_records[index_record_count:]
    table_rows = {table: [] for table in definition.schemas}
    table_offsets = {table: [] for table in definition.schemas}
    cursor = 0
    for table, _root in definition.roots:
        count = counts[table]
        schema = definition.schemas[table]
        selected = row_records[cursor : cursor + count]
        if len(selected) != count:
            raise DecodeError(f"short row range for {table}")
        for offset, record in selected:
            try:
                row = parse_row(record, schema)
            except DecodeError as error:
                raise DecodeError(
                    f"{table} row at 0x{offset:x}: {error}"
                ) from error
            table_rows[table].append(row)
            table_offsets[table].append(offset)
        cursor += count
    if cursor != len(row_records):
        raise DecodeError(f"{len(row_records) - cursor} unassigned row records")

    metadata: dict[str, object] = {
        "format": "fca-hsqldb-1.8.0.x-node-less",
        "cache_version": cache_version,
        "cache_file_scale": scale,
        "file_size": len(data),
        "record_count": len(all_records),
        "index_record_count": index_record_count,
        "row_record_count": len(row_records),
        "row_start": row_records[0][0] if row_records else None,
        "row_end": len(data),
        "table_counts": counts,
        "index_root_offsets": positive_roots,
        "sha256": {
            "data": _sha256(data),
            "script": _sha256(script_bytes),
            "properties": _sha256(properties_bytes),
        },
    }
    return DecodedDatabase(
        definition=definition,
        rows=table_rows,
        row_offsets=table_offsets,
        metadata=metadata,
    )


def validate_database(database: DecodedDatabase) -> list[str]:
    """Validate every declared primary and foreign key in decoded rows."""
    checks: list[str] = []
    rows = database.rows
    definition = database.definition

    for table, columns in definition.primary_keys.items():
        values = [tuple(row[column] for column in columns) for row in rows[table]]
        if any(any(value is None for value in key) for key in values):
            raise DecodeError(f"null primary key in {table}")
        if len(values) != len(set(values)):
            raise DecodeError(f"duplicate primary key in {table}")
        checks.append(f"{table} primary key unique ({len(values)} rows)")

    for key in definition.foreign_keys:
        parent_values = {
            tuple(row[column] for column in key.parent_columns)
            for row in rows[key.parent_table]
        }
        missing: set[tuple[Scalar, ...]] = set()
        for row in rows[key.child_table]:
            child_value = tuple(row[column] for column in key.child_columns)
            if any(value is None for value in child_value):
                continue
            if child_value not in parent_values:
                missing.add(child_value)
        if missing:
            raise DecodeError(
                f"{key.child_table}.{','.join(key.child_columns)} has "
                f"{len(missing)} missing {key.parent_table} references"
            )
        checks.append(
            f"{key.child_table}.{','.join(key.child_columns)} -> "
            f"{key.parent_table}.{','.join(key.parent_columns)} valid"
        )
    return checks


def _unescape_property(value: str) -> str:
    output: list[str] = []
    pos = 0
    escapes = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
    while pos < len(value):
        char = value[pos]
        if char != "\\":
            output.append(char)
            pos += 1
            continue
        pos += 1
        if pos >= len(value):
            output.append("\\")
            break
        escaped = value[pos]
        if escaped == "u":
            digits = value[pos + 1 : pos + 5]
            if len(digits) != 4 or not all(
                digit in "0123456789abcdefABCDEF" for digit in digits
            ):
                raise DecodeError("invalid Unicode escape in label properties")
            output.append(chr(int(digits, 16)))
            pos += 5
            continue
        output.append(escapes.get(escaped, escaped))
        pos += 1
    return "".join(output)


def parse_label_properties(text: str) -> dict[int, str]:
    """Parse numeric keys from an ISO-8859-1 Java label-properties file."""
    labels: dict[int, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.lstrip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.isdigit():
            labels[int(key)] = _unescape_property(value)
    return labels


def load_labels(path: Path) -> tuple[dict[int, str], str]:
    payload = path.read_bytes()
    return parse_label_properties(payload.decode("latin-1")), _sha256(payload)


def label_summary(
    rows: dict[str, list[Row]], labels: dict[int, str]
) -> dict[str, object]:
    references: list[int] = []
    for table_rows in rows.values():
        for row in table_rows:
            references.extend(
                value
                for column, value in row.items()
                if column.endswith("NAME_ID")
                and isinstance(value, int)
                and not isinstance(value, bool)
            )
    missing = sorted(set(references) - labels.keys())
    return {
        "reference_count": len(references),
        "unique_reference_count": len(set(references)),
        "resolved_reference_count": sum(value in labels for value in references),
        "missing_reference_ids": missing,
    }


def _index_rows(
    rows: Iterable[Row], key: str
) -> dict[Scalar, Row]:
    return {row[key]: row for row in rows}


def joined_command_report(
    rows: dict[str, list[Row]],
    labels: dict[int, str],
    xmits: Sequence[str],
) -> dict[str, object]:
    """Join selected ``XMIT_STR`` commands to fields, labels, and conversions."""
    required = {
        "COM_SER_VAR_VER": {"ID", "XMIT_STR", "COM_SER_NAME_ID"},
        "MSG": {"COM_SER_VAR_VER_ID", "SER_MSG_ID", "IS_REQ", "BIT_POS"},
    }
    for table, columns in required.items():
        if table not in rows:
            raise DecodeError(f"--xmit requires table {table}")
        if rows[table] and not columns.issubset(rows[table][0]):
            raise DecodeError(f"--xmit requires columns {sorted(columns)} in {table}")

    def label(label_id: Scalar) -> str | None:
        if isinstance(label_id, int) and not isinstance(label_id, bool):
            return labels.get(label_id)
        return None

    used_label_ids: dict[str, set[int]] = {
        "command": set(),
        "dde": set(),
        "unit": set(),
        "encoding": set(),
    }

    def remember_label(category: str, label_id: Scalar) -> None:
        if isinstance(label_id, int) and not isinstance(label_id, bool):
            used_label_ids[category].add(label_id)

    linear_by_id: dict[Scalar, list[dict[str, object]]] = defaultdict(list)
    for row in rows.get("LINEAR_CONV", []):
        item: dict[str, object] = dict(row)
        item["unit_label"] = label(row.get("UNIT_NAME_ID"))
        linear_by_id[row["LINEAR_CONV_ID"]].append(item)

    encoding_by_id = {
        row["ID"]: {**row, "name_label": label(row.get("NAME_ID"))}
        for row in rows.get("ENCODING", [])
    }
    linear_ids_by_encoding: dict[Scalar, list[Scalar]] = defaultdict(list)
    for row in rows.get("ENCODING_TO_LINEAR_CONV", []):
        linear_ids_by_encoding[row["ENCODING_ID"]].append(row["LINEAR_CONV_ID"])

    table_by_id: dict[Scalar, list[dict[str, object]]] = defaultdict(list)
    for row in rows.get("ENCODING_SEQ", []):
        item = dict(row)
        item["encoding"] = encoding_by_id.get(row["ENCODING_ID"])
        item["linear_conversions"] = [
            linear
            for linear_id in linear_ids_by_encoding.get(row["ENCODING_ID"], [])
            for linear in linear_by_id.get(linear_id, [])
        ]
        table_by_id[row["TABLE_CONV_ID"]].append(item)
    for entries in table_by_id.values():
        entries.sort(
            key=lambda item: (
                item.get("SEQ") if item.get("SEQ") is not None else -1,
                item["ENCODING_ID"],
            )
        )

    string_encoding_by_id = {
        row["ID"]: {**row, "name_label": label(row.get("NAME_ID"))}
        for row in rows.get("STR_ENCODING", [])
    }
    string_table_by_id: dict[Scalar, list[dict[str, object]]] = defaultdict(list)
    for row in rows.get("STR_ENCODING_SEQ", []):
        item = dict(row)
        item["encoding"] = string_encoding_by_id.get(row["STR_ENCODING_ID"])
        string_table_by_id[row["STR_TABLE_CONV_ID"]].append(item)
    for entries in string_table_by_id.values():
        entries.sort(
            key=lambda item: (
                item.get("SEQ") if item.get("SEQ") is not None else -1,
                item["STR_ENCODING_ID"],
            )
        )

    identical_by_id = _index_rows(rows.get("IDENTICAL_CONV", []), "ID")
    algorithmic_by_id = _index_rows(rows.get("ALG_CONV", []), "ID")
    qualifiers_by_id: dict[Scalar, list[Row]] = defaultdict(list)
    for row in rows.get("QUAL_SET", []):
        qualifiers_by_id[row["ID"]].append(row)

    messages_by_command: dict[Scalar, list[Row]] = defaultdict(list)
    for row in rows["MSG"]:
        messages_by_command[row["COM_SER_VAR_VER_ID"]].append(row)

    used: dict[str, set[Scalar]] = {
        "qualifiers": set(),
        "linear": set(),
        "table": set(),
        "string_table": set(),
        "identical": set(),
        "algorithmic": set(),
    }
    reference_columns = {
        "QUAL_SET_ID": "qualifiers",
        "LINEAR_CONV_ID": "linear",
        "TABLE_CONV_ID": "table",
        "STR_TABLE_CONV_ID": "string_table",
        "IDENTICAL_CONV_ID": "identical",
        "ALG_CONV_ID": "algorithmic",
    }

    wanted_order = list(dict.fromkeys(str(xmit).upper() for xmit in xmits))
    wanted = set(wanted_order)
    matched: set[str] = set()
    commands: list[dict[str, object]] = []
    for row in rows["COM_SER_VAR_VER"]:
        xmit = str(row["XMIT_STR"]).upper()
        if xmit not in wanted:
            continue
        matched.add(xmit)
        command: dict[str, object] = dict(row)
        command["name_label"] = label(row.get("COM_SER_NAME_ID"))
        remember_label("command", row.get("COM_SER_NAME_ID"))
        command_messages: list[dict[str, object]] = []
        for message_row in sorted(
            messages_by_command[row["ID"]],
            key=lambda item: (
                not bool(item.get("IS_REQ")),
                item.get("BIT_POS")
                if isinstance(item.get("BIT_POS"), int)
                else -1,
                item["SER_MSG_ID"],
            ),
        ):
            message: dict[str, object] = dict(message_row)
            message["dde_label"] = label(message_row.get("DDE_NAME_ID"))
            remember_label("dde", message_row.get("DDE_NAME_ID"))
            for column, category in reference_columns.items():
                value = message_row.get(column)
                if value is not None:
                    used[category].add(value)
            command_messages.append(message)
        command["messages"] = command_messages
        commands.append(command)

    resolved: dict[str, dict[str, object]] = {
        "qualifiers": {
            str(key): qualifiers_by_id.get(key, [])
            for key in sorted(used["qualifiers"])
        },
        "linear": {
            str(key): linear_by_id.get(key, [])
            for key in sorted(used["linear"])
        },
        "table": {
            str(key): table_by_id.get(key, [])
            for key in sorted(used["table"])
        },
        "string_table": {
            str(key): string_table_by_id.get(key, [])
            for key in sorted(used["string_table"])
        },
        "identical": {
            str(key): identical_by_id.get(key)
            for key in sorted(used["identical"])
        },
        "algorithmic": {
            str(key): algorithmic_by_id.get(key)
            for key in sorted(used["algorithmic"])
        },
    }
    unresolved = {
        category: [
            key
            for key in sorted(keys)
            if not resolved[category].get(str(key))
        ]
        for category, keys in used.items()
    }
    for key in used["linear"]:
        for linear in linear_by_id.get(key, []):
            remember_label("unit", linear.get("UNIT_NAME_ID"))
    for key in used["table"]:
        for item in table_by_id.get(key, []):
            encoding = item.get("encoding")
            if isinstance(encoding, dict):
                remember_label("encoding", encoding.get("NAME_ID"))
            conversions = item.get("linear_conversions")
            if isinstance(conversions, list):
                for linear in conversions:
                    if isinstance(linear, dict):
                        remember_label("unit", linear.get("UNIT_NAME_ID"))
    for key in used["string_table"]:
        for item in string_table_by_id.get(key, []):
            encoding = item.get("encoding")
            if isinstance(encoding, dict):
                remember_label("encoding", encoding.get("NAME_ID"))
    unresolved_label_ids = {
        category: sorted(label_ids - labels.keys())
        for category, label_ids in used_label_ids.items()
    }

    return {
        "requested_xmits": wanted_order,
        "unmatched_xmits": [
            xmit for xmit in wanted_order if xmit not in matched
        ],
        "ecu": rows.get("ECU", []),
        "ecu_to_bus": rows.get("ECU_TO_BUS", []),
        "var_ver": rows.get("VAR_VER", []),
        "commands": commands,
        "resolved_conversions": resolved,
        "unresolved_references": unresolved,
        "unresolved_label_ids": unresolved_label_ids,
    }


def summary_report(
    database: DecodedDatabase,
    validation_checks: list[str],
    labels: dict[int, str] | None = None,
    label_sha256: str | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "mode": "summary",
        "metadata": database.metadata,
        "validation_checks": validation_checks,
    }
    if labels is not None:
        report["labels"] = {
            "sha256": label_sha256,
            **label_summary(database.rows, labels),
        }
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read an extracted FCA custom HSQL cache database and emit "
            "validated JSON to stdout. Inputs are never modified."
        ),
        epilog=(
            "Without --xmit, summary mode is used. This decoder deliberately "
            "rejects stock/spoofed cache versions and does not execute any "
            "diagnostic command represented by XMIT_STR."
        ),
    )
    parser.add_argument("data", type=Path, help="extracted db.data input")
    parser.add_argument(
        "--script", type=Path, required=True, help="matching db.script input"
    )
    parser.add_argument(
        "--properties",
        type=Path,
        required=True,
        help="matching, unmodified db.properties input",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        help="optional matching Java Label.properties input",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--summary",
        action="store_true",
        help="emit metadata, validation checks, and optional label coverage",
    )
    mode.add_argument(
        "--xmit",
        action="append",
        metavar="HEX",
        help=(
            "emit command/message/label/conversion joins for one exact "
            "XMIT_STR; repeat for multiple strings"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        database = decode_database(args.data, args.script, args.properties)
        checks = validate_database(database)
        labels: dict[int, str] | None = None
        label_sha256: str | None = None
        if args.labels:
            labels, label_sha256 = load_labels(args.labels)

        if args.xmit:
            report = joined_command_report(
                database.rows, labels or {}, args.xmit
            )
            report["mode"] = "xmit"
            report["metadata"] = database.metadata
            report["validation_checks"] = checks
            if label_sha256 is not None:
                report["label_sha256"] = label_sha256
        else:
            report = summary_report(
                database, checks, labels, label_sha256
            )
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except (DecodeError, OSError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
