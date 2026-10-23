"""The Contracts table in the repo's AGENTS.md: one row per path something outside the repo calls,
naming the caller and the owner. The first Markdown table under the configured heading is read;
its path column is the one headed "Path" (else the first), caller and owner columns are found by name.
"""
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .references import RepoIndex, Reference, reference_strings

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
SEPARATOR_ROW = re.compile(r"^\|?\s*:?-{2,}")


@dataclass
class Row:
    line: int
    cell: str
    caller: str
    owner: str
    references: List[Reference] = field(default_factory=list)


@dataclass
class Table:
    file: str
    found: bool
    note: str = ""
    rows: List[Row] = field(default_factory=list)
    has_caller: bool = False
    has_owner: bool = False

    def listed(self) -> List[str]:
        return [r.path for row in self.rows for r in row.references if r.kind == "repo"]

    def lists(self, path: str) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.listed())


def split_row(line: str) -> List[str]:
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [c.strip().replace("\\|", "|") for c in cells]


def clean(cell: str) -> str:
    return re.sub(r"<br\s*/?>", " ", cell.replace("`", "")).strip()


def column(header: List[str], word: str, default: Optional[int] = None) -> Optional[int]:
    return next((i for i, name in enumerate(header) if word in name), default)


def cell_at(cells: List[str], index: Optional[int]) -> str:
    return clean(cells[index]) if index is not None and index < len(cells) else ""


def section_lines(lines: List[str], heading: str):
    start = level = None
    for number, line in enumerate(lines):
        m = HEADING.match(line)
        if not m:
            continue
        if start is not None and len(m.group(1)) <= level:
            return start, number
        if start is None and clean(m.group(2)).lower() == heading.lower():
            start, level = number + 1, len(m.group(1))
    return (start, len(lines)) if start is not None else (None, None)


def first_table(lines: List[str], start: int, end: int):
    rows = []
    for number in range(start, end):
        if lines[number].lstrip().startswith("|"):
            rows.append((number, lines[number]))
        elif rows:
            break
    return rows


def read(root: str, settings_table: dict, index: RepoIndex) -> Table:
    rel, heading = settings_table["file"], settings_table["heading"]
    table = Table(file=rel, found=False)
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        table.note = f"{rel} not found"
        return table
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    start, end = section_lines(lines, heading)
    if start is None:
        table.note = f"{rel} has no section headed \"{heading}\""
        return table
    rows = first_table(lines, start, end)
    if len(rows) < 2 or not SEPARATOR_ROW.match(rows[1][1].strip()):
        table.note = f"{rel} section \"{heading}\" has no Markdown table"
        return table
    header = [c.lower() for c in split_row(rows[0][1])]
    path_col, caller_col, owner_col = column(header, "path", 0), column(header, "caller"), column(header, "owner")
    table.found, table.has_caller, table.has_owner = True, caller_col is not None, owner_col is not None
    for number, line in rows[2:]:
        cells = split_row(line)
        row = Row(line=number + 1, cell=cell_at(cells, path_col), caller=cell_at(cells, caller_col),
                  owner=cell_at(cells, owner_col))
        row.references = reference_strings(row.cell, index) if row.cell else []
        table.rows.append(row)
    return table
