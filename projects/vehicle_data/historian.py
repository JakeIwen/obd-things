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

from lib.timeutil import finite_number as _finite_number


# Compatibility surface: direct re-exports, including historical private helpers.
from .history.models import (
    ADVISORY_ACTIVE_STATES,
    ADVISORY_INCONCLUSIVE_STATES,
    ADVISORY_RESOLVING_STATES,
    ADVISORY_SCHEMA_VERSION,
    AdvisoryPersistenceResult,
    BaselineStats,
    DATA_QUALITY_SCHEMA_VERSION,
    DEFAULT_DATABASE,
    HistorianConfig,
    HistorianError,
    IngestResult,
    MAX_NOTIFICATION_RATE_LIMIT_SECONDS,
    MAX_PERIOD_DAYS,
    MAX_QUERY_SAMPLES,
    MAX_SERIES_POINTS,
    MAX_SERIES_WINDOW_SECONDS,
    MICROSECONDS,
    MaintenanceResult,
    NOTIFICATION_RANK_SQL,
    OutOfOrderSnapshotError,
    REGIME_DIMENSIONS,
    SCHEMA_VERSION,
    SYSTEM_NOTE_CATEGORIES,
    SnapshotValidationError,
    TRIP_NONCRITICAL_PUSH_CAP,
    _MetricDefinition,
    _MetricSample,
    _SourceDefinition,
)

from .history.validation import (
    _bool_db,
    _is_placeholder_interface_role,
    _iso,
    _iso_from_us,
    _median_mad,
    _optional_nonnegative_int,
    _required_text,
    _to_us,
    _utc_datetime,
    project_regime,
)

from .history.advisories import AdvisoryMixin as _AdvisoryMixin
from .history.ingest import IngestMixin as _IngestMixin
from .history.queries import QueryMixin as _QueryMixin
from .history.rollups import RollupMixin as _RollupMixin
from .history.store import HistorianStore as _HistorianStore


class TelemetryHistorian(
    _HistorianStore,
    _IngestMixin,
    _RollupMixin,
    _QueryMixin,
    _AdvisoryMixin,
):
    """Transactional, offline telemetry historian.

    A single instance may be shared by threads.  Snapshot time must be
    monotonic across successful ingests because trip and gap intervals are
    stateful.  Replaying the same delivery key is idempotent.
    """

    # Keep the self-referential annotation resolvable in the public module.
    def __enter__(self) -> "TelemetryHistorian":
        return self
