"""Which files the checks read: discovery (git when the root is in a work tree, else a walk),
default exclusions for vendor, generated and build output, source kinds and parsed Python trees.
Never imports or executes project code."""
import ast
import json
import os
import re
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import git_adapter
from .config import Settings, matches_any

PYTHON, JS, SHELL, GO = "python", "js", "shell", "go"
KINDS_BY_EXT = {
    ".py": PYTHON, ".js": JS, ".mjs": JS, ".cjs": JS, ".ts": JS, ".mts": JS, ".cts": JS,
    ".tsx": JS, ".jsx": JS, ".sh": SHELL, ".bash": SHELL, ".zsh": SHELL, ".go": GO,
}
UI_EXT = {".tsx", ".jsx"}  # resolved for imports, but react-app owns their layout
SHEBANGS = ((re.compile(r"python"), PYTHON), (re.compile(r"\b(node|tsx|deno|bun)\b"), JS),
            (re.compile(r"\b(ba|z|da|k)?sh\b"), SHELL))
DEFAULT_EXCLUDE = [
    "**/node_modules/**", "**/dist/**", "**/build/**", "**/vendor/**", "**/__pycache__/**",
    "**/.venv/**", "**/venv/**", "**/site-packages/**", "**/generated/**", "**/*.generated.*",
    "**/*.min.*", ".git/**", ".agent/**", "**/.claude/worktrees/**",
]
PRUNE_DIRS = {"node_modules", "dist", "build", "vendor", "__pycache__", ".venv", "venv", ".git", ".agent",
              "site-packages", "generated", "worktrees"}
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)(test_[^/]*\.py|[^/]*_test\.(py|go)|conftest\.py"
                       r"|[^/]*\.(test|spec)\.[cm]?[jt]sx?|test-[^/]*\.[cm]?[jt]sx?)$")
UI_DEPENDENCIES = ("react", "react-dom", "preact")  # a nested package.json naming one is a UI app (react-app's)
MAX_BYTES = 2_000_000  # larger files are bundles or data, not hand-written modules


@dataclass
class SourceFile:
    path: str
    kind: str
    text: str
    ui: bool = False
    test: bool = False
    subject: bool = False  # inside a source root and not excluded: the rules look at it
    tree: Optional[ast.Module] = None
    parse_error: str = ""

    @property
    def lines(self) -> int:
        return len(self.text.splitlines())

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


@dataclass
class Inventory:
    root: str
    files: Dict[str, SourceFile]  # every readable source file, for import resolution
    paths: List[str] = field(default_factory=list)  # every listed file, sources or not, for target lookup
    skipped_large: int = 0
    skipped_ui: int = 0  # files in nested React/Preact apps or .tsx/.jsx: resolved, not checked
    parse_errors: List[str] = field(default_factory=list)

    @property
    def subjects(self) -> List[SourceFile]:
        return [f for f in self.files.values() if f.subject]

    def scanned_by_kind(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for f in self.subjects:
            counts[f.kind] = counts.get(f.kind, 0) + 1
        return counts


def is_test(path: str) -> bool:
    return bool(TEST_FILE.search(path))


def listed_paths(root: str) -> List[str]:
    """Repo-relative paths: from git inside a work tree, else a walk that prunes build and vendor folders."""
    if git_adapter.inside_work_tree(root):
        return git_adapter.listed_files(root)
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in PRUNE_DIRS)
        rel = os.path.relpath(folder, root)
        found += [n if rel == "." else f"{rel}/{n}" for n in names]
    return sorted(found)


def kind_of(path: str, full: str) -> Optional[str]:
    ext = os.path.splitext(path)[1]
    if ext:
        return KINDS_BY_EXT.get(ext)
    try:
        with open(full, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    line = first.decode("utf-8", "replace")
    return next((kind for rx, kind in SHEBANGS if rx.search(line)), None)


def ui_app_folders(root: str, paths: List[str]) -> List[str]:
    """Folders below the root whose package.json depends on React or Preact."""
    folders = []
    for path in paths:
        if os.path.basename(path) != "package.json" or "/" not in path:
            continue
        try:
            with open(os.path.join(root, path), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        deps = dict(data.get("dependencies") or {}, **(data.get("devDependencies") or {}))
        if any(name in deps for name in UI_DEPENDENCIES):
            folders.append(os.path.dirname(path))
    return folders


def discover(settings: Settings) -> Inventory:
    root, layout = settings.root, settings.layout
    inventory = Inventory(root=root, files={})
    roots = [os.path.normpath(r) for r in layout["sourceRoots"]]
    listed = listed_paths(root)
    ui_folders = ui_app_folders(root, [p for p in listed if not matches_any(p, DEFAULT_EXCLUDE)])
    for path in listed:
        full = os.path.join(root, path)
        if matches_any(path, DEFAULT_EXCLUDE) or os.path.islink(full) or not os.path.isfile(full):
            continue
        inventory.paths.append(path)
        kind = kind_of(path, full)
        if not kind:
            continue
        if os.path.getsize(full) > MAX_BYTES:
            inventory.skipped_large += 1
            continue
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        ui = os.path.splitext(path)[1] in UI_EXT or matches_any(path, ui_folders)
        source = SourceFile(path=path, kind=kind, text=text, ui=ui, test=is_test(path))
        source.subject = matches_any(path, roots) and not matches_any(path, layout["exclude"]) and not ui
        inventory.skipped_ui += ui and matches_any(path, roots)
        inventory.files[path] = source
    return inventory


def python_tree(source: SourceFile, inventory: Inventory) -> Optional[ast.Module]:
    """The parsed module, cached; a syntax error is recorded once and yields None."""
    if source.tree is None and not source.parse_error:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                source.tree = ast.parse(source.text)
        except (SyntaxError, ValueError) as e:
            source.parse_error = f"{source.path}: {e.__class__.__name__}: {e}"
            if source.subject:
                inventory.parse_errors.append(source.parse_error)
    return source.tree
