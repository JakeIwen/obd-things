"""The owner's custom early-warning rules file: tolerant read, one durable write per operation.

File shape: ``{"version": 1, "rules": [row, ...], "removed": ["<64 hex>", ...]}``. A row is
``{id, rule, approved_at, chat_id, owner, ...}``; only ``id`` and ``rule`` are interpreted here.
``removed`` holds the ids of removed rules, newest last, so a retried install of a removed rule
can be refused with one file and one write. Unknown top-level keys are kept verbatim.

File-level problems (symlink, over the size bound, not version 1, rules not a list, more rows
than the cap, duplicate ids) raise ``ValueError``: nothing in such a file is trusted. A row that
fails validation alone is returned in ``rejected``, whole, and every write keeps it, so one bad
row never disables the others and is never lost. Contract: ``docs/custom-warnings.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re

from projects.vehicle_data import custom_warning_contract as contract
from projects.vehicle_data.engine_off_voltage import _atomic_json

ROW_ID = re.compile(r"[0-9a-f]{64}\Z")


class RemovedRule(ValueError):
    """The id was installed once and has since been removed."""


class RulesFull(ValueError):
    """The file already holds the maximum number of rules."""


@dataclass
class RulesFile:
    rows: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    other: dict = field(default_factory=dict)

    def ids(self):
        return {row["id"] for row in self.rows}


def _row_valid(row):
    from projects.vehicle_data.custom_warnings import validate_rule
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not ROW_ID.match(row["id"]):
        return False
    try:
        validate_rule(row.get("rule"))
    except (ValueError, TypeError, KeyError):
        return False
    return True


def _row_id(row):
    identifier = row.get("id") if isinstance(row, dict) else None
    return identifier if isinstance(identifier, str) else None


def read(path) -> RulesFile:
    """Read the file; an absent file is empty. Rows keep their order (valid and rejected)."""
    path = Path(path)
    if not os.path.lexists(path):
        return RulesFile()
    if path.is_symlink() or path.stat().st_size > contract.MAX_FILE_BYTES:
        raise ValueError("Invalid warning configuration file")
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("rules"), list):
        raise ValueError("Invalid warning configuration")
    rows = payload["rules"]
    ids = [_row_id(row) for row in rows if _row_id(row) is not None]
    if len(rows) > contract.MAX_RULES or len(set(ids)) != len(ids):
        raise ValueError("Too many or duplicate custom warnings")
    removed = payload.get("removed", [])
    removed = [item for item in removed if isinstance(item, str) and ROW_ID.match(item)] if isinstance(removed, list) else []
    result = RulesFile(removed=removed,
                       other={key: value for key, value in payload.items() if key not in ("version", "rules", "removed")})
    for row in rows:
        (result.rows if _row_valid(row) else result.rejected).append(row)
    return result


def serialize(rules_file: RulesFile) -> dict:
    """The JSON object ``write`` stores: rows (rejected ones too) verbatim, newest removals kept."""
    present = {_row_id(row) for row in (*rules_file.rows, *rules_file.rejected)}
    removed = [item for item in rules_file.removed if item not in present]
    removed = list(dict.fromkeys(reversed(removed)))[::-1][-contract.MAX_REMOVED_IDS:]
    return {**rules_file.other, "version": 1, "rules": [*rules_file.rows, *rules_file.rejected],
            "removed": removed}


def write(path, rules_file: RulesFile) -> None:
    """Replace the file atomically and durably (temp file fsync, rename, directory fsync).

    Refuses, before writing anything, a file over the size bound or the row cap.
    """
    path = Path(path)
    payload = serialize(rules_file)
    if len(payload["rules"]) > contract.MAX_RULES:
        raise RulesFull("Custom warning limit reached; remove an existing warning first")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    if len(encoded.encode()) > contract.MAX_FILE_BYTES:
        raise ValueError("The saved warnings file would be too large; remove a warning first")
    _atomic_json(path, payload)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def add(path, row) -> bool:
    """Append ``row`` unless its id is present; False when it already was (a replay).

    Raises ``RemovedRule`` for an id in ``removed`` and ``RulesFull`` at the cap.
    """
    rules_file = read(path)
    if row["id"] in rules_file.ids() or row["id"] in {_row_id(item) for item in rules_file.rejected}:
        return False
    if row["id"] in rules_file.removed:
        raise RemovedRule("This warning was added and has since been removed; review it again to add it once more.")
    if len(rules_file.rows) + len(rules_file.rejected) >= contract.MAX_RULES:
        raise RulesFull("Custom warning limit reached; remove an existing warning first")
    rules_file.rows.append(row)
    write(path, rules_file)
    return True


def remove(path, identifier, *, owner=None) -> bool:
    """Drop the row with ``identifier`` (rejected rows too) and record the id as removed.

    With ``owner``, a row saved by another browser is left alone and nothing is written. An id
    that matches no row is still recorded, so a later retried install of it is refused.
    """
    rules_file = read(path)
    every = [*rules_file.rows, *rules_file.rejected]
    match = [row for row in every if _row_id(row) == identifier]
    if owner is not None and any(row.get("owner") != owner for row in match):
        return False
    rules_file.rows = [row for row in rules_file.rows if _row_id(row) != identifier]
    rules_file.rejected = [row for row in rules_file.rejected if _row_id(row) != identifier]
    rules_file.removed = [item for item in rules_file.removed if item != identifier] + [identifier]
    write(path, rules_file)
    return bool(match)
