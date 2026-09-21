import json
from pathlib import Path
import struct
import subprocess
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


class RadarDashboardTests(unittest.TestCase):
    def test_current_mean_peak_stale_and_registered_tile(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "projects/vehicle_data/static/index.html").read_text()
        section = html.split('aria-labelledby="radar-heading"')[0].rsplit("<section", 1)[1]
        self.assertIn('data-widget="radar"', section)
        script = r'''
const fs = require('fs'), vm = require('vm');
const elements = new Map();
const element = id => {if(!elements.has(id)) elements.set(id, {textContent:'',dataset:{}}); return elements.get(id);};
global.document = {getElementById:element};
global.window = {VanDashboardProfiles:{loadSettings:()=>({})}};
const source=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(source.slice(0,source.indexOf('\nbyId("refresh").addEventListener')));
const catalog=['elevation','azimuth'].map(axis=>({name:`radar.alignment.${axis}`,stale_after_seconds:30}));
const metrics={}, summaries={};
catalog.forEach(d=>{metrics[d.name]={available:true,stale:false,value:.9,age_ms:100,quality:'candidate'};
summaries[d.name]={'60':{mean:.5,count:3,span_seconds:20},'300':{mean:.4,count:3,span_seconds:20,peak_abs:1.1}};});
renderRadarAlignment({radar_alignment:summaries},catalog,metrics);
const fresh={badge:element('radar-state').textContent,current:element('radar-elevation-current').textContent,
mean:element('radar-elevation-60').textContent,coverage:element('radar-elevation-coverage').textContent,
timestampHidden:element('radar-elevation-time').hidden};
metrics['radar.alignment.elevation'].stale=true;
metrics['radar.alignment.azimuth'].age_ms=31000;
renderRadarAlignment({radar_alignment:summaries},catalog,metrics);
const stale=element('radar-state').textContent, mean=element('radar-elevation-60').textContent;
catalog.forEach(d=>{summaries[d.name].latest_value=.9;summaries[d.name].observed_at='2026-09-19T10:00:00Z';metrics[d.name]={available:false,reason:'engine_not_running'};});
renderRadarAlignment({radar_alignment:summaries},catalog,metrics);
process.stdout.write(JSON.stringify({fresh,stale,mean,parked:{badge:element('radar-state').textContent,
current:element('radar-elevation-current').textContent,mean:element('radar-elevation-60').textContent,
detail:element('radar-elevation-margin').textContent,detailHidden:element('radar-elevation-margin').hidden,
timestamp:element('radar-elevation-time').textContent,dateTime:element('radar-elevation-time').dateTime}}));
'''
        completed = subprocess.run(["node", "-e", script, str(root / "projects/vehicle_data/static/app.js")],
                                   capture_output=True, text=True, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["fresh"]["badge"], "APPROACHING ±1°")
        self.assertEqual(result["fresh"]["current"], "+0.900°")
        self.assertTrue(result["fresh"]["timestampHidden"])
        self.assertEqual(result["fresh"]["mean"], "+0.500°")
        self.assertIn("1.100°", result["fresh"]["coverage"])
        self.assertNotIn("samples", result["fresh"]["coverage"])
        self.assertNotIn("coverage", result["fresh"]["coverage"])
        self.assertEqual(result["stale"], "NO LIVE ANGLE DATA")
        self.assertEqual(result["mean"], "—")
        self.assertEqual(result["parked"]["badge"], "LAST RECORDED · NOT LIVE")
        self.assertEqual(result["parked"]["current"], "+0.900°")
        self.assertEqual(result["parked"]["mean"], "+0.500°")
        self.assertEqual(result["parked"]["detail"], "")
        self.assertTrue(result["parked"]["detailHidden"])
        self.assertTrue(result["parked"]["timestamp"])
        self.assertEqual(result["parked"]["dateTime"], "2026-09-19T10:00:00Z")


if __name__ == "__main__":
    unittest.main()
