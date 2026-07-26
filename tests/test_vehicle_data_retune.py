import contextlib
import unittest
from unittest import mock

from lib import canbus, diagnostic_safety
from projects.vehicle_data import retune


def interface(
    *,
    bitrate=500000,
    listen_only=True,
    state="ERROR-ACTIVE",
    up=True,
    restart_ms=0,
):
    return canbus.InterfaceState(
        channel="can0",
        present=True,
        up=up,
        bitrate=bitrate,
        listen_only=listen_only,
        controller_state=state,
        restart_ms=restart_ms,
    )


class PassiveRetuneTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            retune, "_active_conflicting_services", return_value=()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    @contextlib.contextmanager
    def _termination_guard():
        yield mock.Mock(begin_cleanup=mock.Mock())

    def _common(self):
        handle = mock.Mock()
        handle.closed = False
        return (
            handle,
            mock.patch.object(
                retune.diagnostic_safety,
                "interrupt_on_termination",
                side_effect=self._termination_guard,
            ),
            mock.patch.object(
                retune.diagnostic_safety,
                "acquire_channel_lock",
                return_value=handle,
            ),
            mock.patch.object(
                retune.diagnostic_safety, "release_channel_lock"
            ),
        )

    def test_wrong_rate_switches_500k_to_verified_bcan_passively(self):
        handle, termination, acquire, release = self._common()
        initial = interface(bitrate=500000)
        target = interface(bitrate=125000)
        with (
            termination,
            acquire,
            release as release_mock,
            mock.patch.object(
                retune.can_operation_state,
                "active_inhibits",
                return_value=(),
            ),
            mock.patch.object(
                retune.canbus,
                "interface_state",
                side_effect=(initial, target),
            ),
            mock.patch.object(
                retune.canbus,
                "identify_bus",
                side_effect=("wrong-rate", "b-can"),
            ),
            mock.patch.object(
                retune.canbus, "bring_up_passive", return_value=True
            ) as bring_up,
            mock.patch.object(retune, "_set_topology") as set_topology,
        ):
            result = retune.attempt_passive_retune(
                "can0", 500000, probe_seconds=0.1
            )

        self.assertEqual(result["state"], "switched")
        self.assertEqual(result["bus"], "b-can")
        self.assertEqual(result["to_bitrate"], 125000)
        bring_up.assert_called_once_with(
            "can0",
            125000,
            restart_ms=0,
            noninteractive=True,
        )
        self.assertEqual(
            set_topology.call_args_list,
            [
                mock.call(
                    "can0",
                    "unknown",
                    note="invalidated before passive telemetry auto-retune",
                ),
                mock.call(
                    "can0",
                    "b-can",
                    note=(
                        "passively identified after guarded telemetry "
                        "bitrate auto-retune"
                    ),
                ),
            ],
        )
        release_mock.assert_called_once_with(handle)

    def test_external_inhibit_blocks_before_probe_or_reconfiguration(self):
        handle, termination, acquire, release = self._common()
        with (
            termination,
            acquire,
            release,
            mock.patch.object(
                retune.canbus,
                "interface_state",
                return_value=interface(),
            ),
            mock.patch.object(
                retune.can_operation_state,
                "active_inhibits",
                return_value=({"name": "alfaobd"},),
            ),
            mock.patch.object(retune.canbus, "identify_bus") as identify,
            mock.patch.object(
                retune.canbus, "bring_up_passive"
            ) as bring_up,
        ):
            result = retune.attempt_passive_retune("can0", 500000)

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "external_inhibit")
        self.assertIn("alfaobd", result["detail"])
        identify.assert_not_called()
        bring_up.assert_not_called()

    def test_known_active_can_service_is_reported_before_interface_access(self):
        with (
            mock.patch.object(
                retune, "_active_conflicting_services",
                return_value=("tpms-logger.service",),
            ),
            mock.patch.object(
                retune.diagnostic_safety,
                "interrupt_on_termination",
                side_effect=self._termination_guard,
            ),
            mock.patch.object(
                retune.diagnostic_safety,
                "acquire_channel_lock",
                return_value=mock.Mock(),
            ),
            mock.patch.object(
                retune.diagnostic_safety, "release_channel_lock"
            ),
            mock.patch.object(
                retune.canbus, "interface_state"
            ) as interface_state,
        ):
            result = retune.attempt_passive_retune("can0", 500000)

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "service_conflict")
        self.assertIn("tpms-logger.service", result["detail"])
        interface_state.assert_not_called()

    def test_silent_recheck_is_insufficient_evidence(self):
        handle, termination, acquire, release = self._common()
        with (
            termination,
            acquire,
            release,
            mock.patch.object(
                retune.canbus,
                "interface_state",
                return_value=interface(),
            ),
            mock.patch.object(
                retune.can_operation_state,
                "active_inhibits",
                return_value=(),
            ),
            mock.patch.object(
                retune.canbus, "identify_bus", return_value="silent"
            ),
            mock.patch.object(
                retune.canbus, "bring_up_passive"
            ) as bring_up,
        ):
            result = retune.attempt_passive_retune("can0", 500000)

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "insufficient_evidence")
        bring_up.assert_not_called()

    def test_armed_interface_is_never_probed_or_changed(self):
        handle, termination, acquire, release = self._common()
        with (
            termination,
            acquire,
            release,
            mock.patch.object(
                retune.canbus,
                "interface_state",
                return_value=interface(listen_only=False),
            ),
            mock.patch.object(
                retune.can_operation_state, "active_inhibits"
            ) as inhibits,
            mock.patch.object(retune.canbus, "identify_bus") as identify,
        ):
            result = retune.attempt_passive_retune("can0", 500000)

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "interface_armed")
        inhibits.assert_not_called()
        identify.assert_not_called()

    def test_unrecognized_alternate_restores_exact_initial_state(self):
        handle, termination, acquire, release = self._common()
        initial = interface(bitrate=500000, restart_ms=100)
        target = interface(bitrate=125000)
        with (
            termination,
            acquire,
            release,
            mock.patch.object(
                retune.can_operation_state,
                "active_inhibits",
                return_value=(),
            ),
            mock.patch.object(
                retune.canbus,
                "interface_state",
                side_effect=(initial, target, target, initial),
            ),
            mock.patch.object(
                retune.canbus,
                "identify_bus",
                side_effect=("wrong-rate", "silent"),
            ),
            mock.patch.object(
                retune.canbus, "bring_up_passive", return_value=True
            ) as bring_up,
            mock.patch.object(retune, "_set_topology"),
        ):
            result = retune.attempt_passive_retune("can0", 500000)

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "alternate_not_identified")
        self.assertEqual(
            bring_up.call_args_list,
            [
                mock.call(
                    "can0",
                    125000,
                    restart_ms=0,
                    noninteractive=True,
                ),
                mock.call(
                    "can0",
                    500000,
                    restart_ms=100,
                    noninteractive=True,
                ),
            ],
        )

    def test_failed_restore_overrides_the_original_retune_failure(self):
        handle, termination, acquire, release = self._common()
        initial = interface(bitrate=500000, restart_ms=100)
        target = interface(bitrate=125000)
        with (
            termination,
            acquire,
            release,
            mock.patch.object(
                retune.can_operation_state,
                "active_inhibits",
                return_value=(),
            ),
            mock.patch.object(
                retune.canbus,
                "interface_state",
                side_effect=(initial, target, target),
            ),
            mock.patch.object(
                retune.canbus,
                "identify_bus",
                side_effect=("wrong-rate", "unknown"),
            ),
            mock.patch.object(
                retune.canbus,
                "bring_up_passive",
                side_effect=(True, False),
            ),
            mock.patch.object(retune, "_set_topology"),
        ):
            result = retune.attempt_passive_retune("can0", 500000)

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "restoration_failed")

    def test_degraded_controller_can_confirm_prior_wrong_rate_by_target_bus(self):
        handle, termination, acquire, release = self._common()
        initial = interface(bitrate=500000, state="ERROR-PASSIVE")
        target = interface(bitrate=125000)
        with (
            termination,
            acquire,
            release,
            mock.patch.object(
                retune.can_operation_state,
                "active_inhibits",
                return_value=(),
            ),
            mock.patch.object(
                retune.canbus,
                "interface_state",
                side_effect=(initial, target),
            ),
            mock.patch.object(
                retune.canbus,
                "identify_bus",
                side_effect=("silent", "b-can"),
            ),
            mock.patch.object(
                retune.canbus, "bring_up_passive", return_value=True
            ),
            mock.patch.object(retune, "_set_topology"),
        ):
            result = retune.attempt_passive_retune("can0", 500000)

        self.assertEqual(result["state"], "switched")
        self.assertEqual(result["bus"], "b-can")

    def test_channel_lock_contention_is_reported(self):
        with (
            mock.patch.object(
                retune.diagnostic_safety,
                "interrupt_on_termination",
                side_effect=self._termination_guard,
            ),
            mock.patch.object(
                retune.diagnostic_safety,
                "acquire_channel_lock",
                side_effect=diagnostic_safety.ChannelLockError("observer owns can0"),
            ),
            mock.patch.object(
                retune.diagnostic_safety, "release_channel_lock"
            ) as release,
        ):
            result = retune.attempt_passive_retune("can0", 500000)

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "channel_busy")
        release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
