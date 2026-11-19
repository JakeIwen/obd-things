"""The durable rolling 24-hour count of research launches (the owner-facing daily limit).

Written before every launch request, so neither a restart nor evicting runs lowers the count.
Every entry counts (a fallback relaunch after a credit failure included). A launch the worker definitively refused (409; under the send-once rule that
attempt is never sent again) is removed at once, so the file never holds more than the limit and
no counted entry is ever dropped to make room. An unreadable ledger is never overwritten.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading

from projects.vehicle_data.dtc_research_contract import LIMITS, ResearchError
from lib.reports import atomic_json

FILE_NAME = "admissions.json"
KINDS = ("initial", "resume", "replay", "fallback")


class Ledger:
    def __init__(self, path, *, limit=LIMITS["dailyLaunches"], window=LIMITS["dailyWindowSeconds"],
                 clock=lambda: datetime.now(timezone.utc)):
        self.path, self.limit, self.window, self.clock = Path(path), limit, window, clock
        self.lock = threading.Lock()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise ResearchError("storage_error", "The research launch ledger on the Pi cannot be read") from None
        launches = raw.get("launches") if isinstance(raw, dict) else None
        if not isinstance(launches, list):
            raise ResearchError("storage_error", "The research launch ledger on the Pi cannot be read")
        return launches

    def _live(self, launches):
        cutoff = self.clock() - timedelta(seconds=self.window)
        return [entry for entry in launches if datetime.fromisoformat(entry["at"]) > cutoff]

    def _write(self, launches):
        try:
            # Directory-durable: a counted fallback admission must survive a power cut (the daily cap).
            atomic_json(self.path, {"version": 1, "launches": launches}, prefix=f".{self.path.name}.", indent=None,
                        sort_keys=True, fsync=True, fsync_dir=True)
        except OSError:
            raise ResearchError("storage_error", "The Pi could not record the launch; nothing was started") from None

    def admit(self, run_id, attempt_id, kind):
        """Record a launch about to be requested, or raise ``daily_limit``."""
        if kind not in KINDS:
            raise ValueError(kind)
        with self.lock:
            launches = self._live(self._load())
            if len(launches) >= self.limit:
                raise ResearchError("daily_limit", f"The daily research limit ({self.limit} launches) has been reached")
            launches.append({"at": self.clock().isoformat(), "run_id": run_id, "attempt_id": attempt_id, "kind": kind})
            self._write(launches)

    def refused(self, attempt_id):
        """Remove a definitively refused launch; if that write fails the entry stays and counts."""
        with self.lock:
            try:
                launches = self._live(self._load())
                self._write([entry for entry in launches if entry["attempt_id"] != attempt_id])
            except ResearchError:
                pass

    def count(self):
        with self.lock:
            return len(self._live(self._load()))
