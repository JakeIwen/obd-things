"""How the broker starts its drive helpers (``active_drive``, ``bcan_auxiliary``).

The vehicle_data programs have no sys.path bootstrap, so a helper runs as a module
(``python -m``) with the repository root as its working directory. This module owns both
halves of that rule; ``tests/test_drive_helper_launch.py`` runs that exact launch.
"""

import os
from pathlib import Path
import sys

from projects.vehicle_data import barometric_pressure as baro
from projects.vehicle_data.ccan_powertrain import INTAKE_AC_ENV, INTAKE_AC_ENV_ON

HELPER_CWD = Path(__file__).resolve().parents[2]


def helper_command(module: str) -> list[str]:
    """Interpreter arguments that start ``module``; the caller appends its options."""
    return [sys.executable, "-m", module]


def helper_environment(*, enable_barometric_pressure: bool) -> dict[str, str]:
    """Advertise optional sources with an environment older helpers ignore."""
    env = {**os.environ, INTAKE_AC_ENV: INTAKE_AC_ENV_ON}
    env.pop(baro.HELPER_ENV, None)
    if enable_barometric_pressure:
        env[baro.HELPER_ENV] = baro.HELPER_ENV_ON
    return env
