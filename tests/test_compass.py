"""Owner-video decode, receive ownership, real timestamps and stale retention."""

from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from projects.vehicle_data import compass, compass_receiver as cr, display_receiver as dr
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.last_readings import LastReadings
from projects.vehicle_data.metrics import METRICS
from tests.test_display_frames import raw_frame
from tests.test_display_receiver import Clock, TimedSocket
from tests.test_vehicle_drive_recorder import auxiliary_ready_status, ready_status, interface


REFERENCE = Path(__file__).resolve().parents[1] / (
    'projects/ecu_mapping/findings/promaster_2022/2026-10-14_compass_direction_video.json'
)


class CompassDecodeTests(unittest.TestCase):
    def test_owner_video_cardinals_and_complete_turn_sequence(self):
        reference = json.loads(REFERENCE.read_text())
        for sample in reference['samples']:
            obs, = compass.decode_compass_observations(
                0x088, bytes.fromhex(sample['most_recent_frame']['data_hex']))
            self.assertEqual(compass.CONTRACT['directions'][obs.value], sample['direction_from_route'])
        events = [e for e in reference['events'] if 0 <= e['video_seconds'] < 89]
        labels = [compass.CONTRACT['directions'][compass.decode_compass_observations(
            e['can_id'], bytes.fromhex(e['data_hex']))[0].value] for e in events]
        self.assertEqual(labels, ['NW', 'W', 'SW', 'S', 'SE', 'E', 'NE', 'N'])

    def test_unknown_payload_flags_and_other_frame_namespaces_are_rejected(self):
        good = bytes.fromhex('000003C000')
        for can_id in (0x089, 0x80000088, 0x40000088, 0x20000088):
            self.assertEqual(compass.decode_compass_observations(can_id, good), ())
        for payload in (b'', good[:-1], good + b'\0', bytes.fromhex('0000038000'),
                        bytes.fromhex('000003C100'), bytes.fromhex('000003C001'),
                        bytes.fromhex('000002C000'), bytes.fromhex('010003C000')):
            self.assertEqual(compass.decode_compass_observations(0x088, payload), ())
        self.assertIs(METRICS[compass.METRIC], compass.DEFINITION)
        self.assertFalse(compass.DEFINITION.sources[0].publisher_allowed)

    def test_silence_ages_last_report_and_receive_loss_retains_it(self):
        clock = Clock()
        broker = TelemetryBroker(acquirer=SimpleNamespace(channel='can7'), monotonic=clock.monotonic)
        at = datetime.now(timezone.utc)
        obs = compass.decode_compass_observations(0x088, bytes.fromhex('000003F000'))
        broker.store_display_frame(obs, at, 0.0, 'armed_diagnostic')
        first = broker.metric_response(compass.METRIC)
        self.assertEqual((first['value'], first['bus'], first['stale']), (6, 'b-can', False))
        clock.now = 360
        old = broker.metric_response(compass.METRIC)
        self.assertEqual((old['value'], old['stale'], old['age_ms']), (6, True, 360000))
        self.assertEqual(old['observed_at'], first['observed_at'])
        broker.invalidate_compass()
        retained = broker.metric_response(compass.METRIC)
        self.assertFalse(retained['available'])
        self.assertEqual(retained['last_recorded']['value'], 6)
        self.assertEqual(retained['last_recorded']['observed_at'], first['observed_at'])
        restored = LastReadings(METRICS)
        self.assertTrue(restored.remember(compass.METRIC, retained['last_recorded']))
        self.assertEqual(restored.get(compass.METRIC)['value'], 6)


class CompassReceiveTests(unittest.TestCase):
    def test_local_client_cannot_request_an_acquisition_or_another_route(self):
        reader = mock.Mock(return_value=auxiliary_ready_status())
        client = cr.LocalBrokerStatusClient(reader)
        for method, path in [('POST', '/v1/status'), ('POST', '/v1/acquire'), ('GET', '/v1/snapshot')]:
            with self.assertRaises(ValueError):
                client.request(method, path)
        reader.assert_not_called()

    def test_stop_during_real_route_revalidation_prevents_publication(self):
        status = auxiliary_ready_status()
        received, published = [], []
        queries = 0

        def query_interface(**kwargs):
            nonlocal queries
            queries += 1
            if queries == 2:
                worker.stop()
            return interface(listen_only=False, bitrate=125000)

        def receive(channel, *, publish, **kwargs):
            received.append(channel)
            publish(compass.decode_compass_observations(0x088, bytes.fromhex('000003C000')),
                    datetime.now(timezone.utc), 0.0)

        worker = cr.CompassReceiver(None, status_reader=lambda: status, receiver=receive,
            publish=lambda *args: published.append(args), invalidate=lambda: None)
        with mock.patch('projects.vehicle_data.drive_recorder.query_interface', query_interface):
            worker.run()
        self.assertEqual(received, ['can8'])
        self.assertEqual(queries, 2)
        self.assertEqual(published, [])

    def test_real_admission_reads_local_broker_status_before_receiving(self):
        status = auxiliary_ready_status()
        broker = TelemetryBroker(acquirer=SimpleNamespace(channel='can7'), monotonic=lambda: 0.0)
        entered, finish = threading.Event(), threading.Event()
        observed = []

        def receive(channel, *, publish, **kwargs):
            publish(compass.decode_compass_observations(0x088, bytes.fromhex('000003D000')),
                    datetime.now(timezone.utc), 0.0)
            observed.append((channel, broker.metric_response(compass.METRIC)))
            entered.set()
            finish.wait(3)

        worker = cr.CompassReceiver(None, status_reader=lambda: status, receiver=receive,
            publish=broker.store_display_frame, invalidate=broker.invalidate_compass)
        with mock.patch('projects.vehicle_data.drive_recorder.query_interface',
                        return_value=interface(listen_only=False, bitrate=125000)) as query:
            worker.start()
            try:
                self.assertTrue(entered.wait(2), worker.status())
            finally:
                finish.set()
                worker.stop(timeout=3)
        self.assertGreaterEqual(query.call_count, 2)
        self.assertEqual(observed[0][0], 'can8')
        self.assertEqual(observed[0][1]['value'], 2)
        self.assertEqual(observed[0][1]['bus'], 'b-can')
        self.assertFalse(broker.metric_response(compass.METRIC)['available'])
        self.assertEqual(broker.metric_response(compass.METRIC)['last_recorded']['value'], 2)

    def test_event_only_feed_preserves_kernel_receipt_and_does_not_resample(self):
        clock = Clock()
        sock = TimedSocket(clock, [(1, raw_frame(0x088, bytes.fromhex('000003C000'))),
                                  (326, raw_frame(0x088, bytes.fromhex('000003C800')))])
        received = []
        dr.receive_display_frames('can9', valid=lambda: True, stopped=lambda: clock.now >= 330,
            publish=lambda obs, at, mono: received.append((obs[0].value, mono)),
            frame_dlcs=compass.FRAME_DLCS, decode=compass.decode_compass_observations,
            lost=lambda: self.fail('unexpected loss'), socket_factory=lambda *_: sock,
            monotonic=clock.monotonic, wall_time=clock.wall_time)
        self.assertEqual(received, [(0, 1), (1, 326)])
        self.assertTrue(sock.closed)
        filters = next(v for level, opt, v in sock.options if opt == dr.cp.CAN_RAW_FILTER and level == dr.cp.SOL_CAN_RAW)
        self.assertEqual(list(struct.iter_unpack('=II', filters)), [(0x088, dr.cp.FILTER_MASK)])

    def test_socket_overflow_and_unknown_payload_end_previous_live_evidence(self):
        clock = Clock()
        ancillary = [(socket.SOL_SOCKET, dr.SO_TIMESTAMPNS, struct.pack(dr.TIMESPEC, 1790000001, 0)),
                     (socket.SOL_SOCKET, dr.SO_RXQ_OVFL, struct.pack('=I', 2))]
        sock = TimedSocket(clock, [(1, raw_frame(0x088, bytes.fromhex('000003D000')), ancillary),
                                  (2, raw_frame(0x088, bytes.fromhex('0000038000')))])
        events = []
        dr.receive_display_frames('can9', valid=lambda: True, stopped=lambda: clock.now >= 3,
            publish=lambda *args: events.append('new direction'), lost=lambda: events.append('lost'),
            frame_dlcs=compass.FRAME_DLCS, decode=compass.decode_compass_observations,
            socket_factory=lambda *_: sock, monotonic=clock.monotonic, wall_time=clock.wall_time)
        self.assertEqual(events, ['lost', 'new direction', 'lost'])

    def test_only_proven_auxiliary_owner_can_admit_compass(self):
        route = cr.armed_compass_route(auxiliary_ready_status())
        self.assertEqual(route.role, 'b-can')
        with self.assertRaises(RuntimeError):
            cr.armed_compass_route(ready_status())
        for field, value in [('helper_pid', None), ('restoration_failed', True), ('state', 'restoring')]:
            status = auxiliary_ready_status()
            status['auxiliary_drive'][field] = value
            with self.assertRaises(RuntimeError):
                cr.armed_compass_route(status)

    def test_owner_replacement_rejects_publication_and_closes_interval(self):
        status = auxiliary_ready_status()
        route = cr.armed_compass_route(status)
        published, invalidated, released = [], [], []

        def admit(*args, **kwargs):
            stack = ExitStack()
            stack.callback(lambda: released.append(True))
            return (route, lambda: None, stack), None

        def receiver(channel, *, publish, **kwargs):
            obs = compass.decode_compass_observations(0x088, bytes.fromhex('000003D000'))
            at = datetime.now(timezone.utc)
            publish(obs, at, 0.0)
            status['auxiliary_drive']['helper_pid'] += 1
            publish(obs, at, 1.0)
            worker.stop()

        worker = cr.CompassReceiver(None, status_reader=lambda: status, admit=admit,
            receiver=receiver, publish=lambda *args: published.append(args),
            invalidate=lambda: invalidated.append(True))
        worker.run()
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][-1], 'armed_diagnostic')
        self.assertEqual(released, [True])
        self.assertTrue(invalidated)
        self.assertEqual(worker.status()['state'], 'stopped')
