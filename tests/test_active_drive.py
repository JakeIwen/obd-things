import json
import socket
import struct
import subprocess
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from lib import canbus, diagnostic_safety
from lib.modules import MODULES
from projects.vehicle_data import active_drive, ccan_powertrain, pcm_electrical
from projects.vehicle_data.broker import ActiveDriveSupervisor, TelemetryBroker
from projects.vehicle_data.models import success


TEST_CHANNEL = "can7"
TEST_SERIAL = "serial-a"
TEST_DEV_ID = 0


def interface(*, listen_only, restart_ms=0, fd_enabled=False):
    return canbus.InterfaceState(
        channel=TEST_CHANNEL,
        present=True,
        up=True,
        bitrate=500000,
        listen_only=listen_only,
        controller_state="ERROR-ACTIVE",
        restart_ms=restart_ms,
        fd_enabled=fd_enabled,
    )


def snapshot(
    rpm_samples,
    *observations,
    frame_count=12,
    completed_monotonic=100.0,
):
    return ccan_powertrain.BroadcastSnapshot(
        observations=tuple(observations),
        rpm_samples=tuple(rpm_samples),
        frame_count=frame_count,
        completed_monotonic=completed_monotonic,
    )


class FakeDiagnosticLock:
    closed = False
    _diagnostic_lock_held = True
    _diagnostic_lock_channel = TEST_CHANNEL
    _diagnostic_lock_mode = "exclusive"

    def fileno(self):
        return 99


def transmit_authorization(
    purpose,
    *,
    clock=lambda: 10.0,
    rpm_samples=(750.0, 751.0, 752.0),
    frame_count=12,
    lock_handle=None,
):
    evidence_at = clock()
    return active_drive.transmit_permit.issue(
        lock_handle or FakeDiagnosticLock(),
        snapshot(
            rpm_samples,
            frame_count=frame_count,
            completed_monotonic=evidence_at,
        ),
        purpose=purpose,
        channel=TEST_CHANNEL,
        monotonic=clock,
    )


def rpm_observation(value=750.0):
    return ccan_powertrain.PassiveObservation(
        metric="engine.rpm",
        value=value,
        unit="rpm",
        source="ccan.broadcast.0x0fc",
        quality="observed_alfa_scale",
        detail="qualified test replay",
    )


def speed_observation(value=31.2):
    return ccan_powertrain.PassiveObservation(
        metric="vehicle.speed",
        value=value,
        unit="mph",
        source="ccan.broadcast.0x101",
        quality="observed_alfa_scale",
        detail="qualified test replay",
    )


def battery_observation(value=13.5):
    return ccan_powertrain.PassiveObservation(
        metric="battery.voltage",
        value=value,
        unit="V",
        source="ccan.broadcast.0x41a",
        quality="verified",
        detail="verified active-owner replay",
    )


class EventSink:
    def __init__(self):
        self.events = []

    def emit(self, event_type, **payload):
        self.events.append({"type": event_type, **payload})


class FakePcmPoller:
    def __init__(
        self,
        result=None,
        torque_result=None,
        exception=None,
        events=None,
    ):
        self.result = result or SimpleNamespace(
            available=True,
            metric="generator.field_duty",
            value=100.008,
            unit="%",
            source="pcm.did.01a1",
            bus="c-can",
            quality="observed_alfa_scale",
            detail="exact 62 01A1 replay",
        )
        self.torque_result = torque_result or SimpleNamespace(
            available=True,
            metric="engine.crankshaft_torque",
            value=177.6,
            unit="lb-ft",
            source="pcm.did.06da",
            bus="c-can",
            quality="observed_alfa_scale",
            detail="exact 62 06DA replay",
        )
        self.exception = exception
        self.events = events
        self.poll_count = 0
        self.permits = []
        self.closed = False

    def poll(self, permit):
        self.poll_count += 1
        self.permits.append(permit)
        if self.events is not None:
            self.events.append("pcm_poll")
        if self.exception is not None:
            raise self.exception
        return self.result

    def poll_crankshaft_torque(self, permit):
        self.poll_count += 1
        self.permits.append(permit)
        if self.events is not None:
            self.events.append("pcm_torque_poll")
        if self.exception is not None:
            raise self.exception
        return self.torque_result

    def close(self):
        self.closed = True
        if self.events is not None:
            self.events.append("pcm_close")


class FakeTpmsPoller:
    def __init__(self, events=None):
        self.events = events
        self.poll_count = 0
        self.permits = []
        self.closed = False

    def poll_next(self, permit):
        self.poll_count += 1
        self.permits.append(permit)
        if self.events is not None:
            self.events.append("tpms_poll")
        return active_drive.PressureResult(
            True,
            "tire.pressure.fl",
            value=63.4,
            source="rf_hub.did.31d0",
            detail="fixed RF Hub replay",
        )

    def close(self):
        self.closed = True
        if self.events is not None:
            self.events.append("tpms_close")


class FakeBackend:
    channel = TEST_CHANNEL

    def __init__(
        self,
        *,
        snapshots,
        topology_bus="c-can",
        topology_pair="6/14",
        topology_usable=True,
        identified_bus="c-can",
        inhibits=(),
        restore_ok=True,
        pcm=None,
        tpms=None,
        events=None,
    ):
        self.snapshots = list(snapshots)
        self.topologies = [
            SimpleNamespace(
                bus=topology_bus,
                pair=topology_pair,
                usable=topology_usable,
                reason="test topology",
            )
        ]
        self.identified_bus = identified_bus
        self.inhibit_values = list(inhibits)
        self.restore_ok = restore_ok
        self.pcm = pcm or FakePcmPoller(events=events)
        self.tpms = tpms or FakeTpmsPoller(events=events)
        self.events = events if events is not None else []
        self.armed = False
        self.arm_count = 0
        self.restore_count = 0
        self.open_pcm_count = 0
        self.open_tpms_count = 0

    def interface_state(self):
        return interface(listen_only=not self.armed)

    def topology(self):
        if len(self.topologies) > 1:
            return self.topologies.pop(0)
        return self.topologies[0]

    def inhibits(self):
        return tuple(self.inhibit_values)

    def identify_bus(self):
        return self.identified_bus

    def broadcast_snapshot(self, _timeout):
        if not self.snapshots:
            raise AssertionError("test backend exhausted broadcast snapshots")
        return self.snapshots.pop(0)

    def arm(self, _initial):
        self.arm_count += 1
        self.events.append("arm")
        self.armed = True
        return True

    def restore(self, _initial):
        self.restore_count += 1
        self.events.append("restore")
        if self.restore_ok:
            self.armed = False
        return self.restore_ok

    def open_pcm(self):
        self.open_pcm_count += 1
        return self.pcm

    def open_tpms(self):
        self.open_tpms_count += 1
        return self.tpms

    def monotonic(self):
        return 100.0

    def sleep(self, _seconds):
        return None


class ActiveSessionTests(unittest.TestCase):
    def test_absolute_script_entrypoint_imports_from_an_unrelated_working_directory(self):
        result = subprocess.run(
            [
                sys.executable,
                active_drive.__file__,
                "--help",
            ],
            cwd="/tmp",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Guarded engine-running", result.stdout)

    def run_session(self, backend):
        sink = EventSink()
        events = backend.events
        with (
            mock.patch.object(
                diagnostic_safety,
                "acquire_channel_lock",
                side_effect=lambda _channel: events.append("lock") or object(),
            ),
            mock.patch.object(
                diagnostic_safety,
                "release_channel_lock",
                side_effect=lambda _handle: events.append("unlock"),
            ),
            mock.patch.object(
                diagnostic_safety,
                "validate_channel_lock",
                side_effect=lambda handle, _channel: handle,
            ),
            mock.patch.object(
                active_drive.can_operation_state,
                "begin_inhibit",
            ) as begin_inhibit,
        ):
            guard = SimpleNamespace(
                begin_cleanup=lambda: events.append("cleanup_guard")
            )
            outcome = active_drive.run_active_session(
                backend,
                sink,
                termination_guard=guard,
            )
        return outcome, sink.events, begin_inhibit

    def test_running_gate_polls_then_loss_stops_and_restores_after_socket_close(self):
        events = []
        pcm = FakePcmPoller(events=events)
        tpms = FakeTpmsPoller(events=events)
        backend = FakeBackend(
            snapshots=[
                snapshot((750.0, 752.0, 751.0), rpm_observation()),
                snapshot((750.0, 751.0, 752.0), rpm_observation()),
                snapshot(
                    (751.0, 752.0, 753.0),
                    rpm_observation(),
                    speed_observation(),
                    battery_observation(),
                ),
                snapshot((0.0, 0.0, 0.0), rpm_observation(0.0)),
            ],
            pcm=pcm,
            tpms=tpms,
            events=events,
        )

        outcome, emitted, _inhibit = self.run_session(backend)

        self.assertEqual(outcome.reason, "engine_not_running")
        self.assertTrue(outcome.restored)
        self.assertEqual(pcm.poll_count, 2)
        self.assertEqual(tpms.poll_count, 1)
        self.assertIsNot(pcm.permits[0], pcm.permits[1])
        self.assertIsNot(pcm.permits[1], tpms.permits[0])
        self.assertTrue(pcm.closed)
        self.assertTrue(tpms.closed)
        self.assertEqual(backend.arm_count, 1)
        self.assertEqual(backend.restore_count, 1)
        self.assertLess(events.index("cleanup_guard"), events.index("pcm_close"))
        self.assertLess(events.index("pcm_close"), events.index("restore"))
        self.assertLess(events.index("tpms_close"), events.index("restore"))
        self.assertLess(events.index("restore"), events.index("unlock"))
        observations = [
            event for event in emitted if event["type"] == "observation"
        ]
        self.assertEqual(
            {event["metric"] for event in observations},
            {
                "engine.rpm",
                "vehicle.speed",
                "battery.voltage",
                "generator.field_duty",
                "engine.crankshaft_torque",
                "tire.pressure.fl",
            },
        )
        self.assertTrue(
            all(
                event["interface_mode"] == "armed_diagnostic"
                for event in observations
            )
        )
        self.assertEqual(emitted[-1]["type"], "final")
        self.assertEqual(emitted[-1]["interface_mode"], "listen_only")
        self.assertTrue(emitted[-1]["restored"])

    def test_torque_only_failure_is_reported_once_without_ending_capture(self):
        pcm = FakePcmPoller(
            torque_result=SimpleNamespace(
                available=False,
                metric="engine.crankshaft_torque",
                unit="lb-ft",
                source="pcm.did.06da",
                bus="c-can",
                quality="observed_alfa_scale",
                reason="session_required",
                detail="PCM rejected 22 06DA with NRC 7E",
            )
        )
        tpms = FakeTpmsPoller()
        backend = FakeBackend(
            snapshots=[
                snapshot((750.0, 752.0, 751.0), rpm_observation()),
                snapshot((750.0, 751.0, 752.0), rpm_observation()),
                snapshot((751.0, 752.0, 753.0), rpm_observation()),
                snapshot((0.0, 0.0, 0.0), rpm_observation(0.0)),
            ],
            pcm=pcm,
            tpms=tpms,
        )

        outcome, emitted, _inhibit = self.run_session(backend)

        self.assertEqual(outcome.reason, "engine_not_running")
        self.assertTrue(outcome.restored)
        self.assertEqual(pcm.poll_count, 2)
        self.assertEqual(tpms.poll_count, 1)
        failures = [
            event
            for event in emitted
            if event["type"] == "metric_failure"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            failures[0]["metric"],
            "engine.crankshaft_torque",
        )
        self.assertEqual(failures[0]["reason"], "session_required")
        observations = {
            event["metric"]
            for event in emitted
            if event["type"] == "observation"
        }
        self.assertIn("generator.field_duty", observations)
        self.assertIn("tire.pressure.fl", observations)
        self.assertNotIn("engine.crankshaft_torque", observations)

    def test_ignition_only_stale_rpm_sleep_wrong_topology_and_inhibit_never_arm(self):
        running = snapshot((750.0, 751.0, 752.0), rpm_observation())
        cases = (
            (
                "ignition_on_engine_off",
                FakeBackend(
                    snapshots=[
                        snapshot((0.0, 0.0, 0.0), rpm_observation(0.0))
                    ]
                ),
                "engine_not_running",
            ),
            (
                "stale_rpm",
                FakeBackend(
                    snapshots=[snapshot((750.0, 751.0), rpm_observation())]
                ),
                "engine_not_running",
            ),
            (
                "sleeping_bus",
                FakeBackend(snapshots=[], identified_bus="silent"),
                "bus_asleep",
            ),
            (
                "b_can",
                FakeBackend(snapshots=[], topology_bus="b-can"),
                "wrong_bus",
            ),
            (
                "can_ch",
                FakeBackend(snapshots=[], topology_bus="can-ch"),
                "wrong_bus",
            ),
            (
                "wrong_pair",
                FakeBackend(snapshots=[], topology_pair="3/11"),
                "wrong_bus",
            ),
            (
                "unusable_topology",
                FakeBackend(snapshots=[], topology_usable=False),
                "wrong_bus",
            ),
            (
                "active_inhibit",
                FakeBackend(
                    snapshots=[running],
                    inhibits=({"name": "alfaobd"},),
                ),
                "inhibited",
            ),
        )
        for name, backend, expected_reason in cases:
            with self.subTest(name=name):
                outcome, emitted, _inhibit = self.run_session(backend)
                self.assertEqual(outcome.reason, expected_reason)
                self.assertEqual(backend.arm_count, 0)
                self.assertEqual(backend.open_pcm_count, 0)
                self.assertEqual(backend.open_tpms_count, 0)
                self.assertEqual(backend.pcm.poll_count, 0)
                self.assertEqual(backend.tpms.poll_count, 0)
                self.assertIsNone(outcome.restored)
                self.assertEqual(emitted[-1]["interface_mode"], "unknown")

    def test_unreadable_restart_timing_never_arms(self):
        backend = FakeBackend(
            snapshots=[
                snapshot((750.0, 751.0, 752.0), rpm_observation())
            ]
        )
        backend.interface_state = lambda: canbus.InterfaceState(
            channel=TEST_CHANNEL,
            present=True,
            up=True,
            bitrate=500000,
            listen_only=True,
            controller_state="ERROR-ACTIVE",
            restart_ms=None,
            fd_enabled=False,
        )

        outcome, emitted, _inhibit = self.run_session(backend)

        self.assertEqual(outcome.reason, "adapter_unhealthy")
        self.assertEqual(backend.arm_count, 0)
        self.assertEqual(backend.open_pcm_count, 0)
        self.assertIsNone(outcome.restored)
        self.assertEqual(emitted[-1]["interface_mode"], "unknown")

    def test_fd_enabled_or_nonzero_restart_policy_never_arms(self):
        for name, unsafe in (
            ("fd_enabled", interface(listen_only=True, fd_enabled=True)),
            ("restart_ms", interface(listen_only=True, restart_ms=100)),
        ):
            with self.subTest(name=name):
                backend = FakeBackend(
                    snapshots=[
                        snapshot((750.0, 751.0, 752.0), rpm_observation())
                    ]
                )
                backend.interface_state = lambda state=unsafe: state

                outcome, emitted, _inhibit = self.run_session(backend)

                self.assertEqual(outcome.reason, "adapter_unhealthy")
                self.assertEqual(backend.arm_count, 0)
                self.assertIsNone(outcome.restored)
                self.assertEqual(emitted[-1]["interface_mode"], "unknown")

    def test_lock_contention_cannot_reach_any_backend_gate_or_transmitter(self):
        backend = FakeBackend(snapshots=[])
        sink = EventSink()
        with (
            mock.patch.object(
                diagnostic_safety,
                "acquire_channel_lock",
                side_effect=diagnostic_safety.ChannelLockError("busy"),
            ),
            mock.patch.object(
                diagnostic_safety, "release_channel_lock"
            ) as release,
        ):
            outcome = active_drive.run_active_session(backend, sink)

        self.assertEqual(outcome.reason, "can_busy")
        self.assertEqual(backend.arm_count, 0)
        self.assertEqual(backend.open_pcm_count, 0)
        self.assertEqual(backend.open_tpms_count, 0)
        release.assert_called_once_with(None)
        self.assertEqual(sink.events[-1]["interface_mode"], "unknown")

    def test_topology_change_stops_before_first_diagnostic_request(self):
        backend = FakeBackend(
            snapshots=[
                snapshot((750.0, 751.0, 752.0), rpm_observation()),
                snapshot((750.0, 751.0, 752.0), rpm_observation()),
            ]
        )
        backend.topologies = [
            SimpleNamespace(
                bus="c-can",
                pair="6/14",
                usable=True,
                reason="test topology",
            ),
            SimpleNamespace(
                bus="b-can",
                pair="3/11",
                usable=True,
                reason="changed",
            ),
        ]

        outcome, _emitted, _inhibit = self.run_session(backend)

        self.assertEqual(outcome.reason, "wrong_bus")
        self.assertEqual(backend.pcm.poll_count, 0)
        self.assertEqual(backend.tpms.poll_count, 0)
        self.assertTrue(outcome.restored)

    def test_inhibit_is_rechecked_at_each_transmission_boundary(self):
        for inhibit_on_call, expected_pcm_polls in (
            (3, 0),
            (4, 1),
            (5, 2),
        ):
            with self.subTest(inhibit_on_call=inhibit_on_call):
                backend = FakeBackend(
                    snapshots=[
                        snapshot(
                            (750.0, 751.0, 752.0), rpm_observation()
                        ),
                        snapshot(
                            (750.0, 751.0, 752.0), rpm_observation()
                        ),
                        snapshot(
                            (750.0, 751.0, 752.0), rpm_observation()
                        ),
                    ]
                )
                inhibit_calls = 0

                def inhibits():
                    nonlocal inhibit_calls
                    inhibit_calls += 1
                    if inhibit_calls == inhibit_on_call:
                        return ({"name": "external-campaign"},)
                    return ()

                backend.inhibits = inhibits
                outcome, _emitted, _inhibit = self.run_session(backend)

                self.assertEqual(outcome.reason, "inhibited")
                self.assertEqual(
                    backend.pcm.poll_count, expected_pcm_polls
                )
                self.assertEqual(backend.tpms.poll_count, 0)
                self.assertTrue(outcome.restored)

    def test_malformed_pcm_response_and_termination_both_cleanup(self):
        cases = (
            FakePcmPoller(
                result=SimpleNamespace(
                    available=False,
                    reason="malformed_response",
                    detail="wrong echo",
                )
            ),
            FakePcmPoller(exception=KeyboardInterrupt()),
            FakePcmPoller(exception=RuntimeError("transport exploded")),
        )
        expected = (
            "malformed_response",
            "engine_not_running",
            "helper_failed",
        )
        for pcm, expected_reason in zip(cases, expected):
            with self.subTest(expected_reason=expected_reason):
                backend = FakeBackend(
                    snapshots=[
                        snapshot(
                            (750.0, 751.0, 752.0), rpm_observation()
                        ),
                        snapshot(
                            (750.0, 751.0, 752.0), rpm_observation()
                        ),
                        snapshot(
                            (750.0, 751.0, 752.0), rpm_observation()
                        ),
                    ],
                    pcm=pcm,
                )
                outcome, _emitted, _inhibit = self.run_session(backend)
                self.assertEqual(outcome.reason, expected_reason)
                self.assertTrue(outcome.restored)
                self.assertTrue(pcm.closed)
                self.assertTrue(backend.tpms.closed)
                self.assertEqual(backend.restore_count, 1)

    def test_failed_restoration_overrides_prior_result_and_latches_inhibit(self):
        backend = FakeBackend(
            snapshots=[
                snapshot((750.0, 751.0, 752.0), rpm_observation()),
                snapshot((0.0, 0.0, 0.0), rpm_observation(0.0)),
            ],
            restore_ok=False,
        )

        outcome, emitted, begin_inhibit = self.run_session(backend)

        self.assertEqual(outcome.reason, "restoration_failed")
        self.assertFalse(outcome.restored)
        self.assertEqual(emitted[-1]["state"], "restoration_failed")
        self.assertEqual(emitted[-1]["interface_mode"], "armed_diagnostic")
        begin_inhibit.assert_called_once()
        self.assertEqual(begin_inhibit.call_args.kwargs["channel"], "*")


class ParentDeathHandshakeTests(unittest.TestCase):
    def test_system_backend_matches_exact_resolved_usb_channel(self):
        resolver = mock.Mock()
        resolver.inventory.return_value = (
            (
                SimpleNamespace(
                    channel="can7",
                    driver="gs_usb",
                    usb_vid="1d50",
                    usb_pid="606f",
                    usb_serial="serial-a",
                    dev_id=1,
                ),
            ),
            (),
        )
        backend = active_drive.SystemBackend(
            "can7",
            expected_usb_serial="serial-a",
            expected_dev_id=1,
            role_resolver=resolver,
        )

        self.assertTrue(backend.identity_matches())
        backend.channel = "can8"
        self.assertFalse(backend.identity_matches())

    def test_system_backend_rejects_duplicate_identity_on_another_channel(self):
        resolver = mock.Mock()
        resolver.inventory.return_value = (
            tuple(
                SimpleNamespace(
                    channel=channel,
                    driver="gs_usb",
                    usb_vid="1d50",
                    usb_pid="606f",
                    usb_serial="serial-a",
                    dev_id=1,
                )
                for channel in ("can7", "can8")
            ),
            (),
        )
        backend = active_drive.SystemBackend(
            "can7",
            expected_usb_serial="serial-a",
            expected_dev_id=1,
            role_resolver=resolver,
        )

        self.assertFalse(backend.identity_matches())

    def test_system_backend_passes_resolved_channel_to_both_pollers(self):
        backend = active_drive.SystemBackend(
            TEST_CHANNEL,
            expected_usb_serial=TEST_SERIAL,
            expected_dev_id=TEST_DEV_ID,
            role_resolver=mock.Mock(),
        )
        pcm_poller = mock.Mock()
        tpms_poller = mock.Mock()
        with (
            mock.patch.object(
                active_drive.pcm_electrical,
                "PcmElectricalPoller",
                return_value=pcm_poller,
            ) as pcm_factory,
            mock.patch.object(
                active_drive,
                "RfHubPressurePoller",
                return_value=tpms_poller,
            ) as tpms_factory,
        ):
            self.assertIs(backend.open_pcm(), pcm_poller)
            self.assertIs(backend.open_tpms(), tpms_poller)

        pcm_factory.assert_called_once_with(
            channel="can7",
            timeout_seconds=active_drive.REQUEST_TIMEOUT_SECONDS,
        )
        tpms_factory.assert_called_once_with(
            channel="can7",
            timeout=active_drive.REQUEST_TIMEOUT_SECONDS,
        )
        pcm_poller.open.assert_called_once_with()
        tpms_poller.open.assert_called_once_with()

    def test_cli_requires_both_usb_identity_arguments(self):
        with self.assertRaises(SystemExit):
            active_drive.build_parser().parse_args(
                [
                    "--channel",
                    TEST_CHANNEL,
                    "--expected-usb-serial",
                    TEST_SERIAL,
                    "--expected-parent-pid",
                    "4242",
                ]
            )

    def test_parent_pid_is_checked_before_and_after_prctl(self):
        expected = 4242
        parents = iter((expected, expected))
        libc = SimpleNamespace(prctl=mock.Mock(return_value=0))

        active_drive._set_parent_death_signal(
            expected,
            parent_pid_reader=lambda: next(parents),
            libc_loader=lambda *_args, **_kwargs: libc,
        )

        libc.prctl.assert_called_once_with(
            1, active_drive.signal.SIGTERM, 0, 0, 0
        )

    def test_parent_already_gone_refuses_before_prctl(self):
        load_libc = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "before prctl"):
            active_drive._set_parent_death_signal(
                4242,
                parent_pid_reader=lambda: 1,
                libc_loader=load_libc,
            )

        load_libc.assert_not_called()

    def test_parent_death_during_prctl_race_fails_closed(self):
        parents = iter((4242, 1))
        libc = SimpleNamespace(prctl=mock.Mock(return_value=0))

        with self.assertRaisesRegex(RuntimeError, "disappeared"):
            active_drive._set_parent_death_signal(
                4242,
                parent_pid_reader=lambda: next(parents),
                libc_loader=lambda *_args, **_kwargs: libc,
            )

        libc.prctl.assert_called_once()

    def test_unavailable_parent_death_signal_fails_closed(self):
        libc = SimpleNamespace()

        with self.assertRaisesRegex(RuntimeError, "could not install"):
            active_drive._set_parent_death_signal(
                4242,
                parent_pid_reader=lambda: 4242,
                libc_loader=lambda *_args, **_kwargs: libc,
            )

    def test_cli_is_closed_and_parent_failure_precedes_backend_construction(self):
        actions = {
            action.dest: action
            for action in active_drive.build_parser()._actions
        }
        self.assertEqual(
            set(actions),
            {
                "help",
                "channel",
                "expected_usb_serial",
                "expected_dev_id",
                "expected_parent_pid",
            },
        )
        self.assertIsNone(actions["channel"].choices)
        self.assertTrue(actions["channel"].required)
        self.assertTrue(actions["expected_usb_serial"].required)
        self.assertTrue(actions["expected_dev_id"].required)
        self.assertTrue(actions["expected_parent_pid"].required)

        with (
            mock.patch.object(
                active_drive,
                "_set_parent_death_signal",
                side_effect=RuntimeError("parent mismatch"),
            ),
            mock.patch.object(active_drive, "SystemBackend") as backend,
            mock.patch.object(active_drive, "run_active_session") as run_session,
        ):
            with self.assertRaisesRegex(SystemExit, "refused parent handshake"):
                active_drive.main(
                    [
                        "--channel",
                        TEST_CHANNEL,
                        "--expected-usb-serial",
                        TEST_SERIAL,
                        "--expected-dev-id",
                        hex(TEST_DEV_ID),
                        "--expected-parent-pid",
                        "4242",
                    ]
                )

        backend.assert_not_called()
        run_session.assert_not_called()


class PressureWireTests(unittest.TestCase):
    class FakeSocket:
        def __init__(self, response):
            self.response = response
            self.timeout = None
            self.sent = []
            self.bound = None
            self.closed = False

        def setsockopt(self, *_args):
            return None

        def bind(self, address):
            self.bound = address

        def settimeout(self, timeout):
            self.timeout = timeout

        def recv(self, _size):
            if self.timeout == 0.0:
                raise BlockingIOError
            return self.response

        def send(self, data):
            self.sent.append(bytes(data))
            return len(data)

        def close(self):
            self.closed = True

    @staticmethod
    def response_frame(
        data,
        *,
        can_id=None,
        dlc=8,
        extended=True,
        flags=0,
    ):
        module = MODULES["rf_hub"]
        response_id = module.rxid if can_id is None else can_id
        raw_id = response_id | flags
        if extended:
            raw_id |= active_drive.CAN_EFF_FLAG
        return struct.pack(
            active_drive.CAN_FRAME_FORMAT,
            raw_id,
            dlc,
            bytes(data).ljust(8, b"\0"),
        )

    def poll_response(self, response, *, permit=None):
        fake = self.FakeSocket(response)
        poller = active_drive.RfHubPressurePoller(
            channel=TEST_CHANNEL,
            timeout=0.5,
            socket_factory=lambda *_args: fake,
            monotonic=lambda: 10.0,
        )
        poller.open()
        try:
            authorization = permit or transmit_authorization(
                active_drive.transmit_permit.RF_HUB_PRESSURE
            )
            return poller.poll_next(authorization), fake
        finally:
            poller.close()

    def test_constructor_accepts_dynamic_can_channel_and_rejects_invalid_input(self):
        socket_factory = mock.Mock()
        invalid_arguments = (
            {"channel": "c-can", "timeout": 0.5},
            {"channel": "vcan0", "timeout": 0.5},
            {"channel": TEST_CHANNEL, "timeout": 0},
            {"channel": TEST_CHANNEL, "timeout": -0.1},
            {"channel": TEST_CHANNEL, "timeout": float("nan")},
            {"channel": TEST_CHANNEL, "timeout": float("inf")},
            {"channel": TEST_CHANNEL, "timeout": True},
            {"channel": TEST_CHANNEL, "timeout": "0.5"},
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    active_drive.RfHubPressurePoller(
                        **arguments,
                        socket_factory=socket_factory,
                    )

        socket_factory.assert_not_called()
        poller = active_drive.RfHubPressurePoller(
            channel="can7",
            timeout=0.5,
            socket_factory=socket_factory,
        )
        self.assertEqual(poller.channel, "can7")
        socket_factory.assert_not_called()

        dynamic_socket = self.FakeSocket(b"")
        dynamic = active_drive.RfHubPressurePoller(
            channel="can7",
            timeout=0.5,
            socket_factory=lambda *_args: dynamic_socket,
        )
        dynamic.open()
        dynamic.close()
        self.assertEqual(dynamic_socket.bound, ("can7",))

    def test_pressure_send_requires_correct_fresh_one_use_permit(self):
        response = self.response_frame(
            bytes.fromhex("05 62 31 D0 09 60 00 00")
        )

        missing_socket = self.FakeSocket(response)
        missing = active_drive.RfHubPressurePoller(
            channel=TEST_CHANNEL,
            timeout=0.5,
            socket_factory=lambda *_args: missing_socket,
            monotonic=lambda: 10.0,
        )
        missing.open()
        try:
            with self.assertRaises(TypeError):
                missing.poll_next()
        finally:
            missing.close()
        self.assertEqual(missing_socket.sent, [])

        wrong = transmit_authorization(
            active_drive.transmit_permit.PCM_GENERATOR_DUTY
        )
        wrong_result, wrong_socket = self.poll_response(
            response,
            permit=wrong,
        )
        self.assertEqual(wrong_result.reason, "response_rejected")
        self.assertEqual(wrong_socket.sent, [])

        clock = SimpleNamespace(value=10.0)
        stale = transmit_authorization(
            active_drive.transmit_permit.RF_HUB_PRESSURE,
            clock=lambda: clock.value,
        )
        clock.value += active_drive.transmit_permit.PERMIT_TTL_SECONDS
        stale_result, stale_socket = self.poll_response(
            response,
            permit=stale,
        )
        self.assertEqual(stale_result.reason, "response_rejected")
        self.assertEqual(stale_socket.sent, [])

        with self.assertRaises(
            active_drive.transmit_permit.TransmitPermitError
        ):
            transmit_authorization(
                active_drive.transmit_permit.RF_HUB_PRESSURE,
                rpm_samples=(750.0, 0.0, 752.0),
            )

    def test_pressure_permit_is_consumed_by_first_send_attempt(self):
        response = self.response_frame(
            bytes.fromhex("05 62 31 D0 09 60 00 00")
        )
        fake = self.FakeSocket(response)
        poller = active_drive.RfHubPressurePoller(
            channel=TEST_CHANNEL,
            timeout=0.5,
            socket_factory=lambda *_args: fake,
            monotonic=lambda: 10.0,
        )
        permit = transmit_authorization(
            active_drive.transmit_permit.RF_HUB_PRESSURE
        )
        poller.open()
        try:
            first = poller.poll_next(permit)
            second = poller.poll_next(permit)
        finally:
            poller.close()

        self.assertTrue(first.available)
        self.assertEqual(second.reason, "response_rejected")
        self.assertEqual(len(fake.sent), 1)

    def test_complete_transmit_allowlist_is_exactly_six_fixed_frames(self):
        pcm = pcm_electrical.GENERATOR_FIELD_DUTY_PROFILE
        torque = pcm_electrical.CRANKSHAFT_TORQUE_PROFILE
        allowed = {
            (
                pcm.request_id,
                pcm.request_data,
            ),
            (
                torque.request_id,
                torque.request_data,
            ),
            *{
                (
                    MODULES["rf_hub"].txid,
                    bytes(
                        (0x03, 0x22, did >> 8, did & 0xFF, 0, 0, 0, 0)
                    ),
                )
                for did, _metric in active_drive.TPMS_PROFILES
            },
        }
        self.assertEqual(
            allowed,
            {
                (0x18DA10F1, bytes.fromhex("03 22 01 A1 00 00 00 00")),
                (0x18DA10F1, bytes.fromhex("03 22 06 DA 00 00 00 00")),
                (0x18DAC7F1, bytes.fromhex("03 22 31 D0 00 00 00 00")),
                (0x18DAC7F1, bytes.fromhex("03 22 31 D1 00 00 00 00")),
                (0x18DAC7F1, bytes.fromhex("03 22 31 D2 00 00 00 00")),
                (0x18DAC7F1, bytes.fromhex("03 22 31 D3 00 00 00 00")),
            },
        )

    def test_round_robin_pressure_path_can_send_only_four_fixed_rdbi_frames(self):
        module = MODULES["rf_hub"]
        response = self.response_frame(
            bytes.fromhex("05 62 31 D0 09 60 00 00")
        )
        result, fake = self.poll_response(response)

        expected = struct.pack(
            active_drive.CAN_FRAME_FORMAT,
            active_drive.CAN_EFF_FLAG | module.txid,
            8,
            bytes.fromhex("03 22 31 D0 00 00 00 00"),
        )
        self.assertEqual(fake.sent, [expected])
        self.assertTrue(result.available)
        self.assertEqual(result.metric, "tire.pressure.fl")
        self.assertAlmostEqual(result.value, 34.8)
        self.assertTrue(fake.closed)
        self.assertEqual(
            {
                bytes((0x03, 0x22, did >> 8, did & 0xFF, 0, 0, 0, 0))
                for did, _metric in active_drive.TPMS_PROFILES
            },
            {
                bytes.fromhex("03 22 31 D0 00 00 00 00"),
                bytes.fromhex("03 22 31 D1 00 00 00 00"),
                bytes.fromhex("03 22 31 D2 00 00 00 00"),
                bytes.fromhex("03 22 31 D3 00 00 00 00"),
            },
        )

    def test_pressure_response_rejects_malformed_raw_frame_dlc_and_payload(self):
        cases = {
            "short_raw_frame": b"\0" * (active_drive.CAN_FRAME_SIZE - 1),
            "classic_can_dlc_overflow": self.response_frame(
                bytes.fromhex("05 62 31 D0 09 60 00 00"),
                dlc=9,
            ),
            "truncated_declared_payload": self.response_frame(
                bytes.fromhex("05 62 31 D0 09 00 00 00"),
                dlc=5,
            ),
            "oversized_declared_payload": self.response_frame(
                bytes.fromhex("06 62 31 D0 09 60 FF 00"),
            ),
            "wrong_service": self.response_frame(
                bytes.fromhex("05 63 31 D0 09 60 00 00"),
            ),
            "wrong_did_echo": self.response_frame(
                bytes.fromhex("05 62 31 D1 09 60 00 00"),
            ),
            "multiframe": self.response_frame(
                bytes.fromhex("10 05 62 31 D0 09 60 00"),
            ),
            "wrong_physical_id": self.response_frame(
                bytes.fromhex("05 62 31 D0 09 60 00 00"),
                can_id=MODULES["rf_hub"].rxid + 1,
            ),
            "standard_identifier": self.response_frame(
                bytes.fromhex("05 62 31 D0 09 60 00 00"),
                can_id=0x123,
                extended=False,
            ),
            "error_frame": self.response_frame(
                bytes.fromhex("05 62 31 D0 09 60 00 00"),
                flags=active_drive.CAN_ERR_FLAG,
            ),
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                result, fake = self.poll_response(response)
                self.assertFalse(result.available)
                self.assertEqual(result.reason, "malformed_response")
                self.assertEqual(len(fake.sent), 1)
                self.assertTrue(fake.closed)

    def test_pressure_response_rejects_physically_implausible_value(self):
        result, fake = self.poll_response(
            self.response_frame(
                bytes.fromhex("05 62 31 D0 40 00 00 00")
            )
        )

        self.assertFalse(result.available)
        self.assertEqual(result.reason, "response_rejected")
        self.assertIn("implausible", result.detail)
        self.assertEqual(len(fake.sent), 1)
        self.assertTrue(fake.closed)


class BrokerActiveDriveTests(unittest.TestCase):
    class Clock:
        def __init__(self):
            self.value = 100.0

        def __call__(self):
            return self.value

    class Acquirer:
        channel = TEST_CHANNEL

        def status_snapshot(self):
            return {
                "channel": TEST_CHANNEL,
                "adapter_present": True,
                "up": True,
                "bitrate": 500000,
                "fd_enabled": False,
                "listen_only": True,
                "controller_state": "ERROR-ACTIVE",
                "topology": {
                    "bus": "c-can",
                    "pair": "6/14",
                    "usable": True,
                    "reason": "",
                },
                "active_inhibits": [],
            }

    def make_broker(self):
        clock = self.Clock()
        broker = TelemetryBroker(
            acquirer=self.Acquirer(),
            monotonic=clock,
        )
        return broker, clock

    def test_active_pipe_validates_and_caches_pcm_and_passive_metrics(self):
        broker, clock = self.make_broker()
        events = (
            {
                "type": "observation",
                "metric": "engine.rpm",
                "value": 751.0,
                "unit": "rpm",
                "source": "ccan.broadcast.0x0fc",
                "bus": "c-can",
                "quality": "observed_alfa_scale",
                "interface_mode": "armed_diagnostic",
            },
            {
                "type": "observation",
                "metric": "vehicle.speed",
                "value": 31.2,
                "unit": "mph",
                "source": "ccan.broadcast.0x101",
                "bus": "c-can",
                "quality": "observed_alfa_scale",
                "interface_mode": "armed_diagnostic",
            },
            {
                "type": "observation",
                "metric": "generator.field_duty",
                "value": 100.008,
                "unit": "%",
                "source": "pcm.did.01a1",
                "bus": "c-can",
                "quality": "observed_alfa_scale",
                "interface_mode": "armed_diagnostic",
            },
            {
                "type": "observation",
                "metric": "engine.crankshaft_torque",
                "value": 177.6,
                "unit": "lb-ft",
                "source": "pcm.did.06da",
                "bus": "c-can",
                "quality": "observed_alfa_scale",
                "interface_mode": "armed_diagnostic",
            },
        )
        for event in events:
            broker.handle_active_drive_event(event)

        generator = broker.metric_response("generator.field_duty")
        torque = broker.metric_response("engine.crankshaft_torque")
        speed = broker.metric_response("vehicle.speed")
        self.assertTrue(generator["available"])
        self.assertEqual(generator["value"], 100.008)
        self.assertEqual(
            generator["acquisition"],
            "physical_read_data_by_identifier",
        )
        self.assertEqual(generator["interface_mode"], "armed_diagnostic")
        self.assertTrue(torque["available"])
        self.assertEqual(torque["value"], 177.6)
        self.assertEqual(torque["interface_mode"], "armed_diagnostic")
        power = broker.metric_response("engine.crankshaft_power")
        self.assertTrue(power["available"])
        self.assertAlmostEqual(power["value"], 177.6 * 751.0 / 5252.113122)
        self.assertEqual(power["unit"], "hp")
        self.assertEqual(power["acquisition"], "derived_time_aligned")
        self.assertEqual(power["interface_mode"], "armed_diagnostic")
        self.assertTrue(speed["available"])
        self.assertEqual(speed["interface_mode"], "armed_diagnostic")
        self.assertEqual(
            broker.status_response()["vehicle_state"]["state"], "running"
        )

        clock.value = 105.0
        self.assertTrue(
            broker.metric_response("generator.field_duty")["stale"]
        )
        self.assertTrue(
            broker.metric_response("engine.crankshaft_torque")["stale"]
        )
        self.assertTrue(
            broker.metric_response("engine.crankshaft_power")["stale"]
        )

        broker.handle_active_drive_event(
            {
                "type": "final",
                "state": "idle",
                "reason": "engine_not_running",
                "detail": "RPM gate lost",
                "interface_mode": "listen_only",
                "restored": True,
            }
        )
        self.assertFalse(
            broker.metric_response("generator.field_duty")["available"]
        )
        self.assertEqual(
            broker.metric_response("generator.field_duty")["reason"],
            "engine_not_running",
        )
        self.assertEqual(
            broker.metric_response("engine.crankshaft_torque")["reason"],
            "engine_not_running",
        )
        self.assertEqual(
            broker.metric_response("engine.crankshaft_power")["reason"],
            "engine_not_running",
        )
        self.assertTrue(broker.metric_response("vehicle.speed")["available"])

    def test_optional_torque_failure_does_not_change_active_owner_state(self):
        broker, _clock = self.make_broker()
        broker.handle_active_drive_event(
            {
                "type": "status",
                "state": "armed_diagnostic",
                "reason": "running_gate_satisfied",
                "detail": "coordinated owner is armed",
                "interface_mode": "armed_diagnostic",
                "pid": 123,
            }
        )
        broker.handle_active_drive_event(
            {
                "type": "metric_failure",
                "metric": "engine.crankshaft_torque",
                "unit": "lb-ft",
                "source": "pcm.did.06da",
                "bus": "c-can",
                "quality": "observed_alfa_scale",
                "reason": "session_required",
                "detail": "PCM rejected 22 06DA with NRC 7E",
                "interface_mode": "armed_diagnostic",
            }
        )

        torque = broker.metric_response("engine.crankshaft_torque")
        self.assertFalse(torque["available"])
        self.assertEqual(torque["reason"], "session_required")
        self.assertEqual(
            torque["interface_mode"],
            "armed_diagnostic",
        )
        power = broker.metric_response("engine.crankshaft_power")
        self.assertFalse(power["available"])
        self.assertEqual(power["reason"], "session_required")
        active = broker.status_response()["active_drive"]
        self.assertEqual(active["state"], "armed_diagnostic")
        self.assertEqual(active["reason"], "running_gate_satisfied")

    def test_derived_power_requires_time_aligned_exact_inputs_and_keeps_overrun(self):
        broker, clock = self.make_broker()

        def observation(metric, value, unit, source):
            broker.handle_active_drive_event(
                {
                    "type": "observation",
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "source": source,
                    "bus": "c-can",
                    "quality": "observed_alfa_scale",
                    "interface_mode": "armed_diagnostic",
                }
            )

        observation("engine.rpm", 2000.0, "rpm", "ccan.broadcast.0x0fc")
        observation(
            "engine.crankshaft_torque",
            100.0,
            "lb-ft",
            "pcm.did.06da",
        )
        self.assertAlmostEqual(
            broker.metric_response("engine.crankshaft_power")["value"],
            100.0 * 2000.0 / 5252.113122,
        )

        prior_power = broker.metric_response("engine.crankshaft_power")["value"]
        clock.value += 1.0
        observation("engine.rpm", 4000.0, "rpm", "ccan.broadcast.0x0fc")
        # A new-cycle RPM must not be multiplied by the previous cycle's
        # torque while the new torque request is still in flight.
        self.assertEqual(
            broker.metric_response("engine.crankshaft_power")["value"],
            prior_power,
        )

        clock.value += 0.1
        observation(
            "engine.crankshaft_torque",
            -25.0,
            "lb-ft",
            "pcm.did.06da",
        )
        overrun = broker.metric_response("engine.crankshaft_power")
        self.assertTrue(overrun["available"])
        self.assertLess(overrun["value"], 0.0)
        self.assertAlmostEqual(
            overrun["value"],
            -25.0 * 4000.0 / 5252.113122,
        )

    def test_derived_power_rejects_over_limit_input_skew_on_first_pair(self):
        broker, clock = self.make_broker()

        def observation(metric, value, unit, source):
            broker.handle_active_drive_event(
                {
                    "type": "observation",
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "source": source,
                    "bus": "c-can",
                    "quality": "observed_alfa_scale",
                    "interface_mode": "armed_diagnostic",
                }
            )

        observation("engine.rpm", 2000.0, "rpm", "ccan.broadcast.0x0fc")
        clock.value += 1.501
        observation(
            "engine.crankshaft_torque",
            100.0,
            "lb-ft",
            "pcm.did.06da",
        )
        power = broker.metric_response("engine.crankshaft_power")
        self.assertFalse(power["available"])
        self.assertEqual(power["reason"], "source_unavailable")
        self.assertIn("skew", power["detail"])

    def test_derived_power_uses_oldest_input_for_freshness_and_wall_time(self):
        broker, clock = self.make_broker()
        rpm_wall = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)
        torque_wall = rpm_wall - timedelta(hours=1)
        with broker._lock:
            broker._cache["engine.rpm"] = success(
                metric="engine.rpm",
                unit="rpm",
                value=2000.0,
                source="ccan.broadcast.0x0fc",
                bus="c-can",
                acquisition="passive_broadcast",
                quality="observed_alfa_scale",
                observed_monotonic=clock.value,
                observed_at=rpm_wall,
                interface_mode="armed_diagnostic",
            )
            broker._cache["engine.crankshaft_torque"] = success(
                metric="engine.crankshaft_torque",
                unit="lb-ft",
                value=100.0,
                source="pcm.did.06da",
                bus="c-can",
                acquisition="physical_read_data_by_identifier",
                quality="observed_alfa_scale",
                observed_monotonic=clock.value + 1.5,
                observed_at=torque_wall,
                interface_mode="armed_diagnostic",
            )
        clock.value += 1.5
        broker._refresh_derived_power()
        power = broker.metric_response("engine.crankshaft_power")
        self.assertTrue(power["available"])
        self.assertEqual(power["observed_at"], rpm_wall.isoformat())
        self.assertEqual(power["age_ms"], 1500)

        clock.value += 2.501
        self.assertTrue(broker.metric_response("engine.crankshaft_power")["stale"])

    def test_armed_status_is_honest_and_blocks_other_active_acquisition(self):
        broker, _clock = self.make_broker()
        interface_status = self.Acquirer().status_snapshot()
        interface_status["role_interfaces"] = {
            "ready": True,
            "issues": [],
            "roles": {
                "c-can": {
                    "resolution": "resolved",
                    "channel": TEST_CHANNEL,
                    "expected": {
                        "usb_serial": "serial-a",
                        "dev_id": 0,
                        "passive_required": True,
                    },
                    "actual": {
                        "up": True,
                        "bitrate": 500000,
                        "listen_only": True,
                        "controller_state": "ERROR-ACTIVE",
                        "restart_ms": 0,
                    },
                    "passive_ready": True,
                    "reason": "ready",
                }
            },
        }
        broker._interface_status = interface_status
        broker.handle_active_drive_event(
            {
                "type": "status",
                "state": "armed_diagnostic",
                "reason": "running_gate_satisfied",
                "detail": "coordinated owner is armed",
                "interface_mode": "armed_diagnostic",
                "pid": 123,
            }
        )

        status = broker.status_response()
        self.assertFalse(status["interface"]["listen_only"])
        self.assertEqual(
            status["interface"]["mode"], "armed_diagnostic"
        )
        self.assertFalse(status["active_acquisition_permitted"])
        self.assertEqual(
            status["current_owner"]["kind"], "broker_active_drive"
        )
        role_snapshot = status["interface"]["role_interfaces"]
        ccan = role_snapshot["roles"]["c-can"]
        self.assertFalse(role_snapshot["ready"])
        self.assertFalse(ccan["actual"]["listen_only"])
        self.assertEqual(ccan["actual"]["mode"], "armed_diagnostic")
        self.assertFalse(ccan["passive_ready"])
        self.assertTrue(ccan["topology_usable"])
        self.assertEqual(ccan["operating_mode"], "armed_diagnostic")

    def test_active_pipe_rejects_unregistered_source_and_public_spoofing(self):
        broker, _clock = self.make_broker()
        with self.assertRaisesRegex(ValueError, "outside"):
            broker.handle_active_drive_event(
                {
                    "type": "observation",
                    "metric": "generator.field_duty",
                    "value": 50.0,
                    "unit": "%",
                    "source": "pcm.did.ffff",
                    "bus": "c-can",
                    "quality": "observed_alfa_scale",
                    "interface_mode": "armed_diagnostic",
                }
            )
        published = broker.publish_observation(
            "generator.field_duty",
            value=50.0,
            unit="%",
            source="pcm.did.01a1",
            bus="c-can",
            quality="observed_alfa_scale",
        )
        self.assertEqual(published.reason, "source_not_publishable")

    def test_inconsistent_final_event_is_rejected(self):
        broker, _clock = self.make_broker()
        with self.assertRaisesRegex(ValueError, "restored"):
            broker.handle_active_drive_event(
                {
                    "type": "final",
                    "state": "idle",
                    "reason": "engine_not_running",
                    "detail": "malformed helper event",
                    "interface_mode": "listen_only",
                    "restored": "yes",
                }
            )
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            broker.handle_active_drive_event(
                {
                    "type": "final",
                    "state": "idle",
                    "reason": "restoration_failed",
                    "detail": "malformed helper event",
                    "interface_mode": "listen_only",
                    "restored": False,
                }
            )

    def test_rejected_returned_final_fails_closed_as_unverified_restoration(self):
        class Supervisor:
            def run(self, _stop):
                return {
                    "type": "final",
                    "state": "idle",
                    "reason": "engine_not_running",
                    "detail": "malformed helper final",
                    "interface_mode": "listen_only",
                    "restored": "yes",
                }

        broker = TelemetryBroker(
            acquirer=self.Acquirer(),
            monotonic=self.Clock(),
            active_drive_supervisor=Supervisor(),
            active_drive_enabled=True,
        )
        broker._passive_engine_evidence = "running"
        with mock.patch(
            "projects.vehicle_data.broker.can_operation_state.begin_inhibit"
        ) as begin_inhibit:
            broker._run_active_drive_if_ready()

        active = broker.status_response()["active_drive"]
        self.assertEqual(active["state"], "restoration_failed")
        self.assertEqual(active["reason"], "restoration_failed")
        self.assertEqual(active["interface_mode"], "armed_diagnostic")
        self.assertTrue(active["restoration_failed"])
        self.assertTrue(broker._active_drive_restoration_latched)
        begin_inhibit.assert_called_once()

    def test_restoration_failure_latches_further_active_collection(self):
        class Supervisor:
            def __init__(self):
                self.calls = 0

            def run(self, _stop):
                self.calls += 1
                return {
                    "type": "final",
                    "state": "restoration_failed",
                    "reason": "restoration_failed",
                    "detail": "listen-only readback failed",
                    "interface_mode": "armed_diagnostic",
                    "restored": False,
                }

        supervisor = Supervisor()
        broker = TelemetryBroker(
            acquirer=self.Acquirer(),
            monotonic=self.Clock(),
            active_drive_supervisor=supervisor,
            active_drive_enabled=True,
        )
        broker._passive_engine_evidence = "running"
        with mock.patch(
            "projects.vehicle_data.broker.can_operation_state.begin_inhibit"
        ) as begin_inhibit:
            broker._run_active_drive_if_ready()
            broker._passive_engine_evidence = "running"
            broker._run_active_drive_if_ready()

        self.assertEqual(supervisor.calls, 1)
        begin_inhibit.assert_called_once()
        self.assertEqual(begin_inhibit.call_args.kwargs["channel"], "*")
        self.assertTrue(
            broker.status_response()["active_drive"]["restoration_failed"]
        )
        self.assertEqual(
            broker.metric_response("generator.field_duty")["reason"],
            "restoration_failed",
        )

    def test_running_epoch_failure_is_not_cleared_by_one_missing_snapshot(self):
        class Supervisor:
            def __init__(self):
                self.calls = 0

            def run(self, _stop):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "type": "final",
                        "state": "idle",
                        "reason": "response_timeout",
                        "detail": "PCM response timed out",
                        "interface_mode": "listen_only",
                        "restored": True,
                    }
                return {
                    "type": "final",
                    "state": "idle",
                    "reason": "engine_not_running",
                    "detail": "preflight RPM gate did not pass",
                    "interface_mode": "unknown",
                    "restored": None,
                }

        supervisor = Supervisor()
        broker = TelemetryBroker(
            acquirer=self.Acquirer(),
            monotonic=self.Clock(),
            active_drive_supervisor=supervisor,
            active_drive_enabled=True,
        )
        broker._passive_engine_evidence = "running"
        broker._run_active_drive_if_ready()
        self.assertEqual(supervisor.calls, 1)

        broker._passive_engine_evidence = "unknown"
        broker._passive_unknown_evidence_streak = 1
        broker._run_active_drive_if_ready()
        broker._passive_engine_evidence = "running"
        broker._run_active_drive_if_ready()
        self.assertEqual(supervisor.calls, 1)

        broker._passive_engine_evidence = "stopped"
        broker._passive_stop_evidence_streak = 1
        broker._run_active_drive_if_ready()
        broker._passive_engine_evidence = "running"
        broker._run_active_drive_if_ready()
        self.assertEqual(supervisor.calls, 1)

        broker._passive_engine_evidence = "stopped"
        broker._passive_stop_evidence_streak = 2
        broker._run_active_drive_if_ready()
        broker._passive_engine_evidence = "running"
        broker._run_active_drive_if_ready()
        self.assertEqual(supervisor.calls, 2)

    def test_supervisor_missing_final_is_unverified_restoration_failure(self):
        class Process:
            def __init__(self):
                self.stdout = iter(())
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                del timeout
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        commands = []
        supervisor = ActiveDriveSupervisor(
            channel=TEST_CHANNEL,
            expected_usb_serial=TEST_SERIAL,
            expected_dev_id=TEST_DEV_ID,
            event_handler=lambda _event: None,
            popen_factory=lambda command, **_kwargs: (
                commands.append(command) or Process()
            ),
        )
        with mock.patch(
            "projects.vehicle_data.broker.os.getpid",
            return_value=4242,
        ):
            result = supervisor.run(threading.Event())

        self.assertEqual(result["reason"], "restoration_failed")
        self.assertFalse(result["restored"])
        self.assertEqual(result["interface_mode"], "armed_diagnostic")
        self.assertEqual(
            commands[0][2:],
            [
                "--channel",
                TEST_CHANNEL,
                "--expected-parent-pid",
                "4242",
                "--expected-usb-serial",
                TEST_SERIAL,
                "--expected-dev-id",
                hex(TEST_DEV_ID),
            ],
        )

    def test_supervisor_passes_resolved_usb_identity_to_helper(self):
        class Process:
            stdout = iter(())

            def poll(self):
                return 0

            def wait(self, timeout=None):
                del timeout
                return 0

            def terminate(self):
                return None

            def kill(self):
                return None

        commands = []
        supervisor = ActiveDriveSupervisor(
            channel=TEST_CHANNEL,
            expected_usb_serial=TEST_SERIAL,
            expected_dev_id=TEST_DEV_ID,
            event_handler=lambda _event: None,
            popen_factory=lambda command, **_kwargs: (
                commands.append(command) or Process()
            ),
        )

        with mock.patch("projects.vehicle_data.broker.os.getpid", return_value=4242):
            supervisor.run(threading.Event())

        self.assertEqual(
            commands[0][-4:],
            [
                "--expected-usb-serial",
                TEST_SERIAL,
                "--expected-dev-id",
                hex(TEST_DEV_ID),
            ],
        )

    def test_supervisor_streams_nonfinal_events_but_returns_final_exactly_once(self):
        status = {
            "type": "status",
            "state": "armed_diagnostic",
            "reason": "running_gate_satisfied",
            "detail": "armed",
            "interface_mode": "armed_diagnostic",
        }
        final = {
            "type": "final",
            "state": "idle",
            "reason": "engine_not_running",
            "detail": "RPM gate lost",
            "interface_mode": "listen_only",
            "restored": True,
        }

        class Process:
            def __init__(self):
                self.stdout = iter(
                    (json.dumps(status) + "\n", json.dumps(final) + "\n")
                )
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                del timeout
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        streamed = []
        supervisor = ActiveDriveSupervisor(
            channel=TEST_CHANNEL,
            expected_usb_serial=TEST_SERIAL,
            expected_dev_id=TEST_DEV_ID,
            event_handler=streamed.append,
            popen_factory=lambda *_args, **_kwargs: Process(),
        )

        result = supervisor.run(threading.Event())

        self.assertEqual(streamed, [status])
        self.assertEqual(result, final)

    def test_supervisor_honors_stop_set_during_process_start_race(self):
        class Process:
            def __init__(self):
                self.stdout = iter(())
                self.returncode = None
                self.terminate_calls = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                del timeout
                return self.returncode

            def terminate(self):
                self.terminate_calls += 1
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        process = Process()
        stopped = threading.Event()
        stopped.set()
        supervisor = ActiveDriveSupervisor(
            channel=TEST_CHANNEL,
            expected_usb_serial=TEST_SERIAL,
            expected_dev_id=TEST_DEV_ID,
            event_handler=lambda _event: None,
            popen_factory=lambda *_args, **_kwargs: process,
        )
        result = supervisor.run(stopped)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(result["reason"], "restoration_failed")

    def test_supervisor_retains_and_terminates_child_after_stream_exception(self):
        class BrokenStream:
            def __iter__(self):
                return self

            def __next__(self):
                raise RuntimeError("pipe read failed")

        class Process:
            def __init__(self):
                self.stdout = BrokenStream()
                self.returncode = None
                self.terminate_calls = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                del timeout
                return self.returncode

            def terminate(self):
                self.terminate_calls += 1
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        process = Process()
        supervisor = ActiveDriveSupervisor(
            channel=TEST_CHANNEL,
            expected_usb_serial=TEST_SERIAL,
            expected_dev_id=TEST_DEV_ID,
            event_handler=lambda _event: None,
            popen_factory=lambda *_args, **_kwargs: process,
        )
        result = supervisor.run(threading.Event())

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(result["reason"], "restoration_failed")
        self.assertIn("pipe read failed", result["detail"])
        self.assertIsNone(supervisor._process)

    def test_supervisor_preserves_unverified_live_child_handle_for_stop_retry(self):
        class Process:
            def __init__(self):
                self.stdout = iter(())
                self.terminate_calls = 0
                self.kill_calls = 0

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("active-drive", timeout)

            def terminate(self):
                self.terminate_calls += 1
                raise OSError("terminate failed")

            def kill(self):
                self.kill_calls += 1
                raise OSError("kill failed")

        process = Process()
        supervisor = ActiveDriveSupervisor(
            channel=TEST_CHANNEL,
            expected_usb_serial=TEST_SERIAL,
            expected_dev_id=TEST_DEV_ID,
            event_handler=lambda _event: None,
            popen_factory=lambda *_args, **_kwargs: process,
            shutdown_timeout_seconds=0.001,
            queue_poll_seconds=0.001,
        )

        result = supervisor.run(threading.Event())
        calls_before_retry = process.terminate_calls

        self.assertEqual(result["reason"], "restoration_failed")
        self.assertIs(supervisor._process, process)
        supervisor.stop()
        self.assertGreater(process.terminate_calls, calls_before_retry)

    def test_supervisor_bounds_silent_child_that_ignores_terminate(self):
        release_reader = threading.Event()

        class BlockingStream:
            def __iter__(self):
                return self

            def __next__(self):
                release_reader.wait(1.0)
                raise StopIteration

            def close(self):
                release_reader.set()

        class Process:
            def __init__(self):
                self.stdout = BlockingStream()
                self.returncode = None
                self.terminate_calls = 0
                self.kill_calls = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("active-drive", timeout)
                return self.returncode

            def terminate(self):
                self.terminate_calls += 1

            def kill(self):
                self.kill_calls += 1
                self.returncode = -9
                release_reader.set()

        process = Process()
        supervisor = ActiveDriveSupervisor(
            channel=TEST_CHANNEL,
            expected_usb_serial=TEST_SERIAL,
            expected_dev_id=TEST_DEV_ID,
            event_handler=lambda _event: None,
            popen_factory=lambda *_args, **_kwargs: process,
            event_silence_timeout_seconds=0.01,
            shutdown_timeout_seconds=0.01,
            queue_poll_seconds=0.001,
        )
        result = supervisor.run(threading.Event())

        self.assertGreaterEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(result["reason"], "restoration_failed")
        self.assertIsNone(supervisor._process)

    def test_collector_reports_unexpected_failure_instead_of_silent_death(self):
        class Supervisor:
            def __init__(self):
                self.stop_calls = 0

            def stop(self):
                self.stop_calls += 1

        supervisor = Supervisor()
        broker = TelemetryBroker(
            acquirer=self.Acquirer(),
            monotonic=self.Clock(),
            active_drive_supervisor=supervisor,
            active_drive_enabled=True,
        )
        with mock.patch.object(
            broker,
            "acquire",
            side_effect=RuntimeError("collector exploded"),
        ):
            broker._collector_loop()

        status = broker.status_response()
        self.assertEqual(status["collector"]["state"], "failed")
        self.assertIn(
            "collector exploded",
            status["collector"]["failure_detail"],
        )
        self.assertEqual(supervisor.stop_calls, 1)

    def test_stop_timeout_retains_live_non_daemon_collector_handle(self):
        class Thread:
            def __init__(self):
                self.join_calls = []

            def join(self, timeout):
                self.join_calls.append(timeout)

            def is_alive(self):
                return True

        broker, _clock = self.make_broker()
        thread = Thread()
        broker._collector_thread = thread
        broker.stop_collector(timeout=0.01)

        self.assertIs(broker._collector_thread, thread)
        self.assertEqual(len(thread.join_calls), 1)
        self.assertGreater(thread.join_calls[0], 0)
        self.assertLessEqual(thread.join_calls[0], 0.01)
        self.assertEqual(
            broker.status_response()["collector"]["state"],
            "stop_timeout",
        )


if __name__ == "__main__":
    unittest.main()
