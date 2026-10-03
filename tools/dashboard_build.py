#!/usr/bin/env python3
"""Compute-worker-only dashboard regression/build entrypoint; no vehicle access.

Installs the pinned dependency lockfile in the compute source snapshot, runs
the maintained Node tests, and returns a separately located build directory.
Use the named dashboard-test-build pi_compute task, not a direct Pi build.
"""
from pathlib import Path
import argparse
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "projects/vehicle_data/dashboard"
    # Ignore dependency lifecycle scripts. esbuild uses its platform-specific
    # optional package from the lockfile; no install hook is needed.
    subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                   cwd=root, check=True, timeout=240)
    tests = sorted(str(p.relative_to(root)) for p in (root / "test").rglob("*.test.mjs"))
    if not tests:
        raise RuntimeError("dashboard test files missing")
    subprocess.run(["node", "--test", *tests], cwd=root, check=True, timeout=300)
    subprocess.run(["node", "tools/build.mjs", "--out-dir", str(args.output.resolve())],
                   cwd=root, check=True, timeout=120)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
