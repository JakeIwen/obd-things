"""Import edges between the repo's own files, resolved by consolidate's import_graph.py (copied
beside this module; never a second resolver). Python: every import statement, tagged module-level,
lazy (inside a function) or typing (under TYPE_CHECKING). JS/TS: relative static imports, with
`aliases` rewritten to relative specifiers first; dynamic `import()` is not seen. Imports that should
resolve but don't, and imports nobody resolves (shell `source`, Go), are counted, never guessed."""
import ast
import posixpath
import re
from dataclasses import dataclass, field
from typing import Dict, List, Set

from . import import_graph
from .config import Settings
from .files import GO, JS, PYTHON, SHELL, Inventory, SourceFile, python_tree

MODULE, LAZY, TYPING = "module", "lazy", "typing"
ALIAS_PREFIXES = ("~", "#", "@/")  # unconfigured path aliases, as opposed to package names
QUOTED = re.compile(r"""(['"])([^'"\n]+)\1""")
SHELL_SOURCE = re.compile(r"^\s*(?:source|\.)\s+\S", re.MULTILINE)
GO_IMPORT = re.compile(r"^\s*import\b", re.MULTILINE)


@dataclass
class Edge:
    source: str
    target: str
    kind: str  # MODULE, LAZY or TYPING
    line: int
    lang: str  # PYTHON or JS


@dataclass
class Graph:
    edges: List[Edge] = field(default_factory=list)
    resolved: int = 0
    unresolved: List[str] = field(default_factory=list)  # "path:line spec"
    unsupported: int = 0

    def importers(self, include_tests: bool, inventory: Inventory) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {}
        for e in self.edges:
            if include_tests or not inventory.files[e.source].test:
                out.setdefault(e.target, set()).add(e.source)
        return out


def build(inventory: Inventory, settings: Settings) -> Graph:
    graph = Graph()
    files = set(inventory.files)
    folders = set()
    for p in files:
        folder = posixpath.dirname(p) if p.endswith(".py") else ""
        while folder and folder not in folders:
            folders.add(folder)
            folder = posixpath.dirname(folder)
    for source in inventory.files.values():
        if not source.subject:
            continue
        if source.kind == PYTHON:
            python_edges(source, inventory, files, folders, graph)
        elif source.kind == JS:
            js_edges(source, files, settings.layout["aliases"], graph)
        elif source.kind == SHELL:
            graph.unsupported += len(SHELL_SOURCE.findall(source.text))
        elif source.kind == GO and GO_IMPORT.search(source.text):
            graph.unsupported += 1
    return graph


def import_kinds(tree: ast.Module) -> Dict[int, str]:
    module_level = {id(n) for n in import_graph.module_level_imports(tree)}
    typing = set()
    for node in tree.body:
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
            typing |= {id(n) for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom))}
    kinds = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            kinds[id(node)] = TYPING if id(node) in typing else MODULE if id(node) in module_level else LAZY
    return kinds


def first_party(path: str, name: str, files: Set[str], folders: Set[str]) -> bool:
    """Whether an absolute import's top-level name is a module or a folder holding Python in one of
    the folders it searches (so `import http.client` beside an unrelated `http/` folder is external)."""
    top = name.split(".")[0]
    for folder in import_graph.py_search_dirs(path):
        candidate = posixpath.join(folder, top) if folder else top
        if candidate + ".py" in files or candidate in folders:
            return True
    return False


def python_edges(source: SourceFile, inventory: Inventory, files, folders, graph: Graph):
    tree = python_tree(source, inventory)
    if tree is None:
        return
    kinds = import_kinds(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        targets = import_graph.py_edges(source.path, ast.unparse(node), files)
        for target in sorted(targets):
            graph.edges.append(Edge(source.path, target, kinds[id(node)], node.lineno, PYTHON))
        if targets:
            graph.resolved += 1
            continue
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        relative = isinstance(node, ast.ImportFrom) and node.level
        if relative or any(n and first_party(source.path, n, files, folders) for n in names):
            graph.unresolved.append(f"{source.path}:{node.lineno} {ast.unparse(node)}")


def rewrite_aliases(path: str, text: str, aliases: Dict[str, str]) -> str:
    if not aliases:
        return text

    def swap(match):
        quote, spec = match.group(1), match.group(2)
        for prefix in sorted(aliases, key=len, reverse=True):
            if spec.startswith(prefix):
                target = posixpath.join(posixpath.normpath(aliases[prefix]), spec[len(prefix):])
                rel = posixpath.relpath(target, posixpath.dirname(path) or ".")
                return quote + (rel if rel.startswith(".") else "./" + rel) + quote
        return match.group(0)
    return QUOTED.sub(swap, text)


def js_specifiers(text: str):
    """(line, specifier) for every static import, export-from and require in the text."""
    for rx, group in ((import_graph.JS_FROM, 4), (import_graph.JS_BARE, 1), (import_graph.JS_REQUIRE, 1)):
        for m in rx.finditer(text):
            yield text.count("\n", 0, m.start(group)) + 1, m.group(group)


def js_edges(source: SourceFile, files, aliases, graph: Graph):
    text = import_graph.strip_js_comments(rewrite_aliases(source.path, source.text, aliases))
    lines = {}
    for line, spec in js_specifiers(text):
        if spec.startswith("."):
            target = import_graph.js_resolve(source.path, spec, files)
            if target:
                lines.setdefault(target, line)
                graph.resolved += 1
            elif not posixpath.splitext(spec.split("?")[0])[1] or spec.endswith(import_graph.JS_EXT):
                graph.unresolved.append(f"{source.path}:{line} {spec}")
        elif spec.startswith(ALIAS_PREFIXES):
            graph.unsupported += 1
    for target, names, type_only in import_graph.js_imports(source.path, text, files):
        if target != source.path:
            graph.edges.append(Edge(source.path, target, TYPING if type_only else MODULE,
                                    lines.get(target, 1), JS))
