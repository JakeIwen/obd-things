"""Completed-bucket rollups and bounded rollup-first retention."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Mapping

from .models import (
    MICROSECONDS,
    MaintenanceResult,
)
from .validation import (
    _iso,
    _iso_from_us,
    _median_mad,
    _to_us,
    _utc_datetime,
)


class RollupMixin:
    """Completed-bucket rollups and bounded rollup-first retention."""

    def refresh_rollups(
        self,
        *,
        through: datetime | str | None = None,
        max_buckets: int | None = None,
    ) -> dict[str, object]:
        """Build exact per-minute robust summaries for completed buckets.

        Work is bounded so a caller cannot accidentally monopolize the Pi after
        a long offline interval.  Repeated calls advance the stored cursor.
        """

        limit = max_buckets or self.config.rollup_max_buckets_per_call
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError("max_buckets must be between 1 and 10000")
        end_dt = datetime.now(timezone.utc) if through is None else _utc_datetime(through, "through")
        bucket_us = self.config.rollup_seconds * MICROSECONDS
        complete_end = (_to_us(end_dt) // bucket_us) * bucket_us
        with self._lock, self._conn:
            return self._refresh_rollups_locked(
                complete_end=complete_end,
                limit=limit,
            )

    def _rollup_start_locked(
        self,
        *,
        complete_end: int,
        bucket_us: int,
        state_key: str,
    ) -> tuple[int, dict[str, object] | None]:
        state = self._conn.execute(
            "SELECT value FROM historian_meta WHERE key=?", (state_key,)
        ).fetchone()
        if state is None:
            first = self._conn.execute(
                """
                SELECT min(captured_us) FROM metric_samples
                WHERE freshness='fresh' AND value_kind='number'
                """
            ).fetchone()[0]
            if first is None:
                self._set_meta_locked(state_key, str(complete_end))
                return complete_end, {
                    "buckets": 0,
                    "rows": 0,
                    "through": _iso_from_us(complete_end),
                    "backlog": False,
                }
            start = (int(first) // bucket_us) * bucket_us
            if complete_end <= start:
                # The database has data, but none is old enough to roll up.
                # Ingest is strictly chronological, so this boundary is safe
                # to persist and prevents a fresh database from appearing to
                # have a seven-day rollup backlog.
                self._set_meta_locked(state_key, str(complete_end))
        else:
            start = int(state[0])
        return start, None

    def _rollup_bucket_starts_locked(
        self,
        *,
        bucket_us: int,
        start: int,
        complete_end: int,
        limit: int,
    ) -> tuple[list[int], bool]:
        bucket_rows = self._conn.execute(
            """
            SELECT DISTINCT (sample.captured_us / ?) * ? AS bucket_us
            FROM metric_samples AS sample
            WHERE sample.freshness='fresh' AND sample.value_kind='number'
              AND sample.observed_us IS NOT NULL
              AND sample.captured_us>=? AND sample.captured_us<?
              AND NOT EXISTS (
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
            ORDER BY bucket_us LIMIT ?
            """,
            (bucket_us, bucket_us, start, complete_end, limit + 1),
        ).fetchall()
        selected = [int(row["bucket_us"]) for row in bucket_rows[:limit]]
        backlog = len(bucket_rows) > limit
        return selected, backlog

    def _rollup_groups_locked(
        self,
        *,
        bucket_us: int,
        query_start: int,
        query_end: int,
    ) -> dict[tuple[object, ...], list[tuple[int, float]]]:
        rows = self._conn.execute(
            """
            SELECT sample.captured_us,coalesce(sample.trip_id,0) AS trip_key,
                   sample.metric,sample.regime,sample.unit,sample.source,
                   sample.quality,sample.provenance,sample.value_num
            FROM metric_samples AS sample
            WHERE sample.freshness='fresh' AND sample.value_kind='number'
              AND sample.observed_us IS NOT NULL
              AND sample.captured_us>=? AND sample.captured_us<?
              AND NOT EXISTS (
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
            ORDER BY sample.captured_us
            """,
            (query_start, query_end),
        ).fetchall()
        groups: dict[tuple[object, ...], list[tuple[int, float]]] = {}
        for row in rows:
            bucket = (row["captured_us"] // bucket_us) * bucket_us
            key = (
                bucket,
                row["trip_key"],
                row["metric"],
                row["regime"],
                row["unit"],
                row["source"],
                row["quality"],
                row["provenance"],
            )
            groups.setdefault(key, []).append((row["captured_us"], row["value_num"]))
        return groups

    def _write_rollup_groups_locked(
        self,
        groups: Mapping[tuple[object, ...], list[tuple[int, float]]],
        *,
        bucket_seconds: int,
    ) -> None:
        for key, points in groups.items():
            values = [float(point[1]) for point in points]
            median, mad = _median_mad(values)
            self._conn.execute(
                """
                INSERT OR REPLACE INTO metric_rollups(
                    bucket_us,bucket_seconds,trip_key,metric,regime,unit,
                    source,quality,provenance,sample_count,minimum,maximum,
                    mean,median,mad,first_us,last_us
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key[0],
                    bucket_seconds,
                    *key[1:],
                    len(values),
                    min(values),
                    max(values),
                    statistics.fmean(values),
                    median,
                    mad,
                    points[0][0],
                    points[-1][0],
                ),
            )

    def _refresh_rollups_locked(
        self,
        *,
        complete_end: int,
        limit: int,
    ) -> dict[str, object]:
        """Advance completed rollups while the caller owns lock/transaction."""

        bucket_seconds = self.config.rollup_seconds
        bucket_us = bucket_seconds * MICROSECONDS
        state_key = f"rollup_through_us:{bucket_seconds}"
        start, empty_result = self._rollup_start_locked(
            complete_end=complete_end,
            bucket_us=bucket_us,
            state_key=state_key,
        )
        if empty_result is not None:
            return empty_result
        if complete_end <= start:
            return {
                "buckets": 0,
                "rows": 0,
                "through": _iso_from_us(start),
                "backlog": False,
            }
        selected, backlog = self._rollup_bucket_starts_locked(
            bucket_us=bucket_us,
            start=start,
            complete_end=complete_end,
            limit=limit,
        )
        if not selected:
            self._set_meta_locked(state_key, str(complete_end))
            return {
                "buckets": 0,
                "rows": 0,
                "through": _iso_from_us(complete_end),
                "backlog": False,
            }
        query_start = selected[0]
        query_end = selected[-1] + bucket_us
        groups = self._rollup_groups_locked(
            bucket_us=bucket_us,
            query_start=query_start,
            query_end=query_end,
        )
        self._write_rollup_groups_locked(
            groups,
            bucket_seconds=bucket_seconds,
        )
        cursor = query_end if backlog else complete_end
        self._set_meta_locked(state_key, str(cursor))
        return {
            "buckets": len(selected),
            "rows": len(groups),
            "through": _iso_from_us(cursor),
            "backlog": backlog,
        }

    def _maintenance_cutoffs(self, now_us: int) -> tuple[int, int]:
        retention_us = self.config.raw_retention_days * 24 * 60 * 60 * MICROSECONDS
        retention_cutoff = now_us - retention_us
        bucket_us = self.config.rollup_seconds * MICROSECONDS
        delete_before = (retention_cutoff // bucket_us) * bucket_us
        return retention_cutoff, delete_before

    def _maintenance_due_locked(self, now_us: int) -> bool:
        last_status = self._meta_locked("maintenance_last_status")
        if last_status not in (None, "completed"):
            return True
        last_days = self._meta_locked("maintenance_last_retention_days")
        if last_days != str(self.config.raw_retention_days):
            return True
        last_success = self._meta_locked("maintenance_last_success_us")
        if last_success is None:
            return True
        interval_us = self.config.maintenance_interval_seconds * MICROSECONDS
        elapsed_us = now_us - int(last_success)
        return elapsed_us < 0 or elapsed_us >= interval_us

    def maintenance_due(self, *, now: datetime | str | None = None) -> bool:
        """Return whether the inexpensive daily retention gate is open."""

        now_dt = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        with self._lock:
            return self._maintenance_due_locked(_to_us(now_dt))

    def maintenance_status(
        self,
        *,
        now: datetime | str | None = None,
    ) -> dict[str, object]:
        """Return compact retention/cadence state without scanning raw tables."""

        now_dt = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        now_us = _to_us(now_dt)
        retention_cutoff, delete_before = self._maintenance_cutoffs(now_us)
        with self._lock:
            last_attempt = self._meta_locked("maintenance_last_attempt_us")
            last_success = self._meta_locked("maintenance_last_success_us")
            last_cutoff = self._meta_locked("maintenance_last_cutoff_us")
            last_status = self._meta_locked("maintenance_last_status") or "never"
            rollup_cursor = self._meta_locked(
                f"rollup_through_us:{self.config.rollup_seconds}"
            )
            last_deleted_text = self._meta_locked("maintenance_last_deleted")
            try:
                last_deleted = (
                    json.loads(last_deleted_text) if last_deleted_text is not None else None
                )
            except json.JSONDecodeError:
                last_deleted = None
            due = self._maintenance_due_locked(now_us)
        return {
            "raw_retention_days": self.config.raw_retention_days,
            "retention_cutoff_at": _iso_from_us(retention_cutoff),
            "effective_cutoff_at": _iso_from_us(delete_before),
            "maintenance_interval_seconds": self.config.maintenance_interval_seconds,
            "delete_limit_per_table": (
                self.config.maintenance_max_delete_rows_per_table
            ),
            "due": due,
            "last_attempt_at": (
                _iso_from_us(int(last_attempt)) if last_attempt is not None else None
            ),
            "last_success_at": (
                _iso_from_us(int(last_success)) if last_success is not None else None
            ),
            "last_effective_cutoff_at": (
                _iso_from_us(int(last_cutoff)) if last_cutoff is not None else None
            ),
            "rollup_through_at": (
                _iso_from_us(int(rollup_cursor)) if rollup_cursor is not None else None
            ),
            "last_status": last_status,
            "last_deleted": last_deleted,
        }

    def _record_maintenance_locked(
        self,
        *,
        now_us: int,
        delete_before: int,
        status: str,
        deleted: Mapping[str, int],
        completed: bool,
    ) -> None:
        self._set_meta_locked("maintenance_last_attempt_us", str(now_us))
        self._set_meta_locked("maintenance_last_cutoff_us", str(delete_before))
        self._set_meta_locked("maintenance_last_status", status)
        self._set_meta_locked(
            "maintenance_last_deleted",
            json.dumps(dict(deleted), sort_keys=True, separators=(",", ":")),
        )
        self._set_meta_locked(
            "maintenance_last_retention_days",
            str(self.config.raw_retention_days),
        )
        if completed:
            self._set_meta_locked("maintenance_last_success_us", str(now_us))

    def _delete_orphan_snapshots_locked(self, cutoff_us: int, limit: int) -> int:
        cursor = self._conn.execute(
            """
            DELETE FROM snapshots WHERE id IN (
                SELECT snapshot.id FROM snapshots AS snapshot
                WHERE snapshot.captured_us<?
                  AND NOT EXISTS (
                      SELECT 1 FROM metric_samples AS metric
                      WHERE metric.snapshot_id=snapshot.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM interface_samples AS interface
                      WHERE interface.snapshot_id=snapshot.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM metric_gaps AS gap
                      WHERE gap.first_snapshot_id=snapshot.id
                         OR gap.last_snapshot_id=snapshot.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM interface_gaps AS gap
                      WHERE gap.first_snapshot_id=snapshot.id
                         OR gap.last_snapshot_id=snapshot.id
                  )
                ORDER BY snapshot.captured_us,snapshot.id
                LIMIT ?
            )
            """,
            (cutoff_us, limit),
        )
        return max(0, cursor.rowcount)

    def _has_orphan_snapshots_locked(self, cutoff_us: int) -> bool:
        return self._conn.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM snapshots AS snapshot
                WHERE snapshot.captured_us<?
                  AND NOT EXISTS (
                      SELECT 1 FROM metric_samples AS metric
                      WHERE metric.snapshot_id=snapshot.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM interface_samples AS interface
                      WHERE interface.snapshot_id=snapshot.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM metric_gaps AS gap
                      WHERE gap.first_snapshot_id=snapshot.id
                         OR gap.last_snapshot_id=snapshot.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM interface_gaps AS gap
                      WHERE gap.first_snapshot_id=snapshot.id
                         OR gap.last_snapshot_id=snapshot.id
                  )
                LIMIT 1
            )
            """,
            (cutoff_us,),
        ).fetchone()[0] == 1

    def _maintenance_not_due_result(
        self,
        *,
        now_dt: datetime,
        retention_cutoff: int,
        delete_before: int,
    ) -> MaintenanceResult:
        return MaintenanceResult(
            status="not_due",
            attempted_at=_iso(now_dt),
            retention_cutoff_at=_iso_from_us(retention_cutoff),
            delete_before_at=_iso_from_us(delete_before),
            rollup_passes=0,
            rollup_buckets=0,
            rollup_rows=0,
            deleted_metric_samples=0,
            deleted_interface_samples=0,
            deleted_snapshots=0,
            raw_backlog=False,
            detail="Daily raw-retention cadence has not elapsed.",
        )

    def _advance_maintenance_rollups_locked(
        self,
        *,
        delete_before: int,
        passes_limit: int,
        rollup_passes: int,
        rollup_buckets: int,
        rollup_rows: int,
    ) -> tuple[int | None, int, int, int]:
        state_key = f"rollup_through_us:{self.config.rollup_seconds}"
        cursor_text = self._meta_locked(state_key)
        cursor_us = int(cursor_text) if cursor_text is not None else None
        while (cursor_us is None or cursor_us < delete_before) and (
            rollup_passes < passes_limit
        ):
            rollup = self._refresh_rollups_locked(
                complete_end=delete_before,
                limit=self.config.rollup_max_buckets_per_call,
            )
            rollup_passes += 1
            rollup_buckets += int(rollup["buckets"])
            rollup_rows += int(rollup["rows"])
            next_cursor_text = self._meta_locked(state_key)
            next_cursor = (
                int(next_cursor_text) if next_cursor_text is not None else None
            )
            if next_cursor == cursor_us:
                break
            cursor_us = next_cursor
        return cursor_us, rollup_passes, rollup_buckets, rollup_rows

    def _blocked_rollup_maintenance_result_locked(
        self,
        *,
        now_dt: datetime,
        now_us: int,
        retention_cutoff: int,
        delete_before: int,
        deleted: Mapping[str, int],
        rollup_passes: int,
        rollup_buckets: int,
        rollup_rows: int,
    ) -> MaintenanceResult:
        self._record_maintenance_locked(
            now_us=now_us,
            delete_before=delete_before,
            status="blocked_rollup_backlog",
            deleted=deleted,
            completed=False,
        )
        return MaintenanceResult(
            status="blocked_rollup_backlog",
            attempted_at=_iso(now_dt),
            retention_cutoff_at=_iso_from_us(retention_cutoff),
            delete_before_at=_iso_from_us(delete_before),
            rollup_passes=rollup_passes,
            rollup_buckets=rollup_buckets,
            rollup_rows=rollup_rows,
            deleted_metric_samples=0,
            deleted_interface_samples=0,
            deleted_snapshots=0,
            raw_backlog=True,
            detail=(
                "Persisted rollup cursor is short of the retention "
                "boundary; no raw rows were deleted."
            ),
        )

    def _delete_raw_history_locked(
        self,
        *,
        delete_before: int,
        limit: int,
        deleted: dict[str, int],
    ) -> None:
        metric_cursor = self._conn.execute(
                    """
                    DELETE FROM metric_samples WHERE id IN (
                        SELECT candidate.id FROM metric_samples AS candidate
                        WHERE candidate.captured_us<?
                          AND NOT (
                              candidate.freshness='fresh'
                              AND candidate.value_kind='number'
                              AND candidate.observed_us IS NOT NULL
                              AND NOT EXISTS (
                                  SELECT 1 FROM metric_samples AS earlier
                                  WHERE earlier.metric=candidate.metric
                                    AND earlier.source=candidate.source
                                    AND earlier.unit=candidate.unit
                                    AND earlier.quality=candidate.quality
                                    AND earlier.provenance=candidate.provenance
                                    AND earlier.observed_us=candidate.observed_us
                                    AND earlier.freshness='fresh'
                                    AND earlier.value_kind='number'
                                    AND (
                                        earlier.captured_us<candidate.captured_us
                                        OR (
                                            earlier.captured_us=candidate.captured_us
                                            AND earlier.id<candidate.id
                                        )
                                    )
                              )
                              AND EXISTS (
                                  SELECT 1 FROM metric_samples AS survivor
                                  WHERE survivor.metric=candidate.metric
                                    AND survivor.source=candidate.source
                                    AND survivor.unit=candidate.unit
                                    AND survivor.quality=candidate.quality
                                    AND survivor.provenance=candidate.provenance
                                    AND survivor.observed_us=candidate.observed_us
                                    AND survivor.freshness='fresh'
                                    AND survivor.value_kind='number'
                                    AND survivor.captured_us>=?
                              )
                          )
                        ORDER BY candidate.captured_us,candidate.id LIMIT ?
                    )
                    """,
                    (delete_before, delete_before, limit),
                )
        deleted["metric_samples"] = max(0, metric_cursor.rowcount)
        interface_cursor = self._conn.execute(
                    """
                    DELETE FROM interface_samples WHERE rowid IN (
                        SELECT rowid FROM interface_samples
                        WHERE captured_us<? ORDER BY captured_us,rowid LIMIT ?
                    )
                    """,
                    (delete_before, limit),
                )
        deleted["interface_samples"] = max(
            0, interface_cursor.rowcount
        )
        deleted["snapshots"] = self._delete_orphan_snapshots_locked(
            delete_before,
            limit,
        )

    def _raw_history_backlog_locked(self, delete_before: int) -> bool:
        metric_backlog = bool(
                    self._conn.execute(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM metric_samples AS candidate
                            WHERE candidate.captured_us<?
                              AND NOT (
                                  candidate.freshness='fresh'
                                  AND candidate.value_kind='number'
                                  AND candidate.observed_us IS NOT NULL
                                  AND NOT EXISTS (
                                      SELECT 1 FROM metric_samples AS earlier
                                      WHERE earlier.metric=candidate.metric
                                        AND earlier.source=candidate.source
                                        AND earlier.unit=candidate.unit
                                        AND earlier.quality=candidate.quality
                                        AND earlier.provenance=candidate.provenance
                                        AND earlier.observed_us=candidate.observed_us
                                        AND earlier.freshness='fresh'
                                        AND earlier.value_kind='number'
                                        AND (
                                            earlier.captured_us<candidate.captured_us
                                            OR (
                                                earlier.captured_us=candidate.captured_us
                                                AND earlier.id<candidate.id
                                            )
                                        )
                                  )
                                  AND EXISTS (
                                      SELECT 1 FROM metric_samples AS survivor
                                      WHERE survivor.metric=candidate.metric
                                        AND survivor.source=candidate.source
                                        AND survivor.unit=candidate.unit
                                        AND survivor.quality=candidate.quality
                                        AND survivor.provenance=candidate.provenance
                                        AND survivor.observed_us=candidate.observed_us
                                        AND survivor.freshness='fresh'
                                        AND survivor.value_kind='number'
                                        AND survivor.captured_us>=?
                                  )
                              )
                            LIMIT 1
                        )
                        """,
                        (delete_before, delete_before),
                    ).fetchone()[0]
                )
        interface_backlog = bool(
                    self._conn.execute(
                        "SELECT EXISTS(SELECT 1 FROM interface_samples "
                        "WHERE captured_us<? LIMIT 1)",
                        (delete_before,),
                    ).fetchone()[0]
                )
        return (
            metric_backlog
            or interface_backlog
            or self._has_orphan_snapshots_locked(delete_before)
        )

    def _finished_maintenance_result_locked(
        self,
        *,
        now_dt: datetime,
        now_us: int,
        retention_cutoff: int,
        delete_before: int,
        deleted: Mapping[str, int],
        rollup_passes: int,
        rollup_buckets: int,
        rollup_rows: int,
        raw_backlog: bool,
    ) -> MaintenanceResult:
        status = "partial" if raw_backlog else "completed"
        self._record_maintenance_locked(
            now_us=now_us,
            delete_before=delete_before,
            status=status,
            deleted=deleted,
            completed=not raw_backlog,
        )
        detail = (
            "Deletion cap reached; remaining cursor-covered raw rows "
            "will be handled on the next cadence check."
            if raw_backlog
            else "Completed rollups cover all pruned raw rows."
        )
        return MaintenanceResult(
            status=status,
            attempted_at=_iso(now_dt),
            retention_cutoff_at=_iso_from_us(retention_cutoff),
            delete_before_at=_iso_from_us(delete_before),
            rollup_passes=rollup_passes,
            rollup_buckets=rollup_buckets,
            rollup_rows=rollup_rows,
            deleted_metric_samples=deleted["metric_samples"],
            deleted_interface_samples=deleted["interface_samples"],
            deleted_snapshots=deleted["snapshots"],
            raw_backlog=raw_backlog,
            detail=detail,
        )

    def run_maintenance(
        self,
        *,
        now: datetime | str | None = None,
        force: bool = False,
        max_rollup_passes: int | None = None,
    ) -> MaintenanceResult:
        """Roll up, then transactionally prune only cursor-covered raw rows.

        A partial rollup never permits deletion.  Each raw table and the orphan
        snapshot sweep has a hard row cap; a capped deletion reports ``partial``
        and remains due for the next inexpensive cadence check.
        """

        now_dt = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        now_us = _to_us(now_dt)
        retention_cutoff, delete_before = self._maintenance_cutoffs(now_us)
        passes_limit = (
            self.config.maintenance_max_rollup_passes
            if max_rollup_passes is None
            else max_rollup_passes
        )
        if (
            not isinstance(passes_limit, int)
            or isinstance(passes_limit, bool)
            or not 1 <= passes_limit <= 10_000
        ):
            raise ValueError("max_rollup_passes must be between 1 and 10000")
        deleted = {
            "metric_samples": 0,
            "interface_samples": 0,
            "snapshots": 0,
        }
        rollup_passes = 0
        rollup_buckets = 0
        rollup_rows = 0
        with self._lock:
            if not force and not self._maintenance_due_locked(now_us):
                return self._maintenance_not_due_result(
                    now_dt=now_dt,
                    retention_cutoff=retention_cutoff,
                    delete_before=delete_before,
                )
            with self._conn:
                (
                    cursor_us,
                    rollup_passes,
                    rollup_buckets,
                    rollup_rows,
                ) = self._advance_maintenance_rollups_locked(
                    delete_before=delete_before,
                    passes_limit=passes_limit,
                    rollup_passes=rollup_passes,
                    rollup_buckets=rollup_buckets,
                    rollup_rows=rollup_rows,
                )
                if cursor_us is None or cursor_us < delete_before:
                    return self._blocked_rollup_maintenance_result_locked(
                        now_dt=now_dt,
                        now_us=now_us,
                        retention_cutoff=retention_cutoff,
                        delete_before=delete_before,
                        deleted=deleted,
                        rollup_passes=rollup_passes,
                        rollup_buckets=rollup_buckets,
                        rollup_rows=rollup_rows,
                    )

                limit = self.config.maintenance_max_delete_rows_per_table
                self._delete_raw_history_locked(
                    delete_before=delete_before,
                    limit=limit,
                    deleted=deleted,
                )
                raw_backlog = self._raw_history_backlog_locked(delete_before)
                return self._finished_maintenance_result_locked(
                    now_dt=now_dt,
                    now_us=now_us,
                    retention_cutoff=retention_cutoff,
                    delete_before=delete_before,
                    deleted=deleted,
                    rollup_passes=rollup_passes,
                    rollup_buckets=rollup_buckets,
                    rollup_rows=rollup_rows,
                    raw_backlog=raw_backlog,
                )

    def maybe_run_maintenance(
        self,
        *,
        now: datetime | str | None = None,
    ) -> MaintenanceResult:
        """Cheap hourly-callable hook; real work is metadata-gated to daily."""

        return self.run_maintenance(now=now, force=False)
