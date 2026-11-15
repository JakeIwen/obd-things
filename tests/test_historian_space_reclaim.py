"""Space reclamation: incremental auto-vacuum, post-prune vacuum, one-time compaction."""

import hashlib
import subprocess
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.vehicle_data.historian import HistorianConfig, TelemetryHistorian
from projects.vehicle_data.history import compaction, rollups
from projects.vehicle_data.history.store import WAL_SIZE_LIMIT_BYTES
from tests.test_vehicle_historian import available, definition, snapshot

UTC = timezone.utc
START = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def pragma(path, name):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        conn.close()


class SpaceFixture(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.dir = Path(tempdir.name)
        self.path = self.dir / "history.sqlite3"
        self.rpm = definition("engine.rpm", "rpm")
        self.oil = definition("engine.oil_pressure", "psi")
        self.definitions = [self.rpm, self.oil]

    def fill(self, historian, count=400, *, running=True):
        for i in range(count):
            at = START + timedelta(seconds=5 * i)
            payload = snapshot(
                at,
                self.definitions,
                {
                    "engine.rpm": available(self.rpm, 800.0 + i, at),
                    "engine.oil_pressure": available(self.oil, 30.0 + i % 7, at),
                },
                running=running,
            )
            historian.ingest_snapshot(payload, captured_at=at)

    def legacy_database(self):
        """A database created before auto_vacuum was set (mode NONE)."""
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA auto_vacuum = NONE")
        conn.execute("CREATE TABLE seed(x)")
        conn.commit()
        conn.close()


class IncrementalVacuumTests(SpaceFixture):
    def test_new_database_is_incremental_with_wal_size_limit(self):
        with TelemetryHistorian(self.path) as historian:
            conn = historian._conn
            self.assertEqual(conn.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
            self.assertEqual(
                conn.execute("PRAGMA journal_size_limit").fetchone()[0],
                WAL_SIZE_LIMIT_BYTES,
            )
        self.assertEqual(pragma(self.path, "auto_vacuum"), 2)

    def test_retention_pass_returns_freed_pages(self):
        config = HistorianConfig(raw_retention_days=1)
        with TelemetryHistorian(self.path, config=config) as historian:
            self.fill(historian)
            result = historian.run_maintenance(now=START + timedelta(days=10), force=True)
            self.assertGreater(result.deleted_metric_samples, 0)
            conn = historian._conn
            self.assertEqual(conn.execute("PRAGMA freelist_count").fetchone()[0], 0)
            self.assertGreater(historian.last_space_reclaim["reclaimed_pages"], 0)
            self.assertIn("checkpoint", historian.last_space_reclaim)

    def test_legacy_database_prune_still_succeeds_without_vacuum(self):
        self.legacy_database()
        config = HistorianConfig(raw_retention_days=1)
        with TelemetryHistorian(self.path, config=config) as historian:
            self.assertEqual(historian._conn.execute("PRAGMA auto_vacuum").fetchone()[0], 0)
            self.fill(historian)
            result = historian.run_maintenance(now=START + timedelta(days=10), force=True)
            self.assertGreater(result.deleted_metric_samples, 0)
            self.assertEqual(historian.last_space_reclaim["reclaimed_pages"], 0)
            self.assertGreater(historian._conn.execute("PRAGMA freelist_count").fetchone()[0], 0)

    def big_freelist(self, historian, rows=6_000):
        conn = historian._conn
        conn.execute("CREATE TABLE junk(x BLOB)")
        conn.executemany("INSERT INTO junk VALUES(?)", [(b"x" * 3_000,)] * rows)
        conn.commit()
        conn.execute("DROP TABLE junk")
        conn.commit()
        return conn.execute("PRAGMA freelist_count").fetchone()[0]

    def patch_budget(self, seconds):
        original = rollups.RECLAIM_TIME_BUDGET_SECONDS
        rollups.RECLAIM_TIME_BUDGET_SECONDS = seconds
        self.addCleanup(setattr, rollups, "RECLAIM_TIME_BUDGET_SECONDS", original)

    def test_one_pass_is_bounded_by_pages(self):
        self.patch_budget(600.0)  # keep slow storage from ending the pass early
        with TelemetryHistorian(self.path) as historian:
            free = self.big_freelist(historian)
            self.assertGreater(free, rollups.RECLAIM_MAX_PAGES_PER_PASS)
            info = historian.reclaim_space()
            # Exactly the page budget, not overshooting by a chunk.
            self.assertEqual(info["reclaimed_pages"], rollups.RECLAIM_MAX_PAGES_PER_PASS)
            left = historian._conn.execute("PRAGMA freelist_count").fetchone()[0]
            self.assertEqual(left, free - rollups.RECLAIM_MAX_PAGES_PER_PASS)
            # A later pass (the next hourly check) continues the work.
            historian.reclaim_space()
            self.assertLess(historian._conn.execute("PRAGMA freelist_count").fetchone()[0], left)

    def test_one_pass_returns_within_the_wall_time_budget(self):
        self.patch_budget(0.05)
        with TelemetryHistorian(self.path) as historian:
            free = self.big_freelist(historian)
            started = time.monotonic()
            info = historian.reclaim_space()
            elapsed = time.monotonic() - started
            # Ends after the budget plus at most one chunk and the checkpoint,
            # long before the whole freelist (which would exceed the page cap).
            self.assertLess(elapsed, 3.0)
            self.assertLess(info["reclaimed_pages"], rollups.RECLAIM_MAX_PAGES_PER_PASS)
            self.assertGreater(historian._conn.execute("PRAGMA freelist_count").fetchone()[0], free - rollups.RECLAIM_MAX_PAGES_PER_PASS)

    def test_zero_time_budget_reclaims_nothing_and_returns(self):
        with TelemetryHistorian(self.path) as historian:
            free = self.big_freelist(historian, rows=500)
            original = rollups.RECLAIM_TIME_BUDGET_SECONDS
            rollups.RECLAIM_TIME_BUDGET_SECONDS = 0.0
            self.addCleanup(setattr, rollups, "RECLAIM_TIME_BUDGET_SECONDS", original)
            self.assertEqual(historian.reclaim_space()["reclaimed_pages"], 0)
            self.assertEqual(historian._conn.execute("PRAGMA freelist_count").fetchone()[0], free)

    def test_hourly_check_with_nothing_due_still_drains_the_freelist(self):
        with TelemetryHistorian(self.path) as historian:
            free = self.big_freelist(historian, rows=500)
            historian.run_maintenance(now=START)  # not due, nothing deleted
            self.assertLess(historian._conn.execute("PRAGMA freelist_count").fetchone()[0], free)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class CompactionTests(SpaceFixture):
    def prepared_legacy_database(self, *, running=False):
        self.legacy_database()
        config = HistorianConfig(raw_retention_days=1)
        with TelemetryHistorian(self.path, config=config) as historian:
            self.fill(historian, running=running)
            historian.run_maintenance(now=START + timedelta(days=10), force=True)
            if running:  # the newest row, kept past the prune
                at = START + timedelta(days=10)
                historian.ingest_snapshot(
                    snapshot(at, self.definitions, {
                        "engine.rpm": available(self.rpm, 900.0, at),
                        "engine.oil_pressure": available(self.oil, 30.0, at),
                    }),
                    captured_at=at,
                )

    def run_compact(self, **overrides):
        options = dict(
            unit_state=lambda unit: "inactive",
            holders=lambda path: ([], []),
            log=lambda message: None,
        )
        options.update(overrides)
        return compaction.compact(self.path, **options)

    def sidecars(self):
        return {p.name for p in self.dir.iterdir()} - {self.path.name}

    def test_plan_is_read_only_and_says_so(self):
        self.prepared_legacy_database()
        wal = self.path.with_name(self.path.name + "-wal")
        before = digest(self.path)
        messages = []
        result = self.run_compact(log=messages.append)
        self.assertFalse(result["applied"])
        self.assertTrue(any("read-only, no checkpoint" in m for m in messages))
        # The main file is byte-identical; SQLite may create empty -wal/-shm
        # for the read-only handle but no frames were written or checkpointed.
        self.assertEqual(digest(self.path), before)
        self.assertLessEqual(self.sidecars(), {wal.name, self.path.name + "-shm"})
        self.assertEqual(wal.stat().st_size if wal.exists() else 0, 0)
        self.assertEqual(pragma(self.path, "auto_vacuum"), 0)

    def test_apply_switches_mode_shrinks_and_keeps_every_row(self):
        self.prepared_legacy_database()
        self.assertGreater(pragma(self.path, "freelist_count"), 0)
        conn = sqlite3.connect(self.path)
        counts = compaction.table_counts(conn)
        conn.close()
        self.path.with_name(self.path.name + "-wal")  # may hold unflushed frames
        size_before = self.path.stat().st_size

        result = self.run_compact(apply=True)

        self.assertTrue(result["applied"])
        self.assertEqual(pragma(self.path, "auto_vacuum"), 2)
        self.assertEqual(pragma(self.path, "freelist_count"), 0)
        self.assertEqual(pragma(self.path, "integrity_check"), "ok")
        self.assertLess(self.path.stat().st_size, size_before)
        conn = sqlite3.connect(self.path)
        self.assertEqual(compaction.table_counts(conn), counts)
        conn.close()
        self.assertLessEqual(self.sidecars(), {self.path.name + "-wal", self.path.name + "-shm"})
        with TelemetryHistorian(self.path) as historian:
            self.assertEqual(historian._conn.execute("PRAGMA auto_vacuum").fetchone()[0], 2)

    def test_writer_holding_the_lock_makes_it_refuse_and_loses_no_commit(self):
        self.prepared_legacy_database()
        writer = sqlite3.connect(self.path, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO seed VALUES('kept')")
        with self.assertRaises(sqlite3.OperationalError):
            self.run_compact(apply=True)
        self.assertEqual(compaction.main(["--db", str(self.path), "--apply"]), 2)
        writer.execute("COMMIT")
        writer.close()
        self.assertEqual(pragma(self.path, "auto_vacuum"), 0)
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("SELECT x FROM seed").fetchall(), [("kept",)])
        conn.close()

    def test_refuses_while_a_unit_is_not_inactive(self):
        self.prepared_legacy_database()
        for state in ("active", "activating", "maintenance", "no-answer(rc=4)"):
            with self.assertRaisesRegex(compaction.CompactionRefused, "not inactive"):
                self.run_compact(
                    apply=True,
                    unit_state=lambda unit, state=state: state if unit == "van-cruise-harvest.timer" else "inactive",
                )
        self.assertEqual(pragma(self.path, "auto_vacuum"), 0)

    def test_systemctl_failures_fail_closed(self):
        original = compaction.subprocess.run

        def broken(*args, **kwargs):
            raise FileNotFoundError("systemctl")

        compaction.subprocess.run = broken
        self.addCleanup(setattr, compaction.subprocess, "run", original)
        with self.assertRaisesRegex(compaction.CompactionRefused, "cannot ask systemd"):
            compaction.systemd_unit_state("van-telemetry.service")

        class Result:
            stdout = ""
            returncode = 4

        compaction.subprocess.run = lambda *a, **k: Result()
        self.assertTrue(compaction.systemd_unit_state("x").startswith("no-answer"))

    def test_refuses_when_a_process_holds_the_file(self):
        self.prepared_legacy_database()
        with self.assertRaisesRegex(compaction.CompactionRefused, "open"):
            self.run_compact(apply=True, holders=lambda path: ([4242], []))

    def test_scan_finds_a_real_holder(self):
        self.prepared_legacy_database()
        child = subprocess.Popen(
            [sys.executable, "-c", f"import time; f=open({str(self.path)!r},'rb'); print('r', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE,
        )
        self.addCleanup(child.kill)
        child.stdout.readline()
        holders, _ = compaction.processes_holding(self.path)
        self.assertIn(child.pid, holders)

    def test_unreadable_descriptors_of_our_own_processes_refuse_others_are_noted(self):
        self.prepared_legacy_database()
        with self.assertRaisesRegex(compaction.CompactionRefused, "cannot inspect"):
            self.run_compact(apply=True, holders=lambda path: ([], [31337]))
        messages = []
        self.run_compact(holders=lambda path: ([], [-1]), log=messages.append)
        self.assertTrue(any("unreadable without sudo" in m for m in messages))

    def test_refuses_when_last_recorded_state_says_the_van_is_running(self):
        self.prepared_legacy_database(running=True)
        with self.assertRaisesRegex(compaction.CompactionRefused, "may be driving"):
            self.run_compact(apply=True, now_state=lambda path: ("running", 60.0))
        # Real lookup: the newest snapshot says running; it is long past the window.
        self.assertEqual(compaction.last_vehicle_state(self.path)[0], "running")
        self.assertGreater(compaction.last_vehicle_state(self.path)[1], compaction.VEHICLE_STATE_MAX_AGE_SECONDS)
        self.assertTrue(self.run_compact(apply=True)["applied"])

    def test_refuses_without_room_for_vacuum(self):
        self.prepared_legacy_database()
        original = compaction.shutil.disk_usage
        compaction.shutil.disk_usage = lambda path: type("U", (), {"free": 1024})()
        self.addCleanup(setattr, compaction.shutil, "disk_usage", original)
        for apply in (False, True):
            with self.assertRaisesRegex(compaction.CompactionRefused, "free"):
                self.run_compact(apply=apply)
        self.assertEqual(pragma(self.path, "auto_vacuum"), 0)

    def test_row_count_mismatch_is_reported(self):
        self.prepared_legacy_database()
        original = compaction.table_counts
        calls = []

        def drifting(conn):
            counts = original(conn)
            if calls:
                counts["seed"] += 1
            calls.append(1)
            return counts

        compaction.table_counts = drifting
        self.addCleanup(setattr, compaction, "table_counts", original)
        with self.assertRaisesRegex(compaction.CompactionRefused, "row counts differ"):
            self.run_compact(apply=True)

    def test_cli_returns_2_when_refused(self):
        self.prepared_legacy_database()
        original = compaction.systemd_unit_state
        compaction.systemd_unit_state = lambda unit: "active"
        self.addCleanup(setattr, compaction, "systemd_unit_state", original)
        self.assertEqual(compaction.main(["--db", str(self.path), "--apply"]), 2)


if __name__ == "__main__":
    unittest.main()
