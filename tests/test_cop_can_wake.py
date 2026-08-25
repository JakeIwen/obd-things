import json
import pathlib
import stat
from types import SimpleNamespace
import tempfile
import threading
import unittest


from projects.vehicle_data import cop_can_wake as cop


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self, value=100.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class FakeClient:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = 0

    def request(self, method, path):
        self.calls += 1
        if method != "GET" or path != "/v1/status":
            raise AssertionError("unexpected broker request")
        return self.status, self.payload


class FakePublisher:
    def __init__(self):
        self.payloads = []

    def publish(self, payload):
        self.payloads.append(dict(payload))


class FakeWakeError(RuntimeError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def safe_status(*, state="asleep", running=False, age_ms=100):
    return {
        "service": "van-telemetry",
        "collector": {"state": "running"},
        "active_drive": {
            "state": "idle",
            "interface_mode": "listen_only",
            "helper_pid": None,
            "restoration_failed": False,
        },
        "vehicle_state": {
            "state": state,
            "running": running,
            "age_ms": age_ms,
            "basis": (
                "passive_bus_silence"
                if state == "asleep"
                else (
                    "passive_bus_activity"
                    if state == "awake"
                    else "qualified_ccan_0x0fc_engine_speed"
                )
            ),
        },
        "interface": {
            "active_inhibits": [],
            "role_interfaces": {
                "roles": {
                    "c-can": {
                        "resolution": "resolved",
                        "passive_ready": True,
                        "expected": {
                            "role": "c-can",
                            "pair": "6/14",
                            "bitrate": 500_000,
                            "dev_id": 0,
                            "usb_serial": "fixture-serial",
                        },
                        "actual": {
                            "up": True,
                            "bitrate": 500_000,
                            "fd_enabled": False,
                            "one_shot": False,
                            "listen_only": True,
                            "controller_state": "ERROR-ACTIVE",
                            "restart_ms": 0,
                        },
                    }
                }
            },
        },
        "usb_can_monitor": {
            "enabled": True,
            "state": "running",
            "active_count": 0,
            "last_error": None,
        },
    }


class BrokerGateTests(unittest.TestCase):
    def test_exact_fresh_stopped_state_is_accepted(self):
        self.assertEqual(cop.broker_safety_conflicts(safe_status()), ())
        self.assertEqual(
            cop.broker_safety_conflicts(
                safe_status(state="ignition_on", running=False)
            ),
            (),
        )

    def test_fresh_awake_activity_after_our_own_wake_is_accepted(self):
        self.assertEqual(
            cop.broker_safety_conflicts(
                safe_status(state="awake", running=None)
            ),
            (),
        )

    def test_unknown_running_state_without_wake_activity_still_blocks(self):
        conflicts = cop.broker_safety_conflicts(
            safe_status(state="unknown", running=None)
        )
        self.assertTrue(any("parked wake state" in item for item in conflicts))

    def test_running_stale_active_drive_and_usb_fault_each_block(self):
        running = safe_status(state="running", running=True)
        self.assertTrue(cop.broker_safety_conflicts(running))

        stale = safe_status(age_ms=cop.MAX_BROKER_START_STATE_AGE_MS + 1)
        self.assertTrue(
            any(
                "missing or stale" in item
                for item in cop.broker_safety_conflicts(stale)
            )
        )
        typed_wrong = safe_status(age_ms="100")
        self.assertTrue(cop.broker_safety_conflicts(typed_wrong))

        active = safe_status()
        active["active_drive"]["state"] = "armed_diagnostic"
        self.assertTrue(cop.broker_safety_conflicts(active))

        usb_fault = safe_status()
        usb_fault["usb_can_monitor"]["active_count"] = 1
        self.assertTrue(cop.broker_safety_conflicts(usb_fault))

    def test_verified_ignition_gate_blocks_even_at_zero_rpm(self):
        status = safe_status(state="ignition_on", running=False)
        status["vehicle_state"]["basis"] = "ccan_0x2ef_ignition_gate"
        conflicts = cop.broker_safety_conflicts(status)
        self.assertTrue(any("ignition-on" in item for item in conflicts))

    def test_transaction_has_a_bounded_nonextendable_freshness_allowance(self):
        in_progress = safe_status(age_ms=5_000)
        self.assertEqual(
            cop.broker_safety_conflicts(
                in_progress,
                require_passive_role=False,
                max_vehicle_state_age_ms=cop.MAX_BROKER_TRANSACTION_STATE_AGE_MS,
            ),
            (),
        )
        expired = safe_status(
            age_ms=cop.MAX_BROKER_TRANSACTION_STATE_AGE_MS + 1
        )
        self.assertTrue(
            cop.broker_safety_conflicts(
                expired,
                require_passive_role=False,
                max_vehicle_state_age_ms=cop.MAX_BROKER_TRANSACTION_STATE_AGE_MS,
            )
        )

    def test_transaction_callback_does_not_race_cores_armed_state(self):
        status = safe_status()
        c_can = status["interface"]["role_interfaces"]["roles"]["c-can"]
        c_can["passive_ready"] = False
        c_can["actual"]["listen_only"] = False
        self.assertTrue(cop.broker_safety_conflicts(status))
        self.assertEqual(
            cop.broker_safety_conflicts(status, require_passive_role=False),
            (),
        )


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tempdir.name)
        self.marker = root / "cop-alert.active"
        self.ignition = root / "ignition_is_on"
        self.clock = FakeClock()
        self.publisher = FakePublisher()
        self.client = FakeClient(safe_status())
        self.stop_event = threading.Event()
        self.events = []

    def tearDown(self):
        self.tempdir.cleanup()

    def supervisor(self, wake_once):
        return cop.CopCanWakeSupervisor(
            marker=self.marker,
            ignition_marker=self.ignition,
            broker_client=self.client,
            wake_once=wake_once,
            wake_error_type=FakeWakeError,
            status_publisher=self.publisher,
            monotonic=self.clock,
            wall_clock=lambda: 1_700_000_000.0 + self.clock.value,
            stop_event=self.stop_event,
            event_logger=self.events.append,
        )

    def arm_marker(self, supervisor):
        self.marker.touch()
        supervisor.tick()
        self.clock.advance(cop.ACTIVATION_DEBOUNCE_SECONDS)

    def test_new_button_marker_has_only_a_short_debounce(self):
        calls = []
        supervisor = self.supervisor(lambda *_args, **_kwargs: calls.append(1))
        self.marker.touch()

        supervisor.tick()

        self.assertEqual(calls, [])
        self.assertEqual(self.publisher.payloads[-1]["state"], "arming_delay")
        self.assertEqual(
            self.publisher.payloads[-1]["last_reason"],
            "activation_debounce",
        )
        self.clock.advance(cop.ACTIVATION_DEBOUNCE_SECONDS)
        supervisor.tick()
        self.assertEqual(calls, [1])

    def test_preexisting_marker_keeps_restart_grace(self):
        calls = []
        self.marker.touch()
        supervisor = self.supervisor(lambda *_args, **_kwargs: calls.append(1))

        supervisor.tick()
        self.clock.advance(cop.ACTIVATION_DEBOUNCE_SECONDS)
        supervisor.tick()
        self.assertEqual(calls, [])
        self.assertEqual(
            self.publisher.payloads[-1]["last_reason"],
            "preexisting_marker_delay",
        )

        self.clock.advance(
            cop.PREEXISTING_MARKER_DELAY_SECONDS
            - cop.ACTIVATION_DEBOUNCE_SECONDS
        )
        supervisor.tick()
        self.assertEqual(calls, [1])

    def test_fixed_role_wake_rechecks_callback_and_restores_before_cadence(self):
        calls = []

        def wake_once(role, *, prearm_check):
            calls.append(role)
            self.assertEqual(prearm_check(), ())
            if len(calls) == 1:
                # The first fixed poke wakes C-CAN, so the cache can briefly
                # know only that passive bus activity is present.
                self.client.payload = safe_status(state="awake", running=None)
            return SimpleNamespace(detail="fixed RF Hub response verified; restored")

        supervisor = self.supervisor(wake_once)
        self.arm_marker(supervisor)
        supervisor.tick()

        self.assertEqual(calls, ["c-can"])
        self.assertEqual(supervisor.wake_count, 1)
        self.assertEqual(self.publisher.payloads[-1]["state"], "active_waiting")
        supervisor.tick()
        self.assertEqual(calls, ["c-can"])
        self.clock.advance(cop.SUCCESS_CADENCE_SECONDS)
        supervisor.tick()
        self.assertEqual(calls, ["c-can", "c-can"])

    def test_marker_removed_in_prearm_callback_blocks_send(self):
        callback_conflicts = []

        def wake_once(_role, *, prearm_check):
            self.marker.unlink()
            callback_conflicts.extend(prearm_check())
            raise FakeWakeError("can_busy", "prearm callback rejected marker loss")

        supervisor = self.supervisor(wake_once)
        self.arm_marker(supervisor)
        supervisor.tick()

        self.assertTrue(any("marker" in item for item in callback_conflicts))
        self.assertEqual(supervisor.wake_count, 0)
        self.assertEqual(supervisor.last_reason, "can_busy")

    def test_ignition_marker_pauses_and_requires_new_stability_window(self):
        calls = []
        supervisor = self.supervisor(lambda *_args, **_kwargs: calls.append(1))
        self.arm_marker(supervisor)
        self.ignition.touch()

        supervisor.tick()

        self.assertEqual(calls, [])
        self.assertEqual(self.publisher.payloads[-1]["state"], "paused_ignition")
        self.ignition.unlink()
        supervisor.tick()
        self.assertEqual(self.publisher.payloads[-1]["state"], "arming_delay")

    def test_broker_unavailable_or_unknown_running_state_blocks(self):
        calls = []
        supervisor = self.supervisor(lambda *_args, **_kwargs: calls.append(1))
        self.arm_marker(supervisor)
        self.client.status = 503
        supervisor.tick()
        self.assertEqual(calls, [])
        self.assertEqual(self.publisher.payloads[-1]["state"], "blocked")

        self.clock.advance(cop.SAFETY_RETRY_SECONDS)
        self.client.status = 200
        self.client.payload = safe_status(state="unknown", running=None)
        supervisor.tick()
        self.assertEqual(calls, [])

    def test_transient_preflight_block_retries_quickly_and_is_retained(self):
        calls = []
        supervisor = self.supervisor(lambda *_args, **_kwargs: calls.append(1))
        self.client.payload = safe_status(
            age_ms=cop.MAX_BROKER_START_STATE_AGE_MS + 1
        )
        self.arm_marker(supervisor)
        supervisor.tick()

        self.assertEqual(calls, [])
        blocked = self.publisher.payloads[-1]
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["last_blocked_reason"], "safety_gate")
        self.assertIn("stale", blocked["last_blocked_detail"])

        self.client.payload = safe_status()
        self.clock.advance(cop.SAFETY_RETRY_SECONDS)
        supervisor.tick()
        self.assertEqual(calls, [1])

        self.marker.unlink()
        supervisor.tick()
        idle = self.publisher.payloads[-1]
        self.assertEqual(idle["state"], "idle")
        self.assertEqual(idle["last_blocked_reason"], "safety_gate")
        self.assertIn("stale", idle["last_blocked_detail"])
        self.assertTrue(any(event["state"] == "blocked" for event in self.events))

    def test_unexpected_or_restoration_exception_is_not_suppressed(self):
        def fail(_role, *, prearm_check):
            self.assertEqual(prearm_check(), ())
            raise RuntimeError("passive restoration was not proven")

        supervisor = self.supervisor(fail)
        self.arm_marker(supervisor)
        with self.assertRaisesRegex(RuntimeError, "restoration"):
            supervisor.tick()

    def test_interrupt_after_core_restoration_is_a_clean_service_stop(self):
        supervisor = self.supervisor(lambda *_args, **_kwargs: None)

        def interrupted_tick():
            raise KeyboardInterrupt

        supervisor.tick = interrupted_tick
        self.assertEqual(supervisor.run(), 0)
        self.assertTrue(self.stop_event.is_set())
        self.assertEqual(self.publisher.payloads[-1]["state"], "stopped")

    def test_broken_journal_sink_does_not_change_supervisor_state(self):
        supervisor = cop.CopCanWakeSupervisor(
            marker=self.marker,
            ignition_marker=self.ignition,
            broker_client=self.client,
            wake_once=lambda *_args, **_kwargs: None,
            wake_error_type=FakeWakeError,
            status_publisher=self.publisher,
            monotonic=self.clock,
            wall_clock=lambda: 1_700_000_000.0 + self.clock.value,
            stop_event=self.stop_event,
            event_logger=lambda _payload: (_ for _ in ()).throw(OSError("closed")),
        )

        self.assertEqual(supervisor.tick(), cop.IDLE_POLL_SECONDS)
        self.assertEqual(self.publisher.payloads[-1]["state"], "idle")


class UnitFileTests(unittest.TestCase):
    def test_public_status_text_removes_ephemeral_channel_and_control_text(self):
        text = cop.public_status_text("can17 failed\nretry\x00 now")
        self.assertEqual(text, "<resolved-can> failed retry now")

    def test_status_file_is_atomic_channel_free_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "status.json"
            publisher = cop.StatusPublisher(path, wall_clock=lambda: 1_700_000_000)
            publisher.publish({"state": "idle", "marker_active": False})
            payload = json.loads(path.read_text())
            self.assertEqual(payload["role"], "c-can")
            self.assertEqual(payload["state"], "idle")
            self.assertNotIn("channel", payload)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_service_uses_fixed_supervisor_and_no_legacy_channel(self):
        unit = (
            REPO_ROOT
            / "projects"
            / "vehicle_data"
            / "systemd"
            / "van-cop-can-wake.service"
        ).read_text()
        self.assertIn("cop_can_wake.py", unit)
        self.assertIn("--confirm-fixed-c-can-wake", unit)
        self.assertIn("RuntimeDirectory=van-cop-can-wake", unit)
        self.assertIn("Wants=van-telemetry.service", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("TimeoutStopSec=30", unit)
        self.assertNotIn("can0", unit)
        self.assertNotIn("NoNewPrivileges=true", unit)


if __name__ == "__main__":
    unittest.main()
