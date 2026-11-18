"""The research worker's credit ledger: one durable file, one writer, display-only spend.

``/var/lib/vanresearch/api-credits.json`` holds one entry per credit launch (opened before the
process spawns, settled from its own result deltas), the latches that stop later credit launches
until the worker restarts, and the last fallback. The in-memory record is only replaced after a
write succeeded, so a failed write leaves memory and the last durable file agreeing. Entries are
never compacted: at the bound new credit launches are refused. The balance itself is the advisor's
view; nothing here decides billing from an estimate.
"""

import copy
from datetime import datetime, timedelta, timezone
import json
import threading

from lib.reports import atomic_json
from projects.vehicle_data.api_credits import latch_scope
from projects.vehicle_data.api_credits_contract import LEDGER, UNTIL

OPEN, SETTLED = "open", "settled"


class LedgerError(Exception):
    """A reservation or settlement that could not be made; ``reason`` is a contract billing reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty():
    return {"schema_version": LEDGER["schemaVersion"], "entries": [],
            "latches": {"account": None, "models": {}}, "last_fallback": None}


def _valid(record):
    if not isinstance(record, dict) or record.get("schema_version") != LEDGER["schemaVersion"]:
        return False
    latches = record.get("latches")
    return isinstance(record.get("entries"), list) and isinstance(latches, dict) \
        and isinstance(latches.get("models"), dict) and all(isinstance(e, dict) for e in record["entries"])


def durable_write(path, record):
    """File bytes and the directory entry both fsynced (``lib.reports.atomic_json`` with its directory option)."""
    atomic_json(path, record, prefix=".api-credits-", indent=None, sort_keys=True, fsync=True, fsync_dir=True)


class CreditLedger:
    def __init__(self, path, *, now=lambda: datetime.now(timezone.utc), write=durable_write):
        self.path, self.now, self._write = path, now, write
        self._lock = threading.Lock()
        self._record = _empty()
        self.available = True  # False when the file exists but cannot be trusted

    # --- start-up --------------------------------------------------------------------------------
    def start(self):
        """Load the file; settle every entry a crash left open as ``unknown``; clear every latch
        (they live until a restart); delete entries after ``until`` + the retention days."""
        with self._lock:
            try:
                loaded = json.loads(self.path.read_text())
            except FileNotFoundError:
                loaded = _empty()
            except (OSError, ValueError):
                loaded = None
            if loaded is None or not _valid(loaded):
                self.available = False
                return
            self.available = True
            record = copy.deepcopy(loaded)
            now = self.now()
            for entry in record["entries"]:
                if entry.get("state") == OPEN:
                    entry.update(state=SETTLED, unknown=True, settled_at=_stamp(now))
            record["latches"] = {"account": None, "models": {}}
            if now >= UNTIL + timedelta(days=LEDGER["retainDaysAfterUntil"]):
                record["entries"] = []
            self._commit(record, loaded)

    def _commit(self, record, previous):
        if record == previous and self.path.exists():
            self._record = record
            return
        try:
            self._write(self.path, record)
        except OSError:
            self.available = False
            return
        self._record = record

    # --- reservation and settlement --------------------------------------------------------------
    @property
    def full(self):
        with self._lock:
            return len(self._record["entries"]) >= LEDGER["maxEntries"]

    def reserve(self, attempt_id, model):
        """Durably open an entry before a credit process spawns, or raise ``LedgerError``."""
        with self._lock:
            if not self.available:
                raise LedgerError("ledger_unavailable")
            if len(self._record["entries"]) >= LEDGER["maxEntries"]:
                raise LedgerError("ledger_full")
            record = copy.deepcopy(self._record)
            record["entries"].append({"attempt_id": attempt_id, "model": model, "started_at": _stamp(self.now()),
                                      "settled_at": None, "known_usd": 0.0, "unknown": False, "state": OPEN})
            self._persist(record)

    def withdraw(self, attempt_id):
        """Remove an entry whose process never spawned. A failed write only leaves a harmless open
        entry, which the next start settles as unknown."""
        with self._lock:
            record = copy.deepcopy(self._record)
            record["entries"] = [e for e in record["entries"] if e.get("attempt_id") != attempt_id]
            try:
                self._persist(record)
            except LedgerError:
                pass

    def settle(self, attempt_id, known_usd, unknown):
        """Close an entry. On a write failure the entry stays open here and in the file."""
        with self._lock:
            record = copy.deepcopy(self._record)
            for entry in record["entries"]:
                if entry.get("attempt_id") == attempt_id and entry.get("state") == OPEN:
                    entry.update(state=SETTLED, settled_at=_stamp(self.now()), known_usd=known_usd, unknown=unknown)
                    break
            else:
                return
            self._persist(record)

    def _persist(self, record):
        try:
            self._write(self.path, record)
        except OSError:
            raise LedgerError("ledger_unavailable") from None
        self._record = record

    # --- latches ---------------------------------------------------------------------------------
    def latch(self, code, model):
        """Record a credit failure and the fallback. The latch applies at once even if the write fails."""
        with self._lock:
            record = copy.deepcopy(self._record)
            if latch_scope(code) == "account":
                record["latches"]["account"] = code
            else:
                record["latches"]["models"][model] = code
            record["last_fallback"] = {"at": _stamp(self.now()), "code": code, "model": model}
            try:
                self._write(self.path, record)
            except OSError:
                pass
            self._record = record

    def latches(self):
        with self._lock:
            latches = self._record["latches"]
            return {"account": latches["account"], "models": dict(latches["models"])}

    def snapshot(self):
        """The ledger part of ``GET /credits``: entries, latches, last fallback, whether it is full."""
        with self._lock:
            record = self._record
            keys = ("attempt_id", "model", "started_at", "settled_at", "known_usd", "unknown", "state")
            return {"entries": [{key: entry.get(key) for key in keys} for entry in record["entries"]],
                    "latches": {"account": record["latches"]["account"], "models": dict(record["latches"]["models"])},
                    "last_fallback": copy.deepcopy(record["last_fallback"]),
                    "ledger_full": len(record["entries"]) >= LEDGER["maxEntries"]}

