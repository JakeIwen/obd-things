"""Offline SQLite historian for curated vehicle telemetry snapshots.

This module has no SocketCAN, UDS, service-control, or network dependencies.  It
accepts the cache-only shape returned by ``TelemetryBroker.snapshot_response``
and preserves the evidence fields which make a value interpretable: source,
bus, quality, provenance, observation time, age, and freshness.

The raw table is deliberately limited to one row per available metric per
ingested snapshot.  Missing data is represented as compact gap intervals rather
than fabricated zeroes.  Completed minute buckets provide bounded trend and
baseline queries without returning raw one-hertz history to the web tier.
Rollups count source observation timestamps, not repeated snapshots of one
broker-cached value.  Raw rows are retained for a bounded configurable window;
rollup-first maintenance preserves compact history before pruning them.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence


SCHEMA_VERSION = 1
ADVISORY_SCHEMA_VERSION = 1
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
    ("unavailable", "insufficient_history", "rejected")
)


class HistorianError(RuntimeError):
    """Base class for historian failures."""


class SnapshotValidationError(HistorianError, ValueError):
    """A snapshot cannot be stored without losing or inventing provenance."""


class OutOfOrderSnapshotError(HistorianError, ValueError):
    """A new snapshot predates already segmented history."""


def _is_placeholder_interface_role(value: object) -> bool:
    """Return whether a stored role is really an ephemeral/placeholder key."""

    if not isinstance(value, str):
        return False
    return value in ("default", "unknown") or (
        value.startswith("can") and value[3:].isdigit()
    )


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


def _utc_datetime(value: datetime | str, field: str) -> datetime:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SnapshotValidationError(f"{field} is not an ISO-8601 timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SnapshotValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _to_us(value: datetime) -> int:
    return int(round(value.timestamp() * MICROSECONDS))


def _iso_from_us(value: int) -> str:
    return datetime.fromtimestamp(value / MICROSECONDS, timezone.utc).isoformat()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not _finite_number(value) or float(value) < 0:
        raise SnapshotValidationError(f"{field} must be finite and nonnegative")
    return int(round(float(value)))


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotValidationError(f"{field} must be a nonempty string")
    return value


def _bool_db(value: object) -> int | None:
    return int(value) if type(value) is bool else None


def _median_mad(values: Sequence[float]) -> tuple[float, float]:
    center = float(statistics.median(values))
    mad = float(statistics.median(abs(value - center) for value in values))
    return center, mad


def project_regime(
    regime: str,
    dimensions: Sequence[str] = REGIME_DIMENSIONS,
) -> str:
    """Return an explicit, rule-specific projection of one stored regime."""

    parts = regime.split(":")
    if len(parts) != len(REGIME_DIMENSIONS):
        raise ValueError(f"invalid stored regime {regime!r}")
    selected = tuple(dimensions)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("regime dimensions must be nonempty and unique")
    unknown = set(selected) - set(REGIME_DIMENSIONS)
    if unknown:
        raise ValueError(f"unknown regime dimensions: {', '.join(sorted(unknown))}")
    by_name = dict(zip(REGIME_DIMENSIONS, parts))
    return "|".join(f"{name}={by_name[name]}" for name in selected)


class TelemetryHistorian:
    """Transactional, offline telemetry historian.

    A single instance may be shared by threads.  Snapshot time must be
    monotonic across successful ingests because trip and gap intervals are
    stateful.  Replaying the same delivery key is idempotent.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        config: HistorianConfig | None = None,
    ):
        self.database = str(database)
        self.config = config or HistorianConfig()
        if self.database != ":memory:":
            Path(self.database).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.database,
            timeout=10.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        if self.database != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "TelemetryHistorian":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS historian_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY,
            started_us INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_active_us INTEGER NOT NULL,
            last_active_at TEXT NOT NULL,
            ended_us INTEGER,
            ended_at TEXT,
            start_basis TEXT NOT NULL,
            end_reason TEXT,
            snapshot_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_trip
            ON trips((1)) WHERE ended_us IS NULL;

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY,
            ingest_key TEXT NOT NULL UNIQUE,
            captured_us INTEGER NOT NULL UNIQUE,
            captured_at TEXT NOT NULL,
            source_instance TEXT,
            source_sequence INTEGER,
            vehicle_state TEXT,
            vehicle_running INTEGER,
            vehicle_confidence TEXT,
            vehicle_basis TEXT,
            vehicle_observed_at TEXT,
            vehicle_age_ms INTEGER,
            regime TEXT NOT NULL,
            trip_id INTEGER REFERENCES trips(id)
        );
        CREATE INDEX IF NOT EXISTS snapshots_trip_time
            ON snapshots(trip_id, captured_us);

        CREATE TABLE IF NOT EXISTS catalog_sources (
            fingerprint TEXT PRIMARY KEY,
            metric TEXT NOT NULL,
            unit TEXT NOT NULL,
            value_type TEXT NOT NULL,
            stale_after_ms INTEGER NOT NULL,
            source TEXT NOT NULL,
            bus TEXT NOT NULL,
            quality TEXT NOT NULL,
            provenance TEXT NOT NULL,
            first_seen_us INTEGER NOT NULL,
            last_seen_us INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS catalog_metric_source
            ON catalog_sources(metric, source);

        CREATE TABLE IF NOT EXISTS metric_samples (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            trip_id INTEGER REFERENCES trips(id),
            captured_us INTEGER NOT NULL,
            metric TEXT NOT NULL,
            value_kind TEXT NOT NULL CHECK(value_kind IN ('number','boolean','string')),
            value_num REAL,
            value_text TEXT,
            value_bool INTEGER CHECK(value_bool IN (0,1) OR value_bool IS NULL),
            unit TEXT NOT NULL,
            source TEXT NOT NULL,
            bus TEXT NOT NULL,
            acquisition TEXT,
            interface_mode TEXT,
            quality TEXT NOT NULL,
            provenance TEXT NOT NULL,
            observed_us INTEGER,
            observed_at TEXT,
            source_age_ms INTEGER,
            reported_stale INTEGER CHECK(reported_stale IN (0,1) OR reported_stale IS NULL),
            freshness TEXT NOT NULL CHECK(freshness IN ('fresh','stale','undated')),
            regime TEXT NOT NULL,
            UNIQUE(snapshot_id, metric)
        );
        CREATE INDEX IF NOT EXISTS metric_samples_lookup
            ON metric_samples(metric, captured_us);
        CREATE INDEX IF NOT EXISTS metric_samples_baseline
            ON metric_samples(metric, regime, freshness, captured_us);
        CREATE INDEX IF NOT EXISTS metric_samples_trip
            ON metric_samples(trip_id, metric, captured_us);
        CREATE INDEX IF NOT EXISTS metric_samples_time
            ON metric_samples(captured_us);
        CREATE INDEX IF NOT EXISTS metric_samples_observation
            ON metric_samples(metric, source, observed_us, captured_us);

        CREATE TABLE IF NOT EXISTS metric_gaps (
            id INTEGER PRIMARY KEY,
            metric TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('missing','stale','undated')),
            reason TEXT NOT NULL,
            detail TEXT NOT NULL,
            started_us INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_seen_us INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            ended_us INTEGER,
            ended_at TEXT,
            first_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            last_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            observation_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_metric_gap
            ON metric_gaps(metric) WHERE ended_us IS NULL;
        CREATE INDEX IF NOT EXISTS metric_gaps_time
            ON metric_gaps(started_us, ended_us);

        CREATE TABLE IF NOT EXISTS interface_samples (
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            captured_us INTEGER NOT NULL,
            channel TEXT,
            usb_serial TEXT,
            bus TEXT,
            adapter_present INTEGER,
            up INTEGER,
            bitrate INTEGER,
            listen_only INTEGER,
            controller_state TEXT,
            topology_usable INTEGER,
            health TEXT NOT NULL CHECK(health IN ('healthy','unhealthy','unknown')),
            reason TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, role)
        );
        CREATE INDEX IF NOT EXISTS interface_samples_lookup
            ON interface_samples(role, captured_us);
        CREATE INDEX IF NOT EXISTS interface_samples_time
            ON interface_samples(captured_us);

        CREATE TABLE IF NOT EXISTS interface_gaps (
            id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('unhealthy','unknown','missing')),
            reason TEXT NOT NULL,
            started_us INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_seen_us INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            ended_us INTEGER,
            ended_at TEXT,
            first_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            last_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            observation_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_interface_gap
            ON interface_gaps(role) WHERE ended_us IS NULL;
        CREATE INDEX IF NOT EXISTS interface_gaps_time
            ON interface_gaps(started_us, ended_us);

        CREATE TABLE IF NOT EXISTS system_health_samples (
            snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
            captured_us INTEGER NOT NULL,
            topology_generation TEXT,
            issues_json TEXT NOT NULL,
            active_inhibits_json TEXT NOT NULL,
            restoration_failed INTEGER CHECK(restoration_failed IN (0,1) OR restoration_failed IS NULL)
        );
        CREATE INDEX IF NOT EXISTS system_health_samples_time
            ON system_health_samples(captured_us);

        CREATE TABLE IF NOT EXISTS interface_role_details (
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            resolution TEXT,
            role_reason TEXT,
            detail TEXT,
            usb_dev_id INTEGER,
            topology_generation TEXT,
            PRIMARY KEY(snapshot_id, role)
        );

        CREATE TABLE IF NOT EXISTS usb_can_monitor_samples (
            snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
            captured_us INTEGER NOT NULL,
            source TEXT NOT NULL,
            producer_instance TEXT NOT NULL,
            dropped_event_count INTEGER NOT NULL CHECK(dropped_event_count >= 0),
            pending_event_count INTEGER NOT NULL CHECK(pending_event_count >= 0)
        );
        CREATE INDEX IF NOT EXISTS usb_can_monitor_samples_time
            ON usb_can_monitor_samples(captured_us);

        CREATE TABLE IF NOT EXISTS usb_can_events (
            event_id TEXT PRIMARY KEY,
            occurred_us INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            kernel_seqnum TEXT,
            kind TEXT NOT NULL,
            action TEXT NOT NULL,
            scope TEXT NOT NULL,
            devpath TEXT NOT NULL,
            usb_vid TEXT,
            usb_pid TEXT,
            usb_serial TEXT,
            affected_serials_json TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            first_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
            last_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS usb_can_events_time
            ON usb_can_events(occurred_us DESC);
        CREATE INDEX IF NOT EXISTS usb_can_events_kind_time
            ON usb_can_events(kind, occurred_us DESC);

        CREATE TABLE IF NOT EXISTS usb_can_advisory_consumption (
            event_id TEXT PRIMARY KEY REFERENCES usb_can_events(event_id)
                ON DELETE CASCADE,
            consumed_us INTEGER NOT NULL,
            consumed_at TEXT NOT NULL,
            snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS usb_can_advisory_consumption_snapshot
            ON usb_can_advisory_consumption(snapshot_id);

        CREATE TABLE IF NOT EXISTS usb_can_incidents (
            incident_id TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK(state IN ('active','resolved')),
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            opened_us INTEGER NOT NULL,
            opened_at TEXT NOT NULL,
            last_seen_us INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_us INTEGER,
            resolved_at TEXT,
            resolution TEXT,
            affected_serials_json TEXT NOT NULL,
            event_count INTEGER NOT NULL CHECK(event_count > 0),
            reappearance_count INTEGER NOT NULL CHECK(reappearance_count >= 0),
            opened_event_id TEXT NOT NULL,
            last_event_id TEXT NOT NULL,
            resolved_event_id TEXT,
            source TEXT NOT NULL,
            producer_instance TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            first_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
            last_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS usb_can_incidents_state_time
            ON usb_can_incidents(state, last_seen_us DESC);

        CREATE TABLE IF NOT EXISTS data_quality_events (
            incident_id TEXT PRIMARY KEY,
            producer_instance TEXT NOT NULL,
            metric TEXT NOT NULL,
            source TEXT NOT NULL,
            bus TEXT NOT NULL,
            quality TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','resolved')),
            first_seen_us INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_us INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_us INTEGER,
            resolved_at TEXT,
            resolution_reason TEXT,
            rejection_count INTEGER NOT NULL CHECK(rejection_count > 0),
            detail TEXT NOT NULL,
            interface_mode TEXT NOT NULL CHECK(
                interface_mode IN ('listen_only','armed_diagnostic')
            ),
            evidence_json TEXT NOT NULL,
            first_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
            last_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
            notification_eligible INTEGER NOT NULL DEFAULT 0 CHECK(
                notification_eligible = 0
            )
        );
        CREATE INDEX IF NOT EXISTS data_quality_events_recent
            ON data_quality_events(last_seen_us DESC);
        CREATE INDEX IF NOT EXISTS data_quality_events_active
            ON data_quality_events(status,last_seen_us DESC);

        CREATE TABLE IF NOT EXISTS advisory_episodes (
            id INTEGER PRIMARY KEY,
            rule_key TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            advisory INTEGER NOT NULL CHECK(advisory IN (0,1)),
            status TEXT NOT NULL CHECK(status IN ('open','resolved')),
            current_state TEXT NOT NULL CHECK(
                current_state IN ('watch','warning','normal','suppressed')
            ),
            evidence_state TEXT NOT NULL,
            opened_us INTEGER NOT NULL,
            opened_at TEXT NOT NULL,
            last_evaluated_us INTEGER NOT NULL,
            last_evaluated_at TEXT NOT NULL,
            last_observed_us INTEGER NOT NULL,
            last_observed_at TEXT NOT NULL,
            resolved_us INTEGER,
            resolved_at TEXT,
            resolution_reason TEXT,
            observation_count INTEGER NOT NULL DEFAULT 1,
            update_count INTEGER NOT NULL DEFAULT 0,
            transition_count INTEGER NOT NULL DEFAULT 0,
            acknowledged_us INTEGER,
            acknowledged_at TEXT,
            acknowledgment_note TEXT,
            first_assessment_json TEXT NOT NULL,
            latest_assessment_json TEXT NOT NULL,
            latest_context_fingerprint TEXT NOT NULL,
            last_notification_enqueued_us INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_advisory_episode
            ON advisory_episodes(rule_key) WHERE status='open';
        CREATE INDEX IF NOT EXISTS advisory_episodes_recent
            ON advisory_episodes(opened_us DESC);

        CREATE TABLE IF NOT EXISTS advisory_episode_events (
            id INTEGER PRIMARY KEY,
            episode_id INTEGER NOT NULL REFERENCES advisory_episodes(id) ON DELETE CASCADE,
            event_us INTEGER NOT NULL,
            event_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            previous_state TEXT,
            new_state TEXT,
            context_fingerprint TEXT NOT NULL,
            assessment_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS advisory_episode_events_lookup
            ON advisory_episode_events(episode_id, event_us);

        CREATE TABLE IF NOT EXISTS advisory_notification_outbox (
            id INTEGER PRIMARY KEY,
            episode_id INTEGER NOT NULL REFERENCES advisory_episodes(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES advisory_episode_events(id) ON DELETE CASCADE,
            rule_key TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE,
            created_us INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            eligible_after_us INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','delivered','failed','cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_us INTEGER,
            last_attempt_at TEXT,
            delivered_us INTEGER,
            delivered_at TEXT,
            last_error TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS advisory_notification_outbox_pending
            ON advisory_notification_outbox(status, eligible_after_us, created_us);

        CREATE TABLE IF NOT EXISTS metric_rollups (
            bucket_us INTEGER NOT NULL,
            bucket_seconds INTEGER NOT NULL,
            trip_key INTEGER NOT NULL,
            metric TEXT NOT NULL,
            regime TEXT NOT NULL,
            unit TEXT NOT NULL,
            source TEXT NOT NULL,
            quality TEXT NOT NULL,
            provenance TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            minimum REAL NOT NULL,
            maximum REAL NOT NULL,
            mean REAL NOT NULL,
            median REAL NOT NULL,
            mad REAL NOT NULL,
            first_us INTEGER NOT NULL,
            last_us INTEGER NOT NULL,
            PRIMARY KEY(
                bucket_us, bucket_seconds, trip_key, metric, regime,
                unit, source, quality, provenance
            )
        );
        CREATE INDEX IF NOT EXISTS metric_rollups_baseline
            ON metric_rollups(metric, regime, bucket_us);
        """
        with self._lock, self._conn:
            self._conn.executescript(schema)
            existing = self._conn.execute(
                "SELECT value FROM historian_meta WHERE key='schema_version'"
            ).fetchone()
            if existing is not None and int(existing[0]) != SCHEMA_VERSION:
                raise HistorianError(
                    f"database schema {existing[0]} is not supported by schema {SCHEMA_VERSION}"
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO historian_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO historian_meta(key,value) "
                "VALUES('advisory_schema_version',?)",
                (str(ADVISORY_SCHEMA_VERSION),),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO historian_meta(key,value) "
                "VALUES('data_quality_schema_version',?)",
                (str(DATA_QUALITY_SCHEMA_VERSION),),
            )
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _parse_catalog(snapshot: Mapping[str, object]) -> dict[str, _MetricDefinition]:
        payload = snapshot.get("catalog")
        if not isinstance(payload, list):
            raise SnapshotValidationError("snapshot.catalog must be a list")
        definitions: dict[str, _MetricDefinition] = {}
        for index, item in enumerate(payload):
            if not isinstance(item, Mapping):
                raise SnapshotValidationError(f"catalog[{index}] must be an object")
            name = _required_text(item.get("name"), f"catalog[{index}].name")
            if name in definitions:
                raise SnapshotValidationError(f"catalog repeats metric {name!r}")
            unit = _required_text(item.get("unit"), f"catalog[{index}].unit")
            value_type = _required_text(
                item.get("value_type"), f"catalog[{index}].value_type"
            )
            if value_type not in ("number", "integer", "boolean", "string"):
                raise SnapshotValidationError(
                    f"catalog metric {name!r} has unsupported value_type {value_type!r}"
                )
            stale_seconds = item.get("stale_after_seconds")
            if not _finite_number(stale_seconds) or float(stale_seconds) < 0:
                raise SnapshotValidationError(
                    f"catalog metric {name!r} has invalid stale_after_seconds"
                )
            source_items = item.get("sources")
            if not isinstance(source_items, list) or not source_items:
                raise SnapshotValidationError(f"catalog metric {name!r} has no sources")
            sources: dict[str, _SourceDefinition] = {}
            for source_index, source_item in enumerate(source_items):
                if not isinstance(source_item, Mapping):
                    raise SnapshotValidationError(
                        f"catalog source {name}[{source_index}] must be an object"
                    )
                source_name = _required_text(
                    source_item.get("name"), f"catalog source {name}.name"
                )
                if source_name in sources:
                    raise SnapshotValidationError(
                        f"catalog metric {name!r} repeats source {source_name!r}"
                    )
                sources[source_name] = _SourceDefinition(
                    name=source_name,
                    bus=_required_text(
                        source_item.get("bus"), f"catalog source {name}.bus"
                    ),
                    quality=_required_text(
                        source_item.get("quality"), f"catalog source {name}.quality"
                    ),
                    provenance=_required_text(
                        source_item.get("provenance"),
                        f"catalog source {name}.provenance",
                    ),
                )
            definitions[name] = _MetricDefinition(
                name=name,
                unit=unit,
                value_type=value_type,
                stale_after_ms=int(round(float(stale_seconds) * 1000)),
                sources=sources,
            )
        return definitions

    @staticmethod
    def _parse_metric(
        name: str,
        payload: Mapping[str, object],
        definition: _MetricDefinition,
    ) -> _MetricSample | tuple[str, str, str]:
        available = payload.get("available")
        if type(available) is not bool:
            raise SnapshotValidationError(f"metric {name!r} available must be boolean")
        if not available:
            reason = payload.get("reason", "source_unavailable")
            detail = payload.get("detail", "")
            if not isinstance(reason, str) or not reason:
                raise SnapshotValidationError(f"metric {name!r} reason must be text")
            if not isinstance(detail, str):
                raise SnapshotValidationError(f"metric {name!r} detail must be text")
            return ("missing", reason, detail)

        unit = _required_text(payload.get("unit"), f"metric {name}.unit")
        if unit != definition.unit:
            raise SnapshotValidationError(
                f"metric {name!r} unit {unit!r} does not match catalog {definition.unit!r}"
            )
        source_name = _required_text(payload.get("source"), f"metric {name}.source")
        source = definition.sources.get(source_name)
        if source is None:
            raise SnapshotValidationError(
                f"metric {name!r} source {source_name!r} is absent from its catalog"
            )
        bus = _required_text(payload.get("bus"), f"metric {name}.bus")
        quality = _required_text(payload.get("quality"), f"metric {name}.quality")
        if bus != source.bus or quality != source.quality:
            raise SnapshotValidationError(
                f"metric {name!r} bus/quality does not match catalog source {source_name!r}"
            )

        value = payload.get("value")
        if definition.value_type == "boolean":
            if type(value) is not bool:
                raise SnapshotValidationError(f"metric {name!r} value must be boolean")
            value_kind, value_num, value_text, value_bool = (
                "boolean",
                None,
                None,
                int(value),
            )
        elif definition.value_type in ("number", "integer"):
            valid = _finite_number(value)
            if definition.value_type == "integer":
                valid = isinstance(value, int) and not isinstance(value, bool)
            if not valid:
                raise SnapshotValidationError(
                    f"metric {name!r} value must be a finite {definition.value_type}"
                )
            value_kind, value_num, value_text, value_bool = (
                "number",
                float(value),
                None,
                None,
            )
        else:
            if not isinstance(value, str):
                raise SnapshotValidationError(f"metric {name!r} value must be text")
            value_kind, value_num, value_text, value_bool = (
                "string",
                None,
                value,
                None,
            )

        observed = payload.get("observed_at")
        if observed is None:
            observed_at = None
            observed_us = None
        else:
            observed_dt = _utc_datetime(observed, f"metric {name}.observed_at")
            observed_at = _iso(observed_dt)
            observed_us = _to_us(observed_dt)
        age_ms = _optional_nonnegative_int(payload.get("age_ms"), f"metric {name}.age_ms")
        reported_stale = payload.get("stale")
        if reported_stale is not None and type(reported_stale) is not bool:
            raise SnapshotValidationError(f"metric {name!r} stale must be boolean or null")
        if observed_us is None or age_ms is None:
            freshness = "undated"
        elif reported_stale is True or age_ms > definition.stale_after_ms:
            freshness = "stale"
        else:
            freshness = "fresh"
        acquisition = payload.get("acquisition")
        interface_mode = payload.get("interface_mode")
        if acquisition is not None and not isinstance(acquisition, str):
            raise SnapshotValidationError(f"metric {name!r} acquisition must be text or null")
        if interface_mode is not None and not isinstance(interface_mode, str):
            raise SnapshotValidationError(
                f"metric {name!r} interface_mode must be text or null"
            )
        return _MetricSample(
            metric=name,
            value_kind=value_kind,
            value_num=value_num,
            value_text=value_text,
            value_bool=value_bool,
            unit=unit,
            source=source_name,
            bus=bus,
            acquisition=acquisition,
            interface_mode=interface_mode,
            quality=quality,
            provenance=source.provenance,
            observed_us=observed_us,
            observed_at=observed_at,
            source_age_ms=age_ms,
            reported_stale=(int(reported_stale) if type(reported_stale) is bool else None),
            freshness=freshness,
        )

    def _store_catalog(
        self,
        definitions: Mapping[str, _MetricDefinition],
        captured_us: int,
    ) -> None:
        for metric in definitions.values():
            for source in metric.sources.values():
                exact = {
                    "metric": metric.name,
                    "unit": metric.unit,
                    "value_type": metric.value_type,
                    "stale_after_ms": metric.stale_after_ms,
                    "source": source.name,
                    "bus": source.bus,
                    "quality": source.quality,
                    "provenance": source.provenance,
                }
                fingerprint = hashlib.sha256(
                    json.dumps(exact, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                self._conn.execute(
                    """
                    INSERT INTO catalog_sources(
                        fingerprint,metric,unit,value_type,stale_after_ms,
                        source,bus,quality,provenance,first_seen_us,last_seen_us
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(fingerprint) DO UPDATE SET last_seen_us=excluded.last_seen_us
                    """,
                    (
                        fingerprint,
                        metric.name,
                        metric.unit,
                        metric.value_type,
                        metric.stale_after_ms,
                        source.name,
                        source.bus,
                        source.quality,
                        source.provenance,
                        captured_us,
                        captured_us,
                    ),
                )

    @staticmethod
    def _vehicle_fields(snapshot: Mapping[str, object]) -> dict[str, object]:
        status = snapshot.get("status")
        vehicle = status.get("vehicle_state") if isinstance(status, Mapping) else None
        if not isinstance(vehicle, Mapping):
            return {
                "state": None,
                "running": None,
                "confidence": None,
                "basis": None,
                "observed_at": None,
                "age_ms": None,
            }
        running = vehicle.get("running")
        observed_at = vehicle.get("observed_at")
        if observed_at is not None:
            observed_at = _iso(
                _utc_datetime(observed_at, "status.vehicle_state.observed_at")
            )
        return {
            "state": vehicle.get("state") if isinstance(vehicle.get("state"), str) else None,
            "running": _bool_db(running),
            "confidence": (
                vehicle.get("confidence")
                if isinstance(vehicle.get("confidence"), str)
                else None
            ),
            "basis": vehicle.get("basis") if isinstance(vehicle.get("basis"), str) else None,
            "observed_at": observed_at,
            "age_ms": _optional_nonnegative_int(
                vehicle.get("age_ms"), "status.vehicle_state.age_ms"
            ),
        }

    def _activity_basis(
        self,
        samples: Mapping[str, _MetricSample],
        vehicle: Mapping[str, object],
    ) -> tuple[bool, str]:
        rpm = samples.get("engine.rpm")
        if (
            rpm is not None
            and rpm.freshness == "fresh"
            and rpm.value_num is not None
            and rpm.value_num >= self.config.running_rpm_threshold
        ):
            return True, "fresh_engine_rpm"
        speed = samples.get("vehicle.speed")
        if (
            speed is not None
            and speed.freshness == "fresh"
            and speed.value_num is not None
            and speed.value_num > self.config.moving_speed_threshold_mph
        ):
            return True, "fresh_vehicle_speed"
        if (
            vehicle.get("running") == 1
            and isinstance(vehicle.get("age_ms"), int)
            and vehicle["age_ms"] <= self.config.vehicle_state_max_age_seconds * 1000
            and vehicle.get("confidence") not in (None, "unknown", "stale")
        ):
            return True, "fresh_vehicle_running_state"
        return False, "no_fresh_running_or_moving_evidence"

    def _classify_regime(self, samples: Mapping[str, _MetricSample]) -> str:
        def numeric(name: str) -> float | None:
            sample = samples.get(name)
            if sample is None or sample.freshness != "fresh":
                return None
            return sample.value_num

        rpm = numeric("engine.rpm")
        speed = numeric("vehicle.speed")
        coolant = numeric("engine.coolant_temperature")
        if rpm is None:
            engine = "engine_unknown"
            rpm_band = "rpm_unknown"
        elif rpm < self.config.running_rpm_threshold:
            engine, rpm_band = "engine_off", "rpm_off"
        elif rpm < 1_000:
            engine, rpm_band = "engine_running", "rpm_idle"
        elif rpm < 2_200:
            engine, rpm_band = "engine_running", "rpm_low"
        elif rpm < 3_500:
            engine, rpm_band = "engine_running", "rpm_mid"
        else:
            engine, rpm_band = "engine_running", "rpm_high"
        if speed is None:
            motion = "speed_unknown"
        elif speed <= self.config.moving_speed_threshold_mph:
            motion = "stationary"
        elif speed < 35:
            motion = "urban"
        elif speed < 65:
            motion = "road"
        else:
            motion = "highway"
        if coolant is None:
            thermal = "thermal_unknown"
        elif coolant < 160:
            thermal = "cold"
        elif coolant <= 220:
            thermal = "warm"
        else:
            thermal = "hot"
        return ":".join((engine, motion, rpm_band, thermal))

    def _resolve_trip(
        self,
        captured_us: int,
        active: bool,
        basis: str,
    ) -> int | None:
        row = self._conn.execute(
            "SELECT * FROM trips WHERE ended_us IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        idle_us = int(round(self.config.trip_idle_timeout_seconds * MICROSECONDS))
        if row is not None and captured_us - row["last_active_us"] >= idle_us:
            self._conn.execute(
                """
                UPDATE trips
                SET ended_us=last_active_us, ended_at=last_active_at,
                    end_reason='activity_timeout'
                WHERE id=?
                """,
                (row["id"],),
            )
            row = None
        if active and row is None:
            cursor = self._conn.execute(
                """
                INSERT INTO trips(
                    started_us,started_at,last_active_us,last_active_at,start_basis
                ) VALUES(?,?,?,?,?)
                """,
                (captured_us, _iso_from_us(captured_us), captured_us, _iso_from_us(captured_us), basis),
            )
            return int(cursor.lastrowid)
        if row is None:
            return None
        if active:
            self._conn.execute(
                """
                UPDATE trips SET last_active_us=?,last_active_at=? WHERE id=?
                """,
                (captured_us, _iso_from_us(captured_us), row["id"]),
            )
        return int(row["id"])

    @staticmethod
    def _delivery(snapshot: Mapping[str, object]) -> tuple[str | None, int | None, int | None]:
        delivery = snapshot.get("web_delivery")
        if not isinstance(delivery, Mapping):
            return None, None, None
        instance = delivery.get("instance_id")
        sequence = delivery.get("sequence")
        generated_ms = delivery.get("generated_at_ms")
        if not isinstance(instance, str) or not instance:
            instance = None
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            sequence = None
        if not isinstance(generated_ms, int) or isinstance(generated_ms, bool) or generated_ms < 0:
            generated_ms = None
        return instance, sequence, generated_ms

    def ingest_snapshot(
        self,
        snapshot: Mapping[str, object],
        *,
        captured_at: datetime | str | None = None,
        ingest_key: str | None = None,
    ) -> IngestResult:
        """Validate and atomically ingest one broker snapshot.

        ``captured_at`` is the historian receipt/generation time, never a
        substitute for each metric's own ``observed_at``.  When omitted, the
        web-delivery wall timestamp is used; a direct broker snapshot falls
        back to the current UTC time.
        """

        if not isinstance(snapshot, Mapping):
            raise SnapshotValidationError("snapshot must be an object")
        instance, sequence, generated_ms = self._delivery(snapshot)
        if captured_at is None:
            if generated_ms is not None:
                captured = datetime.fromtimestamp(generated_ms / 1000, timezone.utc)
            else:
                captured = datetime.now(timezone.utc)
        else:
            captured = _utc_datetime(captured_at, "captured_at")
        captured_us = _to_us(captured)
        captured_iso = _iso(captured)
        if ingest_key is None:
            if instance is not None and sequence is not None:
                ingest_key = f"web:{instance}:{sequence}"
            else:
                ingest_key = f"captured:{captured_us}"
        if not isinstance(ingest_key, str) or not ingest_key.strip():
            raise SnapshotValidationError("ingest_key must be a nonempty string")

        definitions = self._parse_catalog(snapshot)
        metrics_payload = snapshot.get("metrics")
        if not isinstance(metrics_payload, Mapping):
            raise SnapshotValidationError("snapshot.metrics must be an object")
        unknown = set(metrics_payload) - set(definitions)
        if unknown:
            raise SnapshotValidationError(
                f"metrics absent from catalog: {', '.join(sorted(map(str, unknown)))}"
            )
        samples: dict[str, _MetricSample] = {}
        gap_states: dict[str, tuple[str, str, str] | None] = {}
        for name, definition in definitions.items():
            payload = metrics_payload.get(name)
            if payload is None:
                gap_states[name] = (
                    "missing",
                    "metric_absent",
                    "catalog metric is absent from this snapshot",
                )
                continue
            if not isinstance(payload, Mapping):
                raise SnapshotValidationError(f"metric {name!r} must be an object")
            parsed = self._parse_metric(name, payload, definition)
            if isinstance(parsed, tuple):
                gap_states[name] = parsed
                continue
            samples[name] = parsed
            if parsed.freshness == "fresh":
                gap_states[name] = None
            elif parsed.freshness == "stale":
                gap_states[name] = (
                    "stale",
                    "stale_observation",
                    "the value is retained with its age but is not current evidence",
                )
            else:
                gap_states[name] = (
                    "undated",
                    "observation_time_unavailable",
                    "the value lacks a valid observation time or age",
                )
        vehicle = self._vehicle_fields(snapshot)
        active, activity_basis = self._activity_basis(samples, vehicle)
        regime = self._classify_regime(samples)

        with self._lock, self._conn:
            duplicate = self._conn.execute(
                "SELECT * FROM snapshots WHERE ingest_key=?", (ingest_key,)
            ).fetchone()
            if duplicate is not None:
                return IngestResult(
                    snapshot_id=int(duplicate["id"]),
                    captured_at=duplicate["captured_at"],
                    duplicate=True,
                    trip_id=duplicate["trip_id"],
                    regime=duplicate["regime"],
                    stored_samples=0,
                    metric_gap_count=self._active_gap_count("metric_gaps"),
                    interface_gap_count=self._active_gap_count("interface_gaps"),
                )
            latest = self._conn.execute(
                "SELECT captured_us FROM snapshots ORDER BY captured_us DESC LIMIT 1"
            ).fetchone()
            if latest is not None and captured_us <= latest["captured_us"]:
                raise OutOfOrderSnapshotError(
                    "snapshot time must be newer than the latest successful ingest"
                )
            self._store_catalog(definitions, captured_us)
            trip_id = self._resolve_trip(captured_us, active, activity_basis)
            cursor = self._conn.execute(
                """
                INSERT INTO snapshots(
                    ingest_key,captured_us,captured_at,source_instance,source_sequence,
                    vehicle_state,vehicle_running,vehicle_confidence,vehicle_basis,
                    vehicle_observed_at,vehicle_age_ms,regime,trip_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ingest_key,
                    captured_us,
                    captured_iso,
                    instance,
                    sequence,
                    vehicle["state"],
                    vehicle["running"],
                    vehicle["confidence"],
                    vehicle["basis"],
                    vehicle["observed_at"],
                    vehicle["age_ms"],
                    regime,
                    trip_id,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            for sample in samples.values():
                self._conn.execute(
                    """
                    INSERT INTO metric_samples(
                        snapshot_id,trip_id,captured_us,metric,value_kind,value_num,
                        value_text,value_bool,unit,source,bus,acquisition,interface_mode,
                        quality,provenance,observed_us,observed_at,source_age_ms,
                        reported_stale,freshness,regime
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id,
                        trip_id,
                        captured_us,
                        sample.metric,
                        sample.value_kind,
                        sample.value_num,
                        sample.value_text,
                        sample.value_bool,
                        sample.unit,
                        sample.source,
                        sample.bus,
                        sample.acquisition,
                        sample.interface_mode,
                        sample.quality,
                        sample.provenance,
                        sample.observed_us,
                        sample.observed_at,
                        sample.source_age_ms,
                        sample.reported_stale,
                        sample.freshness,
                        regime,
                    ),
                )
            for metric, gap in gap_states.items():
                self._update_gap(
                    table="metric_gaps",
                    key_column="metric",
                    key=metric,
                    gap=gap,
                    captured_us=captured_us,
                    snapshot_id=snapshot_id,
                )
            self._ingest_interfaces(snapshot, captured_us, snapshot_id)
            self._ingest_system_health(snapshot, captured_us, snapshot_id)
            self._ingest_usb_can_monitor(snapshot, captured_us, snapshot_id)
            self._ingest_data_quality(snapshot, captured_us, snapshot_id)
            if trip_id is not None:
                self._conn.execute(
                    "UPDATE trips SET snapshot_count=snapshot_count+1 WHERE id=?",
                    (trip_id,),
                )
            return IngestResult(
                snapshot_id=snapshot_id,
                captured_at=captured_iso,
                duplicate=False,
                trip_id=trip_id,
                regime=regime,
                stored_samples=len(samples),
                metric_gap_count=self._active_gap_count("metric_gaps"),
                interface_gap_count=self._active_gap_count("interface_gaps"),
            )

    def _active_gap_count(self, table: str) -> int:
        if table not in ("metric_gaps", "interface_gaps"):
            raise ValueError("unknown gap table")
        return int(
            self._conn.execute(
                f"SELECT count(*) FROM {table} WHERE ended_us IS NULL"
            ).fetchone()[0]
        )

    def _update_gap(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        gap: tuple[str, str, str] | tuple[str, str] | None,
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        if (table, key_column) not in (
            ("metric_gaps", "metric"),
            ("interface_gaps", "role"),
        ):
            raise ValueError("invalid gap table")
        current = self._conn.execute(
            f"SELECT * FROM {table} WHERE {key_column}=? AND ended_us IS NULL",
            (key,),
        ).fetchone()
        if gap is None:
            if current is not None:
                self._conn.execute(
                    f"UPDATE {table} SET ended_us=?,ended_at=? WHERE id=?",
                    (captured_us, _iso_from_us(captured_us), current["id"]),
                )
            return
        state, reason = gap[0], gap[1]
        detail = gap[2] if len(gap) == 3 else ""
        same = current is not None and (
            current["state"] == state
            and current["reason"] == reason
            and (table != "metric_gaps" or current["detail"] == detail)
        )
        if same:
            self._conn.execute(
                f"""
                UPDATE {table}
                SET last_seen_us=?,last_seen_at=?,last_snapshot_id=?,
                    observation_count=observation_count+1
                WHERE id=?
                """,
                (captured_us, _iso_from_us(captured_us), snapshot_id, current["id"]),
            )
            return
        if current is not None:
            self._conn.execute(
                f"UPDATE {table} SET ended_us=?,ended_at=? WHERE id=?",
                (captured_us, _iso_from_us(captured_us), current["id"]),
            )
        if table == "metric_gaps":
            columns = (
                "metric,state,reason,detail,started_us,started_at,last_seen_us,"
                "last_seen_at,first_snapshot_id,last_snapshot_id"
            )
            values: list[object] = [
                key,
                state,
                reason,
                detail,
                captured_us,
                _iso_from_us(captured_us),
                captured_us,
                _iso_from_us(captured_us),
                snapshot_id,
                snapshot_id,
            ]
        else:
            columns = (
                "role,state,reason,started_us,started_at,last_seen_us,"
                "last_seen_at,first_snapshot_id,last_snapshot_id"
            )
            values = [
                key,
                state,
                reason,
                captured_us,
                _iso_from_us(captured_us),
                captured_us,
                _iso_from_us(captured_us),
                snapshot_id,
                snapshot_id,
            ]
        placeholders = ",".join("?" for _ in values)
        self._conn.execute(
            f"INSERT INTO {table}({columns}) VALUES({placeholders})",
            values,
        )

    @staticmethod
    def _interface_payloads(
        snapshot: Mapping[str, object],
    ) -> dict[str, Mapping[str, object]]:
        status = snapshot.get("status")
        if not isinstance(status, Mapping):
            return {}
        multiple = status.get("interfaces")
        if isinstance(multiple, Mapping):
            normalized_multiple: dict[str, Mapping[str, object]] = {}
            for key, payload in multiple.items():
                if not isinstance(payload, Mapping):
                    continue
                role = key
                if _is_placeholder_interface_role(role):
                    topology = payload.get("topology")
                    topology = topology if isinstance(topology, Mapping) else {}
                    role = topology.get("bus") or payload.get("role")
                if (
                    not isinstance(role, str)
                    or not role
                    or _is_placeholder_interface_role(role)
                ):
                    continue
                normalized_multiple[role] = payload
            if normalized_multiple:
                return normalized_multiple
        single = status.get("interface")
        if not isinstance(single, Mapping):
            return {}
        role_snapshot = single.get("role_interfaces")
        if "role_interfaces" in single:
            if not isinstance(role_snapshot, Mapping):
                return {}
            role_payloads = role_snapshot.get("roles")
            if not isinstance(role_payloads, Mapping):
                return {}
            normalized: dict[str, Mapping[str, object]] = {}
            for role, payload in role_payloads.items():
                if (
                    not isinstance(role, str)
                    or not role
                    or _is_placeholder_interface_role(role)
                    or not isinstance(payload, Mapping)
                ):
                    continue
                expected = payload.get("expected")
                expected = expected if isinstance(expected, Mapping) else {}
                if expected.get("passive_required") is not True:
                    continue
                actual = payload.get("actual")
                actual = actual if isinstance(actual, Mapping) else {}
                operating_mode = payload.get("operating_mode")
                topology_usable = payload.get("topology_usable")
                if type(topology_usable) is not bool:
                    topology_usable = payload.get("passive_ready")
                normalized[role] = {
                    "resolution": payload.get("resolution"),
                    "role_reason": payload.get("reason"),
                    "detail": payload.get("detail"),
                    "channel": payload.get("channel"),
                    "usb_serial": expected.get("usb_serial"),
                    "usb_dev_id": expected.get("dev_id"),
                    "topology_generation": role_snapshot.get("generation"),
                    "adapter_present": payload.get("resolution") == "resolved",
                    "up": actual.get("up"),
                    "bitrate": actual.get("bitrate"),
                    "listen_only": actual.get("listen_only"),
                    "controller_state": actual.get("controller_state"),
                    "mode": operating_mode,
                    "topology": {
                        "bus": role,
                        "usable": topology_usable,
                    },
                }
            # Presence of the role-aware shape is authoritative even before
            # reconciliation has populated a usable vehicle role.  Falling
            # through here used to promote its transient top-level ``canN``
            # channel to a durable historian role.
            return normalized
        topology = single.get("topology")
        bus = topology.get("bus") if isinstance(topology, Mapping) else None
        role = (
            bus
            if (
                isinstance(bus, str)
                and bool(bus)
                and not _is_placeholder_interface_role(bus)
            )
            else single.get("role")
        )
        if (
            not isinstance(role, str)
            or not role
            or _is_placeholder_interface_role(role)
        ):
            return {}
        return {role: single}

    def _retire_placeholder_interface_roles(
        self,
        *,
        captured_us: int,
        snapshot_id: int,
    ) -> set[str]:
        """Close legacy channel-keyed gaps and return durable seen roles.

        Early role-aware startup snapshots could be stored under an ephemeral
        ``canN`` key before reconciliation supplied the logical roles.  Keep
        those rows as provenance, but never use them to create an endless
        ``interface_role_absent`` interval.  Including open gaps in the repair
        set also heals databases whose old raw samples were already pruned.
        """

        previously_seen = {
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT role FROM interface_samples"
            )
        }
        open_gap_roles = {
            row[0]
            for row in self._conn.execute(
                "SELECT role FROM interface_gaps WHERE ended_us IS NULL"
            )
        }
        placeholders = {
            role
            for role in previously_seen | open_gap_roles
            if _is_placeholder_interface_role(role)
        }
        for role in sorted(placeholders):
            self._update_gap(
                table="interface_gaps",
                key_column="role",
                key=role,
                gap=None,
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )
        return previously_seen - placeholders

    @staticmethod
    def _interface_health(payload: Mapping[str, object]) -> tuple[str, str]:
        resolution = payload.get("resolution")
        if isinstance(resolution, str) and resolution != "resolved":
            state = (
                resolution
                if resolution in ("missing", "ambiguous")
                else "unresolved"
            )
            return "unhealthy", f"role_{state}"
        topology = payload.get("topology")
        topology_usable = topology.get("usable") if isinstance(topology, Mapping) else None
        checks = {
            "adapter_missing": payload.get("adapter_present") is False,
            "interface_down": payload.get("up") is False,
            "controller_unhealthy": (
                isinstance(payload.get("controller_state"), str)
                and payload.get("controller_state") != "ERROR-ACTIVE"
            ),
            "topology_unusable": topology_usable is False,
        }
        failures = [reason for reason, failed in checks.items() if failed]
        if failures:
            return "unhealthy", ",".join(failures)
        known = (
            type(payload.get("adapter_present")) is bool
            and type(payload.get("up")) is bool
            and isinstance(payload.get("controller_state"), str)
            and type(topology_usable) is bool
        )
        if not known:
            return "unknown", "interface_health_incomplete"
        if payload.get("mode") == "armed_diagnostic":
            return "healthy", "armed_diagnostic"
        return "healthy", "healthy"

    def _ingest_interfaces(
        self,
        snapshot: Mapping[str, object],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        interfaces = self._interface_payloads(snapshot)
        previously_seen = self._retire_placeholder_interface_roles(
            captured_us=captured_us,
            snapshot_id=snapshot_id,
        )
        if not interfaces:
            self._update_gap(
                table="interface_gaps",
                key_column="role",
                key="interface-status",
                gap=("missing", "interface_status_missing"),
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )
            for role in previously_seen:
                self._update_gap(
                    table="interface_gaps",
                    key_column="role",
                    key=role,
                    gap=("missing", "interface_role_absent"),
                    captured_us=captured_us,
                    snapshot_id=snapshot_id,
                )
            return
        self._update_gap(
            table="interface_gaps",
            key_column="role",
            key="interface-status",
            gap=None,
            captured_us=captured_us,
            snapshot_id=snapshot_id,
        )
        for role in sorted(previously_seen - set(interfaces)):
            self._update_gap(
                table="interface_gaps",
                key_column="role",
                key=role,
                gap=("missing", "interface_role_absent"),
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )
        for role, payload in interfaces.items():
            topology = payload.get("topology")
            topology = topology if isinstance(topology, Mapping) else {}
            health, reason = self._interface_health(payload)
            bitrate = payload.get("bitrate")
            if not isinstance(bitrate, int) or isinstance(bitrate, bool) or bitrate <= 0:
                bitrate = None
            self._conn.execute(
                """
                INSERT INTO interface_samples(
                    snapshot_id,role,captured_us,channel,usb_serial,bus,
                    adapter_present,up,bitrate,listen_only,controller_state,
                    topology_usable,health,reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    role,
                    captured_us,
                    payload.get("channel") if isinstance(payload.get("channel"), str) else None,
                    payload.get("usb_serial") if isinstance(payload.get("usb_serial"), str) else None,
                    topology.get("bus") if isinstance(topology.get("bus"), str) else None,
                    _bool_db(payload.get("adapter_present")),
                    _bool_db(payload.get("up")),
                    bitrate,
                    _bool_db(payload.get("listen_only")),
                    (
                        payload.get("controller_state")
                        if isinstance(payload.get("controller_state"), str)
                        else None
                    ),
                    _bool_db(topology.get("usable")),
                    health,
                    reason,
                ),
            )
            dev_id = payload.get("usb_dev_id")
            if not isinstance(dev_id, int) or isinstance(dev_id, bool) or dev_id < 0:
                dev_id = None
            self._conn.execute(
                """
                INSERT INTO interface_role_details(
                    snapshot_id,role,resolution,role_reason,detail,usb_dev_id,
                    topology_generation
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    role,
                    (
                        payload.get("resolution")
                        if isinstance(payload.get("resolution"), str)
                        else None
                    ),
                    (
                        payload.get("role_reason")
                        if isinstance(payload.get("role_reason"), str)
                        else None
                    ),
                    (
                        payload.get("detail")
                        if isinstance(payload.get("detail"), str)
                        else None
                    ),
                    dev_id,
                    (
                        payload.get("topology_generation")
                        if isinstance(payload.get("topology_generation"), str)
                        else None
                    ),
                ),
            )
            gap = None if health == "healthy" else (health, reason)
            self._update_gap(
                table="interface_gaps",
                key_column="role",
                key=role,
                gap=gap,
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )

    @staticmethod
    def _safe_json_list(value: object) -> list[object]:
        """Keep only JSON-compatible list data from broker health status."""

        if not isinstance(value, list):
            return []
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
            decoded = json.loads(encoded)
        except (TypeError, ValueError):
            return []
        return decoded if isinstance(decoded, list) else []

    def _ingest_system_health(
        self,
        snapshot: Mapping[str, object],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        interface = status.get("interface")
        interface = interface if isinstance(interface, Mapping) else {}
        role_snapshot = interface.get("role_interfaces")
        role_snapshot = role_snapshot if isinstance(role_snapshot, Mapping) else {}
        generation = role_snapshot.get("generation")
        generation = generation if isinstance(generation, str) and generation else None
        issues = self._safe_json_list(role_snapshot.get("issues"))
        inhibits = self._safe_json_list(interface.get("active_inhibits"))
        active_drive = status.get("active_drive")
        active_drive = active_drive if isinstance(active_drive, Mapping) else {}
        restoration_failed = active_drive.get("restoration_failed")
        self._conn.execute(
            """
            INSERT INTO system_health_samples(
                snapshot_id,captured_us,topology_generation,issues_json,
                active_inhibits_json,restoration_failed
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                captured_us,
                generation,
                json.dumps(issues, sort_keys=True, separators=(",", ":")),
                json.dumps(inhibits, sort_keys=True, separators=(",", ":")),
                _bool_db(restoration_failed),
            ),
        )

    @staticmethod
    def _usb_can_serials(value: object, field: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 8:
            raise SnapshotValidationError(f"{field} must contain at most 8 serials")
        serials: list[str] = []
        for index, serial in enumerate(value):
            serial = _required_text(serial, f"{field}[{index}]")
            if len(serial) > 256:
                raise SnapshotValidationError(f"{field}[{index}] is oversized")
            serials.append(serial)
        if len(set(serials)) != len(serials):
            raise SnapshotValidationError(f"{field} contains duplicate serials")
        return sorted(serials)

    @staticmethod
    def _usb_can_time(value: object, field: str) -> tuple[int, str]:
        moment = _utc_datetime(value, field)
        return _to_us(moment), _iso(moment)

    @staticmethod
    def _usb_can_serial_health_proven(
        snapshot: Mapping[str, object], serial: str
    ) -> bool:
        """Require every exact role for one serial to be resolved and safe."""

        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        interface = status.get("interface")
        interface = interface if isinstance(interface, Mapping) else {}
        role_snapshot = interface.get("role_interfaces")
        role_snapshot = role_snapshot if isinstance(role_snapshot, Mapping) else {}
        roles = role_snapshot.get("roles")
        roles = roles if isinstance(roles, Mapping) else {}
        matched: list[Mapping[str, object]] = []
        for payload in roles.values():
            if not isinstance(payload, Mapping):
                continue
            expected = payload.get("expected")
            expected = expected if isinstance(expected, Mapping) else {}
            if expected.get("usb_serial") != serial:
                continue
            matched.append(payload)
            if payload.get("resolution") != "resolved":
                return False
            actual = payload.get("actual")
            actual = actual if isinstance(actual, Mapping) else {}
            if actual.get("present") is False:
                return False
            if payload.get("safe") is True:
                continue
            if expected.get("passive_required") is True:
                if payload.get("passive_ready") is not True:
                    return False
            elif actual.get("up") is not False:
                return False
        return bool(matched)

    def _resolve_previous_usb_can_incidents(
        self,
        snapshot: Mapping[str, object],
        *,
        captured_us: int,
        snapshot_id: int,
        producer_instance: str,
        boot_id: str,
        current_incident_ids: set[str],
    ) -> None:
        """Retire prior-process incidents from authoritative healthy roles.

        A broker restart necessarily empties the monitor's in-memory active
        map.  Absence from a new process is not recovery evidence by itself;
        only a running receive-only monitor plus a fresh, exact, all-role-safe
        broker snapshot may close the prior incident.  Its original removal
        evidence and incident row remain durable.
        """

        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        monitor_status = status.get("usb_can_monitor")
        monitor_status = (
            monitor_status if isinstance(monitor_status, Mapping) else {}
        )
        if not (
            monitor_status.get("state") == "running"
            and monitor_status.get("receive_only") is True
            and monitor_status.get("hardware_actions") is False
            and monitor_status.get("producer_instance") == producer_instance
            and monitor_status.get("boot_id") == boot_id
        ):
            return
        role_snapshot = status.get("interface")
        role_snapshot = (
            role_snapshot.get("role_interfaces")
            if isinstance(role_snapshot, Mapping)
            else None
        )
        generation = (
            role_snapshot.get("generation")
            if isinstance(role_snapshot, Mapping)
            and isinstance(role_snapshot.get("generation"), str)
            else "generation-unavailable"
        )
        rows = self._conn.execute(
            """
            SELECT * FROM usb_can_incidents
            WHERE state='active' AND producer_instance<>?
            ORDER BY opened_us,incident_id
            """,
            (producer_instance,),
        ).fetchall()
        resolved_at = _iso_from_us(captured_us)
        for row in rows:
            incident_id = str(row["incident_id"])
            if incident_id in current_incident_ids:
                continue
            try:
                affected = json.loads(row["affected_serials_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(affected, list) or not affected or not all(
                isinstance(serial, str)
                and self._usb_can_serial_health_proven(snapshot, serial)
                for serial in affected
            ):
                continue
            event_identity = {
                "incident_id": incident_id,
                "producer_instance": producer_instance,
                "topology_generation": generation,
                "resolution": "authoritative_healthy_exact_roles_after_monitor_restart",
            }
            event_id = "usb-can-event-v1:" + hashlib.sha256(
                json.dumps(
                    event_identity,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            recovery = {
                "schema_version": 1,
                "event_id": event_id,
                "boot_id": boot_id,
                "kernel_seqnum": None,
                "kind": "usb_can_recovered",
                "action": "reconcile",
                "scope": row["scope"],
                "devpath": row["scope"],
                "usb_vid": "1d50",
                "usb_pid": "606f",
                "usb_serial": None,
                "affected_serials": affected,
                "occurred_at": resolved_at,
                "observed_monotonic": 0.0,
                "monotonic_timestamp_available": False,
                "source": "serial_role_reconciliation",
                "receive_only": True,
                "hardware_action": False,
                "recovery_basis": (
                    "authoritative healthy exact-role snapshot after monitor restart"
                ),
                "producer_instance": producer_instance,
            }
            recovery_json = json.dumps(
                recovery,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            self._conn.execute(
                """
                INSERT INTO usb_can_events(
                    event_id,occurred_us,occurred_at,boot_id,kernel_seqnum,kind,
                    action,scope,devpath,usb_vid,usb_pid,usb_serial,
                    affected_serials_json,source,payload_json,
                    first_snapshot_id,last_snapshot_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    last_snapshot_id=excluded.last_snapshot_id
                """,
                (
                    event_id,
                    captured_us,
                    resolved_at,
                    boot_id,
                    None,
                    "usb_can_recovered",
                    "reconcile",
                    row["scope"],
                    row["scope"],
                    "1d50",
                    "606f",
                    None,
                    json.dumps(affected, separators=(",", ":")),
                    "serial_role_reconciliation",
                    recovery_json,
                    snapshot_id,
                    snapshot_id,
                ),
            )
            incident = self._usb_can_payload(row)
            incident.update(
                {
                    "state": "resolved",
                    "last_event_id": event_id,
                    "last_seen_at": resolved_at,
                    "resolved_event_id": event_id,
                    "resolved_at": resolved_at,
                    "resolution": (
                        "authoritative_healthy_exact_roles_after_monitor_restart"
                    ),
                    "notification_eligible": False,
                    "event_count": int(row["event_count"]) + 1,
                    "resolved_by_producer_instance": producer_instance,
                }
            )
            self._conn.execute(
                """
                UPDATE usb_can_incidents
                SET state='resolved',last_seen_us=?,last_seen_at=?,resolved_us=?,
                    resolved_at=?,resolution=?,event_count=event_count+1,
                    last_event_id=?,resolved_event_id=?,payload_json=?,
                    last_snapshot_id=?
                WHERE incident_id=? AND state='active'
                """,
                (
                    captured_us,
                    resolved_at,
                    captured_us,
                    resolved_at,
                    "authoritative_healthy_exact_roles_after_monitor_restart",
                    event_id,
                    event_id,
                    json.dumps(
                        incident,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    snapshot_id,
                    incident_id,
                ),
            )

    def _ingest_usb_can_monitor(
        self,
        snapshot: Mapping[str, object],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        """Persist sub-snapshot kernel edges by stable identity.

        The broker acknowledges its bounded in-memory queue only after this
        surrounding snapshot transaction commits.  Replayed kernel events are
        therefore harmless: ``event_id`` is immutable and the first snapshot
        remains the authority for whether an advisory has already observed it.
        """

        monitor = snapshot.get("_usb_can_monitor")
        if monitor is None:
            return
        if not isinstance(monitor, Mapping):
            raise SnapshotValidationError("_usb_can_monitor must be an object")
        if monitor.get("schema_version") != 1:
            raise SnapshotValidationError("unsupported USB CAN monitor schema")
        source = _required_text(monitor.get("source"), "_usb_can_monitor.source")
        if len(source) > 128:
            raise SnapshotValidationError("_usb_can_monitor.source is oversized")
        producer_instance = _required_text(
            monitor.get("producer_instance"),
            "_usb_can_monitor.producer_instance",
        )
        boot_id = _required_text(monitor.get("boot_id"), "_usb_can_monitor.boot_id")
        if len(producer_instance) > 128 or len(boot_id) > 128:
            raise SnapshotValidationError(
                "_usb_can_monitor producer or boot identity is oversized"
            )
        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        monitor_status = status.get("usb_can_monitor")
        if not isinstance(monitor_status, Mapping):
            raise SnapshotValidationError(
                "status.usb_can_monitor must accompany its persistence batch"
            )
        if (
            monitor_status.get("producer_instance") != producer_instance
            or monitor_status.get("boot_id") != boot_id
            or monitor_status.get("receive_only") is not True
            or monitor_status.get("hardware_actions") is not False
        ):
            raise SnapshotValidationError(
                "status.usb_can_monitor does not match its safe producer batch"
            )
        dropped = monitor.get("dropped_event_count", 0)
        if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
            raise SnapshotValidationError(
                "_usb_can_monitor.dropped_event_count must be non-negative"
            )
        events = monitor.get("events", [])
        incidents = monitor.get("incidents", [])
        if not isinstance(events, list) or len(events) > 512:
            raise SnapshotValidationError(
                "_usb_can_monitor.events must contain at most 512 events"
            )
        if not isinstance(incidents, list) or len(incidents) > 128:
            raise SnapshotValidationError(
                "_usb_can_monitor.incidents must contain at most 128 incidents"
            )
        self._conn.execute(
            """
            INSERT INTO usb_can_monitor_samples(
                snapshot_id,captured_us,source,producer_instance,
                dropped_event_count,pending_event_count
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                captured_us,
                source,
                producer_instance,
                dropped,
                len(events),
            ),
        )

        allowed_event_kinds = {
            "usb_parent_hub_removed",
            "usb_parent_hub_added",
            "usb_can_adapter_removed",
            "usb_can_adapter_added",
            "usb_can_netdev_removed",
            "usb_can_netdev_added",
            "usb_can_recovered",
        }
        seen_event_ids: set[str] = set()
        for index, item in enumerate(events):
            prefix = f"_usb_can_monitor.events[{index}]"
            if not isinstance(item, Mapping):
                raise SnapshotValidationError(f"{prefix} must be an object")
            if item.get("schema_version") != 1:
                raise SnapshotValidationError(f"{prefix} has unsupported schema")
            if item.get("receive_only") is not True or item.get("hardware_action") is not False:
                raise SnapshotValidationError(
                    f"{prefix} does not prove receive-only/no-action semantics"
                )
            event_id = _required_text(item.get("event_id"), f"{prefix}.event_id")
            if (
                not event_id.startswith("usb-can-event-v1:")
                or len(event_id) > 128
                or event_id in seen_event_ids
            ):
                raise SnapshotValidationError(f"{prefix}.event_id is invalid or repeated")
            seen_event_ids.add(event_id)
            kind = _required_text(item.get("kind"), f"{prefix}.kind")
            if kind not in allowed_event_kinds:
                raise SnapshotValidationError(f"{prefix}.kind is not allowlisted")
            action = _required_text(item.get("action"), f"{prefix}.action")
            if action not in ("add", "remove", "reconcile"):
                raise SnapshotValidationError(f"{prefix}.action is invalid")
            event_boot_id = _required_text(
                item.get("boot_id"), f"{prefix}.boot_id"
            )
            if event_boot_id != boot_id:
                raise SnapshotValidationError(
                    f"{prefix}.boot_id does not match its producer batch"
                )
            scope = _required_text(item.get("scope"), f"{prefix}.scope")
            devpath = _required_text(item.get("devpath"), f"{prefix}.devpath")
            event_source = _required_text(item.get("source"), f"{prefix}.source")
            if event_source not in (
                "kernel_kobject_uevent",
                "serial_role_reconciliation",
            ):
                raise SnapshotValidationError(f"{prefix}.source is not allowlisted")
            if any(
                len(value) > maximum
                for value, maximum in (
                    (event_boot_id, 128),
                    (scope, 320),
                    (devpath, 4096),
                    (event_source, 128),
                )
            ):
                raise SnapshotValidationError(f"{prefix} contains oversized text")
            occurred_us, occurred_at = self._usb_can_time(
                item.get("occurred_at"), f"{prefix}.occurred_at"
            )
            affected = self._usb_can_serials(
                item.get("affected_serials", []), f"{prefix}.affected_serials"
            )
            observed_monotonic = item.get("observed_monotonic")
            if (
                not isinstance(observed_monotonic, (int, float))
                or isinstance(observed_monotonic, bool)
                or not math.isfinite(float(observed_monotonic))
                or float(observed_monotonic) < 0
            ):
                raise SnapshotValidationError(
                    f"{prefix}.observed_monotonic must be finite and non-negative"
                )
            kernel_seqnum = item.get("kernel_seqnum")
            if kernel_seqnum is not None:
                kernel_seqnum = _required_text(
                    kernel_seqnum, f"{prefix}.kernel_seqnum"
                )
                if len(kernel_seqnum) > 64:
                    raise SnapshotValidationError(
                        f"{prefix}.kernel_seqnum is oversized"
                    )
            optional: dict[str, str | None] = {}
            for field, maximum in (
                ("usb_vid", 4),
                ("usb_pid", 4),
                ("usb_serial", 256),
            ):
                value = item.get(field)
                if value is not None:
                    value = _required_text(value, f"{prefix}.{field}")
                    if len(value) > maximum:
                        raise SnapshotValidationError(f"{prefix}.{field} is oversized")
                optional[field] = value
            try:
                payload_json = json.dumps(
                    item,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise SnapshotValidationError(
                    f"{prefix} is not bounded JSON data: {exc}"
                ) from None
            if len(payload_json.encode("utf-8")) > 16 * 1024:
                raise SnapshotValidationError(f"{prefix} JSON payload is oversized")
            existing = self._conn.execute(
                "SELECT payload_json FROM usb_can_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None and existing["payload_json"] != payload_json:
                raise SnapshotValidationError(
                    f"event identity collision for {event_id!r}"
                )
            self._conn.execute(
                """
                INSERT INTO usb_can_events(
                    event_id,occurred_us,occurred_at,boot_id,kernel_seqnum,kind,
                    action,scope,devpath,usb_vid,usb_pid,usb_serial,
                    affected_serials_json,source,payload_json,
                    first_snapshot_id,last_snapshot_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    last_snapshot_id=excluded.last_snapshot_id
                """,
                (
                    event_id,
                    occurred_us,
                    occurred_at,
                    event_boot_id,
                    kernel_seqnum,
                    kind,
                    action,
                    scope,
                    devpath,
                    optional["usb_vid"],
                    optional["usb_pid"],
                    optional["usb_serial"],
                    json.dumps(affected, separators=(",", ":")),
                    event_source,
                    payload_json,
                    snapshot_id,
                    snapshot_id,
                ),
            )

        seen_incident_ids: set[str] = set()
        current_active_incident_ids: set[str] = set()
        for index, item in enumerate(incidents):
            prefix = f"_usb_can_monitor.incidents[{index}]"
            if not isinstance(item, Mapping):
                raise SnapshotValidationError(f"{prefix} must be an object")
            if item.get("schema_version") != 1:
                raise SnapshotValidationError(f"{prefix} has unsupported schema")
            incident_id = _required_text(
                item.get("incident_id"), f"{prefix}.incident_id"
            )
            if (
                not incident_id.startswith("usb-can-incident-v1:")
                or len(incident_id) > 128
                or incident_id in seen_incident_ids
            ):
                raise SnapshotValidationError(
                    f"{prefix}.incident_id is invalid or repeated"
                )
            seen_incident_ids.add(incident_id)
            state = _required_text(item.get("state"), f"{prefix}.state")
            if state not in ("active", "resolved"):
                raise SnapshotValidationError(f"{prefix}.state is invalid")
            if state == "active":
                current_active_incident_ids.add(incident_id)
            kind = _required_text(item.get("kind"), f"{prefix}.kind")
            if kind not in (
                "usb_parent_hub_removed",
                "usb_can_adapter_removed",
                "usb_can_netdev_removed",
            ):
                raise SnapshotValidationError(f"{prefix}.kind is invalid")
            scope = _required_text(item.get("scope"), f"{prefix}.scope")
            incident_source = _required_text(
                item.get("source"), f"{prefix}.source"
            )
            if incident_source != "kernel_kobject_uevent":
                raise SnapshotValidationError(f"{prefix}.source is invalid")
            incident_producer = _required_text(
                item.get("producer_instance"), f"{prefix}.producer_instance"
            )
            if incident_producer != producer_instance:
                raise SnapshotValidationError(
                    f"{prefix}.producer_instance does not match its snapshot"
                )
            opened_event_id = _required_text(
                item.get("opened_event_id"), f"{prefix}.opened_event_id"
            )
            last_event_id = _required_text(
                item.get("last_event_id"), f"{prefix}.last_event_id"
            )
            affected = self._usb_can_serials(
                item.get("affected_serials", []), f"{prefix}.affected_serials"
            )
            opened_us, opened_at = self._usb_can_time(
                item.get("opened_at"), f"{prefix}.opened_at"
            )
            last_seen_us, last_seen_at = self._usb_can_time(
                item.get("last_seen_at"), f"{prefix}.last_seen_at"
            )
            if last_seen_us < opened_us:
                raise SnapshotValidationError(f"{prefix} predates its opening")
            resolved_at_value = item.get("resolved_at")
            if resolved_at_value is None:
                resolved_us = None
                resolved_at = None
            else:
                resolved_us, resolved_at = self._usb_can_time(
                    resolved_at_value, f"{prefix}.resolved_at"
                )
            resolution = item.get("resolution")
            resolved_event_id = item.get("resolved_event_id")
            if state == "resolved":
                resolution = _required_text(resolution, f"{prefix}.resolution")
                resolved_event_id = _required_text(
                    resolved_event_id, f"{prefix}.resolved_event_id"
                )
                if resolved_us is None or resolved_us < opened_us:
                    raise SnapshotValidationError(
                        f"{prefix}.resolved_at is invalid"
                    )
            elif any(
                value is not None
                for value in (resolved_us, resolution, resolved_event_id)
            ):
                raise SnapshotValidationError(
                    f"{prefix} active incident carries resolution fields"
                )
            notification_eligible = item.get("notification_eligible")
            if type(notification_eligible) is not bool or notification_eligible != (
                state == "active"
            ):
                raise SnapshotValidationError(
                    f"{prefix}.notification_eligible is inconsistent"
                )
            event_count = item.get("event_count")
            reappearance_count = item.get("reappearance_count", 0)
            if (
                not isinstance(event_count, int)
                or isinstance(event_count, bool)
                or event_count < 1
                or not isinstance(reappearance_count, int)
                or isinstance(reappearance_count, bool)
                or reappearance_count < 0
            ):
                raise SnapshotValidationError(f"{prefix} counters are invalid")
            try:
                payload_json = json.dumps(
                    item,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise SnapshotValidationError(
                    f"{prefix} is not bounded JSON data: {exc}"
                ) from None
            if len(payload_json.encode("utf-8")) > 16 * 1024:
                raise SnapshotValidationError(f"{prefix} JSON payload is oversized")
            existing = self._conn.execute(
                """
                SELECT scope,opened_us,opened_event_id,affected_serials_json
                FROM usb_can_incidents WHERE incident_id=?
                """,
                (incident_id,),
            ).fetchone()
            if existing is not None and (
                existing["scope"] != scope
                or int(existing["opened_us"]) != opened_us
                or existing["opened_event_id"] != opened_event_id
                or existing["affected_serials_json"]
                != json.dumps(affected, separators=(",", ":"))
            ):
                raise SnapshotValidationError(
                    f"incident identity collision for {incident_id!r}"
                )
            self._conn.execute(
                """
                INSERT INTO usb_can_incidents(
                    incident_id,state,kind,scope,opened_us,opened_at,last_seen_us,
                    last_seen_at,resolved_us,resolved_at,resolution,
                    affected_serials_json,event_count,reappearance_count,
                    opened_event_id,last_event_id,resolved_event_id,source,
                    producer_instance,payload_json,first_snapshot_id,last_snapshot_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    state=CASE
                        WHEN usb_can_incidents.state='resolved' THEN 'resolved'
                        ELSE excluded.state
                    END,
                    last_seen_us=MAX(usb_can_incidents.last_seen_us,excluded.last_seen_us),
                    last_seen_at=CASE
                        WHEN excluded.last_seen_us >= usb_can_incidents.last_seen_us
                        THEN excluded.last_seen_at ELSE usb_can_incidents.last_seen_at
                    END,
                    resolved_us=COALESCE(usb_can_incidents.resolved_us,excluded.resolved_us),
                    resolved_at=COALESCE(usb_can_incidents.resolved_at,excluded.resolved_at),
                    resolution=COALESCE(usb_can_incidents.resolution,excluded.resolution),
                    event_count=MAX(usb_can_incidents.event_count,excluded.event_count),
                    reappearance_count=MAX(
                        usb_can_incidents.reappearance_count,
                        excluded.reappearance_count
                    ),
                    last_event_id=excluded.last_event_id,
                    resolved_event_id=COALESCE(
                        usb_can_incidents.resolved_event_id,
                        excluded.resolved_event_id
                    ),
                    payload_json=CASE
                        WHEN usb_can_incidents.state='resolved'
                        THEN usb_can_incidents.payload_json ELSE excluded.payload_json
                    END,
                    last_snapshot_id=excluded.last_snapshot_id
                """,
                (
                    incident_id,
                    state,
                    kind,
                    scope,
                    opened_us,
                    opened_at,
                    last_seen_us,
                    last_seen_at,
                    resolved_us,
                    resolved_at,
                    resolution,
                    json.dumps(affected, separators=(",", ":")),
                    event_count,
                    reappearance_count,
                    opened_event_id,
                    last_event_id,
                    resolved_event_id,
                    incident_source,
                    incident_producer,
                    payload_json,
                    snapshot_id,
                    snapshot_id,
                ),
            )
        self._resolve_previous_usb_can_incidents(
            snapshot,
            captured_us=captured_us,
            snapshot_id=snapshot_id,
            producer_instance=producer_instance,
            boot_id=boot_id,
            current_incident_ids=current_active_incident_ids,
        )

    def _ingest_data_quality(
        self,
        snapshot: Mapping[str, object],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        """Upsert the broker's bounded recent quality incidents by stable id."""

        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        quality_status = status.get("data_quality")
        if quality_status is None:
            return
        if not isinstance(quality_status, Mapping):
            raise SnapshotValidationError("status.data_quality must be an object")
        producer_instance = _required_text(
            quality_status.get("producer_instance"),
            "status.data_quality.producer_instance",
        )
        if len(producer_instance) > 300:
            raise SnapshotValidationError(
                "status.data_quality.producer_instance is oversized"
            )
        recent = quality_status.get("recent", [])
        if not isinstance(recent, list) or len(recent) > 32:
            raise SnapshotValidationError(
                "status.data_quality.recent must contain at most 32 events"
            )
        seen_ids: set[str] = set()
        for index, item in enumerate(recent):
            prefix = f"status.data_quality.recent[{index}]"
            if not isinstance(item, Mapping):
                raise SnapshotValidationError(f"{prefix} must be an object")
            incident_id = _required_text(
                item.get("incident_id"), f"{prefix}.incident_id"
            )
            event_producer = _required_text(
                item.get("producer_instance"), f"{prefix}.producer_instance"
            )
            if event_producer != producer_instance:
                raise SnapshotValidationError(
                    f"{prefix}.producer_instance does not match its snapshot"
                )
            metric = _required_text(item.get("metric"), f"{prefix}.metric")
            source = _required_text(item.get("source"), f"{prefix}.source")
            bus = _required_text(item.get("bus"), f"{prefix}.bus")
            quality = _required_text(item.get("quality"), f"{prefix}.quality")
            reason = _required_text(item.get("reason"), f"{prefix}.reason")
            detail = _required_text(item.get("detail"), f"{prefix}.detail")
            if incident_id in seen_ids:
                raise SnapshotValidationError(
                    f"status.data_quality repeats incident {incident_id!r}"
                )
            seen_ids.add(incident_id)
            if len(incident_id) > 300 or any(
                len(value) > 4000
                for value in (metric, source, bus, quality, reason, detail)
            ):
                raise SnapshotValidationError(f"{prefix} contains oversized text")
            if reason != "implausible_transition":
                raise SnapshotValidationError(
                    f"{prefix}.reason is not an admitted quality event"
                )
            event_status = item.get("status")
            if event_status not in ("active", "resolved"):
                raise SnapshotValidationError(
                    f"{prefix}.status must be active or resolved"
                )
            interface_mode = item.get("interface_mode")
            if interface_mode not in ("listen_only", "armed_diagnostic"):
                raise SnapshotValidationError(
                    f"{prefix}.interface_mode is invalid"
                )
            rejection_count = item.get("rejection_count")
            if (
                not isinstance(rejection_count, int)
                or isinstance(rejection_count, bool)
                or not 1 <= rejection_count <= 1_000_000_000
            ):
                raise SnapshotValidationError(
                    f"{prefix}.rejection_count must be a positive integer"
                )
            if item.get("notification_eligible") is not False:
                raise SnapshotValidationError(
                    f"{prefix} must be explicitly ineligible for notifications"
                )
            first_dt = _utc_datetime(
                item.get("first_seen_at"), f"{prefix}.first_seen_at"
            )
            last_dt = _utc_datetime(
                item.get("last_seen_at"), f"{prefix}.last_seen_at"
            )
            first_us = _to_us(first_dt)
            last_us = _to_us(last_dt)
            if last_us < first_us:
                raise SnapshotValidationError(
                    f"{prefix}.last_seen_at predates first_seen_at"
                )
            resolved_at = item.get("resolved_at")
            resolved_dt = (
                None
                if resolved_at is None
                else _utc_datetime(resolved_at, f"{prefix}.resolved_at")
            )
            resolved_us = None if resolved_dt is None else _to_us(resolved_dt)
            if event_status == "active" and resolved_dt is not None:
                raise SnapshotValidationError(
                    f"{prefix} active incident cannot have resolved_at"
                )
            if event_status == "resolved" and (
                resolved_dt is None or resolved_us < last_us
            ):
                raise SnapshotValidationError(
                    f"{prefix} resolved incident requires a valid resolved_at"
                )
            resolution_reason = item.get("resolution_reason")
            if resolution_reason is not None and (
                not isinstance(resolution_reason, str)
                or not resolution_reason
                or len(resolution_reason) > 500
            ):
                raise SnapshotValidationError(
                    f"{prefix}.resolution_reason must be bounded text or null"
                )
            if event_status == "active" and resolution_reason is not None:
                raise SnapshotValidationError(
                    f"{prefix} active incident cannot have a resolution reason"
                )
            evidence = item.get("evidence")
            if not isinstance(evidence, Mapping):
                raise SnapshotValidationError(f"{prefix}.evidence must be an object")
            try:
                evidence_json = json.dumps(
                    dict(evidence),
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise SnapshotValidationError(
                    f"{prefix}.evidence must be finite JSON data"
                ) from exc
            if len(evidence_json.encode("utf-8")) > 16_384:
                raise SnapshotValidationError(f"{prefix}.evidence is oversized")

            existing = self._conn.execute(
                "SELECT * FROM data_quality_events WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            if existing is not None and any(
                existing[field] != expected
                for field, expected in (
                    ("producer_instance", producer_instance),
                    ("metric", metric),
                    ("source", source),
                    ("bus", bus),
                    ("quality", quality),
                    ("reason", reason),
                    ("first_seen_us", first_us),
                )
            ):
                raise SnapshotValidationError(
                    f"{prefix} changes immutable incident identity"
                )
            self._conn.execute(
                """
                INSERT INTO data_quality_events(
                    incident_id,producer_instance,metric,source,bus,quality,reason,status,
                    first_seen_us,first_seen_at,last_seen_us,last_seen_at,
                    resolved_us,resolved_at,resolution_reason,rejection_count,detail,
                    interface_mode,evidence_json,first_snapshot_id,
                    last_snapshot_id,notification_eligible
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(incident_id) DO UPDATE SET
                    status=excluded.status,
                    last_seen_us=max(data_quality_events.last_seen_us,excluded.last_seen_us),
                    last_seen_at=CASE
                        WHEN excluded.last_seen_us >= data_quality_events.last_seen_us
                        THEN excluded.last_seen_at ELSE data_quality_events.last_seen_at END,
                    resolved_us=excluded.resolved_us,
                    resolved_at=excluded.resolved_at,
                    resolution_reason=excluded.resolution_reason,
                    rejection_count=max(
                        data_quality_events.rejection_count,
                        excluded.rejection_count
                    ),
                    detail=excluded.detail,
                    interface_mode=excluded.interface_mode,
                    evidence_json=excluded.evidence_json,
                    last_snapshot_id=excluded.last_snapshot_id
                """,
                (
                    incident_id,
                    producer_instance,
                    metric,
                    source,
                    bus,
                    quality,
                    reason,
                    event_status,
                    first_us,
                    _iso(first_dt),
                    last_us,
                    _iso(last_dt),
                    resolved_us,
                    None if resolved_dt is None else _iso(resolved_dt),
                    resolution_reason,
                    rejection_count,
                    detail,
                    interface_mode,
                    evidence_json,
                    snapshot_id,
                    snapshot_id,
                ),
            )

        authoritative_good = quality_status.get("authoritative_good", [])
        if (
            not isinstance(authoritative_good, list)
            or len(authoritative_good) > 32
        ):
            raise SnapshotValidationError(
                "status.data_quality.authoritative_good must contain at most 32 rows"
            )
        active_current = {
            (item.get("metric"), item.get("source"))
            for item in recent
            if isinstance(item, Mapping) and item.get("status") == "active"
        }
        seen_good: set[tuple[str, str]] = set()
        for index, item in enumerate(authoritative_good):
            prefix = f"status.data_quality.authoritative_good[{index}]"
            if not isinstance(item, Mapping):
                raise SnapshotValidationError(f"{prefix} must be an object")
            metric = _required_text(item.get("metric"), f"{prefix}.metric")
            source = _required_text(item.get("source"), f"{prefix}.source")
            key = (metric, source)
            if key in seen_good:
                raise SnapshotValidationError(
                    f"status.data_quality.authoritative_good repeats {metric!r}"
                )
            seen_good.add(key)
            if key in active_current:
                raise SnapshotValidationError(
                    f"{prefix} conflicts with an active current-process incident"
                )
            observed_dt = _utc_datetime(
                item.get("observed_at"), f"{prefix}.observed_at"
            )
            observed_us = _to_us(observed_dt)
            prior_rows = self._conn.execute(
                """
                SELECT incident_id,last_seen_us FROM data_quality_events
                WHERE status='active' AND metric=? AND source=?
                  AND producer_instance!=?
                """,
                (metric, source, producer_instance),
            ).fetchall()
            for row in prior_rows:
                resolved_us = max(
                    int(row["last_seen_us"]),
                    observed_us,
                    captured_us,
                )
                self._conn.execute(
                    """
                    UPDATE data_quality_events
                    SET status='resolved',resolved_us=?,resolved_at=?,
                        resolution_reason=?,last_snapshot_id=?
                    WHERE incident_id=? AND status='active'
                    """,
                    (
                        resolved_us,
                        _iso_from_us(resolved_us),
                        "producer_restarted_then_authoritative_good_sample",
                        snapshot_id,
                        row["incident_id"],
                    ),
                )

    def finalize_idle(self, *, at: datetime | str | None = None) -> bool:
        """Close an open trip once its configured activity grace has elapsed."""

        moment = datetime.now(timezone.utc) if at is None else _utc_datetime(at, "at")
        at_us = _to_us(moment)
        idle_us = int(round(self.config.trip_idle_timeout_seconds * MICROSECONDS))
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM trips WHERE ended_us IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None or at_us - row["last_active_us"] < idle_us:
                return False
            self._conn.execute(
                """
                UPDATE trips SET ended_us=last_active_us,ended_at=last_active_at,
                    end_reason='activity_timeout' WHERE id=?
                """,
                (row["id"],),
            )
            return True

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
                return {
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
        if complete_end <= start:
            return {
                "buckets": 0,
                "rows": 0,
                "through": _iso_from_us(start),
                "backlog": False,
            }
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
        cursor = query_end if backlog else complete_end
        self._set_meta_locked(state_key, str(cursor))
        return {
            "buckets": len(selected),
            "rows": len(groups),
            "through": _iso_from_us(cursor),
            "backlog": backlog,
        }

    def _set_meta_locked(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO historian_meta(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def _meta_locked(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM historian_meta WHERE key=?",
            (key,),
        ).fetchone()
        return None if row is None else str(row["value"])

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
            with self._conn:
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
                if cursor_us is None or cursor_us < delete_before:
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

                limit = self.config.maintenance_max_delete_rows_per_table
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
                raw_backlog = (
                    metric_backlog
                    or interface_backlog
                    or self._has_orphan_snapshots_locked(delete_before)
                )
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

    def maybe_run_maintenance(
        self,
        *,
        now: datetime | str | None = None,
    ) -> MaintenanceResult:
        """Cheap hourly-callable hook; real work is metadata-gated to daily."""

        return self.run_maintenance(now=now, force=False)

    @staticmethod
    def _sample_dict(row: sqlite3.Row) -> dict[str, object]:
        if row["value_kind"] == "boolean":
            value: object = bool(row["value_bool"])
        elif row["value_kind"] == "number":
            value = row["value_num"]
        else:
            value = row["value_text"]
        return {
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
        )

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

    @staticmethod
    def _advisory_json(value: Mapping[str, object]) -> str:
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("advisory assessment must be JSON serializable") from exc

    @staticmethod
    def _advisory_context(assessment: Mapping[str, object]) -> dict[str, object]:
        """Select material context without volatile values or timestamps."""

        current = assessment.get("current")
        current = current if isinstance(current, Mapping) else {}
        baseline = assessment.get("baseline")
        baseline = baseline if isinstance(baseline, Mapping) else {}
        deviation = assessment.get("deviation")
        deviation = deviation if isinstance(deviation, Mapping) else {}
        plausibility = assessment.get("plausibility")
        plausibility = plausibility if isinstance(plausibility, Mapping) else {}
        return {
            "rule": assessment.get("rule"),
            "state": assessment.get("state"),
            "reason": assessment.get("reason"),
            "category": assessment.get("category"),
            "severity": assessment.get("severity"),
            "regime": assessment.get("regime"),
            "baseline_regime": assessment.get("baseline_regime"),
            "direction": assessment.get("direction"),
            "notification_eligible": assessment.get("notification_eligible"),
            "current": {
                key: current.get(key)
                for key in (
                    "metric",
                    "unit",
                    "source",
                    "bus",
                    "quality",
                    "provenance",
                    "role",
                    "resolution",
                    "role_reason",
                    "health",
                    "reason",
                    "controller_state",
                    "usb_serial",
                    "usb_dev_id",
                    "topology_generation",
                )
                if key in current
            },
            "baseline": {
                key: baseline.get(key)
                for key in (
                    "unit",
                    "quality",
                    "source",
                    "provenance",
                    "median",
                    "mad",
                    "robust_sigma",
                )
                if key in baseline
            },
            "threshold": deviation.get("threshold"),
            "plausibility_limit": {
                "maximum_delta_c": plausibility.get("maximum_delta_c"),
                "maximum_delta_window_seconds": plausibility.get(
                    "maximum_delta_window_seconds"
                ),
            },
            "absolute_threshold": assessment.get("absolute_threshold"),
            "reference_provenance": assessment.get("reference_provenance"),
        }

    @classmethod
    def _advisory_context_fingerprint(
        cls, assessment: Mapping[str, object]
    ) -> str:
        encoded = json.dumps(
            cls._advisory_context(assessment),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _insert_advisory_event_locked(
        self,
        *,
        episode_id: int,
        event_us: int,
        event_type: str,
        previous_state: str | None,
        new_state: str | None,
        context_fingerprint: str,
        assessment_json: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO advisory_episode_events(
                episode_id,event_us,event_at,event_type,previous_state,
                new_state,context_fingerprint,assessment_json
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                episode_id,
                event_us,
                _iso_from_us(event_us),
                event_type,
                previous_state,
                new_state,
                context_fingerprint,
                assessment_json,
            ),
        )
        return int(cursor.lastrowid)

    def _notification_due_locked(
        self,
        *,
        episode_id: int,
        rule_key: str,
        event_us: int,
        rate_limit_seconds: float,
    ) -> bool:
        pending = self._conn.execute(
            """
            SELECT 1 FROM advisory_notification_outbox
            WHERE episode_id=? AND status IN ('pending','failed') LIMIT 1
            """,
            (episode_id,),
        ).fetchone()
        if pending is not None:
            # One durable delivery owner per episode.  A periodic reminder
            # must not overtake a pending retry or resurrect a row that already
            # reached its terminal failed state.  Resolution/reopening creates
            # a new episode and therefore a new bounded owner.
            return False
        last = self._conn.execute(
            """
            SELECT max(coalesce(delivered_us,last_attempt_us,created_us))
            FROM advisory_notification_outbox
            WHERE rule_key=? AND status!='cancelled'
            """,
            (rule_key,),
        ).fetchone()[0]
        return last is None or event_us - int(last) >= int(
            round(rate_limit_seconds * MICROSECONDS)
        )

    @staticmethod
    def _notification_rate_limit(
        assessment: Mapping[str, object],
    ) -> float:
        rate_limit = assessment.get("notification_rate_limit_seconds", 30 * 60)
        if (
            isinstance(rate_limit, bool)
            or not isinstance(rate_limit, (int, float))
            or not math.isfinite(float(rate_limit))
            or not 60 <= float(rate_limit) <= 24 * 60 * 60
        ):
            raise ValueError(
                "notification_rate_limit_seconds must be between 60 and 86400"
            )
        return float(rate_limit)

    def _enqueue_advisory_notification_locked(
        self,
        *,
        episode: sqlite3.Row,
        event_id: int,
        event_us: int,
        context_fingerprint: str,
        assessment: Mapping[str, object],
        assessment_json: str,
    ) -> bool:
        rule_key = str(episode["rule_key"])
        rate_limit = self._notification_rate_limit(assessment)
        if not self._notification_due_locked(
            episode_id=int(episode["id"]),
            rule_key=rule_key,
            event_us=event_us,
            rate_limit_seconds=float(rate_limit),
        ):
            return False
        payload = {
            "schema_version": ADVISORY_SCHEMA_VERSION,
            "advisory": True,
            "episode_id": int(episode["id"]),
            "rule": rule_key,
            "category": episode["category"],
            "title": episode["title"],
            "state": assessment["state"],
            "reason": assessment.get("reason"),
            "opened_at": episode["opened_at"],
            "evaluated_at": _iso_from_us(event_us),
            "assessment": json.loads(assessment_json),
        }
        bucket_us = max(
            MICROSECONDS,
            int(round(float(rate_limit) * MICROSECONDS)),
        )
        dedupe_source = (
            f"{rule_key}:{episode['id']}:{event_us // bucket_us}:"
            f"{context_fingerprint}"
        )
        dedupe_key = hashlib.sha256(dedupe_source.encode()).hexdigest()
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO advisory_notification_outbox(
                episode_id,event_id,rule_key,dedupe_key,created_us,created_at,
                eligible_after_us,status,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                episode["id"],
                event_id,
                rule_key,
                dedupe_key,
                event_us,
                _iso_from_us(event_us),
                event_us,
                "pending",
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )
        if cursor.rowcount:
            self._conn.execute(
                "UPDATE advisory_episodes "
                "SET last_notification_enqueued_us=? WHERE id=?",
                (event_us, episode["id"]),
            )
            return True
        return False

    def record_advisory_assessments(
        self,
        assessments: Sequence[Mapping[str, object]],
        *,
        evaluated_at: datetime | str,
        authoritative_rule_keys: Sequence[str] | None = None,
    ) -> AdvisoryPersistenceResult:
        """Atomically open, update, or resolve advisory episodes.

        ``normal`` and ``suppressed`` are affirmative recovery states and may
        resolve an episode.  Missing history, stale evidence, and plausibility
        rejection are inconclusive: they retain any open episode while making
        that evidence state explicit.  Only an unacknowledged ``warning`` with
        ``notification_eligible=true`` enters the rate-limited outbox.
        """

        moment = _utc_datetime(evaluated_at, "evaluated_at")
        event_us = _to_us(moment)
        authoritative_rules: set[str] | None = None
        if authoritative_rule_keys is not None:
            if isinstance(authoritative_rule_keys, (str, bytes, bytearray)):
                raise ValueError("authoritative_rule_keys must be a sequence of rule keys")
            normalized_rules = tuple(authoritative_rule_keys)
            if any(
                not isinstance(rule, str) or not rule or len(rule) > 500
                for rule in normalized_rules
            ):
                raise ValueError(
                    "authoritative_rule_keys must contain bounded nonempty text"
                )
            if len(normalized_rules) != len(set(normalized_rules)):
                raise ValueError("authoritative_rule_keys must be unique")
            authoritative_rules = set(normalized_rules)
        counters = {
            "opened": 0,
            "updated": 0,
            "resolved": 0,
            "inconclusive": 0,
            "notifications_enqueued": 0,
        }
        seen: set[str] = set()
        with self._lock, self._conn:
            for index, assessment in enumerate(assessments):
                if not isinstance(assessment, Mapping):
                    raise ValueError(f"assessment[{index}] must be an object")
                rule_key = assessment.get("rule")
                title = assessment.get("title")
                state = assessment.get("state")
                if not isinstance(rule_key, str) or not rule_key:
                    raise ValueError(f"assessment[{index}].rule must be text")
                if rule_key in seen:
                    raise ValueError(f"duplicate advisory rule {rule_key!r}")
                seen.add(rule_key)
                if not isinstance(title, str) or not title:
                    raise ValueError(f"assessment[{index}].title must be text")
                allowed_states = (
                    ADVISORY_ACTIVE_STATES
                    | ADVISORY_RESOLVING_STATES
                    | ADVISORY_INCONCLUSIVE_STATES
                )
                if state not in allowed_states:
                    raise ValueError(
                        f"assessment[{index}].state {state!r} is unsupported"
                    )
                category = assessment.get("category", "vehicle_health")
                if not isinstance(category, str) or not category:
                    raise ValueError(f"assessment[{index}].category must be text")
                if assessment.get("advisory") is not True:
                    raise ValueError(f"assessment[{index}] must remain advisory")
                assessment_json = self._advisory_json(assessment)
                fingerprint = self._advisory_context_fingerprint(assessment)
                episode = self._conn.execute(
                    """
                    SELECT * FROM advisory_episodes
                    WHERE rule_key=? AND status='open'
                    """,
                    (rule_key,),
                ).fetchone()

                if state in ADVISORY_INCONCLUSIVE_STATES:
                    counters["inconclusive"] += 1
                    if episode is None:
                        continue
                    prior_evidence = str(episode["evidence_state"])
                    self._conn.execute(
                        """
                        UPDATE advisory_episodes
                        SET evidence_state=?,last_evaluated_us=?,
                            last_evaluated_at=?,latest_assessment_json=?,
                            latest_context_fingerprint=?,update_count=update_count+1
                        WHERE id=?
                        """,
                        (
                            state,
                            event_us,
                            _iso_from_us(event_us),
                            assessment_json,
                            fingerprint,
                            episode["id"],
                        ),
                    )
                    if prior_evidence != state:
                        self._insert_advisory_event_locked(
                            episode_id=int(episode["id"]),
                            event_us=event_us,
                            event_type="evidence_inconclusive",
                            previous_state=prior_evidence,
                            new_state=str(state),
                            context_fingerprint=fingerprint,
                            assessment_json=assessment_json,
                        )
                    counters["updated"] += 1
                    continue

                if state in ADVISORY_RESOLVING_STATES:
                    if episode is None:
                        continue
                    self._conn.execute(
                        """
                        UPDATE advisory_episodes
                        SET status='resolved',current_state=?,evidence_state=?,
                            last_evaluated_us=?,
                            last_evaluated_at=?,resolved_us=?,resolved_at=?,
                            resolution_reason=?,latest_assessment_json=?,
                            latest_context_fingerprint=?,update_count=update_count+1,
                            transition_count=transition_count+1
                        WHERE id=?
                        """,
                        (
                            state,
                            state,
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            assessment.get("reason") or f"assessment became {state}",
                            assessment_json,
                            fingerprint,
                            episode["id"],
                        ),
                    )
                    self._insert_advisory_event_locked(
                        episode_id=int(episode["id"]),
                        event_us=event_us,
                        event_type="resolved",
                        previous_state=str(episode["current_state"]),
                        new_state=str(state),
                        context_fingerprint=fingerprint,
                        assessment_json=assessment_json,
                    )
                    self._conn.execute(
                        """
                        UPDATE advisory_notification_outbox
                        SET status='cancelled',last_error='episode resolved before delivery'
                        WHERE episode_id=? AND status='pending'
                        """,
                        (episode["id"],),
                    )
                    counters["resolved"] += 1
                    continue

                assert state in ADVISORY_ACTIVE_STATES
                event_id: int | None = None
                if episode is None:
                    cursor = self._conn.execute(
                        """
                        INSERT INTO advisory_episodes(
                            rule_key,category,title,advisory,status,current_state,
                            evidence_state,opened_us,opened_at,last_evaluated_us,
                            last_evaluated_at,last_observed_us,last_observed_at,
                            first_assessment_json,latest_assessment_json,
                            latest_context_fingerprint
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            rule_key,
                            category,
                            title,
                            1,
                            "open",
                            state,
                            state,
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            assessment_json,
                            assessment_json,
                            fingerprint,
                        ),
                    )
                    episode_id = int(cursor.lastrowid)
                    event_id = self._insert_advisory_event_locked(
                        episode_id=episode_id,
                        event_us=event_us,
                        event_type="opened",
                        previous_state=None,
                        new_state=str(state),
                        context_fingerprint=fingerprint,
                        assessment_json=assessment_json,
                    )
                    episode = self._conn.execute(
                        "SELECT * FROM advisory_episodes WHERE id=?",
                        (episode_id,),
                    ).fetchone()
                    counters["opened"] += 1
                else:
                    previous_state = str(episode["current_state"])
                    prior_evidence = str(episode["evidence_state"])
                    previous_fingerprint = str(episode["latest_context_fingerprint"])
                    transitions = int(previous_state != state)
                    self._conn.execute(
                        """
                        UPDATE advisory_episodes
                        SET category=?,title=?,current_state=?,evidence_state=?,
                            last_evaluated_us=?,last_evaluated_at=?,
                            last_observed_us=?,last_observed_at=?,
                            observation_count=observation_count+1,
                            update_count=update_count+1,
                            transition_count=transition_count+?,
                            latest_assessment_json=?,latest_context_fingerprint=?
                        WHERE id=?
                        """,
                        (
                            category,
                            title,
                            state,
                            state,
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            transitions,
                            assessment_json,
                            fingerprint,
                            episode["id"],
                        ),
                    )
                    if previous_state != state:
                        event_type = (
                            "escalated" if state == "warning" else "deescalated"
                        )
                    elif prior_evidence in ADVISORY_INCONCLUSIVE_STATES:
                        event_type = "evidence_restored"
                    elif previous_fingerprint != fingerprint:
                        event_type = "context_updated"
                    else:
                        event_type = ""
                    if event_type:
                        event_id = self._insert_advisory_event_locked(
                            episode_id=int(episode["id"]),
                            event_us=event_us,
                            event_type=event_type,
                            previous_state=previous_state,
                            new_state=str(state),
                            context_fingerprint=fingerprint,
                            assessment_json=assessment_json,
                        )
                    episode = self._conn.execute(
                        "SELECT * FROM advisory_episodes WHERE id=?",
                        (episode["id"],),
                    ).fetchone()
                    counters["updated"] += 1

                notification_eligible = (
                    state == "warning"
                    and assessment.get("notification_eligible") is True
                    and episode["acknowledged_us"] is None
                )
                if notification_eligible:
                    rate_limit = self._notification_rate_limit(assessment)
                    if not self._notification_due_locked(
                        episode_id=int(episode["id"]),
                        rule_key=rule_key,
                        event_us=event_us,
                        rate_limit_seconds=rate_limit,
                    ):
                        continue
                    if event_id is None:
                        event_id = self._insert_advisory_event_locked(
                            episode_id=int(episode["id"]),
                            event_us=event_us,
                            event_type="notification_repeat_due",
                            previous_state=str(state),
                            new_state=str(state),
                            context_fingerprint=fingerprint,
                            assessment_json=assessment_json,
                        )
                    if self._enqueue_advisory_notification_locked(
                        episode=episode,
                        event_id=event_id,
                        event_us=event_us,
                        context_fingerprint=fingerprint,
                        assessment=assessment,
                        assessment_json=assessment_json,
                    ):
                        counters["notifications_enqueued"] += 1
            if authoritative_rules is not None:
                if seen != authoritative_rules:
                    raise ValueError(
                        "authoritative_rule_keys must exactly match the evaluated assessments"
                    )
                open_rows = self._conn.execute(
                    "SELECT * FROM advisory_episodes WHERE status='open'"
                ).fetchall()
                for episode in open_rows:
                    if str(episode["rule_key"]) in authoritative_rules:
                        continue
                    reason = "rule retired from authoritative evaluator catalog"
                    try:
                        retired_assessment = json.loads(
                            episode["latest_assessment_json"]
                        )
                    except (TypeError, json.JSONDecodeError):
                        retired_assessment = {}
                    if not isinstance(retired_assessment, dict):
                        retired_assessment = {}
                    retired_assessment.update(
                        {
                            "rule": episode["rule_key"],
                            "title": episode["title"],
                            "category": episode["category"],
                            "advisory": True,
                            "state": "suppressed",
                            "reason": reason,
                            "notification_eligible": False,
                        }
                    )
                    assessment_json = self._advisory_json(retired_assessment)
                    fingerprint = self._advisory_context_fingerprint(
                        retired_assessment
                    )
                    self._conn.execute(
                        """
                        UPDATE advisory_episodes
                        SET status='resolved',current_state='suppressed',
                            evidence_state='suppressed',
                            last_evaluated_us=?,last_evaluated_at=?,
                            resolved_us=?,resolved_at=?,resolution_reason=?,
                            latest_assessment_json=?,latest_context_fingerprint=?,
                            update_count=update_count+1,
                            transition_count=transition_count+1
                        WHERE id=? AND status='open'
                        """,
                        (
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            reason,
                            assessment_json,
                            fingerprint,
                            episode["id"],
                        ),
                    )
                    self._insert_advisory_event_locked(
                        episode_id=int(episode["id"]),
                        event_us=event_us,
                        event_type="rule_retired",
                        previous_state=str(episode["current_state"]),
                        new_state="suppressed",
                        context_fingerprint=fingerprint,
                        assessment_json=assessment_json,
                    )
                    self._conn.execute(
                        """
                        UPDATE advisory_notification_outbox
                        SET status='cancelled',
                            last_error='rule retired before delivery'
                        WHERE episode_id=? AND status='pending'
                        """,
                        (episode["id"],),
                    )
                    counters["resolved"] += 1
        return AdvisoryPersistenceResult(
            evaluated_at=_iso(moment),
            **counters,
        )

    @staticmethod
    def _advisory_episode_dict(row: sqlite3.Row, now_us: int) -> dict[str, object]:
        end_us = row["resolved_us"] if row["resolved_us"] is not None else now_us
        return {
            "id": row["id"],
            "rule": row["rule_key"],
            "category": row["category"],
            "title": row["title"],
            "advisory": bool(row["advisory"]),
            "status": row["status"],
            "state": row["current_state"],
            "evidence_state": row["evidence_state"],
            "opened_at": row["opened_at"],
            "last_evaluated_at": row["last_evaluated_at"],
            "last_observed_at": row["last_observed_at"],
            "resolved_at": row["resolved_at"],
            "resolution_reason": row["resolution_reason"],
            "duration_seconds": max(0.0, (end_us - row["opened_us"]) / MICROSECONDS),
            "observation_count": row["observation_count"],
            "update_count": row["update_count"],
            "transition_count": row["transition_count"],
            "acknowledged": row["acknowledged_us"] is not None,
            "acknowledged_at": row["acknowledged_at"],
            "acknowledgment_note": row["acknowledgment_note"],
            "latest_assessment": json.loads(row["latest_assessment_json"]),
        }

    def list_advisory_episodes(
        self,
        *,
        active_only: bool = False,
        limit: int = 100,
        now: datetime | str | None = None,
    ) -> list[dict[str, object]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        moment = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        where = "WHERE status='open'" if active_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM advisory_episodes {where} "
                "ORDER BY opened_us DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        now_us = _to_us(moment)
        return [self._advisory_episode_dict(row, now_us) for row in rows]

    def list_advisory_events(
        self,
        episode_id: int,
        *,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        if not isinstance(episode_id, int) or isinstance(episode_id, bool) or episode_id < 1:
            raise ValueError("episode_id must be a positive integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM advisory_episode_events WHERE episode_id=?
                ORDER BY event_us DESC,id DESC LIMIT ?
                """,
                (episode_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "episode_id": row["episode_id"],
                "at": row["event_at"],
                "type": row["event_type"],
                "previous_state": row["previous_state"],
                "new_state": row["new_state"],
                "assessment": json.loads(row["assessment_json"]),
            }
            for row in rows
        ]

    def acknowledge_advisory_episode(
        self,
        episode_id: int,
        *,
        acknowledged_at: datetime | str | None = None,
        note: str | None = None,
    ) -> dict[str, object]:
        if not isinstance(episode_id, int) or isinstance(episode_id, bool) or episode_id < 1:
            raise ValueError("episode_id must be a positive integer")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise ValueError("acknowledgment note must be text of at most 500 characters")
        moment = (
            datetime.now(timezone.utc)
            if acknowledged_at is None
            else _utc_datetime(acknowledged_at, "acknowledged_at")
        )
        at_us = _to_us(moment)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM advisory_episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown advisory episode {episode_id}")
            if row["acknowledged_us"] is None:
                self._conn.execute(
                    """
                    UPDATE advisory_episodes
                    SET acknowledged_us=?,acknowledged_at=?,acknowledgment_note=?
                    WHERE id=?
                    """,
                    (at_us, _iso(moment), note, episode_id),
                )
                self._insert_advisory_event_locked(
                    episode_id=episode_id,
                    event_us=at_us,
                    event_type="acknowledged",
                    previous_state=row["current_state"],
                    new_state=row["current_state"],
                    context_fingerprint=row["latest_context_fingerprint"],
                    assessment_json=row["latest_assessment_json"],
                )
                self._conn.execute(
                    """
                    UPDATE advisory_notification_outbox
                    SET status='cancelled',last_error='episode acknowledged before delivery'
                    WHERE episode_id=? AND status='pending'
                    """,
                    (episode_id,),
                )
            updated = self._conn.execute(
                "SELECT * FROM advisory_episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
        return self._advisory_episode_dict(updated, at_us)

    def pending_advisory_notifications(
        self,
        *,
        at: datetime | str | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        moment = datetime.now(timezone.utc) if at is None else _utc_datetime(at, "at")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT outbox.* FROM advisory_notification_outbox AS outbox
                JOIN advisory_episodes AS episode ON episode.id=outbox.episode_id
                WHERE outbox.status='pending' AND outbox.eligible_after_us<=?
                  AND episode.status='open' AND episode.acknowledged_us IS NULL
                ORDER BY outbox.created_us,outbox.id LIMIT ?
                """,
                (_to_us(moment), limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "episode_id": row["episode_id"],
                "rule": row["rule_key"],
                "created_at": row["created_at"],
                "attempt_count": row["attempt_count"],
                "last_error": row["last_error"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def mark_advisory_notification_delivered(
        self,
        notification_id: int,
        *,
        delivered_at: datetime | str | None = None,
    ) -> None:
        moment = (
            datetime.now(timezone.utc)
            if delivered_at is None
            else _utc_datetime(delivered_at, "delivered_at")
        )
        at_us = _to_us(moment)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE advisory_notification_outbox
                SET status='delivered',attempt_count=attempt_count+1,
                    last_attempt_us=?,last_attempt_at=?,delivered_us=?,
                    delivered_at=?,last_error=NULL
                WHERE id=? AND status='pending'
                """,
                (at_us, _iso(moment), at_us, _iso(moment), notification_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"pending advisory notification {notification_id} not found")

    def mark_advisory_notification_failed(
        self,
        notification_id: int,
        *,
        error: str,
        attempted_at: datetime | str | None = None,
        retry_after_seconds: float = 300,
        max_attempts: int = 3,
    ) -> None:
        if not isinstance(error, str) or not error or len(error) > 1000:
            raise ValueError("notification error must be nonempty text of at most 1000 characters")
        if (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not math.isfinite(float(retry_after_seconds))
            or not 1 <= retry_after_seconds <= 24 * 60 * 60
        ):
            raise ValueError("retry_after_seconds must be between 1 and 86400")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        moment = (
            datetime.now(timezone.utc)
            if attempted_at is None
            else _utc_datetime(attempted_at, "attempted_at")
        )
        at_us = _to_us(moment)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM advisory_notification_outbox WHERE id=? AND status='pending'",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"pending advisory notification {notification_id} not found")
            attempts = int(row["attempt_count"]) + 1
            terminal = attempts >= max_attempts
            self._conn.execute(
                """
                UPDATE advisory_notification_outbox
                SET status=?,attempt_count=?,last_attempt_us=?,last_attempt_at=?,
                    eligible_after_us=?,last_error=? WHERE id=?
                """,
                (
                    "failed" if terminal else "pending",
                    attempts,
                    at_us,
                    _iso(moment),
                    at_us + int(round(retry_after_seconds * MICROSECONDS)),
                    error,
                    notification_id,
                ),
            )

    def advisory_summary(
        self,
        *,
        now: datetime | str | None = None,
        recent_limit: int = 25,
    ) -> dict[str, object]:
        moment = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        with self._lock:
            counts = {
                row["status"]: row["count"]
                for row in self._conn.execute(
                    """
                    SELECT status,count(*) AS count
                    FROM advisory_notification_outbox GROUP BY status
                    """
                )
            }
        return {
            "schema_version": ADVISORY_SCHEMA_VERSION,
            "generated_at": _iso(moment),
            "active": self.list_advisory_episodes(
                active_only=True,
                limit=recent_limit,
                now=moment,
            ),
            "recent": self.list_advisory_episodes(
                active_only=False,
                limit=recent_limit,
                now=moment,
            ),
            "notification_outbox": {
                "pending": counts.get("pending", 0),
                "delivered": counts.get("delivered", 0),
                "failed": counts.get("failed", 0),
                "cancelled": counts.get("cancelled", 0),
            },
        }

    def _active_gap_count_threadsafe(self, table: str) -> int:
        with self._lock:
            return self._active_gap_count(table)
