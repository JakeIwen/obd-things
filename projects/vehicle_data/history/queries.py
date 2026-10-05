"""Bounded history queries and dashboard payloads."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .models import (
    BaselineStats,
    DATA_QUALITY_SCHEMA_VERSION,
    MAX_PERIOD_DAYS,
    MAX_QUERY_SAMPLES,
    MAX_SERIES_POINTS,
    MAX_SERIES_WINDOW_SECONDS,
    MICROSECONDS,
    REGIME_DIMENSIONS,
    SCHEMA_VERSION,
)
from .validation import (
    _iso,
    _iso_from_us,
    _median_mad,
    _to_us,
    _utc_datetime,
    project_regime,
)


class QueryMixin:
    """Bounded history queries and dashboard payloads."""

    @staticmethod
    def _sample_dict(row: sqlite3.Row) -> dict[str, object]:
        if row["value_kind"] == "boolean":
            value: object = bool(row["value_bool"])
        elif row["value_kind"] == "number":
            value = row["value_num"]
        else:
            value = row["value_text"]
        return {
            "sample_id": row["id"],
            "captured_at": _iso_from_us(row["captured_us"]),
            "observed_at": row["observed_at"],
            "metric": row["metric"],
            "value": value,
            "unit": row["unit"],
            "source": row["source"],
            "bus": row["bus"],
            "acquisition": row["acquisition"],
            "interface_mode": row["interface_mode"],
            "quality": row["quality"],
            "provenance": row["provenance"],
            "age_ms": row["source_age_ms"],
            "reported_stale": (
                bool(row["reported_stale"]) if row["reported_stale"] is not None else None
            ),
            "freshness": row["freshness"],
            "regime": row["regime"],
            "trip_id": row["trip_id"],
        }

    def query_samples(
        self,
        metric: str,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 500,
        fresh_only: bool = False,
        regime: str | None = None,
        trip_id: int | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, object]]:
        """Return a hard-bounded diagnostic sample query.

        Dashboard trend code should use :meth:`metric_series`; this method is
        intended for focused inspection and tests.
        """

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_QUERY_SAMPLES:
            raise ValueError(f"limit must be between 1 and {MAX_QUERY_SAMPLES}")
        clauses = ["metric=?"]
        args: list[object] = [metric]
        if start is not None:
            clauses.append("captured_us>=?")
            args.append(_to_us(_utc_datetime(start, "start")))
        if end is not None:
            clauses.append("captured_us<=?")
            args.append(_to_us(_utc_datetime(end, "end")))
        if fresh_only:
            clauses.append("freshness='fresh'")
        if regime is not None:
            clauses.append("regime=?")
            args.append(regime)
        if trip_id is not None:
            clauses.append("trip_id=?")
            args.append(trip_id)
        order = "DESC" if newest_first else "ASC"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM metric_samples WHERE {' AND '.join(clauses)} ORDER BY captured_us {order} LIMIT ?",
                args,
            ).fetchall()
        return [self._sample_dict(row) for row in rows]

    def latest_sample(
        self,
        metric: str,
        *,
        at: datetime | str | None = None,
        fresh_only: bool = True,
    ) -> dict[str, object] | None:
        clauses = ["metric=?"]
        args: list[object] = [metric]
        if at is not None:
            clauses.append("captured_us<=?")
            args.append(_to_us(_utc_datetime(at, "at")))
        if fresh_only:
            clauses.append("freshness='fresh'")
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM metric_samples WHERE {' AND '.join(clauses)} ORDER BY captured_us DESC LIMIT 1",
                args,
            ).fetchone()
        return self._sample_dict(row) if row is not None else None

    @staticmethod
    def _validate_continuous_condition_limits(
        *,
        minimum: float,
        max_gap_seconds: float,
        max_lookback_seconds: float,
    ) -> None:
        for name, value in (
            ("minimum", minimum),
            ("max_gap_seconds", max_gap_seconds),
            ("max_lookback_seconds", max_lookback_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite")
        if max_gap_seconds <= 0 or max_lookback_seconds <= 0:
            raise ValueError("condition gap and lookback must be positive")

    @staticmethod
    def _continuous_condition_result(
        rows: Sequence[sqlite3.Row],
        *,
        metric: str,
        minimum: float,
        max_gap_seconds: float,
        source: str,
        quality: str,
        provenance: str,
        trip_id: int | None,
        at_us: int,
    ) -> dict[str, object] | None:
        if not rows or float(rows[0]["value_num"]) < float(minimum):
            return None
        newest_us = int(rows[0]["observed_us"])
        oldest_us = newest_us
        newer_us = newest_us
        count = 0
        max_gap_us = int(round(max_gap_seconds * MICROSECONDS))
        for row in rows:
            observed_us = int(row["observed_us"])
            if newer_us - observed_us > max_gap_us:
                break
            if float(row["value_num"]) < float(minimum):
                break
            oldest_us = observed_us
            newer_us = observed_us
            count += 1
        return {
            "metric": metric,
            "minimum": float(minimum),
            "started_at": _iso_from_us(oldest_us),
            "latest_observed_at": _iso_from_us(newest_us),
            "duration_seconds": max(0.0, (at_us - oldest_us) / MICROSECONDS),
            "observation_count": count,
            "max_gap_seconds": float(max_gap_seconds),
            "source": source,
            "quality": quality,
            "provenance": provenance,
            "trip_id": trip_id,
        }

    def continuous_numeric_condition(
        self,
        metric: str,
        *,
        at: datetime | str,
        minimum: float,
        max_gap_seconds: float,
        source: str,
        quality: str,
        provenance: str,
        trip_id: int | None,
        max_lookback_seconds: float = 15 * 60,
    ) -> dict[str, object] | None:
        """Describe the current independently observed numeric condition.

        This is intentionally exact-source/provenance scoped.  Repeated
        historian snapshots of one cached observation count once, and any
        sub-threshold value or observation gap ends the continuous interval.
        It is used for positive RPM-based engine-running evidence; it does not
        infer running from voltage or mere bus activity.
        """

        self._validate_continuous_condition_limits(
            minimum=minimum,
            max_gap_seconds=max_gap_seconds,
            max_lookback_seconds=max_lookback_seconds,
        )
        at_us = _to_us(_utc_datetime(at, "at"))
        start_us = at_us - int(round(max_lookback_seconds * MICROSECONDS))
        clauses = [
            "sample.metric=?",
            "sample.observed_us>=?",
            "sample.observed_us<=?",
            "sample.freshness='fresh'",
            "sample.value_kind='number'",
            "sample.source=?",
            "sample.quality=?",
            "sample.provenance=?",
            """
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
            """,
        ]
        args: list[object] = [
            metric,
            start_us,
            at_us,
            source,
            quality,
            provenance,
        ]
        if trip_id is None:
            clauses.append("sample.trip_id IS NULL")
        else:
            clauses.append("sample.trip_id=?")
            args.append(trip_id)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT sample.* FROM metric_samples AS sample "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY sample.observed_us DESC,sample.captured_us DESC LIMIT 1000",
                args,
            ).fetchall()
        return self._continuous_condition_result(
            rows,
            metric=metric,
            minimum=minimum,
            max_gap_seconds=max_gap_seconds,
            source=source,
            quality=quality,
            provenance=provenance,
            trip_id=trip_id,
            at_us=at_us,
        )

    def system_health_context(self, snapshot_id: int) -> dict[str, object]:
        """Return persisted role/topology facts for one stored snapshot."""

        if not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool) or snapshot_id < 1:
            raise ValueError("snapshot_id must be a positive integer")
        with self._lock:
            snapshot = self._conn.execute(
                "SELECT id,captured_us,captured_at FROM snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise KeyError(f"unknown snapshot {snapshot_id}")
            system = self._conn.execute(
                "SELECT * FROM system_health_samples WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            probe_row = self._conn.execute("SELECT probe_json FROM interface_probe_samples WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            probe = json.loads(probe_row[0]) if probe_row is not None else None
            role_rows = self._conn.execute(
                """
                SELECT sample.*,detail.resolution,detail.role_reason,
                       detail.detail,detail.usb_dev_id,
                       detail.topology_generation
                FROM interface_samples AS sample
                LEFT JOIN interface_role_details AS detail
                  ON detail.snapshot_id=sample.snapshot_id
                 AND detail.role=sample.role
                WHERE sample.snapshot_id=? ORDER BY sample.role
                """,
                (snapshot_id,),
            ).fetchall()
            gap_rows = self._conn.execute(
                """
                SELECT role,state,reason,observation_count,started_at,last_seen_at
                FROM interface_gaps WHERE ended_us IS NULL
                """
            ).fetchall()
            generation = (
                system["topology_generation"] if system is not None else None
            )
            previous = self._conn.execute(
                """
                SELECT topology_generation,captured_us
                FROM system_health_samples
                WHERE snapshot_id<? AND topology_generation IS NOT NULL
                ORDER BY captured_us DESC LIMIT 1
                """,
                (snapshot_id,),
            ).fetchone()
        gaps = {
            row["role"]: {
                "state": row["state"],
                "reason": row["reason"],
                "observation_count": row["observation_count"],
                "started_at": row["started_at"],
                "last_seen_at": row["last_seen_at"],
            }
            for row in gap_rows
        }
        roles = {
            row["role"]: {
                "role": row["role"],
                "channel": row["channel"],
                "usb_serial": row["usb_serial"],
                "usb_dev_id": row["usb_dev_id"],
                "bus": row["bus"],
                "resolution": row["resolution"],
                "role_reason": row["role_reason"],
                "detail": row["detail"],
                "adapter_present": (
                    bool(row["adapter_present"])
                    if row["adapter_present"] is not None
                    else None
                ),
                "up": bool(row["up"]) if row["up"] is not None else None,
                "bitrate": row["bitrate"],
                "listen_only": (
                    bool(row["listen_only"])
                    if row["listen_only"] is not None
                    else None
                ),
                "controller_state": row["controller_state"],
                "topology_usable": (
                    bool(row["topology_usable"])
                    if row["topology_usable"] is not None
                    else None
                ),
                "topology_generation": row["topology_generation"],
                "health": row["health"],
                "reason": row["reason"],
                "active_gap": gaps.get(row["role"]),
            }
            for row in role_rows
        }
        issues: list[object] = []
        inhibits: list[object] = []
        if system is not None:
            try:
                decoded_issues = json.loads(system["issues_json"])
                decoded_inhibits = json.loads(system["active_inhibits_json"])
                if isinstance(decoded_issues, list):
                    issues = decoded_issues
                if isinstance(decoded_inhibits, list):
                    inhibits = decoded_inhibits
            except (TypeError, json.JSONDecodeError):
                pass
        return {
            "snapshot_id": snapshot_id,
            "captured_at": snapshot["captured_at"],
            "interface_probe": probe,
            "topology_generation": generation,
            "previous_topology_generation": (
                previous["topology_generation"] if previous is not None else None
            ),
            "topology_changed": bool(
                generation is not None
                and previous is not None
                and previous["topology_generation"] is not None
                and generation != previous["topology_generation"]
            ),
            "issues": issues,
            "active_inhibits": inhibits,
            "restoration_failed": (
                bool(system["restoration_failed"])
                if system is not None and system["restoration_failed"] is not None
                else None
            ),
            "roles": roles,
            "active_interface_gaps": gaps,
        }

    @staticmethod
    def _usb_can_payload(row: sqlite3.Row) -> dict[str, object]:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def usb_can_health_context(self, snapshot_id: int) -> dict[str, object]:
        """Return newly observed edges and incidents active at one snapshot."""

        if not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool) or snapshot_id < 1:
            raise ValueError("snapshot_id must be a positive integer")
        with self._lock:
            snapshot = self._conn.execute(
                "SELECT captured_us,captured_at FROM snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise KeyError(f"unknown snapshot {snapshot_id}")
            sample = self._conn.execute(
                "SELECT * FROM usb_can_monitor_samples WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            event_rows = self._conn.execute(
                """
                SELECT * FROM usb_can_events
                WHERE first_snapshot_id=? ORDER BY occurred_us,event_id
                """,
                (snapshot_id,),
            ).fetchall()
            unconsumed_removal_rows = self._conn.execute(
                """
                SELECT event.* FROM usb_can_events AS event
                LEFT JOIN usb_can_advisory_consumption AS consumed
                  ON consumed.event_id=event.event_id
                WHERE consumed.event_id IS NULL
                  AND event.occurred_us<=?
                  AND event.kind IN (
                      'usb_parent_hub_removed',
                      'usb_can_adapter_removed',
                      'usb_can_netdev_removed'
                  )
                ORDER BY event.occurred_us,event.event_id LIMIT 512
                """,
                (snapshot["captured_us"],),
            ).fetchall()
            active_rows = self._conn.execute(
                """
                SELECT * FROM usb_can_incidents
                WHERE opened_us<=?
                  AND (resolved_us IS NULL OR resolved_us>?)
                ORDER BY opened_us,incident_id
                """,
                (snapshot["captured_us"], snapshot["captured_us"]),
            ).fetchall()
            recent_removals = int(
                self._conn.execute(
                    """
                    SELECT count(*) FROM usb_can_events
                    WHERE occurred_us BETWEEN ? AND ?
                      AND kind IN (
                          'usb_parent_hub_removed',
                          'usb_can_adapter_removed',
                          'usb_can_netdev_removed'
                      )
                    """,
                    (
                        int(snapshot["captured_us"]) - 24 * 60 * 60 * MICROSECONDS,
                        snapshot["captured_us"],
                    ),
                ).fetchone()[0]
            )
        events = [self._usb_can_payload(row) for row in event_rows]
        active = [self._usb_can_payload(row) for row in active_rows]
        removal_events = [
            self._usb_can_payload(row) for row in unconsumed_removal_rows
        ]
        recovery_events = [
            event for event in events if event.get("kind") == "usb_can_recovered"
        ]
        return {
            "available": sample is not None,
            "snapshot_id": snapshot_id,
            "captured_at": snapshot["captured_at"],
            "source": sample["source"] if sample is not None else None,
            "producer_instance": (
                sample["producer_instance"] if sample is not None else None
            ),
            "dropped_event_count": (
                int(sample["dropped_event_count"]) if sample is not None else 0
            ),
            "pending_event_count": (
                int(sample["pending_event_count"]) if sample is not None else 0
            ),
            "new_events": events,
            "new_removal_events": removal_events,
            "unconsumed_removal_event_ids": [
                event.get("event_id")
                for event in removal_events
                if isinstance(event.get("event_id"), str)
            ],
            "new_recovery_events": recovery_events,
            "active_incidents": active,
            "removal_event_count_24h": recent_removals,
        }

    def mark_usb_can_advisory_events_consumed(
        self,
        event_ids: Sequence[str],
        *,
        consumed_at: datetime | str,
        snapshot_id: int,
    ) -> int:
        """Checkpoint removal edges only after advisory persistence commits."""

        if (
            not isinstance(snapshot_id, int)
            or isinstance(snapshot_id, bool)
            or snapshot_id < 1
        ):
            raise ValueError("snapshot_id must be a positive integer")
        if isinstance(event_ids, (str, bytes, bytearray)):
            raise ValueError("event_ids must be a sequence")
        normalized = tuple(event_ids)
        if len(normalized) > 512 or any(
            not isinstance(event_id, str)
            or not event_id.startswith("usb-can-event-v1:")
            or len(event_id) > 128
            for event_id in normalized
        ):
            raise ValueError("event_ids contains an invalid USB CAN event identity")
        if len(normalized) != len(set(normalized)):
            raise ValueError("event_ids must be unique")
        moment = _utc_datetime(consumed_at, "consumed_at")
        consumed_us = _to_us(moment)
        with self._lock, self._conn:
            snapshot = self._conn.execute(
                "SELECT 1 FROM snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise KeyError(f"unknown snapshot {snapshot_id}")
            inserted = 0
            for event_id in normalized:
                event = self._conn.execute(
                    "SELECT kind FROM usb_can_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if event is None:
                    raise KeyError(f"unknown USB CAN event {event_id}")
                if event["kind"] not in (
                    "usb_parent_hub_removed",
                    "usb_can_adapter_removed",
                    "usb_can_netdev_removed",
                ):
                    raise ValueError(
                        f"USB CAN event {event_id} is not a removal edge"
                    )
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO usb_can_advisory_consumption(
                        event_id,consumed_us,consumed_at,snapshot_id
                    ) VALUES(?,?,?,?)
                    """,
                    (event_id, consumed_us, _iso(moment), snapshot_id),
                )
                inserted += max(0, cursor.rowcount)
        return inserted

    def usb_can_advisory_consumed_event_ids(
        self,
        event_ids: Sequence[str],
    ) -> tuple[str, ...]:
        """Return the requested removal IDs with a durable advisory checkpoint."""

        if isinstance(event_ids, (str, bytes, bytearray)):
            raise ValueError("event_ids must be a sequence")
        normalized = tuple(event_ids)
        if len(normalized) > 512 or any(
            not isinstance(event_id, str)
            or not event_id.startswith("usb-can-event-v1:")
            or len(event_id) > 128
            for event_id in normalized
        ):
            raise ValueError("event_ids contains an invalid USB CAN event identity")
        if len(normalized) != len(set(normalized)):
            raise ValueError("event_ids must be unique")
        if not normalized:
            return ()
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id FROM usb_can_advisory_consumption "
                f"WHERE event_id IN ({','.join('?' for _ in normalized)})",
                normalized,
            ).fetchall()
        consumed = {str(row["event_id"]) for row in rows}
        return tuple(event_id for event_id in normalized if event_id in consumed)

    def usb_can_incident_summary(self, *, limit: int = 32) -> dict[str, object]:
        """Return bounded durable USB/CAN incident history for health APIs."""

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 128:
            raise ValueError("USB CAN incident limit must be between 1 and 128")
        with self._lock:
            event_count = int(
                self._conn.execute("SELECT count(*) FROM usb_can_events").fetchone()[0]
            )
            incident_count = int(
                self._conn.execute("SELECT count(*) FROM usb_can_incidents").fetchone()[0]
            )
            active_rows = self._conn.execute(
                """
                SELECT * FROM usb_can_incidents WHERE state='active'
                ORDER BY opened_us DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            recent_rows = self._conn.execute(
                """
                SELECT * FROM usb_can_incidents
                ORDER BY opened_us DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            event_rows = self._conn.execute(
                """
                SELECT * FROM usb_can_events
                ORDER BY occurred_us DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            monitor = self._conn.execute(
                """
                SELECT * FROM usb_can_monitor_samples
                ORDER BY captured_us DESC LIMIT 1
                """
            ).fetchone()
        return {
            "schema_version": 1,
            "available": monitor is not None,
            "event_count": event_count,
            "incident_count": incident_count,
            "active_count": len(active_rows),
            "active": [self._usb_can_payload(row) for row in active_rows],
            "recent_incidents": [
                self._usb_can_payload(row) for row in recent_rows
            ],
            "recent_events": [self._usb_can_payload(row) for row in event_rows],
            "dropped_event_count": (
                int(monitor["dropped_event_count"]) if monitor is not None else 0
            ),
            "producer_instance": (
                monitor["producer_instance"] if monitor is not None else None
            ),
            "last_sample_at": (
                _iso_from_us(int(monitor["captured_us"]))
                if monitor is not None
                else None
            ),
            "detail": (
                "Receive-only kernel edges are deduplicated by stable event ID; "
                "resolved incidents remain in durable history."
            ),
        }

    def recent_numeric_samples(
        self,
        metric: str,
        *,
        regime: str,
        trip_id: int | None,
        at: datetime | str,
        limit: int,
        quality: str,
        source: str,
        provenance: str,
        regime_dimensions: Sequence[str] = REGIME_DIMENSIONS,
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        clauses = [
            "sample.metric=?",
            "sample.captured_us<=?",
            "sample.freshness='fresh'",
            "sample.value_kind='number'",
            "sample.observed_us IS NOT NULL",
            "sample.quality=?",
            "sample.source=?",
            "sample.provenance=?",
            """
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
            """,
        ]
        args: list[object] = [
            metric,
            _to_us(_utc_datetime(at, "at")),
            quality,
            source,
            provenance,
        ]
        if trip_id is None:
            clauses.append("sample.trip_id IS NULL")
        else:
            clauses.append("sample.trip_id=?")
            args.append(trip_id)
        # Fetch a bounded superset and project in Python because different
        # warning families intentionally condition on different dimensions.
        fetch_limit = min(1_000, max(limit, limit * 20))
        args.append(fetch_limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT sample.* FROM metric_samples AS sample "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY sample.observed_us DESC,sample.captured_us DESC LIMIT ?",
                args,
            ).fetchall()
        target = project_regime(regime, regime_dimensions)
        selected = [
            self._sample_dict(row)
            for row in rows
            if project_regime(row["regime"], regime_dimensions) == target
        ]
        return selected[:limit]

    def robust_baseline(
        self,
        metric: str,
        regime: str,
        *,
        before: datetime | str,
        lookback_days: int = 30,
        exclude_trip_id: int | None = None,
        unit: str,
        quality: str,
        source: str,
        provenance: str,
        regime_dimensions: Sequence[str] = REGIME_DIMENSIONS,
        minimum_trip_age_seconds: float = 0,
    ) -> BaselineStats | None:
        """Return a median/MAD baseline of completed bucket medians.

        Quality, source, unit, and provenance are exact filters.  A decoder or
        evidence change therefore starts a new baseline instead of silently
        joining unlike observations.
        """

        if not isinstance(lookback_days, int) or not 1 <= lookback_days <= MAX_PERIOD_DAYS:
            raise ValueError(f"lookback_days must be between 1 and {MAX_PERIOD_DAYS}")
        before_dt = _utc_datetime(before, "before")
        before_us = _to_us(before_dt)
        start_us = _to_us(before_dt - timedelta(days=lookback_days))
        clauses = [
            "metric=?",
            "bucket_us>=?",
            "bucket_us<?",
            "unit=?",
            "quality=?",
            "source=?",
            "provenance=?",
        ]
        args: list[object] = [
            metric,
            start_us,
            before_us,
            unit,
            quality,
            source,
            provenance,
        ]
        if minimum_trip_age_seconds:
            clauses.append("EXISTS (SELECT 1 FROM trips WHERE trips.id=metric_rollups.trip_key AND metric_rollups.bucket_us >= trips.started_us + ?)")
            args.append(int(minimum_trip_age_seconds * MICROSECONDS))
        if exclude_trip_id is not None:
            clauses.append("trip_key!=?")
            args.append(exclude_trip_id)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT bucket_us,trip_key,regime,sample_count,minimum,maximum,median,
                       first_us,last_us
                FROM metric_rollups WHERE {' AND '.join(clauses)} ORDER BY bucket_us
                """,
                args,
            ).fetchall()
        projected_regime = project_regime(regime, regime_dimensions)
        rows = [
            row
            for row in rows
            if project_regime(row["regime"], regime_dimensions) == projected_regime
        ]
        if not rows:
            return None
        inputs = [dict(row) for row in rows]
        input_digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            self._baseline_inputs[input_digest] = inputs
            while len(self._baseline_inputs) > 24:
                self._baseline_inputs.pop(next(iter(self._baseline_inputs)))
        values = [float(row["median"]) for row in rows]
        center, mad = _median_mad(values)
        return BaselineStats(
            metric=metric,
            regime=projected_regime,
            unit=unit,
            quality=quality,
            source=source,
            provenance=provenance,
            bucket_count=len(rows),
            trip_count=len({row["trip_key"] for row in rows if row["trip_key"] != 0}),
            sample_count=sum(row["sample_count"] for row in rows),
            median=center,
            mad=mad,
            minimum=min(float(row["minimum"]) for row in rows),
            maximum=max(float(row["maximum"]) for row in rows),
            first_at=_iso_from_us(min(row["first_us"] for row in rows)),
            last_at=_iso_from_us(max(row["last_us"] for row in rows)),
            input_digest=input_digest,
            input_buckets=tuple({"bucket_us": r["bucket_us"], "trip_key": r["trip_key"], "regime": r["regime"], "median": r["median"]} for r in rows[:128]),
            input_buckets_complete=len(rows) <= 128,
        )

    @staticmethod
    def _metric_series_bucket_seconds(
        *,
        window_seconds: int,
        bucket_seconds: int | None,
        max_points: int,
    ) -> int:
        if (
            not isinstance(window_seconds, int)
            or isinstance(window_seconds, bool)
            or not 1 <= window_seconds <= MAX_SERIES_WINDOW_SECONDS
        ):
            raise ValueError(
                f"window_seconds must be between 1 and {MAX_SERIES_WINDOW_SECONDS}"
            )
        if not isinstance(max_points, int) or isinstance(max_points, bool) or not 1 <= max_points <= MAX_SERIES_POINTS:
            raise ValueError(f"max_points must be between 1 and {MAX_SERIES_POINTS}")
        minimum_bucket = max(1, math.ceil(window_seconds / max_points))
        if bucket_seconds is not None and (
            not isinstance(bucket_seconds, int)
            or isinstance(bucket_seconds, bool)
            or bucket_seconds <= 0
        ):
            raise ValueError("bucket_seconds must be a positive integer")
        effective_bucket = max(minimum_bucket, bucket_seconds or minimum_bucket)
        if math.ceil(window_seconds / effective_bucket) > max_points:
            effective_bucket = minimum_bucket
        return effective_bucket

    def _append_metric_series_rollup_part_locked(
        self,
        parts: list[str],
        args: list[object],
        *,
        metric: str,
        start_us: int,
        end_us: int,
        cursor_us: int,
        regime: str | None,
        trip_id: int | None,
    ) -> None:
        rollup_end = min(end_us, cursor_us)
        if rollup_end > start_us:
            clauses = [
                "metric=?",
                "bucket_seconds=?",
                "bucket_us>=?",
                "bucket_us<?",
            ]
            rollup_args: list[object] = [
                metric,
                self.config.rollup_seconds,
                start_us,
                rollup_end,
            ]
            if regime is not None:
                clauses.append("regime=?")
                rollup_args.append(regime)
            if trip_id is not None:
                clauses.append("trip_key=?")
                rollup_args.append(trip_id)
            parts.append(
                        f"""
                        SELECT 'rollup' AS basis,bucket_us AS point_us,
                               sample_count,mean * sample_count AS weighted_sum,
                               minimum,maximum,first_us,last_us,
                               unit,quality,source,provenance
                        FROM metric_rollups
                        WHERE {' AND '.join(clauses)}
                        """
                    )
            args.extend(rollup_args)

    @staticmethod
    def _append_metric_series_raw_part_locked(
        parts: list[str],
        args: list[object],
        *,
        metric: str,
        start_us: int,
        end_us: int,
        cursor_us: int | None,
        use_rollups: bool,
        regime: str | None,
        trip_id: int | None,
    ) -> None:
        raw_start = (
            max(start_us, cursor_us)
            if use_rollups and cursor_us is not None
            else start_us
        )
        if raw_start <= end_us:
            clauses = [
                "sample.metric=?",
                "sample.captured_us>=?",
                "sample.captured_us<=?",
                "sample.freshness='fresh'",
                "sample.value_kind='number'",
                "sample.observed_us IS NOT NULL",
                """
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
                    """,
            ]
            raw_args: list[object] = [metric, raw_start, end_us]
            if regime is not None:
                clauses.append("sample.regime=?")
                raw_args.append(regime)
            if trip_id is not None:
                clauses.append("sample.trip_id=?")
                raw_args.append(trip_id)
            parts.append(
                    f"""
                    SELECT 'raw' AS basis,sample.captured_us AS point_us,
                           1 AS sample_count,sample.value_num AS weighted_sum,
                           sample.value_num AS minimum,
                           sample.value_num AS maximum,
                           sample.captured_us AS first_us,
                           sample.captured_us AS last_us,
                           sample.unit,sample.quality,sample.source,
                           sample.provenance
                    FROM metric_samples AS sample
                    WHERE {' AND '.join(clauses)}
                    """
                )
            args.extend(raw_args)

    @staticmethod
    def _metric_series_payload(
        rows: Sequence[sqlite3.Row],
        *,
        metric: str,
        start_us: int,
        end_dt: datetime,
        window_seconds: int,
        effective_bucket: int,
        width_us: int,
        max_points: int,
        rollup_status: dict[str, object],
    ) -> dict[str, object]:
        points = [
            {
                "at": _iso_from_us(start_us + int(row["bucket_index"]) * width_us),
                "value": row["mean"],
                "minimum": row["minimum"],
                "maximum": row["maximum"],
                "sample_count": row["sample_count"],
            }
            for row in rows
        ]
        units = sorted(
            {item for row in rows for item in (row["units"] or "").split(",") if item}
        )
        qualities = sorted(
            {item for row in rows for item in (row["qualities"] or "").split(",") if item}
        )
        sources = sorted(
            {item for row in rows for item in (row["sources"] or "").split(",") if item}
        )
        used_rollups = any(row["rollup_parts"] for row in rows)
        used_raw = any(row["raw_parts"] for row in rows)
        if used_rollups and used_raw:
            series_basis = "minute_rollups_plus_raw_tail"
        elif used_rollups:
            series_basis = "minute_rollups"
        else:
            series_basis = "independent_raw_observations"
        return {
            "metric": metric,
            "start_at": _iso_from_us(start_us),
            "end_at": _iso(end_dt),
            "window_seconds": window_seconds,
            "bucket_seconds": effective_bucket,
            "point_limit": max_points,
            "points": points,
            "units": units,
            "qualities": qualities,
            "sources": sources,
            "series_basis": series_basis,
            "rollup_backlog": rollup_status["backlog"],
            "mixed_provenance": (
                any(row["provenance_count"] > 1 for row in rows)
                or len(
                    {
                        value
                        for row in rows
                        for value in (row["provenance_min"], row["provenance_max"])
                    }
                )
                > 1
            ),
        }

    def metric_series(
        self,
        metric: str,
        *,
        end: datetime | str | None = None,
        window_seconds: int = 24 * 60 * 60,
        bucket_seconds: int | None = None,
        max_points: int = 288,
        regime: str | None = None,
        trip_id: int | None = None,
    ) -> dict[str, object]:
        """Return bounded downsampled points; never raw one-hertz rows."""

        effective_bucket = self._metric_series_bucket_seconds(
            window_seconds=window_seconds,
            bucket_seconds=bucket_seconds,
            max_points=max_points,
        )
        end_dt = datetime.now(timezone.utc) if end is None else _utc_datetime(end, "end")
        end_us = _to_us(end_dt)
        start_us = end_us - window_seconds * MICROSECONDS
        width_us = effective_bucket * MICROSECONDS
        rollup_status = self.refresh_rollups(through=end_dt)
        with self._lock:
            cursor_text = self._meta_locked(
                f"rollup_through_us:{self.config.rollup_seconds}"
            )
            cursor_us = int(cursor_text) if cursor_text is not None else None
            use_rollups = (
                cursor_us is not None
                and effective_bucket >= self.config.rollup_seconds
            )
            parts: list[str] = []
            args: list[object] = []
            if use_rollups:
                self._append_metric_series_rollup_part_locked(
                    parts,
                    args,
                    metric=metric,
                    start_us=start_us,
                    end_us=end_us,
                    cursor_us=cursor_us,
                    regime=regime,
                    trip_id=trip_id,
                )

            self._append_metric_series_raw_part_locked(
                parts,
                args,
                metric=metric,
                start_us=start_us,
                end_us=end_us,
                cursor_us=cursor_us,
                use_rollups=use_rollups,
                regime=regime,
                trip_id=trip_id,
            )
            rows = self._conn.execute(
                f"""
                WITH parts AS ({' UNION ALL '.join(parts)})
                SELECT ((point_us-?)/?) AS bucket_index,
                       sum(sample_count) AS sample_count,
                       min(minimum) AS minimum,max(maximum) AS maximum,
                       sum(weighted_sum) / sum(sample_count) AS mean,
                       min(first_us) AS first_us,max(last_us) AS last_us,
                       group_concat(DISTINCT unit) AS units,
                       group_concat(DISTINCT quality) AS qualities,
                       group_concat(DISTINCT source) AS sources,
                       count(DISTINCT provenance) AS provenance_count,
                       min(provenance) AS provenance_min,
                       max(provenance) AS provenance_max,
                       sum(CASE WHEN basis='rollup' THEN 1 ELSE 0 END)
                           AS rollup_parts,
                       sum(CASE WHEN basis='raw' THEN sample_count ELSE 0 END)
                           AS raw_parts
                FROM parts
                GROUP BY bucket_index ORDER BY bucket_index
                LIMIT ?
                """,
                [*args, start_us, width_us, max_points],
            ).fetchall()
        return self._metric_series_payload(
            rows,
            metric=metric,
            start_us=start_us,
            end_dt=end_dt,
            window_seconds=window_seconds,
            effective_bucket=effective_bucket,
            width_us=width_us,
            max_points=max_points,
            rollup_status=rollup_status,
        )

    def list_trips(self, *, limit: int = 20) -> list[dict[str, object]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trips ORDER BY started_us DESC LIMIT ?", (limit,)
            ).fetchall()
        now_us = _to_us(datetime.now(timezone.utc))
        return [self._trip_dict(row, now_us) for row in rows]

    @staticmethod
    def _trip_dict(row: sqlite3.Row, now_us: int) -> dict[str, object]:
        end_us = row["ended_us"] if row["ended_us"] is not None else now_us
        return {
            "id": row["id"],
            "state": "complete" if row["ended_us"] is not None else "open",
            "started_at": row["started_at"],
            "last_active_at": row["last_active_at"],
            "ended_at": row["ended_at"],
            "duration_seconds": max(0.0, (end_us - row["started_us"]) / MICROSECONDS),
            "start_basis": row["start_basis"],
            "end_reason": row["end_reason"],
            "snapshot_count": row["snapshot_count"],
        }

    def list_gaps(
        self,
        kind: str,
        *,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        if kind not in ("metric", "interface"):
            raise ValueError("kind must be 'metric' or 'interface'")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        table = f"{kind}_gaps"
        key = "metric" if kind == "metric" else "role"
        where = "WHERE ended_us IS NULL" if active_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM {table} {where} ORDER BY started_us DESC LIMIT ?",
                (limit,),
            ).fetchall()
        now_us = _to_us(datetime.now(timezone.utc))
        result = []
        for row in rows:
            end_us = row["ended_us"] if row["ended_us"] is not None else now_us
            payload = {
                "kind": kind,
                "key": row[key],
                "state": row["state"],
                "reason": row["reason"],
                "started_at": row["started_at"],
                "last_seen_at": row["last_seen_at"],
                "ended_at": row["ended_at"],
                "duration_seconds": max(0.0, (end_us - row["started_us"]) / MICROSECONDS),
                "observation_count": row["observation_count"],
            }
            if kind == "metric":
                payload["detail"] = row["detail"]
            result.append(payload)
        return result

    def _trip_metric_aggregate(self, trip_id: int, metric: str) -> dict[str, object] | None:
        state_key = f"rollup_through_us:{self.config.rollup_seconds}"
        cursor_text = self._meta_locked(state_key)
        cursor_us = int(cursor_text) if cursor_text is not None else None
        parts: list[str] = []
        args: list[object] = []
        if cursor_us is not None:
            parts.append(
                """
                SELECT 'rollup' AS basis,sample_count,
                       mean * sample_count AS weighted_sum,
                       minimum,maximum,first_us,last_us,
                       unit,quality,source,provenance
                FROM metric_rollups
                WHERE bucket_seconds=? AND trip_key=? AND metric=?
                """
            )
            args.extend((self.config.rollup_seconds, trip_id, metric))
        raw_cursor_clause = "AND sample.captured_us>=?" if cursor_us is not None else ""
        parts.append(
            f"""
            SELECT 'raw' AS basis,1 AS sample_count,
                   sample.value_num AS weighted_sum,
                   sample.value_num AS minimum,sample.value_num AS maximum,
                   sample.captured_us AS first_us,sample.captured_us AS last_us,
                   sample.unit,sample.quality,sample.source,sample.provenance
            FROM metric_samples AS sample
            WHERE sample.trip_id=? AND sample.metric=?
              AND sample.freshness='fresh' AND sample.value_kind='number'
              AND sample.observed_us IS NOT NULL
              {raw_cursor_clause}
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
            """
        )
        args.extend((trip_id, metric))
        if cursor_us is not None:
            args.append(cursor_us)
        row = self._conn.execute(
            f"""
            WITH parts AS ({' UNION ALL '.join(parts)})
            SELECT sum(sample_count) AS sample_count,
                   min(minimum) AS minimum,max(maximum) AS maximum,
                   sum(weighted_sum) / sum(sample_count) AS mean,
                   min(first_us) AS first_us,max(last_us) AS last_us,
                   group_concat(DISTINCT unit) AS units,
                   group_concat(DISTINCT quality) AS qualities,
                   group_concat(DISTINCT source) AS sources,
                   count(DISTINCT provenance) AS provenance_count,
                   sum(CASE WHEN basis='rollup' THEN 1 ELSE 0 END) AS rollup_parts,
                   sum(CASE WHEN basis='raw' THEN sample_count ELSE 0 END) AS raw_parts
            FROM parts
            """,
            args,
        ).fetchone()
        # SQLite aggregate queries return one row of NULLs for an empty input.
        # A newly registered history metric can therefore have no samples in
        # an otherwise completed prior trip; treat that as absent rather than
        # attempting timestamp arithmetic on NULL.
        if row is None or not row["sample_count"]:
            return None
        if row["rollup_parts"] and row["raw_parts"]:
            aggregate_basis = "minute_rollups_plus_raw_tail"
        elif row["rollup_parts"]:
            aggregate_basis = "minute_rollups"
        else:
            aggregate_basis = "independent_raw_observations"
        return {
            "trip_id": trip_id,
            "sample_count": row["sample_count"],
            "minimum": row["minimum"],
            "maximum": row["maximum"],
            "mean": row["mean"],
            "first_at": _iso_from_us(row["first_us"]),
            "last_at": _iso_from_us(row["last_us"]),
            "units": sorted((row["units"] or "").split(",")),
            "qualities": sorted((row["qualities"] or "").split(",")),
            "sources": sorted((row["sources"] or "").split(",")),
            "mixed_provenance": row["provenance_count"] > 1,
            "aggregate_basis": aggregate_basis,
            "rollup_backed": bool(row["rollup_parts"]),
        }

    def trip_comparison(
        self,
        metrics: Sequence[str],
        *,
        prior_trip_limit: int = 10,
    ) -> dict[str, object]:
        """Compare the open/current trip with bounded prior completed trips."""

        if not 1 <= prior_trip_limit <= 30:
            raise ValueError("prior_trip_limit must be between 1 and 30")
        metric_names = tuple(dict.fromkeys(metrics))
        if len(metric_names) > 50:
            raise ValueError("at most 50 metrics may be compared")
        with self._lock:
            current = self._conn.execute(
                "SELECT id FROM trips WHERE ended_us IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prior = self._conn.execute(
                "SELECT id FROM trips WHERE ended_us IS NOT NULL ORDER BY started_us DESC LIMIT ?",
                (prior_trip_limit,),
            ).fetchall()
            current_id = int(current["id"]) if current is not None else None
            result: dict[str, object] = {}
            for metric in metric_names:
                current_summary = (
                    self._trip_metric_aggregate(current_id, metric)
                    if current_id is not None
                    else None
                )
                prior_summaries = [
                    summary
                    for row in prior
                    if (summary := self._trip_metric_aggregate(int(row["id"]), metric))
                    is not None
                ]
                prior_means = [float(item["mean"]) for item in prior_summaries]
                if prior_means:
                    center, mad = _median_mad(prior_means)
                    prior_baseline: dict[str, object] | None = {
                        "trip_count": len(prior_means),
                        "median_of_trip_means": center,
                        "mad_of_trip_means": mad,
                        "minimum_trip_mean": min(prior_means),
                        "maximum_trip_mean": max(prior_means),
                    }
                else:
                    prior_baseline = None
                delta = None
                if current_summary is not None and prior_baseline is not None:
                    delta = current_summary["mean"] - prior_baseline["median_of_trip_means"]
                result[metric] = {
                    "current_trip": current_summary,
                    "prior_trips": prior_baseline,
                    "current_minus_prior_median": delta,
                }
        return {
            "current_trip_id": current_id,
            "prior_trip_limit": prior_trip_limit,
            "metrics": result,
        }

    def period_summary(
        self,
        days: int,
        *,
        metrics: Sequence[str] | None = None,
        end: datetime | str | None = None,
    ) -> dict[str, object]:
        """Return a compact fixed-window summary, currently bounded to 30 days."""

        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_PERIOD_DAYS:
            raise ValueError(f"days must be between 1 and {MAX_PERIOD_DAYS}")
        end_dt = datetime.now(timezone.utc) if end is None else _utc_datetime(end, "end")
        end_us = _to_us(end_dt)
        start_us = _to_us(end_dt - timedelta(days=days))
        rollup_status = self.refresh_rollups(through=end_dt)
        metric_names = tuple(dict.fromkeys(metrics or ()))
        if len(metric_names) > 50:
            raise ValueError("at most 50 metrics may be summarized")
        clauses = [
            "bucket_us>=?",
            "bucket_us<?",
        ]
        args: list[object] = [start_us, end_us]
        if metric_names:
            clauses.append("metric IN (%s)" % ",".join("?" for _ in metric_names))
            args.extend(metric_names)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT metric,sum(sample_count) AS sample_count,
                       min(minimum) AS minimum,max(maximum) AS maximum,
                       sum(mean * sample_count) / sum(sample_count) AS mean,
                       group_concat(DISTINCT unit) AS units,
                       group_concat(DISTINCT quality) AS qualities,
                       group_concat(DISTINCT source) AS sources,
                       count(DISTINCT provenance) AS provenance_count
                FROM metric_rollups WHERE {' AND '.join(clauses)}
                GROUP BY metric ORDER BY metric
                """,
                args,
            ).fetchall()
            trip_count = self._conn.execute(
                "SELECT count(*) FROM trips WHERE started_us<=? AND coalesce(ended_us,?)>=?",
                (end_us, end_us, start_us),
            ).fetchone()[0]
        return {
            "days": days,
            "start_at": _iso_from_us(start_us),
            "end_at": _iso(end_dt),
            "trip_count": trip_count,
            "complete_buckets_only": True,
            "rollup_backlog": rollup_status["backlog"],
            "metrics": {
                row["metric"]: {
                    "sample_count": row["sample_count"],
                    "minimum": row["minimum"],
                    "maximum": row["maximum"],
                    "mean": row["mean"],
                    "units": sorted((row["units"] or "").split(",")),
                    "qualities": sorted((row["qualities"] or "").split(",")),
                    "sources": sorted((row["sources"] or "").split(",")),
                    "mixed_provenance": row["provenance_count"] > 1,
                }
                for row in rows
            },
        }

    def dashboard_summary(
        self,
        *,
        now: datetime | str | None = None,
        metrics: Sequence[str] = (),
        recent_trip_limit: int = 5,
    ) -> dict[str, object]:
        """Return compact coverage, trip comparison, and 7/30-day summaries.

        No raw series are embedded.  A UI can fetch :meth:`metric_series` on a
        slower cadence for whichever sparklines are visible.
        """

        now_dt = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        now_us = _to_us(now_dt)
        with self._lock:
            latest = self._conn.execute(
                "SELECT captured_us,captured_at FROM snapshots ORDER BY captured_us DESC LIMIT 1"
            ).fetchone()
        age = None if latest is None else max(0.0, (now_us - latest["captured_us"]) / MICROSECONDS)
        if latest is None:
            coverage_status = "no_history"
        elif age > max(2 * self.config.rollup_seconds, 120):
            coverage_status = "stale"
        elif self._active_gap_count_threadsafe("interface_gaps"):
            coverage_status = "degraded"
        else:
            coverage_status = "current"
        trips = self.list_trips(limit=recent_trip_limit)
        current = next((trip for trip in trips if trip["state"] == "open"), None)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(now_dt),
            "coverage": {
                "status": coverage_status,
                "last_snapshot_at": latest["captured_at"] if latest is not None else None,
                "age_seconds": age,
                "active_metric_gaps": self.list_gaps("metric", active_only=True, limit=100),
                "active_interface_gaps": self.list_gaps(
                    "interface", active_only=True, limit=100
                ),
                "retention": self.maintenance_status(now=now_dt),
            },
            "current_trip": current,
            "recent_trips": [trip for trip in trips if trip["state"] == "complete"],
            "trip_comparison": self.trip_comparison(metrics),
            "windows": {
                "7d": self.period_summary(7, metrics=metrics, end=now_dt),
                "30d": self.period_summary(30, metrics=metrics, end=now_dt),
            },
        }

    @staticmethod
    def _data_quality_event_dict(
        row: sqlite3.Row,
        now_us: int,
    ) -> dict[str, object]:
        end_us = row["resolved_us"] if row["resolved_us"] is not None else now_us
        return {
            "incident_id": row["incident_id"],
            "producer_instance": row["producer_instance"],
            "metric": row["metric"],
            "source": row["source"],
            "bus": row["bus"],
            "quality": row["quality"],
            "reason": row["reason"],
            "status": row["status"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "resolved_at": row["resolved_at"],
            "resolution_reason": row["resolution_reason"],
            "duration_seconds": max(
                0.0, (end_us - row["first_seen_us"]) / MICROSECONDS
            ),
            "rejection_count": row["rejection_count"],
            "detail": row["detail"],
            "interface_mode": row["interface_mode"],
            "evidence": json.loads(row["evidence_json"]),
            "notification_eligible": False,
        }

    def list_data_quality_events(
        self,
        *,
        active_only: bool = False,
        limit: int = 25,
        now: datetime | str | None = None,
    ) -> list[dict[str, object]]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ValueError("data-quality limit must be between 1 and 100")
        moment = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        where = "WHERE status='active'" if active_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM data_quality_events {where} "
                "ORDER BY last_seen_us DESC,incident_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        now_us = _to_us(moment)
        return [self._data_quality_event_dict(row, now_us) for row in rows]

    def data_quality_summary(
        self,
        *,
        now: datetime | str | None = None,
        recent_limit: int = 25,
    ) -> dict[str, object]:
        moment = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        with self._lock:
            counts = {
                row["status"]: int(row["count"])
                for row in self._conn.execute(
                    "SELECT status,count(*) AS count FROM data_quality_events "
                    "GROUP BY status"
                )
            }
        return {
            "schema_version": DATA_QUALITY_SCHEMA_VERSION,
            "generated_at": _iso(moment),
            "active": self.list_data_quality_events(
                active_only=True,
                limit=recent_limit,
                now=moment,
            ),
            "recent": self.list_data_quality_events(
                active_only=False,
                limit=recent_limit,
                now=moment,
            ),
            "counts": {
                "active": counts.get("active", 0),
                "resolved": counts.get("resolved", 0),
            },
            "notification_delivery": "disabled_by_design",
            "detail": (
                "Rejected raw samples are retained as acquisition-quality "
                "evidence and never enter the advisory notification outbox."
            ),
        }

    def _active_gap_count_threadsafe(self, table: str) -> int:
        with self._lock:
            return self._active_gap_count(table)
