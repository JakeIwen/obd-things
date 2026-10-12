"""Light decoding, receipt freshness, and the public telemetry cache."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from projects.vehicle_data import lighting, display_receiver as dr
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.metrics import METRICS
from tests.test_display_frames import raw_frame
from tests.test_display_receiver import Clock, TimedSocket


class LightingTests(unittest.TestCase):
    def test_owner_recorded_cycles_decode_to_real_booleans(self):
        for payload, expected in (
            ('40000020', (False, False)), ('42008020', (True, False)),
            ('46008020', (True, True)), ('42008020', (True, False)),
            ('40000020', (False, False)),
        ):
            with self.subTest(payload=payload):
                observations = lighting.decode_light_observations(0x73E, bytes.fromhex(payload))
                self.assertEqual(tuple(o.value for o in observations), expected)
                self.assertTrue(all(type(o.value) is bool for o in observations))
                self.assertTrue(all(o.source == lighting.SOURCE for o in observations))

    def test_exact_id_and_dlc_and_no_external_publisher(self):
        for can_id, payload in ((0x354, b'\x46\x00\x80\x20'),
                                (0x8000073E, b'\x46\x00\x80\x20'),
                                (0x73E, b'\x46'), (0x73E, b'\x46' * 8)):
            self.assertEqual(lighting.decode_light_observations(can_id, payload), ())
        for definition in lighting.METRICS:
            self.assertIs(METRICS[definition.name], definition)
            self.assertFalse(definition.sources[0].publisher_allowed)
            self.assertEqual(definition.stale_after_seconds, 2.0)

    def test_receiver_rejects_old_or_malformed_frames_and_keeps_receipt_time(self):
        clock = Clock()
        clock.now = 20.0
        high = raw_frame(0x73E, bytes.fromhex('46008020'))
        low = raw_frame(0x73E, bytes.fromhex('42008020'))
        sock = TimedSocket(clock, [(1, high), (20.1, high, []),
                                  (20.2, raw_frame(0x73E, b'\x46' * 8)),
                                  (20.3, high), (20.55, low)])
        received = []
        dr.receive_display_frames(
            'can7', valid=lambda: True, stopped=lambda: clock.now >= 23,
            publish=lambda observations, at, mono: received.append((observations, at, mono)),
            socket_factory=lambda *_: sock, monotonic=clock.monotonic,
            wall_time=clock.wall_time,
        )
        self.assertEqual([tuple(o.value for o in row[0]) for row in received],
                         [(True, True), (True, False)])
        self.assertAlmostEqual(received[1][2], 20.55)
        self.assertTrue(sock.closed)

    def test_public_cache_expires_and_accepts_off_without_retaining_on(self):
        clock = Clock()
        clock.now = 100.0
        broker = TelemetryBroker(acquirer=SimpleNamespace(channel='can7'),
                                 monotonic=clock.monotonic, display_receiver=object())
        at = datetime.fromtimestamp(1_790_000_000, timezone.utc)
        names = [d.name for d in lighting.METRICS]
        high = lighting.decode_light_observations(0x73E, bytes.fromhex('46008020'))
        off = lighting.decode_light_observations(0x73E, bytes.fromhex('40000020'))
        broker.store_display_frame(high, at, 100.0, 'listen_only')
        self.assertTrue(all(broker.metric_response(name)['value'] is True for name in names))
        clock.now = 103.0
        self.assertTrue(all(broker.metric_response(name)['stale'] for name in names))
        broker.store_display_frame(off, at, 103.0, 'listen_only')
        broker.store_display_frame(high, at, 102.0, 'listen_only')
        self.assertTrue(all(broker.metric_response(name)['value'] is False for name in names))
        self.assertFalse(any(broker.metric_response(name)['stale'] for name in names))
