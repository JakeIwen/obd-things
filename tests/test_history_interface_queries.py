"""Real ingestion/context regressions for bounded interface-history lookups."""
from datetime import datetime, timedelta, timezone

import pytest

from projects.vehicle_data.historian import OutOfOrderSnapshotError, TelemetryHistorian
from tests.test_vehicle_historian import role_aware_interface, role_status, snapshot


START = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
ROLES = ("b-can", "c-can", "can-ch")


def payload(at, *, roles=ROLES, generation="generation-a"):
    result = snapshot(at, [], {}, running=False)
    if roles is None:
        result["status"].pop("interface")
        return result
    interfaces = {
        role: role_status(role, f"can{index}", 125000 if role == "b-can" else 500000)
        for index, role in enumerate(roles)
    }
    interface = role_aware_interface(roles=interfaces)
    interface["role_interfaces"]["generation"] = generation
    result["status"]["interface"] = interface
    return result


def ingest(historian, seconds, **kwargs):
    at = START + timedelta(seconds=seconds)
    return historian.ingest_snapshot(payload(at, **kwargs), captured_at=at)


def seed_legacy_placeholder(historian, snapshot_id, role, *, sample):
    """Represent old producer output that current ingestion deliberately rejects."""
    at = START.isoformat()
    captured_us = round(START.timestamp() * 1_000_000)
    with historian.connection_lock, historian.connection as connection:
        if sample:
            connection.execute(
                "INSERT INTO interface_samples(snapshot_id,role,captured_us,health,reason) "
                "VALUES(?,?,?,'unknown','legacy_placeholder')",
                (snapshot_id, role, captured_us),
            )
        connection.execute(
            """INSERT INTO interface_gaps(
                role,state,reason,started_us,started_at,last_seen_us,last_seen_at,
                first_snapshot_id,last_snapshot_id
            ) VALUES(?,'missing','interface_role_absent',?,?,?,?,?,?)""",
            (role, captured_us, at, captured_us, at, snapshot_id, snapshot_id),
        )


def vm_work(historian, operation):
    steps = 0

    def tick():
        nonlocal steps
        steps += 1
        return 0

    historian.connection.set_progress_handler(tick, 1)
    try:
        result = operation()
    finally:
        historian.connection.set_progress_handler(None, 0)
    return result, steps


def add_repeated_history(historian, *, count, generation="generation-a"):
    """Append valid older facts in bulk so fixture setup does not dominate tests."""
    with historian.connection_lock, historian.connection as connection:
        last_id = connection.execute("SELECT max(id) FROM snapshots").fetchone()[0]
        rows = [(last_id + index, START + timedelta(seconds=index + 10))
                for index in range(1, count + 1)]
        connection.executemany(
            "INSERT INTO snapshots(id,ingest_key,captured_us,captured_at,regime) VALUES(?,?,?,?,?)",
            ((number, f"bulk:{number}", round(at.timestamp() * 1_000_000),
              at.isoformat(), "engine_unknown:unknown:unknown:unknown") for number, at in rows),
        )
        connection.executemany(
            "INSERT INTO interface_samples(snapshot_id,role,captured_us,health,reason) "
            "VALUES(?,?,?,'healthy','listen_only_ready')",
            ((number, role, round(at.timestamp() * 1_000_000))
             for number, at in rows for role in ROLES),
        )
        connection.executemany(
            "INSERT INTO system_health_samples(snapshot_id,captured_us,topology_generation,"
            "issues_json,active_inhibits_json) VALUES(?,?,?,'[]','[]')",
            ((number, round(at.timestamp() * 1_000_000), generation) for number, at in rows),
        )
    return count + 11


def test_retains_all_historical_roles_and_retires_legacy_gaps(tmp_path):
    with TelemetryHistorian(tmp_path / "history.sqlite3") as historian:
        first = ingest(historian, 0, roles=(*ROLES, "owner-observer"))
        seed_legacy_placeholder(historian, first.snapshot_id, "can9", sample=True)
        # The old raw sample for this placeholder was already pruned.
        seed_legacy_placeholder(historian, first.snapshot_id, "default", sample=False)
        second = ingest(historian, 5, roles=("c-can",))
        context = historian.system_health_context(second.snapshot_id)
        assert set(context["roles"]) == {"c-can"}
        assert set(context["active_interface_gaps"]) == {"b-can", "can-ch", "owner-observer"}
        assert all(row["reason"] == "interface_role_absent"
                   for row in context["active_interface_gaps"].values())
        gaps = {row["key"]: row for row in historian.list_gaps("interface")}
        assert gaps["can9"]["ended_at"] == second.captured_at
        assert gaps["default"]["ended_at"] == second.captured_at
        assert "can9" in historian.system_health_context(first.snapshot_id)["roles"]
        third = ingest(historian, 10, roles=None)
        context = historian.system_health_context(third.snapshot_id)
        assert set(context["active_interface_gaps"]) == {
            "b-can", "can-ch", "c-can", "owner-observer", "interface-status",
        }


def test_prior_topology_is_relative_to_requested_snapshot_and_survives_holes(tmp_path):
    with TelemetryHistorian(tmp_path / "history.sqlite3") as historian:
        entries = [ingest(historian, index * 5, generation=generation)
                   for index, generation in enumerate((None, "a", None, "b", None, "c"))]
        for entry in entries[:2]:
            context = historian.system_health_context(entry.snapshot_id)
            assert context["previous_topology_generation"] is None
            assert context["topology_changed"] is False
        with historian.connection_lock, historian.connection as connection:
            connection.execute("DELETE FROM snapshots WHERE id=?", (entries[2].snapshot_id,))
        assert historian.system_health_context(entries[3].snapshot_id)["previous_topology_generation"] == "a"
        unknown = historian.system_health_context(entries[4].snapshot_id)
        assert unknown["previous_topology_generation"] == "b"
        assert unknown["topology_changed"] is False
        newest = historian.system_health_context(entries[5].snapshot_id)
        assert newest["previous_topology_generation"] == "b"
        assert newest["topology_changed"] is True
        with historian.connection_lock, historian.connection as connection:
            connection.execute("DELETE FROM snapshots WHERE id=?", (entries[3].snapshot_id,))
        assert historian.system_health_context(entries[5].snapshot_id)["previous_topology_generation"] == "a"


def test_public_ingestion_enforces_id_and_capture_order(tmp_path):
    with TelemetryHistorian(tmp_path / "history.sqlite3") as historian:
        first = ingest(historian, 0, generation="a")
        second = ingest(historian, 10, generation="b")
        for seconds in (5, 10):
            at = START + timedelta(seconds=seconds)
            with pytest.raises(OutOfOrderSnapshotError):
                historian.ingest_snapshot(payload(at), captured_at=at,
                                          ingest_key=f"rejected:{seconds}")
        duplicate = ingest(historian, 0, generation="ignored")
        assert duplicate.duplicate is True
        assert duplicate.snapshot_id == first.snapshot_id
        third = ingest(historian, 15, generation="c")
        assert first.snapshot_id < second.snapshot_id < third.snapshot_id
        assert historian.system_health_context(third.snapshot_id)["previous_topology_generation"] == "b"


def test_repeated_old_history_does_not_scale_ingest_or_topology_lookup_work(tmp_path):
    with TelemetryHistorian(tmp_path / "history.sqlite3") as historian:
        ingest(historian, 0)
        before, small_ingest = vm_work(historian, lambda: ingest(historian, 5))
        context, small_context = vm_work(
            historian, lambda: historian.system_health_context(before.snapshot_id),
        )
        next_second = add_repeated_history(historian, count=10_000)
        after, large_ingest = vm_work(historian, lambda: ingest(historian, next_second))
        enlarged, large_context = vm_work(
            historian, lambda: historian.system_health_context(after.snapshot_id),
        )
        assert after.stored_samples == before.stored_samples == 0
        assert set(enlarged["roles"]) == set(context["roles"]) == set(ROLES)
        assert enlarged["previous_topology_generation"] == context["previous_topology_generation"]
        assert enlarged["active_interface_gaps"] == context["active_interface_gaps"] == {}
        # Allow B-tree depth and interpreter/SQLite variation while rejecting a
        # walk/sort of the 30,000 repeated role rows or 10,000 older generations.
        assert 0 < large_ingest < small_ingest + 2_000
        assert 0 < large_context < small_context + 2_000


@pytest.mark.parametrize("known_before_gap", [None, "generation-a"])
def test_long_unknown_topology_history_has_bounded_work(tmp_path, known_before_gap):
    with TelemetryHistorian(tmp_path / "history.sqlite3") as historian:
        ingest(historian, 0, generation=known_before_gap)
        before = ingest(historian, 5, generation=None)
        context, small_context = vm_work(
            historian, lambda: historian.system_health_context(before.snapshot_id),
        )
        next_second = add_repeated_history(historian, count=10_000, generation=None)
        after = ingest(historian, next_second, generation="returned")
        enlarged, large_context = vm_work(
            historian, lambda: historian.system_health_context(after.snapshot_id),
        )
        assert enlarged["previous_topology_generation"] == known_before_gap
        assert enlarged["topology_changed"] is (known_before_gap is not None)
        assert historian.system_health_context(before.snapshot_id) == context
        # Neither a long observation gap nor a never-known topology may require
        # scanning every retained null generation to recover the prior evidence.
        assert 0 < large_context < small_context + 2_000
