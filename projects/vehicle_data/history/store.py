"""Connection and lock ownership for the SQLite historian."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import (
    HistorianConfig,
)
from .schema import SchemaMixin


class HistorianStore(SchemaMixin):
    """Connection and lock ownership for the SQLite historian."""

    def __init__(
        self,
        database: str | Path,
        *,
        config: HistorianConfig | None = None,
    ):
        self.database = str(database)
        self.config = config or HistorianConfig()
        if self.database != ":memory:":
            Path(self.database).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self._baseline_inputs = {}
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.database,
            timeout=10.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        if self.database != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()
        from projects.vehicle_data.event_history import SCHEMA as event_schema
        with self._lock, self._conn:
            self._conn.executescript(event_schema)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _set_meta_locked(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO historian_meta(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def _meta_locked(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM historian_meta WHERE key=?",
            (key,),
        ).fetchone()
        return None if row is None else str(row["value"])
