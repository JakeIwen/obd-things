"""Acceptance tests for tools/warning_replay.py (offline; no CAN, no live DB)."""

import contextlib
import hashlib
import io
import json
import math
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.vehicle_data.historian import HistorianConfig, TelemetryHistorian
from tests.test_vehicle_historian import available, definition, snapshot, unavailable
from tools import warning_replay as wr

UTC = timezone.utc
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
CONFIG = HistorianConfig(trip_idle_timeout_seconds=15, rollup_seconds=60)
COOLANT_RULE = "engine_coolant_temperature_relative_high"
BATTERY_RULE = "battery_voltage_relative_low"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canned(rule, state, *, metric="engine.coolant_temperature", trip_id=None):
    return {
        "rule": rule,
        "title": "Test advisory",
        "metric": metric,
        "category": "vehicle_health",
        "severity": "warning",
        "state": state,
        "reason": f"test {state}",
        "advisory": True,
        "notification_eligible": state == "warning",
        "notification_rate_limit_seconds": 1800,
        "regime": "engine_running:stationary:rpm_idle:warm",
        "current": {
            "value": 10.0,
            "unit": "psi",
            "source": "test.source",
            "bus": "c-can",
            "quality": "verified",
            "provenance": "test provenance",
            "trip_id": trip_id,
        },
    }


class StubEvaluator:
    """Returns canned assessments keyed on the recent trip's snapshot times."""

    def __init__(self, historian, times, trip_id):
        self.historian = historian
        self.times = times
        self.trip_id = trip_id
        self.calls = 0

    def evaluate(self, *, at):
        self.calls += 1
        us = wr._to_us(at)
        t = self.times
        if us == t[10]:
            coolant = "watch"
        elif t[12] <= us <= t[20]:
            coolant = "warning"
        else:
            coolant = "unavailable"
        if us == t[5]:
            battery = "watch"
        elif us == t[7]:
            battery = "normal"
        else:
            battery = "unavailable"
        return {
            "assessments": [
                canned(COOLANT_RULE, coolant, trip_id=self.trip_id),
                canned(BATTERY_RULE, battery, metric="battery.voltage", trip_id=self.trip_id),
            ]
        }


class WarningReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.source = cls.root / "live" / "history.sqlite3"
        cls.defs = {
            "rpm": definition("engine.rpm", "rpm"),
            "speed": definition("vehicle.speed", "mph"),
            "coolant": definition("engine.coolant_temperature", "°F"),
            "battery": definition("battery.voltage", "V", stale=35.0),
            "rr": definition("tire.pressure.rr", "psi", stale=35.0),
            "load": definition("engine.load", "%"),
        }
        definitions = list(cls.defs.values())
        d = cls.defs

        def running(at, coolant):
            return {
                "engine.rpm": available(d["rpm"], 800.0, at),
                "vehicle.speed": available(d["speed"], 0.0, at),
                "engine.coolant_temperature": available(d["coolant"], coolant, at),
                "battery.voltage": available(d["battery"], 14.0, at),
                "tire.pressure.rr": available(d["rr"], 76.0, at),
                "engine.load": available(d["load"], 20.0, at),
            }

        def parked(at, volts):
            return {
                "engine.rpm": available(d["rpm"], 0.0, at),
                "vehicle.speed": available(d["speed"], 0.0, at),
                "engine.coolant_temperature": unavailable(d["coolant"]),
                "battery.voltage": available(d["battery"], volts, at),
                "tire.pressure.rr": unavailable(d["rr"]),
                "engine.load": unavailable(d["load"]),
            }

        cls.old_trip_ids = []
        cls.recent_times = []
        with TelemetryHistorian(cls.source, config=CONFIG) as historian:
            def ingest(at, values, run=True):
                return historian.ingest_snapshot(
                    snapshot(at, definitions, values, running=run), captured_at=at
                )

            def trip(start, count, coolant_at):
                ids = set()
                times = []
                for index in range(count):
                    at = start + timedelta(seconds=5 * index)
                    ids.add(ingest(at, running(at, coolant_at(index))).trip_id)
                    times.append(wr._to_us(at))
                return ids, times

            def burst(start, volts, count=4):
                for index in range(count):
                    at = start + timedelta(seconds=5 * index)
                    ingest(at, parked(at, volts), run=False)

            base = NOW - timedelta(days=10)
            for k in range(3):
                start = base + timedelta(hours=2 * k)
                ids, _times = trip(start, 24, lambda i: 190.0)
                cls.old_trip_ids.extend(ids)
                burst(start + timedelta(seconds=5 * 24 + 20), 12.5)
            burst(NOW - timedelta(days=2), 12.4)
            recent_start = NOW - timedelta(days=1)
            ids, cls.recent_times = trip(recent_start, 36, lambda i: 190.0 if i < 12 else 210.0)
            (cls.recent_trip_id,) = ids
            burst(recent_start + timedelta(seconds=5 * 36 + 20), 12.3)
            burst(recent_start + timedelta(hours=1), 11.6)
            while historian.refresh_rollups(through=NOW)["backlog"]:
                pass
            # Recorded advisory history: one warned vehicle episode with a push,
            # then an old warned tire episode.
            t = cls.recent_times
            for us, state in ((t[3], "watch"), (t[6], "warning"), (t[9], "normal")):
                historian.record_advisory_assessments(
                    [canned(BATTERY_RULE, state, metric="battery.voltage", trip_id=cls.recent_trip_id)],
                    evaluated_at=wr._dt(us),
                )
                for row in historian.pending_advisory_notifications(at=wr._dt(us), limit=100):
                    historian.mark_advisory_notification_delivered(row["id"], delivered_at=wr._dt(us))
            historian.record_advisory_assessments(
                [canned("tire_pressure_rr_relative_low", "warning", metric="tire.pressure.rr")],
                evaluated_at=base + timedelta(minutes=1),
            )
            # Retention: raw rows of the old trips are gone, rollups remain.
            conn = historian._conn
            conn.commit()
            conn.execute("PRAGMA foreign_keys=OFF")
            with conn:
                marks = ",".join("?" for _ in cls.old_trip_ids)
                conn.execute(f"DELETE FROM metric_samples WHERE trip_id IN ({marks})", cls.old_trip_ids)
                conn.execute(f"DELETE FROM snapshots WHERE trip_id IN ({marks})", cls.old_trip_ids)
            conn.execute("PRAGMA foreign_keys=ON")
        cls.export_path = cls.root / "shared" / "export.sqlite3"
        cls.manifest = wr.export(cls.source, cls.export_path, days=7, now=NOW)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def out_dir(self):
        path = Path(tempfile.mkdtemp(dir=self.root))
        return path

    def stub_factory(self):
        return lambda historian: StubEvaluator(historian, self.recent_times, self.recent_trip_id)

    # 1 -----------------------------------------------------------------
    def test_export_is_bounded_read_only_and_complete(self):
        out = self.out_dir() / "export.sqlite3"
        before = sha256(self.source)
        manifest = wr.export(self.source, out, days=7, now=NOW)
        self.assertEqual(before, sha256(self.source))
        self.assertIn("mode=ro", manifest["source_uri"])
        probe = wr.open_readonly(self.source)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                probe.conn.execute("INSERT INTO historian_meta(key,value) VALUES('x','y')")
            with self.assertRaises(sqlite3.OperationalError):
                probe.conn.execute("CREATE TABLE forbidden(x)")
        finally:
            probe.close()
        self.assertEqual(before, sha256(self.source))

        reference = self.root / "reference.sqlite3"
        if not reference.exists():
            TelemetryHistorian(reference).close()
        ref = sqlite3.connect(reference)
        dst = sqlite3.connect(out)
        src = sqlite3.connect(self.source)
        try:
            ref_objects = set(ref.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            dst_objects = set(dst.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            self.assertLessEqual(ref_objects, dst_objects)
            for table in ("replay_recorded_episodes", "replay_recorded_outbox", "replay_manifest"):
                self.assertIn(("table", table), dst_objects)
            self.assertEqual(dst.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertFalse(Path(str(out) + "-wal").exists())
            self.assertEqual(dst.execute("PRAGMA foreign_key_check").fetchall(), [])
            # all trips
            self.assertEqual(dst.execute("SELECT count(*) FROM trips").fetchone()[0], 4)
            # only the recent trip's raw samples, only rule metrics
            trips = {row[0] for row in dst.execute("SELECT DISTINCT trip_id FROM metric_samples")}
            self.assertEqual(trips, {self.recent_trip_id, None})
            marks = ",".join("?" for _ in wr.RULE_METRICS)
            src_recent = src.execute(
                f"SELECT count(*) FROM metric_samples WHERE trip_id=? AND metric IN ({marks})",
                (self.recent_trip_id, *wr.RULE_METRICS),
            ).fetchone()[0]
            dst_recent = dst.execute(
                "SELECT count(*) FROM metric_samples WHERE trip_id=?", (self.recent_trip_id,)
            ).fetchone()[0]
            self.assertEqual(src_recent, dst_recent)
            self.assertGreater(dst_recent, 0)
            self.assertEqual(dst.execute("SELECT count(*) FROM metric_samples WHERE metric='engine.load'").fetchone()[0], 0)
            # all rule-metric rollups
            src_rollups = src.execute(
                f"SELECT count(*) FROM metric_rollups WHERE metric IN ({marks})", wr.RULE_METRICS
            ).fetchone()[0]
            self.assertEqual(dst.execute("SELECT count(*) FROM metric_rollups").fetchone()[0], src_rollups)
            # parked battery bursts inside the window only, with snapshots and RPM rows
            cutoff = manifest["cutoff_us"]
            src_parked = src.execute(
                "SELECT count(*) FROM metric_samples WHERE metric='battery.voltage' AND trip_id IS NULL "
                "AND freshness='fresh' AND captured_us>=?",
                (cutoff,),
            ).fetchone()[0]
            dst_parked = dst.execute(
                "SELECT count(*),min(captured_us) FROM metric_samples WHERE metric='battery.voltage' AND trip_id IS NULL"
            ).fetchone()
            self.assertEqual(dst_parked[0], src_parked)
            self.assertEqual(src_parked, 12)  # three in-window bursts of four
            self.assertGreaterEqual(dst_parked[1], cutoff)
            self.assertEqual(
                dst.execute("SELECT count(*) FROM metric_samples WHERE metric='engine.rpm' AND trip_id IS NULL").fetchone()[0],
                src_parked,
            )
            # comparison tables
            self.assertEqual(
                dst.execute("SELECT count(*) FROM replay_recorded_episodes").fetchone()[0],
                src.execute("SELECT count(*) FROM advisory_episodes").fetchone()[0],
            )
            self.assertEqual(
                dst.execute("SELECT count(*) FROM replay_recorded_outbox").fetchone()[0],
                src.execute("SELECT count(*) FROM advisory_notification_outbox").fetchone()[0],
            )
            warned = dst.execute(
                "SELECT rule_key,first_warning_us,trip_id FROM replay_recorded_episodes WHERE rule_key=?",
                (BATTERY_RULE,),
            ).fetchone()
            self.assertEqual(warned[1], self.recent_times[6])
            self.assertEqual(warned[2], self.recent_trip_id)
            self.assertEqual(dst.execute("SELECT count(*) FROM advisory_episodes").fetchone()[0], 0)
            meta = dict(dst.execute("SELECT key,value FROM historian_meta"))
            self.assertEqual(meta["maintenance_last_status"], "completed")
            self.assertEqual(
                meta["rollup_through_us:60"],
                src.execute("SELECT value FROM historian_meta WHERE key='rollup_through_us:60'").fetchone()[0],
            )
        finally:
            ref.close()
            dst.close()
            src.close()
        for key in ("exported_at", "database", "days", "cutoff_us", "trip_ids", "table_counts", "git_head"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["trip_ids"], [self.recent_trip_id])
        self.assertEqual(wr.read_manifest(out)["trip_ids"], [self.recent_trip_id])
        self.assertLess(manifest["max_statement_seconds"], wr.STATEMENT_DEADLINE_SECONDS)
        self.assertGreater(manifest["statement_count"], 10)
        with self.assertRaises(FileExistsError):
            wr.export(self.source, out, days=7, now=NOW)
        wr.export(self.source, out, days=7, now=NOW, force=True)
        # A statement past its deadline aborts the export and leaves no file.
        late = out.parent / "late.sqlite3"
        with self.assertRaises(wr.DeadlineExceeded):
            wr.export(self.source, late, days=7, now=NOW, deadline_seconds=0.0)
        self.assertFalse(late.exists())
        self.assertFalse(Path(str(late) + ".partial").exists())
        self.assertEqual(before, sha256(self.source))

    # 2 -----------------------------------------------------------------
    def test_replay_counts_delivery_stride_gaps_and_budget(self):
        out = self.out_dir()
        export_hash = sha256(self.export_path)
        result = wr.replay(self.export_path, evaluator_factory=self.stub_factory(), out_dir=out, config=CONFIG)
        self.assertTrue(result["complete"])
        self.assertEqual(export_hash, sha256(self.export_path))
        with sqlite3.connect(self.export_path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM advisory_episodes").fetchone()[0], 0)
        coolant = result["counts"]["rules"][COOLANT_RULE]
        self.assertEqual((coolant["opened"], coolant["warned"], coolant["pushes"]), (1, 1, 1))
        self.assertEqual(coolant["family"], "cooling")
        battery = result["counts"]["rules"][BATTERY_RULE]
        self.assertEqual((battery["opened"], battery["warned"], battery["pushes"]), (1, 0, 0))
        self.assertEqual(battery["family"], "charging")
        self.assertEqual(result["counts"]["trips"][str(self.recent_trip_id)]["cooling"]["warned"], 1)
        self.assertEqual(result["persistence_totals"]["opened"], 2)
        self.assertEqual(result["persistence_totals"]["notifications_enqueued"], 1)
        self.assertEqual(result["simulated_deliveries"], 1)
        with sqlite3.connect(result["copy"]) as conn:
            row = conn.execute("SELECT status,delivered_us FROM advisory_notification_outbox").fetchone()
        self.assertEqual(row, ("delivered", self.recent_times[12]))
        # recorded comparison travels with the summary
        self.assertEqual(result["recorded"]["window"]["rules"][BATTERY_RULE]["warned"], 1)
        self.assertEqual(result["recorded"]["window"]["rules"][BATTERY_RULE]["pushes"], 1)
        self.assertEqual(result["recorded"]["all_time"]["families"]["tires"]["opened"], 1)
        self.assertNotIn("tires", result["recorded"]["window"]["families"])
        self.assertGreaterEqual(result["cpu_seconds"], 0.0)
        self.assertGreater(result["peak_rss_mb"], 0.0)
        saved = json.loads(Path(result["summary_path"]).read_text())
        self.assertTrue(saved["complete"])

        pending = wr.replay(
            self.export_path,
            evaluator_factory=self.stub_factory(),
            out_dir=out,
            config=CONFIG,
            simulated_delivery=False,
        )
        with sqlite3.connect(pending["copy"]) as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM advisory_notification_outbox").fetchall(), [("pending",)]
            )

        halved = wr.replay(
            self.export_path, evaluator_factory=self.stub_factory(), out_dir=out, config=CONFIG, stride=2
        )
        full_trip = result["ticks"]["planned_by_kind"]["trip"]
        self.assertEqual(halved["ticks"]["planned_by_kind"]["trip"], math.ceil(full_trip / 2))
        self.assertEqual(halved["ticks"]["planned_by_kind"]["parked"], result["ticks"]["planned_by_kind"]["parked"])

        conn = sqlite3.connect(self.export_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            timeline = wr.build_timeline(conn, gap_tick_hours=6.0, end_us=wr._to_us(NOW))
        finally:
            conn.close()
        gaps = [us for us, kind, _trip in timeline if kind == "gap"]
        self.assertTrue(gaps)
        previous = {us for us, _k, _t in timeline}
        for us in gaps:
            before = max(t for t in previous if t < us)
            self.assertLessEqual(us - before, 6 * wr.HOUR_US)
        last_burst = max(us for us, kind, _t in timeline if kind == "parked")
        after = [us for us in gaps if us > last_burst]
        self.assertEqual(after, [last_burst + k * 6 * wr.HOUR_US for k in range(1, len(after) + 1)])
        self.assertEqual(len(after), 3)  # 23 h to the export time
        self.assertEqual(timeline, sorted(timeline))

        aborted = wr.replay(
            self.export_path, evaluator_factory=self.stub_factory(), out_dir=out, config=CONFIG, cpu_budget=0.0001
        )
        self.assertFalse(aborted["complete"])
        self.assertIn("CPU", aborted["abort_reason"])
        self.assertFalse(json.loads(Path(aborted["summary_path"]).read_text())["complete"])

    def test_resumed_chunks_equal_one_continuous_pass(self):
        out = self.out_dir()
        export_hash = sha256(self.export_path)
        whole = wr.replay(self.export_path, evaluator_factory=self.stub_factory(), out_dir=out, config=CONFIG)
        first = wr.replay(
            self.export_path, evaluator_factory=self.stub_factory(), out_dir=out, config=CONFIG, max_ticks=15
        )
        self.assertFalse(first["complete"])
        self.assertIn("tick limit", first["abort_reason"])
        self.assertEqual(first["progress"]["next_index"], 15)
        second = wr.replay(
            evaluator_factory=self.stub_factory(), config=CONFIG, resume=first["summary_path"], max_ticks=10
        )
        self.assertFalse(second["complete"])
        self.assertEqual(second["progress"]["next_index"], 25)
        final = wr.replay(evaluator_factory=self.stub_factory(), config=CONFIG, resume=second["summary_path"])
        self.assertTrue(final["complete"])
        self.assertEqual(final["copy"], first["copy"])
        self.assertEqual(final["summary_path"], first["summary_path"])
        self.assertEqual([c["ticks"] for c in final["chunks"]], [15, 10, whole["ticks"]["planned"] - 25])
        self.assertEqual(final["ticks"]["evaluated"], whole["ticks"]["evaluated"])
        self.assertEqual(final["persistence_totals"], whole["persistence_totals"])
        self.assertEqual(final["simulated_deliveries"], whole["simulated_deliveries"])
        self.assertEqual(final["counts"]["rules"], whole["counts"]["rules"])
        self.assertAlmostEqual(
            final["cpu_seconds_total"], sum(c["cpu_seconds"] for c in final["chunks"]), places=2
        )
        self.assertEqual(final["query_verification"]["chunks_counted"], 3)
        self.assertEqual(
            final["query_verification"]["calls"],
            sum(c["query_verification"]["calls"] for c in final["chunks"]),
        )
        self.assertEqual(final["query_verification"]["mismatches"], 0)

        def tables(path):
            with sqlite3.connect(path) as conn:
                return (
                    conn.execute(
                        "SELECT rule_key,status,current_state,opened_us,resolved_us,resolution_reason "
                        "FROM advisory_episodes ORDER BY id"
                    ).fetchall(),
                    conn.execute(
                        "SELECT episode_id,event_us,event_type,new_state FROM advisory_episode_events ORDER BY id"
                    ).fetchall(),
                    conn.execute(
                        "SELECT rule_key,status,created_us,delivered_us FROM advisory_notification_outbox ORDER BY id"
                    ).fetchall(),
                    conn.execute("SELECT * FROM trips ORDER BY id").fetchall(),
                )

        self.assertEqual(tables(final["copy"]), tables(whole["copy"]))
        self.assertEqual(export_hash, sha256(self.export_path))
        with self.assertRaises(ValueError):
            wr.replay(evaluator_factory=self.stub_factory(), config=CONFIG, resume=final["summary_path"])

    def test_replay_real_evaluator_uses_memo_and_opens_in_copy_only(self):
        from projects.vehicle_data.early_warning import EarlyWarningEvaluator, WarningRule

        rule = WarningRule(
            key=COOLANT_RULE,
            title="Coolant above its usual range",
            metric="engine.coolant_temperature",
            direction="high",
            minimum_effect=5.0,
            minimum_baseline_buckets=3,
            minimum_baseline_trips=1,
            persistence_observations=2,
            persistence_window_seconds=60.0,
        )
        out = self.out_dir()
        result = wr.replay(
            self.export_path,
            evaluator_factory=lambda historian: EarlyWarningEvaluator(historian, (rule,)),
            out_dir=out,
            config=CONFIG,
        )
        self.assertTrue(result["complete"])
        counts = result["counts"]["rules"][COOLANT_RULE]
        self.assertGreaterEqual(counts["opened"], 1)
        self.assertGreaterEqual(counts["warned"], 1)
        self.assertGreater(result["baseline_memo"]["hits"], 0)
        with sqlite3.connect(self.export_path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM advisory_episodes").fetchone()[0], 0)

    def test_budget_abort_exits_three_with_partial_report(self):
        if shutil.which("git") is None or wr._git("rev-parse", "HEAD") is None:
            self.skipTest("git unavailable")
        out = self.out_dir()
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            code = wr.main(
                [
                    "--out-dir", str(out), "replay", "--evaluator", "current",
                    "--export", str(self.export_path), "--cpu-budget", "0.0001",
                ]
            )
        self.assertIn("cpu_seconds=", sink.getvalue())
        self.assertEqual(code, wr.EXIT_INCOMPLETE)
        summaries = sorted(out.glob("replay-current-*.json"))
        self.assertEqual(len(summaries), 1)
        self.assertFalse(json.loads(summaries[0].read_text())["complete"])
        reports = sorted(out.glob("report-*.json"))
        self.assertEqual(len(reports), 1)
        self.assertFalse(json.loads(reports[0].read_text())["complete"])

    def test_baseline_memo_refreshes_when_window_edge_passes_a_bucket(self):
        out = self.out_dir()
        copy = out / "memo.sqlite3"
        shutil.copyfile(self.export_path, copy)
        historian = wr.ReplayHistorian(copy, config=CONFIG)
        shutil.copyfile(self.export_path, out / "memo-ref.sqlite3")
        reference = TelemetryHistorian(out / "memo-ref.sqlite3", config=CONFIG)
        try:
            historian.advance_trips(self.recent_times[-1])
            row = historian._conn.execute(
                "SELECT unit,quality,source,provenance FROM metric_rollups "
                "WHERE metric='engine.coolant_temperature' LIMIT 1"
            ).fetchone()
            kwargs = {
                "lookback_days": 9,
                "exclude_trip_id": self.recent_trip_id,
                "unit": row[0],
                "quality": row[1],
                "source": row[2],
                "provenance": row[3],
                "regime_dimensions": ("engine", "motion", "rpm"),
            }
            regime = "engine_running:stationary:rpm_idle:warm"
            # The old trips start 10 days before NOW, 2 h apart; with a 9-day
            # lookback the lower edge crosses them 1 day before NOW.
            start = NOW - timedelta(days=1, minutes=1)
            results = []
            for minutes in (0, 30, 90, 150, 200, 260, 300):
                at = start + timedelta(minutes=minutes)
                memo = historian.robust_baseline("engine.coolant_temperature", regime, before=at, **kwargs)
                direct = reference.robust_baseline("engine.coolant_temperature", regime, before=at, **kwargs)
                if direct is None:
                    self.assertIsNone(memo)
                else:
                    self.assertEqual(
                        (memo.bucket_count, memo.trip_count, memo.median, memo.input_digest),
                        (direct.bucket_count, direct.trip_count, direct.median, direct.input_digest),
                        minutes,
                    )
                results.append(None if direct is None else direct.trip_count)
            self.assertEqual(results[0], 3)
            self.assertIn(2, results)
            self.assertIn(1, results)
            self.assertGreaterEqual(historian.baseline_edge_refreshes, 2)
            self.assertGreater(historian.baseline_hits, 0)
        finally:
            historian.close()
            reference.close()

    def test_evaluator_memo_is_edge_aware(self):
        from projects.vehicle_data.historian import BaselineStats

        memo = wr.EdgeAwareMemo()
        now = int(NOW.timestamp() * 1_000_000)
        earliest = now - 30 * wr.DAY_US + 10 * wr.US
        # Key layout of EarlyWarningEvaluator._memo_key: lookback_days at index 7.
        key = ("m", "r", "V", "q", "s", "p", ("engine",), 30, 0.0, None, None, None, 7)
        value = BaselineStats(
            metric="m", regime="r", unit="V", quality="q", source="s", provenance="p",
            bucket_count=1, trip_count=1, sample_count=1, median=1.0, mad=0.0,
            minimum=1.0, maximum=1.0, first_at=None, last_at=None, input_digest="d",
            input_buckets=({"bucket_us": earliest, "trip_key": 1, "regime": "r", "median": 1.0},),
        )
        memo.now_us = now
        memo[key] = value
        self.assertIn(key, memo)
        memo.now_us = now + 10 * wr.US  # lower edge reaches the earliest bucket: still inside
        self.assertIn(key, memo)
        memo.now_us = now + 11 * wr.US  # earliest bucket has left the window
        self.assertNotIn(key, memo)
        self.assertEqual(memo.edge_refreshes, 1)
        # Unknown earliest bucket: kept for the hour it was stored in only.
        hour_key = key[:-1] + ("hour",)
        memo.now_us = (now // wr.HOUR_US) * wr.HOUR_US
        memo[hour_key] = object()
        memo.now_us += wr.HOUR_US - 1
        self.assertIn(hour_key, memo)
        memo.now_us += 1
        self.assertNotIn(hour_key, memo)
        memo.clear()
        self.assertEqual(len(memo), 0)

    def test_in_memory_recent_samples_match_sql(self):
        out = self.out_dir()
        copy = out / "copy.sqlite3"
        shutil.copyfile(self.export_path, copy)
        historian = wr.ReplayHistorian(copy, config=CONFIG)
        try:
            historian.verify_every = 1
            conn = historian._conn
            rows = conn.execute(
                "SELECT DISTINCT metric,quality,source,provenance,trip_id FROM metric_samples"
            ).fetchall()
            times = [us for (us,) in conn.execute("SELECT captured_us FROM snapshots ORDER BY captured_us")]
            regimes = [r for (r,) in conn.execute("SELECT DISTINCT regime FROM metric_samples")]
            calls = 0
            for metric, quality, source, provenance, trip_id in rows:
                for us in times[::3] + [times[-1] + 10 * wr.US]:
                    for regime in regimes:
                        for dims, limit in ((wr.REGIME_DIMENSIONS, 10), (("motion",), 3), (("engine",), 60)):
                            historian.recent_numeric_samples(
                                metric, regime=regime, trip_id=trip_id, at=wr._dt(us), limit=limit,
                                quality=quality, source=source, provenance=provenance, regime_dimensions=dims,
                            )
                            calls += 1
            self.assertGreater(calls, 100)
            self.assertEqual(historian.query_checks["checked"], calls)
            self.assertEqual(historian.query_checks["mismatches"], 0, historian.query_checks["first_mismatch"])
        finally:
            historian.close()

    # 3 -----------------------------------------------------------------
    def test_load_current_evaluator_from_git(self):
        if shutil.which("git") is None or wr._git("rev-parse", "HEAD") is None:
            self.skipTest("git unavailable")
        out = self.out_dir()
        module, info = wr.load_current_evaluator("HEAD", out_dir=out)
        self.assertTrue(hasattr(module, "EarlyWarningEvaluator"))
        self.assertEqual(info["blob"], wr._git("rev-parse", f"HEAD:{wr.EVALUATOR_PATH}"))
        self.assertEqual(module.__name__, "early_warning_replay_current")
        self.assertTrue(Path(info["module_path"]).is_file())

    # 4 -----------------------------------------------------------------
    def test_coarse_counts_tier0_conditions_on_rollups(self):
        out = self.out_dir()
        path = out / "coarse.sqlite3"
        TelemetryHistorian(path).close()
        t1 = wr._to_us(NOW - timedelta(days=3))
        t2 = wr._to_us(NOW - timedelta(days=2))
        minute = 60 * wr.US
        rows = []

        def rollup(bucket, trip, metric, regime, minimum, maximum, median):
            rows.append(
                (bucket, 60, trip, metric, regime, "u", "src", "verified", "prov", 12,
                 minimum, maximum, median, median, 0.0, bucket, bucket + 55 * wr.US)
            )

        running = "engine_running:highway:rpm_low:warm"
        rollup(t1 + 10 * minute, 1, "engine.coolant_temperature", running, 200.0, 232.0, 210.0)
        rollup(t2 + 10 * minute, 2, "engine.coolant_temperature", running, 200.0, 226.0, 210.0)
        rollup(t1 + 120 * minute, 0, "battery.voltage", "engine_off:stationary:rpm_off:warm", 11.6, 12.0, 11.8)
        for trip, start, run_length in ((1, t1, 4), (2, t2, 5)):
            for index in range(30):
                bucket = start + (20 + index) * minute
                rollup(bucket, trip, "tire.pressure.rl", running, 76.8, 76.8, 76.8)
                rollup(bucket, trip, "tire.pressure.rr", running, 76.0, 76.0, 76.0)
            for index in range(run_length):
                bucket = start + (60 + index) * minute
                rollup(bucket, trip, "tire.pressure.rl", running, 81.0, 81.0, 81.0)
                rollup(bucket, trip, "tire.pressure.rr", running, 76.0, 76.0, 76.0)
        with sqlite3.connect(path) as conn:
            conn.executemany(
                "INSERT INTO trips(id,started_us,started_at,last_active_us,last_active_at,ended_us,ended_at,start_basis,end_reason) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (1, t1, wr._iso(t1), t1 + 90 * minute, wr._iso(t1 + 90 * minute), t1 + 90 * minute, wr._iso(t1 + 90 * minute), "test", "test"),
                    (2, t2, wr._iso(t2), t2 + 90 * minute, wr._iso(t2 + 90 * minute), t2 + 90 * minute, wr._iso(t2 + 90 * minute), "test", "test"),
                ],
            )
            conn.executemany(
                "INSERT INTO metric_rollups(bucket_us,bucket_seconds,trip_key,metric,regime,unit,source,quality,provenance,"
                "sample_count,minimum,maximum,mean,median,mad,first_us,last_us) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        result = wr.coarse(path, out_dir=out, now_us=wr._to_us(NOW), write=False)
        by_trip = {entry["trip"]: entry for entry in result["trips"]}
        self.assertEqual(result["rollup_only_trip_ids"], [1, 2])
        hot = by_trip[1]["hits"]["engine_coolant_temperature_hot"]
        self.assertEqual((hot["buckets"], hot["hits"], hot["critical_buckets"]), (1, 1, 0))
        self.assertNotIn("engine_coolant_temperature_hot", by_trip[2]["hits"])
        self.assertEqual(by_trip[2]["near_miss"].get("coolant_ge_225"), 1)
        parked = by_trip["parked"]["hits"]["battery_voltage_low_parked"]
        self.assertEqual((parked["buckets"], parked["hits"], parked["critical_buckets"]), (1, 1, 0))
        self.assertEqual(len(result["parked_battery_events"]), 1)
        self.assertEqual(result["parked_battery_events"][0]["after_trip"], 1)
        self.assertAlmostEqual(result["pair_offsets"]["rear"]["offset_psi"], 0.8, places=3)
        rear1 = by_trip[1]["hits"]["tire_pressure_pair_asymmetry:rear"]
        self.assertEqual((rear1["runs"], rear1["longest_run"], rear1["hits"]), (1, 4, 0))
        rear2 = by_trip[2]["hits"]["tire_pressure_pair_asymmetry:rear"]
        self.assertEqual((rear2["runs"], rear2["longest_run"], rear2["hits"]), (1, 5, 1))
        self.assertEqual(result["totals"]["rollup_only"]["tire_pressure_pair_asymmetry:rear"]["hits"], 1)

    # 5 -----------------------------------------------------------------
    def test_report_has_family_table_and_acceptance_lines(self):
        out = self.out_dir()
        current = wr.replay(self.export_path, evaluator_factory=self.stub_factory(), out_dir=out, config=CONFIG)
        coarse = wr.coarse(self.export_path, out_dir=out)
        result = wr.report(out, current=current, coarse_result=coarse)
        markdown = Path(result["report_path"]).read_text()
        self.assertIn("| family | recorded window | recorded all time | replay current | replay new |", markdown)
        self.assertIn("| cooling |", markdown)
        for key in wr.ACCEPTANCE_KEYS:
            self.assertIn(f"- {key}: **", markdown)
            self.assertEqual(result["acceptance"][key]["status"], "not_evaluable")
        self.assertTrue(result["complete"])

        new = {
            "counts": {
                "episodes": [
                    {"rule": "battery_voltage_low_parked", "tier": 0, "warned": True, "family": "battery", "trip": None},
                    {"rule": COOLANT_RULE, "tier": 1, "warned": True, "family": "cooling", "trip": 5},
                    {"rule": COOLANT_RULE, "tier": 1, "warned": True, "family": "cooling", "trip": 5},
                ]
            },
            "tier2": {
                "status": "ok",
                "assessments": [
                    {"rule": "tire_pressure_rr_slow_leak", "state": "warning", "current": {"slope_psi_per_week": -0.6}}
                ],
            },
        }
        checks = wr.acceptance(new, {"totals": {"rollup_only": {}}})
        self.assertEqual(checks["tier0_fires_only_parked_battery"]["status"], "pass")
        self.assertEqual(checks["tier0_fires_only_parked_battery"]["parked_battery_warnings"], 1)
        self.assertEqual(checks["tier1_le_1_warning_per_family_per_trip"]["status"], "fail")
        self.assertEqual(checks["rr_slow_leak_notice"]["status"], "pass")
        failing = wr.acceptance(
            new, {"totals": {"rollup_only": {"engine_coolant_temperature_hot": {"hits": 2}}}}
        )
        self.assertEqual(failing["tier0_fires_only_parked_battery"]["status"], "fail")
        # A replay in which nothing opened is evaluable (and passes) rather
        # than being mistaken for an evaluator without tiers.
        quiet = wr.acceptance({"counts": {"episodes": []}, "tier2": {"status": "not_run"}},
                              {"totals": {"rollup_only": {}}})
        self.assertEqual(quiet["tier0_fires_only_parked_battery"]["status"], "pass")
        self.assertEqual(quiet["tier1_le_1_warning_per_family_per_trip"]["status"], "pass")
        self.assertEqual(quiet["tier1_le_1_warning_per_family_per_trip"]["max_warnings_per_trip_family"], 0)

    def test_family_mapping(self):
        self.assertEqual(wr.family_of("tire_pressure_rr_relative_low"), "tires")
        self.assertEqual(wr.family_of("battery_voltage_low_parked"), "battery")
        self.assertEqual(wr.family_of("battery_voltage_charge_acceptance"), "charging")
        self.assertEqual(wr.family_of("usb_can_transient_disconnect"), "system")
        self.assertEqual(wr.family_of("telemetry_gap_engine_oil_pressure"), "system")
        self.assertEqual(wr.family_of("custom_x"), "custom")
        self.assertEqual(wr.family_of("anything", "oil"), "oil")


if __name__ == "__main__":
    unittest.main()
