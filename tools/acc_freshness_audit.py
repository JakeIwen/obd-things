#!/usr/bin/env python3
"""Bounded read-only freshness audit of exactly one historian trip.

No CAN, service, or interface access. One metric query at a time, at most
20,000 rows each. Output defaults below tmp/; a fresh observation is the
historian's saved freshness, not a recomputation against the current clock.
"""

import argparse
import json
from pathlib import Path
import sqlite3


REPO = Path(__file__).resolve().parents[1]
METRICS = ("vehicle.speed", "engine.oil_pressure", "vehicle.speed_limit",
           "acc.state", "acc.set_speed")
MAX_ROWS = 20_000


def audit(database, trip_id):
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro",
                         uri=True, timeout=5) as connection:
        connection.execute("PRAGMA query_only=ON")
        snapshots = connection.execute(
            "SELECT id FROM snapshots WHERE trip_id=? AND vehicle_running=1 LIMIT ?",
            (trip_id, MAX_ROWS + 1),
        ).fetchall()
        if not snapshots or len(snapshots) > MAX_ROWS:
            raise ValueError("trip has no running snapshots or exceeds the 20,000-row audit bound")
        counts = {}
        for metric in METRICS:
            rows = connection.execute(
                "SELECT m.observed_us,m.freshness FROM metric_samples m "
                "JOIN snapshots s ON s.id=m.snapshot_id "
                "WHERE m.trip_id=? AND m.metric=? AND s.vehicle_running=1 "
                "ORDER BY m.captured_us LIMIT ?", (trip_id, metric, MAX_ROWS + 1),
            ).fetchall()
            if len(rows) > MAX_ROWS:
                raise ValueError("metric exceeds the 20,000-row audit bound")
            fresh = sum(row[1] == "fresh" for row in rows)
            times = sorted({row[0] for row in rows if row[0] is not None})
            gaps = [(b - a) / 1e6 for a, b in zip(times, times[1:])]
            counts[metric] = {
                "samples": len(rows), "fresh": fresh,
                "fresh_percent": round(100 * fresh / len(snapshots), 3),
                "distinct_observations": len(times),
                "gaps_over_15_seconds": sum(g > 15 for g in gaps),
                "gaps_over_30_seconds": sum(g > 30 for g in gaps),
                "maximum_gap_seconds": max(gaps, default=None),
            }
    return {"trip_id": trip_id, "running_snapshots": len(snapshots), "metrics": counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trip", required=True, type=int)
    parser.add_argument("--database", type=Path, default=Path("/var/lib/van-telemetry/history.sqlite3"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.database, args.trip)
    output = args.output or REPO / "tmp/vehicle_data" / f"acc-freshness-trip-{args.trip}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
