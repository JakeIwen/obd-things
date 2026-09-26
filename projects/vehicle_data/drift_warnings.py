"""Tier-2 drift notices: slow trends across trips, read from one-minute rollups.

The job runs at most once a day from ``TelemetryInsights``' maintenance hook,
never on the 5-second evaluation path.  It is read-only: every query touches
only ``trips`` and ``metric_rollups`` (index ``metric_rollups_baseline``),
bounded by ``metric`` and a 45-day ``bucket_us`` window, and it never reads
``metric_samples``.  Findings (warnings-redesign.md section 5):

- ``tire_pressure_<w>_slow_leak``: per-trip cold-start pressure (first minutes
  after a parked gap of at least 4 h) trending down against the axle mate.
  Cold-window buckets that still carry the RF hub's cached pre-trip reading
  are dropped, and the wheel's own series must fall too, so a common-mode
  change (weather, all four tires) or an inflated mate never reads as a leak.
- ``engine_coolant_temperature_idle_creep``: warm idle coolant creeping up.
- ``engine_oil_pressure_decline``: warm idle / low-rpm oil pressure falling.
- ``battery_voltage_charge_acceptance``: many running minutes below 13.0 V.
- ``battery_voltage_resting_low``: settled engine-off voltage per parked stop
  (parked ``trip_key = 0`` rollups at least 60 s after the engine stopped and
  ending at least 120 s before the next trip, so key-on and cranking dips never
  count) low over the last 7 days or falling steadily.

Every assessment is a tier-2 ``notice`` whose state is ``warning`` or
``normal`` (``normal`` with a reason when data is insufficient; a notice that
is already open stays open with ``drift.held_open`` until it can be
re-checked).  Its
``rule_snapshot`` carries no ``recovery_*`` key, so the historian resolves an
episode as soon as a finding returns ``normal``.  A finding that is already a
warning (``previous_state``) stays one until its clear condition holds.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
from typing import Iterable, Mapping, Sequence

from projects.vehicle_data.event_history import digest


EVALUATOR_REVISION = "drift-v1"
STATE_VERSION = 1
STATE_FILENAME = "drift-warnings.json"

MICROSECONDS = 1_000_000
DAY_US = 24 * 60 * 60 * MICROSECONDS
LOOKBACK_DAYS = 45
ROLLUP_SECONDS = 60
NOTIFICATION_RATE_LIMIT_SECONDS = 86_400  # legacy-valid; tier-2 cooldown lives in the historian
MAX_POINTS = 60

# Tire slow leak (psi, psi/week).
SLOW_LEAK_SLOPE = -0.5
SLOW_LEAK_DELTA = -3.0
SLOW_LEAK_CLEAR_SLOPE = -0.25
SLOW_LEAK_CLEAR_DELTA = -1.5
SLOW_LEAK_SLOPE_DAYS = 14
SLOW_LEAK_REFERENCE_DAYS = 30
SLOW_LEAK_MIN_POINTS = 4
SLOW_LEAK_RECENT_POINTS = 3
SLOW_LEAK_MIN_GAP_SECONDS = 4 * 60 * 60
SLOW_LEAK_COLD_SECONDS_AFTER_MOVING = 180
# Cold buckets within this of the wheel's last pre-trip bucket are the RF hub's
# cached reading, not a fresh cold measurement (evidence-iter2.txt section B-C).
SLOW_LEAK_STALE_TOLERANCE_PSI = 0.05
# Fraction of the notice thresholds the wheel's own series must also show.
SLOW_LEAK_OWN_CORROBORATION = 0.5
AXLE_MATE = {"fl": "fr", "fr": "fl", "rl": "rr", "rr": "rl"}

# Trip-over-trip comparisons (coolant idle creep, oil pressure decline).
RECENT_TRIPS = 2
REFERENCE_TRIPS = 10
MIN_REFERENCE_TRIPS = 5
MIN_TRIP_BUCKETS = 3

IDLE_CREEP_F = 5.0
IDLE_CREEP_CLEAR_F = 2.5
IDLE_CREEP_REGIME = "engine_running:stationary:rpm_idle:warm"
IDLE_CREEP_SETTLE_SECONDS = 10 * 60

OIL_DECLINE_PSI = -3.0
OIL_DECLINE_CLEAR_PSI = -1.5
OIL_BANDS = ("rpm_idle", "rpm_low")

# Charge acceptance (fraction of running one-minute buckets below 13.0 V).
CHARGE_ACCEPTANCE_FRACTION = 0.40
CHARGE_ACCEPTANCE_CLEAR_FRACTION = 0.25
CHARGE_LOW_VOLTS = 13.0
CHARGE_MIN_BUCKETS = 10
CHARGE_CONSECUTIVE_TRIPS = 3

# Settled resting (engine-off) voltage per parked stop.
RESTING_LOW_VOLTS = 12.2
RESTING_CLEAR_VOLTS = 12.3
RESTING_SLOPE_V_PER_DAY = -0.1
RESTING_SLOPE_CLEAR_V_PER_DAY = -0.05
RESTING_WINDOW_DAYS = 7
RESTING_REFERENCE_DAYS = 30
RESTING_MIN_STOPS = 3
RESTING_MIN_STOPS_SLOPE = 5
RESTING_SETTLE_SECONDS = 60
RESTING_PRE_CRANK_SECONDS = 120
RESTING_REGIME_PREFIXES = ("engine_off:", "engine_unknown:")

MOVING_MOTIONS = ("urban", "road", "highway")
WHEELS = ("fl", "fr", "rl", "rr")

TIRE_RULE_KEYS = tuple(f"tire_pressure_{wheel}_slow_leak" for wheel in WHEELS)
IDLE_CREEP_KEY = "engine_coolant_temperature_idle_creep"
OIL_DECLINE_KEY = "engine_oil_pressure_decline"
CHARGE_ACCEPTANCE_KEY = "battery_voltage_charge_acceptance"
RESTING_VOLTAGE_KEY = "battery_voltage_resting_low"
DRIFT_RULE_KEYS: tuple[str, ...] = (
    *TIRE_RULE_KEYS,
    IDLE_CREEP_KEY,
    OIL_DECLINE_KEY,
    CHARGE_ACCEPTANCE_KEY,
    RESTING_VOLTAGE_KEY,
)
FINDING_STATES = frozenset(("warning", "normal"))


@dataclass(frozen=True)
class _Spec:
    key: str
    title: str
    metric: str
    direction: str
    group: str
    action: str
    unit: str
    interpretation: str
    regime: str
    snapshot: Mapping[str, object]


@dataclass(frozen=True)
class _Trip:
    id: int
    started_us: int
    started_at: str
    gap_seconds: float | None


@dataclass(frozen=True)
class _Row:
    bucket_us: int
    trip_key: int
    regime: str
    identity: tuple[str, str, str, str]  # unit, source, quality, provenance
    median: float


@dataclass(frozen=True)
class _Point:
    trip: _Trip
    value: float
    buckets: int


@dataclass(frozen=True)
class _Interval:
    after_trip_id: int
    start_us: int
    end_us: int


@dataclass(frozen=True)
class _RestPoint:
    at_us: int
    value: float
    buckets: int
    after_trip_id: int


def _tire_spec(wheel: str) -> _Spec:
    label = wheel.upper()
    return _Spec(
        key=f"tire_pressure_{wheel}_slow_leak",
        title=f"{label} tire slow leak",
        metric=f"tire.pressure.{wheel}",
        direction="low",
        group="tires",
        action=f"Check the {label} tire for a slow leak.",
        unit="psi",
        interpretation=(
            "Cold-start tire pressure across trips, read from one-minute summaries; "
            "a slow trend notice, not a diagnosis. Confirm with a gauge on a cold tire."
        ),
        regime="cold_start",
        snapshot={
            "method": "cold_start_trend_axle_relative",
            "mate": AXLE_MATE[wheel],
            "stale_tolerance_psi": SLOW_LEAK_STALE_TOLERANCE_PSI,
            "own_corroboration_fraction": SLOW_LEAK_OWN_CORROBORATION,
            "slope_fire_psi_per_week": SLOW_LEAK_SLOPE,
            "delta_fire_psi": SLOW_LEAK_DELTA,
            "slope_clear_psi_per_week": SLOW_LEAK_CLEAR_SLOPE,
            "delta_clear_psi": SLOW_LEAK_CLEAR_DELTA,
            "slope_window_days": SLOW_LEAK_SLOPE_DAYS,
            "reference_window_days": SLOW_LEAK_REFERENCE_DAYS,
            "minimum_points": SLOW_LEAK_MIN_POINTS,
            "recent_points": SLOW_LEAK_RECENT_POINTS,
            "minimum_parked_gap_seconds": SLOW_LEAK_MIN_GAP_SECONDS,
            "cold_seconds_after_moving": SLOW_LEAK_COLD_SECONDS_AFTER_MOVING,
            "moving_motions": list(MOVING_MOTIONS),
        },
    )


SPECS: dict[str, _Spec] = {spec.key: spec for spec in (_tire_spec(w) for w in WHEELS)}
SPECS[IDLE_CREEP_KEY] = _Spec(
    key=IDLE_CREEP_KEY,
    title="Coolant idle creep",
    metric="engine.coolant_temperature",
    direction="high",
    group="cooling",
    action="Check the coolant level and fan at the next stop.",
    unit="°F",
    interpretation=(
        "Warm idle coolant temperature per trip compared with this van's earlier "
        "trips; a slow trend notice, not a diagnosis or an overheat limit."
    ),
    regime=IDLE_CREEP_REGIME,
    snapshot={
        "method": "trip_median_vs_prior_trips",
        "regime": IDLE_CREEP_REGIME,
        "settle_seconds": IDLE_CREEP_SETTLE_SECONDS,
        "fire_f": IDLE_CREEP_F,
        "clear_f": IDLE_CREEP_CLEAR_F,
        "recent_trips": RECENT_TRIPS,
        "reference_trips": REFERENCE_TRIPS,
        "minimum_reference_trips": MIN_REFERENCE_TRIPS,
        "minimum_trip_buckets": MIN_TRIP_BUCKETS,
    },
)
SPECS[OIL_DECLINE_KEY] = _Spec(
    key=OIL_DECLINE_KEY,
    title="Oil pressure trending down",
    metric="engine.oil_pressure",
    direction="low",
    group="oil",
    action="Check the oil level and note it for the next service.",
    unit="psi",
    interpretation=(
        "Warm oil pressure at idle and low rpm per trip compared with this van's "
        "earlier trips; a slow trend notice, not an OEM limit or a diagnosis."
    ),
    regime="engine_running:*:rpm_idle|rpm_low:warm",
    snapshot={
        "method": "trip_median_vs_prior_trips",
        "rpm_bands": list(OIL_BANDS),
        "thermal": "warm",
        "fire_psi": OIL_DECLINE_PSI,
        "clear_psi": OIL_DECLINE_CLEAR_PSI,
        "recent_trips": RECENT_TRIPS,
        "reference_trips": REFERENCE_TRIPS,
        "minimum_reference_trips": MIN_REFERENCE_TRIPS,
        "minimum_trip_buckets": MIN_TRIP_BUCKETS,
    },
)
SPECS[CHARGE_ACCEPTANCE_KEY] = _Spec(
    key=CHARGE_ACCEPTANCE_KEY,
    title="Battery charging weak",
    metric="battery.voltage",
    direction="high",
    group="charging",
    action="Have the battery and alternator tested.",
    unit="%",
    interpretation=(
        "Share of engine-running minutes per trip with voltage below 13.0 V; a slow "
        "trend notice, not a battery or alternator test."
    ),
    regime="engine_running:*",
    snapshot={
        "method": "trip_low_voltage_share",
        "low_volts": CHARGE_LOW_VOLTS,
        "fire_fraction": CHARGE_ACCEPTANCE_FRACTION,
        "clear_fraction": CHARGE_ACCEPTANCE_CLEAR_FRACTION,
        "consecutive_trips": CHARGE_CONSECUTIVE_TRIPS,
        "minimum_trip_buckets": CHARGE_MIN_BUCKETS,
        "reference_trips": REFERENCE_TRIPS,
    },
)
SPECS[RESTING_VOLTAGE_KEY] = _Spec(
    key=RESTING_VOLTAGE_KEY,
    title="Battery resting voltage low",
    metric="battery.voltage",
    direction="low",
    group="battery",
    action="Charge the battery or have it tested soon.",
    unit="V",
    interpretation=(
        "Settled engine-off voltage per parked stop (at least 60 s after the engine "
        "stopped, never key-on or cranking dips) over the last 7 days; a slow trend "
        "notice, not a battery test."
    ),
    regime="engine_off:*",
    snapshot={
        "method": "settled_stop_level_and_slope",
        "low_volts": RESTING_LOW_VOLTS,
        "clear_volts": RESTING_CLEAR_VOLTS,
        "slope_fire_v_per_day": RESTING_SLOPE_V_PER_DAY,
        "slope_clear_v_per_day": RESTING_SLOPE_CLEAR_V_PER_DAY,
        "window_days": RESTING_WINDOW_DAYS,
        "reference_window_days": RESTING_REFERENCE_DAYS,
        "minimum_stops": RESTING_MIN_STOPS,
        "minimum_stops_slope": RESTING_MIN_STOPS_SLOPE,
        "settle_seconds": RESTING_SETTLE_SECONDS,
        "pre_crank_seconds": RESTING_PRE_CRANK_SECONDS,
        "regime_prefixes": list(RESTING_REGIME_PREFIXES),
    },
)


def rule_snapshot(key: str) -> dict[str, object]:
    """Complete, JSON-safe rule configuration whose digest is ``rule_revision``."""

    spec = SPECS[key]
    return {
        "key": spec.key,
        "metric": spec.metric,
        "direction": spec.direction,
        "group": spec.group,
        "tier": 2,
        "evaluator_revision": EVALUATOR_REVISION,
        "lookback_days": LOOKBACK_DAYS,
        "rollup_seconds": ROLLUP_SECONDS,
        **json.loads(json.dumps(dict(spec.snapshot))),
    }


# --------------------------------------------------------------------------
# time and small statistics helpers


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _to_us(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(microseconds=1)


def _iso(us: int) -> str:
    return (_EPOCH + timedelta(microseconds=us)).isoformat()


def _utc(moment: object, label: str) -> datetime:
    if not isinstance(moment, datetime):
        raise TypeError(f"{label} must be a datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _median(values: Iterable[float]) -> float:
    return float(statistics.median(list(values)))


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _slope_per_week(points: Sequence[_Point]) -> float | None:
    """Ordinary least squares slope of value against trip start, per week."""

    if len(points) < 2:
        return None
    origin = points[0].trip.started_us
    xs = [(p.trip.started_us - origin) / DAY_US for p in points]
    ys = [p.value for p in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return sxy / sxx * 7.0


def _point_list(points: Sequence[_Point]) -> list[list[object]]:
    return [
        [p.trip.id, p.trip.started_at, _round(p.value)]
        for p in list(points)[-MAX_POINTS:]
    ]


def _regime_parts(regime: str) -> tuple[str, str, str, str] | None:
    parts = regime.split(":")
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def _latest_identity(rows: Iterable[_Row]) -> tuple[str, str, str, str] | None:
    latest = None
    for row in rows:
        if latest is None or (row.bucket_us, row.identity) > (
            latest.bucket_us,
            latest.identity,
        ):
            latest = row
    return latest.identity if latest is not None else None


# --------------------------------------------------------------------------
# bounded read-only queries


class _Reader:
    """Every query holds the historian lock only for its own duration."""

    def __init__(self, historian: object, at_us: int) -> None:
        self.conn = historian._conn  # noqa: SLF001 - event_history.py precedent
        self.lock = historian._lock  # noqa: SLF001
        config = getattr(historian, "config", None)
        seconds = getattr(config, "rollup_seconds", ROLLUP_SECONDS)
        self.bucket_seconds = seconds if isinstance(seconds, int) else ROLLUP_SECONDS
        self.at_us = at_us
        self.lower_us = at_us - LOOKBACK_DAYS * DAY_US

    def _fetch(self, sql: str, params: Sequence[object]) -> list[Sequence[object]]:
        with self.lock:
            return list(self.conn.execute(sql, tuple(params)).fetchall())

    def completed_trips(self) -> list[_Trip]:
        """Trips that started in the window and ended by ``at``, with parked gaps."""

        rows = self._fetch(
            """
            SELECT id, started_us, started_at, last_active_us, ended_us
            FROM trips WHERE started_us >= ? AND started_us < ?
            ORDER BY started_us, id
            """,
            (self.lower_us, self.at_us),
        )
        prior = self._fetch(
            """
            SELECT last_active_us FROM trips WHERE started_us < ?
            ORDER BY started_us DESC, id DESC LIMIT 1
            """,
            (self.lower_us,),
        )
        previous_active = int(prior[0][0]) if prior else None
        trips: list[_Trip] = []
        for trip_id, started_us, started_at, last_active_us, ended_us in rows:
            started_us = int(started_us)
            gap = (
                (started_us - previous_active) / MICROSECONDS
                if previous_active is not None
                else None
            )
            previous_active = int(last_active_us)
            if ended_us is None or int(ended_us) > self.at_us:
                continue
            trips.append(_Trip(int(trip_id), started_us, str(started_at), gap))
        return trips

    def first_moving(self) -> dict[int, int]:
        """First vehicle.speed bucket per trip whose motion band is moving."""

        motion = " OR ".join("regime LIKE ?" for _ in MOVING_MOTIONS)
        rows = self._fetch(
            f"""
            SELECT trip_key, min(bucket_us) FROM metric_rollups
            WHERE metric='vehicle.speed' AND bucket_us >= ? AND bucket_us < ?
              AND bucket_seconds=? AND trip_key != 0 AND ({motion})
            GROUP BY trip_key
            """,
            (
                self.lower_us,
                self.at_us,
                self.bucket_seconds,
                *(f"%:{name}:%" for name in MOVING_MOTIONS),
            ),
        )
        return {int(trip_key): int(first) for trip_key, first in rows}

    def parked_intervals(self) -> list[_Interval]:
        """Settled parked spans between trips (and after the last one, up to ``at``).

        Each span starts ``RESTING_SETTLE_SECONDS`` after the trip's last activity
        and its last bucket ends ``RESTING_PRE_CRANK_SECONDS`` before the next trip
        starts, so key-on and cranking dips fall outside it.
        """

        rows = self._fetch(
            """
            SELECT id, started_us, last_active_us
            FROM trips WHERE started_us >= ? AND started_us < ?
            ORDER BY started_us, id
            """,
            (self.lower_us - DAY_US, self.at_us),
        )
        settle_us = RESTING_SETTLE_SECONDS * MICROSECONDS
        pre_crank_us = (RESTING_PRE_CRANK_SECONDS + self.bucket_seconds) * MICROSECONDS
        intervals: list[_Interval] = []
        for index, (trip_id, _started_us, last_active_us) in enumerate(rows):
            start = int(last_active_us) + settle_us
            if index + 1 < len(rows):
                end = int(rows[index + 1][1]) - pre_crank_us
            else:
                end = self.at_us
            if end >= start:
                intervals.append(_Interval(int(trip_id), start, end))
        return intervals

    def rows(
        self,
        metric: str,
        *,
        regime: str | None = None,
        regime_prefix: str | None = None,
        parked: bool = False,
    ) -> list[_Row]:
        clauses = [
            "metric=?",
            "bucket_us >= ?",
            "bucket_us < ?",
            "bucket_seconds=?",
            "trip_key = 0" if parked else "trip_key != 0",
        ]
        params: list[object] = [metric, self.lower_us, self.at_us, self.bucket_seconds]
        if regime is not None:
            clauses.insert(1, "regime=?")
            params.insert(1, regime)
        elif regime_prefix is not None:
            # A half-open range keeps the (metric, regime, bucket_us) index usable.
            clauses.insert(1, "regime >= ? AND regime < ?")
            params[1:1] = [regime_prefix, regime_prefix[:-1] + chr(ord(regime_prefix[-1]) + 1)]
        fetched = self._fetch(
            f"""
            SELECT bucket_us, trip_key, regime, unit, source, quality, provenance, median
            FROM metric_rollups WHERE {' AND '.join(clauses)}
            """,
            params,
        )
        result = []
        for bucket_us, trip_key, row_regime, unit, source, quality, provenance, median in fetched:
            if median is None or not math.isfinite(float(median)):
                continue
            result.append(
                _Row(
                    int(bucket_us),
                    int(trip_key),
                    str(row_regime),
                    (str(unit), str(source), str(quality), str(provenance)),
                    float(median),
                )
            )
        return result


# --------------------------------------------------------------------------
# assessment assembly


def _assessment(
    spec: _Spec,
    *,
    state: str,
    reason: str,
    current: Mapping[str, object],
    baseline: Mapping[str, object] | None,
    deviation: Mapping[str, object] | None,
    drift: Mapping[str, object],
    persistence: Mapping[str, object],
    evaluated_at: str,
) -> dict[str, object]:
    snapshot = rule_snapshot(spec.key)
    return {
        "rule": spec.key,
        "title": spec.title,
        "metric": spec.metric,
        "direction": spec.direction,
        "category": "vehicle_health",
        "severity": "notice",
        "tier": 2,
        "group": spec.group,
        "action": spec.action,
        "confidence": "medium",
        "advisory": True,
        "state": state,
        "reason": reason,
        "notification_eligible": state == "warning",
        "notification_rate_limit_seconds": NOTIFICATION_RATE_LIMIT_SECONDS,
        "evaluator_revision": EVALUATOR_REVISION,
        "rule_snapshot": snapshot,
        "rule_revision": digest(snapshot),
        "interpretation": spec.interpretation,
        "regime": spec.regime,
        "baseline_regime": spec.regime,
        "generated_at": evaluated_at,
        "current": dict(current),
        "baseline": dict(baseline) if baseline is not None else None,
        "deviation": dict(deviation) if deviation is not None else None,
        "absolute_threshold": None,
        "persistence": dict(persistence),
        "drift": dict(drift),
    }


def _current(
    spec: _Spec,
    *,
    value: float | None,
    trip: _Trip | None,
    identity: tuple[str, str, str, str] | None,
    unit: str | None = None,
) -> dict[str, object]:
    return {
        "metric": spec.metric,
        "value": _round(value),
        "unit": unit or (identity[0] if identity else spec.unit),
        "observed_at": trip.started_at if trip is not None else None,
        "source": identity[1] if identity else None,
        "quality": identity[2] if identity else None,
        "provenance": identity[3] if identity else None,
        "trip_id": None,
    }


HELD_SUFFIX = "; the notice stays open until it can be re-checked"


def _hysteresis(previous: str | None, *, fires: bool, clears: bool) -> str:
    if previous == "warning":
        return "normal" if clears else "warning"
    return "warning" if fires else "normal"


def _insufficient(
    spec: _Spec,
    *,
    reason: str,
    identity: tuple[str, str, str, str] | None,
    drift: Mapping[str, object],
    observed: int,
    required: int,
    evaluated_at: str,
    previous: str | None = None,
) -> dict[str, object]:
    """Not enough data to judge.  An open notice is held, never resolved."""

    held = previous == "warning"
    unit = spec.unit if spec.unit in ("%", "V") else None
    current = _current(spec, value=None, trip=None, identity=identity, unit=unit)
    drift = {**drift, "sufficient": False, "previous_state": previous}
    baseline = deviation = None
    if held:
        reason = f"{reason}{HELD_SUFFIX}"
        drift["held_open"] = True
        baseline = {"median": None, "unit": current["unit"], "bucket_count": 0,
                    "trip_count": observed}
        deviation = {"signed_from_median": None, "effect_in_rule_direction": None,
                     "threshold": None, "basis": "not enough data to re-check"}
    assessment = _assessment(
        spec,
        state="warning" if held else "normal",
        reason=reason,
        current=current,
        baseline=baseline,
        deviation=deviation,
        drift=drift,
        persistence={"observed": observed, "required": required, "satisfied": False,
                     "basis": "trips"},
        evaluated_at=evaluated_at,
    )
    if held:
        # A held notice keeps its event open but has nothing new to say.  If
        # the event was closed meanwhile (a rule-revision change retires it),
        # the held assessment opens a new one and must not push a notice
        # whose only content is "not enough data".
        assessment["notification_eligible"] = False
    return assessment


# --------------------------------------------------------------------------
# findings


class _SortedRows(list):
    """Rows sorted by bucket (then identity) with a cached key list for ``bisect``."""

    def __init__(self, rows: Iterable[_Row]) -> None:
        super().__init__(sorted(rows, key=lambda row: (row.bucket_us, row.identity)))
        self.keys = [row.bucket_us for row in self]


def _pre_trip_value(rows: Sequence[_Row], trip: _Trip) -> float | None:
    """Median of the wheel's newest bucket before ``trip`` that is not ``trip``'s own.

    This is normally the previous trip's last bucket (or a parked bucket, if
    parked tire rollups ever exist): the value the RF hub keeps reporting until
    the sensor wakes and transmits again.
    """

    ordered = rows if isinstance(rows, _SortedRows) else _SortedRows(rows)
    index = bisect.bisect_left(ordered.keys, trip.started_us)
    while index > 0:
        index -= 1
        row = ordered[index]
        if row.trip_key != trip.id:
            return row.median
    return None


def _cold_values(
    trip: _Trip,
    cold_rows: Sequence[_Row],
    pre_value: float | None,
) -> tuple[list[_Row], int]:
    """Cold-window rows minus the cached (pre-trip) ones, and the dropped count."""

    if pre_value is None:
        return list(cold_rows), 0
    kept = [
        row for row in cold_rows
        if abs(row.median - pre_value) > SLOW_LEAK_STALE_TOLERANCE_PSI
    ]
    return kept, len(cold_rows) - len(kept)


def _tire_points(
    wheel: str,
    *,
    trips: Sequence[_Trip],
    first_moving: Mapping[int, int],
    rows: Sequence[_Row],
) -> tuple[list[_Point], dict[str, object], tuple[str, str, str, str] | None]:
    """Non-cached cold-start points for one wheel, stale stats and the identity."""

    ordered = rows if isinstance(rows, _SortedRows) else _SortedRows(rows)
    by_trip: dict[int, list[_Row]] = {}
    for row in ordered:
        by_trip.setdefault(row.trip_key, []).append(row)
    stats = {
        "stale_excluded_trips": 0,
        "stale_excluded_buckets": 0,
        "trips_excluded_short_parked_gap": 0,
        "first_moving_fallbacks": 0,
    }
    candidates: list[tuple[_Trip, list[_Row]]] = []
    for trip in trips:
        if trip.gap_seconds is None or trip.gap_seconds < SLOW_LEAK_MIN_GAP_SECONDS:
            stats["trips_excluded_short_parked_gap"] += 1
            continue
        limit = first_moving.get(trip.id, trip.started_us) + (
            SLOW_LEAK_COLD_SECONDS_AFTER_MOVING * MICROSECONDS
        )
        cold = [row for row in by_trip.get(trip.id, ()) if row.bucket_us <= limit]
        if not cold:
            continue
        if trip.id not in first_moving:
            stats["first_moving_fallbacks"] += 1
        kept, dropped = _cold_values(trip, cold, _pre_trip_value(ordered, trip))
        stats["stale_excluded_buckets"] += dropped
        if not kept:
            stats["stale_excluded_trips"] += 1
            continue
        candidates.append((trip, kept))
    identity = _latest_identity(row for _trip, kept in candidates for row in kept)
    points: list[_Point] = []
    for trip, kept in candidates:
        values = [row.median for row in kept if row.identity == identity]
        if values:
            points.append(_Point(trip, _median(values), len(values)))
    return points, stats, identity


def _delta_and_slope(
    points: Sequence[_Point], at_us: int,
) -> tuple[list[_Point], list[_Point], float | None, float | None]:
    reference = [
        p for p in points
        if p.trip.started_us >= at_us - SLOW_LEAK_REFERENCE_DAYS * DAY_US
    ]
    recent = [
        p for p in reference
        if p.trip.started_us >= at_us - SLOW_LEAK_SLOPE_DAYS * DAY_US
    ]
    slope = _slope_per_week(recent) if len(recent) >= SLOW_LEAK_MIN_POINTS else None
    delta = None
    if reference:
        delta = (
            _median(p.value for p in reference[-SLOW_LEAK_RECENT_POINTS:])
            - _median(p.value for p in reference)
        )
    return reference, recent, slope, delta


def _tire_finding(
    wheel: str,
    *,
    trips: Sequence[_Trip],
    first_moving: Mapping[int, int],
    rows: Sequence[_Row],
    mate_rows: Sequence[_Row],
    at_us: int,
    previous: str | None,
    evaluated_at: str,
) -> dict[str, object]:
    spec = SPECS[f"tire_pressure_{wheel}_slow_leak"]
    label = wheel.upper()
    mate = AXLE_MATE[wheel]
    mate_label = mate.upper()
    own_points, stats, identity = _tire_points(
        wheel, trips=trips, first_moving=first_moving, rows=rows)
    mate_points, mate_stats, _mate_identity = _tire_points(
        mate, trips=trips, first_moving=first_moving, rows=mate_rows)
    mate_by_trip = {p.trip.id: p for p in mate_points}
    relative = [
        _Point(p.trip, p.value - mate_by_trip[p.trip.id].value, p.buckets)
        for p in own_points if p.trip.id in mate_by_trip
    ]
    rel_reference, rel_recent, rel_slope, rel_delta = _delta_and_slope(relative, at_us)
    reference, recent, own_slope, own_delta = _delta_and_slope(own_points, at_us)
    cached = stats["stale_excluded_buckets"]
    drift: dict[str, object] = {
        "method": "cold_start_trend_axle_relative",
        "mate": mate,
        "points": _point_list(reference),
        "relative_points": _point_list(rel_reference),
        "window_days": SLOW_LEAK_REFERENCE_DAYS,
        "slope_window_days": SLOW_LEAK_SLOPE_DAYS,
        "cold_start_count": len(reference),
        "paired_cold_start_count": len(rel_reference),
        "slope_points": len(rel_recent),
        **stats,
        "mate_stats": {**mate_stats, "cold_start_count": sum(
            1 for p in mate_points
            if p.trip.started_us >= at_us - SLOW_LEAK_REFERENCE_DAYS * DAY_US)},
    }
    if len(rel_reference) < SLOW_LEAK_MIN_POINTS:
        return _insufficient(
            spec,
            reason=(
                f"not enough paired cold starts ({len(rel_reference)} of "
                f"{SLOW_LEAK_MIN_POINTS} in {SLOW_LEAK_REFERENCE_DAYS} days; "
                f"{cached} cached readings skipped)"
            ),
            identity=identity,
            drift={**drift, "slope_psi_per_week": None, "delta_psi": None,
                   "relative_slope_psi_per_week": None, "relative_delta_psi": None},
            observed=len(rel_reference),
            required=SLOW_LEAK_MIN_POINTS,
            evaluated_at=evaluated_at,
            previous=previous,
        )
    assert rel_delta is not None and own_delta is not None
    usual_offset = _median(p.value for p in rel_reference)
    slope_fires = rel_slope is not None and rel_slope <= SLOW_LEAK_SLOPE
    delta_fires = rel_delta <= SLOW_LEAK_DELTA
    own_corroborates = (
        own_slope is not None and own_slope <= SLOW_LEAK_SLOPE * SLOW_LEAK_OWN_CORROBORATION
    ) or own_delta <= SLOW_LEAK_DELTA * SLOW_LEAK_OWN_CORROBORATION
    fires = (slope_fires or delta_fires) and own_corroborates
    clears = (
        (rel_slope is None or rel_slope > SLOW_LEAK_CLEAR_SLOPE)
        and rel_delta > SLOW_LEAK_CLEAR_DELTA
    )
    state = _hysteresis(previous, fires=fires, clears=clears)
    slope_text = (
        f"{rel_slope:+.2f} psi a week" if rel_slope is not None
        else "trend needs more cold starts"
    )
    if state == "warning" and fires:
        if slope_fires:
            reason = (
                f"{label} cold pressure {abs(rel_slope):.1f} psi a week below "
                f"{mate_label}'s over {len(rel_recent)} cold starts"
            )
        else:
            reason = (
                f"{label} cold pressure {abs(rel_delta):.1f} psi below its usual "
                f"gap to {mate_label} over the last {SLOW_LEAK_RECENT_POINTS} cold starts"
            )
    elif state == "warning":
        reason = (
            f"{label} cold pressure still lower against {mate_label} than before "
            f"({rel_delta:+.1f} psi, {slope_text}); clears when it levels off"
        )
    else:
        reason = (
            f"{label} cold pressure steady against {mate_label} "
            f"({rel_delta:+.1f} psi, {slope_text})"
        )
    latest = reference[-1]
    reference_median = _median(p.value for p in reference)
    drift.update(
        usual_offset_psi=_round(usual_offset),
        relative_slope_psi_per_week=_round(rel_slope, 4),
        relative_delta_psi=_round(rel_delta),
        own_slope_psi_per_week=_round(own_slope, 4),
        own_delta_psi=_round(own_delta),
        own_corroborates=own_corroborates,
        # legacy names: the wheel's own cold series
        slope_psi_per_week=_round(own_slope, 4),
        delta_psi=_round(own_delta),
        recent_median_psi=_round(
            _median(p.value for p in reference[-SLOW_LEAK_RECENT_POINTS:])),
        reference_median_psi=_round(reference_median),
        slope_fires=slope_fires,
        delta_fires=delta_fires,
        clear_condition_met=clears,
        previous_state=previous,
        sufficient=True,
        last_trip_id=latest.trip.id,
    )
    unit = identity[0] if identity else spec.unit
    return _assessment(
        spec,
        state=state,
        reason=reason,
        current=_current(spec, value=latest.value, trip=latest.trip, identity=identity),
        baseline={
            "median": _round(reference_median),
            "unit": unit,
            "bucket_count": sum(p.buckets for p in reference),
            "trip_count": len(reference),
            "window_days": SLOW_LEAK_REFERENCE_DAYS,
        },
        deviation={
            "signed_from_median": _round(rel_delta),
            "effect_in_rule_direction": _round(-rel_delta),
            "threshold": abs(SLOW_LEAK_DELTA),
            "basis": (
                "median of the last 3 cold starts minus the 30-day median, "
                "wheel relative to its axle mate"
            ),
        },
        drift=drift,
        persistence={"observed": len(rel_reference), "required": SLOW_LEAK_MIN_POINTS,
                     "satisfied": True, "basis": "cold starts"},
        evaluated_at=evaluated_at,
    )


def _trip_medians(
    trips: Sequence[_Trip],
    rows: Sequence[_Row],
    *,
    identity: tuple[str, str, str, str] | None,
    settle_seconds: float = 0.0,
) -> list[_Point]:
    by_trip: dict[int, list[_Row]] = {}
    for row in rows:
        if row.identity == identity:
            by_trip.setdefault(row.trip_key, []).append(row)
    points = []
    for trip in trips:
        start = trip.started_us + int(settle_seconds * MICROSECONDS)
        values = [row.median for row in by_trip.get(trip.id, ()) if row.bucket_us >= start]
        if len(values) >= MIN_TRIP_BUCKETS:
            points.append(_Point(trip, _median(values), len(values)))
    return points


def _recent_vs_reference(points: Sequence[_Point]) -> dict[str, object] | None:
    if len(points) < RECENT_TRIPS + MIN_REFERENCE_TRIPS:
        return None
    recent = list(points[-RECENT_TRIPS:])
    reference = list(points[:-RECENT_TRIPS][-REFERENCE_TRIPS:])
    median = _median(p.value for p in reference)
    return {
        "recent": recent,
        "reference": reference,
        "median": median,
        "signed": [p.value - median for p in recent],
    }


def _comparison_drift(
    comparison: Mapping[str, object] | None,
    points: Sequence[_Point],
) -> dict[str, object]:
    if comparison is None:
        return {"points": _point_list(points), "trip_count": len(points)}
    return {
        "points": _point_list([*comparison["reference"], *comparison["recent"]]),
        "trip_count": len(points),
        "reference_trip_ids": [p.trip.id for p in comparison["reference"]],
        "recent_trip_ids": [p.trip.id for p in comparison["recent"]],
        "reference_median": _round(comparison["median"]),
        "recent_signed_from_median": [_round(v) for v in comparison["signed"]],
    }


def _idle_creep_finding(
    *,
    trips: Sequence[_Trip],
    rows: Sequence[_Row],
    previous: str | None,
    evaluated_at: str,
) -> dict[str, object]:
    spec = SPECS[IDLE_CREEP_KEY]
    starts = {t.id: t.started_us for t in trips}
    settle_us = IDLE_CREEP_SETTLE_SECONDS * MICROSECONDS
    settled = [
        row for row in rows
        if row.trip_key in starts and row.bucket_us >= starts[row.trip_key] + settle_us
    ]
    identity = _latest_identity(settled)
    points = _trip_medians(trips, settled, identity=identity,
                           settle_seconds=IDLE_CREEP_SETTLE_SECONDS)
    comparison = _recent_vs_reference(points)
    drift = {"method": "trip_median_vs_prior_trips", "regime": IDLE_CREEP_REGIME,
             "settle_seconds": IDLE_CREEP_SETTLE_SECONDS,
             **_comparison_drift(comparison, points)}
    required = RECENT_TRIPS + MIN_REFERENCE_TRIPS
    if comparison is None:
        return _insufficient(
            spec,
            reason=f"not enough warm idle stops ({len(points)} of {required} trips)",
            identity=identity, drift=drift, observed=len(points), required=required,
            evaluated_at=evaluated_at, previous=previous,
        )
    signed = comparison["signed"]
    fires = all(value >= IDLE_CREEP_F for value in signed)
    clears = signed[-1] < IDLE_CREEP_CLEAR_F
    state = ("normal" if clears else "warning") if previous == "warning" else (
        "warning" if fires else "normal"
    )
    latest = comparison["recent"][-1]
    if state == "warning":
        reason = (
            f"warm idle coolant {signed[-1]:+.1f} °F against the prior "
            f"{len(comparison['reference'])} trips"
        )
    else:
        reason = f"warm idle coolant steady ({signed[-1]:+.1f} °F against prior trips)"
    drift.update(fires=fires, clear_condition_met=clears, previous_state=previous,
                 sufficient=True, last_trip_id=latest.trip.id)
    unit = identity[0] if identity else spec.unit
    return _assessment(
        spec,
        state=state,
        reason=reason,
        current=_current(spec, value=latest.value, trip=latest.trip, identity=identity),
        baseline={"median": _round(comparison["median"]), "unit": unit,
                  "bucket_count": sum(p.buckets for p in comparison["reference"]),
                  "trip_count": len(comparison["reference"])},
        deviation={"signed_from_median": _round(signed[-1]),
                   "effect_in_rule_direction": _round(signed[-1]),
                   "threshold": IDLE_CREEP_F},
        drift=drift,
        persistence={"observed": sum(v >= IDLE_CREEP_F for v in signed),
                     "required": RECENT_TRIPS, "satisfied": fires, "basis": "trips"},
        evaluated_at=evaluated_at,
    )


def _oil_decline_finding(
    *,
    trips: Sequence[_Trip],
    rows: Sequence[_Row],
    previous: str | None,
    evaluated_at: str,
) -> dict[str, object]:
    spec = SPECS[OIL_DECLINE_KEY]
    trip_ids = {t.id for t in trips}
    banded: dict[str, list[_Row]] = {band: [] for band in OIL_BANDS}
    for row in rows:
        parts = _regime_parts(row.regime)
        if parts is None or row.trip_key not in trip_ids:
            continue
        engine, _motion, rpm, thermal = parts
        if engine == "engine_running" and thermal == "warm" and rpm in banded:
            banded[rpm].append(row)
    identity = _latest_identity(row for band in banded.values() for row in band)
    bands: dict[str, dict[str, object]] = {}
    evaluable: dict[str, dict[str, object]] = {}
    for band, band_rows in banded.items():
        points = _trip_medians(trips, band_rows, identity=identity)
        comparison = _recent_vs_reference(points)
        bands[band] = _comparison_drift(comparison, points)
        if comparison is not None:
            signed = comparison["signed"]
            comparison = {
                **comparison,
                "fires": all(value <= OIL_DECLINE_PSI for value in signed),
                "holds": signed[-1] <= OIL_DECLINE_CLEAR_PSI,
            }
            bands[band].update(fires=comparison["fires"], clear_condition_met=not comparison["holds"])
            evaluable[band] = comparison
    drift = {"method": "trip_median_vs_prior_trips", "thermal": "warm", "bands": bands}
    required = RECENT_TRIPS + MIN_REFERENCE_TRIPS
    if not evaluable:
        observed = max((b.get("trip_count", 0) for b in bands.values()), default=0)
        return _insufficient(
            spec,
            reason=f"not enough warm idle or low-rpm driving ({observed} of {required} trips)",
            identity=identity, drift=drift, observed=int(observed), required=required,
            evaluated_at=evaluated_at, previous=previous,
        )
    fires = any(c["fires"] for c in evaluable.values())
    holds = any(c["holds"] for c in evaluable.values())
    state = ("warning" if holds else "normal") if previous == "warning" else (
        "warning" if fires else "normal"
    )
    if state == "warning":
        pool = {b: c for b, c in evaluable.items() if (c["fires"] if fires else c["holds"])}
    else:
        pool = evaluable
    band = min(pool, key=lambda name: (pool[name]["signed"][-1], OIL_BANDS.index(name)))
    chosen = evaluable[band]
    signed = chosen["signed"]
    latest = chosen["recent"][-1]
    band_text = "idle" if band == "rpm_idle" else "low rpm"
    if state == "warning":
        reason = (
            f"warm oil pressure at {band_text} {signed[-1]:+.1f} psi against the prior "
            f"{len(chosen['reference'])} trips"
        )
    else:
        reason = f"warm oil pressure steady ({band_text} {signed[-1]:+.1f} psi against prior trips)"
    drift.update(band=band, fires=fires, clear_condition_met=not holds,
                 previous_state=previous, sufficient=True, last_trip_id=latest.trip.id)
    unit = identity[0] if identity else spec.unit
    return _assessment(
        spec,
        state=state,
        reason=reason,
        current=_current(spec, value=latest.value, trip=latest.trip, identity=identity),
        baseline={"median": _round(chosen["median"]), "unit": unit,
                  "bucket_count": sum(p.buckets for p in chosen["reference"]),
                  "trip_count": len(chosen["reference"]), "rpm_band": band},
        deviation={"signed_from_median": _round(signed[-1]),
                   "effect_in_rule_direction": _round(-signed[-1]),
                   "threshold": abs(OIL_DECLINE_PSI)},
        drift=drift,
        persistence={"observed": sum(v <= OIL_DECLINE_PSI for v in signed),
                     "required": RECENT_TRIPS, "satisfied": chosen["fires"], "basis": "trips"},
        evaluated_at=evaluated_at,
    )


def _charge_acceptance_finding(
    *,
    trips: Sequence[_Trip],
    rows: Sequence[_Row],
    previous: str | None,
    evaluated_at: str,
) -> dict[str, object]:
    spec = SPECS[CHARGE_ACCEPTANCE_KEY]
    trip_ids = {t.id for t in trips}
    running = [
        row for row in rows
        if row.trip_key in trip_ids and row.regime.startswith("engine_running:")
    ]
    identity = _latest_identity(running)
    by_trip: dict[int, list[float]] = {}
    for row in running:
        if row.identity == identity:
            by_trip.setdefault(row.trip_key, []).append(row.median)
    points: list[_Point] = []
    for trip in trips:
        values = by_trip.get(trip.id, [])
        if len(values) >= CHARGE_MIN_BUCKETS:
            low = sum(1 for value in values if value < CHARGE_LOW_VOLTS)
            points.append(_Point(trip, low / len(values), len(values)))
    drift: dict[str, object] = {
        "method": "trip_low_voltage_share",
        "low_volts": CHARGE_LOW_VOLTS,
        "points": [[p.trip.id, p.trip.started_at, _round(p.value * 100.0, 1)]
                   for p in points[-MAX_POINTS:]],
        "trip_count": len(points),
        "value_unit": "%",
    }
    if len(points) < CHARGE_CONSECUTIVE_TRIPS:
        return _insufficient(
            spec,
            reason=(
                f"not enough running trips ({len(points)} of {CHARGE_CONSECUTIVE_TRIPS})"
            ),
            identity=identity, drift=drift, observed=len(points),
            required=CHARGE_CONSECUTIVE_TRIPS, evaluated_at=evaluated_at,
            previous=previous,
        )
    recent = points[-CHARGE_CONSECUTIVE_TRIPS:]
    reference = points[:-CHARGE_CONSECUTIVE_TRIPS][-REFERENCE_TRIPS:]
    fires = all(p.value > CHARGE_ACCEPTANCE_FRACTION for p in recent)
    latest = recent[-1]
    clears = latest.value < CHARGE_ACCEPTANCE_CLEAR_FRACTION
    state = ("normal" if clears else "warning") if previous == "warning" else (
        "warning" if fires else "normal"
    )
    percent = latest.value * 100.0
    if state == "warning":
        reason = (
            f"{percent:.0f} % of running minutes below {CHARGE_LOW_VOLTS:.1f} V on the "
            f"last trip; {sum(p.value > CHARGE_ACCEPTANCE_FRACTION for p in recent)} of the "
            f"last {CHARGE_CONSECUTIVE_TRIPS} trips above "
            f"{CHARGE_ACCEPTANCE_FRACTION * 100:.0f} %"
        )
    else:
        reason = (
            f"charging steady ({percent:.0f} % of running minutes below "
            f"{CHARGE_LOW_VOLTS:.1f} V on the last trip)"
        )
    reference_median = _median(p.value for p in reference) * 100.0 if reference else None
    drift.update(
        recent_trip_ids=[p.trip.id for p in recent],
        recent_percent=[_round(p.value * 100.0, 1) for p in recent],
        reference_trip_ids=[p.trip.id for p in reference],
        fires=fires,
        clear_condition_met=clears,
        previous_state=previous,
        sufficient=True,
        last_trip_id=latest.trip.id,
    )
    return _assessment(
        spec,
        state=state,
        reason=reason,
        current={**_current(spec, value=percent, trip=latest.trip, identity=identity,
                            unit="%"),
                 "value": _round(percent, 1)},
        baseline={"median": _round(reference_median, 1), "unit": "%",
                  "bucket_count": sum(p.buckets for p in reference),
                  "trip_count": len(reference)},
        deviation={
            "signed_from_median": (
                _round(percent - reference_median, 1) if reference_median is not None else None
            ),
            "effect_in_rule_direction": _round(percent, 1),
            "threshold": CHARGE_ACCEPTANCE_FRACTION * 100.0,
            "basis": "share of running minutes below 13.0 V on the latest trip",
        },
        drift=drift,
        persistence={"observed": sum(p.value > CHARGE_ACCEPTANCE_FRACTION for p in recent),
                     "required": CHARGE_CONSECUTIVE_TRIPS, "satisfied": fires,
                     "basis": "consecutive trips"},
        evaluated_at=evaluated_at,
    )


def _ols_per_day(points: Sequence[_RestPoint]) -> float | None:
    if len(points) < 2:
        return None
    origin = points[0].at_us
    xs = [(p.at_us - origin) / DAY_US for p in points]
    ys = [p.value for p in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx


def _resting_voltage_finding(
    *,
    intervals: Sequence[_Interval],
    rows: Sequence[_Row],
    at_us: int,
    previous: str | None,
    evaluated_at: str,
) -> dict[str, object]:
    spec = SPECS[RESTING_VOLTAGE_KEY]
    window_us = at_us - RESTING_WINDOW_DAYS * DAY_US
    reference_us = at_us - RESTING_REFERENCE_DAYS * DAY_US
    ordered = _SortedRows(rows)
    settled: list[tuple[_Interval, list[_Row]]] = []
    for interval in intervals:
        lo = bisect.bisect_left(ordered.keys, interval.start_us)
        hi = bisect.bisect_right(ordered.keys, interval.end_us)
        if hi > lo:
            settled.append((interval, list(ordered[lo:hi])))
    # One identity for the whole series: the one with the most settled buckets
    # this week (ties -> the one seen latest); all settled rows when none this week.
    counts: dict[tuple[str, str, str, str], list[int]] = {}
    pool = [row for _i, found in settled for row in found if row.bucket_us >= window_us]
    for row in pool or [row for _i, found in settled for row in found]:
        entry = counts.setdefault(row.identity, [0, row.bucket_us])
        entry[0] += 1
        entry[1] = max(entry[1], row.bucket_us)
    identity = (
        max(counts, key=lambda ident: (counts[ident][0], counts[ident][1], ident))
        if counts else None
    )
    points: list[_RestPoint] = []
    for interval, found in settled:
        mine = [row for row in found if row.identity == identity]
        if mine:
            points.append(_RestPoint(mine[0].bucket_us, _median(r.median for r in mine),
                                     len(mine), interval.after_trip_id))
    points.sort(key=lambda p: p.at_us)
    reference = [p for p in points if p.at_us >= reference_us]
    window = [p for p in reference if p.at_us >= window_us]
    drift: dict[str, object] = {
        "method": "settled_stop_level_and_slope",
        "points": [[_iso(p.at_us), _round(p.value), p.buckets, p.after_trip_id]
                   for p in reference[-MAX_POINTS:]],
        "window_days": RESTING_WINDOW_DAYS,
        "stops_in_window": len(window),
        "identity": list(identity) if identity else None,
    }
    if len(window) < RESTING_MIN_STOPS:
        return _insufficient(
            spec,
            reason=(
                f"not enough settled stops ({len(window)} of {RESTING_MIN_STOPS} "
                f"in {RESTING_WINDOW_DAYS} days)"
            ),
            identity=identity,
            drift={**drift, "level_v": None, "slope_v_per_day": None},
            observed=len(window),
            required=RESTING_MIN_STOPS,
            evaluated_at=evaluated_at,
            previous=previous,
        )
    level = _median(p.value for p in window)
    slope = _ols_per_day(window) if len(window) >= RESTING_MIN_STOPS_SLOPE else None
    fires_level = level < RESTING_LOW_VOLTS
    fires_slope = slope is not None and slope < RESTING_SLOPE_V_PER_DAY
    clears = level >= RESTING_CLEAR_VOLTS and (
        slope is None or slope > RESTING_SLOPE_CLEAR_V_PER_DAY
    )
    state = _hysteresis(previous, fires=fires_level or fires_slope, clears=clears)
    stops = len(window)
    if state == "warning" and fires_level:
        reason = (
            f"resting voltage {level:.2f} V over {stops} stops this week "
            f"(limit {RESTING_LOW_VOLTS:.1f} V)"
        )
    elif state == "warning" and fires_slope:
        reason = f"resting voltage falling {abs(slope):.2f} V a day over {stops} stops"
    elif state == "warning":
        reason = (
            f"resting voltage still low ({level:.2f} V over {stops} stops); clears at "
            f"{RESTING_CLEAR_VOLTS:.1f} V and steady"
        )
    else:
        reason = f"resting voltage steady ({level:.2f} V over {stops} stops)"
    latest = window[-1]
    reference_median = _median(p.value for p in reference)
    drift.update(
        level_v=_round(level),
        slope_v_per_day=_round(slope, 4),
        fires_level=fires_level,
        fires_slope=fires_slope,
        clear_condition_met=clears,
        previous_state=previous,
        sufficient=True,
        last_after_trip_id=latest.after_trip_id,
    )
    return _assessment(
        spec,
        state=state,
        reason=reason,
        current={
            "metric": spec.metric,
            "value": _round(latest.value),
            "unit": identity[0] if identity else spec.unit,
            "observed_at": _iso(latest.at_us),
            "source": identity[1] if identity else None,
            "quality": identity[2] if identity else None,
            "provenance": identity[3] if identity else None,
            "trip_id": None,
        },
        baseline={
            "median": _round(reference_median),
            "unit": identity[0] if identity else spec.unit,
            "bucket_count": sum(p.buckets for p in reference),
            "trip_count": len(reference),
            "window_days": RESTING_REFERENCE_DAYS,
        },
        deviation={
            "signed_from_median": _round(level - reference_median),
            "effect_in_rule_direction": _round(RESTING_LOW_VOLTS - level),
            "threshold": 0.0,
            "basis": (
                "7-day median settled resting voltage against 12.2 V; "
                "slope against -0.1 V/day"
            ),
        },
        drift=drift,
        persistence={"observed": stops, "required": RESTING_MIN_STOPS,
                     "satisfied": True, "basis": "parked stops"},
        evaluated_at=evaluated_at,
    )


# --------------------------------------------------------------------------
# public API


def previous_states(
    previous: Mapping[str, object] | Iterable[Mapping[str, object]] | None,
) -> dict[str, str]:
    """Normalize ``{rule: state}`` or a list of prior assessments."""

    if previous is None:
        return {}
    if isinstance(previous, Mapping):
        items = previous.items()
    else:
        items = (
            (item.get("rule"), item.get("state"))
            for item in previous
            if isinstance(item, Mapping)
        )
    return {
        str(rule): str(state)
        for rule, state in items
        if rule in DRIFT_RULE_KEYS and state in FINDING_STATES
    }


def evaluate_drift(
    historian: object,
    *,
    at: datetime,
    previous_state: Mapping[str, object] | Iterable[Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Evaluate every tier-2 finding at ``at``; one assessment per rule key.

    Read-only over ``trips`` and ``metric_rollups``.  Only trips that ended by
    ``at`` count, so a trip in progress waits for the next daily run.
    ``previous_state`` (``{rule: state}`` or the prior assessments) keeps a
    warning until its clear condition holds.
    """

    moment = _utc(at, "at")
    at_us = _to_us(moment)
    evaluated_at = moment.isoformat()
    previous = previous_states(previous_state)
    reader = _Reader(historian, at_us)
    trips = reader.completed_trips()
    first_moving = reader.first_moving()
    assessments: list[dict[str, object]] = []
    tire_rows = {
        wheel: _SortedRows(reader.rows(f"tire.pressure.{wheel}")) for wheel in WHEELS
    }
    for wheel in WHEELS:
        assessments.append(
            _tire_finding(
                wheel,
                trips=trips,
                first_moving=first_moving,
                rows=tire_rows[wheel],
                mate_rows=tire_rows[AXLE_MATE[wheel]],
                at_us=at_us,
                previous=previous.get(f"tire_pressure_{wheel}_slow_leak"),
                evaluated_at=evaluated_at,
            )
        )
    assessments.append(
        _idle_creep_finding(
            trips=trips,
            rows=reader.rows("engine.coolant_temperature", regime=IDLE_CREEP_REGIME),
            previous=previous.get(IDLE_CREEP_KEY),
            evaluated_at=evaluated_at,
        )
    )
    assessments.append(
        _oil_decline_finding(
            trips=trips,
            rows=reader.rows("engine.oil_pressure", regime_prefix="engine_running:"),
            previous=previous.get(OIL_DECLINE_KEY),
            evaluated_at=evaluated_at,
        )
    )
    assessments.append(
        _charge_acceptance_finding(
            trips=trips,
            rows=reader.rows("battery.voltage", regime_prefix="engine_running:"),
            previous=previous.get(CHARGE_ACCEPTANCE_KEY),
            evaluated_at=evaluated_at,
        )
    )
    parked_rows = [
        row
        for prefix in RESTING_REGIME_PREFIXES
        for row in reader.rows("battery.voltage", regime_prefix=prefix, parked=True)
    ]
    assessments.append(
        _resting_voltage_finding(
            intervals=reader.parked_intervals(),
            rows=parked_rows,
            at_us=at_us,
            previous=previous.get(RESTING_VOLTAGE_KEY),
            evaluated_at=evaluated_at,
        )
    )
    return assessments


# --------------------------------------------------------------------------
# state file


def state_path_for(historian: object) -> Path | None:
    """``drift-warnings.json`` beside a file-backed historian database, else None."""

    database = getattr(historian, "database", None)
    if not isinstance(database, str) or not database.strip():
        return None
    if database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database).with_name(STATE_FILENAME)


def _valid_assessment(item: object) -> bool:
    if not isinstance(item, Mapping):
        return False
    rate = item.get("notification_rate_limit_seconds")
    return (
        item.get("rule") in DRIFT_RULE_KEYS
        and item.get("state") in FINDING_STATES
        and isinstance(item.get("title"), str)
        and bool(item.get("title"))
        and item.get("advisory") is True
        and isinstance(item.get("category"), str)
        and bool(item.get("category"))
        and isinstance(rate, (int, float))
        and not isinstance(rate, bool)
        and 60 <= rate <= NOTIFICATION_RATE_LIMIT_SECONDS
    )


def state_is_current(assessments: Iterable[Mapping[str, object]]) -> bool:
    """True when every cached assessment carries this code's rule revision."""

    for item in assessments:
        rule = item.get("rule")
        if rule not in SPECS or item.get("rule_revision") != digest(rule_snapshot(str(rule))):
            return False
    return True


def load_state(path: str | Path) -> dict[str, object] | None:
    """Return ``{"last_run_at", "assessments"}`` or None when missing/invalid.

    A file that fails validation is ignored as a whole: a malformed or
    duplicated assessment would otherwise break every advisory persistence
    call it is merged into.
    """

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return None
    last_run_at = payload.get("last_run_at")
    if last_run_at is not None:
        if not isinstance(last_run_at, str):
            return None
        try:
            parsed = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
    assessments = payload.get("assessments")
    if not isinstance(assessments, list):
        return None
    if not all(_valid_assessment(item) for item in assessments):
        return None
    rules = [item["rule"] for item in assessments]
    if len(rules) != len(set(rules)):
        return None
    return {"last_run_at": last_run_at, "assessments": assessments}


def save_state(
    path: str | Path,
    *,
    last_run_at: str | None,
    assessments: Sequence[Mapping[str, object]],
) -> None:
    """Atomically replace the state file (temporary file + ``os.replace``)."""

    target = Path(path)
    payload = {
        "version": STATE_VERSION,
        "last_run_at": last_run_at,
        "assessments": [dict(item) for item in assessments],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


__all__ = (
    "CHARGE_ACCEPTANCE_FRACTION",
    "DRIFT_RULE_KEYS",
    "EVALUATOR_REVISION",
    "IDLE_CREEP_F",
    "OIL_DECLINE_PSI",
    "RESTING_LOW_VOLTS",
    "SLOW_LEAK_DELTA",
    "SLOW_LEAK_SLOPE",
    "STATE_FILENAME",
    "evaluate_drift",
    "load_state",
    "previous_states",
    "rule_snapshot",
    "save_state",
    "state_is_current",
    "state_path_for",
)
