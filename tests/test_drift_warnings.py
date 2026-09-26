"""Tier-2 drift notices over metric_rollups (warnings-redesign.md section 5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import itertools
import json
from pathlib import Path
import re
import statistics
import tempfile
import time
import unittest
from unittest import mock

from projects.vehicle_data import drift_warnings as dw
from projects.vehicle_data import insights as insights_module
from projects.vehicle_data.event_history import digest
from projects.vehicle_data.historian import TelemetryHistorian
from projects.vehicle_data.insights import TelemetryInsights
from tests.test_vehicle_historian import available, definition, snapshot


AT = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
MINUTE_US = 60_000_000
FORBIDDEN = re.compile(r"\b(regime|mad|deviation|persisten\w*|episode|advisory|unavailable)\b", re.I)
GROUPS = {
    "tire_pressure_fl_slow_leak": "tires",
    "tire_pressure_fr_slow_leak": "tires",
    "tire_pressure_rl_slow_leak": "tires",
    "tire_pressure_rr_slow_leak": "tires",
    "engine_coolant_temperature_idle_creep": "cooling",
    "engine_oil_pressure_decline": "oil",
    "battery_voltage_charge_acceptance": "charging",
    "battery_voltage_resting_low": "battery",
}
PREFIXES = ("tire_pressure_", "engine_coolant_", "engine_oil_pressure_", "battery_voltage_")
IDLE = "engine_running:stationary:rpm_idle:warm"
LOW = "engine_running:urban:rpm_low:warm"
FLAT = {"fl": 58.0, "fr": 57.0, "rl": 78.0, "rr": 78.0}
PARKED = "engine_off:stationary:rpm_off:cold"
SRC_41A = "ccan.broadcast.0x41a"
SRC_46C = "bcan.broadcast.0x46c"


def to_us(moment: datetime) -> int:
    return int(moment.timestamp()) * 1_000_000


def iso(us: int) -> str:
    return datetime.fromtimestamp(us / 1e6, timezone.utc).isoformat()


class Rollups:
    """Direct SQL writer for ``trips`` and ``metric_rollups`` test rows."""

    def __init__(self, historian: TelemetryHistorian) -> None:
        self.h = historian
        self.next_id = 1
        self.pending: list[tuple] = []
        self.ends: dict[int, int] = {}

    def trip(self, start: datetime, minutes: int = 25) -> tuple[int, int]:
        trip_id = self.next_id
        self.next_id += 1
        started = to_us(start)
        ended = started + minutes * MINUTE_US
        self.ends[trip_id] = ended
        with self.h._lock, self.h._conn:
            self.h._conn.execute(
                """INSERT INTO trips(id,started_us,started_at,last_active_us,last_active_at,
                   ended_us,ended_at,start_basis,end_reason,snapshot_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (trip_id, started, iso(started), ended, iso(ended), ended, iso(ended),
                 "test", "activity_timeout", 1),
            )
        return trip_id, started

    def add(self, trip_id, bucket_us, metric, regime, value, unit, *, source="test.src"):
        self.pending.append(
            (bucket_us, 60, trip_id, metric, regime, unit, source, "verified",
             f"finding for {metric}", 1, value, value, value, value, 0.0, bucket_us, bucket_us)
        )

    def flush(self) -> None:
        with self.h._lock, self.h._conn:
            self.h._conn.executemany(
                "INSERT INTO metric_rollups VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                self.pending,
            )
        self.pending = []

    def seed(self, start: datetime) -> int:
        """A first trip: it has no predecessor, so it is never a cold start."""

        return self.trip(start)[0]

    def set_last_bucket(self, trip_id: int, metric: str, value: float) -> None:
        """Make ``trip_id``'s last ``metric`` bucket carry ``value`` (insert one if none)."""

        self.flush()
        with self.h._lock, self.h._conn:
            row = self.h._conn.execute(
                "SELECT max(bucket_us) FROM metric_rollups WHERE trip_key=? AND metric=?",
                (trip_id, metric),
            ).fetchone()
            if row[0] is not None:
                self.h._conn.execute(
                    """UPDATE metric_rollups SET minimum=?, maximum=?, mean=?, median=?
                       WHERE trip_key=? AND metric=? AND bucket_us=?""",
                    (value, value, value, value, trip_id, metric, row[0]),
                )
                return
        self.add(trip_id, self.ends[trip_id] - MINUTE_US, metric,
                 "engine_running:urban:rpm_low:warm", value, "psi")
        self.flush()

    def cold_trip(self, start: datetime, pressures: dict[str, float], *, moving_minute=2,
                  warm_offset=6.0, cached=None, cached_minutes=None) -> int:
        """A cold start; ``cached={"rr": 84.0}`` makes the RF hub repeat 84.0 psi.

        The cached value fills that wheel's first ``cached_minutes`` cold-window
        buckets (all of them by default) and becomes the previous trip's last
        bucket, as recorded in trips 34/36.
        """

        cached = dict(cached or {})
        previous = self.next_id - 1
        for wheel, value in cached.items():
            if previous in self.ends:
                self.set_last_bucket(previous, f"tire.pressure.{wheel}", value)
        trip_id, started = self.trip(start)
        cold_last = moving_minute + 3
        for minute in range(20):
            bucket = started + minute * MINUTE_US
            moving = minute >= moving_minute
            motion = "urban" if moving else "stationary"
            self.add(trip_id, bucket, "vehicle.speed", f"engine_running:{motion}:rpm_idle:cold",
                     25.0 if moving else 0.0, "mph")
            for wheel, pressure in pressures.items():
                # Cold window ends 3 min after the first moving bucket; later
                # (warmed) minutes outnumber it so including them would show.
                value = pressure if minute <= cold_last else pressure + warm_offset
                if wheel in cached and minute <= cold_last and (
                        cached_minutes is None or minute < cached_minutes):
                    value = cached[wheel]
                self.add(trip_id, bucket, f"tire.pressure.{wheel}",
                         f"engine_running:{motion}:rpm_low:cold", value, "psi")
        return trip_id

    def parked(self, start_us: int, minutes: int, values, *, source="test.src") -> None:
        """Parked (``trip_key = 0``) battery.voltage buckets, one per minute."""

        for minute in range(minutes):
            value = values[minute] if isinstance(values, (list, tuple)) else values
            self.add(0, start_us + minute * MINUTE_US, "battery.voltage", PARKED, value, "V",
                     source=source)

    def stops(self, volts, *, first_day=None, source="test.src", minutes=30) -> list[int]:
        """One trip a day (08:00 for 25 min) followed by a parked stop at ``volts[i]``."""

        first_day = len(volts) if first_day is None else first_day
        trip_ids = []
        for index, value in enumerate(volts):
            trip_id, _started = self.trip(AT - timedelta(days=first_day - index, hours=4))
            self.parked(self.ends[trip_id] + 2 * MINUTE_US, minutes, value, source=source)
            trip_ids.append(trip_id)
        return trip_ids

    def engine_trip(self, start: datetime, *, coolant=None, oil_idle=None, oil_low=None,
                    low_share=None) -> int:
        trip_id, started = self.trip(start, minutes=30)
        if coolant is not None:
            for minute in range(5):  # before minute 10: must be ignored
                self.add(trip_id, started + minute * MINUTE_US, "engine.coolant_temperature",
                         IDLE, 150.0, "°F")
            for minute in range(10, 15):
                self.add(trip_id, started + minute * MINUTE_US, "engine.coolant_temperature",
                         IDLE, coolant, "°F")
        if oil_idle is not None:
            for minute in range(15, 20):
                self.add(trip_id, started + minute * MINUTE_US, "engine.oil_pressure",
                         IDLE, oil_idle, "psi")
        if oil_low is not None:
            for minute in range(20, 25):
                self.add(trip_id, started + minute * MINUTE_US, "engine.oil_pressure",
                         LOW, oil_low, "psi")
        if low_share is not None:
            low = round(20 * low_share)
            for minute in range(20):
                self.add(trip_id, started + minute * MINUTE_US, "battery.voltage",
                         LOW, 12.8 if minute < low else 14.0, "V")
        return trip_id


def by_rule(assessments):
    result = {a["rule"]: a for a in assessments}
    assert len(result) == len(assessments)
    return result


class DriftCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="drift-warnings-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.sqlite3"
        self.h = TelemetryHistorian(self.path)
        self.addCleanup(self.h.close)
        self.r = Rollups(self.h)

    def evaluate(self, at=AT, previous=None):
        self.r.flush()
        return by_rule(dw.evaluate_drift(self.h, at=at, previous_state=previous))

    def assert_c1(self, assessments):
        self.assertEqual(sorted(a["rule"] for a in assessments), sorted(dw.DRIFT_RULE_KEYS))
        for a in assessments:
            with self.subTest(rule=a["rule"]):
                self.assertEqual(a["tier"], 2)
                self.assertEqual(a["severity"], "notice")
                self.assertEqual(a["group"], GROUPS[a["rule"]])
                self.assertEqual(a["confidence"], "medium")
                self.assertEqual(a["category"], "vehicle_health")
                self.assertIs(a["advisory"], True)
                self.assertIn(a["state"], ("warning", "normal"))
                self.assertIs(
                    a["notification_eligible"],
                    a["state"] == "warning" and not a["drift"].get("held_open"),
                )
                self.assertEqual(a["notification_rate_limit_seconds"], 86400)
                self.assertEqual(a["evaluator_revision"], "drift-v1")
                self.assertTrue(a["rule"].startswith(PREFIXES))
                self.assertIn(a["direction"], ("low", "high"))
                action = a["action"]
                self.assertTrue(action.endswith("."))
                self.assertLessEqual(len(action), 120)
                self.assertIsNone(FORBIDDEN.search(action))
                self.assertIsNone(FORBIDDEN.search(a["title"]))
                self.assertIsNone(re.search(r"\d", a["title"]))
                self.assertIsNone(FORBIDDEN.search(a["reason"]))
                self.assertNotIn("recovery_observations", a["rule_snapshot"])
                self.assertNotIn("recovery_seconds", a["rule_snapshot"])
                self.assertEqual(a["rule_revision"], digest(a["rule_snapshot"]))
                for key in ("value", "unit", "observed_at", "source", "quality", "provenance"):
                    self.assertIn(key, a["current"])
                self.assertIsNone(a["current"]["trip_id"])
                self.assertIn("drift", a)
                if a["state"] == "warning":
                    for key in ("median", "unit", "bucket_count", "trip_count"):
                        self.assertIn(key, a["baseline"])
                    for key in ("signed_from_median", "effect_in_rule_direction", "threshold"):
                        self.assertIn(key, a["deviation"])
                    if not a["drift"].get("held_open"):
                        self.assertIsNotNone(a["current"]["observed_at"])
                        self.assertIsNotNone(a["current"]["source"])
                json.dumps(a, allow_nan=False)


class SlowLeakTests(DriftCase):
    def test_rr_losing_six_tenths_a_week_warns_and_short_gaps_are_excluded(self):
        self.r.seed(AT - timedelta(days=40))
        days = (13, 11, 8.5, 6, 3.5, 1)
        first = days[0]
        for day in days:
            start = AT - timedelta(days=day)
            rr = 78.0 - 0.6 / 7.0 * (first - day)
            self.r.cold_trip(start, {**FLAT, "rr": rr})
            if day in (11, 6):
                # 1 h later: parked gap < 4 h, warm tires; must not count.
                self.r.cold_trip(start + timedelta(hours=1), {**FLAT, "rr": 60.0})
        result = self.evaluate()
        rr = result["tire_pressure_rr_slow_leak"]
        self.assertEqual(rr["state"], "warning")
        self.assertAlmostEqual(rr["drift"]["slope_psi_per_week"], -0.6, places=3)
        self.assertAlmostEqual(rr["drift"]["relative_slope_psi_per_week"], -0.6, places=3)
        self.assertEqual(rr["drift"]["mate"], "rl")
        self.assertTrue(rr["drift"]["own_corroborates"])
        self.assertEqual(rr["drift"]["stale_excluded_buckets"], 0)
        self.assertIn("RL", rr["reason"])
        self.assertEqual(rr["drift"]["slope_points"], 6)
        self.assertEqual(rr["drift"]["cold_start_count"], 6)
        self.assertEqual(rr["drift"]["trips_excluded_short_parked_gap"], 3)  # seed + 2
        self.assertEqual(rr["action"], "Check the RR tire for a slow leak.")
        self.assertEqual(rr["title"], "RR tire slow leak")
        self.assertEqual(rr["metric"], "tire.pressure.rr")
        latest = 78.0 - 0.6 / 7.0 * (first - days[-1])
        self.assertAlmostEqual(rr["current"]["value"], latest, places=3)
        self.assertEqual(rr["current"]["observed_at"], iso(to_us(AT - timedelta(days=1))))
        self.assertEqual(rr["current"]["source"], "test.src")
        self.assertEqual(rr["baseline"]["trip_count"], 6)
        self.assertEqual(len(rr["drift"]["points"]), 6)
        for wheel in ("fl", "fr", "rl"):
            other = result[f"tire_pressure_{wheel}_slow_leak"]
            self.assertEqual(other["state"], "normal", wheel)
            self.assertEqual(other["drift"]["slope_psi_per_week"], 0.0)
            # cold window only: the warmed minutes (+6 psi) are not in the median
            self.assertEqual(other["current"]["value"], FLAT[wheel])
        self.assert_c1(list(result.values()))

    def test_fewer_than_four_cold_starts_is_normal_with_reason(self):
        self.r.seed(AT - timedelta(days=40))
        for day in (9, 5, 1):
            self.r.cold_trip(AT - timedelta(days=day), {**FLAT, "rr": 70.0 - day})
        result = self.evaluate()
        for wheel in dw.WHEELS:
            a = result[f"tire_pressure_{wheel}_slow_leak"]
            self.assertEqual(a["state"], "normal")
            self.assertTrue(a["reason"].startswith("not enough paired cold starts"),
                            a["reason"])
            self.assertFalse(a["notification_eligible"])
        self.assert_c1(list(result.values()))

    def _delta_history(self, recent_value):
        self.r.seed(AT - timedelta(days=44))
        for day in (29, 27, 25, 23, 21, 19, 17, 15):
            self.r.cold_trip(AT - timedelta(days=day), {"rr": 78.0, "rl": 78.0})
        for day in (13, 10, 7, 4, 1):
            self.r.cold_trip(AT - timedelta(days=day), {"rr": recent_value, "rl": 78.0})

    def test_delta_rule_fires_with_flat_recent_slope(self):
        self._delta_history(74.8)
        rr = self.evaluate()["tire_pressure_rr_slow_leak"]
        self.assertEqual(rr["state"], "warning")
        self.assertAlmostEqual(rr["drift"]["delta_psi"], -3.2, places=3)
        self.assertEqual(rr["drift"]["slope_psi_per_week"], 0.0)
        self.assertTrue(rr["drift"]["delta_fires"])
        self.assertFalse(rr["drift"]["slope_fires"])
        self.assertEqual(rr["baseline"]["median"], 78.0)
        self.assertEqual(rr["current"]["value"], 74.8)
        self.assertAlmostEqual(rr["deviation"]["effect_in_rule_direction"], 3.2, places=3)
        self.assertAlmostEqual(rr["drift"]["own_delta_psi"], -3.2, places=3)
        self.assertTrue(rr["drift"]["own_corroborates"])

    def test_delta_hysteresis_holds_at_minus_two_and_clears_at_minus_one(self):
        self._delta_history(76.0)
        fresh = self.evaluate()["tire_pressure_rr_slow_leak"]
        self.assertEqual(fresh["state"], "normal")
        held = self.evaluate(previous={"tire_pressure_rr_slow_leak": "warning"})
        self.assertEqual(held["tire_pressure_rr_slow_leak"]["state"], "warning")
        self.assertAlmostEqual(held["tire_pressure_rr_slow_leak"]["drift"]["delta_psi"], -2.0)

    def test_delta_hysteresis_clears_at_minus_one(self):
        self._delta_history(77.0)
        previous = [{"rule": "tire_pressure_rr_slow_leak", "state": "warning"}]
        rr = self.evaluate(previous=previous)["tire_pressure_rr_slow_leak"]
        self.assertEqual(rr["state"], "normal")
        self.assertTrue(rr["drift"]["clear_condition_met"])

    def test_open_or_future_trips_are_ignored(self):
        self._delta_history(74.8)
        early = self.evaluate(at=AT - timedelta(days=2))["tire_pressure_rr_slow_leak"]
        # at day -2 the day -1 trip does not exist yet
        self.assertEqual(early["drift"]["points"][-1][1], iso(to_us(AT - timedelta(days=4))))


SIX_DAYS = (13, 11, 8.5, 6, 3.5, 1)


class StaleAndAxleTests(DriftCase):
    """D10: cached RF-hub readings are dropped and each wheel is judged against its mate."""

    def test_s1_cached_readings_are_not_cold_starts(self):
        # RR's first two cold buckets repeat the previous trip's last (warm) bucket,
        # which drifts down 1.2 psi a week; the fresh cold readings are flat.  The
        # old median of 2 cached + 2 fresh buckets fell 0.6 psi a week and warned.
        self.r.seed(AT - timedelta(days=40))
        for day in SIX_DAYS:
            cached = 84.0 - 1.2 / 7.0 * (SIX_DAYS[0] - day)
            self.r.cold_trip(AT - timedelta(days=day), dict(FLAT), moving_minute=0,
                             cached={"rr": cached}, cached_minutes=2)
        result = self.evaluate()
        rr = result["tire_pressure_rr_slow_leak"]
        self.assertEqual(rr["state"], "normal", rr["reason"])
        self.assertEqual(rr["drift"]["stale_excluded_buckets"], 12)
        self.assertEqual(rr["drift"]["stale_excluded_trips"], 0)
        self.assertEqual(rr["drift"]["own_slope_psi_per_week"], 0.0)
        self.assertEqual(rr["current"]["value"], 78.0)
        self.assertEqual(result["tire_pressure_rl_slow_leak"]["drift"]["mate_stats"]
                         ["stale_excluded_buckets"], 12)
        self.assertTrue(all(a["state"] == "normal" for a in result.values()))
        self.assert_c1(list(result.values()))

    def test_s1b_a_trip_with_only_cached_readings_contributes_no_point(self):
        self.r.seed(AT - timedelta(days=40))
        for day in SIX_DAYS:
            self.r.cold_trip(AT - timedelta(days=day), dict(FLAT))
        self.r.cold_trip(AT - timedelta(hours=12), dict(FLAT), cached={"fl": 50.0})
        fl = self.evaluate()["tire_pressure_fl_slow_leak"]
        self.assertEqual(fl["drift"]["stale_excluded_trips"], 1)
        self.assertEqual(fl["drift"]["stale_excluded_buckets"], 6)
        self.assertEqual(fl["drift"]["cold_start_count"], 6)
        self.assertEqual(fl["state"], "normal")

    def test_s2_common_mode_drop_is_not_a_leak(self):
        self.r.seed(AT - timedelta(days=40))
        for day in (29, 26, 23, 20, 17, 14):
            self.r.cold_trip(AT - timedelta(days=day), dict(FLAT))
        for day in (10, 6, 2):
            self.r.cold_trip(AT - timedelta(days=day),
                             {wheel: value - 3.5 for wheel, value in FLAT.items()})
        result = self.evaluate()
        for wheel in dw.WHEELS:
            a = result[f"tire_pressure_{wheel}_slow_leak"]
            with self.subTest(wheel=wheel):
                self.assertEqual(a["state"], "normal", a["reason"])
                self.assertAlmostEqual(a["drift"]["relative_delta_psi"], 0.0, places=3)
                self.assertAlmostEqual(a["drift"]["own_delta_psi"], -3.5, places=3)
                self.assertTrue(a["drift"]["own_corroborates"])
        self.assert_c1(list(result.values()))

    def test_s3_one_wheel_leaking_against_a_flat_mate_warns(self):
        self.r.seed(AT - timedelta(days=40))
        for day in SIX_DAYS:
            rr = 78.0 - 1.2 / 7.0 * (SIX_DAYS[0] - day)
            self.r.cold_trip(AT - timedelta(days=day), {**FLAT, "rr": rr})
        result = self.evaluate()
        rr = result["tire_pressure_rr_slow_leak"]
        self.assertEqual(rr["state"], "warning")
        self.assertAlmostEqual(rr["drift"]["relative_slope_psi_per_week"], -1.2, places=3)
        self.assertTrue(rr["drift"]["slope_fires"])
        self.assertEqual(rr["drift"]["usual_offset_psi"], round(_median_of_offsets(), 3))
        self.assertEqual(len(rr["drift"]["relative_points"]), 6)
        self.assertIn("RL", rr["reason"])
        self.assertTrue(rr["deviation"]["basis"].endswith("wheel relative to its axle mate"))
        for wheel in ("fl", "fr", "rl"):
            other = result[f"tire_pressure_{wheel}_slow_leak"]
            self.assertEqual(other["state"], "normal", (wheel, other["reason"]))
        self.assert_c1(list(result.values()))

    def test_s4_inflating_the_mate_does_not_make_a_leak(self):
        self.r.seed(AT - timedelta(days=40))
        for index, day in enumerate(SIX_DAYS):
            self.r.cold_trip(AT - timedelta(days=day),
                             {**FLAT, "fr": FLAT["fr"] + 4.0 * index / 5})
        result = self.evaluate()
        fl = result["tire_pressure_fl_slow_leak"]
        self.assertEqual(fl["state"], "normal", fl["reason"])
        self.assertLess(fl["drift"]["relative_slope_psi_per_week"], dw.SLOW_LEAK_SLOPE)
        self.assertFalse(fl["drift"]["own_corroborates"])
        self.assertEqual(result["tire_pressure_fr_slow_leak"]["state"], "normal")


def _median_of_offsets():
    return statistics.median(
        -1.2 / 7.0 * (SIX_DAYS[0] - day) for day in SIX_DAYS
    )


class HeldOpenTests(DriftCase):
    """D11/P10: running short of data never resolves an open notice."""

    def test_s5_tire_notice_is_held_with_two_paired_cold_starts(self):
        self.r.seed(AT - timedelta(days=40))
        for day in (5, 1):
            self.r.cold_trip(AT - timedelta(days=day), dict(FLAT))
        key = "tire_pressure_rr_slow_leak"
        fresh = self.evaluate()
        self.assertEqual(fresh[key]["state"], "normal")
        self.assertNotIn("held_open", fresh[key]["drift"])
        held = self.evaluate(previous={key: "warning"})
        a = held[key]
        self.assertEqual(a["state"], "warning")
        self.assertIs(a["drift"]["held_open"], True)
        self.assertIs(a["notification_eligible"], False)
        self.assertTrue(a["reason"].startswith("not enough paired cold starts (2 of 4"))
        self.assertTrue(a["reason"].endswith("the notice stays open until it can be re-checked"))
        self.assertEqual(held["tire_pressure_fl_slow_leak"]["state"], "normal")
        self.assert_c1(list(held.values()))

    def test_s5_idle_creep_and_resting_voltage_are_held(self):
        self.r.seed(AT - timedelta(days=30))
        for day in (3, 2, 1):
            self.r.engine_trip(AT - timedelta(days=day), coolant=230.0)
        self.r.parked(to_us(AT - timedelta(hours=10)), 10, 11.9)
        previous = {"engine_coolant_temperature_idle_creep": "warning",
                    "battery_voltage_resting_low": "warning"}
        fresh = self.evaluate()
        held = self.evaluate(previous=previous)
        for key in previous:
            with self.subTest(key=key):
                self.assertEqual(fresh[key]["state"], "normal")
                self.assertEqual(held[key]["state"], "warning")
                self.assertIs(held[key]["drift"]["held_open"], True)
                self.assertIs(held[key]["drift"]["sufficient"], False)
                self.assertIn("stays open", held[key]["reason"])
        self.assertTrue(held["battery_voltage_resting_low"]["reason"].startswith(
            "not enough settled stops (1 of 3 in 7 days)"))
        self.assert_c1(list(held.values()))



class HeldNoticeNeverPushesAgain(DriftCase):
    """A held notice has nothing new to say; it must never open a fresh push.

    A drift rule-revision change closes the open notice ("rule_replaced").
    With too little data to re-check, the held assessment then opened a new
    episode and, once the 7-day group cooldown had passed, pushed a notice
    whose only content was "not enough ...; the notice stays open".
    """

    def test_held_notice_after_a_revision_change_does_not_push(self):
        key = "tire_pressure_rr_slow_leak"
        held = self.evaluate(previous={key: "warning"})[key]
        self.assertIs(held["drift"]["held_open"], True)
        self.assertEqual(held["state"], "warning")
        self.assertIs(held["notification_eligible"], False)
        old = json.loads(json.dumps(held))
        old.update(rule_revision="0" * 64, notification_eligible=True,
                   reason="RR tire pressure is falling")
        old["drift"].pop("held_open")
        self.h.record_advisory_assessments([old], evaluated_at=AT - timedelta(days=8))
        self.h.record_advisory_assessments([held], evaluated_at=AT)
        rows = self.h._conn.execute(
            "SELECT status FROM advisory_notification_outbox").fetchall()
        self.assertEqual(len(rows), 1)
        episodes = self.h._conn.execute(
            "SELECT status,resolution_reason FROM advisory_episodes ORDER BY id").fetchall()
        self.assertEqual([row["status"] for row in episodes], ["resolved", "open"])


class RestingVoltageTests(DriftCase):
    KEY = "battery_voltage_resting_low"

    def test_s6a_low_settled_level_warns(self):
        self.r.seed(AT - timedelta(days=30))
        self.r.stops([12.0, 12.1, 12.05, 12.0, 12.1, 12.05])
        result = self.evaluate()
        a = result[self.KEY]
        self.assertEqual(a["state"], "warning", a["reason"])
        self.assertTrue(a["drift"]["fires_level"])
        self.assertFalse(a["drift"]["fires_slope"])
        self.assertAlmostEqual(a["drift"]["level_v"], 12.05)
        self.assertEqual(a["drift"]["stops_in_window"], 6)
        self.assertEqual(a["group"], "battery")
        self.assertEqual(a["action"], "Charge the battery or have it tested soon.")
        self.assertEqual(a["title"], "Battery resting voltage low")
        self.assertEqual(a["current"]["unit"], "V")
        self.assertEqual(a["current"]["value"], 12.05)
        self.assertIsNone(a["current"]["trip_id"])
        self.assertAlmostEqual(a["deviation"]["effect_in_rule_direction"], 0.15)
        self.assertEqual(a["deviation"]["threshold"], 0.0)
        self.assertIn("limit 12.2 V", a["reason"])
        self.assertEqual(len(a["drift"]["points"]), 6)
        self.assertEqual(a["drift"]["points"][0][2], 30)
        self.assert_c1(list(result.values()))

    def test_s6b_falling_level_warns_on_slope(self):
        self.r.seed(AT - timedelta(days=30))
        self.r.stops([round(12.6 - 0.12 * i, 2) for i in range(6)])
        a = self.evaluate()[self.KEY]
        self.assertEqual(a["state"], "warning", a["reason"])
        self.assertTrue(a["drift"]["fires_slope"])
        self.assertFalse(a["drift"]["fires_level"])
        self.assertAlmostEqual(a["drift"]["slope_v_per_day"], -0.12, places=3)
        self.assertTrue(a["reason"].startswith("resting voltage falling 0.12 V a day"))

    def test_s6c_steady_level_is_normal(self):
        self.r.seed(AT - timedelta(days=30))
        self.r.stops([12.45] * 5)
        a = self.evaluate()[self.KEY]
        self.assertEqual(a["state"], "normal")
        self.assertEqual(a["drift"]["level_v"], 12.45)
        self.assertEqual(a["drift"]["slope_v_per_day"], 0.0)
        self.assertEqual(a["reason"], "resting voltage steady (12.45 V over 5 stops)")

    def test_s6d_key_off_and_cranking_dips_are_outside_the_settled_span(self):
        self.r.seed(AT - timedelta(days=30))
        trips = self.r.stops([12.45] * 5, minutes=2)
        for index, trip_id in enumerate(trips):
            # within 60 s of the engine stopping
            self.r.parked(self.r.ends[trip_id], 1, 11.2)
            if index + 1 < len(trips):
                # a key-on/crank bucket in the last 120 s before the next start
                next_start = self.r.ends[trips[index + 1]] - 25 * MINUTE_US
                self.r.parked(next_start - MINUTE_US, 1, 11.2)
        a = self.evaluate()[self.KEY]
        self.assertEqual(a["state"], "normal", a["reason"])
        self.assertEqual(a["drift"]["level_v"], 12.45)
        self.assertTrue(all(point[2] == 2 for point in a["drift"]["points"]))

    def test_s6e_the_identity_with_most_settled_buckets_is_used(self):
        self.r.seed(AT - timedelta(days=30))
        trips = self.r.stops([12.45] * 5, source=SRC_41A, minutes=4)
        for trip_id in trips:
            # fewer, later buckets from the other sender
            self.r.parked(self.r.ends[trip_id] + 10 * MINUTE_US, 2, 11.9, source=SRC_46C)
        a = self.evaluate()[self.KEY]
        self.assertEqual(a["state"], "normal", a["reason"])
        self.assertEqual(a["current"]["source"], SRC_41A)
        self.assertEqual(a["drift"]["identity"][1], SRC_41A)
        self.assertEqual(a["drift"]["level_v"], 12.45)

    def test_s6f_hysteresis_holds_below_clear_and_clears_at_it(self):
        self.r.seed(AT - timedelta(days=30))
        self.r.stops([12.25] * 6)
        previous = {self.KEY: "warning"}
        self.assertEqual(self.evaluate()[self.KEY]["state"], "normal")
        held = self.evaluate(previous=previous)[self.KEY]
        self.assertEqual(held["state"], "warning")
        self.assertFalse(held["drift"]["clear_condition_met"])
        self.assertNotIn("held_open", held["drift"])
        with self.h._lock, self.h._conn:
            self.h._conn.execute(
                "UPDATE metric_rollups SET median=12.3 WHERE metric='battery.voltage'")
        cleared = self.evaluate(previous=previous)[self.KEY]
        self.assertEqual(cleared["state"], "normal")
        self.assertTrue(cleared["drift"]["clear_condition_met"])

    def test_parked_intervals_bounds(self):
        self.r.seed(AT - timedelta(days=30))
        trips = self.r.stops([12.45] * 2)
        self.r.flush()
        reader = dw._Reader(self.h, dw._to_us(AT))
        intervals = {i.after_trip_id: i for i in reader.parked_intervals()}
        first, last = trips
        self.assertEqual(intervals[first].start_us, self.r.ends[first] + 60_000_000)
        self.assertEqual(intervals[first].end_us,
                         self.r.ends[last] - 25 * MINUTE_US - 180_000_000)
        self.assertEqual(intervals[last].end_us, dw._to_us(AT))
        parked = reader.rows("battery.voltage", regime_prefix="engine_off:", parked=True)
        self.assertEqual(len(parked), 60)
        self.assertEqual(reader.rows("battery.voltage", regime_prefix="engine_off:"), [])


class TripComparisonTests(DriftCase):
    def history(self, **latest):
        self.r.seed(AT - timedelta(days=30))
        for day in range(20, 10, -1):
            self.r.engine_trip(AT - timedelta(days=day), coolant=195.0, oil_idle=30.0,
                               oil_low=40.0, low_share=0.0)
        for index, day in enumerate((2, 1)):
            values = {key: value[index] for key, value in latest.items()}
            self.r.engine_trip(AT - timedelta(days=day), **values)

    def test_idle_creep_two_trips_fires_and_early_minutes_are_ignored(self):
        self.history(coolant=(201.0, 201.0))
        a = self.evaluate()["engine_coolant_temperature_idle_creep"]
        self.assertEqual(a["state"], "warning")
        self.assertEqual(a["current"]["value"], 201.0)
        self.assertEqual(a["baseline"]["median"], 195.0)
        self.assertEqual(a["baseline"]["trip_count"], 10)
        self.assertEqual(a["deviation"]["signed_from_median"], 6.0)
        self.assertEqual(a["current"]["unit"], "°F")
        self.assertEqual(a["drift"]["recent_signed_from_median"], [6.0, 6.0])

    def test_idle_creep_one_trip_does_not_fire(self):
        self.history(coolant=(195.0, 201.0))
        a = self.evaluate()["engine_coolant_temperature_idle_creep"]
        self.assertEqual(a["state"], "normal")
        held = self.evaluate(previous={"engine_coolant_temperature_idle_creep": "warning"})
        self.assertEqual(held["engine_coolant_temperature_idle_creep"]["state"], "warning")

    def test_idle_creep_clears_below_half_the_threshold(self):
        self.history(coolant=(201.0, 197.0))
        previous = {"engine_coolant_temperature_idle_creep": "warning"}
        a = self.evaluate(previous=previous)["engine_coolant_temperature_idle_creep"]
        self.assertEqual(a["state"], "normal")

    def test_idle_creep_needs_reference_trips(self):
        self.r.seed(AT - timedelta(days=30))
        for day in (3, 2, 1):
            self.r.engine_trip(AT - timedelta(days=day), coolant=230.0)
        a = self.evaluate()["engine_coolant_temperature_idle_creep"]
        self.assertEqual(a["state"], "normal")
        self.assertTrue(a["reason"].startswith("not enough"), a["reason"])

    def test_oil_decline_on_idle_band_fires(self):
        self.history(oil_idle=(26.5, 26.5), oil_low=(40.0, 40.0))
        a = self.evaluate()["engine_oil_pressure_decline"]
        self.assertEqual(a["state"], "warning")
        self.assertEqual(a["drift"]["band"], "rpm_idle")
        self.assertEqual(a["current"]["value"], 26.5)
        self.assertEqual(a["baseline"]["median"], 30.0)
        self.assertAlmostEqual(a["deviation"]["signed_from_median"], -3.5)
        self.assertAlmostEqual(a["deviation"]["effect_in_rule_direction"], 3.5)
        self.assertEqual(a["action"], "Check the oil level and note it for the next service.")
        self.assertFalse(a["drift"]["bands"]["rpm_low"]["fires"])

    def test_oil_steady_is_normal(self):
        self.history(oil_idle=(28.0, 29.0), oil_low=(40.0, 39.0))
        a = self.evaluate()["engine_oil_pressure_decline"]
        self.assertEqual(a["state"], "normal")

    def test_charge_acceptance_three_trips_fire_two_do_not(self):
        self.r.seed(AT - timedelta(days=30))
        for day in range(20, 10, -1):
            self.r.engine_trip(AT - timedelta(days=day), low_share=0.0)
        self.r.engine_trip(AT - timedelta(days=3), low_share=0.0)
        self.r.engine_trip(AT - timedelta(days=2), low_share=0.45)
        self.r.engine_trip(AT - timedelta(days=1), low_share=0.45)
        a = self.evaluate()["battery_voltage_charge_acceptance"]
        self.assertEqual(a["state"], "normal")
        self.r.engine_trip(AT - timedelta(hours=12), low_share=0.45)
        a = self.evaluate()["battery_voltage_charge_acceptance"]
        self.assertEqual(a["state"], "warning")
        self.assertEqual(a["current"]["value"], 45.0)
        self.assertEqual(a["current"]["unit"], "%")
        self.assertEqual(a["drift"]["recent_percent"], [45.0, 45.0, 45.0])
        self.assertEqual(a["baseline"]["median"], 0.0)
        self.assertEqual(a["action"], "Have the battery and alternator tested.")

    def test_charge_acceptance_ignores_short_trips(self):
        self.r.seed(AT - timedelta(days=30))
        for day in (3, 2, 1):
            trip_id, started = self.r.trip(AT - timedelta(days=day))
            for minute in range(9):  # below the 10-bucket minimum
                self.r.add(trip_id, started + minute * MINUTE_US, "battery.voltage", LOW, 12.5, "V")
        a = self.evaluate()["battery_voltage_charge_acceptance"]
        self.assertEqual(a["state"], "normal")
        self.assertTrue(a["reason"].startswith("not enough"), a["reason"])


class LifecycleTests(DriftCase):
    def test_contract_fields_and_episode_open_then_resolve(self):
        self.r.seed(AT - timedelta(days=30))
        for day in range(20, 10, -1):
            self.r.engine_trip(AT - timedelta(days=day), coolant=195.0, oil_idle=30.0,
                               oil_low=40.0, low_share=0.0)
        self.r.engine_trip(AT - timedelta(days=3), coolant=195.0, oil_idle=30.0,
                           oil_low=40.0, low_share=0.45)
        self.r.engine_trip(AT - timedelta(days=2), coolant=195.0, oil_idle=26.5,
                           oil_low=40.0, low_share=0.45)
        self.r.engine_trip(AT - timedelta(days=1), coolant=195.0, oil_idle=26.5,
                           oil_low=40.0, low_share=0.45)
        for trip_id in sorted(self.r.ends)[-3:]:  # the last three trips: low settled stops
            self.r.parked(self.r.ends[trip_id] + 2 * MINUTE_US, 30, 12.0)
        first = list(self.evaluate().values())
        self.assert_c1(first)
        warnings = sorted(a["rule"] for a in first if a["state"] == "warning")
        self.assertEqual(warnings, ["battery_voltage_charge_acceptance",
                                    "battery_voltage_resting_low",
                                    "engine_oil_pressure_decline"])
        keys = tuple(a["rule"] for a in first)
        opened = self.h.record_advisory_assessments(
            first, evaluated_at=AT, authoritative_rule_keys=keys)
        self.assertEqual(opened.opened, 3)
        open_rules = sorted(e["rule"] for e in self.h.list_advisory_episodes(active_only=True))
        self.assertEqual(open_rules, warnings)
        # Re-persisting the same cached findings does not open anything new.
        again = self.h.record_advisory_assessments(
            first, evaluated_at=AT + timedelta(seconds=5), authoritative_rule_keys=keys)
        self.assertEqual(again.opened, 0)

        later = AT + timedelta(days=1)
        for hours in (4, 8):
            trip_id, _started = self.r.trip(AT + timedelta(hours=hours))
            self.r.parked(self.r.ends[trip_id] + 2 * MINUTE_US, 30, 12.7)
        trip_id = self.r.engine_trip(AT + timedelta(hours=12), coolant=195.0, oil_idle=30.0,
                                     oil_low=40.0, low_share=0.1)
        self.r.parked(self.r.ends[trip_id] + 2 * MINUTE_US, 30, 12.7)
        second = list(self.evaluate(at=later, previous=first).values())
        self.assert_c1(second)
        self.assertEqual([a["rule"] for a in second if a["state"] == "warning"], [])
        resolved = self.h.record_advisory_assessments(
            second, evaluated_at=later, authoritative_rule_keys=keys)
        self.assertEqual(resolved.resolved, 3)
        self.assertEqual(self.h.list_advisory_episodes(active_only=True), [])

    def test_insufficient_data_everywhere_is_all_normal(self):
        result = self.evaluate()
        self.assert_c1(list(result.values()))
        self.assertTrue(all(a["state"] == "normal" for a in result.values()))
        self.assertTrue(all(a["drift"]["sufficient"] is False for a in result.values()))

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            dw.evaluate_drift(self.h, at=AT.replace(tzinfo=None))


class StaticVehicleEvaluator:
    def evaluate(self, at=None):
        return {"schema_version": 1, "assessments": [], "active": []}


class StaticInfrastructureEvaluator:
    def evaluate(self, _snapshot_id=None, *, at=None):
        return {"schema_version": 1, "assessments": [], "active": []}


class BareHistorian:
    """No database, _conn or _lock (like tests/test_vehicle_insights.py)."""

    def close(self):
        pass


class InsightsDriftTests(DriftCase):
    def setUp(self):
        super().setUp()
        self.r.seed(AT - timedelta(days=30))
        for day in range(20, 10, -1):
            self.r.engine_trip(AT - timedelta(days=day), low_share=0.0)
        for day in (3, 2, 1):
            self.r.engine_trip(AT - timedelta(days=day), low_share=0.45)
        self.r.flush()
        self.rpm = definition("engine.rpm", "rpm")
        patcher = mock.patch.object(
            insights_module.time, "monotonic",
            side_effect=itertools.count(start=100.0, step=10_000.0),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def insights(self, historian=None):
        return TelemetryInsights(
            historian or self.h,
            warning_evaluator=StaticVehicleEvaluator(),
            infrastructure_evaluator=StaticInfrastructureEvaluator(),
            dtc_cache_path=Path(self.tmp.name) / "dtcs.json",
            history_metrics=("engine.rpm",),
        )

    def ingest(self, insights, at, key):
        payload = snapshot(at, [self.rpm], {"engine.rpm": available(self.rpm, 0.0, at)},
                           running=False)
        return insights.ingest_snapshot(payload, captured_at=at, ingest_key=key)

    def test_daily_job_merges_into_persisted_set_and_health(self):
        insights = self.insights()
        with mock.patch.object(dw, "evaluate_drift", wraps=dw.evaluate_drift) as spy:
            first = self.ingest(insights, AT, "first")
            self.assertEqual(spy.call_count, 1)
            self.assertTrue(first.advisory_checkpoint_complete)
            # Persisted before the job ran: nothing open yet.
            self.assertEqual(self.h.list_advisory_episodes(active_only=True), [])
            second = self.ingest(insights, AT + timedelta(hours=1), "second")
            self.assertEqual(spy.call_count, 1)  # not due again within 24 h
            self.assertTrue(second.advisory_checkpoint_complete)
            opened = self.h.list_advisory_episodes(active_only=True)
            self.assertEqual([e["rule"] for e in opened], ["battery_voltage_charge_acceptance"])
            self.ingest(insights, AT + timedelta(hours=25), "third")
            self.assertEqual(spy.call_count, 2)
            self.assertEqual(spy.call_args.kwargs["previous_state"]
                             ["battery_voltage_charge_acceptance"], "warning")
        health = insights.health_response()
        self.assertEqual(health["drift"]["last_run_at"], (AT + timedelta(hours=25)).isoformat())
        self.assertIsNone(health["drift"]["last_error"])
        self.assertEqual(health["drift"]["finding_count"], 1)
        rules = {a["rule"] for a in health["assessments"]}
        self.assertTrue(set(dw.DRIFT_RULE_KEYS) <= rules)
        self.assertEqual([a["rule"] for a in health["active"]],
                         ["battery_voltage_charge_acceptance"])
        # repeated calls never accumulate duplicates
        again = insights.health_response()
        self.assertEqual(len(again["assessments"]), len(health["assessments"]))
        hook = insights._advisory_hook["last_result"]
        self.assertEqual(hook["drift_assessments"], len(dw.DRIFT_RULE_KEYS))

        state_path = Path(self.tmp.name) / "drift-warnings.json"
        saved = dw.load_state(state_path)
        self.assertEqual(saved["last_run_at"], (AT + timedelta(hours=25)).isoformat())
        self.assertEqual(saved["assessments"], insights._drift_assessments())
        reopened = self.insights()
        self.assertEqual(reopened._drift_assessments(), insights._drift_assessments())
        self.assertEqual(reopened.drift_status()["last_run_at"], saved["last_run_at"])

    def test_failing_job_is_recorded_and_ingest_continues(self):
        insights = self.insights()
        with mock.patch.object(dw, "evaluate_drift", side_effect=RuntimeError("boom")):
            result = self.ingest(insights, AT, "first")
        self.assertFalse(result.duplicate)
        self.assertTrue(result.advisory_checkpoint_complete)
        self.assertEqual(insights.drift_status()["last_error"], "RuntimeError: boom")
        self.assertIsNone(insights.drift_status()["last_run_at"])
        self.assertIsNone(insights._maintenance_hook["last_error"])
        self.assertIsNotNone(insights._maintenance_hook["last_result"])
        self.assertEqual(insights.health_response()["drift"]["last_error"], "RuntimeError: boom")
        # the next hourly check retries and succeeds
        self.ingest(insights, AT + timedelta(hours=1), "second")
        self.assertIsNone(insights.drift_status()["last_error"])
        self.assertEqual(insights.drift_status()["finding_count"], 1)

    def test_maintenance_failure_still_runs_the_job(self):
        insights = self.insights()
        with mock.patch.object(self.h, "maybe_run_maintenance",
                               side_effect=RuntimeError("maintenance failed")):
            self.ingest(insights, AT, "first")
        self.assertEqual(insights._maintenance_hook["last_error"],
                         "RuntimeError: maintenance failed")
        self.assertEqual(insights.drift_status()["finding_count"], 1)

    def test_historian_without_database_and_corrupt_state(self):
        bare = self.insights(BareHistorian())
        self.assertIsNone(bare._drift_path)
        bare._maybe_run_drift(captured_at=AT)
        self.assertIn("AttributeError", bare.drift_status()["last_error"])
        with TelemetryHistorian(":memory:") as memory:
            self.assertIsNone(self.insights(memory)._drift_path)
        state_path = Path(self.tmp.name) / "drift-warnings.json"
        for content in ("{not json", json.dumps({"version": 1, "assessments": [{"rule": "x"}]}),
                        json.dumps({"version": 1, "last_run_at": None,
                                    "assessments": [{"rule": "tire_pressure_rr_slow_leak"}]})):
            state_path.write_text(content)
            loaded = self.insights()
            self.assertEqual(loaded._drift_assessments(), [])
            self.assertIsNone(loaded.drift_status()["last_run_at"])

    def test_state_from_another_rule_revision_is_rerun_at_first_check(self):
        insights = self.insights()
        self.ingest(insights, AT, "first")
        state_path = Path(self.tmp.name) / "drift-warnings.json"
        saved = dw.load_state(state_path)
        self.assertTrue(dw.state_is_current(saved["assessments"]))
        saved["assessments"][0]["rule_revision"] = "older-revision"
        dw.save_state(state_path, last_run_at=saved["last_run_at"],
                      assessments=saved["assessments"])
        reopened = self.insights()
        self.assertIsNone(reopened.drift_status()["last_run_at"])
        self.assertEqual(len(reopened._drift_assessments()), len(dw.DRIFT_RULE_KEYS))
        with mock.patch.object(dw, "evaluate_drift", wraps=dw.evaluate_drift) as spy:
            self.ingest(reopened, AT + timedelta(hours=1), "second")
        self.assertEqual(spy.call_count, 1)
        self.assertTrue(dw.state_is_current(dw.load_state(state_path)["assessments"]))

    def test_duplicate_rules_in_state_file_are_rejected(self):
        a = dw.evaluate_drift(self.h, at=AT)[0]
        path = Path(self.tmp.name) / "state.json"
        dw.save_state(path, last_run_at=AT.isoformat(), assessments=[a, a])
        self.assertIsNone(dw.load_state(path))
        dw.save_state(path, last_run_at=AT.isoformat(), assessments=[a])
        self.assertEqual(dw.load_state(path)["assessments"], [a])
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()
                          if p.name.endswith(".tmp")], [])


class RuntimeTests(DriftCase):
    def test_forty_five_days_of_rollups_evaluate_quickly(self):
        self.r.seed(AT - timedelta(days=46))
        rows = 0
        for day in range(45, 0, -1):
            start = AT - timedelta(days=day) + timedelta(hours=8)
            trip_id, started = self.r.trip(start, minutes=60)
            for minute in range(55):
                bucket = started + minute * MINUTE_US
                motion = "stationary" if minute < 2 else "urban"
                self.r.add(trip_id, bucket, "vehicle.speed", f"engine_running:{motion}:rpm_low:warm",
                           0.0 if minute < 2 else 30.0, "mph")
                self.r.add(trip_id, bucket, "tire.pressure.rr", f"engine_running:{motion}:rpm_low:warm",
                           78.0 + (minute % 3) * 0.4, "psi")
                self.r.add(trip_id, bucket, "engine.coolant_temperature", IDLE,
                           195.0 + minute % 4, "°F")
                self.r.add(trip_id, bucket, "engine.oil_pressure", IDLE if minute % 2 else LOW,
                           30.0 + minute % 3, "psi")
                self.r.add(trip_id, bucket, "battery.voltage", LOW, 14.0 - (minute % 5) * 0.3, "V")
                rows += 5
            self.r.parked(started + 62 * MINUTE_US, 120, 12.4 + (day % 3) * 0.05)
            rows += 120
        self.r.flush()
        self.assertGreaterEqual(rows, 10_000)
        started = time.perf_counter()
        result = by_rule(dw.evaluate_drift(self.h, at=AT))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(result), len(dw.DRIFT_RULE_KEYS))
        self.assertEqual(result["tire_pressure_rr_slow_leak"]["state"], "normal")
        self.assertEqual(result["battery_voltage_resting_low"]["state"], "normal")
        self.assertEqual(result["battery_voltage_resting_low"]["drift"]["stops_in_window"], 7)


if __name__ == "__main__":
    unittest.main()
