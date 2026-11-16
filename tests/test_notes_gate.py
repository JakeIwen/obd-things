"""Scoped gate "notes": Markdown findings and docs that no test reads.

AGENTS.md declares the gate. An amendment to a file in ``NOTES_SCOPE`` commits after this module
passes, without the whole suite, which holds only while no test opens such a file. The check here
is a tripwire, not proof: it flags a Python test whose code names a Markdown path in scope.
"""

import ast
import json
import subprocess
import tempfile
import unittest
from fnmatch import fnmatch
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Tracked Markdown under these globs is the gate's whole scope. Other file types beside them
# (JSON, CSV, logs) are fixtures and evidence that tests do read.
NOTES_SCOPE = ("projects/*/findings/*.md", "projects/*/findings/**/*.md", "docs/*.md", "docs/**/*.md")
TEST_MODULE_DIRS = ("tests", "checks")
# Path fragments that put a ".md" string inside the scope.
SCOPE_MARKERS = ("findings/", "docs/")


def in_scope(path):
    return any(fnmatch(path, pattern) for pattern in NOTES_SCOPE)


def code_strings(source):
    """Every string constant in a module except docstrings."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def names_a_note(text, note_names):
    """True for a string that could open a file in scope: its path, its name or a Markdown glob."""
    if not text.endswith(".md"):
        return False
    if text.startswith("*") or any(marker in text for marker in SCOPE_MARKERS):
        return True
    return Path(text).name in note_names


# Names a van-compute source snapshot always leaves out, plus installed packages.
SNAPSHOT_SKIPPED = {"tmp", "__pycache__", ".pytest_cache", "node_modules"}
# The snapshot always carries the task declarations, so a snapshot can check them too.
TASKS_FILE = ".van-compute.json"


def listed_files(root, *paths, untracked=False):
    """Tracked files under ``paths``; with ``untracked`` also new files Git does not ignore.

    A van-compute source snapshot holds the checkout's files without ``.git``, so there its
    own files stand in for Git's listing. That listing is only as complete as the task's
    source payload, which ``test_suite_tasks_carry_every_note_folder`` guards.
    """
    if not (root / ".git").exists():
        return sorted(
            path.relative_to(root).as_posix()
            for top in paths
            for path in (root / top).rglob("*")
            if path.is_file() and not SNAPSHOT_SKIPPED & set(path.relative_to(root).parts)
        )
    flags = ["-co", "--exclude-standard"] if untracked else []
    listed = subprocess.run(
        ["git", "ls-files", "-z", *flags, "--", *paths],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout
    return [path for path in listed.split("\0") if path]


def notes(root):
    return [path for path in listed_files(root, "projects", "docs") if in_scope(path)]


def note_readers(root, *, skip=None):
    """Return ({test module: strings naming a note}, every test module scanned)."""
    note_names = {Path(path).name for path in notes(root)}
    readers, scanned = {}, []
    for module in listed_files(root, *TEST_MODULE_DIRS, untracked=True):
        path = root / module
        if not module.endswith(".py") or not path.is_file() or path == skip:
            continue
        scanned.append(module)
        hits = [text for text in code_strings(path.read_bytes()) if names_a_note(text, note_names)]
        if hits:
            readers[module] = hits
    return readers, scanned


def suite_tasks(root):
    """The compute tasks that run this module: run_tests.py without the dashboard profile."""
    tasks = json.loads((root / TASKS_FILE).read_text(encoding="utf-8"))["tasks"]
    return [
        task for task in tasks
        if task["argv"][:1] == ["{source:run_tests.py}"] and "--dashboard" not in task["argv"]
    ]


def covered(folder, source_paths):
    return any(folder == path or folder.startswith(path.rstrip("/") + "/") for path in source_paths)


class NotesGateTests(unittest.TestCase):
    def test_scope_matches_only_markdown(self):
        found = notes(REPO)
        self.assertEqual([path for path in found if not path.endswith(".md")], [])
        self.assertTrue(in_scope("projects/radar/findings/2026-10-08_post_collision_alignment.md"))
        self.assertFalse(in_scope("projects/radar/docs/AGENT_HANDOFF.md"))
        self.assertFalse(in_scope("projects/vehicle_data/README.md"))
        self.assertFalse(in_scope("AGENTS.md"))

    def test_inventory_covers_every_scope_root(self):
        """A listing missing docs/ or the findings would let a reader of them pass unseen."""
        found = notes(REPO)
        self.assertTrue([path for path in found if path.startswith("docs/")])
        self.assertTrue([path for path in found if fnmatch(path, "projects/*/findings/*")])

    def test_suite_tasks_carry_every_note_folder(self):
        folders = {str(Path(path).parent) for path in notes(REPO)}
        tasks = suite_tasks(REPO)
        self.assertEqual(
            {task["name"] for task in tasks}, {"repo-full-suite", "repo-tests"}
        )
        for task in tasks:
            missing = sorted(f for f in folders if not covered(f, task["source_paths"]))
            self.assertEqual(missing, [], task["name"])

    def test_no_test_module_names_a_note(self):
        readers, scanned = note_readers(REPO, skip=Path(__file__).resolve())
        self.assertIn("tests/test_run_tests.py", scanned)
        self.assertEqual(readers, {})

    def test_snapshot_listing_flags_a_basename_reference_to_a_root_doc(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for path, text in (
                ("docs/agent-context.md", "# context\n"),
                ("projects/radar/findings/a.md", "# finding\n"),
                ("tests/test_reader.py", 'NOTE = "agent-context.md"\n'),
                ("tests/tmp/test_ignored.py", 'NOTE = "agent-context.md"\n'),
            ):
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            readers, scanned = note_readers(root)
        self.assertEqual(scanned, ["tests/test_reader.py"])
        self.assertEqual(readers, {"tests/test_reader.py": ["agent-context.md"]})


if __name__ == "__main__":
    unittest.main()
