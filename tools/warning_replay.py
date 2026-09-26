#!/usr/bin/env python3
"""Offline early-warning replay against a bounded, read-only historian export.

No CAN access, no service control, no writes to the live historian.

Subcommands (all output under tmp/vehicle_data/warning_replay/):

``export``  Copy the trailing ``--days`` of trip snapshots and rule-metric
            samples, the fresh parked battery bursts, every rule-metric
            rollup, all trips and the recorded advisory outcomes from the live
            historian into one scratch SQLite file.  The source is opened with
            ``mode=ro`` + ``PRAGMA query_only``; every statement is indexed,
            bounded and interrupted by a progress-handler deadline.
``replay``  Copy the export and replay the ``current`` evaluator (loaded from a
            git ref) and/or the ``new`` evaluator (worktree) tick by tick
            through the real historian lifecycle, simulating notification
            delivery so cooldowns key on ``delivered_us`` exactly as live.
            A run that hits its budget can be continued with
            ``replay --resume <replay-*.json>``: same copy, next tick, fresh
            per-run budget (each run stays inside the CPU/RSS limits).
``coarse``  Count tier-0 conditions on 60 s rollup buckets (trips without raw
            rows, plus the replayed trips as a cross-check).
``report``  Merge the latest outputs into ``report-<stamp>.md`` / ``.json``.

The replay is deliberately light (default budget 90 s CPU / 300 MB RSS); it
aborts cleanly with a partial report flagged ``"complete": false`` and exit
code 3 rather than running heavy on the Pi.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import heapq
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import projects.vehicle_data.historian as historian_module  # noqa: E402
from projects.vehicle_data.historian import (  # noqa: E402
    REGIME_DIMENSIONS,
    BaselineStats,
    HistorianConfig,
    TelemetryHistorian,
)

DEFAULT_DATABASE = Path("/var/lib/van-telemetry/history.sqlite3")
OUT_DIR = REPO / "tmp" / "vehicle_data" / "warning_replay"
DEFAULT_EXPORT = OUT_DIR / "export.sqlite3"
EVALUATOR_PATH = "projects/vehicle_data/early_warning.py"
RULE_METRICS = (
    "engine.rpm",
    "vehicle.speed",
    "engine.coolant_temperature",
    "engine.oil_pressure",
    "transmission.oil_temperature",
    "battery.voltage",
    "generator.field_duty",
    "tire.pressure.fl",
    "tire.pressure.fr",
    "tire.pressure.rl",
    "tire.pressure.rr",
)
US = 1_000_000
HOUR_US = 3_600 * US
DAY_US = 86_400 * US
BUCKET_US = 60 * US
TRIP_TAIL_US = 600 * US
STATEMENT_DEADLINE_SECONDS = 10.0
EXIT_INCOMPLETE = 3
GROUPS = ("oil", "cooling", "transmission", "charging", "battery", "tires", "system", "custom")
FAMILY_ORDER = GROUPS + ("other",)
VEHICLE_FAMILIES = ("oil", "cooling", "transmission", "charging", "battery", "tires", "custom")
PERSISTENCE_COUNTERS = (
    "opened",
    "updated",
    "resolved",
    "inconclusive",
    "notifications_enqueued",
    "notifications_capped",
    "notifications_deferred",
)
TIER0_KEYS = (
    "engine_oil_pressure_absolute_critical",
    "engine_oil_pressure_below_band",
    "engine_coolant_temperature_hot",
    "transmission_oil_temperature_hot",
    "battery_voltage_charging_failure",
    "battery_voltage_low_parked",
    "tire_pressure_low_absolute",
    "tire_pressure_pair_asymmetry",
)
ACCEPTANCE_KEYS = (
    "tier0_fires_only_parked_battery",
    "tier1_le_1_warning_per_family_per_trip",
    "rr_slow_leak_notice",
)
COMPARISON_SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_recorded_episodes(
    id INTEGER PRIMARY KEY,
    rule_key TEXT NOT NULL,
    category TEXT,
    title TEXT,
    status TEXT,
    current_state TEXT,
    opened_us INTEGER NOT NULL,
    opened_at TEXT,
    resolved_us INTEGER,
    resolved_at TEXT,
    resolution_reason TEXT,
    opened_state TEXT,
    first_warning_us INTEGER,
    trip_id INTEGER,
    opened_regime TEXT,
    rule_revision TEXT,
    evaluator_revision TEXT
);
CREATE TABLE IF NOT EXISTS replay_recorded_outbox(
    id INTEGER PRIMARY KEY,
    episode_id INTEGER,
    rule_key TEXT,
    status TEXT,
    created_us INTEGER,
    delivered_us INTEGER,
    notification_kind TEXT
);
CREATE TABLE IF NOT EXISTS replay_manifest(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DeadlineExceeded(RuntimeError):
    """A bounded read exceeded its per-statement deadline."""


class BudgetExceeded(RuntimeError):
    """The replay exceeded its CPU or RSS budget."""


# --------------------------------------------------------------------------
# small helpers


def _now_us() -> int:
    return int(time.time() * US)


def _to_us(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(round(value.timestamp() * US))


def _dt(us: int) -> datetime:
    return datetime.fromtimestamp(us / US, timezone.utc)


def _iso(us: int | None) -> str | None:
    return None if us is None else _dt(int(us)).isoformat()


def _stamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"


def _usage(cpu0: float = 0.0) -> dict[str, float]:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports ru_maxrss in KiB; macOS (the pi_compute workers) in bytes.
    peak_mb = peak / (1024.0 * 1024.0) if sys.platform == "darwin" else peak / 1024.0
    return {
        "cpu_seconds": round(time.process_time() - cpu0, 3),
        "peak_rss_mb": round(peak_mb, 1),
    }


def _git(*args: str, strip: bool = True) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() if strip else done.stdout


def _remove_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def family_of(rule_key: str | None, group: object = None) -> str:
    """Map a rule key (legacy or tiered) to its notification family."""

    if isinstance(group, str) and group in GROUPS:
        return group
    key = rule_key or ""
    if key.startswith("tire_pressure_"):
        return "tires"
    if key.startswith("engine_oil_pressure_"):
        return "oil"
    if key.startswith("engine_coolant_"):
        return "cooling"
    if key.startswith("transmission_oil_"):
        return "transmission"
    if key == "battery_voltage_low_parked":
        return "battery"
    if key.startswith("battery_voltage_"):
        return "charging"
    if key.startswith(("can_", "usb_", "telemetry_gap_")):
        return "system"
    if key.startswith("custom_"):
        return "custom"
    return "other"


def _parked_regime(regime: object) -> bool:
    return isinstance(regime, str) and regime.startswith(("engine_off", "engine_unknown"))


# --------------------------------------------------------------------------
# export


class ReadOnlySource:
    """Query-only connection with a per-statement progress-handler deadline."""

    def __init__(
        self,
        database: str | Path,
        *,
        deadline_seconds: float = STATEMENT_DEADLINE_SECONDS,
        progress_interval: int = 500,
    ):
        path = Path(database).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(str(path))
        self.path = path
        self.uri = path.as_uri() + "?mode=ro"
        self.deadline_seconds = float(deadline_seconds)
        self.conn = sqlite3.connect(self.uri, uri=True, timeout=10.0, isolation_level=None)
        self.conn.execute("PRAGMA query_only=ON")
        self._started: float | None = None
        self.statements = 0
        self.max_statement_seconds = 0.0
        self.conn.set_progress_handler(self._progress, progress_interval)

    def _progress(self) -> int:
        if self._started is not None and time.monotonic() - self._started > self.deadline_seconds:
            return 1
        return 0

    def query(self, sql: str, args: tuple | list = ()) -> list[tuple]:
        self._started = time.monotonic()
        try:
            rows = self.conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise DeadlineExceeded(
                    f"read exceeded the {self.deadline_seconds:g} s statement deadline: {sql.split()[0:6]}"
                ) from exc
            raise
        finally:
            elapsed = time.monotonic() - self._started
            self._started = None
            self.statements += 1
            self.max_statement_seconds = max(self.max_statement_seconds, elapsed)
        return rows

    def begin(self) -> None:
        # One read transaction: every read sees the same WAL snapshot.
        self.conn.execute("BEGIN")

    def columns(self, table: str) -> list[str]:
        return [row[1] for row in self.query(f"PRAGMA table_info({table})")]

    def close(self) -> None:
        try:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        finally:
            self.conn.close()


def open_readonly(database: str | Path, **kwargs) -> ReadOnlySource:
    return ReadOnlySource(database, **kwargs)


def _common_columns(source: ReadOnlySource, dest: sqlite3.Connection, table: str) -> list[str]:
    wanted = [row[1] for row in dest.execute(f"PRAGMA table_info({table})")]
    have = set(source.columns(table))
    return [name for name in wanted if name in have]


def _insert(dest: sqlite3.Connection, table: str, columns: list[str], rows: list[tuple], *, ignore: bool = True) -> int:
    if not rows:
        return 0
    verb = "INSERT OR IGNORE" if ignore else "INSERT"
    marks = ",".join("?" for _ in columns)
    before = dest.total_changes
    dest.executemany(f"{verb} INTO {table}({','.join(columns)}) VALUES({marks})", rows)
    return dest.total_changes - before


def read_manifest(path: str | Path) -> dict[str, object]:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT key,value FROM replay_manifest").fetchall()
    finally:
        conn.close()
    return {key: json.loads(value) for key, value in rows}


def export(
    database: str | Path = DEFAULT_DATABASE,
    out: str | Path = DEFAULT_EXPORT,
    *,
    days: int = 7,
    force: bool = False,
    now: datetime | None = None,
    deadline_seconds: float = STATEMENT_DEADLINE_SECONDS,
) -> dict[str, object]:
    """Write the bounded scratch export and return its manifest."""

    cpu0 = time.process_time()
    wall0 = time.monotonic()
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        raise ValueError("days must be an integer between 1 and 30")
    out = Path(out)
    if out.exists() and not force:
        raise FileExistsError(f"{out} exists; pass --force to replace it")
    now_us = _now_us() if now is None else _to_us(now)
    cutoff_us = now_us - days * DAY_US
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    _remove_sqlite(partial)
    source = ReadOnlySource(database, deadline_seconds=deadline_seconds)
    counts: Counter = Counter()
    manifest: dict[str, object] = {}
    try:
        TelemetryHistorian(partial).close()
        dest = sqlite3.connect(partial, isolation_level=None)
        try:
            dest.execute("PRAGMA foreign_keys=OFF")
            dest.executescript(COMPARISON_SCHEMA)
            source.begin()
            dest.execute("BEGIN")
            cols = {
                table: _common_columns(source, dest, table)
                for table in ("trips", "snapshots", "metric_samples", "metric_rollups")
            }
            sel = {table: ",".join(names) for table, names in cols.items()}

            # 1. trips: all rows, an open trip is copied as is.
            trip_rows = source.query(f"SELECT {sel['trips']} FROM trips ORDER BY id")
            counts["trips"] = _insert(dest, "trips", cols["trips"], trip_rows)
            trip_index = {name: i for i, name in enumerate(cols["trips"])}

            # 2. meta: live schema version and rollup cursor verbatim.
            live_meta = dict(
                source.query(
                    "SELECT key,value FROM historian_meta WHERE key IN ('schema_version','rollup_through_us:60')"
                )
            )
            meta = {
                **live_meta,
                "maintenance_last_status": "completed",
                "maintenance_last_success_us": str(now_us),
                "maintenance_last_retention_days": "7",
            }
            dest.executemany(
                "INSERT INTO historian_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                sorted(meta.items()),
            )

            # 3. rollups for the rule metrics, one indexed query per metric.
            for metric in RULE_METRICS:
                rows = source.query(
                    f"SELECT {sel['metric_rollups']} FROM metric_rollups WHERE metric=?",
                    (metric,),
                )
                counts["metric_rollups"] += _insert(dest, "metric_rollups", cols["metric_rollups"], rows)

            # 4. raw rows for trips inside the window.
            window = [
                row for row in trip_rows if int(row[trip_index["started_us"]]) >= cutoff_us
            ]
            window_ids = [int(row[trip_index["id"]]) for row in window]
            for row in window:
                trip_id = int(row[trip_index["id"]])
                start = int(row[trip_index["started_us"]])
                ended = row[trip_index["ended_us"]]
                end = int(ended if ended is not None else row[trip_index["last_active_us"]]) + TRIP_TAIL_US
                snaps = source.query(
                    f"SELECT {sel['snapshots']} FROM snapshots "
                    "WHERE trip_id=? AND captured_us BETWEEN ? AND ?",
                    (trip_id, start, end),
                )
                counts["trip_snapshots"] += _insert(dest, "snapshots", cols["snapshots"], snaps)
                for metric in RULE_METRICS:
                    samples = source.query(
                        f"SELECT {sel['metric_samples']} FROM metric_samples "
                        "WHERE trip_id=? AND metric=? AND captured_us BETWEEN ? AND ?",
                        (trip_id, metric, start, end),
                    )
                    counts["trip_metric_samples"] += _insert(
                        dest, "metric_samples", cols["metric_samples"], samples
                    )

            # 5. fresh parked battery bursts (trip_id NULL) plus their snapshots
            #    and RPM rows, so parked gates see the absent/low RPM.
            sample_index = {name: i for i, name in enumerate(cols["metric_samples"])}
            regimes = [
                row[0]
                for row in source.query(
                    "SELECT DISTINCT regime FROM metric_rollups "
                    "WHERE metric='battery.voltage' AND regime NOT LIKE 'engine_running:%'"
                )
            ]
            parked_rows: list[tuple] = []
            for regime in regimes:
                rows = source.query(
                    f"SELECT {sel['metric_samples']} FROM metric_samples "
                    "WHERE metric='battery.voltage' AND regime=? AND freshness='fresh' AND captured_us>=?",
                    (regime, cutoff_us),
                )
                parked_rows.extend(r for r in rows if r[sample_index["trip_id"]] is None)
            counts["parked_battery_samples"] = _insert(
                dest, "metric_samples", cols["metric_samples"], parked_rows
            )
            snapshot_ids = sorted({int(r[sample_index["snapshot_id"]]) for r in parked_rows})
            for snapshot_id in snapshot_ids:
                snap = source.query(
                    f"SELECT {sel['snapshots']} FROM snapshots WHERE id=?", (snapshot_id,)
                )
                counts["parked_snapshots"] += _insert(dest, "snapshots", cols["snapshots"], snap)
                rpm = source.query(
                    f"SELECT {sel['metric_samples']} FROM metric_samples "
                    "WHERE snapshot_id=? AND metric='engine.rpm'",
                    (snapshot_id,),
                )
                counts["parked_rpm_samples"] += _insert(dest, "metric_samples", cols["metric_samples"], rpm)

            # 6. comparison tables from the (small) advisory tables.
            first_warning = dict(
                source.query(
                    "SELECT episode_id,min(event_us) FROM advisory_episode_events "
                    "WHERE new_state='warning' GROUP BY episode_id"
                )
            )
            episodes = source.query(
                """
                SELECT id,rule_key,category,title,status,current_state,opened_us,opened_at,
                       resolved_us,resolved_at,resolution_reason,
                       json_extract(first_assessment_json,'$.state'),
                       json_extract(first_assessment_json,'$.current.trip_id'),
                       json_extract(first_assessment_json,'$.regime'),
                       json_extract(first_assessment_json,'$.rule_revision'),
                       json_extract(first_assessment_json,'$.evaluator_revision')
                FROM advisory_episodes ORDER BY id
                """
            )
            recorded = []
            for row in episodes:
                trip = row[12]
                trip = int(trip) if isinstance(trip, (int, float)) and not isinstance(trip, bool) else None
                recorded.append(
                    (*row[:12], first_warning.get(row[0]), trip, row[13], row[14], row[15])
                )
            counts["replay_recorded_episodes"] = _insert(
                dest,
                "replay_recorded_episodes",
                [
                    "id", "rule_key", "category", "title", "status", "current_state",
                    "opened_us", "opened_at", "resolved_us", "resolved_at",
                    "resolution_reason", "opened_state", "first_warning_us", "trip_id",
                    "opened_regime", "rule_revision", "evaluator_revision",
                ],
                recorded,
            )
            outbox = source.query(
                """
                SELECT id,episode_id,rule_key,status,created_us,delivered_us,
                       json_extract(payload_json,'$.notification_kind')
                FROM advisory_notification_outbox ORDER BY id
                """
            )
            counts["replay_recorded_outbox"] = _insert(
                dest,
                "replay_recorded_outbox",
                ["id", "episode_id", "rule_key", "status", "created_us", "delivered_us", "notification_kind"],
                outbox,
            )
            table_counts = {
                table: dest.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "trips", "snapshots", "metric_samples", "metric_rollups",
                    "replay_recorded_episodes", "replay_recorded_outbox",
                )
            }
            manifest = {
                "exported_at": _iso(now_us),
                "exported_us": now_us,
                "database": str(source.path),
                "source_uri": source.uri,
                "days": days,
                "cutoff_us": cutoff_us,
                "cutoff_at": _iso(cutoff_us),
                "trip_ids": window_ids,
                "rollup_only_trip_ids": [
                    int(row[trip_index["id"]]) for row in trip_rows if int(row[trip_index["id"]]) not in window_ids
                ],
                "rule_metrics": list(RULE_METRICS),
                "parked_battery_regimes": regimes,
                "table_counts": table_counts,
                "copied_rows": dict(sorted(counts.items())),
                "rollup_through_us": live_meta.get("rollup_through_us:60"),
                "git_head": _git("rev-parse", "HEAD"),
                "statement_count": source.statements,
                "max_statement_seconds": round(source.max_statement_seconds, 4),
                "statement_deadline_seconds": deadline_seconds,
                "note": (
                    "trip snapshots and rule-metric samples for trips started inside the window, "
                    "fresh parked battery rows with their snapshots and RPM rows, all rule-metric "
                    "rollups, all trips; the ~5 s parked snapshots are deliberately omitted"
                ),
            }
            dest.executemany(
                "INSERT OR REPLACE INTO replay_manifest(key,value) VALUES(?,?)",
                [(key, json.dumps(value, sort_keys=True)) for key, value in manifest.items()],
            )
            dest.execute("COMMIT")
            dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dest.execute("PRAGMA journal_mode=DELETE")
        finally:
            if dest.in_transaction:
                dest.execute("ROLLBACK")
            dest.close()
    except BaseException:
        source.close()
        _remove_sqlite(partial)
        raise
    source.close()
    _remove_sqlite(out)
    os.replace(partial, out)
    manifest = dict(manifest)
    manifest["export_path"] = str(out)
    manifest["export_bytes"] = out.stat().st_size
    manifest["export_mb"] = round(out.stat().st_size / (1 << 20), 2)
    manifest["wall_seconds"] = round(time.monotonic() - wall0, 2)
    manifest.update(_usage(cpu0))
    return manifest


# --------------------------------------------------------------------------
# replay


@contextmanager
def pure_function_caches(*modules):
    """Memoize pure historian helpers for the duration of one replay.

    ``project_regime`` and ``_iso_from_us`` are pure functions of their
    arguments; the evaluator calls them thousands of times per tick on the same
    rows.  Unhashable arguments fall back to the original function.  The
    originals are restored afterwards.
    """

    original_project = historian_module.project_regime
    original_iso = historian_module._iso_from_us
    cached_project = functools.lru_cache(maxsize=65_536)(original_project)
    cached_iso = functools.lru_cache(maxsize=262_144)(original_iso)

    def project(regime, dimensions=REGIME_DIMENSIONS):
        try:
            return cached_project(regime, tuple(dimensions))
        except TypeError:
            return original_project(regime, dimensions)

    def iso(value):
        try:
            return cached_iso(value)
        except TypeError:
            return original_iso(value)

    patched = []
    for module in (historian_module, *modules):
        if getattr(module, "project_regime", None) is original_project:
            patched.append((module, "project_regime", original_project))
            module.project_regime = project
    patched.append((historian_module, "_iso_from_us", original_iso))
    historian_module._iso_from_us = iso
    try:
        yield
    finally:
        for module, name, value in reversed(patched):
            setattr(module, name, value)


class _ReplayBaseline(BaselineStats):
    """A memoized baseline whose ``as_dict()`` is computed once.

    ``dataclasses.asdict`` deep-copies the 128-bucket preview on every call
    (about 18 ms on the Pi, eight times per evaluation).  The cached dict has
    identical content; callers receive a shallow copy.
    """

    def as_dict(self) -> dict[str, object]:
        cached = self.__dict__.get("_cached_as_dict")
        if cached is None:
            cached = super().as_dict()
            object.__setattr__(self, "_cached_as_dict", cached)
        return dict(cached)


def _replay_baseline(value: BaselineStats, *, full_evidence: bool) -> _ReplayBaseline:
    fields = {name: getattr(value, name) for name in BaselineStats.__dataclass_fields__}
    if not full_evidence:
        # Display-only preview; no lifecycle, fingerprint, recovery or
        # notification decision reads it.  The input digest is kept.
        fields["input_buckets"] = ()
        fields["input_buckets_complete"] = False
    return _ReplayBaseline(**fields)


def _earliest_bucket_us(value: object) -> int | None:
    """Earliest input bucket of a baseline, when it can be known exactly."""

    earliest = getattr(value, "_replay_earliest_bucket_us", None)
    if isinstance(earliest, int):
        return earliest
    buckets = getattr(value, "input_buckets", None)
    if buckets:
        try:
            return min(int(item["bucket_us"]) for item in buckets)
        except (KeyError, TypeError, ValueError):
            return None
    return None


class EdgeAwareMemo(dict):
    """The evaluator's ``baseline_memo`` (C5) made exact for replay.

    The C5 key has no ``before``: inside a trip the current trip is excluded
    and no other trip's buckets arrive, so the upper edge never changes the
    input set, but the 30-day lower edge does once it passes the earliest
    input bucket (the episode-633 case).  ``now_us`` is set by the replay loop
    before each ``evaluate()``; a stored baseline whose earliest input bucket
    has left the window reads as a miss and is recomputed.  A baseline whose
    earliest bucket is unknown is recomputed on the next hour, like the
    historian memo's parked key.
    """

    LOOKBACK_INDEX = 7  # position of lookback_days in EarlyWarningEvaluator._memo_key

    def __init__(self) -> None:
        super().__init__()
        self.now_us: int | None = None
        self.edge_refreshes = 0
        self._stored: dict[object, tuple[int | None, int | None]] = {}

    def _stale(self, key: object) -> bool:
        if self.now_us is None:
            return False
        earliest, stored_us = self._stored.get(key, (None, None))
        if earliest is None:
            return stored_us is not None and stored_us // HOUR_US != self.now_us // HOUR_US
        try:
            lookback = int(key[self.LOOKBACK_INDEX])  # type: ignore[index]
        except (TypeError, ValueError, IndexError):
            return True
        return self.now_us - lookback * DAY_US > earliest

    def __contains__(self, key: object) -> bool:
        if not super().__contains__(key):
            return False
        if self._stale(key):
            self.edge_refreshes += 1
            super().__delitem__(key)
            self._stored.pop(key, None)
            return False
        return True

    def __setitem__(self, key: object, value: object) -> None:
        super().__setitem__(key, value)
        self._stored[key] = (_earliest_bucket_us(value), self.now_us)

    def clear(self) -> None:
        super().clear()
        self._stored.clear()


class ReplayHistorian(TelemetryHistorian):
    """Historian over a scratch copy: memoized baselines, no rollup work,
    and a trips table that shows each tick only what existed at that time."""

    def __init__(
        self,
        database: str | Path,
        *,
        config: HistorianConfig | None = None,
        time_consistent_trips: bool = True,
        full_evidence: bool = False,
        trip_source: str | Path | None = None,
    ):
        super().__init__(database, config=config)
        self.full_evidence = full_evidence
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute("PRAGMA cache_size=-65536")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self.verify_every = 250
        self.query_checks = {"calls": 0, "checked": 0, "mismatches": 0, "first_mismatch": None}
        self._recent_index = self._build_recent_index()
        self.baseline_memo: dict[tuple, object] = {}
        self.baseline_calls = 0
        self.baseline_hits = 0
        self.baseline_edge_refreshes = 0
        if trip_source is not None:
            # A resumed copy holds the time-consistent view of its last tick;
            # the untouched export holds the final trips table.
            source = sqlite3.connect(Path(trip_source).resolve().as_uri() + "?mode=ro", uri=True)
            source.row_factory = sqlite3.Row
            try:
                self.trip_rows = [
                    dict(row) for row in source.execute("SELECT * FROM trips ORDER BY started_us,id")
                ]
            finally:
                source.close()
        else:
            self.trip_rows = [
                dict(row) for row in self._conn.execute("SELECT * FROM trips ORDER BY started_us,id")
            ]
        self._trip_snapshots: dict[int, list[int]] = defaultdict(list)
        for trip_id, captured_us in self._conn.execute(
            "SELECT trip_id,captured_us FROM snapshots WHERE trip_id IS NOT NULL ORDER BY captured_us"
        ):
            self._trip_snapshots[int(trip_id)].append(int(captured_us))
        self.time_consistent_trips = time_consistent_trips
        self._trip_view: dict[int, tuple] = {}
        self._trip_columns = list(self.trip_rows[0].keys()) if self.trip_rows else []
        if time_consistent_trips:
            self._conn.commit()
            self._conn.execute("PRAGMA foreign_keys=OFF")
            with self._conn:
                self._conn.execute("DELETE FROM trips")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _build_recent_index(self) -> dict[tuple, tuple[list[int], list[sqlite3.Row]]]:
        """First capture of every fresh numeric observation, grouped exactly
        like ``recent_numeric_samples`` filters (the copy is never ingested)."""

        seen: set[tuple] = set()
        grouped: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM metric_samples WHERE freshness='fresh' AND value_kind='number' "
                "AND observed_us IS NOT NULL ORDER BY captured_us,id"
            ).fetchall()
        for row in rows:
            identity = (
                row["metric"], row["source"], row["unit"], row["quality"],
                row["provenance"], row["observed_us"],
            )
            if identity in seen:
                continue
            seen.add(identity)
            grouped[
                (row["metric"], row["quality"], row["source"], row["provenance"], row["trip_id"])
            ].append(row)
        return {key: ([int(r["captured_us"]) for r in values], values) for key, values in grouped.items()}

    def recent_numeric_samples(
        self,
        metric,
        *,
        regime,
        trip_id,
        at,
        limit,
        quality,
        source,
        provenance,
        regime_dimensions=REGIME_DIMENSIONS,
    ):
        """In-memory equivalent of the historian query (same filters, the
        same first-capture dedupe across all trips, the same ordering and
        fetch limit, the same Python regime projection).  Every
        ``verify_every``-th call is cross-checked against the SQL path."""

        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        at_us = historian_module._to_us(historian_module._utc_datetime(at, "at"))
        entry = self._recent_index.get((metric, quality, source, provenance, trip_id))
        selected: list[dict[str, object]] = []
        if entry:
            times, rows = entry
            count = bisect_right(times, at_us)
            fetch_limit = min(1_000, max(limit, limit * 20))
            top = heapq.nlargest(
                fetch_limit,
                rows[:count],
                key=lambda r: (r["observed_us"], r["captured_us"]),
            )
            project = historian_module.project_regime
            target = project(regime, regime_dimensions)
            for row in top:
                if project(row["regime"], regime_dimensions) == target:
                    selected.append(self._sample_dict(row))
                    if len(selected) >= limit:
                        break
        self.query_checks["calls"] += 1
        if self.verify_every and (self.query_checks["calls"] - 1) % self.verify_every == 0:
            reference = super().recent_numeric_samples(
                metric,
                regime=regime,
                trip_id=trip_id,
                at=at,
                limit=limit,
                quality=quality,
                source=source,
                provenance=provenance,
                regime_dimensions=regime_dimensions,
            )
            self.query_checks["checked"] += 1
            if reference != selected:
                self.query_checks["mismatches"] += 1
                if self.query_checks["first_mismatch"] is None:
                    self.query_checks["first_mismatch"] = {
                        "metric": metric,
                        "at": str(at),
                        "sql_ids": [r.get("sample_id") for r in reference],
                        "memory_ids": [r.get("sample_id") for r in selected],
                    }
        return selected

    # (b) rollups are already complete in the export; never touch the DB.
    def refresh_rollups(self, *, through=None, max_buckets=None, **_ignored) -> dict[str, object]:
        if isinstance(through, datetime):
            text = through.astimezone(timezone.utc).isoformat()
        elif isinstance(through, str):
            text = through
        else:
            text = datetime.now(timezone.utc).isoformat()
        return {"buckets": 0, "rows": 0, "through": text, "backlog": False}

    # (a) memoized baseline.  Inside a trip the current trip is excluded and
    # no other trip's buckets arrive, so the upper edge (``before``) never
    # changes the input set; the lower edge (``before`` - lookback) does once
    # it passes the earliest input bucket, so an entry is recomputed exactly
    # then.  Outside a trip the key also carries the hour (parked buckets can
    # arrive within the hour).
    def robust_baseline(self, metric, regime, *, before, **kwargs):
        self.baseline_calls += 1
        try:
            dims = tuple(kwargs.get("regime_dimensions", REGIME_DIMENSIONS))
            before_dt = historian_module._utc_datetime(before, "before")
            before_us = historian_module._to_us(before_dt)
            lookback = kwargs.get("lookback_days", 30)
            start_us = historian_module._to_us(before_dt - timedelta(days=lookback))
            exclude = kwargs.get("exclude_trip_id")
            extra = tuple(
                sorted(
                    (name, value)
                    for name, value in kwargs.items()
                    if name not in {
                        "regime_dimensions", "exclude_trip_id", "lookback_days", "unit",
                        "quality", "source", "provenance", "minimum_trip_age_seconds",
                    }
                )
            )
            key = (
                metric,
                historian_module.project_regime(regime, dims),
                kwargs.get("unit"),
                kwargs.get("quality"),
                kwargs.get("source"),
                kwargs.get("provenance"),
                dims,
                kwargs.get("lookback_days", 30),
                kwargs.get("minimum_trip_age_seconds", 0),
                extra,
                exclude if exclude is not None else f"hour:{before_us // HOUR_US}",
            )
            hash(key)
        except (TypeError, ValueError):
            return super().robust_baseline(metric, regime, before=before, **kwargs)
        cached = self.baseline_memo.get(key)
        if cached is not None:
            value, earliest_bucket_us = cached
            if earliest_bucket_us is None or start_us <= earliest_bucket_us:
                self.baseline_hits += 1
                return value
            self.baseline_edge_refreshes += 1
        value = super().robust_baseline(metric, regime, before=before, **kwargs)
        earliest_bucket_us = None
        if isinstance(value, BaselineStats):
            if value.input_buckets:
                # input_buckets preview = the first 128 rows in bucket order.
                earliest_bucket_us = int(value.input_buckets[0]["bucket_us"])
            value = _replay_baseline(value, full_evidence=self.full_evidence)
            # The preview may be stripped; carry the edge for EdgeAwareMemo.
            object.__setattr__(value, "_replay_earliest_bucket_us", earliest_bucket_us)
        self.baseline_memo[key] = (value, earliest_bucket_us)
        return value

    def advance_trips(self, at_us: int) -> None:
        """Show the trips table as the live historian had it at ``at_us``."""

        if not self.time_consistent_trips:
            return
        idle_us = int(round(self.config.trip_idle_timeout_seconds * US))
        desired: dict[int, dict[str, object]] = {}
        for row in self.trip_rows:
            if int(row["started_us"]) > at_us:
                break
            trip_id = int(row["id"])
            closed = row["ended_us"] is not None and at_us - int(row["last_active_us"]) >= idle_us
            if closed:
                desired[trip_id] = row
                continue
            snaps = self._trip_snapshots.get(trip_id, [])
            count = bisect_right(snaps, at_us)
            last = int(row["last_active_us"])
            if count:
                last = min(last, snaps[count - 1])
            last = max(last, int(row["started_us"]))
            desired[trip_id] = {
                **row,
                "last_active_us": last,
                "last_active_at": _iso(last),
                "ended_us": None,
                "ended_at": None,
                "end_reason": None,
                "snapshot_count": count,
            }
        open_ids = [trip_id for trip_id, row in desired.items() if row["ended_us"] is None]
        for trip_id in open_ids[:-1]:
            # Live closes the previous trip before opening the next one.
            desired[trip_id] = next(r for r in self.trip_rows if int(r["id"]) == trip_id)
            if desired[trip_id]["ended_us"] is None:
                last = int(desired[trip_id]["last_active_us"])
                desired[trip_id] = {**desired[trip_id], "ended_us": last, "ended_at": _iso(last), "end_reason": "replay_overlap"}
        changes = []
        for trip_id, row in desired.items():
            values = tuple(row[name] for name in self._trip_columns)
            if self._trip_view.get(trip_id) != values:
                changes.append((trip_id, row["ended_us"] is None, values))
        if not changes:
            return
        # Close before open so the one-open-trip index never sees two.
        changes.sort(key=lambda item: (item[1], item[0]))
        assignments = ",".join(f"{name}=?" for name in self._trip_columns if name != "id")
        others = [name for name in self._trip_columns if name != "id"]
        with self._lock, self._conn:
            for trip_id, _is_open, values in changes:
                row = dict(zip(self._trip_columns, values))
                if trip_id in self._trip_view:
                    self._conn.execute(
                        f"UPDATE trips SET {assignments} WHERE id=?",
                        [row[name] for name in others] + [trip_id],
                    )
                else:
                    self._conn.execute(
                        f"INSERT INTO trips({','.join(self._trip_columns)}) VALUES({','.join('?' for _ in values)})",
                        values,
                    )
                self._trip_view[trip_id] = values

    def clear_memo(self) -> None:
        self.baseline_memo.clear()


def load_current_evaluator(ref: str = "HEAD", *, out_dir: str | Path = OUT_DIR):
    """Import ``early_warning.py`` exactly as stored at a git ref."""

    blob = _git("rev-parse", f"{ref}:{EVALUATOR_PATH}")
    if blob is None:
        raise RuntimeError(f"git cannot resolve {ref}:{EVALUATOR_PATH}")
    source = _git("show", f"{ref}:{EVALUATOR_PATH}", strip=False)
    if source is None:
        raise RuntimeError(f"git show {ref}:{EVALUATOR_PATH} failed")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", ref)
    path = out_dir / f"early_warning_{safe}.py"
    path.write_text(source, encoding="utf-8")
    name = "early_warning_replay_current"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module  # dataclasses resolve string annotations here
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    info = {
        "source": "git",
        "ref": ref,
        "commit": _git("rev-parse", ref),
        "blob": blob,
        "module_path": str(path),
    }
    return module, info


def load_worktree_evaluator():
    module = importlib.import_module("projects.vehicle_data.early_warning")
    path = REPO / EVALUATOR_PATH
    blob = _git("hash-object", str(path))
    head_blob = _git("rev-parse", f"HEAD:{EVALUATOR_PATH}")
    return module, {
        "source": "worktree",
        "module_path": str(path),
        "blob": blob,
        "matches_head": blob is not None and blob == head_blob,
    }


def build_timeline(
    conn: sqlite3.Connection,
    *,
    stride: int = 1,
    gap_tick_hours: float = 6.0,
    trips: set[int] | None = None,
    end_us: int | None = None,
) -> list[tuple[int, str, int | None]]:
    """Ticks = snapshot times (every ``stride``-th inside trips) + gap ticks."""

    if isinstance(stride, bool) or not isinstance(stride, int) or stride < 1:
        raise ValueError("stride must be a positive integer")
    points: list[tuple[int, str, int | None]] = []
    position: Counter = Counter()
    for captured_us, trip_id in conn.execute(
        "SELECT captured_us,trip_id FROM snapshots ORDER BY captured_us"
    ):
        if trip_id is None:
            if trips:
                continue
            points.append((int(captured_us), "parked", None))
            continue
        trip_id = int(trip_id)
        if trips and trip_id not in trips:
            continue
        index = position[trip_id]
        position[trip_id] += 1
        if index % stride == 0:
            points.append((int(captured_us), "trip", trip_id))
    if not gap_tick_hours or gap_tick_hours <= 0:
        return points
    step = int(round(gap_tick_hours * HOUR_US))
    result: list[tuple[int, str, int | None]] = []
    previous: int | None = None
    bounded = points + ([(int(end_us), "end", None)] if end_us is not None else [])
    for point in bounded:
        if previous is not None:
            tick = previous + step
            while tick < point[0]:
                result.append((tick, "gap", None))
                tick += step
        if point[1] != "end":
            result.append(point)
        previous = point[0]
    return result


def _episode_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT e.id,e.rule_key,e.category,e.status,e.opened_us,e.resolved_us,
               e.resolution_reason,
               json_extract(e.first_assessment_json,'$.state'),
               json_extract(e.first_assessment_json,'$.current.trip_id'),
               json_extract(e.first_assessment_json,'$.tier'),
               json_extract(e.first_assessment_json,'$.group'),
               json_extract(e.first_assessment_json,'$.regime'),
               json_extract(e.first_assessment_json,'$.rule_revision'),
               json_extract(e.first_assessment_json,'$.severity'),
               EXISTS(SELECT 1 FROM advisory_episode_events v
                      WHERE v.episode_id=e.id AND v.new_state='warning')
        FROM advisory_episodes e ORDER BY e.id
        """
    ).fetchall()
    return [
        {
            "id": r[0], "rule": r[1], "category": r[2], "status": r[3],
            "opened_us": r[4], "resolved_us": r[5], "resolution": r[6],
            "opened_state": r[7], "trip_id": r[8], "tier": r[9], "group": r[10],
            "opened_regime": r[11], "rule_revision": r[12], "severity": r[13],
            "warned": bool(r[14]),
        }
        for r in rows
    ]


def _outbox_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT id,episode_id,rule_key,status,created_us,delivered_us,
               json_extract(payload_json,'$.notification_kind'),
               json_extract(payload_json,'$.tier'),
               json_extract(payload_json,'$.group')
        FROM advisory_notification_outbox ORDER BY id
        """
    ).fetchall()
    return [
        {
            "id": r[0], "episode_id": r[1], "rule": r[2], "status": r[3],
            "created_us": r[4], "delivered_us": r[5], "kind": r[6],
            "tier": r[7], "group": r[8],
        }
        for r in rows
    ]


def _recorded_rows(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    episodes = [
        {
            "id": r[0], "rule": r[1], "category": r[2], "status": r[3],
            "opened_us": r[4], "resolved_us": r[5], "resolution": r[6],
            "opened_state": r[7], "trip_id": r[8], "tier": None, "group": None,
            "opened_regime": r[9], "rule_revision": r[10], "severity": None,
            "warned": r[11] is not None,
        }
        for r in conn.execute(
            """
            SELECT id,rule_key,category,status,opened_us,resolved_us,resolution_reason,
                   opened_state,trip_id,opened_regime,rule_revision,first_warning_us
            FROM replay_recorded_episodes ORDER BY id
            """
        )
    ]
    outbox = [
        {
            "id": r[0], "episode_id": r[1], "rule": r[2], "status": r[3],
            "created_us": r[4], "delivered_us": r[5], "kind": r[6],
            "tier": None, "group": None,
        }
        for r in conn.execute(
            "SELECT id,episode_id,rule_key,status,created_us,delivered_us,notification_kind "
            "FROM replay_recorded_outbox ORDER BY id"
        )
    ]
    return episodes, outbox


def attribute_trip(trip_id: object, opened_us: int, trips: list[dict[str, object]]) -> int | None:
    ids = {int(t["id"]) for t in trips}
    if isinstance(trip_id, (int, float)) and not isinstance(trip_id, bool) and int(trip_id) in ids:
        return int(trip_id)
    for trip in trips:
        end = trip["ended_us"] if trip["ended_us"] is not None else trip["last_active_us"]
        if int(trip["started_us"]) <= opened_us <= int(end) + TRIP_TAIL_US:
            return int(trip["id"])
    return None


def _zero() -> dict[str, int]:
    return {"opened": 0, "warned": 0, "pushes": 0, "recovery": 0, "cancelled": 0, "parked_openings": 0}


def summarize(
    episodes: list[dict[str, object]],
    outbox: list[dict[str, object]],
    trips: list[dict[str, object]],
    *,
    end_us: int,
    include_episodes: bool = True,
) -> dict[str, object]:
    """Per rule / family / trip counts: opened, warned, pushes, recovery."""

    rules: dict[str, dict[str, object]] = {}
    families: dict[str, dict[str, int]] = {}
    per_trip: dict[str, dict[str, dict[str, int]]] = {}
    by_id: dict[int, dict[str, object]] = {}
    compact = []
    for episode in episodes:
        family = family_of(str(episode["rule"]), episode.get("group"))
        trip = attribute_trip(episode.get("trip_id"), int(episode["opened_us"]), trips)
        parked = trip is None or _parked_regime(episode.get("opened_regime"))
        episode = {**episode, "family": family, "trip": trip, "parked": parked}
        by_id[int(episode["id"])] = episode
        rule = rules.setdefault(
            str(episode["rule"]),
            {**_zero(), "family": family, "durations": [], "resolutions": Counter(), "tiers": Counter()},
        )
        fam = families.setdefault(family, _zero())
        trip_key = "parked" if trip is None else str(trip)
        trip_fam = per_trip.setdefault(trip_key, {}).setdefault(family, _zero())
        warned = int(bool(episode["warned"]))
        for bucket in (rule, fam, trip_fam):
            bucket["opened"] += 1
            bucket["warned"] += warned
            bucket["parked_openings"] += int(parked)
        end = episode["resolved_us"] if episode["resolved_us"] is not None else end_us
        duration = max(0.0, (int(end) - int(episode["opened_us"])) / US)
        rule["durations"].append(duration)
        reason = episode.get("resolution") or ("open" if episode.get("status") == "open" else "resolved")
        rule["resolutions"][str(reason)[:80]] += 1
        rule["tiers"][str(episode.get("tier"))] += 1
        if include_episodes:
            compact.append(
                {
                    "id": episode["id"], "rule": episode["rule"], "family": family,
                    "tier": episode.get("tier"), "trip": trip, "parked": parked,
                    "opened_at": _iso(int(episode["opened_us"])),
                    "opened_state": episode.get("opened_state"), "warned": bool(warned),
                    "duration_seconds": round(duration, 1), "resolution": reason,
                    "severity": episode.get("severity"), "rule_revision": episode.get("rule_revision"),
                }
            )
    for row in outbox:
        episode = by_id.get(int(row["episode_id"])) if row.get("episode_id") is not None else None
        if episode is None:
            continue
        family = episode["family"]
        rule = rules[str(episode["rule"])]
        fam = families[family]
        trip_key = "parked" if episode["trip"] is None else str(episode["trip"])
        trip_fam = per_trip[trip_key][family]
        if row["status"] == "cancelled":
            field = "cancelled"
        elif row.get("kind") == "recovery":
            field = "recovery"
        else:
            field = "pushes"
        for bucket in (rule, fam, trip_fam):
            bucket[field] += 1
    for rule in rules.values():
        durations = rule.pop("durations")
        rule["duration_median_seconds"] = round(statistics.median(durations), 1) if durations else None
        rule["duration_max_seconds"] = round(max(durations), 1) if durations else None
        rule["resolutions"] = dict(rule["resolutions"].most_common())
        rule["tiers"] = dict(rule["tiers"])
    totals = _zero()
    for fam in families.values():
        for key in totals:
            totals[key] += fam[key]
    result = {
        "rules": dict(sorted(rules.items())),
        "families": {name: families[name] for name in FAMILY_ORDER if name in families},
        "trips": per_trip,
        "totals": totals,
    }
    if include_episodes:
        result["episodes"] = compact
    return result


def _trip_meta(trips: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    meta = {}
    for trip in trips:
        end = trip["ended_us"] if trip["ended_us"] is not None else trip["last_active_us"]
        meta[str(trip["id"])] = {
            "started_at": _iso(int(trip["started_us"])),
            "minutes": round((int(end) - int(trip["started_us"])) / (60 * US), 1),
        }
    return meta


def _compact_assessment(assessment: dict[str, object]) -> dict[str, object]:
    def strip(value, depth=0):
        if isinstance(value, dict):
            return {
                k: strip(v, depth + 1)
                for k, v in value.items()
                if k not in ("input_buckets", "observations", "rule_snapshot", "interpretation")
            }
        if isinstance(value, list):
            return [strip(v, depth + 1) for v in value[:20]]
        return value

    return strip(assessment)


def _timeline_digest(timeline: list[tuple[int, str, int | None]]) -> str:
    digest = hashlib.sha256()
    for us, kind, trip_id in timeline:
        digest.update(f"{us}:{kind}:{trip_id};".encode())
    return digest.hexdigest()[:16]


def _load_resume(resume: str | Path) -> dict[str, object]:
    path = Path(resume)
    previous = json.loads(path.read_text(encoding="utf-8"))
    if previous.get("kind") != "replay":
        raise ValueError(f"{path} is not a replay summary")
    if previous.get("complete"):
        raise ValueError(f"{path} is already complete; nothing to resume")
    progress = previous.get("progress") or {}
    if "next_index" not in progress:
        raise ValueError(f"{path} records no resumable progress")
    if not Path(str(previous.get("copy"))).exists():
        raise FileNotFoundError(f"replay copy {previous.get('copy')} is missing")
    if not Path(str(previous.get("export"))).exists():
        raise FileNotFoundError(f"export {previous.get('export')} is missing")
    return previous


def replay(
    export_path: str | Path = DEFAULT_EXPORT,
    *,
    evaluator: str = "current",
    current_ref: str = "HEAD",
    stride: int = 1,
    trips: set[int] | None = None,
    gap_tick_hours: float = 6.0,
    cpu_budget: float = 90.0,
    rss_budget_mb: float = 300.0,
    tier2: bool = False,
    simulated_delivery: bool = True,
    evaluator_factory=None,
    out_dir: str | Path = OUT_DIR,
    config: HistorianConfig | None = None,
    tag: str | None = None,
    full_evidence: bool = False,
    resume: str | Path | None = None,
    max_ticks: int | None = None,
) -> dict[str, object]:
    """Replay one evaluator over a copy of the export; return a JSON summary.

    ``resume`` continues an aborted run (its summary JSON) on the same copy
    from the next unevaluated tick, in a new process with a fresh CPU/RSS
    budget.  All lifecycle state lives in the copy's tables, so for an
    evaluator that keeps no state between ``evaluate()`` calls (the HEAD
    evaluator) the chunks together equal one continuous pass; only the
    baseline memo restarts at the boundary.  Every option except the budgets
    is taken from the summary being resumed.
    """

    cpu0 = time.process_time()
    wall0 = time.monotonic()
    previous: dict[str, object] | None = None
    if resume is not None:
        previous = _load_resume(resume)
        options = previous.get("options") or {}
        export_path = Path(str(previous["export"]))
        evaluator = str(previous["evaluator"])
        stride = int(options.get("stride") or 1)
        trips = {int(t) for t in options["trips"]} if options.get("trips") else None
        gap_tick_hours = float(options.get("gap_tick_hours") or 0.0)
        tier2 = bool(options.get("tier2"))
        simulated_delivery = bool(options.get("simulated_delivery", True))
        full_evidence = bool(options.get("full_evidence"))
        tag = previous.get("tag")
        previous_info = previous.get("evaluator_info") or {}
        current_ref = str(previous_info.get("commit") or options.get("current_ref") or current_ref)
        out_dir = Path(resume).parent
    export_path = Path(export_path)
    if not export_path.exists():
        raise FileNotFoundError(f"{export_path} does not exist; run the export first")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if evaluator not in ("current", "new") and evaluator_factory is None:
        raise ValueError("evaluator must be 'current' or 'new'")
    manifest = read_manifest(export_path)
    if previous is not None:
        stamp = str(previous["stamp"])
        name = Path(resume).stem
        copy = Path(str(previous["copy"]))
    else:
        stamp = _stamp()
        label = evaluator + (f"@{re.sub(r'[^A-Za-z0-9._-]', '_', tag)}" if tag else "")
        name = f"replay-{label}-{stamp}"
        copy = out_dir / f"{name}.sqlite3"
        shutil.copyfile(export_path, copy)
    chunks: list[dict[str, object]] = list((previous or {}).get("chunks") or [])
    summary: dict[str, object] = {
        "kind": "replay",
        "evaluator": evaluator,
        "tag": tag,
        "stamp": stamp,
        "export": str(export_path),
        "copy": str(copy),
        "manifest": manifest,
        "options": {
            "stride": stride,
            "trips": sorted(trips) if trips else None,
            "gap_tick_hours": gap_tick_hours,
            "cpu_budget": cpu_budget,
            "rss_budget_mb": rss_budget_mb,
            "tier2": tier2,
            "simulated_delivery": simulated_delivery,
            "current_ref": current_ref if evaluator == "current" else None,
            "full_evidence": full_evidence,
            "max_ticks": max_ticks,
        },
        "complete": False,
        "abort_reason": None,
        "notes": [],
    }
    historian = ReplayHistorian(
        copy,
        config=config,
        full_evidence=full_evidence,
        trip_source=export_path if previous is not None else None,
    )
    counters: Counter = Counter()
    kinds_done: Counter = Counter()
    delivered = 0
    companion: dict[str, dict[str, object]] = {}
    slowest_tick = 0.0
    if previous is not None:
        counters.update({k: int(v) for k, v in (previous.get("persistence_totals") or {}).items()})
        kinds_done.update((previous.get("ticks") or {}).get("evaluated_by_kind") or {})
        delivered = int(previous.get("simulated_deliveries") or 0)
        companion = dict(previous.get("companion_gaps") or {})
        slowest_tick = float((previous.get("ticks") or {}).get("slowest_tick_seconds") or 0.0)
    start_index = int(((previous or {}).get("progress") or {}).get("next_index") or 0)
    next_index = start_index
    last_tick_us = ((previous or {}).get("progress") or {}).get("last_tick_us")
    timeline: list[tuple[int, str, int | None]] = []
    caches = None
    ev = None
    try:
        if evaluator_factory is not None:
            ev = evaluator_factory(historian)
            info = {"source": "injected", "class": type(ev).__name__}
        elif evaluator == "current":
            module, info = load_current_evaluator(current_ref, out_dir=out_dir)
            ev = module.EarlyWarningEvaluator(historian)
        else:
            module, info = load_worktree_evaluator()
            ev = module.EarlyWarningEvaluator(historian)
        if previous is not None:
            before_blob = (previous.get("evaluator_info") or {}).get("blob")
            if info.get("blob") != before_blob:
                raise RuntimeError(
                    f"evaluator changed since the run being resumed ({before_blob} -> {info.get('blob')}); "
                    "start a new replay instead"
                )
        caches = pure_function_caches(*(m for m in (sys.modules.get(getattr(type(ev), "__module__", "")),) if m))
        caches.__enter__()
        memo_on_evaluator = hasattr(ev, "baseline_memo")
        if memo_on_evaluator:
            setattr(ev, "baseline_memo", EdgeAwareMemo())
        summary["evaluator_info"] = {**info, "baseline_memo_hook": memo_on_evaluator}
        end_us = int(manifest.get("exported_us") or (_now_us()))
        timeline = build_timeline(
            historian._conn, stride=stride, gap_tick_hours=gap_tick_hours, trips=trips, end_us=end_us
        )
        planned = Counter(kind for _us, kind, _trip in timeline)
        digest = _timeline_digest(timeline)
        if previous is not None:
            old = previous.get("progress") or {}
            if old.get("timeline_digest") != digest or int(old.get("timeline_length") or -1) != len(timeline):
                raise RuntimeError("the rebuilt timeline differs from the run being resumed")
        summary["ticks"] = {"planned": len(timeline), "planned_by_kind": dict(planned)}
        segment: object = object()
        for index in range(start_index, len(timeline)):
            us, kind, trip_id = timeline[index]
            usage = _usage(cpu0)
            if usage["cpu_seconds"] > cpu_budget:
                raise BudgetExceeded(f"CPU {usage['cpu_seconds']:.3f} s exceeded the {cpu_budget:g} s budget")
            if usage["peak_rss_mb"] > rss_budget_mb:
                raise BudgetExceeded(f"peak RSS {usage['peak_rss_mb']:.1f} MB exceeded the {rss_budget_mb:g} MB budget")
            if max_ticks is not None and index - start_index >= max_ticks:
                raise BudgetExceeded(f"tick limit {max_ticks} reached")
            current_segment = trip_id if kind == "trip" else "parked"
            if current_segment != segment:
                historian.clear_memo()
                memo = getattr(ev, "baseline_memo", None)
                if isinstance(memo, dict):
                    memo.clear()
                segment = current_segment
            at = _dt(us)
            tick0 = time.monotonic()
            ev_memo = getattr(ev, "baseline_memo", None)
            if isinstance(ev_memo, EdgeAwareMemo):
                ev_memo.now_us = us
            historian.advance_trips(us)
            report = ev.evaluate(at=at)
            assessments = list(report.get("assessments", []))
            result = historian.record_advisory_assessments(
                assessments,
                evaluated_at=at,
                authoritative_rule_keys=[a["rule"] for a in assessments],
            )
            for field in PERSISTENCE_COUNTERS:
                counters[field] += int(getattr(result, field, 0) or 0)
            for assessment in assessments:
                if "companion_gaps" in assessment:
                    entry = companion.setdefault(
                        str(assessment.get("rule")), {"ticks": 0, "ticks_with_gaps": 0, "last": None, "max": None}
                    )
                    value = assessment.get("companion_gaps")
                    entry["ticks"] += 1
                    if value:
                        entry["ticks_with_gaps"] += 1
                        entry["last"] = value
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            entry["max"] = value if entry["max"] is None else max(entry["max"], value)
            if simulated_delivery:
                for row in historian.pending_advisory_notifications(at=at, limit=100):
                    historian.mark_advisory_notification_delivered(row["id"], delivered_at=at)
                    delivered += 1
            kinds_done[kind] += 1
            next_index = index + 1
            last_tick_us = us
            slowest_tick = max(slowest_tick, time.monotonic() - tick0)
        summary["complete"] = True
        if tier2:
            summary["tier2"] = _run_tier2(historian, end_us, counters)
        else:
            summary["tier2"] = {"status": "not_run"}
    except BudgetExceeded as exc:
        summary["abort_reason"] = str(exc)
        summary["tier2"] = {"status": "not_run"}
    finally:
        if caches is not None:
            caches.__exit__(None, None, None)
        try:
            evaluated = sum(kinds_done.values())
            summary.setdefault("ticks", {})
            summary["ticks"].update(
                {
                    "evaluated": evaluated,
                    "evaluated_by_kind": dict(kinds_done),
                    "slowest_tick_seconds": round(slowest_tick, 4),
                }
            )
            summary["progress"] = {
                "next_index": next_index,
                "last_tick_us": last_tick_us,
                "last_tick_at": _iso(last_tick_us) if last_tick_us is not None else None,
                "timeline_length": len(timeline),
                "timeline_digest": _timeline_digest(timeline) if timeline else None,
            }
            summary["persistence_totals"] = {field: counters[field] for field in PERSISTENCE_COUNTERS}
            summary["simulated_deliveries"] = delivered
            ev_stats = getattr(ev, "baseline_memo_stats", None)
            ev_hits = int((ev_stats or {}).get("hits") or 0) if isinstance(ev_stats, dict) else 0
            ev_misses = int((ev_stats or {}).get("misses") or 0) if isinstance(ev_stats, dict) else 0
            ev_memo_obj = getattr(ev, "baseline_memo", None)
            ev_edge = ev_memo_obj.edge_refreshes if isinstance(ev_memo_obj, EdgeAwareMemo) else 0
            # Evaluator memo (C5) hits never reach the historian, so the
            # totals combine both layers; the split is kept alongside.
            chunk_memo = {
                "calls": historian.baseline_calls + ev_hits,
                "hits": historian.baseline_hits + ev_hits,
                "edge_refreshes": historian.baseline_edge_refreshes + ev_edge,
                "historian_calls": historian.baseline_calls,
                "historian_hits": historian.baseline_hits,
                "evaluator_hits": ev_hits,
                "evaluator_misses": ev_misses,
                "evaluator_edge_refreshes": ev_edge,
            }
            chunk_checks = dict(historian.query_checks)
            summary["companion_gaps"] = companion
            end_us = int(manifest.get("exported_us") or _now_us())
            trips_all = historian.trip_rows
            replayed_trip_ids = sorted(
                {int(t) for t in (manifest.get("trip_ids") or []) if not trips or int(t) in trips}
            )
            summary["replayed_trip_ids"] = replayed_trip_ids
            summary["trip_meta"] = _trip_meta(trips_all)
            summary["counts"] = summarize(
                _episode_rows(historian._conn), _outbox_rows(historian._conn), trips_all, end_us=end_us
            )
            recorded_episodes, recorded_outbox = _recorded_rows(historian._conn)
            cutoff_us = int(manifest.get("cutoff_us") or 0)
            in_window = [e for e in recorded_episodes if cutoff_us <= int(e["opened_us"]) <= end_us]
            window_ids = {int(e["id"]) for e in in_window}
            summary["recorded"] = {
                "window": summarize(
                    in_window,
                    [o for o in recorded_outbox if o.get("episode_id") in window_ids],
                    trips_all,
                    end_us=end_us,
                ),
                "all_time": summarize(
                    recorded_episodes, recorded_outbox, trips_all, end_us=end_us, include_episodes=False
                ),
                "window_rule": "episodes opened inside [cutoff, exported_at]; pushes = their outbox rows",
            }
        finally:
            historian.close()
        summary["wall_seconds"] = round(time.monotonic() - wall0, 2)
        summary.update(_usage(cpu0))
        chunks.append(
            {
                "chunk": len(chunks) + 1,
                "first_index": start_index,
                "next_index": next_index,
                "ticks": next_index - start_index,
                "cpu_seconds": summary["cpu_seconds"],
                "peak_rss_mb": summary["peak_rss_mb"],
                "wall_seconds": summary["wall_seconds"],
                "complete": summary["complete"],
                "abort_reason": summary["abort_reason"],
                "baseline_memo": chunk_memo,
                "query_verification": chunk_checks,
            }
        )
        summary["chunks"] = chunks
        # Counters summed over the chunks that recorded them.
        counted = [c for c in chunks if isinstance(c.get("baseline_memo"), dict)]
        summary["baseline_memo"] = {
            key: sum(int(c["baseline_memo"].get(key) or 0) for c in counted)
            for key in (
                "calls", "hits", "edge_refreshes", "historian_calls", "historian_hits",
                "evaluator_hits", "evaluator_misses", "evaluator_edge_refreshes",
            )
        }
        checked = [c for c in chunks if isinstance(c.get("query_verification"), dict)]
        summary["query_verification"] = {
            **{
                key: sum(int(c["query_verification"].get(key) or 0) for c in checked)
                for key in ("calls", "checked", "mismatches")
            },
            "first_mismatch": next(
                (c["query_verification"].get("first_mismatch") for c in checked
                 if c["query_verification"].get("first_mismatch")),
                None,
            ),
            "chunks_counted": len(checked),
        }
        summary["cpu_seconds_total"] = round(sum(float(c["cpu_seconds"]) for c in chunks), 3)
        summary["peak_rss_mb_max"] = max(float(c["peak_rss_mb"]) for c in chunks)
        evaluated = summary["ticks"].get("evaluated") or 0
        summary["cpu_seconds_per_tick"] = (
            round(summary["cpu_seconds_total"] / evaluated, 5) if evaluated else None
        )
        summary["notes"].extend(_replay_notes(summary))
        _write_json(out_dir / f"{name}.json", summary)
        summary["summary_path"] = str(out_dir / f"{name}.json")
    return summary


def _run_tier2(historian: ReplayHistorian, at_us: int, counters: Counter) -> dict[str, object]:
    try:
        drift = importlib.import_module("projects.vehicle_data.drift_warnings")
    except ImportError as exc:
        return {"status": "tier2 module not available", "detail": str(exc)}
    if not hasattr(drift, "evaluate_drift"):
        return {"status": "tier2 module not available", "detail": "evaluate_drift missing"}
    at = _dt(at_us)
    assessments = list(drift.evaluate_drift(historian, at=at))
    if assessments:
        result = historian.record_advisory_assessments(assessments, evaluated_at=at)
        for field in PERSISTENCE_COUNTERS:
            counters[field] += int(getattr(result, field, 0) or 0)
    return {
        "status": "ok",
        "evaluated_at": at.isoformat(),
        "rule_keys": list(getattr(drift, "DRIFT_RULE_KEYS", ())),
        "assessments": [_compact_assessment(a) for a in assessments],
    }


def _replay_notes(summary: dict[str, object]) -> list[str]:
    notes = [
        "Only vehicle-health evaluators are replayed: interface/USB samples are not exported, so the "
        "system family (can_*, usb_*) is recorded-only; telemetry_gap_* comes from the worktree "
        "monitoring_coverage module and is never notification-eligible.",
        "Parked time is sparse: ticks are the fresh parked battery bursts plus synthetic gap ticks; "
        "the ~5 s parked snapshots and parked non-battery reads (e.g. TPMS) are not exported, so parked "
        "closures happen at the next burst/gap tick instead of within seconds.",
        "The trips table is time-consistent: each tick sees only trips started by then, the running trip "
        "open, and closure after the idle timeout.",
        "Baselines are memoized per trip and recomputed when the 30-day window's lower edge passes the "
        "entry's earliest input bucket (exact inside a trip; outside trips the key also carries the hour); "
        "unless --full-evidence, the display-only baseline.input_buckets preview is omitted from replayed "
        "evidence (no lifecycle or delivery decision reads it).",
        "recent_numeric_samples is served from an exact in-memory index of the static copy (same filters, "
        "first-capture dedupe, ordering, fetch limit and regime projection); every 250th call is cross-checked "
        "against the SQL path (see query_verification).",
    ]
    info = summary.get("evaluator_info") or {}
    if info.get("source") == "git":
        notes.append(
            f"current evaluator = git {info.get('ref')} ({info.get('commit')}), early_warning.py blob "
            f"{info.get('blob')}; its vehicle assessments carry no 'tier', so the historian applies legacy "
            "lifecycle and delivery semantics (rule-keyed rate limit, no 24 h parked closure); its "
            "telemetry_gap_* assessments come from the worktree monitoring_coverage (never eligible)."
        )
    elif info.get("source") == "worktree":
        notes.append(
            f"new evaluator = worktree early_warning.py blob {info.get('blob')} "
            f"(matches HEAD: {info.get('matches_head')})."
        )
    chunks = summary.get("chunks") or []
    if len(chunks) > 1:
        notes.append(
            f"Replayed in {len(chunks)} budgeted chunks on the same copy (resume), "
            f"{summary.get('cpu_seconds_total')} s CPU in total, per chunk "
            + ", ".join(f"{c['cpu_seconds']} s / {c['peak_rss_mb']} MB" for c in chunks)
            + ". Lifecycle state lives in the copy's tables; the baseline memo restarts at each chunk "
            "boundary"
            + (
                "; the git evaluator keeps no state between evaluate() calls, so the chunks equal one pass."
                if info.get("source") == "git"
                else "; any in-memory evaluator state (e.g. candidates) restarts at each boundary."
            )
        )
    if not summary.get("complete"):
        progress = summary.get("progress") or {}
        notes.append(
            f"INCOMPLETE: {summary.get('abort_reason')} (evaluated through tick "
            f"{progress.get('next_index')}/{progress.get('timeline_length')}, {progress.get('last_tick_at')}; "
            "continue with `replay --resume <this summary>`)"
        )
    return notes


# --------------------------------------------------------------------------
# coarse rollup replay


def _runs(buckets: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for bucket in sorted(set(buckets)):
        if runs and bucket - runs[-1][-1] == BUCKET_US:
            runs[-1].append(bucket)
        else:
            runs.append([bucket])
    return runs


def _regime_parts(regime: str) -> tuple[str, str, str, str]:
    parts = str(regime).split(":")
    if len(parts) != 4:
        return ("", "", "", "")
    return parts[0], parts[1], parts[2], parts[3]


def coarse(
    export_path: str | Path = DEFAULT_EXPORT,
    *,
    out_dir: str | Path = OUT_DIR,
    now_us: int | None = None,
    write: bool = True,
) -> dict[str, object]:
    """Tier-0 conditions on 60 s rollup buckets, per trip and rule."""

    cpu0 = time.process_time()
    export_path = Path(export_path)
    conn = sqlite3.connect(export_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        trips = [
            dict(zip(("id", "started_us", "last_active_us", "ended_us"), row))
            for row in conn.execute("SELECT id,started_us,last_active_us,ended_us FROM trips ORDER BY started_us")
        ]
        raw_trips = {
            int(t["id"])
            for t in trips
            if conn.execute("SELECT 1 FROM metric_samples WHERE trip_id=? LIMIT 1", (t["id"],)).fetchone()
        }
        try:
            manifest = {k: json.loads(v) for k, v in conn.execute("SELECT key,value FROM replay_manifest")}
        except sqlite3.OperationalError:
            manifest = {}
        rows: dict[str, list[tuple]] = {}
        for metric in RULE_METRICS:
            rows[metric] = conn.execute(
                "SELECT bucket_us,trip_key,regime,minimum,maximum,median,sample_count "
                "FROM metric_rollups WHERE metric=? AND bucket_seconds=60",
                (metric,),
            ).fetchall()
    finally:
        conn.close()
    end_us = now_us or int(manifest.get("exported_us") or 0) or max(
        (r[0] for values in rows.values() for r in values), default=_now_us()
    )
    starts = {int(t["id"]): int(t["started_us"]) for t in trips}
    hits: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    critical: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    near: dict[int, Counter] = defaultdict(Counter)
    extremes: dict[int, dict[str, float]] = defaultdict(dict)

    def extreme(trip: int, name: str, value: float, high: bool) -> None:
        old = extremes[trip].get(name)
        if old is None or (value > old if high else value < old):
            extremes[trip][name] = round(float(value), 2)

    for bucket, trip, regime, minimum, maximum, _median, _n in rows["engine.coolant_temperature"]:
        engine = _regime_parts(regime)[0]
        if trip and engine == "engine_running":
            extreme(trip, "coolant_max", maximum, True)
            for level in (215, 220, 225):
                if maximum >= level:
                    near[trip][f"coolant_ge_{level}"] += 1
            if maximum >= 230:
                hits["engine_coolant_temperature_hot"][trip].append(bucket)
            if maximum >= 240:
                critical["engine_coolant_temperature_hot"][trip].add(bucket)
    for bucket, trip, regime, minimum, maximum, _median, _n in rows["transmission.oil_temperature"]:
        if trip and _regime_parts(regime)[0] == "engine_running":
            extreme(trip, "transmission_max", maximum, True)
            if maximum >= 230:
                hits["transmission_oil_temperature_hot"][trip].append(bucket)
    bands = {"rpm_idle": 15.0, "rpm_low": 22.0, "rpm_mid": 22.0, "rpm_high": 55.0}
    for bucket, trip, regime, minimum, maximum, _median, _n in rows["engine.oil_pressure"]:
        engine, _motion, rpm, thermal = _regime_parts(regime)
        if not trip or engine != "engine_running":
            continue
        extreme(trip, "oil_min_running", minimum, False)
        if minimum < 12.0:
            hits["engine_oil_pressure_absolute_critical"][trip].append(bucket)
            critical["engine_oil_pressure_absolute_critical"][trip].add(bucket)
        if thermal in ("warm", "hot") and rpm in bands and minimum < bands[rpm]:
            hits["engine_oil_pressure_below_band"][trip].append(bucket)
    duty_max: dict[tuple[int, int], float] = {}
    for bucket, trip, _regime, _minimum, maximum, _median, _n in rows["generator.field_duty"]:
        key = (trip, bucket)
        duty_max[key] = max(duty_max.get(key, float("-inf")), maximum)
    parked_events = []
    for bucket, trip, regime, minimum, maximum, _median, _n in rows["battery.voltage"]:
        engine, _motion, rpm, _thermal = _regime_parts(regime)
        if trip and engine == "engine_running":
            extreme(trip, "battery_min_running", minimum, False)
        if trip and rpm in ("rpm_low", "rpm_mid", "rpm_high") and minimum < 12.0:
            if duty_max.get((trip, bucket), float("-inf")) >= 90.0:
                hits["battery_voltage_charging_failure"][trip].append(bucket)
        if trip == 0 and engine in ("engine_off", "engine_unknown") and minimum < 11.8:
            hits["battery_voltage_low_parked"][0].append(bucket)
            if minimum < 11.5:
                critical["battery_voltage_low_parked"][0].add(bucket)
            after = [tid for tid, start in starts.items() if start <= bucket]
            parked_events.append(
                {
                    "at": _iso(bucket),
                    "minimum": round(float(minimum), 2),
                    "regime": regime,
                    "after_trip": max(after, key=lambda tid: starts[tid]) if after else None,
                }
            )
    limits = {"fl": (50.0, 45.0), "fr": (50.0, 45.0), "rl": (68.0, 60.0), "rr": (68.0, 60.0)}
    wheel_median: dict[str, dict[tuple[int, int], tuple[float, int]]] = {}
    for wheel, (warn, crit) in limits.items():
        per_bucket: dict[tuple[int, int], list[tuple[float, int]]] = defaultdict(list)
        for bucket, trip, regime, minimum, _maximum, median, count in rows[f"tire.pressure.{wheel}"]:
            if not trip:
                continue  # parked TPMS reads are never judged
            per_bucket[(trip, bucket)].append((float(median), int(count)))
            motion = _regime_parts(regime)[1]
            early = trip in starts and bucket < starts[trip] + 180 * US
            if (early or motion == "stationary") and minimum < warn:
                hits["tire_pressure_low_absolute"][trip].append(bucket)
                if minimum < crit:
                    critical["tire_pressure_low_absolute"][trip].add(bucket)
        wheel_median[wheel] = {
            key: (sum(m * n for m, n in values) / max(1, sum(n for _m, n in values)), len(values))
            for key, values in per_bucket.items()
        }
    offsets = {}
    for axle, (left, right) in {"front": ("fl", "fr"), "rear": ("rl", "rr")}.items():
        pairs = {
            key: wheel_median[left][key][0] - wheel_median[right][key][0]
            for key in wheel_median[left].keys() & wheel_median[right].keys()
        }
        recent = [d for (trip, bucket), d in pairs.items() if bucket >= end_us - 30 * DAY_US]
        offset = statistics.median(recent) if recent else 0.0
        offsets[axle] = {"offset_psi": round(offset, 3), "paired_buckets_30d": len(recent)}
        flagged: dict[int, list[int]] = defaultdict(list)
        for (trip, bucket), difference in pairs.items():
            if abs(difference - offset) > 3.0:
                flagged[trip].append(bucket)
        for trip, buckets in flagged.items():
            for run in _runs(buckets):
                hits[f"tire_pressure_pair_asymmetry:{axle}"][trip].extend(run)
    windows_5min = {"tire_pressure_pair_asymmetry:front", "tire_pressure_pair_asymmetry:rear"}
    trip_rows = []
    totals = {"rollup_only": defaultdict(Counter), "replayed": defaultdict(Counter), "parked": defaultdict(Counter)}
    meta = _trip_meta(trips)
    for trip in trips + [{"id": 0}]:
        trip_id = int(trip["id"])
        entry = {
            "trip": trip_id if trip_id else "parked",
            "raw_replayed": trip_id in raw_trips,
            "hits": {},
        }
        if trip_id:
            entry.update(meta[str(trip_id)])
            entry["near_miss"] = dict(near.get(trip_id, {}))
            entry["extremes"] = extremes.get(trip_id, {})
        for rule in sorted(hits):
            buckets = hits[rule].get(trip_id)
            if not buckets:
                continue
            runs = _runs(buckets)
            required = 5 if rule in windows_5min else 1
            record = {
                "buckets": len(set(buckets)),
                "runs": len(runs),
                "hits": sum(1 for run in runs if len(run) >= required),
                "longest_run": max(len(run) for run in runs),
                "critical_buckets": len(critical.get(rule, {}).get(trip_id, ())),
            }
            entry["hits"][rule] = record
            scope = "parked" if not trip_id else ("replayed" if trip_id in raw_trips else "rollup_only")
            total = totals[scope][rule]
            total["trips_hit"] += int(record["hits"] > 0)
            for key in ("buckets", "runs", "hits", "critical_buckets"):
                total[key] += record[key]
        trip_rows.append(entry)
    near_totals = {
        scope: dict(
            sum(
                (Counter(near.get(int(t["id"]), {})) for t in trips if (int(t["id"]) in raw_trips) == (scope == "replayed")),
                Counter(),
            )
        )
        for scope in ("rollup_only", "replayed")
    }
    result = {
        "kind": "coarse",
        "stamp": _stamp(),
        "export": str(export_path),
        "complete": True,
        "end_at": _iso(end_us),
        "rollup_only_trip_ids": sorted(int(t["id"]) for t in trips if int(t["id"]) not in raw_trips),
        "replayed_trip_ids": sorted(raw_trips),
        "pair_offsets": offsets,
        "trips": trip_rows,
        "parked_battery_events": parked_events,
        "totals": {scope: {rule: dict(v) for rule, v in sorted(values.items())} for scope, values in totals.items()},
        "near_miss_totals": near_totals,
        "method": (
            "60 s rollup min/max per bucket with the bucket regime; coolant max>=230 (240 critical) running; "
            "transmission max>=230 running; oil min<12 running; oil below band by rpm band "
            "(idle 15, low/mid 22, high 55) warm/hot; charging battery min<12.0 at rpm_low|mid|high with a "
            "same-bucket generator duty max>=90; parked battery min<11.8 (11.5 critical) trip_key 0 "
            "engine_off/engine_unknown; tire min<50/68 (45/60 critical) first 3 min of a trip or stationary; "
            "pair asymmetry |(L-R)-30-day median|>3 psi, a hit needs >=5 consecutive buckets. Rules with "
            "windows <=60 s count one bucket as a hit."
        ),
    }
    result.update(_usage(cpu0))
    if write:
        out_dir = Path(out_dir)
        path = out_dir / f"coarse-{result['stamp']}.json"
        _write_json(path, result)
        result["summary_path"] = str(path)
    return result


# --------------------------------------------------------------------------
# report


def _latest(out_dir: Path, pattern: str, *, untagged: bool = True) -> dict[str, object] | None:
    for path in sorted(out_dir.glob(pattern), reverse=True):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if untagged and value.get("tag"):
            continue
        value["summary_path"] = str(path)
        return value
    return None


def _owp(counts: dict | None) -> str:
    if not counts:
        return "0 / 0 / 0"
    return f"{counts.get('opened', 0)} / {counts.get('warned', 0)} / {counts.get('pushes', 0)}"


def family_table(recorded_window, recorded_all, current, new) -> str:
    lines = [
        "| family | recorded window | recorded all time | replay current | replay new |",
        "|---|---|---|---|---|",
    ]
    families = set()
    for summary in (recorded_window, recorded_all, current, new):
        if summary:
            families.update(summary.get("families", {}))
    for family in [name for name in FAMILY_ORDER if name in families]:
        cells = []
        for summary in (recorded_window, recorded_all, current, new):
            if summary is None:
                cells.append("n/a")
            else:
                cells.append(_owp(summary.get("families", {}).get(family)))
        lines.append(f"| {family} | " + " | ".join(cells) + " |")
    totals = []
    for summary in (recorded_window, recorded_all, current, new):
        totals.append("n/a" if summary is None else _owp(summary.get("totals")))
    lines.append("| **total** | " + " | ".join(totals) + " |")
    return "\n".join(lines)


def rule_table(summary: dict[str, object], recorded: dict[str, object] | None = None) -> str:
    lines = [
        "| rule | family | recorded window o/w/p | replay opened | warned | pushes | recovery | cancelled | parked openings | median s | max s | resolutions |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    names = set(summary.get("rules", {}))
    if recorded:
        names.update(r for r, v in recorded.get("rules", {}).items() if v.get("family") != "system")
    for rule in sorted(names):
        data = summary.get("rules", {}).get(rule) or {**_zero(), "family": family_of(rule), "resolutions": {}}
        rec = (recorded or {}).get("rules", {}).get(rule)
        resolutions = "; ".join(f"{k} x{v}" for k, v in list(data.get("resolutions", {}).items())[:4])
        lines.append(
            f"| {rule} | {data['family']} | {_owp(rec) if recorded else 'n/a'} | {data['opened']} | {data['warned']} | "
            f"{data['pushes']} | {data['recovery']} | {data['cancelled']} | {data['parked_openings']} | "
            f"{data.get('duration_median_seconds', '')} | {data.get('duration_max_seconds', '')} | {resolutions} |"
        )
    return "\n".join(lines)


def _cell(counts: dict | None) -> str:
    if not counts or not any(counts.get(k) for k in ("opened", "warned", "pushes")):
        return "-"
    return f"{counts.get('opened', 0)}/{counts.get('warned', 0)}/{counts.get('pushes', 0)}"


def trip_table(summary: dict[str, object], meta: dict[str, object], trip_ids: list[int], recorded: dict | None = None) -> str:
    """Per trip, per vehicle family: replay o/w/p (and recorded o/w/p)."""

    present = set()
    for source in (summary, recorded or {}):
        for trip_counts in source.get("trips", {}).values():
            present.update(trip_counts)
    families = [f for f in FAMILY_ORDER if f in present and f != "system"] or ["charging"]
    header = ["trip", "start (UTC)", "min"]
    header += [f"{f} replay" for f in families] + ["replay pushes"]
    if recorded is not None:
        header += [f"{f} recorded" for f in families] + ["recorded pushes"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for key in [str(t) for t in trip_ids] + ["parked"]:
        counts = summary.get("trips", {}).get(key, {})
        rec = (recorded or {}).get("trips", {}).get(key, {})
        if key == "parked" and not counts and not rec:
            continue
        info = meta.get(key, {})
        row = [key, str(info.get("started_at") or "")[:16], str(info.get("minutes", ""))]
        row += [_cell(counts.get(f)) for f in families]
        row.append(str(sum(v.get("pushes", 0) for f, v in counts.items() if f != "system")))
        if recorded is not None:
            row += [_cell(rec.get(f)) for f in families]
            row.append(str(sum(v.get("pushes", 0) for f, v in rec.items() if f != "system")))
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def acceptance(new: dict | None, coarse_result: dict | None) -> dict[str, dict[str, object]]:
    """Spec §8.2 checks, evaluated mechanically on the NEW replay."""

    coarse_non_battery = {}
    if coarse_result:
        for rule, values in coarse_result.get("totals", {}).get("rollup_only", {}).items():
            if not rule.startswith("battery_voltage_low_parked") and values.get("hits"):
                coarse_non_battery[rule] = values["hits"]
    checks: dict[str, dict[str, object]] = {}
    episodes = (new or {}).get("counts", {}).get("episodes", [])
    has_tier = any(e.get("tier") is not None for e in episodes)
    if new is None:
        checks["tier0_fires_only_parked_battery"] = {
            "status": "not_evaluable",
            "detail": "no replay of the new evaluator",
            "coarse_rollup_only_non_battery_hits": coarse_non_battery,
        }
        checks["tier1_le_1_warning_per_family_per_trip"] = {
            "status": "not_evaluable",
            "detail": "no replay of the new evaluator",
        }
    elif episodes and not has_tier:
        # An empty episode list is evaluable: nothing opened, so nothing fired.
        detail = "new-evaluator episodes carry no 'tier' field (rules task not merged?)"
        checks["tier0_fires_only_parked_battery"] = {
            "status": "not_evaluable",
            "detail": detail,
            "coarse_rollup_only_non_battery_hits": coarse_non_battery,
        }
        checks["tier1_le_1_warning_per_family_per_trip"] = {"status": "not_evaluable", "detail": detail}
    else:
        tier0 = Counter(e["rule"] for e in episodes if e.get("tier") == 0 and e.get("warned"))
        non_battery = {rule: n for rule, n in tier0.items() if family_of(rule) != "battery"}
        parked_battery = sum(n for rule, n in tier0.items() if family_of(rule) == "battery")
        status = "pass" if not non_battery and not coarse_non_battery else "fail"
        checks["tier0_fires_only_parked_battery"] = {
            "status": status,
            "tier0_warnings_by_rule": dict(tier0),
            "non_battery_tier0_warnings": non_battery,
            "parked_battery_warnings": parked_battery,
            "coarse_rollup_only_non_battery_hits": coarse_non_battery,
        }
        per = Counter(
            (e.get("trip"), e["family"]) for e in episodes if e.get("tier") == 1 and e.get("warned") and e.get("trip") is not None
        )
        worst = per.most_common(1)[0] if per else ((None, None), 0)
        over = {f"trip {trip} {family}": n for (trip, family), n in per.items() if n > 1}
        checks["tier1_le_1_warning_per_family_per_trip"] = {
            "status": "pass" if not over else "fail",
            "max_warnings_per_trip_family": worst[1],
            "worst": f"trip {worst[0][0]} {worst[0][1]}" if per else None,
            "violations": over,
            "trips_with_tier1_warnings": len({trip for trip, _f in per}),
        }
    tier2 = (new or {}).get("tier2") or {"status": "not_run"}
    if new is None or tier2.get("status") != "ok":
        checks["rr_slow_leak_notice"] = {
            "status": "not_evaluable",
            "detail": "no replay of the new evaluator" if new is None else f"tier 2: {tier2.get('status')}",
        }
    else:
        rr = [a for a in tier2.get("assessments", []) if a.get("rule") == "tire_pressure_rr_slow_leak"]
        fired = any(a.get("state") == "warning" for a in rr)
        numbers = {}
        for a in rr:
            for key in ("current", "drift", "deviation", "trend", "evidence"):
                if isinstance(a.get(key), dict):
                    numbers[key] = a[key]
        checks["rr_slow_leak_notice"] = {
            "status": "pass" if fired else "fail",
            "rr_assessments": len(rr),
            "state": rr[0].get("state") if rr else None,
            "numbers": numbers,
        }
    return checks


def report(
    out_dir: str | Path = OUT_DIR,
    *,
    current: dict | None = None,
    new: dict | None = None,
    coarse_result: dict | None = None,
    write: bool = True,
) -> dict[str, object]:
    """Merge the latest replay/coarse outputs into report-<stamp>.md/.json."""

    cpu0 = time.process_time()
    out_dir = Path(out_dir)
    current = current or _latest(out_dir, "replay-current-*.json")
    new = new or _latest(out_dir, "replay-new-*.json")
    coarse_result = coarse_result or _latest(out_dir, "coarse-*.json")
    tagged = []
    for path in sorted(out_dir.glob("replay-*@*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tagged.append(value)
    base = current or new
    manifest = (base or {}).get("manifest", {})
    recorded = (base or {}).get("recorded", {})
    checks = acceptance(new, coarse_result)
    complete = all(run.get("complete") for run in (current, new, coarse_result) if run)
    stamp = _stamp()
    md: list[str] = [f"# Early-warning replay report {stamp}", ""]
    md.append(f"complete: **{str(complete).lower()}**")
    md.append("")
    md.append("## Export")
    if manifest:
        md.append(
            f"- window {manifest.get('cutoff_at')} .. {manifest.get('exported_at')} ({manifest.get('days')} d); "
            f"replayed trips {manifest.get('trip_ids')}"
        )
        md.append(f"- rows {manifest.get('table_counts')}; copied {manifest.get('copied_rows')}")
        md.append(
            f"- source {manifest.get('source_uri')}; git HEAD {manifest.get('git_head')}; "
            f"{manifest.get('statement_count')} statements, slowest {manifest.get('max_statement_seconds')} s"
        )
    else:
        md.append("- no replay output found")
    for label, run in (("current", current), ("new", new)):
        if run:
            info = run.get("evaluator_info", {})
            md.append(
                f"- replay {label}: {info.get('source')} {info.get('ref') or ''} blob {info.get('blob')}; "
                f"complete {run.get('complete')}; ticks {run.get('ticks', {}).get('evaluated')}/"
                f"{run.get('ticks', {}).get('planned')} {run.get('ticks', {}).get('evaluated_by_kind')}; "
                f"CPU {run.get('cpu_seconds_total', run.get('cpu_seconds'))} s in "
                f"{len(run.get('chunks') or [1])} run(s) (per run "
                + ", ".join(f"{c.get('cpu_seconds')} s" for c in (run.get("chunks") or [run]))
                + f"); peak RSS {run.get('peak_rss_mb_max', run.get('peak_rss_mb'))} MB; "
                f"stride {run.get('options', {}).get('stride')}"
                + (f"; ABORTED: {run.get('abort_reason')}" if not run.get("complete") else "")
            )
    md.append("")
    md.append("## Rule families: opened / warned / pushes")
    md.append("")
    table = family_table(
        recorded.get("window"),
        recorded.get("all_time"),
        (current or {}).get("counts"),
        (new or {}).get("counts"),
    )
    md.append(table)
    md.append("")
    md.append(
        "Recorded window = episodes opened inside the export window (pushes = their outbox rows). "
        "`system` is recorded-only: interface/USB evaluators are not replayed."
    )
    for label, run in (("current", current), ("new", new)):
        if not run:
            continue
        md.append("")
        md.append(f"## Per rule, replay {label}")
        md.append("")
        md.append(rule_table(run.get("counts", {}), recorded.get("window")))
        totals = run.get("persistence_totals", {})
        md.append("")
        md.append(
            f"Lifecycle totals: {totals}; simulated deliveries {run.get('simulated_deliveries')}; "
            f"baseline memo {run.get('baseline_memo')}."
        )
    trip_run = new or current
    if trip_run:
        label = "new" if new else "current (new not run yet)"
        md.append("")
        md.append(f"## Per trip, replay {label} (right: recorded)")
        md.append("")
        md.append(
            trip_table(
                trip_run.get("counts", {}),
                trip_run.get("trip_meta", {}),
                trip_run.get("replayed_trip_ids", []),
                recorded.get("window"),
            )
        )
    md.append("")
    md.append("## Coarse rollup replay (tier-0 conditions on 60 s buckets)")
    md.append("")
    if coarse_result:
        md.append(
            f"Rollup-only trips {coarse_result.get('rollup_only_trip_ids')}; pair offsets "
            f"{coarse_result.get('pair_offsets')}."
        )
        md.append("")
        md.append(
            "| rule | rollup-only trips: trips hit / hits / buckets / critical | replayed trips: same | "
            "parked (trip_key 0, all dates): hits / buckets / critical |"
        )
        md.append("|---|---|---|---|")
        totals = coarse_result.get("totals", {})
        names = sorted(set(TIER0_KEYS) - {"tire_pressure_pair_asymmetry"}
                       | {"tire_pressure_pair_asymmetry:front", "tire_pressure_pair_asymmetry:rear"}
                       | {rule for scope in totals.values() for rule in scope})
        for rule in names:
            cells = []
            for scope in ("rollup_only", "replayed"):
                value = totals.get(scope, {}).get(rule) or {}
                cells.append(
                    f"{value.get('trips_hit', 0)} / {value.get('hits', 0)} / {value.get('buckets', 0)} / {value.get('critical_buckets', 0)}"
                )
            parked = totals.get("parked", {}).get(rule) or {}
            cells.append(
                f"{parked.get('hits', 0)} / {parked.get('buckets', 0)} / {parked.get('critical_buckets', 0)}"
                if rule == "battery_voltage_low_parked" else "-"
            )
            md.append(f"| {rule} | " + " | ".join(cells) + " |")
        md.append("")
        md.append(f"Parked battery buckets < 11.8 V (trip_key 0, all dates): {len(coarse_result.get('parked_battery_events', []))}")
        for event in coarse_result.get("parked_battery_events", [])[:20]:
            md.append(f"- {event['at'][:19]} min {event['minimum']} V after trip {event['after_trip']} ({event['regime']})")
        md.append("")
        md.append(f"Near-miss coolant buckets (running): {coarse_result.get('near_miss_totals')}")
        near_rows = [t for t in coarse_result.get("trips", []) if t.get("near_miss")]
        if near_rows:
            md.append("")
            md.append("| trip | start | raw | coolant >=215 | >=220 | >=225 | coolant max | trans max | oil min | battery min |")
            md.append("|---|---|---|---|---|---|---|---|---|---|")
            for t in near_rows:
                n, x = t.get("near_miss", {}), t.get("extremes", {})
                md.append(
                    f"| {t['trip']} | {str(t.get('started_at', ''))[:16]} | {t['raw_replayed']} | {n.get('coolant_ge_215', 0)} | "
                    f"{n.get('coolant_ge_220', 0)} | {n.get('coolant_ge_225', 0)} | {x.get('coolant_max')} | "
                    f"{x.get('transmission_max')} | {x.get('oil_min_running')} | {x.get('battery_min_running')} |"
                )
    else:
        md.append("not run")
    md.append("")
    md.append("## Tier 2")
    tier2 = (new or {}).get("tier2") or {"status": "no new replay"}
    md.append(f"status: {tier2.get('status')}")
    for a in tier2.get("assessments", []) or []:
        md.append(f"- {a.get('rule')}: {a.get('state')} {json.dumps({k: a.get(k) for k in ('current', 'deviation', 'drift') if a.get(k)}, default=str)[:400]}")
    companion = {}
    for label, run in (("current", current), ("new", new)):
        if run and run.get("companion_gaps"):
            companion[label] = run["companion_gaps"]
    md.append("")
    md.append("## Charging companion gaps")
    md.append(json.dumps(companion, default=str) if companion else "none reported (field absent in the replayed evaluators)")
    md.append("")
    md.append("## Acceptance checks (spec §8.2)")
    for key in ACCEPTANCE_KEYS:
        value = checks.get(key, {})
        md.append(f"- {key}: **{value.get('status')}** {json.dumps({k: v for k, v in value.items() if k != 'status'}, default=str)}")
    if tagged:
        md.append("")
        md.append("## Additional tagged runs")
        md.append("")
        md.append("| run | evaluator | ref | trips | complete | opened / warned / pushes |")
        md.append("|---|---|---|---|---|---|")
        for run in tagged:
            info = run.get("evaluator_info", {})
            md.append(
                f"| {run.get('tag')} | {run.get('evaluator')} | {info.get('ref') or info.get('source')} | "
                f"{run.get('options', {}).get('trips')} | {run.get('complete')} | {_owp(run.get('counts', {}).get('totals'))} |"
            )
    notes = []
    for run in (current, new):
        for note in (run or {}).get("notes", []):
            if note not in notes:
                notes.append(note)
    if notes:
        md.append("")
        md.append("## Notes")
        md.extend(f"- {note}" for note in notes)
    markdown = "\n".join(md) + "\n"
    result = {
        "kind": "report",
        "stamp": stamp,
        "complete": complete,
        "manifest": manifest,
        "family_table": table,
        "acceptance": checks,
        "inputs": {
            "current": (current or {}).get("summary_path"),
            "new": (new or {}).get("summary_path"),
            "coarse": (coarse_result or {}).get("summary_path"),
        },
        "families": {
            "recorded_window": (recorded.get("window") or {}).get("families"),
            "recorded_all_time": (recorded.get("all_time") or {}).get("families"),
            "replay_current": ((current or {}).get("counts") or {}).get("families"),
            "replay_new": ((new or {}).get("counts") or {}).get("families"),
        },
        "markdown": markdown,
    }
    result.update(_usage(cpu0))
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"report-{stamp}.md").write_text(markdown, encoding="utf-8")
        _write_json(out_dir / f"report-{stamp}.json", {k: v for k, v in result.items() if k != "markdown"})
        result["report_path"] = str(out_dir / f"report-{stamp}.md")
    return result


# --------------------------------------------------------------------------
# CLI


def _print_usage(label: str, value: dict[str, object]) -> None:
    print(
        f"[{label}] cpu_seconds={value.get('cpu_seconds')} peak_rss_mb={value.get('peak_rss_mb')}",
        file=sys.stderr,
    )


def _parse_trips(text: str | None) -> set[int] | None:
    if not text:
        return None
    return {int(part) for part in text.split(",") if part.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="output directory (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_export = sub.add_parser("export", help="bounded read-only export of the live historian")
    p_export.add_argument("--database", default=str(DEFAULT_DATABASE))
    p_export.add_argument("--days", type=int, default=7)
    p_export.add_argument("--out", default=None)
    p_export.add_argument("--force", action="store_true")
    p_export.add_argument("--deadline-seconds", type=float, default=STATEMENT_DEADLINE_SECONDS)
    p_replay = sub.add_parser("replay", help="replay evaluators over a copy of the export")
    p_replay.add_argument("--evaluator", choices=("current", "new", "both"), default="current")
    p_replay.add_argument("--export", default=None)
    p_replay.add_argument("--current-ref", default="HEAD")
    p_replay.add_argument("--stride", type=int, default=1)
    p_replay.add_argument("--trips", default=None, help="comma-separated trip ids")
    p_replay.add_argument("--gap-tick-hours", type=float, default=6.0)
    p_replay.add_argument("--cpu-budget", type=float, default=90.0)
    p_replay.add_argument("--rss-budget-mb", type=float, default=300.0)
    p_replay.add_argument("--tier2", action="store_true")
    p_replay.add_argument("--no-simulated-delivery", action="store_true")
    p_replay.add_argument("--tag", default=None, help="label a side run (excluded from the main report tables)")
    p_replay.add_argument("--full-evidence", action="store_true", help="keep the 128-bucket baseline preview")
    p_replay.add_argument(
        "--resume",
        default=None,
        help="continue an aborted run (its replay-*.json) on the same copy with a fresh budget; "
        "all other options come from that summary",
    )
    p_replay.add_argument("--max-ticks", type=int, default=None, help="stop this run after N ticks (resumable)")
    p_coarse = sub.add_parser("coarse", help="tier-0 conditions on rollup buckets")
    p_coarse.add_argument("--export", default=None)
    sub.add_parser("report", help="merge the latest outputs into a report")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    export_path = Path(getattr(args, "export", None) or getattr(args, "out", None) or out_dir / DEFAULT_EXPORT.name)

    if args.command == "export":
        manifest = export(
            args.database,
            export_path,
            days=args.days,
            force=args.force,
            deadline_seconds=args.deadline_seconds,
        )
        print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
        _print_usage("export", manifest)
        return 0
    if args.command == "coarse":
        result = coarse(export_path, out_dir=out_dir)
        print(json.dumps({"totals": result["totals"], "pair_offsets": result["pair_offsets"],
                          "parked_battery_events": result["parked_battery_events"],
                          "near_miss_totals": result["near_miss_totals"],
                          "summary_path": result.get("summary_path")}, indent=2, default=str))
        _print_usage("coarse", result)
        return 0
    if args.command == "report":
        result = report(out_dir)
        print(result["family_table"])
        print(f"report: {result.get('report_path')}", file=sys.stderr)
        _print_usage("report", result)
        return 0 if result["complete"] else EXIT_INCOMPLETE

    if args.resume:
        result = replay(
            resume=args.resume,
            cpu_budget=args.cpu_budget,
            rss_budget_mb=args.rss_budget_mb,
            max_ticks=args.max_ticks,
        )
        name = str(result["evaluator"])
        print(
            f"[replay {name} resume] complete={result['complete']} ticks={result['ticks'].get('evaluated')}/"
            f"{result['ticks'].get('planned')} chunks={len(result.get('chunks', []))} "
            f"summary={result.get('summary_path')}",
            file=sys.stderr,
        )
        _print_usage(f"replay {name} chunk", result)
        print(
            f"[replay {name} total] cpu_seconds={result.get('cpu_seconds_total')} "
            f"peak_rss_mb={result.get('peak_rss_mb_max')}",
            file=sys.stderr,
        )
        code = 0 if result["complete"] else EXIT_INCOMPLETE
        if result.get("tag"):
            return code
        report_dir = Path(args.resume).parent
        summary = report(report_dir, **{name: result})
        print(summary["family_table"])
        print(f"report: {summary.get('report_path')}", file=sys.stderr)
        _print_usage("report", summary)
        return code

    evaluators = ["current", "new"] if args.evaluator == "both" else [args.evaluator]
    code = 0
    runs: dict[str, dict] = {}
    for name in evaluators:
        result = replay(
            export_path,
            evaluator=name,
            current_ref=args.current_ref,
            stride=args.stride,
            trips=_parse_trips(args.trips),
            gap_tick_hours=args.gap_tick_hours,
            cpu_budget=args.cpu_budget,
            rss_budget_mb=args.rss_budget_mb,
            tier2=args.tier2,
            simulated_delivery=not args.no_simulated_delivery,
            out_dir=out_dir,
            tag=args.tag,
            full_evidence=args.full_evidence,
            max_ticks=args.max_ticks,
        )
        runs[name] = result
        print(
            f"[replay {name}] complete={result['complete']} ticks={result['ticks'].get('evaluated')}/"
            f"{result['ticks'].get('planned')} summary={result.get('summary_path')}",
            file=sys.stderr,
        )
        _print_usage(f"replay {name}", result)
        if not result["complete"]:
            code = EXIT_INCOMPLETE
            break
    coarse_result = None
    if args.evaluator == "both" and code == 0:
        coarse_result = coarse(export_path, out_dir=out_dir)
        _print_usage("coarse", coarse_result)
    if args.tag:
        return code
    result = report(out_dir, current=runs.get("current"), new=runs.get("new"), coarse_result=coarse_result)
    print(result["family_table"])
    print(f"report: {result.get('report_path')}", file=sys.stderr)
    _print_usage("report", result)
    return code


if __name__ == "__main__":
    sys.exit(main())
