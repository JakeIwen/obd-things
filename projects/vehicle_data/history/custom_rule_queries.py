"""The one-recording read behind owner-defined (custom) early warnings."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .validation import _iso_from_us, _to_us, _utc_datetime

_READING_COLUMNS = ("metric", "value_kind", "value_num", "unit", "source", "bus", "quality",
                    "provenance", "freshness", "observed_us", "observed_at", "source_age_ms")


class CustomRuleQueryMixin:
    """Read-only; no schema change. Custom-rule holds are timed by the caller, not here."""

    def recording_readings(
        self,
        metrics: Iterable[str],
        *,
        at: datetime | str,
    ) -> dict[str, object] | None:
        """Return the newest snapshot captured at or before ``at`` with its readings.

        ``readings`` maps every requested metric to its row in that snapshot, or ``None`` when the
        broker reported it unavailable there (an older value never stands in). ``rpm`` is the same
        snapshot's ``engine.rpm``. ``previous_snapshot_id`` is the snapshot stored just before it
        (snapshot ids follow capture order: ingestion refuses non-increasing capture times).
        One statement under the historian lock; ``None`` when nothing was captured by ``at``.
        """

        at_us = _to_us(_utc_datetime(at, "at"))
        names = sorted({*metrics, "engine.rpm"})
        marks = ",".join("?" for _ in names)
        columns = ",".join("m." + name for name in _READING_COLUMNS)
        with self._lock:
            rows = self._conn.execute(
                f"""
                WITH s AS (
                    SELECT id,captured_us,trip_id,regime FROM snapshots
                    WHERE captured_us<=? ORDER BY captured_us DESC LIMIT 1
                )
                SELECT s.id AS snapshot_id,s.captured_us AS snapshot_us,s.trip_id AS snapshot_trip,
                       s.regime AS snapshot_regime,
                       (SELECT p.id FROM snapshots p WHERE p.id<s.id ORDER BY p.id DESC LIMIT 1)
                           AS previous_snapshot_id,
                       {columns}
                FROM s LEFT JOIN metric_samples m ON m.snapshot_id=s.id AND m.metric IN ({marks})
                """,
                (at_us, *names),
            ).fetchall()
        if not rows:
            return None
        first = rows[0]
        captured_at = _iso_from_us(first["snapshot_us"])
        readings: dict[str, dict[str, object] | None] = {name: None for name in names}
        for row in rows:
            if row["metric"] is None:
                continue
            readings[row["metric"]] = {
                "metric": row["metric"],
                "value": row["value_num"] if row["value_kind"] == "number" else None,
                **{key: row[key] for key in _READING_COLUMNS[3:]},
                "captured_us": first["snapshot_us"],
                "captured_at": captured_at,
                "trip_id": first["snapshot_trip"],
            }
        return {
            "snapshot_id": first["snapshot_id"],
            "captured_us": first["snapshot_us"],
            "captured_at": captured_at,
            "trip_id": first["snapshot_trip"],
            "regime": first["snapshot_regime"],
            "previous_snapshot_id": first["previous_snapshot_id"],
            "readings": readings,
            "rpm": readings["engine.rpm"],
        }
