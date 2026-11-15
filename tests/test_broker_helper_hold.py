"""The broker's drive-helper hold (`/v1/helpers/hold`, contract in helper_hold.py).

Drives the real broker epoch orchestration (``_run_active_drive_if_ready``) and,
where the stop path matters, the real ``ActiveDriveSupervisor`` with a scripted
helper process (``tests/helper_startup_support.py``). Persistent inhibit writes
are patched out; nothing here reaches CAN, a socket or a service.
"""

import threading
import time
import unittest
from unittest import mock

from projects.vehicle_data import api_server, helper_hold, web, web_v2
from projects.vehicle_data.broker import ActiveDriveSupervisor, TelemetryBroker
from projects.vehicle_data.helper_startup_grant import cancelled_before_grant_final
from projects.vehicle_data.http_common import route_name
from projects.vehicle_data.models import failure
from projects.vehicle_data.radar_service_control import TrackedRun
from projects.vehicle_data.radar_service_alignment import RUNNING
from tests.helper_startup_support import HelperLauncher, ScriptedHelper, WAIT_SECONDS
import tests.test_active_drive as drive_tests

HOLD_ON = {"hold": True, "reason": "restart_broker"}
HOLD_OFF = {"hold": False}
State = helper_hold.HelperHoldState

RESTORED = {
    "type": "final",
    "state": "idle",
    "reason": "engine_not_running",
    "detail": "terminated; listen-only restored",
    "interface_mode": "listen_only",
    "restored": True,
}
FAILED = {
    "type": "final",
    "state": "restoration_failed",
    "reason": "restoration_failed",
    "detail": "listen-only readback failed",
    "interface_mode": "armed_diagnostic",
    "restored": False,
}


class ScriptedSupervisor:
    """Runs until its stop condition or ``stop()``, then returns ``final``.

    A later run returns at once (the helper ended on its own).
    """

    def __init__(self, final=RESTORED):
        self.final = final
        self.calls = 0
        self.stop_calls = 0
        self.started = threading.Event()
        self.stopped = threading.Event()

    def run(self, stop):
        self.calls += 1
        self.started.set()
        deadline = time.monotonic() + WAIT_SECONDS
        while not (stop.is_set() or self.stopped.is_set()):
            if time.monotonic() > deadline:
                raise AssertionError("scripted helper was never stopped")
            time.sleep(0.005)
        return dict(self.final)

    def stop(self):
        self.stop_calls += 1
        self.stopped.set()


class NoStopWrapper:
    """A supervisor whose ``stop()`` reaches nothing, like the role-aware
    wrapper before it has built its delegate: only the stop condition remains."""

    def __init__(self, delegate):
        self.delegate = delegate

    def run(self, stop):
        return self.delegate.run(stop)

    def stop(self):
        pass


class HoldCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(
            "projects.vehicle_data.broker.can_operation_state.begin_inhibit"
        )
        self.begin_inhibit = patcher.start()
        self.addCleanup(patcher.stop)

    def broker(self, active, auxiliary=None):
        self.clock = drive_tests.BrokerActiveDriveTests.Clock()
        broker = TelemetryBroker(
            acquirer=drive_tests.BrokerActiveDriveTests.Acquirer(),
            monotonic=self.clock,
            active_drive_supervisor=active,
            active_drive_enabled=True,
            auxiliary_drive_supervisor=auxiliary,
            auxiliary_drive_enabled=auxiliary is not None,
        )
        broker._passive_engine_evidence = "running"
        return broker

    def epoch_in_thread(self, broker):
        thread = threading.Thread(target=broker._run_active_drive_if_ready, daemon=True)
        thread.start()
        self.addCleanup(thread.join, WAIT_SECONDS)
        return thread

    def join(self, thread):
        thread.join(WAIT_SECONDS)
        self.assertFalse(thread.is_alive(), "broker epoch did not finish")

    def states(self, broker):
        helpers = broker.helper_hold_response()["helpers"]
        return {name: helpers[name]["state"] for name in helper_hold.HELPERS}

    def hold_stopped_epoch(self, active_final=RESTORED, auxiliary_final=RESTORED):
        """A running epoch whose helpers the hold stops; returns the broker."""
        active = ScriptedSupervisor(active_final)
        auxiliary = ScriptedSupervisor(auxiliary_final)
        broker = self.broker(active, auxiliary)
        thread = self.epoch_in_thread(broker)
        self.assertTrue(active.started.wait(WAIT_SECONDS))
        self.assertTrue(auxiliary.started.wait(WAIT_SECONDS))
        code, body = broker.helper_hold_request(dict(HOLD_ON))
        self.assertEqual(code, 202)
        self.assertEqual(
            {name: body["helpers"][name]["state"] for name in helper_hold.HELPERS},
            {"active_drive": "stopping", "auxiliary_drive": "stopping"},
        )
        self.join(thread)
        return broker, active, auxiliary


class HoldGateTests(HoldCase):
    def test_hold_blocks_every_launch_and_reports_idle_helpers(self):
        active, auxiliary = ScriptedSupervisor(), ScriptedSupervisor()
        broker = self.broker(active, auxiliary)

        code, body = broker.helper_hold_request(dict(HOLD_ON))
        broker._run_active_drive_if_ready()

        self.assertEqual(code, 202)
        self.assertEqual(body, broker.helper_hold_response())
        self.assertEqual(body["held"], True)
        self.assertEqual(body["reason"], "restart_broker")
        self.assertEqual(body["remaining_seconds"], helper_hold.HOLD_SECONDS)
        self.assertEqual(body["helpers"], {
            "active_drive": {"state": "idle", "pid": None},
            "auxiliary_drive": {"state": "idle", "pid": None},
        })
        self.assertEqual((active.calls, auxiliary.calls), (0, 0))
        status = broker.status_response()
        for name in ("active_drive", "auxiliary_drive"):
            self.assertEqual(status[name]["state"], "idle")
            self.assertEqual(status[name]["reason"], helper_hold.HELD_BLOCKED_REASON)
            self.assertEqual(status[name]["interface_mode"], "listen_only")

    def test_unheld_broker_reports_no_hold(self):
        broker = self.broker(ScriptedSupervisor())
        self.assertEqual(broker.helper_hold_response(), {
            "held": False,
            "reason": None,
            "remaining_seconds": None,
            "helpers": {
                "active_drive": {"state": "idle", "pid": None},
                "auxiliary_drive": {"state": "idle", "pid": None},
            },
        })

    def test_hold_stops_running_helpers_and_classifies_each_final(self):
        cases = (
            (RESTORED, State.STOPPED_VERIFIED),
            (cancelled_before_grant_final("cancelled before the grant"),
             State.STOPPED_NO_MUTATION),
            (FAILED, State.STOPPED_UNVERIFIED),
        )
        for final, expected in cases:
            with self.subTest(expected=expected.value):
                self.begin_inhibit.reset_mock()
                broker, active, auxiliary = self.hold_stopped_epoch(final, final)
                self.assertGreaterEqual(active.stop_calls, 1)
                self.assertGreaterEqual(auxiliary.stop_calls, 1)
                self.assertEqual(self.states(broker), {
                    "active_drive": expected.value,
                    "auxiliary_drive": expected.value,
                })
                self.assertTrue(broker.helper_hold_response()["held"])
                if expected is State.STOPPED_UNVERIFIED:
                    # Exactly today's handling: latch plus wildcard inhibit.
                    self.assertTrue(broker._active_drive_restoration_latched)
                    self.assertTrue(broker._auxiliary_drive_restoration_latched)
                    self.assertGreaterEqual(self.begin_inhibit.call_count, 1)
                else:
                    self.begin_inhibit.assert_not_called()
                    self.assertFalse(broker._active_drive_restoration_latched)
                    self.assertEqual(
                        broker._active_drive_blocked_reason,
                        helper_hold.HELD_BLOCKED_REASON,
                    )
                    self.assertEqual(
                        broker._auxiliary_drive_blocked_reason,
                        helper_hold.HELD_BLOCKED_REASON,
                    )

    def test_renewal_extends_the_expiry_and_keeps_states(self):
        broker, _active, _auxiliary = self.hold_stopped_epoch()
        before = broker.helper_hold_response()
        self.clock.value += 100.0

        code, body = broker.helper_hold_request(
            {"hold": True, "reason": "renewed"}
        )

        self.assertEqual(code, 202)
        self.assertEqual(body["remaining_seconds"], helper_hold.HOLD_SECONDS)
        self.assertEqual(body["reason"], before["reason"])
        self.assertEqual(body["helpers"], before["helpers"])


class HoldReleaseTests(HoldCase):
    def test_expiry_releases_and_lifts_held_so_the_next_epoch_relaunches(self):
        broker, active, auxiliary = self.hold_stopped_epoch()
        self.clock.value += helper_hold.HOLD_SECONDS - 1.0
        broker._run_active_drive_if_ready()
        self.assertEqual((active.calls, auxiliary.calls), (1, 1))
        self.assertTrue(broker.helper_hold_response()["held"])

        self.clock.value += 1.0
        broker._run_active_drive_if_ready()

        self.assertFalse(broker.helper_hold_response()["held"])
        self.assertEqual((active.calls, auxiliary.calls), (2, 2))
        # The relaunched helpers ended on their own, after the hold: no `held`.
        self.assertEqual(broker._active_drive_blocked_reason, "engine_not_running")
        self.assertEqual(broker._auxiliary_drive_blocked_reason, "engine_not_running")
        self.assertEqual(self.states(broker), {
            "active_drive": "stopped_verified",
            "auxiliary_drive": "stopped_verified",
        })

    def test_hold_off_releases_and_the_next_epoch_relaunches(self):
        broker, active, auxiliary = self.hold_stopped_epoch()

        code, body = broker.helper_hold_request(dict(HOLD_OFF))
        self.assertEqual(code, 200)
        self.assertEqual(body, broker.helper_hold_response())
        self.assertFalse(body["held"])
        self.assertIsNone(body["reason"])
        self.assertIsNone(body["remaining_seconds"])
        broker._run_active_drive_if_ready()

        self.assertEqual((active.calls, auxiliary.calls), (2, 2))

    def test_hold_off_before_the_stop_completes_leaves_no_block(self):
        stop_seen, release = threading.Event(), threading.Event()

        class GatedSupervisor(ScriptedSupervisor):
            def run(self, stop):
                if self.calls:
                    return super().run(stop)
                final = super().run(stop)
                stop_seen.set()
                release.wait(WAIT_SECONDS)  # the final is still on its way
                return final

        active = GatedSupervisor()
        broker = self.broker(active)
        thread = self.epoch_in_thread(broker)
        self.assertTrue(active.started.wait(WAIT_SECONDS))
        broker.helper_hold_request(dict(HOLD_ON))
        self.assertTrue(stop_seen.wait(WAIT_SECONDS))
        broker.helper_hold_request(dict(HOLD_OFF))
        release.set()
        self.join(thread)

        self.assertEqual(self.states(broker)["active_drive"], "stopped_verified")
        self.assertIsNone(broker._active_drive_blocked_reason)
        broker._run_active_drive_if_ready()
        self.assertEqual(active.calls, 2)

    def test_a_blocking_failure_outlives_the_hold(self):
        # Ended during the hold with a reason that blocks the rest of the epoch.
        session = dict(RESTORED, reason="session_required", detail="NRC 7F")
        broker, active, auxiliary = self.hold_stopped_epoch(session, session)
        self.assertEqual(broker._active_drive_blocked_reason, "session_required")

        broker.helper_hold_request(dict(HOLD_OFF))
        broker._run_active_drive_if_ready()

        self.assertEqual((active.calls, auxiliary.calls), (1, 1))
        self.assertEqual(broker._active_drive_blocked_reason, "session_required")
        self.assertEqual(
            broker.status_response()["active_drive"]["state"],
            "blocked_until_engine_stop",
        )

    def test_a_block_set_before_the_hold_survives_hold_off(self):
        active, auxiliary = ScriptedSupervisor(), ScriptedSupervisor()
        broker = self.broker(active, auxiliary)
        broker._auxiliary_drive_blocked_reason = "response_timeout"
        broker._auxiliary_drive_blocked_detail = "earlier B-CAN failure"

        broker.helper_hold_request(dict(HOLD_ON))
        broker._run_active_drive_if_ready()
        broker.helper_hold_request(dict(HOLD_OFF))
        broker._run_active_drive_if_ready()

        self.assertEqual((active.calls, auxiliary.calls), (1, 0))
        self.assertEqual(broker._auxiliary_drive_blocked_reason, "response_timeout")


class LaunchRaceTests(HoldCase):
    def supervisor(self, launcher):
        return ActiveDriveSupervisor(
            channel="can7",
            expected_usb_serial="serial-a",
            expected_dev_id=0,
            event_handler=lambda _event: None,
            popen_factory=launcher,
            queue_poll_seconds=0.01,
        )

    def test_hold_set_while_the_helper_spawns_cancels_it_before_the_grant(self):
        holder = {}
        replies = []
        launcher = HelperLauncher(
            on_spawn=lambda _launcher: replies.append(
                holder["broker"].helper_hold_request(dict(HOLD_ON))
            )
        )
        self.addCleanup(launcher.close)
        # stop() reaches nothing here, so only the hold in the stop condition
        # can cancel this launch.
        broker = self.broker(NoStopWrapper(self.supervisor(launcher)))
        holder["broker"] = broker

        broker._run_active_drive_if_ready()

        self.assertEqual(replies[0][0], 202)
        self.assertEqual(replies[0][1]["helpers"]["active_drive"]["state"], "stopping")
        self.assertEqual(launcher.read_grant(), b"")  # EOF: never granted
        self.assertEqual(launcher.process.terminate_calls, 1)
        self.assertEqual(self.states(broker)["active_drive"], "stopped_no_mutation")
        self.begin_inhibit.assert_not_called()
        self.assertFalse(broker._active_drive_restoration_latched)

    def test_hold_after_the_grant_terminates_and_collects_the_restoration(self):
        process = ScriptedHelper(die_on_terminate=False)
        launcher = HelperLauncher(process)
        self.addCleanup(launcher.close)
        broker = self.broker(self.supervisor(launcher))
        thread = self.epoch_in_thread(broker)
        self.assertTrue(launcher.spawned.wait(WAIT_SECONDS))
        process.ready()
        self.assertEqual(launcher.read_grant(), b"G")

        code, body = broker.helper_hold_request(dict(HOLD_ON))
        self.assertEqual((code, body["helpers"]["active_drive"]["state"]), (202, "stopping"))
        self.assertTrue(process.terminated.wait(WAIT_SECONDS))
        process.emit(dict(RESTORED))
        process.die(0)
        self.join(thread)

        self.assertEqual(self.states(broker)["active_drive"], "stopped_verified")
        self.assertEqual(broker._active_drive_blocked_reason, helper_hold.HELD_BLOCKED_REASON)
        self.begin_inhibit.assert_not_called()

    def test_hold_landing_between_gate_and_claim_prevents_the_launch(self):
        active = ScriptedSupervisor()
        broker = self.broker(active)
        claim = broker._claim_helper_launch

        def hold_then_claim(helper):
            broker.helper_hold_request(dict(HOLD_ON))
            return claim(helper)

        with mock.patch.object(broker, "_claim_helper_launch", side_effect=hold_then_claim):
            broker._run_active_drive_if_ready()

        self.assertEqual(active.calls, 0)
        self.assertEqual(self.states(broker)["active_drive"], "idle")
        self.assertEqual(
            broker.status_response()["active_drive"]["reason"],
            helper_hold.HELD_BLOCKED_REASON,
        )


class HoldRefusalTests(HoldCase):
    def test_inexact_bodies_are_refused(self):
        broker = self.broker(ScriptedSupervisor())
        bodies = (
            {}, [], None, "hold", {"hold": "yes", "reason": "x"},
            {"hold": 1, "reason": "x"}, {"hold": True}, {"hold": True, "reason": ""},
            {"hold": True, "reason": "x" * (helper_hold.REASON_MAX_LENGTH + 1)},
            {"hold": True, "reason": "line\nbreak"}, {"hold": True, "reason": 5},
            {"hold": True, "reason": "x", "extra": 1}, {"hold": False, "reason": "x"},
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    broker.helper_hold_request(body), (400, {"error": "bad_request"})
                )
        self.assertFalse(broker.helper_hold_response()["held"])

    def test_active_alignment_run_refuses_the_hold(self):
        active = ScriptedSupervisor()
        broker = self.broker(active)
        broker.radar_service._run = TrackedRun(
            "a" * 32, requested_at="t", updated_at="t", state=RUNNING
        )
        with mock.patch.object(broker.radar_service._pipe, "alive", return_value=True):
            reply = broker.helper_hold_request(dict(HOLD_ON))

        self.assertEqual(reply, (409, {"error": "alignment_active"}))
        self.assertFalse(broker.helper_hold_response()["held"])
        self.assertEqual(active.stop_calls, 0)

    def recording_acquirer(self, broker):
        calls = []
        broker.acquirer.acquire = lambda mode: calls.append(mode) or failure(
            metric="battery.voltage", unit="V", reason="source_unavailable",
            detail="scripted source",
        )
        return calls

    def test_hold_refuses_wake_acquisitions_with_409(self):
        broker = self.broker(ScriptedSupervisor())
        calls = self.recording_acquirer(broker)
        broker.helper_hold_request(dict(HOLD_ON))

        result = broker.acquire("battery.voltage", "wake_if_asleep")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, "helpers_held")
        self.assertEqual(calls, [])
        handler = mock.Mock()
        handler.broker = broker
        api_server.TelemetryApiHandler._acquisition_response(
            handler, "battery.voltage", result
        )
        self.assertEqual(handler._json.call_args.args[0], 409)

    def test_hold_leaves_passive_acquisitions_alone(self):
        broker = self.broker(ScriptedSupervisor())
        calls = self.recording_acquirer(broker)
        broker.helper_hold_request(dict(HOLD_ON))

        broker.acquire("battery.voltage", "passive")

        self.assertEqual(calls, ["passive"])


class ClaimCleanupTests(HoldCase):
    """A claim ends however the run ends, and the run's own exception survives."""

    def test_failed_final_processing_releases_the_claim_as_unverified(self):
        active = ScriptedSupervisor()
        broker = self.broker(active)
        thread_errors = []

        def failing_epoch():
            try:
                broker._run_active_drive_if_ready()
            except RuntimeError as exc:
                thread_errors.append(exc)

        boom = RuntimeError("final processing failed")
        with mock.patch.object(broker, "handle_active_drive_event", side_effect=boom):
            thread = threading.Thread(target=failing_epoch, daemon=True)
            thread.start()
            self.assertTrue(active.started.wait(WAIT_SECONDS))
            broker.helper_hold_request(dict(HOLD_ON))
            self.join(thread)

        self.assertEqual(thread_errors, [boom])  # the original exception, unchanged
        self.assertEqual(self.states(broker)["active_drive"], "stopped_unverified")
        # Latch policy is unchanged: only a validated failed restoration latches.
        self.assertFalse(broker._active_drive_restoration_latched)
        self.begin_inhibit.assert_not_called()
        broker.helper_hold_request(dict(HOLD_OFF))
        broker.helper_hold_request(dict(HOLD_ON))
        self.assertEqual(self.states(broker)["active_drive"], "idle")  # claim released

    def test_unheld_failure_leaves_no_claim_for_a_later_hold(self):
        broker = self.broker(ScriptedSupervisor())
        broker.active_drive_supervisor.stopped.set()  # the helper ends on its own
        with mock.patch.object(
            broker, "handle_active_drive_event", side_effect=RuntimeError("boom")
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                broker._run_active_drive_if_ready()

        code, body = broker.helper_hold_request(dict(HOLD_ON))
        self.assertEqual(body["helpers"]["active_drive"]["state"], "idle")

    def test_failed_auxiliary_processing_releases_its_claim_as_unverified(self):
        active, auxiliary = ScriptedSupervisor(), ScriptedSupervisor()
        broker = self.broker(active, auxiliary)
        with mock.patch.object(
            broker, "handle_auxiliary_drive_event", side_effect=RuntimeError("aux")
        ), mock.patch("threading.excepthook"):
            thread = self.epoch_in_thread(broker)
            self.assertTrue(auxiliary.started.wait(WAIT_SECONDS))
            broker.helper_hold_request(dict(HOLD_ON))
            self.join(thread)

        states = self.states(broker)
        self.assertEqual(states["auxiliary_drive"], "stopped_unverified")
        self.assertEqual(states["active_drive"], "stopped_verified")
        self.assertFalse(broker._auxiliary_drive_restoration_latched)


class HoldRouteTests(unittest.TestCase):
    def test_broker_socket_serves_the_hold_route(self):
        handler_class = api_server.TelemetryApiHandler
        self.assertEqual(
            handler_class.GET_PRODUCTS[helper_hold.API_PATH], "helper_hold_response"
        )
        self.assertEqual(
            route_name(helper_hold.API_PATH, handler_class.POST_ROUTES),
            "_helper_hold_post",
        )
        handler = mock.Mock()
        handler.broker.helper_hold_request.return_value = (400, {"error": "bad_request"})
        handler_class._helper_hold_post(handler, helper_hold.API_PATH, {"hold": 1})
        handler.broker.helper_hold_request.assert_called_once_with({"hold": 1})
        handler._json.assert_called_once_with(400, {"error": "bad_request"})

    def test_web_listeners_do_not_relay_the_hold_route(self):
        # web_v2 hands every unclaimed /v1 GET and every POST to web.py's tables.
        dashboard = web_v2.DashboardHandler
        self.assertEqual(
            route_name(helper_hold.API_PATH, dashboard.DASHBOARD_GET_ROUTES), "_base_get"
        )
        for table in (
            web.TelemetryWebHandler.GET_ROUTES,
            web.TelemetryWebHandler.POST_ROUTES,
            dashboard.GET_ROUTES,
            dashboard.POST_ROUTES,
        ):
            with self.subTest(table=table):
                self.assertIsNone(route_name(helper_hold.API_PATH, table))


if __name__ == "__main__":
    unittest.main()
