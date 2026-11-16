"""Scoped gate "notes": Markdown findings and docs that no test reads.

AGENTS.md declares the gate. An amendment to a file in ``NOTES_SCOPE`` commits after this module
passes, without the whole suite, which holds only while no test opens such a file. The check here
is a tripwire, not proof: it flags a Python test whose code names a Markdown path in scope.
"""

import ast
import subprocess
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


def snapshot_files(*paths):
    """Every file under ``paths`` in a checkout copy that has no Git data.

    A van-compute source snapshot holds the checkout's files without ``.git`` (and without
    the names it always leaves out), so its files stand in for Git's listing there.
    """
    skipped = {"tmp", "__pycache__", ".pytest_cache", "node_modules"}
    return sorted(
        path.relative_to(REPO).as_posix()
        for top in paths
        for path in (REPO / top).rglob("*")
        if path.is_file() and not skipped & set(path.relative_to(REPO).parts)
    )


class NotesGateTests(unittest.TestCase):
    def tracked(self, *paths, untracked=False):
        """Tracked files under ``paths``; with ``untracked`` also new files Git does not ignore."""
        if not (REPO / ".git").exists():
            return snapshot_files(*paths)
        flags = ["-co", "--exclude-standard"] if untracked else []
        listed = subprocess.run(
            ["git", "ls-files", "-z", *flags, "--", *paths],
            cwd=REPO, check=True, capture_output=True, text=True,
        ).stdout
        return [path for path in listed.split("\0") if path]

    def notes(self):
        return [path for path in self.tracked("projects", "docs") if in_scope(path)]

    def test_scope_matches_only_markdown(self):
        notes = self.notes()
        self.assertTrue(notes)
        self.assertEqual([path for path in notes if not path.endswith(".md")], [])
        self.assertTrue(in_scope("projects/radar/findings/2026-10-08_post_collision_alignment.md"))
        self.assertFalse(in_scope("projects/radar/docs/AGENT_HANDOFF.md"))
        self.assertFalse(in_scope("projects/vehicle_data/README.md"))
        self.assertFalse(in_scope("AGENTS.md"))

    def test_no_test_module_names_a_note(self):
        note_names = {Path(path).name for path in self.notes()}
        readers = {}
        for module in self.tracked(*TEST_MODULE_DIRS, untracked=True):
            path = REPO / module
            if not module.endswith(".py") or not path.is_file() or path == Path(__file__).resolve():
                continue
            hits = [text for text in code_strings(path.read_bytes()) if names_a_note(text, note_names)]
            if hits:
                readers[module] = hits
        self.assertEqual(readers, {})


if __name__ == "__main__":
    unittest.main()
