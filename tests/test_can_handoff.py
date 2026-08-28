import tempfile
import threading
import time
import unittest
from unittest import mock

from lib import can_handoff, diagnostic_safety


class CanHandoffTests(unittest.TestCase):
    def test_role_name_is_logical_and_spare_is_rejected(self):
        self.assertEqual(can_handoff.lock_name("c-can"), "can-handoff-c-can")
        self.assertEqual(
            can_handoff.gate_lock_name("c-can"),
            "can-handoff-gate-c-can",
        )
        self.assertEqual(can_handoff.lock_name("bcan"), "can-handoff-b-can")
        with self.assertRaisesRegex(ValueError, "connected vehicle bus"):
            can_handoff.lock_name("spare")
        with self.assertRaisesRegex(ValueError, "connected vehicle bus"):
            can_handoff.gate_lock_name("spare")

    def test_shared_passive_turns_coexist_and_active_turn_waits(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            diagnostic_safety,
            "LOCK_DIR",
            directory,
        ):
            with can_handoff.passive_turn("c-can"):
                with can_handoff.passive_turn("c-can"):
                    with self.assertRaises(diagnostic_safety.ChannelLockError):
                        with can_handoff.active_turn("c-can", wait_seconds=0):
                            pass

            with can_handoff.active_turn("c-can"):
                with self.assertRaises(diagnostic_safety.ChannelLockError):
                    with can_handoff.passive_turn("c-can"):
                        pass

            # Releasing the exclusive scheduling turn restores ordinary
            # passive admission; the handoff is not a persistent inhibit.
            with can_handoff.passive_turn("c-can"):
                pass

    def test_waiting_active_turn_prevents_new_passive_overtake(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            diagnostic_safety,
            "LOCK_DIR",
            directory,
        ):
            existing = can_handoff.passive_turn("c-can")
            existing.__enter__()
            active_entered = threading.Event()
            release_active = threading.Event()
            failure = []

            def reserve_active():
                try:
                    with can_handoff.active_turn("c-can", wait_seconds=1.0):
                        active_entered.set()
                        release_active.wait(1.0)
                except BaseException as exc:  # pragma: no cover - assertion aid
                    failure.append(exc)

            worker = threading.Thread(target=reserve_active)
            worker.start()
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    with can_handoff.passive_turn("c-can"):
                        pass
                except diagnostic_safety.ChannelLockError:
                    break
                if time.monotonic() >= deadline:
                    self.fail("active waiter never closed the passive admission gate")
                time.sleep(0.005)

            existing.__exit__(None, None, None)
            self.assertTrue(active_entered.wait(1.0))
            with self.assertRaises(diagnostic_safety.ChannelLockError):
                with can_handoff.passive_turn("c-can"):
                    pass
            release_active.set()
            worker.join(1.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failure, [])

            with can_handoff.passive_turn("c-can"):
                pass

    def test_active_wait_is_bounded_and_releases_gate(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            diagnostic_safety,
            "LOCK_DIR",
            directory,
        ):
            with can_handoff.passive_turn("c-can"):
                started = time.monotonic()
                with self.assertRaisesRegex(
                    diagnostic_safety.ChannelLockError,
                    "bounded active handoff deadline",
                ):
                    with can_handoff.active_turn(
                        "c-can",
                        wait_seconds=0.03,
                        retry_seconds=0.005,
                    ):
                        pass
                self.assertLess(time.monotonic() - started, 0.25)

            with can_handoff.passive_turn("c-can"):
                pass

    def test_one_bus_handoff_does_not_exclude_another_bus(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            diagnostic_safety,
            "LOCK_DIR",
            directory,
        ):
            with can_handoff.active_turn("c-can"):
                with can_handoff.passive_turn("b-can"):
                    pass


if __name__ == "__main__":
    unittest.main()
