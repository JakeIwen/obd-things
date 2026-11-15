"""A starting broker inhibits a vehicle adapter left armed with no owner.

Drives one real collector cycle per reconciliation result, with the
reconciler's outcome as the seam (contract in armed_at_startup.py).
Inhibit reads and writes are patched; nothing touches CAN or the state dir.
"""

import unittest
from unittest import mock

from projects.vehicle_data.armed_at_startup import INHIBIT_PREFIX, assess_startup_roles
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.models import failure
import tests.test_active_drive as drive_tests
from tests.test_broker_helper_hold import ScriptedSupervisor

ROLES = ("c-can", "b-can", "can-ch")
CHANNELS = {"c-can": "can0", "b-can": "can1", "can-ch": "can2", "spare": "can3"}


def full_pass(**states):
    """A full reconciliation result; every role ready unless given a state."""
    return {
        "generated_at": "2026-11-15T00:00:00+00:00",
        "interaction": "SocketCAN link configuration only; no CAN frames sent",
        "roles": {
            role: {
                "role": role,
                "state": states.get(role.replace("-", "_"), "ready"),
                "channel": channel,
                "detail": "",
                "changed": False,
            }
            for role, channel in CHANNELS.items()
        },
        "ready": not states,
    }


class Acquirer(drive_tests.BrokerActiveDriveTests.Acquirer):
    def acquire(self, mode):
        return failure(metric="battery.voltage", unit="V",
                       reason="source_unavailable", detail="scripted source")


class Reconciler:
    """Returns one scripted result per collector cycle, then stops the loop."""

    def __init__(self):
        self.broker = None
        self.result = None

    def reconcile_if_needed(self):
        self.broker._collector_stop.set()
        return self.result


class StartupCase(unittest.TestCase):
    def setUp(self):
        self.reconciler = Reconciler()
        self.broker = self.make_broker()
        self.inhibits = []
        for name, kwargs in (
            ("begin_inhibit", {}),
            ("all_active_inhibits", {"side_effect": lambda: tuple(self.inhibits)}),
        ):
            patcher = mock.patch(
                f"projects.vehicle_data.broker.can_operation_state.{name}", **kwargs
            )
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def make_broker(self, **kwargs):
        broker = TelemetryBroker(
            acquirer=Acquirer(),
            monotonic=drive_tests.BrokerActiveDriveTests.Clock(),
            interface_reconciler=self.reconciler,
            **kwargs,
        )
        self.reconciler.broker = broker
        return broker

    def cycle(self, result):
        self.reconciler.result = result
        self.broker._collector_stop.clear()
        self.broker._collector_loop()

    def written(self):
        return [
            (call.args[0], call.kwargs["channel"]) for call in self.begin_inhibit.call_args_list
        ]


class ArmedAtStartupTests(StartupCase):
    def test_armed_unowned_vehicle_role_is_inhibited_for_every_helper(self):
        self.cycle(full_pass(c_can="armed"))

        self.assertEqual(self.written(), [(f"{INHIBIT_PREFIX}-c-can", "*")])
        reason = self.begin_inhibit.call_args.kwargs["reason"]
        self.assertIn("c-can (can0)", reason)
        self.assertLessEqual(len(reason), 240)

    def test_a_role_held_by_another_owner_is_not_inhibited(self):
        # A participating owner holds the role/channel lock: the reconciler says busy.
        self.cycle(full_pass(c_can="busy"))
        # This broker's own claimed helper owns its role.
        broker = self.broker = self.make_broker()
        self.assertTrue(broker._helper_hold.claim("auxiliary_drive"))
        self.cycle(full_pass(b_can="armed"))

        self.begin_inhibit.assert_not_called()

    def test_listen_only_roles_and_the_spare_channel_are_left_alone(self):
        self.cycle(full_pass(spare="armed"))
        self.begin_inhibit.assert_not_called()

    def test_an_existing_inhibit_gets_no_duplicate(self):
        for existing in ({"name": "vehicle-data-restoration-failed", "channel": "*"},
                         {"name": "manual-ccan", "channel": "can0"}):
            with self.subTest(existing=existing["name"]):
                self.inhibits[:] = [existing]
                self.broker = self.make_broker()
                self.cycle(full_pass(c_can="armed"))
                self.begin_inhibit.assert_not_called()
                self.assertNotIn("c-can", self.broker._startup_unassessed)

    def test_a_conclusively_assessed_role_is_not_assessed_again(self):
        self.cycle(full_pass())
        self.cycle(full_pass(c_can="armed"))
        self.begin_inhibit.assert_not_called()
        self.assertEqual(self.broker._startup_unassessed, set())

    def test_an_inconclusive_first_outcome_leaves_the_role_pending(self):
        for first in ("failed", "unavailable", "busy", "topology_changed"):
            with self.subTest(first=first):
                self.begin_inhibit.reset_mock()
                self.broker = self.make_broker()
                self.cycle(full_pass(can_ch=first))
                self.assertEqual(self.broker._startup_unassessed, {"can-ch"})

                self.cycle(full_pass(can_ch="armed"))

                self.assertEqual(self.written(), [(f"{INHIBIT_PREFIX}-can-ch", "*")])
                self.assertEqual(self.broker._startup_unassessed, set())

    def test_no_conclusion_until_a_pass_completes_without_an_active_acquisition(self):
        self.cycle({"enabled": True, "ready": False, "detail": "reconciliation failed"})
        with self.broker._lock:
            self.broker._inflight[("battery.voltage", "wake_if_asleep")] = object()
        self.cycle(full_pass(c_can="armed"))
        self.begin_inhibit.assert_not_called()
        with self.broker._lock:
            self.broker._inflight.clear()

        self.cycle(full_pass(c_can="armed"))

        self.assertEqual(self.written(), [(f"{INHIBIT_PREFIX}-c-can", "*")])

    def test_a_failed_write_is_retried_on_the_next_pass(self):
        self.begin_inhibit.side_effect = [OSError("read-only"), None]
        with mock.patch("sys.stderr"):
            self.cycle(full_pass(b_can="armed"))
        # The retry needs no new evidence: the fault is latched in memory.
        self.cycle({"enabled": True, "ready": False, "detail": "reconciliation failed"})

        self.assertEqual(self.written(), [(f"{INHIBIT_PREFIX}-b-can", "*")] * 2)
        self.assertEqual(self.broker._armed_at_startup_faults, {})


class ArmedAtStartupAdmissionTests(StartupCase):
    """With active drive enabled, an unrecorded fault keeps every helper off."""

    def setUp(self):
        super().setUp()
        self.active = ScriptedSupervisor()
        self.active.stopped.set()  # each launched epoch ends at once
        self.broker = self.make_broker(
            active_drive_supervisor=self.active, active_drive_enabled=True,
        )
        patcher = mock.patch.object(
            self.broker, "_collect_passive_powertrain",
            side_effect=lambda _result: setattr(
                self.broker, "_passive_engine_evidence", "running"
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_helpers_stay_off_until_the_inhibit_is_recorded(self):
        # An orphaned CAN-CH adapter keeps the healthy C-CAN helper off too.
        self.begin_inhibit.side_effect = [OSError("read-only"), OSError("still"), None]
        with mock.patch("sys.stderr"):
            self.cycle(full_pass(can_ch="armed"))
            self.assertEqual(self.active.calls, 0)
            active = self.broker.status_response()["active_drive"]
            self.assertEqual((active["state"], active["reason"]), ("idle", "inhibited"))
            self.assertIn("can-ch", active["detail"])

            self.cycle(full_pass())
            self.assertEqual(self.active.calls, 0)

        self.cycle(full_pass())  # recorded on this cycle; the disk inhibit now governs

        self.assertEqual(self.written(), [(f"{INHIBIT_PREFIX}-can-ch", "*")] * 3)
        self.assertEqual(self.broker._armed_at_startup_faults, {})
        self.assertEqual(self.active.calls, 1)

    def test_no_fault_leaves_admission_alone(self):
        self.cycle(full_pass())
        self.assertEqual(self.active.calls, 1)
        self.begin_inhibit.assert_not_called()


class AssessStartupRolesTests(unittest.TestCase):
    def decide(self, result, **overrides):
        options = {
            "pending_roles": ROLES,
            "owned_roles": frozenset(),
            "inhibits": (),
            "nonpassive_inflight": False,
        }
        options.update(overrides)
        return assess_startup_roles(result, **options)

    def test_conclusive_outcomes_settle_roles_and_report_unaccounted_armed_ones(self):
        found = self.decide(full_pass(c_can="armed", can_ch="armed", b_can="busy"))
        self.assertEqual(found.assessed, {"c-can", "can-ch"})
        self.assertEqual([(r.role, r.channel) for r in found.armed],
                         [("c-can", "can0"), ("can-ch", "can2")])

    def test_nothing_settles_without_outcomes_or_during_an_active_acquisition(self):
        for result, inflight in (({"ready": False}, False), (full_pass(c_can="armed"), True)):
            found = self.decide(result, nonpassive_inflight=inflight)
            self.assertEqual((found.assessed, found.armed), (frozenset(), ()))

    def test_only_pending_and_unowned_roles_are_assessed(self):
        found = self.decide(full_pass(c_can="armed", b_can="armed"),
                            pending_roles=("b-can",), owned_roles=frozenset({"b-can"}))
        self.assertEqual((found.assessed, found.armed), (frozenset(), ()))

    def test_an_inhibit_on_another_channel_does_not_account_for_the_role(self):
        found = self.decide(
            full_pass(c_can="armed"), inhibits=({"name": "x", "channel": "can1"},)
        )
        self.assertEqual([r.role for r in found.armed], ["c-can"])


if __name__ == "__main__":
    unittest.main()
