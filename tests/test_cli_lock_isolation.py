"""Tests must never see or take the live model CLI lock (the guard lives in tests/conftest.py)."""

import tempfile
import unittest
from pathlib import Path

from projects.vehicle_data.model_cli_lock import ACQUIRED, HELD, LOCK_PATH, CliLock
from projects.vehicle_data.warning_chat import WarningChatManager
from tests.assistant_lock_support import private_cli_lock


class CliLockIsolationTests(unittest.TestCase):
    def test_a_lock_on_the_live_path_is_refused_in_tests(self):
        with self.assertRaises(AssertionError):
            CliLock()
        with self.assertRaises(AssertionError):
            CliLock(LOCK_PATH)

    def test_a_manager_without_a_lock_argument_is_refused_in_tests(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(AssertionError):
            WarningChatManager(tmp, object(), runner=None)

    def test_a_held_lock_elsewhere_does_not_reach_a_test_built_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = tempfile.mkdtemp(dir=tmp)
            stand_in = private_cli_lock(other)  # plays the live lock, held by a "research job"
            self.assertEqual(stand_in.try_acquire(), ACQUIRED)
            manager = WarningChatManager(Path(tmp) / "state", object(), runner=None,
                                         cli_lock=private_cli_lock(tmp))
            try:
                self.assertNotEqual(manager.slot.cli_lock.path, LOCK_PATH)
                manager.slot.try_acquire()  # would raise ChatError 429 if it shared the held lock
                manager.slot.release()
                self.assertEqual(CliLock(stand_in.path).try_acquire(), HELD)
            finally:
                stand_in.release()
                manager.close()


if __name__ == "__main__":
    unittest.main()
