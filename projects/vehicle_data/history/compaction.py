"""One-time offline compaction of the telemetry history database.

Switches an existing database to ``auto_vacuum=INCREMENTAL`` (a new one gets it
at creation) and returns its freelist to the filesystem.  It never transmits on
CAN and never touches the live service: it refuses while any telemetry unit is
active, while another process holds the file open, or while the last recorded
vehicle state says the van may be running.

Method: ``VACUUM INTO`` a temporary file beside the database, verify it, then
swap by rename.  The original stays byte-for-byte intact until the final
renames, so a crash, a full disk or a failed check leaves the live database
untouched; an in-place ``VACUUM`` would need the same scratch space plus a
second copy in the WAL and has no such fallback.  The old file is kept as
``<db>.pre-compact`` until ``--delete-old`` (or by hand).

Usage (service stopped)::

    python3 -m projects.vehicle_data.history.compaction            # plan only
    python3 -m projects.vehicle_data.history.compaction --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_DATABASE = "/var/lib/van-telemetry/history.sqlite3"
# Units that open the history database or restart the broker that owns it.
SERVICE_UNITS = (
    "van-telemetry.service",
    "van-telemetry-web.service",
    "van-telemetry-web-tailscale.service",
    "van-telemetry-advisor.service",
    "van-scan-harvest.service",
    "van-cruise-harvest.service",
    "van-drive-recorder.service",
    "van-dtc-batch.service",
)
BUSY_UNIT_STATES = ("active", "activating", "reloading", "deactivating")
# The snapshot rows record the broker's last vehicle_state; with the broker
# stopped there is nothing fresher, so a recent "on" reading blocks the run.
VEHICLE_ON_STATES = ("running", "ignition_on")
VEHICLE_STATE_MAX_AGE_SECONDS = 30 * 60
SPACE_MARGIN_BYTES = 256 * 1024 * 1024
BACKUP_SUFFIX = ".pre-compact"
TEMP_SUFFIX = ".compact-tmp"
AUTO_VACUUM_INCREMENTAL = 2


class CompactionRefused(RuntimeError):
    """A safety precondition failed; nothing was changed."""


def systemd_unit_state(unit: str) -> str:
    """Return systemd's ActiveState for a unit ('inactive' when unknown)."""

    result = subprocess.run(
        ["systemctl", "show", "-p", "ActiveState", "--value", unit],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip() or "inactive"


def processes_holding(path: Path) -> list[int]:
    """PIDs (other than ours) with ``path`` or its -wal/-shm open (Linux /proc)."""

    proc = Path("/proc")
    if not proc.is_dir():
        return []
    targets = {str(path.resolve()) + suffix for suffix in ("", "-wal", "-shm")}
    holders: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            for fd in (entry / "fd").iterdir():
                if os.readlink(fd) in targets:
                    holders.append(int(entry.name))
                    break
        except OSError:
            continue  # exited, or not ours to inspect
    return holders


def _connect(database: Path, *, read_only: bool = False) -> sqlite3.Connection:
    uri = f"file:{database}?mode={'ro' if read_only else 'rw'}"
    return sqlite3.connect(uri, uri=True, timeout=10.0)


def last_vehicle_state(database: Path) -> tuple[str | None, float | None]:
    """Newest recorded (vehicle_state, age in seconds) from the snapshots table."""

    conn = _connect(database, read_only=True)
    try:
        row = conn.execute(
            "SELECT vehicle_state, vehicle_running, captured_us FROM snapshots "
            "ORDER BY captured_us DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None  # no snapshots table: not a historian database
    finally:
        conn.close()
    if row is None:
        return None, None
    state = "running" if row[1] == 1 else row[0]
    return state, max(0.0, time.time() - row[2] / 1_000_000)


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}


def check_preconditions(
    database: Path,
    *,
    unit_state: Callable[[str], str] | None = None,
    holders: Callable[[Path], list[int]] | None = None,
    now_state: Callable[[Path], tuple[str | None, float | None]] | None = None,
) -> None:
    unit_state = unit_state or systemd_unit_state
    holders = holders or processes_holding
    now_state = now_state or last_vehicle_state
    if not database.is_file():
        raise CompactionRefused(f"{database} is not a file")
    busy = {unit: state for unit in SERVICE_UNITS if (state := unit_state(unit)) in BUSY_UNIT_STATES}
    if busy:
        raise CompactionRefused(
            "stop these units first (the parent stops them; this tool never does): "
            + ", ".join(f"{unit}={state}" for unit, state in busy.items())
        )
    pids = holders(database)
    if pids:
        raise CompactionRefused(f"processes {pids} have {database} open")
    state, age = now_state(database)
    if state in VEHICLE_ON_STATES and age is not None and age < VEHICLE_STATE_MAX_AGE_SECONDS:
        raise CompactionRefused(
            f"last recorded vehicle state was {state!r} {int(age)} s ago; the van "
            f"may be driving. Retry once it has been parked for "
            f"{VEHICLE_STATE_MAX_AGE_SECONDS // 60} minutes"
        )
    for leftover in (
        database.with_name(database.name + TEMP_SUFFIX),
        database.with_name(database.name + BACKUP_SUFFIX),
    ):
        if leftover.exists():
            raise CompactionRefused(f"{leftover} exists; inspect and remove it first")


def check_space(database: Path, live_bytes: int) -> int:
    """Require room for the compacted copy beside the kept original."""

    needed = live_bytes + SPACE_MARGIN_BYTES
    free = shutil.disk_usage(database.parent).free
    if free < needed:
        raise CompactionRefused(
            f"need {needed} bytes free beside {database} (compacted copy plus "
            f"margin), have {free}"
        )
    return needed


def compact(
    database: str | Path = DEFAULT_DATABASE,
    *,
    apply: bool = False,
    delete_old: bool = False,
    quick: bool = False,
    unit_state: Callable[[str], str] | None = None,
    holders: Callable[[Path], list[int]] | None = None,
    now_state: Callable[[Path], tuple[str | None, float | None]] | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, object]:
    database = Path(database).resolve()
    check_preconditions(database, unit_state=unit_state, holders=holders, now_state=now_state)
    temp = database.with_name(database.name + TEMP_SUFFIX)
    backup = database.with_name(database.name + BACKUP_SUFFIX)

    conn = _connect(database)
    try:
        # Fold the WAL into the main file so the swap cannot orphan frames.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        mode_before = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        plan = {
            "database": str(database),
            "pages": pages,
            "free_pages": free_pages,
            "page_size": page_size,
            "auto_vacuum_before": mode_before,
            "expected_compacted_bytes": (pages - free_pages) * page_size,
        }
        needed = check_space(database, (pages - free_pages) * page_size)
        plan["required_free_bytes"] = needed
        log(f"plan: {json.dumps(plan)}")
        if not apply:
            log("dry run: nothing changed; re-run with --apply")
            return {"applied": False, **plan}

        started = time.monotonic()
        before = table_counts(conn)
        log(f"row counts before: {json.dumps(before)}")
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        try:
            conn.execute("VACUUM INTO ?", (str(temp),))
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    finally:
        conn.close()

    try:
        new = _connect(temp)
        try:
            mode_after = new.execute("PRAGMA auto_vacuum").fetchone()[0]
            if mode_after != AUTO_VACUUM_INCREMENTAL:
                raise CompactionRefused(f"compacted copy has auto_vacuum={mode_after}, expected 2")
            check = "quick_check" if quick else "integrity_check"
            verdict = [row[0] for row in new.execute(f"PRAGMA {check}")]
            if verdict != ["ok"]:
                raise CompactionRefused(f"{check} failed: {verdict[:5]}")
            after = table_counts(new)
            fk = new.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise CompactionRefused(f"foreign_key_check found {len(fk)} violations")
            if after != before:
                diff = {k: (before.get(k), after.get(k)) for k in set(before) | set(after) if before.get(k) != after.get(k)}
                raise CompactionRefused(f"row counts differ (before, after): {diff}")
            # Leave the copy in rollback-journal form; the broker re-enables WAL.
            new.execute("PRAGMA journal_mode = DELETE").fetchall()
        finally:
            new.close()
        shutil.copymode(database, temp)
        try:
            os.chown(temp, database.stat().st_uid, database.stat().st_gid)
        except PermissionError:
            pass
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        wal = database.with_name(database.name + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise CompactionRefused(f"{wal} still holds frames; aborting swap")
        os.rename(database, backup)
        os.rename(temp, database)
        for sidecar in ("-wal", "-shm"):
            database.with_name(database.name + sidecar).unlink(missing_ok=True)
        dir_fd = os.open(database.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise

    result = {
        "applied": True,
        **plan,
        "auto_vacuum_after": mode_after,
        "check": check,
        "row_counts": after,
        "new_bytes": database.stat().st_size,
        "old_kept_as": str(backup),
        "seconds": round(time.monotonic() - started, 1),
    }
    if delete_old:
        backup.unlink()
        result["old_kept_as"] = None
    log(f"done: {json.dumps({k: v for k, v in result.items() if k != 'row_counts'})}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DATABASE)
    parser.add_argument("--apply", action="store_true", help="do the compaction (default: plan only)")
    parser.add_argument("--delete-old", action="store_true", help="remove <db>.pre-compact after a verified swap")
    parser.add_argument("--quick", action="store_true", help="quick_check instead of the full integrity_check")
    args = parser.parse_args(argv)
    try:
        compact(args.db, apply=args.apply, delete_old=args.delete_old, quick=args.quick)
    except CompactionRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
