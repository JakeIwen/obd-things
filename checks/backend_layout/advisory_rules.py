"""The advisory rules: fan-in-size, shim-stale, effects-in-rules and shell-size. They are review
triggers and clues, reported and never failing a run."""
import ast
import posixpath
import re
from typing import Callable, List, Set

from . import import_graph
from .config import Settings
from .edges import Graph
from .files import JS, PYTHON, SHELL, Inventory, python_tree
from .findings import Finding
from .structure_rules import is_python_main_guard

# Model and rules modules by name: roles are vocabulary, so only conventional names are checked.
RULES_MODULE = re.compile(r"(^|/)(models?|rules|types|schema|[\w-]+_(models?|rules|state|view|schema|types))\.py$"
                          r"|(^|/)(models?|rules|types|schema)\.[cm]?[jt]s$|\.(helpers|types|rules|model)\.[cm]?[jt]s$")
PY_EFFECT_MODULES = {"socket", "subprocess", "time", "http.client", "http.server", "urllib.request", "requests",
                     "httpx", "aiohttp", "sqlite3", "shutil", "smtplib", "asyncio.subprocess"}
PY_EFFECT_PACKAGES = {m for m in PY_EFFECT_MODULES if "." not in m}  # any submodule of these counts too
PY_EFFECT_CALLS = {"datetime.now", "datetime.utcnow", "date.today", "os.system", "os.popen", "os.replace",
                   "os.remove", "os.unlink", "os.rename", "os.makedirs", "os.mkdir"}
PY_EFFECT_METHODS = {"write_text", "write_bytes", "touch", "unlink", "mkdir", "rmdir"}
JS_EFFECTS = re.compile(r"""(?:from\s*|require\(\s*)['"](?:node:)?(child_process|net|http|https|dgram|fs|fs/promises)['"]"""
                        r"|\b(fetch|Date\.now|writeFileSync|writeFile|appendFile|spawn|execSync|execFile)\s*\("
                        r"|new\s+Date\(\s*\)")
JS_REEXPORT = re.compile(r"""(export\s+(?:type\s+)?(?:\*(?:\s+as\s+[\w$]+)?|\{[^}]*\})\s+from\s*['"][^'"]+['"]"""
                         r"""|import\s+(?:type\s+)?[\w$\s,{}*]*?\s*from\s*['"][^'"]+['"]|export\s+(?:type\s+)?\{[^}]*\})"""
                         r"""\s*;?""")
HUB_NAMES = re.compile(r"(^|/)(__init__|__main__)\.py$|(^|/)index\.[cm]?[jt]sx?$")


def fan_in_size_findings(inventory: Inventory, graph: Graph, settings: Settings) -> List[Finding]:
    importers = graph.importers(include_tests=False, inventory=inventory)
    limit, minimum, out = settings.file_lines, settings.thresholds["fanIn"], []
    for f in inventory.subjects:
        users = importers.get(f.path, set())
        if not f.test and f.lines > limit and len(users) >= minimum:
            out.append(Finding("fan-in-size", (f.path,), f"{f.lines} lines (limit {limit}) imported by {len(users)} "
                               f"modules: split it at a decision or adapter boundary, keeping its public names"))
    return out


def python_reexport_only(tree: ast.Module) -> bool:
    reexports = 0  # `from x import y` statements; a plain `import x` alone is a side-effect import
    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            reexports += isinstance(node, ast.ImportFrom)
        elif index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring
        elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        elif is_python_main_guard(node) and all(isinstance(s, (ast.Expr, ast.Raise)) for s in node.body):
            continue  # an executable wrapper: raise SystemExit(main())
        else:
            return False
    return reexports > 0


def js_reexport_only(text: str) -> bool:
    body = import_graph.strip_js_comments(text)
    body = re.sub(r"""^\s*['"]use strict['"];?""", "", body)
    rest = JS_REEXPORT.sub("", body)
    return "export" in body and not re.sub(r"[\s;]", "", rest)


def referenced_elsewhere(path: str, inventory: Inventory) -> bool:
    """Named by another source file: its repo path, its dotted module, an import of its stem, or a
    quoted file name (callers that run it with `-m`, by path, or import it through a launcher's import
    roots resolve no edge)."""
    stem, ext = posixpath.splitext(path)
    name = re.escape(posixpath.basename(stem))
    pattern = re.compile("|".join([
        re.escape(path), re.escape(stem.replace("/", ".")) + r"\b", rf"\b(?:import|from)\s+{name}\b",
        rf"['\"](?:[\w./-]*/{name}(?:{re.escape(ext)})?|{name}{re.escape(ext)})['\"]"]))
    return any(pattern.search(other.text) for other in inventory.files.values() if other.path != path)


def shim_stale_findings(inventory: Inventory, graph: Graph, listed: Callable[[str], bool]) -> List[Finding]:
    imported: Set[str] = {e.target for e in graph.edges}
    out = []
    for f in inventory.subjects:
        if f.test or HUB_NAMES.search(f.path) or f.path in imported or listed(f.path):
            continue
        tree = python_tree(f, inventory) if f.kind == PYTHON else None
        reexport = python_reexport_only(tree) if tree else f.kind == JS and js_reexport_only(f.text)
        if reexport and not referenced_elsewhere(f.path, inventory):
            out.append(Finding("shim-stale", (f.path,), "re-exports only, nothing in the repo imports it and no "
                               "Contracts row or unit lists it: retire it once every caller and rollback state is "
                               "gone, or list it in the Contracts table"))
    return out


def effect_module(name: str) -> bool:
    return name in PY_EFFECT_MODULES or name.split(".")[0] in PY_EFFECT_PACKAGES


def python_effects(tree: ast.Module) -> List[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"{node.lineno}: import {a.name}" for a in node.names if effect_module(a.name)]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module and effect_module(node.module):
            found.append(f"{node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call):
            found += call_effect(node)
    return found


def call_effect(node: ast.Call) -> List[str]:
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else func.value.attr \
            if isinstance(func.value, ast.Attribute) else ""
        if f"{owner}.{func.attr}" in PY_EFFECT_CALLS or func.attr in PY_EFFECT_METHODS:
            return [f"{node.lineno}: {owner + '.' if owner else ''}{func.attr}()"]
    if isinstance(func, ast.Name) and func.id == "open":
        mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) & set("wax+"):
            return [f"{node.lineno}: open(..., {mode.value!r})"]
    return []


def js_effects(text: str) -> List[str]:
    body = import_graph.strip_js_comments(text)
    return [f"{body.count(chr(10), 0, m.start()) + 1}: {m.group(0).strip()}" for m in JS_EFFECTS.finditer(body)]


def effects_in_rules_findings(inventory: Inventory) -> List[Finding]:
    out = []
    for f in inventory.subjects:
        if f.test or not RULES_MODULE.search(f.path):
            continue
        tree = python_tree(f, inventory) if f.kind == PYTHON else None
        effects = python_effects(tree) if tree else js_effects(f.text) if f.kind == JS else []
        if effects:
            out.append(Finding("effects-in-rules", (f.path,), f"a model/rules module by its name, but it touches the "
                               f"outside world ({'; '.join(effects[:3])}{' ...' if len(effects) > 3 else ''}): move "
                               f"the effect into an adapter or the runtime"))
    return out


def shell_size_findings(inventory: Inventory, settings: Settings) -> List[Finding]:
    limit = settings.thresholds["shellLines"]
    return [Finding("shell-size", (f.path,), f"{f.lines} lines of shell (review trigger {limit}): consider "
                    f"splitting it, or moving the hard part to Python under python-tool")
            for f in inventory.subjects if f.kind == SHELL and not f.test and f.lines > limit]
