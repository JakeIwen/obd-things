"""One-time offline compaction of the telemetry history database.

Switches an existing database to ``auto_vacuum=INCREMENTAL`` (a new one gets it
at creation) and returns its freelist to the filesystem, by running
``PRAGMA auto_vacuum=INCREMENTAL; VACUUM`` inside SQLite.  VACUUM is
transactional: a crash, a full disk or a failed statement leaves the original
database intact, and any other connection that tries to write meanwhile gets
SQLITE_BUSY instead of silently losing commits.  The tool creates, renames and
deletes no files itself.

It never transmits on CAN and never touches services.  It refuses unless every
telemetry unit is inactive, no process it can inspect holds the file open, the
newest recorded vehicle state is not a recent running/ignition-on reading, and
the filesystem has room.  Run it as the database's owner (``pi``): the units'
processes run as pi, and a root run would leave root-owned ``-wal``/``-shm``
files the broker cannot open.  Some pi processes are non-dumpable (ssh-agent,
systemd's ``(sd-pam)``), so their ``/proc/<pid>/fd`` is root-owned; for those
alone it lists the descriptors through ``sudo -n`` and refuses if that fails.

Usage (units stopped, van parked)::

    python3 -m projects.vehicle_data.history.compaction            # read-only plan
    python3 -m projects.vehicle_data.history.compaction --apply
"""

from __future__ import annotations

import argparse
import errno
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
# Units that open the history database or start something that does.  vonstar
# and van-cop-can-wake never open it, so they are not listed.
SERVICE_UNITS = (
    "van-telemetry.service",
    "van-telemetry-web.service",
    "van-telemetry-web-tailscale.service",
    "van-telemetry-advisor.service",
    "van-cruise-harvest.service",
    "van-cruise-harvest.timer",
    "van-scan-harvest.service",
    "van-scan-harvest.timer",
    "van-drive-recorder.service",
    "van-dtc-batch.service",
    "van-dtc-batch.path",
)
# Only these systemctl answers prove a unit is not running; anything else
# (including unrecognized output or a failed call) refuses.
IDLE_UNIT_STATES = ("inactive", "failed")
# The snapshot rows record the broker's last vehicle_state; with the broker
# stopped there is nothing fresher, so a recent "on" reading blocks the run.
VEHICLE_ON_STATES = ("running", "ignition_on")
VEHICLE_STATE_MAX_AGE_SECONDS = 30 * 60
SPACE_MARGIN_BYTES = 256 * 1024 * 1024
AUTO_VACUUM_INCREMENTAL = 2


class CompactionRefused(RuntimeError):
    """A safety precondition failed; the database was not modified."""


class CompactionIncomplete(RuntimeError):
    """VACUUM committed but a later step failed: the database IS compacted.

    Verification (checks, row counts, final checkpoint, ownership) is
    unfinished, so inspect before restarting any unit.
    """


def systemd_unit_state(unit: str) -> str:
    """Return ``systemctl is-active`` output; raise when it cannot be trusted."""

    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompactionRefused(f"cannot ask systemd about {unit}: {exc}") from exc
    return result.stdout.strip() or f"no-answer(rc={result.returncode})"


# GNU find prints each descriptor's link target, NUL-terminated.
PRIVILEGED_FD_COMMAND = ("sudo", "-n", "/usr/bin/find")


def privileged_fd_targets(pid: int) -> set[str] | None:
    """Return ``pid``'s descriptor targets read through ``sudo -n``, or None.

    None means the listing could not be trusted (sudo refused, find failed, a
    descriptor or the process vanished mid-listing); the caller treats the
    process as unverifiable unless it has exited by then.
    """

    try:
        result = subprocess.run(
            [*PRIVILEGED_FD_COMMAND, f"/proc/{pid}/fd", "-mindepth", "1", "-maxdepth", "1", "-printf", "%l\\0"],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {os.fsdecode(item) for item in result.stdout.split(b"\0") if item}


def processes_holding(
    path: Path,
    *,
    privileged_targets: Callable[[int], set[str] | None] | None = None,
) -> tuple[list[int], list[int]]:
    """Return (holders, unverifiable) PIDs for ``path`` or its -wal/-shm.

    A process that exited mid-scan (ENOENT/ESRCH) is skipped.  One whose fds
    cannot be read (EACCES/EPERM) is retried through ``privileged_targets``
    when it belongs to our own user (non-dumpable pi processes); if that fails
    too it is returned so the caller can refuse.  Unreadable processes of other
    users are reported but not fatal (SQLite's own lock, taken with no busy
    timeout, still excludes them).
    """

    privileged_targets = privileged_targets or privileged_fd_targets

    proc = Path("/proc")
    if not proc.is_dir():
        return [], []
    targets = {str(path.resolve()) + suffix for suffix in ("", "-wal", "-shm")}
    holders: list[int] = []
    unverifiable: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        pid = int(entry.name)
        try:
            for fd in (entry / "fd").iterdir():
                try:
                    if os.readlink(fd) in targets:
                        holders.append(pid)
                        break
                except OSError as exc:
                    if exc.errno in (errno.ENOENT, errno.ESRCH):
                        continue  # that descriptor closed
                    raise
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESRCH):
                continue  # process exited
            if exc.errno in (errno.EACCES, errno.EPERM):
                try:
                    foreign = entry.stat().st_uid != os.geteuid()
                except OSError:
                    foreign = False
                if foreign:
                    unverifiable.append(-pid)
                    continue
                listed = privileged_targets(pid)
                if listed is None:
                    if entry.exists():
                        unverifiable.append(pid)
                    continue  # unlistable, or exited during the retry
                if listed & targets:
                    holders.append(pid)
                continue
            raise
    return holders, unverifiable


def _connect(database: Path, *, read_only: bool) -> sqlite3.Connection:
    uri = f"file:{database}?mode={'ro' if read_only else 'rw'}"
    # No busy timeout: any other holder of a conflicting lock refuses the run
    # at once instead of queueing behind it.  Autocommit so VACUUM may run.
    conn = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 0")
    return conn


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
    holders: Callable[[Path], tuple[list[int], list[int]]] | None = None,
    now_state: Callable[[Path], tuple[str | None, float | None]] | None = None,
) -> list[int]:
    """Refuse unless the database is quiet; return other users' unreadable PIDs."""

    unit_state = unit_state or systemd_unit_state
    holders = holders or processes_holding
    now_state = now_state or last_vehicle_state
    if not database.is_file():
        raise CompactionRefused(f"{database} is not a file")
    if database.stat().st_uid != os.geteuid():
        raise CompactionRefused(
            f"run as the database owner (uid {database.stat().st_uid}), not uid "
            f"{os.geteuid()}: files SQLite creates would be owned by the wrong user"
        )
    not_idle = {
        unit: state for unit in SERVICE_UNITS if (state := unit_state(unit)) not in IDLE_UNIT_STATES
    }
    if not_idle:
        raise CompactionRefused(
            "these units are not inactive (stop them first; this tool never does): "
            + ", ".join(f"{unit}={state}" for unit, state in not_idle.items())
        )
    pids, unverifiable = holders(database)
    if pids:
        raise CompactionRefused(f"processes {pids} have {database} open")
    own_unreadable = [pid for pid in unverifiable if pid > 0]
    if own_unreadable:
        raise CompactionRefused(
            f"cannot inspect descriptors of our own processes {own_unreadable}; "
            "cannot tell whether they hold the database"
        )
    state, age = now_state(database)
    if state in VEHICLE_ON_STATES and age is not None and age < VEHICLE_STATE_MAX_AGE_SECONDS:
        raise CompactionRefused(
            f"last recorded vehicle state was {state!r} {int(age)} s ago; the van "
            f"may be driving. Retry once it has been parked for "
            f"{VEHICLE_STATE_MAX_AGE_SECONDS // 60} minutes"
        )
    return [-pid for pid in unverifiable if pid < 0]


def required_free_bytes(live_bytes: int, wal_bytes: int) -> int:
    """VACUUM rebuilds the live data in a temp file (placed beside the database,
    see ``compact``) and writes it back through the WAL, so it needs about two
    copies of the live data plus the existing WAL and a margin."""

    return 2 * live_bytes + wal_bytes + SPACE_MARGIN_BYTES


def _verify(conn: sqlite3.Connection, quick: bool) -> str:
    check = "quick_check" if quick else "integrity_check"
    verdict = [row[0] for row in conn.execute(f"PRAGMA {check}")]
    if verdict != ["ok"]:
        raise CompactionRefused(f"{check} failed: {verdict[:5]}")
    return check


def compact(database: str | Path = DEFAULT_DATABASE, **options) -> dict[str, object]:
    """Compact ``database``; see ``_compact`` for the options.

    A failure before VACUUM raises ``CompactionRefused`` (or SQLite's own
    error) and the database is unchanged.  A failure after VACUUM has committed
    raises ``CompactionIncomplete``: the database is already compacted.
    """

    stage = {"vacuumed": False}
    try:
        return _compact(database, stage=stage, **options)
    except (CompactionRefused, sqlite3.Error, OSError) as exc:
        if stage["vacuumed"]:
            raise CompactionIncomplete(f"{type(exc).__name__}: {exc}") from exc
        raise


def _compact(
    database: str | Path = DEFAULT_DATABASE,
    *,
    stage: dict[str, bool],
    apply: bool = False,
    quick: bool = False,
    unit_state: Callable[[str], str] | None = None,
    holders: Callable[[Path], tuple[list[int], list[int]]] | None = None,
    now_state: Callable[[Path], tuple[str | None, float | None]] | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, object]:
    database = Path(database).resolve()
    foreign_unreadable = check_preconditions(
        database, unit_state=unit_state, holders=holders, now_state=now_state
    )
    if foreign_unreadable:
        log(
            f"note: descriptors of {len(foreign_unreadable)} other-user processes are "
            "unreadable without sudo; SQLite's lock (no busy timeout) still excludes them"
        )
    wal = database.with_name(database.name + "-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0

    conn = _connect(database, read_only=not apply)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        mode_before = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        live_bytes = (pages - free_pages) * page_size
        needed = required_free_bytes(live_bytes, wal_bytes)
        free = shutil.disk_usage(database.parent).free
        plan = {
            "database": str(database),
            "pages": pages,
            "free_pages": free_pages,
            "page_size": page_size,
            "wal_bytes": wal_bytes,
            "auto_vacuum_before": mode_before,
            "required_free_bytes": needed,
            "free_bytes": free,
        }
        log(f"plan: {json.dumps(plan)}")
        if free < needed:
            raise CompactionRefused(
                f"need {needed} bytes free beside {database} (VACUUM temp copy, "
                f"WAL and margin), have {free}"
            )
        if not apply:
            log(
                "plan only: opened read-only, no checkpoint, nothing changed; "
                "re-run with --apply"
            )
            return {"applied": False, **plan}

        started = time.monotonic()
        # Keep VACUUM's temp copy on the database's filesystem, the one just
        # checked, rather than a possibly small tmpfs.
        os.environ["SQLITE_TMPDIR"] = str(database.parent)
        before = table_counts(conn)
        log(f"row counts before: {json.dumps(before)}")
        _verify(conn, quick)
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("VACUUM")
        stage["vacuumed"] = True  # committed: later failures leave it compacted
        mode_after = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        if mode_after != AUTO_VACUUM_INCREMENTAL:
            raise CompactionRefused(f"auto_vacuum is {mode_after} after VACUUM, expected 2")
        check = _verify(conn, quick)
        after = table_counts(conn)
        if after != before:
            diff = {
                k: (before.get(k), after.get(k))
                for k in set(before) | set(after)
                if before.get(k) != after.get(k)
            }
            raise CompactionRefused(
                f"row counts differ (before, after): {diff}; a writer appeared "
                "mid-run or the vacuum misbehaved: do not restart units, inspect"
            )
        busy, _log_frames, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise CompactionRefused("final TRUNCATE checkpoint was blocked by another connection")
        free_after = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        conn.close()

    # Whatever SQLite left beside the database must still belong to its owner,
    # or the broker could not open it after the restart.
    owner = database.stat().st_uid
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists() and sidecar.stat().st_uid != owner:
            raise CompactionRefused(f"{sidecar} is not owned by uid {owner}; fix before restart")

    result = {
        "applied": True,
        **plan,
        "auto_vacuum_after": mode_after,
        "free_pages_after": free_after,
        "check": check,
        "row_counts": after,
        "new_bytes": database.stat().st_size,
        "seconds": round(time.monotonic() - started, 1),
    }
    log(f"done: {json.dumps({k: v for k, v in result.items() if k != 'row_counts'})}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DATABASE)
    parser.add_argument("--apply", action="store_true", help="do the compaction (default: read-only plan)")
    parser.add_argument("--quick", action="store_true", help="quick_check instead of the full integrity_check")
    args = parser.parse_args(argv)
    try:
        compact(args.db, apply=args.apply, quick=args.quick)
    except CompactionRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except CompactionIncomplete as exc:
        print(
            f"applied; verification/checkpoint incomplete: {exc}. The database "
            "was compacted (VACUUM committed); inspect it before restarting units",
            file=sys.stderr,
        )
        return 3
    except sqlite3.OperationalError as exc:
        print(f"refused: SQLite could not proceed ({exc}); the database was not changed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
