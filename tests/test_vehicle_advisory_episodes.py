from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from projects.vehicle_data.early_warning import (
    DEFAULT_WARNING_RULES,
    AbsoluteOilPressureRule,
    EarlyWarningEvaluator,
    InfrastructureHealthEvaluator,
    WarningRule,
)
from projects.vehicle_data.historian import HistorianConfig, TelemetryHistorian
from projects.vehicle_data.insights import (
    AdvisoryNotificationDispatcher,
    TelemetryInsights,
)
from tests.test_vehicle_historian import (
    available,
    definition,
    role_aware_interface,
    role_status,
    snapshot,
    unavailable,
)


UTC = timezone.utc


def assessment(
    state: str,
    *,
    rule: str = "test_rule",
    eligible: bool = False,
    reason: str | None = None,
) -> dict[str, object]:
    return {
        "rule": rule,
        "title": "Test advisory",
        "metric": "engine.oil_pressure",
        "category": "vehicle_health",
        "severity": "warning",
        "state": state,
        "reason": reason or f"test {state}",
        "advisory": True,
        "notification_eligible": eligible,
        "notification_rate_limit_seconds": 1800,
        "regime": "engine_running:stationary:rpm_idle:warm",
        "current": {
            "value": 10.0,
            "unit": "psi",
            "source": "ccan.broadcast.0x41d",
            "bus": "c-can",
            "quality": "observed_alfa_scale",
            "provenance": "exact test provenance",
        },
    }


class AdvisoryEpisodePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.sqlite3"
        self.start = datetime(2026, 8, 21, 12, tzinfo=UTC)

    def test_open_escalate_inconclusive_acknowledge_and_resolve(self):
        with TelemetryHistorian(self.path) as historian:
            opened = historian.record_advisory_assessments(
                [assessment("watch")],
                evaluated_at=self.start,
            )
            self.assertEqual(opened.opened, 1)
            self.assertEqual(opened.notifications_enqueued, 0)

            escalated = historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start + timedelta(seconds=5),
            )
            self.assertEqual(escalated.updated, 1)
            self.assertEqual(escalated.notifications_enqueued, 1)
            episode = historian.list_advisory_episodes(active_only=True)[0]
            self.assertEqual(episode["state"], "warning")
            self.assertEqual(episode["evidence_state"], "warning")
            self.assertEqual(len(historian.pending_advisory_notifications()), 1)

            historian.record_advisory_assessments(
                [assessment("unavailable")],
                evaluated_at=self.start + timedelta(seconds=10),
            )
            episode = historian.list_advisory_episodes(active_only=True)[0]
            self.assertEqual(episode["state"], "warning")
            self.assertEqual(episode["evidence_state"], "unavailable")

            acknowledged = historian.acknowledge_advisory_episode(
                int(episode["id"]),
                acknowledged_at=self.start + timedelta(seconds=11),
                note="owner reviewed",
            )
            self.assertTrue(acknowledged["acknowledged"])
            self.assertFalse(historian.pending_advisory_notifications())

            resolved = historian.record_advisory_assessments(
                [assessment("normal")],
                evaluated_at=self.start + timedelta(seconds=15),
            )
            self.assertEqual(resolved.resolved, 1)
            self.assertFalse(historian.list_advisory_episodes(active_only=True))
            self.assertEqual(
                historian.list_advisory_episodes()[0]["state"],
                "normal",
            )
            events = historian.list_advisory_events(int(episode["id"]))
            self.assertEqual(
                {item["type"] for item in events},
                {
                    "opened",
                    "escalated",
                    "evidence_inconclusive",
                    "acknowledged",
                    "resolved",
                },
            )

    def test_outbox_rate_limit_spans_recurrent_episodes(self):
        with TelemetryHistorian(self.path) as historian:
            first = historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start,
            )
            self.assertEqual(first.notifications_enqueued, 1)
            notification = historian.pending_advisory_notifications(at=self.start)[0]
            historian.mark_advisory_notification_delivered(
                int(notification["id"]),
                delivered_at=self.start,
            )
            historian.record_advisory_assessments(
                [assessment("normal")],
                evaluated_at=self.start + timedelta(seconds=5),
            )
            recurrent = historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start + timedelta(minutes=5),
            )
            self.assertEqual(recurrent.opened, 1)
            self.assertEqual(recurrent.notifications_enqueued, 0)
            summary = historian.advisory_summary(now=self.start + timedelta(minutes=5))
            self.assertEqual(summary["notification_outbox"]["delivered"], 1)

    def test_authoritative_rule_catalog_retires_unseen_open_rule(self):
        with TelemetryHistorian(self.path) as historian:
            historian.record_advisory_assessments(
                [assessment("warning", rule="retired_rule", eligible=True)],
                evaluated_at=self.start,
            )
            # Partial/manual callers preserve historical semantics and cannot
            # retire rules merely by omission.
            historian.record_advisory_assessments(
                [],
                evaluated_at=self.start + timedelta(seconds=1),
            )
            self.assertEqual(
                historian.list_advisory_episodes(active_only=True)[0]["rule"],
                "retired_rule",
            )

            result = historian.record_advisory_assessments(
                [assessment("normal", rule="current_rule")],
                evaluated_at=self.start + timedelta(seconds=2),
                authoritative_rule_keys=("current_rule",),
            )

            self.assertEqual(result.resolved, 1)
            self.assertFalse(historian.list_advisory_episodes(active_only=True))
            retired = next(
                item
                for item in historian.list_advisory_episodes()
                if item["rule"] == "retired_rule"
            )
            self.assertEqual(
                retired["resolution_reason"],
                "rule retired from authoritative evaluator catalog",
            )
            self.assertEqual(retired["state"], "suppressed")
            self.assertIn(
                "rule_retired",
                {
                    item["type"]
                    for item in historian.list_advisory_events(int(retired["id"]))
                },
            )
            self.assertEqual(
                historian.advisory_summary()["notification_outbox"]["cancelled"],
                1,
            )

    def test_authoritative_rule_catalog_must_match_complete_assessment_set(self):
        with TelemetryHistorian(self.path) as historian:
            with self.assertRaisesRegex(ValueError, "exactly match"):
                historian.record_advisory_assessments(
                    [assessment("warning", rule="present")],
                    evaluated_at=self.start,
                    authoritative_rule_keys=("different",),
                )
            self.assertFalse(historian.list_advisory_episodes())

    def test_notification_dispatch_is_disabled_by_default(self):
        with TelemetryHistorian(self.path) as historian:
            historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start,
            )
            dispatcher = AdvisoryNotificationDispatcher(historian)
            status = dispatcher.dispatch(at=self.start)
            self.assertFalse(status["enabled"])
            self.assertEqual(status["last_error"], "delivery_disabled")
            self.assertEqual(len(historian.pending_advisory_notifications(at=self.start)), 1)

    def test_delivery_timeout_error_is_bounded_and_retry_remains_scheduled(self):
        class TimeoutSink:
            enabled = True

            def deliver(self, _payload):
                raise subprocess.TimeoutExpired(
                    ["ntfy-send", "x" * 1500],
                    5,
                )

        with TelemetryHistorian(self.path) as historian:
            historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start,
            )
            dispatcher = AdvisoryNotificationDispatcher(
                historian,
                sink=TimeoutSink(),
                enabled=True,
            )

            status = dispatcher.dispatch(at=self.start)

            self.assertEqual(status["last_failed"], 1)
            self.assertEqual(len(status["last_error"]), 1000)
            retry = historian.pending_advisory_notifications(
                at=self.start + timedelta(seconds=301)
            )[0]
            self.assertEqual(retry["attempt_count"], 1)
            self.assertEqual(len(retry["last_error"]), 1000)

    def test_warning_repeat_does_not_overtake_pending_retry(self):
        with TelemetryHistorian(self.path) as historian:
            first = historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start,
            )
            self.assertEqual(first.notifications_enqueued, 1)
            pending = historian.pending_advisory_notifications(at=self.start)[0]
            historian.mark_advisory_notification_failed(
                int(pending["id"]),
                error="temporary delivery failure",
                attempted_at=self.start,
                retry_after_seconds=300,
            )

            repeated = historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start + timedelta(minutes=30),
            )

            self.assertEqual(repeated.notifications_enqueued, 0)
            self.assertEqual(
                historian.advisory_summary()["notification_outbox"]["pending"],
                1,
            )

    def test_rate_limit_restarts_from_delayed_successful_delivery(self):
        with TelemetryHistorian(self.path) as historian:
            historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start,
            )
            notification = historian.pending_advisory_notifications(
                at=self.start
            )[0]
            historian.mark_advisory_notification_failed(
                int(notification["id"]),
                error="temporary delivery failure",
                attempted_at=self.start,
                retry_after_seconds=1800,
            )
            delivered_at = self.start + timedelta(minutes=30)
            historian.mark_advisory_notification_delivered(
                int(notification["id"]),
                delivered_at=delivered_at,
            )

            immediate = historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=delivered_at + timedelta(seconds=5),
            )
            self.assertEqual(immediate.notifications_enqueued, 0)
            later = historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=delivered_at + timedelta(minutes=30, seconds=1),
            )
            self.assertEqual(later.notifications_enqueued, 1)

    def test_terminal_failed_row_owns_episode_until_resolution(self):
        with TelemetryHistorian(self.path) as historian:
            historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start,
            )
            notification = historian.pending_advisory_notifications(
                at=self.start
            )[0]
            for attempt in range(3):
                historian.mark_advisory_notification_failed(
                    int(notification["id"]),
                    error=f"terminal fixture attempt {attempt + 1}",
                    attempted_at=self.start + timedelta(seconds=attempt),
                    retry_after_seconds=1,
                    max_attempts=3,
                )
            self.assertEqual(
                historian.advisory_summary()["notification_outbox"]["failed"],
                1,
            )

            repeated = historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start + timedelta(hours=1),
            )
            self.assertEqual(repeated.notifications_enqueued, 0)
            self.assertEqual(
                historian.advisory_summary()["notification_outbox"]["failed"],
                1,
            )

            historian.record_advisory_assessments(
                [assessment("normal")],
                evaluated_at=self.start + timedelta(hours=1, seconds=1),
            )
            recurrent = historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start + timedelta(hours=2),
            )
            self.assertEqual(recurrent.notifications_enqueued, 1)

    def test_dispatch_serializes_fetch_deliver_and_mark(self):
        class BlockingSink:
            enabled = True

            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.deliveries = 0
                self.lock = threading.Lock()

            def deliver(self, _payload):
                with self.lock:
                    self.deliveries += 1
                self.entered.set()
                if not self.release.wait(2):
                    raise RuntimeError("test delivery release timed out")

        with TelemetryHistorian(self.path) as historian:
            historian.record_advisory_assessments(
                [assessment("warning", eligible=True)],
                evaluated_at=self.start,
            )
            sink = BlockingSink()
            dispatcher = AdvisoryNotificationDispatcher(
                historian,
                sink=sink,
                enabled=True,
            )
            results = []
            errors = []

            def dispatch():
                try:
                    results.append(dispatcher.dispatch(at=self.start))
                except Exception as exc:  # pragma: no cover - assertion below
                    errors.append(exc)

            first = threading.Thread(target=dispatch)
            second = threading.Thread(target=dispatch)
            first.start()
            self.assertTrue(sink.entered.wait(1))
            second.start()
            sink.release.set()
            first.join(2)
            second.join(2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(sink.deliveries, 1)
            self.assertEqual(
                historian.advisory_summary()["notification_outbox"]["delivered"],
                1,
            )

    def test_insights_persists_assessments_on_ingest_but_not_duplicate_delivery(self):
        metric = definition("engine.rpm", "rpm")
        payload = snapshot(
            self.start,
            [metric],
            {"engine.rpm": available(metric, 0.0, self.start)},
            running=False,
        )

        class StaticVehicleEvaluator:
            def evaluate(self, **_kwargs):
                item = assessment("warning", eligible=True)
                return {"schema_version": 1, "active": [item], "assessments": [item]}

        class StaticInfrastructureEvaluator:
            def evaluate(self, _snapshot_id, **_kwargs):
                return {"schema_version": 1, "active": [], "assessments": []}

        historian = TelemetryHistorian(self.path)
        historian.record_advisory_assessments(
            [assessment("warning", rule="removed_from_evaluators", eligible=True)],
            evaluated_at=self.start - timedelta(seconds=1),
        )
        insights = TelemetryInsights(
            historian,
            warning_evaluator=StaticVehicleEvaluator(),
            infrastructure_evaluator=StaticInfrastructureEvaluator(),
            dtc_cache_path=Path(self.tmp.name) / "dtcs.json",
            history_metrics=("engine.rpm",),
        )
        self.addCleanup(insights.close)
        first = insights.ingest_snapshot(
            payload,
            captured_at=self.start,
            ingest_key="same",
        )
        duplicate = insights.ingest_snapshot(
            payload,
            captured_at=self.start + timedelta(seconds=1),
            ingest_key="same",
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(duplicate.duplicate)
        episode = historian.list_advisory_episodes(active_only=True)[0]
        self.assertEqual(episode["rule"], "test_rule")
        self.assertEqual(episode["observation_count"], 1)
        self.assertEqual(len(historian.pending_advisory_notifications(at=self.start)), 1)
        health = insights.health_response()
        self.assertTrue(health["available"])
        self.assertFalse(health["notification_delivery"]["enabled"])
        self.assertEqual(len(health["episodes"]["active"]), 1)
        retired = next(
            item
            for item in historian.list_advisory_episodes()
            if item["rule"] == "removed_from_evaluators"
        )
        self.assertEqual(
            retired["resolution_reason"],
            "rule retired from authoritative evaluator catalog",
        )


class AbsoluteAndPlausibilityWarningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.sqlite3"
        self.start = datetime(2026, 8, 21, 13, tzinfo=UTC)
        self.rpm = definition("engine.rpm", "rpm")
        self.speed = definition("vehicle.speed", "mph")
        self.coolant = definition("engine.coolant_temperature", "°F")
        self.oil = definition("engine.oil_pressure", "psi")
        self.rpm["sources"][0].update(
            name="ccan.broadcast.0x0fc",
            quality="observed_alfa_scale",
        )
        self.oil["sources"][0].update(
            name="ccan.broadcast.0x41d",
            quality="observed_alfa_scale",
        )
        self.transmission = definition("transmission.oil_temperature", "°F")
        self.battery = definition("battery.voltage", "V", stale=30)
        self.generator = definition("generator.field_duty", "%")
        self.definitions = [
            self.rpm,
            self.speed,
            self.coolant,
            self.oil,
            self.transmission,
            self.battery,
            self.generator,
        ]

    def values(
        self,
        at: datetime,
        *,
        rpm: float = 800,
        oil: float = 10,
        transmission: float = 180,
        battery: float = 14,
        generator: float = 30,
    ) -> dict[str, object]:
        return {
            "engine.rpm": available(self.rpm, rpm, at),
            "vehicle.speed": available(self.speed, 0.0, at),
            "engine.coolant_temperature": available(self.coolant, 195.0, at),
            "engine.oil_pressure": available(self.oil, oil, at),
            "transmission.oil_temperature": available(
                self.transmission,
                transmission,
                at,
            ),
            "battery.voltage": available(self.battery, battery, at),
            "generator.field_duty": available(self.generator, generator, at),
        }

    def test_absolute_oil_warning_requires_running_grace_and_post_grace_persistence(self):
        config = HistorianConfig(rollup_seconds=5)
        with TelemetryHistorian(self.path, config=config) as historian:
            evaluator = EarlyWarningEvaluator(
                historian,
                (AbsoluteOilPressureRule(),),
            )
            states = []
            for seconds in (0, 5, 10, 15):
                at = self.start + timedelta(seconds=seconds)
                historian.ingest_snapshot(
                    snapshot(at, self.definitions, self.values(at)),
                    captured_at=at,
                )
                states.append(evaluator.evaluate(at=at)["assessments"][0])
            self.assertEqual(
                [item["state"] for item in states],
                ["suppressed", "suppressed", "watch", "warning"],
            )
            warning = states[-1]
            self.assertTrue(warning["notification_eligible"])
            self.assertTrue(warning["running_evidence"]["qualified"])
            self.assertEqual(warning["absolute_threshold"]["value"], 12.0)
            self.assertEqual(warning["current"]["provenance"], "finding for engine.oil_pressure")

            stopped = self.start + timedelta(seconds=20)
            stopped_values = self.values(stopped, rpm=0)
            stopped_values["engine.oil_pressure"] = unavailable(self.oil)
            historian.ingest_snapshot(
                snapshot(
                    stopped,
                    self.definitions,
                    stopped_values,
                    running=False,
                ),
                captured_at=stopped,
            )
            self.assertEqual(
                evaluator.evaluate(at=stopped)["assessments"][0]["state"],
                "suppressed",
            )

    def test_absolute_oil_rule_requires_exact_pressure_and_rpm_sources(self):
        config = HistorianConfig(rollup_seconds=5)
        pressure_path = Path(self.tmp.name) / "alternate-pressure.sqlite3"
        with TelemetryHistorian(pressure_path, config=config) as historian:
            evaluator = EarlyWarningEvaluator(
                historian,
                (AbsoluteOilPressureRule(),),
            )
            for seconds in (0, 5, 10):
                at = self.start + timedelta(seconds=seconds)
                historian.ingest_snapshot(
                    snapshot(at, self.definitions, self.values(at)),
                    captured_at=at,
                )
            alternate_oil = definition("engine.oil_pressure", "psi")
            alternate_oil["sources"][0].update(
                name="candidate.oil-pressure",
                quality="candidate",
            )
            at = self.start + timedelta(seconds=15)
            definitions = [
                alternate_oil if item["name"] == alternate_oil["name"] else item
                for item in self.definitions
            ]
            values = self.values(at)
            values["engine.oil_pressure"] = available(
                alternate_oil,
                10.0,
                at,
            )
            historian.ingest_snapshot(
                snapshot(at, definitions, values),
                captured_at=at,
            )
            assessment = evaluator.evaluate(at=at)["assessments"][0]
            self.assertEqual(assessment["state"], "unavailable")
            self.assertIn("exact qualified 0x41D", assessment["reason"])

        rpm_path = Path(self.tmp.name) / "alternate-rpm.sqlite3"
        with TelemetryHistorian(rpm_path, config=config) as historian:
            alternate_rpm = definition("engine.rpm", "rpm")
            alternate_rpm["sources"][0].update(
                name="candidate.engine-speed",
                quality="candidate",
            )
            definitions = [
                alternate_rpm if item["name"] == alternate_rpm["name"] else item
                for item in self.definitions
            ]
            values = self.values(self.start)
            values["engine.rpm"] = available(
                alternate_rpm,
                800.0,
                self.start,
            )
            historian.ingest_snapshot(
                snapshot(self.start, definitions, values),
                captured_at=self.start,
            )
            assessment = EarlyWarningEvaluator(
                historian,
                (AbsoluteOilPressureRule(),),
            ).evaluate(at=self.start)["assessments"][0]
            self.assertEqual(assessment["state"], "unavailable")
            self.assertIn("exact qualified 0x0FC", assessment["reason"])

    def test_transmission_temperature_over_ten_c_in_under_one_second_is_rejected(self):
        rule = next(
            item
            for item in DEFAULT_WARNING_RULES
            if item.metric == "transmission.oil_temperature"
        )
        with TelemetryHistorian(self.path, config=HistorianConfig(rollup_seconds=5)) as historian:
            historian.ingest_snapshot(
                snapshot(
                    self.start,
                    self.definitions,
                    self.values(self.start, transmission=180.0),
                ),
                captured_at=self.start,
            )
            next_at = self.start + timedelta(milliseconds=500)
            historian.ingest_snapshot(
                snapshot(
                    next_at,
                    self.definitions,
                    self.values(next_at, transmission=200.0),
                ),
                captured_at=next_at,
            )
            result = EarlyWarningEvaluator(historian, (rule,)).evaluate(at=next_at)
            rejected = result["assessments"][0]
            self.assertEqual(rejected["state"], "rejected")
            self.assertGreater(
                abs(rejected["plausibility"]["delta_c"]),
                10,
            )
            self.assertEqual(
                rejected["plausibility"]["maximum_delta_window_seconds"],
                1.0,
            )
            self.assertFalse(result["active"])

    def test_transmission_temperature_delta_at_one_second_is_not_rejected(self):
        rule = next(
            item
            for item in DEFAULT_WARNING_RULES
            if item.metric == "transmission.oil_temperature"
        )
        with TelemetryHistorian(
            self.path,
            config=HistorianConfig(rollup_seconds=5),
        ) as historian:
            historian.ingest_snapshot(
                snapshot(
                    self.start,
                    self.definitions,
                    self.values(self.start, transmission=180.0),
                ),
                captured_at=self.start,
            )
            next_at = self.start + timedelta(seconds=1)
            historian.ingest_snapshot(
                snapshot(
                    next_at,
                    self.definitions,
                    self.values(next_at, transmission=200.0),
                ),
                captured_at=next_at,
            )
            assessment = EarlyWarningEvaluator(
                historian,
                (rule,),
            ).evaluate(at=next_at)["assessments"][0]
            self.assertNotEqual(assessment["state"], "rejected")

    def test_generator_duty_cannot_be_primary_and_high_duty_alone_is_normal(self):
        with self.assertRaisesRegex(ValueError, "cannot be a primary"):
            WarningRule(
                key="bad_generator_rule",
                title="Bad generator rule",
                metric="generator.field_duty",
                direction="high",
            )

        config = HistorianConfig(trip_idle_timeout_seconds=15, rollup_seconds=5)
        with TelemetryHistorian(self.path, config=config) as historian:
            for trip_index in range(3):
                start = self.start + timedelta(seconds=trip_index * 60)
                for point in range(3):
                    at = start + timedelta(seconds=point * 5)
                    historian.ingest_snapshot(
                        snapshot(at, self.definitions, self.values(at)),
                        captured_at=at,
                    )
                idle = start + timedelta(seconds=20)
                historian.ingest_snapshot(
                    snapshot(
                        idle,
                        self.definitions,
                        {
                            item["name"]: unavailable(item)
                            for item in self.definitions
                        },
                        running=False,
                    ),
                    captured_at=idle,
                )
            current = self.start + timedelta(seconds=200)
            for point in range(3):
                at = current + timedelta(seconds=point * 5)
                historian.ingest_snapshot(
                    snapshot(
                        at,
                        self.definitions,
                        self.values(at, battery=14.0, generator=91.0),
                    ),
                    captured_at=at,
                )
            rule = WarningRule(
                key="battery_primary",
                title="Battery primary",
                metric="battery.voltage",
                direction="low",
                minimum_effect=0.1,
                minimum_baseline_buckets=6,
                minimum_baseline_trips=3,
                persistence_observations=2,
            )
            result = EarlyWarningEvaluator(historian, (rule,)).evaluate(
                at=current + timedelta(seconds=10)
            )
            self.assertEqual(result["assessments"][0]["state"], "normal")
            self.assertFalse(result["active"])


class InfrastructureHealthEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.sqlite3"
        self.start = datetime(2026, 8, 21, 14, tzinfo=UTC)
        self.rpm = definition("engine.rpm", "rpm")
        self.definitions = [self.rpm]

    def payload(self, at: datetime, *, generation: str, missing_ccan=False):
        payload = snapshot(
            at,
            self.definitions,
            {"engine.rpm": available(self.rpm, 0.0, at)},
            running=False,
        )
        roles = {
            "c-can": role_status("c-can", "can0", 500000),
            "b-can": role_status("b-can", "can1", 125000),
            "can-ch": role_status("can-ch", "can2", 500000),
            "spare": role_status("spare", "can3", None, passive_required=False),
        }
        if missing_ccan:
            roles["c-can"].update(
                {
                    "resolution": "missing",
                    "channel": None,
                    "actual": {
                        "up": None,
                        "bitrate": None,
                        "listen_only": None,
                        "controller_state": None,
                    },
                    "passive_ready": False,
                    "reason": "role_missing",
                    "detail": "test adapter missing",
                }
            )
        interface = role_aware_interface(roles=roles)
        interface["role_interfaces"]["generation"] = generation
        payload["status"]["interface"] = interface
        payload["status"]["active_drive"] = {"restoration_failed": False}
        return payload

    def test_missing_role_persists_and_topology_change_is_recorded(self):
        with TelemetryHistorian(self.path) as historian:
            evaluator = InfrastructureHealthEvaluator(historian)
            first = historian.ingest_snapshot(
                self.payload(self.start, generation="generation-a"),
                captured_at=self.start,
            )
            initial = evaluator.evaluate(first.snapshot_id, at=self.start)
            self.assertFalse(initial["active"])

            missing_at = self.start + timedelta(seconds=5)
            missing = historian.ingest_snapshot(
                self.payload(
                    missing_at,
                    generation="generation-b",
                    missing_ccan=True,
                ),
                captured_at=missing_at,
            )
            first_loss = evaluator.evaluate(missing.snapshot_id, at=missing_at)
            ccan = next(
                item
                for item in first_loss["assessments"]
                if item["rule"] == "can_interface_role_c_can"
            )
            topology = next(
                item
                for item in first_loss["assessments"]
                if item["rule"] == "usb_can_topology_generation_changed"
            )
            self.assertEqual(ccan["state"], "watch")
            self.assertEqual(topology["state"], "warning")
            self.assertTrue(topology["notification_eligible"])

            repeated_at = self.start + timedelta(seconds=10)
            repeated = historian.ingest_snapshot(
                self.payload(
                    repeated_at,
                    generation="generation-b",
                    missing_ccan=True,
                ),
                captured_at=repeated_at,
            )
            repeated_loss = evaluator.evaluate(repeated.snapshot_id, at=repeated_at)
            ccan = next(
                item
                for item in repeated_loss["assessments"]
                if item["rule"] == "can_interface_role_c_can"
            )
            topology = next(
                item
                for item in repeated_loss["assessments"]
                if item["rule"] == "usb_can_topology_generation_changed"
            )
            self.assertEqual(ccan["state"], "warning")
            self.assertTrue(ccan["notification_eligible"])
            self.assertEqual(topology["state"], "normal")

    def test_unknown_to_first_authoritative_topology_is_not_a_change(self):
        with TelemetryHistorian(self.path) as historian:
            unknown = self.payload(self.start, generation=None)
            historian.ingest_snapshot(
                unknown,
                captured_at=self.start,
                ingest_key="topology-unknown",
            )
            authoritative_at = self.start + timedelta(seconds=5)
            authoritative = historian.ingest_snapshot(
                self.payload(authoritative_at, generation="generation-a"),
                captured_at=authoritative_at,
                ingest_key="topology-authoritative",
            )

            context = historian.system_health_context(authoritative.snapshot_id)
            self.assertFalse(context["topology_changed"])
            topology = next(
                item
                for item in InfrastructureHealthEvaluator(historian).evaluate(
                    authoritative.snapshot_id,
                    at=authoritative_at,
                )["assessments"]
                if item["rule"] == "usb_can_topology_generation_changed"
            )
            self.assertEqual(topology["state"], "normal")
            self.assertFalse(topology["notification_eligible"])

    def test_restoration_inhibit_is_critical_and_does_not_reset_hardware(self):
        payload = self.payload(self.start, generation="generation-a")
        payload["status"]["active_drive"]["restoration_failed"] = True
        payload["status"]["interface"]["active_inhibits"] = [
            "restoration_failed:c-can"
        ]
        with TelemetryHistorian(self.path) as historian:
            result = historian.ingest_snapshot(payload, captured_at=self.start)
            report = InfrastructureHealthEvaluator(historian).evaluate(
                result.snapshot_id,
                at=self.start,
            )
            restoration = next(
                item
                for item in report["assessments"]
                if item["rule"] == "can_restoration_inhibit"
            )
            self.assertEqual(restoration["state"], "warning")
            self.assertEqual(restoration["severity"], "critical")
            self.assertTrue(restoration["notification_eligible"])
            self.assertFalse(report["method"]["automatic_hardware_reset"])


if __name__ == "__main__":
    unittest.main()
