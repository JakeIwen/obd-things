"""Bounded offline profiles of dashboard history, fuel economy and warnings.

Input must be a finalized SQLite backup without a WAL, never the live database.
Every evaluation runs on a disposable in-memory copy within worker limits.
Its timings measure query work, not production Pi disk I/O. Optional context JSON contains an
``at`` timestamp and saved ``dtc_cache``, ``van_scan_cache``, ``custom_rules`` or
``drift_warnings`` objects. Absent caches stay absent; nothing reads live paths.
Optional ``snapshot`` plus ``snapshot_captured_at`` profiles one later ingest,
with hourly maintenance and external notification delivery explicitly excluded.
Use the telemetry-history-profile compute task for substantial databases.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
import cProfile
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import pstats
import signal
import sqlite3
import tempfile
import time
from unittest.mock import patch

from lib.reports import atomic_json, require_distinct_output
from projects.vehicle_data.early_warning import EarlyWarningEvaluator
from projects.vehicle_data.fuel_economy import attach_fuel_economy
from projects.vehicle_data.historian import TelemetryHistorian
from projects.vehicle_data.insights import TelemetryInsights


REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO / "tmp/vehicle_data/history-profile"
CLOCK_MODULES = (
    "projects.vehicle_data.history.queries",
    "projects.vehicle_data.history.rollups",
    "projects.vehicle_data.history.advisories",
    "projects.vehicle_data.insights",
    "projects.vehicle_data.warning_engine.samples",
)
CACHE_FILES = {
    "dtc_cache": "dtc-cache.json",
    "van_scan_cache": "van-scan.json",
    "custom_rules": "custom-rules.json",
    "drift_warnings": "drift-warnings.json",
}


class ProfileBudgetExceeded(RuntimeError):
    """The bounded offline run exhausted its elapsed-time budget."""


class SQLRecorder:
    """Measure SQLite execute/fetch work, excluding Python between calls."""

    def __init__(self, deadline):
        self.deadline = deadline
        self.rows = {}
        self.active = None

    def statement(self, sql, parameters):
        sql = " ".join(sql.split())
        key = (sql, json.dumps(parameters, sort_keys=True))
        if key not in self.rows:
            if len(self.rows) >= 4096:
                raise ProfileBudgetExceeded("more than 4096 distinct statements")
            self.rows[key] = {
                "sql": sql, "parameters": parameters, "calls": 0,
                "wall_seconds": 0.0, "cpu_seconds": 0.0, "rows": 0,
                "vm_steps_approx": 0,
            }
        entry = self.rows[key]
        entry["calls"] += 1
        return entry

    def progress(self):
        if self.active is not None:
            self.active["vm_steps_approx"] += 1000
        return int(time.monotonic() > self.deadline)

    def measure(self, entry, operation, *args):
        if time.monotonic() > self.deadline:
            raise ProfileBudgetExceeded("profile deadline exceeded")
        wall, cpu = time.perf_counter(), time.process_time()
        previous, self.active = self.active, entry
        try:
            return operation(*args)
        finally:
            self.active = previous
            entry["wall_seconds"] += time.perf_counter() - wall
            entry["cpu_seconds"] += time.process_time() - cpu


class MeasuredCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.entry = self.connection.recorder.statement(sql, parameters)
        return self.connection.recorder.measure(self.entry, super().execute, sql, parameters)

    def fetchone(self):
        row = self.connection.recorder.measure(self.entry, super().fetchone)
        self.entry["rows"] += int(row is not None)
        return row

    def fetchall(self):
        rows = self.connection.recorder.measure(self.entry, super().fetchall)
        self.entry["rows"] += len(rows)
        return rows

    def __next__(self):
        row = self.connection.recorder.measure(self.entry, super().__next__)
        self.entry["rows"] += 1
        return row


class MeasuredConnection(sqlite3.Connection):
    def cursor(self, factory=MeasuredCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)


@contextmanager
def frozen_clock(at):
    class ClockType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockType):
        @classmethod
        def now(cls, tz=None):
            return at.replace(tzinfo=None) if tz is None else at.astimezone(tz)

    with ExitStack() as stack:
        for module in CLOCK_MODULES:
            stack.enter_context(patch(module + ".datetime", Clock))
        yield


@contextmanager
def elapsed_budget(seconds):
    def exhausted(_signum, _frame):
        raise ProfileBudgetExceeded("profile elapsed-time budget exceeded")

    previous = signal.signal(signal.SIGALRM, exhausted)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield time.monotonic() + seconds
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous)


def saved_database(source, destination, deadline):
    """Load a finalized read-only evidence database into the memory connection."""
    source = source.resolve(strict=True)
    if source == Path("/var/lib/van-telemetry/history.sqlite3"):
        raise ValueError("use a consistent saved backup, never the live historian")
    if Path(str(source) + "-wal").exists():
        raise ValueError("saved input must have no WAL; make a consistent SQLite backup")
    before = source.stat()

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise ProfileBudgetExceeded("database backup deadline exceeded")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        latest = conn.execute(
            "SELECT captured_at FROM snapshots ORDER BY captured_us DESC LIMIT 1"
        ).fetchone()
        indexes = [{"name": row[0], "sql": row[1]} for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' ORDER BY name"
        )]
        if latest is None:
            raise ValueError("saved historian has no snapshots")
        conn.backup(destination, pages=1024, progress=progress)
    after = source.stat()
    signature = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if signature(after) != signature(before) or Path(str(source) + "-wal").exists():
        raise ValueError("saved database changed while copied")
    return {"latest_snapshot_at": latest[0], "database_bytes": before.st_size,
            "input_schema_indexes": indexes}


def open_profile_historian(source, database_label, recorder):
    connection = sqlite3.connect(
        ":memory:", factory=MeasuredConnection, check_same_thread=False, timeout=10,
    )
    connection.recorder = recorder
    connection.set_progress_handler(recorder.progress, 1000)

    def connect_for_historian(database, **_kwargs):
        if str(database) != str(database_label):
            raise ValueError("unexpected connection during offline historian construction")
        return connection

    try:
        started = time.perf_counter()
        metadata = saved_database(source, connection, recorder.deadline)
        metadata["clone_load_wall_seconds"] = time.perf_counter() - started
        started = time.perf_counter()
        # Run the real constructor and schema/index initialization on the loaded
        # memory database. The scratch filename is only a sidecar-location label.
        with patch("projects.vehicle_data.history.store.sqlite3.connect", connect_for_historian):
            historian = TelemetryHistorian(database_label)
        metadata["schema_init_wall_seconds"] = time.perf_counter() - started
        connection.execute("PRAGMA temp_store=MEMORY")
        metadata["database_list"] = [list(row) for row in connection.execute("PRAGMA database_list")]
        return historian, metadata
    except BaseException:
        connection.close()
        raise


def write_context(context, scratch):
    for key, name in CACHE_FILES.items():
        if key in context:
            atomic_json(scratch / name, context[key])


def statement_plans(connection, rows):
    planned = []
    for row in sorted(rows, key=lambda item: item["wall_seconds"], reverse=True)[:100]:
        row = dict(row)
        if row["sql"].upper().startswith(("SELECT ", "WITH ")):
            try:
                row["query_plan"] = [list(item) for item in sqlite3.Connection.execute(
                    connection, "EXPLAIN QUERY PLAN " + row["sql"], row["parameters"]
                )]
            except sqlite3.Error as error:
                row["query_plan_error"] = str(error)
        planned.append(row)
    return planned


def semantic_response(value, scratch):
    """Replace only the disposable directory name; retain every response field."""
    if isinstance(value, str):
        return value.replace(str(scratch), "<scratch>")
    if isinstance(value, list):
        return [semantic_response(item, scratch) for item in value]
    if isinstance(value, dict):
        return {key: semantic_response(item, scratch) for key, item in value.items()}
    return value


def profile_phase(name, operation, recorder, output, historian):
    recorder.rows = {}
    profiler = cProfile.Profile()
    wall, cpu = time.perf_counter(), time.process_time()
    profiler.enable()
    try:
        value = operation()
    finally:
        profiler.disable()
    elapsed = time.perf_counter() - wall
    cpu_seconds = time.process_time() - cpu
    profiler.dump_stats(str(output / (name + ".pstats")))
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(80)
    (output / (name + ".txt")).write_text(stream.getvalue())
    semantic = semantic_response(value, Path(historian.database).parent)
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    atomic_json(output / (name + "-response.json"), semantic, sort_keys=True)
    return {
        "name": name, "wall_seconds": elapsed, "cpu_seconds": cpu_seconds,
        "response_sha256": hashlib.sha256(encoded).hexdigest(),
        "statement_count": sum(row["calls"] for row in recorder.rows.values()),
        "vm_steps_approx": sum(row["vm_steps_approx"] for row in recorder.rows.values()),
        "sqlite_wall_seconds": sum(row["wall_seconds"] for row in recorder.rows.values()),
        "statements": statement_plans(historian.connection, recorder.rows.values()),
    }, value


def profile_ingest(insights, context, recorder, output):
    """Replay one saved snapshot after queries, excluding first-start maintenance."""
    if "snapshot" not in context:
        return None
    captured = datetime.fromisoformat(context["snapshot_captured_at"].replace("Z", "+00:00"))
    if captured.tzinfo is None:
        raise ValueError("snapshot_captured_at must be timezone-aware")
    latest = insights.historian.connection.execute(
        "SELECT captured_us FROM snapshots ORDER BY captured_us DESC LIMIT 1"
    ).fetchone()[0]
    if round(captured.timestamp() * 1_000_000) <= latest:
        raise ValueError("saved snapshot must have been captured after the database backup")
    # Steady-state ingestion normally skips this hourly check. Exclude its
    # first-start cost explicitly instead of mixing retention/drift into a tick.
    insights._maintenance_next_check = float("inf")
    with frozen_clock(captured):
        measured, _ = profile_phase(
            "ingest", lambda: asdict(insights.ingest_snapshot(
                context["snapshot"], captured_at=captured, ingest_key="offline-cpu-profile",
            )), recorder, output, insights.historian,
        )
    measured["scope"] = "Saved snapshot ingestion, warning evaluation and persistence; hourly maintenance and external notification delivery excluded."
    return measured


def profile_loaders(historian, context, recorder, output, report, at, repeats):
    scratch = Path(historian.database).parent
    evaluator = EarlyWarningEvaluator(
        historian, custom_rules_path=scratch / CACHE_FILES["custom_rules"],
    )
    insights = TelemetryInsights(
        historian, warning_evaluator=evaluator,
        dtc_cache_path=scratch / CACHE_FILES["dtc_cache"],
        van_scan_cache_path=scratch / CACHE_FILES["van_scan_cache"],
    )
    report["schema_indexes"] = [dict(row) for row in historian.connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='index' ORDER BY name"
    )]
    for iteration in range(repeats):
        phases = (("history", insights.history_response), ("health", insights.health_response))
        for name, operation in phases:
            measured, value = profile_phase(
                f"{iteration}-{name}", operation, recorder, output, historian,
            )
            report["phases"].append(measured)
            if name == "history":
                measured, _ = profile_phase(
                    f"{iteration}-fuel", lambda: attach_fuel_economy(value, historian, now=at),
                    recorder, output, historian,
                )
                report["phases"].append(measured)
        measured, _ = profile_phase(
            f"{iteration}-dtcs", insights.dtc_response, recorder, output, historian,
        )
        report["phases"].append(measured)
        atomic_json(output / "report.json", report)
    ingestion = profile_ingest(insights, context, recorder, output)
    if ingestion is not None:
        report["phases"].append(ingestion)


def profile_saved(database, output, *, context=None, repeats=3, budget_seconds=180):
    """Profile unchanged public queries on a disposable, fixed-time clone."""
    if not 1 <= repeats <= 5 or not 1 <= budget_seconds <= 600:
        raise ValueError("repeats must be 1..5 and budget_seconds 1..600")
    context = context or {}
    output = output.resolve()
    require_distinct_output(output, [database])
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "complete": False, "phases": [], "context_fields": sorted(context),
        "response_normalization": "Only disposable directory names become <scratch>; clocks are frozen, no fields removed.",
        "scope": "History, fuel, health and DTC loaders, optional saved-snapshot ingestion; no broker acquisition, cruise sidecars or external notification delivery.",
        "storage": "Disposable in-memory SQLite backup and MEMORY temp store, within unchanged worker limits; timings do not model Pi disk I/O.",
    }
    with elapsed_budget(budget_seconds) as deadline, tempfile.TemporaryDirectory(
        prefix="history-profile-", dir=output.parent,
    ) as directory:
        scratch = Path(directory)
        write_context(context, scratch)
        recorder = SQLRecorder(deadline)
        historian, metadata = open_profile_historian(database, scratch / "history.sqlite3", recorder)
        with historian:
            report.update(metadata, sqlite_version=sqlite3.sqlite_version)
            at = datetime.fromisoformat(context.get("at", metadata["latest_snapshot_at"]).replace("Z", "+00:00"))
            if at.tzinfo is None:
                raise ValueError("profile time must be timezone-aware")
            report["at"] = at.isoformat()
            with frozen_clock(at):
                profile_loaders(historian, context, recorder, output, report, at, repeats)
        report["complete"] = True
        atomic_json(output / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("context", type=Path, nargs="?")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--budget-seconds", type=int, default=180)
    args = parser.parse_args(argv)
    context = json.loads(args.context.read_text()) if args.context else None
    try:
        report = profile_saved(args.database, args.output, context=context,
                               repeats=args.repeats, budget_seconds=args.budget_seconds)
    except (ValueError, OSError, sqlite3.Error, MemoryError, ProfileBudgetExceeded) as error:
        parser.exit(2, f"history profile failed: {error}\n")
    print(json.dumps({"complete": report["complete"], "at": report["at"],
                      "phases": [{k: row[k] for k in ("name", "wall_seconds", "cpu_seconds")}
                                 for row in report["phases"]]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
