"""How the broker starts its drive helpers (``active_drive``, ``bcan_auxiliary``).

The vehicle_data programs have no sys.path bootstrap, so a helper runs as a module
(``python -m``) and the caller must start it with the repository root as its working
directory. ``tests/test_drive_helper_launch.py`` runs that exact launch.
"""

import sys


def helper_command(module: str) -> list[str]:
    """Interpreter arguments that start ``module``; the caller appends its options."""
    return [sys.executable, "-m", module]
