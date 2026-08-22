import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from projects.vehicle_data.early_warning import InfrastructureHealthEvaluator
from projects.vehicle_data.historian import (
    SnapshotValidationError,
    TelemetryHistorian,
)
from projects.vehicle_data.insights import TelemetryInsights
from tests.test_vehicle_historian import (
    available,
    definition,
    role_aware_interface,
    role_status,
    snapshot,
)


UTC = timezone.utc


def usb_event(at, event_id, kind, action, *, affected=("serial-c-can",)):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "boot_id": "boot-a",
        "kernel_seqnum": "100" if action != "reconcile" else None,
        "kind": kind,
        "action": action,
        "scope": "path:fixture",
        "devpath": "/devices/platform/usb1/fixture",
        "usb_vid": "1d50",
        "usb_pid": "606f",
        "usb_serial": affected[0] if len(affected) == 1 else None,
        "affected_serials": list(affected),
        "occurred_at": at.isoformat(),
        "observed_monotonic": 100.0,
        "source": (
            "serial_role_reconciliation"
            if action == "reconcile"
            else "kernel_kobject_uevent"
        ),
        "receive_only": True,
        "hardware_action": False,
    }


def usb_incident(
    opened,
    event,
    *,
    state="active",
    resolved=None,
    producer="usb-monitor:producer-a",
):
    payload = {
        "schema_version": 1,
        "incident_id": "usb-can-incident-v1:fixture",
        "state": state,
        "kind": "usb_parent_hub_removed",
        "scope": "hub:fixture",
        "affected_serials": ["serial-c-can", "serial-b-can"],
        "opened_event_id": event["event_id"],
        "opened_at": opened.isoformat(),
        "last_event_id": event["event_id"],
        "last_seen_at": opened.isoformat(),
        "event_count": 1,
        "reappearance_count": 0,
        "reappeared_at": None,
        "resolved_event_id": None,
        "resolved_at": None,
        "resolution": None,
        "source": "kernel_kobject_uevent",
        "producer_instance": producer,
        "notification_eligible": state == "active",
    }
    if state == "resolved":
        payload.update(
            resolved_event_id="usb-can-event-v1:recovered",
            resolved_at=resolved.isoformat(),
            resolution="exact_serial_role_health_reestablished",
            notification_eligible=False,
            last_seen_at=resolved.isoformat(),
            event_count=2,
            reappearance_count=2,
        )
    return payload


class UsbCanHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.sqlite3"
        self.start = datetime(2026, 8, 20, 11, 53, 2, tzinfo=UTC)
        self.rpm = definition("engine.rpm", "rpm")

    def payload(self, at, batch):
        payload = snapshot(
            at,
            [self.rpm],
            {"engine.rpm": available(self.rpm, 0.0, at)},
            running=False,
        )
        roles = {
            "c-can": role_status("c-can", "can0", 500000),
            "b-can": role_status("b-can", "can1", 125000),
            "can-ch": role_status("can-ch", "can2", 500000),
            "spare": role_status(
                "spare", "can3", None, passive_required=False
            ),
        }
        payload["status"]["interface"] = role_aware_interface(roles=roles)
        payload["status"]["interface"]["role_interfaces"][
            "generation"
        ] = "generation-a"
        payload["status"]["active_drive"] = {"restoration_failed": False}
        payload["status"]["usb_can_monitor"] = {
            "state": "running",
            "source": "kernel_kobject_uevent",
            "producer_instance": batch["producer_instance"],
            "boot_id": batch["boot_id"],
            "receive_only": True,
            "hardware_actions": False,
        }
        payload["_usb_can_monitor"] = batch
        return payload

    @staticmethod
    def batch(
        events,
        incidents,
        *,
        dropped=0,
        producer="usb-monitor:producer-a",
        boot_id="boot-a",
    ):
        return {
            "schema_version": 1,
            "source": "kernel_kobject_uevent",
            "producer_instance": producer,
            "boot_id": boot_id,
            "events": events,
            "incidents": incidents,
            "dropped_event_count": dropped,
        }

    def test_fast_remove_and_recovery_are_durable_and_close_advisory(self):
        removed = usb_event(
            self.start,
            "usb-can-event-v1:removed",
            "usb_parent_hub_removed",
            "remove",
            affected=("serial-c-can", "serial-b-can"),
        )
        active = usb_incident(self.start, removed)
        with TelemetryHistorian(self.path) as historian:
            first = historian.ingest_snapshot(
                self.payload(self.start, self.batch([removed], [active])),
                captured_at=self.start,
                ingest_key="first",
            )
            assessment = next(
                item
                for item in InfrastructureHealthEvaluator(historian).evaluate(
                    first.snapshot_id, at=self.start
                )["assessments"]
                if item["rule"] == "usb_can_transient_disconnect"
            )
            self.assertEqual(assessment["state"], "warning")
            self.assertTrue(assessment["notification_eligible"])
            persistence = historian.record_advisory_assessments(
                [assessment], evaluated_at=self.start
            )
            self.assertEqual(persistence.opened, 1)
            self.assertEqual(persistence.notifications_enqueued, 1)
            historian.mark_usb_can_advisory_events_consumed(
                (removed["event_id"],),
                consumed_at=self.start,
                snapshot_id=first.snapshot_id,
            )

            recovered_at = self.start + timedelta(seconds=5)
            recovered = usb_event(
                recovered_at,
                "usb-can-event-v1:recovered",
                "usb_can_recovered",
                "reconcile",
                affected=("serial-c-can", "serial-b-can"),
            )
            resolved = usb_incident(
                self.start, removed, state="resolved", resolved=recovered_at
            )
            second = historian.ingest_snapshot(
                self.payload(
                    recovered_at,
                    self.batch([recovered], [resolved]),
                ),
                captured_at=recovered_at,
                ingest_key="second",
            )
            assessment = next(
                item
                for item in InfrastructureHealthEvaluator(historian).evaluate(
                    second.snapshot_id, at=recovered_at
                )["assessments"]
                if item["rule"] == "usb_can_transient_disconnect"
            )
            self.assertEqual(assessment["state"], "normal")
            persistence = historian.record_advisory_assessments(
                [assessment], evaluated_at=recovered_at
            )
            self.assertEqual(persistence.resolved, 1)

            summary = historian.usb_can_incident_summary()
            self.assertEqual(summary["event_count"], 2)
            self.assertEqual(summary["incident_count"], 1)
            self.assertEqual(summary["active_count"], 0)
            self.assertEqual(
                summary["recent_incidents"][0]["state"], "resolved"
            )

    def test_failed_advisory_pass_replays_fast_recovered_edge_exactly_once(self):
        removed = usb_event(
            self.start,
            "usb-can-event-v1:checkpoint-remove",
            "usb_parent_hub_removed",
            "remove",
            affected=("serial-c-can", "serial-b-can"),
        )
        recovered_at = self.start + timedelta(seconds=1)
        recovered = usb_event(
            recovered_at,
            "usb-can-event-v1:checkpoint-recovered",
            "usb_can_recovered",
            "reconcile",
            affected=("serial-c-can", "serial-b-can"),
        )
        resolved = usb_incident(
            self.start,
            removed,
            state="resolved",
            resolved=recovered_at,
        )

        class FailOnceVehicleEvaluator:
            def __init__(self):
                self.calls = 0

            def evaluate(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("injected advisory evaluation failure")
                return {"schema_version": 1, "active": [], "assessments": []}

        class CollectingSink:
            enabled = True

            def __init__(self):
                self.payloads = []

            def deliver(self, payload):
                self.payloads.append(payload)

        historian = TelemetryHistorian(self.path)
        sink = CollectingSink()
        insights = TelemetryInsights(
            historian,
            warning_evaluator=FailOnceVehicleEvaluator(),
            notification_sink=sink,
            enable_notification_delivery=True,
            dtc_cache_path=Path(self.tmp.name) / "dtcs.json",
            history_metrics=("engine.rpm",),
        )
        self.addCleanup(insights.close)
        batch = self.batch([removed, recovered], [resolved])

        first = insights.ingest_snapshot(
            self.payload(recovered_at, batch),
            captured_at=recovered_at,
            ingest_key="checkpoint-failed",
        )
        self.assertFalse(first.advisory_checkpoint_complete)
        self.assertIn(
            removed["event_id"],
            historian.usb_can_health_context(first.snapshot_id)[
                "unconsumed_removal_event_ids"
            ],
        )
        self.assertEqual(sink.payloads, [])

        second_at = self.start + timedelta(seconds=6)
        second = insights.ingest_snapshot(
            self.payload(second_at, batch),
            captured_at=second_at,
            ingest_key="checkpoint-retry",
        )
        self.assertTrue(second.advisory_checkpoint_complete)
        self.assertEqual(
            second.advisory_consumed_event_ids,
            (removed["event_id"],),
        )
        self.assertEqual(len(sink.payloads), 1)
        self.assertEqual(
            historian.usb_can_health_context(second.snapshot_id)[
                "unconsumed_removal_event_ids"
            ],
            [],
        )

        third_at = self.start + timedelta(seconds=11)
        third = insights.ingest_snapshot(
            self.payload(third_at, batch),
            captured_at=third_at,
            ingest_key="checkpoint-after-consumption",
        )
        self.assertTrue(third.advisory_checkpoint_complete)
        self.assertEqual(len(sink.payloads), 1)
        self.assertEqual(
            historian.advisory_summary()["notification_outbox"]["delivered"],
            1,
        )

    def test_future_timestamp_removal_waits_for_next_snapshot_checkpoint(self):
        captured_at = self.start
        occurred_at = self.start + timedelta(seconds=1)
        removed = usb_event(
            occurred_at,
            "usb-can-event-v1:future-checkpoint",
            "usb_can_adapter_removed",
            "remove",
        )
        active = usb_incident(occurred_at, removed)

        class EmptyVehicleEvaluator:
            def evaluate(self, **_kwargs):
                return {"schema_version": 1, "active": [], "assessments": []}

        class CollectingSink:
            enabled = True

            def __init__(self):
                self.payloads = []

            def deliver(self, payload):
                self.payloads.append(payload)

        historian = TelemetryHistorian(self.path)
        sink = CollectingSink()
        insights = TelemetryInsights(
            historian,
            warning_evaluator=EmptyVehicleEvaluator(),
            notification_sink=sink,
            enable_notification_delivery=True,
            dtc_cache_path=Path(self.tmp.name) / "future-dtcs.json",
            history_metrics=("engine.rpm",),
        )
        self.addCleanup(insights.close)
        batch = self.batch([removed], [active])

        first = insights.ingest_snapshot(
            self.payload(captured_at, batch),
            captured_at=captured_at,
            ingest_key="before-event-wall-time",
        )
        self.assertTrue(first.advisory_checkpoint_complete)
        self.assertEqual(first.advisory_consumed_event_ids, ())
        self.assertEqual(sink.payloads, [])

        second_at = self.start + timedelta(seconds=2)
        second = insights.ingest_snapshot(
            self.payload(second_at, batch),
            captured_at=second_at,
            ingest_key="after-event-wall-time",
        )
        self.assertTrue(second.advisory_checkpoint_complete)
        self.assertEqual(
            second.advisory_consumed_event_ids,
            (removed["event_id"],),
        )
        self.assertEqual(len(sink.payloads), 1)

    def test_replayed_event_id_is_not_a_second_new_edge(self):
        removed = usb_event(
            self.start,
            "usb-can-event-v1:deduped",
            "usb_can_adapter_removed",
            "remove",
        )
        active = usb_incident(self.start, removed)
        with TelemetryHistorian(self.path) as historian:
            first = historian.ingest_snapshot(
                self.payload(self.start, self.batch([removed], [active])),
                captured_at=self.start,
                ingest_key="first",
            )
            second_at = self.start + timedelta(seconds=5)
            second = historian.ingest_snapshot(
                self.payload(second_at, self.batch([removed], [active])),
                captured_at=second_at,
                ingest_key="second",
            )
            self.assertEqual(
                len(historian.usb_can_health_context(first.snapshot_id)["new_events"]),
                1,
            )
            context = historian.usb_can_health_context(second.snapshot_id)
            self.assertEqual(context["new_events"], [])
            self.assertEqual(len(context["active_incidents"]), 1)
            self.assertEqual(historian.usb_can_incident_summary()["event_count"], 1)

    def test_unavailable_incident_history_is_inconclusive_not_recovery(self):
        removed = usb_event(
            self.start,
            "usb-can-event-v1:unavailable",
            "usb_can_adapter_removed",
            "remove",
        )
        active = usb_incident(self.start, removed)
        with TelemetryHistorian(self.path) as historian:
            stored = historian.ingest_snapshot(
                self.payload(self.start, self.batch([removed], [active])),
                captured_at=self.start,
                ingest_key="active-before-history-loss",
            )
            evaluator = InfrastructureHealthEvaluator(historian)
            warning = next(
                item
                for item in evaluator.evaluate(
                    stored.snapshot_id,
                    at=self.start,
                )["assessments"]
                if item["rule"] == "usb_can_transient_disconnect"
            )
            historian.record_advisory_assessments(
                [warning],
                evaluated_at=self.start,
            )

            with mock.patch.object(
                historian,
                "usb_can_health_context",
                side_effect=RuntimeError("fixture history unavailable"),
            ):
                unavailable = next(
                    item
                    for item in evaluator.evaluate(
                        stored.snapshot_id,
                        at=self.start + timedelta(seconds=1),
                    )["assessments"]
                    if item["rule"] == "usb_can_transient_disconnect"
                )
            self.assertEqual(unavailable["state"], "unavailable")
            self.assertFalse(unavailable["notification_eligible"])
            persistence = historian.record_advisory_assessments(
                [unavailable],
                evaluated_at=self.start + timedelta(seconds=1),
            )
            self.assertEqual(persistence.inconclusive, 1)
            self.assertEqual(persistence.resolved, 0)
            episode = historian.list_advisory_episodes(active_only=True)[0]
            self.assertEqual(episode["state"], "warning")
            self.assertEqual(episode["evidence_state"], "unavailable")

    def test_usb_incident_is_sole_notification_owner_for_covered_role_loss(self):
        with TelemetryHistorian(self.path) as historian:
            baseline = self.payload(
                self.start,
                self.batch([], []),
            )
            baseline["status"]["interface"]["role_interfaces"][
                "generation"
            ] = "generation-a"
            historian.ingest_snapshot(
                baseline,
                captured_at=self.start,
                ingest_key="healthy-baseline",
            )

            removed_at = self.start + timedelta(seconds=5)
            removed = usb_event(
                removed_at,
                "usb-can-event-v1:sole-owner",
                "usb_parent_hub_removed",
                "remove",
                affected=("serial-c-can", "serial-b-can"),
            )
            active = usb_incident(removed_at, removed)

            def missing_payload(at, *, events):
                payload = self.payload(
                    at,
                    self.batch(events, [active]),
                )
                payload["status"]["interface"]["role_interfaces"][
                    "generation"
                ] = "generation-b"
                roles = payload["status"]["interface"]["role_interfaces"][
                    "roles"
                ]
                for role in ("c-can", "b-can"):
                    roles[role].update(
                        {
                            "resolution": "missing",
                            "channel": None,
                            "passive_ready": False,
                            "reason": "role_missing",
                            "detail": "fixture hub branch is absent",
                            "actual": {
                                "up": None,
                                "bitrate": None,
                                "listen_only": None,
                                "controller_state": None,
                            },
                        }
                    )
                return payload

            first = historian.ingest_snapshot(
                missing_payload(removed_at, events=[removed]),
                captured_at=removed_at,
                ingest_key="removed",
            )
            first_report = InfrastructureHealthEvaluator(historian).evaluate(
                first.snapshot_id,
                at=removed_at,
            )
            self.assertEqual(
                [
                    item["rule"]
                    for item in first_report["assessments"]
                    if item["notification_eligible"]
                ],
                ["usb_can_transient_disconnect"],
            )
            topology = next(
                item
                for item in first_report["assessments"]
                if item["rule"] == "usb_can_topology_generation_changed"
            )
            self.assertEqual(topology["state"], "warning")
            self.assertEqual(
                topology["notification_suppressed_by"],
                "usb_can_transient_disconnect",
            )

            repeated_at = self.start + timedelta(seconds=10)
            repeated = historian.ingest_snapshot(
                missing_payload(repeated_at, events=[]),
                captured_at=repeated_at,
                ingest_key="still-missing",
            )
            repeated_report = InfrastructureHealthEvaluator(historian).evaluate(
                repeated.snapshot_id,
                at=repeated_at,
            )
            self.assertEqual(
                [
                    item["rule"]
                    for item in repeated_report["assessments"]
                    if item["notification_eligible"]
                ],
                ["usb_can_transient_disconnect"],
            )
            ccan = next(
                item
                for item in repeated_report["assessments"]
                if item["rule"] == "can_interface_role_c_can"
            )
            self.assertEqual(ccan["state"], "warning")
            self.assertEqual(
                ccan["notification_suppressed_by"],
                "usb_can_transient_disconnect",
            )

    def test_restart_retires_prior_active_incident_only_after_exact_roles_are_healthy(self):
        removed = usb_event(
            self.start,
            "usb-can-event-v1:restart-remove",
            "usb_can_adapter_removed",
            "remove",
        )
        active = usb_incident(self.start, removed)
        with TelemetryHistorian(self.path) as historian:
            first_payload = self.payload(
                self.start,
                self.batch([removed], [active]),
            )
            # The affected C-CAN role is still absent before the restart.
            first_payload["status"]["interface"]["role_interfaces"]["roles"][
                "c-can"
            ].update(
                {
                    "resolution": "missing",
                    "safe": False,
                    "passive_ready": False,
                    "actual": {"present": False, "up": None},
                }
            )
            first = historian.ingest_snapshot(
                first_payload,
                captured_at=self.start,
                ingest_key="before-restart",
            )
            historian.mark_usb_can_advisory_events_consumed(
                (removed["event_id"],),
                consumed_at=self.start,
                snapshot_id=first.snapshot_id,
            )

            still_missing_at = self.start + timedelta(seconds=5)
            still_missing = self.payload(
                still_missing_at,
                self.batch(
                    [],
                    [],
                    producer="usb-monitor:producer-b",
                    boot_id="boot-a",
                ),
            )
            still_missing["status"]["interface"]["role_interfaces"]["roles"][
                "c-can"
            ].update(
                {
                    "resolution": "missing",
                    "safe": False,
                    "passive_ready": False,
                    "actual": {"present": False, "up": None},
                }
            )
            historian.ingest_snapshot(
                still_missing,
                captured_at=still_missing_at,
                ingest_key="restart-still-missing",
            )
            self.assertEqual(
                historian.usb_can_incident_summary()["active_count"], 1
            )

            healthy_at = self.start + timedelta(seconds=10)
            healthy = historian.ingest_snapshot(
                self.payload(
                    healthy_at,
                    self.batch(
                        [],
                        [],
                        producer="usb-monitor:producer-b",
                        boot_id="boot-a",
                    ),
                ),
                captured_at=healthy_at,
                ingest_key="restart-healthy",
            )
            summary = historian.usb_can_incident_summary()
            self.assertEqual(summary["active_count"], 0)
            self.assertEqual(summary["event_count"], 2)
            self.assertEqual(
                summary["recent_incidents"][0]["resolution"],
                "authoritative_healthy_exact_roles_after_monitor_restart",
            )
            assessment = next(
                item
                for item in InfrastructureHealthEvaluator(historian).evaluate(
                    healthy.snapshot_id, at=healthy_at
                )["assessments"]
                if item["rule"] == "usb_can_transient_disconnect"
            )
            self.assertEqual(assessment["state"], "normal")

    def test_monitor_payload_must_prove_no_hardware_action(self):
        removed = usb_event(
            self.start,
            "usb-can-event-v1:unsafe",
            "usb_can_adapter_removed",
            "remove",
        )
        removed["hardware_action"] = True
        with TelemetryHistorian(self.path) as historian:
            with self.assertRaisesRegex(
                SnapshotValidationError, "receive-only/no-action"
            ):
                historian.ingest_snapshot(
                    self.payload(self.start, self.batch([removed], [])),
                    captured_at=self.start,
                    ingest_key="unsafe",
                )
            self.assertEqual(
                historian._conn.execute("SELECT count(*) FROM snapshots").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
