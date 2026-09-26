"""Tiered warning delivery and lifecycle (warnings-redesign.md sections 6-7).

Synthetic assessments only; no evaluator, CAN, or service involvement.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zoneinfo

from projects.vehicle_data import notifications
from projects.vehicle_data.event_history import detail
from projects.vehicle_data.historian import TelemetryHistorian
from projects.vehicle_data.insights import AdvisoryNotificationDispatcher
from projects.vehicle_data.notifications import (
    FORBIDDEN_WORDS_RE,
    NtfyAdvisoryNotificationSink,
    local_zone,
    quiet_hours_deferral,
    render_message,
)
from tests.test_vehicle_advisory_episodes import assessment


UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MOUNTAIN = zoneinfo.ZoneInfo("US/Mountain")
# 10:00 MDT: outside quiet hours.
DAY = datetime(2026, 9, 24, 16, tzinfo=UTC)
# 23:10 MDT on 2026-09-24; the next 07:30 MDT is 2026-09-25T13:30Z.
NIGHT = datetime(2026, 9, 25, 5, 10, tzinfo=UTC)
DIGEST = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
LEGACY_PAYLOAD_KEYS = {
    "schema_version", "advisory", "episode_id", "rule", "category", "title",
    "state", "reason", "opened_at", "evaluated_at", "assessment",
}


def us(moment: datetime) -> int:
    return (moment - EPOCH) // timedelta(microseconds=1)


def tiered(
    state: str,
    *,
    at: datetime,
    rule: str = "tire_pressure_relative_low",
    title: str = "RR tire low",
    tier: int = 1,
    group: str | None = "tires",
    severity: str = "warning",
    action: str = "Check at next stop.",
    value: float = 74.0,
    unit: str = "psi",
    eligible: bool | None = None,
    **extra,
) -> dict[str, object]:
    item = assessment(
        state,
        rule=rule,
        eligible=(state == "warning") if eligible is None else eligible,
    )
    item.update(
        title=title,
        tier=tier,
        group=group,
        severity=severity,
        action=action,
        confidence="medium",
        rule_snapshot={
            "max_age_seconds": 10,
            "recovery_seconds": 600,
            "deescalate_fraction": 0.7,
        },
    )
    item["current"].update(
        value=value,
        unit=unit,
        observed_at=at.isoformat(),
        captured_at=at.isoformat(),
        trip_id=None,
    )
    item.update(extra)
    return item


def oil_critical(state: str, *, at: datetime, value: float = 9.0) -> dict[str, object]:
    return tiered(
        state,
        at=at,
        rule="engine_oil_pressure_absolute_critical",
        title="Oil pressure critical",
        tier=0,
        group="oil",
        severity="critical",
        action="Pull over and shut off.",
        value=value,
    )


class TieredDeliveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.historian = TelemetryHistorian(Path(self.tmp.name) / "history.sqlite3")
        self.addCleanup(self.historian.close)
        zone = patch.object(notifications, "QUIET_HOURS_ZONE", "US/Mountain")
        zone.start()
        self.addCleanup(zone.stop)

    def record(self, *items, at: datetime):
        return self.historian.record_advisory_assessments(list(items), evaluated_at=at)

    def rows(self, where: str = "1=1", args=()):
        return self.historian._conn.execute(
            f"SELECT * FROM advisory_notification_outbox WHERE {where} ORDER BY id",
            args,
        ).fetchall()

    def payload(self, row) -> dict[str, object]:
        return json.loads(row["payload_json"])

    def event_types(self, episode_id: int) -> list[str]:
        return [
            event["type"]
            for event in self.historian.list_advisory_events(episode_id, limit=500)
        ]

    def open_trip(self, started: datetime) -> None:
        with self.historian._conn:
            self.historian._conn.execute(
                """
                INSERT INTO trips(started_us,started_at,last_active_us,last_active_at,
                    ended_us,ended_at,start_basis,end_reason,snapshot_count)
                VALUES(?,?,?,?,NULL,NULL,'test',NULL,0)
                """,
                (us(started), started.isoformat(), us(started), started.isoformat()),
            )

    def end_trips(self, ended: datetime) -> None:
        with self.historian._conn:
            self.historian._conn.execute(
                "UPDATE trips SET ended_us=?,ended_at=?,end_reason='test' WHERE ended_us IS NULL",
                (us(ended), ended.isoformat()),
            )

    def deliver_pending(self, at: datetime) -> int:
        count = 0
        for row in self.historian.pending_advisory_notifications(at=at, limit=100):
            self.historian.mark_advisory_notification_delivered(int(row["id"]), delivered_at=at)
            count += 1
        return count

    def hold_normal(self, make, start: datetime, *, seconds: int = 600, step: int = 10):
        results = []
        for offset in range(0, seconds + 1, step):
            at = start + timedelta(seconds=offset)
            results.append(self.record(make(at), at=at))
        return results

    def episode(self, rule: str):
        return self.historian._conn.execute(
            "SELECT * FROM advisory_episodes WHERE rule_key=? ORDER BY id DESC LIMIT 1",
            (rule,),
        ).fetchone()


class GroupCooldownAndRepeatTests(TieredDeliveryBase):
    def test_group_dedupe_spans_rules_until_cooldown_expires(self):
        first = self.record(tiered("warning", at=DAY), at=DAY)
        self.assertEqual(first.notifications_enqueued, 1)
        later = DAY + timedelta(minutes=1)
        # Another same-urgency (tier-1) rule in the group is deduplicated.
        second = self.record(
            tiered("warning", at=later, rule="tire_pressure_custom_low",
                   title="Tire pressure low"),
            at=later,
        )
        self.assertEqual(second.notifications_enqueued, 0)
        self.assertEqual(len(self.rows()), 1)
        # Another group is independent.
        cooling_at = DAY + timedelta(minutes=2)
        cooling = self.record(
            tiered("warning", at=cooling_at, rule="engine_coolant_temperature_hot", tier=0,
                   group="cooling", title="Coolant hot", value=232, unit="°F"),
            at=cooling_at,
        )
        self.assertEqual(cooling.notifications_enqueued, 1)
        third_at = DAY + timedelta(hours=7)
        third = self.record(
            tiered("warning", at=third_at, rule="tire_pressure_low_absolute", tier=0,
                   title="FL tire low"),
            at=third_at,
        )
        self.assertEqual(third.notifications_enqueued, 1)
        tire_rows = [row for row in self.rows() if self.payload(row)["group"] == "tires"]
        self.assertEqual(len(tire_rows), 2)
        payload = self.payload(tire_rows[0])
        for key in ("tier", "group", "severity", "action", "confidence",
                    "notification_kind", "deferred_until", "trip_id"):
            self.assertIn(key, payload)
        self.assertEqual(payload["notification_kind"], "warning")
        self.assertIsNone(payload["deferred_until"])
        self.assertTrue(LEGACY_PAYLOAD_KEYS <= set(payload))

    def test_tier1_warning_pushes_once_per_episode_without_reminders(self):
        self.record(tiered("warning", at=DAY), at=DAY)
        self.deliver_pending(DAY)
        for minutes in range(10, 7 * 60 + 1, 10):
            at = DAY + timedelta(minutes=minutes)
            result = self.record(tiered("warning", at=at), at=at)
            self.assertEqual(result.notifications_enqueued, 0)
            if minutes == 180:
                self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.rows()), 1)
        episode = self.episode("tire_pressure_relative_low")
        types = self.event_types(int(episode["id"]))
        self.assertNotIn("notification_repeat_due", types)
        self.assertNotIn("notification_due", types)

    def test_tier0_critical_repeats_every_five_minutes_after_delivery(self):
        self.record(oil_critical("warning", at=DAY), at=DAY)
        self.assertEqual(len(self.rows()), 1)
        self.deliver_pending(DAY)
        soon = DAY + timedelta(seconds=5)
        self.assertEqual(self.record(oil_critical("warning", at=soon), at=soon).notifications_enqueued, 0)
        repeat_at = DAY + timedelta(seconds=300)
        self.assertEqual(
            self.record(oil_critical("warning", at=repeat_at), at=repeat_at).notifications_enqueued,
            1,
        )
        second = self.payload(self.rows()[-1])
        self.assertEqual(second["notification_kind"], "repeat")
        self.assertEqual(second["severity"], "critical")
        self.assertIsNone(second["deferred_until"])
        self.deliver_pending(repeat_at)
        third_at = DAY + timedelta(seconds=600)
        self.assertEqual(
            self.record(oil_critical("warning", at=third_at), at=third_at).notifications_enqueued,
            1,
        )
        # An undelivered row still owns the episode: no repeat overtakes it.
        blocked_at = DAY + timedelta(seconds=900)
        self.assertEqual(
            self.record(oil_critical("warning", at=blocked_at), at=blocked_at).notifications_enqueued,
            0,
        )
        self.assertEqual(len(self.rows()), 3)
        episode = self.episode("engine_oil_pressure_absolute_critical")
        self.assertEqual(self.event_types(int(episode["id"])).count("notification_repeat_due"), 2)

    def test_first_push_after_group_cooldown_records_a_due_event(self):
        self.record(tiered("warning", at=DAY), at=DAY)
        other = "tire_pressure_pair_asymmetry"
        self.record(tiered("warning", at=DAY + timedelta(minutes=1), rule=other), at=DAY + timedelta(minutes=1))
        late = DAY + timedelta(hours=6, minutes=1)
        result = self.record(tiered("warning", at=late, rule=other), at=late)
        self.assertEqual(result.notifications_enqueued, 1)
        episode = self.episode(other)
        self.assertIn("notification_due", self.event_types(int(episode["id"])))
        self.assertEqual(self.payload(self.rows()[-1])["notification_kind"], "warning")


def coolant_hot(state: str, *, at: datetime, severity: str = "warning",
                value: float = 232.0) -> dict[str, object]:
    return tiered(
        state, at=at, rule="engine_coolant_temperature_hot", tier=0, group="cooling",
        title="Coolant hot", severity=severity, value=value, unit="°F",
    )


def coolant_relative(state: str, *, at: datetime) -> dict[str, object]:
    return tiered(
        state, at=at, rule="engine_coolant_temperature_relative_high", tier=1,
        group="cooling", title="Coolant hotter than usual", value=216.0, unit="°F",
    )


class EscalationDeliveryTests(TieredDeliveryBase):
    """A lower-urgency push in a group never swallows or delays a higher one."""

    def test_tier0_warning_after_a_tier1_push_in_the_group_still_pushes(self):
        self.record(coolant_relative("warning", at=DAY), at=DAY)
        self.deliver_pending(DAY)
        later = DAY + timedelta(hours=1)
        result = self.record(coolant_hot("warning", at=later), at=later)
        self.assertEqual(result.notifications_enqueued, 1)
        # A second tier-1 item in the group is still held by the tier-0 push.
        again = later + timedelta(minutes=5)
        other = tiered("warning", at=again, rule="coolant_custom_high", group="cooling",
                       title="Coolant high", value=216.0, unit="°F")
        self.assertEqual(self.record(other, at=again).notifications_enqueued, 0)

    def test_tier0_warning_at_night_is_not_held_by_a_deferred_tier1_row(self):
        self.record(coolant_relative("warning", at=NIGHT), at=NIGHT)
        later = NIGHT + timedelta(minutes=5)
        result = self.record(coolant_hot("warning", at=later), at=later)
        self.assertEqual(result.notifications_enqueued, 1)
        pending = self.historian.pending_advisory_notifications(at=later)
        self.assertEqual([item["rule"] for item in pending], ["engine_coolant_temperature_hot"])

    def test_critical_escalation_pushes_immediately_after_a_warning_push(self):
        self.record(coolant_hot("warning", at=DAY), at=DAY)
        self.deliver_pending(DAY)
        later = DAY + timedelta(seconds=60)
        result = self.record(
            coolant_hot("warning", at=later, severity="critical", value=241.0), at=later
        )
        self.assertEqual(result.notifications_enqueued, 1)
        payload = self.payload(self.rows()[-1])
        self.assertEqual(payload["severity"], "critical")
        self.assertEqual(payload["notification_kind"], "warning")  # first critical push
        repeat_at = later + timedelta(seconds=300)
        self.deliver_pending(later)
        self.assertEqual(
            self.record(coolant_hot("warning", at=repeat_at, severity="critical", value=241.0),
                        at=repeat_at).notifications_enqueued,
            1,
        )
        self.assertEqual(self.payload(self.rows()[-1])["notification_kind"], "repeat")

    def test_critical_supersedes_a_pending_warning_and_survives_a_failed_row(self):
        self.record(coolant_hot("warning", at=DAY), at=DAY)  # sink down: stays pending
        later = DAY + timedelta(seconds=30)
        result = self.record(
            coolant_hot("warning", at=later, severity="critical", value=241.0), at=later
        )
        self.assertEqual(result.notifications_enqueued, 1)
        rows = self.rows()
        self.assertEqual([row["status"] for row in rows], ["cancelled", "pending"])
        self.assertEqual(rows[0]["last_error"], "superseded by a critical notification")
        critical_id = int(rows[1]["id"])
        for attempt in range(3):
            self.historian.mark_advisory_notification_failed(
                critical_id, error="offline", attempted_at=later + timedelta(seconds=attempt)
            )
        self.assertEqual(self.rows("id=?", (critical_id,))[0]["status"], "failed")
        resumed = later + timedelta(seconds=310)
        self.assertEqual(
            self.record(coolant_hot("warning", at=resumed, severity="critical", value=241.0),
                        at=resumed).notifications_enqueued,
            1,
        )

    def test_recurrence_after_a_recovery_push_pushes_again(self):
        self.record(coolant_hot("warning", at=DAY), at=DAY)
        self.deliver_pending(DAY)
        self.hold_normal(lambda at: coolant_hot("normal", at=at, value=200.0),
                         DAY + timedelta(seconds=10))
        kinds = [self.payload(row)["notification_kind"] for row in self.rows()]
        self.assertEqual(kinds, ["warning", "recovery"])
        again = DAY + timedelta(minutes=30)
        self.assertEqual(
            self.record(coolant_hot("warning", at=again), at=again).notifications_enqueued, 1
        )

    def test_tier0_warning_is_not_capped_by_earlier_pushes_this_trip(self):
        self.open_trip(DAY - timedelta(minutes=1))
        for index, (rule, group) in enumerate(
            (("tire_pressure_relative_low", "tires"),
             ("transmission_oil_temperature_relative_high", "transmission"),
             ("battery_voltage_relative_low", "charging"))
        ):
            at = DAY + timedelta(seconds=index)
            self.record(tiered("warning", at=at, rule=rule, group=group), at=at)
        at = DAY + timedelta(minutes=5)
        result = self.record(
            tiered("warning", at=at, rule="engine_oil_pressure_below_band", tier=0,
                   group="oil", title="Oil pressure low", value=13.0),
            at=at,
        )
        self.assertEqual((result.notifications_enqueued, result.notifications_capped), (1, 0))


class TripCapTests(TieredDeliveryBase):
    def test_three_noncritical_pushes_per_open_trip(self):
        self.open_trip(DAY - timedelta(minutes=1))
        specs = (
            ("tire_pressure_relative_low", "tires"),
            ("engine_coolant_temperature_relative_high", "cooling"),
            ("transmission_oil_temperature_relative_high", "transmission"),
        )
        for index, (rule, group) in enumerate(specs):
            at = DAY + timedelta(seconds=index)
            self.assertEqual(
                self.record(tiered("warning", at=at, rule=rule, group=group), at=at).notifications_enqueued,
                1,
            )
        at = DAY + timedelta(seconds=10)
        capped = self.record(
            tiered("warning", at=at, rule="battery_voltage_relative_low", group="charging"),
            at=at,
        )
        self.assertEqual(capped.notifications_enqueued, 0)
        self.assertEqual(capped.notifications_capped, 1)
        critical_at = DAY + timedelta(seconds=15)
        critical = self.record(oil_critical("warning", at=critical_at), at=critical_at)
        self.assertEqual(critical.notifications_enqueued, 1)
        self.assertEqual(critical.notifications_capped, 0)
        self.assertEqual(len(self.rows()), 4)

        self.end_trips(DAY + timedelta(minutes=5))
        parked = DAY + timedelta(minutes=10)
        uncapped = self.record(
            tiered("warning", at=parked, rule="battery_voltage_low_parked", tier=0,
                   group="battery", title="Battery low", value=11.6, unit="V"),
            at=parked,
        )
        self.assertEqual(uncapped.notifications_enqueued, 1)
        self.assertEqual(uncapped.notifications_capped, 0)

    def test_result_counters_default_for_legacy_consumers(self):
        result = self.record(assessment("warning", eligible=True), at=DAY)
        self.assertEqual(result.as_dict()["notifications_capped"], 0)
        self.assertEqual(result.as_dict()["notifications_deferred"], 0)


class QuietHoursTests(TieredDeliveryBase):
    def test_tier1_is_deferred_to_the_morning_digest(self):
        result = self.record(tiered("warning", at=NIGHT), at=NIGHT)
        self.assertEqual(result.notifications_enqueued, 1)
        self.assertEqual(result.notifications_deferred, 1)
        row = self.rows()[0]
        self.assertEqual(row["eligible_after_us"], us(DIGEST))
        self.assertEqual(self.payload(row)["deferred_until"], DIGEST.isoformat())
        six_local = datetime(2026, 9, 25, 12, tzinfo=UTC)
        self.assertEqual(self.historian.pending_advisory_notifications(at=six_local), [])
        after = DIGEST + timedelta(minutes=1)
        self.assertEqual(len(self.historian.pending_advisory_notifications(at=after)), 1)

    def test_tier1_during_an_open_trip_is_not_deferred(self):
        # Trips 50-55 (2026-09-21 local) ran until 23:27 MDT: a tier-1 warning
        # raised while driving must reach the driver now, not at 07:30 after
        # the drive (or never, if the episode resolves overnight).
        self.open_trip(NIGHT - timedelta(minutes=20))
        result = self.record(tiered("warning", at=NIGHT), at=NIGHT)
        self.assertEqual((result.notifications_enqueued, result.notifications_deferred), (1, 0))
        row = self.rows()[0]
        self.assertEqual(row["eligible_after_us"], us(NIGHT))
        self.assertIsNone(self.payload(row)["deferred_until"])
        self.assertEqual(len(self.historian.pending_advisory_notifications(at=NIGHT)), 1)

    def test_parked_tier2_still_waits_for_the_digest_during_a_trip(self):
        # The in-trip exemption is tier 1 only.
        self.open_trip(NIGHT - timedelta(minutes=20))
        self.record(tiered("warning", at=NIGHT, tier=2, rule="tire_pressure_slow_leak",
                           group="tires"), at=NIGHT)
        self.assertEqual(self.rows()[0]["eligible_after_us"], us(DIGEST))

    # The tier-3 cases below feed synthetic *eligible* System items to the
    # historian.  Producers never mark them eligible (owner decision
    # 2026-09-24: System notes only), and the historian refuses them anyway,
    # so a producer mistake cannot push; see also
    # test_interface_health_events.test_system_items_never_enqueue_a_notification.
    def test_tier0_and_restoration_inhibit_are_immediate_at_night(self):
        self.record(
            tiered("warning", at=NIGHT, rule="engine_coolant_temperature_hot", tier=0,
                   group="cooling", title="Coolant hot", value=232, unit="°F"),
            oil_critical("warning", at=NIGHT),
            tiered("warning", at=NIGHT, rule="can_restoration_inhibit", tier=3,
                   group="system", severity="critical", title="CAN restoration blocked",
                   category="can_infrastructure", action="Check the adapters."),
            at=NIGHT,
        )
        pending = self.historian.pending_advisory_notifications(at=NIGHT)
        self.assertEqual(
            {item["rule"] for item in pending},
            {"engine_coolant_temperature_hot", "engine_oil_pressure_absolute_critical"},
        )
        self.assertTrue(all(item["payload"]["deferred_until"] is None for item in pending))

    def test_eligible_system_items_never_enqueue(self):
        self.record(
            tiered("warning", at=NIGHT, rule="can_interface_role_c_can", tier=3,
                   group="system", title="Telemetry offline", category="can_infrastructure"),
            # An untiered (legacy-path) item in a System category is refused too.
            {**tiered("warning", at=NIGHT, rule="telemetry_gap_engine_rpm",
                      category="telemetry_quality"), "tier": None},
            at=NIGHT,
        )
        self.assertEqual(len(self.historian.list_advisory_episodes(active_only=True)), 2)
        self.assertEqual(self.rows(), [])

    def test_deferred_row_is_cancelled_when_resolved_before_delivery(self):
        self.record(tiered("warning", at=NIGHT), at=NIGHT)
        self.hold_normal(lambda at: tiered("normal", at=at), NIGHT + timedelta(minutes=10))
        episode = self.episode("tire_pressure_relative_low")
        self.assertEqual(episode["status"], "resolved")
        row = self.rows()[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["last_error"], "episode resolved before delivery")
        self.assertEqual(
            self.historian.pending_advisory_notifications(at=DIGEST + timedelta(minutes=1)),
            [],
        )


class RecoveryNotificationTests(TieredDeliveryBase):
    def test_delivered_tier0_item_sends_one_recovery(self):
        self.record(oil_critical("warning", at=DAY), at=DAY)
        self.deliver_pending(DAY)
        results = self.hold_normal(
            lambda at: oil_critical("normal", at=at, value=31.0),
            DAY + timedelta(seconds=10),
        )
        self.assertEqual(results[-1].resolved, 1)
        self.assertEqual(results[-1].notifications_enqueued, 1)
        self.assertTrue(all(item.notifications_enqueued == 0 for item in results[:-1]))
        episode = self.episode("engine_oil_pressure_absolute_critical")
        self.assertEqual(episode["status"], "resolved")
        rows = self.rows()
        self.assertEqual([row["status"] for row in rows], ["delivered", "pending"])
        recovery = self.payload(rows[-1])
        self.assertEqual(recovery["notification_kind"], "recovery")
        self.assertEqual(recovery["state"], "normal")
        self.assertEqual(recovery["tier"], 0)
        resolved_event = self.historian._conn.execute(
            "SELECT id FROM advisory_episode_events WHERE episode_id=? AND event_type='resolved'",
            (episode["id"],),
        ).fetchone()
        self.assertEqual(rows[-1]["event_id"], resolved_event["id"])
        at = DAY + timedelta(minutes=11)
        pending = self.historian.pending_advisory_notifications(at=at)
        self.assertEqual([item["id"] for item in pending], [rows[-1]["id"]])

        delivered = []

        class Sink:
            enabled = True

            def deliver(self, payload):
                delivered.append(render_message(payload))

        status = AdvisoryNotificationDispatcher(self.historian, sink=Sink(), enabled=True).dispatch(at=at)
        self.assertEqual(status["last_delivered"], 1)
        self.assertEqual(delivered[0][0], "Oil pressure normal")
        self.assertEqual(delivered[0][1], "31 psi at 10:10.")

    def test_tier1_and_undelivered_tier0_recoveries_send_nothing(self):
        self.record(tiered("warning", at=DAY), at=DAY)
        self.deliver_pending(DAY)
        self.hold_normal(lambda at: tiered("normal", at=at), DAY + timedelta(seconds=10))
        self.assertEqual(len(self.rows()), 1)

        later = DAY + timedelta(hours=1)
        self.record(oil_critical("warning", at=later), at=later)
        self.hold_normal(lambda at: oil_critical("normal", at=at, value=31.0), later + timedelta(seconds=10))
        oil_rows = self.rows("rule_key='engine_oil_pressure_absolute_critical'")
        self.assertEqual([row["status"] for row in oil_rows], ["cancelled"])


class TimedRecoveryTests(TieredDeliveryBase):
    def normal(self, at: datetime, *, effect: float = 1.0, regime: str = "B"):
        return tiered(
            "normal",
            at=at,
            regime=f"engine_running:{regime}",
            baseline_regime=regime,
            deviation={"effect_in_rule_direction": effect, "threshold": 5.0},
        )

    def test_any_regime_counts_and_ten_minutes_are_required(self):
        self.record(tiered("warning", at=DAY, baseline_regime="A",
                           deviation={"effect_in_rule_direction": 6.0, "threshold": 5.0}), at=DAY)
        start = DAY + timedelta(seconds=10)
        self.hold_normal(self.normal, start, seconds=540)
        episode = self.episode("tire_pressure_relative_low")
        self.assertEqual(episode["status"], "open")
        self.assertEqual(episode["evidence_state"], "recovering")
        self.assertEqual(episode["current_state"], "warning")
        code, packet = detail(self.historian._conn, int(episode["id"]))
        recovery = packet["event"]["evidence"]["recovery"]
        self.assertEqual(recovery["held_seconds"], 540)
        self.assertEqual(recovery["required_seconds"], 600)
        self.assertTrue(recovery["comparable"])
        self.assertAlmostEqual(recovery["hysteresis_fraction"], 0.3)
        results = self.hold_normal(self.normal, start + timedelta(seconds=550), seconds=50)
        self.assertEqual([item.resolved for item in results], [0, 0, 0, 0, 0, 1])
        self.assertEqual(self.episode("tire_pressure_relative_low")["status"], "resolved")

    def test_gap_and_unsafe_margin_restart_the_clock(self):
        self.record(tiered("warning", at=DAY), at=DAY)
        start = DAY + timedelta(seconds=10)
        self.hold_normal(self.normal, start, seconds=300)
        after_gap = start + timedelta(seconds=330)  # 30 s gap > 2 x 10 s
        self.hold_normal(self.normal, after_gap, seconds=300)
        episode = self.episode("tire_pressure_relative_low")
        self.assertEqual(episode["status"], "open")
        recovery = detail(self.historian._conn, int(episode["id"]))[1]["event"]["evidence"]["recovery"]
        self.assertEqual(recovery["first_normal_at"], after_gap.isoformat())
        unsafe = after_gap + timedelta(seconds=310)
        self.record(self.normal(unsafe, effect=4.0), at=unsafe)  # 4.0 > 0.7 x 5.0
        self.hold_normal(self.normal, unsafe + timedelta(seconds=10), seconds=590)
        self.assertEqual(self.episode("tire_pressure_relative_low")["status"], "open")
        final = unsafe + timedelta(seconds=610)
        self.record(self.normal(final), at=final)
        self.assertEqual(self.episode("tire_pressure_relative_low")["status"], "resolved")

    def test_measurement_source_change_is_not_applicable(self):
        self.record(tiered("warning", at=DAY), at=DAY)
        at = DAY + timedelta(seconds=10)
        changed = self.normal(at)
        changed["current"]["source"] = "different.source"
        self.record(changed, at=at)
        self.assertEqual(self.episode("tire_pressure_relative_low")["evidence_state"], "not_applicable")

    def test_drift_normal_without_recovery_keys_resolves_immediately(self):
        drift = dict(rule="tire_pressure_rr_slow_leak", tier=2, severity="notice",
                     title="RR tire slow leak", action="Check RR pressure when cold.")
        self.record(tiered("warning", at=DAY, **drift, rule_snapshot={}), at=DAY)
        at = DAY + timedelta(seconds=5)
        result = self.record(tiered("normal", at=at, **drift, rule_snapshot={}), at=at)
        self.assertEqual(result.resolved, 1)


class ParkedClosureTests(TieredDeliveryBase):
    def unavailable(self, at: datetime, **kwargs):
        item = oil_critical("unavailable", at=at) if kwargs.pop("oil", False) else tiered("unavailable", at=at, **kwargs)
        item["current"]["observed_at"] = DAY.isoformat()
        return item

    def test_tiered_warning_closes_after_24_hours_parked(self):
        self.record(oil_critical("warning", at=DAY), at=DAY)
        for hours in range(1, 24):
            at = DAY + timedelta(hours=hours)
            self.record(self.unavailable(at, oil=True), at=at)
        episode = self.episode("engine_oil_pressure_absolute_critical")
        self.assertEqual(episode["status"], "open")
        self.assertEqual(self.rows()[0]["status"], "pending")
        closed_at = DAY + timedelta(hours=24)
        result = self.record(self.unavailable(closed_at, oil=True), at=closed_at)
        self.assertEqual(result.resolved, 1)
        episode = self.episode("engine_oil_pressure_absolute_critical")
        self.assertEqual(episode["status"], "resolved")
        self.assertEqual(episode["current_state"], "suppressed")
        self.assertEqual(episode["resolution_reason"], "not_re_observed")
        self.assertIn("parked_closed", self.event_types(int(episode["id"])))
        rows = self.rows()
        self.assertEqual([row["status"] for row in rows], ["cancelled"])
        self.assertEqual(rows[0]["last_error"], "closed while parked")
        event = detail(self.historian._conn, int(episode["id"]))[1]["event"]
        self.assertEqual(event["outcome"], "closed")
        note = event["timeline"][0]
        self.assertEqual(note["type"], "parked_closed")
        self.assertEqual(note["presentation"], "monitoring_note")
        self.assertEqual(note["monitoring_note"]["title"], "Closed while parked")
        self.assertEqual(self.historian.list_advisory_episodes(active_only=True), [])

    def test_open_trip_keeps_the_episode_open(self):
        self.record(tiered("warning", at=DAY), at=DAY)
        self.open_trip(DAY + timedelta(hours=1))
        for hours in (1, 24, 30):
            at = DAY + timedelta(hours=hours)
            self.record(self.unavailable(at), at=at)
        self.assertEqual(self.episode("tire_pressure_relative_low")["status"], "open")

    def test_legacy_episode_is_never_closed_while_parked(self):
        self.record(assessment("warning", eligible=True), at=DAY)
        for hours in (1, 24, 48):
            at = DAY + timedelta(hours=hours)
            self.record(assessment("unavailable"), at=at)
        episode = self.episode("test_rule")
        self.assertEqual(episode["status"], "open")
        self.assertNotIn("parked_closed", self.event_types(int(episode["id"])))


class LegacyPayloadTests(TieredDeliveryBase):
    def test_legacy_rate_limit_bound_and_payload_shape(self):
        limit = TelemetryHistorian._notification_rate_limit
        self.assertEqual(limit({"notification_rate_limit_seconds": 604800}), 604800.0)
        for bad in (59, 700000, True, float("nan")):
            with self.assertRaises(ValueError):
                limit({"notification_rate_limit_seconds": bad})
        self.assertEqual(TelemetryHistorian._notification_policy({"tier": None}), {"legacy": True})
        self.assertEqual(TelemetryHistorian._notification_policy({"tier": True}), {"legacy": True})
        self.record(assessment("warning", eligible=True), at=DAY)
        payload = self.payload(self.rows()[0])
        self.assertEqual(set(payload), LEGACY_PAYLOAD_KEYS)
        self.assertEqual(self.rows()[0]["eligible_after_us"], us(DAY))

    def test_legacy_warning_at_night_is_not_deferred(self):
        self.record(assessment("warning", eligible=True), at=NIGHT)
        self.assertEqual(self.rows()[0]["eligible_after_us"], us(NIGHT))

    def test_policy_table(self):
        policy = TelemetryHistorian._notification_policy
        rows = {
            (0, "critical"): (300, True, False, False),
            (0, "warning"): (21600, False, False, True),
            (1, "warning"): (21600, False, True, True),
            (2, "notice"): (604800, False, True, True),
            (3, "warning"): (21600, False, True, True),
            (3, "critical"): (21600, False, False, True),
        }
        for (tier, severity), expected in rows.items():
            with self.subTest(tier=tier, severity=severity):
                item = policy({"tier": tier, "severity": severity, "group": "oil", "rule": "r"})
                self.assertEqual(
                    (item["cooldown_seconds"], item["repeat"], item["defer"], item["once_per_episode"]),
                    expected,
                )
                self.assertEqual(item["dedupe_key"], "oil")
        self.assertEqual(policy({"tier": 1, "severity": "warning", "rule": "r"})["dedupe_key"], "r")


class QuietHoursFunctionTests(unittest.TestCase):
    def setUp(self):
        zone = patch.object(notifications, "QUIET_HOURS_ZONE", "US/Mountain")
        zone.start()
        self.addCleanup(zone.stop)

    def local_us(self, *parts, fold=0):
        return us(datetime(*parts, tzinfo=MOUNTAIN, fold=fold).astimezone(UTC))

    def test_window_boundaries(self):
        self.assertIsNone(quiet_hours_deferral(self.local_us(2026, 9, 24, 21, 59, 59)))
        self.assertEqual(
            quiet_hours_deferral(self.local_us(2026, 9, 24, 22, 0)),
            self.local_us(2026, 9, 25, 7, 30),
        )
        self.assertEqual(
            quiet_hours_deferral(self.local_us(2026, 9, 25, 7, 29, 59)),
            self.local_us(2026, 9, 25, 7, 30),
        )
        self.assertIsNone(quiet_hours_deferral(self.local_us(2026, 9, 25, 7, 30)))
        self.assertIsNone(quiet_hours_deferral(self.local_us(2026, 9, 25, 12, 0)))
        with self.assertRaises(TypeError):
            quiet_hours_deferral(1.5)

    def test_fall_back_night(self):
        digest = us(datetime(2026, 11, 1, 14, 30, tzinfo=UTC))  # 07:30 MST
        # 01:30 local happens twice on 2026-11-01: 07:30Z (MDT) and 08:30Z (MST).
        self.assertEqual(quiet_hours_deferral(us(datetime(2026, 11, 1, 7, 30, tzinfo=UTC))), digest)
        self.assertEqual(quiet_hours_deferral(us(datetime(2026, 11, 1, 8, 30, tzinfo=UTC))), digest)
        # 23:10 MDT the evening before: 9 h 20 min of wall time, not 8 h 20 min.
        night = us(datetime(2026, 11, 1, 5, 10, tzinfo=UTC))
        self.assertEqual(quiet_hours_deferral(night), digest)
        self.assertEqual(digest - night, int(9 * 3600e6 + 20 * 60e6))

    def test_spring_forward_night(self):
        # 2027-03-14 01:30 MST (08:30Z); 07:30 MDT is 13:30Z.
        self.assertEqual(
            quiet_hours_deferral(us(datetime(2027, 3, 14, 8, 30, tzinfo=UTC))),
            us(datetime(2027, 3, 14, 13, 30, tzinfo=UTC)),
        )

    def test_missing_zone_falls_back_to_utc(self):
        with patch.object(notifications, "QUIET_HOURS_ZONE", "Nowhere/Missing_Zone"), \
                patch.object(notifications, "QUIET_HOURS_ZONE_FALLBACK", False):
            self.assertIs(local_zone(), timezone.utc)
            self.assertTrue(notifications.QUIET_HOURS_ZONE_FALLBACK)
            self.assertEqual(
                quiet_hours_deferral(us(datetime(2026, 9, 24, 23, tzinfo=UTC))),
                us(datetime(2026, 9, 25, 7, 30, tzinfo=UTC)),
            )


def payload_for(assessment_fields: dict, *, title: str, tier: int, group: str,
                severity: str = "warning", at: str, **top) -> dict[str, object]:
    item = {"tier": tier, "group": group, "severity": severity, "evaluated_at": at,
            "state": "warning", **assessment_fields}
    payload = {
        "schema_version": 2, "advisory": True, "episode_id": 7, "rule": "rule",
        "category": "vehicle_health", "title": title, "state": "warning",
        "reason": "test", "opened_at": at, "evaluated_at": at, "assessment": item,
        "tier": tier, "group": group, "severity": severity, "action": item.get("action"),
        "confidence": "high", "notification_kind": "warning", "deferred_until": None,
        "trip_id": 3,
    }
    payload.update(top)
    return payload


class MessageTests(unittest.TestCase):
    def setUp(self):
        zone = patch.object(notifications, "QUIET_HOURS_ZONE", "US/Mountain")
        zone.start()
        self.addCleanup(zone.stop)

    def examples(self):
        return {
            "oil": payload_for(
                {"current": {"value": 9.0, "unit": "psi"},
                 "running_evidence": {"rpm": {"value": 1850.0}},
                 "absolute_threshold": {"operator": "below", "value": 12.0, "unit": "psi"},
                 "persistence": {"window_seconds": 10, "observed": 2, "required": 2},
                 "action": "Pull over and shut off."},
                title="Oil pressure critical", tier=0, group="oil", severity="critical",
                at="2026-09-25T00:42:00+00:00"),
            "coolant": payload_for(
                {"current": {"value": 232.0, "unit": "°F"},
                 "absolute_threshold": {"operator": "above", "value": 230.0, "unit": "°F"},
                 "persistence": {"window_seconds": 30, "observed": 6, "required": 6},
                 "action": "Ease off, check gauge."},
                title="Coolant hot", tier=0, group="cooling", at="2026-09-24T20:05:00+00:00"),
            "tire": payload_for(
                {"current": {"value": 74.0, "unit": "psi"},
                 "baseline": {"median": 78.0},
                 "deviation": {"effect_in_rule_direction": 4.0, "threshold": 3.0},
                 "persistence": {"window_seconds": 300, "observed": 60, "required": 60},
                 "action": "Check at next stop."},
                title="RR tire low", tier=1, group="tires", at="2026-09-24T15:12:00+00:00"),
            "charging": payload_for(
                {"current": {"value": 11.9, "unit": "V"},
                 "running_evidence": {"rpm": {"value": 1600.0}},
                 "absolute_threshold": {"operator": "below", "value": 12.0, "unit": "V"},
                 "persistence": {"window_seconds": 60, "observed": 12, "required": 12},
                 "action": "Limit loads."},
                title="Charging problem", tier=0, group="charging", at="2026-09-24T22:20:00+00:00"),
        }

    def test_spec_section_7_examples(self):
        expected = {
            "oil": ("Oil pressure critical",
                    "9 psi @ 1,850 rpm, below the 12 psi minimum, 10 s.\nPull over and shut off. 18:42"),
            "coolant": ("Coolant hot",
                        "232 °F, above your 230 °F limit, 30 s.\nEase off, check gauge. 14:05"),
            "tire": ("RR tire low",
                     "74 psi, 4 psi under its usual 78, 5 min.\nCheck at next stop. 09:12"),
            "charging": ("Charging problem",
                         "11.9 V @ 1,600 rpm, below the 12 V minimum, 1 min.\nLimit loads. 16:20"),
        }
        for name, payload in self.examples().items():
            with self.subTest(name=name):
                title, body = render_message(payload)
                self.assertEqual((title, body), expected[name])
                self.assertLessEqual(len(title), 120)
                self.assertLessEqual(len(body), 1500)
                self.assertLessEqual(len(body.splitlines()), 2)
                self.assertIsNone(FORBIDDEN_WORDS_RE.search(title + " " + body))

    def test_owner_rule_system_item_recovery_and_repeat(self):
        custom = payload_for(
            {"current": {"value": 240.0, "unit": "°F"}, "baseline": None,
             "custom_rule": {"operator": "above", "threshold": 235}, "persistence":
             {"window_seconds": 30, "observed": 6, "required": 6}, "action": "Ease off."},
            title="Coolant limit", tier=1, group="custom", at="2026-09-24T16:00:00+00:00")
        self.assertEqual(render_message(custom)[1],
                         "240 °F, above your 235 °F threshold, 30 s.\nEase off. 10:00")
        system = payload_for(
            {"current": {"role": "c-can"}, "reason": "advisory episode regime deviation",
             "action": "Check the USB hub."},
            title="Telemetry offline", tier=3, group="system", at="2026-09-24T16:00:00+00:00")
        title, body = render_message(system)
        self.assertEqual((title, body), ("Telemetry offline", "Telemetry offline.\nCheck the USB hub. 10:00"))
        self.assertIsNone(FORBIDDEN_WORDS_RE.search(body))
        recovery = self.examples()["oil"]
        recovery.update(notification_kind="recovery", state="normal")
        recovery["assessment"]["current"] = {"value": 31.0, "unit": "psi"}
        recovery["assessment"]["evaluated_at"] = "2026-09-25T00:52:00+00:00"
        self.assertEqual(render_message(recovery), ("Oil pressure normal", "31 psi at 18:52."))
        repeat = self.examples()["oil"]
        repeat.update(notification_kind="repeat", opened_at="2026-09-25T00:32:00+00:00")
        self.assertTrue(render_message(repeat)[1].startswith("9 psi @ 1,850 rpm, below the 12 psi minimum, 10 min."))
        building = self.examples()["tire"]
        building["assessment"]["persistence"]["observed"] = 3
        self.assertIn(", just now.", render_message(building)[1])

    def test_priority_and_tags_per_tier(self):
        sink = NtfyAdvisoryNotificationSink
        payloads = self.examples()
        self.assertEqual(sink._priority(payloads["oil"]), "urgent")
        self.assertEqual(sink._priority(payloads["coolant"]), "high")
        self.assertEqual(sink._priority(payloads["tire"]), "high")
        self.assertEqual(sink._priority({**payloads["tire"], "tier": 2}), "default")
        self.assertEqual(sink._priority({**payloads["tire"], "tier": 3}), "default")
        self.assertEqual(sink._priority({**payloads["oil"], "notification_kind": "recovery"}), "default")
        legacy = {"advisory": True, "state": "warning", "assessment": {"severity": "critical"}}
        self.assertEqual(sink._priority(legacy), "max")
        self.assertEqual(sink._priority({**legacy, "assessment": {"severity": "warning"}}), "high")
        self.assertEqual(sink._tags(payloads["tire"]), "warning,car")
        self.assertEqual(sink._tags(payloads["charging"]), "warning,battery")
        self.assertEqual(sink._tags({**payloads["tire"], "group": "system"}), "warning,computer")
        self.assertEqual(sink._tags({**payloads["oil"], "notification_kind": "recovery"}),
                         "white_check_mark,car")
        self.assertEqual(sink._tags({**legacy, "category": "can_infrastructure"}), "warning,computer")

    def test_deliver_accepts_recovery_and_rejects_legacy_nonwarning(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stderr="")

        sink = NtfyAdvisoryNotificationSink("van-telemetry", executable="/safe/ntfy-send", run=run)
        recovery = self.examples()["oil"]
        recovery.update(notification_kind="recovery", state="normal")
        sink.deliver(recovery)
        argv = calls[-1]
        self.assertEqual(argv[argv.index("--title") + 1], "Oil pressure normal")
        self.assertEqual(argv[argv.index("--priority") + 1], "default")
        self.assertEqual(argv[-1], "9 psi at 18:42.")
        sink.deliver(self.examples()["tire"])
        self.assertEqual(calls[-1][-1], "74 psi, 4 psi under its usual 78, 5 min.\nCheck at next stop. 09:12")
        with self.assertRaisesRegex(ValueError, "warning advisory"):
            sink.deliver({"advisory": True, "state": "normal", "title": "x"})
        with self.assertRaisesRegex(ValueError, "warning advisory"):
            sink.deliver({**recovery, "advisory": False})
        legacy = {"advisory": True, "state": "warning", "title": "Legacy", "reason": "r",
                  "evaluated_at": "2026-09-24T16:00:00+00:00", "episode_id": 4,
                  "assessment": {"severity": "warning"}}
        sink.deliver(legacy)
        self.assertIn("Episode: 4", calls[-1][-1])
        self.assertEqual(calls[-1][calls[-1].index("--title") + 1], "Legacy")


if __name__ == "__main__":
    unittest.main()
