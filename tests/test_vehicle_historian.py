import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.vehicle_data.historian import (
    HistorianConfig,
    OutOfOrderSnapshotError,
    SnapshotValidationError,
    TelemetryHistorian,
)


UTC = timezone.utc


def definition(name, unit, *, value_type="number", stale=5.0, provenance=None):
    source = f"test.{name}"
    return {
        "name": name,
        "unit": unit,
        "value_type": value_type,
        "stale_after_seconds": stale,
        "sources": [
            {
                "name": source,
                "bus": "c-can",
                "quality": "verified",
                "provenance": provenance or f"finding for {name}",
            }
        ],
    }


def available(defn, value, observed_at, *, age_ms=0, stale=False):
    source = defn["sources"][0]
    return {
        "metric": defn["name"],
        "available": True,
        "unit": defn["unit"],
        "value": value,
        "source": source["name"],
        "bus": source["bus"],
        "acquisition": "passive",
        "interface_mode": "listen_only",
        "quality": source["quality"],
        "observed_at": observed_at.isoformat(),
        "age_ms": age_ms,
        "stale": stale,
    }


def unavailable(defn, reason="source_unavailable"):
    return {
        "metric": defn["name"],
        "available": False,
        "unit": defn["unit"],
        "reason": reason,
        "detail": "test gap",
    }


def snapshot(at, definitions, values, *, interface="healthy", running=True):
    if interface == "healthy":
        interface_payload = {
            "channel": "can9",
            "usb_serial": "test-serial",
            "adapter_present": True,
            "up": True,
            "bitrate": 500000,
            "listen_only": True,
            "controller_state": "ERROR-ACTIVE",
            "topology": {"bus": "c-can", "usable": True},
        }
    elif interface == "missing":
        interface_payload = None
    else:
        interface_payload = {
            "channel": "can2",
            "adapter_present": False,
            "up": False,
            "bitrate": None,
            "listen_only": None,
            "controller_state": None,
            "topology": {"bus": "c-can", "usable": False},
        }
    status = {
        "vehicle_state": {
            "state": "running" if running else "unknown",
            "running": True if running else None,
            "confidence": "verified" if running else "unknown",
            "basis": "test_rpm" if running else "no_evidence",
            "observed_at": at.isoformat() if running else None,
            "age_ms": 0 if running else None,
        }
    }
    if interface_payload is not None:
        status["interface"] = interface_payload
    return {
        "status": status,
        "catalog": definitions,
        "metrics": values,
    }


def role_aware_interface(*, roles):
    return {
        "channel": "can0",
        "adapter_present": True,
        "up": True,
        "bitrate": 500000,
        "listen_only": True,
        "controller_state": "ERROR-ACTIVE",
        "topology": {"bus": "c-can", "usable": True},
        "role_interfaces": {"ready": bool(roles), "issues": [], "roles": roles},
    }


def role_status(role, channel, bitrate, *, passive_required=True):
    return {
        "resolution": "resolved",
        "channel": channel,
        "expected": {
            "usb_serial": f"serial-{role}",
            "passive_required": passive_required,
        },
        "actual": {
            "up": passive_required,
            "bitrate": bitrate,
            "listen_only": passive_required,
            "controller_state": "ERROR-ACTIVE" if passive_required else "STOPPED",
        },
        "passive_ready": passive_required,
    }


class HistorianIngestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "history.sqlite3"
        self.rpm = definition("engine.rpm", "rpm")
        self.speed = definition("vehicle.speed", "mph")
        self.coolant = definition("engine.coolant_temperature", "°F")
        self.oil = definition("engine.oil_pressure", "psi")
        self.battery = definition("battery.voltage", "V", stale=30)
        self.definitions = [
            self.rpm,
            self.speed,
            self.coolant,
            self.oil,
            self.battery,
        ]
        self.start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    def values(self, at, *, oil=30.0, battery=None):
        return {
            "engine.rpm": available(self.rpm, 800.0, at),
            "vehicle.speed": available(self.speed, 0.0, at),
            "engine.coolant_temperature": available(self.coolant, 195.0, at),
            "engine.oil_pressure": available(self.oil, oil, at),
            "battery.voltage": (
                unavailable(self.battery) if battery is None else available(self.battery, battery, at)
            ),
        }

    def test_preserves_provenance_and_separates_missing_stale_and_interface_gaps(self):
        with TelemetryHistorian(self.path) as historian:
            first = historian.ingest_snapshot(
                snapshot(self.start, self.definitions, self.values(self.start)),
                captured_at=self.start,
                ingest_key="first",
            )
            self.assertIsNotNone(first.trip_id)
            self.assertEqual(first.stored_samples, 4)
            battery_gap = next(
                gap
                for gap in historian.list_gaps("metric", active_only=True)
                if gap["key"] == "battery.voltage"
            )
            self.assertEqual(battery_gap["state"], "missing")

            stale_at = self.start + timedelta(seconds=1)
            values = self.values(stale_at, battery=12.6)
            values["engine.oil_pressure"] = available(
                self.oil, 30.0, stale_at, age_ms=6_000, stale=True
            )
            historian.ingest_snapshot(
                snapshot(stale_at, self.definitions, values, interface="unhealthy"),
                captured_at=stale_at,
                ingest_key="stale",
            )
            samples = historian.query_samples("engine.oil_pressure")
            self.assertEqual(samples[-1]["freshness"], "stale")
            self.assertEqual(samples[-1]["quality"], "verified")
            self.assertEqual(samples[-1]["provenance"], "finding for engine.oil_pressure")
            active_metric = {
                gap["key"]: gap for gap in historian.list_gaps("metric", active_only=True)
            }
            self.assertEqual(active_metric["engine.oil_pressure"]["state"], "stale")
            active_interface = historian.list_gaps("interface", active_only=True)
            self.assertEqual(active_interface[0]["key"], "c-can")
            self.assertEqual(active_interface[0]["state"], "unhealthy")

            recovered_at = stale_at + timedelta(seconds=1)
            historian.ingest_snapshot(
                snapshot(
                    recovered_at,
                    self.definitions,
                    self.values(recovered_at, battery=12.7),
                ),
                captured_at=recovered_at,
                ingest_key="recovered",
            )
            self.assertFalse(historian.list_gaps("metric", active_only=True))
            self.assertFalse(historian.list_gaps("interface", active_only=True))
            completed = historian.list_gaps("interface")
            self.assertIsNotNone(completed[0]["ended_at"])

            missing_at = recovered_at + timedelta(seconds=1)
            historian.ingest_snapshot(
                snapshot(
                    missing_at,
                    self.definitions,
                    self.values(missing_at, battery=12.7),
                    interface="missing",
                ),
                captured_at=missing_at,
                ingest_key="missing-interface",
            )
            missing_roles = {
                gap["key"]: gap
                for gap in historian.list_gaps("interface", active_only=True)
            }
            self.assertEqual(
                missing_roles["interface-status"]["reason"],
                "interface_status_missing",
            )
            self.assertEqual(
                missing_roles["c-can"]["reason"], "interface_role_absent"
            )

    def test_armed_role_snapshot_records_non_passive_but_healthy_ccan(self):
        payload = snapshot(self.start, self.definitions, self.values(self.start))
        payload["status"]["interface"] = {
            "channel": "can7",
            "adapter_present": True,
            "up": True,
            "bitrate": 500000,
            "listen_only": False,
            "controller_state": "ERROR-ACTIVE",
            "mode": "armed_diagnostic",
            "topology": {"bus": "c-can", "usable": True},
            "role_interfaces": {
                "ready": False,
                "issues": [],
                "roles": {
                    "c-can": {
                        "resolution": "resolved",
                        "channel": "can7",
                        "expected": {
                            "usb_serial": "serial-a",
                            "passive_required": True,
                        },
                        "actual": {
                            "up": True,
                            "bitrate": 500000,
                            "listen_only": False,
                            "controller_state": "ERROR-ACTIVE",
                            "mode": "armed_diagnostic",
                        },
                        "passive_ready": False,
                        "topology_usable": True,
                        "operating_mode": "armed_diagnostic",
                    }
                },
            },
        }
        with TelemetryHistorian(self.path) as historian:
            historian.ingest_snapshot(payload, captured_at=self.start)

            row = historian._conn.execute(
                "SELECT * FROM interface_samples WHERE role='c-can'"
            ).fetchone()
            self.assertEqual(row["channel"], "can7")
            self.assertEqual(row["listen_only"], 0)
            self.assertEqual(row["topology_usable"], 1)
            self.assertEqual(row["health"], "healthy")
            self.assertEqual(row["reason"], "armed_diagnostic")
            self.assertFalse(historian.list_gaps("interface", active_only=True))

    def test_pre_reconcile_channel_never_becomes_a_durable_role(self):
        before = snapshot(self.start, self.definitions, self.values(self.start))
        before["status"]["interface"] = role_aware_interface(roles={})
        before["status"]["interface"]["topology"] = {
            "bus": "unknown",
            "usable": False,
        }

        roles = {
            "c-can": role_status("c-can", "can0", 500000),
            "b-can": role_status("b-can", "can1", 125000),
            "can-ch": role_status("can-ch", "can2", 500000),
            "spare": role_status(
                "spare", "can3", None, passive_required=False
            ),
        }
        after_at = self.start + timedelta(seconds=1)
        after = snapshot(after_at, self.definitions, self.values(after_at))
        after["status"]["interface"] = role_aware_interface(roles=roles)
        unresolved_at = self.start + timedelta(seconds=2)
        unresolved = snapshot(
            unresolved_at,
            self.definitions,
            self.values(unresolved_at),
        )
        unresolved["status"]["interface"] = {
            "channel": "can8",
            "adapter_present": True,
            "up": True,
            "bitrate": 500000,
            "listen_only": True,
            "controller_state": "ERROR-ACTIVE",
            "topology": {"bus": "unknown", "usable": False},
        }
        recovered_at = self.start + timedelta(seconds=3)
        recovered = snapshot(
            recovered_at,
            self.definitions,
            self.values(recovered_at),
        )
        recovered["status"]["interface"] = role_aware_interface(roles=roles)

        with TelemetryHistorian(self.path) as historian:
            historian.ingest_snapshot(
                before,
                captured_at=self.start,
                ingest_key="pre-reconcile",
            )
            stored_roles = {
                row[0]
                for row in historian._conn.execute(
                    "SELECT DISTINCT role FROM interface_samples"
                )
            }
            self.assertEqual(stored_roles, set())
            active = historian.list_gaps("interface", active_only=True)
            self.assertEqual([gap["key"] for gap in active], ["interface-status"])

            historian.ingest_snapshot(
                after,
                captured_at=after_at,
                ingest_key="roles-ready",
            )
            stored_roles = {
                row[0]
                for row in historian._conn.execute(
                    "SELECT DISTINCT role FROM interface_samples"
                )
            }
            self.assertEqual(stored_roles, {"c-can", "b-can", "can-ch"})

            historian.ingest_snapshot(
                unresolved,
                captured_at=unresolved_at,
                ingest_key="roles-temporarily-unresolved",
            )
            active = {
                gap["key"]
                for gap in historian.list_gaps("interface", active_only=True)
            }
            self.assertEqual(
                active,
                {"interface-status", "c-can", "b-can", "can-ch"},
            )
            historian.ingest_snapshot(
                recovered,
                captured_at=recovered_at,
                ingest_key="roles-recovered",
            )
            self.assertFalse(historian.list_gaps("interface", active_only=True))
            self.assertEqual(
                historian.dashboard_summary(now=recovered_at)["coverage"]["status"],
                "current",
            )

    def test_malformed_role_snapshot_never_falls_back_to_channel(self):
        variants = (None, [], {}, {"roles": None})
        for index, role_snapshot in enumerate(variants):
            with self.subTest(role_snapshot=role_snapshot):
                payload = snapshot(
                    self.start,
                    self.definitions,
                    self.values(self.start),
                )
                payload["status"]["interface"]["channel"] = "can9"
                payload["status"]["interface"]["topology"] = {
                    "bus": "c-can",
                    "usable": True,
                }
                payload["status"]["interface"][
                    "role_interfaces"
                ] = role_snapshot
                with TelemetryHistorian(":memory:") as historian:
                    historian.ingest_snapshot(
                        payload,
                        captured_at=self.start,
                        ingest_key=f"malformed-{index}",
                    )
                    self.assertEqual(
                        historian._conn.execute(
                            "SELECT count(*) FROM interface_samples"
                        ).fetchone()[0],
                        0,
                    )
                    active = historian.list_gaps("interface", active_only=True)
                    self.assertEqual(
                        [gap["key"] for gap in active],
                        ["interface-status"],
                    )

    def test_interfaces_map_uses_logical_payload_role_and_does_not_mask_nested(self):
        first = snapshot(self.start, self.definitions, self.values(self.start))
        flat = first["status"].pop("interface")
        first["status"]["interfaces"] = {"can9": flat}

        roles = {
            "c-can": role_status("c-can", "can0", 500000),
            "b-can": role_status("b-can", "can1", 125000),
            "can-ch": role_status("can-ch", "can2", 500000),
        }
        next_at = self.start + timedelta(seconds=1)
        following = snapshot(next_at, self.definitions, self.values(next_at))
        following["status"]["interfaces"] = {}
        following["status"]["interface"] = role_aware_interface(roles=roles)

        with TelemetryHistorian(self.path) as historian:
            historian.ingest_snapshot(first, captured_at=self.start)
            self.assertEqual(
                {
                    row[0]
                    for row in historian._conn.execute(
                        "SELECT DISTINCT role FROM interface_samples"
                    )
                },
                {"c-can"},
            )
            historian.ingest_snapshot(following, captured_at=next_at)
            self.assertEqual(
                {
                    row[0]
                    for row in historian._conn.execute(
                        "SELECT DISTINCT role FROM interface_samples"
                    )
                },
                {"c-can", "b-can", "can-ch"},
            )
            self.assertFalse(historian.list_gaps("interface", active_only=True))

    def test_existing_channel_keyed_gap_is_closed_without_deleting_evidence(self):
        roles = {
            "c-can": role_status("c-can", "can0", 500000),
            "b-can": role_status("b-can", "can1", 125000),
            "can-ch": role_status("can-ch", "can2", 500000),
        }
        first = snapshot(self.start, self.definitions, self.values(self.start))
        first["status"]["interface"] = role_aware_interface(roles=roles)
        next_at = self.start + timedelta(seconds=1)
        following = snapshot(next_at, self.definitions, self.values(next_at))
        following["status"]["interface"] = role_aware_interface(roles=roles)
        final_at = self.start + timedelta(seconds=2)
        final = snapshot(final_at, self.definitions, self.values(final_at))
        final["status"]["interface"] = role_aware_interface(roles=roles)

        with TelemetryHistorian(self.path) as historian:
            result = historian.ingest_snapshot(
                first,
                captured_at=self.start,
                ingest_key="before-upgrade",
            )
            historian._conn.execute(
                """
                INSERT INTO interface_samples(
                    snapshot_id,role,captured_us,channel,usb_serial,bus,
                    adapter_present,up,bitrate,listen_only,controller_state,
                    topology_usable,health,reason
                )
                SELECT snapshot_id,'can0',captured_us,'can0',usb_serial,'unknown',
                    adapter_present,up,bitrate,listen_only,controller_state,
                    0,'unhealthy','topology_unusable'
                FROM interface_samples
                WHERE snapshot_id=? AND role='c-can'
                """,
                (result.snapshot_id,),
            )
            historian._update_gap(
                table="interface_gaps",
                key_column="role",
                key="can0",
                gap=("missing", "interface_role_absent"),
                captured_us=int(self.start.timestamp() * 1_000_000),
                snapshot_id=result.snapshot_id,
            )
            historian._update_gap(
                table="interface_gaps",
                key_column="role",
                key="can4",
                gap=("missing", "interface_role_absent"),
                captured_us=int(self.start.timestamp() * 1_000_000),
                snapshot_id=result.snapshot_id,
            )
            historian._conn.commit()

            historian.ingest_snapshot(
                following,
                captured_at=next_at,
                ingest_key="after-upgrade",
            )
            historian.ingest_snapshot(
                final,
                captured_at=final_at,
                ingest_key="after-upgrade-again",
            )

            self.assertEqual(
                historian._conn.execute(
                    "SELECT count(*) FROM interface_samples WHERE role='can0'"
                ).fetchone()[0],
                1,
            )
            old_gaps = historian._conn.execute(
                "SELECT role,ended_at FROM interface_gaps WHERE role IN ('can0','can4')"
            ).fetchall()
            self.assertEqual(
                {row["role"]: row["ended_at"] for row in old_gaps},
                {"can0": next_at.isoformat(), "can4": next_at.isoformat()},
            )
            self.assertNotIn(
                "can0",
                {
                    gap["key"]
                    for gap in historian.list_gaps("interface", active_only=True)
                },
            )
            self.assertFalse(historian.list_gaps("interface", active_only=True))

    def test_duplicate_is_idempotent_and_invalid_source_rolls_back(self):
        with TelemetryHistorian(self.path) as historian:
            payload = snapshot(self.start, self.definitions, self.values(self.start))
            first = historian.ingest_snapshot(payload, captured_at=self.start, ingest_key="same")
            duplicate = historian.ingest_snapshot(
                payload,
                captured_at=self.start + timedelta(seconds=1),
                ingest_key="same",
            )
            self.assertTrue(duplicate.duplicate)
            self.assertEqual(duplicate.snapshot_id, first.snapshot_id)
            self.assertEqual(len(historian.query_samples("engine.rpm")), 1)

            invalid_at = self.start + timedelta(seconds=2)
            invalid = self.values(invalid_at)
            invalid["engine.rpm"]["quality"] = "candidate"
            with self.assertRaises(SnapshotValidationError):
                historian.ingest_snapshot(
                    snapshot(invalid_at, self.definitions, invalid),
                    captured_at=invalid_at,
                    ingest_key="invalid",
                )
            self.assertEqual(len(historian.query_samples("engine.rpm")), 1)

    def test_rejects_out_of_order_snapshot(self):
        with TelemetryHistorian(self.path) as historian:
            historian.ingest_snapshot(
                snapshot(self.start, self.definitions, self.values(self.start)),
                captured_at=self.start,
            )
            earlier = self.start - timedelta(seconds=1)
            with self.assertRaises(OutOfOrderSnapshotError):
                historian.ingest_snapshot(
                    snapshot(earlier, self.definitions, self.values(earlier)),
                    captured_at=earlier,
                )


class HistorianTripAndQueryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "history.sqlite3"
        self.rpm = definition("engine.rpm", "rpm")
        self.speed = definition("vehicle.speed", "mph")
        self.coolant = definition("engine.coolant_temperature", "°F")
        self.oil = definition("engine.oil_pressure", "psi")
        self.definitions = [self.rpm, self.speed, self.coolant, self.oil]
        self.start = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

    def active_values(self, at, oil=30.0):
        return {
            "engine.rpm": available(self.rpm, 800.0, at),
            "vehicle.speed": available(self.speed, 0.0, at),
            "engine.coolant_temperature": available(self.coolant, 190.0, at),
            "engine.oil_pressure": available(self.oil, oil, at),
        }

    def inactive_values(self):
        return {definition["name"]: unavailable(definition) for definition in self.definitions}

    def test_segments_trips_after_activity_timeout(self):
        config = HistorianConfig(trip_idle_timeout_seconds=5, rollup_seconds=10)
        with TelemetryHistorian(self.path, config=config) as historian:
            first = historian.ingest_snapshot(
                snapshot(self.start, self.definitions, self.active_values(self.start)),
                captured_at=self.start,
            )
            grace_at = self.start + timedelta(seconds=2)
            grace = historian.ingest_snapshot(
                snapshot(
                    grace_at,
                    self.definitions,
                    self.inactive_values(),
                    running=False,
                ),
                captured_at=grace_at,
            )
            self.assertEqual(grace.trip_id, first.trip_id)
            closed_at = self.start + timedelta(seconds=6)
            closed = historian.ingest_snapshot(
                snapshot(
                    closed_at,
                    self.definitions,
                    self.inactive_values(),
                    running=False,
                ),
                captured_at=closed_at,
            )
            self.assertIsNone(closed.trip_id)
            next_at = self.start + timedelta(seconds=7)
            next_trip = historian.ingest_snapshot(
                snapshot(next_at, self.definitions, self.active_values(next_at)),
                captured_at=next_at,
            )
            self.assertNotEqual(first.trip_id, next_trip.trip_id)
            trips = historian.list_trips()
            self.assertEqual(trips[0]["state"], "open")
            self.assertEqual(trips[1]["state"], "complete")
            self.assertEqual(trips[1]["ended_at"], self.start.isoformat())

    def test_rollup_baseline_series_and_compact_dashboard_queries_are_bounded(self):
        config = HistorianConfig(trip_idle_timeout_seconds=300, rollup_seconds=10)
        with TelemetryHistorian(self.path, config=config) as historian:
            for index in range(12):
                at = self.start + timedelta(seconds=index * 10)
                historian.ingest_snapshot(
                    snapshot(
                        at,
                        self.definitions,
                        self.active_values(at, oil=29.0 + index % 3),
                    ),
                    captured_at=at,
                )
            result = historian.refresh_rollups(
                through=self.start + timedelta(seconds=130)
            )
            self.assertEqual(result["buckets"], 12)
            sample = historian.latest_sample("engine.oil_pressure")
            baseline = historian.robust_baseline(
                "engine.oil_pressure",
                sample["regime"],
                before=self.start + timedelta(seconds=130),
                unit=sample["unit"],
                quality=sample["quality"],
                source=sample["source"],
                provenance=sample["provenance"],
            )
            self.assertEqual(baseline.bucket_count, 12)
            self.assertEqual(baseline.median, 30.0)
            series = historian.metric_series(
                "engine.oil_pressure",
                end=self.start + timedelta(seconds=120),
                window_seconds=120,
                max_points=5,
            )
            self.assertLessEqual(len(series["points"]), 5)
            self.assertGreaterEqual(series["bucket_seconds"], 24)
            summary = historian.dashboard_summary(
                now=self.start + timedelta(seconds=120),
                metrics=("engine.oil_pressure",),
            )
            self.assertIn("7d", summary["windows"])
            self.assertIn("30d", summary["windows"])
            self.assertIn("trip_comparison", summary)
            self.assertNotIn("points", summary)
            with self.assertRaises(ValueError):
                historian.metric_series("engine.oil_pressure", max_points=513)
            with self.assertRaises(ValueError):
                historian.query_samples("engine.oil_pressure", limit=2_001)

    def test_rollups_count_independent_observations_not_cached_repeats(self):
        config = HistorianConfig(rollup_seconds=5)
        with TelemetryHistorian(self.path, config=config) as historian:
            for index in range(3):
                at = self.start + timedelta(seconds=index * 5)
                values = self.active_values(at)
                values["engine.oil_pressure"] = available(
                    self.oil,
                    30.0,
                    self.start,
                    age_ms=index * 1_000,
                )
                historian.ingest_snapshot(
                    snapshot(at, self.definitions, values),
                    captured_at=at,
                )
            historian.refresh_rollups(through=self.start + timedelta(seconds=20))
            sample = historian.latest_sample("engine.oil_pressure")
            baseline = historian.robust_baseline(
                "engine.oil_pressure",
                sample["regime"],
                before=self.start + timedelta(seconds=20),
                unit=sample["unit"],
                quality=sample["quality"],
                source=sample["source"],
                provenance=sample["provenance"],
            )
            self.assertIsNotNone(baseline)
            self.assertEqual(baseline.bucket_count, 1)
            self.assertEqual(baseline.sample_count, 1)

    def test_completed_trip_comparison_tolerates_new_metric_without_samples(self):
        config = HistorianConfig(
            trip_idle_timeout_seconds=1,
            rollup_seconds=5,
        )
        with TelemetryHistorian(self.path, config=config) as historian:
            historian.ingest_snapshot(
                snapshot(
                    self.start,
                    self.definitions,
                    self.active_values(self.start),
                ),
                captured_at=self.start,
            )
            ended = self.start + timedelta(seconds=2)
            historian.ingest_snapshot(
                snapshot(
                    ended,
                    self.definitions,
                    self.inactive_values(),
                    running=False,
                ),
                captured_at=ended,
            )

            comparison = historian.trip_comparison(
                ("engine.crankshaft_power",)
            )

            metric = comparison["metrics"]["engine.crankshaft_power"]
            self.assertIsNone(metric["current_trip"])
            self.assertIsNone(metric["prior_trips"])
            self.assertIsNone(metric["current_minus_prior_median"])


class HistorianMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "history.sqlite3"
        self.rpm = definition("engine.rpm", "rpm")
        self.speed = definition("vehicle.speed", "mph")
        self.coolant = definition("engine.coolant_temperature", "°F")
        self.oil = definition("engine.oil_pressure", "psi")
        self.definitions = [self.rpm, self.speed, self.coolant, self.oil]
        self.start = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)

    def active_values(self, at, *, oil=30.0):
        return {
            "engine.rpm": available(self.rpm, 800.0, at),
            "vehicle.speed": available(self.speed, 0.0, at),
            "engine.coolant_temperature": available(self.coolant, 190.0, at),
            "engine.oil_pressure": available(self.oil, oil, at),
        }

    def inactive_values(self):
        return {
            item["name"]: unavailable(item)
            for item in self.definitions
        }

    def test_rollup_backlog_blocks_all_raw_deletion(self):
        config = HistorianConfig(
            rollup_seconds=5,
            rollup_max_buckets_per_call=1,
            raw_retention_days=1,
        )
        with TelemetryHistorian(self.path, config=config) as historian:
            for index in range(3):
                at = self.start + timedelta(seconds=index * 5)
                historian.ingest_snapshot(
                    snapshot(at, self.definitions, self.active_values(at)),
                    captured_at=at,
                )
            result = historian.run_maintenance(
                now=self.start + timedelta(days=3),
                force=True,
                max_rollup_passes=1,
            )
            self.assertEqual(result.status, "blocked_rollup_backlog")
            self.assertEqual(result.deleted_metric_samples, 0)
            self.assertEqual(result.deleted_interface_samples, 0)
            self.assertEqual(result.deleted_snapshots, 0)
            self.assertEqual(len(historian.query_samples("engine.rpm")), 3)
            self.assertTrue(historian.maintenance_due(now=self.start + timedelta(days=3)))

    def test_maintenance_with_only_retained_data_advances_empty_boundary(self):
        config = HistorianConfig(raw_retention_days=7)
        now = self.start + timedelta(hours=1)
        with TelemetryHistorian(self.path, config=config) as historian:
            historian.ingest_snapshot(
                snapshot(
                    self.start,
                    self.definitions,
                    self.active_values(self.start),
                ),
                captured_at=self.start,
            )

            result = historian.run_maintenance(now=now, force=True)

            self.assertEqual(result.status, "completed")
            self.assertFalse(result.raw_backlog)
            self.assertEqual(result.deleted_metric_samples, 0)
            self.assertEqual(result.deleted_interface_samples, 0)
            status = historian.maintenance_status(now=now)
            self.assertEqual(status["rollup_through_at"], result.delete_before_at)
            self.assertFalse(status["due"])

    def test_pruning_preserves_cross_boundary_observation_identity(self):
        config = HistorianConfig(rollup_seconds=5, raw_retention_days=1)
        boundary = self.start + timedelta(days=1)
        first_at = boundary - timedelta(seconds=1)
        repeat_at = boundary + timedelta(seconds=1)
        now = boundary + timedelta(days=1)
        with TelemetryHistorian(self.path, config=config) as historian:
            historian.ingest_snapshot(
                snapshot(
                    first_at,
                    self.definitions,
                    self.active_values(first_at, oil=30.0),
                ),
                captured_at=first_at,
            )
            repeat_values = self.active_values(repeat_at, oil=30.0)
            repeat_values["engine.oil_pressure"] = available(
                self.oil,
                30.0,
                first_at,
                age_ms=2000,
            )
            historian.ingest_snapshot(
                snapshot(repeat_at, self.definitions, repeat_values),
                captured_at=repeat_at,
            )

            maintenance = historian.run_maintenance(now=now, force=True)
            self.assertEqual(maintenance.status, "completed")
            self.assertFalse(maintenance.raw_backlog)
            sentinel_count = historian._conn.execute(
                "SELECT count(*) FROM metric_samples "
                "WHERE metric='engine.oil_pressure' AND captured_us<?",
                (int(boundary.timestamp() * 1_000_000),),
            ).fetchone()[0]
            self.assertEqual(sentinel_count, 1)

            historian.refresh_rollups(through=repeat_at + timedelta(seconds=10))
            rolled_count = historian._conn.execute(
                "SELECT coalesce(sum(sample_count),0) FROM metric_rollups "
                "WHERE metric='engine.oil_pressure'"
            ).fetchone()[0]
            self.assertEqual(rolled_count, 1)
            series = historian.metric_series(
                "engine.oil_pressure",
                end=repeat_at + timedelta(seconds=10),
                window_seconds=20,
                max_points=16,
            )
            self.assertEqual(
                sum(point["sample_count"] for point in series["points"]),
                1,
            )

    def test_pruning_preserves_rollups_trip_comparison_and_gap_references(self):
        config = HistorianConfig(
            trip_idle_timeout_seconds=3,
            rollup_seconds=5,
            raw_retention_days=1,
        )
        now = self.start + timedelta(days=3)
        with TelemetryHistorian(self.path, config=config) as historian:
            first = historian.ingest_snapshot(
                snapshot(
                    self.start,
                    self.definitions,
                    self.active_values(self.start, oil=30.0),
                ),
                captured_at=self.start,
            )
            second_at = self.start + timedelta(seconds=2)
            historian.ingest_snapshot(
                snapshot(
                    second_at,
                    self.definitions,
                    self.active_values(second_at, oil=32.0),
                ),
                captured_at=second_at,
            )
            gap_first_at = self.start + timedelta(seconds=6)
            gap_first = historian.ingest_snapshot(
                snapshot(
                    gap_first_at,
                    self.definitions,
                    self.inactive_values(),
                    running=False,
                ),
                captured_at=gap_first_at,
            )
            gap_last_at = self.start + timedelta(seconds=7)
            gap_last = historian.ingest_snapshot(
                snapshot(
                    gap_last_at,
                    self.definitions,
                    self.inactive_values(),
                    running=False,
                ),
                captured_at=gap_last_at,
            )
            current_at = now - timedelta(hours=1)
            current = historian.ingest_snapshot(
                snapshot(
                    current_at,
                    self.definitions,
                    self.active_values(current_at, oil=20.0),
                ),
                captured_at=current_at,
            )
            self.assertNotEqual(first.trip_id, current.trip_id)

            result = historian.run_maintenance(now=now, force=True)
            self.assertEqual(result.status, "completed")
            self.assertGreater(result.deleted_metric_samples, 0)
            self.assertGreater(result.deleted_interface_samples, 0)
            comparison = historian.trip_comparison(("engine.oil_pressure",))
            oil = comparison["metrics"]["engine.oil_pressure"]
            self.assertEqual(oil["current_trip"]["mean"], 20.0)
            self.assertEqual(oil["prior_trips"]["trip_count"], 1)
            self.assertEqual(oil["prior_trips"]["median_of_trip_means"], 31.0)
            self.assertTrue(oil["current_trip"]["rollup_backed"] is False)

            old_rollups = historian._conn.execute(
                "SELECT count(*) FROM metric_rollups WHERE trip_key=?",
                (first.trip_id,),
            ).fetchone()[0]
            self.assertGreater(old_rollups, 0)
            catalog_rows = historian._conn.execute(
                "SELECT count(*) FROM catalog_sources"
            ).fetchone()[0]
            self.assertEqual(catalog_rows, len(self.definitions))
            self.assertTrue(historian.list_gaps("metric"))
            self.assertEqual(len(historian.list_trips()), 2)
            status = historian.maintenance_status(now=now)
            self.assertFalse(status["due"])
            self.assertEqual(status["last_status"], "completed")
            series = historian.metric_series(
                "engine.oil_pressure",
                end=now,
                window_seconds=4 * 24 * 60 * 60,
                max_points=96,
            )
            self.assertEqual(series["series_basis"], "minute_rollups")
            self.assertEqual(
                sum(point["sample_count"] for point in series["points"]),
                3,
            )
            dashboard = historian.dashboard_summary(
                now=now,
                metrics=("engine.oil_pressure",),
            )
            self.assertEqual(
                dashboard["coverage"]["retention"]["last_status"],
                "completed",
            )
            seven_day_oil = dashboard["windows"]["7d"]["metrics"][
                "engine.oil_pressure"
            ]
            self.assertEqual(seven_day_oil["sample_count"], 3)

        with sqlite3.connect(self.path) as connection:
            surviving = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM snapshots WHERE id IN (?,?,?)",
                    (first.snapshot_id, gap_first.snapshot_id, gap_last.snapshot_id),
                )
            }
            self.assertNotIn(first.snapshot_id, surviving)
            self.assertIn(gap_first.snapshot_id, surviving)
            self.assertIn(gap_last.snapshot_id, surviving)
            old_raw = connection.execute(
                "SELECT count(*) FROM metric_samples WHERE captured_us<?",
                (int((now - timedelta(days=1)).timestamp() * 1_000_000),),
            ).fetchone()[0]
            self.assertEqual(old_raw, 0)
            old_interfaces = connection.execute(
                "SELECT count(*) FROM interface_samples WHERE captured_us<?",
                (int((now - timedelta(days=1)).timestamp() * 1_000_000),),
            ).fetchone()[0]
            self.assertEqual(old_interfaces, 0)

    def test_delete_cap_reports_partial_and_remains_due(self):
        config = HistorianConfig(
            rollup_seconds=5,
            raw_retention_days=1,
            maintenance_max_delete_rows_per_table=1,
        )
        with TelemetryHistorian(self.path, config=config) as historian:
            for index in range(2):
                at = self.start + timedelta(seconds=index * 5)
                historian.ingest_snapshot(
                    snapshot(at, self.definitions, self.active_values(at)),
                    captured_at=at,
                )
            now = self.start + timedelta(days=3)
            result = historian.run_maintenance(now=now, force=True)
            self.assertEqual(result.status, "partial")
            self.assertTrue(result.raw_backlog)
            self.assertEqual(result.deleted_metric_samples, 1)
            self.assertTrue(historian.maintenance_due(now=now))

    def test_retention_configuration_requires_positive_integers(self):
        with self.assertRaises(ValueError):
            HistorianConfig(raw_retention_days=0)
        with self.assertRaises(ValueError):
            HistorianConfig(raw_retention_days=True)


if __name__ == "__main__":
    unittest.main()
