"""Historian configuration, result records, constants and errors."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


SCHEMA_VERSION = 1


ADVISORY_SCHEMA_VERSION = 2


DATA_QUALITY_SCHEMA_VERSION = 1


DEFAULT_DATABASE = Path("/var/lib/van-telemetry/history.sqlite3")


MAX_QUERY_SAMPLES = 2_000


MAX_SERIES_POINTS = 512


MAX_SERIES_WINDOW_SECONDS = 31 * 24 * 60 * 60


MAX_PERIOD_DAYS = 30


MICROSECONDS = 1_000_000


REGIME_DIMENSIONS = ("engine", "motion", "rpm", "thermal")


ADVISORY_ACTIVE_STATES = frozenset(("watch", "warning"))


ADVISORY_RESOLVING_STATES = frozenset(("normal", "suppressed"))


ADVISORY_INCONCLUSIVE_STATES = frozenset(
    ("unavailable", "insufficient_history", "rejected", "not_applicable", "recovering")
)


# Tiered notification policy (warnings-redesign.md section 6).  Cooldowns are
# per group (dedupe key), never per rule; the 8-day rate-limit bound admits the
# 7-day tier-2 cooldown.
MAX_NOTIFICATION_RATE_LIMIT_SECONDS = 8 * 24 * 60 * 60


TRIP_NONCRITICAL_PUSH_CAP = 3


# Categories shown only as dashboard System notes; never notified (owner
# decision 2026-09-24), whatever tier or eligibility an assessment carries.
SYSTEM_NOTE_CATEGORIES = frozenset(
    ("can_infrastructure", "telemetry_quality", "data_quality", "system")
)


# Urgency rank of a tiered outbox row.  A group cooldown only counts earlier
# pushes of the same or higher rank, so an escalation (notice -> tier-1 ->
# tier-0 warning -> critical) in one group is never swallowed or delayed by a
# lower-urgency push.  Keep in step with ``_notification_rank``.
NOTIFICATION_RANK_SQL = """
    CASE
        WHEN json_extract(payload_json,'$.severity')='critical' THEN 4
        WHEN json_extract(payload_json,'$.tier')=0 THEN 3
        WHEN json_extract(payload_json,'$.tier')=2 THEN 1
        ELSE 2
    END
"""


class HistorianError(RuntimeError):
    """Base class for historian failures."""


class SnapshotValidationError(HistorianError, ValueError):
    """A snapshot cannot be stored without losing or inventing provenance."""


class OutOfOrderSnapshotError(HistorianError, ValueError):
    """A new snapshot predates already segmented history."""


@dataclass(frozen=True)
class HistorianConfig:
    """Deterministic segmentation and aggregation settings."""

    trip_idle_timeout_seconds: float = 300.0
    running_rpm_threshold: float = 400.0
    moving_speed_threshold_mph: float = 1.0
    vehicle_state_max_age_seconds: float = 5.0
    rollup_seconds: int = 60
    rollup_max_buckets_per_call: int = 1_440
    raw_retention_days: int = 7
    maintenance_interval_seconds: int = 24 * 60 * 60
    maintenance_max_rollup_passes: int = 32
    maintenance_max_delete_rows_per_table: int = 2_000_000

    def __post_init__(self) -> None:
        numeric_positive = (
            ("trip_idle_timeout_seconds", self.trip_idle_timeout_seconds),
            ("running_rpm_threshold", self.running_rpm_threshold),
            ("moving_speed_threshold_mph", self.moving_speed_threshold_mph),
            ("vehicle_state_max_age_seconds", self.vehicle_state_max_age_seconds),
            ("rollup_seconds", self.rollup_seconds),
            ("rollup_max_buckets_per_call", self.rollup_max_buckets_per_call),
            ("raw_retention_days", self.raw_retention_days),
            ("maintenance_interval_seconds", self.maintenance_interval_seconds),
            ("maintenance_max_rollup_passes", self.maintenance_max_rollup_passes),
            (
                "maintenance_max_delete_rows_per_table",
                self.maintenance_max_delete_rows_per_table,
            ),
        )
        for name, value in numeric_positive:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        integer_fields = (
            ("rollup_seconds", self.rollup_seconds),
            ("rollup_max_buckets_per_call", self.rollup_max_buckets_per_call),
            ("raw_retention_days", self.raw_retention_days),
            ("maintenance_interval_seconds", self.maintenance_interval_seconds),
            ("maintenance_max_rollup_passes", self.maintenance_max_rollup_passes),
            (
                "maintenance_max_delete_rows_per_table",
                self.maintenance_max_delete_rows_per_table,
            ),
        )
        for name, value in integer_fields:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")


@dataclass(frozen=True)
class IngestResult:
    snapshot_id: int
    captured_at: str
    duplicate: bool
    trip_id: int | None
    regime: str
    stored_samples: int
    metric_gap_count: int
    interface_gap_count: int
    advisory_checkpoint_complete: bool | None = None
    advisory_consumed_event_ids: tuple[str, ...] = ()
    advisory_checkpoint_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MaintenanceResult:
    """One bounded rollup-first raw-retention attempt."""

    status: str
    attempted_at: str
    retention_cutoff_at: str
    delete_before_at: str
    rollup_passes: int
    rollup_buckets: int
    rollup_rows: int
    deleted_metric_samples: int
    deleted_interface_samples: int
    deleted_snapshots: int
    raw_backlog: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AdvisoryPersistenceResult:
    """Outcome of one atomic advisory lifecycle update."""

    evaluated_at: str
    opened: int
    updated: int
    resolved: int
    inconclusive: int
    notifications_enqueued: int
    # Tiered delivery policy counters (defaults keep older consumers working):
    # evaluations in which the per-trip cap blocked a due push, and rows
    # enqueued with a quiet-hours deferral.
    notifications_capped: int = 0
    notifications_deferred: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BaselineStats:
    metric: str
    regime: str
    unit: str
    quality: str
    source: str
    provenance: str
    bucket_count: int
    trip_count: int
    sample_count: int
    median: float
    mad: float
    minimum: float
    maximum: float
    first_at: str
    last_at: str
    input_digest: str = ""
    input_buckets: tuple = ()
    input_buckets_complete: bool = False

    @property
    def robust_sigma(self) -> float:
        return 1.4826 * self.mad

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "robust_sigma": self.robust_sigma}


@dataclass(frozen=True)
class _SourceDefinition:
    name: str
    bus: str
    quality: str
    provenance: str


@dataclass(frozen=True)
class _MetricDefinition:
    name: str
    unit: str
    value_type: str
    stale_after_ms: int
    sources: Mapping[str, _SourceDefinition]


@dataclass(frozen=True)
class _MetricSample:
    metric: str
    value_kind: str
    value_num: float | None
    value_text: str | None
    value_bool: int | None
    unit: str
    source: str
    bus: str
    acquisition: str | None
    interface_mode: str | None
    quality: str
    provenance: str
    observed_us: int | None
    observed_at: str | None
    source_age_ms: int | None
    reported_stale: int | None
    freshness: str

    @property
    def scalar(self) -> bool | float | str:
        if self.value_kind == "boolean":
            return bool(self.value_bool)
        if self.value_kind == "number":
            assert self.value_num is not None
            return self.value_num
        assert self.value_text is not None
        return self.value_text
