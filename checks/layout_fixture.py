"""Fixture repos for the backend-layout tests: write a tree into a temp folder, run check_layout.py
as a subprocess (its public entry point) and read its JSON. Nothing here imports the checker."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
# An installed copy sits beside its tests; the skill keeps the checker in checks/.
CHECKER = next(p for p in (HERE / "check_layout.py", HERE.parent / "checks" / "check_layout.py") if p.exists())
GIT_IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]


def python_module(lines: int, main_guard: bool = False, body: str = "") -> str:
    text = body + "".join(f"value_{i} = {i}\n" for i in range(lines))
    return text + ('\nif __name__ == "__main__":\n    raise SystemExit(0)\n' if main_guard else "")


class LayoutCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="backend-layout-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, files: dict):
        for rel, text in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def config(self, layout: dict = None, **extra):
        data = dict(extra)
        if layout is not None:
            data["layout"] = layout
        self.write({".codeshape/config.json": json.dumps(data, indent=2) + "\n"})

    def run_checker(self, *args, root: Path = None, env: dict = None):
        fixture_root = (root or self.root).resolve()
        command = [sys.executable, str(CHECKER), *args, "--root", str(fixture_root)]
        environment = dict(os.environ, **(env or {}))
        # A fixture inside an ignored checkout tmp/ must not inherit that repo.
        # Git still discovers .git inside fixtures that explicitly initialize it.
        environment["GIT_CEILING_DIRECTORIES"] = str(fixture_root.parent)
        return subprocess.run(command, capture_output=True, text=True, check=False, env=environment)

    def check(self, *args, expect: int = None) -> dict:
        result = self.run_checker(*args, "--json")
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        data["exitCode"] = result.returncode
        return data

    def found(self, data: dict, rule: str) -> list:
        return [f for f in data["findings"] if f["rule"] == rule]

    def paths(self, data: dict, rule: str) -> list:
        return sorted(f["path"] for f in self.found(data, rule))

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *GIT_IDENTITY, *args], capture_output=True, text=True,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def commit(self, message: str = "fixture"):
        if not (self.root / ".git").exists():
            self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def baseline_file(self, text: str = None):
        """A consolidate-style baseline (version 2, limits) without a layout key."""
        data = {"version": 2, "head": "", "date": "2026-10-21", "limits": {"file_lines": 800, "func_lines": 80,
                                                                         "enforce_tests": True}, "files": {}}
        self.write({".codeshape/baseline.json": text or json.dumps(data, indent=1, sort_keys=True) + "\n"})
        return self.root / ".codeshape" / "baseline.json"
