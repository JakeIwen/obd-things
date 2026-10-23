"""Indexed interface-history lookups; callers retain the historian lock."""

from __future__ import annotations

import sqlite3


def historical_interface_roles(connection: sqlite3.Connection) -> set[str]:
    """Return every recorded role without walking repeated samples of each one."""
    # Each min() seeks the next distinct leading key of interface_samples_lookup
    # (role, captured_us). DISTINCT would walk all retained rows of each role.
    rows = connection.execute(
        """
        WITH RECURSIVE roles(role) AS (
            SELECT min(role) FROM interface_samples
            UNION ALL
            SELECT (
                SELECT min(role) FROM interface_samples WHERE role>roles.role
            )
            FROM roles WHERE role IS NOT NULL
        )
        SELECT role FROM roles WHERE role IS NOT NULL
        """
    )
    return {row[0] for row in rows}


def previous_topology_sample(
    connection: sqlite3.Connection, snapshot_id: int,
) -> sqlite3.Row | None:
    """Return the newest earlier recorded, non-null topology generation."""
    # Ingestion rejects non-increasing captured_us before allocating its rowid;
    # retention only removes rows. Snapshot order therefore also orders capture
    # times. The known-topology index can seek backwards, skipping null evidence
    # without sorting the history or walking a long observation gap.
    return connection.execute(
        """
        SELECT topology_generation,captured_us
        FROM system_health_samples
        WHERE snapshot_id<? AND topology_generation IS NOT NULL
        ORDER BY snapshot_id DESC LIMIT 1
        """,
        (snapshot_id,),
    ).fetchone()
