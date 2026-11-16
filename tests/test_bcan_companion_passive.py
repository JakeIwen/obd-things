"""The B-CAN compass/fuel companion receives parked (ignition on, engine off).

The passive branch mirrors the C-CAN display receiver: shared leases behind the
cooperative passive turn, a one-second route proof, a yield to cooperative
exclusive users, and a pause the broker takes before it claims the auxiliary
helper. Everything here is offline: fake sockets, fake interface state, and real
flock files only in a private lock directory.
"""

import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import errno
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from lib import can_handoff, can_operation_state, diagnostic_safety
from projects.vehicle_data import bcan_companion, broker as broker_module
from projects.vehicle_data import compass, compass_receiver as cr, fuel_level, fuel_range
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.can_runtime import ALL_CAN_ROLES, PassiveRoleReconciler
from projects.vehicle_data.display_receivers import DisplayReceivers
from tests.test_broker_armed_at_startup import StartupCase, full_pass, readiness_snapshot
from tests.test_broker_helper_hold import HOLD_ON, RESTORED, HoldCase, ScriptedSupervisor
from tests.test_vehicle_data_can_runtime import FakeManager, FakeResolution, FakeTopology, interface
from tests.test_vehicle_drive_recorder import auxiliary_ready_status, ready_status

COMPASS_FRAME = bytes.fromhex('000003F000')


def compass_obs():
    return compass.decode_compass_observations(0x088, COMPASS_FRAME)


class LockingManager(FakeManager):
    """observe() takes the same two shared locks the real manager takes."""

    @contextlib.contextmanager
    def observe(self, role):
        with diagnostic_safety.channel_observer_lock(f'can-role-{role}'):
            with diagnostic_safety.channel_observer_lock(self.lease.channel):
                yield self.lease


class PassiveCase(unittest.TestCase):
    def setUp(self):
        resolution = FakeResolution('b-can', 'can8', 125000)
        resolution.spec.usb_serial = 'serial-b'
        self.lease = SimpleNamespace(channel='can8', usb_serial='serial-b', dev_id=0)
        self.manager = FakeManager(FakeTopology({'b-can': resolution}), lease=self.lease)
        self.state = interface('can8', bitrate=125000)
        self.manager.interface_state_reader = lambda _channel: self.state
        self.status = ready_status()
        self.published = []
        self.broker = TelemetryBroker(acquirer=SimpleNamespace(channel='can7'), monotonic=lambda: 0.0)
        self.invalidated = []
        self.worker = cr.CompassReceiver(
            self.manager, status_reader=lambda: self.status, publish=self.store,
            invalidate=self.invalidate, invalidate_frame=self.invalidate)
        for patcher in (mock.patch.object(can_operation_state, 'active_inhibits', return_value=[]),
                        mock.patch.object(can_handoff, 'passive_turn',
                                          side_effect=lambda _bus: contextlib.nullcontext()),
                        mock.patch.object(can_handoff, 'passive_yield_requested', return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def invalidate(self, can_id=None):
        self.invalidated.append(True)
        self.broker.invalidate_bcan_companion(can_id)

    def store(self, observations, at, mono, mode):
        self.published.append(mode)
        self.broker.store_display_frame(observations, at, mono, mode)

    def hold_receiver(self, entered, finish=None, on_valid=None):
        def receive(channel, *, valid, stopped, publish, **kwargs):
            publish(compass_obs(), datetime.now(timezone.utc), 0.0)
            entered.set()
            while not stopped() and valid():
                if on_valid:
                    on_valid()
                threading.Event().wait(0.005)
        return receive


class PassiveAdmissionTests(PassiveCase):
    def test_frames_publish_listen_only_when_the_auxiliary_helper_is_idle(self):
        entered = threading.Event()
        self.worker.receiver = self.hold_receiver(entered)
        self.worker.start()
        try:
            self.assertTrue(entered.wait(2), self.worker.status())
            shown = self.broker.metric_response(compass.METRIC)
            self.assertEqual((shown['value'], shown['bus'], shown['available']), (6, 'b-can', True))
            self.assertEqual(self.published, ['listen_only'])
            self.assertEqual(self.worker.status()['channel'], 'can8')
            self.assertEqual(self.worker.status()['interface_mode'], 'listen_only')
        finally:
            self.worker.stop()
        self.assertEqual(self.worker.status()['state'], 'stopped')
        self.assertFalse(self.broker.metric_response(compass.METRIC)['available'])
        self.assertEqual(self.broker.metric_response(compass.METRIC)['last_recorded']['value'], 6)

    def test_fuel_and_range_frames_publish_through_the_same_passive_session(self):
        miles = 260
        range_bytes = bytes([0, 0, 0, 0, miles >> 3, (miles & 7) << 5])
        frames = {0x356: bytes([99]), 0x760: range_bytes}

        def receive(channel, *, publish, decode, **kwargs):
            for can_id, data in frames.items():
                data = data.ljust(bcan_companion.FRAME_DLCS[can_id], b'\0')
                publish(decode(can_id, data), datetime.now(timezone.utc), 0.0)
            self.worker.stop()

        self.worker.receiver = receive
        self.worker.run()
        self.assertEqual(self.published, ['listen_only', 'listen_only'])
        recorded = {metric: self.broker.metric_response(metric)['last_recorded']['value']
                    for metric in (fuel_level.METRIC, fuel_range.METRIC)}
        self.assertEqual(recorded, {fuel_level.METRIC: 99, fuel_range.METRIC: 260})

    def test_every_unsafe_route_mode_and_identity_rejects_the_passive_branch(self):
        base = interface('can8', bitrate=125000)
        for change in ({'up': False}, {'present': False}, {'bitrate': 500000},
                       {'listen_only': False}, {'fd_enabled': True}, {'one_shot': True},
                       {'restart_ms': 100}, {'controller_state': 'BUS-OFF'}, {'channel': 'can9'}):
            with self.subTest(change=change):
                self.state = replace(base, **change)
                self.worker.receiver = mock.Mock(side_effect=AssertionError('received on an unsafe route'))
                with self.assertRaises(RuntimeError):
                    self.worker._run_passive()
        self.state = base
        self.manager._topology.resolutions['b-can'].spec.usb_serial = 'another-adapter'
        with self.assertRaises(RuntimeError):
            self.worker._run_passive()
        self.manager._topology.resolutions['b-can'].spec.usb_serial = 'serial-b'
        with mock.patch.object(can_operation_state, 'active_inhibits', return_value=['blocked']):
            with self.assertRaises(RuntimeError):
                self.worker._run_passive()

    def test_a_route_that_turns_unsafe_mid_session_stops_publication_and_releases(self):
        released = []

        @contextlib.contextmanager
        def observe(_role):
            try:
                yield self.lease
            finally:
                released.append(True)

        self.manager.observe = observe

        def receive(channel, *, publish, valid, **kwargs):
            self.state = replace(self.state, listen_only=False)  # an armed orphan appears
            publish(compass_obs(), datetime.now(timezone.utc), 0.0)

        self.worker.receiver = receive
        with self.assertRaises(RuntimeError):
            self.worker._run_passive()
        self.assertEqual((self.published, released), ([], [True]))

    def test_an_armed_orphan_with_no_broker_proof_never_becomes_a_passive_route(self):
        self.state = interface('can8', bitrate=125000, listen_only=False)
        self.worker.receiver = mock.Mock(side_effect=AssertionError('received on an armed adapter'))
        self.worker.start()
        try:
            deadline = time.monotonic() + 2
            while 'no longer healthy' not in str(self.worker.status()['reason']) \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            self.worker.stop()
        self.assertIn('no longer healthy', self.worker.status()['reason'])
        self.worker.receiver.assert_not_called()

    def test_socket_loss_releases_the_lease_and_a_later_session_reacquires_it(self):
        sessions = []

        @contextlib.contextmanager
        def observe(_role):
            sessions.append('acquire')
            try:
                yield self.lease
            finally:
                sessions.append('release')

        self.manager.observe = observe

        def receive(channel, **kwargs):
            if sessions.count('acquire') == 1:
                raise OSError(errno.ENETDOWN, 'restoration down/up')
            self.worker.stop()

        self.worker.receiver = receive
        self.worker.run()
        self.assertEqual(sessions, ['acquire', 'release', 'acquire', 'release'])
        self.assertTrue(self.invalidated)


class PassiveHandoffTests(PassiveCase):
    def test_active_turn_waiter_gets_the_bus_within_its_deadline(self):
        entered, acquired = threading.Event(), threading.Event()
        self.worker.receiver = self.hold_receiver(entered)
        self.worker.manager = LockingManager(self.manager._topology, lease=self.lease)
        self.worker.manager.interface_state_reader = lambda _channel: self.state
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(diagnostic_safety, 'LOCK_DIR', directory):
            # setUp patched the handoff functions; use the real ones for this test.
            with mock.patch.object(can_handoff, 'passive_turn', REAL_PASSIVE_TURN), \
                    mock.patch.object(can_handoff, 'passive_yield_requested', REAL_YIELD_REQUESTED):
                self.worker.start()
                try:
                    self.assertTrue(entered.wait(2), self.worker.status())
                    started = time.monotonic()
                    with can_handoff.active_turn('b-can', wait_seconds=1.25):
                        acquired.set()
                        self.assertTrue(self.worker._passive_released.wait(0.5))
                    self.assertLess(time.monotonic() - started, 1.25)
                finally:
                    self.worker.stop()
        self.assertTrue(acquired.is_set())

    def test_pause_releases_shared_locks_before_returning_and_armed_takes_no_lease(self):
        events = []
        listening = threading.Event()

        @contextlib.contextmanager
        def observe(_role):
            events.append('acquire')
            try:
                yield self.lease
            finally:
                events.append('release')

        self.manager.observe = observe
        self.worker.receiver = self.hold_receiver(listening)
        self.worker.start()
        try:
            self.assertTrue(listening.wait(2))
            self.assertTrue(self.worker.pause_passive())
            self.assertEqual(events, ['acquire', 'release'])
            self.assertEqual(self.worker.status()['state'], 'waiting')
            # While paused only the armed branch is a route, and it takes no lease.
            self.status = auxiliary_ready_status()
            armed_route = cr.armed_compass_route(self.status)
            with mock.patch('projects.vehicle_data.drive_recorder.query_interface',
                            return_value=interface('can8', listen_only=False, bitrate=125000)):
                (route, _check, stack), _ = self.worker.admit(
                    'b-can', self.status, self.worker.status_client, self.manager,
                    expected_route=armed_route)
                stack.close()
            self.assertEqual(events, ['acquire', 'release'])
        finally:
            self.worker.stop()

    def test_paused_receiver_takes_no_lease_until_resumed(self):
        events = []

        @contextlib.contextmanager
        def observe(_role):
            events.append('acquire')
            yield self.lease
            events.append('release')

        self.manager.observe = observe
        entered = threading.Event()
        self.worker.receiver = self.hold_receiver(entered)
        self.assertTrue(self.worker.pause_passive())
        self.worker.start()
        try:
            time.sleep(0.6)
            self.assertEqual(events, [])
            self.assertIn('paused', self.worker.status()['reason'])
            self.worker.resume_passive()
            self.assertTrue(entered.wait(2))
            self.assertEqual(events, ['acquire'])
        finally:
            self.worker.stop()

    def test_a_published_yield_request_ends_the_session_and_it_is_retaken(self):
        sessions = []
        asked = threading.Event()
        second = threading.Event()

        @contextlib.contextmanager
        def observe(_role):
            sessions.append('acquire')
            if len(sessions) > 1:
                second.set()
            try:
                yield self.lease
            finally:
                sessions.append('release')

        self.manager.observe = observe
        self.worker.receiver = self.hold_receiver(asked)
        with mock.patch.object(can_handoff, 'passive_yield_requested',
                               side_effect=lambda _bus: asked.is_set() and len(sessions) == 1):
            self.worker.start()
            try:
                self.assertTrue(second.wait(3), sessions)
            finally:
                self.worker.stop()
        self.assertEqual(sessions[:3], ['acquire', 'release', 'acquire'])

    def test_armed_session_ends_when_the_pause_is_resumed(self):
        self.status = auxiliary_ready_status()
        route = cr.armed_compass_route(self.status)
        released, entered = [], threading.Event()
        self.worker.admit = lambda *a, **k: ((route, lambda: None, contextlib.ExitStack()), None)

        def receive(channel, *, valid, stopped, publish, **kwargs):
            entered.set()
            while not stopped() and valid():
                threading.Event().wait(0.005)
            released.append(True)

        self.worker.receiver = receive
        self.worker.pause_passive()
        self.worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(released, [])  # a paused armed session keeps receiving
            self.worker.resume_passive()
            deadline = time.monotonic() + 2
            while not released and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(released, [True])
        finally:
            self.worker.stop()


REAL_PASSIVE_TURN = can_handoff.passive_turn
REAL_YIELD_REQUESTED = can_handoff.passive_yield_requested


class DisplayReceiversTests(unittest.TestCase):
    def facade(self):
        broker = SimpleNamespace(status_response=lambda: {}, store_display_frame=lambda *a: None,
                                 invalidate_bcan_companion=lambda *a: None)
        return DisplayReceivers(None, broker)

    def test_start_stop_and_status_fan_out_to_both_receivers(self):
        receivers = self.facade()
        order = []
        for name in ('cluster', 'compass'):
            part = getattr(receivers, name)
            part.start = lambda name=name: order.append(f'start {name}')
            part.stop = lambda timeout=2.0, name=name: order.append(f'stop {name}')
        receivers.start()
        receivers.stop()
        self.assertEqual(order, ['start cluster', 'start compass', 'stop compass', 'stop cluster'])
        status = receivers.status()
        self.assertEqual(status['compass']['state'], 'stopped')
        self.assertEqual(status['state'], 'stopped')

    def test_c_can_pause_and_b_can_pause_are_independent(self):
        receivers = self.facade()
        self.assertTrue(receivers.pause_passive())
        self.assertTrue(receivers.cluster._pause.is_set())
        self.assertFalse(receivers.compass._pause.is_set())
        receivers.resume_passive()
        self.assertTrue(receivers.pause_bcan_passive())
        self.assertTrue(receivers.compass._pause.is_set())
        self.assertFalse(receivers.cluster._pause.is_set())
        receivers.resume_bcan_passive()
        self.assertFalse(receivers.compass._pause.is_set())

    def test_a_passive_lease_that_does_not_release_makes_the_pause_report_false(self):
        receivers = self.facade()
        receivers.compass._passive_released.clear()
        self.assertFalse(receivers.pause_bcan_passive(timeout=0.05))


class RecordingDisplay:
    """A DisplayReceivers stand-in logging the pauses the broker takes."""

    def __init__(self, log, release_bcan=True):
        self.log = log
        self.release_bcan = release_bcan

    def pause_passive(self, timeout=2.0):
        self.log.append('pause c-can')
        return True

    def resume_passive(self):
        self.log.append('resume c-can')

    def pause_bcan_passive(self, timeout=2.0):
        self.log.append('pause b-can')
        return self.release_bcan

    def resume_bcan_passive(self):
        self.log.append('resume b-can')

    def status(self):
        return {}

    def start(self):
        pass

    def stop(self, timeout=2.0):
        pass


class QuickSupervisor(ScriptedSupervisor):
    """Ends its run at once, as a helper that finished on its own."""

    def __init__(self, log, name, final=RESTORED):
        super().__init__(final)
        self.log, self.name = log, name

    def run(self, stop):
        self.log.append(f'{self.name} run')
        self.calls += 1
        self.started.set()
        return dict(self.final)


class BrokerBcanPauseTests(HoldCase):
    def epoch(self, *, release_bcan=True, auxiliary=True, active=None):
        self.log = []
        active = active or QuickSupervisor(self.log, 'active')
        aux = QuickSupervisor(self.log, 'auxiliary') if auxiliary else None
        broker = self.broker(active, aux)
        broker.display_receiver = RecordingDisplay(self.log, release_bcan)
        claim = broker._claim_helper_launch
        broker._claim_helper_launch = lambda helper: (self.log.append(f'claim {helper}'), claim(helper))[1]
        handle = broker.handle_auxiliary_drive_event
        broker.handle_auxiliary_drive_event = lambda event: (self.log.append('auxiliary outcome'),
                                                              handle(event))[1]
        return broker, active, aux

    def wait_for(self, entry):
        deadline = time.monotonic() + 5
        while entry not in self.log and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIn(entry, self.log)

    def test_b_can_is_paused_before_the_claim_and_resumed_after_the_auxiliary_outcome(self):
        broker, _active, aux = self.epoch()
        broker._run_active_drive_if_ready()
        self.wait_for('resume b-can')
        self.assertEqual(aux.calls, 1)
        order = [e for e in self.log if 'b-can' in e or e in ('claim auxiliary_drive', 'auxiliary run',
                                                              'auxiliary outcome')]
        self.assertEqual(order, ['pause b-can', 'claim auxiliary_drive', 'auxiliary run',
                                 'auxiliary outcome', 'resume b-can'])
        self.assertEqual(self.log.count('pause b-can'), self.log.count('resume b-can'))

    def test_an_unreleased_b_can_lease_blocks_only_the_auxiliary_launch(self):
        broker, active, aux = self.epoch(release_bcan=False)
        broker._run_active_drive_if_ready()
        self.assertEqual((active.calls, aux.calls), (1, 0))
        self.assertNotIn('claim auxiliary_drive', self.log)
        self.assertEqual(self.log.count('resume b-can'), 1)
        shown = broker.status_response()['auxiliary_drive']
        self.assertEqual(shown['reason'], 'can_busy')
        self.assertEqual(self.states(broker)['auxiliary_drive'], 'idle')
        self.assertIsNone(broker._auxiliary_drive_blocked_reason)

    def test_a_hold_between_the_pause_and_the_claim_resumes_b_can_and_launches_nothing(self):
        active = ScriptedSupervisor()
        broker, _active, aux = self.epoch(active=active)
        claim = broker._claim_helper_launch

        def hold_then_claim(helper):
            if helper == 'auxiliary_drive':
                self.log.append('hold lands')
                broker.helper_hold_request(dict(HOLD_ON))
            return claim(helper)

        broker._claim_helper_launch = hold_then_claim
        broker._run_active_drive_if_ready()
        self.assertEqual(aux.calls, 0)
        self.assertLess(self.log.index('pause b-can'), self.log.index('hold lands'))
        self.assertLess(self.log.index('hold lands'), self.log.index('resume b-can'))
        self.assertEqual(self.states(broker)['auxiliary_drive'], 'idle')

    def test_a_failed_thread_start_resumes_b_can_and_releases_the_claim(self):
        broker, _active, aux = self.epoch()
        real = broker_module.threading.Thread

        def refuse(*args, **kwargs):
            if kwargs.get('name') == 'van-telemetry-bcan-auxiliary':
                raise RuntimeError('cannot start new thread')
            return real(*args, **kwargs)

        with mock.patch.object(broker_module.threading, 'Thread', side_effect=refuse):
            with self.assertRaises(RuntimeError):
                broker._run_active_drive_if_ready()
        self.assertEqual(aux.calls, 0)
        self.assertEqual(self.log.count('resume b-can'), 1)
        self.assertEqual(self.states(broker)['auxiliary_drive'], 'idle')

    def test_c_can_only_epochs_never_pause_b_can(self):
        for case, kwargs in (('disabled', {'auxiliary': False}),):
            with self.subTest(case=case):
                broker, active, _ = self.epoch(**kwargs)
                broker._run_active_drive_if_ready()
                self.assertEqual(active.calls, 1)
                self.assertFalse([e for e in self.log if 'b-can' in e])
                self.assertEqual(self.log.count('pause c-can'), 1)
        broker, active, aux = self.epoch()
        broker._auxiliary_drive_blocked_reason = 'helper_failed'
        broker._run_active_drive_if_ready()
        self.assertEqual((active.calls, aux.calls), (1, 0))
        self.assertFalse([e for e in self.log if 'b-can' in e])

    def test_the_epoch_join_timeout_does_not_resume_b_can_before_the_helper_is_restored(self):
        log, release = [], threading.Event()

        class Stubborn(QuickSupervisor):
            def run(self, stop):
                log.append('auxiliary run')
                release.wait(5)
                return dict(self.final)

        self.log = log
        broker = self.broker(QuickSupervisor(log, 'active'), Stubborn(log, 'auxiliary'))
        broker.display_receiver = RecordingDisplay(log)
        original = threading.Thread.join

        def quick_join(thread, timeout=None):
            return original(thread, 0.2 if timeout == 15.0 else timeout)

        with mock.patch.object(threading.Thread, 'join', quick_join):
            broker._run_active_drive_if_ready()
        self.assertNotIn('resume b-can', log)
        release.set()
        self.wait_for('resume b-can')


class ReconciliationWithCompanionTests(unittest.TestCase):
    def reconciler(self):
        resolutions, states, written = {}, {}, []
        for role in ALL_CAN_ROLES:
            bitrate = 125000 if role == 'b-can' else 500000
            channel = f'can{len(resolutions)}'
            resolution = FakeResolution(role, channel, bitrate)
            resolution.spec.usb_serial, resolution.spec.dev_id = f'serial-{role}', 0
            resolutions[role] = resolution
            states[channel] = interface(channel, bitrate=bitrate, up=role != 'spare',
                                        listen_only=True)
        self.generation = 'g1'
        manager = FakeManager(FakeTopology(resolutions))
        manager.status_snapshot = lambda: {
            'generation': self.generation,
            'roles': {role: {'passive_ready': True, 'safe': True} for role in ALL_CAN_ROLES},
            'ready': True,
        }
        reconciler = PassiveRoleReconciler(
            manager, interface_state_reader=lambda channel: states[channel],
            inhibit_reader=lambda _channel: (), topology_writer=lambda *a, **k: written.append(a[1]),
            configure=mock.Mock(side_effect=AssertionError('no configuration expected')),
            keep_down=mock.Mock(side_effect=AssertionError('no configuration expected')))
        return reconciler, resolutions['b-can'], written

    def test_a_full_pass_reports_b_can_busy_while_the_companion_holds_its_lease(self):
        reconciler, b_can, written = self.reconciler()
        lease = SimpleNamespace(channel=b_can.channel, usb_serial='serial-b-can', dev_id=0)
        companion = LockingManager(reconciler.manager.topology(), lease=lease)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(diagnostic_safety, 'LOCK_DIR', directory):
            with companion.observe('b-can'):
                first = reconciler.reconcile_if_needed()  # startup: a full pass
                self.assertEqual(first['roles']['b-can']['state'], 'busy')
                self.assertEqual(first['roles']['c-can']['state'], 'ready')
                written.clear()
                self.generation = 'g2'  # a topology generation change: another full pass
                second = reconciler.reconcile_if_needed()
                self.assertEqual(second['roles']['b-can']['state'], 'busy')
                self.assertIn('c-can', written)  # healthy roles were re-proven too
                self.assertNotIn('b-can', written)
            self.assertEqual(reconciler._one('b-can').state, 'ready')


class StartupWithCompanionTests(StartupCase):
    def test_a_busy_b_can_stays_pending_until_a_verified_snapshot_settles_it(self):
        self.cycle(full_pass(b_can='busy'))
        self.assertEqual(self.broker._startup_unassessed, {'b-can'})
        self.begin_inhibit.assert_not_called()
        self.cycle(readiness_snapshot())
        self.assertEqual(self.broker._startup_unassessed, set())

    def test_an_armed_b_can_found_once_conclusive_is_inhibited_never_made_passive(self):
        self.cycle(full_pass(b_can='busy'))
        self.cycle(full_pass(b_can='armed'))
        self.assertEqual(len(self.written()), 1)
        self.assertTrue(self.written()[0][0].endswith('-b-can'))


if __name__ == '__main__':
    unittest.main()
