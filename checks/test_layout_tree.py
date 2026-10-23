"""The repo's own tree passes its backend-layout checks: no new ratchet findings and no
declared-target failures. Installed by the backend-layout skill beside check_layout.py; the repo
root is the nearest folder above whose .codeshape/config.json has a `layout` block."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def repo_root():
    for folder in (HERE, *HERE.parents):
        config = folder / ".codeshape" / "config.json"
        if config.is_file() and "layout" in json.loads(config.read_text(encoding="utf-8")):
            return folder
    return None


class LayoutTree(unittest.TestCase):
    def test_tree_passes_the_layout_checks(self):
        root = repo_root()
        self.assertIsNotNone(root, f"no .codeshape/config.json with a layout block above {HERE}")
        result = subprocess.run([sys.executable, str(HERE / "check_layout.py"), "--root", str(root)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
