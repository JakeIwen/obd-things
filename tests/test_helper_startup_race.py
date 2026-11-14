"""Regression: the broker cancelling a helper that has not started must not latch.

Drives the real ``AuxiliaryDriveSupervisor`` through the real broker epoch
orchestration (``_run_active_drive_if_ready``): the B-CAN helper is launched and
held before readiness, the C-CAN side answers ``can_busy``, and the broker stops
the auxiliary supervisor.  Only the helper process and the CAN-side supervisor are
scripted; nothing here can reach CAN.
"""

import contextlib
import threading
import unittest
from unittest import mock

from projects.vehicle_data.broker import AuxiliaryDriveSupervisor
from tests.helper_startup_support import StartupRaceCase, WAIT_SECONDS

CAN_BUSY_FINAL = {
    "type": "final",
    "state": "idle",
    "reason": "can_busy",
    "detail": "another participating CAN operation owns can7",
    "interface_mode": "unknown",
    "restored": None,
}


class CancelledBeforeStartupTests(StartupRaceCase):
    def test_broker_cancelling_an_unstarted_auxiliary_helper_does_not_latch(self):
        launcher = self.launcher()
        auxiliary = AuxiliaryDriveSupervisor(
            channel="can8",
            expected_usb_serial="serial-b",
            expected_dev_id=1,
            event_handler=lambda _event: None,
            popen_factory=launcher,
            queue_poll_seconds=0.01,
        )

        class BusyActiveSupervisor:
            def run(self, _stop):
                # The auxiliary helper is launched and held before readiness.
                assert launcher.spawned.wait(WAIT_SECONDS)
                return dict(CAN_BUSY_FINAL)

        broker = self.make_broker(active=BusyActiveSupervisor(), auxiliary=auxiliary)
        broker._passive_engine_evidence = "running"
        with mock.patch(
            "projects.vehicle_data.broker.can_operation_state.begin_inhibit"
        ) as begin_inhibit:
            broker._run_active_drive_if_ready()

        self.assertEqual(launcher.process.terminate_calls, 1)
        begin_inhibit.assert_not_called()
        self.assertFalse(broker._auxiliary_drive_restoration_latched)
        status = broker.status_response()["auxiliary_drive"]
        self.assertFalse(status["restoration_failed"])
        self.assertNotEqual(status["reason"], "restoration_failed")

    def broker_with_failing_launch(self, *, pipe_fails):
        holder = {}
        attempted = threading.Event()

        def factory(_command, **_kwargs):
            attempted.set()
            raise OSError("no such interpreter")

        spawn = mock.Mock(side_effect=factory)

        def no_pipe():
            attempted.set()
            raise OSError("emfile")

        auxiliary = AuxiliaryDriveSupervisor(
            channel="can8",
            expected_usb_serial="serial-b",
            expected_dev_id=1,
            event_handler=lambda event: holder["broker"].handle_auxiliary_drive_event(event),
            popen_factory=spawn,
            queue_poll_seconds=0.01,
        )

        class IdleActive:
            def run(self, _stop):
                # Hold the epoch open until the launch has failed, so the broker's
                # own cancel cannot pre-empt the failure under test.
                assert attempted.wait(WAIT_SECONDS)
                return dict(CAN_BUSY_FINAL)

        broker = self.make_broker(active=IdleActive(), auxiliary=auxiliary)
        holder["broker"] = broker
        broker._passive_engine_evidence = "running"
        patches = [mock.patch(
            "projects.vehicle_data.broker.can_operation_state.begin_inhibit"
        )]
        if pipe_fails:
            patches.append(mock.patch(
                "projects.vehicle_data.broker.StartupGrant", side_effect=no_pipe
            ))
        with contextlib.ExitStack() as stack:
            begin_inhibit = stack.enter_context(patches[0])
            for patch in patches[1:]:
                stack.enter_context(patch)
            broker._run_active_drive_if_ready()
        return broker, begin_inhibit, spawn

    def test_failed_helper_start_is_not_a_restoration_failure(self):
        for pipe_fails in (True, False):
            with self.subTest(pipe_fails=pipe_fails):
                broker, begin_inhibit, spawn = self.broker_with_failing_launch(
                    pipe_fails=pipe_fails
                )
                begin_inhibit.assert_not_called()
                self.assertFalse(broker._auxiliary_drive_restoration_latched)
                status = broker.status_response()["auxiliary_drive"]
                self.assertFalse(status["restoration_failed"])
                self.assertEqual(status["reason"], "helper_start_failed")
                if pipe_fails:
                    spawn.assert_not_called()
                else:
                    spawn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
