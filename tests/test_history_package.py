"""Compatibility contracts for the historian's implementation package."""

import inspect
import pickle
import typing
import unittest

from projects.vehicle_data import historian
from projects.vehicle_data.history import (
    advisories,
    ingest,
    models,
    queries,
    rollups,
    schema,
    store,
    validation,
)


class HistoryPackageTests(unittest.TestCase):
    def test_model_and_constant_exports_are_the_implementation_objects(self):
        names = (
            "SCHEMA_VERSION", "ADVISORY_SCHEMA_VERSION", "DATA_QUALITY_SCHEMA_VERSION",
            "DEFAULT_DATABASE", "MAX_QUERY_SAMPLES", "MAX_SERIES_POINTS",
            "MAX_SERIES_WINDOW_SECONDS", "MAX_PERIOD_DAYS", "MICROSECONDS",
            "REGIME_DIMENSIONS", "ADVISORY_ACTIVE_STATES", "ADVISORY_RESOLVING_STATES",
            "ADVISORY_INCONCLUSIVE_STATES", "MAX_NOTIFICATION_RATE_LIMIT_SECONDS",
            "TRIP_NONCRITICAL_PUSH_CAP", "SYSTEM_NOTE_CATEGORIES", "NOTIFICATION_RANK_SQL",
            "HistorianError", "SnapshotValidationError", "OutOfOrderSnapshotError",
            "HistorianConfig", "IngestResult", "MaintenanceResult",
            "AdvisoryPersistenceResult", "BaselineStats", "_SourceDefinition",
            "_MetricDefinition", "_MetricSample",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(getattr(historian, name), getattr(models, name))

    def test_helper_exports_are_the_implementation_objects(self):
        names = (
            "_is_placeholder_interface_role", "_utc_datetime", "_to_us",
            "_iso_from_us", "_iso", "_optional_nonnegative_int", "_required_text",
            "_bool_db", "_median_mad", "project_regime", "_finite_number",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(getattr(historian, name), getattr(validation, name))

    def test_facade_inherits_original_descriptors_without_wrappers(self):
        owners = (
            store.HistorianStore, schema.SchemaMixin, validation.ValidationMixin,
            ingest.IngestMixin, rollups.RollupMixin, queries.QueryMixin,
            advisories.AdvisoryMixin,
        )
        seen = set()
        for owner in owners:
            for name, descriptor in vars(owner).items():
                if not isinstance(descriptor, (staticmethod, property)) and not inspect.isfunction(descriptor):
                    continue
                with self.subTest(owner=owner.__name__, name=name):
                    self.assertNotIn(name, seen)
                    self.assertIs(inspect.getattr_static(historian.TelemetryHistorian, name), descriptor)
                seen.add(name)
        self.assertIn("ingest_snapshot", seen)
        self.assertIn("run_maintenance", seen)
        self.assertIn("record_advisory_assessments", seen)

    def test_legacy_pickle_class_references_still_resolve(self):
        for name in ("HistorianConfig", "IngestResult", "BaselineStats", "TelemetryHistorian"):
            with self.subTest(name=name):
                old_reference = f"cprojects.vehicle_data.historian\n{name}\n.".encode()
                self.assertIs(pickle.loads(old_reference), getattr(historian, name))

    def test_public_method_annotations_remain_resolvable(self):
        self.assertEqual(
            typing.get_type_hints(historian.TelemetryHistorian.__enter__),
            {"return": historian.TelemetryHistorian},
        )
        for name, method in inspect.getmembers(historian.TelemetryHistorian, inspect.isfunction):
            with self.subTest(name=name):
                typing.get_type_hints(method)

    def test_connection_accessors_preserve_identity_and_transaction_scope(self):
        with historian.TelemetryHistorian(":memory:") as history:
            self.assertIs(history.connection, history._conn)
            self.assertIs(history.connection_lock, history._lock)
            self.assertFalse(history.connection.in_transaction)
            with history.connection_lock:
                # The accessor is a lock, not a connection context manager:
                # leaving this block must not commit the caller's transaction.
                history.connection.execute(
                    "INSERT INTO historian_meta(key,value) VALUES('accessor-test','pending')"
                )
            self.assertTrue(history.connection.in_transaction)
            history.connection.rollback()
            self.assertIsNone(history.connection.execute(
                "SELECT value FROM historian_meta WHERE key='accessor-test'"
            ).fetchone())

    def test_drift_reader_uses_the_same_connection_and_lock(self):
        from projects.vehicle_data.drift_warnings import _Reader

        with historian.TelemetryHistorian(":memory:") as history:
            reader = _Reader(history, 0)
            self.assertIs(reader.conn, history._conn)
            self.assertIs(reader.lock, history._lock)

    def test_store_lifetime_preserves_context_manager_identity(self):
        with historian.TelemetryHistorian(":memory:") as history:
            self.assertIs(history.__enter__(), history)
            self.assertIsInstance(history, store.HistorianStore)
            self.assertEqual(history._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(historian.sqlite3.ProgrammingError):
            history._conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
