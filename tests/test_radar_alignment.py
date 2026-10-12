import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data import radar_alignment as radar, transmit_permit as permits
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.metrics import METRICS
from tests.test_active_drive import FakeBackend, ActiveSessionTests, snapshot, rpm_observation


class RadarAlignmentTests(unittest.TestCase):
    def test_decode_signed_angles_exact_echo_length_and_sentinels(self):
        payload = b"\x62\x08\x45" + struct.pack(">ii", -1259959, -6817)
        self.assertEqual(radar.decode_alignment(payload), (-1.259959, -.006817))
        for bad in (payload[:-1], payload + b"\0", b"\x62\x08\x50" + payload[3:],
                    b"\x62\x08\x45" + b"\xff" * 8,
                    b"\x62\x08\x45" + struct.pack(">ii", 21000000, 0)):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                radar.decode_alignment(bad)

    def poll(self, frames, fail_consume=None):
        class FakeSocket:
            timeout = None
            sent = []

            def settimeout(self, timeout):
                self.timeout = timeout

            def recv(self, count):
                if self.timeout == 0:
                    raise BlockingIOError()
                if not frames:
                    raise TimeoutError()
                return frames.pop(0)

            def send(self, frame):
                self.sent.append(frame)
                return len(frame)

        sock = FakeSocket()
        poller = radar.RadarAlignmentPoller("can7", monotonic=lambda: 10)
        poller.sock = sock
        with mock.patch.object(permits, "consume", side_effect=fail_consume) as consume:
            result = poller.poll(object(), object())
        return result, sock.sent, consume

    @staticmethod
    def frame(data, can_id=None):
        return struct.pack(radar.FRAME, can_id or radar.EFF | radar.MODULE.rxid,
                           len(data), data.ljust(8, b"\0"))

    def test_only_exact_first_frame_gets_single_flow_control(self):
        payload = b"\x62\x08\x45" + struct.pack(">ii", 278000, -200000)
        first = self.frame(b"\x10\x0b" + payload[:6])
        last = self.frame(b"\x21" + payload[6:])
        result, sent, consume = self.poll([first, last])
        self.assertTrue(result.available)
        self.assertEqual(result.angles, (.278, -.2))
        self.assertEqual([struct.unpack(radar.FRAME, frame)[2] for frame in sent],
                         [radar.REQUEST_DATA, radar.FLOW_CONTROL_DATA])
        self.assertEqual(consume.call_count, 2)
        bad_frames = (
            self.frame(b"\x10\x0b\x62\x08\x50\0\0\0"),
            self.frame(b"\x10\x20\x62\x08\x45\0\0\0"),
            self.frame(b"\x10\x0b\x62\x08\x45\0\0\0", 0x123),
        )
        for bad in bad_frames:
            result, sent, _ = self.poll([bad])
            self.assertEqual(result.reason, "malformed_response")
            self.assertEqual(len(sent), 1)

    def test_expiry_before_request_sends_nothing_and_session_nrc_sends_no_fc(self):
        result, sent, _ = self.poll([], permits.ExpiredTransmitPermitError("expired"))
        self.assertEqual(result.reason, "transmit_permit_expired")
        self.assertEqual(sent, [])
        result, sent, _ = self.poll([self.frame(b"\x03\x7f\x22\x7f")])
        self.assertEqual(result.reason, "session_required")
        self.assertEqual(len(sent), 1)

    def test_expired_flow_control_never_transmits_fc_or_claims_unsent_request(self):
        first = self.frame(b"\x10\x0b\x62\x08\x45\0\0\0")
        result, sent, _ = self.poll([first], [None, permits.ExpiredTransmitPermitError("expired")])
        self.assertEqual(result.reason, "response_timeout")
        self.assertEqual(len(sent), 1)

    def test_summary_deduplicates_retains_parked_windows_and_resets_after_gap(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[0]
        for stamp, value in ((0, -.9), (10, .9), (10, .9), (20, .6)):
            history.add(metric, value, stamp)
        window = history.summary(20)[metric]["60"]
        self.assertEqual(window["count"], 3)
        self.assertAlmostEqual(window["mean"], .2)
        self.assertEqual(window["peak_abs"], .9)
        history.add(metric, .1, 55)
        self.assertEqual(history.summary(55)[metric]["300"]["count"], 1)
        self.assertEqual(history.summary(356)[metric]["300"]["mean"], .1)

    def test_long_windows_span_gaps_that_reset_the_short_ones(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[0]
        for stamp in range(0, 600, 10):
            history.add(metric, 1.0, stamp)
        # A 50 s gap: the short windows start over, the long ones keep the run before it.
        history.add(metric, -1.0, 650)
        row = history.summary(650)[metric]
        self.assertEqual(row["60"]["count"], 1)
        self.assertEqual(row["300"]["count"], 1)
        self.assertEqual(row["1800"]["count"], 61)
        self.assertEqual(row["7200"]["count"], 61)
        self.assertAlmostEqual(row["1800"]["mean"], 59 / 61)
        self.assertEqual(row["1800"]["span_seconds"], 650)
        # Samples older than two hours leave the longest window; the 30-minute one trails.
        history.add(metric, -1.0, 7500)
        row = history.summary(7500)[metric]
        self.assertEqual(row["7200"]["count"], 32)
        self.assertEqual(row["1800"]["count"], 1)
        self.assertAlmostEqual(row["7200"]["mean"], 28 / 32)
        self.assertEqual(row["7200"]["span_seconds"], 7200)
        self.assertEqual(sorted(k for k in row if k.isdigit()), ["1800", "300", "60", "7200"])

    def test_two_hours_of_ten_second_reads_fit_the_sample_bound(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[1]
        for stamp in range(0, 7201, 10):
            history.add(metric, .5, stamp)
        self.assertEqual(history.summary(7200)[metric]["7200"]["count"], 721)
        self.assertLessEqual(len(history.samples[metric]), radar.MAX_SAMPLES)

    def test_legacy_saved_summary_loads_without_long_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.json"
            window = {"count": 2, "mean": .3, "peak_abs": .4, "span_seconds": 10}
            row = {"latest_value": .4, "observed_at": "2026-09-20T10:00:10+00:00",
                   "60": window, "300": window}
            path.write_text(json.dumps({"version": 1, "source": radar.SOURCE,
                                        "last": {m: dict(row) for m in radar.METRICS}}))
            history = radar.AlignmentHistory(path)
            self.assertIsNone(history.storage_error)
            self.assertEqual(history.summary(0)[radar.METRICS[0]]["300"], window)
            self.assertNotIn("1800", history.summary(0)[radar.METRICS[0]])
            del row["300"]
            path.write_text(json.dumps({"version": 1, "source": radar.SOURCE,
                                        "last": {m: dict(row) for m in radar.METRICS}}))
            self.assertIn("required window", radar.AlignmentHistory(path).storage_error)

    def test_history_recovery_rebuilds_a_legacy_summary_with_every_window(self):
        metric = radar.METRICS[0]
        latest = dict(metric=metric, source=radar.SOURCE, unit="deg", value=.4,
                      observed_at="2026-09-20T10:00:10+00:00", captured_at="2026-09-20T10:00:15+00:00")
        earlier = {**latest, "value": .2, "observed_at": "2026-09-20T09:20:00+00:00"}
        historian = mock.Mock()
        historian.latest_sample.side_effect = lambda name: latest if name == metric else None
        historian.query_samples.return_value = [latest, earlier]
        history = radar.AlignmentHistory()
        legacy = {"count": 1, "mean": .4, "peak_abs": .4, "span_seconds": 0}
        history.last[metric] = {"latest_value": .4, "observed_at": latest["observed_at"],
                                "60": legacy, "300": legacy}
        history.restore_from_historian(historian)
        result = history.summary(0)[metric]
        self.assertEqual(result["60"]["count"], 1)
        self.assertEqual(result["1800"]["count"], 1)
        self.assertEqual(result["7200"]["count"], 2)
        self.assertAlmostEqual(result["7200"]["mean"], .3)
        kwargs = historian.query_samples.call_args.kwargs
        self.assertEqual(kwargs["limit"], radar.MAX_SAMPLES)
        self.assertEqual((kwargs["end"], kwargs["start"].isoformat()),
                         (latest["captured_at"], "2026-09-20T08:00:10+00:00"))
        # An up-to-date summary that already has every window is left alone.
        historian.query_samples.reset_mock()
        history.restore_from_historian(historian)
        historian.query_samples.assert_not_called()

    def test_retained_windows_survive_restart_without_becoming_new_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.json"
            history = radar.AlignmentHistory(path)
            metric = radar.METRICS[0]
            history.add(metric, .2, 10, "2026-09-20T10:00:00+00:00")
            history.add(metric, .4, 20, "2026-09-20T10:00:10+00:00")
            before = history.summary(20)
            self.assertEqual(history.summary(999999), before)
            history.flush()
            restored = radar.AlignmentHistory(path)
            self.assertEqual(restored.summary(0), before)
            self.assertEqual(len(restored.samples[metric]), 0)
            restored.add(metric, .7, 1, "2026-09-21T10:00:00+00:00")
            self.assertEqual(restored.summary(2)[metric]["60"]["count"], 1)
            self.assertEqual(restored.summary(2)[metric]["60"]["mean"], .7)

    def test_history_recovery_deduplicates_saved_snapshots_and_keeps_original_date(self):
        metric = radar.METRICS[0]
        latest = dict(metric=metric, source=radar.SOURCE, unit="deg", value=.4,
                      observed_at="2026-09-20T10:00:10+00:00", captured_at="2026-09-20T10:00:15+00:00")
        earlier = {**latest, "value": .2, "observed_at": "2026-09-20T10:00:00+00:00"}
        historian = mock.Mock()
        historian.latest_sample.side_effect = lambda name: latest if name == metric else None
        historian.query_samples.return_value = [latest, latest, earlier, earlier]
        history = radar.AlignmentHistory()
        history.restore_from_historian(historian)
        result = history.summary(0)[metric]
        self.assertEqual(result["observed_at"], latest["observed_at"])
        self.assertEqual(result["60"]["count"], 2)
        self.assertAlmostEqual(result["60"]["mean"], .3)
        self.assertEqual(len(history.samples[metric]), 0)

    def test_broker_records_observations_not_browser_reads(self):
        clock = [100.0]
        broker = TelemetryBroker(acquirer=mock.Mock(channel="can7"), monotonic=lambda: clock[0])
        event = dict(type="observation", metric=radar.METRICS[0], value=.85,
                     unit="deg", source=radar.SOURCE, bus="c-can", quality="candidate",
                     interface_mode="armed_diagnostic")
        broker.handle_active_drive_event(event)
        for _ in range(5):
            summary = broker.snapshot_response()["status"]["radar_alignment"]
            self.assertEqual(summary[radar.METRICS[0]]["60"]["count"], 1)
        self.assertFalse(METRICS[radar.METRICS[0]].sources[0].publisher_allowed)

    def test_optional_radar_failure_does_not_stop_pcm_tpms_and_closes_socket(self):
        backend = FakeBackend(snapshots=[
            snapshot((750, 751, 752), rpm_observation()) for _ in range(5)
        ] + [snapshot((0, 0, 0), rpm_observation(0))])
        poller = mock.Mock()
        poller.poll.return_value = radar.RadarResult(False, reason="session_required", detail="no session")
        backend.open_radar = lambda: poller
        outcome, events, _ = ActiveSessionTests().run_session(backend)
        self.assertEqual(outcome.reason, "engine_not_running")
        self.assertTrue(outcome.restored)
        self.assertEqual(backend.tpms.poll_count, 2)
        poller.poll.assert_called_once()
        self.assertEqual(len([e for e in events if e["type"] == "metric_failure"]), 2)
        poller.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
