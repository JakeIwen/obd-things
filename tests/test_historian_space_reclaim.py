"""Space reclamation: incremental auto-vacuum, post-prune vacuum, one-time compaction."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.vehicle_data.historian import HistorianConfig, TelemetryHistorian
from projects.vehicle_data.history import compaction
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
            holders=lambda path: [],
            log=lambda message: None,
        )
        options.update(overrides)
        return compaction.compact(self.path, **options)

    def test_plan_only_changes_nothing(self):
        self.prepared_legacy_database()
        size = self.path.stat().st_size
        result = self.run_compact()
        self.assertFalse(result["applied"])
        self.assertEqual(self.path.stat().st_size, size)
        self.assertEqual(pragma(self.path, "auto_vacuum"), 0)
        self.assertEqual([p.name for p in self.dir.iterdir() if "compact" in p.name], [])

    def test_apply_switches_mode_shrinks_and_keeps_every_row(self):
        self.prepared_legacy_database()
        free_before = pragma(self.path, "freelist_count")
        self.assertGreater(free_before, 0)
        size_before = self.path.stat().st_size
        conn = sqlite3.connect(self.path)
        counts = compaction.table_counts(conn)
        conn.close()

        result = self.run_compact(apply=True)

        self.assertTrue(result["applied"])
        self.assertEqual(pragma(self.path, "auto_vacuum"), 2)
        self.assertEqual(pragma(self.path, "freelist_count"), 0)
        self.assertEqual(pragma(self.path, "integrity_check"), "ok")
        self.assertLess(self.path.stat().st_size, size_before)
        conn = sqlite3.connect(self.path)
        self.assertEqual(compaction.table_counts(conn), counts)
        conn.close()
        backup = self.path.with_name(self.path.name + compaction.BACKUP_SUFFIX)
        self.assertEqual(backup.stat().st_size, size_before)
        self.assertFalse(self.path.with_name(self.path.name + "-wal").exists())
        # The broker can reopen it as usual and keeps the new mode.
        with TelemetryHistorian(self.path) as historian:
            self.assertEqual(historian._conn.execute("PRAGMA auto_vacuum").fetchone()[0], 2)

    def test_delete_old_removes_the_kept_copy(self):
        self.prepared_legacy_database()
        self.run_compact(apply=True, delete_old=True)
        self.assertFalse(self.path.with_name(self.path.name + compaction.BACKUP_SUFFIX).exists())

    def test_refuses_while_a_unit_is_active(self):
        self.prepared_legacy_database()
        with self.assertRaisesRegex(compaction.CompactionRefused, "van-telemetry.service=active"):
            self.run_compact(
                apply=True,
                unit_state=lambda unit: "active" if unit == "van-telemetry.service" else "inactive",
            )
        self.assertEqual(pragma(self.path, "auto_vacuum"), 0)

    def test_refuses_while_another_process_holds_the_file(self):
        self.prepared_legacy_database()
        with self.assertRaisesRegex(compaction.CompactionRefused, "open"):
            self.run_compact(apply=True, holders=lambda path: [4242])

    def test_refuses_when_last_recorded_state_says_the_van_is_running(self):
        self.prepared_legacy_database(running=True)
        with self.assertRaisesRegex(compaction.CompactionRefused, "may be driving"):
            self.run_compact(apply=True, now_state=lambda path: ("running", 60.0))
        # Real lookup: the newest snapshot says running; it is long past the window.
        self.assertEqual(compaction.last_vehicle_state(self.path)[0], "running")
        self.assertGreater(compaction.last_vehicle_state(self.path)[1], compaction.VEHICLE_STATE_MAX_AGE_SECONDS)
        self.assertTrue(self.run_compact(apply=True)["applied"])

    def test_refuses_without_room_for_the_compacted_copy(self):
        self.prepared_legacy_database()
        original = compaction.shutil.disk_usage
        compaction.shutil.disk_usage = lambda path: type("U", (), {"free": 1024})()
        self.addCleanup(setattr, compaction.shutil, "disk_usage", original)
        with self.assertRaisesRegex(compaction.CompactionRefused, "free"):
            self.run_compact(apply=True)
        self.assertEqual(pragma(self.path, "auto_vacuum"), 0)

    def test_failed_verification_leaves_the_original_in_place(self):
        self.prepared_legacy_database()
        size = self.path.stat().st_size
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
        self.assertEqual(self.path.stat().st_size, size)
        self.assertEqual(pragma(self.path, "auto_vacuum"), 0)
        self.assertEqual([p.name for p in self.dir.iterdir() if "compact" in p.name], [])

    def test_cli_returns_2_when_refused(self):
        self.prepared_legacy_database()
        original = compaction.systemd_unit_state
        compaction.systemd_unit_state = lambda unit: "active"
        self.addCleanup(setattr, compaction, "systemd_unit_state", original)
        self.assertEqual(compaction.main(["--db", str(self.path), "--apply"]), 2)


if __name__ == "__main__":
    unittest.main()
