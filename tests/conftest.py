"""Suite-wide guard: no test may build a model CLI lock on the live machine-wide path."""

from pathlib import Path

import pytest

from projects.vehicle_data import model_cli_lock


@pytest.fixture(autouse=True)
def _no_live_cli_lock(monkeypatch):
    real_init = model_cli_lock.CliLock.__init__

    def guarded(self, path=model_cli_lock.LOCK_PATH):
        if Path(path) == model_cli_lock.LOCK_PATH:
            raise AssertionError(
                f"a test built a CliLock on the live path {path}; use tests.assistant_lock_support.private_cli_lock")
        real_init(self, path)

    monkeypatch.setattr(model_cli_lock.CliLock, "__init__", guarded)
