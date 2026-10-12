"""Radar alignment reference points: zeroing, rollback, calibration points, storage."""

from datetime import datetime, timedelta, timezone
import json
import pathlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from projects.vehicle_data import radar_reference as reference
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.radar_alignment import AlignmentHistory
from tests.radar_service_support import record_reading


START = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
ZERO, CLEAR = {"action": "zero"}, {"action": "clear"}


class Fixture:
    """Reference points on a real reading history, with a clock the test moves."""

    def __init__(self, directory=None):
        self.directory = directory
        self.now = START
        self.history = AlignmentHistory(
            None if directory is None else pathlib.Path(directory) / "radar-alignment.json")
        self.references = reference.RadarReferences(self.history, now=lambda: self.now)

    def read(self, elevation, azimuth, seconds=10):
        """Ten seconds pass and the helper stores one full reading."""
        self.now += timedelta(seconds=seconds)
        record_reading(self.history, self.now, elevation, azimuth)

    def state(self):
        return self.references.status_response()[1]

    def post(self, body):
        return self.references.request(body)

    def select(self, point_id):
        return self.post({"action": "select", "id": point_id})


class ZeroAndRollbackTests(unittest.TestCase):
    def test_zero_copies_the_last_full_reading_and_selects_it(self):
        fixture = Fixture()
        self.assertEqual(fixture.state()["current"], None)
        code, body = fixture.post(ZERO)
        self.assertEqual((code, body["reason"], body["points"]), (409, "no_reading", []))
        record_reading(fixture.history, START, -3.766, None)   # one axis is not a reading
        self.assertEqual(fixture.post(ZERO)[0], 409)
        fixture.read(-3.766, -0.068)
        code, body = fixture.post(ZERO)
        self.assertEqual(code, 201)
        point = body["points"][0]
        self.assertEqual((point["source"], point["angles"], point["request_id"]),
                         ("manual", {"elevation": -3.766, "azimuth": -0.068}, None))
        self.assertEqual(point["observed_at"], fixture.now.isoformat())
        self.assertEqual((body["selected"], body["available"]), (point["id"], True))
        self.assertEqual(body["current"], {"angles": point["angles"], "observed_at": point["observed_at"]})

    def test_every_earlier_point_stays_selectable(self):
        fixture = Fixture()
        fixture.read(-3.766, -0.068)
        first = fixture.post(ZERO)[1]["selected"]
        fixture.read(-3.1, -0.05)
        second = fixture.post(ZERO)[1]["selected"]
        body = fixture.state()
        self.assertEqual([point["id"] for point in body["points"]], [second, first])
        self.assertEqual(body["selected"], second)
        code, body = fixture.select(first)
        self.assertEqual((code, body["selected"], len(body["points"])), (200, first, 2))
        code, body = fixture.post(CLEAR)
        self.assertEqual((code, body["selected"], len(body["points"])), (200, None, 2))
        self.assertEqual(fixture.select(second)[1]["selected"], second)

    def test_zeroing_twice_on_one_reading_keeps_one_point(self):
        fixture = Fixture()
        fixture.read(0.2, 0.1)
        first = fixture.post(ZERO)[1]["selected"]
        fixture.post(CLEAR)
        code, body = fixture.post(ZERO)
        self.assertEqual((code, body["selected"], len(body["points"])), (200, first, 1))

    def test_only_the_three_exact_requests_are_accepted(self):
        fixture = Fixture()
        fixture.read(0.2, 0.1)
        for body in (None, [], {}, {"action": "zero", "extra": 1}, {"action": "select"},
                     {"action": "select", "id": 7}, {"action": "select", "id": "XYZ"},
                     {"action": "clear", "id": "a" * 32}, {"action": "reset"}):
            code, reply = fixture.post(body)
            self.assertEqual((code, reply["reason"], reply["available"]), (400, "invalid_request", False), body)
        code, reply = fixture.select("a" * 32)
        self.assertEqual((code, reply["reason"]), (404, "unknown_reference"))
        self.assertEqual(fixture.state()["points"], [])

    def test_the_oldest_unselected_points_go_first_and_zeroed_ones_before_calibrations(self):
        with mock.patch.object(reference, "MAX_POINTS", 3):
            fixture = Fixture()
            fixture.read(1.0, 0.0)
            oldest = fixture.post(ZERO)[1]["selected"]
            fixture.references.calibration_completed("c" * 32)
            fixture.read(2.0, 0.0)
            fixture.read(3.0, 0.0)
            fixture.post(ZERO)
            fixture.select(oldest)
            fixture.read(4.0, 0.0)
            fixture.post(ZERO)                    # a fourth point: one has to go
            fixture.select(oldest)
            fixture.read(5.0, 0.0)
            body = fixture.post(ZERO)[1]          # and a fifth
        kept = [(point["source"], point["angles"]["elevation"]) for point in body["points"]]
        # The point each zero replaced survived that trim, and the calibration point both.
        self.assertEqual(kept, [("manual", 5.0), ("calibration", 2.0), ("manual", 1.0)])


class CalibrationPointTests(unittest.TestCase):
    def test_a_finished_calibration_saves_the_first_full_reading_after_it(self):
        fixture = Fixture()
        fixture.read(-3.766, -0.068)
        manual = fixture.post(ZERO)[1]["selected"]
        fixture.now += timedelta(seconds=3)
        fixture.references.calibration_completed("c" * 32)
        body = fixture.state()
        self.assertEqual(body["pending_calibration"],
                         {"request_id": "c" * 32, "completed_at": fixture.now.isoformat()})
        self.assertEqual(len(body["points"]), 1)       # the reading from before does not count
        fixture.now += timedelta(seconds=7)
        record_reading(fixture.history, fixture.now, -0.4, None)
        self.assertEqual(len(fixture.state()["points"]), 1)   # elevation alone is not a full reading
        record_reading(fixture.history, fixture.now, None, 0.02)
        body = fixture.state()
        point = body["points"][0]
        self.assertEqual((point["source"], point["request_id"], point["angles"]),
                         ("calibration", "c" * 32, {"elevation": -0.4, "azimuth": 0.02}))
        self.assertEqual((body["pending_calibration"], body["selected"]), (None, manual))
        fixture.references.calibration_completed("c" * 32)    # reported twice: still one point
        fixture.read(-0.3, 0.02)
        self.assertEqual(len(fixture.state()["points"]), 2)
        self.assertEqual(fixture.select(point["id"])[1]["selected"], point["id"])

    def test_a_reading_without_a_usable_time_is_no_reading_and_never_raises(self):
        rows = {"elevation": (1.0, "2026-10-12T14:00:10"), "azimuth": (0.0, "not a time")}
        history = SimpleNamespace(path=None, on_reading=None, latest=lambda: rows)
        references = reference.RadarReferences(history, now=lambda: START)
        references.calibration_completed("c" * 32)
        history.on_reading()
        body = references.status_response()[1]
        self.assertEqual((body["current"], body["points"]), (None, []))
        self.assertEqual(references.request(ZERO)[0], 409)

    def test_the_broker_wires_its_reading_history_to_the_service_control(self):
        history = AlignmentHistory()
        broker = TelemetryBroker(acquirer=mock.Mock(channel="can7"), radar_history=history)
        self.addCleanup(broker.close)
        record_reading(history, START, 0.5, -0.25)
        references = broker.radar_service.endpoint(reference.API_PATH)
        code, body = references.request(ZERO)
        self.assertEqual((code, body["points"][0]["angles"]), (201, {"elevation": 0.5, "azimuth": -0.25}))
        self.assertIs(broker.radar_service.endpoint("/v1/radar/service-alignment"), broker.radar_service)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = pathlib.Path(self.tmp.name) / reference.FILE_NAME

    def test_points_selection_and_a_waiting_calibration_survive_a_restart(self):
        fixture = Fixture(self.tmp.name)
        fixture.read(-3.766, -0.068)
        fixture.post(ZERO)
        fixture.references.calibration_completed("c" * 32)
        before = fixture.state()
        self.assertTrue(before["persistent"])
        again = Fixture(self.tmp.name)
        again.now = fixture.now
        after = again.state()
        self.assertEqual({key: after[key] for key in ("points", "selected", "selected_at", "pending_calibration")},
                         {key: before[key] for key in ("points", "selected", "selected_at", "pending_calibration")})
        again.read(-0.4, 0.02)
        self.assertEqual(again.state()["points"][0]["source"], "calibration")

    def test_an_unreadable_file_is_reported_and_never_overwritten(self):
        good = Fixture(self.tmp.name)
        good.read(0.2, 0.1)
        good.post(ZERO)
        saved = json.loads(self.file.read_text())
        for damage in (b"{not json", json.dumps({**saved, "version": 2}).encode(),
                       json.dumps({**saved, "selected": "f" * 32}).encode(),
                       json.dumps({**saved, "points": [{**saved["points"][0], "angles": {"elevation": 99.0, "azimuth": 0.0}}]}).encode(),
                       json.dumps({**saved, "points": [{**saved["points"][0], "source": "calibration"}]}).encode()):
            self.file.write_bytes(damage)
            fixture = Fixture(self.tmp.name)
            fixture.read(0.3, 0.1)
            body = fixture.state()
            self.assertEqual((body["available"], body["points"]), (False, []), damage)
            self.assertIn("could not be read", body["storage_error"])
            code, reply = fixture.post(ZERO)
            self.assertEqual((code, reply["reason"]), (503, "storage_unavailable"))
            fixture.references.calibration_completed("c" * 32)
            self.assertEqual(self.file.read_bytes(), damage)

    def test_a_failed_write_changes_nothing_and_is_reported(self):
        fixture = Fixture(self.tmp.name)
        fixture.read(0.2, 0.1)
        first = fixture.post(ZERO)[1]["selected"]
        fixture.read(0.3, 0.1)
        with mock.patch.object(reference, "_atomic_json", side_effect=OSError("disk full")):
            code, reply = fixture.post(ZERO)
            self.assertEqual((code, reply["reason"], reply["selected"]), (503, "storage_unavailable", first))
            self.assertEqual(len(reply["points"]), 1)
            fixture.references.calibration_completed("c" * 32)
            body = fixture.state()
            self.assertEqual(body["pending_calibration"], None)
            self.assertIn("disk full", body["storage_error"])
        self.assertEqual(fixture.post(ZERO)[0], 201)
        self.assertIsNone(fixture.state()["storage_error"])


if __name__ == "__main__":
    unittest.main()
