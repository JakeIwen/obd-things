"""B-CAN 0x356 fuel tank level: decode edges, shared receive path, retention."""

from contextlib import ExitStack
from datetime import datetime, timezone
import socket
import struct
from types import SimpleNamespace
import unittest

from projects.vehicle_data import bcan_companion, compass_receiver as cr, compass, display_receiver as dr, fuel_level
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.metrics import METRICS
from tests.test_display_frames import raw_frame
from tests.test_display_receiver import Clock, TimedSocket
from tests.test_vehicle_drive_recorder import auxiliary_ready_status

TAIL = bytes.fromhex('0B800000000000')


def frame(level):
    return bytes([level]) + TAIL


class FuelLevelDecodeTests(unittest.TestCase):
    def test_valid_percent_range_decodes_to_one_candidate_observation(self):
        for level in (1, 48, 99, 100):
            obs, = fuel_level.decode_fuel_level_observations(0x356, frame(level))
            self.assertEqual((obs.metric, obs.value, obs.unit, obs.quality),
                             ('fuel.tank_level', level, '%', 'candidate'))
        self.assertIs(METRICS[fuel_level.METRIC], fuel_level.DEFINITION)
        self.assertFalse(fuel_level.DEFINITION.sources[0].publisher_allowed)

    def test_zero_is_no_value_not_loss_and_out_of_range_or_wrong_shape_is_loss(self):
        self.assertIsNone(fuel_level.decode_fuel_level_observations(0x356, frame(0)))
        self.assertEqual(fuel_level.decode_fuel_level_observations(0x356, frame(101)), ())
        self.assertEqual(fuel_level.decode_fuel_level_observations(0x356, frame(255)), ())
        for can_id in (0x355, 0x80000356, 0x40000356):
            self.assertEqual(fuel_level.decode_fuel_level_observations(can_id, frame(50)), ())
        for payload in (b'', frame(50)[:-1], frame(50) + b'\0'):
            self.assertEqual(fuel_level.decode_fuel_level_observations(0x356, payload), ())

    def test_companion_dispatch_keeps_compass_and_fuel_apart(self):
        self.assertEqual(set(bcan_companion.FRAME_DLCS), {0x088, 0x356})
        fuel, = bcan_companion.decode_bcan_companion(0x356, frame(60))
        self.assertEqual(fuel.metric, fuel_level.METRIC)
        direction, = bcan_companion.decode_bcan_companion(0x088, bytes.fromhex('000003C000'))
        self.assertEqual(direction.metric, compass.METRIC)
        self.assertEqual(bcan_companion.decode_bcan_companion(0x088, frame(60)), ())


class FuelLevelReceiveTests(unittest.TestCase):
    def receive(self, frames, until):
        clock = Clock()
        sock = TimedSocket(clock, frames)
        events = []
        dr.receive_display_frames(
            'can9', valid=lambda: True, stopped=lambda: clock.now >= until,
            publish=lambda obs, at, mono: events.append((obs[0].metric, obs[0].value)),
            lost=lambda: events.append('lost'),
            frame_dlcs=bcan_companion.FRAME_DLCS, decode=bcan_companion.decode_bcan_companion,
            socket_factory=lambda *_: sock, monotonic=clock.monotonic, wall_time=clock.wall_time)
        return events

    def test_zero_at_wake_keeps_the_reading_and_invalid_level_ends_live_evidence(self):
        events = self.receive([(1, raw_frame(0x356, frame(49))), (2, raw_frame(0x356, frame(0))),
                               (3, raw_frame(0x356, frame(50))), (4, raw_frame(0x356, frame(101)))], 5)
        self.assertEqual(events, [('fuel.tank_level', 49), ('fuel.tank_level', 50), 'lost'])


class FuelLevelBrokerTests(unittest.TestCase):
    def test_live_stale_and_loss_keep_a_dated_last_reading(self):
        clock = Clock()
        broker = TelemetryBroker(acquirer=SimpleNamespace(channel='can7'), monotonic=clock.monotonic)
        at = datetime.now(timezone.utc)
        obs = fuel_level.decode_fuel_level_observations(0x356, frame(48))
        broker.store_display_frame(obs, at, 0.0, 'armed_diagnostic')
        live = broker.metric_response(fuel_level.METRIC)
        self.assertEqual((live['value'], live['unit'], live['bus'], live['quality'], live['stale']),
                         (48, '%', 'b-can', 'candidate', False))
        clock.now = 60
        self.assertTrue(broker.metric_response(fuel_level.METRIC)['stale'])
        broker.invalidate_bcan_companion()
        retained = broker.metric_response(fuel_level.METRIC)
        self.assertFalse(retained['available'])
        self.assertEqual(retained['last_recorded']['value'], 48)
        self.assertEqual(retained['last_recorded']['observed_at'], live['observed_at'])


COMPASS_FRAME = bytes.fromhex('000003C000')


class PerMetricInvalidationTests(unittest.TestCase):
    def run_feed(self, frames, until=10):
        clock = Clock()
        broker = TelemetryBroker(acquirer=SimpleNamespace(channel='can7'), monotonic=clock.monotonic)
        sock = TimedSocket(clock, frames)
        dr.receive_display_frames(
            'can9', valid=lambda: True, stopped=lambda: clock.now >= until,
            publish=lambda obs, at, mono: broker.store_display_frame(obs, at, mono, 'armed_diagnostic'),
            lost=broker.invalidate_bcan_companion, frame_lost=broker.invalidate_bcan_companion,
            frame_dlcs=bcan_companion.FRAME_DLCS, decode=bcan_companion.decode_bcan_companion,
            socket_factory=lambda *_: sock, monotonic=clock.monotonic, wall_time=clock.wall_time)
        return broker

    def available(self, broker, metric):
        return broker.metric_response(metric)['available']

    def test_bad_fuel_frame_leaves_compass_live_and_ends_only_fuel(self):
        for bad in (raw_frame(0x356, frame(101)), raw_frame(0x356, frame(50)[:7])):
            broker = self.run_feed([(1, raw_frame(0x356, frame(50))), (2, raw_frame(0x088, COMPASS_FRAME)),
                                    (3, bad)], 4)
            self.assertTrue(self.available(broker, compass.METRIC))
            self.assertFalse(self.available(broker, fuel_level.METRIC))

    def test_bad_compass_frame_leaves_fuel_live_and_ends_only_compass(self):
        broker = self.run_feed([(1, raw_frame(0x088, COMPASS_FRAME)), (2, raw_frame(0x356, frame(50))),
                                (3, raw_frame(0x088, bytes.fromhex('0000038000')))], 4)
        self.assertTrue(self.available(broker, fuel_level.METRIC))
        self.assertFalse(self.available(broker, compass.METRIC))

    def test_transport_loss_ends_both(self):
        clock = Clock()
        overflow = [(socket.SOL_SOCKET, dr.SO_TIMESTAMPNS, struct.pack(dr.TIMESPEC, 1_790_000_003, 0)),
                    (socket.SOL_SOCKET, dr.SO_RXQ_OVFL, struct.pack('=I', 5))]
        broker = self.run_feed([(1, raw_frame(0x088, COMPASS_FRAME)), (2, raw_frame(0x356, frame(50))),
                                (3, raw_frame(0x356, frame(0)), overflow)], 4)
        self.assertFalse(self.available(broker, compass.METRIC))
        self.assertFalse(self.available(broker, fuel_level.METRIC))


class FuelThrottleTests(unittest.TestCase):
    def feed(self, readings, silence_until=None):
        """Run readings [(monotonic, level)] through the real receiver; return (published, checks, broker)."""
        status = auxiliary_ready_status()
        route = cr.armed_compass_route(status)
        checks, published = [], []
        clock = Clock()
        broker = TelemetryBroker(acquirer=SimpleNamespace(channel='can7'), monotonic=clock.monotonic)

        def reader():
            checks.append(1)
            return status

        def admit(*args, **kwargs):
            return (route, lambda: None, ExitStack()), None

        def receiver(channel, *, publish, valid, **kwargs):
            for at, level in readings:
                obs = fuel_level.decode_fuel_level_observations(0x356, frame(level))
                publish(obs, datetime.now(timezone.utc), at)
            worker.stop()

        def store(obs, wall, mono, mode):
            published.append((mono, obs[0].value))
            broker.store_display_frame(obs, wall, mono, mode)

        worker = cr.CompassReceiver(None, status_reader=reader, admit=admit, receiver=receiver,
                                    publish=store, invalidate=lambda: None)
        worker.run()
        self.clock = clock
        return published, checks, broker

    def test_identical_4hz_frames_publish_about_every_two_seconds_within_freshness(self):
        readings = [(n * 0.25, 60) for n in range(40)]
        published, _checks, _broker = self.feed(readings)
        self.assertEqual([mono for mono, _ in published], [0.0, 2.0, 4.0, 6.0, 8.0])
        gaps = [b - a for (a, _), (b, _) in zip(published, published[1:])]
        self.assertTrue(all(gap < fuel_level.CONTRACT['stale_after_seconds'] for gap in gaps))

    def test_ownership_check_runs_only_for_admitted_frames_not_skipped(self):
        _published, checks, _broker = self.feed([(n * 0.25, 60) for n in range(40)])
        full = len(self.feed([(n * 0.25, 60 + n) for n in range(40)])[1])
        self.assertLess(len(checks), full)

    def test_changed_value_publishes_at_once(self):
        published, _checks, _broker = self.feed([(0.0, 60), (0.25, 60), (0.5, 59), (0.75, 59)])
        self.assertEqual(published, [(0.0, 60), (0.5, 59)])

    def test_silence_after_the_last_publish_still_goes_stale(self):
        published, _checks, broker = self.feed([(0.0, 60), (0.25, 60), (2.0, 60)])
        self.assertEqual(len(published), 2)
        stored = broker.metric_response(fuel_level.METRIC)
        self.assertEqual(stored['value'], 60)
        self.assertFalse(stored['stale'])
        self.clock.now = 2.0 + fuel_level.CONTRACT['stale_after_seconds'] + 1
        self.assertTrue(broker.metric_response(fuel_level.METRIC)['stale'])



if __name__ == '__main__':
    unittest.main()
