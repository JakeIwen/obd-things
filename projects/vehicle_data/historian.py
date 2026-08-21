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
DEFAULT_DATABASE = Path("/var/lib/van-telemetry/history.sqlite3")
MAX_QUERY_SAMPLES = 2_000
MAX_SERIES_POINTS = 512
MAX_SERIES_WINDOW_SECONDS = 31 * 24 * 60 * 60
MAX_PERIOD_DAYS = 30
MICROSECONDS = 1_000_000
REGIME_DIMENSIONS = ("engine", "motion", "rpm", "thermal")


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
    def _interface_payloads(snapshot: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
        status = snapshot.get("status")
        if not isinstance(status, Mapping):
            return {}
        multiple = status.get("interfaces")
        if isinstance(multiple, Mapping):
            return {
                str(role): payload
                for role, payload in multiple.items()
                if isinstance(payload, Mapping)
            }
        single = status.get("interface")
        if not isinstance(single, Mapping):
            return {}
        role_snapshot = single.get("role_interfaces")
        role_payloads = (
            role_snapshot.get("roles")
            if isinstance(role_snapshot, Mapping)
            else None
        )
        if isinstance(role_payloads, Mapping):
            normalized: dict[str, Mapping[str, object]] = {}
            for role, payload in role_payloads.items():
                if not isinstance(role, str) or not isinstance(payload, Mapping):
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
                    "channel": payload.get("channel"),
                    "usb_serial": expected.get("usb_serial"),
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
            if normalized:
                return normalized
        topology = single.get("topology")
        bus = topology.get("bus") if isinstance(topology, Mapping) else None
        role = (
            bus
            if isinstance(bus, str) and bus not in ("", "unknown")
            else single.get("role")
        )
        if not isinstance(role, str) or not role:
            role = single.get("channel")
        if not isinstance(role, str) or not role:
            role = "default"
        return {role: single}

    @staticmethod
    def _interface_health(payload: Mapping[str, object]) -> tuple[str, str]:
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
        if not interfaces:
            self._update_gap(
                table="interface_gaps",
                key_column="role",
                key="interface-status",
                gap=("missing", "interface_status_missing"),
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )
            previously_seen = {
                row[0]
                for row in self._conn.execute("SELECT DISTINCT role FROM interface_samples")
            }
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
        previously_seen = {
            row[0]
            for row in self._conn.execute("SELECT DISTINCT role FROM interface_samples")
        }
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
            gap = None if health == "healthy" else (health, reason)
            self._update_gap(
                table="interface_gaps",
                key_column="role",
                key=role,
                gap=gap,
                captured_us=captured_us,
                snapshot_id=snapshot_id,
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
        if row is None or row["sample_count"] == 0:
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

    def _active_gap_count_threadsafe(self, table: str) -> int:
        with self._lock:
            return self._active_gap_count(table)
