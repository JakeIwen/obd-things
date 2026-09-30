import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data import cruise_summary as cs, cruise_harvest as ch
from tests.test_display_frames import ACC_OFF, ACC_SET_66, ACC_STANDBY_66


def frame(t, data=ACC_SET_66):
    return t, 0x5A0, bytes.fromhex(data)


def button(t, seq, pressed=False, corrupt=False):
    data = bytes((0x80 if pressed else 0, 0x30 | seq))
    return t, 0x2FA, data + bytes((cs.crc8_sae_j1850(data) ^ int(corrupt), 0x0E))


def brake(t, pressed):
    return t, 0x1FA, bytes((0, 0, 0, 2 if pressed else 0, 0, 0, 0, 0))


class ReducerTests(unittest.TestCase):
    def test_time_weighted_display_summary_and_expiry_gaps(self):
        rows = [frame(0), frame(1, "2D006A42033C2480"), frame(2, ACC_OFF)]
        rows += [(t / 10, 0x0E0, bytes((55, 0x44, 0, 0))) for t in range(21)]
        r = cs.reduce_frames(sorted(rows, key=lambda f: f[0]), start=0, end=10)
        self.assertEqual(r["engaged_seconds"], 2)
        self.assertEqual(r["following_seconds"], 1)
        self.assertEqual(r["coverage_seconds"], 4)
        self.assertEqual(r["unknown_seconds"], 6)
        self.assertEqual(r["comparison"]["mean_delta_mph"], 11)
        self.assertEqual(r["comparison"]["above_seconds"], 2)

    def test_limit_zero_and_stale_limit_never_become_zero_mph_comparison(self):
        rows = [frame(0), (0, 0x0E0, bytes((55, 0, 0, 0))), frame(1),
                (1, 0x0E0, bytes(4)), frame(2)]
        r = cs.reduce_frames(rows, start=0, end=3)
        self.assertEqual(r["comparison"]["paired_seconds"], 0.5)
        self.assertEqual(r["comparison"]["unpaired_seconds"], 2.5)

    def test_override_is_separate_and_following_includes_icon_during_override(self):
        r = cs.reduce_frames([frame(0, "2D006A42033C9500"), frame(1, ACC_OFF)], start=0, end=2)
        self.assertEqual(r["engaged_seconds"], 0)
        self.assertEqual(r["override_seconds"], 1)
        self.assertEqual(r["following_seconds"], 1)

    def test_button_cancel_edges_not_held_frames_and_raw3_transition(self):
        rows = [frame(0), button(0, 0), button(.02, 1, True), button(.04, 2, True),
                frame(.05, "2D466A42033C44C0"), frame(.1, ACC_STANDBY_66), frame(1, ACC_STANDBY_66)]
        r = cs.reduce_frames(rows, start=0, end=2)
        self.assertEqual(r["cancels"], dict(button=1, brake_associated=0, unknown=0, ambiguous=0))

    def test_brake_association_unknown_and_both_remain_distinct(self):
        for extra, wanted in (([], "unknown"), ([brake(0, False), brake(.02, True)], "brake_associated"),
                              ([brake(0, False), brake(.02, True), button(0, 0), button(.02, 1, True)], "ambiguous")):
            rows = sorted([frame(0), frame(.1, ACC_STANDBY_66), *extra], key=lambda f: f[0])
            r = cs.reduce_frames(rows, start=0, end=2)
            self.assertEqual(r["cancels"][wanted], 1)
            self.assertFalse(r["brake_association_verified"])

    def test_corrupt_or_discontinuous_buttons_cannot_supply_a_cancel_cause(self):
        for row in (button(.02, 1, True, corrupt=True), button(.02, 5, True)):
            r = cs.reduce_frames([frame(0), button(0, 0), row, frame(.1, ACC_STANDBY_66)], start=0, end=1)
            self.assertEqual(r["cancels"]["button"], 0)
            self.assertEqual(r["cancels"]["unknown"], 1)
            self.assertTrue(r["faults"])

    def test_off_and_recording_gap_are_not_cancel_events(self):
        r = cs.reduce_frames([frame(0), frame(1, ACC_OFF), frame(2), frame(8, ACC_STANDBY_66)], start=0, end=10)
        self.assertEqual(sum(r["cancels"].values()), 0)
        self.assertEqual(r["unknown_seconds"], 4)

    def test_out_of_order_rejected_and_trip_bounds_do_not_import_neighboring_trip(self):
        with self.assertRaises(ValueError):
            cs.reduce_frames([frame(1), frame(0)], start=0, end=2)
        r = cs.reduce_frames([frame(0), frame(2, ACC_OFF), frame(4)], start=1, end=3)
        self.assertEqual(r["engaged_seconds"], 0)
        self.assertEqual(r["unknown_seconds"], 1)

    def test_capture_hash_channel_namespace_and_payload(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "full.candump"
            path.write_text("(1.0) can7 5A0#" + ACC_SET_66 + "\n(1.1) can9 5A0#" + ACC_OFF + "\n")
            meta = {"size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "channel": "can7"}
            self.assertEqual(list(cs.iter_frames([path], [meta])), [frame(1)])
            meta["sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                list(cs.iter_frames([path], [meta]))


class HarvesterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "history.sqlite3"
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE trips (id INTEGER, started_us INTEGER, ended_us INTEGER, started_at TEXT, ended_at TEXT)")
            c.execute("INSERT INTO trips VALUES (67,1000000,3000000,'start','end')")
        self.chunk = {"role": "c-can", "stream": "full", "first": 1, "last": 3,
                      "path": str(self.root / "full.zst"), "size": 3, "sha256": "a" * 64, "channel": "can7"}

    def test_plan_does_not_submit_and_execute_does_not_duplicate_pending_job(self):
        def compute(*args):
            if args[0] == "tasks": return {"tasks": [{"name": ch.TASK}]}
            if args[0] == "run": return {"id": "job-1"}
            if args[0] == "status": return {"state": "running"}
            raise AssertionError(args)
        runner = mock.Mock(side_effect=compute)
        with mock.patch.object(ch, "recorded_chunks", return_value=[self.chunk]):
            plan = ch.harvest(self.db, self.root, self.root / "out", trip_id=67, runner=runner)
            self.assertEqual(plan[0]["state"], "planned")
            runner.assert_not_called()
            for _ in range(2):
                ch.harvest(self.db, self.root, self.root / "out", trip_id=67, execute=True, runner=runner)
        self.assertEqual(sum(c.args[0] == "run" for c in runner.call_args_list), 1)

    def test_missing_compute_task_refuses_before_submission(self):
        runner = mock.Mock(return_value={"tasks": []})
        with mock.patch.object(ch, "recorded_chunks", return_value=[self.chunk]), self.assertRaisesRegex(RuntimeError, "owner approval"):
            ch.harvest(self.db, self.root, self.root / "out", execute=True, runner=runner)
        self.assertEqual(runner.call_count, 1)

    def test_summary_must_match_trip_identity_and_dates(self):
        out = self.root / "out"
        report = {"schema_version": 1, "trip": {"id": 67, "started_at": "start", "ended_at": "wrong"}}
        ch.atomic_json(out / "trips/67.json", report)
        history = {"recent_trips": [{"id": 67, "started_at": "start", "ended_at": "end"}]}
        self.assertNotIn("cruise_summary", ch.attach_summaries(history, out)["recent_trips"][0])
