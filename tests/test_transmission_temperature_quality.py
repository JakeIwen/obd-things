from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from projects.vehicle_data import active_drive, ccan_powertrain
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.historian import TelemetryHistorian
from projects.vehicle_data.insights import TelemetryInsights


UTC = timezone.utc


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class Acquirer:
    channel = "can7"

    def acquire(self, _mode):
        raise AssertionError("quality tests must not acquire from CAN")


def quality_event(*, count=1):
    return {
        "type": "quality_event",
        "metric": "transmission.oil_temperature",
        "source": "ccan.broadcast.0x1f7",
        "bus": "c-can",
        "quality": "observed_alfa_scale",
        "reason": "implausible_transition",
        "detail": (
            "raw 0x1F7 transmission-oil temperature was rejected by the "
            "10 C / less-than-one-second gate"
        ),
        "previous_value_c": 37.7777777778,
        "rejected_value_c": 49.7777777778,
        "delta_c": 12.0,
        "elapsed_seconds": 0.2,
        "rejection_count": count,
        "interface_mode": "armed_diagnostic",
    }


class BrokerQualityLatchTests(unittest.TestCase):
    def test_rejection_is_nonfatal_deduplicated_and_last_good_recovers(self):
        clock = Clock()
        broker = TelemetryBroker(acquirer=Acquirer(), monotonic=clock)
        common = {
            "metric": "transmission.oil_temperature",
            "unit": "°F",
            "source": "ccan.broadcast.0x1f7",
            "bus": "c-can",
            "quality": "observed_alfa_scale",
        }
        self.assertTrue(broker.publish_observation(value=100.0, **common).available)
        active_state_before = broker.status_response()["active_drive"]["state"]

        broker.handle_active_drive_event(quality_event(count=2))
        broker.handle_active_drive_event(quality_event(count=3))

        retained = broker.metric_response("transmission.oil_temperature")
        self.assertEqual(retained["value"], 100.0)
        self.assertEqual(
            retained["last_acquisition_error"]["reason"],
            "implausible_transition",
        )
        status = broker.status_response()
        self.assertEqual(status["data_quality"]["active_count"], 1)
        self.assertEqual(status["data_quality"]["authoritative_good"], [])
        self.assertEqual(
            status["data_quality"]["active"][0]["rejection_count"], 5
        )
        self.assertFalse(
            status["data_quality"]["active"][0]["notification_eligible"]
        )
        self.assertEqual(status["active_drive"]["state"], active_state_before)
        self.assertFalse(status["active_drive"]["restoration_failed"])

        # A plausible raw observation proves recovery without an interface or
        # CAN-state action.  The incident remains in bounded recent evidence.
        recovered = broker.publish_observation(value=100.0, **common)
        self.assertTrue(recovered.available)
        status = broker.status_response()
        self.assertEqual(status["data_quality"]["active_count"], 0)
        self.assertEqual(
            status["data_quality"]["authoritative_good"][0]["metric"],
            "transmission.oil_temperature",
        )
        self.assertEqual(status["data_quality"]["recent"][0]["status"], "resolved")
        self.assertNotIn(
            "last_acquisition_error",
            broker.metric_response("transmission.oil_temperature"),
        )

    def test_active_quality_event_is_strictly_allowlisted(self):
        broker = TelemetryBroker(acquirer=Acquirer(), monotonic=Clock())
        event = quality_event()
        event["delta_c"] = 10.0
        with self.assertRaisesRegex(ValueError, "exceed"):
            broker.handle_active_drive_event(event)


class ActiveHelperQualityTests(unittest.TestCase):
    def test_helper_emits_quality_evidence_as_nonfatal_event(self):
        stream = io.StringIO()
        sink = active_drive.JsonEventSink(stream)
        event = ccan_powertrain.DataQualityEvent(
            metric="transmission.oil_temperature",
            source="ccan.broadcast.0x1f7",
            reason="implausible_transition",
            detail="raw level quarantined",
            previous_value_c=40.0,
            rejected_value_c=52.0,
            delta_c=12.0,
            elapsed_seconds=0.2,
            rejection_count=4,
        )

        active_drive._emit_quality_event(sink, event)

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["type"], "quality_event")
        self.assertEqual(payload["rejection_count"], 4)
        self.assertEqual(payload["interface_mode"], "armed_diagnostic")
        self.assertNotIn("state", payload)

    def test_system_backend_reuses_one_gate_across_snapshots(self):
        backend = active_drive.SystemBackend(
            "can7",
            expected_usb_serial="serial-a",
            expected_dev_id=0,
            role_resolver=mock.Mock(),
        )
        expected = ccan_powertrain.BroadcastSnapshot((), (), 0)
        with mock.patch.object(
            ccan_powertrain,
            "read_broadcast_snapshot",
            return_value=expected,
        ) as reader:
            backend.broadcast_snapshot(0.2)
            backend.broadcast_snapshot(0.3)

        self.assertEqual(reader.call_count, 2)
        first_gate = reader.call_args_list[0].kwargs["temperature_gate"]
        second_gate = reader.call_args_list[1].kwargs["temperature_gate"]
        self.assertIs(first_gate, backend.temperature_gate)
        self.assertIs(second_gate, first_gate)


def metric_definition():
    return {
        "name": "transmission.oil_temperature",
        "unit": "°F",
        "value_type": "number",
        "stale_after_seconds": 5.0,
        "sources": [
            {
                "name": "ccan.broadcast.0x1f7",
                "bus": "c-can",
                "quality": "observed_alfa_scale",
                "provenance": "raw 0x1F7 exact-vehicle mapping",
            }
        ],
    }


def metric_value(at):
    return {
        "metric": "transmission.oil_temperature",
        "available": True,
        "unit": "°F",
        "value": 100.0,
        "source": "ccan.broadcast.0x1f7",
        "bus": "c-can",
        "acquisition": "passive",
        "interface_mode": "listen_only",
        "quality": "observed_alfa_scale",
        "observed_at": at.isoformat(),
        "age_ms": 0,
        "stale": False,
    }


def persisted_event(start, *, status="active", count=2, producer="broker-old"):
    resolved_at = start + timedelta(seconds=1) if status == "resolved" else None
    return {
        "incident_id": f"{producer}:1",
        "producer_instance": producer,
        "metric": "transmission.oil_temperature",
        "source": "ccan.broadcast.0x1f7",
        "bus": "c-can",
        "quality": "observed_alfa_scale",
        "reason": "implausible_transition",
        "status": status,
        "first_seen_at": start.isoformat(),
        "last_seen_at": (start + timedelta(milliseconds=200)).isoformat(),
        "resolved_at": resolved_at.isoformat() if resolved_at else None,
        "resolution_reason": (
            "authoritative_good_sample" if status == "resolved" else None
        ),
        "rejection_count": count,
        "detail": "raw level quarantined; last good retained",
        "interface_mode": "listen_only",
        "evidence": {
            "previous_value_c": 37.7777777778,
            "rejected_value_c": 49.7777777778,
            "delta_c": 12.0,
            "elapsed_seconds": 0.2,
        },
        "notification_eligible": False,
    }


def snapshot(at, event=None, *, producer=None, authoritative_good=()):
    definition = metric_definition()
    producer = producer or event["producer_instance"]
    return {
        "status": {
            "vehicle_state": {
                "state": "running",
                "running": True,
                "confidence": "verified",
                "basis": "test_rpm",
                "observed_at": at.isoformat(),
                "age_ms": 0,
            },
            "data_quality": {
                "producer_instance": producer,
                "active_count": int(
                    event is not None and event["status"] == "active"
                ),
                "active": (
                    [event]
                    if event is not None and event["status"] == "active"
                    else []
                ),
                "recent": [] if event is None else [event],
                "authoritative_good": list(authoritative_good),
                "notification_delivery": "disabled_by_design",
            },
        },
        "catalog": [definition],
        "metrics": {
            "transmission.oil_temperature": metric_value(at),
        },
    }


class HistorianQualityTests(unittest.TestCase):
    def test_incident_is_upserted_resolved_and_never_enters_outbox(self):
        start = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "history.sqlite3"
            with TelemetryHistorian(database) as historian:
                historian.ingest_snapshot(
                    snapshot(start, persisted_event(start)),
                    captured_at=start,
                    ingest_key="active",
                )
                active = historian.data_quality_summary(now=start)
                self.assertEqual(active["counts"]["active"], 1)
                self.assertEqual(active["active"][0]["rejection_count"], 2)

                later = start + timedelta(seconds=5)
                historian.ingest_snapshot(
                    snapshot(
                        later,
                        persisted_event(start, status="resolved", count=5),
                    ),
                    captured_at=later,
                    ingest_key="resolved",
                )
                summary = historian.data_quality_summary(now=later)
                self.assertEqual(summary["counts"], {"active": 0, "resolved": 1})
                self.assertEqual(summary["recent"][0]["rejection_count"], 5)
                self.assertFalse(summary["recent"][0]["notification_eligible"])
                outbox_count = historian._conn.execute(
                    "SELECT count(*) FROM advisory_notification_outbox"
                ).fetchone()[0]
                self.assertEqual(outbox_count, 0)
                # Quality evidence must not pin raw snapshots beyond their
                # normal retention window.
                quality_foreign_keys = historian._conn.execute(
                    "PRAGMA foreign_key_list(data_quality_events)"
                ).fetchall()
                snapshot_actions = {
                    row[3]: row[6]
                    for row in quality_foreign_keys
                    if row[2] == "snapshots"
                }
                self.assertEqual(
                    snapshot_actions,
                    {
                        "first_snapshot_id": "SET NULL",
                        "last_snapshot_id": "SET NULL",
                    },
                )

                evaluator = mock.Mock()
                evaluator.evaluate.return_value = {
                    "schema_version": 1,
                    "active": [],
                    "assessments": [],
                }
                insights = TelemetryInsights(
                    historian,
                    warning_evaluator=evaluator,
                    dtc_cache_path=Path(temporary) / "dtc-cache.json",
                )
                health = insights.health_response()
                self.assertEqual(
                    health["data_quality"]["counts"]["resolved"], 1
                )

    def test_new_broker_good_sample_retires_prior_process_active_incident(self):
        start = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        later = start + timedelta(seconds=5)
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "history.sqlite3"
            with TelemetryHistorian(database) as historian:
                historian.ingest_snapshot(
                    snapshot(
                        start,
                        persisted_event(start, producer="broker-prior"),
                    ),
                    captured_at=start,
                    ingest_key="prior-active",
                )
                historian.ingest_snapshot(
                    snapshot(
                        later,
                        producer="broker-current",
                        authoritative_good=(
                            {
                                "metric": "transmission.oil_temperature",
                                "source": "ccan.broadcast.0x1f7",
                                "observed_at": later.isoformat(),
                            },
                        ),
                    ),
                    captured_at=later,
                    ingest_key="current-good",
                )

                summary = historian.data_quality_summary(now=later)
                self.assertEqual(summary["counts"], {"active": 0, "resolved": 1})
                incident = summary["recent"][0]
                self.assertEqual(
                    incident["resolution_reason"],
                    "producer_restarted_then_authoritative_good_sample",
                )
                self.assertEqual(incident["producer_instance"], "broker-prior")


class DashboardQualityTests(unittest.TestCase):
    def test_dashboard_labels_quality_as_non_notifying_sample_filter(self):
        app = (
            Path(__file__).resolve().parents[1]
            / "projects"
            / "vehicle_data"
            / "static"
            / "app.js"
        ).read_text()
        self.assertIn("Telemetry sample filter active", app)
        self.assertIn("data quality only — never notified", app)


if __name__ == "__main__":
    unittest.main()
