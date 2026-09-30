#!/usr/bin/env python3
"""Plan/queue file-only cruise summaries for completed trips via named compute.

The Pi reads only small recorder metadata and completed-trip rows. It never
decompresses captures. Default is plan-only; --execute submits/imports jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from projects.vehicle_data.van_scan_harvest import bus_capture_ended, completed_chunks

TASK = "cruise-summary-reduce"
COMPUTE = "/home/pi/van_compute/scripts/pi_compute.py"
DEFAULT_ROOT = Path("/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive")
DEFAULT_OUT = REPO / "tmp/vehicle_data/cruise_summaries"


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temp.replace(path)


def read_trips(database, trip_id=None):
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as con:
        con.execute("PRAGMA query_only=ON")
        con.row_factory = sqlite3.Row
        where = " AND id=?" if trip_id is not None else ""
        return [dict(r) for r in con.execute(
            "SELECT id,started_us,ended_us,started_at,ended_at FROM trips "
            "WHERE ended_us IS NOT NULL" + where + " ORDER BY id DESC LIMIT 20",
            (trip_id,) if trip_id is not None else (),
        )]


def recorded_chunks(root):
    chunks = []
    for campaign in sorted(root.glob("broker-drive-*")):
        bus = campaign / "c-can"
        if not bus_capture_ended(bus):
            continue
        try:
            route = json.loads((campaign / "run.json").read_text())["routes"]["c-can"]
            entries = [json.loads(line) for line in (bus / "manifest.jsonl").read_text().splitlines()]
            end = next(e for e in reversed(entries) if e.get("type") == "capture_end")
            sizes = {Path(e["streams"]["full"]["path"]).name: e["streams"]["full"].get("compressed_bytes")
                     for e in entries if e.get("type") == "chunk" and e.get("complete") is True
                     and e.get("streams", {}).get("full", {}).get("complete") is True}
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            continue
        if (route.get("role") != "c-can" or route.get("pair") != "6/14"
                or route.get("bitrate") != 500000 or not re.fullmatch(r"can\d+", str(route.get("channel")))):
            continue
        for row in completed_chunks(bus):
            if type(sizes.get(row["name"])) is not int or sizes[row["name"]] != row["size"]:
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256"))):
                continue
            if row["first_frame_timestamp"] is None or row["last_frame_timestamp"] is None:
                continue
            chunks.append({
                "campaign": campaign.name, "role": "c-can", "stream": "full",
                "channel": route["channel"], "sequence": row["sequence"],
                "path": str(row["path"].resolve()), "size": row["size"],
                "sha256": row["sha256"], "first": row["first_frame_timestamp"],
                "last": row["last_frame_timestamp"],
                "recording_complete": end.get("success") is True and end.get("detected_socket_drops") == 0,
                "socket_drops": end.get("detected_socket_drops"),
                "end_reason": end.get("reason"),
            })
    return sorted(chunks, key=lambda c: c["first"])


def manifest_for(trip, chunks):
    selected = [c for c in chunks if c["last"] * 1e6 >= trip["started_us"]
                and c["first"] * 1e6 <= trip["ended_us"]]
    return {"schema_version": 1, "trip": trip, "chunks": selected}


def compute(*args):
    done = subprocess.run([COMPUTE, *args], check=True, capture_output=True, text=True, timeout=60)
    return json.loads(done.stdout)


def harvest(database, root=DEFAULT_ROOT, out=DEFAULT_OUT, *, trip_id=None, execute=False, runner=compute):
    trips = read_trips(database, trip_id)
    chunks = recorded_chunks(root)
    state_path = out / "jobs.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if execute and TASK not in {t["name"] for t in runner("tasks")["tasks"]}:
        raise RuntimeError(f"named task {TASK} is not registered; owner approval is required before adding it")
    reports = []
    for trip in trips:
        manifest = manifest_for(trip, chunks)
        key = str(trip["id"])
        if not manifest["chunks"]:
            reports.append({"trip": trip["id"], "state": "no_completed_recording"})
            continue
        if len(manifest["chunks"]) > 127:
            reports.append({"trip": trip["id"], "state": "exceeds_127_chunk_limit"})
            continue
        path = out / "manifests" / f"trip-{key}.json"
        # Only write when changed: inputs are immutable while a job is queued.
        serialized = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        entry = state.get(key, {})
        if entry.get("job_id") and entry.get("manifest_sha256") != digest:
            reports.append({"trip": trip["id"], "state": "manifest_changed_review_required"})
            continue
        if not path.exists() or path.read_text() != serialized:
            atomic_json(path, manifest)
        command = ["run", TASK, "--input", str(path)]
        for chunk in manifest["chunks"]:
            command += ["--input", chunk["path"]]
        if not execute:
            reports.append({"trip": trip["id"], "state": "planned", "command": [COMPUTE, *command]})
            continue
        if not entry.get("job_id"):
            result = runner(*command)
            entry = {"job_id": result["id"], "manifest_sha256": digest}
            state[key] = entry
            atomic_json(state_path, state)
        status = runner("status", entry["job_id"])
        if status["state"] == "done" and status.get("exit_code") == 0:
            result = runner("result", entry["job_id"], "cruise-summary.json")
            if result.get("manifest_sha256") != digest or result.get("trip") != trip or result.get("schema_version") != 1:
                raise ValueError("computed cruise summary does not match the exact planned trip")
            atomic_json(out / "trips" / f"{key}.json", result)
            reports.append({"trip": trip["id"], "state": "imported", "coverage_percent": result["coverage_percent"]})
        else:
            reports.append({"trip": trip["id"], "state": status["state"], "job_id": entry["job_id"]})
    return reports


def attach_summaries(history, out=DEFAULT_OUT):
    """Read at most the five summaries History will show, during cache refresh."""
    for trip in history.get("recent_trips", [])[:5]:
        if type(trip.get("id")) is not int:
            continue
        path = out / "trips" / f"{trip['id']}.json"
        try:
            if path.stat().st_size > 256 * 1024:
                continue
            result = json.loads(path.read_text())
            recorded = result.get("trip", {})
            if (result.get("schema_version") == 1 and recorded.get("id") == trip["id"]
                    and recorded.get("started_at") == trip.get("started_at")
                    and recorded.get("ended_at") == trip.get("ended_at")):
                trip["cruise_summary"] = {k: result[k] for k in (
                    "coverage_percent", "unknown_seconds", "engaged_seconds", "override_seconds",
                    "following_seconds", "cancels", "brake_association_verified", "comparison", "faults",
                    "recording_complete")}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("/var/lib/van-telemetry/history.sqlite3"))
    parser.add_argument("--capture-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--trip", type=int)
    parser.add_argument("--execute", action="store_true", help="submit/import through the named compute task; no CAN")
    args = parser.parse_args()
    print(json.dumps(harvest(args.database, args.capture_root, args.out_dir,
                            trip_id=args.trip, execute=args.execute), indent=2))


if __name__ == "__main__":
    main()
