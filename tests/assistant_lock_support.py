"""A model CLI lock inside a test's own temp directory.

``CliLock()`` defaults to the machine-wide ``/run/van-assistant/model-cli.lock``; a test that used
it would see (or take) the live advisor's and research worker's lock. ``tests/conftest.py`` fails
any test that builds a lock on that path.
"""

from pathlib import Path

from projects.vehicle_data.model_cli_lock import CliLock


def private_cli_lock(directory):
    path = Path(directory) / "model-cli.lock"
    path.touch()
    return CliLock(path)
