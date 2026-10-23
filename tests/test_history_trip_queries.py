"""Real SQLite trip queries stay bounded as unrelated rolled history grows."""
from datetime import datetime, timedelta, timezone

from projects.vehicle_data.fuel_economy import METRIC_UNITS, economy
from projects.vehicle_data.historian import TelemetryHistorian
from tests.test_vehicle_historian import available, definition, snapshot


START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def trip_history(path):
    definitions = [definition(name, unit) for name, unit in (
        ("engine.rpm", "rpm"), ("vehicle.speed", "mph"), ("engine.fuel_rate", "gal/h"),
    )]
    with TelemetryHistorian(path) as historian:
        for trip in range(3):
            start = START + timedelta(hours=trip)
            for index in range(24):
                at = start + timedelta(seconds=5 * index)
                values = (800.0 if index < 12 else 2000.0, 40.0, 2.0)
                readings = {d["name"]: available(d, value, at)
                            for d, value in zip(definitions, values)}
                historian.ingest_snapshot(snapshot(at, definitions, readings), captured_at=at)
            if trip < 2:
                at = start + timedelta(seconds=420)
                readings = {d["name"]: available(d, 0.0, at) for d in definitions}
                historian.ingest_snapshot(snapshot(at, definitions, readings, running=False),
                                          captured_at=at)
        historian.refresh_rollups(through=start + timedelta(seconds=90))
        # Simulate an existing database from before the additive index migration.
        historian.connection.execute("DROP INDEX IF EXISTS metric_rollups_trip")


def add_old_rollups(historian):
    """Stress the same metrics on the oldest, unselected trip only."""
    for metric in METRIC_UNITS:
        template = dict(historian.connection.execute(
            "SELECT * FROM metric_rollups WHERE trip_key=1 AND metric=? LIMIT 1", (metric,),
        ).fetchone())
        columns = list(template)
        rows = []
        for index in range(10_000):
            row = dict(template)
            row["bucket_us"] = int(START.timestamp() * 1_000_000) - (index + 1) * 60_000_000
            row["first_us"] = row["bucket_us"]
            row["last_us"] = row["bucket_us"] + 55_000_000
            rows.append(tuple(row[column] for column in columns))
        historian.connection.executemany(
            f"INSERT INTO metric_rollups({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            rows,
        )
    historian.connection.commit()


def selected_trip_products(historian):
    comparison = historian.trip_comparison(tuple(METRIC_UNITS), prior_trip_limit=1)
    totals = historian.running_totals_for_trip(3, METRIC_UNITS)
    return {"comparison": comparison, "totals": totals, "economy": economy(totals)}


def measured(connection, operation):
    steps = 0

    def progress():
        nonlocal steps
        steps += 100
        return 0

    connection.set_progress_handler(progress, 100)
    try:
        result = operation()
    finally:
        connection.set_progress_handler(None, 0)
    return result, steps


def test_existing_history_migrates_without_trip_queries_scanning_old_trips(tmp_path):
    path = tmp_path / "history.sqlite3"
    trip_history(path)
    with TelemetryHistorian(path) as historian:
        before, work_before = measured(historian.connection, lambda: selected_trip_products(historian))
        assert before["totals"] == {"vehicle.speed": (960.0, 24), "engine.fuel_rate": (48.0, 24)}
        assert before["economy"]["mpg"] == 20.0
        speed = before["comparison"]["metrics"]["vehicle.speed"]["current_trip"]
        assert speed["sample_count"] == 24
        assert speed["aggregate_basis"] == "minute_rollups_plus_raw_tail"
        add_old_rollups(historian)
        after, work_after = measured(historian.connection, lambda: selected_trip_products(historian))
        assert after == before
        # A B-tree lookup may deepen; visiting the 20,000 unrelated rows may not.
        assert work_after < work_before + 3_000
        end = START + timedelta(hours=2, seconds=120)
        series = historian.metric_series("vehicle.speed", end=end, window_seconds=180, trip_id=3)
        # Removing the optimization only on this disposable database proves the
        # measured result matches the original query plan, including its sums.
        historian.connection.execute("DROP INDEX metric_rollups_trip")
        assert historian.metric_series("vehicle.speed", end=end, window_seconds=180, trip_id=3) == series
    with TelemetryHistorian(path) as reopened:
        assert reopened.metric_series("vehicle.speed", end=end, window_seconds=180, trip_id=3) == series
