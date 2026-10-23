"""Offline profiler checks through its real SQLite and public CLI boundaries."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from projects.vehicle_data.historian import TelemetryHistorian
from tests.test_vehicle_historian import available, definition, snapshot
from tools.telemetry_history_profile import main, profile_saved


def saved_history(path):
    start = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    definitions = [definition(name, unit) for name, unit in (
        ("engine.rpm", "rpm"), ("vehicle.speed", "mph"), ("engine.fuel_rate", "gal/h"),
    )]
    with TelemetryHistorian(path) as historian:
        for index in range(15):
            at = start + timedelta(seconds=index * 5)
            values = {row["name"]: available(row, value, at)
                      for row, value in zip(definitions, (1500.0, 40.0, 2.0))}
            historian.ingest_snapshot(snapshot(at, definitions, values), captured_at=at)
        historian.refresh_rollups(through=at)
    return at


def test_profile_saved_preserves_evidence_and_measures_real_queries(tmp_path):
    source = tmp_path / "saved.sqlite3"
    at = saved_history(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    reports = [profile_saved(source, tmp_path / name, repeats=1, budget_seconds=30)
               for name in ("first", "second")]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not list(tmp_path.glob("saved.sqlite3-*"))
    for report in reports:
        assert report["complete"] is True
        assert report["at"] == at.isoformat()
        assert "in-memory" in report["storage"]
        assert all(row[2] == "" for row in report["database_list"])
        phases = {row["name"]: row for row in report["phases"]}
        assert phases["0-history"]["statement_count"] > 0
        assert phases["0-history"]["sqlite_wall_seconds"] > 0
        assert any(row.get("query_plan") for row in phases["0-history"]["statements"])
        assert phases["0-fuel"]["statement_count"] > 0
    assert [row["response_sha256"] for row in reports[0]["phases"]] == [
        row["response_sha256"] for row in reports[1]["phases"]
    ]
    fuel = json.loads((tmp_path / "first/0-fuel-response.json").read_text())
    assert fuel["current_trip"]["fuel_economy"]["mpg"] == 20.0
    assert (tmp_path / "first/0-history.pstats").stat().st_size > 0
    assert not list(tmp_path.glob("history-profile-*"))
    assert sorted(path.name for path in tmp_path.rglob("*.sqlite3")) == ["saved.sqlite3"]


def test_profiles_saved_snapshot_ingestion_on_clone_only(tmp_path):
    source = tmp_path / "saved.sqlite3"
    at = saved_history(source) + timedelta(seconds=5)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    rpm = definition("engine.rpm", "rpm")
    context = {"snapshot": snapshot(at, [rpm], {rpm["name"]: available(rpm, 1500.0, at)}),
               "snapshot_captured_at": at.isoformat()}
    result = profile_saved(source, tmp_path / "profile", context=context, repeats=1)
    phase = result["phases"][-1]
    assert phase["name"] == "ingest"
    assert phase["statement_count"] > 0
    response = json.loads((tmp_path / "profile/ingest-response.json").read_text())
    assert response["duplicate"] is False
    assert response["advisory_checkpoint_complete"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_cli_rejects_wal_input_without_changing_it(tmp_path):
    source = tmp_path / "saved.sqlite3"
    saved_history(source)
    wal = Path(str(source) + "-wal")
    wal.write_bytes(b"not a finalized backup")
    with pytest.raises(SystemExit) as result:
        main([str(source), "--output", str(tmp_path / "profile")])
    assert result.value.code == 2
    assert wal.read_bytes() == b"not a finalized backup"


def test_profile_rejects_output_input_alias(tmp_path):
    source = tmp_path / "saved.sqlite3"
    saved_history(source)
    with pytest.raises(ValueError, match="differ"):
        profile_saved(source, source, repeats=1)
