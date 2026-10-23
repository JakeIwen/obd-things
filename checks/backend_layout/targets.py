"""declared-target: the Contracts table against what the repo's units, plists, package scripts and
manifests actually run. Files are found with layout.targets globs and read by type: systemd units
(Exec* lines, WorkingDirectory), launchd plists (Program, ProgramArguments, WorkingDirectory),
package.json (scripts, bin), and any other file as a manifest (JSON strings or text lines)."""
import json
import os
import plistlib
import posixpath
import re
from dataclasses import dataclass, field
from typing import Dict, List

from .config import Settings, matches_any
from .contracts_table import Table
from .findings import ADVISORY, Finding
from .references import (DYNAMIC, MISSING, REPO, ROOT, Reference, RepoIndex, Workdir, command_references,
                         reference_strings)

UNIT_EXT = (".service", ".socket", ".timer", ".path", ".mount")
EXEC_LINE = re.compile(r"^\s*(Exec(?:Start|StartPre|StartPost|Stop|StopPost|Reload|Condition))\s*=\s*(.*)$")
WORKDIR_LINE = re.compile(r"^\s*WorkingDirectory\s*=\s*-?(.*?)\s*$")


@dataclass
class Targets:
    files: List[str] = field(default_factory=list)  # unit, plist, package and manifest files read
    found: Dict[str, List[str]] = field(default_factory=dict)  # repo path -> callers ("file: what")
    missing: Dict[str, List[str]] = field(default_factory=dict)
    dynamic: List[str] = field(default_factory=list)  # "caller: reference (why)", check by hand
    errors: List[str] = field(default_factory=list)

    def add(self, caller: str, refs: List[Reference]):
        for ref in refs:
            if ref.kind == REPO:
                self.found.setdefault(ref.path, []).append(caller)
            elif ref.kind == MISSING:
                self.missing.setdefault(ref.path, []).append(caller)
            elif ref.kind == DYNAMIC:
                self.dynamic.append(f"{caller}: {ref.path}" + (f" ({ref.hint})" if ref.hint else ""))


def read_unit(rel: str, text: str, index: RepoIndex, out: Targets):
    where = ROOT
    for line in text.splitlines():
        m = WORKDIR_LINE.match(line)
        if m and m.group(1):
            where = index.working_directory(m.group(1))
    for number, line in enumerate(text.splitlines(), 1):
        m = EXEC_LINE.match(line)
        if m:
            command = m.group(2).lstrip("@-:+!")
            out.add(f"{rel}:{number} {m.group(1)}", command_references(command, index, where))


def read_plist(rel: str, raw: bytes, index: RepoIndex, out: Targets):
    try:
        data = plistlib.loads(raw)
    except Exception as e:  # plistlib raises several types for malformed input
        out.errors.append(f"{rel}: unreadable plist ({e.__class__.__name__})")
        return
    if not isinstance(data, dict):
        return
    workdir = str(data.get("WorkingDirectory", ""))
    where = index.working_directory(workdir) if workdir else ROOT
    argv = data.get("ProgramArguments") or ([data["Program"]] if data.get("Program") else [])
    if argv:
        out.add(f"{rel} ProgramArguments", command_references([str(a) for a in argv], index, where))


def read_package(rel: str, raw: bytes, index: RepoIndex, out: Targets):
    try:
        data = json.loads(raw)
    except ValueError as e:
        out.errors.append(f"{rel}: {e}")
        return
    where = Workdir(posixpath.dirname(rel))
    for name, command in sorted((data.get("scripts") or {}).items()):
        if isinstance(command, str):
            out.add(f"{rel} scripts.{name}", command_references(command, index, where))
    bins = data.get("bin")
    bins = {data.get("name", "bin"): bins} if isinstance(bins, str) else bins or {}
    for name, path in sorted(bins.items()):
        out.add(f"{rel} bin.{name}", reference_strings(str(path), index, where))


def json_strings(value, trail=""):
    if isinstance(value, str):
        yield trail, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from json_strings(item, f"{trail}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from json_strings(item, f"{trail}.{key}" if trail else str(key))


def read_manifest(rel: str, raw: bytes, index: RepoIndex, out: Targets):
    text = raw.decode("utf-8", "replace")
    try:
        items = list(json_strings(json.loads(text))) if rel.endswith(".json") else None
    except ValueError as e:
        out.errors.append(f"{rel}: {e}")
        return
    if items is None:
        items = [(f"line {n}", line.strip()) for n, line in enumerate(text.splitlines(), 1)
                 if line.strip() and not line.lstrip().startswith("#")]
    for where, value in items:
        out.add(f"{rel} {where}", reference_strings(value, index))


def collect(settings: Settings, paths: List[str], index: RepoIndex) -> Targets:
    out = Targets()
    layout = settings.layout
    for rel in paths:
        if not matches_any(rel, layout["targets"]) or matches_any(rel, layout["exclude"]):
            continue
        try:
            with open(os.path.join(settings.root, rel), "rb") as fh:
                raw = fh.read()
        except OSError as e:
            out.errors.append(f"{rel}: {e}")
            continue
        out.files.append(rel)
        name = posixpath.basename(rel)
        if rel.endswith(UNIT_EXT):
            read_unit(rel, raw.decode("utf-8", "replace"), index, out)
        elif rel.endswith(".plist"):
            read_plist(rel, raw, index, out)
        elif name == "package.json":
            read_package(rel, raw, index, out)
        else:
            read_manifest(rel, raw, index, out)
    return out


def findings(table: Table, targets: Targets) -> List[Finding]:
    """Without a table, unlisted targets are reported as advisory (what the table would need to list)."""
    out = []
    treatment = "" if table.found else ADVISORY
    suffix = "" if table.found else " (advisory: no Contracts table yet)"
    for row in table.rows:
        for ref in row.references:
            if ref.kind == MISSING:
                out.append(Finding("declared-target", (ref.path,), f"{table.file}:{row.line} lists {ref.path}, "
                                   f"which does not exist", line=row.line))
        if (table.has_caller and not row.caller) or (table.has_owner and not row.owner):
            out.append(Finding("declared-target", (row.cell or f"{table.file}:{row.line}",),
                               f"{table.file}:{row.line} names no caller or no owner", line=row.line))
    for path, callers in sorted(targets.missing.items()):
        out.append(Finding("declared-target", (path, callers[0].split(" ")[0]),
                           f"{callers[0]} runs {path}, which does not exist", treatment=treatment))
    for path, callers in sorted(targets.found.items()):
        if not table.lists(path):
            more = f" (and {len(callers) - 1} more)" if len(callers) > 1 else ""
            out.append(Finding("declared-target", (path, callers[0].split(" ")[0]),
                               f"{callers[0]}{more} runs {path}; the Contracts table does not list it{suffix}",
                               treatment=treatment))
    return out
