"""A starting broker inhibits a vehicle adapter left armed with no owner.

Drives one real collector cycle per reconciliation result, with the
reconciler's outcome as the seam (contract in armed_at_startup.py).
Inhibit reads and writes are patched; nothing touches CAN or the state dir.
"""

import unittest
from unittest import mock

from projects.vehicle_data.armed_at_startup import INHIBIT_PREFIX, unowned_armed_roles
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.models import failure
import tests.test_active_drive as drive_tests

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


class ArmedAtStartupTests(unittest.TestCase):
    def setUp(self):
        self.reconciler = Reconciler()
        self.broker = TelemetryBroker(
            acquirer=Acquirer(),
            monotonic=drive_tests.BrokerActiveDriveTests.Clock(),
            interface_reconciler=self.reconciler,
        )
        self.reconciler.broker = self.broker
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

    def cycle(self, result):
        self.reconciler.result = result
        self.broker._collector_stop.clear()
        self.broker._collector_loop()

    def written(self):
        return [
            (call.args[0], call.kwargs["channel"]) for call in self.begin_inhibit.call_args_list
        ]

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
        broker = TelemetryBroker(
            acquirer=Acquirer(),
            monotonic=drive_tests.BrokerActiveDriveTests.Clock(),
            interface_reconciler=self.reconciler,
        )
        self.reconciler.broker = self.broker = broker
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
                self.broker._armed_at_startup_pending = True
                self.cycle(full_pass(c_can="armed"))
                self.begin_inhibit.assert_not_called()

    def test_only_the_first_full_pass_is_checked(self):
        self.cycle(full_pass())
        self.cycle(full_pass(c_can="armed"))
        self.begin_inhibit.assert_not_called()

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
        self.cycle(full_pass(b_can="armed"))

        self.assertEqual(self.written(), [(f"{INHIBIT_PREFIX}-b-can", "*")] * 2)
        self.assertFalse(self.broker._armed_at_startup_pending)


class UnownedArmedRolesTests(unittest.TestCase):
    def decide(self, result, **overrides):
        options = {
            "vehicle_roles": ROLES,
            "owned_roles": frozenset(),
            "inhibits": (),
            "nonpassive_inflight": False,
        }
        options.update(overrides)
        return unowned_armed_roles(result, **options)

    def test_every_unaccounted_armed_vehicle_role_is_reported(self):
        found = self.decide(full_pass(c_can="armed", can_ch="armed", b_can="busy"))
        self.assertEqual([(r.role, r.channel) for r in found],
                         [("c-can", "can0"), ("can-ch", "can2")])

    def test_no_conclusion_without_outcomes_or_during_an_active_acquisition(self):
        self.assertIsNone(self.decide({"ready": False}))
        self.assertIsNone(self.decide(full_pass(c_can="armed"), nonpassive_inflight=True))

    def test_an_inhibit_on_another_channel_does_not_account_for_the_role(self):
        found = self.decide(
            full_pass(c_can="armed"), inhibits=({"name": "x", "channel": "can1"},)
        )
        self.assertEqual([r.role for r in found], ["c-can"])


if __name__ == "__main__":
    unittest.main()
