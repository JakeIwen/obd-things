"""Run the dashboard v2 node suite from the Python test runners (critique M4).

``npm test`` in ``projects/vehicle_data/dashboard`` is the primary gate on the
Pi. This wrapper makes ``python3 -m unittest`` / pytest (and therefore the
``repo-tests`` compute task) run the same ``node --test`` suite as a regression
gate. It skips when Node 20+ or the installed dependencies are absent, for
example in a checkout where ``npm ci`` has not run.
"""

from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "projects/vehicle_data/dashboard"
NODE_MODULES = DASHBOARD / "node_modules"
MIN_NODE_MAJOR = 20
TIMEOUT_SECONDS = 300


def node_major() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"v(\d+)\.", version)
    return int(match.group(1)) if match else None


def missing_dependencies() -> list[str]:
    if not NODE_MODULES.is_dir():
        return ["node_modules"]
    try:
        package = json.loads((DASHBOARD / "package.json").read_text())
    except (OSError, ValueError):
        return ["package.json"]
    return [
        name
        for name in sorted(package.get("dependencies", {}))
        if not (NODE_MODULES / name / "package.json").is_file()
    ]


def esbuild_problem() -> str | None:
    """Why the installed esbuild cannot run here, or None when it works.

    ``node_modules`` is installed on the Pi (linux-arm64). The repo-tests compute task can ship
    the tree to a worker on another platform (the Mac), where esbuild's native binary refuses to
    load; that is an environment mismatch, not a dashboard failure.
    """
    node = shutil.which("node")
    if not node or not NODE_MODULES.is_dir():
        return None
    try:
        result = subprocess.run(
            [node, "-e", "require('esbuild').transformSync('let a = 1')"],
            cwd=DASHBOARD,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if result.returncode == 0:
        return None
    return (result.stderr.strip().splitlines() or ["esbuild failed to load"])[-1][:200]


NODE_MAJOR = node_major()
MISSING = missing_dependencies()
ESBUILD_PROBLEM = esbuild_problem() if NODE_MAJOR is not None and not MISSING else None


@unittest.skipUnless(
    NODE_MAJOR is not None and NODE_MAJOR >= MIN_NODE_MAJOR,
    f"node >= {MIN_NODE_MAJOR} is required for the dashboard v2 suite",
)
@unittest.skipIf(
    bool(MISSING),
    "dashboard v2 dependencies are not installed "
    f"(missing {', '.join(MISSING)}); run 'npm ci' in {DASHBOARD}",
)
@unittest.skipIf(
    ESBUILD_PROBLEM is not None,
    f"installed esbuild cannot run on this platform ({ESBUILD_PROBLEM}); "
    "node_modules was installed for another OS/CPU",
)
class DashboardV2NodeSuiteTests(unittest.TestCase):
    def test_node_suite_passes(self):
        # Explicit files rather than a directory argument: Node 20 recurses into a directory,
        # Node 21+ treats arguments as globs, and a file list means the same on both.
        files = sorted(str(path.relative_to(DASHBOARD)) for path in (DASHBOARD / "test").rglob("*.test.mjs"))
        self.assertTrue(files, "no dashboard node tests found")
        try:
            result = subprocess.run(
                ["node", "--test", *files],
                cwd=DASHBOARD,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b"")
            output = output.decode(errors="replace") if isinstance(output, bytes) else output
            self.fail(f"node --test exceeded {TIMEOUT_SECONDS} s\n{output[-4000:]}")
        report = (result.stdout + result.stderr)[-6000:]
        self.assertEqual(result.returncode, 0, f"node --test failed\n{report}")
        self.assertRegex(result.stdout, r"# fail 0\b", report)


if __name__ == "__main__":
    unittest.main()
