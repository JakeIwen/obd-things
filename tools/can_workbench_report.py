#!/usr/bin/env python3
"""Generate a local, offline CAN investigation report and JSON bundle."""
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.can_workbench.report import main

if __name__ == "__main__":
    raise SystemExit(main())
