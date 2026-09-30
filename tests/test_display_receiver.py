"""Offline continuous-display timing, ownership, and cache regressions."""

import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import errno
import socket
import struct
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from lib import can_handoff, diagnostic_safety
from projects.vehicle_data import ccan_powertrain as cp, display_receiver as dr
from projects.vehicle_data.broker import TelemetryBroker, ACTIVE_DRIVE_SOURCES
from tests.test_display_frames import ACC_OFF, ACC_SET_66, raw_frame
from tests.test_vehicle_data_can_runtime import interface, FakeResolution, FakeTopology, FakeManager
from tests.test_vehicle_drive_recorder import ready_status


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def wall_time(self):
        return 1_790_000_000 + self.now


class TimedSocket:
    def __init__(self, clock, frames):
        self.clock = clock
        self.frames = list(frames)
        self.options = []
        self.closed = False

    def setsockopt(self, *args):
        self.options.append(args)

    def bind(self, route):
        self.route = route

    def settimeout(self, seconds):
        self.timeout = seconds

    def recvmsg(self, *_args):
        if not self.frames or self.frames[0][0] > self.clock.now + self.timeout:
            self.clock.now += self.timeout
            raise socket.timeout
        arrival, frame, *extra = self.frames.pop(0)
        self.clock.now = max(self.clock.now, arrival)
        if isinstance(frame, Exception):
            raise frame
        wall = 1_790_000_000 + arrival
        sec = int(wall)
        anc = [(socket.SOL_SOCKET, dr.SO_TIMESTAMPNS,
                struct.pack(dr.TIMESPEC, sec, round((wall - sec) * 1e9)))]
        return frame, extra[0] if extra else anc, 0, ("can7",)

    def close(self):
        self.closed = True


class ContinuousReceiveTests(unittest.TestCase):
    def run_feed(self, frames, *, clock=None, end=180.0, valid=lambda: True):
        clock = clock or Clock()
        sock = TimedSocket(clock, frames)
        received = []
        dr.receive_display_frames(
            "can7", valid=valid, stopped=lambda: clock.now >= end,
            publish=lambda obs, at, mono: received.append((clock.now, obs, at, mono)),
            socket_factory=lambda *_: sock, monotonic=clock.monotonic,
            wall_time=clock.wall_time,
        )
        self.assertTrue(sock.closed)
        return received, sock

    def test_all_100_phases_of_one_hz_against_one_second_snapshot_cycle(self):
        frame = raw_frame(0x5A0, bytes.fromhex(ACC_SET_66))
        for phase in range(100):
            with self.subTest(phase=phase):
                times = [n + phase / 100 for n in range(180)]
                received, sock = self.run_feed([(t, frame) for t in times])
                self.assertEqual(len(received), 180)
                self.assertEqual([r[0] for r in received], times)
                self.assertLessEqual(max(b[0] - a[0] for a, b in zip(received, received[1:])), 1.000001)
                self.assertEqual(sock.route, ("can7",))
                filters = next(v for level, opt, v in sock.options
                               if level == cp.SOL_CAN_RAW and opt == cp.CAN_RAW_FILTER)
                self.assertEqual(list(struct.iter_unpack("=II", filters)),
                                 [(i, cp.FILTER_MASK) for i in cp.DISPLAY_FRAME_IDS])

    def test_on_change_frame_and_ten_hz_limit_publish_between_snapshots(self):
        frames = [(0.4, raw_frame(0x5A0, bytes.fromhex(ACC_SET_66))),
                  (0.731, raw_frame(0x5A0, bytes.fromhex(ACC_OFF)))]
        frames += [(n / 10 + 0.09, raw_frame(0x0E0, bytes.fromhex("37440000")))
                   for n in range(20)]
        received, _ = self.run_feed(sorted(frames), end=2)
        acc = [(t, next(o.value for o in obs if o.metric == "acc.state"))
               for t, obs, _, _ in received if any(o.metric == "acc.state" for o in obs)]
        self.assertEqual(acc, [(0.4, "engaged"), (0.731, "off")])
        self.assertEqual(sum(any(o.metric == "vehicle.speed_limit" for o in r[1]) for r in received), 20)

    def test_no_frame_no_refresh_and_rejects_old_queue_or_missing_timestamp(self):
        clock = Clock()
        clock.now = 20.0
        frame = raw_frame(0x5A0, bytes.fromhex(ACC_SET_66))
        rows, _ = self.run_feed([(1, frame), (20.1, frame, []), (20.2, frame)],
                                clock=clock, end=30)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][3], 20.2, places=5)
        self.assertAlmostEqual(rows[0][2].timestamp(), 1_790_000_020.2, places=5)

    def test_rejects_other_ids_frame_types_and_wrong_dlc(self):
        payload = bytes.fromhex(ACC_SET_66)
        frames = [(i / 10, raw_frame(can_id, payload[:dlc])) for i, (can_id, dlc) in enumerate([
            (0x4AF, 8), (0x5A0 | cp.CAN_EFF_FLAG, 8), (0x5A0 | cp.CAN_RTR_FLAG, 8),
            (0x5A0 | cp.CAN_ERR_FLAG, 8), (0x5A0, 7), (0x0E0, 8),
        ])]
        rows, _ = self.run_feed(frames, end=2)
        self.assertEqual(rows, [])

    def test_link_down_closes_socket_and_does_not_publish(self):
        clock = Clock()
        sock = TimedSocket(clock, [(0.1, OSError(errno.ENETDOWN, "link down"))])
        with self.assertRaises(OSError):
            dr.receive_display_frames("can7", valid=lambda: True, stopped=lambda: False,
                                      publish=mock.Mock(side_effect=AssertionError),
                                      socket_factory=lambda *_: sock,
                                      monotonic=clock.monotonic, wall_time=clock.wall_time)
        self.assertTrue(sock.closed)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.resolution = FakeResolution("c-can", "can7", 500000)
        self.resolution.spec.usb_serial = "serial-a"
        self.lease = SimpleNamespace(channel="can7", usb_serial="serial-a", dev_id=0)
        self.manager = FakeManager(FakeTopology({"c-can": self.resolution}), lease=self.lease)
        self.state = interface("can7")
        self.manager.interface_state_reader = lambda _: self.state
        self.status = ready_status()
        self.worker = dr.DisplayReceiver(self.manager, status_reader=lambda: self.status,
                                        publish=mock.Mock())
        self.inhibits = mock.patch.object(dr.can_operation_state, "active_inhibits", return_value=[])
        self.inhibits.start()
        self.addCleanup(self.inhibits.stop)

    def test_passive_and_armed_routes_reject_every_unsafe_mode_and_identity(self):
        for passive in (True, False):
            base = interface("can7", listen_only=passive)
            self.state = base
            self.worker._check_route("can7", "serial-a", 0, passive=passive)
            for change in ({"up": False}, {"present": False}, {"bitrate": 125000},
                           {"listen_only": not passive}, {"fd_enabled": True},
                           {"one_shot": True}, {"restart_ms": 100},
                           {"controller_state": "BUS-OFF"}, {"channel": "can8"}):
                with self.subTest(passive=passive, change=change):
                    self.state = replace(base, **change)
                    with self.assertRaises(RuntimeError):
                        self.worker._check_route("can7", "serial-a", 0, passive=passive)
            self.state = base
            with self.assertRaises(RuntimeError):
                self.worker._check_route("can7", "another-adapter", 0, passive=passive)

    def test_armed_requires_same_owner_and_inhibit_free_route(self):
        self.state = interface("can7", listen_only=False)
        self.status["current_owner"]["kind"] = "outside_tool"
        with self.assertRaises(RuntimeError):
            self.worker._check_route("can7", "serial-a", 0, passive=False)
        self.status = ready_status()
        with mock.patch.object(dr.can_operation_state, "active_inhibits", return_value=["blocked"]):
            with self.assertRaises(RuntimeError):
                self.worker._check_route("can7", "serial-a", 0, passive=False)

    def test_pause_releases_shared_role_before_helper_and_armed_takes_no_lease(self):
        events = []
        listening = threading.Event()

        @contextlib.contextmanager
        def observe(_bus):
            events.append("acquire")
            try:
                yield self.lease
            finally:
                events.append("release")

        def receive(_channel, *, valid, stopped, publish):
            listening.set()
            while not stopped() and valid():
                threading.Event().wait(0.005)

        self.manager.observe = observe
        self.worker.receiver = receive
        with mock.patch.object(dr.can_handoff, "passive_turn", side_effect=lambda _: contextlib.nullcontext()), \
             mock.patch.object(dr.can_handoff, "passive_yield_requested", return_value=False):
            self.worker.start()
            try:
                self.assertTrue(listening.wait(2))
                self.assertTrue(self.worker.pause_passive())
                self.assertEqual(events, ["acquire", "release"])
                self.state = interface("can7", listen_only=False)
                with self.worker._route(False) as route:
                    self.assertEqual(route, ("can7", "serial-a", 0))
                self.assertEqual(events, ["acquire", "release"])
            finally:
                self.worker.stop()
        self.assertEqual(self.worker.status()["state"], "stopped")

    def test_wake_waiter_gets_existing_shared_turn_within_deadline(self):
        entered = threading.Event()
        acquired = threading.Event()

        def receive(_channel, *, valid, stopped, publish):
            entered.set()
            while not stopped() and valid():
                threading.Event().wait(0.01)

        self.worker.receiver = receive
        # Exercise real flock handoff behavior, isolated from installed locks.
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(diagnostic_safety, "LOCK_DIR", directory):
            self.worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with can_handoff.active_turn("c-can", wait_seconds=1.25):
                    acquired.set()
                    self.assertTrue(self.worker._passive_released.wait(0.5))
                self.assertTrue(acquired.is_set())
            finally:
                self.worker.stop()

    def test_socket_loss_resolves_new_channel_and_closes_previous_route(self):
        received_routes = []
        released = []

        @contextlib.contextmanager
        def route(_passive):
            channel = "can7" if not received_routes else "can9"
            try:
                yield channel, "serial-a", 0
            finally:
                released.append(channel)

        def listen(_passive, current):
            received_routes.append(current[0])
            if len(received_routes) == 1:
                raise OSError(errno.ENETDOWN, "restoration down/up")
            self.worker._stop.set()

        self.worker._route = route
        self.worker._listen = listen
        self.worker.run()
        self.assertEqual(received_routes, ["can7", "can9"])
        self.assertEqual(released, received_routes)


class CacheTests(unittest.TestCase):
    def test_receipt_timestamp_atomic_state_change_and_no_snapshot_regression(self):
        broker = TelemetryBroker(acquirer=SimpleNamespace(channel="can7"), monotonic=lambda: 100.0,
                                 display_receiver=object())
        at = datetime.fromtimestamp(1_790_000_000, timezone.utc)
        engaged = cp.decode_frame_observations(0x5A0, bytes.fromhex(ACC_SET_66))
        off = cp.decode_frame_observations(0x5A0, bytes.fromhex(ACC_OFF))
        self.assertTrue(dr.DISPLAY_SOURCES <= ACTIVE_DRIVE_SOURCES)
        broker.store_display_frame(engaged, at, 90.0, "armed_diagnostic")
        broker.store_display_frame(off, at, 91.0, "armed_diagnostic")
        broker.store_display_frame(engaged, at, 89.0, "listen_only")
        for o in engaged:
            broker.handle_active_drive_event({"type": "observation", "metric": o.metric,
                "source": o.source, "value": o.value, "unit": o.unit, "quality": o.quality,
                "bus": "c-can", "interface_mode": "armed_diagnostic"})
        self.assertEqual(broker._cache["acc.state"].value, "off")
        self.assertEqual(broker._cache["acc.state"].observed_monotonic, 91.0)
        self.assertEqual(broker._cache["acc.set_speed"].observed_monotonic, 90.0)
        self.assertEqual(broker._cache["acc.state"].observed_at, at)
