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
        self.assertAlmostEqual(window["change"], 1.5, msg="newest read minus oldest, signed")
        history.add(metric, .1, 55)
        self.assertEqual(history.summary(55)[metric]["300"]["count"], 1)
        self.assertEqual(history.summary(356)[metric]["300"]["mean"], .1)

    def test_long_window_spans_gaps_that_reset_the_short_ones(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[0]
        for stamp in range(0, 600, 10):
            history.add(metric, 1.0, stamp)
        # A 50 s gap: the short windows start over, the 30-minute one keeps the run before it.
        history.add(metric, -1.0, 650)
        row = history.summary(650)[metric]
        self.assertEqual(row["60"]["count"], 1)
        self.assertEqual(row["300"]["count"], 1)
        self.assertEqual(row["1800"]["count"], 61)
        self.assertAlmostEqual(row["1800"]["mean"], 59 / 61)
        self.assertEqual(row["1800"]["span_seconds"], 650)
        # The short windows' change starts over with them; the long one spans the gap.
        self.assertEqual((row["60"]["change"], row["300"]["change"]), (0, 0))
        self.assertAlmostEqual(row["1800"]["change"], -2.0)
        self.assertEqual(sorted(k for k in row if k.isdigit()), ["1800", "300", "60"])

    def _pre_change_summary(self):
        """A current-format summary saved before windows carried a change: a drive that
        began three hours earlier (beyond the bounded query) and a parked figure."""
        window = lambda: {"count": 2, "mean": .25, "peak_abs": .4, "span_seconds": 10}
        return {"latest_value": .4, "observed_at": "2026-09-20T12:00:10+00:00",
                "60": window(), "300": window(), "1800": window(),
                "drive": {"change": .3, "start_value": .1, "started_at": "2026-09-20T09:00:00+00:00",
                          "count": 1000, "span_seconds": 10810.0},
                "parked": {"change": .05, "previous_value": .05,
                           "previous_observed_at": "2026-09-19T20:00:00+00:00",
                           "observed_at": "2026-09-20T09:00:00+00:00"}}

    def _historian(self, metric, *rows):
        historian = mock.Mock()
        historian.latest_sample.side_effect = lambda name: rows[0] if name == metric else None
        historian.query_samples.return_value = list(rows)
        return historian

    def test_saved_windows_without_a_change_load_and_gain_it_without_losing_anything(self):
        metric = radar.METRICS[0]
        saved = self._pre_change_summary()
        radar.AlignmentHistory._validate_retained(saved)
        with self.assertRaises(ValueError):
            radar.AlignmentHistory._validate_retained({**saved, "60": {**saved["60"], "change": 41}})
        latest = dict(metric=metric, source=radar.SOURCE, unit="deg", value=.4,
                      observed_at="2026-09-20T12:00:10+00:00", captured_at="2026-09-20T12:00:15+00:00")
        first = {**latest, "value": .1, "observed_at": "2026-09-20T12:00:00+00:00"}
        expected = json.loads(json.dumps(saved))
        history = radar.AlignmentHistory()
        history.last[metric] = saved
        history.restore_from_historian(self._historian(metric, latest, first))
        row = history.summary(0)[metric]
        for seconds in ("60", "300", "1800"):
            self.assertAlmostEqual(row[seconds].pop("change"), .3)
        self.assertEqual(row, expected, "the drive start, parked figure and windows are kept")

    def test_saved_summary_newer_than_the_historian_waits_for_a_live_read(self):
        metric = radar.METRICS[0]
        saved = self._pre_change_summary()
        expected = json.loads(json.dumps(saved))
        behind = dict(metric=metric, source=radar.SOURCE, unit="deg", value=.3,
                      observed_at="2026-09-20T12:00:00+00:00", captured_at="2026-09-20T12:00:05+00:00")
        history = radar.AlignmentHistory()
        history.last[metric] = saved
        history.restore_from_historian(self._historian(metric, behind))
        self.assertEqual(history.summary(0)[metric], expected, "older history never replaces it")
        history.add(metric, .5, 100.0, "2026-09-20T12:00:20+00:00")
        self.assertIn("change", history.summary(0)[metric]["60"])

    def test_drive_change_accumulates_across_a_gap_that_resets_the_windows(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[0]
        history.add(metric, .10, 0)
        history.add(metric, .15, 10)
        history.add(metric, .30, 70)
        row = history.summary(70)[metric]
        self.assertEqual(row["60"]["count"], 1)
        self.assertAlmostEqual(row["drive"]["change"], .20)
        self.assertEqual(row["drive"]["start_value"], .10)
        self.assertEqual(row["drive"]["count"], 3)
        self.assertEqual(row["drive"]["span_seconds"], 70)

    def test_gap_over_five_minutes_starts_a_new_drive(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[0]
        history.add(metric, .10, 0)
        history.add(metric, .20, 300)
        self.assertEqual(history.summary(0)[metric]["drive"]["count"], 2)
        history.add(metric, .50, 601)
        drive = history.summary(0)[metric]["drive"]
        self.assertEqual((drive["count"], drive["change"], drive["start_value"]), (1, 0, .50))

    def test_drive_longer_than_the_sample_window_keeps_its_start(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[1]
        for stamp in range(0, 3 * 3600, 10):
            history.add(metric, .1 + stamp / 1e6, stamp)
        drive = history.summary(0)[metric]["drive"]
        self.assertEqual(drive["start_value"], .1)
        self.assertEqual(drive["count"], 1080)
        self.assertAlmostEqual(drive["change"], 10790 / 1e6)
        self.assertEqual(drive["span_seconds"], 10790)
        self.assertLessEqual(len(history.samples[metric]), radar.MAX_SAMPLES)

    def test_drive_continues_after_restart_within_the_gap_only(self):
        metric = radar.METRICS[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.json"
            history = radar.AlignmentHistory(path)
            history.add(metric, .2, 10, "2026-09-20T10:00:00+00:00")
            history.add(metric, .4, 20, "2026-09-20T10:00:10+00:00")
            history.flush()
            resumed = radar.AlignmentHistory(path)
            resumed.add(metric, .7, 1, "2026-09-20T10:04:00+00:00")
            drive = resumed.summary(0)[metric]["drive"]
            self.assertAlmostEqual(drive["change"], .5)
            self.assertEqual((drive["count"], drive["started_at"]), (3, "2026-09-20T10:00:00+00:00"))
            self.assertEqual(drive["span_seconds"], 240)
            late = radar.AlignmentHistory(path)
            late.add(metric, .7, 1, "2026-09-20T10:05:11+00:00")
            drive = late.summary(0)[metric]["drive"]
            self.assertEqual((drive["count"], drive["change"]), (1, 0))
            # A reading stamped before the retained one never continues it.
            early = radar.AlignmentHistory(path)
            early.add(metric, .7, 1, "2026-09-20T09:59:00+00:00")
            self.assertEqual(early.summary(0)[metric]["drive"]["count"], 1)

    def test_new_drive_records_the_change_across_the_park(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[0]
        history.add(metric, .10, 0, "2026-09-20T10:00:00+00:00")
        self.assertNotIn("parked", history.summary(0)[metric])
        history.add(metric, .20, 10, "2026-09-20T10:00:10+00:00")
        history.add(metric, .35, 4000, "2026-09-20T11:06:40+00:00")
        parked = history.summary(0)[metric]["parked"]
        self.assertAlmostEqual(parked["change"], .15)
        self.assertEqual(parked["previous_value"], .20)
        self.assertEqual(parked["previous_observed_at"], "2026-09-20T10:00:10+00:00")
        self.assertEqual(parked["observed_at"], "2026-09-20T11:06:40+00:00")
        history.add(metric, .60, 4010, "2026-09-20T11:06:50+00:00")
        self.assertEqual(history.summary(0)[metric]["parked"], parked)

    def test_gap_under_five_minutes_leaves_the_parked_figure_alone(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[0]
        history.add(metric, .1, 0, "2026-09-20T10:00:00+00:00")
        history.add(metric, .2, 4000, "2026-09-20T11:06:40+00:00")
        before = history.summary(0)[metric]["parked"]
        history.add(metric, .9, 4290, "2026-09-20T11:11:30+00:00")
        self.assertEqual(history.summary(0)[metric]["parked"], before)

    def test_parked_change_survives_a_restart_during_the_park(self):
        metric = radar.METRICS[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.json"
            history = radar.AlignmentHistory(path)
            history.add(metric, .2, 10, "2026-09-20T10:00:00+00:00")
            history.flush()
            resumed = radar.AlignmentHistory(path)
            resumed.add(metric, .5, 1, "2026-09-20T12:00:00+00:00")
            parked = resumed.summary(0)[metric]["parked"]
            self.assertAlmostEqual(parked["change"], .3)
            self.assertEqual(parked["previous_observed_at"], "2026-09-20T10:00:00+00:00")
            resumed.flush()
            # A restart inside the drive keeps the figure.
            again = radar.AlignmentHistory(path)
            again.add(metric, .6, 1, "2026-09-20T12:00:20+00:00")
            self.assertEqual(again.summary(0)[metric]["parked"], parked)

    def test_no_previous_reading_means_no_parked_figure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.json"
            history = radar.AlignmentHistory(path)
            history.add(radar.METRICS[0], .5, 1, "2026-09-20T12:00:00+00:00")
            self.assertNotIn("parked", history.summary(0)[radar.METRICS[0]])
        self.assertIsNone(radar.parked_change({}, .1, "2026-09-20T12:00:00+00:00"))
        self.assertIsNone(radar.parked_change(
            {"latest_value": .1, "observed_at": "2026-09-20T12:00:00+00:00"}, .2, "2026-09-20T11:00:00+00:00"))

    def test_invalid_retained_parked_change_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.json"
            window = {"count": 2, "mean": .3, "peak_abs": .4, "span_seconds": 10}
            parked = {"change": .1, "previous_value": .3, "previous_observed_at": "2026-09-20T09:00:00+00:00",
                      "observed_at": "2026-09-20T10:00:00+00:00"}
            for bad in ({"change": 41}, {"previous_value": True}, {"observed_at": "2026-09-20T10:00:00"}):
                row = {"latest_value": .4, "observed_at": "2026-09-20T10:00:10+00:00",
                       "60": window, "300": window, "parked": {**parked, **bad}}
                path.write_text(json.dumps({"version": 1, "source": radar.SOURCE,
                                            "last": {m: dict(row) for m in radar.METRICS}}))
                with self.subTest(bad=bad):
                    self.assertIsNotNone(radar.AlignmentHistory(path).storage_error)

    def test_saved_file_drops_retired_window_and_loads_without_drive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.json"
            window = {"count": 2, "mean": .3, "peak_abs": .4, "span_seconds": 10}
            row = {"latest_value": .4, "observed_at": "2026-09-20T10:00:10+00:00",
                   "60": window, "300": window, "1800": window, "7200": window}
            path.write_text(json.dumps({"version": 1, "source": radar.SOURCE,
                                        "last": {m: dict(row) for m in radar.METRICS}}))
            history = radar.AlignmentHistory(path)
            self.assertIsNone(history.storage_error)
            saved = history.summary(0)[radar.METRICS[0]]
            self.assertNotIn("7200", saved)
            self.assertNotIn("drive", saved)
            history.flush()
            history.dirty = True
            history.flush()
            self.assertNotIn("7200", path.read_text())
            # The first reading after loading it starts a drive.
            history.add(radar.METRICS[0], .5, 1, "2026-09-20T10:00:20+00:00")
            self.assertEqual(history.summary(0)[radar.METRICS[0]]["drive"]["count"], 1)

    def test_invalid_retained_drive_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.json"
            window = {"count": 2, "mean": .3, "peak_abs": .4, "span_seconds": 10}
            drive = {"change": .1, "start_value": .3, "started_at": "2026-09-20T10:00:00+00:00",
                     "count": 2, "span_seconds": 10}
            for bad in ({"change": 41}, {"count": True}, {"span_seconds": -1},
                        {"started_at": "2026-09-20T10:00:00"}):
                row = {"latest_value": .4, "observed_at": "2026-09-20T10:00:10+00:00",
                       "60": window, "300": window, "drive": {**drive, **bad}}
                path.write_text(json.dumps({"version": 1, "source": radar.SOURCE,
                                            "last": {m: dict(row) for m in radar.METRICS}}))
                with self.subTest(bad=bad):
                    self.assertIsNotNone(radar.AlignmentHistory(path).storage_error)

    def test_hours_of_ten_second_reads_fit_the_sample_bound(self):
        history = radar.AlignmentHistory()
        metric = radar.METRICS[1]
        for stamp in range(0, 7201, 10):
            history.add(metric, .5, stamp)
        self.assertEqual(history.summary(7200)[metric]["1800"]["count"], 181)
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

    def test_history_recovery_rebuilds_a_legacy_summary_with_every_window_and_drive(self):
        metric = radar.METRICS[0]
        latest = dict(metric=metric, source=radar.SOURCE, unit="deg", value=.4,
                      observed_at="2026-09-20T10:00:10+00:00", captured_at="2026-09-20T10:00:15+00:00")
        earlier = {**latest, "value": .2, "observed_at": "2026-09-20T09:58:00+00:00"}
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
        self.assertEqual(result["1800"]["count"], 2)
        self.assertAlmostEqual(result["1800"]["mean"], .3)
        self.assertEqual(result["drive"]["count"], 2)
        self.assertAlmostEqual(result["drive"]["change"], .2)
        self.assertEqual(result["drive"]["started_at"], earlier["observed_at"])
        kwargs = historian.query_samples.call_args.kwargs
        self.assertEqual(kwargs["limit"], radar.MAX_SAMPLES)
        self.assertEqual((kwargs["end"], kwargs["start"].isoformat()),
                         (latest["captured_at"], "2026-09-20T07:09:30+00:00"))
        # An up-to-date summary that already has every window is left alone.
        historian.query_samples.reset_mock()
        history.restore_from_historian(historian)
        historian.query_samples.assert_not_called()

    def test_history_recovery_extends_a_saved_drive_instead_of_rebuilding_it(self):
        # The broker stopped after the historian stored two readings but before the
        # summary flushed; the saved drive began three hours earlier, beyond the query.
        metric = radar.METRICS[0]
        latest = dict(metric=metric, source=radar.SOURCE, unit="deg", value=.55,
                      observed_at="2026-09-20T13:00:20+00:00", captured_at="2026-09-20T13:00:25+00:00")
        middle = {**latest, "value": .5, "observed_at": "2026-09-20T13:00:10+00:00"}
        flushed = {**latest, "value": .45, "observed_at": "2026-09-20T13:00:00+00:00"}
        historian = mock.Mock()
        historian.latest_sample.side_effect = lambda name: latest if name == metric else None
        historian.query_samples.return_value = [latest, latest, middle, flushed, flushed]
        window = {"count": 1, "mean": .45, "peak_abs": .45, "span_seconds": 0}
        history = radar.AlignmentHistory()
        history.last[metric] = {
            "latest_value": .45, "observed_at": flushed["observed_at"],
            "60": window, "300": window, "1800": window,
            "drive": {"change": .35, "start_value": .1, "started_at": "2026-09-20T10:00:00+00:00",
                      "count": 1000, "span_seconds": 10800.0},
        }
        history.restore_from_historian(historian)
        drive = history.summary(0)[metric]["drive"]
        self.assertEqual(drive["started_at"], "2026-09-20T10:00:00+00:00")
        self.assertEqual(drive["count"], 1002)
        self.assertAlmostEqual(drive["change"], .45)
        self.assertAlmostEqual(drive["span_seconds"], 10820.0)
        # The first live reading after the restart keeps extending the same drive.
        history.add(metric, .6, 5.0, "2026-09-20T13:00:30+00:00")
        drive = history.summary(0)[metric]["drive"]
        self.assertEqual((drive["started_at"], drive["count"]), ("2026-09-20T10:00:00+00:00", 1003))
        self.assertAlmostEqual(drive["change"], .5)

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
