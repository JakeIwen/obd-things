from __future__ import annotations

import contextlib
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from lib import can_wake, canbus, diagnostic_safety
from projects.vehicle_data.can_runtime import (
    PassiveRoleReconciler,
    RoleAwareActiveDriveSupervisor,
    RoleAwareAuxiliaryDriveSupervisor,
    RoleAwareCcanPowertrainReader,
    RoleAwareVoltageAcquirer,
    configure_classical_listen_only,
)
from projects.vehicle_data.models import success


def interface(
    channel: str,
    *,
    up: bool = True,
    bitrate: int = 500000,
    listen_only: bool = True,
    controller_state: str = "ERROR-ACTIVE",
    restart_ms: int = 0,
    one_shot: bool = False,
) -> canbus.InterfaceState:
    return canbus.InterfaceState(
        channel=channel,
        present=True,
        up=up,
        bitrate=bitrate,
        listen_only=listen_only,
        controller_state=controller_state,
        restart_ms=restart_ms,
        fd_enabled=False,
        one_shot=one_shot,
    )


class FakeResolution:
    def __init__(self, role, channel, bitrate, device=None):
        self.state = "resolved"
        self.spec = SimpleNamespace(
            role=role,
            bitrate=bitrate,
            pair="6/14" if role == "c-can" else None,
            board="A",
            connector="CAN1",
            usb_serial="test-serial",
            dev_id=0,
        )
        self.channel = channel
        self.device = device or object()

    def require_channel(self):
        return self.channel


class FakeTopology:
    def __init__(self, resolutions):
        self.resolutions = resolutions

    def resolution(self, role):
        return self.resolutions[role]


class FakeManager:
    def __init__(self, topology, *, lease=None, status=None):
        self._topology = topology
        self.lease = lease
        self._status = status or {}

    def topology(self):
        return self._topology

    def channel_for_bus(self, _bus):
        return self.lease.channel

    @contextlib.contextmanager
    def observe(self, _bus):
        yield self.lease

    def status_snapshot(self):
        return self._status


class PassiveConfigurationTests(unittest.TestCase):
    def test_classical_configuration_is_fixed_fd_off_and_verified(self):
        commands = []

        def run(command, **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(
            canbus,
            "interface_state",
            return_value=interface("can7", bitrate=500000),
        ):
            configured = configure_classical_listen_only(
                "can7",
                500000,
                run=run,
            )

        self.assertTrue(configured)
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0][-2:], ["can7", "down"])
        self.assertIn("fd", commands[1])
        self.assertEqual(commands[1][commands[1].index("fd") + 1], "off")
        self.assertEqual(
            commands[1][commands[1].index("listen-only") + 1],
            "on",
        )
        self.assertEqual(
            commands[1][commands[1].index("one-shot") + 1],
            "off",
        )
        self.assertEqual(
            commands[1][commands[1].index("restart-ms") + 1],
            "0",
        )
        self.assertEqual(commands[2][-2:], ["can7", "up"])

    def test_reconciler_refuses_to_change_an_armed_role(self):
        device = object()
        resolutions = {
            "c-can": FakeResolution("c-can", "can7", 500000, device),
            "b-can": FakeResolution("b-can", "can2", 125000),
            "can-ch": FakeResolution("can-ch", "can9", 500000),
            "spare": FakeResolution("spare", "can4", None),
        }
        manager = FakeManager(FakeTopology(resolutions))
        configure = mock.Mock(return_value=True)
        states = {
            "can7": interface("can7", listen_only=False),
            "can2": interface("can2", bitrate=125000),
            "can9": interface("can9"),
            "can4": interface(
                "can4",
                up=False,
                listen_only=False,
                controller_state="STOPPED",
            ),
        }
        reconciler = PassiveRoleReconciler(
            manager,
            configure=configure,
            keep_down=mock.Mock(return_value=True),
            interface_state_reader=lambda channel: states[channel],
            inhibit_reader=lambda _channel: (),
            topology_writer=mock.Mock(),
        )
        with mock.patch(
            "projects.vehicle_data.can_runtime.diagnostic_safety.channel_lock",
            side_effect=lambda _name: contextlib.nullcontext(object()),
        ):
            result = reconciler._one("c-can")

        self.assertEqual(result.state, "armed")
        self.assertFalse(result.changed)
        configure.assert_not_called()

    def test_reconciler_configures_only_after_role_and_channel_ownership(self):
        device = object()
        resolution = FakeResolution("c-can", "can7", 500000, device)
        manager = FakeManager(FakeTopology({"c-can": resolution}))
        configure = mock.Mock(return_value=True)
        lock_names = []

        @contextlib.contextmanager
        def locked(name):
            lock_names.append(name)
            yield object()

        reconciler = PassiveRoleReconciler(
            manager,
            configure=configure,
            interface_state_reader=lambda _channel: interface(
                "can7",
                up=False,
                controller_state="STOPPED",
            ),
            inhibit_reader=lambda _channel: (),
            topology_writer=mock.Mock(),
        )
        with mock.patch(
            "projects.vehicle_data.can_runtime.diagnostic_safety.channel_lock",
            side_effect=locked,
        ):
            result = reconciler._one("c-can")

        self.assertEqual(result.state, "reconciled")
        self.assertTrue(result.changed)
        self.assertEqual(lock_names, ["can-role-c-can", "can7"])
        configure.assert_called_once_with("can7", 500000)

    def test_ready_role_renumber_rewrites_channel_topology(self):
        def resolutions(channels):
            return {
                "c-can": FakeResolution("c-can", channels[0], 500000),
                "b-can": FakeResolution("b-can", channels[1], 125000),
                "can-ch": FakeResolution("can-ch", channels[2], 500000),
                "spare": FakeResolution("spare", channels[3], None),
            }

        old_channels = ("can0", "can1", "can2", "can3")
        new_channels = ("can2", "can3", "can0", "can1")
        manager = FakeManager(
            FakeTopology(resolutions(old_channels)),
            status={"ready": True, "generation": "generation-old", "roles": {}},
        )
        states = {
            channel: interface(
                channel,
                bitrate=125000 if channel in ("can1", "can3") else 500000,
            )
            for channel in old_channels
        }
        states["can3"] = interface(
            "can3", up=False, listen_only=False, controller_state="STOPPED"
        )
        topology_writer = mock.Mock()
        reconciler = PassiveRoleReconciler(
            manager,
            configure=mock.Mock(return_value=True),
            keep_down=mock.Mock(return_value=True),
            interface_state_reader=lambda channel: states[channel],
            inhibit_reader=lambda _channel: (),
            topology_writer=topology_writer,
        )
        with mock.patch(
            "projects.vehicle_data.can_runtime.diagnostic_safety.channel_lock",
            side_effect=lambda _name: contextlib.nullcontext(object()),
        ):
            first = reconciler.reconcile_if_needed()
            skipped = reconciler.reconcile_if_needed()
            first_call_count = topology_writer.call_count

            manager._topology = FakeTopology(resolutions(new_channels))
            manager._status = {
                "ready": True,
                "generation": "generation-new",
                "roles": {},
            }
            states.update(
                {
                    "can2": interface("can2"),
                    "can3": interface("can3", bitrate=125000),
                    "can0": interface("can0"),
                    "can1": interface(
                        "can1",
                        up=False,
                        listen_only=False,
                        controller_state="STOPPED",
                    ),
                }
            )
            renamed = reconciler.reconcile_if_needed()

        self.assertTrue(first["ready"])
        self.assertFalse(skipped["changed"])
        self.assertEqual(topology_writer.call_count, first_call_count + 3)
        rewritten_channels = {
            call.args[0]
            for call in topology_writer.call_args_list[first_call_count:]
        }
        self.assertEqual(rewritten_channels, {"can0", "can2", "can3"})
        self.assertEqual(renamed["generation"], "generation-new")

    def test_stable_generation_retries_only_the_unready_role(self):
        resolutions = {
            "c-can": FakeResolution("c-can", "can7", 500000),
            "b-can": FakeResolution("b-can", "can2", 125000),
            "can-ch": FakeResolution("can-ch", "can9", 500000),
            "spare": FakeResolution("spare", "can4", None),
        }
        manager = FakeManager(
            FakeTopology(resolutions),
            status={
                "ready": False,
                "generation": "stable-generation",
                "roles": {
                    "c-can": {"passive_ready": True, "safe": True},
                    "b-can": {"passive_ready": False, "safe": False},
                    "can-ch": {"passive_ready": True, "safe": True},
                    "spare": {"passive_ready": False, "safe": True},
                },
            },
        )
        states = {
            "can7": interface("can7"),
            "can2": interface(
                "can2",
                up=False,
                bitrate=125000,
                controller_state="STOPPED",
            ),
            "can9": interface("can9"),
            "can4": interface(
                "can4",
                up=False,
                listen_only=False,
                controller_state="STOPPED",
            ),
        }
        configure = mock.Mock(return_value=False)
        topology_writer = mock.Mock()
        lock_names = []

        @contextlib.contextmanager
        def locked(name):
            lock_names.append(name)
            yield object()

        reconciler = PassiveRoleReconciler(
            manager,
            configure=configure,
            keep_down=mock.Mock(return_value=True),
            interface_state_reader=lambda channel: states[channel],
            inhibit_reader=lambda _channel: (),
            topology_writer=topology_writer,
        )
        with mock.patch(
            "projects.vehicle_data.can_runtime.diagnostic_safety.channel_lock",
            side_effect=locked,
        ):
            startup = reconciler.reconcile_if_needed()
            lock_names.clear()
            configure.reset_mock()
            topology_writer.reset_mock()
            retry = reconciler.reconcile_if_needed()

        self.assertEqual(set(startup["roles"]), set(resolutions))
        self.assertFalse(startup["ready"])
        self.assertEqual(lock_names, ["can-role-b-can", "can2"])
        configure.assert_called_once_with("can2", 125000)
        topology_writer.assert_not_called()
        self.assertEqual(set(retry["roles"]), set(resolutions))
        self.assertFalse(retry["ready"])
        self.assertEqual(retry["generation"], "stable-generation")


class RoleAwareSourceTests(unittest.TestCase):
    def setUp(self):
        self.lease = SimpleNamespace(channel="can7")
        self.manager = FakeManager(
            FakeTopology({}),
            lease=self.lease,
            status={
                "roles": {
                    "c-can": {
                        "resolution": "resolved",
                        "channel": "can7",
                        "passive_ready": True,
                        "detail": "ready",
                        "actual": {
                            "up": True,
                            "bitrate": 500000,
                            "listen_only": True,
                            "controller_state": "ERROR-ACTIVE",
                            "restart_ms": 0,
                        },
                    }
                }
            },
        )

    def test_voltage_source_resolves_role_each_passive_read(self):
        expected = success(
            metric="battery.voltage",
            unit="V",
            value=12.8,
            source="ccan.broadcast.0x41a",
            bus="c-can",
            acquisition="passive",
            quality="verified",
            observed_monotonic=1.0,
        )
        delegate = mock.Mock()
        delegate.acquire.return_value = expected
        factory = mock.Mock(return_value=delegate)
        source = RoleAwareVoltageAcquirer(
            self.manager,
            delegate_factory=factory,
        )

        result = source.acquire("passive")

        self.assertIs(result, expected)
        factory.assert_called_once_with(
            channel="can7",
            expected_bus="c-can",
            probe_seconds=0.75,
            read_timeout=2.0,
        )
        delegate.acquire.assert_called_once_with("passive")
        self.assertEqual(source.status_snapshot()["channel"], "can7")

    def test_unready_other_role_does_not_suppress_healthy_ccan(self):
        self.manager._status.update(
            ready=False,
            roles={
                **self.manager._status["roles"],
                "b-can": {
                    "resolution": "missing",
                    "channel": None,
                    "passive_ready": False,
                    "detail": "B-CAN adapter is unavailable",
                    "actual": {},
                },
            },
        )
        expected = success(
            metric="battery.voltage",
            unit="V",
            value=12.7,
            source="ccan.broadcast.0x41a",
            bus="c-can",
            acquisition="passive",
            quality="verified",
            observed_monotonic=2.0,
        )
        delegate = mock.Mock()
        delegate.acquire.return_value = expected
        source = RoleAwareVoltageAcquirer(
            self.manager,
            delegate_factory=mock.Mock(return_value=delegate),
        )

        result = source.acquire("passive")
        status = source.status_snapshot()

        self.assertIs(result, expected)
        self.assertTrue(status["topology"]["usable"])
        self.assertFalse(status["role_interfaces"]["ready"])

    def test_voltage_observer_yields_to_reserved_active_handoff(self):
        factory = mock.Mock()
        source = RoleAwareVoltageAcquirer(
            self.manager,
            delegate_factory=factory,
        )
        with mock.patch(
            "projects.vehicle_data.can_runtime.can_handoff.passive_turn",
            side_effect=diagnostic_safety.ChannelLockError("active turn reserved"),
        ):
            result = source.acquire("passive")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, "can_busy")
        self.assertIn("reserved active handoff", result.detail)
        factory.assert_not_called()

    def test_role_mode_rejects_nonpassive_acquisition(self):
        source = RoleAwareVoltageAcquirer(self.manager)

        result = source.acquire("transmit")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, "unsupported_mode")
        self.assertIn("passive or wake_if_asleep", result.detail)

    def test_wake_mode_is_passive_first_then_fixed_b_can_only(self):
        ccan = mock.Mock()
        ccan.acquire.return_value = SimpleNamespace(
            available=False, reason="bus_asleep"
        )
        bcan = mock.Mock()
        bcan.acquire.return_value = SimpleNamespace(
            available=False, reason="bus_asleep"
        )
        factory = mock.Mock(side_effect=(ccan, bcan))
        wake = mock.Mock(
            return_value=can_wake.WakeResult(
                role="b-can",
                source="bcan.network_wake.0x7ff",
                detail="fixed wake validated",
                voltage=12.45,
            )
        )
        prearm = mock.Mock(return_value=())
        source = RoleAwareVoltageAcquirer(
            self.manager,
            delegate_factory=factory,
            wake_once=wake,
            wake_prearm_check=prearm,
        )

        result = source.acquire("wake_if_asleep")

        self.assertTrue(result.available)
        self.assertEqual(result.bus, "b-can")
        self.assertEqual(result.source, "bcan.broadcast.0x46c")
        self.assertEqual(result.acquisition, "wake_assisted")
        self.assertEqual(result.value, 12.45)
        self.assertIn("restoration verified", result.detail)
        self.assertEqual(
            [call.kwargs["expected_bus"] for call in factory.call_args_list],
            ["c-can", "b-can"],
        )
        wake.assert_called_once_with(
            "b-can", prearm_check=prearm, manager=self.manager
        )

    def test_wake_restoration_failure_is_never_published(self):
        asleep = mock.Mock()
        asleep.acquire.return_value = SimpleNamespace(
            available=False, reason="bus_asleep"
        )
        source = RoleAwareVoltageAcquirer(
            self.manager,
            delegate_factory=mock.Mock(side_effect=(asleep, asleep)),
            wake_once=mock.Mock(
                side_effect=canbus.PassiveRestoreError("restore unverified")
            ),
            wake_prearm_check=lambda: (),
        )

        result = source.acquire("wake_if_asleep")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, "restoration_failed")

    def test_powertrain_reader_uses_resolved_channel(self):
        reader = RoleAwareCcanPowertrainReader(self.manager)
        observations = (SimpleNamespace(metric="engine.rpm"),)
        quality_event = SimpleNamespace(reason="implausible_transition")
        with (
            mock.patch.object(canbus, "identify_bus", return_value="c-can") as identify,
            mock.patch.object(
                __import__(
                    "projects.vehicle_data.ccan_powertrain",
                    fromlist=["read_broadcast_snapshot"],
                ),
                "read_broadcast_snapshot",
                return_value=SimpleNamespace(
                    observations=observations,
                    quality_events=(quality_event,),
                ),
            ) as read,
        ):
            result = reader.read()

        self.assertEqual(result, observations)
        self.assertEqual(reader.drain_quality_events(), (quality_event,))
        self.assertEqual(reader.drain_quality_events(), ())
        identify.assert_called_once_with("can7", probe=0.25)
        read.assert_called_once_with(
            "can7",
            timeout=0.5,
            temperature_gate=reader.temperature_gate,
        )

    def test_powertrain_observer_yields_to_reserved_active_handoff(self):
        reader = RoleAwareCcanPowertrainReader(self.manager)
        with (
            mock.patch(
                "projects.vehicle_data.can_runtime.can_handoff.passive_turn",
                side_effect=diagnostic_safety.ChannelLockError(
                    "active turn reserved"
                ),
            ),
            mock.patch.object(canbus, "identify_bus") as identify,
        ):
            self.assertEqual(reader.read(), ())

        identify.assert_not_called()

    def test_active_supervisor_binds_dynamic_channel_and_usb_identity(self):
        resolution = FakeResolution("c-can", "can7", 500000)
        self.manager._topology = FakeTopology({"c-can": resolution})
        delegate = mock.Mock()
        delegate.run.return_value = {"type": "final", "restored": True}
        factory = mock.Mock(return_value=delegate)
        supervisor = RoleAwareActiveDriveSupervisor(
            self.manager,
            event_handler=mock.Mock(),
            supervisor_factory=factory,
        )

        with mock.patch(
            "projects.vehicle_data.can_runtime.diagnostic_safety.channel_lock",
            side_effect=lambda _name: contextlib.nullcontext(object()),
        ):
            result = supervisor.run(object())

        self.assertTrue(result["restored"])
        factory.assert_called_once_with(
            channel="can7",
            event_handler=supervisor.event_handler,
            expected_usb_serial="test-serial",
            expected_dev_id=0,
        )
        delegate.run.assert_called_once()

    def test_auxiliary_supervisor_binds_dynamic_bcan_identity(self):
        resolution = FakeResolution("b-can", "can8", 125000)
        self.manager._topology = FakeTopology({"b-can": resolution})
        delegate = mock.Mock()
        delegate.run.return_value = {"type": "final", "restored": True}
        factory = mock.Mock(return_value=delegate)
        supervisor = RoleAwareAuxiliaryDriveSupervisor(
            self.manager,
            event_handler=mock.Mock(),
            supervisor_factory=factory,
        )

        with mock.patch(
            "projects.vehicle_data.can_runtime.diagnostic_safety.channel_lock",
            side_effect=lambda _name: contextlib.nullcontext(object()),
        ):
            result = supervisor.run(object())

        self.assertTrue(result["restored"])
        factory.assert_called_once_with(
            channel="can8",
            event_handler=supervisor.event_handler,
            expected_usb_serial="test-serial",
            expected_dev_id=0,
        )
        delegate.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
