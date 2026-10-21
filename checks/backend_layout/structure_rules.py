"""The ratcheted structure rules: contract, prefix-group, entry-heavy, kind-folder and cycle.
Each takes the inventory and import graph and returns findings; baseline and exceptions are
applied afterwards. Test files and React UI files (.tsx/.jsx) are never subjects here."""
import ast
import itertools
import os
import posixpath
import re
from typing import Dict, List, Optional, Set, Tuple

from . import cochange, git_adapter, import_graph
from .config import Settings, matches_any, path_matches
from .edges import JS, LAZY, MODULE, PYTHON, Graph
from .files import GO, SHELL, Inventory, SourceFile, python_tree
from .findings import ADVISORY, Finding

KIND_FOLDERS = {"util", "utils", "helper", "helpers", "common", "misc"}
PREFIX = re.compile(r"^([a-z0-9]{2,})([_-])(.+)$")
JS_MAIN_GUARD = re.compile(r"require\.main\s*===\s*module|import\.meta\.main\b"
                           r"|process\.argv\[1\][^\n]*(import\.meta\.url|fileURLToPath|modulePath|__filename)"
                           r"|(import\.meta\.url|fileURLToPath\()[^\n]*process\.argv\[1\]")
GO_MAIN = re.compile(r"^package\s+main\b", re.MULTILINE)
# react-app's check-structure defaults (its STRUCTURE_DEFAULTS), used when its config block is absent.
REACT_STRUCTURE_DEFAULTS = {"sourceRoot": "src", "contracts": [
    {"from": "content", "to": ["components", "pages"]}, {"from": "components", "to": ["pages"]},
    {"from": "*", "to": ["dev"], "allowDynamic": True}]}


def layout_subjects(inventory: Inventory) -> List[SourceFile]:
    return [f for f in inventory.subjects if not f.test and not f.ui]


# ---------- contract ----------

def react_structure(settings: Settings, inventory: Inventory) -> Optional[dict]:
    """react-app's structure block when its check-structure is installed in this repo, else None."""
    installed = settings.structure is not None or any(p.endswith("check-structure.mjs") for p in inventory.files)
    return dict(REACT_STRUCTURE_DEFAULTS, **(settings.structure or {})) if installed else None


def contract_signature(source: str, targets) -> Tuple[str, frozenset]:
    norm = posixpath.normpath
    return (source if source == "*" else norm(source), frozenset(norm(t) for t in targets))


def active_contracts(settings: Settings, structure: Optional[dict]) -> List[dict]:
    if structure is None:
        return list(settings["contracts"])
    base = structure.get("sourceRoot", "src")
    theirs = {contract_signature(c["from"] if c["from"] == "*" else posixpath.join(base, c["from"]),
                                 [posixpath.join(base, t) for t in c["to"]]) for c in structure.get("contracts", [])}
    kept = []
    for index, contract in enumerate(settings["contracts"]):
        if contract_signature(contract["from"], contract["to"]) in theirs:
            settings.warnings.append(f"layout.contracts[{index}] duplicates a react-app structure contract; "
                                     f"check-structure reports it, so this check skips it")
        else:
            kept.append(contract)
    return kept


def contract_findings(graph: Graph, contracts: List[dict]) -> List[Finding]:
    out, seen = [], set()
    for edge in graph.edges:
        for contract in contracts:
            targets = contract["to"]
            applies = (not matches_any(edge.source, targets)) if contract["from"] == "*" \
                else path_matches(edge.source, contract["from"])
            if not applies or not matches_any(edge.target, targets):
                continue
            if edge.kind == LAZY and contract.get("allowDynamic"):
                continue
            if (edge.source, edge.target) in seen:
                continue
            seen.add((edge.source, edge.target))
            source = "everything outside " + ", ".join(targets) if contract["from"] == "*" else contract["from"]
            out.append(Finding("contract", (edge.source, edge.target), f"{source} must not import "
                               f"{', '.join(targets)} ({edge.kind} import of {edge.target})", line=edge.line))
    return out


# ---------- prefix-group ----------

def stutters(folder: str, prefix: str) -> bool:
    """The folder already carries the prefix (backup/backup_*.sh, task-kinds/task-*.ts)."""
    name = posixpath.basename(folder).lower()
    return name == prefix or name.startswith((prefix + "_", prefix + "-"))


def prefix_groups(inventory: Inventory, minimum: int) -> Dict[Tuple[str, str], List[str]]:
    """(folder, prefix) -> members. A file named exactly the prefix (api.py beside api_*.py) joins."""
    groups: Dict[Tuple[str, str], List[str]] = {}
    bare: Dict[Tuple[str, str], str] = {}
    for f in layout_subjects(inventory):
        name = posixpath.basename(f.path).split(".")[0].lower()
        m = PREFIX.match(name)
        if m:
            groups.setdefault((f.folder, m.group(1)), []).append(f.path)
        else:
            bare[(f.folder, name)] = f.path
    for key, path in bare.items():
        if key in groups:
            groups[key].append(path)
    return {k: sorted(v) for k, v in groups.items() if len(v) >= minimum and not stutters(k[0], k[1])
            and not os.path.isdir(os.path.join(inventory.root, k[0], k[1]))}


def changed_together(members: Set[str], commits) -> int:
    return sum(1 for files in commits if len(files) <= cochange.MAX_FILES and len(members & set(files)) >= 2)


def cohesion(members: List[str], graph: Graph, inventory: Inventory, commits) -> str:
    group = set(members)
    internal = sum(1 for e in graph.edges if e.source in group and e.target in group)
    callers = {e.source for e in graph.edges if e.target in group and e.source not in group
               and not inventory.files[e.source].test}
    targets: Dict[str, Set[str]] = {}
    for e in graph.edges:
        if e.source in group and e.target not in group:
            targets.setdefault(e.target, set()).add(e.source)
    shared = sum(1 for importers in targets.values() if len(importers) >= 2)
    together = changed_together(group, commits)
    return (f"cohesion: {internal} import(s) within the group, {shared} shared import target(s), "
            f"{len(callers)} outside caller(s), {together} commit(s) changing 2+ of them")


def prefix_group_findings(inventory: Inventory, graph: Graph, settings: Settings) -> List[Finding]:
    groups = prefix_groups(inventory, settings.thresholds["prefixGroup"])
    commits = git_adapter.recent_commits(inventory.root) if groups else []
    out = []
    for (folder, prefix), members in sorted(groups.items()):
        label = posixpath.join(folder, prefix + "*") if folder else prefix + "*"
        message = (f"{label}: {len(members)} files share the prefix and there is no {prefix}/ folder; "
                   f"{cohesion(members, graph, inventory, commits)}")
        out += [Finding("prefix-group", (m,), message, group=label) for m in members]
    return out


# ---------- entry-heavy ----------

def has_main_guard(source: SourceFile, inventory: Inventory) -> bool:
    if source.kind == PYTHON:
        tree = python_tree(source, inventory)
        return bool(tree) and any(is_python_main_guard(node) for node in tree.body)
    if source.kind == JS:
        return source.text.startswith("#!") or bool(JS_MAIN_GUARD.search(source.text))
    return source.kind == GO and bool(GO_MAIN.search(source.text))


def is_python_main_guard(node) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    sides = [node.test.left] + list(node.test.comparators)
    names = {s.id for s in sides if isinstance(s, ast.Name)}
    values = {s.value for s in sides if isinstance(s, ast.Constant)}
    return names == {"__name__"} and values == {"__main__"}


def entry_heavy_findings(inventory: Inventory, settings: Settings) -> List[Finding]:
    limit, out = settings.thresholds["entryLines"], []
    for f in layout_subjects(inventory):
        if f.kind == SHELL or f.lines <= limit or matches_any(f.path, settings["entries"]):
            continue
        if has_main_guard(f, inventory):
            out.append(Finding("entry-heavy", (f.path,), f"{f.lines} lines with a main guard (limit {limit}): keep "
                               f"the entry thin and move the work into modules, or list a standalone tool in "
                               f"layout.entries"))
    return out


# ---------- kind-folder ----------

def kind_folder_findings(inventory: Inventory) -> List[Finding]:
    folders: Dict[str, Set[str]] = {}
    for f in layout_subjects(inventory):
        parts = f.path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            folders.setdefault("/".join(parts[:depth]), set()).add(f.kind if depth == len(parts) else "")
    out = []
    for folder, direct_kinds in sorted(folders.items()):
        name = posixpath.basename(folder).lower()
        if name in KIND_FOLDERS:
            out.append(Finding("kind-folder", (folder,), f"{folder}/ is named for a kind of code, not a feature; "
                               f"name shared code for what it does"))
        elif name == "sh" and PYTHON in direct_kinds:
            out.append(Finding("kind-folder", (folder,), f"{folder}/ is a shell folder holding Python; extend a "
                               f"documented compatibility layout through its owners or declare it as an exception"))
    return out


# ---------- cycle ----------

def cycle_findings(graph: Graph, inventory: Inventory, structure: Optional[dict]) -> List[Finding]:
    subjects = {f.path for f in inventory.subjects}
    skip_js = (structure or {}).get("sourceRoot") if structure else None
    graphs: Dict[str, Dict[str, Set[str]]] = {PYTHON: {}, JS: {}}
    for e in graph.edges:
        if e.kind != MODULE or e.source not in subjects or e.target not in subjects:
            continue
        if e.lang == JS and skip_js and path_matches(e.source, skip_js):
            continue  # react-app's check-structure reports cycles inside its source root
        graphs[e.lang].setdefault(e.source, set()).add(e.target)
    out = []
    for lang, edges in graphs.items():
        for members in import_graph.cycles(edges):
            treatment = "" if lang == PYTHON else ADVISORY
            out.append(Finding("cycle", tuple(members), f"{lang} import cycle among {len(members)} modules: "
                               + ", ".join(itertools.islice(members, 6)) + (" ..." if len(members) > 6 else ""),
                               treatment=treatment))
    return out
