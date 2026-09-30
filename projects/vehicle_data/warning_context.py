"""Bounded historian context for the tiered early-warning rules. No CAN I/O.

This module follows the ``event_history.py`` precedent of running bounded
SELECTs on the historian connection.  Every query runs inside
``historian._lock`` on ``historian._conn``, uses indexed predicates only, and
reads ``metric_samples`` only with ``metric=``/``trip_id=`` equality plus
``captured_us`` bounds.  Nothing here writes to the database; the only side
effect is registering baseline inputs in ``historian._baseline_inputs`` exactly
as ``TelemetryHistorian.robust_baseline`` does, so
``event_history.archive_baselines`` can archive them with a decision.

Trip phases (tires):
    cold     [trip start, first moving + 3 min), only when the vehicle had
             been parked for at least 4 h before the trip (otherwise empty)
    warming  [first moving + 3 min, first moving + 20 min)
    warm     [first moving + 20 min, end of trip)
Stated deviation from the specification: the "or stationary" alternative of
the cold phase is dropped.  A stationary sample after driving is heat-soaked
and would pollute both the cold baseline and the cold absolute thresholds, so
a sample outside these windows (and every parked sample without a trip) has no
phase and the rule reports ``not_applicable``.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from lib.timeutil import finite_number as _number
from projects.vehicle_data.historian import (
    REGIME_DIMENSIONS,
    BaselineStats,
    project_regime,
)


MICROSECONDS = 1_000_000
MAX_LOOKBACK_DAYS = 30
BASELINE_INPUT_LIMIT = 24
INPUT_BUCKET_PREVIEW = 128
PHASES = ("cold", "warming", "warm")
COLD_PRIOR_GAP_SECONDS = 4 * 60 * 60
COLD_AFTER_MOVING_SECONDS = 180
WARM_AFTER_MOVING_SECONDS = 20 * 60
PHASE_INDEX_LOOKBACK_DAYS = 45
MOVING_SPEED_MPH = 1.0
DUTY_LOW_BELOW = 40.0
DUTY_HIGH_FROM = 85.0
# Transmission thermal bands from the transmission value itself:
# cold < 120 °F <= warm < 175 °F <= hot.  (lower inclusive, upper exclusive)
TRANSMISSION_BANDS: tuple[tuple[str, float | None, float | None], ...] = (
    ("cold", None, 120.0),
    ("warm", 120.0, 175.0),
    ("hot", 175.0, None),
)
SERIES_ROW_LIMIT = 2_000
# The robust_baseline input columns, selected in sorted-key order so the
# plain JSON encoding equals ``json.dumps(inputs, sort_keys=True, ...)``.
_BASELINE_KEYS = (
    "bucket_us",
    "first_us",
    "last_us",
    "maximum",
    "median",
    "minimum",
    "regime",
    "sample_count",
    "trip_key",
)
_BASELINE_COLUMNS = ",".join(f"r.{key}" for key in _BASELINE_KEYS)
_REGIME_COLUMN = _BASELINE_KEYS.index("regime")
_TRIP_COLUMN = _BASELINE_KEYS.index("trip_key")
_BUCKET_COLUMN = _BASELINE_KEYS.index("bucket_us")
_DIGEST_ENCODER = json.JSONEncoder(separators=(",", ":"))
# Content-addressed reuse of the pure statistics/digest step.  The rollup rows
# are still read from the database on every call; only when the exact same
# rows come back is the previous (identical) result reused, so this never
# serves stale history.
_RESULT_CACHE: "OrderedDict[tuple, tuple[tuple, BaselineStats, list]]" = OrderedDict()
_RESULT_CACHE_LIMIT = 48
_RESULT_LOCK = threading.Lock()
_DEDUPE = """
    NOT EXISTS (
        SELECT 1 FROM metric_samples AS earlier
        WHERE earlier.metric=sample.metric
          AND earlier.source=sample.source
          AND earlier.unit=sample.unit
          AND earlier.quality=sample.quality
          AND earlier.provenance=sample.provenance
          AND earlier.observed_us=sample.observed_us
          AND earlier.freshness='fresh'
          AND earlier.value_kind='number'
          AND (
              earlier.captured_us<sample.captured_us
              OR (
                  earlier.captured_us=sample.captured_us
                  AND earlier.id<sample.id
              )
          )
    )
"""


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        value = datetime.fromisoformat(text)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(timezone.utc)


def to_us(value: datetime | str) -> int:
    return int(round(_utc(value).timestamp() * MICROSECONDS))


def _iso_from_us(value: int) -> str:
    return datetime.fromtimestamp(value / MICROSECONDS, timezone.utc).isoformat()


def duty_band(value: object) -> str | None:
    """Generator field-duty band: low < 40 %, mid, high >= 85 %."""

    if not _number(value):
        return None
    number = float(value)
    if number < DUTY_LOW_BELOW:
        return "low"
    if number >= DUTY_HIGH_FROM:
        return "high"
    return "mid"


def transmission_band(value: object) -> tuple[str, float | None, float | None] | None:
    """Thermal band taken from the transmission temperature itself."""

    if not _number(value):
        return None
    number = float(value)
    for name, low, high in TRANSMISSION_BANDS:
        if (low is None or number >= low) and (high is None or number < high):
            return name, low, high
    return None


def _regime_range(regime: str, dimensions: Sequence[str]) -> tuple[str, str] | None:
    """Index-usable ``regime`` range covering a projection's leading dimensions.

    The stored regime is ``engine:motion:rpm:thermal``.  When the projection
    contains a leading run of those dimensions, every matching stored regime
    starts with the same text, so ``regime >= prefix AND regime < prefix'``
    narrows the ``metric_rollups_baseline`` scan.  The exact projection is
    still applied afterwards in Python, so this is only an index hint.
    """

    parts = regime.split(":")
    if len(parts) != len(REGIME_DIMENSIONS):
        return None
    selected = set(dimensions)
    count = 0
    while count < len(REGIME_DIMENSIONS) and REGIME_DIMENSIONS[count] in selected:
        count += 1
    if count == 0:
        return None
    if count == len(REGIME_DIMENSIONS):
        return regime, regime + "\x00"
    prefix = ":".join(parts[:count]) + ":"
    return prefix, prefix[:-1] + ";"


def _exact_prefix(dimensions: Sequence[str]) -> bool:
    """True when a projection is exactly a leading run of the stored regime."""

    selected = tuple(dimensions)
    return bool(selected) and set(selected) == set(REGIME_DIMENSIONS[: len(selected)])


def _phase_window(info: Mapping[str, object], phase: str) -> tuple[int, int | None] | None:
    """Return ``[start, end)`` in microseconds for one trip phase, or None."""

    first = info.get("first_moving_us")
    started = int(info["started_us"])
    if first is None:
        if phase == "cold" and info.get("cold_eligible"):
            return started, None
        return None
    first = int(first)
    cold_end = first + COLD_AFTER_MOVING_SECONDS * MICROSECONDS
    warm_start = first + WARM_AFTER_MOVING_SECONDS * MICROSECONDS
    if phase == "cold":
        return (started, cold_end) if info.get("cold_eligible") else None
    if phase == "warming":
        return cold_end, warm_start
    if phase == "warm":
        return warm_start, None
    return None


def phase_at(info: Mapping[str, object], captured_us: int) -> str | None:
    """Phase of one trip-relative moment (see the module docstring)."""

    if captured_us < int(info["started_us"]):
        return None
    for phase in PHASES:
        window = _phase_window(info, phase)
        if window is None:
            continue
        start, end = window
        if captured_us >= start and (end is None or captured_us < end):
            return phase
    return None


def phase_of(
    sample: Mapping[str, object],
    phase_index: Mapping[int, Mapping[str, object]],
) -> str | None:
    """Phase of one stored sample; parked samples without a trip have none."""

    trip_id = sample.get("trip_id")
    if trip_id is None or isinstance(trip_id, bool):
        return None
    info = phase_index.get(int(trip_id))
    if info is None:
        return None
    captured = sample.get("captured_us")
    if not isinstance(captured, int) or isinstance(captured, bool):
        text = sample.get("captured_at")
        if not isinstance(text, str):
            return None
        captured = to_us(text)
    return phase_at(info, captured)


def trip_phase_index(historian, *, at: datetime | str) -> dict[int, dict[str, object]]:
    """Per-trip phase facts from ``trips`` and 60 s ``vehicle.speed`` rollups.

    ``first_moving_us`` is the first rollup bucket whose regime is not
    stationary (falls back to ``started_us`` for a completed trip that never
    moved).  The open trip is refined from its raw speed samples; while it has
    not moved yet its ``first_moving_us`` is None.
    """

    at_us = to_us(at)
    lower_us = at_us - PHASE_INDEX_LOOKBACK_DAYS * 24 * 60 * 60 * MICROSECONDS
    with historian._lock:
        trips = historian._conn.execute(
            "SELECT id,started_us,last_active_us,ended_us FROM trips "
            "WHERE started_us<=? ORDER BY started_us,id",
            (at_us,),
        ).fetchall()
        moving = historian._conn.execute(
            """
            SELECT trip_key,min(bucket_us) FROM metric_rollups
            WHERE metric='vehicle.speed' AND regime NOT LIKE '%:stationary:%'
              AND bucket_us>=? AND bucket_us<=? AND trip_key!=0
            GROUP BY trip_key
            """,
            (lower_us, at_us),
        ).fetchall()
    first_moving = {int(row[0]): int(row[1]) for row in moving}
    index: dict[int, dict[str, object]] = {}
    previous_last: int | None = None
    for row in trips:
        trip_id = int(row[0])
        started = int(row[1])
        last_active = int(row[2])
        is_open = row[3] is None
        gap = None if previous_last is None else (started - previous_last) / MICROSECONDS
        first = first_moving.get(trip_id)
        index[trip_id] = {
            "trip_id": trip_id,
            "started_us": started,
            "last_active_us": last_active,
            "open": is_open,
            "prior_gap_seconds": gap,
            # The first recorded trip has no known parked interval before it;
            # treat it as a cold start rather than silently dropping it.
            "cold_eligible": gap is None or gap >= COLD_PRIOR_GAP_SECONDS,
            "first_moving_us": first if first is not None else (None if is_open else started),
            "moved": first is not None,
            "refined": False,
        }
        previous_last = last_active if previous_last is None else max(previous_last, last_active)
    refine_open_trips(historian, index, at=at_us)
    return index


def refine_open_trips(historian, index: dict[int, dict[str, object]], *, at: int) -> None:
    """Refine the open trip's first moving moment from its raw speed samples."""

    for info in index.values():
        if not info.get("open") or info.get("refined"):
            continue
        started = int(info["started_us"])
        with historian._lock:
            row = historian._conn.execute(
                """
                SELECT min(captured_us) FROM metric_samples
                WHERE trip_id=? AND metric='vehicle.speed'
                  AND captured_us>=? AND captured_us<=?
                  AND freshness='fresh' AND value_num>?
                """,
                (int(info["trip_id"]), started, int(at), MOVING_SPEED_MPH),
            ).fetchone()
        found = row[0] if row is not None else None
        if found is not None:
            info["first_moving_us"] = int(found)
            info["moved"] = True
            info["refined"] = True


def rollup_baseline(
    historian,
    metric: str,
    *,
    regime: str,
    regime_dimensions: Sequence[str],
    before: datetime | str,
    lookback_days: int = 30,
    exclude_trip_id: int | None,
    unit: str,
    quality: str,
    source: str,
    provenance: str,
    median_range: tuple[float | None, float | None] | None = None,
    phase: tuple[str, Mapping[int, Mapping[str, object]]] | None = None,
    companion: tuple[str, float, float] | None = None,
    minimum_trip_age_seconds: float = 0,
    label: str | None = None,
) -> BaselineStats | None:
    """Median/MAD baseline of completed bucket medians with extra conditioning.

    The statistics, input digest, input preview and ``_baseline_inputs``
    registration match ``TelemetryHistorian.robust_baseline`` (rows ordered by
    bucket, ties broken by trip and regime).  Extra filters:
    ``median_range=(low, high)`` keeps buckets with ``low <= median < high``
    (None = open); ``companion=(metric, low, high)`` keeps buckets whose
    same-trip, same-bucket companion rollup median is inside ``[low, high]``;
    ``phase=(name, phase_index)`` keeps trip buckets inside that trip phase.
    """

    if (
        not isinstance(lookback_days, int)
        or isinstance(lookback_days, bool)
        or not 1 <= lookback_days <= MAX_LOOKBACK_DAYS
    ):
        raise ValueError(f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}")
    before_dt = _utc(before)
    before_us = to_us(before_dt)
    start_us = to_us(before_dt - timedelta(days=lookback_days))
    projected = project_regime(regime, regime_dimensions)
    suffix: list[str] = []
    windows: dict[int, tuple[int, int]] | None = None
    if phase is not None:
        phase_name, phase_index = phase
        windows = {}
        for trip_id, info in phase_index.items():
            if exclude_trip_id is not None and int(trip_id) == int(exclude_trip_id):
                continue
            window = _phase_window(info, phase_name)
            if window is None:
                continue
            low, high = window
            high = before_us if high is None else min(high, before_us)
            if high <= start_us or low >= before_us or high <= low:
                continue
            windows[int(trip_id)] = (int(low), int(high))
        if not windows:
            return None
    clauses = [
        "r.metric=?",
        "r.bucket_us>=?",
        "r.bucket_us<?",
        "r.unit=?",
        "r.quality=?",
        "r.source=?",
        "r.provenance=?",
    ]
    args: list[object] = [metric, start_us, before_us, unit, quality, source, provenance]
    regime_range = _regime_range(regime, regime_dimensions)
    if regime_range is not None:
        clauses.append("r.regime>=? AND r.regime<?")
        args.extend(regime_range)
    if minimum_trip_age_seconds:
        clauses.append(
            "EXISTS (SELECT 1 FROM trips WHERE trips.id=r.trip_key "
            "AND r.bucket_us >= trips.started_us + ?)"
        )
        args.append(int(minimum_trip_age_seconds * MICROSECONDS))
    if exclude_trip_id is not None:
        clauses.append("r.trip_key!=?")
        args.append(exclude_trip_id)
    if median_range is not None:
        low, high = median_range
        if low is not None:
            clauses.append("r.median>=?")
            args.append(float(low))
        if high is not None:
            clauses.append("r.median<?")
            args.append(float(high))
    if companion is not None:
        companion_metric, low, high = companion
        # A list subquery is evaluated once; a correlated EXISTS would rescan
        # every companion rollup for each primary bucket.
        clauses.append(
            "(r.bucket_us,r.trip_key) IN (SELECT c.bucket_us,c.trip_key FROM metric_rollups AS c "
            "WHERE c.metric=? AND c.bucket_us>=? AND c.bucket_us<? AND c.median BETWEEN ? AND ?)"
        )
        args.extend((companion_metric, start_us, before_us, float(low), float(high)))
        suffix.append(f"{companion_metric}={float(low):g}..{float(high):g}")
    if windows is not None:
        clauses.append("r.trip_key!=0")
    with historian._lock:
        cursor = historian._conn.cursor()
        cursor.row_factory = None  # plain tuples: cheaper than sqlite3.Row here
        rows = cursor.execute(
            f"SELECT {_BASELINE_COLUMNS} FROM metric_rollups AS r "
            f"WHERE {' AND '.join(clauses)} ORDER BY r.bucket_us,r.trip_key,r.regime",
            args,
        ).fetchall()
    if not _exact_prefix(regime_dimensions):
        rows = [
            row for row in rows
            if project_regime(row[_REGIME_COLUMN], regime_dimensions) == projected
        ]
    if windows is not None:
        kept = []
        for row in rows:
            window = windows.get(row[_TRIP_COLUMN])
            if window is not None and window[0] <= row[_BUCKET_COLUMN] < window[1]:
                kept.append(row)
        rows = kept
    if phase is not None:
        suffix.append(f"tire_phase={phase[0]}")
    if label:
        suffix.insert(0, label)
    elif median_range is not None:
        suffix.insert(0, f"median={median_range[0]}..{median_range[1]}")
    if not rows:
        return None
    label_regime = "|".join((projected, *suffix))
    cache_key = (metric, label_regime, unit, quality, source, provenance)
    content = tuple(rows)
    with _RESULT_LOCK:
        cached = _RESULT_CACHE.get(cache_key)
        if cached is not None and cached[0] == content:
            _RESULT_CACHE.move_to_end(cache_key)
        else:
            cached = None
    if cached is not None:
        _register_inputs(historian, cached[1].input_digest, cached[2])
        return cached[1]
    stats, inputs = _baseline_stats(
        rows, metric=metric, regime=label_regime, unit=unit, quality=quality,
        source=source, provenance=provenance,
    )
    _register_inputs(historian, stats.input_digest, inputs)
    with _RESULT_LOCK:
        _RESULT_CACHE[cache_key] = (content, stats, inputs)
        _RESULT_CACHE.move_to_end(cache_key)
        while len(_RESULT_CACHE) > _RESULT_CACHE_LIMIT:
            _RESULT_CACHE.popitem(last=False)
    return stats


def _register_inputs(historian, digest: str, inputs: list) -> None:
    """Register baseline inputs for archival exactly as robust_baseline does."""

    store = getattr(historian, "_baseline_inputs", None)
    if isinstance(store, dict):
        with historian._lock:
            store[digest] = inputs
            while len(store) > BASELINE_INPUT_LIMIT:
                store.pop(next(iter(store)))


def _baseline_stats(
    rows: Sequence[tuple],
    *,
    metric: str,
    regime: str,
    unit: str,
    quality: str,
    source: str,
    provenance: str,
) -> tuple[BaselineStats, list[dict[str, object]]]:
    # Keys in sorted order: plain encoding equals json.dumps(sort_keys=True).
    inputs = [dict(zip(_BASELINE_KEYS, row)) for row in rows]
    input_digest = hashlib.sha256(_DIGEST_ENCODER.encode(inputs).encode()).hexdigest()
    values = [float(item["median"]) for item in inputs]
    center = float(statistics.median(values))
    mad = float(statistics.median(abs(value - center) for value in values))
    trips = {item["trip_key"] for item in inputs if item["trip_key"] != 0}
    return BaselineStats(
        metric=metric,
        regime=regime,
        unit=unit,
        quality=quality,
        source=source,
        provenance=provenance,
        bucket_count=len(inputs),
        trip_count=len(trips),
        sample_count=sum(item["sample_count"] for item in inputs),
        median=center,
        mad=mad,
        minimum=min(float(item["minimum"]) for item in inputs),
        maximum=max(float(item["maximum"]) for item in inputs),
        first_at=_iso_from_us(min(item["first_us"] for item in inputs)),
        last_at=_iso_from_us(max(item["last_us"] for item in inputs)),
        input_digest=input_digest,
        input_buckets=tuple(
            {
                "bucket_us": item["bucket_us"],
                "trip_key": item["trip_key"],
                "regime": item["regime"],
                "median": item["median"],
            }
            for item in inputs[:INPUT_BUCKET_PREVIEW]
        ),
        input_buckets_complete=len(inputs) <= INPUT_BUCKET_PREVIEW,
    ), inputs


def recent_series(
    historian,
    metric: str,
    *,
    trip_id: int | None,
    at: datetime | str,
    lookback_seconds: float,
    source: str,
    quality: str,
    provenance: str,
) -> list[dict[str, object]]:
    """Newest-first distinct fresh observations inside ``[at - lookback, at]``.

    Same row selection and de-duplication as
    ``TelemetryHistorian.recent_numeric_samples`` (one row per source
    observation, trip-scoped, exact source/quality/provenance), but bounded by
    capture time instead of a row count and without regime projection, so the
    evaluator can fetch each metric once per tick and project per rule in
    Python.  Each dict carries ``captured_us``/``observed_us`` as integers.
    """

    at_us = to_us(at)
    start_us = at_us - int(round(max(0.0, float(lookback_seconds)) * MICROSECONDS))
    clauses = [
        "sample.metric=?",
        "sample.captured_us>=?",
        "sample.captured_us<=?",
        "sample.freshness='fresh'",
        "sample.value_kind='number'",
        "sample.observed_us IS NOT NULL",
        "sample.quality=?",
        "sample.source=?",
        "sample.provenance=?",
        _DEDUPE,
    ]
    args: list[object] = [metric, start_us, at_us, quality, source, provenance]
    if trip_id is None:
        clauses.append("sample.trip_id IS NULL")
    else:
        clauses.append("sample.trip_id=?")
        args.append(int(trip_id))
    args.append(SERIES_ROW_LIMIT)
    with historian._lock:
        rows = historian._conn.execute(
            f"SELECT sample.* FROM metric_samples AS sample "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY sample.observed_us DESC,sample.captured_us DESC LIMIT ?",
            args,
        ).fetchall()
    result = []
    for row in rows:
        item = historian._sample_dict(row)
        item["captured_us"] = int(row["captured_us"])
        item["observed_us"] = int(row["observed_us"])
        result.append(item)
    return result
