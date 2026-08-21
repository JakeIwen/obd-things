from types import SimpleNamespace
import unittest
from unittest import mock

from lib.alfaobd_adb import UiState, WaitOutcome
from tools import alfaobd_controller as controller


class AlfaCampaignGateTests(unittest.TestCase):
    def test_campaign_begin_creates_external_inhibit_without_adb(self):
        with (
            mock.patch.object(
                controller.can_operation_state,
                "begin_inhibit",
                return_value={"name": "alfaobd"},
            ) as begin,
            mock.patch.object(controller, "_live_objects") as live,
        ):
            self.assertEqual(controller.main(["campaign-begin"]), 0)

        begin.assert_called_once_with(
            "alfaobd",
            channel="*",
            reason="explicit AlfaOBD campaign begin",
        )
        live.assert_not_called()

    def test_campaign_end_is_explicit_and_does_not_open_adb(self):
        with (
            mock.patch.object(
                controller.can_operation_state,
                "end_inhibit",
                return_value=True,
            ) as end,
            mock.patch.object(controller, "_live_objects") as live,
        ):
            self.assertEqual(controller.main(["campaign-end"]), 0)

        end.assert_called_once_with("alfaobd")
        live.assert_not_called()

    def test_plan_only_action_does_not_create_inhibit(self):
        with mock.patch.object(
            controller.can_operation_state, "begin_inhibit"
        ) as begin:
            self.assertEqual(controller.main(["action", "connect"]), 0)
        begin.assert_not_called()

    def test_executed_action_creates_inhibit_before_live_objects(self):
        events = []
        snapshot = SimpleNamespace(
            primary=UiState.DISCONNECTED,
            states=frozenset((UiState.DISCONNECTED,)),
        )
        result = SimpleNamespace(
            outcome=WaitOutcome.MATCHED,
            attempts=1,
            elapsed_seconds=0.1,
            evidence_prefix=None,
            snapshot=snapshot,
        )

        def begin(*_args, **_kwargs):
            events.append("inhibit")
            return {}

        def live(_args):
            events.append("adb")
            return "serial", object(), object()

        with (
            mock.patch.object(
                controller.can_operation_state,
                "begin_inhibit",
                side_effect=begin,
            ),
            mock.patch.object(controller, "_live_objects", side_effect=live),
            mock.patch.object(
                controller.GuardedController,
                "perform",
                return_value=result,
            ),
        ):
            self.assertEqual(
                controller.main(["action", "disconnect", "--execute"]),
                0,
            )

        self.assertEqual(events, ["inhibit", "adb"])

    def test_adapter_prompt_sets_global_inhibit_without_topology_stamp(self):
        snapshot = SimpleNamespace(states=frozenset((UiState.ADAPTER_PROMPT,)))
        with (
            mock.patch.object(
                controller.can_operation_state, "begin_inhibit"
            ) as begin,
            mock.patch.object(
                controller.can_operation_state, "set_topology"
            ) as set_topology,
        ):
            controller._inhibit_for_adapter_prompt(snapshot)

        begin.assert_called_once_with(
            "alfaobd",
            channel="*",
            reason="AlfaOBD adapter prompt requires external campaign review",
        )
        set_topology.assert_not_called()


if __name__ == "__main__":
    unittest.main()
