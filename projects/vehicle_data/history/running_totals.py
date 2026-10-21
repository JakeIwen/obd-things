"""Sample totals restricted to engine-running regimes, per trip or time window.

Ratios of two metrics recorded by the same snapshots (fuel economy is mean
speed over mean fuel rate) need both totals over the same running samples.
Engine-off and engine-unknown snapshots are excluded: road speed can still be
broadcast with the engine stopped, while the fuel-rate frame says nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from .validation import _iso_from_us, _to_us, _utc_datetime

# Regimes are "<engine>:<motion>:<rpm>:<thermal>". ";" sorts right after ":",
# so this half-open range is every running regime and stays an index range.
RUNNING_REGIME_RANGE = ("engine_running:", "engine_running;")


class RunningTotalsMixin:
    """Engine-running totals from minute rollups plus the unrolled raw tail."""

    def running_totals_for_trip(
        self, trip_id: int, metric_units: Mapping[str, str]
    ) -> dict[str, tuple[float, int]]:
        """``{metric: (sum of values, sample count)}`` over one trip's running samples.

        Rolled-up minutes count each source observation once; samples newer
        than the rollup cursor are added from the raw table, as in the trip
        comparison aggregates.
        """
        totals = {}
        with self._lock:
            cursor_text = self._meta_locked(f"rollup_through_us:{self.config.rollup_seconds}")
            cursor_us = int(cursor_text) if cursor_text is not None else 0
            for metric, unit in metric_units.items():
                rolled = self._conn.execute(
                    """
                    SELECT coalesce(sum(mean * sample_count), 0.0), coalesce(sum(sample_count), 0)
                    FROM metric_rollups
                    WHERE metric=? AND regime>=? AND regime<? AND bucket_seconds=?
                      AND trip_key=? AND unit=?
                    """,
                    (metric, *RUNNING_REGIME_RANGE, self.config.rollup_seconds, trip_id, unit),
                ).fetchone()
                tail = self._conn.execute(
                    """
                    SELECT coalesce(sum(value_num), 0.0), count(*)
                    FROM metric_samples
                    WHERE trip_id=? AND metric=? AND captured_us>=? AND unit=?
                      AND regime>=? AND regime<? AND freshness='fresh'
                      AND value_kind='number' AND observed_us IS NOT NULL
                    """,
                    (trip_id, metric, cursor_us, unit, *RUNNING_REGIME_RANGE),
                ).fetchone()
                totals[metric] = (float(rolled[0]) + float(tail[0]), int(rolled[1]) + int(tail[1]))
        return totals

    def running_totals_for_window(
        self,
        metric_units: Mapping[str, str],
        *,
        start: datetime | str,
        end: datetime | str,
    ) -> dict[str, tuple[float, int]]:
        """Running totals over complete rollup buckets in ``[start, end)``."""
        start_us = _to_us(_utc_datetime(start, "start"))
        end_us = _to_us(_utc_datetime(end, "end"))
        totals = {}
        with self._lock:
            for metric, unit in metric_units.items():
                row = self._conn.execute(
                    """
                    SELECT coalesce(sum(mean * sample_count), 0.0), coalesce(sum(sample_count), 0)
                    FROM metric_rollups
                    WHERE metric=? AND regime>=? AND regime<? AND bucket_seconds=?
                      AND bucket_us>=? AND bucket_us<? AND unit=?
                    """,
                    (metric, *RUNNING_REGIME_RANGE, self.config.rollup_seconds,
                     start_us, end_us, unit),
                ).fetchone()
                totals[metric] = (float(row[0]), int(row[1]))
        return totals

    def first_running_bucket(self, metric: str, unit: str) -> str | None:
        """Start of the earliest rolled-up running minute that recorded ``metric``, or None."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT min(bucket_us) FROM metric_rollups
                WHERE metric=? AND regime>=? AND regime<? AND bucket_seconds=? AND unit=?
                """,
                (metric, *RUNNING_REGIME_RANGE, self.config.rollup_seconds, unit),
            ).fetchone()
        return None if row[0] is None else _iso_from_us(int(row[0]))
