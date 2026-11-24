"""How the broker starts its drive helpers (``active_drive``, ``bcan_auxiliary``).

The vehicle_data programs have no sys.path bootstrap, so a helper runs as a module
(``python -m``) with the repository root as its working directory. This module owns both
halves of that rule; ``tests/test_drive_helper_launch.py`` runs that exact launch.
"""

from pathlib import Path
import sys

HELPER_CWD = Path(__file__).resolve().parents[2]


def helper_command(module: str) -> list[str]:
    """Interpreter arguments that start ``module``; the caller appends its options."""
    return [sys.executable, "-m", module]
