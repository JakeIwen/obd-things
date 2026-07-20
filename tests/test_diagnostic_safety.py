import tempfile
import unittest
from unittest import mock

from lib import diagnostic_safety


class DiagnosticLockTests(unittest.TestCase):
    def test_lock_is_exclusive_and_reusable_after_release(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            diagnostic_safety, "LOCK_DIR", directory
        ):
            first = diagnostic_safety.acquire_channel_lock("can0")
            try:
                with self.assertRaisesRegex(RuntimeError, "already holds"):
                    diagnostic_safety.acquire_channel_lock("can0")
            finally:
                diagnostic_safety.release_channel_lock(first)

            second = diagnostic_safety.acquire_channel_lock("can0")
            diagnostic_safety.release_channel_lock(second)

    def test_channel_name_cannot_escape_lock_directory(self):
        with self.assertRaisesRegex(ValueError, "unsafe"):
            diagnostic_safety.acquire_channel_lock("../can0")

    def test_lock_capability_validates_exact_live_channel_only(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            diagnostic_safety, "LOCK_DIR", directory
        ):
            handle = diagnostic_safety.acquire_channel_lock("can0")
            self.assertIs(diagnostic_safety.validate_channel_lock(handle, "can0"), handle)
            with self.assertRaisesRegex(RuntimeError, "held can1"):
                diagnostic_safety.validate_channel_lock(handle, "can1")
            diagnostic_safety.release_channel_lock(handle)
            with self.assertRaisesRegex(RuntimeError, "held can0"):
                diagnostic_safety.validate_channel_lock(handle, "can0")

    def test_context_manager_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            diagnostic_safety, "LOCK_DIR", directory
        ):
            with self.assertRaisesRegex(OSError, "socket failed"):
                with diagnostic_safety.channel_lock("can0"):
                    raise OSError("socket failed")

            handle = diagnostic_safety.acquire_channel_lock("can0")
            diagnostic_safety.release_channel_lock(handle)

    def test_interrupted_acquisition_closes_newly_locked_handle(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            diagnostic_safety, "LOCK_DIR", directory
        ):
            with mock.patch.object(
                diagnostic_safety.time, "time", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    diagnostic_safety.acquire_channel_lock("can0")

            handle = diagnostic_safety.acquire_channel_lock("can0")
            diagnostic_safety.release_channel_lock(handle)

    def test_termination_handler_interrupts_once_and_restores_handlers(self):
        installed = {}
        restored = []
        old_handlers = {
            diagnostic_safety.signal.SIGTERM: object(),
            diagnostic_safety.signal.SIGHUP: object(),
        }

        def fake_signal(signum, handler):
            if callable(handler):
                installed[signum] = handler
                return old_handlers[signum]
            restored.append((signum, handler))
            return None

        with mock.patch.object(diagnostic_safety.signal, "signal", side_effect=fake_signal):
            with diagnostic_safety.interrupt_on_termination():
                with self.assertRaises(KeyboardInterrupt):
                    installed[diagnostic_safety.signal.SIGTERM](
                        diagnostic_safety.signal.SIGTERM, None
                    )
                self.assertIsNone(
                    installed[diagnostic_safety.signal.SIGHUP](
                        diagnostic_safety.signal.SIGHUP, None
                    )
                )

        self.assertEqual(
            restored,
            [
                (diagnostic_safety.signal.SIGTERM, old_handlers[diagnostic_safety.signal.SIGTERM]),
                (diagnostic_safety.signal.SIGHUP, old_handlers[diagnostic_safety.signal.SIGHUP]),
            ],
        )

    def test_cleanup_phase_ignores_first_and_repeated_termination_signals(self):
        old_handlers = {
            diagnostic_safety.signal.SIGTERM: mock.sentinel.old_term,
            diagnostic_safety.signal.SIGHUP: mock.sentinel.old_hup,
        }
        current = dict(old_handlers)

        def fake_signal(signum, handler):
            previous = current[signum]
            current[signum] = handler
            return previous

        with mock.patch.object(diagnostic_safety.signal, "signal", side_effect=fake_signal):
            with diagnostic_safety.interrupt_on_termination() as guard:
                guard.begin_cleanup()
                self.assertIsNone(current[diagnostic_safety.signal.SIGTERM](
                    diagnostic_safety.signal.SIGTERM, None
                ))
                self.assertIsNone(current[diagnostic_safety.signal.SIGHUP](
                    diagnostic_safety.signal.SIGHUP, None
                ))
                self.assertIsNone(current[diagnostic_safety.signal.SIGTERM](
                    diagnostic_safety.signal.SIGTERM, None
                ))
                self.assertEqual(guard.received_signal, diagnostic_safety.signal.SIGTERM)

        self.assertEqual(current, old_handlers)


if __name__ == "__main__":
    unittest.main()
