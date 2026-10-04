"""Ordering and exception-precedence checks for interval worker cleanup."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from projects.vehicle_data import drive_recorder


class IntervalWorkerCleanupTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.threads = {}
        self.settled = {}
        for index, role in enumerate(drive_recorder.SECONDARY_ROLES, 1):
            thread = mock.Mock(ident=index)
            thread.start.side_effect = lambda role=role: self.events.append(("start", role))
            thread.join.side_effect = lambda timeout, role=role: self.events.append(
                ("join", role, timeout)
            )
            thread.is_alive.return_value = False
            self.threads[role] = thread
            event = mock.Mock()
            event.wait.side_effect = lambda remaining, role=role: (
                self.events.append(("wait", role, remaining)) or True
            )
            self.settled[role] = event
        self.primary = mock.Mock()
        self.primary.run.side_effect = lambda: self.events.append(("primary",))
        self.health = mock.Mock(side_effect=lambda: self.events.append(("health",)))
        self.recorders = drive_recorder._IntervalRecorders(
            self.primary, {}, self.threads, self.settled,
            SimpleNamespace(set=lambda: self.events.append(("stop",))), self.health,
        )

    @contextmanager
    def campaign_lock(self, _run_dir):
        self.events.append(("lock_enter",))
        try:
            yield
        finally:
            self.events.append(("lock_exit",))

    def run_workers(self):
        with (
            mock.patch.object(
                drive_recorder.capture, "campaign_file_lock", self.campaign_lock
            ),
            mock.patch.object(
                drive_recorder.time, "monotonic", side_effect=[10.0, 11.0, 12.0]
            ),
        ):
            drive_recorder._run_interval_recorders(Path("/unused"), self.recorders)

    def fail_primary(self, failure):
        def run():
            self.events.append(("primary",))
            raise failure
        self.primary.run.side_effect = run

    def cleanup_events(self):
        return [
            ("lock_exit",), ("stop",),
            ("join", "b-can", drive_recorder.SECONDARY_JOIN_TIMEOUT_SECONDS),
            ("join", "can-ch", drive_recorder.SECONDARY_JOIN_TIMEOUT_SECONDS),
        ]

    def test_lock_release_precedes_stop_and_join_then_final_health(self):
        self.run_workers()
        self.assertEqual(self.events, [
            ("lock_enter",), ("start", "b-can"), ("start", "can-ch"),
            ("wait", "b-can", 7.0), ("wait", "can-ch", 6.0),
            ("health",), ("primary",), *self.cleanup_events(), ("health",),
        ])

    def test_primary_failure_propagates_unchanged_after_cleanup(self):
        failure = OSError("primary storage failed")
        self.fail_primary(failure)
        with self.assertRaises(OSError) as raised:
            self.run_workers()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.events[-4:], self.cleanup_events())
        self.assertEqual(self.health.call_count, 1)

    def test_secondary_failure_overrides_primary_ownership_loss_after_cleanup(self):
        self.fail_primary(drive_recorder.BrokerOwnershipLost("owner disappeared"))
        failure = drive_recorder.DriveRecorderError("secondary compression failed")

        def health():
            self.events.append(("health",))
            if self.health.call_count == 2:
                raise failure

        self.health.side_effect = health
        with self.assertRaises(drive_recorder.DriveRecorderError) as raised:
            self.run_workers()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.events[-5:], [*self.cleanup_events(), ("health",)])

    def test_missing_start_witness_converts_only_after_cleanup(self):
        failure = drive_recorder.capture.CaptureError("required start CAN ID missing")
        self.fail_primary(failure)
        with self.assertRaises(drive_recorder.BrokerOwnershipLost) as raised:
            self.run_workers()
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(str(raised.exception), str(failure))
        self.assertEqual(self.events[-4:], self.cleanup_events())
        self.assertEqual(self.health.call_count, 1)

    def test_cleanup_timeout_takes_precedence_over_primary_failure(self):
        self.fail_primary(OSError("primary failed"))
        self.threads["b-can"].is_alive.return_value = True
        with self.assertRaisesRegex(
            drive_recorder.DriveRecorderError, "secondary recorder cleanup timed out: b-can"
        ):
            self.run_workers()
        self.assertEqual(self.events[-4:], self.cleanup_events())
        self.assertEqual(self.health.call_count, 1)

    def test_startup_timeout_checks_health_before_cleanup(self):
        self.settled["b-can"].wait.side_effect = lambda remaining: (
            self.events.append(("wait", "b-can", remaining)) or False
        )
        with self.assertRaisesRegex(
            drive_recorder.DriveRecorderError, "required b-can recorder did not start"
        ):
            self.run_workers()
        self.primary.run.assert_not_called()
        self.assertEqual(self.events, [
            ("lock_enter",), ("start", "b-can"), ("start", "can-ch"),
            ("wait", "b-can", 7.0), ("health",), *self.cleanup_events(),
        ])

    def test_partial_thread_start_joins_only_started_threads(self):
        failure = KeyboardInterrupt()
        self.threads["can-ch"].ident = None

        def start():
            self.events.append(("start", "can-ch"))
            raise failure

        self.threads["can-ch"].start.side_effect = start
        with self.assertRaises(KeyboardInterrupt) as raised:
            self.run_workers()
        self.assertIs(raised.exception, failure)
        self.primary.run.assert_not_called()
        self.health.assert_not_called()
        self.threads["can-ch"].join.assert_not_called()
        self.assertEqual(self.events, [
            ("lock_enter",), ("start", "b-can"), ("start", "can-ch"),
            ("lock_exit",), ("stop",),
            ("join", "b-can", drive_recorder.SECONDARY_JOIN_TIMEOUT_SECONDS),
        ])
