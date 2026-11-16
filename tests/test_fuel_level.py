"""B-CAN 0x356 fuel tank level: decode edges, shared receive path, retention."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from projects.vehicle_data import bcan_companion, compass, display_receiver as dr, fuel_level
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.metrics import METRICS
from tests.test_display_frames import raw_frame
from tests.test_display_receiver import Clock, TimedSocket

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


if __name__ == '__main__':
    unittest.main()
